/**
 * SignalStory.jsx — "Why this call?" for one ticker, told as a story:
 *
 *   The verdict   one plain-English sentence: call, evidence balance, final size
 *   Step 1        what the analyst looked at — every input with its lean and a
 *                 link to the tab it came from
 *   Step 2        how the analyst decided — which prompt rules fired for this
 *                 ticker, then the analyst's own reasoning
 *   Step 3        how big a position — the deterministic sizing math with this
 *                 ticker's real numbers, then allocator + enforcer
 *
 * The BUY/SELL/HOLD call itself is made by the Claude analyst (analyst_agent.py),
 * not by a formula. The "lean" on each input is a reading guide that mirrors the
 * analyst prompt's rules — it is NOT a hidden score the model used. Step 3 is
 * different: that math is deterministic Python (run_allocator.py) and is shown
 * with the actual numbers.
 */

// Mirrors run_allocator.py — keep in sync if the Python constants change.
const SIGNAL_BASE   = { BUY: 1.0, HOLD: 0.5, SELL: 0.0 };
const STRENGTH_MULT = { STRONG: 1.0, MODERATE: 0.75, WEAK: 0.5 };
const CASH_FLOOR    = { LOW: 5, MODERATE: 10, HIGH: 20, CRITICAL: 35 };
const RISK_ORDER    = ["LOW", "MODERATE", "HIGH", "CRITICAL"];
const RISK_EXCLUDE  = 8;

const fmt    = (n, d = 2) => n == null || isNaN(n) ? "—" : Number(n).toFixed(d);
const sgn    = (n, d = 1) => n == null || isNaN(n) ? "—" : `${n > 0 ? "+" : ""}${Number(n).toFixed(d)}`;
const parseJ = v => { try { return typeof v === "string" ? JSON.parse(v) : v || []; } catch { return []; } };

const riskPenalty = r => 1 - ((r - 1) / 9) * 0.5;
const tickerScore = (signal, strength, riskScore) => {
  if (signal === "SELL" || riskScore >= RISK_EXCLUDE) return 0;
  return (SIGNAL_BASE[signal] ?? 0.5) * (STRENGTH_MULT[strength] ?? 0.75) * riskPenalty(riskScore);
};
const effectiveRisk = (level, floor) => {
  if (!floor || !RISK_ORDER.includes(floor)) return level;
  if (!RISK_ORDER.includes(level)) return floor;
  return RISK_ORDER[Math.max(RISK_ORDER.indexOf(level), RISK_ORDER.indexOf(floor))];
};

