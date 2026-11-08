"""News-driven signal + Kelly position sizing for the 'news' strategy.

Pipeline, per symbol, every cycle:
  1. Pull the last NEWS_LOOKBACK_HOURS of headlines for the symbol (Alpaca News API).
  2. Score each headline with a transparent finance keyword lexicon (-1..+1),
     weighted by recency (exponential decay, NEWS_HALF_LIFE_HOURS).
  3. "Mispricing" check: strong positive news but price hasn't already run up
     more than NEWS_MAX_RUNUP_PCT over the recent window -> news not priced in yet -> buy.
     If price already ran, it's skipped as "priced in".
  4. Exit when the news flow turns negative (stop-loss in bot.py still applies).
  5. Size the buy with fractional Kelly, using the bot's OWN realized win rate
     and payoff (shrunk toward a conservative prior until there's enough history).
     If Kelly says there's no edge, it doesn't bet.

Everything here is pure functions (no network), so it's unit-testable offline.
"""

import math
import re
import time

import config

# Phrase -> score. Multi-word phrases are matched before single words.
_LEXICON = {
    # strongly positive
    "beats estimates": 1.0, "beat estimates": 1.0, "tops estimates": 1.0, "raises guidance": 1.0,
    "raised guidance": 1.0, "record revenue": 0.9, "record quarter": 0.9, "upgrade": 0.8,
    "upgraded": 0.8, "buyback": 0.6, "share repurchase": 0.6, "approval": 0.7, "approved": 0.7,
    "surge": 0.7, "surges": 0.7, "soars": 0.8, "jumps": 0.6, "rallies": 0.6, "outperform": 0.6,
    "strong demand": 0.7, "partnership": 0.4, "contract win": 0.6, "wins contract": 0.6,
    "dividend increase": 0.6, "raises dividend": 0.6, "price target raised": 0.7, "raises price target": 0.7,
    "beats": 0.7, "growth": 0.3, "profit": 0.3, "bullish": 0.6, "expands": 0.3,
    # strongly negative
    "misses estimates": -1.0, "missed estimates": -1.0, "cuts guidance": -1.0, "lowers guidance": -1.0,
    "downgrade": -0.8, "downgraded": -0.8, "lawsuit": -0.6, "sued": -0.6, "investigation": -0.7,
    "probe": -0.6, "recall": -0.6, "plunges": -0.8, "plunge": -0.8, "tumbles": -0.7, "sinks": -0.6,
    "falls": -0.4, "drops": -0.4, "layoffs": -0.4, "bankruptcy": -1.0, "fraud": -1.0,
    "antitrust": -0.5, "fine": -0.4, "fined": -0.5, "delay": -0.4, "delays": -0.4, "halts": -0.6,
    "weak demand": -0.7, "price target cut": -0.7, "cuts price target": -0.7, "underperform": -0.6,
    "misses": -0.7, "bearish": -0.6, "warning": -0.5, "warns": -0.6, "loss": -0.3, "decline": -0.4,
}
_NEGATORS = ("not ", "no ", "fails to ", "failed to ")
_PHRASES = sorted(_LEXICON, key=len, reverse=True)


def score_text(text):
    """Score one headline/summary in [-1, 1]. 0 = neutral / no known keywords."""
    t = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    total, hits = 0.0, 0
    for phrase in _PHRASES:
        needle = f" {phrase} "
        while needle in t:
            idx = t.index(needle)
            s = _LEXICON[phrase]
            if any(t[max(0, idx - len(n) + 1):idx + 1].endswith(n) for n in _NEGATORS):
                s = -s
            total += s
            hits += 1
            # blank out so shorter phrases inside it aren't double-counted
            t = t[:idx] + " " * len(needle) + t[idx + len(needle):]
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, total / math.sqrt(hits)))


def sentiment(articles, now=None):
    """Recency-weighted sentiment for a list of {'t': epoch, 'headline', 'summary'}.
    Returns (score, n_articles_scored, top_headline)."""
    now = now or time.time()
    half_life = config.NEWS_HALF_LIFE_HOURS * 3600
    num, den, top, top_w = 0.0, 0.0, None, 0.0
    n = 0
    for a in articles:
        s = score_text(a.get("headline", "")) * 0.7 + score_text(a.get("summary", "")) * 0.3
        if s == 0:
            continue
        age = max(0.0, now - a["t"])
        w = 0.5 ** (age / half_life)
        num += s * w
        den += w
        n += 1
        if abs(s) * w > top_w:
            top, top_w = a.get("headline", ""), abs(s) * w
    if den == 0:
        return 0.0, 0, None
    # Shrink toward 0 when there's only one or two articles — one headline isn't a trend.
    confidence = n / (n + 1)
    return (num / den) * confidence, n, top


