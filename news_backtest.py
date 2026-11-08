"""Backtest the news strategy against real past headlines and prices.

Doesn't touch your account or place any orders. It downloads past 5-minute price bars and
past Alpaca headlines, then steps through market hours and asks the bot's own rules what
they would have done at each check:

  * news.decide_news       - buy on positive news that isn't priced in, sell on negative news
  * news.same_story_losers - one article driving several buys counts as one bet
  * the 5% stop-loss, plus exits.check (3-day time limit + trailing stop)

Two reports:
  1. Trades: count, win rate, profit/loss, compared with just buying and holding.
  2. Scorer check: after a headline scores positive (or negative), how often did the stock
     go up (or down) over the next trading day? Compared with how often it went up anyway.

Run:  python news_backtest.py --days 90
"""

import argparse
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import exits
import news

NY = ZoneInfo("America/New_York")
BAR_MINUTES = 5
# The live run-up check looks at the last NEWS_PRICE_WINDOW_BARS one-minute bars.
RUNUP_BARS = max(2, config.NEWS_PRICE_WINDOW_BARS // BAR_MINUTES)
BARS_PER_DAY = 78  # 6.5 market hours of 5-minute bars
# Same stakes the live workflow uses (.github/workflows), used when STAKES isn't set locally.
LIVE_STAKES = {"QQQ": 317.29, "AAPL": 199.08, "NVDA": 214.8, "GOOGL": 196.82}


# ---------------- pure simulation (no network, unit-tested) ----------------

def _window(articles, t, hours):
    """Articles published in (t - hours, t]."""
    lo = t - hours * 3600
    return [a for a in articles if lo < a["t"] <= t]


def simulate(bars, articles, stakes, step_bars=3):
    """bars: {symbol: [(epoch, close), ...]} market-hours bars, oldest first, same timestamps
    for every symbol is not required. articles: {symbol: [article dicts as broker.recent_news]}.
    Checks every `step_bars` bars. Returns a list of closed trades:
    {symbol, buy_t, buy_price, sell_t, sell_price, reason, pnl}."""
    times = sorted({t for rows in bars.values() for t, _ in rows})
    idx = {s: {t: i for i, (t, _) in enumerate(rows)} for s, rows in bars.items()}
    open_pos, trades = {}, []

    for k, t in enumerate(times):
        if k % step_bars:
            continue
        buys = {}
        for sym, rows in bars.items():
            i = idx[sym].get(t)
            if i is None:
                continue
            price = rows[i][1]
            closes = [c for _, c in rows[max(0, i - RUNUP_BARS + 1):i + 1]]
            arts = _window(articles.get(sym, []), t, config.NEWS_LOOKBACK_HOURS)
            pos = open_pos.get(sym)

            if pos:
                reason = None
                if price < pos["buy_price"] * (1 - config.STOP_LOSS_PCT / 100):
                    reason = "stop-loss"
                else:
                    action, _ = news.decide_news(closes, arts, True, now=t)
                    if action == "sell":
                        reason = "negative-news"
                    else:
                        reason, pos["peak"] = exits.check(pos["buy_price"], price, pos["peak"],
                                                          pos["buy_t"], t, news.sentiment(arts, t)[0])
                if reason:
                    trades.append(_close(sym, pos, t, price, reason))
                    del open_pos[sym]
                continue

            action, _ = news.decide_news(closes, arts, False, now=t)
            if action == "buy":
                buys[sym] = (price, arts)

        if buys:
            cands = {s: (news.sentiment(a, t)[0], news.top_article_id(a, t)) for s, (_, a) in buys.items()}
            skip = news.same_story_losers(cands)
            for sym, (price, _) in buys.items():
                if sym in skip:
                    continue
                open_pos[sym] = {"buy_t": t, "buy_price": price, "peak": price,
                                 "qty": stakes.get(sym, 100.0) / price}

    for sym, pos in open_pos.items():  # mark anything still open at the last price
        t, price = bars[sym][-1]
        trades.append(_close(sym, pos, t, price, "still-open"))
    return trades


def _close(sym, pos, t, price, reason):
    return {"symbol": sym, "buy_t": pos["buy_t"], "buy_price": pos["buy_price"], "sell_t": t,
            "sell_price": price, "reason": reason, "pnl": (price - pos["buy_price"]) * pos["qty"]}


def summarize(trades, bars, stakes):
    wins = [t for t in trades if t["pnl"] > 0]
    pnl = sum(t["pnl"] for t in trades)
    hold = sum(stakes.get(s, 100.0) * (rows[-1][1] / rows[0][1] - 1) for s, rows in bars.items() if rows)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"trades": len(trades), "wins": len(wins),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "pnl": pnl, "hold_pnl": hold, "budget": sum(stakes.get(s, 100.0) for s in bars),
            "reasons": reasons}


