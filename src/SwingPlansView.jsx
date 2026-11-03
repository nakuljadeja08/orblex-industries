// SwingPlansView.jsx — swing trade plans (TRADING_MODE=swing)
// Reads: agent_reports (the latest run, passed in), swing_backtest_trades
//        (simulated outcomes), trade_orders + trading_controls. Approve / reject
//        and the kill switch call SECURITY DEFINER functions (approval_schema.sql)
//        through the signed-in admin session; nothing here places an order.

import { useState, useEffect, useCallback } from "react";
import { authClient, useAdminSession } from "./RunPipeline";

export const SWING_CSS = `
  .sw-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--border); }
  .sw-card  { background: var(--bg2); padding: 16px; }
  .sw-sub   { font-family: var(--mono); font-size: 10px; color: var(--text-dimmer); margin-top: 4px; }
  .sw-panel { background: var(--bg2); padding: 20px; margin-top: 1px; }
  .sw-table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
  .sw-table th { color: var(--text-dimmer); font-weight: 400; padding: 6px 10px; text-align: left;
                 border-bottom: 1px solid var(--border); font-size: 10px; letter-spacing: .05em; white-space: nowrap; }
  .sw-table td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,.03); color: var(--text-dim); vertical-align: top; }
  .sw-table tr:hover td { background: rgba(255,255,255,.02); }
  .sw-num   { text-align: right; white-space: nowrap; }
  .sw-tk    { color: var(--amber); font-weight: 700; cursor: pointer; background: none; border: 0; padding: 0;
              font-family: var(--mono); font-size: 12px; }
  .sw-tk:hover { text-decoration: underline; }
  .sw-chip  { display: inline-block; font-family: var(--mono); font-size: 10px; padding: 2px 7px; border-radius: 2px; font-weight: 700; }
  .sw-buy   { background: var(--green-dim); color: var(--green); }
  .sw-sell  { background: var(--red-dim); color: var(--red); }
  .sw-mute  { background: rgba(255,255,255,.05); color: var(--text-dim); }
  .sw-pos, .sw-table td.sw-pos { color: var(--green); }
  .sw-neg, .sw-table td.sw-neg { color: var(--red); }
  .sw-why   { color: var(--text-dimmer); font-size: 10px; margin-top: 3px; line-height: 1.5; max-width: 340px; }
  .sw-empty { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer); padding: 40px; text-align: center; background: var(--bg2); }
  .sw-note  { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer); line-height: 1.6; }
  .sw-orders-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
  .sw-actions { white-space: nowrap; text-align: right; }
  .sw-btn { font-family: var(--mono); font-size: 10px; letter-spacing: .06em; padding: 5px 10px; margin-left: 6px;
            background: var(--bg3); color: var(--text-dim); border: 1px solid var(--border); border-radius: 2px; cursor: pointer; }
  .sw-btn:hover:not(:disabled) { color: var(--text); border-color: var(--text-dimmer); }
  .sw-btn:disabled { opacity: .4; cursor: not-allowed; }
  .sw-btn-go   { color: var(--green); border-color: rgba(60,174,111,.4); }
  .sw-btn-stop { color: var(--red);   border-color: rgba(226,75,74,.4); }
  .sw-alert { font-family: var(--mono); font-size: 11px; color: var(--red); background: var(--red-dim);
              border: 1px solid rgba(226,75,74,.3); padding: 8px 10px; margin-bottom: 10px; }

  /* Price ladder: stop → target, with entry and last close marked */
  .sw-ladder { position: relative; height: 18px; min-width: 150px; }
  .sw-ladder-risk, .sw-ladder-reward { position: absolute; top: 7px; height: 4px; }
  .sw-ladder-risk   { background: rgba(226,75,74,.55); border-radius: 2px 0 0 2px; }
  .sw-ladder-reward { background: rgba(60,174,111,.55); border-radius: 0 2px 2px 0; }
  .sw-ladder-mark { position: absolute; top: 2px; width: 2px; height: 14px; background: var(--text); }
  .sw-ladder-last { position: absolute; top: 4px; width: 8px; height: 8px; margin-left: -4px; border-radius: 50%;
                    border: 2px solid var(--amber); background: var(--bg2); }

  /* Copy-ready tickets: one card per approved order, fields in the app's order */
  .sw-tickets { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .sw-ticket  { background: var(--bg3); border: 1px solid var(--border); border-radius: 2px; padding: 12px; font-family: var(--mono); }
  .sw-ticket-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
  .sw-ticket-title { font-size: 12px; color: var(--text); font-weight: 700; }
  .sw-ticket-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 5px 0; min-height: 32px; box-sizing: border-box;
                   border-top: 1px solid rgba(255,255,255,.04); font-size: 11px; }
  .sw-ticket-k { color: var(--text-dimmer); font-size: 10px; letter-spacing: .05em; }
  .sw-ticket-v { color: var(--text); font-weight: 700; margin-left: auto; }
  .sw-copy { font-family: var(--mono); font-size: 9px; letter-spacing: .06em; padding: 3px 7px; min-width: 52px;
             background: none; color: var(--text-dimmer); border: 1px solid var(--border); border-radius: 2px; cursor: pointer; }
  .sw-copy:hover { color: var(--text); border-color: var(--text-dimmer); }
  .sw-copy.done { color: var(--green); border-color: rgba(60,174,111,.4); }
  .sw-ticket-warn { font-size: 10px; color: var(--red); margin-bottom: 6px; line-height: 1.5; }

  /* Daily mandate: what the (future) executor may enter today */
  .sw-mandate { border: 1px solid var(--border); border-left: 3px solid var(--blue); background: var(--bg3);
                padding: 12px 14px; margin-bottom: 14px; font-family: var(--mono); font-size: 11px; color: var(--text-dim); }
  .sw-mandate.on { border-left-color: var(--green); }
  .sw-mandate-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
  .sw-mandate-title { color: var(--text); font-weight: 700; letter-spacing: .05em; }
  .sw-mandate-grid { display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: center; }
  .sw-mandate label { display: flex; align-items: center; gap: 6px; }
  .sw-mandate input[type=number], .sw-mandate select { font-family: var(--mono); font-size: 11px; background: var(--bg2);
                color: var(--text); border: 1px solid var(--border); border-radius: 2px; padding: 4px 6px; width: 76px; }
  .sw-mandate-tk { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; color: var(--text); }
  .sw-mandate-lock { color: var(--text-dimmer); }
  .sw-mandate-foot { margin-top: 8px; font-size: 10px; color: var(--text-dimmer); line-height: 1.6; }

  @media (max-width: 680px) {
    .sw-cards { grid-template-columns: 1fr 1fr; }
    .sw-tickets { grid-template-columns: 1fr; }
    .sw-panel { padding: 14px; overflow-x: auto; }
    .sw-orders-head { flex-wrap: wrap; }
  }
`;

