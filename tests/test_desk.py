"""Offline tests for the trading desk: the agents (agents.py), the Head of Desk (desk.py) and how
bot.py wires them together."""
import json, logging, os, sys, tempfile, time, unittest, unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import agents  # noqa: E402
from agents import Verdict  # noqa: E402
from desk import HeadOfDesk  # noqa: E402


class Patch:
    """Temporarily set config values: `with Patch(STAKES={...}, TOTAL_BUDGET=500):`"""
    def __init__(self, **kw): self.kw, self.old = kw, {}
    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(config, k); setattr(config, k, v)
    def __exit__(self, *a):
        for k, v in self.old.items(): setattr(config, k, v)


class CrashGuardTests(unittest.TestCase):
    def test_blocks_a_falling_stock(self):
        with Patch(CRASH_BLOCK_PCT=3):
            v = agents.crash_guard([100, 101, 99, 97.5])      # 3.5% below the 101 high
            self.assertEqual((v.agent, v.action, v.tag), ("crash", "veto", "crash"))
            self.assertIn("3.5%", v.note)

    def test_allows_normal_moves_and_can_be_turned_off(self):
        with Patch(CRASH_BLOCK_PCT=3):
            self.assertIsNone(agents.crash_guard([100, 101, 99.5]))
            self.assertIsNone(agents.crash_guard([]))
        with Patch(CRASH_BLOCK_PCT=0):
            self.assertIsNone(agents.crash_guard([100, 50]))


class RiskTests(unittest.TestCase):
    def test_rules_in_order(self):
        with Patch(MAX_TRADES_PER_DAY=5):
            self.assertEqual(agents.risk("buy", "daily", False, True, 9).tag, "window")   # window checked first
            self.assertEqual(agents.risk("buy", "news", True, True, 9).tag, "cooldown")
            self.assertEqual(agents.risk("sell", "news", True, False, 5).tag, "daily-limit")
            self.assertEqual(agents.risk("buy", "news", True, False, 4).action, "ok")

    def test_messages_match_the_old_log_lines(self):
        self.assertEqual(agents.risk("buy", "news", True, True, 0).note,
                         "buy signal ignored (cooldown, avoids churning).")
        with Patch(ICT_WINDOW_MINUTES=30):
            self.assertIn("first 30 min after open", agents.risk("buy", "ict", False, False, 0).note)


class ExitWatchTests(unittest.TestCase):
    def test_stop_loss_wins(self):
        with Patch(STOP_LOSS_PCT=5):
            v, peak = agents.exit_watch("news", [94], 94.0, 1, 100.0, 110.0, None, 0, [], "hold")
            self.assertEqual((v.action, v.tag, peak), ("sell", "stop-loss", 110.0))   # peak untouched

    def test_trailing_stop_updates_peak(self):
        with Patch(STOP_LOSS_PCT=5, TRAIL_ACTIVATE_PCT=3, TRAIL_PCT=3):
            v, peak = agents.exit_watch("news", [101.8], 101.8, 1, 100.0, 105.0, None, 0, [], "hold")
            self.assertEqual((v.tag, peak), ("trailing-stop", 105.0))
            v, peak = agents.exit_watch("news", [106], 106.0, 1, 100.0, 105.0, None, 0, [], "hold")
            self.assertEqual((v, peak), (None, 106.0))

    def test_no_trailing_check_when_already_selling_or_other_strategy(self):
        v, peak = agents.exit_watch("news", [101.8], 101.8, 1, 100.0, None, None, 0, [], "sell")
        self.assertEqual((v, peak), (None, None))
        v, peak = agents.exit_watch("meanrev", [101.8], 101.8, 1, 100.0, None, None, 0, [], "hold")
        self.assertEqual((v, peak), (None, None))


class SizerTests(unittest.TestCase):
    def test_exposure_lookup_only_when_kelly_wants_to_buy(self):
        calls = []
        with Patch(KELLY_FRACTION=0.25, MAX_TOTAL_EXPOSURE_PCT=40):
            stake, v = agents.size_news([], 200.0, 1000.0, lambda: calls.append(1) or 0.0, 0.0)
        self.assertEqual((stake, v.action, len(calls)), (25.0, "ok", 1))
        self.assertIn(" · ", v.note)                     # Kelly note · exposure note


