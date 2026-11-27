"""Trading desk sim: replays past prices and headlines through the real bot, agents and all.

Unlike news_backtest.py (which re-implements the rules in a simplified loop), this drives the live
code itself: bot.run_cycle with every agent, the Head of Desk, Kelly sizing, the exposure cap,
cooldowns, daily limits and the circuit breaker. Only the broker and the clock are simulated. It
never touches your account and never places an order.

  * Prices: 5-minute bars (Alpaca's free IEX feed). Orders "fill" at the last bar's close, so real
    fills would be a little worse.
  * Settings: the same ones the live bot uses, read from .github/workflows/trade.yml.
  * Checks: every 15 minutes of market hours, like the scheduled workflow.

Run:
  python desk_sim.py --days 90              download real history (needs Alpaca keys in .env)
  python desk_sim.py --days 90 --compare    also run with CRASH off, to see what CRASH changes
  python desk_sim.py --synthetic            made-up data, no keys: checks the plumbing, NOT the strategy
  python desk_sim.py --save-data h.json     keep the download;  --data h.json  to replay it offline
"""

import argparse
import bisect
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import config
import desk
import news_backtest

NY = ZoneInfo("America/New_York")
BAR_MINUTES = news_backtest.BAR_MINUTES
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "trade.yml")


# ---------------- settings ----------------

