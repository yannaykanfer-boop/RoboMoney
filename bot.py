"""Trading bot entrypoint.

Defaults to PAPER trading no matter what's in .env. To place real orders
against a live account you must pass --live AND have ALPACA_PAPER=false
in .env AND type the confirmation phrase when prompted. This is deliberately
annoying so real money is never risked by accident.
"""

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import logging
import time
import sys

import config
from broker import Broker
import strategy
import state
import news
import exits
import real_portfolio
import agents
from desk import HeadOfDesk

LIVE_CONFIRM_PHRASE = "trade real money"
NY = ZoneInfo("America/New_York")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_FILE),
        ],
    )


def confirm_live_trading():
    print("\n*** LIVE TRADING REQUESTED ***")
    print(f"Symbols: {', '.join(config.SYMBOLS)}")
    print(f"Stake per symbol: ${config.STAKE_PER_SYMBOL}")
    print("This will place REAL orders with REAL money.")
    answer = input(f"Type '{LIVE_CONFIRM_PHRASE}' to continue: ").strip().lower()
    return answer == LIVE_CONFIRM_PHRASE


def has_strong_signal(closes):
    fast = strategy.moving_avg(closes, config.FAST_MA)
    slow = strategy.moving_avg(closes, config.SLOW_MA)
    if fast is None or slow is None:
        return False
    return abs((fast - slow) / slow * 100) > config.PAUSE_RESUME_BUFFER_PCT


class _SymbolNotes(logging.Handler):
    """Collects this run's log messages that start with 'SYMBOL:' so the dashboard can show why
    the bot did (or didn't) trade each stock."""
    def __init__(self, symbols):
        super().__init__(level=logging.INFO)
        self.symbols, self.notes = set(symbols), {}

    def emit(self, record):
        msg = record.getMessage()
        sym, sep, rest = msg.partition(": ")
        if sep and sym in self.symbols:
            self.notes.setdefault(sym, []).append(rest.strip())


def run_cycle(broker, log):
    notes = _SymbolNotes(config.SYMBOLS)
    old_level = log.level
    if log.getEffectiveLevel() > logging.INFO:
        log.setLevel(logging.INFO)  # the reasons are logged at INFO; make sure they reach the recorder
    log.addHandler(notes)
    try:
        _run_cycle(broker, log, notes)
    finally:
        log.removeHandler(notes)
        log.setLevel(old_level)


