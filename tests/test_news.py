"""Offline tests for the news strategy + Kelly sizing. Run: python -m unittest discover tests"""
import logging
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import news  # noqa: E402

NOW = 1_800_000_000


def art(headline, hours_ago=1, summary=""):
    return {"t": NOW - hours_ago * 3600, "headline": headline, "summary": summary}


class ScoreTests(unittest.TestCase):
    def test_polarity(self):
        self.assertGreater(news.score_text("Apple beats estimates, raises guidance"), 0.5)
        self.assertLess(news.score_text("Nvidia downgraded as demand concerns mount; shares plunge"), -0.5)
        self.assertEqual(news.score_text("Company to present at conference"), 0.0)

    def test_negation_and_no_double_count(self):
        self.assertLess(news.score_text("Company fails to beats expectations"), 0)
        # "beats estimates" must not also count "beats"
        self.assertAlmostEqual(news.score_text("X beats estimates"), 1.0)

    def test_recency_and_confidence(self):
        fresh, _, _ = news.sentiment([art("Stock surges on upgrade", 0.1)] * 3, NOW)
        stale_mix, _, _ = news.sentiment([art("Stock surges on upgrade", 48), art("Shares plunge on probe", 0.1)], NOW)
        self.assertGreater(fresh, 0.5)
        self.assertLess(stale_mix, 0)
        single, _, _ = news.sentiment([art("Stock surges on upgrade", 0.1)], NOW)
        self.assertLess(single, fresh)  # one headline is not a trend


class DecideTests(unittest.TestCase):
    good = [art("Microsoft beats estimates, raises guidance", 1), art("Analyst upgrade for Microsoft", 2),
            art("Record revenue quarter", 0.5)]
    bad = [art("Shares plunge after downgrade", 1), art("SEC investigation announced", 0.5)]

    def test_buy_when_not_priced_in(self):
        self.assertEqual(news.decide_news([100, 100.3], self.good, False, NOW)[0], "buy")

    def test_skip_when_priced_in(self):
        action, desc = news.decide_news([100, 103], self.good, False, NOW)
        self.assertEqual(action, "hold")
        self.assertIn("priced in", desc)

    def test_sell_on_negative(self):
        self.assertEqual(news.decide_news([100, 99], self.bad, True, NOW)[0], "sell")
        self.assertEqual(news.decide_news([100, 99], self.bad, False, NOW)[0], "hold")

    def test_no_news_holds(self):
        self.assertEqual(news.decide_news([100, 101], [], False, NOW)[0], "hold")


class KellyTests(unittest.TestCase):
    def trades(self, rets):
        out, t = [], 0
        for r in rets:
            out += [{"t": t, "symbol": "X", "side": "BUY", "price": 100},
                    {"t": t + 1, "symbol": "X", "side": "SELL", "price": 100 * (1 + r)}]
            t += 2
        return out

    def test_prior_only(self):
        p, b, f = news.kelly_estimate([])
        self.assertAlmostEqual(f, 0.55 - 0.45 / 1.0)
        dollars, _ = news.kelly_dollars([], stake_cap=500, budget=1000)
        self.assertAlmostEqual(dollars, 0.10 * 0.25 * 1000)

    def test_losing_history_stops_betting(self):
        dollars, note = news.kelly_dollars(self.trades([-0.03] * 30 + [0.01] * 5), 500, 1000)
        self.assertEqual(dollars, 0)
        self.assertIn("no bet", note)

    def test_winning_history_grows_but_capped(self):
        d, _ = news.kelly_dollars(self.trades([0.04] * 40 + [-0.02] * 10), 100, 1000)
        self.assertEqual(d, 100)  # capped at the symbol stake


class ExposureTests(unittest.TestCase):
    def test_cap(self):
        # 40% of $1000 = $400 limit
        self.assertEqual(news.cap_to_exposure(25, 0, 0, 1000)[0], 25)
        self.assertEqual(news.cap_to_exposure(100, 250, 80, 1000)[0], 70)   # shrunk to fit
        self.assertEqual(news.cap_to_exposure(25, 400, 0, 1000)[0], 0)      # no room

    def test_same_cycle_counts_once(self):
        trades = []
        for sym in ("QQQ", "AAPL", "NVDA", "GOOGL"):   # one headline, four buys, all win 2%
            trades += [{"t": 1, "symbol": sym, "side": "BUY", "price": 100, "cycle": 7},
                       {"t": 2, "symbol": sym, "side": "SELL", "price": 102}]
        self.assertEqual(len(news.round_trip_returns(trades)), 1)
        self.assertAlmostEqual(news.round_trip_returns(trades)[0], 0.02)


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