def live_settings(path=WORKFLOW):
    """The KEY: "value" settings from the live workflow's env block (secrets are skipped)."""
    out = {}
    with open(path) as f:
        for line in f:
            m = re.match(r'\s+([A-Z][A-Z0-9_]+):\s*"([^"]*)"\s*$', line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def apply_settings(env):
    """Set config values from {NAME: "text"} the way config.py would have read them. Returns the old
    values so they can be put back."""
    old = {}
    for k, v in env.items():
        if k == "STAKES":
            old.update(STAKES=config.STAKES, SYMBOLS=config.SYMBOLS)
            config.STAKES = config._parse_stakes(v, 100.0)
            config.SYMBOLS = list(config.STAKES)
        elif k == "STRATEGY":
            old[k] = config.STRATEGY
            config.STRATEGY = v.strip().lower()
        elif hasattr(config, k) and isinstance(getattr(config, k), (int, float)) and not isinstance(getattr(config, k), bool):
            old[k] = getattr(config, k)
            setattr(config, k, type(old[k])(float(v)) if isinstance(old[k], int) else float(v))
    return old


def restore_settings(old):
    for k, v in old.items():
        setattr(config, k, v)


# ---------------- simulated broker ----------------

class SimBroker:
    """Looks like broker.Broker to bot.py, but answers from history as of the sim clock."""

    def __init__(self, bars, articles, cash, clock):
        self.bars, self.articles, self.cash, self.clock = bars, articles, cash, clock
        self.times = {s: [t for t, _ in rows] for s, rows in bars.items()}
        self.pos = {}      # symbol -> {"qty", "cost"}
        self.fills = []    # every simulated order

    def _i(self, sym):
        return bisect.bisect_right(self.times.get(sym, []), self.clock.t) - 1

    def price(self, sym):
        i = self._i(sym)
        return self.bars[sym][i][1] if i >= 0 else None

    def is_market_open(self):
        return True

    def minutes_to_close(self):
        ny = datetime.fromtimestamp(self.clock.t, NY)
        return 16 * 60 - (ny.hour * 60 + ny.minute)

    def minutes_since_open(self):
        ny = datetime.fromtimestamp(self.clock.t, NY)
        return ny.hour * 60 + ny.minute - (9 * 60 + 30)

    def account_summary(self):
        held = sum(p["qty"] * (self.price(s) or 0) for s, p in self.pos.items())
        return {"equity": self.cash + held, "cash": self.cash, "buying_power": self.cash, "paper": True}

    def recent_closes(self, sym, lookback=30):
        """The live bot asks for `lookback` one-minute bars; history here is 5-minute bars."""
        i = self._i(sym)
        if i < 0:
            return []
        n = max(2, lookback // BAR_MINUTES)
        return [c for _, c in self.bars[sym][max(0, i - n + 1):i + 1]]

    def recent_news(self, sym, hours=24, limit=50):
        lo, hi = self.clock.t - hours * 3600, self.clock.t
        hits = [a for a in self.articles.get(sym, []) if lo < a["t"] <= hi]
        return sorted(hits, key=lambda a: a["t"], reverse=True)[:limit]

    def get_position_qty(self, sym):
        return self.pos.get(sym, {}).get("qty", 0.0)

    def get_entry_price(self, sym):
        p = self.pos.get(sym)
        return p["cost"] / p["qty"] if p and p["qty"] else None

    def get_position(self, sym):
        p, price = self.pos.get(sym), self.price(sym)
        if not p or not p["qty"] or price is None:
            return None
        value = p["qty"] * price
        return {"qty": p["qty"], "entry": p["cost"] / p["qty"], "cost": p["cost"], "value": value,
                "pnl": value - p["cost"]}

    def buy_notional(self, sym, dollars):
        price = self.price(sym)
        p = self.pos.setdefault(sym, {"qty": 0.0, "cost": 0.0})
        p["qty"] += dollars / price
        p["cost"] += dollars
        self.cash -= dollars
        self.fills.append({"t": self.clock.t, "symbol": sym, "side": "BUY", "price": price, "dollars": dollars})
        return type("Order", (), {"id": f"sim-{len(self.fills)}"})()

    def sell_all(self, sym):
        p, price = self.pos.pop(sym, None), self.price(sym)
        if not p:
            return None
        self.cash += p["qty"] * price
        self.fills.append({"t": self.clock.t, "symbol": sym, "side": "SELL", "price": price,
                           "dollars": p["qty"] * price, "cost": p["cost"]})
        return type("Order", (), {"id": f"sim-{len(self.fills)}"})()


class Clock:
    t = 0.0


def _sim_datetime(clock):
    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.t, tz)
    return SimDatetime


# ---------------- running ----------------

def run(bars, articles, step_bars=3, crash_pct=None):
    """Replays history through bot.run_cycle. Returns a result dict (see summarize)."""
    import bot
    if config.STRATEGY != "news":
        raise SystemExit("The sim supports the news strategy (what the live bot runs). Set STRATEGY=news.")
    old_crash = config.CRASH_BLOCK_PCT
    old_state = config.STATE_FILE
    if crash_pct is not None:
        config.CRASH_BLOCK_PCT = crash_pct
    clock = Clock()
    broker = SimBroker(bars, articles, config.TOTAL_BUDGET, clock)
    log = logging.getLogger("desk-sim")
    log.propagate = False                      # the desk's own record is the report; keep the console quiet
    tmp = tempfile.mkdtemp()
    config.STATE_FILE = os.path.join(tmp, "state.json")
    times = sorted({t for rows in bars.values() for t, _ in rows})
    verdicts, alerts, paused_at = [], [], None

    real_alert = desk.HeadOfDesk.alert

    def spy_alert(self, symbol, message, level="warn"):
        real_alert(self, symbol, message, level)
        alerts.append(dict(self.alerts[-1], t=clock.t))

    try:
        with mock.patch("time.time", lambda: clock.t), \
             mock.patch.object(bot, "datetime", _sim_datetime(clock)), \
             mock.patch.object(bot.real_portfolio, "update", lambda *a: (None, None)), \
             mock.patch.object(desk.HeadOfDesk, "alert", spy_alert):
            for k, t in enumerate(times):
                if k % step_bars:
                    continue
                clock.t = t
                bot.run_cycle(broker, log)
                with open(config.STATE_FILE) as f:
                    st = json.load(f)
                if st.get("paused") and paused_at is None:
                    paused_at = t
                rep = st.get("desk") or {}
                if rep.get("t") == int(t):
                    for sym, r in rep.get("symbols", {}).items():
                        verdicts.extend(dict(v, symbol=sym, t=t) for v in r["verdicts"])
    finally:
        config.CRASH_BLOCK_PCT = old_crash
        config.STATE_FILE = old_state

    last = {s: rows[-1][1] for s, rows in bars.items() if rows}
    clock.t = max(times) if times else 0
    return {"fills": broker.fills, "verdicts": verdicts, "alerts": alerts, "paused_at": paused_at,
            "checks": len(range(0, len(times), step_bars)), "final_equity": broker.account_summary()["equity"],
            "open": {s: dict(p, price=last.get(s)) for s, p in broker.pos.items()}}


def round_trips(fills, open_pos):
    """Pairs each BUY with the SELL that closed it. Positions still open are valued at the last price."""
    out, opened = [], {}
    for f in fills:
        if f["side"] == "BUY":
            opened.setdefault(f["symbol"], []).append(f)
        else:
            buys = opened.pop(f["symbol"], [])
            out.append({"symbol": f["symbol"], "buy_t": buys[0]["t"] if buys else None, "sell_t": f["t"],
                        "pnl": f["dollars"] - f["cost"], "open": False})
    for sym, p in open_pos.items():
        buys = opened.get(sym, [])
        out.append({"symbol": sym, "buy_t": buys[0]["t"] if buys else None, "sell_t": None,
                    "pnl": p["qty"] * p["price"] - p["cost"], "open": True})
    return out


SCORE = re.compile(r"news score ([+-]?\d+\.\d+) from (\d+) article")


def why_search(verdicts):
    """Per stock, from SEARCH's own notes: best news score seen, how often it reached the buy line,
    and what SEARCH said at those moments. Answers "why didn't it trade?"."""
    out = {}
    for v in verdicts:
        if v["agent"] != "search":
            continue
        m = SCORE.search(v.get("note", ""))
        if not m:
            continue
        w = out.setdefault(v["symbol"], {"checks": 0, "with_news": 0, "best": None, "at_line": {}})
        score, n = float(m.group(1)), int(m.group(2))
        w["checks"] += 1
        w["with_news"] += n > 0
        w["best"] = score if w["best"] is None else max(w["best"], score)
        if score >= config.NEWS_BUY_THRESHOLD:
            said = ("bought" if v["action"] == "buy" else "already priced in" if "priced in" in v["note"]
                    else "price falling" if "disagrees" in v["note"]
                    else "already holding" if v["action"] == "hold" else v["action"])
            w["at_line"][said] = w["at_line"].get(said, 0) + 1
    return out


def summarize(res, bars):
    budget = config.TOTAL_BUDGET
    rts = round_trips(res["fills"], res["open"])
    closed = [r for r in rts if not r["open"]]
    hold = sum(config.STAKES.get(s, 0) * (rows[-1][1] / rows[0][1] - 1) for s, rows in bars.items() if rows)
    agents = {}
    for v in res["verdicts"]:
        a = agents.setdefault(v["agent"], {"calls": 0, "by": {}})
        a["calls"] += 1
        key = v.get("tag") or v["action"]
        if v["agent"] == "desk":
            key = ("approved " if v["action"] == "approve" else "blocked ") + \
                  ("buy" if "buy" in v.get("note", "") else "sell")
        a["by"][key] = a["by"].get(key, 0) + 1
    return {"why": why_search(res["verdicts"]), "pnl": res["final_equity"] - budget, "hold_pnl": hold, "budget": budget,
            "round_trips": len(closed), "wins": sum(r["pnl"] > 0 for r in closed),
            "still_open": [r for r in rts if r["open"]], "agents": agents, "alerts": res["alerts"],
            "paused_at": res["paused_at"], "checks": res["checks"], "buys": sum(f["side"] == "BUY" for f in res["fills"])}


# ---------------- report ----------------

AGENT_ORDER = [("search", "SEARCH"), ("exit", "EXIT"), ("risk", "RISK"), ("crash", "CRASH"),
               ("sizer", "SIZER"), ("desk", "HEAD OF DESK")]


def _day(t):
    return datetime.fromtimestamp(t, NY).strftime("%b %d")


def report(s, bars, title):
    t0 = min(rows[0][0] for rows in bars.values() if rows)
    t1 = max(rows[-1][0] for rows in bars.values() if rows)
    pct = lambda x: f"{x / s['budget'] * 100:+.1f}%"
    lines = [title, f"  {_day(t0)} to {_day(t1)} · {s['checks']:,} checks · ${s['budget']:.2f} budget · "
                    f"stocks: {', '.join(f'{k} ${v:g}' for k, v in config.STAKES.items())}", ""]
    wr = f"{s['wins'] / s['round_trips']:.0%}" if s["round_trips"] else "—"
    lines += [f"  Bot:           ${s['pnl']:+.2f}  ({pct(s['pnl'])} of budget)",
              f"  Just holding:  ${s['hold_pnl']:+.2f}  ({pct(s['hold_pnl'])})",
              f"  Trades:        {s['buys']} buys, {s['round_trips']} closed, {s['wins']} won ({wr})"
              + (f", {len(s['still_open'])} still open" if s["still_open"] else "")]
    lines.append("  Circuit breaker: " + (f"tripped {_day(s['paused_at'])}. The bot stopped opening trades "
                                          f"after that and waits for you, just like live."
                                          if s["paused_at"] else "never tripped"))
    lines += ["", "  The desk (each agent's calls; vetoes and exits by reason):"]
    for key, name in AGENT_ORDER:
        a = s["agents"].get(key)
        if not a:
            note = "turned off" if key == "crash" and config.CRASH_BLOCK_PCT <= 0 else "never needed"
            lines.append(f"    {name:<13} {note}")
            continue
        by = ", ".join(f"{k} {n}" for k, n in sorted(a["by"].items(), key=lambda kv: -kv[1]))
        lines.append(f"    {name:<13} {a['calls']:>6,}  {by}")
    if s.get("why"):
        lines += ["", f"  Why SEARCH did or didn't buy (buy line: news score +{config.NEWS_BUY_THRESHOLD:.2f}):"]
        for sym in config.SYMBOLS:
            w = s["why"].get(sym)
            if not w:
                continue
            at = sum(w["at_line"].values())
            detail = ", ".join(f"{k} {n}" for k, n in w["at_line"].items())
            lines.append(f"    {sym:<6} headlines at {w['with_news'] / w['checks']:.0%} of checks · best score "
                         f"{w['best']:+.2f} · at/over the line {at} time{'s' if at != 1 else ''}"
                         + (f" ({detail})" if detail else ""))
    alerts = s["alerts"]
    lines.append(f"\n  Alerts to you: {len(alerts)}")
    for a in alerts[:8]:
        lines.append(f"    {_day(a['t'])}  {a.get('symbol') or 'DESK'}: {a['message'][:150]}")
    if len(alerts) > 8:
        lines.append(f"    … and {len(alerts) - 8} more")
    return "\n".join(lines)


# ---------------- data ----------------

def synthetic(symbols, days, seed=7):
    """Made-up 5-minute prices and headlines, with a few spike-and-crash episodes so every agent gets
    exercised. There is no real edge built in: results say nothing about the strategy."""
    rnd = random.Random(seed)
    end = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
    day = end - timedelta(days=days)
    bars = {s: [] for s in symbols}
    articles = {s: [] for s in symbols}
    price = {s: rnd.uniform(100, 600) for s in symbols}
    good = ["{s} beats estimates, raises guidance", "Analysts upgrade {s}", "{s} posts record revenue"]
    bad = ["{s} misses estimates", "{s} faces investigation", "Analysts downgrade {s}"]
    meh = ["{s} to present at conference", "{s} announces dividend date"]
    aid = 0
    while day < end:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        open_t = day.replace(hour=9, minute=30).timestamp()
        for s in symbols:
            crash_at = rnd.randrange(78) if rnd.random() < 0.08 else None
            for i in range(78):
                shock = rnd.gauss(0, 0.0015)
                if crash_at is not None and crash_at <= i < crash_at + 3:
                    shock = 0.015                       # spike...
                elif crash_at is not None and crash_at + 3 <= i < crash_at + 7:
                    shock = -0.011                      # ...and collapse back to about where it started
                price[s] *= 1 + shock
                bars[s].append((open_t + (i + 1) * BAR_MINUTES * 60, round(price[s], 2)))
            if crash_at is not None:                    # the spike comes with good headlines
                for j in range(2):
                    aid += 1
                    articles[s].append({"id": aid, "t": open_t + (crash_at + j) * BAR_MINUTES * 60,
                                        "headline": good[j].format(s=s), "summary": ""})
            for _ in range(rnd.randint(0, 3)):
                aid += 1
                pool = rnd.choice([good, good, bad, meh])
                articles[s].append({"id": aid, "t": open_t + rnd.uniform(-6, 6.5) * 3600,
                                    "headline": rnd.choice(pool).format(s=s), "summary": ""})
    for s in symbols:
        articles[s].sort(key=lambda a: a["t"])
    return bars, articles


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--step", type=int, default=3, help="check every N five-minute bars (3 = every 15 min)")
    p.add_argument("--compare", action="store_true", help="also run with CRASH off and show the difference")
    p.add_argument("--synthetic", action="store_true", help="made-up data (no keys); tests the plumbing only")
    p.add_argument("--data", help="replay a file saved earlier with --save-data")
    p.add_argument("--save-data", help="save the downloaded history to this file")
    p.add_argument("--own-settings", action="store_true",
                   help="use your .env settings instead of the live workflow's")
    args = p.parse_args()
    if args.days < 1 or args.step < 1:
        p.error("--days and --step must be at least 1")

    if not args.own_settings:
        apply_settings(live_settings())

    if args.data:
        with open(args.data) as f:
            raw = json.load(f)
        bars = {s: [tuple(r) for r in rows] for s, rows in raw["bars"].items()}
        articles, source = raw["articles"], f"saved history ({args.data})"
    elif args.synthetic:
        bars, articles = synthetic(config.SYMBOLS, args.days)
        source = "SYNTHETIC data (made up; checks the plumbing, says nothing about the strategy)"
    else:
        problems = [x for x in config.validate() if "ALPACA" in x]
        if problems:
            p.error(problems[0] + " (or try --synthetic)")
        print(f"Downloading {args.days} days of prices and headlines for {', '.join(config.SYMBOLS)}...")
        bars, articles = news_backtest.fetch(config.SYMBOLS, args.days)
        source = f"real prices and headlines, last {args.days} days"
    if args.save_data:
        with open(args.save_data, "w") as f:
            json.dump({"bars": bars, "articles": articles}, f)
    bars = {s: rows for s, rows in bars.items() if rows}
    if not bars:
        sys.exit("No price data.")

    started = time.time()
    res = run(bars, articles, args.step)
    s = summarize(res, bars)
    print()
    print(report(s, bars, f"TRADING DESK SIM · {source}"))
    if args.compare:
        res0 = run(bars, articles, args.step, crash_pct=0)
        s0 = summarize(res0, bars)
        print()
        print("WITH CRASH OFF (same data, same everything else):")
        print(f"  Bot: ${s0['pnl']:+.2f}  vs  ${s['pnl']:+.2f} with CRASH on  →  CRASH changed P/L by "
              f"${s['pnl'] - s0['pnl']:+.2f}")
        print(f"  Buys: {s0['buys']} without CRASH, {s['buys']} with it. "
              f"Closed trades won: {s0['wins']}/{s0['round_trips']} vs {s['wins']}/{s['round_trips']}.")
    print(f"\n(took {time.time() - started:.1f}s. Fills are at the last 5-minute close, so real orders would "
          f"do slightly worse. Past results don't predict future ones.)")


if __name__ == "__main__":
    main()
