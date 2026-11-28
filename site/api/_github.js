// Shared GitHub helpers for the RoboMoney control page. The GitHub token lives only here, on the
// server (Vercel environment variable GITHUB_TOKEN); the browser never sees it.
const OWNER = "yannaykanfer-boop";
const REPO = "RoboMoney";
const WORKFLOW = "trade.yml";

async function gh(path, opts = {}) {
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "robomoney-control",
                    "X-GitHub-Api-Version": "2022-11-28", ...(opts.headers || {}) };
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  return fetch(`https://api.github.com/repos/${OWNER}/${REPO}${path}`, { ...opts, headers });
}

async function recentRuns(n = 6) {
  const r = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=${n}`);
  if (!r.ok) return { error: `GitHub said ${r.status}`, runs: [] };
  const d = await r.json();
  return { runs: (d.workflow_runs || []).map(x => ({
    id: x.id, status: x.status, conclusion: x.conclusion, event: x.event,
    created_at: x.created_at, updated_at: x.updated_at, url: x.html_url })) };
}

module.exports = { gh, recentRuns, OWNER, REPO, WORKFLOW };
