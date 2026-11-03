// OpsViews.jsx — the automated jobs (automation_schema.sql, jobs.py, trading_jobs.yml).
//   AutomationView   today's schedule (ET) with each job's status, and the last 7 days
//   DailyReviewView  the end-of-day review (daily_reviews; admins only: it holds account values)
// Read-only: nothing here starts, stops or changes a job or an order.

import { useState, useEffect, useMemo } from "react";
import { authClient, useAdminSession } from "./RunPipeline";

export const OPS_CSS = `
  .ops-panel { background: var(--bg2); padding: 18px 20px; margin-top: 1px; }
  .ops-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .ops-title { font-family: var(--mono); font-size: 11px; letter-spacing: .1em; color: var(--text-dimmer); }
  .ops-big { font-size: 20px; color: var(--text); margin-top: 4px; }
  .ops-note { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer); line-height: 1.6; }
  .ops-table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
  .ops-table th { color: var(--text-dimmer); font-weight: 400; text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border);
                  font-size: 10px; letter-spacing: .05em; white-space: nowrap; }
  .ops-table td { padding: 8px; border-bottom: 1px solid rgba(255,255,255,.03); color: var(--text-dim); vertical-align: top; }
  .ops-chip { display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 2px; letter-spacing: .04em; white-space: nowrap; }
  .ops-OK { background: var(--green-dim); color: var(--green); }
  .ops-WARN { background: rgba(239,159,39,.14); color: var(--amber); }
  .ops-FAILED, .ops-MISSED { background: var(--red-dim); color: var(--red); }
  .ops-RUNNING, .ops-DISPATCHED { background: var(--blue-dim); color: var(--blue); }
  .ops-SKIPPED, .ops-LATER, .ops-OFF, .ops-BEFORE { background: rgba(255,255,255,.05); color: var(--text-dimmer); }
  .ops-grid { display: grid; grid-template-columns: 150px repeat(7, minmax(34px, 1fr)); gap: 3px; font-family: var(--mono); font-size: 10px; }
  .ops-grid div { padding: 4px 2px; text-align: center; border-radius: 2px; }
  .ops-grid .lbl { text-align: left; color: var(--text-dim); }
  .ops-dot-OK { background: var(--green-dim); color: var(--green); }
  .ops-dot-WARN { background: rgba(239,159,39,.14); color: var(--amber); }
  .ops-dot-FAILED, .ops-dot-MISSED { background: var(--red-dim); color: var(--red); }
  .ops-dot-SKIPPED, .ops-dot-NONE { background: rgba(255,255,255,.03); color: var(--text-dimmer); }
  .ops-dot-RUNNING, .ops-dot-DISPATCHED { background: var(--blue-dim); color: var(--blue); }
  .ops-sum { color: var(--text-dim); max-width: 520px; line-height: 1.5; }
  .ops-link { color: var(--blue); text-decoration: none; }
  .ops-link:hover { text-decoration: underline; }
  .ops-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--border); }
  .ops-card { background: var(--bg2); padding: 14px 16px; }
  .ops-card .k { font-family: var(--mono); font-size: 10px; color: var(--text-dimmer); letter-spacing: .06em; }
  .ops-card .v { font-size: 18px; color: var(--text); margin-top: 4px; }
  .ops-issue { font-family: var(--mono); font-size: 11px; color: var(--amber); margin: 3px 0; }
  .ops-pre { font-family: var(--mono); font-size: 11px; color: var(--text-dim); white-space: pre-wrap; line-height: 1.6; }
  .ops-select { font-family: var(--mono); font-size: 11px; background: var(--bg3); color: var(--text); border: 1px solid var(--border);
                border-radius: 2px; padding: 4px 6px; }
  @media (max-width: 760px) {
    .ops-cards { grid-template-columns: 1fr 1fr; }
    .ops-grid { grid-template-columns: 110px repeat(7, minmax(26px, 1fr)); }
    .ops-panel { padding: 14px; overflow-x: auto; }
  }
`;

