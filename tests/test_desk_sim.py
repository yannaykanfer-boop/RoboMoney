"""Offline tests for desk_sim.py: the simulated broker, the settings reader, and a short end-to-end
replay through the real bot.run_cycle on made-up data."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import desk_sim  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_reads_the_live_workflow(self):
        env = desk_sim.live_settings()
        self.assertEqual(env["STRATEGY"], "news")
        self.assertIn("STAKES", env)
        self.assertNotIn("ALPACA_API_KEY", env)            # secrets are never picked up

    def test_apply_and_restore(self):
        before = (config.STAKES, config.SYMBOLS, config.MAX_TRADES_PER_DAY, config.COOLDOWN_MINUTES)
        old = desk_sim.apply_settings({"STAKES": "AAA:10,BBB:20", "MAX_TRADES_PER_DAY": "7", "COOLDOWN_MINUTES": "5"})
        try:
            self.assertEqual(config.STAKES, {"AAA": 10.0, "BBB": 20.0})
            self.assertEqual(config.SYMBOLS, ["AAA", "BBB"])
            self.assertEqual((config.MAX_TRADES_PER_DAY, type(config.MAX_TRADES_PER_DAY)), (7, int))
            self.assertEqual(config.COOLDOWN_MINUTES, 5.0)
        finally:
            desk_sim.restore_settings(old)
        self.assertEqual(before, (config.STAKES, config.SYMBOLS, config.MAX_TRADES_PER_DAY, config.COOLDOWN_MINUTES))


class SimBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = desk_sim.Clock()
        bars = {"AAA": [(100 + 300 * i, 10.0 + i) for i in range(20)]}
        arts = {"AAA": [{"id": 1, "t": -5000, "headline": "old"}, {"id": 2, "t": 1000, "headline": "new"},
                        {"id": 3, "t": 99999, "headline": "future"}]}
        self.b = desk_sim.SimBroker(bars, arts, 100.0, self.clock)

    def test_never_sees_the_future(self):
        self.clock.t = 1000
        self.assertEqual(self.b.price("AAA"), 13.0)                       # bar ending at t=1000
        self.assertEqual([a["id"] for a in self.b.recent_news("AAA", hours=1)], [2])
        self.clock.t = 50
        self.assertIsNone(self.b.price("AAA"))
        self.assertEqual(self.b.recent_closes("AAA"), [])

    def test_window_is_in_five_minute_bars(self):
        self.clock.t = 100 + 300 * 19
        self.assertEqual(self.b.recent_closes("AAA", lookback=60), [10.0 + i for i in range(8, 20)])  # 12 bars
        self.assertEqual(len(self.b.recent_closes("AAA", lookback=1)), 2)

    def test_stocks_only_trade_in_market_hours_crypto_always(self):
        clock = desk_sim.Clock()
        b = desk_sim.SimBroker({"QQQ": [(1000, 5.0), (1300, 6.0)], "BTC/USD": [(1000, 1.0), (1300, 2.0)]},
                               {}, 0.0, clock)
        clock.t = 1300
        self.assertTrue(b.tradable("QQQ")); self.assertEqual(b.recent_closes("QQQ"), [5.0, 6.0])
        clock.t = 1300 + 3600                        # an hour after QQQ's last bar: market shut
        self.assertFalse(b.tradable("QQQ")); self.assertEqual(b.recent_closes("QQQ"), [])
        self.assertEqual(b.price("QQQ"), 6.0)        # still valued at its last price
        self.assertTrue(b.tradable("BTC/USD")); self.assertEqual(b.recent_closes("BTC/USD"), [1.0, 2.0])
        self.assertEqual(desk_sim.news_symbol("BTC/USD"), "BTCUSD")

    def test_buy_sell_cash_and_positions(self):
        self.clock.t = 100                                                # price 10
        self.b.buy_notional("AAA", 50.0)
        self.assertEqual((self.b.cash, self.b.get_position_qty("AAA"), self.b.get_entry_price("AAA")), (50.0, 5.0, 10.0))
        self.clock.t = 100 + 300 * 2                                      # price 12
        self.assertEqual(self.b.get_position("AAA")["pnl"], 10.0)
        self.assertEqual(self.b.account_summary()["equity"], 110.0)
        self.b.sell_all("AAA")
        self.assertEqual((self.b.cash, self.b.get_position_qty("AAA")), (110.0, 0.0))
        self.assertEqual(desk_sim.round_trips(self.b.fills, {})[0]["pnl"], 10.0)


class EndToEndTests(unittest.TestCase):
    def test_replay_runs_the_real_bot(self):
        old = desk_sim.apply_settings(desk_sim.live_settings())
        try:
            bars, articles = desk_sim.synthetic(config.SYMBOLS, days=12, seed=3)
            res = desk_sim.run(bars, articles, step_bars=3)
            s = desk_sim.summarize(res, bars)
            self.assertGreater(s["checks"], 100)
            self.assertIn("search", s["agents"])
            self.assertEqual(s["agents"]["search"]["calls"], s["checks"] * len(config.SYMBOLS))
            buys = sum(f["side"] == "BUY" for f in res["fills"])
            self.assertEqual(s["agents"].get("desk", {}).get("by", {}).get("approved buy", 0), buys)
            self.assertAlmostEqual(s["pnl"], sum(r["pnl"] for r in desk_sim.round_trips(res["fills"], res["open"])), places=6)
            self.assertIn("TRADING DESK SIM", desk_sim.report(s, bars, "TRADING DESK SIM"))
        finally:
            desk_sim.restore_settings(old)


class WhySearchTests(unittest.TestCase):
    def test_best_score_and_what_happened_at_the_line(self):
        v = lambda sym, act, note: {"agent": "search", "symbol": sym, "action": act, "note": note}
        w = desk_sim.why_search([
            v("AAA", "hold", "news score +0.00 from 0 article(s), price +0.10% over window — no actionable signal"),
            v("AAA", "hold", "news score +0.50 from 3 article(s), price +2.00% over window — positive but already priced in"),
            v("AAA", "buy", "news score +0.40 from 2 article(s), price +0.20% over window — positive news not yet priced in, buy"),
            {"agent": "risk", "symbol": "AAA", "action": "ok", "note": "news score +9.00 from 9 article(s)"},
        ])["AAA"]
        self.assertEqual((w["checks"], w["with_news"], w["best"]), (3, 2, 0.5))
        self.assertEqual(w["at_line"], {"already priced in": 1, "bought": 1})


if __name__ == "__main__":
    unittest.main()
