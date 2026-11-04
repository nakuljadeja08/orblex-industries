/**
 * Dashboard.jsx — Orblex Industries Portfolio Dashboard (v3)
 *
 * New in v3:
 *   - REGIME: header chip (always visible) + full view reading market_regimes:
 *     SPY vs 200MA, drawdown, VIX, death cross, rules_fired, rationale
 *   - ALLOCATION: sector & TECH-theme exposure bars with cap markers,
 *     constraint-violations panel (model proposed → enforcer disposed),
 *     new sizing_method chips (REGIME_EXCLUDED / INVERSE_HOLD_EXPIRED /
 *     POLICY_CAPPED / SECTOR_CAPPED)
 *   - ANALYST: sentiment gauge (−1..+1), valuation chip, fundamentals card
 *     with snapshot freshness date
 *   - Accessibility: all tab systems are real <button>s with tablist/tab
 *     semantics, :focus-visible outlines, prefers-reduced-motion respected,
 *     dim-text contrast raised, charts get text alternatives
 *
 * New tables read:
 *   market_regimes, stock_fundamentals
 *   (+ new columns: allocation_plans.sector_exposure / theme_exposure /
 *    constraint_violations, allocation_details.sector,
 *    agent_reports.sentiment_score / valuation_view)
 *
 * Dependencies:
 *   npm install @supabase/supabase-js chart.js react-chartjs-2
 */

import { useEffect, useState, useCallback, useMemo } from "react";
import BacktestView, { BACKTEST_CSS } from "./BacktestView";
import SignalStory, { STORY_CSS } from "./SignalStory";
import RunPipeline, { RUN_CSS, authClient } from "./RunPipeline";
import DemoIntro, { DEMO_CSS } from "./demo/DemoIntro";
import Playground, { PLAYGROUND_CSS, returningToPlayground } from "./demo/Playground";
import SmartMoneyView, { SMART_CSS, aliasesFor } from "./SmartMoneyView";
import SwingPlansView, { SWING_CSS } from "./SwingPlansView";
import LongTermView, { LONGTERM_CSS } from "./LongTermView";
import { AutomationView, DailyReviewView, OPS_CSS } from "./OpsViews";
import {
  Chart as ChartJS,
  CategoryScale, LinearScale, PointElement,
  LineElement, BarElement, Filler, Tooltip, ArcElement, Legend,
} from "chart.js";
import { Line, Doughnut, Bar } from "react-chartjs-2";

ChartJS.register(
  CategoryScale, LinearScale, PointElement,
  LineElement, BarElement, Filler, Tooltip, ArcElement, Legend
);

// Every read runs as the signed-in admin: private_access_schema.sql refuses the public key.
// In the demo build (.env.demo) this client is src/demo/fakeSupabase.js, with sample data.
const supabase = authClient;
export const DEMO = import.meta.env.VITE_DEMO === "1";

