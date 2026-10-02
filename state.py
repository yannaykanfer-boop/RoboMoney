"""Reads/writes the bot's live status to state.json, so the dashboard
website can show what the bot (running separately) is doing without
needing its own connection to Alpaca."""

import json
import os
import time

import config

MAX_HISTORY = 500
MAX_TRADES = 100


def load():
    if not os.path.exists(config.STATE_FILE):
        return {"history": [], "trades": []}
    try:
        with open(config.STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"history": [], "trades": []}


def save(acct, market_open, positions, new_trades, paused=None, pause_note=None):
    data = load()

    if paused is not None:
        data["paused"] = paused
        data["pause_note"] = pause_note

    if "baseline_equity" not in data:
        data["baseline_equity"] = acct["equity"]

    change_abs = acct["equity"] - data["baseline_equity"]
    # % is relative to your real budget, not the (much larger) paper account
    # balance — otherwise a real $20 gain would show as a meaningless 0.02%.
    change_pct = (change_abs / config.TOTAL_BUDGET * 100) if config.TOTAL_BUDGET else 0.0

    data["updated_at"] = time.time()
    data["market_open"] = market_open
    data["equity"] = acct["equity"]
    data["cash"] = acct["cash"]
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
    history.append({"t": time.time(), "equity": acct["equity"]})
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
