# RoboMoney control page (Vercel)

Live at https://robomoney-replay.vercel.app

- `index.html`: the control page. Paper balance, a **Start bot** button, what the bot decided last time,
  recent runs, and when the next automatic run is.
- `replay.html` + `sim.json`: the 90-day Desk Replay simulation.
- `api/status.js`: reads the bot's `state.json` and recent GitHub Actions runs. It leaves out the
  real (Greenlight) portfolio.
- `api/start.js`: starts the `Trade` workflow. It refuses while a run is queued or running, or within 10
  minutes of the last one.

Vercel environment variables:
- `GITHUB_TOKEN` (needed for the Start button): a fine-grained GitHub token for this repo only, with
  **Actions: Read and write**. It stays on the server; the page never sees it.
- `BOT_PIN` (optional): if set, the button asks for this PIN.

The automatic market-hours runs come from a Claude scheduled task, not from this site.
