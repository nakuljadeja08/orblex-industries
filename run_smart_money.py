"""
run_smart_money.py — Orblex Industries Smart Money Tracker
─────────────────────────────────────────────────────────
Tracks what famous US investors hold and what they have recently traded,
straight from SEC EDGAR (free, no API key):

  13F-HR          Each fund's full long book, quarterly, filed up to 45 days
                  after quarter end. Diffed against the prior quarter to label
                  every position NEW / ADDED / REDUCED / UNCHANGED / SOLD.
  Form 4          Trades by >10% owners, filed within 2 business days — the
                  freshest signal EDGAR has (e.g. Berkshire adding to a stake).
  Schedule 13D/G  New or changed >5% stakes; 13D signals activist intent.
  House PTRs      Members of Congress' trades (STOCK Act), up to 45 days after
                  the trade, amounts as ranges — see congress_ptr.py.

13F filings only list CUSIPs, so CUSIPs are mapped to tickers via OpenFIGI
(free; OPENFIGI_API_KEY optional, raises the rate limit) and cached in
cusip_ticker_map so each CUSIP is looked up once.

No LLM calls. The analyst reads the result as context via context_builder.

Tables: smart_money_schema.sql

Usage:
  python run_smart_money.py
  python run_smart_money.py --investors 1067983 1697748
  python run_smart_money.py --investors -1            # Pelosi only (members use negative ids)
  python run_smart_money.py --force        # re-pull 13Fs even if unchanged
  python run_smart_money.py --dry-run
"""

import os, sys, re, json, time, argparse, uuid, datetime, logging
import xml.etree.ElementTree as ET

import requests
from supabase import create_client
from dotenv import load_dotenv

import congress_ptr

load_dotenv()

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("smart_money")
logging.getLogger("httpx").setLevel(logging.WARNING)  # supabase-py logs every request at INFO

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# SEC fair-access policy asks every client to identify itself, ideally with a
# contact address: "Company Name admin@company.com". Set SEC_USER_AGENT.
SEC_USER_AGENT  = os.environ.get("SEC_USER_AGENT", "").strip() or "OrblexIndustries-Portfolio research-bot"
OPENFIGI_KEY    = os.environ.get("OPENFIGI_API_KEY", "").strip()

# ── Tracked investors ─────────────────────────────────────────────────────────
# CIKs verified against EDGAR on 2026-10-02 (all filing current 13Fs).
# `cik` is the current 13F filer and the investor's key; `aliases` are older or
# sibling filer entities whose filings are merged in (Pershing moved its 13F to
# Pershing Square Inc. in 2026, but still files Form 4s as the old LP).
# Deliberately left out:
#   Michael Burry / Scion (1649339) — deregistered; last 13F Nov 2025.
#   Bridgewater (1350694) — ~1,000 mostly index-like positions; noise, not conviction.
INVESTORS = [
    {"cik": 1067983, "manager": "Warren Buffett",       "fund": "Berkshire Hathaway",        "style": "Value"},
    {"cik": 2026053, "manager": "Bill Ackman",          "fund": "Pershing Square",           "style": "Activist", "aliases": [1336528]},
    {"cik": 1536411, "manager": "Stanley Druckenmiller", "fund": "Duquesne Family Office",   "style": "Macro"},
    {"cik": 1656456, "manager": "David Tepper",         "fund": "Appaloosa",                 "style": "Value / Distressed"},
    {"cik":  921669, "manager": "Carl Icahn",           "fund": "Icahn Capital",             "style": "Activist"},
    {"cik": 1697748, "manager": "Cathie Wood",          "fund": "ARK Invest",                "style": "Growth / Innovation"},
    {"cik": 1040273, "manager": "Dan Loeb",             "fund": "Third Point",               "style": "Activist"},
    {"cik": 1061768, "manager": "Seth Klarman",         "fund": "Baupost Group",             "style": "Value"},
    {"cik": 1029160, "manager": "George Soros",         "fund": "Soros Fund Management",     "style": "Macro"},
    {"cik": 1167483, "manager": "Chase Coleman",        "fund": "Tiger Global",              "style": "Growth / Tech"},
    {"cik": 1489933, "manager": "David Einhorn",        "fund": "Greenlight (DME Capital)",  "style": "Value / Short-biased"},
]

