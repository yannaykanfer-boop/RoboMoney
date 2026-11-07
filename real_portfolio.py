"""Read-only tracker for your real (Greenlight) portfolio.

Greenlight has no API, so holdings are copied by hand into real_portfolio.json
as dollar values on a given date ("as_of"). Each bot run scales every holding
by how much its price has moved since that date's close. Nothing is ever traded.
"""

import json
import os
from datetime import date

FILE = "real_portfolio.json"


def load(path=FILE):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def valuate(port, ref_prices, live_prices):
    """Pure function: snapshot values scaled by live/ref price. Missing prices -> unchanged value."""
    rows, total, total_gain, snap_total = {}, port.get("cash", 0.0), 0.0, port.get("cash", 0.0)
    for sym, h in port["holdings"].items():
        ref, live = ref_prices.get(sym), live_prices.get(sym)
        ratio = live / ref if ref and live else 1.0
        value = h["value"] * ratio
        cost = h["value"] - h.get("gain", 0.0)
        rows[sym] = {"name": h.get("name", sym), "value": round(value, 2), "gain": round(value - cost, 2),
                     "price": live, "since_snapshot": round(value - h["value"], 2), "live": bool(ref and live)}
        total += value
        snap_total += h["value"]
        total_gain += value - cost
    return {"as_of": port.get("as_of"), "cash": port.get("cash", 0.0), "holdings": rows,
            "total": round(total, 2), "gain": round(total_gain, 2),
            "since_snapshot": round(total - snap_total, 2)}


def update(broker, prior_state, log):
    """Fetch prices and return (valuation, ref_prices_to_cache). Never raises."""
    port = load()
    if not port:
        return None, None
    cache = prior_state.get("real_ref_prices") or {}
    if cache.get("as_of") != port.get("as_of"):
        cache = {"as_of": port.get("as_of"), "prices": {}}
    refs, lives = cache["prices"], {}
    as_of = date.fromisoformat(port["as_of"])
    for sym in port["holdings"]:
        try:
            if sym not in refs:
                ref = broker.close_on_or_before(sym, as_of)
                if ref:
                    refs[sym] = ref
            closes = broker.recent_closes(sym, lookback=1)
            if closes:
                lives[sym] = closes[-1]
        except Exception as e:
            log.warning(f"real portfolio: no price for {sym} ({e}) — showing its last known value.")
    return valuate(port, refs, lives), cache
