"""Read-only tracker for your real (Greenlight) portfolio.

Greenlight has no API, so holdings are copied by hand into real_portfolio.json
as dollar values on a given date ("as_of"). Each bot run scales every holding
by how much its price has moved since that date's close. Nothing is ever traded.
"""

import json
import os
from datetime import date, timedelta

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


def today(port, refs, bars, prev_closes, max_points=160):
    """Pure: intraday portfolio value for the latest session.
    bars: {sym: [(epoch, close), ...]}; prev_closes: {sym: close the day before}.
    Each holding is treated as (snapshot value / ref price) shares; missing data -> flat at snapshot value."""
    shares = {s: h["value"] / refs[s] for s, h in port["holdings"].items() if refs.get(s)}
    fixed = sum(h["value"] for s, h in port["holdings"].items() if s not in shares) + port.get("cash", 0.0)
    prev_total = fixed + sum(q * prev_closes.get(s, refs[s]) for s, q in shares.items())
    times = sorted({t for s in shares for t, _ in bars.get(s, [])})
    if not times:
        return None
    idx = {s: 0 for s in shares}
    lastp = {s: prev_closes.get(s, refs[s]) for s in shares}
    series = []
    for t in times:
        for s in shares:  # forward-fill each symbol's latest price at or before t
            b = bars.get(s, [])
            while idx[s] < len(b) and b[idx[s]][0] <= t:
                lastp[s] = b[idx[s]][1]
                idx[s] += 1
        series.append({"t": t, "v": round(fixed + sum(q * lastp[s] for s, q in shares.items()), 2)})
    if len(series) > max_points:
        step = len(series) / max_points
        series = [series[int(i * step)] for i in range(max_points)] + [series[-1]]
    last = series[-1]["v"]
    return {"prev_total": round(prev_total, 2), "series": series, "change": round(last - prev_total, 2),
            "pct": round((last - prev_total) / prev_total * 100, 2) if prev_total else 0.0}


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
    result = valuate(port, refs, lives)
    try:
        bars, prev, session = {}, {}, None
        for sym in port["holdings"]:
            day, b = broker.session_bars(sym)
            if not b:
                continue
            bars[sym], session = b, max(session or day, day)
            pc = broker.close_on_or_before(sym, day - timedelta(days=1))
            if pc:
                prev[sym] = pc
        t = today(port, refs, bars, prev)
        if t:
            t["session"] = session.isoformat() if session else None
            result["today"] = t
    except Exception as e:
        log.warning(f"real portfolio: couldn't build today's chart ({e}).")
    return result, cache
