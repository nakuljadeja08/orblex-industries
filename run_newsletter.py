"""
run_newsletter.py — Orblex Industries newsletter. The weekday email is the swing
brief: today's entries and exits, what changed, risk watch, scorecard. Smart
money, macro, the full signals table, earnings, sector exposure and enforcement
stay on the dashboard.

  - Accessibility/readability: real plain-text alternative with full
    content, hidden preheader text, role="presentation" on layout tables,
    raised text contrast (#555452/#888780 -> #8B8A84/#CCCBC5 where the text
    carries meaning), lang attr, alt-less decorative glyphs wrapped for
    screen readers
"""

import os, sys, argparse, uuid, datetime, smtplib, json, re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from supabase import create_client
import anthropic

from llm import ask

from dotenv import load_dotenv
load_dotenv()

from config import load_config
from performance_context import build_performance_summary
from context_builder import SHARE_CLASS_ALIASES
from performance_context import build_swing_feedback
from swing_policy import MAX_HOLD_DAYS as SWING_MAX_HOLD, MIN_HOLD_DAYS as SWING_MIN_HOLD
from exec_limits import RISK_PER_TRADE_PCT
from trade_plan import effective_level, size_entry
from build_longterm import week_of as longterm_week_of

# Caps come from the enforcing module — single source of truth.
try:
    from instrument_policy import SECTOR_CAPS, DEFAULT_SECTOR_CAP, THEME_CAPS
except ImportError:
    SECTOR_CAPS, DEFAULT_SECTOR_CAP, THEME_CAPS = {}, 25.0, {"TECH": 80.0}

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]

SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER       = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD   = os.environ.get("SMTP_PASSWORD", "")
NEWSLETTER_TO   = [e.strip() for e in os.environ.get("NEWSLETTER_TO", "").split(",") if e.strip()]
NEWSLETTER_FROM = os.environ.get("NEWSLETTER_FROM", f"Orblex Industries <{SMTP_USER}>")


# ── Constants ─────────────────────────────────────────────────────────────────

SIGNAL_COLORS = {"BUY": "#3CAE6F", "SELL": "#E24B4A", "HOLD": "#378ADD"}
RISK_COLORS   = {"LOW": "#3CAE6F", "MODERATE": "#EF9F27", "HIGH": "#E24B4A", "CRITICAL": "#E24B4A"}
FLOW_COLORS   = {"BULLISH": "#3CAE6F", "BEARISH": "#E24B4A", "NEUTRAL": "#8B8A84"}
MACRO_COLORS  = {"INFLATIONARY": "#E24B4A", "DISINFLATIONARY": "#EF9F27",
                 "STABLE": "#3CAE6F", "DEFLATIONARY": "#378ADD"}
REGIME_COLORS = {"BULL": "#3CAE6F", "CHOPPY": "#EF9F27",
                 "CORRECTION": "#F4783C", "BEAR": "#E24B4A"}
VALUATION_COLORS = {"UNDERVALUED": "#3CAE6F", "FAIR": "#378ADD", "OVERVALUED": "#E24B4A"}
RISK_BG       = {"LOW": "#3CAE6F22", "MODERATE": "#EF9F2722",
                 "HIGH": "#E24B4A22", "CRITICAL": "#E24B4A33"}

# Raised-contrast text tokens for email body content
T_BODY   = "#CCCBC5"   # body copy (was #E8E6E0/#999 mix)
T_MUTED  = "#8B8A84"   # secondary copy that still carries meaning (was #888780)
T_LABEL  = "#6E6D66"   # decorative uppercase labels only (was #555452)
T_FAINT  = "#6E6D66"   # placeholders / "—" (was #444)


def _parse_jsonish(v, default):
    try:
        out = json.loads(v) if isinstance(v, str) else v
        return out if out is not None else default
    except Exception:
        return default


def _enforcer_state(alloc: dict) -> tuple[bool, list]:
    """
    Read the constraint enforcer's result off an allocation_plans row.

    Returns (ran, violations). The distinction matters: a NULL column means the
    enforcer never ran for this plan, which is NOT the same as running and
    finding nothing. Reporting the first as "fully compliant" is a false
    all-clear — exactly the bug that let regime-ineligible instruments sit in the
    book unnoticed.
    """
    raw = alloc.get("constraint_violations")
    if raw is None:
        return False, []
    return True, _parse_jsonish(raw, [])


def _smart_truncate(text: str, limit: int) -> str:
    """
    Trim to <= limit chars without cutting mid-word.

    Prefers the last sentence boundary inside the limit so the cell ends on a
    complete thought; falls back to the last word boundary. Returns the text
    with a trailing ellipsis only when something was actually removed.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    window = text[:limit]

    # Last sentence terminator followed by a space (avoids splitting "3.52%").
    ends = [m.end() for m in re.finditer(r'[.!?](?=\s|$)', window)]
    if ends and ends[-1] >= limit * 0.5:
        return window[:ends[-1]].strip()

    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(" ,;:-") + "…"


def _esc(s: str) -> str:
    """Minimal HTML escaping for model-generated text dropped into the template."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Claude newsletter summary ─────────────────────────────────────────────────

