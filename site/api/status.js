// GET /api/status: the bot's latest state (paper account only) and its recent runs.
// Deliberately leaves out the owner's real (Greenlight) portfolio that state.json also holds.
const { gh, recentRuns, OWNER, REPO } = require("./_github");

async function loadState() {
  // Fresh copy through the API (works without a token on a public repo; a token raises rate limits).
  const r = await gh(`/contents/state.json?ref=main`, { headers: { Accept: "application/vnd.github.raw+json" } });
  if (r.ok) return r.json();
  const raw = await fetch(`https://raw.githubusercontent.com/${OWNER}/${REPO}/main/state.json`);
  if (!raw.ok) throw new Error(`state.json: ${r.status}/${raw.status}`);
  return raw.json();
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  try {
    const [s, runs] = await Promise.all([loadState(), recentRuns()]);
    const lastT = Math.max(0, ...(s.decisions || []).map(d => d.t));
    const latest = (s.decisions || []).filter(d => d.t === lastT);
    res.status(200).json({
      canStart: Boolean(process.env.GITHUB_TOKEN),
      needsPin: Boolean(process.env.BOT_PIN),
      bot: {
        updated_at: s.updated_at, market_open: s.market_open, paused: s.paused, pause_note: s.pause_note,
        strategy: s.strategy, paper: s.paper, budget: s.total_budget, balance: s.real_equity,
        change: s.change_abs, trades: (s.trades || []).length, stakes: s.stakes,
        positions: Object.fromEntries(Object.entries(s.positions || {}).map(([k, p]) =>
          [k, { held: p.qty > 0, price: p.price, value: p.value, pnl: p.pnl, signal: p.signal }])),
        decisions: latest, decided_at: lastT || null,
        alerts: (s.desk_alerts || []).slice(-3),
      },
      runs: runs.runs, runsError: runs.error || null,
    });
  } catch (e) {
    res.status(502).json({ error: `Couldn't read the bot's status (${e.message}).` });
  }
};
