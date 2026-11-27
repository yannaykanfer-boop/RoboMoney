"""Crypto trend-pullback strategy: rules, backtest, and the order plan for live use.

Spot only, long only, no leverage. BTC/USD and ETH/USD by default. Decisions on CLOSED 1-hour
bars; the 4-hour trend filter only ever uses 4-hour bars that have fully closed. Every fill happens
at the NEXT bar's open (or at a stop/target level inside a later bar), never at the signal bar's
own close, so the backtest can't trade on prices it hadn't seen yet.

The rules (see README "Crypto trend pullback" for the plain-English walkthrough):

  REGIME (stay flat unless all true, judged on the last closed 4H bar)
    4H close > 4H EMA200, 4H EMA50 > 4H EMA200, and 4H EMA50 rising over the last 6 4H bars.
    1H volatility in a workable band: ATR(14) / close between 0.4% and 3.0%.
    No shock in the last 6 hours: no 1H bar with a range above 3 x ATR (news spikes, liquidations).

  ENTRY (on a closed 1H bar, buy at the next bar's open)
    Pullback: some bar in the last 5 dipped to or below the 1H EMA20.
    Structure held: no close below the 1H EMA50 in those 5 bars.
    Resumption: this bar closes above the EMA20 and above the previous bar's high.
    Not stretched: RSI(14) between 40 and 65.

  RISK
    Stop: lowest low of the last 5 bars minus 0.25 x ATR. Skip if that is under 0.5 ATR or over
    3 ATR away. Size so that hitting the stop loses 1% of equity (fees included in the check),
    capped at 50% of equity per position, 2 positions at once, never more than 100% of equity.
    No new entries after a 3% losing day (UTC) and for 6 hours after a stop-out on that pair.

  EXITS (whichever comes first)
    Stop-loss (resting on the exchange). Take-profit at 2R. After the price reaches +1R the stop
    moves to break-even plus fees. Time stop after 48 hours. Regime break: 4H close under the 4H
    EMA200 exits at the next open. When the stop and the target are both inside one bar, the
    backtest assumes the stop hit first.

Run:
  python crypto_pullback.py --download            # fetch ~5 years of 1H bars (no keys needed)
  python crypto_pullback.py                       # backtest with the defaults
  python crypto_pullback.py --risk-pct 0.5 --fee-pct 0.15
Nothing here places orders. order_plan() describes what a live version would send.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("crypto_pullback")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HOUR = pd.Timedelta(hours=1)


# ---------------------------------------------------------------- settings

@dataclass
class Params:
    symbols: tuple = ("BTC/USD", "ETH/USD")
    entry_hours: int = 1           # entry bars: 1 = hourly; 4 = 4-hour (built from hourly data)
    trend_hours: int = 4           # trend-filter bars: 4 = 4-hour; 24 = daily
    # regime (trend bars)
    ema_trend_fast: int = 50
    ema_trend_slow: int = 200
    slope_bars_4h: int = 6
    atr_len: int = 14
    min_atr_pct: float = 0.4
    max_atr_pct: float = 3.0
    shock_atr_mult: float = 3.0
    shock_lookback: int = 6
    # entry (1H)
    ema_pull: int = 20
    ema_structure: int = 50
    pullback_lookback: int = 5
    rsi_len: int = 14
    rsi_min: float = 40.0
    rsi_max: float = 65.0
    # risk
    stop_atr_buffer: float = 0.25
    min_stop_atr: float = 0.5
    max_stop_atr: float = 3.0
    risk_pct: float = 1.0
    max_position_pct: float = 50.0
    max_positions: int = 2
    daily_loss_pct: float = 3.0
    cooldown_bars: int = 6
    # exits
    take_profit_r: float = 2.0
    breakeven_at_r: float = 1.0
    max_hold_bars: int = 48
    # costs (assumed; check your exchange's fee tier)
    fee_pct: float = 0.25          # per side, on notional
    slippage_pct: float = 0.05     # per side, on market-style fills
    stop_slippage_pct: float = 0.10  # extra on stop fills
    starting_equity: float = 1000.0


# ---------------------------------------------------------------- data

def download(symbols, start="2022-01-01", end=None, out_dir=DATA_DIR):
    """Hourly crypto bars from Alpaca's market data (no API keys needed for crypto)."""
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
    os.makedirs(out_dir, exist_ok=True)
    client = CryptoHistoricalDataClient()
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    t1 = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else datetime.now(timezone.utc)
    for sym in symbols:
        rows, s = [], t0
        while s < t1:
            e = min(s + timedelta(days=365), t1)
            bars = client.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Hour,
                                                            start=s, end=e)).data.get(sym, [])
            rows += [(int(b.timestamp.timestamp()), b.open, b.high, b.low, b.close, b.volume) for b in bars]
            s = e
        df = pd.DataFrame(sorted(set(rows)), columns=["t", "open", "high", "low", "close", "volume"])
        df.to_csv(data_path(sym, out_dir), index=False)
        log.info("%s: %d hourly bars saved", sym, len(df))


