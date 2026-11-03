// SmartMoneyView.jsx — famous investors' portfolios and latest moves
// Reads from: smart_money_investors, investor_holdings, investor_filings
// (all written by run_smart_money.py from SEC EDGAR).

import { useState, useEffect, useMemo } from "react";

// Funds often hold a different share class than the universe tracks (GOOGL vs
// GOOG). Mirrors context_builder.SHARE_CLASS_ALIASES so the tab and the analyst
// see the same overlap.
const CLASS_ALIASES = { GOOG: ["GOOG", "GOOGL"], GOOGL: ["GOOG", "GOOGL"], "BRK-A": ["BRK-A", "BRK-B"], "BRK-B": ["BRK-A", "BRK-B"] };
export const aliasesFor = t => CLASS_ALIASES[t] || [t];

const fmtUsd = v => v == null ? "—"
  : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B`
  : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M`
  : `$${Math.round(v / 1e3)}K`;
const fmtSh = v => v == null ? "—"
  : v >= 1e6 ? `${(v / 1e6).toFixed(2)}M`
  : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : String(Math.round(v));
const lastName = m => (m || "").split(" ").slice(-1)[0];
const daysAgo = d => d ? Math.round((Date.now() - new Date(d + "T00:00:00").getTime()) / 864e5) : null;

const CHG_META = {
  NEW:       { cls: "sm-chg-new",     label: "NEW",     arrow: "✦" },
  ADDED:     { cls: "sm-chg-added",   label: "ADDED",   arrow: "▲" },
  REDUCED:   { cls: "sm-chg-reduced", label: "REDUCED", arrow: "▼" },
  SOLD:      { cls: "sm-chg-sold",    label: "SOLD",    arrow: "✕" },
  UNCHANGED: { cls: "sm-chg-flat",    label: "HELD",    arrow: "·" },
};
const chgMeta = c => CHG_META[c] || { cls: "sm-chg-flat", label: "—", arrow: "·" };
// A congressional report with more trades than this collapses to one summary row.
const PTR_GROUP_MIN = 4;
const isBullish = c => c === "NEW" || c === "ADDED";
const isBearish = c => c === "REDUCED" || c === "SOLD";

function ChangeBadge({ row }) {
  const m = chgMeta(row.change_type);
  const pct = row.share_change_pct != null && (row.change_type === "ADDED" || row.change_type === "REDUCED")
    ? ` ${row.share_change_pct > 0 ? "+" : ""}${Math.round(row.share_change_pct)}%` : "";
  return <span className={`sm-chg ${m.cls}`}>{m.label}{pct}</span>;
}

function FormBadge({ form }) {
  const cls = form === "PTR" ? "sm-form-ptr" : form.startsWith("4") ? "sm-form-4" : form.includes("13D") ? "sm-form-13d" : "sm-form-13g";
  const label = form === "PTR" ? "CONGRESS" : form.startsWith("4") ? "FORM 4" : form.replace("SCHEDULE ", "SC ");
  return <span className={`sm-form ${cls}`}>{label}</span>;
}