const fmt   = (v, d = 2) => v == null ? "—" : Number(v).toFixed(d);
const pct   = (from, to) => from && to ? `${to >= from ? "+" : ""}${((to / from - 1) * 100).toFixed(1)}%` : "";
const rCls  = v => v == null ? "" : v > 0 ? "sw-pos" : v < 0 ? "sw-neg" : "";
const arr   = v => Array.isArray(v) ? v : typeof v === "string" && v.startsWith("{")
  ? v.slice(1, -1).split(",").map(s => s.replace(/^"|"$/g, "")) : [];
const RANK  = { STRONG: 0, MODERATE: 1, WEAK: 2 };
const CLOSED = new Set(["TARGET", "STOP", "TIME", "SIGNAL_EXIT"]);

function Ladder({ stop, entry, target, last }) {
  const lo = Math.min(stop, last ?? stop), hi = Math.max(target, last ?? target);
  const pos = v => `${((v - lo) / (hi - lo || 1)) * 100}%`;
  return (
    <div className="sw-ladder" role="img"
         aria-label={`stop ${fmt(stop)}, entry ${fmt(entry)}, target ${fmt(target)}${last ? `, last close ${fmt(last)}` : ""}`}>
      <div className="sw-ladder-risk"   style={{ left: pos(stop),  width: `calc(${pos(entry)} - ${pos(stop)})` }} />
      <div className="sw-ladder-reward" style={{ left: pos(entry), width: `calc(${pos(target)} - ${pos(entry)})` }} />
      <div className="sw-ladder-mark"   style={{ left: pos(entry) }} title={`entry ${fmt(entry)}`} />
      {last != null && <div className="sw-ladder-last" style={{ left: pos(last) }} title={`last close ${fmt(last)}`} />}
    </div>
  );
}