// ── Helpers ────────────────────────────────────────────────────────────────────
const fmt     = (n, d = 2) => n == null ? "—" : Number(n).toFixed(d);
const fmtVol  = v => (v / 1e6).toFixed(1) + "M";
const fmtDur  = s => s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Number(s).toFixed(1)}s`;
const fmtK    = v => v >= 1000 ? (v / 1000).toFixed(0) + "K" : String(v);
const signCls = v => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const parseJ  = v => { try { return typeof v === "string" ? JSON.parse(v) : v || []; } catch { return []; } };
const parseObj = v => { try { const o = typeof v === "string" ? JSON.parse(v) : v; return o && typeof o === "object" && !Array.isArray(o) ? o : {}; } catch { return {}; } };

// Raw technical indicators, recomputed from the stock_prices series the analyst
// saw. Formulas AND window mirror context_builder.py exactly: a LOOKBACK_DAYS=30
// calendar-day slice, then SMA-5/20, sample-std vol annualised ×√252, and
// simple-average RSI-14 — so the trace reproduces the numbers that fed the
// analyst prompt rather than a separate calculation. Returns null when too short.
const TRACE_LOOKBACK_DAYS = 30;   // == context_builder.LOOKBACK_DAYS
function computeIndicators(pricesAll) {
  if (!pricesAll || pricesAll.length < 5) return null;
  // Slice to the same 30-calendar-day window the analyst context used, so
  // volatility (computed over the whole series) matches, not just the windowed
  // MA/RSI values.
  const lastDate = pricesAll[pricesAll.length - 1].date;
  const cutoff = new Date(lastDate);
  cutoff.setDate(cutoff.getDate() - TRACE_LOOKBACK_DAYS);
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  const prices = pricesAll.filter(p => p.date >= cutoffStr);
  if (prices.length < 5) return null;
  const closes = prices.map(p => Number(p.close)).filter(c => !isNaN(c));
  if (closes.length < 5) return null;
  const n = closes.length;
  const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
  const close = closes[n - 1];
  const ma5  = mean(closes.slice(-5));
  const ma20 = n >= 20 ? mean(closes.slice(-20)) : null;

  // Annualised volatility of daily returns (pandas sample std, ddof=1).
  const rets = [];
  for (let i = 1; i < n; i++) rets.push(closes[i] / closes[i - 1] - 1);
  let volAnn = null;
  if (rets.length >= 5) {
    const m = mean(rets);
    const variance = rets.reduce((s, r) => s + (r - m) ** 2, 0) / (rets.length - 1);
    volAnn = Math.sqrt(variance) * Math.sqrt(252) * 100;
  }

  // RSI-14 — simple rolling mean of gains/losses over the last 14 deltas.
  let rsi14 = null;
  if (n >= 15) {
    const deltas = [];
    for (let i = 1; i < n; i++) deltas.push(closes[i] - closes[i - 1]);
    const last14 = deltas.slice(-14);
    const avgGain = mean(last14.map(d => (d > 0 ? d : 0)));
    const avgLoss = mean(last14.map(d => (d < 0 ? -d : 0)));
    rsi14 = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }

  return { close, ma5, ma20, volAnn, rsi14,
           priceVsMa20: ma20 ? (close / ma20 - 1) * 100 : null,
           asOf: prices[n - 1].date };
}

// ── Regime / policy constants ──────────────────────────────────────────────────
// NOTE: caps mirror instrument_policy.py (the enforcing source of truth).
// If you change SECTOR_CAPS / THEME_CAPS there, update here too.
const REGIME_META = {
  BULL:       { color: "var(--green)",  bg: "var(--green-dim)",  border: "rgba(60,174,111,.3)" },
  CHOPPY:     { color: "var(--amber)",  bg: "var(--amber-glow)", border: "var(--border-amber)" },
  CORRECTION: { color: "var(--orange)", bg: "rgba(244,120,60,.12)", border: "rgba(244,120,60,.35)" },
  BEAR:       { color: "var(--red)",    bg: "var(--red-dim)",    border: "rgba(226,75,74,.3)" },
};
const SECTOR_CAPS_UI = {
  SEMICONDUCTORS: 30, SOFTWARE_CLOUD: 30, BIG_TECH_PLATFORMS: 25,
  OPTICAL_COMPONENTS: 15, AI_INFRASTRUCTURE: 10, CLEAN_ENERGY: 10,
  CONSUMER_STAPLES: 12, UTILITIES: 12, COMMODITIES: 12, INVERSE_HEDGE: 5,
};
const DEFAULT_SECTOR_CAP_UI = 25;
const TECH_THEME_CAP_UI = 80;

// ── CSS ────────────────────────────────────────────────────────────────────────
export const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500&display=swap');
  :root {
    --amber:#EF9F27; --amber-dim:#BA7517; --amber-glow:rgba(239,159,39,0.12);
    --green:#3CAE6F; --green-dim:rgba(60,174,111,0.15);
    --red:#E24B4A;   --red-dim:rgba(226,75,74,0.15);
    --blue:#378ADD;  --blue-dim:rgba(55,138,221,0.12);
    --purple:#9B6DFF; --purple-dim:rgba(155,109,255,0.12);
    --teal:#2ECFA8;  --teal-dim:rgba(46,207,168,0.12);
    --orange:#F4783C;
    --bg:#0D0E0F; --bg2:#141517; --bg3:#1A1C1F; --bg4:#1E2024;
    --border:rgba(255,255,255,0.07); --border-amber:rgba(239,159,39,0.25);
    --text:#E8E6E0; --text-dim:#969489; --text-dimmer:#6E6D66;
    --mono:'Space Mono',monospace; --sans:'DM Sans',sans-serif;
    --radius:4px;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);}
  .dash{min-height:100vh;background:var(--bg);}

  /* Accessibility primitives */
  :focus-visible{outline:2px solid var(--amber);outline-offset:2px;border-radius:2px;}
  button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;text-align:inherit;}
  .visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0;}
  @media (prefers-reduced-motion: reduce){
    .live-dot,.status-warn{animation:none !important;}
    *{transition:none !important;}
  }

  .header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border-amber);background:var(--bg2);}
  .logo{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--amber);letter-spacing:.1em;}
  .logo span{color:var(--text-dim);font-weight:400;}
  .live-dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;margin:0 8px;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .regime-chip{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.08em;padding:4px 10px;border-radius:var(--radius);border:1px solid;margin-left:12px;}

  /* Book switch: swing (amber, daily) vs main account (teal, weekly advice) */
  .book-bar{display:flex;align-items:center;gap:14px;padding:8px 20px;background:var(--bg2);border-bottom:1px solid var(--border);flex-wrap:wrap;}
  .book-toggle{display:flex;gap:4px;}
  .book-toggle button{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.06em;padding:4px 9px;border-radius:2px;border:1px solid var(--border);color:var(--text-dimmer);background:transparent;cursor:pointer;}
  .book-toggle button:hover{color:var(--text-dim);}
  .book-toggle button.on{color:var(--amber);border-color:var(--amber);}
  .book-sub{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--text-dimmer);}
  .book-longterm .header{border-bottom:2px solid var(--teal);}
  .book-longterm .book-toggle button.on{color:var(--teal);border-color:var(--teal);background:var(--teal-dim);}
  .book-longterm .book-sub{color:var(--teal);}

  .nav-tabs{display:flex;gap:2px;padding:0 20px;background:var(--bg2);border-bottom:1px solid var(--border);overflow-x:auto;}
  .nav-tab{padding:10px 16px;cursor:pointer;font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.06em;border:none;border-bottom:2px solid transparent;color:var(--text-dimmer);transition:all .15s;white-space:nowrap;background:transparent;}
  .nav-tab:hover{color:var(--text-dim);}
  .nav-tab.active{color:var(--amber);border-bottom-color:var(--amber);}
  .nav-group{display:flex;align-items:flex-end;gap:2px;padding-left:10px;margin-left:6px;border-left:1px solid var(--border);}
  .nav-group:first-child{border-left:none;margin-left:0;padding-left:0;}
  .nav-group-label{font-family:var(--mono);font-size:8px;letter-spacing:.12em;color:var(--text-dimmer);align-self:center;margin-right:4px;white-space:nowrap;opacity:.8;}

  /* Where-am-I bar under the nav: the view's role in the pipeline + breadcrumb */
  .context-bar{display:flex;align-items:center;gap:12px;padding:8px 20px;background:var(--bg);border-bottom:1px solid var(--border);font-size:12px;color:var(--text-dim);line-height:1.5;flex-wrap:wrap;}
  .context-stage{font-family:var(--mono);font-size:9px;letter-spacing:.08em;color:var(--text-dimmer);white-space:nowrap;}
  .context-text{flex:1;min-width:200px;color:var(--text);}
  .crumb{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.04em;padding:4px 10px;border-radius:var(--radius);border:1px solid var(--border-amber);background:var(--amber-glow);color:var(--amber);white-space:nowrap;}
  .focus-row td{background:var(--amber-glow)!important;}
  .focus-card{outline:1px solid var(--amber);outline-offset:-1px;}
  .tk-link{font-family:var(--mono);font-weight:700;color:var(--amber);text-decoration:underline;text-decoration-color:rgba(239,159,39,.35);text-underline-offset:3px;}
  .tk-link:hover{text-decoration-color:var(--amber);}

  .ticker-bar{display:flex;gap:2px;padding:10px 20px;background:var(--bg);border-bottom:1px solid var(--border);overflow-x:auto;}
  .ticker-tab{padding:6px 14px;border-radius:var(--radius);cursor:pointer;font-family:var(--mono);font-size:12px;font-weight:700;border:1px solid transparent;transition:all .15s;white-space:nowrap;color:var(--text-dim);background:transparent;}
  .ticker-tab:hover{color:var(--text);border-color:var(--border);}
  .ticker-tab.active{background:var(--amber-glow);border-color:var(--border-amber);color:var(--amber);}
  .tab-signal{font-size:9px;font-family:var(--sans);margin-left:5px;vertical-align:middle;}
  .sig-buy{color:var(--green)}.sig-sell{color:var(--red)}.sig-hold{color:var(--blue)}
  .sig-filter{display:flex;gap:4px;align-items:center;margin-right:10px;padding-right:12px;border-right:1px solid var(--border);}
  .filter-btn{font-family:var(--mono);font-size:10px;font-weight:700;padding:4px 9px;border-radius:var(--radius);border:1px solid var(--border);color:var(--text-dimmer);background:transparent;letter-spacing:.05em;white-space:nowrap;}
  .filter-btn:hover{color:var(--text-dim);}
  .filter-btn.active{background:var(--amber-glow);border-color:var(--border-amber);color:var(--amber);}

  .main-grid{display:grid;grid-template-columns:1fr 340px;gap:1px;background:var(--border);}
  .panel{background:var(--bg2);padding:16px 20px;}
  .right-panel{display:flex;flex-direction:column;gap:1px;background:var(--border);}
  .full-panel{background:var(--bg2);padding:20px;}

  .signal-hero{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;}
  .signal-label{font-family:var(--mono);font-size:28px;font-weight:700;letter-spacing:.05em;}
  .signal-strength{font-family:var(--mono);font-size:11px;padding:3px 8px;border-radius:var(--radius);letter-spacing:.08em;}
  .buy .signal-label{color:var(--green)}.buy .signal-strength{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .sell .signal-label{color:var(--red)}.sell .signal-strength{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .hold .signal-label{color:var(--blue)}.hold .signal-strength{background:var(--blue-dim);color:var(--blue);border:1px solid rgba(55,138,221,.3);}

  /* Context chips on analyst view */
  .context-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;}
  .chip{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:var(--radius);letter-spacing:.06em;border:1px solid;white-space:nowrap;}
  .chip-earn-high{background:var(--red-dim);color:var(--red);border-color:rgba(226,75,74,.3);}
  .chip-earn-med{background:var(--amber-glow);color:var(--amber);border-color:var(--border-amber);}
  .chip-earn-low{background:var(--green-dim);color:var(--green);border-color:rgba(60,174,111,.3);}
  .chip-flow-bull{background:var(--green-dim);color:var(--green);border-color:rgba(60,174,111,.3);}
  .chip-flow-bear{background:var(--red-dim);color:var(--red);border-color:rgba(226,75,74,.3);}
  .chip-flow-neut{background:var(--bg3);color:var(--text-dimmer);border-color:var(--border);}
  .chip-macro{background:var(--purple-dim);color:var(--purple);border-color:rgba(155,109,255,.3);}
  .chip-val-under{background:var(--green-dim);color:var(--green);border-color:rgba(60,174,111,.3);}
  .chip-val-fair{background:var(--blue-dim);color:var(--blue);border-color:rgba(55,138,221,.3);}
  .chip-val-over{background:var(--red-dim);color:var(--red);border-color:rgba(226,75,74,.3);}
  .chip-val-na{background:var(--bg3);color:var(--text-dimmer);border-color:var(--border);}

  /* Sentiment gauge */
  .senti-wrap{margin-bottom:16px;}
  .senti-head{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10px;color:var(--text-dimmer);letter-spacing:.06em;margin-bottom:5px;}
  .senti-track{position:relative;height:8px;border-radius:4px;background:linear-gradient(90deg,var(--red) 0%,var(--bg4) 50%,var(--green) 100%);opacity:.92;}
  .senti-marker{position:absolute;top:-3px;width:3px;height:14px;background:var(--text);border-radius:2px;box-shadow:0 0 6px rgba(0,0,0,.8);}
  .senti-center{position:absolute;top:-2px;left:50%;width:1px;height:12px;background:rgba(255,255,255,.25);}

  .metrics-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px;}
  .metrics-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px;}
  .metrics-grid-2{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:16px;}
  .metric{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;}
  .metric-label{font-size:10px;color:var(--text-dimmer);font-family:var(--mono);letter-spacing:.05em;margin-bottom:5px;}
  .metric-value{font-family:var(--mono);font-size:15px;font-weight:700;color:var(--text);}
  .pos{color:var(--green)!important}.neg{color:var(--red)!important}.neutral{color:var(--amber)!important}

  .summary-block{background:var(--bg3);border-left:2px solid var(--amber-dim);padding:12px 14px;border-radius:0 var(--radius) var(--radius) 0;margin-bottom:12px;}
  .summary-label{font-size:10px;color:var(--amber);font-family:var(--mono);letter-spacing:.08em;margin-bottom:6px;}
  .summary-text{font-size:13px;color:var(--text);line-height:1.6;}
  .reasoning-block{background:var(--bg3);border:1px solid var(--border);padding:12px 14px;border-radius:var(--radius);}
  .reasoning-label{font-size:10px;color:var(--text-dimmer);font-family:var(--mono);letter-spacing:.08em;margin-bottom:6px;}
  .reasoning-text{font-size:12px;color:var(--text-dim);line-height:1.65;}
  .section-label{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px;}

  .anomaly-list{display:flex;flex-direction:column;gap:8px;}
  .anomaly-item{display:flex;align-items:flex-start;gap:10px;background:var(--bg3);border-radius:var(--radius);padding:10px 12px;border:1px solid var(--border);}
  .anomaly-icon{width:20px;height:20px;border-radius:3px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;font-family:var(--mono);}
  .sev-high{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .sev-medium{background:var(--amber-glow);color:var(--amber);border:1px solid var(--border-amber);}
  .sev-low{background:var(--blue-dim);color:var(--blue);border:1px solid rgba(55,138,221,.2);}
  .atype{font-family:var(--mono);font-size:11px;color:var(--text);font-weight:700;text-transform:uppercase;letter-spacing:.04em;}
  .adetail{font-size:11px;color:var(--text-dim);margin-top:2px;line-height:1.5;}
  .ainterpret{font-size:11px;color:var(--text-dimmer);margin-top:3px;font-style:italic;}
  .no-anomaly{font-family:var(--mono);font-size:11px;color:var(--text-dimmer);padding:10px 0;}

  .price-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;}
  .price-table th{color:var(--text-dimmer);font-weight:400;padding:4px 6px;text-align:right;border-bottom:1px solid var(--border);letter-spacing:.04em;}
  .price-table th:first-child{text-align:left;}
  .price-table td{color:var(--text-dim);padding:5px 6px;text-align:right;border-bottom:1px solid rgba(255,255,255,.03);}
  .price-table td:first-child{text-align:left;color:var(--text-dimmer);}
  .price-table tr:last-child td{color:var(--text);}
  .price-table tr:last-child td:first-child{color:var(--amber);}

  .chart-wrap{position:relative;width:100%;height:180px;margin-bottom:16px;}
  .chart-wrap-lg{position:relative;width:100%;height:240px;margin-bottom:16px;}

  /* Fundamentals card */
  .fund-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;}
  .fund-table td{padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.03);}
  .fund-table td:first-child{color:var(--text-dimmer);}
  .fund-table td:last-child{text-align:right;color:var(--text);font-weight:700;}
  .fund-fresh{font-family:var(--mono);font-size:9px;color:var(--text-dimmer);margin-top:8px;letter-spacing:.05em;}

  /* Risk */
  .risk-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;}
  .risk-rating{font-family:var(--mono);font-size:22px;font-weight:700;letter-spacing:.06em;}
  .risk-low{color:var(--green)}.risk-moderate{color:var(--amber)}.risk-high{color:var(--red)}.risk-critical{color:var(--red);text-shadow:0 0 12px rgba(226,75,74,.5);}
  .risk-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;margin-bottom:16px;}
  .risk-table th{color:var(--text-dimmer);font-weight:400;padding:6px 8px;text-align:right;border-bottom:1px solid var(--border);letter-spacing:.04em;font-size:10px;}
  .risk-table th:first-child{text-align:left;}
  .risk-table td{padding:7px 8px;text-align:right;border-bottom:1px solid rgba(255,255,255,.03);font-size:11px;color:var(--text-dim);}
  .risk-table td:first-child{text-align:left;color:var(--amber);font-weight:700;}
  .risk-score-bar{display:inline-block;height:6px;border-radius:3px;margin-right:6px;vertical-align:middle;}
  .flags-list{display:flex;flex-wrap:wrap;gap:4px;}
  .flag-chip{font-size:9px;font-family:var(--mono);padding:2px 6px;border-radius:2px;letter-spacing:.04em;background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.25);}
  .flag-chip.warn{background:var(--amber-glow);color:var(--amber);border-color:var(--border-amber);}

  /* Allocation */
  .alloc-grid{display:grid;grid-template-columns:1fr 220px;gap:16px;align-items:start;}
  .alloc-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;}
  .alloc-table th{color:var(--text-dimmer);font-weight:400;padding:6px 8px;text-align:right;border-bottom:1px solid var(--border);letter-spacing:.04em;font-size:10px;}
  .alloc-table th:first-child{text-align:left;}
  .alloc-table td{padding:7px 8px;text-align:right;border-bottom:1px solid rgba(255,255,255,.03);font-size:11px;color:var(--text-dim);}
  .alloc-table td:first-child{text-align:left;color:var(--amber);font-weight:700;}
  .method-chip{font-size:9px;font-family:var(--mono);padding:2px 5px;border-radius:2px;letter-spacing:.03em;}
  .method-sw{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.25);}
  .method-rc{background:var(--amber-glow);color:var(--amber);border:1px solid var(--border-amber);}
  .method-ex{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.25);}
  .method-cr{background:var(--bg3);color:var(--text-dimmer);border:1px solid var(--border);}
  .method-regime{background:var(--purple-dim);color:var(--purple);border:1px solid rgba(155,109,255,.3);}
  .method-sector{background:var(--teal-dim);color:var(--teal);border:1px solid rgba(46,207,168,.3);}
  .donut-wrap{position:relative;width:180px;height:180px;margin:0 auto;}

  /* Exposure bars (sector / theme caps) */
  .cap-row{margin-bottom:10px;}
  .cap-head{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10px;margin-bottom:4px;}
  .cap-name{color:var(--text-dim);letter-spacing:.05em;}
  .cap-nums{color:var(--text-dimmer);}
  .cap-track{position:relative;height:8px;background:var(--bg4);border-radius:4px;}
  .cap-fill{position:absolute;left:0;top:0;height:100%;border-radius:4px;}
  .cap-marker{position:absolute;top:-3px;width:2px;height:14px;background:var(--text-dim);}
  .cap-at-limit .cap-fill{box-shadow:0 0 8px rgba(239,159,39,.45);}

  /* Constraint enforcement panel */
  .enforce-panel{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:20px;}
  .enforce-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--bg3);border-bottom:1px solid var(--border);}
  .enforce-title{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--text-dim);}
  .enforce-badge{font-family:var(--mono);font-size:10px;font-weight:700;padding:3px 9px;border-radius:var(--radius);letter-spacing:.06em;}
  .enforce-clean{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .enforce-corrected{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .violation-item{display:flex;gap:10px;padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.03);align-items:flex-start;}
  .violation-item:last-child{border-bottom:none;}
  .violation-n{font-family:var(--mono);font-size:10px;color:var(--red);min-width:20px;font-weight:700;}
  .violation-text{font-family:var(--mono);font-size:11px;color:var(--text-dim);line-height:1.55;}
  .enforce-empty{padding:12px 14px;font-family:var(--mono);font-size:11px;color:var(--green);}

  /* Regime view */
  .regime-banner{border-radius:var(--radius);padding:18px 22px;margin-bottom:20px;display:flex;align-items:center;justify-content:space-between;border:1px solid;}
  .regime-name{font-family:var(--mono);font-size:26px;font-weight:700;letter-spacing:.06em;}
  .rule-chip{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:var(--radius);letter-spacing:.04em;background:var(--bg3);border:1px solid var(--border);color:var(--text-dim);}

  /* OPTIONS VIEW */
  .options-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:20px;}
  .options-card{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;}
  .options-card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
  .pcr-bar-wrap{width:100%;height:8px;background:var(--bg4);border-radius:4px;overflow:hidden;margin:6px 0 3px;}
  .pcr-bar{height:100%;border-radius:4px;transition:width .3s;}
  .flow-badge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:var(--radius);font-weight:700;letter-spacing:.06em;}
  .flow-bullish{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .flow-bearish{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .flow-neutral{background:var(--bg4);color:var(--text-dimmer);border:1px solid var(--border);}
  .unusual-item{display:flex;align-items:center;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03);}
  .expiry-tabs{display:flex;gap:4px;margin-bottom:12px;}
  .expiry-tab{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:var(--radius);cursor:pointer;border:1px solid var(--border);color:var(--text-dimmer);background:transparent;}
  .expiry-tab.active{background:var(--amber-glow);border-color:var(--border-amber);color:var(--amber);}

  /* EARNINGS VIEW */
  .earnings-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:20px;}
  .earnings-card{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden;}
  .earnings-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
  .earn-high::before{background:var(--red);}
  .earn-med::before{background:var(--amber);}
  .earn-low::before{background:var(--green);}
  .earn-ticker{font-family:var(--mono);font-size:16px;font-weight:700;color:var(--amber);margin-bottom:4px;}
  .earn-date{font-family:var(--mono);font-size:11px;color:var(--text-dim);margin-bottom:10px;}
  .earn-dte{font-family:var(--mono);font-size:28px;font-weight:700;margin-bottom:4px;}
  .earn-label{font-size:10px;color:var(--text-dimmer);font-family:var(--mono);letter-spacing:.06em;}
  .surprise-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0;font-family:var(--mono);font-size:10px;}
  .beat-streak{font-family:var(--mono);font-size:11px;padding:2px 6px;border-radius:2px;}
  .beat-pos{background:var(--green-dim);color:var(--green);}
  .beat-neg{background:var(--red-dim);color:var(--red);}

  /* MACRO VIEW */
  .macro-regime-banner{border-radius:var(--radius);padding:16px 20px;margin-bottom:20px;display:flex;align-items:center;justify-content:space-between;}
  .macro-regime-label{font-family:var(--mono);font-size:24px;font-weight:700;letter-spacing:.06em;}
  .macro-impact{font-family:var(--mono);font-size:12px;padding:4px 12px;border-radius:var(--radius);border:1px solid;letter-spacing:.06em;}
  .macro-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;}
  .macro-stat-card{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;}
  .macro-stat-value{font-family:var(--mono);font-size:20px;font-weight:700;margin-bottom:3px;}
  .macro-stat-label{font-size:10px;color:var(--text-dimmer);font-family:var(--mono);letter-spacing:.06em;}
  .macro-stat-change{font-family:var(--mono);font-size:11px;margin-top:4px;}

  /* Report + Watchdog (unchanged) */
  .report-wrap{max-width:860px;}
  .report-meta{display:flex;gap:16px;align-items:center;margin-bottom:20px;flex-wrap:wrap;}
  .report-tag{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:var(--radius);background:var(--bg3);border:1px solid var(--border);color:var(--text-dim);letter-spacing:.05em;}
  .report-section{margin-bottom:20px;}
  .report-section-title{font-family:var(--mono);font-size:11px;color:var(--amber);letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border-amber);}
  .report-body{font-size:13px;color:var(--text);line-height:1.75;}
  .report-ticker-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;}
  .report-ticker-card{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;}
  .report-ticker-name{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--amber);margin-bottom:4px;}
  .report-ticker-text{font-size:11px;color:var(--text-dim);line-height:1.5;}
  .markdown-block{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:16px;font-size:13px;color:var(--text);line-height:1.75;white-space:pre-wrap;}
  .watchdog-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;margin-bottom:20px;}
  .agent-status-card{background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;display:flex;align-items:flex-start;gap:10px;}
  .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:4px;}
  .status-ok{background:var(--green);}
  .status-warn{background:var(--amber);animation:pulse 2s infinite;}
  .status-fail{background:var(--red);}
  .agent-name{font-family:var(--mono);font-size:12px;font-weight:700;color:var(--text);margin-bottom:3px;}
  .agent-run-id{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);margin-bottom:3px;}
  .agent-ts{font-size:11px;color:var(--text-dimmer);}
  .watchdog-alerts{display:flex;flex-direction:column;gap:6px;}
  .alert-item{display:flex;align-items:flex-start;gap:10px;background:var(--bg3);border-radius:var(--radius);padding:10px 12px;border:1px solid var(--border);}
  .alert-icon{font-family:var(--mono);font-size:10px;font-weight:700;padding:2px 5px;border-radius:2px;flex-shrink:0;}
  .alert-critical{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .alert-warning{background:var(--amber-glow);color:var(--amber);border:1px solid var(--border-amber);}
  .alert-info{background:var(--blue-dim);color:var(--blue);border:1px solid rgba(55,138,221,.2);}
  .alert-text{font-size:12px;color:var(--text-dim);line-height:1.5;}
  .watchdog-summary{background:var(--bg3);border-left:2px solid var(--teal);padding:12px 14px;border-radius:0 var(--radius) var(--radius) 0;margin-bottom:16px;}
  .watchdog-summary-label{font-size:10px;color:var(--teal);font-family:var(--mono);letter-spacing:.08em;margin-bottom:6px;}
  .no-alerts{font-family:var(--mono);font-size:11px;color:var(--green);padding:10px 0;}

  /* PIPELINE METRICS VIEW */
  .pm-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;margin-bottom:20px;}
  .pm-table th{color:var(--text-dimmer);font-weight:400;padding:6px 8px;text-align:right;border-bottom:1px solid var(--border);letter-spacing:.04em;font-size:10px;}
  .pm-table th:first-child{text-align:left;}
  .pm-table td{padding:7px 8px;text-align:right;border-bottom:1px solid rgba(255,255,255,.03);color:var(--text-dim);}
  .pm-table td:first-child{text-align:left;color:var(--text);font-weight:700;font-family:var(--mono);}
  .pm-badge{font-family:var(--mono);font-size:9px;font-weight:700;padding:2px 6px;border-radius:2px;letter-spacing:.06em;}
  .pm-ok{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .pm-fail{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .pm-skip{background:var(--bg4);color:var(--text-dimmer);border:1px solid var(--border);}
  .pm-history td:first-child{color:var(--text-dimmer);font-weight:400;}

  /* DRIFT VIEW */
  .drift-grid{display:grid;grid-template-columns:1fr 300px;gap:1px;background:var(--border);min-height:400px;}
  .drift-ticker-list{background:var(--bg2);padding:16px 0;}
  .drift-ticker-row{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;cursor:pointer;border:none;border-left:2px solid transparent;transition:all .12s;width:100%;background:transparent;}
  .drift-ticker-row:hover{background:var(--bg3);}
  .drift-ticker-row.active{background:var(--bg3);border-left-color:var(--amber);}
  .drift-ticker-name{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--amber);}
  .drift-flip-count{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:2px;}
  .drift-chronic{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .drift-active{background:var(--amber-glow);color:var(--amber);border:1px solid var(--border-amber);}
  .drift-clean{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .drift-detail{background:var(--bg2);padding:20px;}
  .drift-hero{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border);}
  .flip-timeline{display:flex;flex-direction:column;gap:8px;margin-bottom:20px;}
  .flip-event{display:flex;align-items:center;gap:12px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;position:relative;overflow:hidden;}
  .flip-event::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;}
  .flip-major::before{background:var(--red);}
  .flip-moderate::before{background:var(--amber);}
  .flip-minor::before{background:var(--blue);}
  .flip-date{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);min-width:72px;}
  .flip-arrow{display:flex;align-items:center;gap:6px;flex:1;}
  .flip-sig{font-family:var(--mono);font-size:12px;font-weight:700;padding:2px 8px;border-radius:2px;}
  .flip-sig-buy{background:var(--green-dim);color:var(--green);border:1px solid rgba(60,174,111,.3);}
  .flip-sig-sell{background:var(--red-dim);color:var(--red);border:1px solid rgba(226,75,74,.3);}
  .flip-sig-hold{background:var(--blue-dim);color:var(--blue);border:1px solid rgba(55,138,221,.3);}
  .flip-chevron{color:var(--text-dimmer);font-size:12px;}
  .flip-severity{font-family:var(--mono);font-size:9px;padding:2px 5px;border-radius:2px;letter-spacing:.04em;}
  .sev-major{background:var(--red-dim);color:var(--red);}
  .sev-moderate{background:var(--amber-glow);color:var(--amber);}
  .sev-minor{background:var(--blue-dim);color:var(--blue);}
  .flip-price{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);}
  .drift-stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px;}
  .chronic-banner{background:var(--red-dim);border:1px solid rgba(226,75,74,.3);border-radius:var(--radius);padding:10px 14px;margin-bottom:16px;display:flex;align-items:center;gap:10px;}
  .chronic-icon{font-family:var(--mono);font-size:18px;color:var(--red);}
  .chronic-text{font-family:var(--mono);font-size:11px;color:var(--red);line-height:1.5;}
  .history-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px;}
  .history-table th{color:var(--text-dimmer);font-weight:400;padding:5px 8px;text-align:left;border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.05em;}
  .history-table td{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.03);color:var(--text-dim);}
  .history-table tr:hover td{background:rgba(255,255,255,.02);}
  .no-drift{font-family:var(--mono);font-size:11px;color:var(--green);padding:20px 0;text-align:center;}

  .footer{padding:10px 20px;background:var(--bg);border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
  .footer-left{font-family:var(--mono);font-size:10px;color:var(--text-dimmer);}
  .footer-right{font-size:11px;color:var(--text-dimmer);}
  .footer-right span{color:var(--amber);}

  .loading{display:flex;align-items:center;justify-content:center;height:300px;font-family:var(--mono);font-size:13px;color:var(--text-dim);}
  .error{background:var(--red-dim);border:1px solid rgba(226,75,74,.3);border-radius:var(--radius);padding:12px 16px;margin:16px 20px;font-family:var(--mono);font-size:12px;color:var(--red);}
  .empty-state{font-family:var(--mono);font-size:11px;color:var(--text-dimmer);padding:20px 0;text-align:center;}

  @media(max-width:680px){
    .main-grid{grid-template-columns:1fr}
    .metrics-grid,.metrics-grid-3{grid-template-columns:repeat(2,1fr)}
    .alloc-grid,.macro-grid{grid-template-columns:1fr}
    .options-grid,.earnings-grid{grid-template-columns:1fr}
    .drift-grid{grid-template-columns:1fr}
    .header{flex-wrap:wrap;gap:10px;padding:12px 16px;}
    .header > div{flex-wrap:wrap;row-gap:8px;}
    .book-bar{gap:8px;}
    .book-toggle{flex:1 1 100%;}
    .book-toggle button{flex:1;}
    .book-bar,.nav-tabs,.ticker-bar,.context-bar{padding-left:16px;padding-right:16px;}
    .panel,.full-panel{padding:16px;}
  }
`;

