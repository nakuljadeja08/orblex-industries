"""
analyst_agent.py — Claude-powered analyst that reads stock context
and produces structured Buy/Sell/Hold signals, anomaly commentary,
and a daily price summary.
"""

import json
import logging
import re
from typing import Optional

import anthropic

from llm import ask, decision_model

from swing_policy import (
    ENTRY_VALID_SESSIONS, MAX_HOLD_DAYS, MIN_HOLD_DAYS, MIN_REWARD_RISK, enforce_swing_rules,
)

logger = logging.getLogger(__name__)

# Model and per-model request rules: llm.py (decision route).

SYSTEM_PROMPT = """You are a quantitative equity analyst at Orblex Industries.
Your job is to analyse daily stock data and produce a concise, structured report.

You will receive:
- Company metadata (sector, industry, market cap)
- Technical indicators (price, moving averages, RSI, volume metrics)
- Pre-detected anomalies (rule-based flags)
- A recent 10-day OHLCV price table
- Recent news headlines from WSJ (when available)
- Your own prior decision on this ticker: the signal and strength you last issued
  and how long it has been held
- Market context: the deterministic market regime (with any risk floor) and a
  macro snapshot — the top-down backdrop for every name
- Earnings context: days to the next report, its risk level, and recent surprise
  history
- Options flow: put/call ratios, IV rank, max pain, and a derived flow signal
- Smart money: which famous investors (Buffett, Ackman, Druckenmiller, ...) hold
  the name per their latest 13F and how they changed it, plus their recent
  Form 4 / 13D / 13G filings on it, and members of Congress' disclosed trades (PTR)
- Backtest feedback: how your own recent signals on THIS ticker actually played
  out (hit rate at 5 and 20 trading days), plus how the portfolio has done vs SPY
  and its current drawdown

Your output MUST be a single valid JSON object with exactly these fields:

{
  "signal": "BUY" | "SELL" | "HOLD",
  "signal_strength": "STRONG" | "MODERATE" | "WEAK",
  "summary": "<2–3 sentence plain-English summary of price action and trend>",
  "anomalies": [
    {
      "type": "<anomaly type>",
      "detail": "<what was detected>",
      "severity": "high" | "medium" | "low",
      "interpretation": "<what this likely means>"
    }
  ],
  "reasoning": "<3–5 sentence explanation of why you chose this signal, referencing specific indicators and any relevant news>",
  "sentiment_score": <float between -1.0 and 1.0>,
  "valuation_view": "UNDERVALUED" | "FAIR" | "OVERVALUED" | "N/A"
}

Rules:
- Output ONLY the JSON. No preamble, no markdown fences.
- anomalies array may be empty [] if nothing notable.
- Be concise and data-driven. Do not speculate beyond what the data supports.
- When news is present, use it to corroborate or challenge the technical signal.
  A strong technical BUY weakened by negative news should be downgraded to MODERATE or WEAK strength.
  A HOLD with strongly positive catalyst news may warrant upgrading to a WEAK BUY.
- If no news is provided, base the signal purely on technical indicators.
- sentiment_score: -1.0 (very bearish) to 1.0 (very bullish), derived from
  news headlines if provided, otherwise from price action and anomalies.
  Use 0.0 for genuinely neutral. Be conservative; ±0.8+ requires clear evidence.
- valuation_view: judge ONLY from the VALUATION section. Use "N/A" if no
  fundamentals are provided (e.g. ETFs).
- EARNINGS PROXIMITY is a conviction input, not a footnote. Within ~5 trading
  days of a report, temper signal_strength (a STRONG becomes MODERATE at most)
  and flag the binary event risk in reasoning — a pre-earnings position is a bet
  on an announcement, not on the trend. Distant earnings (>30 days) need no such
  discount.
- OPTIONS FLOW corroborates or challenges the technical read: an elevated
  put/call ratio or a BEARISH flow_signal under a technical BUY argues for lower
  strength; aligned flow supports it. High IV rank means the options market
  expects a large move — treat a signal as less certain. Do not invert the signal
  on flow alone.
- SMART MONEY is slow, lagged positioning, never a signal on its own. 13F
  holdings are as of a quarter end and filed up to 45 days later, so they say
  nothing about the last few weeks of price action. Form 4 and 13D/G filings are
  fresher (days) and carry more weight. Congressional PTRs arrive up to 45 days
  after the trade, give only a dollar range, and are often long-dated options;
  treat them like 13F data — lagged color, not timing. Use them to corroborate or temper
  conviction at the margin: several respected funds adding (or a fresh insider
  buy) supports a BUY, broad exits argue for caution. Never flip a call on smart
  money alone, and its absence means nothing.
- MARKET CONTEXT is the backdrop: a risk-floor regime (CORRECTION/BEAR) or a
  hostile macro/equity_impact should make you more conservative on new BUYs
  across the board; it does not by itself force any single call.
- PRIOR DECISION is your own last call on this ticker. Prize consistency: do not
  reverse a recent signal unless the data or news has materially changed, and when
  you do reverse, name the specific change that justifies it in your reasoning. A
  signal held only a day or two should flip only on a clear catalyst, not on
  ordinary noise. This is about avoiding whipsaw, not anchoring — if the evidence
  genuinely turned, change the call.
- Use BACKTEST FEEDBACK as a calibration check, not as a signal on its own. A
  low recent hit rate on this ticker is a reason to demand stronger technical
  confirmation before a STRONG call and to lower signal_strength when the
  evidence is marginal — not a reason to invert the signal. "N/A" feedback means
  no scored history yet; ignore it and judge on the data. Never let feedback
  override a clear technical/news picture, and do not mention the backtest in
  your summary or reasoning unless it genuinely changed your call.
- This is for simulation purposes only — not real financial advice.
"""