FORMS_13F      = {"13F-HR", "13F-HR/A"}
ACTIVITY_FORMS = {"4", "4/A",
                  "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
                  "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
ACTIVITY_LOOKBACK_DAYS = 120

# 13F <value> moved from thousands to whole dollars on 2023-01-03, but some
# filers (Duquesne, Baupost) still report thousands. A median implied price per
# share below this means the filing is in thousands.
THOUSANDS_PRICE_THRESHOLD = 0.5

# Below this |change| a position counts as UNCHANGED — absorbs share-count
# rounding between sub-manager line items.
CHANGE_EPSILON_PCT = 0.5

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


def get_run_id():
    return f"smartmoney-{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:6]}"


# ── EDGAR client ──────────────────────────────────────────────────────────────

class Edgar:
    """Throttled EDGAR session. SEC allows 10 req/s; we stay well under."""
    MIN_INTERVAL = 0.15

    def __init__(self, user_agent: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0

    def get(self, url: str, as_json: bool = False):
        for attempt in range(4):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            r = self.s.get(url, timeout=30)
            self._last = time.monotonic()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * 2 ** attempt)
                continue
            r.raise_for_status()
            return r.json() if as_json else r.text
        r.raise_for_status()


def _strip_ns(root: ET.Element) -> ET.Element:
    """EDGAR XML uses per-schema default namespaces; drop them so paths stay simple."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _xml(text: str) -> ET.Element:
    return _strip_ns(ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text))


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def norm_ticker(t: str | None) -> str | None:
    """Match the universe's yfinance convention: share classes use a dash (BRK-B)."""
    if not t:
        return None
    return re.sub(r"[./ ]", "-", t.strip().upper()) or None


def acc_path(cik: int, accession: str) -> str:
    return f"{ARCHIVES}/{cik}/{accession.replace('-', '')}"


def filing_url(cik: int, accession: str) -> str:
    return f"{acc_path(cik, accession)}/{accession}-index.htm"


def inv_ciks(inv: dict) -> list[int]:
    return [inv["cik"], *inv.get("aliases", [])]


def recent_filings(edgar: Edgar, ciks: list[int]) -> list[dict]:
    """Recent filings across all of an investor's filer entities, tagged with the
    filer CIK (archive paths are per filer) and de-duplicated by accession."""
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    out, seen = [], set()
    for cik in ciks:
        r = edgar.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", as_json=True)["filings"]["recent"]
        for i in range(len(r["form"])):
            if r["accessionNumber"][i] not in seen:
                seen.add(r["accessionNumber"][i])
                out.append({"cik": cik, **{k: r[k][i] for k in keys}})
    return out


def load_cik_tickers(edgar: Edgar) -> dict[int, str]:
    """SEC's own CIK → ticker list, used to name the issuer of 13D/G filings."""
    data = edgar.get("https://www.sec.gov/files/company_tickers.json", as_json=True)
    out: dict[int, str] = {}
    for row in data.values():
        out.setdefault(int(row["cik_str"]), norm_ticker(row["ticker"]))
    return out


# ── 13F parsing ───────────────────────────────────────────────────────────────

def latest_13f_periods(filings: list[dict], n: int = 2) -> list[tuple[str, list[dict]]]:
    """The n most recent report periods that have an original 13F-HR, newest first."""
    by_period: dict[str, list[dict]] = {}
    for f in filings:
        if f["form"] in FORMS_13F and f["reportDate"]:
            by_period.setdefault(f["reportDate"], []).append(f)
    periods = [p for p in sorted(by_period, reverse=True)
               if any(f["form"] == "13F-HR" for f in by_period[p])]
    return [(p, by_period[p]) for p in periods[:n]]


def _amendment_type(edgar: Edgar, f: dict) -> str:
    xml = edgar.get(f"{acc_path(f['cik'], f['accessionNumber'])}/primary_doc.xml")
    root = _xml(xml)
    return (root.findtext(".//amendmentType") or "").strip().upper()


def _info_table(edgar: Edgar, f: dict) -> list[dict]:
    """Rows of the filing's information table (the XML that is not primary_doc)."""
    base = acc_path(f["cik"], f["accessionNumber"])
    idx = edgar.get(f"{base}/index.json", as_json=True)
    names = [i["name"] for i in idx["directory"]["item"]
             if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
    for name in names:
        root = _xml(edgar.get(f"{base}/{name}"))
        tables = root.findall(".//infoTable")
        if not tables:
            continue
        rows = []
        for t in tables:
            rows.append({
                "cusip":       (t.findtext("cusip") or "").strip().upper(),
                "issuer":      (t.findtext("nameOfIssuer") or "").strip(),
                "title_class": (t.findtext("titleOfClass") or "").strip(),
                "put_call":    (t.findtext("putCall") or "").strip().upper(),
                "shares":      _num(t.findtext("shrsOrPrnAmt/sshPrnamt")) or 0.0,
                "value_usd":   _num(t.findtext("value")) or 0.0,
            })
        prices = sorted(r["value_usd"] / r["shares"] for r in rows if r["shares"] > 0 and not r["put_call"])
        if prices and prices[len(prices) // 2] < THOUSANDS_PRICE_THRESHOLD:
            for r in rows:
                r["value_usd"] *= 1000
        return rows
    return []


def period_holdings(edgar: Edgar, filings: list[dict]) -> tuple[dict, dict]:
    """
    Aggregated holdings for one report period, keyed (cusip, put_call).
    Applies 13F-HR/A amendments: RESTATEMENT replaces the book, NEW HOLDINGS
    appends to it (how confidential positions, e.g. Berkshire's, surface late).
    When several of the investor's filer entities report the same period (Pershing's
    LP and Inc. both filed for Q1 2026), their books are summed.
    Returns (holdings, latest filing used).
    """
    by_date = lambda f: f["filingDate"]
    rows, used = [], None
    for cik in sorted({f["cik"] for f in filings if f["form"] == "13F-HR"}):
        mine = [f for f in filings if f["cik"] == cik]
        base = max((f for f in mine if f["form"] == "13F-HR"), key=by_date)
        cik_rows = _info_table(edgar, base)
        last = base
        for amd in sorted((f for f in mine if f["form"] == "13F-HR/A" and f["filingDate"] >= base["filingDate"]), key=by_date):
            kind = _amendment_type(edgar, amd)
            if kind == "RESTATEMENT":
                cik_rows = _info_table(edgar, amd)
            elif kind == "NEW HOLDINGS":
                cik_rows += _info_table(edgar, amd)
            else:
                continue
            last = amd
        rows += cik_rows
        if used is None or last["filingDate"] > used["filingDate"]:
            used = last

    agg: dict[tuple, dict] = {}
    for r in rows:
        if not r["cusip"]:
            continue
        key = (r["cusip"], r["put_call"])
        if key in agg:
            agg[key]["shares"]    += r["shares"]
            agg[key]["value_usd"] += r["value_usd"]
        else:
            agg[key] = dict(r)
    return agg, used


def diff_holdings(cur: dict, prev: dict | None) -> list[dict]:
    """Label each position vs the prior quarter; exited positions become SOLD rows."""
    total = sum(h["value_usd"] for h in cur.values()) or 1.0
    out = []
    for key, h in cur.items():
        p = (prev or {}).get(key)
        row = {**h, "prev_shares": p["shares"] if p else None,
               "pct_portfolio": round(h["value_usd"] / total * 100, 4),
               "share_change_pct": None, "change_type": None}
        if prev is not None:
            if not p:
                row["change_type"] = "NEW"
            elif p["shares"]:
                chg = (h["shares"] - p["shares"]) / p["shares"] * 100
                row["share_change_pct"] = round(chg, 2)
                row["change_type"] = ("ADDED" if chg > CHANGE_EPSILON_PCT else
                                      "REDUCED" if chg < -CHANGE_EPSILON_PCT else "UNCHANGED")
            else:
                row["change_type"] = "UNCHANGED"
        out.append(row)

    out.sort(key=lambda r: r["value_usd"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i

    for key, p in (prev or {}).items():
        if key not in cur:
            out.append({**p, "shares": 0.0, "value_usd": 0.0, "prev_shares": p["shares"],
                        "pct_portfolio": 0.0, "share_change_pct": -100.0,
                        "change_type": "SOLD", "rank": None})
    return out


# ── CUSIP → ticker ────────────────────────────────────────────────────────────

def map_cusips(cusips: set[str], sb) -> dict[str, str]:
    """Cached lookups first, OpenFIGI for the rest. Misses are not cached (bonds, foreign lines)."""
    mapping: dict[str, str] = {}
    todo = sorted(cusips)
    if sb is not None and todo:
        for i in range(0, len(todo), 200):
            resp = sb.table("cusip_ticker_map").select("cusip,ticker").in_("cusip", todo[i:i + 200]).execute()
            for r in resp.data or []:
                mapping[r["cusip"]] = r["ticker"]
    todo = [c for c in todo if c not in mapping]
    if not todo:
        return mapping

    # Keyless: 10 jobs/request, 25 requests/min. With a key: 100 jobs, 25 per 6s.
    batch, pause = (100, 0.3) if OPENFIGI_KEY else (10, 2.6)
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    log.info(f"  OpenFIGI: mapping {len(todo)} new CUSIP(s)...")

    fresh = []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        # Foreign-domiciled issuers (ASML, Aon, Chubb, Seagate) carry a CINS — a
        # CUSIP whose first character is a letter — which OpenFIGI only resolves as ID_CINS.
        jobs = [{"idType": "ID_CINS" if c[:1].isalpha() else "ID_CUSIP", "idValue": c, "exchCode": "US"}
                for c in chunk]
        for attempt in range(4):
            r = requests.post("https://api.openfigi.com/v3/mapping", headers=headers, json=jobs, timeout=30)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            log.warning(f"  OpenFIGI HTTP {r.status_code} — {len(chunk)} CUSIP(s) left unmapped")
            continue
        for cusip, res in zip(chunk, r.json()):
            data = res.get("data") or []
            if data and data[0].get("ticker"):
                t = norm_ticker(data[0]["ticker"])
                mapping[cusip] = t
                fresh.append({"cusip": cusip, "ticker": t, "name": data[0].get("name"), "source": "openfigi"})
        time.sleep(pause)

    if sb is not None and fresh:
        sb.table("cusip_ticker_map").upsert(fresh, on_conflict="cusip").execute()
    return mapping


# ── Form 4 / 13D / 13G ────────────────────────────────────────────────────────

def _fmt_sh(n: float) -> str:
    n = abs(n)
    return f"{n/1e6:.2f}M" if n >= 1e6 else f"{n/1e3:.1f}K" if n >= 1e3 else f"{n:,.0f}"


def parse_form4(edgar: Edgar, inv: dict, f: dict) -> dict | None:
    doc = os.path.basename(f["primaryDocument"])
    root = _xml(edgar.get(f"{acc_path(f['cik'], f['accessionNumber'])}/{doc}"))
    issuer_cik = int(_num(root.findtext("issuer/issuerCik")) or 0)
    if issuer_cik in inv_ciks(inv):
        return None  # an insider trading the fund's own stock (e.g. a BRK director)

    # One Form 4 can cover several share classes (Lennar Class A + Class B), each
    # with its own running position. Aggregate per class and report the class
    # with the most shares traded, so "holds" is that class's position.
    by_class: dict[str, dict] = {}
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        title = (t.findtext("securityTitle/value") or "").strip()
        c = by_class.setdefault(title, {"bought": 0.0, "sold": 0.0, "buy_val": 0.0, "sell_val": 0.0, "after": None})
        sh = _num(t.findtext("transactionAmounts/transactionShares/value")) or 0.0
        px = _num(t.findtext("transactionAmounts/transactionPricePerShare/value")) or 0.0
        ad = (t.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip()
        post = _num(t.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
        if post is not None:
            c["after"] = post
        if ad == "A":
            c["bought"] += sh; c["buy_val"] += sh * px
        elif ad == "D":
            c["sold"] += sh; c["sell_val"] += sh * px

    main = max(by_class.values(), key=lambda c: c["bought"] + c["sold"], default=None) or \
        {"bought": 0.0, "sold": 0.0, "buy_val": 0.0, "sell_val": 0.0, "after": None}
    bought, sold, buy_val, sell_val, after = (main[k] for k in ("bought", "sold", "buy_val", "sell_val", "after"))

    direction = "BUY" if bought > sold else "SELL" if sold > bought else "OTHER"
    if direction == "BUY":
        avg = buy_val / bought if bought else None
        summary = f"Bought {_fmt_sh(bought)} sh" + (f" @ ${avg:,.2f}" if avg else "")
    elif direction == "SELL":
        avg = sell_val / sold if sold else None
        summary = f"Sold {_fmt_sh(sold)} sh" + (f" @ ${avg:,.2f}" if avg else "")
    else:
        avg = None
        summary = "Derivative / non-trade ownership change"
    if after is not None and direction != "OTHER":
        summary += f" → holds {_fmt_sh(after)}"
    if len(by_class) > 1:
        summary += f" (+{len(by_class) - 1} other class)"

    # Multi-class issuers list every symbol: "LEN, LEN.B" → LEN
    symbol = (root.findtext("issuer/issuerTradingSymbol") or "").split(",")[0]
    return {
        "ticker":        norm_ticker(symbol),
        "issuer":        (root.findtext("issuer/issuerName") or "").strip(),
        "direction":     direction,
        "shares_bought": bought or None,
        "shares_sold":   sold or None,
        "avg_price":     round(avg, 4) if avg else None,
        "shares_after":  after,
        "pct_class":     None,
        "summary":       summary,
    }


def parse_13dg(edgar: Edgar, inv: dict, f: dict, cik_tickers: dict[int, str]) -> dict | None:
    base = acc_path(f["cik"], f["accessionNumber"])
    issuer_cik, issuer, pct = None, None, None

    if f["form"].startswith("SCHEDULE"):
        # Structured XML (EDGAR 13D/G modernization, Dec 2024 onward)
        root = _xml(edgar.get(f"{base}/primary_doc.xml"))
        issuer_cik = int(_num(root.findtext(".//issuerCik")) or 0) or None
        issuer = (root.findtext(".//issuerName") or "").strip() or None
        pct = _num(root.findtext(".//classPercent"))
    else:
        # Legacy SC 13D/G: free-text document; the SGML header names the subject.
        hdr = edgar.get(f"{base}/{f['accessionNumber']}-index-headers.html")
        m = re.search(r"<SUBJECT-COMPANY>.*?<CONFORMED-NAME>([^\n<]+).*?<CIK>(\d+)", hdr, re.S)
        if m:
            issuer, issuer_cik = m.group(1).strip(), int(m.group(2))

    if issuer_cik is None or issuer_cik in inv_ciks(inv):
        return None  # someone else's stake IN the fund, not the fund's stake

    kind = "13D (activist)" if "13D" in f["form"] else "13G (passive)"
    amended = f["form"].endswith("/A")
    summary = f"{'Updated' if amended else 'New'} {kind} stake" + (f": {pct:g}% of class" if pct is not None else "")
    return {
        "ticker":        cik_tickers.get(issuer_cik),
        "issuer":        issuer,
        "direction":     "STAKE",
        "shares_bought": None, "shares_sold": None, "avg_price": None, "shares_after": None,
        "pct_class":     pct,
        "summary":       summary,
    }


def fetch_activity(edgar: Edgar, inv: dict, filings: list[dict], known: set[str],
                   cik_tickers: dict[int, str], run_id: str) -> list[dict]:
    since = (datetime.date.today() - datetime.timedelta(days=ACTIVITY_LOOKBACK_DAYS)).isoformat()
    out = []
    for f in filings:
        if f["form"] not in ACTIVITY_FORMS or f["filingDate"] < since or f["accessionNumber"] in known:
            continue
        try:
            parsed = (parse_form4(edgar, inv, f) if f["form"].startswith("4")
                      else parse_13dg(edgar, inv, f, cik_tickers))
        except Exception as e:
            log.warning(f"    skip {f['form']} {f['accessionNumber']}: {e}")
            continue
        if not parsed:
            continue
        out.append({
            "run_id": run_id, "cik": inv["cik"], "manager": inv["manager"], "fund": inv["fund"],
            "form": f["form"], "filing_date": f["filingDate"], "accession": f["accessionNumber"],
            "url": filing_url(f["cik"], f["accessionNumber"]), **parsed,
        })
    return out


# ── Per-investor driver ───────────────────────────────────────────────────────

def process_investor(edgar, sb, inv, cik_tickers, run_id, force, dry_run) -> bool:
    cik = inv["cik"]
    log.info(f"\n[{inv['manager']} · {inv['fund']}] CIK {cik}")
    filings = recent_filings(edgar, inv_ciks(inv))

    # ── Recent moves (Form 4 / 13D / 13G) ──
    known: set[str] = set()
    if sb is not None:
        resp = sb.table("investor_filings").select("accession").eq("cik", cik).execute()
        known = {r["accession"] for r in resp.data or []}
    moves = fetch_activity(edgar, inv, filings, known, cik_tickers, run_id)
    for m in moves:
        log.info(f"  {m['filing_date']}  {m['form']:<15} {m['ticker'] or '?':<6} {m['summary']}")
    if moves and sb is not None:
        sb.table("investor_filings").upsert(moves, on_conflict="cik,accession").execute()
    if not moves:
        log.info(f"  No new Form 4 / 13D / 13G filings in the last {ACTIVITY_LOOKBACK_DAYS} days")

    # ── 13F holdings ──
    periods = latest_13f_periods(filings)
    if not periods:
        log.warning("  No 13F-HR on file — skipping holdings")
        return True
    latest_period, latest_filings = periods[0]
    latest_acc = max(latest_filings, key=lambda f: f["filingDate"])["accessionNumber"]

    if sb is not None and not force:
        cur = sb.table("smart_money_investors").select("accession").eq("cik", cik).execute()
        if cur.data and cur.data[0]["accession"] == latest_acc:
            log.info(f"  13F for {latest_period} unchanged ({latest_acc}) — holdings up to date")
            return True

    cur_h, used = period_holdings(edgar, latest_filings)
    prev_h, prev_period = None, None
    if len(periods) > 1:
        prev_period = periods[1][0]
        prev_h, _ = period_holdings(edgar, periods[1][1])

    rows = diff_holdings(cur_h, prev_h)
    tickers = map_cusips({r["cusip"] for r in rows}, sb)

    base = {"run_id": run_id, "cik": cik, "manager": inv["manager"], "fund": inv["fund"],
            "period_of_report": latest_period, "filing_date": used["filingDate"], "accession": latest_acc}
    records = [{**base, **{k: r.get(k) for k in (
                    "cusip", "issuer", "title_class", "put_call", "shares", "prev_shares",
                    "value_usd", "pct_portfolio", "share_change_pct", "change_type", "rank")},
                "ticker": tickers.get(r["cusip"])} for r in rows]

    held = [r for r in rows if r["change_type"] != "SOLD"]
    count = lambda t: sum(1 for r in rows if r["change_type"] == t)
    summary = {
        "cik": cik, "manager": inv["manager"], "fund": inv["fund"], "style": inv["style"],
        "period_of_report": latest_period, "prev_period": prev_period,
        "filing_date": used["filingDate"], "accession": latest_acc,
        "total_value_usd": round(sum(r["value_usd"] for r in held), 2),
        "positions": len(held),
        "new_count": count("NEW"), "added_count": count("ADDED"),
        "reduced_count": count("REDUCED"), "sold_count": count("SOLD"),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    top = ", ".join(f"{tickers.get(r['cusip']) or r['issuer'][:14]} {r['pct_portfolio']:.1f}%" for r in held[:5])
    log.info(f"  13F {latest_period} (filed {used['filingDate']}): {len(held)} positions, "
             f"${summary['total_value_usd']/1e9:,.2f}B · new {summary['new_count']} / "
             f"added {summary['added_count']} / reduced {summary['reduced_count']} / sold {summary['sold_count']}")
    log.info(f"  Top: {top}")

    if dry_run:
        log.info(f"  [DRY RUN] Would replace {len(records)} holding rows")
        return True

    # Replace the investor's book wholesale: the table always mirrors the latest 13F.
    sb.table("investor_holdings").delete().eq("cik", cik).execute()
    for i in range(0, len(records), 500):
        sb.table("investor_holdings").insert(records[i:i + 500]).execute()
    sb.table("smart_money_investors").upsert([summary], on_conflict="cik").execute()
    log.info(f"  Saved ✓ {len(records)} holding rows")
    return True


def process_congress(sb, members: list[dict], run_id: str, dry_run: bool) -> int:
    """House members' PTR trades → investor_filings. Returns the number of members that failed."""
    session = requests.Session()
    session.headers["User-Agent"] = SEC_USER_AGENT
    this_year = datetime.date.today().year
    index = congress_ptr.load_house_index(session, [this_year - 1, this_year])
    since = (datetime.date.today() - datetime.timedelta(days=ACTIVITY_LOOKBACK_DAYS)).isoformat()

    failed = 0
    for m in members:
        log.info(f"\n[{m['manager']} · U.S. House {m['district']}] member id {m['id']}")
        try:
            rows = congress_ptr.fetch_member_trades(
                session, m, index, congress_ptr.known_ptr_docs(sb, m["id"]), since, run_id)
        except Exception as e:
            failed += 1
            log.error(f"  ERROR for {m['manager']}: {e}")
            continue
        for r in rows:
            log.info(f"  {r['filing_date']}  PTR  {r['ticker'] or '—':<6} {r['summary'][:110]}")
        if not rows:
            log.info(f"  No new PTRs in the last {ACTIVITY_LOOKBACK_DAYS} days")
        elif sb is not None:
            sb.table("investor_filings").upsert(rows, on_conflict="cik,accession").execute()
    return failed


def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Smart Money Tracker")
    parser.add_argument("--investors", nargs="+", type=int,
                        help="Limit to these CIKs (and/or negative Congress member ids)")
    parser.add_argument("--force",     action="store_true", help="Re-pull 13Fs even if unchanged")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()

    run_id = args.run_id or get_run_id()
    investors = [i for i in INVESTORS if not args.investors or i["cik"] in args.investors]
    members   = [m for m in congress_ptr.CONGRESS_MEMBERS if not args.investors or m["id"] in args.investors]

    log.info(f"\n{'='*55}")
    log.info(f"  Orblex Industries — Smart Money Tracker")
    log.info(f"  Run ID   : {run_id}")
    log.info(f"  Investors: {len(investors)} funds, {len(members)} members of Congress")
    log.info(f"  Dry run  : {args.dry_run}")
    log.info(f"{'='*55}")

    sb = None if args.dry_run else create_client(SUPABASE_URL, SUPABASE_KEY)
    edgar = Edgar(SEC_USER_AGENT)
    cik_tickers = load_cik_tickers(edgar)

    failed = 0
    for inv in investors:
        try:
            process_investor(edgar, sb, inv, cik_tickers, run_id, args.force, args.dry_run)
        except Exception as e:
            failed += 1
            log.error(f"  ERROR for {inv['manager']}: {e}")
            import traceback; traceback.print_exc()

    if members:
        try:
            failed += process_congress(sb, members, run_id, args.dry_run)
        except Exception as e:  # House index unreachable — every member fails
            failed += len(members)
            log.error(f"  ERROR loading House disclosures: {e}")

    total = len(investors) + len(members)
    log.info(f"\n✓ Smart money tracker complete — {total - failed}/{total} sources. Run ID: {run_id}\n")
    # One source's bad filing should not fail the stage; all of them failing should.
    sys.exit(1 if failed == total else 0)


if __name__ == "__main__":
    main()
