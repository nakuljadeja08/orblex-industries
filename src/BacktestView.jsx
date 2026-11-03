// BacktestView.jsx — Orblex Industries Backtesting Dashboard Panel
// Reads from: backtest_results, backtest_ticker_results,
//             backtest_portfolio_summary (view), backtest_signal_accuracy (view)

import { useState, useEffect, useCallback } from "react";

// ── CSS — injected into Dashboard.jsx's <style> tag ──────────────────────────
export const BACKTEST_CSS = `
  .bt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--border); }
  .bt-full  { grid-column: 1 / -1; }

  /* Period tabs */
  .bt-period-tabs { display: flex; gap: 1px; background: var(--border); margin-bottom: 1px; }
  .bt-period-tab  { flex: 1; padding: 8px; text-align: center; font-family: var(--mono); font-size: 11px;
                    letter-spacing: .06em; cursor: pointer; background: var(--bg2); color: var(--text-dimmer);
                    transition: all .12s; }
  .bt-period-tab:hover  { background: var(--bg3); color: var(--text-dim); }
  .bt-period-tab.active { background: var(--bg3); color: var(--amber); border-bottom: 2px solid var(--amber); }

  /* Summary cards */
  .bt-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--border); }
  .bt-card  { background: var(--bg2); padding: 16px; }

  /* Performance table */
  .bt-table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
  .bt-table th { color: var(--text-dimmer); font-weight: 400; padding: 6px 10px; text-align: left;
                 border-bottom: 1px solid var(--border); font-size: 10px; letter-spacing: .05em; }
  .bt-table td { padding: 7px 10px; border-bottom: 1px solid rgba(255,255,255,.03); color: var(--text-dim); }
  .bt-table tr:hover td { background: rgba(255,255,255,.02); }

  /* Return coloring */
  .ret-pos  { color: var(--green); }
  .ret-neg  { color: var(--red); }
  .ret-neu  { color: var(--text-dim); }

  /* Alpha badge */
  .alpha-badge { display: inline-block; font-family: var(--mono); font-size: 10px;
                 padding: 2px 6px; border-radius: 2px; }
  .alpha-pos { background: var(--green-dim); color: var(--green); border: 1px solid rgba(60,174,111,.3); }
  .alpha-neg { background: var(--red-dim);   color: var(--red);   border: 1px solid rgba(226,75,74,.3); }

  /* Accuracy bar */
  .acc-bar-wrap { display: flex; align-items: center; gap: 8px; }
  .acc-bar-bg   { flex: 1; height: 4px; background: rgba(255,255,255,.06); border-radius: 2px; }
  .acc-bar-fill { height: 100%; border-radius: 2px; transition: width .3s; }

  /* Signal chip */
  .sig-chip { font-family: var(--mono); font-size: 10px; padding: 2px 7px; border-radius: 2px; font-weight: 700; }
  .sig-buy  { background: var(--green-dim); color: var(--green); }
  .sig-sell { background: var(--red-dim);   color: var(--red);   }
  .sig-hold { background: var(--blue-dim);  color: var(--blue);  }

  /* Empty state */
  .bt-empty { font-family: var(--mono); font-size: 11px; color: var(--text-dimmer);
              padding: 40px; text-align: center; background: var(--bg2); }

  /* Sharpe proxy */
  .sharpe-good { color: var(--green); }
  .sharpe-bad  { color: var(--red); }
  .sharpe-neu  { color: var(--text-dim); }
`;

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt    = (v, d = 2) => v == null ? "—" : Number(v).toFixed(d);
const fmtPct = v => v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
const retCls = v => v == null ? "ret-neu" : v > 0 ? "ret-pos" : v < 0 ? "ret-neg" : "ret-neu";
const sigCls = s => s === "BUY" ? "sig-buy" : s === "SELL" ? "sig-sell" : "sig-hold";