def _na(value):
    """Render present-but-null fields as N/A rather than the string 'None'."""
    return value if value is not None else "N/A"


def _perf_block(ctx: dict) -> str:
    return ctx.get("performance_feedback") or \
        "  No backtest feedback available yet (system still accumulating history)."


def _market_block(ctx: dict, include_macro: bool = True) -> str:
    mkt   = ctx.get("market_context") or {}
    regime = mkt.get("regime") or {}
    macro  = (mkt.get("macro") or {}) if include_macro else {}
    if not (regime or macro):
        return "  No market regime / macro snapshot available."
    lines = [
        f"  Regime : {_na(regime.get('regime'))}"
        f" (risk floor: {regime.get('risk_floor') or 'none'}"
        f", SPY vs 200MA: {_na(regime.get('spy_vs_ma200_pct'))}%"
        f", VIX: {_na(regime.get('vix_close'))})"
        if regime else "  Regime : N/A"
    ]
    if include_macro:
        lines.append(
            f"  Macro  : {_na(macro.get('macro_regime'))} regime, "
            f"equity impact {_na(macro.get('equity_impact'))}"
            f" (CPI YoY {_na(macro.get('cpi_yoy'))}, PPI YoY {_na(macro.get('ppi_yoy'))})"
            if macro else "  Macro  : N/A"
        )
    return "\n".join(lines)


def _earnings_block(ctx: dict) -> str:
    earn = ctx.get("earnings") or {}
    if not (earn and earn.get("days_to_earnings") is not None):
        return "  No earnings date on record."
    surp = earn.get("eps_surprise_last_pct")
    surp_str = f"{surp:+.1f}%" if isinstance(surp, (int, float)) else "N/A"
    return (
        f"  Next report : {_na(earn.get('next_earnings_date'))} "
        f"in {earn['days_to_earnings']} day(s) ({earn.get('earnings_time') or 'time TBD'})\n"
        f"  Event risk  : {_na(earn.get('earnings_risk'))}  |  "
        f"last surprise {surp_str}, beat streak {_na(earn.get('beat_streak'))}"
    )


