// LongTermView.jsx — the weekly long-term book for the MAIN account (EXECUTOR_DESIGN.md §2.1).
// Reads, as a signed-in admin: longterm_advice (written by build_longterm.py after the
//        Saturday run) and longterm_holdings (current weights).
// Advice only: nothing here places or prepares an order. Teal, not amber, so it never
// reads as part of the swing flow.

import { useState, useEffect, useMemo } from "react";
import { authClient, useAdminSession } from "./RunPipeline";

export const LONGTERM_CSS = `
  .lt { --lt: var(--teal); --lt-dim: var(--teal-dim); }
  .lt-head { background: var(--bg2); padding: 18px 20px; border-left: 3px solid var(--lt); }
  .lt-kicker { font-family: var(--mono); font-size: 10px; letter-spacing: .1em; color: var(--lt); font-weight: 700; }
  .lt-title { font-size: 20px; color: var(--text); margin: 6px 0 4px; font-weight: 500; }
  .lt-sub { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer); line-height: 1.6; }
  .lt-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
  .lt-chip { font-family: var(--mono); font-size: 11px; padding: 4px 9px; border-radius: 2px; border: 1px solid var(--border);
             color: var(--text); background: var(--bg3); cursor: pointer; }
  .lt-chip:hover { border-color: var(--lt); }
  .lt-act { display: inline-block; font-family: var(--mono); font-size: 10px; font-weight: 700; padding: 2px 7px;
            border-radius: 2px; letter-spacing: .04em; min-width: 46px; text-align: center; }
  .lt-NEW, .lt-ADD { background: var(--green-dim); color: var(--green); }
  .lt-TRIM { background: rgba(239,159,39,.14); color: var(--amber); }
  .lt-EXIT { background: var(--red-dim); color: var(--red); }
  .lt-HOLD { background: var(--blue-dim); color: var(--blue); }
  .lt-AVOID { background: rgba(255,255,255,.05); color: var(--text-dimmer); }
  .lt-panel { background: var(--bg2); padding: 18px 20px; margin-top: 1px; }
  .lt-row { display: grid; grid-template-columns: 70px 150px 60px 70px minmax(0, 1fr); gap: 14px; align-items: start;
            padding: 12px 0; border-top: 1px solid rgba(255,255,255,.04); }
  .lt-row:first-of-type { border-top: 0; }
  .lt-row.lt-focus { background: var(--lt-dim); margin: 0 -20px; padding: 12px 20px; }
  .lt-tk { font-family: var(--mono); font-weight: 700; font-size: 13px; color: var(--text); background: none; border: 0;
           padding: 0; cursor: pointer; text-align: left; }
  .lt-tk:hover { color: var(--lt); }
  .lt-sector { font-family: var(--mono); font-size: 9px; color: var(--text-dimmer); margin-top: 3px; letter-spacing: .04em; }
  .lt-w { font-family: var(--mono); font-size: 11px; color: var(--text-dim); }
  .lt-w b { color: var(--text); font-weight: 700; }
  .lt-bar { position: relative; height: 6px; background: rgba(255,255,255,.05); border-radius: 3px; margin-top: 6px; }
  .lt-bar-now { position: absolute; top: 0; height: 6px; border-radius: 3px; background: rgba(255,255,255,.22); }
  .lt-bar-tgt { position: absolute; top: -3px; width: 2px; height: 12px; background: var(--lt); }
  .lt-dots { font-size: 15px; letter-spacing: 3px; color: var(--lt); line-height: 1; }
  .lt-dots span { color: rgba(255,255,255,.15); }
  .lt-why { list-style: none; font-size: 12px; color: var(--text-dim); line-height: 1.55; }
  .lt-why li { margin-bottom: 3px; }
  .lt-src { font-family: var(--mono); font-size: 9px; letter-spacing: .05em; color: var(--lt); margin-right: 6px;
            text-transform: uppercase; }
  .lt-wrong { font-size: 11px; color: var(--text-dimmer); margin-top: 4px; }
  .lt-wrong b { color: var(--text-dim); font-weight: 500; }
  .lt-changes { font-family: var(--mono); font-size: 10px; color: var(--amber); margin-top: 4px; }
  .lt-btn { font-family: var(--mono); font-size: 10px; letter-spacing: .06em; padding: 6px 12px; border-radius: 2px;
            border: 1px solid var(--border); background: var(--bg3); color: var(--text-dim); cursor: pointer; }
  .lt-btn:hover { color: var(--text); border-color: var(--lt); }
  .lt-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
  .lt-label { font-family: var(--mono); font-size: 10px; letter-spacing: .1em; color: var(--text-dimmer); }
  .lt-toggle { display: flex; gap: 4px; }
  .lt-toggle button { font-family: var(--mono); font-size: 10px; padding: 4px 9px; border-radius: 2px; border: 1px solid var(--border);
                      color: var(--text-dimmer); background: transparent; cursor: pointer; }
  .lt-toggle button.on { color: var(--lt); border-color: var(--lt); background: var(--lt-dim); }
  .lt-note { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer); line-height: 1.6; }
  .lt-empty { background: var(--bg2); padding: 40px 20px; text-align: center; font-family: var(--mono); font-size: 12px;
              color: var(--text-dim); line-height: 1.8; }
  .lt-chg-list { list-style: none; font-size: 12px; color: var(--text-dim); line-height: 1.9; }
  .lt-chg-list b { font-family: var(--mono); color: var(--text); margin-right: 8px; }
  @media (max-width: 760px) {
    .lt-row { grid-template-columns: 1fr 1fr; gap: 8px 12px; }
    .lt-row > .lt-cell-why { grid-column: 1 / -1; }
    .lt-row.lt-focus { margin: 0 -14px; padding: 12px 14px; }
    .lt-head, .lt-panel { padding: 14px; }
  }
`;

