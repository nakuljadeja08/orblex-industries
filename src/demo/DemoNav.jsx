// DemoNav.jsx — the public demo's navigation and headline numbers. The private dashboard keeps its
// compact one-row tab strip; the demo is read by people seeing the system for the first time, so it
// gets a numbered section stepper, larger sub-tabs, a KPI band, and Back / Next through every view.
import { useEffect, useRef, useState } from "react";

// One plain-language line per nav group (labels come from Dashboard.jsx's VIEW_GROUPS).
const SECTION = {
  "START HERE":    ["★", "Start here", "What this is"],
  "1 · MARKET":    ["1", "Market", "The backdrop"],
  "2 · INPUTS":    ["2", "Inputs", "Data per ticker"],
  "3 · DECISIONS": ["3", "Decisions", "Call, rules, size"],
  "4 · OUTPUT":    ["4", "Output", "Weekly report"],
  "CHECKS":        ["✓", "Checks", "Is it working?"],
  "OPERATIONS":    ["⚙", "Operations", "Schedule, review"],
};
const pretty = v => v === "HOW IT WORKS" ? "How it works" : v.charAt(0) + v.slice(1).toLowerCase();
export const viewName = pretty;

const groupOf = (groups, view) => groups.find(g => g.views.includes(view)) || groups[0];

export function DemoNav({ groups, view, onGo }) {
  const active = groupOf(groups, view);
  const steps = useRef(null);
  useEffect(() => {   // keep the current section visible when the stepper scrolls sideways (phones)
    const el = steps.current?.querySelector(".dn-step.on");
    const box = steps.current;
    if (el && box) box.scrollTo({ left: el.offsetLeft - (box.clientWidth - el.offsetWidth) / 2, behavior: "smooth" });
  }, [active]);
  return (
    <nav className="dn" aria-label="Dashboard sections">
      <ol className="dn-steps" ref={steps}>
        {groups.map(g => {
          const [n, title, sub] = SECTION[g.label] || ["", g.label, ""];
          const on = g === active;
          return (
            <li key={g.label}>
              <button className={`dn-step${on ? " on" : ""}`} aria-current={on ? "step" : undefined}
                      onClick={() => onGo(on ? view : g.views[0])}>
                <span className="dn-num" aria-hidden="true">{n}</span>
                <span className="dn-txt"><b>{title}</b><small>{sub}</small></span>
              </button>
            </li>
          );
        })}
      </ol>
      {active.views.length > 1 && (
        <div className="dn-sub" role="tablist" aria-label={`${(SECTION[active.label] || [])[1] || active.label} views`}>
          {active.views.map(v => (
            <button key={v} role="tab" aria-selected={view === v} className={`dn-tab${view === v ? " on" : ""}`} onClick={() => onGo(v)}>
              {pretty(v)}
            </button>
          ))}
        </div>
      )}
    </nav>
  );
}