// The exact order to enter in the Robinhood app, worded the way the app words it.
function ticket(o) {
  const qty = `${Number(o.qty)} ${o.ticker}`;
  const tif = o.time_in_force === "GTC" ? "Good till canceled" : "Good for day";
  if (o.order_type === "LIMIT") return `${o.side === "BUY" ? "Buy" : "Sell"} ${qty} · Limit order · limit $${px(o.limit_price)} · ${tif}`;
  if (o.order_type === "STOP")  return `Sell ${qty} · Stop loss order · stop $${px(o.stop_price)} · ${tif}`;
  return `${o.side === "BUY" ? "Buy" : "Sell"} ${qty} · Market order · ${tif}`;
}

// Robinhood takes 2 decimals at or above $1 and up to 4 below it. The fill sync
// matches on the exact price, so the copied value must be exactly the plan's.
const px = v => v == null ? "—" : Number(v) >= 1 ? Number(v).toFixed(2) : String(Number(Number(v).toFixed(4)));

// The ticket as the app's fields, top to bottom: [label, value, copyable].
function ticketFields(o) {
  const stop = o.order_type === "STOP";
  return [
    ["SYMBOL",     o.ticker, true],
    ["ACTION",     o.side === "BUY" ? "Buy" : "Sell", false],
    ["ORDER TYPE", o.order_type === "LIMIT" ? "Limit order" : stop ? "Stop loss order" : "Market order", false],
    ...(o.order_type === "LIMIT" ? [["LIMIT PRICE", px(o.limit_price), true]] : []),
    ...(stop ? [["STOP PRICE", px(o.stop_price), true]] : []),
    ["SHARES",     String(Number(o.qty)), true],
    ["EXPIRES",    o.time_in_force === "GTC" ? "Good till canceled" : "Good for day", false],
  ];
}

function TicketCard({ o, copied, onCopy }) {
  const fields = ticketFields(o);
  const key = f => `${o.id}:${f}`;
  return (
    <div className="sw-ticket">
      <div className="sw-ticket-head">
        <span className="sw-ticket-title">
          <span className={`sw-chip ${o.side === "BUY" ? "sw-buy" : "sw-sell"}`}>{o.side}</span> {o.ticker}
          <span className="sw-ticket-k"> · #{o.id} · {o.intent}</span>
        </span>
        <button className={`sw-copy ${copied === key("all") ? "done" : ""}`} onClick={() => onCopy(key("all"), ticket(o))}>
          {copied === key("all") ? "COPIED" : "COPY ALL"}
        </button>
      </div>
      {arr(o.cancel_first).length > 0 && (
        <div className="sw-ticket-warn">First cancel the open stop order on {o.ticker} in the app, then place this.</div>
      )}
      {fields.map(([k, v, canCopy]) => (
        <div key={k} className="sw-ticket-row">
          <span className="sw-ticket-k">{k}</span>
          <span className="sw-ticket-v">{v}</span>
          {canCopy
            ? <button className={`sw-copy ${copied === key(k) ? "done" : ""}`} onClick={() => onCopy(key(k), v)}>{copied === key(k) ? "COPIED" : "COPY"}</button>
            : <span style={{ minWidth: 52 }} />}
        </div>
      ))}
    </div>
  );
}

const STATUS_CLS ={ APPROVED: "sw-pos", FILLED: "sw-pos", PLACED: "sw-pos", PARTIAL: "sw-pos",
                     BLOCKED: "sw-neg", REJECTED: "sw-neg", CANCELLED: "", EXPIRED: "" };