const ACTIONABLE = new Set(["NEW", "ADD", "TRIM", "EXIT"]);
const ORDER = { NEW: 0, ADD: 1, TRIM: 2, EXIT: 3, HOLD: 4, AVOID: 5 };
const pct = v => v == null ? "—" : `${Number(v).toFixed(1)}%`;
const signed = v => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%`;
const fmtDate = d => d ? new Date(`${d}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";

function Dots({ n }) {
  if (!n) return <span className="lt-dots" aria-label="no conviction score">—</span>;
  return (
    <span className="lt-dots" aria-label={`conviction ${n} of 3`}>
      {"●".repeat(n)}<span>{"●".repeat(3 - n)}</span>
    </span>
  );
}

function WeightBar({ now, target, max }) {
  const scale = v => `${Math.min(100, (Number(v || 0) / (max || 1)) * 100)}%`;
  return (
    <div className="lt-bar" aria-hidden="true">
      {now != null && <div className="lt-bar-now" style={{ width: scale(now) }} />}
      <div className="lt-bar-tgt" style={{ left: `calc(${scale(target)} - 1px)` }} />
    </div>
  );
}

function rebalanceText(rows, now, week) {
  const lines = rows.filter(r => ACTIONABLE.has(r.action)).map(r => {
    const cur = now ? now[r.ticker] ?? 0 : null;
    const from = cur != null ? ` (now ${pct(cur)})` : "";
    if (r.action === "EXIT") return `EXIT ${r.ticker}: sell to 0%${from}`;
    return `${r.action} ${r.ticker}: to ${pct(r.target_pct)}${from}`;
  });
  return `Long-term rebalance, week of ${week} (advice only):\n` + (lines.length ? lines.join("\n") : "No changes.");
}