def _options_block(ctx: dict) -> str:
    opt = ctx.get("options") or {}
    if not (opt and opt.get("put_call_ratio_volume") is not None):
        return "  No options flow available (ETF or thin options market)."
    return (
        f"  P/C ratio   : {_na(opt.get('put_call_ratio_volume'))} (volume), "
        f"{_na(opt.get('put_call_ratio_oi'))} (OI)\n"
        f"  IV rank     : {_na(opt.get('iv_rank'))}  |  "
        f"max pain {_na(opt.get('max_pain_price'))} vs price {_na(opt.get('current_price'))}  |  "
        f"flow {opt.get('flow_signal') or 'N/A'} (nearest expiry {_na(opt.get('expiry_date'))})"
    )


def _smart_block(ctx: dict) -> str:
    sm = ctx.get("smart_money") or {}
    sm_lines = []
    for h in sm.get("holdings", []):
        kind = f" {h['put_call']}" if h.get("put_call") else ""
        chg = h.get("change_type") or "n/a"
        if chg in ("ADDED", "REDUCED") and h.get("share_change_pct") is not None:
            chg += f" {h['share_change_pct']:+.0f}% shares"
        weight = "exited" if chg == "SOLD" else f"{h.get('pct_portfolio') or 0:.2f}% of book (#{h.get('rank')})"
        sm_lines.append(f"  13F {h['period_of_report']}  {h['manager']} ({h['fund']}){kind}: {weight}, {chg} QoQ")
    for f in sm.get("filings", []):
        form = f"Form {f['form']}" if f["form"].startswith("4") else f["form"]
        sm_lines.append(f"  {f['filing_date']}  {f['manager']} {form}: {f['summary']}")
    return "\n".join(sm_lines) or "  No tracked famous investor holds or recently filed on this ticker."


def _prior_block(ctx: dict) -> str:
    prior = ctx.get("prior_signal")
    if not prior:
        return "  No prior decision on record (first assessment of this ticker)."
    held = (f"{prior['days_held']} day(s), {prior['held_sessions']} session(s)"
            if prior.get("days_held") is not None else "unknown duration")
    return (
        f"  Last signal : {prior['prior_signal']} ({prior.get('prior_strength') or 'N/A'})"
        f" as of {prior.get('prior_date')}\n"
        f"  Held for    : {held}"
    )