export default function SmartMoneyView({ supabase, universe = [], focusTicker, onTicker }) {
  const [investors, setInvestors] = useState([]);
  const [overlap,   setOverlap]   = useState([]);
  const [filings,   setFilings]   = useState([]);
  const [activeCik, setActiveCik] = useState(null);
  const [book,      setBook]      = useState([]);
  const [bookFilter, setBookFilter] = useState("ALL");
  const [movesFilter, setMovesFilter] = useState("ALL");
  const [openGroups, setOpenGroups] = useState(() => new Set());
  const [loading,   setLoading]   = useState(true);
  const [error,     setError]     = useState(null);

  const universeSet = useMemo(() => new Set(universe), [universe]);
  // holding ticker → the universe ticker it counts toward (GOOGL → GOOG)
  const toUniverse = useMemo(() => {
    const m = {};
    universe.forEach(u => aliasesFor(u).forEach(a => { m[a] = u; }));
    return m;
  }, [universe]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const symbols = [...new Set(universe.flatMap(aliasesFor))];
        const [inv, ov, fl] = await Promise.all([
          supabase.from("smart_money_investors").select("*").order("total_value_usd", { ascending: false }),
          symbols.length
            ? supabase.from("investor_holdings")
                .select("cik,manager,fund,ticker,put_call,pct_portfolio,change_type,share_change_pct,rank,value_usd,period_of_report")
                .in("ticker", symbols)
            : Promise.resolve({ data: [] }),
          supabase.from("investor_filings").select("*").neq("direction", "SKIPPED")
            .order("filing_date", { ascending: false }).limit(200),
        ]);
        // PGRST205 = table not in the schema yet: smart_money_schema.sql not applied. Show setup hint.
        if (inv.error && inv.error.code !== "PGRST205") throw inv.error;
        if (cancelled) return;
        setInvestors(inv.data || []);
        setOverlap(ov.data || []);
        setFilings(fl.data || []);
        // Open on the investor most involved with the focused ticker, else the largest book.
        const focusHolder = focusTicker && (ov.data || [])
          .filter(r => toUniverse[r.ticker] === focusTicker)
          .sort((a, b) => (b.pct_portfolio || 0) - (a.pct_portfolio || 0))[0];
        setActiveCik(focusHolder?.cik ?? inv.data?.[0]?.cik ?? null);
      } catch (e) {
        if (!cancelled) setError(e.message || String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [supabase, universe, focusTicker, toUniverse]);

  useEffect(() => {
    if (activeCik == null) return;
    let cancelled = false;
    supabase.from("investor_holdings").select("*").eq("cik", activeCik)
      .order("value_usd", { ascending: false })
      .then(({ data }) => { if (!cancelled) setBook(data || []); });
    return () => { cancelled = true; };
  }, [supabase, activeCik]);

  // Universe tickers held (or exited) by at least one tracked fund.
  const overlapRows = useMemo(() => {
    const by = {};
    overlap.forEach(r => {
      const u = toUniverse[r.ticker];
      if (!u) return;
      (by[u] = by[u] || []).push(r);
    });
    return Object.entries(by).map(([ticker, rows]) => {
      const holding = rows.filter(r => r.change_type !== "SOLD" && !r.put_call);
      return {
        ticker, rows: rows.sort((a, b) => (b.pct_portfolio || 0) - (a.pct_portfolio || 0)),
        holders: new Set(holding.map(r => r.cik)).size,
        bull: new Set(rows.filter(r => !r.put_call && isBullish(r.change_type)).map(r => r.cik)).size,
        bear: new Set(rows.filter(r => !r.put_call && isBearish(r.change_type)).map(r => r.cik)).size,
      };
    }).sort((a, b) => b.holders - a.holders || (b.bull - b.bear) - (a.bull - a.bear));
  }, [overlap, toUniverse]);

  const active = investors.find(i => i.cik === activeCik);
  const visibleBook = book.filter(r =>
    bookFilter === "ALL" ? r.change_type !== "SOLD"
    : bookFilter === "CHANGES" ? r.change_type && r.change_type !== "UNCHANGED"
    : toUniverse[r.ticker]);
  const visibleMoves = movesFilter === "ALL" ? filings : filings.filter(f => toUniverse[f.ticker]);
  // Bundle each congressional report (member + filing date) so one member
  // selling down 40 positions reads as one line, not 40.
  const feed = useMemo(() => {
    const out = [], groups = new Map();
    for (const f of visibleMoves) {
      if (f.form !== "PTR") { out.push({ kind: "row", f }); continue; }
      const key = `${f.cik}|${f.filing_date}`;
      if (!groups.has(key)) { const g = { kind: "group", key, rows: [] }; groups.set(key, g); out.push(g); }
      groups.get(key).rows.push(f);
    }
    return out.flatMap(x => x.kind === "group" && x.rows.length < PTR_GROUP_MIN
      ? x.rows.map(f => ({ kind: "row", f })) : [x]);
  }, [visibleMoves]);
  const toggleGroup = key => setOpenGroups(prev => {
    const next = new Set(prev);
    next.has(key) ? next.delete(key) : next.add(key);
    return next;
  });

  if (loading) return <div className="loading">// loading smart money data...</div>;
  if (error) return <div className="error" role="alert">// error: {error}</div>;
  if (investors.length === 0) return (
    <div className="full-panel"><div className="sm-empty">
      // no smart money data yet — run smart_money_schema.sql in Supabase, then python run_smart_money.py
    </div></div>
  );

  const latestPeriod = investors.map(i => i.period_of_report).filter(Boolean).sort().pop();

  return (
    <div className="sm-wrap">
      <div className="sm-note">
        <b>How fresh is this?</b> 13F portfolios are a snapshot at quarter end ({latestPeriod}), filed up to 45 days later,
        so they can be <b>~{daysAgo(latestPeriod)} days old</b> and show long US positions only (no shorts).
        Form 4 and Schedule 13D/G filings are much fresher: days, not months.
        Congressional trades (Pelosi, Khanna, Gottheimer) are disclosed up to 45 days after the trade, as dollar ranges.
        The analyst uses all of it as context; it never flips a call on its own.
      </div>

      {/* ── Universe overlap ─────────────────────────────────────────── */}
      <section className="sm-panel">
        <div className="section-label">YOUR UNIVERSE · WHICH FAMOUS INVESTORS OWN IT</div>
        {overlapRows.length === 0
          ? <div className="sm-empty">// none of the tracked funds hold a ticker in today's universe</div>
          : (
          <div className="sm-scroll">
            <table className="sm-table">
              <thead><tr><th>TICKER</th><th>HOLDERS</th><th>LAST QUARTER</th><th>WHO · % OF THEIR BOOK · CHANGE</th></tr></thead>
              <tbody>
                {overlapRows.map(o => (
                  <tr key={o.ticker} className={o.ticker === focusTicker ? "focus-row" : ""}>
                    <td><button className="sm-ticker" onClick={() => onTicker?.(o.ticker)} title={`Open ${o.ticker}'s story`}>{o.ticker}</button></td>
                    <td className="sm-num">{o.holders}</td>
                    <td>
                      {o.bull > 0 && <span className="sm-chg sm-chg-added">▲ {o.bull} buying</span>}{" "}
                      {o.bear > 0 && <span className="sm-chg sm-chg-sold">▼ {o.bear} selling</span>}
                      {o.bull === 0 && o.bear === 0 && <span className="sm-dim">no change</span>}
                    </td>
                    <td>
                      <div className="sm-holders">
                        {o.rows.map(r => (
                          <button key={`${r.cik}-${r.ticker}-${r.put_call}`} className={`sm-holder ${chgMeta(r.change_type).cls}`}
                                  onClick={() => setActiveCik(r.cik)} title={`${r.manager} · ${r.fund}`}>
                            {lastName(r.manager)}{r.ticker !== o.ticker ? ` (${r.ticker})` : ""}{r.put_call ? ` ${r.put_call}` : ""}
                            {r.change_type !== "SOLD" && ` ${Number(r.pct_portfolio || 0).toFixed(1)}%`} {chgMeta(r.change_type).arrow}
                          </button>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Investor list + selected book ────────────────────────────── */}
      <div className="sm-grid">
        <div className="sm-list" role="tablist" aria-label="Famous investors">
          <div className="sm-list-head">INVESTORS · 13F</div>
          {investors.map(i => (
            <button key={i.cik} role="tab" aria-selected={i.cik === activeCik}
                    className={`sm-inv${i.cik === activeCik ? " active" : ""}`} onClick={() => { setActiveCik(i.cik); setBookFilter("ALL"); }}>
              <div>
                <div className="sm-inv-name">{i.manager}</div>
                <div className="sm-inv-fund">{i.fund} · {i.style}</div>
              </div>
              <div className="sm-inv-aum">{fmtUsd(i.total_value_usd)}</div>
            </button>
          ))}
        </div>

        <div className="sm-book">
          {active && (
            <>
              <div className="sm-book-head">
                <div>
                  <div className="sm-book-title">{active.manager}</div>
                  <div className="sm-dim">{active.fund} · {active.style} · CIK {active.cik}</div>
                </div>
                <a className="sm-link" href={`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${active.cik}&type=13F&dateb=&owner=include&count=40`}
                   target="_blank" rel="noopener noreferrer">EDGAR ↗</a>
              </div>
              <div className="metrics-grid">
                <div className="metric"><div className="metric-label">13F VALUE</div><div className="metric-value">{fmtUsd(active.total_value_usd)}</div></div>
                <div className="metric"><div className="metric-label">POSITIONS</div><div className="metric-value">{active.positions}</div></div>
                <div className="metric"><div className="metric-label">AS OF</div><div className="metric-value" style={{ fontSize: 13 }}>{active.period_of_report}</div></div>
                <div className="metric"><div className="metric-label">FILED</div><div className="metric-value" style={{ fontSize: 13 }}>{active.filing_date}</div></div>
              </div>
              <div className="sm-counts">
                vs {active.prev_period || "prior quarter"}:
                <span className="sm-chg sm-chg-new">{active.new_count} new</span>
                <span className="sm-chg sm-chg-added">{active.added_count} added</span>
                <span className="sm-chg sm-chg-reduced">{active.reduced_count} reduced</span>
                <span className="sm-chg sm-chg-sold">{active.sold_count} sold out</span>
              </div>

              <div className="sm-filters" role="group" aria-label="Filter holdings">
                {[["ALL", "ALL HOLDINGS"], ["CHANGES", "CHANGES ONLY"], ["UNIVERSE", "IN OUR UNIVERSE"]].map(([k, l]) => (
                  <button key={k} className={`filter-btn ${bookFilter === k ? "active" : ""}`} aria-pressed={bookFilter === k} onClick={() => setBookFilter(k)}>{l}</button>
                ))}
              </div>

              <div className="sm-scroll sm-book-table">
                <table className="sm-table">
                  <thead><tr><th>#</th><th>TICKER</th><th>ISSUER</th><th className="r">% BOOK</th><th className="r">VALUE</th><th className="r">SHARES</th><th>QoQ</th></tr></thead>
                  <tbody>
                    {visibleBook.map(r => {
                      const u = toUniverse[r.ticker];
                      return (
                        <tr key={`${r.cusip}-${r.put_call}`} className={u ? "sm-in-universe" : ""}>
                          <td className="sm-dim">{r.rank ?? "—"}</td>
                          <td>
                            {u ? <button className="sm-ticker" onClick={() => onTicker?.(u)} title={`In our universe — open ${u}'s story`}>{r.ticker}</button>
                               : <span className="sm-tk">{r.ticker || "—"}</span>}
                            {r.put_call && <span className="sm-opt">{r.put_call}</span>}
                          </td>
                          <td className="sm-issuer">{r.issuer}</td>
                          <td className="r">{r.change_type === "SOLD" ? "—" : `${Number(r.pct_portfolio).toFixed(2)}%`}</td>
                          <td className="r">{r.change_type === "SOLD" ? "—" : fmtUsd(r.value_usd)}</td>
                          <td className="r">{r.change_type === "SOLD" ? <span className="sm-dim">was {fmtSh(r.prev_shares)}</span> : fmtSh(r.shares)}</td>
                          <td><ChangeBadge row={r} /></td>
                        </tr>
                      );
                    })}
                    {visibleBook.length === 0 && <tr><td colSpan={7} className="sm-dim" style={{ padding: 16 }}>// nothing matches this filter</td></tr>}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </div>

      {/* ── Latest moves feed ────────────────────────────────────────── */}
      <section className="sm-panel">
        <div className="sm-moves-head">
          <div className="section-label" style={{ marginBottom: 0 }}>LATEST MOVES · FORM 4, SCHEDULE 13D/G &amp; CONGRESS</div>
          <div className="sm-filters" role="group" aria-label="Filter moves">
            {[["ALL", "ALL"], ["UNIVERSE", "OUR UNIVERSE"]].map(([k, l]) => (
              <button key={k} className={`filter-btn ${movesFilter === k ? "active" : ""}`} aria-pressed={movesFilter === k} onClick={() => setMovesFilter(k)}>{l}</button>
            ))}
          </div>
        </div>
        {visibleMoves.length === 0
          ? <div className="sm-empty">// no recent Form 4 / 13D / 13G filings{movesFilter === "UNIVERSE" ? " on universe tickers" : ""}</div>
          : (
          <div className="sm-scroll">
            <table className="sm-table">
              <thead><tr><th>FILED</th><th>WHO</th><th>FORM</th><th>TICKER</th><th>WHAT</th><th></th></tr></thead>
              <tbody>
                {feed.flatMap(item => {
                  if (item.kind === "row") return [moveRow(item.f)];
                  const { key, rows } = item, open = openGroups.has(key), f0 = rows[0];
                  const buys = rows.filter(r => r.direction === "BUY").length;
                  const sells = rows.filter(r => r.direction === "SELL").length;
                  const tickers = [...new Set(rows.map(r => r.ticker).filter(Boolean))]
                    .sort((a, b) => (toUniverse[b] ? 1 : 0) - (toUniverse[a] ? 1 : 0));
                  const shown = tickers.slice(0, 6);
                  const head = (
                    <tr key={key} className={tickers.some(t => toUniverse[t]) ? "sm-in-universe sm-group" : "sm-group"}>
                      <td className="sm-nowrap">{f0.filing_date} <span className="sm-dim">· {daysAgo(f0.filing_date)}d</span></td>
                      <td className="sm-nowrap">{f0.manager}</td>
                      <td><FormBadge form="PTR" /></td>
                      <td className="sm-group-tickers">
                        {shown.map(t => toUniverse[t]
                          ? <button key={t} className="sm-ticker" onClick={() => onTicker?.(toUniverse[t])}>{t}</button>
                          : <span key={t} className="sm-tk">{t}</span>)}
                        {tickers.length > shown.length && <span className="sm-dim">+{tickers.length - shown.length}</span>}
                      </td>
                      <td>
                        {rows.length} positions in one report:{" "}
                        {buys > 0 && <span className="pos">{buys} bought</span>}{buys > 0 && sells > 0 && " · "}
                        {sells > 0 && <span className="neg">{sells} sold</span>}{" "}
                        <button className="sm-expand" aria-expanded={open} onClick={() => toggleGroup(key)}>
                          {open ? "hide ▴" : "show all ▾"}</button>
                      </td>
                      <td><a className="sm-link" href={f0.url} target="_blank" rel="noopener noreferrer"
                            aria-label="Open disclosure PDF on the House Clerk site">PDF ↗</a></td>
                    </tr>
                  );
                  return open ? [head, ...rows.map(r => moveRow(r, true))] : [head];
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );

  function moveRow(f, nested = false) {
                  const u = toUniverse[f.ticker];
                  return (
                    <tr key={`${f.cik}-${f.accession}`} className={`${u ? "sm-in-universe" : ""}${nested ? " sm-nested" : ""}`}>
                      <td className="sm-nowrap">{f.filing_date} <span className="sm-dim">· {daysAgo(f.filing_date)}d</span></td>
                      <td className="sm-nowrap">{f.manager}</td>
                      <td><FormBadge form={f.form} /></td>
                      <td>{u ? <button className="sm-ticker" onClick={() => onTicker?.(u)}>{f.ticker}</button>
                              : <span className="sm-tk" title={f.issuer}>{f.ticker || f.issuer || "—"}</span>}</td>
                      <td className={f.direction === "BUY" ? "pos" : f.direction === "SELL" ? "neg" : ""}>{f.summary}</td>
                      <td><a className="sm-link" href={f.url} target="_blank" rel="noopener noreferrer"
                            aria-label={f.form === "PTR" ? "Open disclosure PDF on the House Clerk site" : "Open filing on SEC EDGAR"}>
                          {f.form === "PTR" ? "PDF ↗" : "SEC ↗"}</a></td>
                    </tr>
                  );
  }
}

// ── CSS — injected into Dashboard.jsx's <style> tag ──────────────────────────
export const SMART_CSS = `
  .sm-wrap{display:flex;flex-direction:column;gap:1px;background:var(--border);}
  .sm-note{background:var(--bg2);padding:12px 20px;font-size:12px;color:var(--text-dim);line-height:1.6;border-left:2px solid var(--amber-dim);}
  .sm-note b{color:var(--text);font-weight:500;}
  .sm-panel{background:var(--bg2);padding:16px 20px;}
  .sm-empty{font-family:var(--mono);font-size:11px;color:var(--text-dimmer);padding:16px 0;}
  .sm-scroll{overflow-x:auto;}
  .sm-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;}
  .sm-table th{color:var(--text-dimmer);font-weight:400;padding:6px 8px;text-align:left;border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.05em;white-space:nowrap;}
  .sm-table td{padding:7px 8px;border-bottom:1px solid rgba(255,255,255,.03);color:var(--text-dim);vertical-align:middle;}
  .sm-table .r{text-align:right;}
  .sm-table tr:hover td{background:rgba(255,255,255,.02);}
  .sm-table tr.focus-row td{background:var(--amber-glow);}
  .sm-in-universe td{background:rgba(239,159,39,.05);}
  .sm-num{color:var(--text)!important;font-weight:700;}
  .sm-dim{color:var(--text-dimmer);}
  .sm-nowrap{white-space:nowrap;}
  .sm-issuer{max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .sm-ticker{font-family:var(--mono);font-weight:700;color:var(--amber);text-decoration:underline dotted;text-underline-offset:3px;}
  .sm-tk{font-weight:700;color:var(--text);}
  .sm-opt{font-size:9px;margin-left:5px;padding:1px 4px;border-radius:2px;background:var(--purple-dim);color:var(--purple);}
  .sm-link{font-family:var(--mono);font-size:10px;color:var(--blue);text-decoration:none;white-space:nowrap;}
  .sm-link:hover{text-decoration:underline;}

  .sm-chg{display:inline-block;font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:2px;border:1px solid;white-space:nowrap;}
  .sm-chg-new{background:var(--teal-dim);color:var(--teal);border-color:rgba(46,207,168,.3);}
  .sm-chg-added{background:var(--green-dim);color:var(--green);border-color:rgba(60,174,111,.3);}
  .sm-chg-reduced{background:var(--amber-glow);color:var(--amber);border-color:var(--border-amber);}
  .sm-chg-sold{background:var(--red-dim);color:var(--red);border-color:rgba(226,75,74,.3);}
  .sm-chg-flat{background:var(--bg3);color:var(--text-dimmer);border-color:var(--border);}
  .sm-holders{display:flex;flex-wrap:wrap;gap:4px;}
  .sm-holder{font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:2px;border:1px solid;white-space:nowrap;}
  .sm-holder:hover{filter:brightness(1.25);}

  .sm-form{font-family:var(--mono);font-size:9px;font-weight:700;padding:2px 6px;border-radius:2px;white-space:nowrap;letter-spacing:.04em;}
  .sm-form-4{background:var(--blue-dim);color:var(--blue);}
  .sm-form-13d{background:var(--red-dim);color:var(--orange);}
  .sm-form-13g{background:var(--bg3);color:var(--text-dim);}
  .sm-form-ptr{background:var(--purple-dim);color:var(--purple);}
  .sm-group-tickers{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
  .sm-expand{font-family:var(--mono);font-size:10px;color:var(--blue);margin-left:6px;}
  .sm-expand:hover{text-decoration:underline;}
  .sm-nested td{background:rgba(155,109,255,.04);}
  .sm-nested td:nth-child(2){padding-left:18px;}

  .sm-grid{display:grid;grid-template-columns:270px 1fr;gap:1px;background:var(--border);}
  .sm-list{background:var(--bg2);padding:14px 0;}
  .sm-list-head{padding:0 16px 10px;font-family:var(--mono);font-size:10px;color:var(--text-dimmer);letter-spacing:.1em;}
  .sm-inv{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;padding:9px 16px;border-left:2px solid transparent;transition:background .12s;}
  .sm-inv:hover{background:var(--bg3);}
  .sm-inv.active{background:var(--bg3);border-left-color:var(--amber);}
  .sm-inv-name{font-size:13px;color:var(--text);}
  .sm-inv-fund{font-family:var(--mono);font-size:9px;color:var(--text-dimmer);margin-top:2px;}
  .sm-inv-aum{font-family:var(--mono);font-size:11px;color:var(--text-dim);white-space:nowrap;}
  .sm-book{background:var(--bg2);padding:16px 20px;min-width:0;}
  .sm-book-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;}
  .sm-book-title{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--amber);}
  .sm-book-head .sm-dim{font-family:var(--mono);font-size:10px;margin-top:3px;}
  .sm-counts{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--text-dimmer);margin-bottom:12px;}
  .sm-filters{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px;}
  .sm-book-table{max-height:560px;overflow-y:auto;}
  .sm-moves-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
  .sm-moves-head .sm-filters{margin-bottom:0;}

  @media(max-width:680px){
    .sm-grid{grid-template-columns:1fr;}
    .sm-list{max-height:260px;overflow-y:auto;}
    .sm-panel,.sm-book,.sm-note{padding-left:16px;padding-right:16px;}
    .sm-issuer{max-width:120px;}
  }
`;
