// DemoIntro.jsx — the demo's opening tab: what the system does, who decides what, how a weekday
// runs, and where to click next. The headline numbers sit above it in DemoKpis (DemoNav.jsx).
const REPO = import.meta.env.VITE_REPO_URL;

// Who acts at each step: data, the AI (Claude), the rules (Python), or a person.
const WHO = { data: "Data", ai: "Claude", rules: "Python rules", you: "You" };
const PATH = [
  ["data",  "Market data",      "Prices, earnings, options and the market regime are collected.", "REGIME"],
  ["ai",    "Analyst",          "Claude reads everything and proposes BUY, SELL or HOLD with an entry, stop and target.", "ANALYST"],
  ["rules", "Swing rules",      "Python clamps the levels and timing, and can turn a BUY into a HOLD.", "TRADE PLANS"],
  ["rules", "Risk score",       "Volatility, earnings and the regime set a score. 8 or more excludes the ticker.", "RISK"],
  ["rules", "Sizing and guard", "Python sizes whole shares, then checks every limit before an order exists.", "TRADE PLANS"],
  ["you",   "Approval",         "Nothing is placed until a person approves it.", "TRADE PLANS"],
];

const ROLES = [
  ["ai", "Claude proposes", "Reads the data and the news, writes the call and its reasoning. It never sets a size or places an order."],
  ["rules", "Python decides", "Owns every number, cap and order rule. It can only make a trade smaller or stop it, never bigger."],
  ["you", "A person approves", "Every order waits for approval. Halting new entries is one click."],
];

const DAY = [
  ["Before the open", "The pipeline pulls data, the analyst makes its calls, and the rules build the trade plan.", "PIPELINE"],
  ["Around the open", "A dry run checks the plan against the account, then orders wait for approval.", "AUTOMATION"],
  ["During the day", "Fills are synced and every open position keeps a resting stop.", "TRADE PLANS"],
  ["After the close", "A daily review compares what was planned with what happened.", "DAILY REVIEW"],
];

const TRY = [
  ["Trace one call", "Open a ticker in Analyst and read \"Why this call?\" back to its inputs.", "ANALYST"],
  ["Approve an order", "Approve or reject a proposed order, or halt new entries. Clicks reset on reload.", "TRADE PLANS"],
  ["Run the real analyst", "Shape a chart in the Playground and run it with your own Claude key.", "PLAYGROUND"],
];

const SETUP = [
  ["Claude API key", "It powers the analyst. Create one in the Claude Console and set ANTHROPIC_API_KEY. You pay for your own usage."],
  ["Supabase project", "A free project holds the plans and orders. Run the schema files, then set SUPABASE_URL and SUPABASE_KEY."],
  ["Run a paper plan", "python run_all.py --dry-run computes everything and writes nothing. python run_trade_plan.py plan --paper 5000 --dry-run sizes a plan with no broker at all."],
  ["Robinhood MCP (optional)", "python rh_client.py login signs you in through your own browser and phone. The client only reads your account, and the live executor stays off unless you switch it on."],
];

