/**
 * api/run-pipeline.js — Vercel serverless function behind the dashboard's
 * "RUN PIPELINE" button.
 *
 *   GET  → status of the most recent pipeline workflow run
 *   POST → dispatch .github/workflows/orblex_pipeline.yml (workflow_dispatch)
 *
 * Both require a Supabase Auth session (Authorization: Bearer <access_token>)
 * whose email is on the PIPELINE_ADMIN_EMAILS allowlist. The GitHub token never
 * leaves the server — the browser only ever holds its own Supabase session.
 *
 * Environment (Vercel project settings):
 *   GITHUB_TOKEN            fine-grained PAT, this repo only, "Actions: read & write"
 *   PIPELINE_ADMIN_EMAILS   comma-separated emails allowed to trigger runs
 *   GITHUB_REPO             optional, "owner/repo"; defaults to the repository Vercel deploys from
 *   GITHUB_DISPATCH_REF     optional, branch whose workflow + code runs, default "release"
 *   VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY   already set for the frontend
 */

const REPO     = process.env.GITHUB_REPO || (process.env.VERCEL_GIT_REPO_OWNER && process.env.VERCEL_GIT_REPO_SLUG
  ? `${process.env.VERCEL_GIT_REPO_OWNER}/${process.env.VERCEL_GIT_REPO_SLUG}` : "");
const WORKFLOW = "orblex_pipeline.yml";
const REF      = process.env.GITHUB_DISPATCH_REF || "release";
const SUPABASE_URL = process.env.SUPABASE_URL || process.env.VITE_SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_PUBLISHABLE_KEY || process.env.VITE_SUPABASE_PUBLISHABLE_KEY;

// run_all.py takes --stages as ints: 0-9 plus 35 for drift (3.5).
const STAGES_RE = /^(?:\d{1,2})(?: \d{1,2}){0,11}$/;
const VALID_STAGES = new Set(["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "35"]);

async function authorize(req) {
  const header = req.headers.authorization || "";
  const token  = header.startsWith("Bearer ") ? header.slice(7) : null;
  if (!token) return { status: 401, error: "Sign in required." };
  if (!SUPABASE_URL || !SUPABASE_KEY) return { status: 500, error: "Supabase env vars missing on the server." };

  // Ask Supabase Auth who this token belongs to — this verifies the signature
  // and expiry server-side, so a forged or stale token is rejected.
  const r = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { Authorization: `Bearer ${token}`, apikey: SUPABASE_KEY },
  });
  if (!r.ok) return { status: 401, error: "Session expired — sign in again." };
  const user = await r.json();

  const allowed = (process.env.PIPELINE_ADMIN_EMAILS || "")
    .split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
  if (allowed.length === 0) return { status: 500, error: "PIPELINE_ADMIN_EMAILS is not configured." };
  if (!user?.email || !allowed.includes(user.email.toLowerCase())) {
    return { status: 403, error: `${user?.email || "This account"} is not allowed to run the pipeline.` };
  }
  return { user };
}

function gh(path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "orblex-dashboard",
      ...(init.headers || {}),
    },
  });
}

async function latestRuns(n = 5) {
  const r = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=${n}`);
  if (!r.ok) throw new Error(`GitHub API ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const { workflow_runs = [] } = await r.json();
  return workflow_runs.map(w => ({
    id: w.id, status: w.status, conclusion: w.conclusion, event: w.event,
    html_url: w.html_url, created_at: w.created_at, updated_at: w.updated_at,
    title: w.display_title,
  }));
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "GET" && req.method !== "POST") {
    res.setHeader("Allow", "GET, POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  const auth = await authorize(req);
  if (auth.error) return res.status(auth.status).json({ error: auth.error });
  if (!process.env.GITHUB_TOKEN) return res.status(500).json({ error: "GITHUB_TOKEN is not configured." });
  if (!REPO) return res.status(500).json({ error: "GITHUB_REPO is not configured." });

  try {
    if (req.method === "GET") {
      const runs = await latestRuns(1);
      return res.status(200).json({ run: runs[0] || null });
    }

    // ── POST: dispatch ──
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const stages = String(body.stages || "").trim().replace(/\s+/g, " ");
    if (stages && (!STAGES_RE.test(stages) || !stages.split(" ").every(s => VALID_STAGES.has(s)))) {
      return res.status(400).json({ error: "Stages must be space-separated numbers from 0-9 (35 = drift)." });
    }

    // One run at a time — a manual run on top of the scheduled one would
    // double-write the same day's reports.
    const active = (await latestRuns(5)).find(w => w.status !== "completed");
    if (active) {
      return res.status(409).json({ error: "A pipeline run is already in progress.", run: active });
    }

    const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST",
      body: JSON.stringify({
        ref: REF,
        inputs: {
          dry_run:         body.dry_run ? "true" : "false",
          skip_newsletter: body.skip_newsletter ? "true" : "false",
          force_run:       body.force_run ? "true" : "false",
          stages,
          source:          "dashboard",
        },
      }),
    });
    if (!r.ok) {
      return res.status(502).json({ error: `GitHub refused the dispatch (${r.status}): ${(await r.text()).slice(0, 300)}` });
    }
    console.log(`pipeline dispatched by ${auth.user.email} on ${REF}`, { stages, dry_run: !!body.dry_run });
    return res.status(202).json({ ok: true, ref: REF, dispatched_at: new Date().toISOString() });
  } catch (e) {
    return res.status(502).json({ error: e.message || "GitHub request failed." });
  }
}
