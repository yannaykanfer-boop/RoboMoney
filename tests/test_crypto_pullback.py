"""Offline tests for crypto_pullback.py: no lookahead, bar timing, fills, and position sizing."""
import os, sys, unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import crypto_pullback as cp  # noqa: E402


def make_bars(n=900, seed=1, start="2025-01-01"):
    """Random-walk hourly bars with a gentle uptrend, so the trend filter switches on sometimes."""
    rnd = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rnd.normal(0.0004, 0.006, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rnd.uniform(0, 0.004, n))
    low = np.minimum(open_, close) * (1 - rnd.uniform(0, 0.004, n))
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


class NoLookaheadTests(unittest.TestCase):
    def test_signals_never_change_when_future_bars_change(self):
        """Signals for any bar must be identical whether or not later bars exist (or are different)."""
        p = cp.Params()
        full = make_bars(1500)
        cut = full.index[1100]
        base = cp.signals(full, p)
        trunc = cp.signals(full[full.index <= cut], p)
        cols = ["entry_signal", "stop_level", "regime_ok", "regime_exit"]
        pd.testing.assert_frame_equal(base.loc[:cut, cols], trunc.loc[:cut, cols])
        wild = full.copy()
        wild.loc[wild.index > cut, ["open", "high", "low", "close"]] *= 3      # rewrite the future
        pd.testing.assert_frame_equal(base.loc[:cut, cols], cp.signals(wild, p).loc[:cut, cols])

    def test_4h_trend_comes_from_the_last_closed_4h_bar(self):
        h1 = make_bars(48)
        h4 = cp.four_hour(h1)
        self.assertEqual(h4.index[0], h1.index[0] + pd.Timedelta(hours=4))   # labelled by close time
        self.assertAlmostEqual(h4["close"].iloc[0], h1["close"].iloc[3])
        p = cp.Params(ema_trend_fast=2, ema_trend_slow=3, slope_bars_4h=1)
        s = cp.signals(h1, p)
        # the 1H bar opening 05:00 closes 06:00; the 04:00-08:00 4H bar is still open, so it must use 00:00-04:00
        row = s.loc[h1.index[0] + pd.Timedelta(hours=5)]
        self.assertEqual(row["h4_close_time"], h1.index[0] + pd.Timedelta(hours=4))

    def test_incomplete_4h_bars_are_dropped(self):
        h1 = make_bars(48).drop(make_bars(48).index[5])                       # one missing hour
        self.assertEqual(len(cp.four_hour(h1)), 11)


class BacktestMechanicsTests(unittest.TestCase):
    def frame(self, rows, p):
        """A hand-built signals() frame: list of (open, high, low, close, entry_signal, stop_level)."""
        idx = pd.date_range("2025-01-01", periods=len(rows), freq="h", tz="UTC")
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "entry_signal", "stop_level"], index=idx)
        df["regime_exit"] = False
        return df

    def test_entry_fills_next_open_and_stop_wins_a_tie(self):
        p = cp.Params(fee_pct=0, slippage_pct=0, stop_slippage_pct=0, take_profit_r=2)
        rows = [(100, 101, 99, 100, True, 98),        # signal at close, stop 98
                (100, 105, 97, 101, False, np.nan),   # entry at 100; bar spans both stop 98 and target 104
                (101, 102, 100, 101, False, np.nan)]
        res = cp.backtest({"X": self.frame(rows, p)}, p)
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (100, 98, "stop"))
        self.assertAlmostEqual(t["r"], -1.0)

    def test_gap_through_stop_fills_at_the_open(self):
        p = cp.Params(fee_pct=0, slippage_pct=0, stop_slippage_pct=0)
        rows = [(100, 101, 99, 100, True, 98), (100, 101, 99.5, 100, False, np.nan),
                (95, 96, 94, 95, False, np.nan)]
        t = cp.backtest({"X": self.frame(rows, p)}, p).trades[0]
        self.assertEqual((t["exit"], t["reason"]), (95, "stop"))           # worse than the stop: honest

    def test_target_and_breakeven(self):
        p = cp.Params(fee_pct=0, slippage_pct=0, stop_slippage_pct=0, take_profit_r=2, breakeven_at_r=1)
        rows = [(100, 101, 99, 100, True, 98), (100, 102.5, 99.5, 102, False, np.nan),   # +1R seen: stop -> 100
                (102, 103, 99.9, 100.5, False, np.nan)]                                   # back to entry
        t = cp.backtest({"X": self.frame(rows, p)}, p).trades[0]
        self.assertEqual(t["reason"], "breakeven")
        rows[2] = (102, 104.5, 101, 104, False, np.nan)
        t = cp.backtest({"X": self.frame(rows, p)}, p).trades[0]
        self.assertEqual((t["exit"], t["reason"]), (104, "target"))


class SizingTests(unittest.TestCase):
    def test_one_percent_risk_including_costs(self):
        p = cp.Params(fee_pct=0.25, stop_slippage_pct=0.1, max_position_pct=100)
        qty = cp.size_position(1000, 100, 95, 0, p)
        loss = qty * ((100 - 95) + 100 * 0.0025 + 95 * 0.0035)
        self.assertAlmostEqual(loss, 10.0)                                  # 1% of $1,000

    def test_caps_position_and_leverage(self):
        p = cp.Params(max_position_pct=50)
        self.assertAlmostEqual(cp.size_position(1000, 100, 99.9, 0, p) * 100, 500)       # tight stop: capped at 50%
        self.assertLessEqual(cp.size_position(1000, 100, 99.9, 900, p) * 100 * 1.0025, 100.0001)  # only cash left


class OrderPlanTests(unittest.TestCase):
    def test_stop_rests_on_the_exchange_target_is_script_managed(self):
        plan = cp.order_plan("BTC/USD", 0.01, 60000, 64000)
        self.assertEqual([o.get("type") for o in plan], ["market", "stop_limit", "script-managed", "script-managed"])
        self.assertLess(plan[1]["limit_price"], plan[1]["stop_price"])


if __name__ == "__main__":
    unittest.main()
