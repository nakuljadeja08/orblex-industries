// Playground.jsx — the demo's PLAYGROUND tab. A visitor makes an account, enters their own Claude
// key, shapes a made-up chart, and watches the real analyst and rules handle it, step by step.
//
// Accounts live in the playground's own Supabase project (playground_schema.sql), never the trading
// one. The key stays in this tab's memory: it is sent with each run to /api/playground
// (deploy/vercel-api/playground.py), used once, and never stored or shown again.
import { useCallback, useEffect, useMemo, useState } from "react";
import { createClient } from "@supabase/supabase-js";

const URL_ = import.meta.env.VITE_PLAYGROUND_SUPABASE_URL;
const ANON = import.meta.env.VITE_PLAYGROUND_SUPABASE_ANON_KEY;
const pg = URL_ && ANON
  ? createClient(URL_, ANON, { auth: { storageKey: "orblex-playground-auth", persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } })
  : null;

const RETURN_KEY = "orblex-playground-return";   // reopen this tab after the sign-in link
const store = {
  get: k => { try { return window.sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { window.sessionStorage.setItem(k, v); } catch { /* private mode */ } },
  del: k => { try { window.sessionStorage.removeItem(k); } catch { /* private mode */ } },
};
export const returningToPlayground = () => { const r = store.get(RETURN_KEY); store.del(RETURN_KEY); return !!r; };

// Same names and limits as playground.py's PRESETS and LIMITS.
const BASE = { ticker: "ACME", company: "Acme Robotics", sector: "Robotics", price: 62, trend_20d_pct: 8, move_5d_pct: -3,
               atr_pct: 2.5, volume_ratio: 1, days_to_earnings: "", regime: "BULL", equity: 5000, headlines: "" };
const PRESETS = {
  "Clean pullback":     { trend_20d_pct: 10, move_5d_pct: -4, atr_pct: 2.2, volume_ratio: 0.8, days_to_earnings: "", regime: "BULL" },
  "Extended breakout":  { trend_20d_pct: 22, move_5d_pct: 9, atr_pct: 3.5, volume_ratio: 2.4, days_to_earnings: "", regime: "BULL" },
  "Earnings next week": { trend_20d_pct: 9, move_5d_pct: -3, atr_pct: 2.6, volume_ratio: 1, days_to_earnings: 4, regime: "BULL" },
  "Breakdown":          { trend_20d_pct: -12, move_5d_pct: -7, atr_pct: 4, volume_ratio: 2, days_to_earnings: "", regime: "BULL" },
  "Bear market":        { trend_20d_pct: 6, move_5d_pct: -3, atr_pct: 3, volume_ratio: 1, days_to_earnings: "", regime: "BEAR" },
};
const FIELDS = [
  ["price", "Last close $", 1, 2000, 0.01], ["trend_20d_pct", "20-day change %", -30, 30, 0.5],
  ["move_5d_pct", "Last 5 days %", -15, 15, 0.5], ["atr_pct", "Daily range % (ATR)", 0.5, 10, 0.1],
  ["volume_ratio", "Last volume vs average ×", 0.3, 5, 0.1], ["days_to_earnings", "Earnings in (sessions)", 0, 60, 1],
  ["equity", "Paper account $", 100, 100000, 100],
];
const WHY = {
  MAX_ENTRIES_TODAY: "the 2 new entries a day are taken", RISK_SCORE_EXCLUDED: "a risk score of 8 or more excludes the ticker",
  RISK_LEVEL_CRITICAL: "a bear market sets risk to CRITICAL, which blocks every new entry", TOO_SMALL: "the risk budget can't buy one whole share",
  POSITION_TOO_LARGE: "the position would be over 30% of the account", OPEN_RISK_LIMIT: "open risk would pass 5% of the account",
  BUYING_POWER: "not enough cash", BAD_LEVELS: "the stop isn't below the entry", STALE_PLAN: "the plan is too old",
};
const money = v => v == null ? "—" : `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

function Spark({ bars, entry, stop, target }) {
  if (!bars?.length) return null;
  const W = 520, H = 150, P = 14;   // P leaves room above the top line for its label
  const ys = bars.map(b => b.close).concat([entry, stop, target].filter(v => v != null));
  const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const x = i => P + (i / (bars.length - 1)) * (W - 2 * P);
  const y = v => P + (1 - (v - lo) / span) * (H - 2 * P);
  const line = bars.map((b, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(b.close).toFixed(1)}`).join(" ");
  const level = (v, cls, label) => v == null ? null : (
    <g key={label}><line x1={P} x2={W - P} y1={y(v)} y2={y(v)} className={cls} /><text x={P} y={y(v) - 3} className="pg-lvl-t">{label} {v}</text></g>);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="pg-spark" role="img"
         aria-label={`Last ${bars.length} closes, from ${bars[0].close} to ${bars[bars.length - 1].close}`}>
      <path d={line} className="pg-spark-line" />
      {level(target, "pg-lvl pg-lvl-g", "target")}{level(entry, "pg-lvl pg-lvl-a", "entry")}{level(stop, "pg-lvl pg-lvl-r", "stop")}
    </svg>
  );
}

