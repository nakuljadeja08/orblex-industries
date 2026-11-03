/**
 * RunPipeline.jsx — header button + confirmation dialog that triggers the
 * GitHub Actions pipeline through /api/run-pipeline.
 *
 * Auth is Supabase magic-link sign-in, and LoginGate.jsx requires it before the
 * dashboard renders. Every table (private_access_schema.sql) is read through
 * authClient, by trading admins only. The server re-verifies the session and checks an email allowlist before it
 * touches GitHub — hiding this button is not the security boundary.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import { createClient } from "@supabase/supabase-js";

export const authClient = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY,
  { auth: { storageKey: "orblex-admin-auth", persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } },
);

// The admin session: undefined while loading, then the session or null.
export function useAdminSession() {
  const [session, setSession] = useState(undefined);
  useEffect(() => {
    authClient.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = authClient.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);
  return session;
}

const PENDING_KEY = "orblex-run-pending";   // reopen the dialog after the magic-link round trip
const safeStore = {
  get: k => { try { return window.localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { window.localStorage.setItem(k, v); } catch { /* private mode */ } },
  del: k => { try { window.localStorage.removeItem(k); } catch { /* private mode */ } },
};

const isActive = run => run && run.status !== "completed";
const runLabel = run => {
  if (!run) return null;
  if (run.status === "queued" || run.status === "waiting" || run.status === "pending" || run.status === "requested") return "QUEUED";
  if (run.status === "in_progress") return "RUNNING";
  return (run.conclusion || "done").toUpperCase();
};

