"""
swing_screen_backtest.py — replays swing_screen.py day by day on history to decide,
with evidence, which v2 rules to keep (EXECUTOR_DESIGN.md §3, §4, phase 1).

For every session d from START: rebuild the regime from SPY/QQQ/VIX closes up to d
(regime_detector._classify), compute each ticker's indicators from bars up to d
(swing_policy.compute_swing_indicators, same as live), screen, and plan for the
next session. Trades are simulated with swing_backtest.simulate_trade (limit
entry good for 2 sessions, no exit on the fill day, stop-before-target on a bar
that touches both, 5 bps per side).

Two views per setup:
  signals    every signal, one trade per ticker at a time: the rule's raw edge
  portfolio  the account limits on top (≤ 2 entries/day, ≤ MAX_OPEN_POSITIONS open incl. resting
             entries, best-ranked first): what the account would have traded

Caveats, printed with the results: today's universe/candidates lists are
applied to the past (selection bias), there is no earnings blackout (no
historical calendar), and daily bars cannot see intraday order.

Usage:
  python swing_screen_backtest.py                       # longs (each filter) + inverse, 2022-01-03 → today
  python swing_screen_backtest.py --start 2026-01-02    # same window as the +0.21R baseline
  python swing_screen_backtest.py --only inverse
  python swing_screen_backtest.py --only longs --limits   # BULL only: open-position caps × fill-day stops
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from exec_limits import MAX_NEW_ENTRIES_DAY, MAX_OPEN_POSITIONS
from regime_detector import _classify, _compute_benchmark_stats
from swing_backtest import CLOSED_OUTCOMES, simulate_trade
from swing_policy import compute_swing_indicators
from swing_screen import FILTERS, INVERSE_ETFS, RANKINGS, rank, screen_inverse, screen_long

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("screen-bt")

WINDOW         = 260        # bars handed to the indicators (≥ 252 for the regime's 52-week high)
IND_BARS       = 120        # bars for swing indicators (matches SWING_LOOKBACK_DAYS)
MAX_ENTRIES    = MAX_NEW_ENTRIES_DAY
MAX_OPEN       = MAX_OPEN_POSITIONS
OUT_DIR       = Path(".trading/backtests")


def _tickers(path: str) -> list[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        tok = line.split("#")[0].split()
        if tok:
            out.append(tok[0].upper())
    return out


def load_bars(tickers: list[str], start: date) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    fetch_from = start - timedelta(days=420)       # warm-up for MA200 / 52-week high
    raw = yf.download(sorted(set(tickers)), start=fetch_from.isoformat(), auto_adjust=True,
                      progress=False, group_by="ticker", threads=True)
    out = {}
    for t in set(tickers):
        try:
            df = raw[t].dropna(subset=["Close"]).reset_index()
        except KeyError:
            continue
        if df.empty:
            continue
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        out[t] = df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return out


def regime_on(bars: dict, d: date) -> str | None:
    spy, qqq, vix = bars.get("SPY"), bars.get("QQQ"), bars.get("^VIX")
    s = spy[spy["date"] <= d].tail(WINDOW)
    if len(s) < 200:
        return None
    q = qqq[qqq["date"] <= d].tail(WINDOW) if qqq is not None else None
    v = vix[vix["date"] <= d] if vix is not None else None
    regime, _ = _classify(_compute_benchmark_stats(s.rename(columns={"close": "Close"})),
                          _compute_benchmark_stats(q.rename(columns={"close": "Close"})) if q is not None and len(q) >= 200 else None,
                          float(v["close"].iloc[-1]) if v is not None and len(v) else None)
    return regime


def ticker_indicators(job: tuple) -> tuple[str, dict]:
    """(ticker, {session: indicators}) for every session ≥ start. Top-level so a
    process pool can run one ticker per worker; windows are positional slices."""
    t, df, spy, start = job
    spy_pos = {d: i for i, d in enumerate(spy["date"])}
    out = {}
    for i, d in enumerate(df["date"]):
        if d < start or i + 1 < 60 or d not in spy_pos:
            continue
        w = df.iloc[max(0, i + 1 - IND_BARS): i + 1]
        j = spy_pos[d]
        out[d] = compute_swing_indicators(w, spy.iloc[max(0, j + 1 - IND_BARS): j + 1])
    return t, out


def precompute(bars: dict, start: date, tickers: list[str]) -> dict:
    """{ticker: {session: indicators}}, computed once and shared by every filter variant."""
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(t, bars[t], bars["SPY"], start) for t in tickers if t in bars]
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
        return dict(pool.map(ticker_indicators, jobs, chunksize=1))


def generate_signals(bars: dict, start: date, longs: list[str], inverse: list[str],
                     filters: tuple[str, ...], ind_cache: dict | None = None,
                     regimes: dict | None = None, rank_by: str = "rs",
                     long_regimes: set | None = None) -> tuple[list[dict], dict]:
    spy = bars["SPY"]
    sessions = [d for d in spy["date"] if d >= start]
    next_session = {sessions[i]: sessions[i + 1] for i in range(len(sessions) - 1)}
    ind_cache = ind_cache or {}
    regimes = dict(regimes or {})
    signals = []
    for d in sessions[:-1]:
        if d not in regimes:
            regimes[d] = regime_on(bars, d)
        regime = regimes[d]
        day = []
        for t in longs:
            ind = ind_cache.get(t, {}).get(d)
            if not ind:
                continue
            if long_regimes is not None and regime not in long_regimes:
                continue
            plan, _ = screen_long(t, ind, regime, filters)
            if plan:
                day.append(plan)
        if inverse:
            spy_w = spy[spy["date"] <= d].tail(IND_BARS)
        for t in inverse:
            df, idx = bars.get(t), bars.get(INVERSE_ETFS[t][0])
            if df is None or idx is None:
                continue
            w = df[df["date"] <= d].tail(IND_BARS)
            iw = idx[idx["date"] <= d].tail(IND_BARS)
            if len(w) < 60 or w["date"].iloc[-1] != d:
                continue
            plan, _ = screen_inverse(t, compute_swing_indicators(w, spy_w), regime, iw)
            if plan:
                day.append(plan)
        for rank_i, p in enumerate(rank(day, rank_by)):
            p.update(signal_date=d, report_date=next_session[d], regime=regime, day_rank=rank_i)
            signals.append(p)
    return signals, regimes


def _simulate(p: dict, bars: dict, fill_day_stop: bool = False) -> dict:
    """simulate_trade on this ticker's bars, merged onto the plan."""
    return {**p, **simulate_trade(p, bars[p["ticker"]], fill_day_stop=fill_day_stop)}