function Account({ session, onReturnHere }) {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  if (!pg) return <div className="pg-note">Accounts aren't set up on this copy of the demo, so runs are off. See playground_schema.sql.</div>;
  if (session) return (
    <div className="pg-row">
      <span>Signed in as <b>{session.user?.email}</b></span>
      <button className="pg-link" onClick={() => pg.auth.signOut()}>Sign out</button>
      {confirmDelete
        ? <span className="pg-warn">Delete your account and all your saved runs?{" "}
            <button className="pg-link pg-danger" onClick={async () => { await pg.rpc("delete_my_account"); await pg.auth.signOut(); }}>Delete</button>{" "}
            <button className="pg-link" onClick={() => setConfirmDelete(false)}>Keep it</button></span>
        : <button className="pg-link" onClick={() => setConfirmDelete(true)}>Delete my account</button>}
    </div>
  );
  if (sent) return <div className="pg-note">Check <b>{email}</b> for a sign-in link, and open it in this browser.</div>;
  const send = async e => {
    e.preventDefault();
    setBusy(true); setErr(null);
    onReturnHere();
    const { error } = await pg.auth.signInWithOtp({ email: email.trim(), options: { shouldCreateUser: true, emailRedirectTo: window.location.origin } });
    setBusy(false);
    if (error) setErr(error.message); else setSent(true);
  };
  return (
    <form className="pg-row" onSubmit={send}>
      <label className="pg-label" htmlFor="pg-email">Create an account or sign in</label>
      <input id="pg-email" className="pg-input" type="email" required autoComplete="email" placeholder="you@example.com"
             value={email} onChange={e => setEmail(e.target.value)} />
      <button className="pg-btn" type="submit" disabled={busy || !email.trim()}>{busy ? "Sending…" : "Email me a link"}</button>
      {err && <span className="pg-err">{err}</span>}
    </form>
  );
}