export default function LongTermView({ onTicker }) {
  const session = useAdminSession();
  const [rows, setRows] = useState(null);         // null = loading / table missing
  const [now, setNow] = useState(null);           // {ticker: weight} when signed in as admin
  const [snapDate, setSnapDate] = useState(null);
  const [show, setShow] = useState("ACTIONS");    // ACTIONS | ALL
  const [copied, setCopied] = useState(false);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    if (!session) return;
    let live = true;
    (async () => {
      const latest = await authClient.from("longterm_advice").select("run_id,week_of").order("week_of", { ascending: false })
        .order("created_at", { ascending: false }).limit(1);
      if (!live) return;
      if (latest.error) { setMissing(true); setRows([]); return; }
      if (!latest.data?.length) { setRows([]); return; }
      const r = await authClient.from("longterm_advice").select("*").eq("run_id", latest.data[0].run_id);
      if (live) setRows(r.data || []);
    })();
    return () => { live = false; };
  }, [session]);

  useEffect(() => {
    if (!session) return;
    let live = true;
    (async () => {
      const snap = await authClient.from("longterm_holdings").select("snapshot_date").order("snapshot_date", { ascending: false }).limit(1);
      if (!live || snap.error || !snap.data?.length) return;
      const d = snap.data[0].snapshot_date;
      const h = await authClient.from("longterm_holdings").select("ticker,weight_pct").eq("snapshot_date", d);
      if (!live || h.error) return;
      setNow(Object.fromEntries((h.data || []).map(x => [x.ticker, Number(x.weight_pct)])));
      setSnapDate(d);
    })();
    return () => { live = false; };
  }, [session]);

  const sorted = useMemo(() => (rows || []).slice().sort((a, b) =>
    (ORDER[a.action] ?? 9) - (ORDER[b.action] ?? 9) || Number(b.target_pct) - Number(a.target_pct)), [rows]);

  if (session === undefined) return null;
  if (!session) return <div className="lt lt-empty">This advice is measured against the main account's real weights, so it's for signed-in admins only. Sign in through RUN PIPELINE (top right).</div>;
  if (rows == null) return <div className="lt-empty">// loading long-term advice…</div>;
  if (rows.length === 0) return (
    <div className="lt lt-empty">
      {missing
        ? <>The long-term tables aren't set up yet. Run <b>longterm_schema.sql</b> in the Supabase SQL editor.</>
        : <>No long-term advice yet. It's built every Saturday at 9:00 ET from the weekly long-term run.<br />
           To build it now: GitHub Actions → <b>Orblex Industries — Daily Pipeline</b> → Run workflow → book: <b>longterm</b>.</>}
    </div>
  );

  const head = rows[0];
  const week = fmtDate(head.week_of);
  const actions = sorted.filter(r => ACTIONABLE.has(r.action));
  const visible = show === "ACTIONS" ? actions : sorted;
  const invested = rows.reduce((s, r) => s + Number(r.target_pct || 0), 0);
  const maxW = Math.max(...rows.map(r => Math.max(Number(r.target_pct || 0), now?.[r.ticker] || 0)), 1);
  const firstWeek = rows.every(r => r.prev_target_pct == null);
  const drift = now ? rows.reduce((s, r) => s + Math.abs(Number(r.target_pct || 0) - (now[r.ticker] || 0)), 0) / 2 : null;
  const changed = sorted.filter(r => (r.changes || []).some(c => c !== "new this week"));
  const basisNote = now
    ? `Actions compare with your main account's weights on ${fmtDate(snapDate)}.`
    : head.basis === "HOLDINGS"
      ? "Actions compare with your main account's weights. Sign in through RUN PIPELINE to see them here."
      : "Actions compare with last week's targets: no snapshot of your main account yet.";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(rebalanceText(sorted, now, week));
      setCopied(true); setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard blocked */ }
  };

  return (
    <div className="lt">
      <div className="lt-head">
        <div className="lt-kicker">LONG TERM · MAIN ACCOUNT · ADVICE ONLY</div>
        <div className="lt-title">
          {actions.length === 0 ? "No changes this week" : `${actions.length} change${actions.length === 1 ? "" : "s"} suggested`}
          {drift != null && <span style={{ color: "var(--text-dim)", fontSize: 14 }}> · {drift.toFixed(1)}% of the account drifted from target</span>}
        </div>
        <div className="lt-sub">
          Week of {week} · regime {head.regime || "—"} · {pct(invested)} invested, {pct(head.cash_pct)} cash reserve
          {firstWeek && " · first week: nothing to compare with yet"}
        </div>
        {actions.length > 0 && (
          <div className="lt-chips">
            {actions.map(r => {
              const cur = now ? now[r.ticker] ?? 0 : r.prev_target_pct != null ? Number(r.prev_target_pct) : 0;
              const delta = Number(r.target_pct) - cur;
              return (
                <button key={r.ticker} className="lt-chip" onClick={() => document.getElementById(`lt-${r.ticker}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}>
                  <span className={`lt-act lt-${r.action}`}>{r.action}</span> {r.ticker} {r.action !== "EXIT" && signed(delta)}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="lt-panel">
        <div className="lt-toolbar">
          <div className="lt-toggle" role="group" aria-label="Which holdings to show">
            <button className={show === "ACTIONS" ? "on" : ""} aria-pressed={show === "ACTIONS"} onClick={() => setShow("ACTIONS")}>CHANGES {actions.length}</button>
            <button className={show === "ALL" ? "on" : ""} aria-pressed={show === "ALL"} onClick={() => setShow("ALL")}>ALL {rows.length}</button>
          </div>
          <button className="lt-btn" onClick={copy}>{copied ? "COPIED" : "COPY REBALANCE LIST"}</button>
        </div>
        <div className="lt-note" style={{ marginBottom: 6 }}>{basisNote} Bar: grey = now, teal tick = target.</div>
        {visible.length === 0 && <div className="lt-note" style={{ padding: "16px 0" }}>Nothing to change this week. Switch to ALL to see every holding.</div>}
        {visible.map(r => {
          const cur = now ? now[r.ticker] ?? 0 : null;
          const changes = (r.changes || []).filter(c => !(firstWeek && c === "new this week"));
          return (
            <div className="lt-row" key={r.ticker} id={`lt-${r.ticker}`}>
              <div>
                {onTicker ? <button className="lt-tk" onClick={() => onTicker(r.ticker)}>{r.ticker}</button>
                          : <span className="lt-tk" style={{ cursor: "default" }}>{r.ticker}</span>}
                <div className="lt-sector">{(r.sector || "").replace(/_/g, " ")}</div>
              </div>
              <div>
                <div className="lt-w">{cur != null ? <>now <b>{pct(cur)}</b> → </> : null}target <b>{pct(r.target_pct)}</b></div>
                <WeightBar now={cur} target={r.target_pct} max={maxW} />
              </div>
              <div><span className={`lt-act lt-${r.action}`}>{r.action}</span></div>
              <div><Dots n={r.conviction} /></div>
              <div className="lt-cell-why">
                <ul className="lt-why">
                  {(r.why || []).map((w, i) => <li key={i}><span className="lt-src">{w.source}</span>{w.text}</li>)}
                </ul>
                {r.wrong_if && <div className="lt-wrong"><b>Wrong if:</b> {r.wrong_if}</div>}
                {changes.length > 0 && <div className="lt-changes">since last week: {changes.join(" · ")}</div>}
              </div>
            </div>
          );
        })}
      </div>

      <div className="lt-panel">
        <div className="lt-label" style={{ marginBottom: 10 }}>WHAT CHANGED SINCE LAST WEEK</div>
        {firstWeek
          ? <div className="lt-note">This is the first week of advice. From next week, new names, action flips, target moves of 1%+ and conviction changes show up here.</div>
          : changed.length === 0
            ? <div className="lt-note">Nothing moved since last week.</div>
            : <ul className="lt-chg-list">{changed.map(r => <li key={r.ticker}><b>{r.ticker}</b>{r.changes.filter(c => c !== "new this week").join(" · ")}</li>)}</ul>}
      </div>

      <div className="lt-panel">
        <div className="lt-label" style={{ marginBottom: 10 }}>TRACK RECORD</div>
        <div className="lt-note">
          Builds once there are 4 weekly runs: the advised portfolio's weekly return against SPY, so the advice earns (or loses) trust over time.
        </div>
      </div>
    </div>
  );
}
