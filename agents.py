"""The trading desk's agents: one small job each, and each one explains itself.

Every agent is a plain function that looks at data and returns a Verdict. None of them touch the
broker, so they're easy to test offline and none of them can place an order. bot.py asks each agent
in turn, and the Head of Desk (desk.py) reviews their verdicts before anything is executed.

    SEARCH   finds the trade idea (news score / price signal for the chosen strategy)
    EXIT     protects open positions: stop-loss, downtrend stop, trailing stop, time limit
    RISK     enforces the house rules: trading window, cooldown, daily trade limit, same-story
    CRASH    refuses to buy a stock that is falling hard right now (the stock-market "rug" check)
    SIZER    decides how many dollars (fractional Kelly, capped by total exposure)

Agents the original "8-agent" idea had that are left out on purpose: WHALE (copying big wallets has
no clean free equivalent for stocks) and SHILL (hype tracking needs paid social-media APIs).
"""

from dataclasses import dataclass, asdict

import config
import exits
import news
import strategy

OVERRIDE_TAGS = ("stop-loss", "downtrend-stop", "trailing-stop", "time-limit")


@dataclass
class Verdict:
    agent: str        # search | exit | risk | crash | sizer
    action: str       # search: buy/sell/hold · exit: sell · others: ok/veto
    note: str = ""    # plain-English reason, shown on the dashboard
    tag: str = ""     # short machine tag, e.g. "stop-loss" (becomes the trade's reason)

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v != ""}


def search(strat, closes, series, has_position, ict_ref_bars=None):
    """SEARCH: what the strategy wants to do with this symbol."""
    if strat == "ict":
        action = strategy.decide_ict(series, ict_ref_bars, has_position)
        return Verdict("search", action, strategy.describe_ict(series, ict_ref_bars))
    if strat == "news":
        action, note = news.decide_news(closes, series, has_position)
        return Verdict("search", action, note)
    return Verdict("search", strategy.decide(series, has_position), strategy.describe(series))


def exit_watch(strat, closes, price, qty, entry, peak, opened_at, now, series, proposed_action):
    """EXIT: should an open position be sold to protect it? Returns (Verdict or None, peak).

    The peak (highest price since the buy) is only updated when the trailing-stop rules are checked,
    exactly as before; otherwise the incoming peak is handed back unchanged."""
    if entry and price < entry * (1 - config.STOP_LOSS_PCT / 100):
        return Verdict("exit", "sell", f"stop-loss — now ${price:.2f}, bought ${entry:.2f} "
                                       f"(limit {config.STOP_LOSS_PCT:g}% down)", "stop-loss"), peak
    if entry and strat == "trend" and (entry - price) * qty >= config.DOWNTREND_STOP_MIN_LOSS_DOLLARS:
        fast = strategy.moving_avg(closes, config.FAST_MA)
        slow = strategy.moving_avg(closes, config.SLOW_MA)
        if fast is not None and slow is not None and fast < slow * (1 - config.TREND_BUFFER_PCT / 100):
            return Verdict("exit", "sell", f"downtrend-stop — down ${(entry - price) * qty:.2f} and the "
                                           f"trend turned down", "downtrend-stop"), peak
    if strat == "news" and proposed_action != "sell":
        reason, peak = exits.check(entry, price, peak, opened_at, now, news.sentiment(series)[0])
        if reason:
            note = (f"{reason} — high ${peak:.2f}, now ${price:.2f}, bought ${entry:.2f}" if entry else reason)
            return Verdict("exit", "sell", note, reason), peak
    return None, peak


def risk(action, strat, in_act_window, in_cooldown, trades_today):
    """RISK: house rules for a non-emergency trade. Protective exits never come through here."""
    if not in_act_window:
        window = (f"first {config.ICT_WINDOW_MINUTES} min after open" if strat == "ict"
                  else f"last {config.DAILY_ACT_WINDOW_MIN} min of the day")
        return Verdict("risk", "veto", f"{action} signal noted outside the {window} — ignored.", "window")
    if in_cooldown:
        return Verdict("risk", "veto", f"{action} signal ignored (cooldown, avoids churning).", "cooldown")
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return Verdict("risk", "veto", f"{action} signal ignored (daily limit of {config.MAX_TRADES_PER_DAY} "
                                       f"trades reached).", "daily-limit")
    return Verdict("risk", "ok", "within trading window, cooldown and daily limit")


def same_story(symbol, winner):
    """RISK (news): one headline should be one bet, not several correlated ones."""
    return Verdict("risk", "veto", f"buy skipped — same news story is already behind {winner}, "
                                   f"so this would be the same bet twice.", "same-story")


def crash_guard(closes):
    """CRASH: don't buy a stock that has fallen CRASH_BLOCK_PCT or more from its high in the recent
    price window — something is going on that the headline score may not have caught. Returns a veto
    Verdict, or None when it's fine (or the check is turned off with CRASH_BLOCK_PCT=0)."""
    if config.CRASH_BLOCK_PCT <= 0 or not closes:
        return None
    high, now = max(closes), closes[-1]
    drop = (high - now) / high * 100 if high else 0.0
    if drop >= config.CRASH_BLOCK_PCT:
        return Verdict("crash", "veto", f"buy blocked — price is down {drop:.1f}% from ${high:.2f} in the "
                                        f"recent window (limit {config.CRASH_BLOCK_PCT:g}%).", "crash")
    return None


def size_news(trades, stake_cap, budget, open_value_fn, spent_this_cycle):
    """SIZER (news): fractional Kelly from the bot's own record, then the total-exposure cap.
    open_value_fn is only called if Kelly wants to buy. Returns (dollars, Verdict)."""
    stake, kelly_note = news.kelly_dollars(trades, stake_cap, budget)
    notes = [kelly_note]
    if stake > 0:
        stake, cap_note = news.cap_to_exposure(stake, open_value_fn(), spent_this_cycle, budget)
        notes.append(cap_note)
    return stake, Verdict("sizer", "ok" if stake > 0 else "veto", " · ".join(notes))