// ── Step 1: evidence ─────────────────────────────────────────────────────────
// lean: bull | bear | caution | neutral | missing
function buildEvidence({ report, indicators, earnings, options, regime, macro }) {
  const ev = [];
  const ind = indicators || {};

  if (ind.priceVsMa20 != null) {
    const p = ind.priceVsMa20;
    const cross = ind.ma5 != null && ind.ma20 != null ? (ind.ma5 > ind.ma20 ? "short-term avg above long-term" : "short-term avg below long-term") : null;
    ev.push({
      id: "trend", label: "Price trend",
      reading: `${sgn(p)}% vs 20-day average${cross ? ` · ${cross}` : ""}`,
      lean: p > 2 ? "bull" : p < -2 ? "bear" : "neutral",
      why: p > 2 ? "Trading above its recent average — uptrend." : p < -2 ? "Trading below its recent average — downtrend." : "Hugging its average — no clear trend.",
      source: "price chart above",
    });
  } else ev.push({ id: "trend", label: "Price trend", reading: "not enough history", lean: "missing" });

  if (ind.rsi14 != null) {
    const r = ind.rsi14;
    ev.push({
      id: "rsi", label: "Momentum (RSI-14)", reading: fmt(r, 1),
      lean: r >= 70 || r <= 30 ? "caution" : r > 55 ? "bull" : r < 45 ? "bear" : "neutral",
      why: r >= 70 ? "Overbought — the run may be stretched." : r <= 30 ? "Oversold — selling may be exhausted." : r > 55 ? "Buyers in control." : r < 45 ? "Sellers in control." : "Balanced.",
      source: "price chart above",
    });
  }

  if (ind.volAnn != null) {
    ev.push({
      id: "vol", label: "Volatility", reading: `${fmt(ind.volAnn, 1)}% annualised`,
      lean: ind.volAnn > 60 ? "caution" : "neutral",
      why: ind.volAnn > 60 ? "Big daily swings — lowers conviction and feeds the risk score." : "Normal range.",
      tab: "RISK",
    });
  }

  if (report?.sentiment_score != null) {
    const s = Number(report.sentiment_score);
    ev.push({
      id: "news", label: "News sentiment", reading: sgn(s, 2),
      lean: s >= 0.2 ? "bull" : s <= -0.2 ? "bear" : "neutral",
      why: "From the last 7 days of headlines (or price action when there is no news). News can strengthen or weaken the technical read.",
      source: "gauge above",
    });
  }

  const val = report?.valuation_view;
  ev.push({
    id: "val", label: "Valuation", reading: val || "N/A",
    lean: val === "UNDERVALUED" ? "bull" : val === "OVERVALUED" ? "bear" : val === "FAIR" ? "neutral" : "missing",
    why: val && val !== "N/A" ? "Analyst's read of P/E, PEG, P/S and analyst targets." : "No fundamentals (ETF or no snapshot).",
    source: "Fundamentals panel →",
  });

  if (earnings?.days_to_earnings != null) {
    const d = earnings.days_to_earnings;
    ev.push({
      id: "earn", label: "Earnings", reading: `in ${d} day${d === 1 ? "" : "s"} (${earnings.next_earnings_date || "date TBD"})`,
      lean: d <= 5 ? "caution" : "neutral",
      why: d <= 5 ? "Within 5 days of a report — a coin-flip event. Rules cap strength at MODERATE." : d <= 14 ? "Report coming up soon — watch it." : "Far enough away not to matter.",
      tab: "EARNINGS",
    });
  } else ev.push({ id: "earn", label: "Earnings", reading: "no date on record", lean: "missing", tab: "EARNINGS" });

  if (options?.flow_signal) {
    const f = options.flow_signal;
    ev.push({
      id: "flow", label: "Options flow", reading: `${f} · put/call ${fmt(options.put_call_ratio_volume, 2)}`,
      lean: f === "BULLISH" ? "bull" : f === "BEARISH" ? "bear" : "neutral",
      why: "What options traders are betting on. Adjusts conviction but never flips a call on its own.",
      tab: "OPTIONS",
    });
  } else ev.push({ id: "flow", label: "Options flow", reading: "no options data", lean: "missing", tab: "OPTIONS" });

  if (regime?.regime) {
    const g = regime.regime;
    ev.push({
      id: "regime", label: "Market regime", reading: `${g}${regime.risk_floor ? ` · risk floor ${regime.risk_floor}` : ""}`,
      lean: g === "BULL" ? "bull" : g === "CORRECTION" || g === "BEAR" ? "bear" : "neutral",
      why: g === "CORRECTION" || g === "BEAR" ? "Weak market — be more careful with new buys across the board." : "The backdrop for every ticker, the same for all of them.",
      tab: "REGIME",
    });
  }

  if (macro?.equity_impact) {
    const m = macro.equity_impact;
    ev.push({
      id: "macro", label: "Macro", reading: `${macro.macro_regime || "—"} · equity impact ${m}`,
      lean: m === "POSITIVE" ? "bull" : m === "NEGATIVE" ? "bear" : "neutral",
      why: "Inflation backdrop (CPI/PPI). A hostile reading makes new buys more conservative.",
      tab: "MACRO",
    });
  }
  return ev;
}