function Trace({ r }) {
  const a = r.analyst || {}, o = r.order, k = r.risk || {}, i = r.indicators || {};
  const sigCls = { BUY: "pg-buy", SELL: "pg-sell" }[a.signal] || "pg-hold";
  return (
    <div className="pg-trace">
      <section className="pg-step">
        <h4><span>1</span> The chart you made</h4>
        <Spark bars={r.bars} entry={o?.limit_price} stop={o?.stop_price} target={o?.target_price} />
        <div className="pg-chips">
          {[["trend", i.trend], ["RSI", i.rsi_14], ["ATR", i.atr_pct != null && `${i.atr_pct}%`], ["vs MA20", i.price_vs_ma20_pct != null && `${i.price_vs_ma20_pct}%`],
            ["5d", i.ret_5d_pct != null && `${i.ret_5d_pct}%`], ["20d", i.ret_20d_pct != null && `${i.ret_20d_pct}%`], ["regime", r.scenario?.regime]]
            .filter(([, v]) => v != null && v !== false).map(([n, v]) => <span key={n} className="pg-chip">{n} <b>{v}</b></span>)}
        </div>
      </section>
      <section className="pg-step">
        <h4><span>2</span> Claude's call</h4>
        <div className="pg-row"><span className={`pg-sig ${sigCls}`}>{a.signal}</span><span>{a.signal_strength} · {a.setup_type}</span>
          {a.entry_price != null && <span className="pg-mono">entry {a.entry_price} · stop {a.stop_price} · target {a.target_price} · {a.horizon_days} sessions · R:R {a.risk_reward}</span>}</div>
        {a.summary && <p>{a.summary}</p>}
        {a.reasoning && <details><summary>Full reasoning</summary><p>{a.reasoning}</p>{a.invalidation && <p><b>Wrong if:</b> {a.invalidation}</p>}</details>}
      </section>
      <section className="pg-step">
        <h4><span>3</span> Swing rules</h4>
        {(a.swing_notes || []).length
          ? <ul>{a.swing_notes.map((n, j) => <li key={j}>{n}</li>)}</ul>
          : <p>No corrections: the levels, horizon and earnings timing passed as Claude set them.</p>}
        {a.model_signal && a.model_signal !== a.signal && <p className="pg-warn">Claude said {a.model_signal}; the rules made it {a.signal}.</p>}
      </section>
      <section className="pg-step">
        <h4><span>4</span> Risk rules</h4>
        <p>Score <b>{k.risk_score}/10</b>, position cap <b>{k.max_position_pct}%</b>{(k.risk_flags || []).length ? <>, flags {k.risk_flags.join(", ")}</> : null}. {k.notes}</p>
      </section>
      <section className="pg-step">
        <h4><span>5</span> Sizing and the trade guard</h4>
        {!o
          ? <p>No order: only a BUY with levels becomes an entry. A {a.signal} keeps the paper account flat.</p>
          : o.status === "PROPOSED"
            ? <p><span className="pg-sig pg-buy">PROPOSED</span> Buy <b>{o.qty}</b> shares at a {money(o.limit_price)} limit, risking {money(o.risk_dollars)} on a {money(r.equity)} account. It would now wait for a person to approve it.</p>
            : <p><span className="pg-sig pg-sell">BLOCKED</span> {(o.guard_reasons || []).map(g => WHY[g] || g).join("; ")}.</p>}
      </section>
      <div className="pg-foot">{a.model} · {a.prompt_tokens} tokens in, {a.output_tokens} out, on your key</div>
    </div>
  );
}