// The daily mandate (exec_limits.py layer 2): which tickers, how many entries and how much
// total risk the executor may use today. The database clamps it to the code ceilings.
// Without one, the executor only manages exits. The manual flow below is unaffected.
function MandateCard({ orders, session }) {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const [m, setM] = useState(undefined);              // undefined = loading, null = none / table missing
  const [missing, setMissing] = useState(false);
  const [picked, setPicked] = useState(null);
  const [maxEntries, setMaxEntries] = useState(2);
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    const r = await authClient.from("trading_mandates").select("*").eq("mandate_date", today).limit(1);
    if (r.error) { setMissing(true); setM(null); return; }
    setM((r.data || [])[0] || null);
  }, [today]);
  useEffect(() => { load(); }, [load]);

  const candidates = (orders || []).filter(o => o.intent === "ENTRY" && o.plan_date === today
    && ["PROPOSED", "APPROVED", "PLACED", "PARTIAL", "FILLED"].includes(o.status));
  const sel = picked ?? new Set(candidates.map(o => o.ticker));
  const riskOf = set => candidates.filter(o => set.has(o.ticker)).reduce((s, o) => s + Number(o.risk_dollars || 0), 0);
  const budgetVal = budget === "" ? Math.ceil(riskOf(sel)) : Number(budget);

  if (m === undefined) return null;
  if (missing) return <div className="sw-mandate"><span className="sw-mandate-title">DAILY MANDATE</span> · run <b>executor_schema.sql</b> in Supabase to enable it.</div>;

  const live = m && m.status === "ACTIVE" && new Date(m.expires_at) > new Date();
  const toggle = t => { const n = new Set(sel); n.has(t) ? n.delete(t) : n.add(t); setPicked(n); setBudget(""); };

  const approve = async () => {
    const tickers = [...sel];
    if (!window.confirm(`Approve today's mandate?

Tickers: ${tickers.join(", ") || "none"}
Max entries: ${maxEntries}
Total risk: $${budgetVal}

Limit orders only, stop required, no same-day re-buys. Expires 4:00 PM ET.`)) return;
    setBusy(true); setErr(null);
    const { error } = await authClient.rpc("set_trading_mandate", { p_max_entries: maxEntries, p_max_total_risk_usd: budgetVal, p_tickers: tickers });
    if (error) setErr(error.message);
    setBusy(false); await load();
  };
  const revoke = async () => {
    if (!window.confirm("Revoke today's mandate? The executor will manage exits only.")) return;
    setBusy(true); setErr(null);
    const { error } = await authClient.rpc("revoke_trading_mandate");
    if (error) setErr(error.message);
    setBusy(false); await load();
  };

  return (
    <div className={`sw-mandate ${live ? "on" : ""}`}>
      <div className="sw-mandate-head">
        <span className="sw-mandate-title">DAILY MANDATE · {live ? "ACTIVE UNTIL 4:00 PM ET" : m?.status === "REVOKED" ? "REVOKED" : "NOT SET"}</span>
        {live
          ? <button className="sw-btn sw-btn-stop" disabled={!session || busy} onClick={revoke}>REVOKE</button>
          : <button className="sw-btn sw-btn-go" disabled={!session || busy || sel.size === 0} onClick={approve}
                    title={session ? "" : "Sign in through RUN PIPELINE first"}>APPROVE MANDATE</button>}
      </div>
      {live ? (
        <div className="sw-mandate-grid">
          <span>tickers <b style={{ color: "var(--text)" }}>{(m.allowed_tickers || []).join(", ") || "none"}</b></span>
          <span>max entries <b style={{ color: "var(--text)" }}>{m.max_entries}</b></span>
          <span>total risk <b style={{ color: "var(--text)" }}>${Number(m.max_total_risk_usd).toFixed(0)}</b></span>
          <span className="sw-mandate-lock">limit orders · stop required · no same-day re-buys</span>
          {m.approved_by && <span className="sw-mandate-lock">approved by {m.approved_by}</span>}
        </div>
      ) : candidates.length === 0 ? (
        <div>No entries in today's plan to authorise. Without a mandate the executor manages exits only.</div>
      ) : (
        <div className="sw-mandate-grid">
          <span>
            {candidates.map(o => (
              <label key={o.id} className="sw-mandate-tk">
                <input type="checkbox" checked={sel.has(o.ticker)} onChange={() => toggle(o.ticker)} />
                {o.ticker} <span className="sw-mandate-lock">${Number(o.risk_dollars || 0).toFixed(0)}</span>
              </label>
            ))}
          </span>
          <label>max entries
            <select value={maxEntries} onChange={e => setMaxEntries(Number(e.target.value))}>
              {[0, 1, 2].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label>total risk $
            <input type="number" min="0" step="1" value={budget === "" ? Math.ceil(riskOf(sel)) : budget}
                   onChange={e => setBudget(e.target.value)} />
          </label>
          <span className="sw-mandate-lock">limit only · stop required · no same-day re-buys (locked)</span>
        </div>
      )}
      {err && <div className="sw-alert" style={{ marginTop: 8, marginBottom: 0 }}>{err}</div>}
      <div className="sw-mandate-foot">
        The executor (not live yet) will only place entries inside this mandate, and never past the hard limits:
        2 entries a day, 6 a week, 5 open, 8 orders a day. Today you still place orders yourself; the mandate changes nothing in that flow.
      </div>
    </div>
  );
}

