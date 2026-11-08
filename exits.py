"""Exit rules for the news strategy: when to sell a position that hasn't hit the stop-loss.

News moves prices for a day or two, then the effect fades. Without these rules a position only
closed on negative news or the 5% stop-loss, so a winning trade could ride all the way back down.

Two rules, both pure functions (no network) so they're unit-testable offline:
  * Time limit: sell after NEWS_MAX_HOLD_DAYS trading days, unless the news score is still at or
    above the buy line (the reason to own it is still there).
  * Loose trailing stop: once a position has been up at least TRAIL_ACTIVATE_PCT, sell if it
    falls TRAIL_PCT from its highest price since the buy.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config

NY = ZoneInfo("America/New_York")


def trading_days_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end` (market holidays are ignored)."""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def check(entry, price, peak, opened_at, now, news_score):
    """Returns (reason or None, updated peak). reason is 'trailing-stop' or 'time-limit'."""
    peak = max(p for p in (peak, entry, price) if p)
    if entry and peak >= entry * (1 + config.TRAIL_ACTIVATE_PCT / 100):
        if price <= peak * (1 - config.TRAIL_PCT / 100):
            return "trailing-stop", peak
    if opened_at:
        held = trading_days_between(datetime.fromtimestamp(opened_at, NY).date(),
                                    datetime.fromtimestamp(now, NY).date())
        if held >= config.NEWS_MAX_HOLD_DAYS and news_score < config.NEWS_BUY_THRESHOLD:
            return "time-limit", peak
    return None, peak


def opened_at(trades, symbol):
    """Time of the most recent BUY of `symbol` that hasn't been sold since, or None."""
    t_open = None
    for t in sorted(trades, key=lambda x: x["t"]):
        if t["symbol"] != symbol:
            continue
        t_open = t["t"] if t["side"] == "BUY" else None
    return t_open
