// DemoIntro.jsx — the demo's opening tab: what the system does, how to read the sample screens,
// and how to run it under your own accounts.
const REPO = import.meta.env.VITE_REPO_URL;

const PATH = [
  ["Market data", "Prices, earnings, options and the market regime.", "REGIME"],
  ["Analyst", "Claude reads it and proposes a call with entry, stop and target.", "ANALYST"],
  ["Rules", "Python clamps the levels and can turn a BUY into a HOLD.", "TRADE PLANS"],
  ["Risk", "Volatility, earnings and the regime set a score. 8 or more excludes the ticker.", "RISK"],
  ["Sizing and guard", "Python sizes whole shares, then checks every limit.", "TRADE PLANS"],
  ["You", "Nothing happens until a person approves it and places it.", "TRADE PLANS"],
];

const SETUP = [
  ["Claude API key", "It powers the analyst. Create one in the Claude Console and set ANTHROPIC_API_KEY. You pay for your own usage."],
  ["Supabase project", "A free project holds the plans and orders. Run the schema files, then set SUPABASE_URL and SUPABASE_KEY."],
  ["Run a paper plan", "python run_all.py --dry-run computes everything and writes nothing. python run_trade_plan.py plan --paper 5000 --dry-run sizes a plan with no broker at all."],
  ["Robinhood MCP (optional)", "python rh_client.py login signs you in through your own browser and phone. The client only reads your account, and the live executor stays off unless you switch it on."],
];

export const DEMO_CSS = `
  .demo-banner{background:var(--amber-glow);border-bottom:1px solid var(--border-amber);color:var(--text-dim);font-size:12px;padding:8px 16px;line-height:1.5;}
  .demo-banner b{font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--amber);margin-right:8px;}
  .demo-intro{max-width:880px;display:flex;flex-direction:column;gap:28px;padding:16px 16px 32px;}
  .demo-intro h2{font-size:20px;font-weight:600;color:var(--text);margin-bottom:8px;text-wrap:balance;}
  .demo-intro h3{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--amber);margin-bottom:10px;}
  .demo-intro p{font-size:14px;line-height:1.65;color:var(--text-dim);max-width:68ch;}
  .demo-path{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:10px;counter-reset:step;}
  .demo-step{text-align:left;background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;counter-increment:step;}
  .demo-step:hover{border-color:var(--border-amber);}
  .demo-step b{font-size:13px;font-weight:600;color:var(--text);}
  .demo-step b::before{content:counter(step) " · ";font-family:var(--mono);color:var(--amber);}
  .demo-step span{font-size:12px;line-height:1.5;color:var(--text-dim);}
  .demo-setup{display:flex;flex-direction:column;gap:10px;margin:0;padding:0;list-style:none;}
  .demo-setup li{font-size:13px;line-height:1.6;color:var(--text-dim);max-width:72ch;}
  .demo-setup b{display:block;color:var(--text);font-weight:600;}
`;

export default function DemoIntro({ onGo }) {
  return (
    <div className="demo-intro">
      <section>
        <h2>An AI analyst that proposes trades, and rules that decide what gets through</h2>
        <p>Each weekday, a pipeline turns market data into swing trade plans held for 1 to 10 days. Claude reads the context and proposes. Python code owns every number, cap and order rule, and can only make a trade smaller or stop it. A person approves every order.</p>
      </section>
      <section>
        <h3>ONE TRADE'S PATH · SELECT A STEP TO SEE IT</h3>
        <div className="demo-path">
          {PATH.map(([title, text, view]) => (
            <button key={title} className="demo-step" onClick={() => onGo(view)}>
              <b>{title}</b><span>{text}</span>
            </button>
          ))}
        </div>
      </section>
      <section>
        <h3>TRY THESE</h3>
        <p>Open TRADE PLANS and approve or reject an order, set a daily mandate, or halt new entries. Your clicks change only this page and reset when you reload. Then open ANALYST and read "Why this call?" for any ticker, or switch to MAIN ACCOUNT for the weekly advice book.</p>
      </section>
      <section>
        <h3>RUN IT WITH YOUR OWN ACCOUNTS</h3>
        <ul className="demo-setup">
          {SETUP.map(([title, text]) => <li key={title}><b>{title}</b>{text}</li>)}
        </ul>
        <p style={{ marginTop: 12 }}>
          {REPO ? <>The code and full setup guide are at <a href={REPO} target="_blank" rel="noopener noreferrer" style={{ color: "var(--amber)" }}>the public repository</a>.</> : "The public repository and full setup guide are coming soon."}
          {" "}Hosted accounts, where you can try this with your own Claude key without installing anything, are planned too.
        </p>
      </section>
      <section>
        <h3>WHAT THIS IS NOT</h3>
        <p>Every number on these screens is made up, so they say nothing about how the strategy performs. This is a personal project and not financial advice. Real trading can lose money, and the system hasn't shown a proven edge.</p>
      </section>
    </div>
  );
}