def _run_cycle(broker, log, notes):
    market_open = broker.is_market_open()
    if not market_open:
        log.info("Market closed — skipping trading, updating dashboard only.")

    in_act_window = True
    if market_open and config.STRATEGY in ("daily", "openclose"):
        in_act_window = broker.minutes_to_close() <= config.DAILY_ACT_WINDOW_MIN
    if market_open and config.STRATEGY == "ict":
        since_open = broker.minutes_since_open()
        in_act_window = since_open is not None and since_open <= config.ICT_WINDOW_MINUTES

    acct = broker.account_summary()
    positions = {}
    new_trades = []
    peaks = {}  # highest price since each open position was bought (filled in below)
    cycle_id = int(time.time())  # tags trades opened in this run, so Kelly can count them as one bet
    spent_this_cycle = 0.0
    bought_this_cycle = set()
    desk = HeadOfDesk(cycle_id)
    last_trade = {}
    today = datetime.now(NY).date()
    trades_today = 0
    prior = state.load()
    peaks.update(prior.get("peaks", {}))
    for t in prior.get("trades", []):
        last_trade[t["symbol"]] = max(last_trade.get(t["symbol"], 0), t["t"])
        if datetime.fromtimestamp(t["t"], NY).date() == today:
            trades_today += 1

    paused = prior.get("paused", False)
    pause_note = prior.get("pause_note")

    ict_ref_bars = None
    if config.STRATEGY == "ict":
        ict_lookback = config.ICT_RANGE_BARS + config.ICT_REACTION_BARS + 5
        ict_ref_bars = broker.recent_bars(config.ICT_REF_SYMBOL, lookback=ict_lookback)

    price_data = {}
    for symbol in config.SYMBOLS:
        if config.STRATEGY in ("daily", "openclose"):
            bars = broker.daily_bars(symbol)
            closes = [c for _, c in bars]
            series = [c - o for o, c in bars] if config.STRATEGY == "openclose" else closes
        elif config.STRATEGY == "ict":
            ohlc = broker.recent_bars(symbol, lookback=ict_lookback)
            closes = [b["close"] for b in ohlc]
            series = ohlc
        elif config.STRATEGY == "news":
            closes = broker.recent_closes(symbol, lookback=config.NEWS_PRICE_WINDOW_BARS)
            try:
                series = broker.recent_news(symbol, hours=config.NEWS_LOOKBACK_HOURS)
            except Exception as e:
                log.warning(f"{symbol}: news fetch failed ({e}) — treating as no news.")
                series = []
        else:
            closes = broker.recent_closes(symbol, lookback=max(config.SLOW_MA, config.MEANREV_LOOKBACK) + 5)
            series = closes
        price_data[symbol] = (closes, series)

    if paused and market_open and config.STRATEGY == "trend":
        for symbol, (closes, _) in price_data.items():
            if closes and has_strong_signal(closes):
                paused, pause_note = False, None
                log.info(f"RESUMING: {symbol} shows a confirmed trend "
                         f"(gap over {config.PAUSE_RESUME_BUFFER_PCT}%), trading is back on.")
                break

    # Portfolio-wide circuit breaker: overrides everything above, including a
    # resume that just happened this same cycle — capital protection wins.
    baseline_equity = prior.get("baseline_equity", acct["equity"])
    overall_change = acct["equity"] - baseline_equity
    if overall_change <= -config.MAX_OVERALL_LOSS_DOLLARS and not paused:
        paused = True
        pause_note = (f"overall account down ${abs(overall_change):.2f}, "
                       f"past the ${config.MAX_OVERALL_LOSS_DOLLARS} limit — paused automatically")
        log.warning(f"CIRCUIT BREAKER: {pause_note}")
        desk.alert(None, f"Circuit breaker: {pause_note}. New trades are stopped until you look.", level="error")

    # Same-story check (news strategy): if one article is the main reason for buying
    # several symbols this cycle, buy only the symbol with the strongest news score.
    # Otherwise one headline turns into several correlated bets on the same idea.
    same_story_skip = {}
    if config.STRATEGY == "news" and market_open and not paused:
        candidates, held_stories = {}, {}
        for symbol, (closes, series) in price_data.items():
            if not closes:
                continue
            try:
                has_position = broker.get_position_qty(symbol) > 0
                if has_position and news.top_article_id(series) is not None:
                    held_stories[news.top_article_id(series)] = symbol
                if news.decide_news(closes, series, has_position)[0] == "buy":
                    candidates[symbol] = (news.sentiment(series)[0], news.top_article_id(series))
            except Exception as e:
                log.warning(f"{symbol}: same-story check failed ({e}) — not deduping it.")
        losers = news.same_story_losers(candidates, held_stories)
        for symbol in losers:
            winner = held_stories.get(candidates[symbol][1]) or max((s for s in candidates if candidates[s][1] == candidates[symbol][1]),
                         key=lambda s: candidates[s][0])
            same_story_skip[symbol] = winner

    def open_value():
        """Dollars already in the market from earlier cycles (this cycle's buys are counted separately)."""
        return news.held_exposure(
            {sym: (broker.get_position(sym) or {}).get("value", 0.0) for sym in config.SYMBOLS},
            bought_this_cycle)

    def committed():
        """Dollars put into open positions in earlier cycles, at cost. The Head of Desk's budget check
        uses cost, not today's value, so a winning position growing doesn't block new buys."""
        return news.held_exposure(
            {sym: (broker.get_position(sym) or {}).get("cost", 0.0) for sym in config.SYMBOLS},
            bought_this_cycle)

    for symbol in config.SYMBOLS:
        try:
            closes, series = price_data[symbol]
            if not closes:
                log.warning(f"{symbol}: no price data returned, skipping.")
                continue

            price = closes[-1]
            qty = broker.get_position_qty(symbol)
            has_position = qty > 0

            if market_open:
                action, reason = "hold", "signal"

                # SEARCH: what does the strategy want?
                if not paused:
                    found = agents.search(config.STRATEGY, closes, series, has_position, ict_ref_bars)
                    desk.hear(symbol, found)
                    action = found.action
                    if config.STRATEGY == "news" and action != "hold":
                        log.info(f"{symbol}: {found.note}")
                    if action == "buy" and symbol in same_story_skip:
                        veto = agents.same_story(symbol, same_story_skip[symbol])
                        desk.hear(symbol, veto)
                        log.info(f"{symbol}: {veto.note}")
                        action = "hold"
                elif has_position:
                    log.info(f"{symbol}: trading paused, but still watching for stop-loss/downtrend on this open position.")

                # EXIT: protects open positions even while paused. Pausing means "don't open new
                # trades," not "remove the safety net."
                if has_position:
                    entry = broker.get_entry_price(symbol)
                    exit_v, peak = agents.exit_watch(
                        config.STRATEGY, closes, price, qty, entry, peaks.get(symbol),
                        exits.opened_at(prior.get("trades", []), symbol), time.time(), series, action)
                    if peak is not None:
                        peaks[symbol] = peak
                    if exit_v:
                        desk.hear(symbol, exit_v)
                        log.info(f"{symbol}: {exit_v.note}")
                        action, reason = "sell", exit_v.tag

                is_override = reason in agents.OVERRIDE_TAGS

                # RISK: trading window, cooldown, daily limit (protective exits skip this).
                if action != "hold" and not is_override:
                    in_cooldown = (time.time() - last_trade.get(symbol, 0)) < config.COOLDOWN_MINUTES * 60
                    rule = agents.risk(action, config.STRATEGY, in_act_window, in_cooldown, trades_today)
                    desk.hear(symbol, rule)
                    if rule.action == "veto":
                        log.info(f"{symbol}: {rule.note}")
                        action = "hold"

                # CRASH: don't buy into a stock that is falling hard right now.
                if action == "buy":
                    crash = agents.crash_guard(closes)
                    if crash:
                        desk.hear(symbol, crash)
                        log.info(f"{symbol}: {crash.note}")
                        # One alert per stock per hour: a crash lasts several checks.
                        if not any(x.get("symbol") == symbol and "CRASH stopped" in x.get("message", "")
                                   and time.time() - x.get("t", 0) < 3600 for x in prior.get("desk_alerts", [])):
                            desk.alert(symbol, f"SEARCH wanted to buy {symbol}, but CRASH stopped it: "
                                               f"{crash.note.split('— ', 1)[-1]} Worth checking the news yourself.",
                                       level="info")
                        action = "hold"

                # SIZER: how many dollars.
                stake = config.STAKES[symbol]
                if action == "buy" and config.STRATEGY == "news":
                    stake, sized = agents.size_news(prior.get("trades", []) + new_trades, config.STAKES[symbol],
                                                    config.TOTAL_BUDGET, open_value, spent_this_cycle)
                    desk.hear(symbol, sized)
                    for part in sized.note.split(" · "):
                        log.info(f"{symbol}: {part}")
                    if stake <= 0:
                        action = "hold"

                # HEAD OF DESK: final review. Buys can be blocked; sells are only recorded.
                if action == "buy":
                    ok, why = desk.review_buy(symbol, stake, price, committed(), trades_today)
                    if not ok:
                        log.warning(f"{symbol}: HEAD OF DESK blocked buy — {why}")
                        action = "hold"
                elif action == "sell":
                    desk.review_sell(symbol, reason)

                # EXECUTION: the only place orders are sent.
                if action == "buy":
                    order = broker.buy_notional(symbol, stake)
                    log.info(f"{symbol}: BUY ~${stake} (order id {order.id})")
                    new_trades.append({"t": time.time(), "symbol": symbol, "side": "BUY", "price": price, "dollars": stake,
                                       "cycle": cycle_id, "reason": reason, "est_cost": config.EST_COST_PER_TRADE})
                    desk.filled(symbol, "BUY", stake)
                    spent_this_cycle += stake
                    bought_this_cycle.add(symbol)
                    last_trade[symbol] = time.time()
                    trades_today += 1
                    qty = broker.get_position_qty(symbol)
                elif action == "sell":
                    order = broker.sell_all(symbol)
                    log.info(f"{symbol}: SELL all ({qty} shares, {reason}, order id {order.id if order else 'n/a'})")
                    new_trades.append({"t": time.time(), "symbol": symbol, "side": "SELL", "price": price, "qty": qty,
                                       "reason": reason, "est_cost": config.EST_COST_PER_TRADE})
                    desk.filled(symbol, "SELL")
                    last_trade[symbol] = time.time()
                    trades_today += 1
                    qty = 0
                    peaks.pop(symbol, None)
                else:
                    log.info(f"{symbol}: hold (price {price:.2f}, position {qty})")
                desk.done(symbol, action)

            pos = broker.get_position(symbol) if qty > 0 else None
            positions[symbol] = {
                "qty": qty,
                "price": price,
                "value": pos["value"] if pos else 0.0,
                "entry": pos["entry"] if pos else None,
                "cost": pos["cost"] if pos else 0.0,
                "pnl": pos["pnl"] if pos else 0.0,
                "signal": (strategy.describe_ict(series, ict_ref_bars) if config.STRATEGY == "ict"
                           else news.decide_news(closes, series, qty > 0)[1] if config.STRATEGY == "news"
                           else strategy.describe(series)),
            }

        except Exception as e:
            log.error(f"{symbol}: error during cycle — {e}")
            desk.alert(symbol, f"error during cycle — {e}", level="error")

    real, real_refs = real_portfolio.update(broker, prior, log)
    if real:
        log.info(f"Real portfolio (read-only): ${real['total']:.2f} ({real['since_snapshot']:+.2f} since {real['as_of']})")
    decisions = None
    if market_open:
        decisions = state.decisions_for_cycle(time.time(), config.SYMBOLS, notes.notes, new_trades,
                                              {s: p.get("signal") for s, p in positions.items()})
    state.save(acct, market_open, positions, new_trades, paused=paused, pause_note=pause_note,
               real=real, real_ref_prices=real_refs, decisions=decisions,
               desk=desk.report() if market_open else None,
               desk_log=desk.log_entries() if market_open else None, desk_alerts=desk.alerts,
               peaks={k: v for k, v in peaks.items() if positions.get(k, {}).get("qty", 0) > 0})


