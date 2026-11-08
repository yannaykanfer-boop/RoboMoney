# Trading bot (Alpaca, rule-based)

Same moving-average / mean-reversion logic as the UNIT7 demo, but wired to a
real broker (Alpaca) instead of a simulated random walk. Defaults to **paper
trading** (fake money, real prices) so you can watch it work before any real
money is involved.

## Setup

1. Create a free account at https://alpaca.markets and generate **paper**
   API keys from the dashboard (Paper Trading section).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and paste in your API key/secret. Leave
   `ALPACA_PAPER=true`.
4. Run one test cycle:
   ```bash
   python bot.py --once
   ```
5. If that looks right, run it continuously:
   ```bash
   python bot.py
   ```

Watch `trading_bot.log` (and the console) for buy/sell decisions.

## Going live (real money)

Only after you've watched paper trading behave the way you expect for a few
days:

1. Generate **live** API keys from Alpaca and put them in `.env`.
2. Set `ALPACA_PAPER=false` in `.env`.
3. Run with the explicit flag: `python bot.py --live`
4. You'll be asked to type a confirmation phrase before any real order is
   placed.

## Configuration (`.env`)

- `SYMBOLS` — tickers to trade, comma-separated.
- `STAKE_PER_SYMBOL` — dollars allocated to each symbol (not total).
- `STRATEGY` — `trend` (moving-average crossover) or `meanrev` (buy dips,
  sell pops).
- `POLL_INTERVAL_SECONDS` — how often it re-checks prices.

## News strategy (`STRATEGY=news`)

An event-driven mode that reads live headlines for each symbol (Alpaca News
API — same keys, no extra signup) and trades on them:

1. **Reads news**: last `NEWS_LOOKBACK_HOURS` (24) of headlines per symbol.
2. **Scores it**: a transparent finance keyword lexicon ("beats estimates",
   "downgrade", "investigation"…), with negation handling, weighted toward
   recent stories (`NEWS_HALF_LIFE_HOURS`, 6). A single headline is
   discounted; it takes several before the score counts as a trend.
3. **Checks if it's priced in**: buys only if the score is at least `NEWS_BUY_THRESHOLD`
   *and* price hasn't already moved more than `NEWS_MAX_RUNUP_PCT` (1.5%)
   over the last `NEWS_PRICE_WINDOW_BARS` minute bars. If the price already
   ran, it skips.
4. **Exits** when news turns negative (`NEWS_SELL_THRESHOLD`), on the normal stop-loss, or on
   two rules in `exits.py`. **Time limit:** sell after `NEWS_MAX_HOLD_DAYS` (3) trading days,
   unless the news is still strong. **Trailing stop:** once a position is up `TRAIL_ACTIVATE_PCT`
   (3%), sell if it falls `TRAIL_PCT` (3%) from its high since the buy.
   Every run's decisions and reasons are shown in the dashboard's Execution Log.
5. **Sizes with fractional Kelly**: win rate and payoff come from the bot's
   *own* closed trades, blended with a conservative prior (55% / 1:1, worth 20
   trades) until real history builds up. Default is quarter-Kelly
   (`KELLY_FRACTION=0.25`), capped at each symbol's `STAKES` amount. If its
   record shows no edge, Kelly comes out ≤ 0 and **it stops buying**.

All existing guardrails (paper default, daily trade limit, cooldown, circuit
breaker, stop-loss) still apply. Run the offline tests with
`python -m unittest discover tests`.

Be realistic: headline sentiment on large caps is heavily competed. Funds
react in milliseconds, and this bot polls every few minutes. Treat it as an
experiment and paper-trade it for weeks before any real money. Beat
buy-and-hold first.

## What this is and isn't

- It's a deterministic rule-based bot, not an LLM deciding trades live —
  same as most real algo-trading setups. Predictable and debuggable.
- It only ever holds **one long position per symbol** — buys with the
  configured stake, sells the whole position on the opposite signal. No
  leverage, no shorting, no options.
- Not financial advice. You are responsible for the strategy, the capital,
  and monitoring the account. Start small, watch it run in paper mode first,
  and only allocate money you can afford to lose.
