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


if __name__ == "__main__":
    unittest.main()
