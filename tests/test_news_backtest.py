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

    def test_same_story_buys_once_across_checks(self):
        arts = {s: [art(START - 60, GOOD, "shared")] for s in ("AAA", "BBB")}
        bars = {s: flat_bars(10) for s in ("AAA", "BBB")}
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


class WilsonTests(unittest.TestCase):
    def test_empty_is_unknown(self):
        self.assertEqual(nb.wilson_interval(0, 0), (0.0, 1.0))

    def test_known_value(self):
        lo, hi = nb.wilson_interval(100, 50)
        self.assertAlmostEqual(lo, 0.404, places=3)
        self.assertAlmostEqual(hi, 0.596, places=3)

    def test_edges_stay_in_range_and_narrow_with_n(self):
        lo, hi = nb.wilson_interval(10, 10)
        self.assertLessEqual(hi, 1.0)
        self.assertGreater(lo, 0.6)
        small = nb.wilson_interval(10, 5)
        big = nb.wilson_interval(1000, 500)
        self.assertGreater(small[1] - small[0], big[1] - big[0])


DAY = 86400


class HorizonTests(unittest.TestCase):
    def test_three_day_horizon_differs_from_one_day(self):
        # price is 101 one day later (hit) but 90 three days later (miss)
        rows = [(START + i * 300, 100.0 if i == 0 else (101.0 if i <= 78 else 90.0)) for i in range(78 * 4)]
        arts = {"AAA": [art(START, GOOD, "g")]}
        one = nb.scorer_check({"AAA": rows}, arts, horizon_bars=78)
        three = nb.scorer_check({"AAA": rows}, arts, horizon_bars=3 * 78)
        self.assertEqual(one["pos"], (1, 1))
        self.assertEqual(three["pos"], (1, 0))

    def test_time_range_filters_signals(self):
        rows = [(START + i * 300, 100.0 + i) for i in range(400)]
        arts = {"AAA": [art(START, GOOD, "g")]}
        inside = nb.scorer_check({"AAA": rows}, arts, t_lo=START, t_hi=START + 10)
        outside = nb.scorer_check({"AAA": rows}, arts, t_lo=START + 10, t_hi=START + 100000)
        self.assertEqual(inside["pos"], (1, 1))
        self.assertEqual(outside["pos"], (0, 0))


class PeriodTests(unittest.TestCase):
    def _bars(self):
        # 180 days, one bar per day: two 90-day periods, rising then falling
        return {"AAA": [(START + d * DAY, 100.0 + (d if d < 90 else 180 - d)) for d in range(180)]}

    def test_split_periods(self):
        ps = nb.split_periods(self._bars(), 90)
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[0][1], ps[1][0])
        self.assertEqual(nb.split_periods({}, 90), [])
        self.assertEqual(len(nb.split_periods({"A": [(START, 1.0), (START + 5 * DAY, 1.0)]}, 90)), 1)

    def test_breakdown_assigns_trades_and_hold(self):
        trades = [{"buy_t": START + 10 * DAY, "pnl": 5.0}, {"buy_t": START + 20 * DAY, "pnl": -1.0},
                  {"buy_t": START + 120 * DAY, "pnl": -3.0}]
        rows = nb.period_breakdown(trades, self._bars(), {}, {"AAA": 100.0}, 90)
        self.assertEqual([r["trades"] for r in rows], [2, 1])
        self.assertEqual([r["wins"] for r in rows], [1, 0])
        self.assertAlmostEqual(rows[0]["win_rate"], 0.5)
        self.assertAlmostEqual(rows[0]["pnl"], 4.0)
        self.assertAlmostEqual(rows[1]["pnl"], -3.0)
        self.assertGreater(rows[0]["hold_pnl"], 0)
        self.assertLess(rows[1]["hold_pnl"], 0)

    def test_breakdown_hit_rates_per_period(self):
        # 100 trading days of 78 bars, one calendar day apart; always rising
        bars = {"AAA": [(START + (d // 78) * DAY + (d % 78) * 300, 100.0 + d) for d in range(78 * 100)]}
        arts = {"AAA": [art(START + 60, GOOD, "early")]}
        rows = nb.period_breakdown([], bars, arts, {"AAA": 100.0}, 50, horizon_bars=78)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pos"], (1, 1))
        self.assertEqual(rows[1]["pos"], (0, 0))
        self.assertEqual(rows[0]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
