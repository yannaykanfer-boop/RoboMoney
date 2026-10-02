"""Compare strategies over the trial week on the same real prices.

Replays 'daily' (20/50-day averages) and 'openclose' (open-vs-close) day by day
from the start date, each starting flat with your STAKES, filling at each day's
close, next to plain buy-and-hold. Also shows what the live paper account did.
Nothing here places orders.

Usage: python compare.py            (trial started 2026-09-21)
       python compare.py --start 2026-09-21
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
import strategy


def load_bars(client, symbol, start_date):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=150),
    )
    return client.get_stock_bars(req).data.get(symbol, [])


def replay(name, bars_by_symbol, start_date):
    total_start = sum(config.STAKES.values())
    total_end, total_trades, notes = 0.0, 0, []
    for symbol, stake in config.STAKES.items():
        bars = bars_by_symbol[symbol]
        opens = [float(b.open) for b in bars]
        closes = [float(b.close) for b in bars]
        cash, shares, trades = stake, 0.0, 0
        for i, b in enumerate(bars):
            if b.timestamp.date() < start_date:
                continue
            if name == "hold":
                if shares == 0 and cash > 0:
                    shares, cash = cash / closes[i], 0.0
                continue
            if name == "openclose":
                series = [c - o for o, c in zip(opens[: i + 1], closes[: i + 1])]
                act = strategy.decide_openclose(series, shares > 0)
            else:
                act = strategy.decide_daily(closes[: i + 1], shares > 0)
            if act == "buy" and cash > 0:
                shares, cash, trades = cash / closes[i], 0.0, trades + 1
                notes.append(f"  {b.timestamp.date()} BUY  {symbol} @ {closes[i]:.2f}")
            elif act == "sell" and shares > 0:
                cash, shares, trades = shares * closes[i], 0.0, trades + 1
                notes.append(f"  {b.timestamp.date()} SELL {symbol} @ {closes[i]:.2f}")
        total_end += cash + shares * closes[-1]
        total_trades += trades
    return total_start, total_end, total_trades, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-21")
    args = ap.parse_args()
    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()

    client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
    bars_by_symbol = {s: load_bars(client, s, start_date) for s in config.STAKES}
    trial_days = len({b.timestamp.date() for b in bars_by_symbol[config.SYMBOLS[0]] if b.timestamp.date() >= start_date})

    print(f"\nTrial from {start_date} — {trial_days} trading day(s) so far, stakes {config.STAKES}\n")
    print(f"{'Strategy':<26}{'Start $':<10}{'End $':<10}{'P&L $':<10}{'P&L %':<9}{'Trades'}")
    print("-" * 65)
    labels = {"daily": "daily (20/50-day avgs)", "openclose": "openclose (open vs close)", "hold": "just hold"}
    all_notes = {}
    for name in ("daily", "openclose", "hold"):
        s, e, t, notes = replay(name, bars_by_symbol, start_date)
        all_notes[name] = notes
        print(f"{labels[name]:<26}{s:<10.2f}{e:<10.2f}{e - s:<+10.2f}{(e - s) / s * 100:<+9.2f}{t}")

    for name in ("daily", "openclose"):
        print(f"\n{labels[name]} trades:")
        print("\n".join(all_notes[name]) or "  (none)")

    if os.path.exists(config.STATE_FILE):
        st = json.load(open(config.STATE_FILE))
        since = datetime.combine(start_date, datetime.min.time()).timestamp()
        live = [t for t in st.get("trades", []) if t["t"] >= since]
        print(f"\nLive paper account (strategy '{st.get('strategy')}'): "
              f"{len(live)} trades since {start_date}, account change {st.get('change_abs', 0):+.2f} $, "
              f"est. costs {st.get('est_costs', 0):.2f} $")

    print("\nCaution: a few trading days is far too little data to prove one strategy is better.")


if __name__ == "__main__":
    main()
