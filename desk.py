"""Head of Desk: reviews every trade the agents propose before it reaches the broker.

It never places orders and never invents trades. It can only say yes or no, and it keeps its own
count of what happened this cycle instead of trusting the agents' bookkeeping. That way a bug in
one agent (a stake that's too big, a second buy of the same stock) gets caught by a second check.

Rules:
  * Sells are always approved. Blocking a stop-loss would remove the safety net, so the desk only
    records exits; it never stands in their way.
  * A buy is approved only if SEARCH proposed it, no agent vetoed it, the price is real, the size
    is positive and within that stock's stake, the stock hasn't already traded this cycle, the
    daily trade limit isn't reached, and the money put into stocks (at cost) would stay within
    TOTAL_BUDGET.
  * It "calls you in" (an alert) only when a person should look: the circuit breaker tripped, the
    desk had to block something an agent let through, an order failed, or CRASH stopped a buy that
    SEARCH liked.
"""

import time

import config

MAX_NOTE = 300


class HeadOfDesk:
    def __init__(self, cycle_t=None):
        self.t = cycle_t or time.time()
        self.verdicts = {}      # symbol -> [verdict dicts, in the order the agents spoke]
        self.final = {}         # symbol -> final action this cycle
        self.alerts = []
        self.traded = set()     # symbols bought or sold this cycle (desk's own count)
        self.spent = 0.0        # dollars bought this cycle (desk's own count)

    # -- listening ---------------------------------------------------------------------------
    def hear(self, symbol, verdict):
        if verdict is None:
            return
        d = verdict.to_dict()
        d["note"] = d.get("note", "")[:MAX_NOTE]
        self.verdicts.setdefault(symbol, []).append(d)

    def alert(self, symbol, message, level="warn"):
        self.alerts.append({"t": time.time(), "symbol": symbol, "level": level, "message": message[:MAX_NOTE]})

    # -- reviewing ---------------------------------------------------------------------------
    def review_buy(self, symbol, stake, price, open_value, trades_today):
        """Returns (approved, note). open_value: dollars already put into open positions in earlier
        cycles, at cost."""
        said = self.verdicts.get(symbol, [])
        problem = None
        if not any(v["agent"] == "search" and v["action"] == "buy" for v in said):
            problem = "SEARCH never proposed this buy"
        elif any(v["action"] == "veto" for v in said):
            vetoers = sorted({v["agent"].upper() for v in said if v["action"] == "veto"})
            problem = f"vetoed by {', '.join(vetoers)}"
        elif not price or price <= 0:
            problem = "no valid price"
        elif not stake or stake <= 0:
            problem = "size is zero"
        elif stake > config.STAKES.get(symbol, 0) + 0.01:
            problem = f"size ${stake:.2f} is over this stock's ${config.STAKES.get(symbol, 0):.2f} stake"
        elif symbol in self.traded:
            problem = "already traded this stock this cycle"
        elif trades_today >= config.MAX_TRADES_PER_DAY:
            problem = f"daily limit of {config.MAX_TRADES_PER_DAY} trades reached"
        elif open_value + self.spent + stake > config.TOTAL_BUDGET + 0.01:
            problem = (f"would have ${open_value + self.spent + stake:.2f} put into stocks, "
                       f"over the ${config.TOTAL_BUDGET:.2f} budget")
        if problem:
            self._note(symbol, "veto", f"blocked buy: {problem}")
            self.alert(symbol, f"Head of Desk blocked a ${stake or 0:.2f} buy — {problem}. "
                               f"An agent let this through; worth a look.")
            return False, problem
        self._note(symbol, "approve", f"approved buy ${stake:.2f} @ ~${price:.2f}")
        return True, "approved"

    def review_sell(self, symbol, reason):
        """Exits are never blocked; this only records them."""
        self._note(symbol, "approve", f"approved sell ({reason})")
        return True, "approved"

    def filled(self, symbol, side, dollars=0.0):
        self.traded.add(symbol)
        if side == "BUY":
            self.spent += dollars

    def done(self, symbol, action):
        self.final[symbol] = action

    def _note(self, symbol, action, note):
        self.verdicts.setdefault(symbol, []).append({"agent": "desk", "action": action, "note": note[:MAX_NOTE]})

    # -- reporting ---------------------------------------------------------------------------
    def report(self):
        """This cycle, per symbol: every agent's verdict in order and the final action."""
        return {"t": self.t,
                "symbols": {s: {"final": self.final.get(s, "hold"), "verdicts": v} for s, v in self.verdicts.items()}}

    def log_entries(self):
        """Flat per-agent records, so each agent keeps its own history on the dashboard."""
        return [{"t": self.t, "symbol": s, **v} for s, vs in self.verdicts.items() for v in vs]