def _runup_pct(closes):
    if len(closes) < 2 or closes[0] <= 0:
        return 0.0
    return (closes[-1] - closes[0]) / closes[0] * 100


def decide_news(closes, articles, has_position, now=None):
    """Returns (action, description)."""
    score, n, top = sentiment(articles, now)
    move = _runup_pct(closes)
    headline = f' — "{top[:80]}"' if top else ""
    base = f"news score {score:+.2f} from {n} article(s), price {move:+.2f}% over window"

    if not closes:
        return "hold", "no price data"
    if has_position and score <= config.NEWS_SELL_THRESHOLD:
        return "sell", f"{base} — news turned negative, exit{headline}"
    if not has_position and score >= config.NEWS_BUY_THRESHOLD:
        if move >= config.NEWS_MAX_RUNUP_PCT:
            return "hold", f"{base} — positive but already priced in (ran {move:+.2f}%){headline}"
        if move <= -config.NEWS_MAX_RUNUP_PCT:
            return "hold", f"{base} — positive news but price falling hard, market disagrees{headline}"
        return "buy", f"{base} — positive news not yet priced in, buy{headline}"
    return "hold", f"{base} — no actionable signal"


# ---------------- Kelly sizing ----------------

def round_trip_returns(trades):
    """Pair each BUY with the next SELL of the same symbol -> list of returns (fraction).
    Round trips whose BUYs were opened in the same bot cycle (same "cycle" id) are averaged
    into ONE result: QQQ/AAPL/NVDA/GOOGL move together, so four buys on one cycle are really
    one bet and shouldn't count as four separate wins or losses in the Kelly stats."""
    open_buy = {}
    groups = {}   # cycle id (or a unique key) -> list of returns
    for t in sorted(trades, key=lambda x: x["t"]):
        sym = t["symbol"]
        if t["side"] == "BUY":
            open_buy[sym] = t
        elif t["side"] == "SELL" and sym in open_buy and open_buy[sym].get("price"):
            buy = open_buy.pop(sym)
            key = buy.get("cycle", ("solo", sym, buy["t"]))
            groups.setdefault(key, []).append((t["price"] - buy["price"]) / buy["price"])
    return [sum(g) / len(g) for g in groups.values()]


def cap_to_exposure(stake, open_value, spent_this_cycle, budget):
    """Shrink a new buy so total money in the market stays under MAX_TOTAL_EXPOSURE_PCT of the
    budget. All four symbols are big US tech, so they share one cap. Returns (dollars, note)."""
    cap = budget * config.MAX_TOTAL_EXPOSURE_PCT / 100
    room = cap - open_value - spent_this_cycle
    if room < config.MIN_ORDER_DOLLARS:
        return 0.0, (f"exposure cap: ${open_value + spent_this_cycle:.2f} already in the market, "
                     f"limit ${cap:.2f} — no room, skip")
    if stake > room:
        return round(room, 2), f"exposure cap: shrunk ${stake:.2f} -> ${room:.2f} to stay under ${cap:.2f}"
    return stake, f"exposure ok: ${open_value + spent_this_cycle + stake:.2f} of ${cap:.2f} limit"


def kelly_estimate(returns):
    """Estimate (p_win, payoff_ratio, full_kelly_fraction) from realized returns,
    shrunk toward the configured prior with KELLY_PRIOR_TRADES pseudo-trades."""
    k = config.KELLY_PRIOR_TRADES
    p0, b0 = config.KELLY_PRIOR_WIN_RATE, config.KELLY_PRIOR_PAYOFF
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r <= 0]
    n = len(returns)
    p = (len(wins) + p0 * k) / (n + k)
    if wins and losses:
        b_obs = (sum(wins) / len(wins)) / max(1e-9, sum(losses) / len(losses))
        b = (b_obs * n + b0 * k) / (n + k)
    else:
        b = b0
    f = p - (1 - p) / b if b > 0 else 0.0
    return p, b, f


def kelly_dollars(trades, stake_cap, budget):
    """Dollar size for a new buy: KELLY_FRACTION * full Kelly * budget, capped at the
    symbol's stake. Returns (dollars, explanation). 0 dollars = no edge, skip."""
    p, b, f = kelly_estimate(round_trip_returns(trades))
    f_used = max(0.0, f) * config.KELLY_FRACTION
    dollars = min(stake_cap, f_used * budget)
    note = (f"Kelly: win rate {p:.0%}, payoff {b:.2f}x -> full {f:+.1%}, "
            f"using {config.KELLY_FRACTION:g}x = {f_used:.1%} of budget")
    if dollars < config.MIN_ORDER_DOLLARS:
        return 0.0, note + " — below minimum order, no bet"
    return round(dollars, 2), note