def data_path(sym, data_dir=DATA_DIR):
    return os.path.join(data_dir, sym.replace("/", "") + "_1h.csv")


def load(sym, data_dir=DATA_DIR) -> pd.DataFrame:
    """1H bars indexed by each bar's OPEN time (UTC)."""
    df = pd.read_csv(data_path(sym, data_dir))
    df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
    return df.sort_index()


# ---------------------------------------------------------------- indicators

def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df, n):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def resample_bars(h1: pd.DataFrame, hours: int, label_close=True) -> pd.DataFrame:
    """N-hour bars built from 1H bars, keeping only complete ones (all N hours present). With
    label_close the index is the time each bar CLOSES, so it can only be used once finished;
    otherwise the index is the bar's open time, like the hourly data."""
    if hours == 1:
        out = h1.copy()
        if label_close:
            out.index = out.index + HOUR
        return out
    g = h1.resample(f"{hours}h", origin="epoch", label="left", closed="left")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    out = out[g.size() == hours]
    if label_close:
        out.index = out.index + pd.Timedelta(hours=hours)
    return out


def four_hour(h1: pd.DataFrame) -> pd.DataFrame:
    return resample_bars(h1, 4)


# ---------------------------------------------------------------- signals

def signals(h1: pd.DataFrame, p: Params) -> pd.DataFrame:
    """One row per entry bar, judged at that bar's CLOSE. Uses only data available by then."""
    bar = pd.Timedelta(hours=p.entry_hours)
    df = resample_bars(h1, p.entry_hours, label_close=False)
    df["close_time"] = df.index + bar
    df["ema_pull"] = ema(df["close"], p.ema_pull)
    df["ema_struct"] = ema(df["close"], p.ema_structure)
    df["atr"] = atr(df, p.atr_len)
    df["rsi"] = rsi(df["close"], p.rsi_len)
    df["atr_pct"] = df["atr"] / df["close"] * 100

    # Trend regime, attached to each entry bar from the latest trend bar that had CLOSED by then.
    h4 = resample_bars(h1, p.trend_hours)
    r = pd.DataFrame(index=h4.index)
    r["h4_close"] = h4["close"]
    r["h4_fast"] = ema(h4["close"], p.ema_trend_fast)
    r["h4_slow"] = ema(h4["close"], p.ema_trend_slow)
    r["h4_slope_up"] = r["h4_fast"] > r["h4_fast"].shift(p.slope_bars_4h)
    r["trend_up"] = (r["h4_close"] > r["h4_slow"]) & (r["h4_fast"] > r["h4_slow"]) & r["h4_slope_up"]
    r["trend_broken"] = r["h4_close"] < r["h4_slow"]
    r = r.rename_axis("h4_close_time").reset_index()
    df = pd.merge_asof(df.reset_index(names="open_time"), r, left_on="close_time", right_on="h4_close_time",
                       direction="backward").set_index("open_time")

    L = p.pullback_lookback
    shock = (df["high"] - df["low"]) > p.shock_atr_mult * df["atr"].shift(1)
    df["shock_recent"] = shock.astype(float).rolling(p.shock_lookback, min_periods=1).max().fillna(0).astype(bool)
    df["vol_ok"] = df["atr_pct"].between(p.min_atr_pct, p.max_atr_pct)
    df["regime_ok"] = df["trend_up"].astype("boolean").fillna(False).astype(bool) & df["vol_ok"] & ~df["shock_recent"]

    # Missing values (the warm-up at the start) count as "no", never as "yes".
    touched = (df["low"] <= df["ema_pull"]).astype(float).rolling(L, min_periods=L).max().fillna(0).astype(bool)
    held = (df["close"] >= df["ema_struct"]).astype(float).rolling(L, min_periods=L).min().fillna(0).astype(bool)
    resume = (df["close"] > df["ema_pull"]) & (df["close"] > df["high"].shift(1))
    rsi_ok = df["rsi"].between(p.rsi_min, p.rsi_max)
    df["stop_level"] = df["low"].rolling(L, min_periods=L).min() - p.stop_atr_buffer * df["atr"]
    stop_dist = df["close"] - df["stop_level"]
    df["stop_ok"] = stop_dist.between(p.min_stop_atr * df["atr"], p.max_stop_atr * df["atr"])
    df["entry_signal"] = df["regime_ok"] & touched & held & resume & rsi_ok & df["stop_ok"]
    df["regime_exit"] = df["trend_broken"].astype("boolean").fillna(False).astype(bool)
    return df


