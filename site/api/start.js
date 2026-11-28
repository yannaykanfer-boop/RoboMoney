// POST /api/start: start one run of the bot's GitHub workflow ("Trade"). Paper money only.
// Guards: needs GITHUB_TOKEN; optional BOT_PIN; refuses while a run is queued or running, or if
// one started less than 10 minutes ago (so the link can't be used to spam runs).
const { gh, recentRuns, WORKFLOW } = require("./_github");
const MIN_GAP_MIN = 10;

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") return res.status(405).json({ error: "Use POST." });
  if (!process.env.GITHUB_TOKEN)
    return res.status(503).json({ error: "The Start button isn't connected to GitHub yet (GITHUB_TOKEN is missing)." });
  const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
  if (process.env.BOT_PIN && String(body.pin || "") !== process.env.BOT_PIN)
    return res.status(401).json({ error: "Wrong PIN." });

  const { runs, error } = await recentRuns(3);
  if (error) return res.status(502).json({ error: `Couldn't check recent runs: ${error}.` });
  const busy = runs.find(r => r.status === "queued" || r.status === "in_progress" || r.status === "waiting");
  if (busy) return res.status(409).json({ error: "The bot is already running. Watch it finish below." });
  const last = runs[0];
  if (last) {
    const mins = (Date.now() - Date.parse(last.created_at)) / 60000;
    if (mins < MIN_GAP_MIN)
      return res.status(429).json({ error: `The bot ran ${Math.max(1, Math.round(mins))} min ago. ` +
                                           `You can start it again in ${Math.ceil(MIN_GAP_MIN - mins)} min.` });
  }
  const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref: "main" }) });
  if (r.status === 204) return res.status(200).json({ ok: true, startedAt: new Date().toISOString() });
  const text = await r.text();
  return res.status(502).json({ error: `GitHub refused the start (${r.status}). ${text.slice(0, 200)}` });
};