// ── Step 2: which prompt rules fired for this ticker ─────────────────────────
function firedRules({ report, earnings, options, regime, macro, prior }) {
  const sig = report?.signal;
  const out = [];
  if (earnings?.days_to_earnings != null && earnings.days_to_earnings <= 5)
    out.push({ tone: "caution", text: `Earnings in ${earnings.days_to_earnings}d → strength is capped at MODERATE (a pre-earnings position is a bet on the announcement, not the trend).`, tab: "EARNINGS" });
  if (options?.flow_signal && sig && ((sig === "BUY" && options.flow_signal === "BEARISH") || (sig === "SELL" && options.flow_signal === "BULLISH")))
    out.push({ tone: "caution", text: `Options flow is ${options.flow_signal} against a ${sig} → lower strength, but flow alone never flips the call.`, tab: "OPTIONS" });
  if (options?.flow_signal && sig && ((sig === "BUY" && options.flow_signal === "BULLISH") || (sig === "SELL" && options.flow_signal === "BEARISH")))
    out.push({ tone: "support", text: `Options flow (${options.flow_signal}) agrees with the ${sig} → supports conviction.`, tab: "OPTIONS" });
  if (sig === "BUY" && (regime?.regime === "CORRECTION" || regime?.regime === "BEAR"))
    out.push({ tone: "caution", text: `${regime.regime} regime → be conservative with new BUYs.`, tab: "REGIME" });
  if (sig === "BUY" && macro?.equity_impact === "NEGATIVE")
    out.push({ tone: "caution", text: "Macro equity impact is NEGATIVE → be conservative with new BUYs.", tab: "MACRO" });
  if (sig && report?.sentiment_score != null) {
    const s = Number(report.sentiment_score);
    if ((sig === "BUY" && s <= -0.3) || (sig === "SELL" && s >= 0.3))
      out.push({ tone: "caution", text: `News sentiment (${sgn(s, 2)}) goes against the ${sig} → should bring strength down.` });
  }
  if (prior && sig) {
    if (prior.signal !== sig)
      out.push({ tone: "flip", text: `Reverses the prior ${prior.signal} call (held ${prior.sessions} session${prior.sessions === 1 ? "" : "s"}). The rules require the reasoning to name what changed.`, tab: "DRIFT" });
    else
      out.push({ tone: "support", text: `Same call as before (${prior.signal}, ${prior.sessions} session${prior.sessions === 1 ? "" : "s"} running) → consistency rule: don't flip on noise.`, tab: "DRIFT" });
  }
  return out;
}

const LEAN_META = {
  bull:    { label: "BULLISH", cls: "ev-bull" },
  bear:    { label: "BEARISH", cls: "ev-bear" },
  caution: { label: "CAUTION", cls: "ev-caution" },
  neutral: { label: "NEUTRAL", cls: "ev-neutral" },
  missing: { label: "NO DATA", cls: "ev-missing" },
};

function GoLink({ tab, ticker, onNavigate, children }) {
  if (!tab || !onNavigate) return null;
  return (
    <button className="story-link" onClick={() => onNavigate(tab, ticker)}
            aria-label={`Open ${tab} tab${ticker ? ` for ${ticker}` : ""}`}>
      {children || `${tab} →`}
    </button>
  );
}