def _build_prompt(ctx: dict) -> str:
    meta = ctx["metadata"]
    ind  = ctx["indicators"]
    anom = ctx["anomalies"]

    fund = ctx.get("fundamentals") or {}
    news = ctx.get("news") or []

    fund_block = "\n".join(
        f"  {label:<16}: {fund.get(key)}"
        for label, key in [
            ("Trailing P/E", "trailing_pe"), ("Forward P/E", "forward_pe"),
            ("PEG", "peg_ratio"), ("P/S", "price_to_sales"), ("P/B", "price_to_book"),
            ("Div Yield", "dividend_yield"), ("Profit Margin", "profit_margin"),
            ("Rev Growth YoY", "revenue_growth"), ("Beta", "beta"),
            ("52w Range", "fifty_two_wk_low"), ("Tgt Mean Price", "target_mean_price"),
        ] if fund.get(key) is not None
    ) or "  None available (ETF or no snapshot yet)"

    news_block = "\n".join(
        f"  [{a['published']}] ({a['source']}) {a['title']}" for a in news
    ) or "  No recent headlines available"

    perf_block = _perf_block(ctx)
    market_block = _market_block(ctx)
    earnings_block = _earnings_block(ctx)
    options_block = _options_block(ctx)
    smart_block = _smart_block(ctx)
    prior_block = _prior_block(ctx)

    return f"""Analyse {ctx['ticker']} as of {ctx['as_of_date']}.

COMPANY
  Name     : {meta.get('company_name', 'N/A')}
  Sector   : {meta.get('sector', 'N/A')}
  Industry : {meta.get('industry', 'N/A')}
  Mkt Cap  : {meta.get('market_cap', 'N/A')}

TECHNICAL INDICATORS
  Latest Close : ${ind.get('latest_close')}
  Prev Close   : ${ind.get('prev_close')}
  Day Change   : {ind.get('day_change_pct'):+.2f}%
  5-Day Return : {ind.get('ret_5d_pct', 'N/A')}%
  20-Day Return: {ind.get('ret_20d_pct', 'N/A')}%
  MA(5)        : {ind.get('ma_5', 'N/A')}
  MA(20)       : {ind.get('ma_20', 'N/A')}
  Price vs MA5 : {ind.get('price_vs_ma5_pct', 'N/A')}%
  Price vs MA20: {ind.get('price_vs_ma20_pct', 'N/A')}%
  RSI(14)      : {ind.get('rsi_14', 'N/A')}
  Volatility   : {ind.get('volatility_ann_pct', 'N/A')}% (annualised)
  Volume Today : {ind.get('latest_volume', 'N/A'):,}
  Avg Vol 20d  : {ind.get('avg_volume_20d', 'N/A'):,}
  Volume Ratio : {ind.get('volume_ratio', 'N/A')}x

PRE-DETECTED ANOMALIES
{json.dumps(anom, indent=2) if anom else "None detected"}

VALUATION (latest snapshot {fund.get('snapshot_date', 'n/a')})
{fund_block}

RECENT NEWS HEADLINES (last 7 days)
{news_block}

MARKET CONTEXT (top-down backdrop, same for all tickers)
{market_block}

EARNINGS CONTEXT (binary event risk — factor into conviction)
{earnings_block}

OPTIONS FLOW (positioning — corroborate or challenge the technicals)
{options_block}

SMART MONEY (famous investors — 13F is quarter-lagged; Form 4 / 13D/G are recent)
{smart_block}

PRIOR DECISION (your own last call on this ticker)
{prior_block}

BACKTEST FEEDBACK (how prior signals actually played out)
{perf_block}

RECENT PRICE TABLE (last 10 trading days)
{ctx['price_table']}

Produce your JSON report now.
"""