// ── Main Component ────────────────────────────────────────────────────────────
export default function BacktestView({ supabase }) {
  const [period, setPeriod]         = useState(5);
  const [summary, setSummary]       = useState([]);
  const [results, setResults]       = useState([]);
  const [accuracy, setAccuracy]     = useState([]);
  const [tickerRows, setTickerRows] = useState([]);
  const [loading, setLoading]       = useState(true);
  const [hasData, setHasData]       = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const { data: sumData } = await supabase
        .from("backtest_portfolio_summary")
        .select("*")
        .order("holding_period_days");

      const { data: accData } = await supabase
        .from("backtest_signal_accuracy")
        .select("*")
        .order("holding_period_days")
        .order("accuracy_pct", { ascending: false });

      setSummary(sumData || []);
      setAccuracy(accData || []);
      setHasData((sumData || []).length > 0);
    } finally {
      setLoading(false);
    }
  }, [supabase]);

  const fetchPeriod = useCallback(async (n) => {
    const { data: resData } = await supabase
      .from("backtest_results")
      .select("*")
      .eq("holding_period_days", n)
      .eq("data_complete", true)
      .order("plan_date", { ascending: false })
      .limit(30);

    const { data: tkData } = await supabase
      .from("backtest_ticker_results")
      .select("*")
      .eq("holding_period_days", n)
      .not("analyst_signal", "eq", "SELL")
      .order("plan_date", { ascending: false })
      .limit(100);

    setResults(resData || []);
    setTickerRows(tkData || []);
  }, [supabase]);

  useEffect(() => { fetchAll(); }, [fetchAll]);
  useEffect(() => { if (hasData) fetchPeriod(period); }, [period, hasData, fetchPeriod]);

  const activeSummary  = summary.find(s => s.holding_period_days === period);
  const activeAccuracy = accuracy.filter(a => a.holding_period_days === period);

  if (loading) return (
    <div className="bt-empty">// loading backtest data...</div>
  );

  if (!hasData) return (
    <div className="bt-empty">
      // no backtest data yet — run: <span style={{ color: "var(--amber)" }}>python run_backtest.py</span>
    </div>
  );

  return (
    <div>
      {/* ── Period selector ──────────────────────────────────────────────────── */}
      <div className="bt-period-tabs">
        {[1, 5, 20].map(n => (
          <div
            key={n}
            className={`bt-period-tab${period === n ? " active" : ""}`}
            onClick={() => setPeriod(n)}
          >
            {n === 1 ? "1D — NEXT DAY" : n === 5 ? "5D — 1 WEEK" : "20D — 1 MONTH"}
          </div>
        ))}
      </div>

      {/* ── Summary cards ────────────────────────────────────────────────────── */}
      {activeSummary && (
        <div className="bt-cards">
          {[
            {
              label: "AVG PORTFOLIO",
              value: fmtPct(activeSummary.avg_portfolio_return),
              cls:   retCls(activeSummary.avg_portfolio_return),
              sub:   `${activeSummary.periods_tested} periods`,
            },
            {
              label: "AVG SPY",
              value: fmtPct(activeSummary.avg_spy_return),
              cls:   retCls(activeSummary.avg_spy_return),
              sub:   "S&P 500 benchmark",
            },
            {
              label: "ALPHA vs SPY",
              value: fmtPct(activeSummary.avg_alpha_vs_spy),
              cls:   retCls(activeSummary.avg_alpha_vs_spy),
              sub:   `beats SPY: ${activeSummary.beats_spy_count}/${activeSummary.periods_tested}`,
            },
            {
              label: "SHARPE PROXY",
              value: activeSummary.sharpe_proxy != null ? fmt(activeSummary.sharpe_proxy, 2) : "—",
              cls:   activeSummary.sharpe_proxy > 1 ? "sharpe-good"
                   : activeSummary.sharpe_proxy < 0 ? "sharpe-bad"
                   : "sharpe-neu",
              sub:   "return / stddev (no rf)",
            },
          ].map(c => (
            <div key={c.label} className="bt-card">
              <div className="metric-label">{c.label}</div>
              <div className={`metric-value ${c.cls}`} style={{ fontSize: 22 }}>{c.value}</div>
              <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginTop: 4 }}>{c.sub}</div>
            </div>
          ))}
        </div>
      )}

      <div className="bt-grid" style={{ marginTop: 1 }}>

        {/* ── Per-run results table ─────────────────────────────────────────── */}
        <div style={{ background: "var(--bg2)", padding: 20 }}>
          <div className="section-label" style={{ marginBottom: 12 }}>
            ALLOCATION RUN PERFORMANCE ({period}D HOLD)
          </div>
          {results.length === 0 ? (
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dimmer)" }}>
              No complete results for this period.
            </div>
          ) : (
            <table className="bt-table">
              <thead>
                <tr>
                  <th>DATE</th><th>PORTFOLIO</th><th>SPY</th><th>EQ-WT</th>
                  <th>α SPY</th><th>RISK</th><th>CASH</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={i}>
                    <td style={{ color: "var(--amber)" }}>{r.plan_date}</td>
                    <td className={retCls(r.total_return_pct)}>{fmtPct(r.total_return_pct)}</td>
                    <td className={retCls(r.spy_return_pct)}>{fmtPct(r.spy_return_pct)}</td>
                    <td className={retCls(r.eq_weight_return_pct)}>{fmtPct(r.eq_weight_return_pct)}</td>
                    <td>
                      <span className={`alpha-badge ${r.alpha_vs_spy >= 0 ? "alpha-pos" : "alpha-neg"}`}>
                        {fmtPct(r.alpha_vs_spy)}
                      </span>
                    </td>
                    <td style={{
                      color: r.market_risk_level === "CRITICAL" ? "var(--red)"
                           : r.market_risk_level === "HIGH"     ? "var(--amber)"
                           : "var(--text-dim)",
                      fontFamily: "var(--mono)", fontSize: 10,
                    }}>
                      {r.market_risk_level || "—"}
                    </td>
                    <td>{r.cash_pct != null ? `${r.cash_pct}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* ── Signal accuracy ───────────────────────────────────────────────── */}
        <div style={{ background: "var(--bg2)", padding: 20 }}>
          <div className="section-label" style={{ marginBottom: 12 }}>
            SIGNAL ACCURACY ({period}D HOLD)
          </div>
          {activeAccuracy.length === 0 ? (
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dimmer)" }}>
              No signal accuracy data for this period.
            </div>
          ) : (
            <table className="bt-table">
              <thead>
                <tr>
                  <th>TICKER</th><th>SIGNAL</th><th>ACCURACY</th>
                  <th>AVG RETURN</th><th>SIGNALS</th>
                </tr>
              </thead>
              <tbody>
                {activeAccuracy
                  .filter(a => a.analyst_signal !== "SELL")
                  .map((a, i) => {
                    const acc    = parseFloat(a.accuracy_pct) || 0;
                    const barClr = acc >= 60 ? "var(--green)" : acc >= 40 ? "var(--amber)" : "var(--red)";
                    return (
                      <tr key={i}>
                        <td style={{ color: "var(--amber)", fontWeight: 700 }}>{a.ticker}</td>
                        <td><span className={`sig-chip ${sigCls(a.analyst_signal)}`}>{a.analyst_signal}</span></td>
                        <td>
                          <div className="acc-bar-wrap">
                            <div className="acc-bar-bg">
                              <div className="acc-bar-fill" style={{ width: `${acc}%`, background: barClr }} />
                            </div>
                            <span style={{ fontFamily: "var(--mono)", fontSize: 11,
                              color: barClr, minWidth: 36, textAlign: "right" }}>
                              {fmt(acc, 0)}%
                            </span>
                          </div>
                        </td>
                        <td className={retCls(a.avg_return_pct)}>{fmtPct(a.avg_return_pct)}</td>
                        <td style={{ color: "var(--text-dimmer)" }}>{a.total_signals}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          )}
        </div>

        {/* ── Per-ticker trade log ──────────────────────────────────────────── */}
        <div className="bt-full" style={{ background: "var(--bg2)", padding: 20 }}>
          <div className="section-label" style={{ marginBottom: 12 }}>
            PER-TICKER TRADE LOG ({period}D HOLD)
          </div>
          {tickerRows.length === 0 ? (
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dimmer)" }}>
              No ticker data for this period.
            </div>
          ) : (
            <table className="bt-table">
              <thead>
                <tr>
                  <th>DATE</th><th>TICKER</th><th>SIGNAL</th><th>ALLOC</th>
                  <th>ENTRY</th><th>EXIT</th><th>RETURN</th><th>CONTRIBUTION</th><th>CORRECT</th>
                </tr>
              </thead>
              <tbody>
                {tickerRows.slice(0, 50).map((t, i) => (
                  <tr key={i}>
                    <td style={{ color: "var(--text-dimmer)" }}>{t.plan_date}</td>
                    <td style={{ color: "var(--amber)", fontWeight: 700, fontFamily: "var(--mono)" }}>{t.ticker}</td>
                    <td><span className={`sig-chip ${sigCls(t.analyst_signal)}`}>{t.analyst_signal}</span></td>
                    <td>{t.allocated_pct != null ? `${t.allocated_pct}%` : "—"}</td>
                    <td>${fmt(t.entry_price)}</td>
                    <td>${fmt(t.exit_price)}</td>
                    <td className={retCls(t.ticker_return_pct)}>{fmtPct(t.ticker_return_pct)}</td>
                    <td className={retCls(t.weighted_contribution)}>{fmtPct(t.weighted_contribution)}</td>
                    <td>
                      {t.signal_correct === null ? (
                        <span style={{ color: "var(--text-dimmer)", fontFamily: "var(--mono)", fontSize: 10 }}>N/A</span>
                      ) : t.signal_correct ? (
                        <span style={{ color: "var(--green)", fontFamily: "var(--mono)", fontSize: 12 }}>✓</span>
                      ) : (
                        <span style={{ color: "var(--red)", fontFamily: "var(--mono)", fontSize: 12 }}>✗</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

      </div>
    </div>
  );
}
