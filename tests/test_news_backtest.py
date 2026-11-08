import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_backtest as nb  # noqa: E402

NY = ZoneInfo("America/New_York")
START = datetime(2026, 10, 5, 10, 0, tzinfo=NY).timestamp()  # a Monday


def flat_bars(n, price=100.0, start=START):
    return [(start + i * 300, price) for i in range(n)]


def art(t, headline, aid=None):
    return {"id": aid, "t": t, "headline": headline, "summary": ""}


GOOD = "Company beats estimates, strong growth, record revenue surge"
BAD = "Company misses estimates, profit warning, shares plunge"


class SimulateTests(unittest.TestCase):
    def test_no_news_no_trades(self):
        bars = {"AAA": flat_bars(50)}
        self.assertEqual(nb.simulate(bars, {}, {"AAA": 100}, step_bars=1), [])

    def test_good_news_buys_then_bad_news_sells(self):
        rows = flat_bars(40)
        rows = rows[:20] + [(t, 105.0) for t, _ in rows[20:]]
        arts = {"AAA": [art(START + 60, GOOD, "a1"), art(START + 25 * 300, BAD, "a2"),
                        art(START + 25 * 300 + 1, BAD, "a3"), art(START + 25 * 300 + 2, BAD, "a4")]}
        trades = nb.simulate({"AAA": rows}, arts, {"AAA": 100}, step_bars=1)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["buy_price"], 100.0)
        self.assertEqual(t["sell_price"], 105.0)
        self.assertEqual(t["reason"], "negative-news")
        self.assertAlmostEqual(t["pnl"], 5.0)

    def test_stop_loss(self):
        rows = flat_bars(10) + [(START + (10 + i) * 300, 90.0) for i in range(5)]
        arts = {"AAA": [art(START + 60, GOOD)]}
        trades = nb.simulate({"AAA": rows}, arts, {"AAA": 100}, step_bars=1)
        self.assertEqual(trades[0]["reason"], "stop-loss")
        self.assertAlmostEqual(trades[0]["pnl"], -10.0)

    def test_open_position_marked_at_end(self):
        arts = {"AAA": [art(START + 60, GOOD)]}
        trades = nb.simulate({"AAA": flat_bars(10)}, arts, {"AAA": 100}, step_bars=1)
        self.assertEqual([t["reason"] for t in trades], ["still-open"])

    def test_same_story_buys_once_per_check(self):
        # Mirrors bot.py: the check only compares symbols wanting to buy in the same cycle.
        arts = {s: [art(START - 60, GOOD, "shared")] for s in ("AAA", "BBB")}
        bars = {s: flat_bars(1) for s in ("AAA", "BBB")}
        trades = nb.simulate(bars, arts, {"AAA": 100, "BBB": 100}, step_bars=1)
        self.assertEqual(len(trades), 1)

    def test_news_from_the_future_is_ignored(self):
        arts = {"AAA": [art(START + 10_000, GOOD)]}
        trades = nb.simulate({"AAA": flat_bars(10)}, arts, {"AAA": 100}, step_bars=1)
        self.assertEqual(trades, [])


class SummaryTests(unittest.TestCase):
    def test_summary_and_hold(self):
        trades = [{"pnl": 5.0, "reason": "a"}, {"pnl": -2.0, "reason": "a"}]
        bars = {"AAA": [(0, 100.0), (1, 110.0)]}
        s = nb.summarize(trades, bars, {"AAA": 200})
        self.assertEqual((s["trades"], s["wins"]), (2, 1))
        self.assertAlmostEqual(s["pnl"], 3.0)
        self.assertAlmostEqual(s["hold_pnl"], 20.0)


class ScorerCheckTests(unittest.TestCase):
    def test_counts_hits(self):
        rows = [(START + i * 300, 100.0 + i) for i in range(600)]  # always rising
        arts = {"AAA": [art(START, GOOD, "g"), art(START + 600, "nothing here", "n"), art(START + 86400, BAD, "b")]}
        c = nb.scorer_check({"AAA": rows}, arts)
        self.assertEqual(c["pos"], (1, 1))
        self.assertEqual(c["neg"], (1, 0))

    def test_duplicate_ids_counted_once(self):
        rows = [(START + i * 300, 100.0 + i) for i in range(200)]
        arts = {"AAA": [art(START, GOOD, "g"), art(START, GOOD, "g")]}
        self.assertEqual(nb.scorer_check({"AAA": rows}, arts)["pos"], (1, 1))

    def test_many_headlines_one_day_count_once(self):
        rows = [(START + i * 300, 100.0 + i) for i in range(200)]
        arts = {"AAA": [art(START + i, GOOD, f"g{i}") for i in range(10)]}
        self.assertEqual(nb.scorer_check({"AAA": rows}, arts)["pos"], (1, 1))

    def test_p_value(self):
        self.assertAlmostEqual(nb.binom_p_two_sided(100, 50), 1.0)
        self.assertLess(nb.binom_p_two_sided(100, 70), 0.001)


if __name__ == "__main__":
    unittest.main()