# ---------------------------------------------------------------- backtest

@dataclass
class Position:
    symbol: str
    entry_time: pd.Timestamp
    entry_price: float
    qty: float
    stop: float
    target: float
    risk_per_unit: float
    entry_fee: float
    bars_held: int = 0
    breakeven: bool = False


@dataclass
class Result:
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)   # (time, equity) at each bar close


def size_position(equity, entry, stop, open_notional, p: Params):
    """Units to buy so a stop-out (with fees and stop slippage) costs risk_pct of equity, within caps."""
    per_unit_loss = (entry - stop) + entry * p.fee_pct / 100 + stop * (p.fee_pct + p.stop_slippage_pct) / 100
    if per_unit_loss <= 0:
        return 0.0
    qty = equity * p.risk_pct / 100 / per_unit_loss
    qty = min(qty, equity * p.max_position_pct / 100 / entry)            # one position's cap
    qty = min(qty, max(0.0, equity - open_notional) / (entry * (1 + p.fee_pct / 100)))  # no leverage
    return max(qty, 0.0)


def backtest(frames: dict, p: Params) -> Result:
    """frames: {symbol: signals() output}. Portfolio-level: shared equity, caps across symbols."""
    times = sorted(set().union(*[f.index for f in frames.values()]))
    BAR = pd.Timedelta(hours=p.entry_hours)
    cash, res = p.starting_equity, Result()
    open_pos: dict[str, Position] = {}
    pending: dict[str, tuple] = {}            # symbol -> ("enter", stop) or ("exit", reason), done at next open
    cooldown_until: dict[str, pd.Timestamp] = {}
    day, day_start_equity, halted_day = None, p.starting_equity, None
    last_close = {}

    def equity_now():
        return cash + sum(pos.qty * last_close.get(s, pos.entry_price) for s, pos in open_pos.items())

    def close_pos(sym, t, price, reason):
        nonlocal cash
        pos = open_pos.pop(sym)
        fee = price * pos.qty * p.fee_pct / 100
        cash += price * pos.qty - fee
        pnl = (price - pos.entry_price) * pos.qty - pos.entry_fee - fee
        r_mult = pnl / (pos.risk_per_unit * pos.qty) if pos.risk_per_unit > 0 else 0.0
        res.trades.append({"symbol": sym, "entry_time": str(pos.entry_time), "exit_time": str(t),
                           "entry": round(pos.entry_price, 4), "exit": round(price, 4), "qty": pos.qty,
                           "pnl": round(pnl, 4), "r": round(r_mult, 3), "bars": pos.bars_held, "reason": reason})
        log.debug("EXIT %s %s @ %.2f pnl %.2f (%s)", sym, t, price, pnl, reason)
        if reason == "stop":
            cooldown_until[sym] = t + p.cooldown_bars * BAR

    for t in times:
        bar_day = t.date()
        if bar_day != day:
            day, day_start_equity = bar_day, equity_now()

        # 1) orders decided at the previous close fill at this bar's open
        for sym, (kind, val) in list(pending.items()):
            f = frames[sym]
            if t not in f.index:
                continue
            o = f.at[t, "open"]
            del pending[sym]
            if kind == "exit" and sym in open_pos:
                close_pos(sym, t, o * (1 - p.slippage_pct / 100), val)
            elif kind == "enter" and sym not in open_pos and len(open_pos) < p.max_positions:
                entry = o * (1 + p.slippage_pct / 100)
                stop = val
                if stop >= entry:                                        # gapped below the stop: skip
                    continue
                notional_open = sum(x.qty * x.entry_price for x in open_pos.values())
                qty = size_position(equity_now(), entry, stop, notional_open, p)
                if qty * entry < 10:                                     # too small to be worth an order
                    continue
                fee = entry * qty * p.fee_pct / 100
                cash -= entry * qty + fee
                risk = entry - stop
                open_pos[sym] = Position(sym, t, entry, qty, stop, entry + p.take_profit_r * risk, risk, fee)
                log.debug("ENTER %s %s @ %.2f qty %.6f stop %.2f", sym, t, entry, qty, stop)

        # 2) stops and targets inside this bar (stop assumed first if both are inside the range)
        for sym, pos in list(open_pos.items()):
            f = frames[sym]
            if t not in f.index:
                continue
            o, h, l = f.at[t, "open"], f.at[t, "high"], f.at[t, "low"]
            if l <= pos.stop:
                fill = min(o, pos.stop) * (1 - p.stop_slippage_pct / 100)
                close_pos(sym, t, fill, "breakeven" if pos.breakeven else "stop")
            elif h >= pos.target:
                close_pos(sym, t, max(o, pos.target), "target")

        # 3) at this bar's close: update marks, manage open trades, look for new entries
        for sym, f in frames.items():
            if t not in f.index:
                continue
            row = f.loc[t]
            last_close[sym] = row["close"]
            pos = open_pos.get(sym)
            if pos:
                pos.bars_held += 1
                if not pos.breakeven and row["high"] >= pos.entry_price + p.breakeven_at_r * pos.risk_per_unit:
                    pos.stop = max(pos.stop, pos.entry_price * (1 + 2 * p.fee_pct / 100))
                    pos.breakeven = True
                if row["regime_exit"]:
                    pending[sym] = ("exit", "regime")
                elif pos.bars_held >= p.max_hold_bars:
                    pending[sym] = ("exit", "time")

        eq = equity_now()
        if eq < day_start_equity * (1 - p.daily_loss_pct / 100):
            halted_day = day
        for sym, f in frames.items():
            if t not in f.index or sym in open_pos or sym in pending:
                continue
            if halted_day == day or t + BAR < cooldown_until.get(sym, t):
                continue
            row = f.loc[t]
            if row["entry_signal"] and len(open_pos) + sum(1 for k in pending.values() if k[0] == "enter") < p.max_positions:
                pending[sym] = ("enter", float(row["stop_level"]))
        res.equity.append((t + BAR, eq))

    for sym in list(open_pos):                                            # mark anything still open
        close_pos(sym, times[-1] + BAR, last_close[sym], "end")
    return res


