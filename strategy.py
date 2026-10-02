"""Deterministic, rule-based strategies (no black-box 'AI decides' logic).

Same shape as the UNIT7 demo's trend/mean-reversion rules, but fed real
historical prices instead of a simulated random walk.
"""

import config


def moving_avg(closes, n):
    if len(closes) < n:
        return None
    window = closes[-n:]
    return sum(window) / n


def decide_trend(closes, has_position):
    fast = moving_avg(closes, config.FAST_MA)
    slow = moving_avg(closes, config.SLOW_MA)
    if fast is None or slow is None:
        return "hold"
    buf = config.TREND_BUFFER_PCT / 100
    if fast > slow * (1 + buf) and not has_position:
        return "buy"
    if fast < slow * (1 - buf) and has_position:
        return "sell"
    return "hold"


def decide_meanrev(closes, has_position):
    avg = moving_avg(closes, config.MEANREV_LOOKBACK)
    if avg is None:
        return "hold"
    price = closes[-1]
    if price < avg * config.MEANREV_BUY_PCT and not has_position:
        return "buy"
    if price > avg * config.MEANREV_SELL_PCT and has_position:
        return "sell"
    return "hold"


def decide_daily(closes, has_position):
    fast = moving_avg(closes, config.DAILY_FAST_MA)
    slow = moving_avg(closes, config.DAILY_SLOW_MA)
    if fast is None or slow is None:
        return "hold"
    buf = config.DAILY_BUFFER_PCT / 100
    if fast > slow * (1 + buf) and not has_position:
        return "buy"
    if fast < slow * (1 - buf) and has_position:
        return "sell"
    return "hold"


def decide_openclose(bodies, has_position):
    if len(bodies) < config.OPENCLOSE_DAYS:
        return "hold"
    avg = sum(bodies[-config.OPENCLOSE_DAYS:]) / config.OPENCLOSE_DAYS
    if avg > 0 and not has_position:
        return "buy"
    if avg < 0 and has_position:
        return "sell"
    return "hold"


def describe_openclose(bodies):
    n = config.OPENCLOSE_DAYS
    if len(bodies) < n:
        return f"needs {n} days of history…"
    avg = sum(bodies[-n:]) / n
    up_days = sum(1 for b in bodies[-n:] if b > 0)
    if avg > 0:
        return f"last {n} days closed above their open on average ({up_days}/{n} up days) — buy signal"
    if avg < 0:
        return f"last {n} days closed below their open on average ({up_days}/{n} up days) — sell signal"
    return f"last {n} days closed flat vs open — no signal"


def _find_setup(bars, ref_bars):
    """Scan bars for: a recent range -> a liquidity sweep of it in the reaction
    window -> SMT divergence against ref_bars at that same bar -> a market
    structure shift confirming it by the most recent bar. Returns ('buy'/'sell'/None, detail dict)."""
    n = config.ICT_RANGE_BARS
    k = config.ICT_REACTION_BARS
    if len(bars) < n + k + 1 or len(ref_bars) < n + k + 1:
        return None, {}

    range_bars = bars[-(n + k):-k]
    reaction_bars = bars[-k:]
    ref_range = ref_bars[-(n + k):-k]
    ref_reaction = ref_bars[-k:]

    range_low = min(b["low"] for b in range_bars)
    range_high = max(b["high"] for b in range_bars)
    ref_range_low = min(b["low"] for b in ref_range)
    ref_range_high = max(b["high"] for b in ref_range)

    for i, (b, rb) in enumerate(zip(reaction_bars, ref_reaction)):
        # Bullish: sweep below range low, close back above it, SPY does NOT make an equally low low.
        if b["low"] < range_low and b["close"] > range_low and rb["low"] >= ref_range_low:
            structure_high = max((x["high"] for x in reaction_bars[i + 1:-1]), default=range_high)
            if reaction_bars[-1]["close"] > structure_high:
                return "buy", {"sweep_price": b["low"], "structure_level": structure_high}
        # Bearish: sweep above range high, close back below it, SPY does NOT make an equally high high.
        if b["high"] > range_high and b["close"] < range_high and rb["high"] <= ref_range_high:
            structure_low = min((x["low"] for x in reaction_bars[i + 1:-1]), default=range_low)
            if reaction_bars[-1]["close"] < structure_low:
                return "sell", {"sweep_price": b["high"], "structure_level": structure_low}

    return None, {}


