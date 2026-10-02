import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER = _bool("ALPACA_PAPER", True)

def _parse_stakes(raw, fallback_amount):
    stakes = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            sym, amt = part.split(":", 1)
            stakes[sym.strip().upper()] = float(amt.strip())
        else:
            stakes[part.upper()] = fallback_amount
    return stakes


# STAKES: comma-separated SYMBOL:DOLLARS pairs, e.g. "QQQ:310,NVDA:200,AAPL:200".
# Falls back to the older SYMBOLS + STAKE_PER_SYMBOL (same amount for everyone) if STAKES isn't set.
STAKES = _parse_stakes(
    os.getenv("STAKES", os.getenv("SYMBOLS", "SPY,QQQ")),
    float(os.getenv("STAKE_PER_SYMBOL", "100")),
)
SYMBOLS = list(STAKES.keys())
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET", "1000"))
STRATEGY = os.getenv("STRATEGY", "trend").strip().lower()
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "15"))
COOLDOWN_MINUTES = float(os.getenv("COOLDOWN_MINUTES", "30"))
TREND_BUFFER_PCT = float(os.getenv("TREND_BUFFER_PCT", "0.2"))
# While paused, trading only resumes once a fast/slow gap this large shows up (a real trend, not noise).
PAUSE_RESUME_BUFFER_PCT = float(os.getenv("PAUSE_RESUME_BUFFER_PCT", str(TREND_BUFFER_PCT * 3)))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))
# Downtrend-stop only fires once a position is down at least this many dollars
# AND the trend clears the same buffer as a normal signal — not on any 1-cent dip.
DOWNTREND_STOP_MIN_LOSS_DOLLARS = float(os.getenv("DOWNTREND_STOP_MIN_LOSS_DOLLARS", "15"))
# Portfolio-wide circuit breaker: pause ALL new trading once the whole account
# is down this many dollars from where it started. Existing positions' own
# stop-loss/downtrend-stop still work while paused.
MAX_OVERALL_LOSS_DOLLARS = float(os.getenv("MAX_OVERALL_LOSS_DOLLARS", "30"))
# Assumed cost per trade, shown on the dashboard only — Alpaca itself charges $0 commission.
EST_COST_PER_TRADE = float(os.getenv("EST_COST_PER_TRADE", "1.00"))

DAILY_FAST_MA = 20
DAILY_SLOW_MA = 50
DAILY_BUFFER_PCT = 0.5
DAILY_ACT_WINDOW_MIN = 30
# The daily strategy acts on daily prices, so it should trade at most once per day per stock.
OPENCLOSE_DAYS = 10
if STRATEGY in ("daily", "openclose"):
    COOLDOWN_MINUTES = max(COOLDOWN_MINUTES, 24 * 60)
# ICT setups confirm on a specific 1-minute bar within a narrow 30-min window —
# polling every 5 min (the other strategies' default) would miss most of them.
if STRATEGY == "ict":
    POLL_INTERVAL_SECONDS = min(POLL_INTERVAL_SECONDS, 60)

FAST_MA = 5
SLOW_MA = 20
MEANREV_LOOKBACK = 12
MEANREV_BUY_PCT = 0.97
MEANREV_SELL_PCT = 1.02

# ICT-style strategy: liquidity sweep + SMT divergence vs SPY + market structure shift.
ICT_WINDOW_MINUTES = int(os.getenv("ICT_WINDOW_MINUTES", "30"))  # only act in the first N min after the 9:30 ET open
ICT_REF_SYMBOL = os.getenv("ICT_REF_SYMBOL", "SPY")
ICT_RANGE_BARS = int(os.getenv("ICT_RANGE_BARS", "20"))   # bars used to define the "recent range" that gets swept
ICT_REACTION_BARS = int(os.getenv("ICT_REACTION_BARS", "10"))  # bars after the range to look for sweep + reversal in

LOG_FILE = "trading_bot.log"
STATE_FILE = "state.json"


def validate():
    problems = []
    if not API_KEY or not SECRET_KEY:
        problems.append("ALPACA_API_KEY / ALPACA_SECRET_KEY are missing. Copy .env.example to .env and fill them in.")
    if not STAKES:
        problems.append("STAKES (or SYMBOLS) is empty.")
    if any(amt <= 0 for amt in STAKES.values()):
        problems.append("All STAKES amounts must be positive.")
    total = sum(STAKES.values())
    if total > TOTAL_BUDGET:
        problems.append(
            f"Total STAKES (${total:.0f}) exceeds TOTAL_BUDGET (${TOTAL_BUDGET:.0f}). "
            "Lower your STAKES or raise TOTAL_BUDGET in .env if you actually have more to invest."
        )
    if STRATEGY not in ("trend", "meanrev", "daily", "openclose", "ict"):
        problems.append("STRATEGY must be 'trend', 'meanrev', 'daily', 'openclose' or 'ict'.")
    return problems