SWING_SYSTEM_PROMPT = f"""You are a swing trader at Orblex Industries.
You plan LONG-ONLY trades held {MIN_HOLD_DAYS}–{MAX_HOLD_DAYS} trading days. The account
cannot day trade: a position bought today can be sold at the earliest on the next
session, so every entry must be worth holding overnight through an opening gap.

You will receive, per ticker, as of the latest daily close:
- Swing technicals: trend (close vs MA10/20/50), ATR(14), RSI(14) and its 3-day
  change, the prior 20-day high/low and distance to them in ATRs, Bollinger %B,
  today's gap and close location in the bar, up/down-close streak, volume ratio,
  and relative strength vs SPY over 5 and 20 days
- A 10-day OHLCV table
- Headlines from the last 3 days (when available)
- Market regime (SPY/QQQ/VIX, rule-based)
- Earnings date — Python will block any BUY whose holding window reaches the report
- Options positioning for the nearest expiry (put/call, IV rank, flow signal)
- Fresh insider Form 4 filings (last 14 days), if any
- Your prior call, and if it was a BUY, the plan you set (entry/stop/target)
- Backtest feedback on your past calls

Signals:
- BUY  : a defined setup with a clear invalidation level. entry_price is a LIMIT
         order good for {ENTRY_VALID_SESSIONS} sessions: at or near the close to buy now, or
         lower (a pullback level, up to 2 ATR below the close) to buy the dip
         you would otherwise "wait for". If it never fills, nothing is bought.
- HOLD : no new entry. If a position from a prior BUY plan is open, keep it — its
         stop and target still apply.
- SELL : the setup has broken (e.g. closed below the prior plan's stop, failed
         breakout, trend turned DOWN on volume). Exit any open position. This is
         never a short.

Your output MUST be a single valid JSON object with exactly these fields:

{{
  "signal": "BUY" | "SELL" | "HOLD",
  "signal_strength": "STRONG" | "MODERATE" | "WEAK",
  "setup_type": "BREAKOUT" | "PULLBACK" | "MOMENTUM" | "REVERSAL" | "BREAKDOWN" | "NONE",
  "entry_price": <float limit price anywhere from 2 ATR below to 0.5 ATR above the close, or null unless BUY>,
  "stop_price": <float, or null unless BUY>,
  "target_price": <float, or null unless BUY>,
  "horizon_days": <int {MIN_HOLD_DAYS}-{MAX_HOLD_DAYS}, or null unless BUY>,
  "invalidation": "<one sentence: what price action would prove the call wrong>",
  "summary": "<2–3 sentences on the short-term price action and setup>",
  "anomalies": [
    {{
      "type": "<anomaly type>",
      "detail": "<what was detected>",
      "severity": "high" | "medium" | "low",
      "interpretation": "<what this likely means for the next few sessions>"
    }}
  ],
  "reasoning": "<3–5 sentences: the setup, why now, and why the stop/target sit where they do>",
  "sentiment_score": <float between -1.0 and 1.0>
}}

Rules:
- Output ONLY the JSON. No preamble, no markdown fences.
- Judge the next {MAX_HOLD_DAYS} sessions only. Valuation, long-term quality and
  famous-investor holdings are deliberately not provided — do not reason about them.
- Levels: never chase more than half an ATR above the close. Put stop_price below
  a real level (recent swing low, MA20, breakout level), normally 1–2.5 ATR below
  entry. target_price sits at the next resistance (prior 20-day high, prior swing
  high); when price is already at or above the prior 20-day high there is no
  nearby overhead resistance, so use a measured target of 2–3 ATR above entry.
  Reward:risk is measured from entry_price and must be at least {MIN_REWARD_RISK}.
  Python validates every level and downgrades bad plans.
- "Wait for a pullback" is a plan, not a HOLD: if the trend and relative
  strength are good but the stock is extended, answer BUY with setup_type
  PULLBACK and entry_price at the level you would buy (breakout retest, MA10,
  prior shelf). If your reasoning names an entry zone, a stop and a target that
  clear {MIN_REWARD_RISK}R, that IS a BUY at that entry — do not describe a plan
  and then answer HOLD. Answer HOLD only when you would not want to own it at
  any nearby price, or there is no clean level for a stop.
- Prefer trades in the direction of the trend. A BUY against a DOWN trend needs a
  clear reversal (reclaim of MA20 on volume, bullish close location after a
  flush) and at most MODERATE strength.
- Avoid chasing extension: RSI(14) > 75, %B > 1.0, or price > 3 ATR above MA20
  argues for a PULLBACK entry below the close, not a BUY at the close.
- Overnight gaps: a large gap today or high IV rank means wider stops and smaller
  conviction. Strong relative strength vs SPY supports a BUY; weak RS argues
  against.
- EARNINGS: never plan to hold through a report. If the report lands inside your
  horizon, shorten horizon_days to exit the session before it, or answer HOLD.
- PRIOR PLAN: if your last call was a BUY with levels, manage it — HOLD while
  price is between the stop and target, SELL if the last close is below the stop
  or the setup is clearly invalidated. Do not abandon a plan on one noisy bar, and
  do not keep a broken one out of consistency. A prior HOLD or SELL carries no
  weight: judge today's setup on its own. Prior calls may come from the older
  long-horizon analyst and have no levels.
- MARKET REGIME: in CORRECTION or BEAR, new BUYs need STRONG relative strength and
  are at most MODERATE strength.
- OPTIONS FLOW and insider buys tilt conviction; they never create a trade alone.
- News in the last 3 days can trigger or veto a setup; older themes are priced in.
- BACKTEST FEEDBACK is your simulated swing trades in R (risk units). Use it as
  calibration only: negative expectancy on this ticker or setup type means demand
  a cleaner setup and lower strength, not invert the signal.
- This is for simulation purposes only — not real financial advice.
"""