class HeadOfDeskTests(unittest.TestCase):
    def setUp(self):
        self.p = Patch(STAKES={"AAPL": 200.0, "NVDA": 200.0}, TOTAL_BUDGET=500.0, MAX_TRADES_PER_DAY=10)
        self.p.__enter__()
        self.d = HeadOfDesk(1)

    def tearDown(self): self.p.__exit__()

    def proposed(self, sym="AAPL"):
        self.d.hear(sym, Verdict("search", "buy", "good news"))

    def test_approves_a_clean_buy(self):
        self.proposed()
        self.assertEqual(self.d.review_buy("AAPL", 25.0, 200.0, 0.0, 0), (True, "approved"))
        self.assertEqual(self.d.alerts, [])
        self.assertEqual(self.d.verdicts["AAPL"][-1]["agent"], "desk")

    def test_blocks_and_alerts(self):
        cases = [
            (lambda: None, dict(stake=25.0), "SEARCH never proposed"),
            (lambda: (self.proposed(), self.d.hear("AAPL", Verdict("crash", "veto", "x"))), dict(stake=25.0), "vetoed by CRASH"),
            (self.proposed, dict(stake=25.0, price=0), "no valid price"),
            (self.proposed, dict(stake=0), "size is zero"),
            (self.proposed, dict(stake=250.0), "over this stock's $200.00 stake"),
            (self.proposed, dict(stake=25.0, trades_today=10), "daily limit"),
            (self.proposed, dict(stake=150.0, open_value=400.0), "over the $500.00 budget"),
        ]
        for setup, kw, expect in cases:
            with self.subTest(expect=expect):
                self.d = HeadOfDesk(1)
                setup()
                args = dict(stake=25.0, price=200.0, open_value=0.0, trades_today=0); args.update(kw)
                ok, why = self.d.review_buy("AAPL", args["stake"], args["price"], args["open_value"], args["trades_today"])
                self.assertFalse(ok)
                self.assertIn(expect, why)
                self.assertEqual(len(self.d.alerts), 1)

    def test_counts_its_own_spending_and_repeat_trades(self):
        self.proposed("AAPL"); self.proposed("NVDA")
        self.assertTrue(self.d.review_buy("AAPL", 200.0, 1.0, 250.0, 0)[0])
        self.d.filled("AAPL", "BUY", 200.0)
        self.assertIn("budget", self.d.review_buy("NVDA", 100.0, 1.0, 250.0, 1)[1])   # 250 + 200 + 100 > 500
        self.proposed("AAPL")
        self.assertIn("already traded", self.d.review_buy("AAPL", 10.0, 1.0, 0.0, 1)[1])

    def test_sells_are_never_blocked(self):
        self.d.hear("AAPL", Verdict("risk", "veto", "nope"))
        self.assertEqual(self.d.review_sell("AAPL", "stop-loss"), (True, "approved"))
        self.assertEqual(self.d.alerts, [])

    def test_report_and_log(self):
        self.proposed(); self.d.review_buy("AAPL", 25.0, 200.0, 0.0, 0); self.d.done("AAPL", "buy")
        r = self.d.report()
        self.assertEqual(r["symbols"]["AAPL"]["final"], "buy")
        self.assertEqual([v["agent"] for v in r["symbols"]["AAPL"]["verdicts"]], ["search", "desk"])
        self.assertEqual({e["agent"] for e in self.d.log_entries()}, {"search", "desk"})


class BreakerAlertTests(unittest.TestCase):
    def test_alerts_once_not_on_every_resume_and_retrip(self):
        import bot
        tmp = tempfile.mkdtemp()
        closes = [100.0] * 15 + [100.0 + i for i in range(1, 11)]          # a strong uptrend: "resume"
        with Patch(STRATEGY="trend", STATE_FILE=os.path.join(tmp, "s.json"), STAKES={"AAPL": 100.0}, SYMBOLS=["AAPL"],
                   MAX_OVERALL_LOSS_DOLLARS=30.0):
            json.dump({"history": [], "trades": [], "baseline_equity": 1100.0}, open(config.STATE_FILE, "w"))
            b = FakeBroker(closes={"AAPL": closes}, articles={"AAPL": []})
            for _ in range(3):
                bot.run_cycle(b, logging.getLogger("test-desk"))
            with open(config.STATE_FILE) as f:
                st = json.load(f)
            self.assertTrue(st["paused"])
            self.assertEqual(sum("Circuit breaker" in a["message"] for a in st["desk_alerts"]), 1)