// ── Shared chart options factory ───────────────────────────────────────────────
const monoTooltip = {
  backgroundColor: "#1A1C1F", borderColor: "rgba(255,255,255,0.1)", borderWidth: 1,
  titleColor: "#969489", bodyColor: "#E8E6E0",
};
const monoTick = { color: "#6E6D66", font: { family: "Space Mono", size: 9 } };
const gridLine = { color: "rgba(255,255,255,0.04)" };

// ── Sub-components ─────────────────────────────────────────────────────────────

function PriceChart({ prices, isUp, ticker }) {
  const c = isUp ? "#3CAE6F" : "#E24B4A";
  const first = prices[0]?.close, last = prices[prices.length - 1]?.close;
  const data = {
    labels: prices.map(p => p.date.slice(5)),
    datasets: [{ data: prices.map(p => p.close), borderColor: c, borderWidth: 2, pointRadius: 2, pointBackgroundColor: c, fill: true, backgroundColor: isUp ? "rgba(60,174,111,0.15)" : "rgba(226,75,74,0.12)", tension: 0.4 }],
  };
  return (
    <div className="chart-wrap" role="img"
         aria-label={`${ticker} price chart, last ${prices.length} sessions, from $${fmt(first)} to $${fmt(last)}`}>
      <Line data={data} options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { ...monoTooltip, callbacks: { label: ctx => " $" + fmt(ctx.parsed.y) } } }, scales: { x: { grid: gridLine, ticks: monoTick }, y: { grid: gridLine, ticks: { ...monoTick, callback: v => "$" + v }, position: "right" } } }} />
    </div>
  );
}

function SentimentGauge({ score }) {
  if (score == null) return null;
  const s   = Math.max(-1, Math.min(1, Number(score)));
  const pct = ((s + 1) / 2) * 100;
  const tone = s > 0.2 ? "bullish" : s < -0.2 ? "bearish" : "neutral";
  return (
    <div className="senti-wrap" role="img"
         aria-label={`News sentiment score ${s.toFixed(2)} on a scale from minus 1 bearish to plus 1 bullish, currently ${tone}`}>
      <div className="senti-head">
        <span>SENTIMENT (NEWS + PRICE ACTION)</span>
        <span style={{ color: s > 0.2 ? "var(--green)" : s < -0.2 ? "var(--red)" : "var(--amber)", fontWeight: 700 }}>
          {s > 0 ? "+" : ""}{s.toFixed(2)} {tone.toUpperCase()}
        </span>
      </div>
      <div className="senti-track">
        <div className="senti-center" />
        <div className="senti-marker" style={{ left: `calc(${pct}% - 1px)` }} />
      </div>
    </div>
  );
}

function FundamentalsPanel({ fund }) {
  if (!fund) return (
    <div className="panel">
      <div className="section-label">Valuation</div>
      <div className="no-anomaly">// no fundamentals snapshot — run run_fundamentals.py</div>
    </div>
  );
  const rows = [
    ["Trailing P/E", fund.trailing_pe != null ? fmt(fund.trailing_pe, 1) : "—"],
    ["Forward P/E",  fund.forward_pe  != null ? fmt(fund.forward_pe, 1)  : "—"],
    ["PEG",          fund.peg_ratio   != null ? fmt(fund.peg_ratio, 2)   : "—"],
    ["P/S (TTM)",    fund.price_to_sales != null ? fmt(fund.price_to_sales, 2) : "—"],
    ["Div Yield",    fund.dividend_yield != null ? `${fmt(fund.dividend_yield * 100, 2)}%` : "—"],
    ["Rev Growth",   fund.revenue_growth != null ? `${fmt(fund.revenue_growth * 100, 1)}%` : "—"],
    ["Beta",         fund.beta != null ? fmt(fund.beta, 2) : "—"],
  ];
  return (
    <div className="panel">
      <div className="section-label">Valuation</div>
      <table className="fund-table">
        <tbody>
          {rows.map(([k, v]) => (<tr key={k}><td>{k}</td><td>{v}</td></tr>))}
        </tbody>
      </table>
      <div className="fund-fresh">SNAPSHOT: {fund.snapshot_date || "unknown"} — yfinance .info, not real-time</div>
    </div>
  );
}

function AnomalyPanel({ anomalies }) {
  const sevLabel = { high: "HI", medium: "MD", low: "LO" };
  return (
    <div className="panel">
      <div className="section-label">Anomalies detected ({anomalies.length})</div>
      <div className="anomaly-list">
        {anomalies.length === 0 ? <div className="no-anomaly">// no anomalies flagged</div> : anomalies.map((a, i) => (
          <div className="anomaly-item" key={i}>
            <div className={`anomaly-icon sev-${a.severity}`} aria-label={`severity ${a.severity}`}>{sevLabel[a.severity] || "?"}</div>
            <div>
              <div className="atype">{a.type?.replace(/_/g, " ")}</div>
              <div className="adetail">{a.detail}</div>
              <div className="ainterpret">{a.interpretation}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PriceTable({ prices }) {
  return (
    <div className="panel">
      <div className="section-label">Recent prices</div>
      <table className="price-table">
        <thead><tr><th scope="col">DATE</th><th scope="col">OPEN</th><th scope="col">HIGH</th><th scope="col">LOW</th><th scope="col">CLOSE</th><th scope="col">VOL</th></tr></thead>
        <tbody>
          {prices.slice(-7).map((p, i) => (
            <tr key={i}><td>{p.date.slice(5)}</td><td>{fmt(p.open)}</td><td>{fmt(p.high)}</td><td>{fmt(p.low)}</td><td>{fmt(p.close)}</td><td>{fmtVol(p.volume)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── REGIME VIEW ────────────────────────────────────────────────────────────────

function RegimeView({ regime }) {
  if (!regime) return <div className="empty-state">// no regime data — run run_regime.py</div>;

  const meta  = REGIME_META[regime.regime] || REGIME_META.CHOPPY;
  const rules = parseJ(regime.rules_fired);

  return (
    <div className="full-panel">
      <div className="regime-banner" style={{ background: meta.bg, borderColor: meta.border }}>
        <div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: meta.color, letterSpacing: ".1em", marginBottom: 6 }}>
            MARKET REGIME — DETERMINISTIC, NON-LLM
          </div>
          <div className="regime-name" style={{ color: meta.color }}>{regime.regime}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginBottom: 6 }}>{regime.regime_date}</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: meta.color, border: `1px solid ${meta.border}`, padding: "4px 12px", borderRadius: 4 }}>
            RISK FLOOR: {regime.risk_floor || "NONE"}
          </div>
        </div>
      </div>

      <div className="metrics-grid">
        {[
          { label: "SPY VS 200-DAY MA", value: `${regime.spy_vs_ma200_pct > 0 ? "+" : ""}${fmt(regime.spy_vs_ma200_pct)}%`, cls: signCls(regime.spy_vs_ma200_pct) },
          { label: "SPY DRAWDOWN (52W)", value: `${fmt(regime.spy_drawdown_pct)}%`, cls: regime.spy_drawdown_pct <= -10 ? "neg" : regime.spy_drawdown_pct <= -5 ? "neutral" : "pos" },
          { label: "VIX", value: regime.vix_close != null ? fmt(regime.vix_close, 1) : "—", cls: regime.vix_close >= 30 ? "neg" : regime.vix_close >= 20 ? "neutral" : "pos" },
          { label: "QQQ VS 200-DAY MA", value: regime.qqq_vs_ma200_pct != null ? `${regime.qqq_vs_ma200_pct > 0 ? "+" : ""}${fmt(regime.qqq_vs_ma200_pct)}%` : "—", cls: signCls(regime.qqq_vs_ma200_pct) },
        ].map(m => (
          <div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls}`}>{m.value}</div></div>
        ))}
      </div>

      <div className="metrics-grid-3">
        {[
          { label: "SPY CLOSE", value: `$${fmt(regime.spy_close)}` },
          { label: "SPY 50-DAY MA", value: `$${fmt(regime.spy_ma50)}` },
          { label: "MA CROSS", value: regime.death_cross ? "DEATH CROSS" : "GOLDEN", cls: regime.death_cross ? "neg" : "pos" },
        ].map(m => (
          <div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls || ""}`}>{m.value}</div></div>
        ))}
      </div>

      {rules.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div className="section-label">Rules fired</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {rules.map((r, i) => <span key={i} className="rule-chip">{r}</span>)}
          </div>
        </div>
      )}

      {regime.rationale && (
        <div className="summary-block" style={{ borderColor: meta.color }}>
          <div className="summary-label" style={{ color: meta.color }}>CLASSIFICATION RATIONALE — AUDIT TRAIL</div>
          <div className="summary-text">{regime.rationale}</div>
        </div>
      )}

      <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", lineHeight: 1.7 }}>
        run_id: {regime.run_id} — Regime floors are enforced in Python by the Allocator
        (cash ladder + position caps). The model proposes; the code disposes.
      </div>
    </div>
  );
}

// ── OPTIONS VIEW ───────────────────────────────────────────────────────────────