export default function SignalStory({
  ticker, report, indicators, earnings, options, regime, macro, prior,
  risk, alloc, allReports, allRisk, riskPlan, enforcerNotes, enforcerRan,
  methodChipCls, onNavigate,
}) {
  if (!report) return null;
  const sig      = report.signal;
  const strength = report.signal_strength;

  const evidence = buildEvidence({ report, indicators, earnings, options, regime, macro });
  const counted  = evidence.filter(e => e.lean !== "missing");
  const nBull    = counted.filter(e => e.lean === "bull").length;
  const nBear    = counted.filter(e => e.lean === "bear").length;
  const nCaut    = counted.filter(e => e.lean === "caution").length;
  const rules    = firedRules({ report, earnings, options, regime, macro, prior });

  // ── Step 3 numbers ──
  const riskScore = risk?.risk_score != null ? Number(risk.risk_score) : null;
  const score     = riskScore != null ? tickerScore(sig, strength, riskScore) : null;
  const riskById  = Object.fromEntries((allRisk || []).map(r => [r.ticker, Number(r.risk_score)]));
  const totalScore = (allReports || []).reduce((s, r) =>
    riskById[r.ticker] != null ? s + tickerScore(r.signal, r.signal_strength, riskById[r.ticker]) : s, 0);
  const effRisk   = effectiveRisk(riskPlan?.market_risk_level || riskPlan?.portfolio_risk_rating || "MODERATE", regime?.risk_floor);
  const cashFloor = CASH_FLOOR[effRisk] ?? 10;
  const share     = score != null && totalScore > 0 ? score / totalScore * (100 - cashFloor) : null;
  const cap       = risk?.max_position_pct != null ? Number(risk.max_position_pct) : null;
  const base      = alloc?.base_weight != null ? Number(alloc.base_weight) : null;
  const final     = alloc?.allocated_pct != null ? Number(alloc.allocated_pct) : null;
  const delta     = base != null && final != null ? final - base : null;
  const excludedReason = sig === "SELL" ? "SELL calls are never held"
                       : riskScore != null && riskScore >= RISK_EXCLUDE ? `risk ${riskScore}/10 is at or above the exclusion line of ${RISK_EXCLUDE}`
                       : null;

  // ── Verdict sentence ──
  const lean = nBull > nBear + 1 ? "leaned bullish" : nBear > nBull + 1 ? "leaned bearish" : "was mixed";
  const sizePart = final == null ? "It has no allocation row in the latest plan."
    : final === 0 ? `It holds 0% of the portfolio${excludedReason ? ` — ${excludedReason}` : alloc?.sizing_method ? ` (${alloc.sizing_method.replace(/_/g, " ").toLowerCase()})` : ""}.`
    : `Risk scored it ${riskScore ?? "—"}/10${cap != null ? ` (cap ${fmt(cap, 1)}%)` : ""}, and it ends up at ${fmt(final)}% of the portfolio.`;
  const flipPart = prior && prior.signal !== sig ? ` This reverses the prior ${prior.signal}.` : "";

  return (
    <section className="story" aria-labelledby="story-title">
      <div className="story-head">
        <div id="story-title" className="section-label" style={{ marginBottom: 0 }}>WHY {sig} ON {ticker}?</div>
        <div className="story-legend">3 steps: inputs → decision → position size</div>
      </div>

      <div className={`story-verdict story-verdict-${sig.toLowerCase()}`}>
        <strong>{sig} {strength}.</strong> The evidence {lean}: {nBull} bullish, {nBear} bearish{nCaut ? `, ${nCaut} caution` : ""} out of {counted.length} inputs. {sizePart}{flipPart}
      </div>

      {/* ── Step 1 ── */}
      <div className="story-step">
        <div className="story-step-head"><span className="story-num">1</span>WHAT THE ANALYST LOOKED AT</div>
        <div className="story-balance" role="img" aria-label={`${nBull} bullish, ${nBear} bearish, ${nCaut} caution inputs`}>
          {nBull > 0 && <div className="bal-bull" style={{ flex: nBull }} />}
          {nCaut > 0 && <div className="bal-caution" style={{ flex: nCaut }} />}
          {counted.length - nBull - nBear - nCaut > 0 && <div className="bal-neutral" style={{ flex: counted.length - nBull - nBear - nCaut }} />}
          {nBear > 0 && <div className="bal-bear" style={{ flex: nBear }} />}
        </div>
        <ul className="ev-list">
          {evidence.map(e => {
            const m = LEAN_META[e.lean];
            return (
              <li className="ev-row" key={e.id}>
                <span className={`ev-lean ${m.cls}`}>{m.label}</span>
                <div className="ev-body">
                  <div className="ev-top">
                    <span className="ev-label">{e.label}</span>
                    <span className="ev-reading">{e.reading}</span>
                  </div>
                  {e.why && <div className="ev-why">{e.why}</div>}
                </div>
                <div className="ev-src">
                  {e.tab ? <GoLink tab={e.tab} ticker={ticker} onNavigate={onNavigate} /> : e.source ? <span className="ev-src-text">{e.source}</span> : null}
                </div>
              </li>
            );
          })}
        </ul>
        <div className="story-note">
          The labels are a reading guide based on the analyst's rules. The analyst isn't adding up scores.
        </div>
      </div>

      {/* ── Step 2 ── */}
      <div className="story-step">
        <div className="story-step-head"><span className="story-num">2</span>HOW THE ANALYST DECIDED</div>
        <p className="story-p">
          The Claude analyst reads all of the inputs above together and picks <b>BUY, SELL or HOLD</b> and a strength of <b>STRONG, MODERATE or WEAK</b>.
          Technicals set the direction. News, earnings, options flow and the market backdrop move conviction up or down.
        </p>
        {rules.length > 0 ? (
          <ul className="rule-list">
            {rules.map((r, i) => (
              <li key={i} className={`rule-row rule-${r.tone}`}>
                <span className="rule-dot" aria-hidden="true" />
                <span className="rule-text">{r.text}</span>
                <GoLink tab={r.tab} ticker={ticker} onNavigate={onNavigate} />
              </li>
            ))}
          </ul>
        ) : (
          <div className="story-note" style={{ marginBottom: 10 }}>No special rules fired, so the call rests on the technicals and the news.</div>
        )}
        {report.reasoning && (
          <blockquote className="story-quote">
            <div className="story-quote-label">ANALYST'S REASONING</div>
            {report.reasoning}
          </blockquote>
        )}
      </div>

      {/* ── Step 3 ── */}
      <div className="story-step">
        <div className="story-step-head"><span className="story-num">3</span>HOW BIG A POSITION</div>
        <p className="story-p">Sizing is plain Python arithmetic (no AI), then the allocator fine-tunes it and the enforcer checks the result.</p>

        <table className="math-table">
          <tbody>
            <tr><td>Signal</td><td>{sig}</td><td className="math-n">{fmt(SIGNAL_BASE[sig], 2)}</td></tr>
            <tr><td>× Strength</td><td>{strength}</td><td className="math-n">{fmt(STRENGTH_MULT[strength], 2)}</td></tr>
            <tr>
              <td>× Risk penalty</td>
              <td>{riskScore != null ? <>risk {riskScore}/10 → 1 − ({riskScore}−1)/9 × 0.5 <GoLink tab="RISK" ticker={ticker} onNavigate={onNavigate} /></> : "not assessed"}</td>
              <td className="math-n">{riskScore != null ? fmt(riskPenalty(riskScore), 2) : "—"}</td>
            </tr>
            <tr className="math-total">
              <td>= Score</td>
              <td>{excludedReason ? `0: ${excludedReason}` : "this ticker's claim on the investable budget"}</td>
              <td className="math-n">{score != null ? fmt(score, 3) : "—"}</td>
            </tr>
            {score != null && score > 0 && share != null && (
              <tr>
                <td>Share of budget</td>
                <td>{fmt(score, 3)} ÷ {fmt(totalScore, 3)} (all scores) × {100 - cashFloor}% investable <span className="math-dim">({cashFloor}% cash floor at {effRisk} risk)</span></td>
                <td className="math-n">≈{fmt(share)}%</td>
              </tr>
            )}
            {cap != null && score > 0 && (
              <tr><td>Risk cap</td><td>no single position above the Risk Manager's max</td><td className="math-n">≤{fmt(cap, 1)}%</td></tr>
            )}
            <tr className="math-total">
              <td>= Base weight</td>
              <td>
                after caps, regime policy and re-normalisation (stored)
                {base == null && alloc && <div className="math-dim" style={{ marginTop: 3 }}>not stored for this plan: the base_weight column migration hasn't been applied yet</div>}
              </td>
              <td className="math-n">{base != null ? `${fmt(base)}%` : "—"}</td>
            </tr>
            <tr>
              <td>Allocator</td>
              <td>
                Claude adjusts the base weight within the caps
                {alloc?.sizing_method && <span className={`method-chip ${methodChipCls ? methodChipCls(alloc.sizing_method) : ""}`} style={{ marginLeft: 6 }}>{alloc.sizing_method}</span>}
                {" "}<GoLink tab="ALLOCATION" ticker={ticker} onNavigate={onNavigate} />
                {alloc?.allocation_rationale && <div className="math-dim" style={{ marginTop: 3 }}>{alloc.allocation_rationale}</div>}
              </td>
              <td className="math-n">{delta != null ? <span className={delta > 0.05 ? "pos" : delta < -0.05 ? "neg" : ""}>{sgn(delta, 2)}pp</span> : "—"}</td>
            </tr>
            <tr>
              <td>Enforcer</td>
              <td>
                {!enforcerRan ? <span style={{ color: "var(--amber)" }}>not verified: the enforcer did not run for this plan</span>
                 : enforcerNotes.length ? enforcerNotes.map((v, i) => <div key={i} style={{ color: "var(--red)" }}>{v}</div>)
                 : <span style={{ color: "var(--green)" }}>passed every cap, floor and exclusion</span>}
              </td>
              <td className="math-n">{enforcerRan ? (enforcerNotes.length ? "clamped" : "✓") : "?"}</td>
            </tr>
            <tr className="math-total math-final">
              <td>= Final weight</td>
              <td>share of the portfolio</td>
              <td className="math-n">{final != null ? `${fmt(final)}%` : "—"}</td>
            </tr>
          </tbody>
        </table>
        {base != null && share != null && score > 0 && Math.abs(Math.min(share, cap ?? Infinity) - base) > 1 && (
          <div className="story-note">
            The ≈{fmt(share)}% estimate and the {fmt(base)}% base weight differ. Budget freed by other names' caps gets
            re-spread, regime policy can boost or exclude a name, and a ticker that flips often gets a strength haircut.
          </div>
        )}
      </div>
    </section>
  );
}

export const STORY_CSS = `
  .story{margin-top:20px;border-top:1px solid var(--border);padding-top:16px;}
  .story-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
  .story-legend{font-family:var(--mono);font-size:9px;color:var(--text-dimmer);letter-spacing:.06em;}
  .story-verdict{font-size:13px;line-height:1.6;color:var(--text);padding:12px 14px;border-radius:var(--radius);background:var(--bg3);border-left:3px solid var(--blue);margin-bottom:18px;}
  .story-verdict-buy{border-left-color:var(--green);} .story-verdict-sell{border-left-color:var(--red);}
  .story-verdict strong{font-family:var(--mono);font-size:12px;letter-spacing:.04em;}
  .story-step{margin-bottom:22px;}
  .story-step-head{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--amber);margin-bottom:10px;}
  .story-num{width:18px;height:18px;border-radius:50%;border:1px solid var(--border-amber);display:inline-flex;align-items:center;justify-content:center;font-size:10px;}
  .story-p{font-size:12px;color:var(--text-dim);line-height:1.6;margin-bottom:10px;}
  .story-p b{color:var(--text);font-weight:500;}
  .story-note{font-size:10px;color:var(--text-dimmer);line-height:1.5;margin-top:8px;font-style:italic;}
  .story-link{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.06em;color:var(--amber);padding:2px 6px;border:1px solid var(--border-amber);border-radius:2px;white-space:nowrap;margin-left:4px;}
  .story-link:hover{background:var(--amber-glow);}

  .story-balance{display:flex;height:6px;border-radius:3px;overflow:hidden;gap:2px;margin-bottom:10px;}
  .bal-bull{background:var(--green);} .bal-bear{background:var(--red);} .bal-caution{background:var(--amber);} .bal-neutral{background:var(--bg4);}
  .ev-list{list-style:none;display:flex;flex-direction:column;gap:1px;background:var(--border);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;}
  .ev-row{display:grid;grid-template-columns:72px 1fr auto;gap:10px;align-items:start;padding:8px 10px;background:var(--bg3);}
  .ev-lean{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.05em;padding:2px 0;text-align:center;border-radius:2px;border:1px solid;}
  .ev-bull{color:var(--green);background:var(--green-dim);border-color:rgba(60,174,111,.3);}
  .ev-bear{color:var(--red);background:var(--red-dim);border-color:rgba(226,75,74,.3);}
  .ev-caution{color:var(--amber);background:var(--amber-glow);border-color:var(--border-amber);}
  .ev-neutral{color:var(--text-dim);background:var(--bg4);border-color:var(--border);}
  .ev-missing{color:var(--text-dimmer);background:transparent;border-color:var(--border);border-style:dashed;}
  .ev-top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;}
  .ev-label{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--text);}
  .ev-reading{font-family:var(--mono);font-size:11px;color:var(--text-dim);}
  .ev-why{font-size:11px;color:var(--text-dimmer);line-height:1.45;margin-top:2px;}
  .ev-src-text{font-family:var(--mono);font-size:9px;color:var(--text-dimmer);white-space:nowrap;}

  .rule-list{list-style:none;display:flex;flex-direction:column;gap:6px;margin-bottom:12px;}
  .rule-row{display:flex;align-items:flex-start;gap:8px;font-size:12px;color:var(--text-dim);line-height:1.5;}
  .rule-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:6px;background:var(--text-dimmer);}
  .rule-caution .rule-dot{background:var(--amber);} .rule-support .rule-dot{background:var(--green);} .rule-flip .rule-dot{background:var(--red);}
  .rule-text{flex:1;}
  .story-quote{background:var(--bg3);border-left:2px solid var(--amber-dim);padding:10px 14px;border-radius:0 var(--radius) var(--radius) 0;font-size:12px;color:var(--text-dim);line-height:1.65;}
  .story-quote-label{font-family:var(--mono);font-size:9px;color:var(--amber);letter-spacing:.08em;margin-bottom:5px;}

  .math-table{width:100%;border-collapse:collapse;font-size:11px;}
  .math-table td{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:top;color:var(--text-dim);line-height:1.5;}
  .math-table td:first-child{font-family:var(--mono);color:var(--text-dimmer);white-space:nowrap;width:110px;}
  .math-n{font-family:var(--mono);text-align:right;color:var(--text);white-space:nowrap;width:80px;}
  .math-total td{border-top:1px solid var(--border);color:var(--text);}
  .math-total td:first-child{color:var(--amber);}
  .math-final td{font-weight:700;font-size:12px;}
  .math-dim{color:var(--text-dimmer);font-size:10px;}
  @media(max-width:680px){
    .ev-row{grid-template-columns:64px 1fr;}
    .ev-src{grid-column:2;}
    .math-table td:first-child{width:auto;}
  }
`;