class CycleTest(unittest.TestCase):
    def test_full_cycle(self):
        import bot
        now = time.time()
        good = [{"t": now - 600, "headline": "AAPL beats estimates, raises guidance", "summary": ""},
                {"t": now - 1200, "headline": "Analysts upgrade Apple", "summary": ""}]
        tmp = tempfile.mkdtemp()
        old = (config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE, config.COOLDOWN_MINUTES)
        try:
            config.STRATEGY, config.STATE_FILE, config.COOLDOWN_MINUTES = "news", os.path.join(tmp, "s.json"), 0
            config.STAKES = {"AAPL": 200.0, "NVDA": 200.0, "QQQ": 200.0}
            config.SYMBOLS = list(config.STAKES)
            b = FakeBroker(
                closes={"AAPL": [200, 200.5], "NVDA": [100, 104], "QQQ": [500, 501]},
                articles={"AAPL": good, "NVDA": [dict(a, headline=a["headline"].replace("AAPL", "NVDA")) for a in good],
                          "QQQ": []})
            bot.run_cycle(b, logging.getLogger("test"))
            self.assertEqual(b.orders, [("BUY", "AAPL", 25.0)])
  # NVDA already ran 4% = priced in; QQQ no news
        finally:
            config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE, config.COOLDOWN_MINUTES = old

    def test_cycle_respects_exposure_cap(self):
        import bot
        now = time.time()
        good = lambda tk: [{"t": now - 600, "headline": f"{tk} beats estimates, raises guidance", "summary": ""},
                           {"t": now - 1200, "headline": f"Analysts upgrade {tk}", "summary": ""}]
        tmp = tempfile.mkdtemp()
        old = (config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE, config.COOLDOWN_MINUTES,
               config.TOTAL_BUDGET, config.MAX_TOTAL_EXPOSURE_PCT)
        try:
            config.STRATEGY, config.STATE_FILE, config.COOLDOWN_MINUTES = "news", os.path.join(tmp, "s.json"), 0
            config.STAKES = {s: 200.0 for s in ("QQQ", "AAPL", "NVDA", "GOOGL")}
            config.SYMBOLS = list(config.STAKES)
            config.TOTAL_BUDGET, config.MAX_TOTAL_EXPOSURE_PCT = 1000.0, 6   # $60 cap
            b = FakeBroker(closes={s: [100, 100.2] for s in config.SYMBOLS},
                           articles={s: good(s) for s in config.SYMBOLS})
            bot.run_cycle(b, logging.getLogger("test"))
            self.assertEqual([o[2] for o in b.orders], [25.0, 25.0, 10.0])  # 4th gets nothing
        finally:
            (config.STRATEGY, config.STAKES, config.SYMBOLS, config.STATE_FILE, config.COOLDOWN_MINUTES,
             config.TOTAL_BUDGET, config.MAX_TOTAL_EXPOSURE_PCT) = old


if __name__ == "__main__":
    unittest.main()


class SameStoryTests(unittest.TestCase):
    def test_top_article_id_picks_strongest(self):
        arts = [dict(art("Company to present at conference", 0.1), id=1),
                dict(art("Stock surges on upgrade", 0.5), id=2),
                dict(art("Shares rally", 30), id=3)]
        self.assertEqual(news.top_article_id(arts, NOW), 2)
        self.assertIsNone(news.top_article_id([art("Company to present at conference")], NOW))

    def test_one_story_many_symbols_keeps_strongest(self):
        cands = {"QQQ": (0.40, 7), "AAPL": (0.55, 7), "NVDA": (0.70, 7), "GOOGL": (0.50, 9)}
        self.assertEqual(news.same_story_losers(cands), {"QQQ", "AAPL"})

    def test_different_stories_and_missing_ids_untouched(self):
        self.assertEqual(news.same_story_losers({"AAPL": (0.5, 1), "NVDA": (0.6, 2)}), set())
        self.assertEqual(news.same_story_losers({"AAPL": (0.5, None), "NVDA": (0.6, None)}), set())
        self.assertEqual(news.same_story_losers({}), set())

    def test_tie_goes_to_first_symbol(self):
        self.assertEqual(news.same_story_losers({"QQQ": (0.5, 4), "AAPL": (0.5, 4)}), {"AAPL"})
