// fixtures.js — the demo's sample data. Every ticker, price, fund and order here is made up and
// generated from a fixed seed, with dates that move with today so the screens look current.
// Nothing is copied from the real system: scripts/check_demo_bundle.mjs fails the build if a real
// ticker appears here. Rows carry the same columns as the production tables.

export const DEMO_EMAIL = "demo@example.com";
const EQUITY = 5000;

let seed = 20261102;
const rnd = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
const between = (a, b) => a + rnd() * (b - a);
const round = (x, d = 2) => Number(x.toFixed(d));

export const todayEt = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
const iso = d => d.toISOString().slice(0, 10);
const daysAgo = n => { const d = new Date(`${todayEt()}T12:00:00Z`); d.setUTCDate(d.getUTCDate() - n); return d; };
const stamp = (dateStr, hhmm) => `${dateStr}T${hhmm}:00+00:00`;

// The last n weekdays, oldest first, ending today.
function sessions(n) {
  const out = [];
  for (let i = 0; out.length < n; i++) {
    const d = daysAgo(i);
    if (d.getUTCDay() % 6 !== 0) out.unshift(iso(d));
  }
  return out;
}

const NAMES = [
  // ticker, company, sector, start price, daily drift, daily vol, signal, strength, setup, risk score
  ["ACME",  "Acme Robotics",        "ROBOTICS",  62,  0.0016, 0.019, "BUY",  "STRONG",   "PULLBACK", 4],
  ["GLOBX", "Globex Compute",       "SEMIS",     118, 0.0012, 0.024, "BUY",  "MODERATE", "BREAKOUT", 5],
  ["INITC", "Initech Software",     "SOFTWARE",  41,  0.0009, 0.021, "BUY",  "WEAK",     "PULLBACK", 5],
  ["TYRLL", "Tyrell Biosystems",    "OTHER",     24,  0.0010, 0.052, "BUY",  "WEAK",     "PULLBACK", 8],
  ["UMBRL", "Umbrella Power",       "POWER",     87,  0.0005, 0.014, "HOLD", "MODERATE", "NONE",     3],
  ["STRKI", "Stark Networks",       "SOFTWARE",  203, 0.0007, 0.017, "HOLD", "MODERATE", "NONE",     3],
  ["HOOLI", "Hooli Cloud",          "MEGA_CAP_TECH", 154, 0.0004, 0.016, "HOLD", "WEAK", "NONE",     4],
  ["SOYLN", "Soylent Foods",        "OTHER",     33,  0.0001, 0.013, "HOLD", "WEAK",     "NONE",     2],
  ["WONKA", "Wonka Materials",      "OTHER",     71,  0.0008, 0.018, "HOLD", "MODERATE", "NONE",     4],
  ["PIEDP", "Pied Piper Data",      "SOFTWARE",  19,  -0.0022, 0.031, "SELL", "MODERATE", "NONE",    7],
].map(([ticker, company, sector, price, drift, vol, signal, strength, setup, risk]) =>
  ({ ticker, company, sector, price, drift, vol, signal, strength, setup, risk }));

const WHY = {
  ACME:  "The trend is up, with price above its 20 and 50-day averages, and it has pulled back to the 20-day on light volume. A limit near that average gives a tight stop under the recent swing low.",
  GLOBX: "Price closed above a five-week range on 1.8x average volume while the sector leads the market. The plan buys a small dip into the breakout level.",
  INITC: "The uptrend is intact but relative strength has faded this week, so this is a smaller pullback entry with a horizon shorter than usual.",
  TYRLL: "The chart setup is acceptable, but volatility is very high. The risk rules exclude it at a score of 8, so no order is sized.",
  UMBRL: "A steady trend, but price sits far above its 20-day average, so there is no good entry today. Any open position keeps its levels.",
  STRKI: "Range-bound for three weeks with no catalyst in the news window. It waits for a break or a pullback to support.",
  HOOLI: "Mixed: strong over 20 days, weak over 5. There is no clean setup, so no new entry.",
  SOYLN: "Low volatility and no trend. Nothing here offers 1.5R of reward over the planned risk.",
  WONKA: "The position held from last week is working. It keeps its stop and target and there is no new entry.",
  PIEDP: "Price lost its 50-day average on heavy volume and the prior setup is broken. The plan exits any position and doesn't short.",
};