async function api(session, method, body) {
  const r = await fetch("/api/run-pipeline", {
    method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.access_token}` },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch {
    throw new Error(r.status === 404 || text.startsWith("<")
      ? "The /api endpoint isn't reachable. It only exists on Vercel (or locally via `vercel dev`)."
      : `Unexpected response (${r.status}).`);
  }
  if (!r.ok) { const e = new Error(data.error || `Request failed (${r.status})`); e.status = r.status; e.run = data.run; throw e; }
  return data;
}

export default function RunPipeline({ onRunFinished }) {
  const [open,    setOpen]    = useState(false);
  const [session, setSession] = useState(null);
  const [email,   setEmail]   = useState("");
  const [linkSent,setLinkSent]= useState(false);
  const [opts,    setOpts]    = useState({ dry_run: false, skip_newsletter: false, force_run: false, stages: "" });
  const [busy,    setBusy]    = useState(false);
  const [error,   setError]   = useState(null);
  const [run,     setRun]     = useState(null);
  const [dispatchedAt, setDispatchedAt] = useState(null);
  const dialogRef = useRef(null);
  const openerRef = useRef(null);
  const runRef    = useRef(null);

  // ── Session ──
  useEffect(() => {
    authClient.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = authClient.auth.onAuthStateChange((event, s) => {
      setSession(s);
      if (event === "SIGNED_IN" && safeStore.get(PENDING_KEY)) { safeStore.del(PENDING_KEY); setOpen(true); }
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  // ── Status polling: while the dialog is open, or while a run we know of is live ──
  const refreshStatus = useCallback(async () => {
    if (!session) return;
    try {
      const { run: latest } = await api(session, "GET");
      const prev = runRef.current;
      runRef.current = latest;
      setRun(latest);
      // A run we watched go live just finished cleanly — pull the fresh data.
      if (prev && isActive(prev) && latest && latest.id === prev.id && !isActive(latest) && latest.conclusion === "success") onRunFinished?.();
    } catch (e) {
      if (e.status === 401 || e.status === 403) setError(e.message);
    }
  }, [session, onRunFinished]);

  useEffect(() => { if (session) refreshStatus(); }, [session, refreshStatus]);
  useEffect(() => {
    if (!session) return;
    // A just-dispatched run takes a few seconds to appear, so poll quickly at first.
    const justDispatched = dispatchedAt && Date.now() - dispatchedAt < 60_000;
    if (!open && !isActive(run) && !justDispatched) return;
    const t = setInterval(refreshStatus, justDispatched ? 4000 : 15000);
    return () => clearInterval(t);
  }, [session, open, run, dispatchedAt, refreshStatus]);

  // ── Dialog a11y: Esc closes, focus moves in and returns to the opener ──
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement;
    openerRef.current = prev;
    dialogRef.current?.querySelector("input,button:not([disabled])")?.focus();
    const onKey = e => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); openerRef.current?.focus?.(); };
  }, [open]);

  const sendLink = async e => {
    e.preventDefault();
    setBusy(true); setError(null);
    safeStore.set(PENDING_KEY, "1");
    const { error: err } = await authClient.auth.signInWithOtp({
      email: email.trim(),
      // Never create accounts from this form — only pre-provisioned users get a link.
      options: { shouldCreateUser: false, emailRedirectTo: window.location.origin },
    });
    setBusy(false);
    if (err) { safeStore.del(PENDING_KEY); setError(err.message); } else setLinkSent(true);
  };

  const confirmRun = async () => {
    setBusy(true); setError(null);
    try {
      await api(session, "POST", opts);
      setDispatchedAt(Date.now());
      runRef.current = null;
      setRun(null);
      setTimeout(refreshStatus, 3000);
    } catch (e) {
      setError(e.message);
      if (e.run) { runRef.current = e.run; setRun(e.run); }
    } finally { setBusy(false); }
  };

  const signOut = async () => { await authClient.auth.signOut(); runRef.current = null; setRun(null); setLinkSent(false); };

  const live      = isActive(run);
  const awaiting  = dispatchedAt && !run && Date.now() - dispatchedAt < 60_000;
  const lbl       = runLabel(run);
  const whatRuns  = opts.stages.trim() ? `stages ${opts.stages.trim()}` : "the daily stages (context → analyst → risk → newsletter)";

  return (
    <>
      <button className={`run-btn ${live ? "run-btn-live" : ""}`} onClick={() => setOpen(true)}
              aria-haspopup="dialog" aria-label={live ? `Pipeline ${lbl?.toLowerCase()}. Open run dialog.` : "Run the pipeline"}>
        {live ? <><span className="run-dot" aria-hidden="true" />{lbl}</> : "▶ RUN PIPELINE"}
      </button>

      {open && (
        <div className="run-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setOpen(false); }}>
          <div className="run-dialog" role="dialog" aria-modal="true" aria-labelledby="run-title" ref={dialogRef}>
            <div className="run-head">
              <div id="run-title" className="run-title">RUN THE PIPELINE</div>
              <button className="run-x" onClick={() => setOpen(false)} aria-label="Close">✕</button>
            </div>

            {!session ? (
              linkSent ? (
                <div className="run-body">
                  <p className="run-p">Check <b>{email}</b> for a sign-in link. Open it in this browser and this window will reopen, ready to run.</p>
                  <button className="run-secondary" onClick={() => setLinkSent(false)}>Use a different email</button>
                </div>
              ) : (
                <form className="run-body" onSubmit={sendLink}>
                  <p className="run-p">Only the portfolio owner can start a run. Sign in with your email and we'll send you a one-time link.</p>
                  <label className="run-label" htmlFor="run-email">EMAIL</label>
                  <input id="run-email" className="run-input" type="email" required autoComplete="email"
                         value={email} onChange={e => setEmail(e.target.value)} />
                  <button className="run-primary" type="submit" disabled={busy || !email.trim()}>
                    {busy ? "SENDING…" : "SEND SIGN-IN LINK"}
                  </button>
                </form>
              )
            ) : (
              <div className="run-body">
                <div className="run-who">
                  Signed in as <b>{session.user?.email}</b>
                  <button className="run-link" onClick={signOut}>sign out</button>
                </div>

                {(live || awaiting || (run && dispatchedAt)) && (
                  <div className={`run-status run-status-${live || awaiting ? "live" : run?.conclusion === "success" ? "ok" : "fail"}`} role="status">
                    <span>{awaiting ? "DISPATCHED: waiting for GitHub to start the run…" : `${run.title || "Pipeline run"} · ${lbl}`}</span>
                    {run?.html_url && <a href={run.html_url} target="_blank" rel="noopener noreferrer">view logs ↗</a>}
                  </div>
                )}

                {!live && !awaiting && (
                  <>
                    <fieldset className="run-opts">
                      <legend className="run-label">OPTIONS</legend>
                      {[
                        ["dry_run",         "Dry run",         "run everything but write nothing to Supabase"],
                        ["skip_newsletter", "Skip newsletter", "don't email the newsletter"],
                        ["force_run",       "Run even if the market is closed", "skip the NYSE holiday/weekend check"],
                      ].map(([k, label, hint]) => (
                        <label key={k} className="run-check">
                          <input type="checkbox" checked={opts[k]} onChange={e => setOpts(o => ({ ...o, [k]: e.target.checked }))} />
                          <span>{label}<span className="run-hint">{hint}</span></span>
                        </label>
                      ))}
                      <label className="run-label" htmlFor="run-stages" style={{ marginTop: 10 }}>STAGES (OPTIONAL)</label>
                      <input id="run-stages" className="run-input" placeholder="all, or e.g. 3 35 4 5" inputMode="numeric"
                             value={opts.stages} onChange={e => setOpts(o => ({ ...o, stages: e.target.value.replace(/[^\d ]/g, "") }))} />
                    </fieldset>

                    <div className="run-confirm">
                      This starts <b>{whatRuns}</b> on GitHub Actions
                      {opts.dry_run ? " as a dry run" : ", overwriting today's results"}
                      {opts.skip_newsletter || opts.dry_run ? "" : ", and emails the newsletter"}. A full run takes about 10–20 minutes.
                    </div>
                    <div className="run-actions">
                      <button className="run-secondary" onClick={() => setOpen(false)}>CANCEL</button>
                      <button className="run-primary" onClick={confirmRun} disabled={busy}>{busy ? "STARTING…" : "CONFIRM & RUN"}</button>
                    </div>
                  </>
                )}
                {run && !live && run.conclusion === "success" && dispatchedAt && (
                  <button className="run-secondary" style={{ marginTop: 10 }} onClick={() => { onRunFinished?.(); setOpen(false); }}>RELOAD DASHBOARD DATA</button>
                )}
              </div>
            )}
            {error && <div className="run-error" role="alert">{error}</div>}
          </div>
        </div>
      )}
    </>
  );
}

export const RUN_CSS = `
  .run-btn{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.06em;padding:5px 12px;border-radius:4px;border:1px solid rgba(60,174,111,.35);background:var(--green-dim);color:var(--green);display:inline-flex;align-items:center;gap:6px;}
  .run-btn:hover{background:rgba(60,174,111,.25);}
  .run-btn-live{border-color:var(--border-amber);background:var(--amber-glow);color:var(--amber);}
  .run-dot{width:6px;height:6px;border-radius:50%;background:var(--amber);animation:pulse 1.4s infinite;}
  .run-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:100;padding:16px;}
  .run-dialog{width:100%;max-width:440px;background:var(--bg2);border:1px solid var(--border-amber);border-radius:6px;box-shadow:0 20px 60px rgba(0,0,0,.6);max-height:calc(100vh - 32px);overflow:auto;}
  .run-head{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border);}
  .run-title{font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--amber);}
  .run-x{color:var(--text-dimmer);font-size:14px;padding:2px 6px;}
  .run-x:hover{color:var(--text);}
  .run-body{padding:16px 18px;display:flex;flex-direction:column;gap:10px;}
  .run-p{font-size:13px;color:var(--text-dim);line-height:1.6;}
  .run-p b,.run-confirm b,.run-who b{color:var(--text);font-weight:500;}
  .run-label{font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--text-dimmer);display:block;}
  .run-input{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:8px 10px;color:var(--text);font-family:var(--mono);font-size:12px;}
  .run-input:focus{outline:none;border-color:var(--border-amber);}
  .run-primary{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.06em;padding:9px 14px;border-radius:4px;background:var(--amber);color:#111;text-align:center;}
  .run-primary:disabled{opacity:.5;cursor:not-allowed;}
  .run-secondary{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.06em;padding:9px 14px;border-radius:4px;border:1px solid var(--border);color:var(--text-dim);text-align:center;}
  .run-secondary:hover{color:var(--text);}
  .run-link{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);text-decoration:underline;margin-left:8px;}
  .run-who{font-size:12px;color:var(--text-dim);}
  .run-opts{border:none;display:flex;flex-direction:column;gap:8px;}
  .run-opts legend{margin-bottom:6px;}
  .run-check{display:flex;gap:8px;align-items:flex-start;font-size:12px;color:var(--text);cursor:pointer;}
  .run-check input{margin-top:3px;accent-color:var(--amber);}
  .run-hint{display:block;font-size:10px;color:var(--text-dimmer);}
  .run-confirm{font-size:12px;color:var(--text-dim);line-height:1.6;background:var(--bg3);border-left:2px solid var(--amber-dim);padding:10px 12px;border-radius:0 4px 4px 0;}
  .run-actions{display:grid;grid-template-columns:1fr 2fr;gap:8px;}
  .run-status{display:flex;justify-content:space-between;gap:10px;align-items:center;font-family:var(--mono);font-size:11px;padding:9px 12px;border-radius:4px;border:1px solid;}
  .run-status a{color:inherit;white-space:nowrap;}
  .run-status-live{color:var(--amber);background:var(--amber-glow);border-color:var(--border-amber);}
  .run-status-ok{color:var(--green);background:var(--green-dim);border-color:rgba(60,174,111,.3);}
  .run-status-fail{color:var(--red);background:var(--red-dim);border-color:rgba(226,75,74,.3);}
  .run-error{margin:0 18px 16px;font-family:var(--mono);font-size:11px;color:var(--red);background:var(--red-dim);border:1px solid rgba(226,75,74,.3);border-radius:4px;padding:9px 12px;line-height:1.5;}
`;