# ---------------------------------------------------------------- reporting

def max_drawdown(values):
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


def stats(trades, equity, start_equity):
    if not equity:
        return {}
    eq = [e for _, e in equity]
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    rs = [t["r"] for t in trades]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"trades": len(trades), "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            "avg_r": round(float(np.mean(rs)), 3) if rs else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "return_pct": round((eq[-1] / start_equity - 1) * 100, 2),
            "max_drawdown_pct": round(max_drawdown(eq) * 100, 2), "exits": reasons}


def buy_and_hold(frames, p: Params, t0, t1):
    out = 0.0
    for f in frames.values():
        s = f.loc[(f.index >= t0) & (f.index < t1), "close"]
        if len(s) > 1:
            out += (s.iloc[-1] / s.iloc[0] - 1) / len(frames)
    return round(out * 100, 2)


def report(res: Result, frames, p: Params):
    """Whole period, each calendar year, and the two halves (rules were fixed before any run;
    the halves are a check that results don't come from one lucky stretch)."""
    t_first, t_last = res.equity[0][0], res.equity[-1][0]
    BAR = pd.Timedelta(hours=p.entry_hours)
    out = {"period": [str(t_first.date()), str(t_last.date())], "params": asdict(p),
           "all": stats(res.trades, res.equity, p.starting_equity),
           "buy_and_hold_pct": buy_and_hold(frames, p, t_first - BAR, t_last), "years": {}, "halves": {}}
    eq = pd.Series([e for _, e in res.equity], index=[t for t, _ in res.equity])

    def window(a, b):
        tr = [x for x in res.trades if a <= pd.Timestamp(x["entry_time"]) < b]
        seg = eq[(eq.index > a) & (eq.index <= b)]
        if seg.empty:
            return None
        before = eq[eq.index <= a]
        start = before.iloc[-1] if len(before) else p.starting_equity
        s = stats(tr, list(zip(seg.index, seg.values)), start)
        s["buy_and_hold_pct"] = buy_and_hold(frames, p, a, b)
        return s

    for y in range(t_first.year, t_last.year + 1):
        a = pd.Timestamp(f"{y}-01-01", tz="UTC"); b = pd.Timestamp(f"{y + 1}-01-01", tz="UTC")
        w = window(a, b)
        if w:
            out["years"][str(y)] = w
    mid = t_first + (t_last - t_first) / 2
    out["halves"]["first"] = window(t_first - BAR, mid)
    out["halves"]["second"] = window(mid, t_last)
    return out


# ---------------------------------------------------------------- live order plan (no orders sent)