def scorer_check(bars, articles, threshold=None, horizon_bars=BARS_PER_DAY):
    """For each stock-day with articles scoring at least `threshold` (either sign), compare the
    price at the first bar after the first such article with the price `horizon_bars` later.
    The day's direction is the sign of its summed scores.
    Returns {'pos': (n, n_up), 'neg': (n, n_down), 'base': (n, n_up)}; base is every
    bar-to-bar+horizon pair, i.e. how often the stock went up anyway."""
    threshold = config.NEWS_BUY_THRESHOLD if threshold is None else threshold
    out = {"pos": [0, 0], "neg": [0, 0], "base": [0, 0]}
    for sym, rows in bars.items():
        ts = [t for t, _ in rows]
        for i in range(0, len(rows) - horizon_bars, BARS_PER_DAY // 6):
            out["base"][0] += 1
            out["base"][1] += rows[i + horizon_bars][1] > rows[i][1]
        # One signal per stock per day: ten stories on one big day are one event, not ten.
        days, seen = {}, set()
        for a in articles.get(sym, []):
            if a.get("id") is not None and a["id"] in seen:
                continue
            seen.add(a.get("id"))
            s = news.score_text(a.get("headline", "")) * 0.7 + news.score_text(a.get("summary", "")) * 0.3
            if abs(s) < threshold:
                continue
            day = days.setdefault(datetime.fromtimestamp(a["t"], NY).date(), [0.0, a["t"]])
            day[0] += s
        for total, t in days.values():
            i = _first_at_or_after(ts, t)
            if total == 0 or i is None or i + horizon_bars >= len(rows):
                continue
            up = rows[i + horizon_bars][1] > rows[i][1]
            key = "pos" if total > 0 else "neg"
            out[key][0] += 1
            out[key][1] += up if total > 0 else not up
    return {k: tuple(v) for k, v in out.items()}


def _first_at_or_after(ts, t):
    lo, hi = 0, len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(ts) else None


def binom_p_two_sided(n, k, p=0.5):
    """Normal-approximation p-value: could k-out-of-n be luck if the true rate is p?"""
    if n == 0:
        return 1.0
    z = (k - n * p) / math.sqrt(n * p * (1 - p))
    return math.erfc(abs(z) / math.sqrt(2))


# ---------------- data download ----------------

def fetch(symbols, days):
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import NewsClient, StockHistoricalDataClient
    from alpaca.data.requests import NewsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    end = datetime.now(timezone.utc) - timedelta(minutes=20)  # free data can't be the last 15 min
    start = end - timedelta(days=days)
    data = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
    news_client = NewsClient(config.API_KEY, config.SECRET_KEY)
    bars, articles = {}, {}
    for sym in symbols:
        req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
                               start=start, end=end, feed=DataFeed.IEX)
        rows = []
        for b in data.get_stock_bars(req).data.get(sym, []):
            ny = b.timestamp.astimezone(NY)
            mins = ny.hour * 60 + ny.minute
            if 9 * 60 + 30 <= mins < 16 * 60:  # regular market hours only
                rows.append((b.timestamp.timestamp() + BAR_MINUTES * 60, float(b.close)))  # bar END time
        bars[sym] = rows
        # Same lookback padding as the live bot, so the first checks have news to look at.
        nreq = NewsRequest(symbols=sym, start=start - timedelta(hours=config.NEWS_LOOKBACK_HOURS),
                           end=end, limit=None)
        raw = news_client.get_news(nreq).data.get("news", [])
        articles[sym] = sorted(({"id": getattr(a, "id", None), "t": a.created_at.timestamp(),
                                 "headline": a.headline or "", "summary": a.summary or ""} for a in raw),
                               key=lambda a: a["t"])
        print(f"  {sym}: {len(rows)} price bars, {len(articles[sym])} headlines")
    return bars, articles


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--step", type=int, default=3, help="check every N five-minute bars (3 = every 15 min)")
    args = p.parse_args()

    stakes = config.STAKES if config.STRATEGY == "news" else LIVE_STAKES
    print(f"Downloading {args.days} days of prices and headlines for {', '.join(stakes)}...")
    bars, articles = fetch(list(stakes), args.days)

    trades = simulate(bars, articles, stakes, step_bars=args.step)
    s = summarize(trades, bars, stakes)
    print(f"\nNews strategy, last {args.days} days (${s['budget']:.0f} split across {len(stakes)} symbols)")
    print(f"  trades: {s['trades']}   wins: {s['wins']} ({s['win_rate']:.0%})   by reason: {s['reasons']}")
    print(f"  bot P/L:        ${s['pnl']:+.2f}")
    print(f"  buy-and-hold:   ${s['hold_pnl']:+.2f}")
    for t in trades:
        bt = datetime.fromtimestamp(t["buy_t"], NY).strftime("%b %d %H:%M")
        st = datetime.fromtimestamp(t["sell_t"], NY).strftime("%b %d %H:%M")
        print(f"    {t['symbol']:5} {bt} @ {t['buy_price']:.2f} -> {st} @ {t['sell_price']:.2f}  "
              f"{t['pnl']:+7.2f}  {t['reason']}")

    c = scorer_check(bars, articles)
    print(f"\nScorer check (headlines scoring at least ±{config.NEWS_BUY_THRESHOLD}, next trading day):")
    n, k = c["base"]
    base = k / n if n else 0.5
    print(f"  any time:          up {base:.0%} of the time ({n} samples)")
    for key, word in (("pos", "up"), ("neg", "down")):
        n, k = c[key]
        if not n:
            print(f"  {key} days:     none")
            continue
        expect = base if key == "pos" else 1 - base
        print(f"  {key} days:     {word} {k / n:.0%} of the time ({n} stock-days; "
              f"would be {expect:.0%} by chance, p={binom_p_two_sided(n, k, expect):.2f})")


if __name__ == "__main__":
    main()