def _busy_until(t: dict, bars: dict) -> date:
    """Last session the trade (or its resting entry) occupies a slot."""
    if t.get("exit_date"):
        return t["exit_date"]
    df = bars[t["ticker"]]
    after = df[df["date"] >= t["report_date"]]["date"].tolist()
    if t["outcome"] in ("NO_FILL", "CANCELLED_GAP") or not t.get("fill_date"):
        return after[min(1, len(after) - 1)] if after else t["report_date"]
    return after[-1] if after else t["report_date"]   # still open at the end of the data


def run_signals(signals: list[dict], bars: dict) -> list[dict]:
    """Every signal, one trade per ticker at a time (no portfolio limits)."""
    busy, out = {}, []
    for p in sorted(signals, key=lambda s: (s["report_date"], s["day_rank"])):
        if busy.get(p["ticker"]) and busy[p["ticker"]] >= p["report_date"]:
            continue
        t = _simulate(p, bars)
        busy[p["ticker"]] = _busy_until(t, bars)
        out.append(t)
    return out


def run_portfolio(signals: list[dict], bars: dict, max_open: int = MAX_OPEN,
                  fill_day_stop: bool = False) -> list[dict]:
    """The account limits: ≤ MAX_ENTRIES per day and ≤ max_open slots, best rank first."""
    by_day: dict = {}
    for p in signals:
        by_day.setdefault(p["report_date"], []).append(p)
    open_until: dict = {}     # ticker → last busy session
    out = []
    for d in sorted(by_day):
        open_until = {k: v for k, v in open_until.items() if v >= d}
        taken = 0
        for p in sorted(by_day[d], key=lambda s: s["day_rank"]):
            if taken >= MAX_ENTRIES or len(open_until) >= max_open or p["ticker"] in open_until:
                continue
            t = _simulate(p, bars, fill_day_stop)
            open_until[p["ticker"]] = _busy_until(t, bars)
            out.append(t)
            taken += 1
    return out


def stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["outcome"] in CLOSED_OUTCOMES]
    rs = [float(t["r_multiple"]) for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    attempted = [t for t in trades if t["outcome"] not in ("PENDING",)]
    return {
        "signals": len(trades), "closed": len(closed),
        "fill_rate": round(sum(1 for t in attempted if t.get("fill_date")) / len(attempted), 2) if attempted else None,
        "win_rate": round(len(wins) / len(rs), 2) if rs else None,
        "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
        "total_R": round(sum(rs), 1) if rs else 0.0,
        "PF": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
    }


def _row(label, s):
    f = lambda v, fmt: "—" if v is None else format(v, fmt)
    return (f"  {label:<34} n={s['closed']:>4}  fill {f(s['fill_rate'], '.0%'):>4}  win {f(s['win_rate'], '.0%'):>4}"
            f"  avg {f(s['avg_R'], '+.3f'):>7}R  total {s['total_R']:>+7.1f}R  PF {f(s['PF'], '.2f'):>5}")


def report(title: str, trades: list[dict], by: str | None = None) -> None:
    print(f"\n{title}")
    print(_row("all", stats(trades)))
    if by:
        keys = sorted({str(t.get(by)) for t in trades})
        for k in keys:
            print(_row(f"{by}={k}", stats([t for t in trades if str(t.get(by)) == k])))


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay the v2 swing screen on history")
    ap.add_argument("--start", default="2022-01-03")
    ap.add_argument("--only", choices=["longs", "inverse"])
    ap.add_argument("--ranking", action="store_true",
                    help="longs: compare rankings (rs, pullback) × regime gates (all, BULL only), no filters")
    ap.add_argument("--limits", action="store_true",
                    help="longs, BULL only: compare 2, 3 and 5 open positions, with and without fill-day stops")
    ap.add_argument("--filters", nargs="*", default=None,
                    help=f"long filters to test (default: none, each alone, and all). Options: {', '.join(FILTERS)}")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)

    longs = [] if args.only == "inverse" else sorted(set(_tickers("universe.txt") + _tickers("candidates.txt")))
    inverse = [] if args.only == "longs" else list(INVERSE_ETFS)
    log.info(f"Loading bars for {len(longs)} long tickers + {len(inverse)} inverse ETFs from {start} …")
    bars = load_bars(longs + inverse + ["SPY", "QQQ", "^VIX"], start)
    missing = [t for t in longs + inverse if t not in bars]
    if missing:
        log.info(f"  no data: {', '.join(missing)}")
    longs = [t for t in longs if t in bars]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nReplay {start} → {bars['SPY']['date'].iloc[-1]} · costs 5 bps/side · "
          f"limits {MAX_ENTRIES}/day, {MAX_OPEN} open")
    print("Caveats: today's ticker lists applied to the past (selection bias); no earnings blackout; daily bars only.")

    if longs:
        variants = [()] if args.filters is None else []
        if args.filters is None:
            variants += [(f,) for f in FILTERS] + [tuple(FILTERS)]
        else:
            variants.append(tuple(args.filters))
        log.info(f"Computing indicators for {len(longs)} tickers (parallel) …")
        cache = precompute(bars, start, longs)
        regimes = None
        if args.limits:
            sig, regimes = generate_signals(bars, start, longs, [], (), cache, regimes, "rs", {"BULL"})
            print("\nLONGS · rank rs · BULL only · portfolio view")
            for max_open in (2, 3, 5):
                for fill_day in (False, True):
                    label = f"{max_open} open, fill-day stops {'on' if fill_day else 'off'}"
                    print(_row(label, stats(run_portfolio(sig, bars, max_open, fill_day))))
            variants = []
        if args.ranking:
            for by in RANKINGS:
                for gate_name, gate in (("all regimes", None), ("BULL only", {"BULL"})):
                    sig, regimes = generate_signals(bars, start, longs, [], (), cache, regimes, by, gate)
                    port = run_portfolio(sig, bars)
                    report(f"LONGS · rank {by} · {gate_name} · portfolio view", port, by="regime")
                    pd.DataFrame(port).to_csv(OUT_DIR / f"longs_rank-{by}_{gate_name.split()[0]}_{start}.csv", index=False)
            variants = []
        for v in variants:
            name = "+".join(v) or "baseline"
            sig, regimes = generate_signals(bars, start, longs, [], v, cache, regimes)
            report(f"LONGS · {name} · signals view", run_signals(sig, bars), by="regime")
            port = run_portfolio(sig, bars)
            report(f"LONGS · {name} · portfolio view", port)
            pd.DataFrame(port).to_csv(OUT_DIR / f"longs_{name}_{start}.csv", index=False)

    if inverse:
        sig, regimes = generate_signals(bars, start, [], inverse, ())
        trades = run_signals(sig, bars)
        report("INVERSE · signals view, by setup", trades, by="setup_type")
        for setup in ("BEAR_TREND", "DOWN_DAY"):
            report(f"INVERSE · {setup} · by ticker", [t for t in trades if t["setup_type"] == setup], by="ticker")
        report("INVERSE · portfolio view", run_portfolio(sig, bars), by="setup_type")
        pd.DataFrame(trades).to_csv(OUT_DIR / f"inverse_{start}.csv", index=False)
        days = pd.Series(regimes).value_counts()
        print("\nRegime days in window: " + ", ".join(f"{k} {v}" for k, v in days.items()))
    print(f"\nTrade lists saved under {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