def claude_newsletter_summary_v2(client, data: dict) -> dict:
    """
    Generates newsletter content: headline, narrative, insights, risk watch,
    what-to-watch, and portfolio stance — regime- and enforcement-aware.
    """
    reports_summary = [
        {
            "ticker": r["ticker"],
            "signal": r["signal"],
            "strength": r.get("signal_strength", ""),
            "sentiment": r.get("sentiment_score"),
            "valuation": r.get("valuation_view"),
            "summary": (r.get("summary") or "")[:180],
        }
        for r in data["analyst_reports"][:12]
    ]
    risk_summary = {
        "rating":   (data["risk"].get("market_risk_level") or
                     data["risk"].get("portfolio_risk_rating") or "N/A") if data["risk"] else "N/A",
        "score":    data["risk"].get("overall_risk_score") if data["risk"] else "N/A",
        "summary":  (data["risk"].get("risk_summary") or "")[:250] if data["risk"] else "",
    }
    alloc = data.get("allocation") or {}
    alloc_summary = {
        "invested_pct":  alloc.get("invested_pct"),
        "cash_pct":      alloc.get("cash_pct"),
        "strategy_note": (alloc.get("strategy_note") or "")[:250],
        "sector_exposure": _parse_jsonish(alloc.get("sector_exposure"), {}),
        "theme_exposure":  _parse_jsonish(alloc.get("theme_exposure"), {}),
        "enforcer_corrections": _parse_jsonish(alloc.get("constraint_violations"), []),
    }
    macro_summary = {
        "regime":       data["macro"].get("macro_regime")            if data["macro"] else "N/A",
        "cpi_yoy":      data["macro"].get("cpi_yoy")                 if data["macro"] else None,
        "ppi_yoy":      data["macro"].get("ppi_yoy")                 if data["macro"] else None,
        "equity_impact":data["macro"].get("equity_impact")           if data["macro"] else "N/A",
        "narrative":    (data["macro"].get("regime_summary") or "")[:300] if data["macro"] else "",
    }
    regime = data.get("regime") or {}
    regime_summary = {
        "regime":           regime.get("regime", "N/A"),
        "risk_floor":       regime.get("risk_floor"),
        "spy_vs_200ma_pct": regime.get("spy_vs_ma200_pct"),
        "spy_drawdown_pct": regime.get("spy_drawdown_pct"),
        "vix":              regime.get("vix_close"),
        "rationale":        (regime.get("rationale") or "")[:300],
    }
    earnings_near = [
        {"ticker": e["ticker"], "days": e.get("days_to_earnings"), "risk": e.get("earnings_risk")}
        for e in data["earnings"]
        if e.get("days_to_earnings") is not None and e["days_to_earnings"] <= 14
    ]

    buys  = [r["ticker"] for r in reports_summary if r["signal"] == "BUY"]
    sells = [r["ticker"] for r in reports_summary if r["signal"] == "SELL"]
    holds = [r["ticker"] for r in reports_summary if r["signal"] == "HOLD"]

    prompt = f"""You are the chief investment officer at Orblex Industries writing a daily AI-generated portfolio intelligence newsletter in the style of a professional macro letter.

Date: {datetime.date.today().strftime("%B %d, %Y")}

Portfolio signals:
  BUY:  {buys}
  SELL: {sells}
  HOLD: {holds}

Market regime (deterministic, drives hard allocation floors): {json.dumps(regime_summary)}
Risk: {json.dumps(risk_summary)}
Allocation (incl. sector/theme exposure and any post-LLM enforcer corrections): {json.dumps(alloc_summary)}
Macro: {json.dumps(macro_summary)}
Upcoming earnings (<14 days): {json.dumps(earnings_near)}
Full signal details (incl. numeric sentiment -1..1 and valuation view): {json.dumps(reports_summary[:8])}

Write these sections for the newsletter:

1. "headline" — 6-10 word punchy headline summarising today's portfolio theme (e.g. "Defensive Posture Holds as Inflation Regime Persists")
2. "subheadline" — 1 sentence expanding the headline
3. "market_pulse" — 3-4 sentence narrative of today's signals and market tone. Be specific: name tickers, reference the market regime and sentiment scores. Write in an authoritative but clear voice.
4. "key_insights" — exactly 3 actionable bullet points as a JSON array of strings. Each should be 1-2 sentences referencing specific tickers or data points. If the enforcer made corrections, one insight should mention it.
5. "risk_watch" — 2-3 sentences on the top risks. Reference specific earnings events, macro headwinds, regime pressure, or sector concentration by name.
6. "what_to_watch" — exactly 3 upcoming catalysts to monitor this week as a JSON array of strings (e.g. "MDB earnings in 7 days — high risk event with SELL signal already active")
7. "portfolio_stance" — 1 sentence summary of overall portfolio posture (e.g. "Defensively positioned with 42% cash given BEAR regime floor and broad high-volatility flags")

CRITICAL OUTPUT RULES:
- Output ONLY a raw JSON object. No markdown, no backticks, no preamble.
- Do NOT truncate any field.
- Complete sentences only.

{{
  "headline": "...",
  "subheadline": "...",
  "market_pulse": "...",
  "key_insights": ["...", "...", "..."],
  "risk_watch": "...",
  "what_to_watch": ["...", "...", "..."],
  "portfolio_stance": "..."
}}"""

    msg = ask("fast", None, prompt, 2000, client)

    raw = msg.text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```\s*$',       '', raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                result = {}
        else:
            result = {}

    result.setdefault("headline",        "Orblex Industries Daily Portfolio Brief")
    result.setdefault("subheadline",     "AI-generated analysis across all pipeline stages.")
    result.setdefault("market_pulse",    "Market pulse data unavailable.")
    result.setdefault("key_insights",    [])
    result.setdefault("risk_watch",      "Risk watch data unavailable.")
    result.setdefault("what_to_watch",   [])
    result.setdefault("portfolio_stance","Portfolio stance data unavailable.")

    if isinstance(result["key_insights"], str):
        result["key_insights"] = [result["key_insights"]]
    if isinstance(result["what_to_watch"], str):
        result["what_to_watch"] = [result["what_to_watch"]]

    result["prompt_tokens"] = msg.input_tokens
    result["output_tokens"] = msg.output_tokens
    return result


# ── Plain-text alternative (real content) ──────────────────────────────────────

def _plaintext_change_lines(change: dict) -> list:
    """WHAT CHANGED block for the text/plain part — mirrors build_change_html."""
    if not change or not change.get("available"):
        return []
    prior_s = str(change.get("prior_date") or "the prior run")
    lines = ["", f"WHAT CHANGED (vs {prior_s})"]
    n_total = (len(change["flips"]) + len(change["entered"])
               + len(change["exited"]) + len(change["deltas"]))
    if n_total == 0 and change.get("flips_only"):
        lines.append("  No signal flips.")
        return lines
    if n_total == 0:
        lines.append(f"  No flips, no positions entered or exited, and no weight "
                     f"moved >= {_DELTA_THRESHOLD:.0f}pp. Book is steady.")
        return lines
    for f in change["flips"]:
        chronic = "  [CHRONIC]" if f["is_chronic"] else ""
        lines.append(f"  FLIP    {f['ticker']:<6} {f['signal_from']} -> {f['signal_to']} "
                     f"({f['severity']}){chronic}")
    for e in change["entered"]:
        lines.append(f"  ENTER   {e['ticker']:<6} new position at {e['pct']:.1f}%")
    for x in change["exited"]:
        lines.append(f"  EXIT    {x['ticker']:<6} exited the book (was {x['prior_pct']:.1f}%)")
    for d in change["deltas"]:
        sign = "+" if d["delta"] > 0 else ""
        lines.append(f"  WEIGHT  {d['ticker']:<6} {d['prior_pct']:.1f}% -> {d['pct']:.1f}% "
                     f"({sign}{d['delta']:.1f}pp)")
    return lines


def _swing_counts(swing: dict):
    """(new entries, exits) from collect_swing_data, or None when there is no swing run."""
    if not swing or not swing.get("available"):
        return None
    return len(swing["plans"]), len(swing["exits"])


def _swing_count_line(swing: dict) -> str:
    counts = _swing_counts(swing)
    if counts is None:
        return "No new plans"
    n_entries, n_exits = counts
    return (f"{n_entries} new entr{'y' if n_entries == 1 else 'ies'}, "
            f"{n_exits} exit{'' if n_exits == 1 else 's'}")


def swing_subject(swing: dict, day: datetime.date) -> str:
    date_s = day.strftime("%b %d, %Y")
    if _swing_counts(swing) is None:
        return f"Swing — no new plans — {date_s}"
    return f"Swing — {_swing_count_line(swing)} — {date_s}"


def _signal_counts(data: dict) -> tuple[int, int, int]:
    reports = data.get("analyst_reports") or []
    return tuple(sum(1 for r in reports if r.get("signal") == s) for s in ("BUY", "SELL", "HOLD"))


def build_plaintext(data: dict, summary: dict, change: dict = None,
                    scorecard: dict = None, swing: dict = None) -> str:
    """Text/plain twin of build_html_v2: same sections, same order, same words."""
    today_str = datetime.date.today().strftime("%B %d, %Y")
    regime = data.get("regime") or {}
    n_buy, n_sell, n_hold = _signal_counts(data)
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()

    lines = [
        f"ORBLEX INDUSTRIES — SWING BRIEF — {today_str}",
        f"{SWING_MIN_HOLD}–{SWING_MAX_HOLD} DAY HOLDS · {n_buy} BUY · {n_sell} SELL · {n_hold} HOLD",
        "=" * 60,
        "",
        _swing_count_line(swing),
        summary.get("headline", ""),
        "",
        f"MARKET REGIME: {regime.get('regime', 'N/A')}"
        + (f" (risk floor: {regime['risk_floor']})" if regime.get("risk_floor") else ""),
    ]
    lines += _plaintext_swing_lines(swing) or ["", "SWING TRADE PLANS", "  No swing-mode reports in the latest run."]
    lines += _plaintext_change_lines(change)
    lines += ["", "RISK WATCH", summary.get("risk_watch", "")]
    lines += _plaintext_scorecard_lines(scorecard)
    lines += ["", "-" * 60]
    if dashboard_url:
        lines.append(f"Full dashboard: {dashboard_url}")
    lines += [
        "Generated by an autonomous AI pipeline for research and simulation",
        "purposes only. Not financial advice. All signals are technical and",
        "model-driven; past performance does not guarantee future results.",
    ]
    return "\n".join(lines)


def _section(title: str, body: str, note: str = "") -> str:
    """One stacked section row of the swing brief."""
    note_td = (f'<td align="right" style="font-family:monospace;font-size:12px;color:{T_LABEL};">{note}</td>'
               if note else "")
    return f"""
  <tr>
    <td class="wi-pad" style="background:#141517;padding:22px 28px;border-bottom:1px solid #1E2024;">
      <table role="presentation" width="100%"><tr>
        <td style="font-family:monospace;font-size:13px;color:#EF9F27;letter-spacing:.1em;">{title}</td>
        {note_td}
      </tr></table>
      <div style="margin-top:10px;">{body}</div>
    </td>
  </tr>"""


def build_html_v2(data: dict, summary: dict, change: dict = None,
                  scorecard: dict = None, swing: dict = None) -> str:
    """The weekday swing brief: today's entries and exits first, single column, ≤600px."""
    today_str = datetime.date.today().strftime("%B %d, %Y")
    weekday   = datetime.date.today().strftime("%A")
    change    = change or {}

    change_body    = build_change_html(change)
    scorecard_body = build_scorecard_html(scorecard or {})
    swing_body     = build_swing_html(swing or {}) or (
        f'<div style="font-family:monospace;font-size:13px;color:{T_MUTED};padding:7px 0;">'
        f'No swing-mode reports in the latest run.</div>')

    # Optional — only rendered when set, so we never ship a dead link.
    _dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()

    regime       = data.get("regime") or {}
    regime_name  = regime.get("regime", "N/A")
    regime_color = REGIME_COLORS.get(regime_name, T_MUTED)
    regime_floor = regime.get("risk_floor")
    regime_chips_html = "".join(
        f'<span style="font-family:monospace;font-size:12px;color:{T_MUTED};'
        f'background:#1A1C1F;border:1px solid #2A2D31;padding:2px 7px;'
        f'border-radius:3px;margin:0 4px 4px 0;display:inline-block;">{_esc(rule)}</span>'
        for rule in _parse_jsonish(regime.get("rules_fired"), [])[:6])

    n_buy, n_sell, n_hold = _signal_counts(data)
    count_line = _swing_count_line(swing)

    sections = _section(
        "SWING TRADE PLANS", swing_body,
        f"{SWING_MIN_HOLD}&ndash;{SWING_MAX_HOLD} day holds &middot; long only &middot; no day trades")
    if change_body:
        sections += _section("WHAT CHANGED", change_body,
                             f'vs {_esc(str(change.get("prior_date") or "prior run"))}')
    sections += f"""
  <tr>
    <td class="wi-pad" style="background:#141517;padding:22px 28px;border-bottom:1px solid #1E2024;">
      <div style="font-family:monospace;font-size:13px;color:#E24B4A;letter-spacing:.1em;margin-bottom:12px;">
        <span aria-hidden="true">&#9888;</span> RISK WATCH
      </div>
      <div style="font-size:16px;line-height:1.75;color:#E8E6E0;background:#E24B4A0D;
                  border-left:3px solid #E24B4A;padding:14px 16px;border-radius:0 4px 4px 0;">
        {_esc(summary.get("risk_watch","No risk watch generated."))}
      </div>
    </td>
  </tr>"""
    if scorecard_body:
        sections += _section("SCORECARD", scorecard_body, "latest backtest &middot; realised outcomes")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Orblex Industries — Swing Brief</title>
<style>
  /* Clients that honour <style> (Gmail web/app, Apple Mail) get a tighter,
     stack-friendly layout on narrow screens. Everything degrades to the inline
     styles below where <style> is stripped, so this is progressive enhancement. */
  @media only screen and (max-width:600px) {{
    .wi-shell   {{ width:100% !important; }}
    .wi-pad     {{ padding-left:16px !important; padding-right:16px !important; }}
    .wi-hero    {{ font-size:22px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#0D0E0F;font-family:'DM Sans',Helvetica,Arial,sans-serif;color:#E8E6E0;">

<!-- Preheader: shown in inbox preview, hidden in body -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
  {_esc(count_line)}
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0D0E0F;padding:30px 20px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" class="wi-shell" style="max-width:600px;width:100%;">

  <!-- ══ HEADER ══ -->
  <tr>
    <td class="wi-pad" style="background:#141517;border-bottom:2px solid #EF9F27;padding:24px 28px;border-radius:6px 6px 0 0;">
      <table role="presentation" width="100%"><tr valign="top">
        <td>
          <div style="font-family:monospace;font-size:13px;color:{T_LABEL};letter-spacing:.1em;margin-bottom:6px;">
            {weekday.upper()}, {today_str.upper()}
          </div>
          <div style="font-family:monospace;font-size:20px;font-weight:700;color:#EF9F27;letter-spacing:.06em;">
            ORBLEX INDUSTRIES
          </div>
          <div style="font-family:monospace;font-size:13px;color:{T_LABEL};margin-top:2px;letter-spacing:.08em;">
            SWING BRIEF
          </div>
        </td>
        <td align="right" valign="top">
          <div style="background:#EF9F2718;border:1px solid rgba(239,159,39,.25);border-radius:4px;
                      padding:5px 12px;display:inline-block;margin-bottom:8px;">
            <span style="font-family:monospace;font-size:13px;color:#EF9F27;font-weight:700;">{SWING_MIN_HOLD}&ndash;{SWING_MAX_HOLD} DAY HOLDS</span>
          </div>
          <div>
            <span style="background:#3CAE6F22;color:#3CAE6F;font-family:monospace;font-size:13px;
                         padding:3px 8px;border-radius:3px;margin-left:6px;">{n_buy} BUY</span>
            <span style="background:#E24B4A22;color:#E24B4A;font-family:monospace;font-size:13px;
                         padding:3px 8px;border-radius:3px;margin-left:6px;">{n_sell} SELL</span>
            <span style="background:#378ADD22;color:#378ADD;font-family:monospace;font-size:13px;
                         padding:3px 8px;border-radius:3px;margin-left:6px;">{n_hold} HOLD</span>
          </div>
        </td>
      </tr></table>
    </td>
  </tr>

  <!-- ══ TODAY'S COUNTS ══ -->
  <tr>
    <td class="wi-pad" style="background:#141517;padding:24px 28px 18px;border-bottom:1px solid #1E2024;">
      <div class="wi-hero" style="font-size:26px;font-weight:700;color:#E8E6E0;line-height:1.3;margin-bottom:8px;
                  letter-spacing:-.01em;">
        {_esc(count_line)}
      </div>
      <div style="font-size:15px;color:{T_MUTED};line-height:1.6;">
        {_esc(summary.get("headline",""))}
      </div>
    </td>
  </tr>

  <!-- ══ MARKET REGIME STRIP ══ -->
  <tr>
    <td class="wi-pad" style="background:{regime_color}14;border-bottom:1px solid #1E2024;padding:14px 28px;">
      <table role="presentation" width="100%"><tr valign="top">
        <td>
          <div style="font-family:monospace;font-size:13px;color:{T_MUTED};letter-spacing:.1em;margin-bottom:4px;">
            MARKET REGIME
          </div>
          <div style="font-family:monospace;font-size:18px;font-weight:700;color:{regime_color};letter-spacing:.05em;">
            {regime_name}{f' &mdash; FLOOR {regime_floor}' if regime_floor else ''}
          </div>
        </td>
        <td align="right" style="font-family:monospace;font-size:13px;color:{T_MUTED};">
          {f"SPY {regime.get('spy_vs_ma200_pct', 0):+.1f}% vs 200MA" if regime.get('spy_vs_ma200_pct') is not None else ''}<br/>
          {f"Drawdown {regime.get('spy_drawdown_pct', 0):.1f}%" if regime.get('spy_drawdown_pct') is not None else ''}
          {f" &middot; VIX {regime.get('vix_close'):.1f}" if regime.get('vix_close') is not None else ''}
        </td>
      </tr></table>
      {f'<div style="margin-top:8px;">{regime_chips_html}</div>' if regime_chips_html else ''}
    </td>
  </tr>
{sections}

  <!-- ══ FOOTER ══ -->
  <tr>
    <td class="wi-pad" style="background:#0F1011;border-top:1px solid #1E2024;padding:20px 28px;border-radius:0 0 6px 6px;">
      <div style="font-family:monospace;font-size:13px;color:{T_LABEL};margin-bottom:8px;letter-spacing:.05em;">
        ORBLEX INDUSTRIES v3 — SWING BRIEF — {today_str.upper()}
      </div>
      {f'<div style="font-size:14px;margin-bottom:8px;">Per-name analyst detail, risk and allocation live on the <a href="{_dashboard_url}" style="color:#EF9F27;">full dashboard</a>.</div>' if _dashboard_url else ''}
      <div style="font-size:13px;color:{T_LABEL};line-height:1.6;">
        This newsletter is generated by an autonomous AI pipeline for research and simulation purposes only.
        It does not constitute financial advice. All signals are technical and model-driven.
        Past performance of signals does not guarantee future results.
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Weekly advice letter (BOOK=longterm) ───────────────────────────────────────
# Rules-only: reads longterm_advice rows written by build_longterm.py, no model call.

LT_TEAL       = "#2ECFA8"
LT_ORDER      = {"NEW": 0, "ADD": 1, "TRIM": 2, "EXIT": 3, "HOLD": 4, "AVOID": 5}
LT_ACTIONABLE = {"NEW", "ADD", "TRIM", "EXIT"}
LT_COLORS     = {"NEW": "#3CAE6F", "ADD": "#3CAE6F", "TRIM": "#F4783C", "EXIT": "#E24B4A",
                 "HOLD": "#378ADD", "AVOID": T_MUTED}
LT_FOOTER     = "This is advice for the main account. It is not an order."


def collect_longterm_rows(sb, run_id: str = None) -> list:
    """longterm_advice rows for run_id (latest run when None), in the dashboard's order."""
    if not run_id:
        latest = (sb.table("longterm_advice").select("run_id").order("week_of", desc=True)
                    .order("created_at", desc=True).limit(1).execute().data)
        if not latest:
            return []
        run_id = latest[0]["run_id"]
    rows = sb.table("longterm_advice").select("*").eq("run_id", run_id).execute().data or []
    for r in rows:
        r["why"] = _parse_jsonish(r.get("why"), []) or []
        r["changes"] = _parse_jsonish(r.get("changes"), []) or []
    return sorted(rows, key=lambda r: (LT_ORDER.get(r["action"], 9), -float(r.get("target_pct") or 0)))


def _lt_week(rows: list) -> datetime.date:
    if rows and rows[0].get("week_of"):
        return datetime.date.fromisoformat(str(rows[0]["week_of"])[:10])
    return longterm_week_of(_et_date())


def _lt_day(d: datetime.date) -> str:
    return f"{d:%b} {d.day}"


def _lt_pct(v) -> str:
    return "—" if v is None else f"{float(v):.1f}%"


def _lt_actions(rows: list) -> list:
    return [r for r in rows if r["action"] in LT_ACTIONABLE]


def _lt_title(rows: list) -> str:
    if not rows:
        return "No advice this week"
    n = len(_lt_actions(rows))
    return "No changes this week" if n == 0 else f"{n} change{'' if n == 1 else 's'} suggested"


def _lt_meta(rows: list) -> str:
    week = f"Week of {_lt_day(_lt_week(rows))}"
    if not rows:
        return week
    head = rows[0]
    return f"{week} · regime {head.get('regime') or '—'} · {_lt_pct(head.get('cash_pct'))} cash"


def _lt_dots(n) -> str:
    return "●" * n + "○" * (3 - n) if n else "—"


def _lt_target(r: dict) -> str:
    if r["action"] == "EXIT":
        return "to 0%"
    prev = f" (last week {_lt_pct(r['prev_target_pct'])})" if r.get("prev_target_pct") is not None else ""
    return f"target {_lt_pct(r['target_pct'])}{prev}"


def _lt_changed(rows: list) -> list:
    """(ticker, changes) for rows that moved since last week, minus the new-this-week placeholder."""
    out = []
    for r in rows:
        ch = [c for c in r.get("changes") or [] if c != "new this week"]
        if ch:
            out.append((r["ticker"], ch))
    return out


def _lt_first_week(rows: list) -> bool:
    return all(r.get("prev_target_pct") is None for r in rows)


def _lt_changes_text(rows: list) -> str:
    if _lt_first_week(rows):
        return ("This is the first week of advice. From next week, new names, action flips, "
                "target moves of 1%+ and conviction changes show up here.")
    return "" if _lt_changed(rows) else "Nothing moved since last week."


def longterm_subject(rows: list) -> str:
    n = len(_lt_actions(rows))
    what = "no advice this week" if not rows else "no changes" if n == 0 else f"{n} change{'' if n == 1 else 's'}"
    return f"Main account — {what} — week of {_lt_day(_lt_week(rows))}"


def build_longterm_plaintext(rows: list) -> str:
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    lines = [
        "ORBLEX INDUSTRIES — WEEKLY REVIEW",
        "MAIN ACCOUNT · ADVICE ONLY",
        _lt_meta(rows),
        "=" * 60,
        "",
        _lt_title(rows),
    ]
    if not rows:
        lines += ["", "The Saturday long-term run wrote no advice for this week."]
    for r in _lt_actions(rows):
        lines += ["", f"{r['action']:<5} {r['ticker']:<6} {_lt_target(r)}   conviction {_lt_dots(r.get('conviction'))}"]
        lines += [f"      {w.get('source', '')}: {w.get('text', '')}" for w in r.get("why") or []]
        if r.get("wrong_if"):
            lines.append(f"      Wrong if: {r['wrong_if']}")
    holds = [r["ticker"] for r in rows if r["action"] == "HOLD"]
    if holds:
        lines += ["", f"Everything else holds: {', '.join(holds)}"]
    if rows:
        lines += ["", "WHAT CHANGED SINCE LAST WEEK"]
        note = _lt_changes_text(rows)
        lines += [f"  {note}"] if note else [f"  {t:<6} {' · '.join(ch)}" for t, ch in _lt_changed(rows)]
    lines += ["", "-" * 60, LT_FOOTER]
    if dashboard_url:
        lines.append(f"Full dashboard: {dashboard_url}")
    return "\n".join(lines)


def build_longterm_html(rows: list) -> str:
    title = _lt_title(rows)
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()

    action_rows = ""
    for r in _lt_actions(rows):
        color = LT_COLORS.get(r["action"], T_MUTED)
        why = "".join(
            f'<div style="font-size:14px;color:{T_BODY};line-height:1.55;margin-top:4px;">'
            f'<span style="font-family:monospace;font-size:11px;color:{LT_TEAL};letter-spacing:.05em;'
            f'text-transform:uppercase;margin-right:6px;">{_esc(w.get("source", ""))}</span>'
            f'{_esc(w.get("text", ""))}</div>'
            for w in r.get("why") or [])
        wrong = (f'<div style="font-size:13px;color:{T_MUTED};margin-top:6px;">'
                 f'<b style="color:{T_BODY};font-weight:500;">Wrong if:</b> {_esc(r["wrong_if"])}</div>'
                 if r.get("wrong_if") else "")
        action_rows += f"""
      <tr><td style="padding:14px 0;border-top:1px solid #1E2024;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr valign="top">
          <td style="width:60px;">
            <span style="background:{color}22;border:1px solid {color}55;color:{color};font-family:monospace;
                         font-size:12px;font-weight:700;padding:2px 7px;border-radius:3px;">{r["action"]}</span>
          </td>
          <td style="font-family:monospace;font-size:15px;font-weight:700;color:#E8E6E0;">{_esc(r["ticker"])}</td>
          <td align="right" style="font-family:monospace;font-size:13px;color:{T_BODY};">
            {_esc(_lt_target(r))}
            <span style="color:{LT_TEAL};letter-spacing:2px;margin-left:8px;" aria-label="conviction {r.get('conviction') or 0} of 3">{_lt_dots(r.get("conviction"))}</span>
          </td>
        </tr></table>
        {why}{wrong}
      </td></tr>"""

    holds = [r["ticker"] for r in rows if r["action"] == "HOLD"]
    holds_html = (f'<div style="font-size:14px;color:{T_BODY};padding-top:14px;border-top:1px solid #1E2024;">'
                  f'Everything else holds: {_esc(", ".join(holds))}</div>' if holds else "")

    if not rows:
        body = (f'<div style="font-size:15px;color:{T_BODY};line-height:1.6;">'
                f'The Saturday long-term run wrote no advice for this week.</div>')
    else:
        body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{action_rows}</table>'
                f'{holds_html}')

    note = _lt_changes_text(rows)
    changes_html = "" if not rows else (
        f'<div style="font-size:14px;color:{T_MUTED};line-height:1.6;">{_esc(note)}</div>' if note else "".join(
            f'<div style="font-size:14px;color:{T_BODY};line-height:1.9;">'
            f'<b style="font-family:monospace;color:#E8E6E0;margin-right:8px;">{_esc(t)}</b>{_esc(" · ".join(ch))}</div>'
            for t, ch in _lt_changed(rows)))
    changes_section = f"""
  <tr>
    <td class="wi-pad" style="background:#141517;padding:22px 28px;border-bottom:1px solid #1E2024;">
      <div style="font-family:monospace;font-size:13px;color:{LT_TEAL};letter-spacing:.1em;margin-bottom:12px;">
        WHAT CHANGED SINCE LAST WEEK
      </div>
      {changes_html}
    </td>
  </tr>""" if rows else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Orblex Industries — Weekly Review</title>
<style>
  @media only screen and (max-width:600px) {{
    .wi-shell   {{ width:100% !important; }}
    .wi-pad     {{ padding-left:16px !important; padding-right:16px !important; }}
    .wi-hero    {{ font-size:22px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#0D0E0F;font-family:'DM Sans',Helvetica,Arial,sans-serif;color:#E8E6E0;">

<!-- Preheader: shown in inbox preview, hidden in body -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
  {_esc(title)}
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0D0E0F;padding:30px 20px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" class="wi-shell" style="max-width:600px;width:100%;">

  <!-- ══ HEADER ══ -->
  <tr>
    <td class="wi-pad" style="background:#141517;border-bottom:2px solid {LT_TEAL};padding:24px 28px;border-radius:6px 6px 0 0;">
      <div style="font-family:monospace;font-size:20px;font-weight:700;color:{LT_TEAL};letter-spacing:.06em;">
        WEEKLY REVIEW
      </div>
      <div style="font-family:monospace;font-size:13px;color:{LT_TEAL};margin-top:4px;letter-spacing:.1em;">
        MAIN ACCOUNT &middot; ADVICE ONLY
      </div>
      <div style="font-family:monospace;font-size:13px;color:{T_MUTED};margin-top:8px;">
        {_esc(_lt_meta(rows))}
      </div>
    </td>
  </tr>

  <!-- ══ ADVICE ══ -->
  <tr>
    <td class="wi-pad" style="background:#141517;padding:24px 28px;border-bottom:1px solid #1E2024;">
      <div class="wi-hero" style="font-size:26px;font-weight:700;color:#E8E6E0;line-height:1.3;margin-bottom:12px;">
        {_esc(title)}
      </div>
      {body}
    </td>
  </tr>
{changes_section}

  <!-- ══ FOOTER ══ -->
  <tr>
    <td class="wi-pad" style="background:#0F1011;border-top:1px solid #1E2024;padding:20px 28px;border-radius:0 0 6px 6px;">
      <div style="font-size:14px;color:{T_BODY};line-height:1.6;">
        {LT_FOOTER}
        {f' See every holding on the <a href="{dashboard_url}" style="color:{LT_TEAL};">dashboard</a>.' if dashboard_url else ''}
      </div>
      <div style="font-family:monospace;font-size:12px;color:{T_LABEL};margin-top:8px;letter-spacing:.05em;">
        ORBLEX INDUSTRIES — WEEKLY REVIEW
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Data collector ─────────────────────────────────────────────────────────────

def _book(q, col: str):
    """Limit a query to this run's book: long-term run ids start with "longterm-"."""
    return q.like(col, "longterm-%") if os.environ.get("BOOK") == "longterm" else q.not_.like(col, "longterm-%")


def collect_data(sb, tickers: list) -> dict:
    """Pull latest data from all agent tables, scoped to the tickers the latest run analysed."""
    data = {}

    # Analyst reports — every ticker of the latest run of this book. The universe stage
    # adds names beyond universe.txt (candidates.txt sleeve), so the run, not the file,
    # is the ticker list; `tickers` is only the fallback when there is no run. The daily
    # newsletter never reads the weekly long-term run (and vice versa), and created_at
    # breaks ties between two runs stamped with the same date.
    run_resp = _book(sb.table("agent_reports").select("run_id"), "run_id")         .order("report_date", desc=True).order("created_at", desc=True).limit(1).execute()
    latest_analyst_run = run_resp.data[0]["run_id"] if run_resp.data else None
    if latest_analyst_run:
        r = sb.table("agent_reports").select("*").eq("run_id", latest_analyst_run).execute()
        data["analyst_reports"] = r.data or []
    else:
        data["analyst_reports"] = []
    tickers = sorted({r["ticker"] for r in data["analyst_reports"]}) or list(tickers)
    data["tickers"] = tickers

    # Risk assessment — latest run
    r = _book(sb.table("risk_assessments").select("*"), "analyst_run_id")         .order("assessment_date", desc=True).order("created_at", desc=True).limit(1).execute()
    data["risk"] = r.data[0] if r.data else None

    if data["risk"]:
        r2 = sb.table("risk_ticker_details").select("*").eq("assessment_run_id", data["risk"]["run_id"]).in_("ticker", tickers).execute()
        data["risk_details"] = r2.data or []
    else:
        data["risk_details"] = []

    # Allocation — latest run, filtered to the run's tickers
    r = _book(sb.table("allocation_plans").select("*"), "analyst_run_id")         .order("plan_date", desc=True).order("created_at", desc=True).limit(1).execute()
    data["allocation"] = r.data[0] if r.data else None
    # Daily swing runs build no plan (run_all.py), so an older one says nothing about today.
    if data["allocation"] and data["allocation"].get("analyst_run_id") != latest_analyst_run:
        data["allocation"] = None

    if data["allocation"]:
        r2 = sb.table("allocation_details").select("*").eq("plan_run_id", data["allocation"]["run_id"]).in_("ticker", tickers).order("allocated_pct", desc=True).execute()
        data["alloc_details"] = r2.data or []
    else:
        data["alloc_details"] = []

    # Options flow — latest run, filtered to the run's tickers
    try:
        run_resp = sb.table("options_flow").select("run_id").order("snapshot_date", desc=True).limit(1).execute()
        latest_opt_run = run_resp.data[0]["run_id"] if run_resp.data else None
        if latest_opt_run:
            r = sb.table("options_flow").select("*").eq("run_id", latest_opt_run).in_("ticker", tickers).execute()
            data["options"] = r.data or []
        else:
            data["options"] = []
    except Exception:
        data["options"] = []

    # Earnings calendar — latest run, filtered to the run's tickers
    try:
        run_resp = sb.table("earnings_calendar").select("run_id").order("fetch_date", desc=True).limit(1).execute()
        latest_earn_run = run_resp.data[0]["run_id"] if run_resp.data else None
        if latest_earn_run:
            r = sb.table("earnings_calendar").select("*").eq("run_id", latest_earn_run).in_("ticker", tickers).execute()
            data["earnings"] = r.data or []
        else:
            data["earnings"] = []
    except Exception:
        data["earnings"] = []

    # Macro snapshot — no ticker filter needed
    try:
        r = sb.table("macro_snapshots").select("*").order("snapshot_date", desc=True).limit(1).execute()
        data["macro"] = r.data[0] if r.data else None
    except Exception:
        data["macro"] = None

    # ── NEW v3: Market regime — latest row, no ticker filter ──────────────────
    try:
        r = sb.table("market_regimes").select("*").order("created_at", desc=True).limit(1).execute()
        data["regime"] = r.data[0] if r.data else None
    except Exception:
        data["regime"] = None

    return data


# ── Scorecard (performance feedback, IMPROVEMENTS 3.3) ─────────────────────────

def collect_scorecard_data(sb, tickers: list) -> dict:
    """
    Pull the latest backtest feedback into the three numbers a reader wants first:
    signal hit rate, alpha vs SPY, and current paper-book drawdown. This is the
    SAME summary the analyst/risk prompts consume (performance_context), so the
    band the reader sees is exactly the feedback the system fed itself.

    Returns {"available": False} when no backtest has matured yet — a fresh
    system, or one whose windows have not closed — so the band is omitted rather
    than showing zeros that read as "flat" instead of "no data".
    """
    try:
        summary = build_performance_summary(sb, tickers)
    except Exception:
        summary = None
    if not summary:
        return {"available": False}
    return {"available": True, "summary": summary}


def _scorecard_metrics(summary: dict) -> list:
    """
    Normalise the summary into ordered display metrics, longest horizon first for
    hit rate / alpha (20d is the more meaningful window). Each metric is
    {label, value, sub, tone} where tone in {"good","bad","neutral","muted"}.
    A metric with no matured data renders as N/A (muted), never as zero.
    """
    metrics = []

    # Signal hit rate — prefer 20d, fall back to 5d.
    overall = summary.get("hit_rate_overall") or {}
    hr = overall.get(20) or overall.get(5)
    hr_period = 20 if overall.get(20) else (5 if overall.get(5) else None)
    if hr and hr_period:
        pct = hr["rate"] * 100
        tone = "good" if pct >= 55 else ("bad" if pct < 45 else "neutral")
        metrics.append({
            "label": f"SIGNAL HIT RATE ({hr_period}d)",
            "value": f"{pct:.0f}%",
            "sub":   f"{hr['n']} scored calls",
            "tone":  tone,
        })
    else:
        metrics.append({"label": "SIGNAL HIT RATE", "value": "N/A",
                        "sub": "no scored outcomes yet", "tone": "muted"})

    # Alpha vs SPY — prefer 20d, fall back to 5d; only matured windows count.
    alpha = summary.get("portfolio_alpha") or {}
    a20, a5 = alpha.get(20) or {}, alpha.get(5) or {}
    chosen, a_period = (a20, 20) if a20.get("avg_alpha_spy") is not None else (
                       (a5, 5) if a5.get("avg_alpha_spy") is not None else ({}, None))
    avg = chosen.get("avg_alpha_spy")
    if avg is not None and a_period:
        tone = "good" if avg > 0 else ("bad" if avg < 0 else "neutral")
        metrics.append({
            "label": f"ALPHA vs SPY ({a_period}d)",
            "value": f"{avg:+.2f}%",
            "sub":   f"{chosen.get('matured', 0)} matured plan(s)",
            "tone":  tone,
        })
    else:
        # Surface why: windows exist but none have matured.
        pending = (a20.get("plans") or a5.get("plans") or 0)
        metrics.append({"label": "ALPHA vs SPY", "value": "N/A",
                        "sub": (f"no window matured ({pending} pending)" if pending
                                else "no benchmark yet"), "tone": "muted"})

    # Current drawdown of the paper book.
    dd = summary.get("drawdown")
    if dd:
        cur = dd["current_drawdown_pct"]
        # 0 (or effectively flat) = at the highs, which is good.
        tone = "good" if cur >= -0.05 else ("bad" if cur <= -10 else "neutral")
        metrics.append({
            "label": "PAPER DRAWDOWN",
            "value": f"{cur:.1f}%",
            "sub":   f"worst {dd['max_drawdown_pct']:.1f}% / {dd['days']}d",
            "tone":  tone,
        })
    else:
        metrics.append({"label": "PAPER DRAWDOWN", "value": "N/A",
                        "sub": "insufficient history", "tone": "muted"})

    return metrics


_SCORE_TONE = {
    "good":    "#3CAE6F",
    "bad":     "#E24B4A",
    "neutral": "#EF9F27",
    "muted":   T_MUTED,
}


def build_scorecard_html(scorecard: dict) -> str:
    """Inner HTML for the SCORECARD band, or "" when no backtest has matured."""
    if not scorecard or not scorecard.get("available"):
        return ""
    metrics = _scorecard_metrics(scorecard["summary"])
    if not metrics:
        return ""

    cells = []
    for m in metrics:
        color = _SCORE_TONE.get(m["tone"], T_MUTED)
        cells.append(f'''
        <td width="33%" align="center" style="padding:4px 8px;">
          <div style="font-family:monospace;font-size:11px;color:{T_LABEL};letter-spacing:.06em;
                      margin-bottom:6px;line-height:1.3;">{_esc(m["label"])}</div>
          <div style="font-family:monospace;font-size:26px;font-weight:700;color:{color};
                      line-height:1.1;">{_esc(m["value"])}</div>
          <div style="font-family:monospace;font-size:11px;color:{T_MUTED};margin-top:4px;
                      line-height:1.3;">{_esc(m["sub"])}</div>
        </td>''')

    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr valign="top">
      {''.join(cells)}
    </tr></table>'''


def _plaintext_scorecard_lines(scorecard: dict) -> list:
    """SCORECARD block for the text/plain part — mirrors build_scorecard_html."""
    if not scorecard or not scorecard.get("available"):
        return []
    lines = ["", "SCORECARD (latest backtest)"]
    for m in _scorecard_metrics(scorecard["summary"]):
        lines.append(f"  {m['label']:<24} {m['value']:>8}   ({m['sub']})")
    return lines


# ── What-changed diff (deterministic, Python-computed) ─────────────────────────

# A position below this weight is treated as "not in the book" — the boundary for
# the entered/exited test. Anything at or above it is a real holding.
_POS_THRESHOLD = 0.5   # %
# The reader does not care about a 0.3pp drift; only surface material moves.
_DELTA_THRESHOLD = 1.0   # percentage points


def collect_change_data(sb, tickers: list, data: dict) -> dict:
    """
    Compute what moved since the prior run — deterministically, in Python, so the
    section is a factual diff rather than another model narrative.

    Three diffs, in the order a reader asks about them:
      - flips     : analyst signal changes today (from signal_drift, the single
                    source of truth for flips — never recomputed here)
      - entered   : names that crossed into the book (~0 -> real weight)
      - exited    : names that dropped out of the book (real weight -> ~0)
      - deltas    : held-in-both names whose weight moved >= _DELTA_THRESHOLD

    Returns {"available": False, ...} when there is no prior run to compare against
    (a first-ever run, or only one allocation plan on record). "available" True with
    empty lists is a real, useful result: nothing changed.
    """
    change = {
        "available": False,
        "prior_date": None,
        "flips": [], "entered": [], "exited": [], "deltas": [],
    }

    # ── Signal flips — scoped to *today's* analyst run ────────────────────────
    # signal_drift only holds rows for runs that produced a flip, so the latest
    # drift row can belong to an older date. Anchor to today's report_date so a
    # quiet day correctly shows "no flips" instead of resurfacing a stale one.
    report_dates = [r.get("report_date") for r in (data.get("analyst_reports") or [])
                    if r.get("report_date")]
    latest_report_date = max(report_dates) if report_dates else None
    if latest_report_date:
        try:
            fr = (sb.table("signal_drift").select("*")
                    .eq("flip_date", latest_report_date)
                    .in_("ticker", tickers).execute())
            for d in sorted(fr.data or [], key=lambda x: str(x.get("flip_severity") or "")):
                change["flips"].append({
                    "ticker":      d.get("ticker", ""),
                    "signal_from": d.get("signal_from") or "?",
                    "signal_to":   d.get("signal_to") or "?",
                    "severity":    d.get("flip_severity") or "MINOR",
                    "is_chronic":  bool(d.get("is_chronic")),
                })
        except Exception:
            pass

    # ── Allocation diff — current plan vs the immediately prior plan ──────────
    current = data.get("allocation") or {}
    current_run = current.get("run_id")
    if not current_run and latest_report_date:     # a daily swing run: signal flips only
        change["available"], change["flips_only"] = True, True
        return change
    try:
        plans = (_book(sb.table("allocation_plans").select("run_id,plan_date"), "analyst_run_id")
                   .order("plan_date", desc=True).order("created_at", desc=True).limit(2).execute()).data or []
    except Exception:
        plans = []

    prior = next((p for p in plans if p.get("run_id") and p.get("run_id") != current_run), None)
    if not (current_run and prior):
        return change   # first run, or no distinct prior plan — nothing to diff

    change["available"]  = True
    change["prior_date"] = prior.get("plan_date")

    try:
        pr = (sb.table("allocation_details").select("ticker,allocated_pct")
                .eq("plan_run_id", prior["run_id"]).in_("ticker", tickers).execute())
        prior_map = {a.get("ticker"): float(a.get("allocated_pct") or 0) for a in (pr.data or [])}
    except Exception:
        prior_map = {}

    cur_map = {a.get("ticker"): float(a.get("allocated_pct") or 0)
               for a in (data.get("alloc_details") or [])}

    for tkr in sorted(set(cur_map) | set(prior_map)):
        now  = cur_map.get(tkr, 0.0)
        was  = prior_map.get(tkr, 0.0)
        in_now, in_was = now >= _POS_THRESHOLD, was >= _POS_THRESHOLD
        if in_now and not in_was:
            change["entered"].append({"ticker": tkr, "pct": now})
        elif in_was and not in_now:
            change["exited"].append({"ticker": tkr, "prior_pct": was})
        elif in_now and in_was and abs(now - was) >= _DELTA_THRESHOLD:
            change["deltas"].append({"ticker": tkr, "pct": now, "prior_pct": was,
                                     "delta": now - was})

    change["entered"].sort(key=lambda x: x["pct"], reverse=True)
    change["exited"].sort(key=lambda x: x["prior_pct"], reverse=True)
    change["deltas"].sort(key=lambda x: abs(x["delta"]), reverse=True)
    return change


_SEVERITY_COLORS = {"MAJOR": "#E24B4A", "MODERATE": "#EF9F27", "MINOR": T_MUTED}


def _badge_line(badge_txt, badge_color, body):
    """One badge + text row, shared by the WHAT CHANGED and SMART MONEY sections."""
    return (
        f'<div style="display:flex;gap:10px;align-items:baseline;padding:7px 0;'
        f'border-bottom:1px solid #1A1C1F;">'
        f'<span style="background:{badge_color}22;border:1px solid {badge_color}55;'
        f'color:{badge_color};font-family:monospace;font-size:11px;font-weight:700;'
        f'padding:1px 6px;border-radius:3px;white-space:nowrap;min-width:58px;'
        f'text-align:center;">{badge_txt}</span>'
        f'<span style="font-family:monospace;font-size:13px;color:{T_BODY};'
        f'line-height:1.55;">{body}</span></div>'
    )


# ── Smart money (famous investors — run_smart_money.py) ───────────────────────

SMART_MOVES_DAYS   = 7    # Form 4 / 13D / 13G window shown in the email
SMART_MAX_MOVES    = 6
SMART_MAX_HOLDINGS = 6
SMART_MAX_PER_FILING = 2   # one member's 40-trade report must not fill the list


def _move_badge(m: dict) -> tuple:
    if m.get("direction") == "STAKE":
        return ("13D", "#F4783C") if "13D" in m["form"] else ("13G", T_MUTED)
    return {"BUY": ("BUY", "#3CAE6F"), "SELL": ("SELL", "#E24B4A")}.get(
        m.get("direction"), ("PTR" if m.get("form") == "PTR" else "FORM 4", T_MUTED))


def _short_name(manager: str) -> str:
    """Last name for compact lines; skips suffixes ("Tom Kean Jr." → "Kean")."""
    parts = [p for p in (manager or "").split(" ") if p.rstrip(".") not in ("Jr", "Sr", "II", "III", "IV")]
    return parts[-1] if parts else (manager or "")


def collect_smart_money_data(sb, tickers: list) -> dict:
    """
    Deterministic smart-money digest for the email, from the run_smart_money.py
    tables. Three parts:
      - new_13f  : investors whose quarterly 13F landed this week (quarter-end news)
      - moves    : Form 4 / 13D / 13G filings this week, universe tickers first
      - holdings : universe tickers ranked by how many tracked funds hold them,
                   with last quarter's net buying vs selling (distinct funds)
    Unavailable (section omitted) when the tables are missing or empty.
    """
    out = {"available": False, "new_13f": [], "moves": [], "moves_total": 0,
           "holdings": [], "period": None}
    try:
        inv = (sb.table("smart_money_investors")
               .select("manager,fund,period_of_report,filing_date,positions,"
                       "new_count,added_count,reduced_count,sold_count")
               .execute().data or [])
    except Exception as e:
        print(f"  [WARN] smart money unavailable: {e}")
        return out
    if not inv:
        return out

    to_universe = {a: t for t in tickers for a in SHARE_CLASS_ALIASES.get(t, [t])}
    since = (datetime.date.today() - datetime.timedelta(days=SMART_MOVES_DAYS)).isoformat()

    out["available"] = True
    out["period"]    = max((i["period_of_report"] for i in inv if i.get("period_of_report")), default=None)
    out["new_13f"]   = sorted((i for i in inv if (i.get("filing_date") or "") >= since),
                              key=lambda i: i["filing_date"], reverse=True)

    try:
        moves = (sb.table("investor_filings")
                 .select("manager,form,filing_date,ticker,issuer,direction,summary,url")
                 .gte("filing_date", since).neq("direction", "SKIPPED")
                 .order("filing_date", desc=True)
                 .limit(100).execute().data or [])
    except Exception as e:
        print(f"  [WARN] smart money filings unavailable: {e}")
        moves = []
    for m in moves:
        m["universe"] = to_universe.get(m.get("ticker"))
    moves.sort(key=lambda m: m["universe"] is None)   # stable: date order kept within each group
    picked, per_filing = [], {}
    for m in moves:
        key = (m["manager"], m["filing_date"], m["form"])
        if per_filing.get(key, 0) < SMART_MAX_PER_FILING:
            per_filing[key] = per_filing.get(key, 0) + 1
            picked.append(m)
    out["moves"], out["moves_total"] = picked[:SMART_MAX_MOVES], len(moves)

    try:
        rows = (sb.table("investor_holdings")
                .select("manager,ticker,change_type,pct_portfolio")
                .in_("ticker", list(to_universe)).eq("put_call", "")
                .execute().data or [])
    except Exception as e:
        print(f"  [WARN] smart money holdings unavailable: {e}")
        rows = []
    by_t: dict = {}
    for r in rows:
        by_t.setdefault(to_universe[r["ticker"]], []).append(r)
    agg = []
    for t, rs in by_t.items():
        held = [r for r in rs if r["change_type"] != "SOLD"]
        if not held:
            continue
        top = max(held, key=lambda r: float(r.get("pct_portfolio") or 0))
        agg.append({
            "ticker":      t,
            "holders":     len({r["manager"] for r in held}),
            "buying":      len({r["manager"] for r in rs if r["change_type"] in ("NEW", "ADDED")}),
            "selling":     len({r["manager"] for r in rs if r["change_type"] in ("REDUCED", "SOLD")}),
            "top_manager": top["manager"],
            "top_pct":     float(top.get("pct_portfolio") or 0),
        })
    agg.sort(key=lambda a: (-a["holders"], -(a["buying"] - a["selling"])))
    out["holdings"] = agg[:SMART_MAX_HOLDINGS]
    return out


def build_smart_money_html(sm: dict) -> str:
    """Inner HTML for the SMART MONEY section, or "" when there is no data."""
    if not sm or not sm.get("available"):
        return ""

    def _sub(text, top):
        return (f'<div style="font-family:monospace;font-size:11px;color:{T_LABEL};'
                f'letter-spacing:.08em;margin:{top}px 0 2px;">{text}</div>')

    html = ""
    for i in sm["new_13f"]:
        body = (f'<span style="font-weight:700;">{_esc(i["manager"])}</span> filed a 13F '
                f'(as of {_esc(str(i["period_of_report"]))}): '
                f'<span style="color:#2ECFA8;">{i["new_count"]} new</span> &middot; '
                f'<span style="color:#3CAE6F;">{i["added_count"]} added</span> &middot; '
                f'<span style="color:#EF9F27;">{i["reduced_count"]} reduced</span> &middot; '
                f'<span style="color:#E24B4A;">{i["sold_count"]} sold out</span>')
        html += _badge_line("13F", "#2ECFA8", body)

    html += _sub(f"LATEST MOVES &middot; LAST {SMART_MOVES_DAYS} DAYS", 4)
    if not sm["moves"]:
        html += (f'<div style="font-family:monospace;font-size:13px;color:{T_MUTED};padding:7px 0;">'
                 f'No Form 4 / 13D / 13G / congressional filings by tracked investors this week.</div>')
    for m in sm["moves"]:
        badge, color = _move_badge(m)
        tk_color = "#EF9F27" if m["universe"] else T_BODY
        name = m.get("ticker") or m.get("issuer") or "?"
        body = (f'<span style="color:{T_MUTED};">{_esc(str(m["filing_date"])[5:])}</span> '
                f'<span style="font-weight:700;">{_esc(_short_name(m["manager"]))}</span> '
                f'<span style="color:{tk_color};font-weight:700;">{_esc(name)}</span> '
                f'{_esc(m.get("summary") or "")} '
                f'<a href="{_esc(m.get("url") or "")}" style="color:#378ADD;text-decoration:none;">'
                f'{"PDF" if m.get("form") == "PTR" else "SEC"}&nbsp;&#8599;</a>')
        html += _badge_line(badge, color, body)
    extra = sm["moves_total"] - len(sm["moves"])
    if extra > 0:
        html += (f'<div style="font-family:monospace;font-size:12px;color:{T_LABEL};padding:6px 0;">'
                 f'+{extra} more on the dashboard&rsquo;s SMART MONEY tab</div>')

    if sm["holdings"]:
        html += _sub(f"YOUR UNIVERSE &middot; 13F AS OF {_esc(str(sm['period']))} "
                     f"(UP TO 45 DAYS STALE)", 14)
        for h in sm["holdings"]:
            net = h["buying"] - h["selling"]
            color = "#3CAE6F" if net > 0 else "#E24B4A" if net < 0 else T_MUTED
            flow = []
            if h["buying"]:
                flow.append(f'<span style="color:#3CAE6F;">&#9650;{h["buying"]} buying</span>')
            if h["selling"]:
                flow.append(f'<span style="color:#E24B4A;">&#9660;{h["selling"]} selling</span>')
            if not flow:
                flow.append(f'<span style="color:{T_MUTED};">no change</span>')
            body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(h["ticker"])}</span> '
                    f'{" ".join(flow)} '
                    f'<span style="color:{T_MUTED};">&middot; top: {_esc(_short_name(h["top_manager"]))} '
                    f'{h["top_pct"]:.1f}% of book</span>')
            html += _badge_line(f'{h["holders"]} FUND{"S" if h["holders"] != 1 else ""}', color, body)
    return html


def _plaintext_smart_money_lines(sm: dict) -> list:
    """SMART MONEY block for the text/plain part — mirrors build_smart_money_html."""
    if not sm or not sm.get("available"):
        return []
    lines = ["", "SMART MONEY (famous investors and Congress, SEC + House filings)"]
    for i in sm["new_13f"]:
        lines.append(f"  13F    {i['manager']} filed (as of {i['period_of_report']}): "
                     f"{i['new_count']} new, {i['added_count']} added, "
                     f"{i['reduced_count']} reduced, {i['sold_count']} sold out")
    lines.append(f"  Latest moves, last {SMART_MOVES_DAYS} days:")
    if not sm["moves"]:
        lines.append("    None this week.")
    for m in sm["moves"]:
        badge, _ = _move_badge(m)
        lines.append(f"    {str(m['filing_date'])[5:]}  {badge:<6} {_short_name(m['manager']):<14} "
                     f"{(m.get('ticker') or m.get('issuer') or '?'):<6} {m.get('summary') or ''}")
    extra = sm["moves_total"] - len(sm["moves"])
    if extra > 0:
        lines.append(f"    +{extra} more on the dashboard")
    if sm["holdings"]:
        lines.append(f"  Your universe (13F as of {sm['period']}, up to 45 days stale):")
        for h in sm["holdings"]:
            lines.append(f"    {h['ticker']:<6} {h['holders']} funds  +{h['buying']} buying / "
                         f"-{h['selling']} selling  top: {_short_name(h['top_manager'])} "
                         f"{h['top_pct']:.1f}%")
    return lines


# ── Swing trade plans (TRADING_MODE=swing — swing_policy / swing_backtest) ──

# Account size the sizing preview assumes. The live order size comes from the
# broker snapshot in run_trade_plan.py; this is only an indication.
SWING_PREVIEW_EQUITY = float(os.environ.get("SWING_PREVIEW_EQUITY", "5000"))
_STRENGTH_RANK = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}


def collect_swing_data(sb, data: dict) -> dict:
    """
    Swing plans from the latest analyst run (already loaded in data), plus the
    closed-trade record from swing_backtest_trades. Unavailable when the latest
    run was not a swing run, so the section is omitted in position mode.
    """
    reports = [r for r in data.get("analyst_reports") or [] if r.get("trading_mode") == "swing"]
    if not reports:
        return {"available": False}
    risk = data.get("risk") or {}
    regime = data.get("regime") or {}
    level = effective_level(risk.get("market_risk_level"), regime.get("risk_floor"))
    caps = {d["ticker"]: d for d in data.get("risk_details") or []}

    plans = []
    for r in reports:
        if r.get("signal") != "BUY" or r.get("entry_price") is None:
            continue
        p = {**r, **{k: float(r[k]) for k in ("entry_price", "stop_price", "target_price")}}
        cap = (caps.get(r["ticker"]) or {}).get("max_position_pct")
        qty, risk_usd = size_entry(p, SWING_PREVIEW_EQUITY, level, float(cap) if cap is not None else None)
        plans.append({**p, "preview_qty": qty, "preview_risk": risk_usd,
                      "preview_excluded": ((caps.get(r["ticker"]) or {}).get("risk_score") or 0) >= 8})
    plans.sort(key=lambda p: (_STRENGTH_RANK.get(p.get("signal_strength"), 3), -float(p.get("risk_reward") or 0)))

    overridden = [r for r in reports if r.get("model_signal") and r["model_signal"] != r.get("signal")]
    try:
        record = build_swing_feedback(sb)
    except Exception:
        record = None
    return {
        "available":  True,
        "plans":      plans,
        "exits":      [r for r in reports if r.get("signal") == "SELL"],
        "overridden": overridden,
        "level":      level,
        "record":     (record or {}).get("overall"),
        "fill_rate":  (record or {}).get("fill_rate"),
    }


def _pct_from(entry: float, px: float) -> str:
    return f"{(px / entry - 1) * 100:+.1f}%"


def _swing_record_text(sw: dict) -> str:
    rec = sw.get("record")
    if not rec:
        return ("No closed swing trades yet. The backtest scores each plan once its stop, "
                "target or time limit is reached.")
    pf = f", profit factor {rec['profit_factor']}" if rec.get("profit_factor") is not None else ""
    fill = f", {sw['fill_rate'] * 100:.0f}% of entries filled" if sw.get("fill_rate") is not None else ""
    return (f"{rec['n']} closed trade(s): {rec['win_rate'] * 100:.0f}% winners, "
            f"{rec['avg_r']:+.2f}R per trade{pf}, avg hold {rec['avg_hold']} sessions{fill}.")


def _swing_size_text(p: dict, html: bool) -> str:
    if p["preview_excluded"]:
        return "excluded by risk manager"
    if p["preview_qty"] < 1:
        return "too small to size"
    approx = "&asymp;" if html else "~"
    return (f"{approx}{p['preview_qty']} sh (${p['preview_qty'] * p['entry_price']:,.0f}), "
            f"risk ${p['preview_risk']:,.0f}")


def build_swing_html(sw: dict) -> str:
    """Inner HTML for the SWING TRADE PLANS section, or "" outside swing mode."""
    if not sw or not sw.get("available"):
        return ""

    def _sub(text, top):
        return (f'<div style="font-family:monospace;font-size:11px;color:{T_LABEL};'
                f'letter-spacing:.08em;margin:{top}px 0 2px;">{text}</div>')

    html = _sub(f"NEW ENTRIES &middot; LIMIT ORDERS GOOD FOR 2 SESSIONS &middot; "
                f"SIZED FOR ${SWING_PREVIEW_EQUITY:,.0f} AT {RISK_PER_TRADE_PCT:g}% RISK, LEVEL {_esc(sw['level'])}", 0)
    if not sw["plans"]:
        html += (f'<div style="font-family:monospace;font-size:13px;color:{T_MUTED};padding:7px 0;">'
                 f'No new entries today: no setup cleared the rules.</div>')
    for p in sw["plans"]:
        e, st, tg = p["entry_price"], p["stop_price"], p["target_price"]
        wrong = (f' &middot; wrong if: {_esc(_smart_truncate(p["invalidation"], 110))}'
                 if p.get("invalidation") else "")
        body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(p["ticker"])}</span> '
                f'<span style="color:{T_MUTED};">{_esc(p.get("setup_type") or "")} &middot; '
                f'{_esc(p.get("signal_strength") or "")}</span><br>'
                f'limit <b>{e:.2f}</b> &middot; stop <span style="color:#E24B4A;">{st:.2f}</span> '
                f'({_pct_from(e, st)}) &middot; target <span style="color:#3CAE6F;">{tg:.2f}</span> '
                f'({_pct_from(e, tg)}) &middot; {float(p.get("risk_reward") or 0):.1f}R &middot; '
                f'{p.get("horizon_days")}d<br>'
                f'<span style="color:{T_MUTED};">{_swing_size_text(p, True)}{wrong}</span>')
        html += _badge_line("BUY", "#3CAE6F", body)

    if sw["exits"]:
        html += _sub("EXIT SIGNALS &middot; SELL AT THE OPEN IF HELD, NEVER ON THE BUY DAY", 14)
        for r in sw["exits"]:
            why = (f'<span style="color:{T_MUTED};">wrong if:</span> {_esc(_smart_truncate(r["invalidation"], 130))}'
                   if r.get("invalidation") else _esc(_smart_truncate(r.get("summary") or "", 140)))
            body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(r["ticker"])}</span> '
                    f'<span style="color:{T_MUTED};">{_esc(r.get("setup_type") or "")}</span> {why}')
            html += _badge_line("SELL", "#E24B4A", body)

    if sw["overridden"]:
        html += _sub("OVERRIDDEN BY THE RULES &middot; MODEL &rarr; FINAL", 14)
        for r in sw["overridden"]:
            notes = _parse_jsonish(r.get("swing_notes"), []) or []
            body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(r["ticker"])}</span> '
                    f'{_esc(r["model_signal"])} &rarr; {_esc(r.get("signal") or "")} '
                    f'<span style="color:{T_MUTED};">{_esc(notes[0] if notes else "")}</span>')
            html += _badge_line("RULE", T_MUTED, body)

    html += _sub("TRACK RECORD &middot; SIMULATED ON DAILY BARS, NET OF COSTS", 14)
    html += (f'<div style="font-family:monospace;font-size:13px;color:{T_BODY};padding:7px 0;'
             f'line-height:1.55;">{_esc(_swing_record_text(sw))}</div>')
    return html


def _plaintext_swing_lines(sw: dict) -> list:
    """SWING TRADE PLANS block for the text/plain part — mirrors build_swing_html."""
    if not sw or not sw.get("available"):
        return []
    lines = ["", f"SWING TRADE PLANS ({SWING_MIN_HOLD}-{SWING_MAX_HOLD} day holds, long only, no day trades)",
             f"  New entries (limit orders good for 2 sessions; sized for ${SWING_PREVIEW_EQUITY:,.0f} "
             f"at {RISK_PER_TRADE_PCT:g}% risk, level {sw['level']}):"]
    if not sw["plans"]:
        lines.append("    None today.")
    for p in sw["plans"]:
        e = p["entry_price"]
        lines.append(f"    {p['ticker']:<6} {(p.get('setup_type') or ''):<9} limit {e:.2f}  "
                     f"stop {p['stop_price']:.2f} ({_pct_from(e, p['stop_price'])})  "
                     f"target {p['target_price']:.2f} ({_pct_from(e, p['target_price'])})  "
                     f"{float(p.get('risk_reward') or 0):.1f}R  {p.get('horizon_days')}d  "
                     f"{_swing_size_text(p, False)}")
    if sw["exits"]:
        lines.append("  Exit signals (sell at the open if held):")
        lines += [f"    {r['ticker']:<6} {r.get('setup_type') or ''}" for r in sw["exits"]]
    if sw["overridden"]:
        lines.append("  Overridden by the rules:")
        for r in sw["overridden"]:
            notes = _parse_jsonish(r.get("swing_notes"), []) or []
            lines.append(f"    {r['ticker']:<6} {r['model_signal']} -> {r.get('signal')}  {notes[0] if notes else ''}")
    lines.append(f"  Track record: {_swing_record_text(sw)}")
    return lines


def build_change_html(change: dict) -> str:
    """Inner HTML for the WHAT CHANGED section, or "" when there is no prior run."""
    if not change.get("available"):
        return ""

    prior_s = str(change.get("prior_date") or "the prior run")
    n_total = (len(change["flips"]) + len(change["entered"])
               + len(change["exited"]) + len(change["deltas"]))

    if n_total == 0 and change.get("flips_only"):
        return ('<div style="font-family:monospace;font-size:13px;color:#3CAE6F;padding:8px 0;'
                'line-height:1.6;">No signal flips.</div>')
    if n_total == 0:
        return (f'<div style="font-family:monospace;font-size:13px;color:#3CAE6F;padding:8px 0;'
                f'line-height:1.6;">No signal flips, no positions entered or exited, and no weight '
                f'moved &ge; {_DELTA_THRESHOLD:.0f}pp since {_esc(prior_s)}. Book is steady.</div>')

    rows = ""
    _line = _badge_line

    for f in change["flips"]:
        sev_color = _SEVERITY_COLORS.get(f["severity"], T_MUTED)
        chronic = (' <span style="color:#E24B4A;font-weight:700;">CHRONIC</span>'
                   if f["is_chronic"] else "")
        from_c = SIGNAL_COLORS.get(f["signal_from"], T_MUTED)
        to_c   = SIGNAL_COLORS.get(f["signal_to"], T_MUTED)
        body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(f["ticker"])}</span> '
                f'<span style="color:{from_c};">{_esc(f["signal_from"])}</span> '
                f'<span style="color:{T_FAINT};">&rarr;</span> '
                f'<span style="color:{to_c};font-weight:700;">{_esc(f["signal_to"])}</span>'
                f'<span style="color:{sev_color};"> &middot; {_esc(f["severity"])}</span>{chronic}')
        rows += _line("FLIP", sev_color, body)

    for e in change["entered"]:
        body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(e["ticker"])}</span> '
                f'new position at <span style="color:#3CAE6F;font-weight:700;">{e["pct"]:.1f}%</span>')
        rows += _line("ENTER", "#3CAE6F", body)

    for x in change["exited"]:
        body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(x["ticker"])}</span> '
                f'exited the book (was <span style="color:{T_MUTED};font-weight:700;">'
                f'{x["prior_pct"]:.1f}%</span>)')
        rows += _line("EXIT", "#E24B4A", body)

    for d in change["deltas"]:
        arrow_c = "#3CAE6F" if d["delta"] > 0 else "#E24B4A"
        sign = "+" if d["delta"] > 0 else ""
        body = (f'<span style="color:#EF9F27;font-weight:700;">{_esc(d["ticker"])}</span> '
                f'<span style="color:{T_MUTED};">{d["prior_pct"]:.1f}%</span> '
                f'<span style="color:{T_FAINT};">&rarr;</span> '
                f'<span style="color:{T_BODY};font-weight:700;">{d["pct"]:.1f}%</span> '
                f'<span style="color:{arrow_c};font-weight:700;">({sign}{d["delta"]:.1f}pp)</span>')
        rows += _line("WEIGHT", arrow_c, body)

    return rows


# ── SMTP sender ────────────────────────────────────────────────────────────────

def send_email(html: str, plaintext: str, subject: str, dry_run: bool) -> bool:
    if not NEWSLETTER_TO:
        print("  [WARN] NEWSLETTER_TO not set in .env — skipping send")
        return False
    if not SMTP_USER or not SMTP_PASSWORD:
        print("  [WARN] SMTP_USER or SMTP_PASSWORD not set — skipping send")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = NEWSLETTER_FROM
    # Recipients go on the SMTP envelope only (below), never in a visible header,
    # so subscribers never see each other's addresses. The visible To: is the
    # brand itself. No Bcc header is added — SMTP delivery uses the envelope list.
    msg["To"]      = NEWSLETTER_FROM
    # Real plaintext alternative (not a stub) — for screen readers and text clients.
    msg.attach(MIMEText(plaintext, "plain"))
    msg.attach(MIMEText(html, "html"))

    if dry_run:
        print(f"  [DRY RUN] Would BCC {len(NEWSLETTER_TO)} recipient(s): {', '.join(NEWSLETTER_TO)}")
        return True

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, NEWSLETTER_TO, msg.as_string())
        print(f"  [OK] Email sent (BCC) to {len(NEWSLETTER_TO)} recipient(s): {', '.join(NEWSLETTER_TO)}")
        return True
    except Exception as e:
        print(f"  [ERROR] Failed to send email: {e}")
        return False


# ── Once per day ───────────────────────────────────────────────────────────────
# More than one pipeline can finish on the same day (the 6:30 ET dispatch, the GitHub
# cron fallback, a dashboard RUN PIPELINE click), and each used to email the
# newsletter again. A sent newsletter is recorded in job_runs; later runs that day
# skip the send unless --force. The weekly long-term book is tracked separately.

def _newsletter_job() -> str:
    return "newsletter_longterm" if os.environ.get("BOOK") == "longterm" else "newsletter"


def _et_date() -> datetime.date:
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo("America/New_York")).date()


def _et_today() -> str:
    return _et_date().isoformat()


def _market_open(d: datetime.date) -> bool:
    """True if NYSE has a session on d. Falls back to a plain weekday check when
    pandas_market_calendars is unavailable — matches is_session in run_trade_plan.py."""
    try:
        import pandas_market_calendars as mcal
        return not mcal.get_calendar("NYSE").schedule(start_date=d, end_date=d).empty
    except Exception:
        return d.weekday() < 5


def _skip_closed_market(job: str, d: datetime.date) -> bool:
    return job == "newsletter" and not _market_open(d)


def _save_outputs(prefix: str, html: str, plaintext: str) -> None:
    today = datetime.date.today().isoformat()
    for ext, body in (("html", html), ("txt", plaintext)):
        fname = f"{prefix}_{today}.{ext}"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"  [OK] {'HTML' if ext == 'html' else 'Plaintext'} saved: {fname}")


def already_sent(sb, job: str, day: str):
    """The job_runs row of today's sent newsletter, or None. Unknown counts as not sent."""
    try:
        r = (sb.table("job_runs").select("started_at,gh_run_url").eq("job", job).eq("run_date", day)
               .eq("status", "OK").limit(1).execute().data)
        return r[0] if r else None
    except Exception as e:
        print(f"  [WARN] could not check job_runs for an earlier send ({e}); sending")
        return None


def record_sent(sb, job: str, day: str, subject: str, run_id) -> None:
    url = os.environ.get("GITHUB_SERVER_URL")
    gh = f"{url}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}" if url else None
    try:
        sb.table("job_runs").insert({"job": job, "run_date": day, "status": "OK", "summary": subject[:2000],
                                     "details": {"run_id": run_id}, "gh_run_url": gh,
                                     "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}).execute()
    except Exception as e:
        print(f"  [WARN] newsletter sent but not recorded in job_runs ({e})")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Daily Newsletter Agent")
    parser.add_argument("--dry-run",     action="store_true", help="Generate HTML only, don't send")
    parser.add_argument("--output-html", action="store_true", help="Save HTML to file")
    parser.add_argument("--run-id",      help="Pipeline run ID (recorded with the send)")
    parser.add_argument("--force",       action="store_true", help="Send even if today's newsletter already went out")
    args = parser.parse_args()

    dry_run     = args.dry_run
    output_html = args.output_html or dry_run

    cfg = load_config()

    print(f"\n{'='*55}")
    print(f"  Orblex Industries — Newsletter Agent")
    print(f"  Date    : {datetime.date.today()}")
    print(f"  Tickers : {', '.join(cfg.tickers)}")
    print(f"  Dry run : {dry_run}")
    print(f"{'='*55}\n")

    sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
    job, et_day = _newsletter_job(), _et_today()
    # Swing brief only: a closed market has no trades to report, and its summary is
    # the one Claude call in this script, so skip before it. The long-term letter
    # goes out on Saturday by design and makes no model call.
    if not dry_run and not args.force and _skip_closed_market(job, _et_date()):
        print(f"  [SKIP] NYSE is closed ({et_day}) — no newsletter sent, no LLM credits spent; use --force to override")
        return
    if not dry_run and not args.force:
        prev = already_sent(sb, job, et_day)
        if prev:
            print(f"  [SKIP] today's {job} already went out at {prev['started_at']} "
                  f"({prev.get('gh_run_url') or 'local run'}); use --force to send again")
            return

    if job == "newsletter_longterm":
        print("[1/2] Reading this week's long-term advice...")
        rows = collect_longterm_rows(sb, args.run_id)
        print(f"  Advice rows   : {len(rows)} ({len(_lt_actions(rows))} actionable)")
        print("[2/2] Building and sending the weekly review (HTML + plaintext)...")
        html, plaintext = build_longterm_html(rows), build_longterm_plaintext(rows)
        subject = longterm_subject(rows)
        print(f"  Subject: {subject}")
        if output_html:
            _save_outputs("newsletter_longterm", html, plaintext)
        if send_email(html, plaintext, subject, dry_run) and not dry_run:
            record_sent(sb, job, et_day, subject, args.run_id)
        print(f"\n[OK] Newsletter agent complete.\n")
        return

    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    print("[1/3] Collecting data from all agents...")
    data = collect_data(sb, cfg.tickers)
    print(f"  Analyst reports : {len(data['analyst_reports'])}")
    print(f"  Options entries : {len(data['options'])}")
    print(f"  Earnings entries: {len(data['earnings'])}")
    print(f"  Macro snapshot  : {'yes' if data['macro'] else 'no'}")
    print(f"  Market regime   : {data['regime']['regime'] if data['regime'] else 'none — run run_regime.py'}")
    if data["allocation"]:
        enforcer_ran, violations = _enforcer_state(data["allocation"])
        if not enforcer_ran:
            print("  Enforcer        : NOT VERIFIED — did not run for this plan")
        else:
            print(f"  Enforcer        : {len(violations)} correction(s)" if violations
                  else "  Enforcer        : fully compliant")

    book_tickers = sorted(set(cfg.tickers) | set(data["tickers"]))
    change = collect_change_data(sb, book_tickers, data)
    if not change["available"]:
        print("  What changed  : no prior plan to diff against")
    else:
        print(f"  What changed  : {len(change['flips'])} flip(s), "
              f"{len(change['entered'])} entered, {len(change['exited'])} exited, "
              f"{len(change['deltas'])} weight move(s) vs {change['prior_date']}")

    scorecard = collect_scorecard_data(sb, book_tickers)
    if not scorecard["available"]:
        print("  Scorecard     : no matured backtest yet — band omitted")
    else:
        m = _scorecard_metrics(scorecard["summary"])
        print("  Scorecard     : " + "  ".join(f"{x['label'].split(' (')[0]}={x['value']}" for x in m))

    swing = collect_swing_data(sb, data)
    if not swing["available"]:
        print("  Swing plans   : no swing-mode reports in the latest run; section omitted")
    else:
        print(f"  Swing plans   : {len(swing['plans'])} BUY plan(s), {len(swing['exits'])} exit signal(s), "
              f"{len(swing['overridden'])} overridden by rules")

    print("[2/3] Generating newsletter summary with Claude...")
    summary = claude_newsletter_summary_v2(claude, data)
    print(f"  Tokens: {summary.get('prompt_tokens', 0)} in / {summary.get('output_tokens', 0)} out")

    print("[3/3] Building and sending email (HTML + plaintext)...")
    html      = build_html_v2(data, summary, change, scorecard, swing)
    plaintext = build_plaintext(data, summary, change, scorecard, swing)
    subject   = swing_subject(swing, datetime.date.today())
    print(f"  Subject: {subject}")

    if output_html:
        _save_outputs("newsletter_swing", html, plaintext)

    if send_email(html, plaintext, subject, dry_run) and not dry_run:
        record_sent(sb, job, et_day, subject, args.run_id)
    print(f"\n[OK] Newsletter agent complete.\n")


if __name__ == "__main__":
    main()