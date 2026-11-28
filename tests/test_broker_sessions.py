"""Offline test for broker.in_regular_session: early-close days end when the calendar says."""
import os, sys, unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from broker import in_regular_session  # noqa: E402

NY = ZoneInfo("America/New_York")
t = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=NY)


class RegularSessionTests(unittest.TestCase):
    def test_early_close_day_ends_at_1pm(self):
        sessions = {date(2026, 11, 27): (t(27, 9, 30), t(27, 13, 0))}   # day after Thanksgiving
        self.assertTrue(in_regular_session(t(27, 12, 55), sessions))    # last bar before the close
        self.assertFalse(in_regular_session(t(27, 13, 0), sessions))    # after-hours
        self.assertFalse(in_regular_session(t(27, 15, 30), sessions))
        self.assertFalse(in_regular_session(t(27, 9, 25), sessions))

    def test_falls_back_to_normal_hours_without_a_calendar(self):
        self.assertTrue(in_regular_session(t(25, 15, 55), {}))
        self.assertFalse(in_regular_session(t(25, 16, 0), {}))
        self.assertFalse(in_regular_session(t(25, 9, 25), {}))


if __name__ == "__main__":
    unittest.main()