const NY = "America/New_York";
const etDate = d => d.toLocaleDateString("en-CA", { timeZone: NY });
const etTime = iso => iso ? new Date(iso).toLocaleTimeString("en-US", { timeZone: NY, hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
const nowEt = () => {
  const p = new Intl.DateTimeFormat("en-US", { timeZone: NY, weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(new Date());
  const get = t => p.find(x => x.type === t)?.value;
  const dow = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[get("weekday")];
  return { dow, minutes: Number(get("hour")) % 24 * 60 + Number(get("minute")) };
};
const mins = t => { const [h, m] = String(t).split(":").map(Number); return h * 60 + m; };
// The pipeline workflow records itself in job_runs as "pipeline" / "longterm_run" (run_all.py).
const jobOf = s => (s.inputs && s.inputs.job)
  || (s.workflow === "orblex_pipeline.yml" ? (s.inputs?.book === "longterm" ? "longterm_run" : "pipeline") : null);

// Wall-clock instant of an ET date + time (DST-correct via the offset Intl reports for that date).
function etInstant(day, time) {
  const guess = new Date(`${day}T${String(time).slice(0, 5)}:00Z`);
  const off = new Intl.DateTimeFormat("en-US", { timeZone: NY, timeZoneName: "shortOffset" })
    .formatToParts(guess).find(p => p.type === "timeZoneName")?.value || "GMT-4";
  const h = Number((off.match(/GMT([+-]\d+)/) || [0, -4])[1]);
  return new Date(guess.getTime() - h * 3600e3);
}

// Status of one schedule row on one ET day, from dispatches + job runs. Anything scheduled
// before the system's first dispatch is BEFORE (not automated yet), never "missed".
function statusFor(s, day, dispatches, runs, isToday, now, since) {
  const disp = dispatches.find(d => d.name === s.name && d.run_date === day);
  if (!disp && since && etInstant(day, s.et_time) < since) return { status: "BEFORE", note: "before automation" };
  const job = jobOf(s);
  const jobRuns = job ? runs.filter(r => r.job === job && r.run_date === day) : [];
  // fill_sync runs three times: match the run that started after this row's dispatch.
  const run = disp && jobRuns.length
    ? jobRuns.filter(r => new Date(r.started_at) >= new Date(disp.dispatched_at)).sort((a, b) => a.started_at.localeCompare(b.started_at))[0]
    : null;
  if (run) return { status: run.status, run, disp };
  if (disp) {
    const age = (Date.now() - new Date(disp.dispatched_at)) / 60000;
    return { status: job && age > 20 ? "MISSED" : "DISPATCHED", disp, note: job && age > 20 ? "dispatched, never ran" : "" };
  }
  if (isToday && now.minutes < mins(s.et_time)) return { status: "LATER" };
  if (isToday && now.minutes < mins(s.et_time) + (s.grace_min || 45)) return { status: "LATER", note: "due now" };
  return { status: "MISSED", note: "not dispatched" };
}

export function AutomationView({ supabase }) {
  const session = useAdminSession();
  const [data, setData] = useState(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    if (!session) return;
    let live = true;
    (async () => {
      const since = new Date(Date.now() - 8 * 864e5).toISOString().slice(0, 10);
      const [s, d, r] = await Promise.all([
        supabase.from("automation_schedule").select("*").order("et_time"),
        supabase.from("automation_dispatches").select("*").gte("run_date", since),
        authClient.from("job_runs").select("*").gte("run_date", since).order("started_at", { ascending: false }).limit(300),
      ]);
      if (!live) return;
      if (s.error || d.error || r.error) { setMissing(true); return; }
      setData({ schedule: s.data || [], dispatches: d.data || [], runs: r.data || [] });
    })();
    return () => { live = false; };
  }, [supabase, session]);

  const days = useMemo(() => {
    const out = [];
    for (let i = 6; i >= 0; i--) out.push(etDate(new Date(Date.now() - i * 864e5)));
    return out;
  }, []);

  if (session === undefined) return null;
  if (!session) return <div className="ops-panel ops-note">Job results include account details, so they're for signed-in admins only. Sign in through RUN PIPELINE (top right).</div>;
  if (missing) return <div className="ops-panel ops-note">The automation tables aren't set up yet. Run <b>automation_schema.sql</b> in Supabase.</div>;
  if (!data) return <div className="ops-panel ops-note">// loading automation…</div>;

  const now = nowEt();
  const today = etDate(new Date());
  const todays = data.schedule.filter(s => s.enabled && s.days.includes(now.dow));
  const since = data.dispatches.length ? new Date(Math.min(...data.dispatches.map(d => +new Date(d.dispatched_at))) - 60e3) : new Date();
  const rows = todays.map(s => ({ s, ...statusFor(s, today, data.dispatches, data.runs, true, now, since) }));
  const bad = rows.filter(r => ["FAILED", "MISSED"].includes(r.status)).length;   // BEFORE never counts
  const warn = rows.filter(r => r.status === "WARN").length;
  const next = data.schedule.filter(s => s.enabled && s.days.includes(now.dow) && mins(s.et_time) > now.minutes)[0];

  return (
    <div>
      <div className="ops-panel">
        <div className="ops-head">
          <div>
            <div className="ops-title">AUTOMATION · TODAY (ET) · RUNS IN GITHUB ACTIONS, ON TIME FROM SUPABASE</div>
            <div className="ops-big">
              {todays.length === 0 ? "Nothing scheduled today"
                : bad ? `${bad} job${bad === 1 ? "" : "s"} failed or missed`
                : warn ? `${warn} job${warn === 1 ? "" : "s"} need attention`
                : "All jobs on track"}
            </div>
          </div>
          <div className="ops-note">{next ? `next: ${next.name} at ${String(next.et_time).slice(0, 5)} ET` : "no more jobs today"}</div>
        </div>
        {todays.length > 0 && (
          <table className="ops-table">
            <thead><tr><th>ET</th><th>JOB</th><th>STATUS</th><th>STARTED → DONE</th><th>RESULT</th></tr></thead>
            <tbody>
              {rows.map(({ s, status, run, note }) => (
                <tr key={s.name}>
                  <td>{String(s.et_time).slice(0, 5)}</td>
                  <td style={{ color: "var(--text)" }}>{s.name.replace(/_/g, " ")}</td>
                  <td><span className={`ops-chip ops-${status}`}>{status === "LATER" ? (note || "later") : status === "BEFORE" ? "not yet automated" : status}</span></td>
                  <td>{run ? `${etTime(run.started_at)} → ${etTime(run.finished_at)}` : "—"}</td>
                  <td className="ops-sum">
                    {run?.summary || note || (s.workflow === "orblex_pipeline.yml" ? "pipeline: see the PIPELINE tab" : "")}
                    {run?.gh_run_url && <> · <a className="ops-link" href={run.gh_run_url} target="_blank" rel="noreferrer">log</a></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="ops-panel">
        <div className="ops-title" style={{ marginBottom: 10 }}>LAST 7 DAYS</div>
        <div className="ops-grid" role="table" aria-label="Job status by day">
          <div className="lbl" />
          {days.map(d => <div key={d} className="lbl" style={{ textAlign: "center" }}>{d.slice(5)}</div>)}
          {data.schedule.filter(s => s.enabled).map(s => (
            <FragmentRow key={s.name} s={s} days={days} data={data} today={today} now={now} since={since} />
          ))}
        </div>
        <div className="ops-note" style={{ marginTop: 10 }}>
          Green OK · amber needs attention · red failed or missed · blue running · grey skipped (market closed) or not scheduled.
          Emails go out for anything amber or red.
        </div>
      </div>
    </div>
  );
}

function FragmentRow({ s, days, data, today, now, since }) {
  return (
    <>
      <div className="lbl">{s.name.replace(/_/g, " ")}</div>
      {days.map(day => {
        const dow = new Date(`${day}T12:00:00`).getDay() || 7;
        if (!s.days.includes(dow)) return <div key={day} className="ops-dot-NONE">·</div>;
        const st = statusFor(s, day, data.dispatches, data.runs, day === today, now, since);
        const label = { OK: "✓", WARN: "!", FAILED: "✗", MISSED: "✗", SKIPPED: "–", RUNNING: "…", DISPATCHED: "…", LATER: "" }[st.status] ?? "";
        return <div key={day} className={`ops-dot-${["LATER", "BEFORE"].includes(st.status) ? "NONE" : st.status}`} title={`${day} ${s.name}: ${st.status}${st.run?.summary ? " · " + st.run.summary : ""}`}>{label}</div>;
      })}
    </>
  );
}

export function DailyReviewView() {
  const session = useAdminSession();
  const [reviews, setReviews] = useState(null);
  const [pick, setPick] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!session) return;
    authClient.from("daily_reviews").select("*").order("run_date", { ascending: false }).limit(30)
      .then(({ data, error }) => { if (error) setErr(error.message); else { setReviews(data || []); setPick((data || [])[0]?.run_date || null); } });
  }, [session]);

  if (session === undefined) return null;
  if (!session) return <div className="ops-panel ops-note">The daily review includes account values, so it's for signed-in admins only. Sign in through RUN PIPELINE (top right).</div>;
  if (err) return <div className="ops-panel ops-note">{/relation|schema cache/i.test(err) ? "Run automation_schema.sql in Supabase to enable daily reviews." : err}</div>;
  if (!reviews) return <div className="ops-panel ops-note">// loading reviews…</div>;
  if (!reviews.length) return <div className="ops-panel ops-note">No daily review yet. The first one is written at 4:35 PM ET by the eod_review job.</div>;

  const r = reviews.find(x => x.run_date === pick)?.review || reviews[0].review;
  const lat = r.latency_median_min || {};
  const fmtMin = v => v == null ? "—" : `${v} min`;
  return (
    <div>
      <div className="ops-panel">
        <div className="ops-head">
          <div>
            <div className="ops-title">DAILY REVIEW · {r.date}</div>
            <div className="ops-big">{r.biggest_issue === "none" ? "Clean day" : `Biggest issue: ${r.biggest_issue}`}</div>
          </div>
          <select className="ops-select" value={pick || ""} onChange={e => setPick(e.target.value)} aria-label="Review date">
            {reviews.map(x => <option key={x.run_date} value={x.run_date}>{x.run_date}</option>)}
          </select>
        </div>
        {(r.issues || []).map((i, k) => <div key={k} className="ops-issue">! {i}</div>)}
      </div>
      <div className="ops-cards">
        <div className="ops-card"><div className="k">ACCOUNT VALUE</div><div className="v">${Number(r.account?.total_value || 0).toFixed(2)}</div>
          <div className="ops-note">cash ${Number(r.account?.cash || 0).toFixed(2)} · {(r.account?.positions || []).length} positions</div></div>
        <div className="ops-card"><div className="k">PIPELINE → PLAN</div><div className="v">{r.pipeline?.report_at || "—"} → {r.plan?.created_at || "—"}</div>
          <div className="ops-note">ET · report must land before the plan</div></div>
        <div className="ops-card"><div className="k">PLAN → APPROVED</div><div className="v">{fmtMin(lat.plan_to_approve)}</div><div className="ops-note">median</div></div>
        <div className="ops-card"><div className="k">APPROVED → PLACED</div><div className="v">{fmtMin(lat.approve_to_place)}</div>
          <div className="ops-note">median · fill {fmtMin(lat.place_to_fill)}</div></div>
      </div>
      <div className="ops-panel">
        <div className="ops-title" style={{ marginBottom: 8 }}>ORDERS · {Object.entries(r.plan?.counts || {}).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"}</div>
        {(r.orders || []).length > 0 && (
          <table className="ops-table">
            <thead><tr><th>#</th><th>TICKER</th><th>INTENT</th><th>STATUS</th><th>LIMIT</th><th>STOP</th><th>FILL</th><th></th></tr></thead>
            <tbody>
              {r.orders.map(o => (
                <tr key={o.id}>
                  <td>{o.id}</td><td style={{ color: "var(--text)" }}>{o.ticker}</td><td>{o.intent}</td>
                  <td><span className={`ops-chip ops-${["FILLED", "PLACED"].includes(o.status) ? "OK" : o.status === "BLOCKED" ? "WARN" : "SKIPPED"}`}>{o.status}</span></td>
                  <td>{o.limit ?? "—"}</td><td>{o.stop ?? "—"}</td><td>{o.fill_price ?? "—"}</td>
                  <td>{o.placed_before_approval && <span className="ops-chip ops-WARN">placed before approval</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {Object.keys(r.plan?.blocked_reasons || {}).length > 0 && (
          <div className="ops-note" style={{ marginTop: 8 }}>blocked: {Object.entries(r.plan.blocked_reasons).map(([k, v]) => `${k} ×${v}`).join(", ")}</div>
        )}
      </div>
      <div className="ops-panel">
        <div className="ops-title" style={{ marginBottom: 8 }}>SAFETY</div>
        {(r.safety?.unprotected || []).length
          ? r.safety.unprotected.map(u => <div key={u.ticker} className="ops-issue">! {u.ticker}: {u.qty} sh without a covering stop (covered {u.covered})</div>)
          : <div className="ops-note">Every position held overnight had a stop at the close.</div>}
      </div>
      <div className="ops-panel">
        <div className="ops-title" style={{ marginBottom: 8 }}>EXECUTOR DRY RUN VS WHAT WAS PLACED</div>
        <div className="ops-pre">{r.dryrun_vs_actual || "—"}</div>
      </div>
    </div>
  );
}