def _fmt(v, suffix=""):
    return "N/A" if v is None else f"{v}{suffix}"


def _vs_level(dist_atr, pos_word: str, neg_word: str) -> str:
    """'close 0.4 ATR below' / 'close 0.2 ATR above' from a signed ATR distance."""
    if dist_atr is None:
        return "N/A"
    return f"close {abs(dist_atr)} ATR {pos_word if dist_atr >= 0 else neg_word}"


def _build_swing_prompt(ctx: dict) -> str:
    sw = ctx.get("swing") or {}
    news = ctx.get("news") or []
    news_block = "\n".join(
        f"  [{a['published']}] ({a['source']}) {a['title']}" for a in news
    ) or "  No headlines in the last 3 days"

    plan = ctx.get("prior_plan")
    plan_block = (
        f"  Last BUY plan {plan['report_date']} ({plan.get('setup_type') or 'n/a'}): "
        f"entry {_fmt(plan.get('entry_price'))}, stop {_fmt(plan.get('stop_price'))}, "
        f"target {_fmt(plan.get('target_price'))}, horizon {_fmt(plan.get('horizon_days'), 'd')}"
        if plan else "  No prior swing BUY plan on record."
    )

    return f"""Plan a swing trade for {ctx['ticker']} as of the {sw.get('latest_date', 'latest')} close.

COMPANY
  Name     : {ctx['metadata'].get('company_name', 'N/A')}
  Sector   : {ctx['metadata'].get('sector', 'N/A')}

SWING TECHNICALS
  Close        : {_fmt(sw.get('latest_close'))}   (day {_fmt(sw.get('day_change_pct'), '%')}, gap at open {_fmt(sw.get('gap_pct'), '%')})
  Returns      : 5d {_fmt(sw.get('ret_5d_pct'), '%')}, 10d {_fmt(sw.get('ret_10d_pct'), '%')}, 20d {_fmt(sw.get('ret_20d_pct'), '%')}
  RS vs SPY    : 5d {_fmt(sw.get('rs_vs_spy_5d_pct'), ' pts')}, 20d {_fmt(sw.get('rs_vs_spy_20d_pct'), ' pts')}
  Trend        : {_fmt(sw.get('trend'))}   MA10 {_fmt(sw.get('ma_10'))}, MA20 {_fmt(sw.get('ma_20'))} (slope 5d {_fmt(sw.get('ma20_slope_5d_pct'), '%')}), MA50 {_fmt(sw.get('ma_50'))}
  vs MAs       : {_fmt(sw.get('price_vs_ma20_pct'), '%')} vs MA20, {_fmt(sw.get('price_vs_ma50_pct'), '%')} vs MA50
  ATR(14)      : {_fmt(sw.get('atr_14'))} ({_fmt(sw.get('atr_pct'), '%')} of price)
  RSI(14)      : {_fmt(sw.get('rsi_14'))} (3 sessions ago {_fmt(sw.get('rsi_14_3d_ago'))})
  Prior 20d    : high {_fmt(sw.get('high_20d'))} ({_vs_level(sw.get('dist_high_20d_atr'), 'below', 'above')}), low {_fmt(sw.get('low_20d'))} ({_vs_level(sw.get('dist_low_20d_atr'), 'above', 'below')})
  Bollinger %B : {_fmt(sw.get('bollinger_pct_b'))}   Close location in bar: {_fmt(sw.get('close_location'))} (0 = low, 1 = high)
  Streak       : {_fmt(sw.get('streak_days'))} session(s) (+ up closes, − down closes)
  Volume       : {_fmt(sw.get('volume_ratio'), 'x')} the 20-day average

PRE-DETECTED ANOMALIES
{json.dumps(ctx['anomalies'], indent=2) if ctx['anomalies'] else "None detected"}

RECENT NEWS (last 3 days, newest first)
{news_block}

MARKET REGIME
{_market_block(ctx, include_macro=False)}

EARNINGS
{_earnings_block(ctx)}

OPTIONS FLOW (nearest expiry)
{_options_block(ctx)}

INSIDER FORM 4 (last 14 days)
{_smart_block(ctx) if ctx.get('smart_money') else "  None."}

PRIOR DECISION
{_prior_block(ctx)}
{plan_block}

BACKTEST FEEDBACK
{_perf_block(ctx)}

RECENT PRICE TABLE (last 10 trading days)
{ctx['price_table']}

Produce your JSON trade plan now.
"""


