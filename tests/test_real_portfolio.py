import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import real_portfolio  # noqa: E402

PORT = {"as_of": "2026-11-06", "cash": 0.34,
        "holdings": {"AAPL": {"value": 200.0, "gain": 4.0}, "SPCX": {"value": 40.0, "gain": -8.0}}}


class ValuateTests(unittest.TestCase):
    def test_scales_by_price_move(self):
        v = real_portfolio.valuate(PORT, {"AAPL": 100.0}, {"AAPL": 110.0})
        self.assertAlmostEqual(v["holdings"]["AAPL"]["value"], 220.0)
        self.assertAlmostEqual(v["holdings"]["AAPL"]["gain"], 24.0)
        self.assertAlmostEqual(v["holdings"]["SPCX"]["value"], 40.0)   # no price -> unchanged
        self.assertFalse(v["holdings"]["SPCX"]["live"])
        self.assertAlmostEqual(v["total"], 260.34)
        self.assertAlmostEqual(v["since_snapshot"], 20.0)

    def test_real_file_matches_greenlight_total(self):
        v = real_portfolio.valuate(real_portfolio.load(os.path.join(os.path.dirname(__file__), "..", "real_portfolio.json")), {}, {})
        self.assertAlmostEqual(v["total"] - v["cash"], 829.09, places=2)


class TodayTests(unittest.TestCase):
    def test_intraday_series_and_change(self):
        refs = {"AAPL": 100.0}
        bars = {"AAPL": [(1, 99.0), (2, 101.0), (3, 98.0)]}
        t = real_portfolio.today(PORT, refs, bars, {"AAPL": 100.0})
        # AAPL = 2 shares; SPCX (no price) flat at 40; cash 0.34
        self.assertEqual([p["v"] for p in t["series"]], [238.34, 242.34, 236.34])
        self.assertAlmostEqual(t["prev_total"], 240.34)
        self.assertAlmostEqual(t["change"], -4.0)

    def test_forward_fill_across_symbols(self):
        port = {"cash": 0, "holdings": {"A": {"value": 10}, "B": {"value": 10}}}
        t = real_portfolio.today(port, {"A": 10, "B": 10}, {"A": [(1, 11), (3, 12)], "B": [(2, 9)]}, {"A": 10, "B": 10})
        self.assertEqual([p["v"] for p in t["series"]], [21, 20, 21])

    def test_no_bars(self):
        self.assertIsNone(real_portfolio.today(PORT, {"AAPL": 100.0}, {}, {}))


if __name__ == "__main__":
    unittest.main()


class PerfTests(unittest.TestCase):
    def test_continues_greenlight_returns(self):
        port = {"cash": 0, "you_pct_at_snapshot": -10.0, "holdings": {"A": {"value": 100.0}}}
        hist = [{"date": "2026-10-04", "you_pct": 4.0, "sp_pct": 30.0}]
        p = real_portfolio.perf(port, 110.0, hist, spy_now=110.0, spy_anchor=100.0)
        self.assertAlmostEqual(p["you_pct"], -1.0)   # 0.9 * 1.1 - 1
        self.assertAlmostEqual(p["sp_pct"], 43.0)    # 1.3 * 1.1 - 1

    def test_missing_spy(self):
        port = {"cash": 0, "you_pct_at_snapshot": -9.47, "holdings": {"A": {"value": 100.0}}}
        p = real_portfolio.perf(port, 100.0, [], None, None)
        self.assertEqual(p, {"you_pct": -9.47})

    def test_history_file_loads(self):
        pts = real_portfolio.load_history(os.path.join(os.path.dirname(__file__), "..", "greenlight_history.json"))
        self.assertGreater(len(pts), 10)
        self.assertEqual(sorted(pts, key=lambda p: p["date"]), pts)


class BuyAndHoldTests(unittest.TestCase):
    def test_value(self):
        import state
        stakes = {"A": 300.0, "B": 200.0}
        # A up 10%, B down 5%, $100 left as cash
        v = state.buy_and_hold_value(stakes, 600.0, {"A": 50, "B": 20}, {"A": 55, "B": 19})
        self.assertAlmostEqual(v, 100 + 330 + 190)
        self.assertIsNone(state.buy_and_hold_value(stakes, 600.0, {"A": 50}, {"A": 55, "B": 19}))
        self.assertIsNone(state.buy_and_hold_value(stakes, 600.0, None, {"A": 55, "B": 19}))
