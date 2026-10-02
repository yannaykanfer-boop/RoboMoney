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

## What this is and isn't

- It's a deterministic rule-based bot, not an LLM deciding trades live —
  same as most real algo-trading setups. Predictable and debuggable.
- It only ever holds **one long position per symbol** — buys with the
  configured stake, sells the whole position on the opposite signal. No
  leverage, no shorting, no options.
- Not financial advice. You are responsible for the strategy, the capital,
  and monitoring the account. Start small, watch it run in paper mode first,
  and only allocate money you can afford to lose.