def has_fvg(bars):
    """3-candle fair value gap: candle 1 and candle 3 wicks don't overlap. Informational only."""
    if len(bars) < 3:
        return None
    a, _, c = bars[-3], bars[-2], bars[-1]
    if a["high"] < c["low"]:
        return "bullish"
    if a["low"] > c["high"]:
        return "bearish"
    return None


def decide_ict(bars, ref_bars, has_position):
    setup, _ = _find_setup(bars, ref_bars)
    if setup == "buy" and not has_position:
        return "buy"
    if setup == "sell" and has_position:
        return "sell"
    return "hold"


def describe_ict(bars, ref_bars):
    if len(bars) < config.ICT_RANGE_BARS + config.ICT_REACTION_BARS + 1:
        return "gathering price history…"
    setup, detail = _find_setup(bars, ref_bars)
    fvg = has_fvg(bars)
    fvg_note = f", {fvg} FVG present" if fvg else ""
    if setup == "buy":
        return f"bullish liquidity sweep @ {detail['sweep_price']:.2f} + SMT divergence + structure break — buy signal{fvg_note}"
    if setup == "sell":
        return f"bearish liquidity sweep @ {detail['sweep_price']:.2f} + SMT divergence + structure break — sell signal{fvg_note}"
    return f"watching for a liquidity sweep + SMT divergence + structure shift{fvg_note}"


def decide(closes, has_position):
    if config.STRATEGY == "openclose":
        return decide_openclose(closes, has_position)
    if config.STRATEGY == "meanrev":
        return decide_meanrev(closes, has_position)
    if config.STRATEGY == "daily":
        return decide_daily(closes, has_position)
    return decide_trend(closes, has_position)


def describe_daily(closes):
    fast = moving_avg(closes, config.DAILY_FAST_MA)
    slow = moving_avg(closes, config.DAILY_SLOW_MA)
    if fast is None or slow is None:
        return f"needs {config.DAILY_SLOW_MA} days of history…"
    gap_pct = (fast - slow) / slow * 100
    buf = config.DAILY_BUFFER_PCT
    if gap_pct > buf:
        return f"{config.DAILY_FAST_MA}-day avg {gap_pct:+.2f}% above {config.DAILY_SLOW_MA}-day avg — uptrend (buy signal)"
    if gap_pct < -buf:
        return f"{config.DAILY_FAST_MA}-day avg {gap_pct:+.2f}% below {config.DAILY_SLOW_MA}-day avg — downtrend (sell signal)"
    return f"{config.DAILY_FAST_MA}/{config.DAILY_SLOW_MA}-day averages close ({gap_pct:+.2f}%) — no clear trend"


def describe_trend(closes):
    fast = moving_avg(closes, config.FAST_MA)
    slow = moving_avg(closes, config.SLOW_MA)
    if fast is None or slow is None:
        return "gathering price history…"
    gap_pct = (fast - slow) / slow * 100
    buf = config.TREND_BUFFER_PCT
    if gap_pct > buf:
        return f"fast avg {gap_pct:+.2f}% above slow avg — buy signal"
    if gap_pct < -buf:
        return f"fast avg {gap_pct:+.2f}% vs slow avg — sell signal"
    return f"fast/slow averages close ({gap_pct:+.2f}%) — no clear signal yet"


def describe_meanrev(closes):
    avg = moving_avg(closes, config.MEANREV_LOOKBACK)
    if avg is None:
        return "gathering price history…"
    price = closes[-1]
    dev_pct = (price - avg) / avg * 100
    if price < avg * config.MEANREV_BUY_PCT:
        return f"price {dev_pct:.2f}% below average — buy signal (dip)"
    if price > avg * config.MEANREV_SELL_PCT:
        return f"price {dev_pct:+.2f}% above average — sell signal (pop)"
    return f"price near average ({dev_pct:+.2f}%) — no clear signal yet"


def describe(closes):
    if config.STRATEGY == "openclose":
        return describe_openclose(closes)
    if config.STRATEGY == "meanrev":
        return describe_meanrev(closes)
    if config.STRATEGY == "daily":
        return describe_daily(closes)
    return describe_trend(closes)