function OptionsView({ optionsFlow, optionsStrikes, focusTicker, onTicker }) {
  const [activeTicker, setTicker] = useState(
    focusTicker && optionsFlow.some(o => o.ticker === focusTicker) ? focusTicker : optionsFlow[0]?.ticker || null);

  if (!optionsFlow.length) return <div className="empty-state">// no options data — run run_options.py</div>;

  // Group by ticker
  const byTicker = {};
  optionsFlow.forEach(row => {
    if (!byTicker[row.ticker]) byTicker[row.ticker] = [];
    byTicker[row.ticker].push(row);
  });

  const tickers   = Object.keys(byTicker);
  const rows      = byTicker[activeTicker] || [];
  const nearestRow = rows.sort((a, b) => a.days_to_expiry - b.days_to_expiry)[0];

  const flowCls = { BULLISH: "flow-bullish", BEARISH: "flow-bearish", NEUTRAL: "flow-neutral" };

  // PCR bar: 0=pure calls, 1=50/50, 2=pure puts. Centre at 1.
  const pcrToWidth = pcr => Math.min(Math.round((pcr / 2) * 100), 100);
  const pcrColor   = pcr => pcr < 0.7 ? "var(--green)" : pcr > 1.2 ? "var(--red)" : "var(--amber)";

  // OI bar chart for strikes
  const strikes = optionsStrikes.filter(s => s.ticker === activeTicker && s.expiry_date === nearestRow?.expiry_date);
  const callStrikes = strikes.filter(s => s.option_type === "CALL").sort((a, b) => b.open_interest - a.open_interest).slice(0, 6);
  const putStrikes  = strikes.filter(s => s.option_type === "PUT").sort((a, b) => b.open_interest - a.open_interest).slice(0, 6);

  const oiChartData = {
    labels: [...callStrikes.map(s => `$${s.strike}C`), ...putStrikes.map(s => `$${s.strike}P`)],
    datasets: [{
      label: "Open Interest",
      data: [...callStrikes.map(s => s.open_interest), ...putStrikes.map(s => s.open_interest)],
      backgroundColor: [...callStrikes.map(() => "rgba(60,174,111,0.5)"), ...putStrikes.map(() => "rgba(226,75,74,0.5)")],
      borderColor: [...callStrikes.map(() => "#3CAE6F"), ...putStrikes.map(() => "#E24B4A")],
      borderWidth: 1,
    }],
  };

  return (
    <div className="full-panel">
      {/* Ticker tabs */}
      <div style={{ marginBottom: 16 }}>
        <div className="section-label" style={{ marginBottom: 10 }} id="options-ticker-label">OPTIONS FLOW — SELECT TICKER</div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} role="tablist" aria-labelledby="options-ticker-label">
          {tickers.map(t => (
            <button key={t} role="tab" aria-selected={t === activeTicker}
                    className={`expiry-tab ${t === activeTicker ? "active" : ""}`}
                    onClick={() => setTicker(t)} style={{ fontSize: 12 }}>{t}</button>
          ))}
        </div>
      </div>

      {nearestRow && (
        <>
          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
            <div>
              <div style={{ fontFamily: "var(--mono)", fontSize: 22, fontWeight: 700, color: "var(--amber)" }}>
                {onTicker ? <button className="tk-link" onClick={() => onTicker(activeTicker)} title={`Open ${activeTicker}'s signal story`}>{activeTicker}</button> : activeTicker}
              </div>
              <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)", marginTop: 3 }}>Nearest expiry: {nearestRow.expiry_date} ({nearestRow.days_to_expiry}d)</div>
            </div>
            <div className={`flow-badge ${flowCls[nearestRow.flow_signal] || "flow-neutral"}`}>{nearestRow.flow_signal || "NEUTRAL"}</div>
          </div>

          {/* Metrics */}
          <div className="metrics-grid" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
            {[
              { label: "PCR (VOLUME)", value: fmt(nearestRow.put_call_ratio_volume, 3), cls: nearestRow.put_call_ratio_volume < 0.7 ? "pos" : nearestRow.put_call_ratio_volume > 1.2 ? "neg" : "neutral" },
              { label: "PCR (OI)",     value: fmt(nearestRow.put_call_ratio_oi, 3),     cls: nearestRow.put_call_ratio_oi < 0.7 ? "pos" : nearestRow.put_call_ratio_oi > 1.2 ? "neg" : "neutral" },
              { label: "MAX PAIN",     value: `$${fmt(nearestRow.max_pain_price)}`,      cls: "" },
              { label: "CURRENT PRICE", value: `$${fmt(nearestRow.current_price)}`,     cls: "" },
            ].map(m => (
              <div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls}`}>{m.value}</div></div>
            ))}
          </div>

          {/* PCR bar */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)" }}>
              <span style={{ color: "var(--green)" }}>← CALLS DOMINANT</span>
              <span style={{ color: "var(--text-dimmer)" }}>BALANCED</span>
              <span style={{ color: "var(--red)" }}>PUTS DOMINANT →</span>
            </div>
            <div className="pcr-bar-wrap" role="img" aria-label={`Put call ratio ${fmt(nearestRow.put_call_ratio_volume, 2)}`}>
              <div className="pcr-bar" style={{ width: `${pcrToWidth(nearestRow.put_call_ratio_volume)}%`, background: pcrColor(nearestRow.put_call_ratio_volume) }} />
            </div>
          </div>

          {/* Signal reasoning */}
          {nearestRow.signal_reasoning && (
            <div className="summary-block" style={{ marginBottom: 16 }}>
              <div className="summary-label">FLOW ANALYSIS</div>
              <div className="summary-text">{nearestRow.signal_reasoning}</div>
            </div>
          )}

          {/* Two-column: call+put volumes | OI chart */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div>
              <div className="section-label">Volume by Side</div>
              {rows.map((row, i) => (
                <div key={i} style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
                    <span>{row.expiry_date} ({row.days_to_expiry}d)</span>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <div style={{ flex: 1, height: 6, background: "var(--bg4)", borderRadius: 3, overflow: "hidden" }}>
                      <div style={{ width: `${Math.min(100, (row.call_volume / (row.call_volume + row.put_volume + 1)) * 100)}%`, height: "100%", background: "var(--green)" }} />
                    </div>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--green)", minWidth: 50 }}>{fmtK(row.call_volume)}C</span>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--red)", minWidth: 50 }}>{fmtK(row.put_volume)}P</span>
                  </div>
                </div>
              ))}
            </div>

            {(callStrikes.length > 0 || putStrikes.length > 0) && (
              <div>
                <div className="section-label">Top Strikes by OI (nearest expiry)</div>
                <div className="chart-wrap-lg" style={{ height: 200 }} role="img" aria-label={`Open interest by strike for ${activeTicker}`}>
                  <Bar data={oiChartData} options={{ responsive: true, maintainAspectRatio: false, indexAxis: "y", plugins: { legend: { display: false }, tooltip: { ...monoTooltip, callbacks: { label: ctx => ` OI: ${fmtK(ctx.parsed.x)}` } } }, scales: { x: { grid: gridLine, ticks: monoTick }, y: { grid: { display: false }, ticks: { ...monoTick, font: { family: "Space Mono", size: 9 } } } } }} />
                </div>
              </div>
            )}
          </div>

          {/* Unusual activity */}
          {(() => {
            const unusual = parseJ(nearestRow.unusual_activity);
            if (!unusual.length) return null;
            return (
              <div style={{ marginTop: 16 }}>
                <div className="section-label">Unusual Activity</div>
                {unusual.map((u, i) => (
                  <div className="unusual-item" key={i}>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: u.type === "CALL" ? "var(--green)" : "var(--red)", fontWeight: 700 }}>{u.type} ${u.strike}</span>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dim)" }}>Vol {fmtK(u.volume)} / OI {fmtK(u.open_interest)}</span>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--amber)", fontWeight: 700 }}>{u.ratio}x ratio</span>
                  </div>
                ))}
              </div>
            );
          })()}
        </>
      )}
    </div>
  );
}

// ── EARNINGS VIEW ──────────────────────────────────────────────────────────────

function EarningsView({ earningsData, focusTicker, onTicker }) {
  if (!earningsData.length) return <div className="empty-state">// no earnings data — run run_earnings.py</div>;

  const sorted = [...earningsData].sort((a, b) => {
    const da = a.days_to_earnings ?? 9999;
    const db = b.days_to_earnings ?? 9999;
    return da - db;
  });

  const earnCls = risk => ({ HIGH: "earn-high", MEDIUM: "earn-med", LOW: "earn-low" }[risk] || "earn-low");
  const dteCls  = risk => ({ HIGH: "var(--red)", MEDIUM: "var(--amber)", LOW: "var(--green)" }[risk] || "var(--text-dim)");

  return (
    <div className="full-panel">
      <div style={{ marginBottom: 20 }}>
        <div className="section-label" style={{ marginBottom: 4 }}>EARNINGS CALENDAR</div>
        <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>
          Showing {sorted.length} tickers — sorted by proximity to earnings
        </div>
      </div>

      <div className="earnings-grid">
        {sorted.map((e, i) => {
          const surprises = parseJ(e.historical_surprises);
          const streak    = e.beat_streak || 0;
          const dte       = e.days_to_earnings;

          return (
            <div className={`earnings-card ${earnCls(e.earnings_risk)} ${e.ticker === focusTicker ? "focus-card" : ""}`} key={i}>
              <div className="earn-ticker">
                {onTicker ? <button className="tk-link" onClick={() => onTicker(e.ticker)} title={`Open ${e.ticker}'s signal story`}>{e.ticker}</button> : e.ticker}
              </div>
              <div className="earn-date">{e.next_earnings_date || "Date unknown"}</div>

              <div style={{ display: "flex", alignItems: "flex-end", gap: 10, marginBottom: 12 }}>
                <div>
                  <div className="earn-dte" style={{ color: dteCls(e.earnings_risk) }}>
                    {dte != null ? dte : "—"}
                  </div>
                  <div className="earn-label">DAYS TO EARNINGS ({e.earnings_risk || "LOW"} RISK)</div>
                </div>
                <div style={{ marginBottom: 6 }}>
                  <span className={`beat-streak ${streak >= 0 ? "beat-pos" : "beat-neg"}`}>
                    {streak >= 0 ? `+${streak}` : streak} beat streak
                  </span>
                </div>
              </div>

              {/* EPS estimate */}
              {e.eps_estimate != null && (
                <div style={{ display: "flex", gap: 16, marginBottom: 10 }}>
                  <div>
                    <div className="earn-label">EPS ESTIMATE</div>
                    <div style={{ fontFamily: "var(--mono)", fontSize: 13, fontWeight: 700, color: "var(--text)" }}>${fmt(e.eps_estimate, 2)}</div>
                  </div>
                  {e.eps_actual_last != null && (
                    <div>
                      <div className="earn-label">LAST ACTUAL</div>
                      <div style={{ fontFamily: "var(--mono)", fontSize: 13, fontWeight: 700, color: "var(--text)" }}>${fmt(e.eps_actual_last, 2)}</div>
                    </div>
                  )}
                  {e.eps_surprise_last_pct != null && (
                    <div>
                      <div className="earn-label">LAST SURPRISE</div>
                      <div style={{ fontFamily: "var(--mono)", fontSize: 13, fontWeight: 700, color: e.eps_surprise_last_pct > 0 ? "var(--green)" : "var(--red)" }}>
                        {e.eps_surprise_last_pct > 0 ? "+" : ""}{fmt(e.eps_surprise_last_pct)}%
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Surprise history */}
              {surprises.length > 0 && (
                <div>
                  <div className="earn-label" style={{ marginBottom: 4 }}>SURPRISE HISTORY</div>
                  {surprises.slice(0, 4).map((s, j) => (
                    <div className="surprise-row" key={j}>
                      <span style={{ color: "var(--text-dimmer)" }}>{s.date?.slice(0, 7)}</span>
                      <span style={{ color: "var(--text-dim)" }}>Est ${fmt(s.eps_estimate ?? 0)}</span>
                      <span style={{ color: s.surprise_pct > 0 ? "var(--green)" : s.surprise_pct < 0 ? "var(--red)" : "var(--text-dim)" }}>
                        {s.surprise_pct != null ? `${s.surprise_pct > 0 ? "+" : ""}${fmt(s.surprise_pct)}%` : "—"}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {/* Claude commentary */}
              {e.claude_commentary && (
                <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border)", fontSize: 11, color: "var(--text-dimmer)", lineHeight: 1.55, fontStyle: "italic" }}>
                  {e.claude_commentary}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── MACRO VIEW ─────────────────────────────────────────────────────────────────

function MacroView({ macroSnapshot, macroIndicators }) {
  if (!macroSnapshot && !macroIndicators.length) {
    return <div className="empty-state">// no macro data — run run_macro.py</div>;
  }

  const snap = macroSnapshot || {};

  const regimeColors = {
    INFLATIONARY:  { bg: "var(--red-dim)",    border: "rgba(226,75,74,.3)",    text: "var(--red)" },
    DISINFLATIONARY:{ bg: "var(--amber-glow)", border: "var(--border-amber)",   text: "var(--amber)" },
    STABLE:        { bg: "var(--green-dim)",   border: "rgba(60,174,111,.3)",   text: "var(--green)" },
    DEFLATIONARY:  { bg: "var(--blue-dim)",    border: "rgba(55,138,221,.2)",   text: "var(--blue)" },
  };
  const impactColors = {
    POSITIVE: "var(--green)", NEGATIVE: "var(--red)", NEUTRAL: "var(--text-dimmer)",
  };

  const regime = snap.macro_regime || "STABLE";
  const rc     = regimeColors[regime] || regimeColors.STABLE;

  // Build CPI + PPI chart data
  const cpiPts = macroIndicators.filter(r => r.indicator === "CPI").sort((a, b) => a.period_date.localeCompare(b.period_date)).slice(-18);
  const ppiPts = macroIndicators.filter(r => r.indicator === "PPI").sort((a, b) => a.period_date.localeCompare(b.period_date)).slice(-18);

  const labels = cpiPts.map(p => p.period_date.slice(0, 7));
  const trendData = {
    labels,
    datasets: [
      {
        label: "CPI YoY %",
        data: cpiPts.map(p => p.yoy_change),
        borderColor: "#EF9F27", borderWidth: 2, pointRadius: 3, pointBackgroundColor: "#EF9F27",
        fill: false, tension: 0.3,
      },
      {
        label: "PPI YoY %",
        data: ppiPts.slice(-labels.length).map(p => p.yoy_change),
        borderColor: "#9B6DFF", borderWidth: 2, pointRadius: 3, pointBackgroundColor: "#9B6DFF",
        fill: false, tension: 0.3, borderDash: [4, 3],
      },
    ],
  };

  const trendOptions = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { display: true, labels: { color: "#969489", font: { family: "Space Mono", size: 9 }, boxWidth: 12 } },
      tooltip: { ...monoTooltip, callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.parsed.y)}%` } },
    },
    scales: {
      x: { grid: gridLine, ticks: monoTick },
      y: { grid: gridLine, ticks: { ...monoTick, callback: v => v + "%" }, position: "right" },
    },
  };

  return (
    <div className="full-panel">
      {/* Regime banner */}
      <div className="macro-regime-banner" style={{ background: rc.bg, border: `1px solid ${rc.border}`, marginBottom: 20 }}>
        <div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: rc.text, letterSpacing: ".1em", marginBottom: 6 }}>MACRO REGIME</div>
          <div className="macro-regime-label" style={{ color: rc.text }}>{regime}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginBottom: 6 }}>{snap.snapshot_date}</div>
          <div className="macro-impact" style={{ color: impactColors[snap.equity_impact] || "var(--text-dim)", borderColor: (impactColors[snap.equity_impact] || "var(--border)") + "55" }}>
            EQUITY: {snap.equity_impact || "NEUTRAL"}
          </div>
        </div>
      </div>

      {/* CPI / PPI stats */}
      <div className="macro-grid">
        {[
          { label: "CPI YoY", value: snap.cpi_latest, yoy: snap.cpi_yoy, color: "#EF9F27" },
          { label: "PPI YoY", value: snap.ppi_latest, yoy: snap.ppi_yoy, color: "#9B6DFF" },
        ].map(s => (
          <div className="macro-stat-card" key={s.label}>
            <div className="macro-stat-label" style={{ color: s.color }}>{s.label}</div>
            <div className="macro-stat-value" style={{ color: "var(--text)" }}>{s.value != null ? fmt(s.value, 1) : "—"}</div>
            <div className="macro-stat-change" style={{ color: s.yoy > 3.5 ? "var(--red)" : s.yoy > 2 ? "var(--amber)" : "var(--green)" }}>
              {s.yoy != null ? `${s.yoy > 0 ? "+" : ""}${fmt(s.yoy)}% YoY` : "—"}
            </div>
          </div>
        ))}
      </div>

      {/* Trend chart */}
      {(cpiPts.length > 2 || ppiPts.length > 2) && (
        <div style={{ marginBottom: 20 }}>
          <div className="section-label">CPI & PPI Year-over-Year Trend</div>
          <div className="chart-wrap-lg" role="img" aria-label={`CPI and PPI year over year trend, latest CPI ${fmt(snap.cpi_yoy)}%, PPI ${fmt(snap.ppi_yoy)}%`}>
            <Line data={trendData} options={trendOptions} />
          </div>
        </div>
      )}

      {/* Fed target reference */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", letterSpacing: ".08em" }}>REFERENCE:</div>
        {[
          { label: "Fed Target",  value: "2.0%",  color: "var(--green)" },
          { label: "Watch Zone",  value: "2-3.5%", color: "var(--amber)" },
          { label: "Elevated",    value: ">3.5%",  color: "var(--red)" },
        ].map(ref => (
          <span key={ref.label} style={{ fontFamily: "var(--mono)", fontSize: 10, color: ref.color, padding: "2px 8px", border: `1px solid ${ref.color}44`, borderRadius: 3 }}>
            {ref.label}: {ref.value}
          </span>
        ))}
      </div>

      {/* Regime narrative */}
      {snap.regime_summary && (
        <div className="summary-block" style={{ borderColor: rc.text }}>
          <div className="summary-label" style={{ color: rc.text }}>MACRO ANALYSIS</div>
          <div className="summary-text">{snap.regime_summary}</div>
        </div>
      )}

      {/* Monthly data table */}
      {cpiPts.length > 0 && (
        <div>
          <div className="section-label">Recent Monthly Readings</div>
          <table className="price-table" style={{ marginTop: 0 }}>
            <thead><tr>
              <th scope="col" style={{ textAlign: "left" }}>PERIOD</th>
              <th scope="col">CPI</th><th scope="col">CPI YoY</th><th scope="col">CPI MoM</th>
              <th scope="col">PPI</th><th scope="col">PPI YoY</th>
            </tr></thead>
            <tbody>
              {cpiPts.slice(-8).reverse().map((cp, i) => {
                const pp = ppiPts.find(p => p.period_date.slice(0, 7) === cp.period_date.slice(0, 7));
                return (
                  <tr key={i}>
                    <td style={{ textAlign: "left", color: i === 0 ? "var(--amber)" : "var(--text-dimmer)" }}>{cp.period_date.slice(0, 7)}</td>
                    <td>{fmt(cp.value, 1)}</td>
                    <td style={{ color: cp.yoy_change > 3.5 ? "var(--red)" : cp.yoy_change > 2 ? "var(--amber)" : "var(--green)" }}>{fmt(cp.yoy_change)}%</td>
                    <td style={{ color: signCls(cp.mom_change) }}>{cp.mom_change > 0 ? "+" : ""}{fmt(cp.mom_change)}%</td>
                    <td>{pp ? fmt(pp.value, 1) : "—"}</td>
                    <td style={{ color: pp && pp.yoy_change > 4 ? "var(--red)" : pp && pp.yoy_change > 2 ? "var(--amber)" : "var(--green)" }}>{pp ? `${fmt(pp.yoy_change)}%` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── RISK VIEW (unchanged) ─────────────────────────────────────────────────────
function styleFromString(s) {
  if (!s) return {};
  return Object.fromEntries(s.split(";").filter(Boolean).map(p => { const [k, v] = p.split(":").map(x => x.trim()); return [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v]; }));
}

function RiskView({ riskPlan, riskDetails, focusTicker, onTicker }) {
  if (!riskPlan) return <div className="empty-state">// no risk assessment — run run_risk.py</div>;
  // risk_saver writes market_risk_level; portfolio_risk_rating is the legacy name.
  const rating    = riskPlan.market_risk_level || riskPlan.portfolio_risk_rating;
  const ratingCls = { LOW: "risk-low", MODERATE: "risk-moderate", HIGH: "risk-high", CRITICAL: "risk-critical" }[rating] || "";
  const riskFlags = parseJ(riskPlan.risk_flags);
  return (
    <div className="full-panel">
      <div className="risk-header">
        <div>
          <div className="section-label" style={{ marginBottom: 6 }}>PORTFOLIO RISK ASSESSMENT</div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div className={`risk-rating ${ratingCls}`}>{rating}</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>Score: {riskPlan.overall_risk_score}/10</div>
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>{riskPlan.assessment_date}</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginTop: 4 }}>{riskPlan.run_id}</div>
        </div>
      </div>
      <div className="metrics-grid-3" style={{ marginBottom: 16 }}>
        {[
          { label: "RISK SCORE", value: `${riskPlan.overall_risk_score}/10`, cls: riskPlan.overall_risk_score >= 7 ? "neg" : riskPlan.overall_risk_score >= 4 ? "neutral" : "pos" },
          { label: "TICKERS ASSESSED", value: riskPlan.total_tickers_assessed, cls: "" },
          { label: "HIGH RISK TICKERS", value: riskPlan.high_risk_tickers || 0, cls: riskPlan.high_risk_tickers > 0 ? "neg" : "" },
        ].map(m => (<div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls}`}>{m.value}</div></div>))}
      </div>
      {riskFlags.length > 0 && (<div style={{ marginBottom: 16 }}><div className="section-label">Portfolio Risk Flags</div><div className="flags-list">{riskFlags.map((f, i) => <span key={i} className="flag-chip warn">{f}</span>)}</div></div>)}
      {riskPlan.summary && (<div className="summary-block" style={{ marginBottom: 16 }}><div className="summary-label">RISK SUMMARY</div><div className="summary-text">{riskPlan.summary}</div></div>)}
      {riskDetails.length > 0 && (
        <><div className="section-label">Per-Ticker Risk Details</div>
        <table className="risk-table"><thead><tr><th scope="col">TICKER</th><th scope="col">SCORE</th><th scope="col">RATING</th><th scope="col">MAX POS %</th><th scope="col">FLAGS</th></tr></thead>
        <tbody>{riskDetails.map((r, i) => {
          const flags = parseJ(r.risk_flags);
          const barW = Math.round((r.risk_score / 10) * 60);
          const barColor = r.risk_score >= 7 ? "var(--red)" : r.risk_score >= 4 ? "var(--amber)" : "var(--green)";
          return (<tr key={i} className={r.ticker === focusTicker ? "focus-row" : ""}><td>{onTicker ? <button className="tk-link" onClick={() => onTicker(r.ticker)} title={`Open ${r.ticker}'s signal story`}>{r.ticker}</button> : r.ticker}</td><td><span className="risk-score-bar" style={{ width: barW, background: barColor }} aria-hidden="true" />{r.risk_score}/10</td><td style={{ color: r.risk_rating === "HIGH" || r.risk_rating === "CRITICAL" ? "var(--red)" : r.risk_rating === "MODERATE" ? "var(--amber)" : "var(--green)" }}>{r.risk_rating}</td><td>{r.max_position_pct != null ? `${fmt(r.max_position_pct)}%` : "—"}</td><td><div className="flags-list" style={{ justifyContent: "flex-end" }}>{flags.length === 0 ? <span style={{ color: "var(--text-dimmer)", fontSize: 10 }}>none</span> : flags.slice(0, 3).map((f, j) => <span key={j} className="flag-chip">{f.replace(/_/g, " ")}</span>)}</div></td></tr>);
        })}</tbody></table></>
      )}
    </div>
  );
}

// ── ALLOCATION VIEW (v3: sector caps + enforcer panel) ────────────────────────
function methodChipCls(method) {
  if (!method) return "method-cr";
  const m = method.toUpperCase();
  if (m.includes("SECTOR"))  return "method-sector";
  if (m.includes("REGIME") || m.includes("INVERSE")) return "method-regime";
  if (m.includes("POLICY"))  return "method-rc";
  if (m.includes("SIGNAL"))  return "method-sw";
  if (m.includes("RISK"))    return "method-rc";
  if (m.includes("EXCL"))    return "method-ex";
  return "method-cr";
}

function CapBar({ name, value, cap }) {
  const limitPct = Math.min((value / cap) * 100, 100);
  const atLimit  = value >= cap - 0.05;
  const color    = atLimit ? "var(--amber)" : "var(--green)";
  // bar scale: cap sits at 80% of track width so overage direction is visible
  const fillW    = Math.min((value / cap) * 80, 100);
  return (
    <div className={`cap-row ${atLimit ? "cap-at-limit" : ""}`} role="img"
         aria-label={`${name.replace(/_/g, " ")} exposure ${fmt(value, 1)} percent of a ${cap} percent cap${atLimit ? ", at limit" : ""}`}>
      <div className="cap-head">
        <span className="cap-name">{name.replace(/_/g, " ")}</span>
        <span className="cap-nums">
          <span style={{ color, fontWeight: 700 }}>{fmt(value, 1)}%</span> / cap {cap}%
          {atLimit && <span style={{ color: "var(--amber)", marginLeft: 6 }}>AT LIMIT</span>}
        </span>
      </div>
      <div className="cap-track">
        <div className="cap-fill" style={{ width: `${fillW}%`, background: color }} />
        <div className="cap-marker" style={{ left: "80%" }} title={`cap ${cap}%`} />
      </div>
    </div>
  );
}

function AllocView({ allocPlan, allocDetails, focusTicker, onTicker }) {
  if (!allocPlan) return <div className="empty-state">// no allocation plan — run run_allocator.py</div>;

  const sectorExp  = parseObj(allocPlan.sector_exposure);
  const themeExp   = parseObj(allocPlan.theme_exposure);
  const violations = parseJ(allocPlan.constraint_violations);
  const hasNewData = Object.keys(sectorExp).length > 0 || violations.length > 0
                     || allocPlan.constraint_violations != null;

  const donutLabels = allocDetails.map(d => d.ticker);
  const donutData   = allocDetails.map(d => parseFloat(d.allocated_pct) || 0);
  const donutColors = allocDetails.map((d, i) => d.ticker === "CASH" ? "#6E6D66" : (d.sizing_method || "").toUpperCase().includes("EXCL") ? "#E24B4A" : ["#3CAE6F","#EF9F27","#378ADD","#9B6DFF","#2ECFA8","#E24B4A"][i % 6]);
  const donutChartData = { labels: donutLabels, datasets: [{ data: donutData, backgroundColor: donutColors, borderWidth: 1, borderColor: "#141517" }] };

  return (
    <div className="full-panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div><div className="section-label" style={{ marginBottom: 6 }}>PORTFOLIO ALLOCATION PLAN</div><div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>{allocPlan.plan_date}</div></div>
        <div style={{ textAlign: "right" }}><div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)" }}>{allocPlan.run_id}</div></div>
      </div>

      <div className="metrics-grid-3" style={{ marginBottom: 16 }}>
        {[{ label: "INVESTED", value: `${fmt(allocPlan.invested_pct ?? 0)}%`, cls: "" }, { label: "CASH RESERVE", value: `${fmt(allocPlan.cash_pct ?? 0)}%`, cls: "neutral" }, { label: "TICKERS", value: allocPlan.total_tickers, cls: "" }].map(m => (<div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls}`}>{m.value}</div></div>))}
      </div>

      {/* ── Constraint enforcement: model proposed → code disposed ── */}
      {hasNewData && (
        <div className="enforce-panel">
          <div className="enforce-head">
            <span className="enforce-title">CONSTRAINT ENFORCEMENT — POST-LLM, PYTHON-GUARANTEED</span>
            <span className={`enforce-badge ${violations.length ? "enforce-corrected" : "enforce-clean"}`}>
              {violations.length ? `${violations.length} CORRECTED` : "FULLY COMPLIANT"}
            </span>
          </div>
          {violations.length === 0 ? (
            <div className="enforce-empty">// model output respected every cap, floor, and exclusion — no corrections needed</div>
          ) : (
            violations.map((v, i) => (
              <div className="violation-item" key={i}>
                <span className="violation-n">{String(i + 1).padStart(2, "0")}</span>
                <span className="violation-text">{v}</span>
              </div>
            ))
          )}
        </div>
      )}

      {/* ── Sector & theme exposure vs caps ── */}
      {Object.keys(sectorExp).length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <div className="section-label">Sector exposure vs caps</div>
          {Object.entries(sectorExp).map(([sec, val]) => (
            <CapBar key={sec} name={sec} value={Number(val)} cap={SECTOR_CAPS_UI[sec] ?? DEFAULT_SECTOR_CAP_UI} />
          ))}
          {themeExp.TECH != null && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
              <CapBar name="TECH (ALL TECH SECTORS COMBINED)" value={Number(themeExp.TECH)} cap={TECH_THEME_CAP_UI} />
            </div>
          )}
        </div>
      )}

      {allocPlan.strategy_note && (<div className="summary-block" style={{ marginBottom: 16 }}><div className="summary-label">STRATEGY</div><div className="summary-text">{allocPlan.strategy_note}</div></div>)}

      <div className="alloc-grid">
        <div><div className="section-label">Allocation Details</div>
        <table className="alloc-table"><thead><tr><th scope="col">TICKER</th><th scope="col">SECTOR</th><th scope="col">ALLOCATED</th><th scope="col">METHOD</th><th scope="col">RATIONALE</th></tr></thead>
        <tbody>{allocDetails.map((d, i) => { const pct = parseFloat(d.allocated_pct) || 0; const barColor = d.ticker === "CASH" ? "var(--text-dimmer)" : (d.sizing_method || "").toUpperCase().includes("EXCL") ? "var(--red)" : "var(--green)"; return (<tr key={i} className={d.ticker === focusTicker ? "focus-row" : ""}><td>{onTicker && d.ticker !== "CASH" ? <button className="tk-link" onClick={() => onTicker(d.ticker)} title={`Open ${d.ticker}'s signal story`}>{d.ticker}</button> : d.ticker}</td><td style={{ fontSize: 9, color: "var(--text-dimmer)" }}>{(d.sector || "").replace(/_/g, " ") || "—"}</td><td><div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "flex-end" }}><div style={{ width: 50, height: 5, background: "var(--bg4)", borderRadius: 3, overflow: "hidden" }} aria-hidden="true"><div style={{ width: `${Math.min(pct, 100)}%`, height: "100%", background: barColor, borderRadius: 3 }} /></div>{fmt(pct)}%</div></td><td><span className={`method-chip ${methodChipCls(d.sizing_method)}`}>{d.sizing_method || "—"}</span></td><td title={d.allocation_rationale || ""} style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-dimmer)", fontSize: 10 }}>{d.allocation_rationale || "—"}</td></tr>); })}</tbody></table></div>
        {donutData.some(v => v > 0) && (<div><div className="section-label" style={{ textAlign: "center" }}>Allocation Mix</div><div className="donut-wrap" role="img" aria-label={`Allocation mix donut chart across ${donutLabels.length} positions`}><Doughnut data={donutChartData} options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { ...monoTooltip, callbacks: { label: ctx => ` ${ctx.label}: ${ctx.parsed}%` } } }, cutout: "68%" }} /></div><div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10, justifyContent: "center" }}>{donutLabels.map((l, i) => (<div key={i} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, fontFamily: "var(--mono)", color: "var(--text-dim)" }}><div style={{ width: 8, height: 8, borderRadius: 2, background: donutColors[i] }} aria-hidden="true" />{l}</div>))}</div></div>)}
      </div>
    </div>
  );
}

// ── REPORT + WATCHDOG (trimmed, unchanged logic) ───────────────────────────────
function ReportView({ report, reportTickers }) {
  if (!report) return <div className="empty-state">// no report — run run_report_writer.py</div>;
  const themes = Array.isArray(report.key_themes) ? report.key_themes : (typeof report.key_themes === "string" ? JSON.parse(report.key_themes) : []);
  const statusColor = report.portfolio_stance === "BULLISH" ? "var(--green)" : report.portfolio_stance === "BEARISH" ? "var(--red)" : "var(--amber)";
  return (
    <div className="full-panel"><div className="report-wrap">
      <div style={{ marginBottom: 20 }}>
        <div className="section-label" style={{ marginBottom: 8 }}>INTELLIGENCE REPORT</div>
        <div className="report-meta">
          <span className="report-tag">{report.report_date}</span>
          <span className="report-tag">{report.run_id}</span>
          {report.portfolio_stance && <span className="report-tag" style={{ color: statusColor, borderColor: statusColor }}>{report.portfolio_stance}</span>}
          {report.confidence_level && <span className="report-tag">{report.confidence_level} CONFIDENCE</span>}
        </div>
      </div>
      {report.executive_summary && (<div className="report-section"><div className="report-section-title">Executive Summary</div><div className="summary-block" style={{ marginBottom: 0 }}><div className="summary-text">{report.executive_summary}</div></div></div>)}
      {report.market_outlook && (<div className="report-section"><div className="report-section-title">Market Outlook</div><div className="report-body"><p>{report.market_outlook}</p></div></div>)}
      {report.risk_highlights && (<div className="report-section"><div className="report-section-title">Risk Highlights</div><div className="report-body"><p>{report.risk_highlights}</p></div></div>)}
      {themes.length > 0 && (<div className="report-section"><div className="report-section-title">Key Themes</div><div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>{themes.map((t, i) => <span key={i} className="chip" style={{ color: "var(--amber)", borderColor: "var(--border-amber)", background: "var(--amber-glow)" }}>{t}</span>)}</div></div>)}
      {reportTickers.length > 0 && (<div className="report-section"><div className="report-section-title">Per-Ticker Analysis</div><div className="report-ticker-grid">{reportTickers.map(t => (<div className="report-ticker-card" key={t.ticker}><div className="report-ticker-name">{t.ticker}</div><div className="report-ticker-text">{t.one_liner}</div>{t.full_narrative && <div className="report-ticker-text" style={{ marginTop: 6, color: "var(--text-dimmer)" }}>{t.full_narrative}</div>}</div>))}</div></div>)}
      {report.watchdog_notes && (<div className="report-section"><div className="report-section-title">Watchdog Notes</div><div className="report-body"><p>{report.watchdog_notes}</p></div></div>)}
    </div></div>
  );
}

// ── Pipeline Metrics View ─────────────────────────────────────────────────────

const PM_STAGE_ORDER  = ["regime","macro","earnings","fundamentals","pipeline","options","analyst","drift","risk","allocator","report","watchdog","newsletter"];
const PM_STAGE_LABEL  = {
  regime:"REGIME", macro:"MACRO", earnings:"EARNINGS", fundamentals:"FUNDAMENTALS",
  pipeline:"OHLCV INGEST", options:"OPTIONS",
  analyst:"ANALYST", drift:"DRIFT TRACKER", risk:"RISK MGR", allocator:"ALLOCATOR",
  report:"REPORT WTR", watchdog:"WATCHDOG", newsletter:"NEWSLETTER",
};

function PipelineView({ pipelineMetrics }) {
  if (!pipelineMetrics.length) return (
    <div className="empty-state">// no pipeline metrics — run run_all.py then apply pipeline_schema.sql in Supabase</div>
  );

  // Group rows by pipeline_run_id, sort newest first
  const grouped = {};
  pipelineMetrics.forEach(m => {
    if (!grouped[m.pipeline_run_id])
      grouped[m.pipeline_run_id] = { run_id: m.pipeline_run_id, run_date: m.run_date, stages: {} };
    grouped[m.pipeline_run_id].stages[m.stage_name] = m;
  });
  const runs    = Object.values(grouped).sort((a, b) => b.run_date.localeCompare(a.run_date));
  const latest  = runs[0];
  const history = runs.slice(0, 15);

  const summarise = run => {
    const rows     = Object.values(run.stages);
    const ok_count = rows.filter(s => s.status === "ok").length;
    const total    = rows.length;
    const duration = rows.reduce((acc, s) => acc + (s.duration_seconds || 0), 0);
    const tokens   = rows.reduce((acc, s) => acc + (s.token_cost || 0), 0);
    return { ok_count, total, duration, tokens };
  };

  const ls = summarise(latest);

  return (
    <div className="full-panel">
      {/* ── Latest Run ── */}
      <div style={{ display:"flex", alignItems:"flex-start", justifyContent:"space-between", marginBottom:14 }}>
        <div>
          <div className="section-label" style={{ marginBottom:4 }}>LATEST PIPELINE RUN</div>
          <div style={{ fontFamily:"var(--mono)", fontSize:11, color:"var(--text-dimmer)" }}>{latest.run_id}</div>
        </div>
        <div style={{ textAlign:"right" }}>
          <div style={{ fontFamily:"var(--mono)", fontSize:11, color:"var(--text-dim)" }}>{latest.run_date}</div>
          <div style={{ fontFamily:"var(--mono)", fontSize:11, marginTop:2,
            color: ls.ok_count === ls.total ? "var(--green)" : "var(--amber)" }}>
            {ls.ok_count}/{ls.total} stages OK
          </div>
        </div>
      </div>

      {/* Summary metrics */}
      <div className="metrics-grid" style={{ marginBottom:16 }}>
        {[
          { label:"TOTAL DURATION", value: fmtDur(ls.duration) },
          { label:"TOTAL TOKENS",   value: ls.tokens > 0 ? fmtK(ls.tokens) : "—" },
          { label:"STAGES PASSED",  value: `${ls.ok_count} / ${ls.total}` },
          { label:"FAILURES",       value: ls.total - ls.ok_count,
            cls: (ls.total - ls.ok_count) > 0 ? "neg" : "" },
        ].map(m => (
          <div className="metric" key={m.label}>
            <div className="metric-label">{m.label}</div>
            <div className={`metric-value ${m.cls || ""}`}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* Stage breakdown */}
      <table className="pm-table">
        <thead>
          <tr>
            <th scope="col">STAGE</th><th scope="col">STATUS</th><th scope="col">DURATION</th><th scope="col">TOKENS</th>
          </tr>
        </thead>
        <tbody>
          {PM_STAGE_ORDER.map(name => {
            const row = latest.stages[name];
            if (!row) return (
              <tr key={name}>
                <td>{PM_STAGE_LABEL[name] || name.toUpperCase()}</td>
                <td><span className="pm-badge pm-skip">SKIP</span></td>
                <td>—</td><td>—</td>
              </tr>
            );
            const ok = row.status === "ok";
            return (
              <tr key={name}>
                <td>{PM_STAGE_LABEL[name] || name.toUpperCase()}</td>
                <td><span className={`pm-badge ${ok ? "pm-ok" : "pm-fail"}`}>{ok ? "OK" : "FAIL"}</span></td>
                <td>{fmtDur(row.duration_seconds)}</td>
                <td>{row.token_cost != null ? fmtK(row.token_cost) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* ── Run History ── */}
      {history.length > 1 && (
        <>
          <div className="section-label" style={{ marginBottom:10 }}>
            RUN HISTORY — LAST {history.length} RUNS
          </div>
          <table className="pm-table pm-history">
            <thead>
              <tr>
                <th scope="col" style={{ textAlign:"left" }}>RUN ID</th>
                <th scope="col">DATE</th><th scope="col">STAGES</th><th scope="col">DURATION</th><th scope="col">TOKENS</th>
              </tr>
            </thead>
            <tbody>
              {history.map(run => {
                const { ok_count, total, duration, tokens } = summarise(run);
                const allOk = ok_count === total;
                return (
                  <tr key={run.run_id}>
                    <td style={{ color:"var(--text-dimmer)", maxWidth:220, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                      {run.run_id.length > 30 ? run.run_id.slice(0, 30) + "…" : run.run_id}
                    </td>
                    <td>{run.run_date}</td>
                    <td style={{ color: allOk ? "var(--green)" : "var(--amber)" }}>
                      {ok_count}/{total}
                    </td>
                    <td>{fmtDur(duration)}</td>
                    <td>{tokens > 0 ? fmtK(tokens) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function WatchdogView({ watchdogReport, watchdogIssues, runLog }) {
  if (!watchdogReport && runLog.length === 0) return <div className="empty-state">// no watchdog data — run run_watchdog.py</div>;
  const status = watchdogReport?.overall_status || "UNKNOWN";
  const statusBg    = status === "CLEAN" ? "var(--green-dim)"  : status === "WARNINGS" ? "var(--amber-glow)" : "var(--red-dim)";
  const statusColor = status === "CLEAN" ? "var(--green)"      : status === "WARNINGS" ? "var(--amber)"      : "var(--red)";
  const statusBorder= status === "CLEAN" ? "rgba(60,174,111,.3)" : status === "WARNINGS" ? "rgba(239,159,39,.3)" : "rgba(226,75,74,.3)";
  return (
    <div className="full-panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <div className="section-label" style={{ marginBottom: 6 }}>PIPELINE WATCHDOG</div>
          {watchdogReport && <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>Last: {watchdogReport.watchdog_date || watchdogReport.created_at?.slice(0, 10)}</div>}
        </div>
        {watchdogReport && (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {watchdogReport.pipeline_health_score != null && <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>Health: {watchdogReport.pipeline_health_score}/10</div>}
            <div style={{ fontFamily: "var(--mono)", fontSize: 12, fontWeight: 700, padding: "4px 10px", borderRadius: 4, background: statusBg, color: statusColor, border: `1px solid ${statusBorder}` }}>{status}</div>
          </div>
        )}
      </div>
      {watchdogReport?.audit_summary && (<div className="watchdog-summary" style={{ marginBottom: 16 }}><div className="watchdog-summary-label">WATCHDOG SUMMARY</div><div className="summary-text">{watchdogReport.audit_summary}</div></div>)}
      {runLog.length > 0 && (<><div className="section-label" style={{ marginBottom: 10 }}>Agent Run Status</div><div className="watchdog-grid">{runLog.map((e, i) => { const ok = e.status === "success" || e.status === "completed"; const fail = e.status === "failed" || e.status === "error"; return (<div className="agent-status-card" key={i}><div className={`status-dot ${ok ? "status-ok" : fail ? "status-fail" : "status-warn"}`} aria-label={ok ? "status ok" : fail ? "status failed" : "status warning"} /><div><div className="agent-name">{e.agent_name || e.agent}</div><div className="agent-run-id">{e.run_id}</div><div className="agent-ts">{e.created_at?.slice(0, 19).replace("T", " ")}</div></div></div>); })}</div></>)}
      <div className="section-label" style={{ marginBottom: 10 }}>Issues ({watchdogIssues.length})</div>
      <div className="watchdog-alerts">{watchdogIssues.length === 0 ? <div className="no-alerts">// pipeline clean — no issues flagged</div> : watchdogIssues.map((a, i) => { const lvl = (a.severity || "INFO").toLowerCase(); return (<div className="alert-item" key={i}><span className={`alert-icon ${lvl === "critical" ? "alert-critical" : lvl === "warning" || lvl === "warn" ? "alert-warning" : "alert-info"}`}>{lvl.slice(0, 4).toUpperCase()}</span><div><div className="alert-text">{a.issue_type}{a.ticker && a.ticker !== "PORTFOLIO" ? ` — ${a.ticker}` : ""}</div><div className="alert-detail">{a.description}</div></div></div>); })}</div>
    </div>
  );
}

// ── Drift View ────────────────────────────────────────────────────────────────

const sigClass  = s => s === "BUY" ? "flip-sig-buy" : s === "SELL" ? "flip-sig-sell" : "flip-sig-hold";
const sevClass  = s => s === "MAJOR" ? "sev-major" : s === "MODERATE" ? "sev-moderate" : "sev-minor";
const flipClass = s => s === "MAJOR" ? "flip-major" : s === "MODERATE" ? "flip-moderate" : "flip-minor";

function DriftView({ supabase, focusTicker }) {
  const [summary, setSummary] = useState([]);
  const [drifts,  setDrifts]  = useState([]);
  const [history, setHistory] = useState([]);
  const [active,  setActive]  = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => { fetchSummary(); }, []);

  async function fetchSummary() {
    setLoading(true);
    try {
      const { data } = await supabase
        .from("signal_drift_summary")
        .select("*")
        .order("flips_7d", { ascending: false });

      const { data: histTickers } = await supabase
        .from("signal_history")
        .select("ticker")
        .order("report_date", { ascending: false });

      const driftTickers = new Set((data || []).map(r => r.ticker));
      const allTickers   = [...new Set((histTickers || []).map(r => r.ticker))];
      const cleanTickers = allTickers
        .filter(t => !driftTickers.has(t))
        .map(t => ({ ticker: t, total_flips: 0, flips_7d: 0, flips_30d: 0, ever_chronic: false }));

      const combined = [...(data || []), ...cleanTickers]
        .sort((a, b) => b.flips_7d - a.flips_7d || b.total_flips - a.total_flips);

      setSummary(combined);
      if (combined.length > 0) {
        const first = focusTicker && combined.some(c => c.ticker === focusTicker) ? focusTicker : combined[0].ticker;
        setActive(first);
        fetchDetail(first);
      }
    } finally {
      setLoading(false);
    }
  }

  async function fetchDetail(ticker) {
    setActive(ticker);
    const { data: driftData } = await supabase
      .from("signal_drift")
      .select("*")
      .eq("ticker", ticker)
      .order("flip_date", { ascending: false })
      .limit(30);

    const { data: histData } = await supabase
      .from("signal_history")
      .select("report_date,signal,signal_strength,close_price,rsi_14,volatility_ann")
      .eq("ticker", ticker)
      .order("report_date", { ascending: false })
      .limit(20);

    setDrifts(driftData || []);
    setHistory(histData || []);
  }

  const activeSummary = summary.find(s => s.ticker === active);

  if (loading) return (
    <div className="loading">
      <span style={{ fontFamily: "var(--mono)", color: "var(--text-dimmer)" }}>// loading signal drift data...</span>
    </div>
  );

  if (summary.length === 0) return (
    <div className="full-panel">
      <div className="no-drift">// no signal history yet — run run_agent.py and run_drift.py first</div>
    </div>
  );

  return (
    <div className="drift-grid">

      {/* ── Left: Ticker list ──────────────────────────────────────────── */}
      <div className="drift-ticker-list" role="tablist" aria-label="Signal stability by ticker">
        <div style={{ padding: "0 16px 10px", fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", letterSpacing: ".1em" }}>
          SIGNAL STABILITY
        </div>
        {summary.map(s => {
          const chipCls   = s.ever_chronic ? "drift-chronic" : s.flips_7d > 0 ? "drift-active" : "drift-clean";
          const chipLabel = s.ever_chronic ? `⚠ ${s.flips_7d}d` : s.flips_7d > 0 ? `${s.flips_7d} flip${s.flips_7d > 1 ? "s" : ""}` : "stable";
          return (
            <button key={s.ticker} role="tab" aria-selected={active === s.ticker}
                    className={`drift-ticker-row${active === s.ticker ? " active" : ""}`}
                    onClick={() => fetchDetail(s.ticker)}>
              <div>
                <div className="drift-ticker-name">{s.ticker}</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--text-dimmer)", marginTop: 2, textAlign: "left" }}>
                  {s.total_flips} total · {s.flips_30d} (30d)
                </div>
              </div>
              <span className={`drift-flip-count ${chipCls}`}>{chipLabel}</span>
            </button>
          );
        })}
      </div>

      {/* ── Right: Detail panel ────────────────────────────────────────── */}
      <div className="drift-detail">
        {activeSummary && (
          <>
            {/* Header */}
            <div className="drift-hero">
              <div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", letterSpacing: ".1em", marginBottom: 6 }}>
                  SIGNAL DRIFT — {active}
                </div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 22, fontWeight: 700, color: "var(--amber)" }}>
                  {activeSummary.total_flips === 0 ? "STABLE" : `${activeSummary.total_flips} FLIP${activeSummary.total_flips > 1 ? "S" : ""}`}
                </div>
                {activeSummary.last_flip_date && (
                  <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginTop: 4 }}>
                    Last flip: {activeSummary.last_flip_date}
                  </div>
                )}
              </div>
              {activeSummary.latest_signal_to && (
                <div style={{ textAlign: "right" }}>
                  <div style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", marginBottom: 4 }}>LAST FLIP</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span className={`flip-sig ${sigClass(activeSummary.latest_signal_from)}`}>{activeSummary.latest_signal_from}</span>
                    <span style={{ color: "var(--text-dimmer)" }} aria-hidden="true">→</span>
                    <span className={`flip-sig ${sigClass(activeSummary.latest_signal_to)}`}>{activeSummary.latest_signal_to}</span>
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <span className={`flip-severity ${sevClass(activeSummary.latest_severity)}`}>{activeSummary.latest_severity}</span>
                  </div>
                </div>
              )}
            </div>

            {/* Chronic warning */}
            {activeSummary.ever_chronic && (
              <div className="chronic-banner">
                <div className="chronic-icon" aria-hidden="true">⚠</div>
                <div className="chronic-text">
                  CHRONIC INSTABILITY — this ticker has flipped 3+ times in a 7-day window.<br/>
                  Review analyst context quality and indicator reliability.
                </div>
              </div>
            )}

            {/* Stats */}
            <div className="drift-stats-row">
              {[
                { label: "FLIPS 7D",  value: activeSummary.flips_7d  || 0, cls: activeSummary.flips_7d  >= 3 ? "neg" : activeSummary.flips_7d  > 0 ? "neutral" : "pos" },
                { label: "FLIPS 30D", value: activeSummary.flips_30d || 0, cls: activeSummary.flips_30d >= 5 ? "neg" : activeSummary.flips_30d > 0 ? "neutral" : "pos" },
                { label: "TOTAL",     value: activeSummary.total_flips || 0, cls: "" },
              ].map(m => (
                <div key={m.label} className="metric">
                  <div className="metric-label">{m.label}</div>
                  <div className={`metric-value ${m.cls}`}>{m.value}</div>
                </div>
              ))}
            </div>

            {/* Flip timeline */}
            {drifts.length > 0 ? (
              <>
                <div className="section-label" style={{ marginBottom: 8 }}>FLIP HISTORY</div>
                <div className="flip-timeline">
                  {drifts.map((d, i) => (
                    <div key={i} className={`flip-event ${flipClass(d.flip_severity)}`}>
                      <div className="flip-date">{d.flip_date}</div>
                      <div className="flip-arrow">
                        <span className={`flip-sig ${sigClass(d.signal_from)}`}>{d.signal_from}</span>
                        <span className="flip-chevron" aria-hidden="true">→</span>
                        <span className={`flip-sig ${sigClass(d.signal_to)}`}>{d.signal_to}</span>
                      </div>
                      <span className={`flip-severity ${sevClass(d.flip_severity)}`}>{d.flip_severity}</span>
                      {d.price_at_flip && <div className="flip-price">${fmt(d.price_at_flip)}</div>}
                      {d.is_chronic && <span style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--red)", marginLeft: 4 }}>CHRONIC</span>}
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="no-drift">// no signal flips detected for {active}</div>
            )}

            {/* Signal history table */}
            {history.length > 0 && (
              <>
                <div className="section-label" style={{ marginTop: 16, marginBottom: 8 }}>SIGNAL HISTORY (LAST 20 RUNS)</div>
                <table className="history-table">
                  <thead>
                    <tr><th scope="col">DATE</th><th scope="col">SIGNAL</th><th scope="col">STRENGTH</th><th scope="col">CLOSE</th><th scope="col">RSI</th><th scope="col">VOL%</th></tr>
                  </thead>
                  <tbody>
                    {history.map((h, i) => (
                      <tr key={i}>
                        <td style={{ color: i === 0 ? "var(--amber)" : "var(--text-dimmer)" }}>{h.report_date}</td>
                        <td><span className={`flip-sig ${sigClass(h.signal)}`} style={{ fontSize: 10 }}>{h.signal}</span></td>
                        <td style={{ color: "var(--text-dimmer)" }}>{h.signal_strength || "—"}</td>
                        <td>{h.close_price ? `$${fmt(h.close_price)}` : "—"}</td>
                        <td style={{ color: h.rsi_14 >= 70 ? "var(--red)" : h.rsi_14 <= 30 ? "var(--green)" : "var(--text-dim)" }}>
                          {h.rsi_14 ? fmt(h.rsi_14, 1) : "—"}
                        </td>
                        <td>{h.volatility_ann ? `${fmt(h.volatility_ann, 1)}%` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── Main Dashboard ─────────────────────────────────────────────────────────────

// Tabs grouped in the order data flows through the pipeline, so the nav itself
// reads as the story: market backdrop → per-ticker inputs → decisions → output.
const VIEW_GROUPS = [
  ...(DEMO ? [{ label: "START HERE", views: ["HOW IT WORKS", "PLAYGROUND"] }] : []),
  { label: "1 · MARKET",    views: ["REGIME", "MACRO"] },
  { label: "2 · INPUTS",    views: ["EARNINGS", "OPTIONS", "SMART MONEY"] },
  { label: "3 · DECISIONS", views: ["ANALYST", "TRADE PLANS", "RISK", "ALLOCATION"] },
  { label: "4 · OUTPUT",    views: ["REPORT"] },
  { label: "CHECKS",        views: ["DRIFT", "WATCHDOG", "PIPELINE", "BACKTEST"] },
  { label: "OPERATIONS",    views: ["AUTOMATION", "DAILY REVIEW"] },
];

// One line per view: where it sits in the pipeline and what it feeds.
const VIEW_INFO = {
  PLAYGROUND: ["DEMO · YOUR KEY", "Shape a made-up chart and run the real analyst and rules on it, with your own Claude key."],
  REGIME:     ["STAGE 0 · MARKET",   "Rule-based market regime (SPY vs 200-day, drawdown, VIX). It sets a risk floor that raises the cash reserve and makes the analyst cautious on new buys."],
  MACRO:      ["STAGE 0 · MARKET",   "Inflation backdrop from CPI/PPI. The analyst reads its equity impact as context for every ticker."],
  EARNINGS:   ["STAGE 0 · INPUT",    "Upcoming reports per ticker. Within 5 days of earnings, the analyst caps signal strength at MODERATE."],
  OPTIONS:    ["STAGE 2 · INPUT",    "Options positioning per ticker. Flow can raise or lower the analyst's conviction but never flips a call on its own."],
  "SMART MONEY": ["STAGE 0 · INPUT",  "What famous investors (Buffett, Ackman, Druckenmiller, Wood, ...) hold and have recently traded, from SEC filings. The analyst weighs it as lagged context, never as a signal on its own."],
  ANALYST:    ["STAGE 3 · DECISION", "The Claude analyst turns every input into BUY / SELL / HOLD plus a strength. Scroll down to the WHY section to trace any call back to its inputs."],
  "TRADE PLANS": ["STAGE 3 · DECISION", "Swing mode only: each BUY's limit entry, stop, target and holding window after the Python rules (stale data, earnings, reward:risk), exit signals, and how simulated trades have played out."],
  RISK:       ["STAGE 4 · DECISION", "Scores each ticker 1–10 and sets its maximum position. A score of 8+ excludes the ticker, and the score shrinks its weight in sizing."],
  ALLOCATION: ["STAGE 5 · WEEKLY",   "Saturday's main-account weights: signal × strength × risk, then the enforcer clamps anything that breaks a cap. Swing orders never read it."],
  REPORT:     ["STAGE 6 · WEEKLY",   "The written summary of Saturday's main-account run. Weekday swing runs don't write one."],
  DRIFT:      ["STAGE 3.5 · CHECK",  "How often each ticker's call has flipped. Frequent flippers get a sizing haircut in allocation."],
  WATCHDOG:   ["CHECK · WEEKLY",     "Health check across Saturday's allocation and report: stale data, failures and anomalies."],
  PIPELINE:   ["CHECK",              "Timing and status of every stage in recent runs."],
  BACKTEST:   ["CHECK",              "How past signals actually played out. It feeds back into the analyst as calibration."],
  AUTOMATION: ["OPERATIONS",         "Every scheduled job today and over the last week: dispatched on time from Supabase, run in GitHub Actions, emails on anything amber or red. Trades are still approved and placed by you."],
  "DAILY REVIEW": ["OPERATIONS",     "The end-of-day review written at 4:35 PM ET: timing, orders, safety, account value, and the executor dry run vs what was placed. Admins only."],
};
const TICKER_VIEWS = new Set(["OPTIONS", "EARNINGS", "SMART MONEY", "RISK", "ALLOCATION", "DRIFT"]);

export default function Dashboard() {
  const [reports,       setReports]       = useState([]);
  const [prices,        setPrices]        = useState({});
  const [activeTicker,  setActive]        = useState(null);
  const [book,          setBook]          = useState("swing");
  const [activeView,    setView]          = useState(() => DEMO ? (returningToPlayground() ? "PLAYGROUND" : "HOW IT WORKS") : "TRADE PLANS");
  const [riskPlan,      setRiskPlan]      = useState(null);
  const [riskDetails,   setRiskDetails]   = useState([]);
  const [allocPlan,     setAllocPlan]     = useState(null);
  const [allocDetails,  setAllocDetails]  = useState([]);
  const [reportDoc,     setReportDoc]     = useState(null);
  const [reportTickers, setReportTickers] = useState([]);
  const [watchdogDoc,   setWatchdogDoc]   = useState(null);
  const [watchdogIssues,setWatchdogIssues]= useState([]);
  const [runLog,        setRunLog]        = useState([]);
  const [optionsFlow,   setOptionsFlow]   = useState([]);
  const [optionsStrikes,setOptionsStrikes]= useState([]);
  const [earningsData,  setEarningsData]  = useState([]);
  const [macroSnapshot, setMacroSnapshot] = useState(null);
  const [macroIndicators,setMacroInds]   = useState([]);
  const [pipelineMetrics,setPipelineMetrics] = useState([]);
  // New v3 state
  const [regimeRow,     setRegimeRow]     = useState(null);
  const [fundamentals,  setFundamentals]  = useState({});
  const [smartHoldings, setSmartHoldings] = useState([]);
  const [signalFilter,  setSignalFilter]  = useState("ALL");
  const [lastUpdated,   setLastUpdated]   = useState(null);
  const [signalHistory, setSignalHistory] = useState({});
  // Set when you follow a link out of a ticker's story; drives highlights + the back button.
  const [focusTicker,   setFocusTicker]   = useState(null);

  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  const [time,    setTime]    = useState(new Date());

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      // ── Core (existing) ──
      // Fetch only the latest run_id so old tickers from previous runs don't appear
      // The weekly long-term book ("longterm-…" run ids) has its own tab; the rest of the dashboard follows the daily run.
      const { data: latestRun } = await supabase.from("agent_reports").select("run_id").not("run_id", "like", "longterm-%").order("report_date", { ascending: false }).limit(1);
      const latestRunId = latestRun?.[0]?.run_id;
      const repQuery = latestRunId
        ? supabase.from("agent_reports").select("*").eq("run_id", latestRunId)
        : supabase.from("agent_reports").select("*").order("report_date", { ascending: false }).limit(50);
      const { data: repData, error: repErr } = await repQuery;
      if (repErr) throw repErr;
      const latest = repData || [];
      setReports(latest);
      if (latest.length > 0) setActive(latest[0].ticker);

      const tickers = latest.map(r => r.ticker);
      if (tickers.length > 0) {
        const since = new Date();
        since.setDate(since.getDate() - 90);
        const sinceStr = since.toISOString().slice(0, 10);
        // PostgREST caps a response at 1000 rows regardless of .limit(), so an
        // ascending fetch silently returned the OLDEST 1000 rows and dropped the
        // recent ones — leaving charts, the price table, day-change, and the
        // decision-trace indicators stuck weeks in the past. Page newest-first so
        // the current sessions are always present, then sort each series ascending.
        const PAGE = 1000;
        let priceData = [];
        for (let from = 0; from < 4000; from += PAGE) {
          const { data: page } = await supabase
            .from("stock_prices").select("ticker,date,open,high,low,close,volume")
            .in("ticker", tickers).gte("date", sinceStr)
            .order("date", { ascending: false })
            .range(from, from + PAGE - 1);
          if (!page || page.length === 0) break;
          priceData = priceData.concat(page);
          if (page.length < PAGE) break;
        }
        const grouped = {};
        priceData.forEach(p => { if (!grouped[p.ticker]) grouped[p.ticker] = []; grouped[p.ticker].push(p); });
        Object.values(grouped).forEach(a => a.sort((x, y) => x.date.localeCompare(y.date)));
        setPrices(grouped);

        // Prior calls per ticker, for the "reverses / continues" line in the story.
        try {
          const hSince = new Date(); hSince.setDate(hSince.getDate() - 45);
          const { data: hist } = await supabase
            .from("signal_history").select("ticker,report_date,signal,signal_strength,run_id")
            .in("ticker", tickers).gte("report_date", hSince.toISOString().slice(0, 10))
            .order("report_date", { ascending: false }).limit(1000);
          const byT = {};
          (hist || []).forEach(h => { (byT[h.ticker] = byT[h.ticker] || []).push(h); });
          setSignalHistory(byT);
        } catch (_) { setSignalHistory({}); }
      }

      const { data: riskData } = await supabase.from("risk_assessments").select("*").order("assessment_date", { ascending: false }).order("created_at", { ascending: false }).limit(1);
      if (riskData?.length > 0) { setRiskPlan(riskData[0]); const { data: rd } = await supabase.from("risk_ticker_details").select("*").eq("assessment_run_id", riskData[0].run_id); setRiskDetails(rd || []); }

      const { data: allocData } = await supabase.from("allocation_plans").select("*").order("plan_date", { ascending: false }).order("created_at", { ascending: false }).limit(1);
      if (allocData?.length > 0) { setAllocPlan(allocData[0]); const { data: ad } = await supabase.from("allocation_details").select("*").eq("plan_run_id", allocData[0].run_id).order("allocated_pct", { ascending: false }); setAllocDetails(ad || []); }

      try {
        const { data: rwData } = await supabase.from("final_reports").select("*").order("report_date", { ascending: false }).order("created_at", { ascending: false }).limit(1);
        if (rwData?.length > 0) {
          setReportDoc(rwData[0]);
          const { data: rtData } = await supabase.from("final_report_tickers").select("*").eq("report_run_id", rwData[0].run_id);
          setReportTickers(rtData || []);
        }
      } catch (_) {}
      try {
        const { data: wdData } = await supabase.from("watchdog_logs").select("*").order("created_at", { ascending: false }).limit(1);
        if (wdData?.length > 0) {
          setWatchdogDoc(wdData[0]);
          const { data: wiData } = await supabase.from("watchdog_issues").select("*").eq("watchdog_run_id", wdData[0].run_id);
          setWatchdogIssues(wiData || []);
        }
      } catch (_) {}
      try { const { data: logData } = await supabase.from("agent_run_log").select("*").order("created_at", { ascending: false }).limit(20); const seenA = new Set(); setRunLog((logData || []).filter(r => { const k = r.agent_name || r.agent; if (seenA.has(k)) return false; seenA.add(k); return true; })); } catch (_) {}

      // ── Options flow ──
      try {
        const { data: latestOptRun } = await supabase.from("options_flow").select("run_id").order("snapshot_date", { ascending: false }).limit(1);
        const latestOptRunId = latestOptRun?.[0]?.run_id;
        const { data: optData } = latestOptRunId
          ? await supabase.from("options_flow").select("*").eq("run_id", latestOptRunId)
          : await supabase.from("options_flow").select("*").order("snapshot_date", { ascending: false }).limit(50);
        setOptionsFlow(optData || []);
        if (latestOptRunId) {
          const { data: strikesData } = await supabase.from("options_strikes").select("*").eq("run_id", latestOptRunId).limit(200);
          setOptionsStrikes(strikesData || []);
        }
      } catch (_) { setOptionsFlow([]); }

      // ── Earnings ──
      try {
        const { data: latestEarnRun } = await supabase.from("earnings_calendar").select("run_id").order("fetch_date", { ascending: false }).limit(1);
        const latestEarnRunId = latestEarnRun?.[0]?.run_id;
        const { data: earnData } = latestEarnRunId
          ? await supabase.from("earnings_calendar").select("*").eq("run_id", latestEarnRunId)
          : await supabase.from("earnings_calendar").select("*").order("fetch_date", { ascending: false }).limit(30);
        setEarningsData(earnData || []);
      } catch (_) { setEarningsData([]); }

      // ── Macro ──
      try {
        const { data: msData } = await supabase.from("macro_snapshots").select("*").order("snapshot_date", { ascending: false }).limit(1);
        setMacroSnapshot(msData?.[0] || null);
        const { data: miData } = await supabase.from("macro_indicators").select("*").order("period_date", { ascending: true }).limit(100);
        setMacroInds(miData || []);
      } catch (_) { setMacroSnapshot(null); setMacroInds([]); }

      // ── NEW v3: Market regime ──
      try {
        const { data: rgData } = await supabase.from("market_regimes").select("*").order("created_at", { ascending: false }).limit(1);
        setRegimeRow(rgData?.[0] || null);
      } catch (_) { setRegimeRow(null); }

      // ── NEW v3: Fundamentals (latest snapshot per ticker) ──
      try {
        const { data: fdData } = await supabase
          .from("stock_fundamentals").select("*")
          .order("snapshot_date", { ascending: false }).limit(120);
        const byTicker = {};
        (fdData || []).forEach(f => { if (!byTicker[f.ticker]) byTicker[f.ticker] = f; });
        setFundamentals(byTicker);
      } catch (_) { setFundamentals({}); }

      // ── Smart money: famous investors holding universe tickers (analyst chip) ──
      try {
        const symbols = [...new Set(latest.flatMap(r => aliasesFor(r.ticker)))];
        const { data: smData } = symbols.length
          ? await supabase.from("investor_holdings").select("manager,ticker,put_call,change_type").in("ticker", symbols)
          : { data: [] };
        setSmartHoldings(smData || []);
      } catch (_) { setSmartHoldings([]); }

      // ── Pipeline metrics ──
      try {
        const { data: pmData } = await supabase
          .from("pipeline_metrics")
          .select("*")
          .order("created_at", { ascending: false })
          .limit(300);
        setPipelineMetrics(pmData || []);
      } catch (_) { setPipelineMetrics([]); }

    } catch (err) {
      setError(err.message);
    } finally {
      setLastUpdated(new Date());
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  // Jump to another tab with a ticker in focus (or back to that ticker's story).
  const goTo = useCallback((view, ticker) => {
    if (view === "ANALYST" && ticker) {
      setActive(ticker);
      if (signalFilter !== "ALL" && !reports.some(r => r.ticker === ticker && r.signal === signalFilter)) setSignalFilter("ALL");
    }
    setFocusTicker(ticker || null);
    setView(view);
    window.scrollTo({ top: 0 });
  }, [reports, signalFilter]);
  const openStory = useCallback(t => goTo("ANALYST", t), [goTo]);

  // Bring the focused row/card into view after a cross-tab jump.
  useEffect(() => {
    if (!focusTicker || activeView === "ANALYST") return;
    const t = setTimeout(() => document.querySelector(".focus-row,.focus-card")?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    return () => clearTimeout(t);
  }, [activeView, focusTicker]);

  // Filtered ticker list for the analyst ticker bar
  const visibleReports = signalFilter === "ALL"
    ? reports
    : reports.filter(r => r.signal === signalFilter);

  // Keyboard shortcuts: ← / → cycle tickers in the ANALYST view
  useEffect(() => {
    const onKey = e => {
      if (book !== "swing" || activeView !== "ANALYST" || visibleReports.length === 0) return;
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const idx  = Math.max(0, visibleReports.findIndex(r => r.ticker === activeTicker));
      const next = e.key === "ArrowRight"
        ? (idx + 1) % visibleReports.length
        : (idx - 1 + visibleReports.length) % visibleReports.length;
      setActive(visibleReports[next].ticker);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [book, activeView, visibleReports, activeTicker]);

  const activeReport    = reports.find(r => r.ticker === activeTicker);
  const activeAnomalies = activeReport ? parseJ(activeReport.anomalies) : [];
  const activePrices    = prices[activeTicker] || [];
  const isUp            = activeReport ? activeReport.signal !== "SELL" : true;

  // Context data for active ticker (analyst view chips)
  const activeEarnings = earningsData.find(e => e.ticker === activeTicker);
  const activeOptions  = optionsFlow.find(o => o.ticker === activeTicker);
  const activeFund     = fundamentals[activeTicker];
  const activeSmart    = (() => {
    if (!activeTicker) return null;
    const syms = aliasesFor(activeTicker);
    const rows = smartHoldings.filter(h => syms.includes(h.ticker) && !h.put_call);
    if (!rows.length) return null;
    // Count funds, not rows: Buffett holding both GOOG and GOOGL is one holder.
    const funds = pred => new Set(rows.filter(pred).map(h => h.manager)).size;
    return {
      holders: funds(h => h.change_type !== "SOLD"),
      bull:    funds(h => h.change_type === "NEW" || h.change_type === "ADDED"),
      bear:    funds(h => h.change_type === "REDUCED" || h.change_type === "SOLD"),
    };
  })();
  const universe = useMemo(() => reports.map(r => r.ticker), [reports]);
  // Decision-trace inputs for the active ticker — each read straight from state.
  const activeIndicators = computeIndicators(activePrices);
  const activeRisk       = riskDetails.find(r => r.ticker === activeTicker);
  const activeAlloc      = allocDetails.find(d => d.ticker === activeTicker);
  const enforcerRan      = allocPlan ? allocPlan.constraint_violations != null : false;
  const enforcerNotes    = (() => {
    if (!activeTicker || !enforcerRan) return [];
    const esc = activeTicker.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
    const re  = new RegExp(`\\b${esc}\\b`);
    return parseJ(allocPlan.constraint_violations).filter(v => re.test(String(v)));
  })();
  // Prior call = most recent signal_history row from an earlier run, plus how
  // many consecutive sessions that call had been held.
  const activePrior = (() => {
    const h = (signalHistory[activeTicker] || []).filter(x => x.run_id !== activeReport?.run_id && x.report_date <= (activeReport?.report_date || "9999"));
    if (!h.length) return null;
    let sessions = 0;
    for (const x of h) { if (x.signal === h[0].signal) sessions++; else break; }
    return { signal: h[0].signal, strength: h[0].signal_strength, date: h[0].report_date, sessions };
  })();
  const macroRegime    = macroSnapshot?.macro_regime;
  const regimeMeta     = regimeRow ? (REGIME_META[regimeRow.regime] || REGIME_META.CHOPPY) : null;
  const longterm       = book === "longterm";

  const valChipCls = v => ({ UNDERVALUED: "chip-val-under", FAIR: "chip-val-fair", OVERVALUED: "chip-val-over" }[v] || "chip-val-na");

  return (
    <>
      <style>{CSS + BACKTEST_CSS + STORY_CSS + RUN_CSS + SMART_CSS + SWING_CSS + LONGTERM_CSS + OPS_CSS + DEMO_CSS + PLAYGROUND_CSS}</style>
      <div className={`dash${longterm ? " book-longterm" : ""}`}>
        {DEMO && (
          <div className="demo-banner" role="note">
            <b>DEMO · SAMPLE DATA</b> Made-up tickers and numbers. No real account, no real orders, not financial advice.
          </div>
        )}
        {/* Header */}
        <header className="header">
          <div style={{ display: "flex", alignItems: "center" }}>
            <div className="logo">Orblex <span>/ Industries</span></div>
            <div className="live-dot" aria-hidden="true" />
            <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)" }}>{time.toLocaleTimeString("en-US", { hour12: false })}</span>
            {regimeRow && (
              <button className="regime-chip"
                      style={{ color: regimeMeta.color, background: regimeMeta.bg, borderColor: regimeMeta.border }}
                      onClick={() => { setBook("swing"); setView("REGIME"); }}
                      aria-label={`Market regime ${regimeRow.regime}${regimeRow.risk_floor ? `, risk floor ${regimeRow.risk_floor}` : ""}. Open regime view.`}>
                ◆ {regimeRow.regime}{regimeRow.risk_floor ? ` · FLOOR ${regimeRow.risk_floor}` : ""}
              </button>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {!longterm && activeReport && activeView === "ANALYST" && (
              <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)" }}>{activeReport.run_id}</span>
            )}
            {!longterm && lastUpdated && (
              <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--text-dimmer)", letterSpacing: ".05em" }}
                    title="When the dashboard last pulled from Supabase. Prices are prior-day close via yfinance.">
                AS OF {lastUpdated.toLocaleTimeString("en-US", { hour12: false })}
              </span>
            )}
            {!DEMO && <RunPipeline onRunFinished={fetchAll} />}
            {!DEMO && (
              <button onClick={() => authClient.auth.signOut()} aria-label="Sign out of the dashboard"
                      style={{ fontFamily: "var(--mono)", fontSize: 10, fontWeight: 700, letterSpacing: ".06em", padding: "5px 12px", borderRadius: 4, border: "1px solid var(--border)", color: "var(--text-dim)" }}>
                SIGN OUT
              </button>
            )}
            {!longterm && (
              <button
                onClick={fetchAll}
                disabled={loading}
                aria-label="Refresh all dashboard data"
                style={{ fontFamily: "var(--mono)", fontSize: 10, fontWeight: 700, letterSpacing: ".06em", padding: "5px 12px", borderRadius: 4, border: "1px solid var(--border-amber)", background: loading ? "transparent" : "var(--amber-glow)", color: loading ? "var(--text-dimmer)" : "var(--amber)", cursor: loading ? "not-allowed" : "pointer", transition: "all .15s" }}
              >
                {loading ? "LOADING..." : "↻ REFRESH"}
              </button>
            )}
          </div>
        </header>

        {/* Book switch */}
        <div className="book-bar">
          <div className="book-toggle" role="group" aria-label="Which book">
            <button className={!longterm ? "on" : ""} aria-pressed={!longterm} onClick={() => setBook("swing")}>SWING · TODAY</button>
            <button className={longterm ? "on" : ""} aria-pressed={longterm} onClick={() => setBook("longterm")}>MAIN ACCOUNT · THIS WEEK</button>
          </div>
          <span className="book-sub">{longterm ? "MAIN ACCOUNT · ADVICE ONLY" : "SWING · 1–10 DAY HOLDS"}</span>
        </div>

        {longterm ? <LongTermView /> : <>
        {/* Nav */}
        <nav className="nav-tabs" role="tablist" aria-label="Dashboard views">
          {VIEW_GROUPS.map(g => (
            <div className="nav-group" key={g.label} role="presentation">
              <span className="nav-group-label" aria-hidden="true">{g.label}</span>
              {g.views.map(v => (
                <button key={v} role="tab" aria-selected={activeView === v}
                        className={`nav-tab ${activeView === v ? "active" : ""}`}
                        onClick={() => { setFocusTicker(null); setView(v); }}>{v}</button>
              ))}
            </div>
          ))}
        </nav>

        {VIEW_INFO[activeView] && (
          <div className="context-bar">
            <span className="context-text">
              {focusTicker && activeView !== "ANALYST"
                ? <>Showing <b style={{ color: "var(--amber)" }}>{focusTicker}</b>{TICKER_VIEWS.has(activeView) ? " (highlighted)" : ""}, one of the inputs behind its call. {VIEW_INFO[activeView][1]}</>
                : VIEW_INFO[activeView][1]}
            </span>
            <span className="context-stage">{VIEW_INFO[activeView][0]}</span>
            {focusTicker && activeView !== "ANALYST" && (
              <button className="crumb" onClick={() => openStory(focusTicker)}>← BACK TO {focusTicker}'S STORY</button>
            )}
          </div>
        )}

        {loading && <div className="loading" role="status">// loading agent data...</div>}
        {error   && <div className="error" role="alert">// error: {error}</div>}

        {!loading && !error && (
          <>
            {/* ANALYST VIEW */}
            {activeView === "ANALYST" && (
              <>
                <div className="ticker-bar" role="tablist" aria-label="Tickers">
                  <div className="sig-filter" role="group" aria-label="Filter tickers by signal">
                    {["ALL", "BUY", "SELL", "HOLD"].map(f => {
                      const count = f === "ALL" ? reports.length : reports.filter(r => r.signal === f).length;
                      return (
                        <button key={f} className={`filter-btn ${signalFilter === f ? "active" : ""}`}
                                aria-pressed={signalFilter === f}
                                onClick={() => {
                                  setSignalFilter(f);
                                  const vis = f === "ALL" ? reports : reports.filter(r => r.signal === f);
                                  if (vis.length > 0 && !vis.some(r => r.ticker === activeTicker)) setActive(vis[0].ticker);
                                }}>
                          {f} {count}
                        </button>
                      );
                    })}
                  </div>
                  {visibleReports.map(r => {
                    const sc = r.signal === "BUY" ? "sig-buy" : r.signal === "SELL" ? "sig-sell" : "sig-hold";
                    return (
                      <button key={r.ticker} role="tab" aria-selected={r.ticker === activeTicker}
                              className={`ticker-tab ${r.ticker === activeTicker ? "active" : ""}`}
                              onClick={() => setActive(r.ticker)}>
                        {r.ticker}<span className={`tab-signal ${sc}`}>{r.signal}</span>
                      </button>
                    );
                  })}
                  {visibleReports.length === 0 && (
                    <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dimmer)", padding: "6px 0" }}>
                      // no {signalFilter} signals today
                    </span>
                  )}
                </div>

                {activeReport && (
                  <div className="main-grid">
                    <div className="panel">
                      <div className="signal-hero">
                        <div className={activeReport.signal.toLowerCase()} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div className="signal-label">{activeReport.signal}</div>
                          <div className="signal-strength">{activeReport.signal_strength}</div>
                        </div>
                        <div style={{ textAlign: "right" }}>
                          <div style={{ fontSize: 14, fontWeight: 500 }}>{activeTicker}</div>
                          <div style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-dim)", marginTop: 2 }}>{activeReport.report_date}</div>
                        </div>
                      </div>

                      {/* Context chips: regime + valuation + earnings + options flow + macro */}
                      <div className="context-chips">
                        {regimeRow && (
                          <span className="chip" style={{ color: regimeMeta.color, background: regimeMeta.bg, borderColor: regimeMeta.border }}>
                            REGIME: {regimeRow.regime}
                          </span>
                        )}
                        {activeReport.valuation_view && (
                          <span className={`chip ${valChipCls(activeReport.valuation_view)}`}>
                            VALUATION: {activeReport.valuation_view}
                          </span>
                        )}
                        {activeEarnings?.days_to_earnings != null && (
                          <span className={`chip chip-earn-${activeEarnings.earnings_risk === "HIGH" ? "high" : activeEarnings.earnings_risk === "MEDIUM" ? "med" : "low"}`}>
                            EARN {activeEarnings.days_to_earnings}d → {activeEarnings.next_earnings_date}
                          </span>
                        )}
                        {activeOptions?.flow_signal && (
                          <span className={`chip chip-flow-${activeOptions.flow_signal.toLowerCase()}`}>
                            FLOW: {activeOptions.flow_signal} (PCR {fmt(activeOptions.put_call_ratio_volume, 2)})
                          </span>
                        )}
                        {macroRegime && (
                          <span className="chip chip-macro">MACRO: {macroRegime}</span>
                        )}
                        {activeSmart && (
                          <button className={`chip ${activeSmart.bull > activeSmart.bear ? "chip-flow-bull" : activeSmart.bear > activeSmart.bull ? "chip-flow-bear" : "chip-flow-neut"}`}
                                  onClick={() => goTo("SMART MONEY", activeTicker)}
                                  title="Famous investors holding this ticker per their latest 13F. Click for details.">
                            SMART $: {activeSmart.holders} FUND{activeSmart.holders === 1 ? "" : "S"}
                            {activeSmart.bull > 0 ? ` ▲${activeSmart.bull}` : ""}{activeSmart.bear > 0 ? ` ▼${activeSmart.bear}` : ""}
                          </button>
                        )}
                      </div>

                      {/* Sentiment gauge */}
                      <SentimentGauge score={activeReport.sentiment_score} />

                      {activePrices.length > 0 && (() => {
                        const last = activePrices[activePrices.length - 1];
                        const prev = activePrices[activePrices.length - 2] || last;
                        const dayChg = ((last.close - prev.close) / prev.close * 100);
                        return (
                          <div className="metrics-grid">
                            {[
                              { label: "CLOSE",   value: `$${fmt(last.close)}`,   cls: "" },
                              { label: "DAY CHG", value: `${dayChg > 0 ? "+" : ""}${fmt(dayChg)}%`, cls: signCls(dayChg) },
                              { label: "VOLUME",  value: fmtVol(last.volume),     cls: "" },
                              { label: "HIGH",    value: `$${fmt(last.high)}`,    cls: "" },
                            ].map(m => (<div className="metric" key={m.label}><div className="metric-label">{m.label}</div><div className={`metric-value ${m.cls}`}>{m.value}</div></div>))}
                          </div>
                        );
                      })()}

                      {activePrices.length >= 2 && <PriceChart prices={activePrices.slice(-14)} isUp={isUp} ticker={activeTicker} />}

                      <div className="summary-block"><div className="summary-label">AGENT SUMMARY</div><div className="summary-text">{activeReport.summary}</div></div>
                      <SignalStory
                        ticker={activeTicker}
                        report={activeReport}
                        indicators={activeIndicators}
                        earnings={activeEarnings}
                        options={activeOptions}
                        regime={regimeRow}
                        macro={macroSnapshot}
                        prior={activePrior}
                        risk={activeRisk}
                        alloc={activeAlloc}
                        allReports={reports}
                        allRisk={riskDetails}
                        riskPlan={riskPlan}
                        enforcerNotes={enforcerNotes}
                        enforcerRan={enforcerRan}
                        methodChipCls={methodChipCls}
                        onNavigate={goTo}
                      />
                    </div>

                    <div className="right-panel">
                      <FundamentalsPanel fund={activeFund} />
                      <AnomalyPanel anomalies={activeAnomalies} />
                      <PriceTable prices={activePrices} />
                    </div>
                  </div>
                )}
              </>
            )}

            {activeView === "HOW IT WORKS" && <DemoIntro onGo={setView} />}
            {activeView === "PLAYGROUND" && <Playground />}
            {activeView === "REGIME"     && <RegimeView   regime={regimeRow} />}
            {activeView === "SMART MONEY" && <SmartMoneyView supabase={supabase} universe={universe} focusTicker={focusTicker} onTicker={openStory} />}
            {activeView === "OPTIONS"    && <OptionsView  optionsFlow={optionsFlow} optionsStrikes={optionsStrikes} focusTicker={focusTicker} onTicker={openStory} />}
            {activeView === "EARNINGS"   && <EarningsView earningsData={earningsData} focusTicker={focusTicker} onTicker={openStory} />}
            {activeView === "MACRO"      && <MacroView    macroSnapshot={macroSnapshot} macroIndicators={macroIndicators} />}
            {activeView === "TRADE PLANS" && <SwingPlansView supabase={supabase} reports={reports} prices={prices} onTicker={openStory} />}
            {activeView === "AUTOMATION" && <AutomationView supabase={supabase} />}
            {activeView === "DAILY REVIEW" && <DailyReviewView />}
            {activeView === "RISK"       && <RiskView     riskPlan={riskPlan} riskDetails={riskDetails} focusTicker={focusTicker} onTicker={openStory} />}
            {activeView === "ALLOCATION" && <AllocView    allocPlan={allocPlan} allocDetails={allocDetails} focusTicker={focusTicker} onTicker={openStory} />}
            {activeView === "REPORT"     && <ReportView   report={reportDoc} reportTickers={reportTickers} />}
            {activeView === "WATCHDOG"   && <WatchdogView watchdogReport={watchdogDoc} watchdogIssues={watchdogIssues} runLog={runLog} />}
            {activeView === "DRIFT"      && <DriftView supabase={supabase} focusTicker={focusTicker} />}
            {activeView === "PIPELINE"   && <PipelineView pipelineMetrics={pipelineMetrics} />}
            {activeView === "BACKTEST"   && <BacktestView supabase={supabase} />}
          </>
        )}
        </>}

        <footer className="footer">
          <div className="footer-left">
            {longterm
              ? "Orblex Industries v3 · Weekly advice for the main account"
              : "Orblex Industries v3 · Autonomous Pipeline · ← → cycle tickers · click any ticker to open its story"}
          </div>
          {!longterm && activeView === "ANALYST" && activeReport && (
            <div className="footer-right">tokens: <span>{activeReport.prompt_tokens} in / {activeReport.output_tokens} out</span></div>
          )}
          {!longterm && activeView === "MACRO" && macroSnapshot && (
            <div className="footer-right">tokens: <span>{macroSnapshot.prompt_tokens} in / {macroSnapshot.output_tokens} out</span></div>
          )}
        </footer>
      </div>
    </>
  );
}