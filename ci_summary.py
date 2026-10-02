"""Prints a Markdown summary of the current bot state, for GitHub Actions'
step summary (visible on each workflow run's page). Read-only, no side effects."""

import json
import os

import config

if not os.path.exists(config.STATE_FILE):
    print("No state.json yet — this must be the first run.")
    raise SystemExit

with open(config.STATE_FILE) as f:
    d = json.load(f)

print(f"## Trading bot status\n")
print(f"- Strategy: `{d.get('strategy', '?')}` | Paused: `{d.get('paused', False)}`")
print(f"- Market open: `{d.get('market_open', False)}`")
print(f"- Equity: ${d.get('equity', 0):,.2f} | Cash: ${d.get('cash', 0):,.2f}")
print(f"- Since start: {d.get('change_abs', 0):+.2f} ({d.get('change_pct', 0):+.2f}%)")
print()
print("| Symbol | Qty | Price | Value | P&L | Signal |")
print("|---|---|---|---|---|---|")
for sym, p in d.get("positions", {}).items():
    print(f"| {sym} | {p.get('qty', 0):.4f} | ${p.get('price', 0):.2f} | "
          f"${p.get('value', 0):.2f} | {p.get('pnl', 0):+.2f} | {p.get('signal', '')} |")

trades = d.get("trades", [])[-10:]
if trades:
    print("\n### Last 10 trades\n")
    for t in reversed(trades):
        print(f"- {t['side']} {t['symbol']} @ ${t['price']:.2f} ({t.get('reason', '')})")
