"""Backtest the bot's strategies against real historical daily prices.

Doesn't touch your account or place any orders — just downloads past prices
and replays the same buy/sell logic bot.py uses, so you can see how the
strategy would have performed before ever running it live.
"""

import argparse
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
import strategy


def fetch_daily_closes(client, symbol, days):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    bars = client.get_stock_bars(req)
    rows = bars.data.get(symbol, [])
    return [float(b.close) for b in rows]


def run_backtest(symbol, closes, strat_name, stake):
    config.STRATEGY = strat_name  # strategy.decide() reads this
    cash = stake
    shares = 0.0
    trades = []

    # Walk forward day by day, only ever seeing prices up to "today".
    for i in range(1, len(closes) + 1):
        window = closes[:i]
        price = window[-1]
        has_position = shares > 0
        action = strategy.decide(window, has_position)

        if action == "buy" and cash > 0:
            shares = cash / price
            cash = 0.0
            trades.append(("BUY", price))
        elif action == "sell" and shares > 0:
            cash = shares * price
            shares = 0.0
            trades.append(("SELL", price))

    final_value = cash + shares * closes[-1]
    buy_hold_value = stake / closes[0] * closes[-1]

    return {
        "symbol": symbol,
        "strategy": strat_name,
        "start_price": closes[0],
        "end_price": closes[-1],
        "num_trades": len(trades),
        "final_value": final_value,
        "return_pct": (final_value - stake) / stake * 100,
        "buy_hold_value": buy_hold_value,
        "buy_hold_return_pct": (buy_hold_value - stake) / stake * 100,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest trend/meanrev strategies on real history.")
    parser.add_argument("--days", type=int, default=180, help="How many calendar days of history to test.")
    parser.add_argument("--stake", type=float, default=100.0, help="Starting dollars per symbol for the test.")
    args = parser.parse_args()

    problems = [p for p in config.validate() if "SYMBOLS" not in p and "STAKE" not in p]
    if problems:
        for p in problems:
            print("ERROR:", p)
        return

    client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

    print(f"Backtesting over the last {args.days} days, ${args.stake:.0f} per symbol\n")
    print(f"{'Symbol':<8}{'Strategy':<10}{'Trades':<8}{'Strategy $':<14}{'Strategy %':<12}{'Buy&Hold %':<12}")
    print("-" * 64)

    for symbol in config.SYMBOLS:
        closes = fetch_daily_closes(client, symbol, args.days)
        if len(closes) < 25:
            print(f"{symbol}: not enough history returned, skipping.")
            continue

        for strat_name in ("trend", "meanrev", "daily"):
            result = run_backtest(symbol, closes, strat_name, args.stake)
            print(f"{result['symbol']:<8}{result['strategy']:<10}{result['num_trades']:<8}"
                  f"${result['final_value']:<13.2f}{result['return_pct']:<+11.1f}%{result['buy_hold_return_pct']:<+11.1f}%")

    print("\nStrategy % = what this bot's rules would have returned.")
    print("Buy&Hold % = what simply buying on day 1 and holding would have returned.")
    print("This is historical only — past performance doesn't predict future results.")


if __name__ == "__main__":
    main()