export default function Playground() {
  const [session, setSession] = useState(undefined);
  const [key, setKey] = useState("");
  const [s, setS] = useState({ ...BASE, ...PRESETS["Clean pullback"] });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);

  useEffect(() => {
    if (!pg) { setSession(null); return; }
    pg.auth.getSession().then(({ data }) => setSession(data.session));
    const { data } = pg.auth.onAuthStateChange((_e, x) => setSession(x));
    return () => data.subscription.unsubscribe();
  }, []);
  const loadHistory = useCallback(async () => {
    if (!pg || !session) { setHistory([]); return; }
    const { data } = await pg.from("playground_runs").select("id,created_at,scenario,result").order("created_at", { ascending: false }).limit(10);
    setHistory(data || []);
  }, [session]);
  useEffect(() => { loadHistory(); }, [loadHistory]);

  const set = (k, v) => setS(x => ({ ...x, [k]: v }));
  const scenario = useMemo(() => ({ ...s, headlines: s.headlines.split("\n").map(h => h.trim()).filter(Boolean).slice(0, 3) }), [s]);

  const runIt = async () => {
    setBusy(true); setErr(null);
    try {
      const resp = await fetch("/api/playground", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.access_token}`, "X-Claude-Key": key.trim() },
        body: JSON.stringify(scenario),
      });
      const text = await resp.text();
      let data;
      try { data = JSON.parse(text); } catch { throw new Error("The playground server isn't reachable here. It runs on the deployed demo site."); }
      if (!resp.ok) throw new Error(data.error || `The run failed (${resp.status}).`);
      setResult(data.result);
      loadHistory();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const ready = session && key.trim().startsWith("sk-ant-");
  return (
    <div className="pg">
      <div className="pg-intro">
        <h2>Try the real analyst on a chart you make</h2>
        <p>Shape a made-up stock, then run it. Your own Claude key calls the same analyst prompt and model the live system uses, and the same Python rules then check, size and guard the trade. Each run costs a few cents of your Claude usage. Nothing here touches a real account.</p>
      </div>

      <section className="pg-card"><h3>Account</h3><Account session={session} onReturnHere={() => store.set(RETURN_KEY, "1")} /></section>

      <section className="pg-card">
        <h3>Your Claude API key</h3>
        <div className="pg-row">
          <input id="pg-key" className="pg-input pg-key" type="password" autoComplete="off" spellCheck={false} placeholder="sk-ant-…"
                 value={key} onChange={e => setKey(e.target.value)} aria-describedby="pg-key-note" />
        </div>
        <p id="pg-key-note" className="pg-note">Kept only in this tab, and gone when you close it. It's sent with each run, used once, and never saved or shown back. Create one in the Claude Console.</p>
      </section>

      <section className="pg-card">
        <h3>Your scenario</h3>
        <div className="pg-chips">{Object.keys(PRESETS).map(p => <button key={p} className="pg-chip pg-chip-btn" onClick={() => setS(x => ({ ...x, ...PRESETS[p] }))}>{p}</button>)}</div>
        <div className="pg-grid">
          <label className="pg-label">Ticker<input className="pg-input" value={s.ticker} maxLength={5} onChange={e => set("ticker", e.target.value.toUpperCase())} /></label>
          <label className="pg-label">Company<input className="pg-input" value={s.company} maxLength={60} onChange={e => set("company", e.target.value)} /></label>
          {FIELDS.map(([k, label, min, max, step]) => (
            <label key={k} className="pg-label">{label}
              <input className="pg-input" type="number" min={min} max={max} step={step} value={s[k]} placeholder={k === "days_to_earnings" ? "none" : ""}
                     onChange={e => set(k, e.target.value)} /></label>
          ))}
          <label className="pg-label">Market regime
            <select className="pg-input" value={s.regime} onChange={e => set("regime", e.target.value)}>
              {["BULL", "CHOPPY", "CORRECTION", "BEAR"].map(x => <option key={x}>{x}</option>)}
            </select></label>
        </div>
        <label className="pg-label">Headlines, one per line (up to 3)
          <textarea className="pg-input" rows={3} maxLength={620} value={s.headlines} placeholder="Acme Robotics wins a three-year warehouse contract"
                    onChange={e => set("headlines", e.target.value)} /></label>
        <div className="pg-row">
          <button className="pg-btn" onClick={runIt} disabled={!ready || busy}>{busy ? "Running… about 10 seconds" : "Run it"}</button>
          {!session && <span className="pg-note">Sign in to run.</span>}
          {session && !ready && <span className="pg-note">Enter your Claude key to run.</span>}
        </div>
        {err && <div className="pg-err" role="alert">{err}</div>}
      </section>

      {result && <section className="pg-card" aria-live="polite"><h3>One trade's path</h3><Trace r={result} /></section>}

      {history.length > 0 && (
        <section className="pg-card">
          <h3>Your recent runs</h3>
          <ul className="pg-history">
            {history.map(h => (
              <li key={h.id}>
                <button className="pg-link" onClick={() => setResult(h.result)}>
                  {new Date(h.created_at).toLocaleString()} · {h.scenario?.ticker} · {h.result?.analyst?.signal} · {h.result?.order?.status || "no order"}
                </button>
                <button className="pg-link pg-danger" aria-label="Delete this run" onClick={async () => { await pg.from("playground_runs").delete().eq("id", h.id); loadHistory(); }}>delete</button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export const PLAYGROUND_CSS = `
  .pg{max-width:900px;display:flex;flex-direction:column;gap:16px;padding:16px 16px 40px;}
  .pg-intro h2{font-size:20px;font-weight:600;color:var(--text);margin-bottom:8px;text-wrap:balance;}
  .pg-intro p,.pg-step p,.pg-step li{font-size:13px;line-height:1.6;color:var(--text-dim);max-width:72ch;}
  .pg-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:14px 16px;display:flex;flex-direction:column;gap:10px;min-width:0;}
  .pg-card h3{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--amber);text-transform:uppercase;}
  .pg-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:13px;color:var(--text-dim);}
  .pg-row b,.pg-note b{color:var(--text);font-weight:500;}
  .pg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,170px),1fr));gap:10px;}
  .pg-label{display:flex;flex-direction:column;gap:4px;font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--text-dimmer);text-transform:uppercase;}
  .pg-input{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:7px 9px;color:var(--text);font-family:var(--mono);font-size:12px;text-transform:none;letter-spacing:0;width:100%;}
  .pg-input:focus{outline:none;border-color:var(--border-amber);}
  .pg-key{max-width:420px;}
  .pg-btn{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.06em;padding:9px 16px;border-radius:4px;background:var(--amber);color:#111;}
  .pg-btn:disabled{opacity:.45;cursor:not-allowed;}
  .pg-link{font-family:var(--mono);font-size:11px;color:var(--text-dim);text-decoration:underline;}
  .pg-link:hover{color:var(--text);}
  .pg-danger{color:var(--red);}
  .pg-note{font-size:12px;color:var(--text-dimmer);line-height:1.5;}
  .pg-err{font-family:var(--mono);font-size:11px;color:var(--red);background:var(--red-dim);border:1px solid rgba(226,75,74,.3);border-radius:4px;padding:8px 10px;}
  .pg-warn{font-size:12px;color:var(--amber);}
  .pg-chips{display:flex;flex-wrap:wrap;gap:6px;}
  .pg-chip{font-family:var(--mono);font-size:11px;padding:3px 8px;border:1px solid var(--border);border-radius:3px;color:var(--text-dim);}
  .pg-chip b{color:var(--text);font-weight:600;}
  .pg-chip-btn:hover{border-color:var(--border-amber);color:var(--text);}
  .pg-trace{display:flex;flex-direction:column;gap:14px;}
  .pg-step{display:flex;flex-direction:column;gap:6px;border-left:2px solid var(--border);padding-left:12px;}
  .pg-step h4{font-size:13px;font-weight:600;color:var(--text);display:flex;gap:8px;align-items:center;}
  .pg-step h4 span{font-family:var(--mono);font-size:10px;color:var(--amber);border:1px solid var(--border-amber);border-radius:50%;width:18px;height:18px;display:inline-flex;align-items:center;justify-content:center;}
  .pg-step ul{padding-left:18px;}
  .pg-step details summary{font-size:12px;color:var(--text-dim);cursor:pointer;}
  .pg-sig{font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;margin-right:6px;}
  .pg-buy{background:var(--green-dim);color:var(--green);} .pg-sell{background:var(--red-dim);color:var(--red);} .pg-hold{background:var(--bg3);color:var(--text-dim);}
  .pg-mono{font-family:var(--mono);font-size:11px;}
  .pg-spark{width:100%;max-width:560px;height:auto;display:block;}
  .pg-spark-line{fill:none;stroke:var(--amber);stroke-width:1.6;}
  .pg-lvl{stroke-width:1;stroke-dasharray:4 3;} .pg-lvl-a{stroke:var(--text-dim);} .pg-lvl-r{stroke:var(--red);} .pg-lvl-g{stroke:var(--green);}
  .pg-lvl-t{fill:var(--text-dim);font-family:var(--mono);font-size:9px;}
  .pg-foot{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);}
  .pg-history{list-style:none;display:flex;flex-direction:column;gap:6px;padding:0;}
  .pg-history li{display:flex;gap:12px;flex-wrap:wrap;}
`;
