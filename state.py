"""Reads/writes the bot's live status to state.json, so the dashboard
website can show what the bot (running separately) is doing without
needing its own connection to Alpaca."""

import json
import os
import time

import config

MAX_HISTORY = 500
MAX_TRADES = 100
MAX_DECISIONS = 120


def decisions_for_cycle(t, symbols, notes, new_trades, signals):
    """Pure: one entry per symbol saying what the bot did this run and why.
    notes: {symbol: [log messages about it this run]}; signals: {symbol: signal description}."""
    acted = {}
    for tr in new_trades:
        acted[tr["symbol"]] = tr["side"].lower()
    out = []
    for sym in symbols:
        msgs = [m for m in notes.get(sym, []) if not m.startswith("hold (price")]
        why = " · ".join(msgs) if msgs else (signals.get(sym) or "no signal")
        out.append({"t": t, "symbol": sym, "action": acted.get(sym, "hold"), "why": why[:400]})
    return out


def load():
    if not os.path.exists(config.STATE_FILE):
        return {"history": [], "trades": []}
    try:
        with open(config.STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"history": [], "trades": []}


def save(acct, market_open, positions, new_trades, paused=None, pause_note=None, real=None, real_ref_prices=None,
         decisions=None):
    data = load()

    if decisions:
        data["decisions"] = (data.get("decisions", []) + decisions)[-MAX_DECISIONS:]

    if real is not None:
        data["real_portfolio"] = real
        rh = data.get("real_history", [])
        rh.append({"t": time.time(), "total": real["total"], **real.get("perf", {})})
        data["real_history"] = rh[-MAX_HISTORY:]
    if real_ref_prices is not None:
        data["real_ref_prices"] = real_ref_prices

    if paused is not None:
        data["paused"] = paused
        data["pause_note"] = pause_note

    if "baseline_equity" not in data:
        data["baseline_equity"] = acct["equity"]

    change_abs = acct["equity"] - data["baseline_equity"]
    # % is relative to your real budget, not the (much larger) paper account
    # balance — otherwise a real $20 gain would show as a meaningless 0.02%.
    change_pct = (change_abs / config.TOTAL_BUDGET * 100) if config.TOTAL_BUDGET else 0.0

    # Alpaca's paper account is funded with a fake ~$100k balance by default —
    # not what the user actually has. "real_equity"/"real_cash" scale that down
    # to their actual budget, so the dashboard shows real numbers, not Alpaca's.
    positions_value = sum(p.get("value", 0) for p in positions.values())
    real_equity = config.TOTAL_BUDGET + change_abs
    real_cash = real_equity - positions_value

    data["updated_at"] = time.time()
    data["market_open"] = market_open
    data["equity"] = acct["equity"]
    data["cash"] = acct["cash"]
    data["real_equity"] = real_equity
    data["real_cash"] = real_cash
    data["paper"] = acct["paper"]
    data["strategy"] = config.STRATEGY
    data["symbols"] = config.SYMBOLS
    data["stakes"] = config.STAKES
    data["total_budget"] = config.TOTAL_BUDGET
    data["total_allocated"] = sum(config.STAKES.values())
    data["change_abs"] = change_abs
    data["change_pct"] = change_pct
    data["positions"] = positions

    history = data.get("history", [])
    history.append({"t": time.time(), "equity": real_equity})
    data["history"] = history[-MAX_HISTORY:]

    trades = data.get("trades", [])
    for t in new_trades:
        trades.append(t)
    data["trades"] = trades[-MAX_TRADES:]
    data["est_costs"] = sum(t.get("est_cost", 0) for t in data["trades"])
    data["max_trades_per_day"] = config.MAX_TRADES_PER_DAY
    data["cooldown_minutes"] = config.COOLDOWN_MINUTES
    data["stop_loss_pct"] = config.STOP_LOSS_PCT
    data["max_overall_loss"] = config.MAX_OVERALL_LOSS_DOLLARS

    # Write to a temp file then swap it in, so a shutdown mid-write can't corrupt the saved data.
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, config.STATE_FILE)