def main():
    parser = argparse.ArgumentParser(description="Rule-based trading bot (Alpaca).")
    parser.add_argument("--live", action="store_true", help="Allow live (real-money) trading if .env also has ALPACA_PAPER=false.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit, instead of looping.")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("bot")

    problems = config.validate()
    if problems:
        for p in problems:
            log.error(p)
        sys.exit(1)

    if not config.PAPER:
        if not args.live:
            log.error("ALPACA_PAPER=false in .env but --live was not passed. Refusing to start. "
                       "Pass --live if you really mean to trade real money, or set ALPACA_PAPER=true to test safely.")
            sys.exit(1)
        if not confirm_live_trading():
            log.info("Confirmation not received. Exiting without trading.")
            sys.exit(0)
    else:
        log.info("Running in PAPER mode (simulated money, real market data).")

    broker = Broker()
    acct = broker.account_summary()
    log.info(f"Account: equity=${acct['equity']:.2f} cash=${acct['cash']:.2f} paper={acct['paper']}")
    log.info(f"Strategy={config.STRATEGY} stakes={config.STAKES}")
    state.save(acct, broker.is_market_open(), {}, [])

    if args.once:
        run_cycle(broker, log)
        return

    log.info(f"Starting loop, polling every {config.POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            run_cycle(broker, log)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()
