"""Offline tests for the news strategy's exit rules (exits.py) and their wiring in bot.py."""
import json, logging, os, sys, tempfile, time, unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import exits  # noqa: E402

NY = ZoneInfo("America/New_York")
ts = lambda y, m, d, h=12: datetime(y, m, d, h, tzinfo=NY).timestamp()


class TradingDaysTests(unittest.TestCase):
    def test_skips_weekends(self):
        self.assertEqual(exits.trading_days_between(date(2026, 11, 6), date(2026, 11, 9)), 1)   # Fri -> Mon
        self.assertEqual(exits.trading_days_between(date(2026, 11, 9), date(2026, 11, 12)), 3)  # Mon -> Thu
        self.assertEqual(exits.trading_days_between(date(2026, 11, 9), date(2026, 11, 9)), 0)


class CheckTests(unittest.TestCase):
    def test_trailing_stop(self):
        # bought 100, peaked 105 (+5% > 3% activation), now 101.8 (down 3.05% from the high) -> sell
        self.assertEqual(exits.check(100, 101.8, 105, None, 0, 0.5), ("trailing-stop", 105))
        # never got up 3% -> trailing stop not active yet
        self.assertEqual(exits.check(100, 99.0, 102, None, 0, 0.5)[0], None)
        # a new high just raises the peak
        self.assertEqual(exits.check(100, 106, 105, None, 0, 0.5), (None, 106))

    def test_time_limit(self):
        opened, now = ts(2026, 11, 9), ts(2026, 11, 12)          # Mon -> Thu = 3 trading days
        self.assertEqual(exits.check(100, 100.5, None, opened, now, 0.0)[0], "time-limit")
        # news still strong -> keep holding
        self.assertEqual(exits.check(100, 100.5, None, opened, now, config.NEWS_BUY_THRESHOLD)[0], None)
        # only 2 trading days -> too early
        self.assertEqual(exits.check(100, 100.5, None, opened, ts(2026, 11, 11), 0.0)[0], None)

    def test_opened_at(self):
        tr = [{"t": 1, "symbol": "A", "side": "BUY"}, {"t": 2, "symbol": "A", "side": "SELL"},
              {"t": 3, "symbol": "A", "side": "BUY"}, {"t": 4, "symbol": "B", "side": "BUY"}]
        self.assertEqual(exits.opened_at(tr, "A"), 3)
        self.assertIsNone(exits.opened_at(tr[:2], "A"))


class Broker:
    """Holds 1 share of AAPL bought at $100; price is now $101.80."""
    def __init__(self): self.orders, self.qty = [], {"AAPL": 1.0}
    def is_market_open(self): return True
    def account_summary(self): return {"equity": 1000.0, "cash": 900.0, "buying_power": 900.0, "paper": True}
    def recent_closes(self, s, lookback=30): return [101.5, 101.8]
    def recent_news(self, s, hours=24): return []
    def get_position_qty(self, s): return self.qty.get(s, 0.0)
    def get_entry_price(self, s): return 100.0 if self.qty.get(s) else None
    def get_position(self, s):
        q = self.qty.get(s, 0)
        return {"qty": q, "entry": 100.0, "cost": 100.0 * q, "value": 101.8 * q, "pnl": 1.8 * q} if q else None
    def sell_all(self, s):
        self.orders.append(("SELL", s)); self.qty[s] = 0
        return type("O", (), {"id": "x"})()


class CycleTests(unittest.TestCase):
    def test_trailing_stop_sells_and_clears_peak(self):
        import bot
        tmp = tempfile.mkdtemp()
        old = (config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE)
        try:
            config.STRATEGY, config.STATE_FILE = "news", os.path.join(tmp, "s.json")
            config.STAKES = {"AAPL": 200.0}; config.SYMBOLS = ["AAPL"]
            json.dump({"history": [], "peaks": {"AAPL": 105.0},
                       "trades": [{"t": time.time() - 600, "symbol": "AAPL", "side": "BUY", "price": 100.0}]},
                      open(config.STATE_FILE, "w"))
            b = Broker()
            bot.run_cycle(b, logging.getLogger("test-exits"))
            self.assertEqual(b.orders, [("SELL", "AAPL")])
            st = json.load(open(config.STATE_FILE))
            self.assertEqual(st["trades"][-1]["reason"], "trailing-stop")
            self.assertEqual(st["peaks"], {})
            self.assertIn("trailing-stop", st["decisions"][-1]["why"])
        finally:
            config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE = old


if __name__ == "__main__":
    unittest.main()