class FakeBroker:
    def __init__(self, closes, articles):
        self.closes, self.articles, self.orders, self.qty = closes, articles, [], {}
    def is_market_open(self): return True
    def account_summary(self): return {"equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0, "paper": True}
    def recent_closes(self, s, lookback=30): return self.closes[s]
    def recent_news(self, s, hours=24): return self.articles[s]
    def get_position_qty(self, s): return self.qty.get(s, 0.0)
    def get_entry_price(self, s): return None
    def get_position(self, s): return None
    def buy_notional(self, s, dollars):
        self.orders.append(("BUY", s, dollars)); self.qty[s] = dollars / self.closes[s][-1]
        return type("O", (), {"id": "fake"})()
    def sell_all(self, s):
        self.orders.append(("SELL", s)); self.qty[s] = 0
        return type("O", (), {"id": "fake"})()


class CycleTests(unittest.TestCase):
    def test_crash_agent_blocks_buy_and_desk_records_everything(self):
        import bot
        now = time.time()
        good = lambda tk: [{"t": now - 600, "headline": f"{tk} beats estimates, raises guidance", "summary": ""},
                           {"t": now - 1200, "headline": f"Analysts upgrade {tk}", "summary": ""}]
        tmp = tempfile.mkdtemp()
        with Patch(STRATEGY="news", STATE_FILE=os.path.join(tmp, "s.json"), COOLDOWN_MINUTES=0,
                   STAKES={"AAPL": 200.0, "NVDA": 200.0}, SYMBOLS=["AAPL", "NVDA"], CRASH_BLOCK_PCT=3,
                   TOTAL_BUDGET=1000.0, MAX_TOTAL_EXPOSURE_PCT=40):
            # AAPL: good news, steady price -> buy. NVDA: good news and flat over the window (+0.5%), so
            # SEARCH likes it, but it spiked to 106 and fell 5% back -> CRASH says no.
            b = FakeBroker(closes={"AAPL": [200, 200.5], "NVDA": [100, 106, 100.5]},
                           articles={"AAPL": good("AAPL"), "NVDA": good("NVDA")})
            bot.run_cycle(b, logging.getLogger("test-desk"))
            self.assertEqual(b.orders, [("BUY", "AAPL", 25.0)])
            st = json.load(open(config.STATE_FILE))
            syms = st["desk"]["symbols"]
            self.assertEqual(syms["AAPL"]["final"], "buy")
            self.assertEqual([v["agent"] for v in syms["AAPL"]["verdicts"]], ["search", "risk", "sizer", "desk"])
            self.assertEqual(syms["NVDA"]["final"], "hold")
            self.assertEqual([v["agent"] for v in syms["NVDA"]["verdicts"]], ["search", "risk", "crash"])
            self.assertEqual(len(st["desk_alerts"]), 1)
            self.assertEqual(st["desk_alerts"][0]["symbol"], "NVDA")
            self.assertIn("buy blocked", [d for d in st["decisions"] if d["symbol"] == "NVDA"][0]["why"])
            self.assertTrue(any(e["agent"] == "crash" for e in st["desk_log"]))

            # Next check 15 minutes later, still crashing: CRASH vetoes again but doesn't alert again.
            with unittest.mock.patch("time.time", lambda: now + 900):
                bot.run_cycle(b, logging.getLogger("test-desk"))
            st = json.load(open(config.STATE_FILE))
            self.assertEqual(st["desk"]["symbols"]["NVDA"]["verdicts"][-1]["agent"], "crash")
            self.assertEqual(len(st["desk_alerts"]), 1)


if __name__ == "__main__":
    unittest.main()