// Back / Next through every view in pipeline order, so a first-time visitor can just keep clicking.
export function DemoStepper({ groups, view, onGo }) {
  const order = groups.flatMap(g => g.views);
  const i = order.indexOf(view);
  if (i < 0) return null;
  const go = v => { onGo(v); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const prev = order[i - 1], next = order[i + 1];
  return (
    <div className="dn-pager">
      {prev ? <button className="dn-pg" onClick={() => go(prev)}><small>← Back</small><b>{pretty(prev)}</b></button> : <span />}
      <span className="dn-pos">Step {i + 1} of {order.length}</span>
      {next ? <button className="dn-pg dn-pg-next" onClick={() => go(next)}><small>Next →</small><b>{pretty(next)}</b></button> : <span />}
    </div>
  );
}

// Today's run in five numbers. Each tile opens the view that explains it.
export function DemoKpis({ supabase, reports, regime, onGo }) {
  const [orders, setOrders] = useState([]);
  const [equity, setEquity] = useState(null);
  useEffect(() => {
    let live = true;
    (async () => {
      const o = await supabase.from("trade_orders").select("*").order("id");
      const c = await supabase.from("trading_controls").select("*").eq("id", 1);
      if (!live) return;
      setOrders(o.data || []);
      setEquity((c.data || [])[0]?.equity_peak ?? null);
    })();
    return () => { live = false; };
  }, [supabase]);

  const n = s => reports.filter(r => r.signal === s).length;
  const proposed = orders.filter(o => o.status === "PROPOSED").length;
  const blocked = orders.filter(o => o.status === "BLOCKED").length;
  const tone = { BULL: "g", BEAR: "r", CORRECTION: "r", CHOPPY: "a" }[regime?.regime] || "";
  const tiles = [
    ["Market regime", regime?.regime || "—", regime?.risk_floor ? `risk floor ${regime.risk_floor}` : "trend and volatility", "REGIME", tone],
    ["Tickers analysed", reports.length, `${n("BUY")} buy · ${n("SELL")} sell · ${n("HOLD")} hold`, "ANALYST", ""],
    ["Orders proposed", proposed, "waiting for a person to approve", "TRADE PLANS", "a"],
    ["Blocked by rules", blocked, "the guard said no", "TRADE PLANS", blocked ? "r" : ""],
    ["Paper account", equity != null ? `$${Number(equity).toLocaleString("en-US")}` : "—", "sample money, no real orders", "DAILY REVIEW", ""],
  ];
  return (
    <section className="dk" aria-label="Today at a glance">
      {tiles.map(([label, value, sub, view, t]) => (
        <button key={label} className="dk-tile" onClick={() => onGo(view)} aria-label={`${label}: ${value}, ${sub}. Open ${pretty(view)}.`}>
          <span className="dk-label">{label}</span>
          <span className={`dk-value${t ? ` dk-${t}` : ""}`}>{value}</span>
          <span className="dk-sub">{sub}</span>
        </button>
      ))}
    </section>
  );
}

export const DEMO_NAV_CSS = `
  .dn{background:var(--bg2);border-bottom:1px solid var(--border);}
  .dn-steps{display:flex;gap:6px;list-style:none;margin:0;padding:10px 20px;overflow-x:auto;scrollbar-width:thin;}
  .dn-steps li{flex:1 0 auto;}
  .dn-step{width:100%;display:flex;align-items:center;gap:10px;text-align:left;padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text-dim);cursor:pointer;transition:border-color .15s,background .15s;}
  .dn-step:hover{border-color:var(--border-amber);color:var(--text);}
  .dn-step.on{border-color:var(--amber);background:var(--amber-glow);color:var(--text);}
  .dn-num{flex:none;width:26px;height:26px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:12px;font-weight:700;border:1px solid var(--border);color:var(--text-dim);}
  .dn-step.on .dn-num{background:var(--amber);border-color:var(--amber);color:#111;}
  .dn-txt{display:flex;flex-direction:column;line-height:1.25;white-space:nowrap;}
  .dn-txt b{font-size:14px;font-weight:600;}
  .dn-txt small{font-size:11.5px;color:var(--text-dimmer);}
  .dn-sub{display:flex;gap:4px;padding:0 20px;overflow-x:auto;border-top:1px solid var(--border);}
  .dn-tab{padding:11px 14px;font-size:14px;font-weight:500;color:var(--text-dim);border:none;border-bottom:2px solid transparent;background:transparent;cursor:pointer;white-space:nowrap;}
  .dn-tab:hover{color:var(--text);}
  .dn-tab.on{color:var(--amber);border-bottom-color:var(--amber);}

  .dk{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;padding:14px 20px;border-bottom:1px solid var(--border);}
  .dk-tile{display:flex;flex-direction:column;gap:4px;text-align:left;padding:14px 16px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;cursor:pointer;min-width:0;transition:border-color .15s;}
  .dk-tile:hover{border-color:var(--border-amber);}
  .dk-label{font-size:12px;font-weight:500;color:var(--text-dim);}
  .dk-value{font-family:var(--mono);font-size:30px;font-weight:700;line-height:1.1;color:var(--text);font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .dk-g{color:var(--green);} .dk-r{color:var(--red);} .dk-a{color:var(--amber);}
  .dk-sub{font-size:12px;color:var(--text-dimmer);line-height:1.4;}

  .dn-pager{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px;padding:18px 20px 24px;border-top:1px solid var(--border);margin-top:16px;}
  .dn-pg{display:flex;flex-direction:column;gap:2px;text-align:left;padding:10px 14px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);cursor:pointer;justify-self:start;max-width:100%;}
  .dn-pg:hover{border-color:var(--border-amber);}
  .dn-pg small{font-size:11.5px;color:var(--text-dimmer);}
  .dn-pg b{font-size:14px;font-weight:600;}
  .dn-pg-next{justify-self:end;text-align:right;border-color:var(--border-amber);}
  .dn-pg-next small{color:var(--amber);}
  .dn-pos{font-family:var(--mono);font-size:11px;color:var(--text-dimmer);white-space:nowrap;}

  /* Easier reading in the demo: larger context line and stat numbers across the views. */
  .is-demo .context-bar{font-size:14px;padding:10px 20px;}
  .is-demo .context-stage{font-size:10.5px;}
  .is-demo .metric-label,.is-demo .macro-stat-label{font-size:11.5px;}
  .is-demo .metric-value{font-size:19px;}
  .is-demo .macro-stat-value{font-size:26px;}
  .is-demo .footer{font-size:12px;}

  @media(max-width:1180px){
    .dn-txt small{display:none;}
    .dn-step{padding:8px 10px;gap:8px;}
    .dn-steps{gap:4px;}
  }
  @media(max-width:900px){ .dk{grid-template-columns:repeat(3,minmax(0,1fr));} }
  @media(max-width:680px){
    .dn-steps,.dn-sub,.dk,.dn-pager{padding-left:16px;padding-right:16px;}
    .dn-txt small{display:none;}
    .dk{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;scroll-padding:0 16px;gap:8px;}
    .dk-tile{flex:0 0 46%;scroll-snap-align:start;padding:12px;}
    .dk-value{font-size:24px;}
    .dn-pos{display:none;}
    .dn-pager{grid-template-columns:1fr 1fr;}
  }
`;