function OrdersPanel({ onChanged }) {
  const session = useAdminSession();
  const [orders, setOrders]   = useState(undefined);
  const [ctrl, setCtrl]       = useState(null);
  const [busy, setBusy]       = useState(null);
  const [err, setErr]         = useState(null);
  const [copied, setCopied]   = useState(null);

  const load = useCallback(async () => {
    const today = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    // Today's plan, plus anything still working from earlier (GTC stops, partial fills).
    const o = await authClient.from("trade_orders").select("*")
      .or(`plan_date.eq.${today},status.in.(PLACED,PARTIAL)`).order("id");
    setOrders(o.error ? null : o.data || []);
    const c = await authClient.from("trading_controls").select("*").eq("id", 1);
    setCtrl(c.error ? null : (c.data || [])[0] || null);
  }, []);

  useEffect(() => { if (session) load(); }, [load, session]);

  const decide = async (o, approve) => {
    const verb = approve ? "Approve" : "Reject";
    if (!window.confirm(`${verb} order #${o.id}?\n\n${ticket(o)}${approve ? "\n\nYou then place it yourself in the Robinhood app (MCP account)." : ""}`)) return;
    setBusy(o.id); setErr(null);
    const { error } = await authClient.rpc("approve_trade_order", { p_order_id: o.id, p_approve: approve });
    if (error) setErr(error.message);
    setBusy(null); await load(); onChanged?.();
  };

  const toggleHalt = async () => {
    const halting = !ctrl?.halted;
    const reason = halting ? window.prompt("Halt new entries — reason?", "manual halt from dashboard") : null;
    if (halting && reason === null) return;
    setErr(null);
    const { error } = await authClient.rpc("set_trading_halt", { p_halted: halting, p_reason: reason });
    if (error) setErr(error.message);
    await load();
  };

  const copy = async (key, text) => {
    try { await navigator.clipboard.writeText(text); setCopied(key); setTimeout(() => setCopied(c => c === key ? null : c), 1500); }
    catch { /* clipboard blocked — the ticket text is on screen */ }
  };
  const toPlace = orders?.filter(o => o.status === "APPROVED") || [];

  if (session === null) return (
    <div className="sw-panel">
      <div className="section-label">ORDERS</div>
      <div className="sw-note">Orders are for signed-in admins only. Sign in through RUN PIPELINE (top right).</div>
    </div>
  );
  if (orders === undefined) return null;
  if (orders === null) return (   // trade tables not migrated yet
    <div className="sw-panel">
      <div className="section-label">ORDERS</div>
      <div className="sw-note">Nothing here for this run.</div>
    </div>
  );
  return (
    <div className="sw-panel">
      <div className="sw-orders-head">
        <div className="section-label">ORDERS · APPROVE HERE, THEN PLACE IN ROBINHOOD</div>
        {ctrl && (
          <button className={`sw-btn ${ctrl.halted ? "sw-btn-go" : "sw-btn-stop"}`} onClick={toggleHalt} disabled={!session}
                  title={session ? "" : "Sign in through RUN PIPELINE first"}>
            {ctrl.halted ? "RESUME ENTRIES" : "HALT ENTRIES"}
          </button>
        )}
      </div>
      {ctrl?.halted && <div className="sw-alert">New entries halted: {ctrl.halt_reason || "no reason given"}. Exits still allowed.</div>}
      {err && <div className="sw-alert">{err}</div>}
      <MandateCard orders={orders} session={session} />
      {toPlace.length > 0 && (<>
        <div className="sw-note" style={{ marginBottom: 8 }}>
          TO PLACE NOW · {toPlace.length} approved · enter each field exactly as shown (the fill sync matches on symbol, shares and price)
        </div>
        <div className="sw-tickets">
          {toPlace.map(o => <TicketCard key={o.id} o={o} copied={copied} onCopy={copy} />)}
        </div>
      </>)}
      {orders.length === 0 ? <div className="sw-note">No orders yet today. The morning task builds them after the pipeline runs.</div> : (
        <table className="sw-table">
          <thead><tr><th>#</th><th>STATUS</th><th>ORDER</th><th className="sw-num">RISK</th><th></th></tr></thead>
          <tbody>
            {orders.map(o => (
              <tr key={o.id}>
                <td>{o.id}</td>
                <td className={STATUS_CLS[o.status] || ""}>{o.status}</td>
                <td>
                  <span className={`sw-chip ${o.side === "BUY" ? "sw-buy" : "sw-sell"}`}>{o.side}</span>{" "}
                  <span style={{ color: "var(--text)" }}>{ticket(o).replace(/^(Buy|Sell) /, "")}</span>
                  <div className="sw-why">
                    {o.intent}{o.note ? ` · ${o.note}` : ""}
                    {o.status === "BLOCKED" && ` · blocked: ${arr(o.guard_reasons).join(", ")}`}
                    {arr(o.cancel_first).length > 0 && " · cancel the open stop on this ticker first"}
                    {o.approved_by && ` · ${o.status === "REJECTED" ? "rejected" : "approved"} by ${o.approved_by}`}
                  </div>
                </td>
                <td className="sw-num">{o.risk_dollars != null ? `$${fmt(o.risk_dollars, 0)}` : "—"}</td>
                <td className="sw-actions">
                  {o.status === "PROPOSED" && (<>
                    <button className="sw-btn sw-btn-go" disabled={!session || busy === o.id} onClick={() => decide(o, true)}>APPROVE</button>
                    <button className="sw-btn" disabled={!session || busy === o.id} onClick={() => decide(o, false)}>REJECT</button>
                  </>)}
                  {o.status === "APPROVED" && <span className="sw-why">ticket above ↑</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="sw-note" style={{ marginTop: 10 }}>
        Approved orders are not sent anywhere: place each one in the Robinhood app exactly as written, today only.
        The fill sync links it back and records the fill. Same-day exits are allowed, but never buy a stock back on the day you sold it.
      </div>
    </div>
  );
}

// Which tickers the universe stage chose today and why (universe_runs, written
// by run_universe.py from the account balance).
function UniversePanel({ onTicker }) {
  const session = useAdminSession();
  const [rows, setRows] = useState(undefined);
  useEffect(() => {
    if (!session) return;
    let live = true;
    (async () => {
      const r = await authClient.from("universe_runs").select("*").order("created_at", { ascending: false }).limit(300);
      if (!live) return;
      if (r.error || !r.data?.length) { setRows(null); return; }
      const runId = r.data[0].run_id;
      setRows(r.data.filter(x => x.run_id === runId));
    })();
    return () => { live = false; };
  }, [session]);
  if (!session || rows === undefined) return null;
  if (!rows) return (
    <div className="sw-panel">
      <div className="section-label">UNIVERSE TODAY</div>
      <div className="sw-note">Nothing here for this run.</div>
    </div>
  );

  const group = (src, inc) => rows.filter(r => r.source === src && r.included === inc)
    .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999) || a.ticker.localeCompare(b.ticker));
  const chips = list => list.map(r => (
    <button key={r.ticker} className="sw-tk" style={{ marginRight: 10 }} title={r.reason} onClick={() => onTicker(r.ticker)}>{r.ticker}</button>
  ));
  const analysed = rows.filter(r => r.included).length;
  const skippedCore = group("CORE", false);
  return (
    <div className="sw-panel">
      <div className="section-label" style={{ marginBottom: 12 }}>
        UNIVERSE TODAY · {analysed} ANALYSED · CHOSEN FROM THE ACCOUNT BALANCE · {rows[0].run_date}
      </div>
      <table className="sw-table"><tbody>
        {[["HELD", "Open positions — always analysed", group("HELD", true)],
          ["CORE", "universe.txt names the account can size", group("CORE", true)],
          ["SLEEVE", "Added from candidates.txt (top by relative strength, ≤3 per sector)", group("SLEEVE", true)]]
          .filter(([, , list]) => list.length).map(([src, label, list]) => (
            <tr key={src}>
              <td style={{ width: 80 }}><span className="sw-chip sw-mute">{src}</span></td>
              <td>{chips(list)}<div className="sw-why">{label}{src === "SLEEVE" ? ` · ${list.map(r => `${r.ticker}: ${r.reason.replace(/^sleeve #\d+: /, "")}`).join(" · ")}` : ""}</div></td>
            </tr>
          ))}
        {skippedCore.length > 0 && (
          <tr>
            <td><span className="sw-chip sw-mute">SKIPPED</span></td>
            <td className="sw-why" style={{ marginTop: 0 }}>
              Core names too large for the account today (no Claude call spent):{" "}
              {skippedCore.map(r => `${r.ticker} — ${r.reason}`).join(" · ")}
            </td>
          </tr>
        )}
      </tbody></table>
    </div>
  );
}

export default function SwingPlansView({ supabase, reports, prices, onTicker }) {
  const [trades, setTrades] = useState(null);   // null = table missing / not loaded

  useEffect(() => {
    let live = true;
    (async () => {
      const t = await supabase.from("swing_backtest_trades").select("*").order("report_date", { ascending: false }).limit(500);
      if (live) setTrades(t.error ? null : t.data || []);
    })();
    return () => { live = false; };
  }, [supabase]);

  const swing = reports.filter(r => r.trading_mode === "swing");
  if (swing.length === 0) return (
    <div className="sw-empty">
      // the latest analyst run is not a swing run — set <span style={{ color: "var(--amber)" }}>TRADING_MODE=swing</span> and run the pipeline
    </div>
  );

  const lastClose = t => { const s = prices[t]; return s && s.length ? Number(s[s.length - 1].close) : null; };
  const plans = swing.filter(r => r.signal === "BUY" && r.entry_price != null)
    .sort((a, b) => (RANK[a.signal_strength] ?? 3) - (RANK[b.signal_strength] ?? 3) || (b.risk_reward ?? 0) - (a.risk_reward ?? 0));
  const exits = swing.filter(r => r.signal === "SELL");
  const overridden = swing.filter(r => r.model_signal && r.model_signal !== r.signal);

  const closed = (trades || []).filter(t => CLOSED.has(t.outcome));
  const rs = closed.map(t => Number(t.r_multiple));
  const wins = rs.filter(r => r > 0).length;
  const expectancy = rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null;
  const attempted = (trades || []).filter(t => !["SKIPPED_IN_TRADE", "PENDING"].includes(t.outcome));
  const fillRate = attempted.length ? attempted.filter(t => t.fill_date).length / attempted.length : null;

  const cards = [
    { label: "NEW ENTRIES", value: plans.length, sub: `limit orders · good for 2 sessions` },
    { label: "EXIT SIGNALS", value: exits.length, cls: exits.length ? "neg" : "", sub: "sell at the open if held" },
    { label: "WIN RATE", value: rs.length ? `${Math.round(wins / rs.length * 100)}%` : "—",
      sub: trades == null ? "apply swing_schema.sql" : `${rs.length} closed simulated trade${rs.length === 1 ? "" : "s"}` },
    { label: "EXPECTANCY", value: expectancy == null ? "—" : `${expectancy >= 0 ? "+" : ""}${expectancy.toFixed(2)}R`,
      cls: expectancy == null ? "" : expectancy > 0 ? "pos" : "neg",
      sub: fillRate == null ? "avg R per trade, net of costs" : `per trade · ${Math.round(fillRate * 100)}% of entries filled` },
  ];

  return (
    <div>
      <div className="sw-cards">
        {cards.map(c => (
          <div key={c.label} className="sw-card">
            <div className="metric-label">{c.label}</div>
            <div className={`metric-value ${c.cls || ""}`} style={{ fontSize: 22 }}>{c.value}</div>
            <div className="sw-sub">{c.sub}</div>
          </div>
        ))}
      </div>

      <div className="sw-panel">
        <div className="section-label" style={{ marginBottom: 12 }}>
          NEW ENTRIES · {swing[0].report_date} · 1–10 DAY HOLDS · LONG ONLY · NO SAME-DAY RE-BUYS
        </div>
        {plans.length === 0 ? <div className="sw-note">No new entries — no setup cleared the rules today.</div> : (
          <table className="sw-table">
            <thead><tr>
              <th>TICKER</th><th>SETUP</th><th className="sw-num">LIMIT</th><th className="sw-num">STOP</th>
              <th className="sw-num">TARGET</th><th className="sw-num">R:R</th><th className="sw-num">DAYS</th><th>STOP → TARGET</th>
            </tr></thead>
            <tbody>
              {plans.map(p => (
                <tr key={p.ticker}>
                  <td><button className="sw-tk" onClick={() => onTicker(p.ticker)}>{p.ticker}</button></td>
                  <td>{p.setup_type}<div className="sw-why">{p.signal_strength}</div></td>
                  <td className="sw-num" style={{ color: "var(--text)" }}>{fmt(p.entry_price)}</td>
                  <td className="sw-num sw-neg">{fmt(p.stop_price)}<div className="sw-why">{pct(p.entry_price, p.stop_price)}</div></td>
                  <td className="sw-num sw-pos">{fmt(p.target_price)}<div className="sw-why">{pct(p.entry_price, p.target_price)}</div></td>
                  <td className="sw-num">{fmt(p.risk_reward, 1)}R</td>
                  <td className="sw-num">{p.horizon_days}</td>
                  <td>
                    <Ladder stop={Number(p.stop_price)} entry={Number(p.entry_price)} target={Number(p.target_price)} last={lastClose(p.ticker)} />
                    {p.invalidation && <div className="sw-why">wrong if: {p.invalidation}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="sw-note" style={{ marginTop: 10 }}>
          Bar: red = risk to the stop, green = reward to the target, white tick = limit, amber dot = last close.
        </div>
      </div>

      {(exits.length > 0 || overridden.length > 0) && (
        <div className="sw-panel">
          <div className="section-label" style={{ marginBottom: 12 }}>EXITS AND RULE OVERRIDES</div>
          <table className="sw-table"><tbody>
            {exits.map(r => (
              <tr key={`x-${r.ticker}`}>
                <td style={{ width: 70 }}><span className="sw-chip sw-sell">SELL</span></td>
                <td style={{ width: 70 }}><button className="sw-tk" onClick={() => onTicker(r.ticker)}>{r.ticker}</button></td>
                <td>{r.setup_type}{r.invalidation && <div className="sw-why">wrong if: {r.invalidation}</div>}</td>
              </tr>
            ))}
            {overridden.map(r => (
              <tr key={`o-${r.ticker}`}>
                <td><span className="sw-chip sw-mute">RULE</span></td>
                <td><button className="sw-tk" onClick={() => onTicker(r.ticker)}>{r.ticker}</button></td>
                <td>{r.model_signal} → {r.signal}<div className="sw-why">{arr(r.swing_notes).join(" · ")}</div></td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}

      <OrdersPanel />

      <UniversePanel onTicker={onTicker} />

      <div className="sw-panel">
        <div className="section-label" style={{ marginBottom: 12 }}>SIMULATED TRADES · DAILY BARS · NET OF COSTS</div>
        {trades == null ? <div className="sw-note">swing_backtest_trades not found — apply swing_schema.sql in Supabase.</div>
          : trades.length === 0 ? <div className="sw-note">No swing plans simulated yet. Stage 9 scores each plan as its stop, target or time limit is reached.</div>
          : (
            <table className="sw-table">
              <thead><tr><th>DATE</th><th>TICKER</th><th>SETUP</th><th>OUTCOME</th><th className="sw-num">FILL</th><th className="sw-num">EXIT</th><th className="sw-num">HELD</th><th className="sw-num">RETURN</th><th className="sw-num">R</th></tr></thead>
              <tbody>
                {trades.filter(t => t.outcome !== "SKIPPED_IN_TRADE").slice(0, 25).map(t => (
                  <tr key={`${t.report_run_id}-${t.ticker}`}>
                    <td>{t.report_date}</td>
                    <td><button className="sw-tk" onClick={() => onTicker(t.ticker)}>{t.ticker}</button></td>
                    <td>{t.setup_type}</td>
                    <td className={t.outcome === "TARGET" ? "sw-pos" : t.outcome === "STOP" ? "sw-neg" : ""}>{t.outcome}</td>
                    <td className="sw-num">{fmt(t.fill_price)}</td>
                    <td className="sw-num">{fmt(t.exit_price)}</td>
                    <td className="sw-num">{t.hold_sessions ?? "—"}</td>
                    <td className={`sw-num ${rCls(t.return_pct)}`}>{t.return_pct == null ? "—" : `${t.return_pct >= 0 ? "+" : ""}${fmt(t.return_pct)}%`}</td>
                    <td className={`sw-num ${rCls(t.r_multiple)}`}>{t.r_multiple == null ? "—" : `${t.r_multiple >= 0 ? "+" : ""}${fmt(t.r_multiple)}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
    </div>
  );
}