def order_plan(symbol, qty, stop, target, stop_limit_offset_pct=0.5):
    """What a live version sends. Alpaca crypto takes only simple orders (market, limit, stop_limit;
    gtc or ioc): no bracket or OCO. So the stop rests on the exchange the moment the entry fills,
    and the script manages the target. If the script dies, the exchange-side stop still protects
    the position; only the take-profit and time/regime exits wait for the script to come back."""
    return [
        {"step": 1, "side": "buy", "type": "market", "symbol": symbol, "qty": qty, "time_in_force": "gtc",
         "note": "entry; wait for the fill before step 2"},
        {"step": 2, "side": "sell", "type": "stop_limit", "symbol": symbol, "qty": "filled qty",
         "stop_price": round(stop, 2), "limit_price": round(stop * (1 - stop_limit_offset_pct / 100), 2),
         "time_in_force": "gtc",
         "note": "exchange-side stop. A stop-limit can skip past its limit in a crash: watch it"},
        {"step": 3, "side": "sell", "type": "script-managed", "symbol": symbol, "trigger": f"price >= {target:.2f}",
         "note": "take-profit: cancel the step-2 stop, then sell (a second resting sell would reserve the same coins)"},
        {"step": 4, "type": "script-managed", "note": "at +1R replace the step-2 stop at break-even; "
                                                       "time stop after 48 bars; regime-break exit"},
    ]


# Version A is the default Params(). Version B was fixed BEFORE it was first run, after A failed:
# slower bars so the stop sits further away and costs are a smaller share of each trade.
PRESETS = {
    "A": {},
    "B": {"entry_hours": 4, "trend_hours": 24, "min_atr_pct": 0.8, "max_atr_pct": 6.0,
          "max_hold_bars": 30, "cooldown_bars": 2},
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--download", action="store_true", help="fetch hourly bars first (no keys needed)")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD")
    ap.add_argument("--version", choices=sorted(PRESETS), default="A",
                    help="A: 1H entries + 4H trend (default). B: 4H entries + daily trend")
    ap.add_argument("--risk-pct", type=float, default=Params.risk_pct)
    ap.add_argument("--fee-pct", type=float, default=Params.fee_pct)
    ap.add_argument("--equity", type=float, default=Params.starting_equity)
    ap.add_argument("--trades-csv", help="write every trade to this CSV")
    ap.add_argument("--json", help="write the full report to this JSON file")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every entry and exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")
    p = Params(symbols=tuple(s.strip().upper() for s in a.symbols.split(",")), risk_pct=a.risk_pct,
               fee_pct=a.fee_pct, starting_equity=a.equity, **PRESETS[a.version])
    if a.download:
        download(p.symbols, a.start, out_dir=a.data_dir)
    frames = {s: signals(load(s, a.data_dir), p) for s in p.symbols}
    res = backtest(frames, p)
    rep = report(res, frames, p)
    al = rep["all"]
    print(f"\nCrypto trend pullback, version {a.version} ({p.entry_hours}H entries, {p.trend_hours}H trend) · "
          f"{', '.join(p.symbols)} · {rep['period'][0]} to {rep['period'][1]}")
    print(f"  start ${p.starting_equity:,.0f} · risk {p.risk_pct}%/trade · fee {p.fee_pct}%/side · "
          f"slippage {p.slippage_pct}% (+{p.stop_slippage_pct}% on stops)")
    print(f"  strategy {al['return_pct']:+.1f}%   max drawdown {al['max_drawdown_pct']:.1f}%   "
          f"buy-and-hold (50/50) {rep['buy_and_hold_pct']:+.1f}%")
    print(f"  {al['trades']} trades · win rate {al['win_rate']} · avg {al['avg_r']}R · "
          f"profit factor {al['profit_factor']} · exits {al['exits']}")
    print("  by year:")
    for y, s in rep["years"].items():
        print(f"    {y}: {s['return_pct']:+6.1f}%  dd {s['max_drawdown_pct']:6.1f}%  {s['trades']:>3} trades  "
              f"avg {s['avg_r']}R   (buy-and-hold {s['buy_and_hold_pct']:+.1f}%)")
    for k, s in rep["halves"].items():
        if s:
            print(f"  {k} half: {s['return_pct']:+.1f}%, {s['trades']} trades, avg {s['avg_r']}R, "
                  f"PF {s['profit_factor']}")
    if a.trades_csv:
        pd.DataFrame(res.trades).to_csv(a.trades_csv, index=False)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1, default=str)
    print("\nBacktest only. Past results don't predict future ones; nothing here places orders.")


if __name__ == "__main__":
    main()