export const DEMO_CSS = `
  .demo-banner{background:var(--amber-glow);border-bottom:1px solid var(--border-amber);color:var(--text-dim);font-size:13px;padding:8px 20px;line-height:1.5;}
  .demo-banner b{font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--amber);margin-right:8px;}
  .demo-intro{max-width:1100px;display:flex;flex-direction:column;gap:36px;padding:24px 20px 8px;}
  .demo-intro h2{font-size:28px;line-height:1.2;font-weight:700;color:var(--text);margin-bottom:10px;text-wrap:balance;max-width:28ch;}
  .demo-intro h3{font-size:18px;font-weight:600;color:var(--text);margin-bottom:4px;}
  .demo-intro .di-kicker{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--amber);margin-bottom:8px;}
  .demo-intro p{font-size:15px;line-height:1.65;color:var(--text-dim);max-width:70ch;}
  .demo-intro .di-lede{font-size:16px;}
  .di-cta{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;}
  .di-btn{font-size:14px;font-weight:600;padding:10px 16px;border-radius:6px;border:1px solid var(--border);background:var(--bg2);color:var(--text);cursor:pointer;}
  .di-btn:hover{border-color:var(--border-amber);}
  .di-btn.primary{background:var(--amber);border-color:var(--amber);color:#111;}
  .di-sec-head{margin-bottom:14px;}
  .di-sec-head p{font-size:14px;}

  .di-flow{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:22px;list-style:none;margin:0;padding:0;counter-reset:step;}
  .di-flow li{position:relative;counter-increment:step;display:flex;}
  .di-flow li:not(:last-child)::after{content:"→";position:absolute;right:-17px;top:50%;transform:translateY(-50%);color:var(--text-dimmer);font-size:16px;}
  .di-node{flex:1;display:flex;flex-direction:column;gap:8px;text-align:left;padding:14px;background:var(--bg2);border:1px solid var(--border);border-top:3px solid var(--who);border-radius:6px;cursor:pointer;}
  .di-node:hover{border-color:var(--border-amber);border-top-color:var(--who);}
  .di-node b{font-size:15px;font-weight:600;color:var(--text);}
  .di-node b::before{content:counter(step) ". ";font-family:var(--mono);color:var(--text-dimmer);}
  .di-node span{font-size:13px;line-height:1.5;color:var(--text-dim);}
  .di-who{align-self:flex-start;white-space:nowrap;font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:2px 8px;border-radius:999px;color:var(--who);border:1px solid var(--who);}
  .w-data{--who:var(--text-dim);} .w-ai{--who:#8FB4FF;} .w-rules{--who:var(--amber);} .w-you{--who:var(--green);}

  .di-roles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;}
  .di-role{padding:16px;background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--who);border-radius:6px;}
  .di-role b{display:block;font-size:16px;font-weight:600;color:var(--text);margin-bottom:6px;}
  .di-role p{font-size:14px;}

  .di-day{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;list-style:none;margin:0;padding:0;}
  .di-day li{position:relative;padding:18px 16px 0 0;border-top:2px solid var(--border);}
  .di-day li::before{content:"";position:absolute;top:-7px;left:0;width:12px;height:12px;border-radius:50%;background:var(--bg);border:2px solid var(--amber);}
  .di-day b{display:block;font-size:15px;font-weight:600;color:var(--text);margin-bottom:4px;}
  .di-day p{font-size:13.5px;}
  .di-link{margin-top:6px;font-size:13px;color:var(--amber);background:none;border:none;padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:3px;}

  .di-try{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;}
  .di-card{display:flex;flex-direction:column;gap:6px;text-align:left;padding:16px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;cursor:pointer;}
  .di-card:hover{border-color:var(--border-amber);}
  .di-card b{font-size:15px;font-weight:600;color:var(--text);}
  .di-card span{font-size:13.5px;line-height:1.5;color:var(--text-dim);}
  .di-card em{font-style:normal;font-size:13px;color:var(--amber);margin-top:auto;padding-top:4px;}

  .di-details{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:14px 16px;}
  .di-details summary{cursor:pointer;font-size:15px;font-weight:600;color:var(--text);}
  .demo-setup{display:flex;flex-direction:column;gap:12px;margin:14px 0 0;padding:0;list-style:none;}
  .demo-setup li{font-size:14px;line-height:1.6;color:var(--text-dim);max-width:76ch;}
  .demo-setup b{display:block;color:var(--text);font-weight:600;}

  @media(max-width:1200px){
    .di-flow{grid-template-columns:repeat(3,minmax(0,1fr));}
    .di-flow li:nth-child(3)::after{content:none;}
  }
  @media(max-width:680px){
    .demo-intro{padding:20px 16px 8px;gap:30px;}
    .demo-intro h2{font-size:23px;}
    .di-flow,.di-roles,.di-try{grid-template-columns:1fr;}
    .di-flow{gap:18px;}
    .di-flow li:not(:last-child)::after{content:"↓";right:auto;left:50%;top:auto;bottom:-18px;transform:translateX(-50%);font-size:14px;}
    .di-flow li:nth-child(3)::after{content:"↓";}
    .di-day{grid-template-columns:1fr;}
    .di-day li{border-top:none;border-left:2px solid var(--border);padding:0 0 18px 18px;}
    .di-day li::before{top:2px;left:-7px;}
  }
`;