SETUP_TYPES = {"BREAKOUT", "PULLBACK", "MOMENTUM", "REVERSAL", "BREAKDOWN", "NONE"}


def _parse_response(text: str) -> Optional[dict]:
    """Extract and validate the JSON from the model's response."""
    text = text.strip()

    # Strip markdown fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"  JSON parse error: {e}\n  Raw response: {text[:300]}")
        return None

    required = {"signal", "signal_strength", "summary", "anomalies", "reasoning"}
    missing  = required - data.keys()
    if missing:
        logger.error(f"  Missing fields in agent response: {missing}")
        return None

    if data["signal"] not in ("BUY", "SELL", "HOLD"):
        logger.error(f"  Invalid signal value: {data['signal']}")
        return None

    try:
        s = float(data.get("sentiment_score", 0.0))
    except (TypeError, ValueError):
        s = 0.0
    data["sentiment_score"] = max(-1.0, min(1.0, round(s, 3)))

    return data


def analyse(ctx: dict, api_key: Optional[str] = None, raise_errors: bool = False) -> Optional[dict]:
    """
    Call Claude with the stock context and return structured analysis.

    Returns a dict with parsed agent output + token usage, or None on failure.
    raise_errors=True re-raises API errors instead (the playground reports them to the visitor).
    """
    client = anthropic.Anthropic(api_key=api_key)  # uses ANTHROPIC_API_KEY env var if None

    swing = ctx.get("trading_mode") == "swing"
    prompt = _build_swing_prompt(ctx) if swing else _build_prompt(ctx)
    system = SWING_SYSTEM_PROMPT if swing else SYSTEM_PROMPT
    logger.info(f"  Calling Claude ({decision_model()}, {'swing' if swing else 'position'}) for {ctx['ticker']}...")

    has_news = bool(ctx.get("news") or [])
    logger.info(f"  News context: {'present' if has_news else 'absent'}")

    try:
        msg = ask("decision", system, prompt, 4096 if swing else 2048, client)

        raw_text = msg.text
        parsed   = _parse_response(raw_text)

        if not parsed:
            return None

        if swing:
            # Swing plans carry no valuation view; Python then validates the
            # model's levels, horizon and earnings timing (swing_policy).
            parsed["valuation_view"] = "N/A"
            if parsed.get("setup_type") not in SETUP_TYPES:
                parsed["setup_type"] = "NONE"
            enforce_swing_rules(parsed, ctx.get("swing") or {}, ctx.get("earnings") or {})

        return {
            **parsed,
            "model":         msg.model,
            "prompt_tokens": msg.input_tokens,
            "output_tokens": msg.output_tokens,
            "news_used":     has_news,   # audit flag — stored alongside report
        }

    except Exception as e:
        if raise_errors:
            raise
        logger.error(f"  Anthropic API error for {ctx['ticker']}: {e}")
        return None