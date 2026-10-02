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


def run_cycle(broker, log):
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
    last_trade = {}
    today = datetime.now(NY).date()
    trades_today = 0
    prior = state.load()
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
                if not paused:
                    if config.STRATEGY == "ict":
                        action = strategy.decide_ict(series, ict_ref_bars, has_position)
                    else:
                        action = strategy.decide(series, has_position)
                elif has_position:
                    log.info(f"{symbol}: trading paused, but still watching for stop-loss/downtrend on this open position.")

                # Stop-loss and downtrend-stop protect open positions even while paused —
                # pausing means "don't open new trades," not "remove the safety net."
                if has_position:
                    entry = broker.get_entry_price(symbol)
                    if entry and price < entry * (1 - config.STOP_LOSS_PCT / 100):
                        action, reason = "sell", "stop-loss"
                    elif (entry and config.STRATEGY == "trend"
                          and (entry - price) * qty >= config.DOWNTREND_STOP_MIN_LOSS_DOLLARS):
                        fast = strategy.moving_avg(closes, config.FAST_MA)
                        slow = strategy.moving_avg(closes, config.SLOW_MA)
                        buf = config.TREND_BUFFER_PCT / 100
                        if fast is not None and slow is not None and fast < slow * (1 - buf):
                            action, reason = "sell", "downtrend-stop"

                is_override = reason in ("stop-loss", "downtrend-stop")

                if action != "hold" and not is_override and not in_act_window:
                    window_desc = (f"first {config.ICT_WINDOW_MINUTES} min after open" if config.STRATEGY == "ict"
                                    else f"last {config.DAILY_ACT_WINDOW_MIN} min of the day")
                    log.info(f"{symbol}: {action} signal noted outside the {window_desc} — ignored.")
                    action = "hold"

                in_cooldown = (time.time() - last_trade.get(symbol, 0)) < config.COOLDOWN_MINUTES * 60
                if action != "hold" and in_cooldown and not is_override:
                    log.info(f"{symbol}: {action} signal ignored (cooldown, avoids churning).")
                    action = "hold"

                if action != "hold" and not is_override and trades_today >= config.MAX_TRADES_PER_DAY:
                    log.info(f"{symbol}: {action} signal ignored (daily limit of {config.MAX_TRADES_PER_DAY} trades reached).")
                    action = "hold"

                if action == "buy":
                    stake = config.STAKES[symbol]
                    order = broker.buy_notional(symbol, stake)
                    log.info(f"{symbol}: BUY ~${stake} (order id {order.id})")
                    new_trades.append({"t": time.time(), "symbol": symbol, "side": "BUY", "price": price,
                                       "reason": reason, "est_cost": config.EST_COST_PER_TRADE})
                    last_trade[symbol] = time.time()
                    trades_today += 1
                    qty = broker.get_position_qty(symbol)
                elif action == "sell":
                    order = broker.sell_all(symbol)
                    log.info(f"{symbol}: SELL all ({qty} shares, {reason}, order id {order.id if order else 'n/a'})")
                    new_trades.append({"t": time.time(), "symbol": symbol, "side": "SELL", "price": price,
                                       "reason": reason, "est_cost": config.EST_COST_PER_TRADE})
                    last_trade[symbol] = time.time()
                    trades_today += 1
                    qty = 0
                else:
                    log.info(f"{symbol}: hold (price {price:.2f}, position {qty})")

            pos = broker.get_position(symbol) if qty > 0 else None
            positions[symbol] = {
                "qty": qty,
                "price": price,
                "value": pos["value"] if pos else 0.0,
                "entry": pos["entry"] if pos else None,
                "cost": pos["cost"] if pos else 0.0,
                "pnl": pos["pnl"] if pos else 0.0,
                "signal": strategy.describe_ict(series, ict_ref_bars) if config.STRATEGY == "ict" else strategy.describe(series),
            }

        except Exception as e:
            log.error(f"{symbol}: error during cycle — {e}")

    state.save(acct, market_open, positions, new_trades, paused=paused, pause_note=pause_note)


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