export function tables() {
  const days = sessions(95);
  const today = days[days.length - 1];
  const RUN = `pipeline-${today}-demo01`;
  const RISK_RUN = `risk-${today}-demo01`;
  const LT_DAY = iso(daysAgo((new Date(`${today}T12:00:00Z`).getUTCDay() + 1) % 7 || 7));
  const LT_RUN = `longterm-${LT_DAY}-demo01`;
  const ALLOC_RUN = `alloc-${LT_DAY}-demo01`;
  const REPORT_RUN = `report-${LT_DAY}-demo01`;
  const WD_RUN = `watchdog-${LT_DAY}-demo01`;
  const at = hhmm => stamp(today, hhmm);

  // ── Prices ──
  const stock_prices = [];
  const last = {};
  let pid = 1;
  for (const n of NAMES) {
    let close = n.price;
    const rows = days.map(date => {
      const open = close * (1 + between(-0.006, 0.006));
      close = close * (1 + n.drift + between(-n.vol, n.vol));
      const high = Math.max(open, close) * (1 + between(0, n.vol / 2));
      const low = Math.min(open, close) * (1 - between(0, n.vol / 2));
      return { id: pid++, ticker: n.ticker, date, open: round(open), high: round(high), low: round(low), close: round(close),
               adj_close: round(close), volume: Math.round(between(0.8e6, 6e6)), ingested_at: at("11:00") };
    });
    stock_prices.push(...rows);
    const trs = rows.slice(-14).map(r => r.high - r.low);
    last[n.ticker] = { close: rows[rows.length - 1].close, atr: round(trs.reduce((a, b) => a + b, 0) / 14, 3) };
  }

  // ── Analyst ──
  const agent_reports = NAMES.map((n, i) => {
    const { close, atr } = last[n.ticker];
    const buy = n.signal === "BUY";
    const entry = round(close - 0.5 * atr);
    const stop = round(entry - 1.5 * atr);
    const target = round(entry + 3 * atr);
    const overridden = n.ticker === "SOYLN";
    return {
      id: i + 1, run_id: RUN, ticker: n.ticker, report_date: today, signal: n.signal, signal_strength: n.strength,
      summary: `${n.company}: ${WHY[n.ticker].split(". ")[0]}.`, reasoning: WHY[n.ticker],
      anomalies: n.ticker === "PIEDP" ? [{ type: "volume_spike", detail: "Volume 2.3x average on a down day" }] : [],
      model: "sample", prompt_tokens: 3500, output_tokens: 720, created_at: at("11:30"), news_used: true, news_count: 3,
      sentiment_score: round(n.signal === "SELL" ? -0.4 : n.signal === "BUY" ? 0.35 : 0.05), valuation_view: "N/A", trading_mode: "swing",
      setup_type: n.setup, entry_price: buy ? entry : null, stop_price: buy ? stop : null, target_price: buy ? target : null,
      horizon_days: buy ? 7 : null, risk_reward: buy ? 2 : null,
      invalidation: buy ? `A close below ${stop} breaks the setup.` : "Not applicable.",
      atr_14: atr, swing_flags: overridden ? ["RR_TOO_LOW"] : [], swing_notes: overridden ? ["Reward:risk 1.2 is below 1.5, so the BUY became a HOLD."] : [],
      model_signal: overridden ? "BUY" : n.signal,
    };
  });

  const signal_history = [];
  NAMES.forEach((n, t) => days.slice(-12).forEach((d, i) => {
    const flipped = n.ticker === "PIEDP" ? (i < 8 ? "HOLD" : "SELL") : n.ticker === "GLOBX" ? (i < 10 ? "HOLD" : "BUY") : n.signal;
    signal_history.push({ id: t * 100 + i, run_id: d === today ? RUN : `pipeline-${d}-demo01`, ticker: n.ticker, report_date: d, signal: flipped,
                          signal_strength: n.strength, close_price: null, rsi_14: null, ma_5: null, ma_20: null, volatility_ann: null, volume_ratio: null, created_at: stamp(d, "11:35") });
  }));

  // ── Risk ──
  const annVol = n => round(n.vol * 16 * 0.75, 4);
  const cap = s => (s >= 8 ? 0 : s >= 7 ? 5 : s >= 5 ? 10 : s >= 4 ? 14 : 20);
  const risk_assessments = [{
    id: 1, run_id: RISK_RUN, assessment_date: today, total_tickers: NAMES.length, buy_count: 4, sell_count: 1, hold_count: 5,
    market_risk_level: "MODERATE", concentration_risk: "LOW", volatility_risk: "MODERATE",
    position_limits: Object.fromEntries(NAMES.map(n => [n.ticker, cap(n.risk)])),
    flagged_tickers: NAMES.filter(n => n.risk >= 7).map(n => n.ticker),
    risk_summary: "Market risk MODERATE: one SELL signal, average volatility 30%, and no sector over its cap.",
    reasoning: "Scored by rules. Volatility sets the starting score, then anomalies, SELL calls, earnings and the regime adjust it.",
    model: "rules-v2", prompt_tokens: 0, output_tokens: 0, created_at: at("11:35"), overall_risk_score: 4.4, analyst_run_id: RUN,
  }];
  const risk_ticker_details = NAMES.map((n, i) => ({
    id: i + 1, assessment_run_id: RISK_RUN, ticker: n.ticker, analyst_signal: n.signal, signal_strength: n.strength, volatility: annVol(n), rsi: null,
    anomaly_count: n.ticker === "PIEDP" ? 1 : 0, risk_score: n.risk, max_position_pct: cap(n.risk), risk_flags: n.risk >= 8 ? ["HIGH_VOLATILITY"] : [],
    notes: n.risk >= 8 ? "Volatility above 55% scores 8, which excludes the ticker." : "Within limits.",
  }));

  // ── Weekly allocation, report, audit (Saturday's book) ──
  const weights = { ACME: 14, GLOBX: 12, INITC: 8, TYRLL: 0, UMBRL: 14, STRKI: 12, HOOLI: 10, SOYLN: 5, WONKA: 10, PIEDP: 0 };
  const allocation_plans = [{
    id: 1, run_id: ALLOC_RUN, plan_date: LT_DAY, analyst_run_id: LT_RUN, risk_run_id: RISK_RUN, total_tickers: NAMES.length, invested_pct: 85, cash_pct: 15,
    strategy_note: "Sample plan: a moderate-risk week holds 15% cash, and a single enforcer correction trimmed Globex to stay under its cap.",
    allocations: weights, model: "sample", prompt_tokens: 3000, output_tokens: 2200, created_at: stamp(LT_DAY, "13:20"),
    constraint_violations: ["GLOBX: model proposed 14.5%, capped at 12.0% (risk-manager limit)"],
    sector_exposure: { ROBOTICS: 14, SEMIS: 12, SOFTWARE: 20, POWER: 14, MEGA_CAP_TECH: 10, OTHER: 15 }, theme_exposure: { TECH: 56, ENERGY: 14, INDUSTRIALS: 14 },
  }];
  const allocation_details = NAMES.map((n, i) => ({
    id: i + 1, plan_run_id: ALLOC_RUN, ticker: n.ticker, analyst_signal: n.signal, signal_strength: n.strength, risk_score: n.risk, max_position_pct: cap(n.risk),
    risk_flags: [], allocated_pct: weights[n.ticker], base_weight: weights[n.ticker], sector: n.sector,
    allocation_rationale: weights[n.ticker] ? `Signal × strength × risk gives ${weights[n.ticker]}%.` : "Excluded by the rules.",
    sizing_method: weights[n.ticker] ? (n.ticker === "GLOBX" ? "POLICY_CAPPED" : "SIGNAL_WEIGHTED") : "RISK_EXCLUDED",
  }));
  const final_reports = [{
    id: 1, run_id: REPORT_RUN, report_date: LT_DAY, alloc_run_id: ALLOC_RUN, risk_run_id: RISK_RUN, analyst_run_id: LT_RUN, market_risk_level: "MODERATE", invested_pct: 85, cash_pct: 15,
    portfolio_stance: "BALANCED", confidence_level: "MODERATE", confidence_reasoning: "Sample text: signals lean constructive, with one clear exit and one exclusion.",
    executive_summary: "Sample report. The market is in an uptrend with moderate risk. The book holds 85% across eight names and 15% in cash.",
    market_outlook: "The sample index trades above its 200-day average and volatility is contained, so the regime stays BULL.",
    key_themes: ["Trend followers leading", "One broken setup exited", "Volatility screen excludes one name"],
    risk_highlights: "Tyrell Biosystems is excluded at a risk score of 8. Pied Piper Data carries a SELL.",
    anomaly_digest: "One volume spike, on Pied Piper Data.", watchdog_notes: "Allocation totals check out: 85% invested + 15% cash = 100%.",
    disclaimer: "Sample data for demonstration. Not financial advice.", model: "sample", prompt_tokens: 5000, output_tokens: 3700, created_at: stamp(LT_DAY, "13:25"),
  }];
  const final_report_tickers = NAMES.map((n, i) => ({ id: i + 1, report_run_id: REPORT_RUN, ticker: n.ticker, one_liner: `${n.signal} on ${n.company}.`, full_narrative: WHY[n.ticker] }));
  const watchdog_logs = [{
    id: 1, run_id: WD_RUN, watchdog_date: LT_DAY, report_run_id: REPORT_RUN, alloc_run_id: ALLOC_RUN, risk_run_id: RISK_RUN, analyst_run_id: LT_RUN, overall_status: "WARNINGS", pipeline_health_score: 9,
    audit_summary: "5 of 5 rule checks passed, with 1 note for review.", signal_quality_assessment: "4 BUY, 1 SELL, 5 HOLD. All funded BUYs sit inside their caps.",
    rule_check_results: [{ rule: "ALLOCATION_SUM_TO_100", status: "PASS", finding: "85% + 15% cash = 100%." }, { rule: "SELL_TICKERS_EXCLUDED", status: "PASS", finding: "No SELL holds a weight." }, { rule: "POSITION_CAPS_RESPECTED", status: "PASS", finding: "All within caps after one correction." }],
    recommendations: ["Review why Globex keeps hitting its cap."], model: "rules-v1", prompt_tokens: 0, output_tokens: 0, created_at: stamp(LT_DAY, "13:26"),
  }];
  const watchdog_issues = [{ id: 1, watchdog_run_id: WD_RUN, issue_type: "CAP_CORRECTION", severity: "LOW", ticker: "GLOBX", description: "The model proposed 14.5% and the enforcer capped it at 12%." }];
  const agent_run_log = [["analyst", 10], ["risk", 1]].map(([agent_name, n], i) => ({ id: i + 1, run_id: RUN, agent_name, tickers_processed: n, status: "ok", started_at: at("11:30"), finished_at: at("11:35"), created_at: at("11:35") }));

  // ── Inputs ──
  const options_flow = NAMES.map((n, i) => {
    const c = Math.round(between(800, 9000)), p = Math.round(between(800, 9000));
    return { id: i + 1, run_id: RUN, ticker: n.ticker, snapshot_date: today, expiry_date: iso(daysAgo(-11)), days_to_expiry: 11, call_volume: c, put_volume: p, put_call_ratio_volume: round(p / c, 3),
             call_oi: c * 4, put_oi: p * 4, put_call_ratio_oi: round(p / c, 3), max_pain_price: round(last[n.ticker].close * 0.98), current_price: last[n.ticker].close, iv_rank: round(between(15, 75)),
             unusual_activity: [], flow_signal: p > c * 1.3 ? "BEARISH" : c > p * 1.3 ? "BULLISH" : "NEUTRAL", signal_reasoning: "Sample: compares put and call volume on the nearest monthly expiry.", prompt_tokens: 500, output_tokens: 200, created_at: at("11:28") };
  });
  const options_strikes = NAMES.flatMap(n => [0.9, 0.95, 1, 1.05, 1.1].flatMap((m, j) => ["CALL", "PUT"].map(option_type => ({
    id: `${n.ticker}${j}${option_type}`, run_id: RUN, ticker: n.ticker, snapshot_date: today, expiry_date: iso(daysAgo(-11)), strike: round(last[n.ticker].close * m), option_type,
    open_interest: Math.round(between(200, 6000)), volume: Math.round(between(20, 900)), implied_vol: round(between(0.3, 0.9), 3), delta: 0, in_the_money: option_type === "CALL" ? m < 1 : m > 1, created_at: at("11:28"),
  }))));
  const earnings_calendar = NAMES.map((n, i) => {
    const dte = 6 + i * 4;
    return { id: i + 1, run_id: RUN, ticker: n.ticker, fetch_date: today, next_earnings_date: iso(daysAgo(-dte)), days_to_earnings: dte, earnings_time: "AMC", eps_estimate: round(between(0.2, 2)), eps_actual_last: round(between(0.2, 2)),
             eps_surprise_last_pct: round(between(-8, 12)), revenue_estimate: Math.round(between(1e8, 4e9)), revenue_actual_last: Math.round(between(1e8, 4e9)), beat_streak: Math.round(between(-2, 5)), earnings_risk: dte < 10 ? "HIGH" : dte < 25 ? "MEDIUM" : "LOW",
             historical_surprises: [1, 2, 3, 4].map(q => ({ date: iso(daysAgo(q * 91)), eps_estimate: 0.8, eps_actual: round(between(0.7, 1)), surprise_pct: round(between(-8, 12)) })), claude_commentary: "Sample commentary on the last four reports.", prompt_tokens: 400, output_tokens: 160, created_at: at("11:27") };
  });
  const macro_snapshots = [{ id: 1, run_id: RUN, snapshot_date: today, macro_regime: "DISINFLATIONARY", cpi_latest: 320.4, cpi_yoy: 2.6, ppi_latest: 150.2, ppi_yoy: 2.1, regime_summary: "Sample: inflation has eased for three straight months and stays below the 3.5% threshold.", equity_impact: "NEUTRAL", prompt_tokens: 1000, output_tokens: 250, created_at: at("11:27") }];
  const macro_indicators = ["CPI", "PPI"].flatMap((indicator, k) => [...Array(14).keys()].map(i => ({
    id: k * 100 + i, run_id: RUN, series_id: `SAMPLE_${indicator}`, series_name: `${indicator} (sample)`, indicator, period_date: `${iso(daysAgo((14 - i) * 30)).slice(0, 7)}-01`, value: round((k ? 148 : 312) + i * 0.6 + between(-0.2, 0.2)), mom_change: round(between(0, 0.4), 3), yoy_change: round(3.4 - i * 0.06, 3), source: "SAMPLE", created_at: at("11:27"),
  })));
  const market_regimes = [{ id: 1, run_id: RUN, regime_date: today, regime: "BULL", risk_floor: null, spy_close: 512.4, spy_ma50: 503.2, spy_ma200: 478.9, spy_vs_ma200_pct: 7, spy_drawdown_pct: -1.8, death_cross: false, qqq_vs_ma200_pct: 8.3, vix_close: 15.6,
                            rationale: "Sample regime: the index is 7% above its 200-day average and 1.8% off its high, with volatility at 15.6.", rules_fired: [], created_at: at("11:26") }];
  const stock_fundamentals = NAMES.map((n, i) => ({ id: i + 1, run_id: RUN, ticker: n.ticker, snapshot_date: today, trailing_pe: round(between(14, 48)), forward_pe: round(between(12, 38)), peg_ratio: round(between(0.8, 3)), price_to_sales: round(between(1, 14)), price_to_book: round(between(1, 12)), dividend_yield: 0, profit_margin: round(between(0.04, 0.3), 3), revenue_growth: round(between(-0.03, 0.4), 3), earnings_growth: round(between(-0.1, 0.5), 3), beta: round(between(0.7, 1.9)), market_cap: Math.round(between(2e9, 9e10)), fifty_two_wk_high: round(last[n.ticker].close * 1.15), fifty_two_wk_low: round(last[n.ticker].close * 0.68), target_mean_price: round(last[n.ticker].close * 1.1), recommendation_key: "hold", created_at: at("11:27") }));

  // Smart money: made-up funds.
  const FUNDS = [[900001, "Sample Value Investor", "Sample Value Partners", "Value"], [900002, "Sample Growth Investor", "Sample Growth Capital", "Growth"], [900003, "Sample Activist", "Sample Activist Fund", "Activist"]];
  const q = `${iso(daysAgo(120)).slice(0, 7)}-30`;
  const smart_money_investors = FUNDS.map(([cik, manager, fund, style], i) => ({ cik, manager, fund, style, period_of_report: q, prev_period: `${iso(daysAgo(210)).slice(0, 7)}-30`, filing_date: iso(daysAgo(70)), accession: `sample-${i}`, total_value_usd: 4.2e9 - i * 1e9, positions: 5, new_count: 1, added_count: 2, reduced_count: 1, sold_count: 0, updated_at: at("11:26") }));
  const CHANGES = ["NEW", "ADDED", "UNCHANGED", "REDUCED", "ADDED"];
  const investor_holdings = FUNDS.flatMap(([cik, manager, fund], f) => NAMES.slice(f, f + 5).map((n, i) => ({
    id: f * 10 + i + 1, run_id: "sample", cik, manager, fund, period_of_report: q, filing_date: iso(daysAgo(70)), accession: `sample-${f}`, cusip: `SAMPLE${f}${i}`, ticker: n.ticker, issuer: n.company.toUpperCase(), title_class: "COM", put_call: "", shares: 500000 - i * 80000, prev_shares: 450000 - i * 70000, value_usd: (500000 - i * 80000) * last[n.ticker].close, pct_portfolio: round(24 - i * 4), share_change_pct: round(between(-20, 30)), change_type: CHANGES[i], rank: i + 1, created_at: at("11:26"),
  })));
  const investor_filings = [["ACME", "BUY", "4", 900001, 12], ["GLOBX", "BUY", "13D", 900003, 21], ["PIEDP", "SELL", "4", 900002, 30]].map(([ticker, direction, form, cik, ago], i) => {
    const f = FUNDS.find(x => x[0] === cik);
    return { id: i + 1, run_id: "sample", cik, manager: f[1], fund: f[2], form, filing_date: iso(daysAgo(ago)), accession: `sample-f${i}`, ticker, issuer: ticker, direction, shares_bought: direction === "BUY" ? 40000 : null, shares_sold: direction === "SELL" ? 25000 : null, avg_price: last[ticker].close, shares_after: 400000, pct_class: null, summary: `${direction === "BUY" ? "Bought" : "Sold"} sample shares`, url: "https://example.com/sample-filing", created_at: at("11:26") };
  });

  // ── Checks ──
  const STAGES = ["universe", "macro", "earnings", "regime", "smart_money", "pipeline", "options", "analyst", "drift", "risk", "newsletter", "backtest"];
  const pipeline_metrics = days.slice(-8).flatMap((d, r) => STAGES.map((stage_name, i) => ({ id: r * 20 + i, pipeline_run_id: `pipeline-${d}-demo01`, run_date: d, stage_name, status: "ok", duration_seconds: round(between(2, stage_name === "analyst" ? 240 : 45)), token_cost: null, created_at: stamp(d, "11:40") })));
  const signal_drift = [["PIEDP", "HOLD", "SELL", 3, "MAJOR", false], ["GLOBX", "HOLD", "BUY", 1, "MODERATE", false], ["HOOLI", "BUY", "HOLD", 9, "MODERATE", false]].map(([ticker, signal_from, signal_to, ago, flip_severity, is_chronic], i) => ({ id: i + 1, run_id: RUN, ticker, flip_date: iso(daysAgo(ago)), signal_from, signal_to, strength_from: "MODERATE", strength_to: "MODERATE", price_at_flip: last[ticker].close, flip_severity, flip_count_7d: ago < 7 ? 1 : 0, flip_count_30d: 1, is_chronic, created_at: at("11:35") }));
  const signal_drift_summary = signal_drift.map(d => ({ ticker: d.ticker, total_flips: 1, flips_7d: d.flip_count_7d, flips_30d: 1, last_flip_date: d.flip_date, ever_chronic: false, latest_signal_to: d.signal_to, latest_signal_from: d.signal_from, latest_severity: d.flip_severity }));
  const backtest_portfolio_summary = [[1, 40, 0.05, 0.07], [5, 36, 0.42, 0.33], [20, 24, 1.5, 1.1]].map(([holding_period_days, periods_tested, a, b]) => ({ holding_period_days, periods_tested, avg_portfolio_return: a, avg_spy_return: b, avg_eq_weight_return: b + 0.03, avg_alpha_vs_spy: round(a - b, 3), avg_alpha_vs_eq_weight: round(a - b - 0.03, 3), return_stddev: 1.9, sharpe_proxy: 0.4, beats_spy_count: Math.round(periods_tested * 0.55), beats_eq_weight_count: Math.round(periods_tested * 0.5) }));
  const backtest_signal_accuracy = NAMES.flatMap(n => [5, 20].map(holding_period_days => ({ ticker: n.ticker, analyst_signal: n.signal, holding_period_days, total_signals: 12, correct_signals: Math.round(between(4, 9)), accuracy_pct: round(between(38, 72)), avg_return_pct: round(between(-1, 2.5)), avg_weighted_contribution: round(between(-0.1, 0.3)), return_stddev: 2.5, worst_return_pct: -6.4, best_return_pct: 7.9 })));
  const backtest_results = days.slice(-30, -5).map((plan_date, i) => {
    const r = round(between(-1.4, 1.8), 3), s = round(between(-1, 1.3), 3);
    return { id: i + 1, backtest_run_id: "backtest-demo", alloc_run_id: `alloc-${plan_date}`, plan_date, holding_period_days: 5, exit_date: days[days.indexOf(plan_date) + 5], portfolio_return_pct: r, cash_return_pct: 0, total_return_pct: r, spy_return_pct: s, eq_weight_return_pct: s, alpha_vs_spy: round(r - s, 3), alpha_vs_eq_weight: round(r - s, 3), market_risk_level: "MODERATE", cash_pct: 15, invested_pct: 85, tickers_evaluated: 8, data_complete: true, created_at: at("11:40") };
  });
  const backtest_ticker_results = NAMES.flatMap((n, t) => days.slice(-20, -5).map((plan_date, i) => ({ id: t * 100 + i, backtest_run_id: "backtest-demo", alloc_run_id: `alloc-${plan_date}`, plan_date, ticker: n.ticker, holding_period_days: 5, allocated_pct: 8, analyst_signal: n.signal, signal_strength: n.strength, entry_price: last[n.ticker].close, exit_price: round(last[n.ticker].close * (1 + between(-0.04, 0.05))), exit_date: days[days.indexOf(plan_date) + 5], ticker_return_pct: round(between(-4, 5)), weighted_contribution: round(between(-0.3, 0.4)), signal_correct: rnd() > 0.45, created_at: at("11:40") })));

  // Simulated closed trades, so the record panel has something to show.
  const OUTCOMES = [["TARGET", 2], ["STOP", -1], ["TARGET", 2], ["TIME", 0.4], ["STOP", -1], ["STOP", -1.02], ["TARGET", 2], ["TIME", -0.3], ["STOP", -1], ["TARGET", 2]];
  const swing_backtest_trades = OUTCOMES.map(([outcome, r], i) => {
    const n = NAMES[i % 6];
    const report_date = days[days.length - 30 + i * 2];
    const exit_date = days[days.length - 25 + i * 2];
    const entry = round(last[n.ticker].close * 0.95);
    const risk = round(entry * 0.04);
    return { report_run_id: `pipeline-${report_date}-demo01`, ticker: n.ticker, report_date, signal_strength: n.strength, setup_type: i % 3 ? "PULLBACK" : "BREAKOUT", entry_price: entry, stop_price: round(entry - risk), target_price: round(entry + 2 * risk), horizon_days: 7, outcome, fill_date: report_date, fill_price: entry, exit_date, exit_price: round(entry + r * risk), hold_sessions: 4, return_pct: round((r * risk) / entry * 100), r_multiple: r, mfe_r: Math.max(r, 0.6), mae_r: Math.min(r, -0.4), backtest_run_id: "backtest-demo", updated_at: at("11:40") };
  });

  // ── The sample account: a $5,000 book ──
  const size = (ticker, pct) => { const r = agent_reports.find(x => x.ticker === ticker); const per = r.entry_price - r.stop_price; const qty = Math.max(1, Math.min(Math.floor(EQUITY * pct / 100 / per), Math.floor(EQUITY * 0.3 / r.entry_price))); return { qty, risk: round(qty * per), r }; };
  const entry = (id, ticker, pct, status, guard_reasons, note) => {
    const { qty, risk, r } = size(ticker, pct);
    return { id, plan_id: `tradeplan-${today}-demo01`, plan_date: today, ticker, side: "BUY", intent: "ENTRY", order_type: "LIMIT", time_in_force: "DAY", qty, limit_price: r.entry_price, stop_price: r.stop_price, target_price: r.target_price, horizon_days: 7, risk_dollars: risk, report_run_id: RUN, report_date: today, signal_strength: r.signal_strength, setup_type: r.setup_type, cancel_first: [], status, guard_reasons, note, broker_order_id: null, account_source: "demo", checked_at: null, created_at: at("12:40"), updated_at: at("12:40"), approved_by: null, approved_at: null };
  };
  const wonkaStop = round(last.WONKA.close * 0.94);
  const trade_orders = [
    entry(1, "ACME", 2.5, "PROPOSED", [], "STRONG PULLBACK, R:R 2, level MODERATE"),
    entry(2, "GLOBX", 1.875, "PROPOSED", [], "MODERATE BREAKOUT, R:R 2, level MODERATE"),
    entry(3, "INITC", 1.25, "BLOCKED", ["MAX_ENTRIES_TODAY"], "WEAK PULLBACK. The 2 new entries for today are taken."),
    entry(4, "TYRLL", 1.25, "BLOCKED", ["RISK_SCORE_EXCLUDED"], "WEAK PULLBACK. A risk score of 8 excludes it."),
    { id: 5, plan_id: `tradeplan-${today}-demo01`, plan_date: today, ticker: "WONKA", side: "SELL", intent: "PROTECT_STOP", order_type: "STOP", time_in_force: "GTC", qty: 6, limit_price: null, stop_price: wonkaStop, target_price: round(last.WONKA.close * 1.1), horizon_days: 7, risk_dollars: null, report_run_id: RUN, report_date: null, signal_strength: null, setup_type: null, cancel_first: [], status: "PROPOSED", guard_reasons: [], note: "held 3 sessions. A resting stop protects the position.", broker_order_id: null, account_source: "demo", checked_at: null, created_at: at("12:40"), updated_at: at("12:40"), approved_by: null, approved_at: null },
  ];
  const trading_controls = [{ id: 1, halted: false, halt_reason: null, equity_peak: EQUITY, updated_at: at("12:40") }];
  const universe_runs = NAMES.map((n, i) => ({ run_id: RUN, run_date: today, ticker: n.ticker, source: n.ticker === "WONKA" ? "HELD" : i < 7 ? "CORE" : "SLEEVE", included: true, reason: i >= 7 ? `sleeve #${i - 6}: relative strength ${(8 - i) * 2 >= 0 ? "+" : ""}${(8 - i) * 2}%` : "sizeable", rank: i + 1, price: last[n.ticker].close, atr_pct: round(last[n.ticker].atr / last[n.ticker].close * 100), dollar_vol_m: round(between(30, 900)), max_qty: 10, rs20: round(between(-4, 12)), sector: n.sector, created_at: at("11:25") }));

  // ── The sample main account: advice only ──
  const ACTIONS = { ACME: "ADD", GLOBX: "NEW", UMBRL: "HOLD", STRKI: "HOLD", HOOLI: "TRIM", SOYLN: "HOLD", WONKA: "HOLD", INITC: "HOLD", PIEDP: "EXIT", TYRLL: "AVOID" };
  const prevWeight = { ACME: 10, GLOBX: null, UMBRL: 14, STRKI: 12, HOOLI: 13, SOYLN: 5, WONKA: 10, INITC: 8, PIEDP: 6, TYRLL: null };
  const longterm_advice = NAMES.map((n, i) => ({
    id: i + 1, run_id: LT_RUN, week_of: LT_DAY, ticker: n.ticker, action: ACTIONS[n.ticker], target_pct: weights[n.ticker], prev_target_pct: prevWeight[n.ticker], basis: "HOLDINGS", conviction: ACTIONS[n.ticker] === "HOLD" ? 1 : 2, signal: n.signal, signal_strength: n.strength, valuation_view: "FAIR", risk_score: n.risk, sector: n.sector,
    why: [{ source: "Analyst", text: WHY[n.ticker] }, { source: "Risk", text: `Risk score ${n.risk} of 10.` }], wrong_if: "the analyst turns SELL or risk rises to 8+/10", changes: prevWeight[n.ticker] === weights[n.ticker] ? [] : [`target ${prevWeight[n.ticker] ?? 0}% → ${weights[n.ticker]}%`], regime: "BULL", cash_pct: 15, created_at: stamp(LT_DAY, "13:22"),
  }));
  const longterm_holdings = NAMES.filter(n => prevWeight[n.ticker]).map(n => ({ snapshot_date: LT_DAY, ticker: n.ticker, weight_pct: prevWeight[n.ticker], created_at: stamp(LT_DAY, "12:30") }));

  // ── Operations ──
  const SCHEDULE = [["pipeline", "orblex_pipeline.yml", "06:30:00", "pipeline"], ["morning_plan", "trading_jobs.yml", "08:40:00", "morning_plan"], ["dryrun", "trading_jobs.yml", "09:25:00", "dryrun"], ["fill_sync_1020", "trading_jobs.yml", "10:20:00", "fill_sync"], ["fill_sync_1320", "trading_jobs.yml", "13:20:00", "fill_sync"], ["fill_sync_1620", "trading_jobs.yml", "16:20:00", "fill_sync"], ["eod_review", "trading_jobs.yml", "16:35:00", "eod_review"]];
  const automation_schedule = [...SCHEDULE.map(([name, workflow, et_time, job]) => ({ name, workflow, inputs: { job }, et_time, days: [1, 2, 3, 4, 5], grace_min: 45, enabled: true })),
    { name: "holdings_snapshot", workflow: "trading_jobs.yml", inputs: { job: "holdings_snapshot" }, et_time: "08:30:00", days: [6], grace_min: 45, enabled: true },
    { name: "longterm_run", workflow: "orblex_pipeline.yml", inputs: { source: "cron", book: "longterm" }, et_time: "09:00:00", days: [6], grace_min: 45, enabled: true }];
  const nowMin = (() => { const p = new Date().toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour12: false }); return Number(p.slice(0, 2)) * 60 + Number(p.slice(3, 5)); })();
  const recent = days.slice(-6);
  const automation_dispatches = [], job_runs = [];
  let jid = 1;
  for (const d of recent) {
    for (const [name, , et_time, job] of SCHEDULE) {
      const m = Number(et_time.slice(0, 2)) * 60 + Number(et_time.slice(3, 5));
      if (d === today && m > nowMin) continue;
      const when = new Date(`${d}T${et_time.slice(0, 5)}:00-05:00`).toISOString();
      automation_dispatches.push({ id: jid, name, run_date: d, dispatched_at: when, request_id: jid });
      job_runs.push({ id: jid++, job, run_date: d, started_at: when, finished_at: new Date(+new Date(when) + 18e4).toISOString(), status: "OK", summary: "sample run, nothing to report", details: {}, gh_run_url: null });
    }
  }
  const daily_reviews = recent.slice(0, -1).reverse().map(d => ({
    run_date: d, summary: "{'PLACED': 1, 'BLOCKED': 2} · biggest issue: none", created_at: stamp(d, "20:35"),
    review: { date: d, pipeline: { report_at: "06:42" }, plan: { created_at: "08:40", counts: { PLACED: 1, BLOCKED: 2 }, blocked_reasons: { MAX_ENTRIES_TODAY: 1, RISK_SCORE_EXCLUDED: 1 } }, latency_median_min: { plan_to_approve: 12, approve_to_place: 3, place_to_fill: 21 },
      orders: [{ id: 1, ticker: "ACME", intent: "ENTRY", status: "PLACED", limit: 61.2, stop: 59.1, placed_before_approval: false, fill_price: null }], fills: [], safety: { unprotected: [] }, account: { total_value: EQUITY, cash: 3400, positions: [{ ticker: "WONKA", qty: 6, avg_cost: 70 }] }, dryrun_vs_actual: "Sample: the dry run and your orders match.", issues: [], biggest_issue: "none" },
  }));

  return {
    agent_reports, stock_prices, signal_history, risk_assessments, risk_ticker_details, allocation_plans, allocation_details, final_reports, final_report_tickers,
    watchdog_logs, watchdog_issues, agent_run_log, options_flow, options_strikes, earnings_calendar, macro_snapshots, macro_indicators, market_regimes, stock_fundamentals,
    smart_money_investors, investor_holdings, investor_filings, pipeline_metrics, signal_drift, signal_drift_summary, backtest_portfolio_summary, backtest_signal_accuracy,
    backtest_results, backtest_ticker_results, swing_backtest_trades, trade_orders, trading_controls, trading_mandates: [], universe_runs, longterm_advice, longterm_holdings,
    automation_schedule, automation_dispatches, job_runs, daily_reviews,
  };
}