export default function DemoIntro({ onGo }) {
  const go = v => { onGo(v); window.scrollTo({ top: 0, behavior: "smooth" }); };
  return (
    <div className="demo-intro">
      <section>
        <div className="di-kicker">HOW IT WORKS</div>
        <h2>An AI analyst that proposes trades, and rules that decide what gets through</h2>
        <p className="di-lede">Each weekday a pipeline turns market data into swing trade plans held for 1 to 10 days. Claude proposes, Python rules size and check every order, and a person approves before anything is placed.</p>
        <div className="di-cta">
          <button className="di-btn primary" onClick={() => go("REGIME")}>Walk through it step by step →</button>
          <button className="di-btn" onClick={() => go("TRADE PLANS")}>See today's trade plans</button>
          <button className="di-btn" onClick={() => go("PLAYGROUND")}>Try the playground</button>
        </div>
      </section>

      <section>
        <div className="di-sec-head">
          <h3>How one trade moves</h3>
          <p>Six steps from raw data to an order. Select any step to see it on the sample data.</p>
        </div>
        <ol className="di-flow">
          {PATH.map(([who, title, text, view]) => (
            <li key={title}>
              <button className={`di-node w-${who}`} onClick={() => go(view)}>
                <span className="di-who">{WHO[who]}</span>
                <b>{title}</b>
                <span>{text}</span>
              </button>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <div className="di-sec-head"><h3>Who decides what</h3></div>
        <div className="di-roles">
          {ROLES.map(([who, title, text]) => (
            <div key={title} className={`di-role w-${who}`}><b>{title}</b><p>{text}</p></div>
          ))}
        </div>
      </section>

      <section>
        <div className="di-sec-head">
          <h3>A weekday, on schedule</h3>
          <p>Scheduled jobs run the whole day. Any amber or red result sends an email.</p>
        </div>
        <ol className="di-day">
          {DAY.map(([when, text, view]) => (
            <li key={when}><b>{when}</b><p>{text}</p><button className="di-link" onClick={() => go(view)}>Open {view.charAt(0) + view.slice(1).toLowerCase()}</button></li>
          ))}
        </ol>
      </section>

      <section>
        <div className="di-sec-head">
          <h3>Try it</h3>
          <p>Everything here runs on made-up data, so click freely.</p>
        </div>
        <div className="di-try">
          {TRY.map(([title, text, view]) => (
            <button key={title} className="di-card" onClick={() => go(view)}>
              <b>{title}</b><span>{text}</span><em>Open {view.charAt(0) + view.slice(1).toLowerCase()} →</em>
            </button>
          ))}
        </div>
      </section>

      <section>
        <details className="di-details">
          <summary>Run it with your own accounts</summary>
          <ul className="demo-setup">
            {SETUP.map(([title, text]) => <li key={title}><b>{title}</b>{text}</li>)}
          </ul>
          <p style={{ marginTop: 12, fontSize: 14 }}>
            {REPO ? <>The code and full setup guide are at <a href={REPO} target="_blank" rel="noopener noreferrer" style={{ color: "var(--amber)" }}>the public repository</a>.</> : "The public repository and full setup guide are coming soon."}
          </p>
        </details>
      </section>

      <section>
        <div className="di-sec-head"><h3>What this is not</h3></div>
        <p>Every number on these screens is made up, so they say nothing about how the strategy performs. This is a personal project and not financial advice. Real trading can lose money, and the system hasn't shown a proven edge.</p>
      </section>
    </div>
  );
}
