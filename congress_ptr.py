"""
congress_ptr.py — U.S. House members' stock trades (STOCK Act PTRs)
───────────────────────────────────────────────────────────────────
Members of Congress don't file 13Fs; they file a Periodic Transaction Report
(PTR) within 45 days of each trade, on the House Clerk's disclosure site.

  Index  https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
         XML list of every filing: member, FilingType ('P' = PTR), date, DocID.
  PTR    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{DocID}.pdf
         E-filed PTRs have a text layer and are parsed row by row. Paper filings
         are scanned images (no text) and are recorded as one unparsed row
         linking the PDF, so the feed still shows that the member filed.

Amounts are disclosed only as ranges ($1,001 - $15,000, ...). Rows are written
to investor_filings (form 'PTR') alongside Form 4 / 13D / 13G, so the dashboard
feed, analyst context and newsletter pick them up unchanged. Members have no
SEC CIK; each gets a stable negative id in the cik column.

Called from run_smart_money.py — not a stage of its own.
"""

import io, re, zipfile, datetime, logging
import xml.etree.ElementTree as ET

import requests
from pypdf import PdfReader

log = logging.getLogger("smart_money")

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"

# id: negative, stable, never reused — it is the key in investor_filings.cik.
# Matched on last name + state/district, which is unique per sitting member.
CONGRESS_MEMBERS = [
    {"id": -1, "manager": "Nancy Pelosi",    "last": "Pelosi",     "district": "CA11"},
    {"id": -2, "manager": "Ro Khanna",       "last": "Khanna",     "district": "CA17"},
    {"id": -3, "manager": "Josh Gottheimer", "last": "Gottheimer", "district": "NJ05"},
    # Most active 2026 PTR filers (House index, as of 2026-10-04).
    {"id": -4,  "manager": "David Taylor",         "last": "Taylor",      "district": "OH02"},
    {"id": -5,  "manager": "Kelly Morrison",       "last": "Morrison",    "district": "MN03"},
    {"id": -6,  "manager": "Cleo Fields",          "last": "Fields",      "district": "LA06"},
    {"id": -7,  "manager": "Steve Cohen",          "last": "Cohen",       "district": "TN09"},
    {"id": -8,  "manager": "Suzan DelBene",        "last": "DelBene",     "district": "WA01"},
    {"id": -9,  "manager": "Diana Harshbarger",    "last": "Harshbarger", "district": "TN01"},  # scanned filings
    {"id": -10, "manager": "Kevin Hern",           "last": "Hern",        "district": "OK01"},
    {"id": -11, "manager": "Mike Kelly",           "last": "Kelly",       "district": "PA16"},
    {"id": -12, "manager": "Rick Allen",           "last": "Allen",       "district": "GA12"},
    {"id": -13, "manager": "Gil Cisneros",         "last": "Cisneros",    "district": "CA31"},
    {"id": -14, "manager": "April McClain Delaney", "last": "Delaney",    "district": "MD06"},  # indexed as "Delaney"
    {"id": -15, "manager": "Tom Kean Jr.",         "last": "Kean",        "district": "NJ07"},
]

# Rows in the smallest band ($1,001 - $15,000) are skipped: managed-account
# rebalancing noise (Gottheimer's UMA files dozens), not conviction.
MIN_AMOUNT_UPPER = 15_000

# Fewer extracted characters than this means a scanned paper filing.
SCANNED_TEXT_THRESHOLD = 200

OWNER_LABELS = {"SP": "Spouse", "JT": "Joint", "DC": "Child", "": "Self"}
TX_VERBS     = {"P": "bought", "S": "sold", "S (partial)": "partly sold", "E": "exchanged"}
TX_DIRECTION = {"P": "BUY", "S": "SELL", "S (partial)": "SELL", "E": "OTHER"}

# "P 07/24/2026 07/24/2026 $1,000,001 -" — type, trade date, notification date, amount.
_ANCHOR = re.compile(
    r"(?:^|\s)(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.*)$")
_TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,7})\)\s*\[([A-Z]{2})\]")
_TYPE_ONLY = re.compile(r"\[([A-Z]{2})\]")
_DOLLARS    = re.compile(r"\$[\d,]+")
_TRAILING_DOLLARS = re.compile(r"(?:^|\s)(\$[\d,]+)\s*$")
# Field labels under each row, after NUL-stripping: "F S: New", "S O: <account>", "D: <description>".
_DESC   = re.compile(r"^D\s*:\s*(.*)$")
_LABEL  = re.compile(r"^(F\s*S|S\s*O|C)\s*:")
_SKIP   = re.compile(r"^(ID Owner Asset|Type$|Date Notification|Date$|Amount Cap|Gains >|\$200\?|"
                     r"Filing ID #|\* For the complete list)")
_END    = re.compile(r"^(I P O|Initial Public Offerings|I V D|Investment Vehicle Details|C\s+S$|Certification)")


def _short_amount(s: str) -> str:
    """'$1,000,001 - $5,000,000' → '$1M–$5M'."""
    def one(v: str) -> str:
        n = int(v.replace("$", "").replace(",", ""))
        if n >= 1_000_000:
            return f"${round(n / 1_000_000):g}M"
        if n >= 1_000:
            return f"${round(n / 1_000):g}K"
        return f"${n}"
    nums = re.findall(r"\$[\d,]+", s)
    if len(nums) == 2:
        return f"{one(nums[0])}–{one(nums[1])}"
    if len(nums) == 1:
        return f"over {one(nums[0])}"
    return s.strip()


def _amount_upper(s: str) -> int | None:
    nums = [int(v.replace("$", "").replace(",", "")) for v in re.findall(r"\$[\d,]+", s)]
    return max(nums) if nums else None


def parse_ptr_text(text: str) -> list[dict]:
    """Transaction rows from an e-filed PTR's extracted text."""
    # The form's small-caps labels extract as NULs ("F\x00\x00 S\x00: New").
    lines = [l.replace("\x00", "").strip() for l in text.splitlines()]
    try:  # the table starts after the first header row ends with "$200?"
        start = next(i for i, l in enumerate(lines) if l.startswith("$200?")) + 1
    except StopIteration:
        return []

    # `open_row`: the last row's date line was seen but none of its labels yet.
    # Lines in that gap are the rest of its asset name, wrapped across a page
    # break ("... Common P 08/06/2026 ..." / page header / "Stock (RSG) [ST]").
    rows, buf, cur, open_row = [], [], None, False
    i = start
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line or _SKIP.match(line):
            continue
        if _END.match(line):
            break
        d = _DESC.match(line)
        if d and cur is not None:
            cur["description"] = d.group(1).strip()
            open_row = False
            continue
        if _LABEL.match(line):
            open_row = False
            continue
        m = _ANCHOR.search(line)
        if m:
            prefix = line[:m.start()].strip()
            if prefix:
                buf.append(prefix)
            amount = m.group(4).strip()
            # Range wrapped onto the next line ("$250,001 -" / "$500,000"). Across a
            # page break the upper bound comes after the repeated header instead;
            # the open_row branch below picks it up there.
            if amount.endswith("-") and i < len(lines) and _DOLLARS.fullmatch(lines[i].strip()):
                amount = f"{amount} {lines[i].strip()}"
                i += 1
            cur = {"asset_text": " ".join(buf), "tx_type": m.group(1),
                   "trade_date": m.group(2), "amount": amount, "description": ""}
            rows.append(cur)
            buf, open_row = [], True
            continue
        if open_row:
            if cur["amount"].endswith("-"):
                t = _TRAILING_DOLLARS.search(line)
                if t:  # "Stock (DVN) [ST] $250,000" or a bare "$50,000"
                    cur["amount"] = f"{cur['amount']} {t.group(1)}"
                    line = line[:t.start()].strip()
                    if not line:
                        continue
            cur["asset_text"] += " " + line
            continue
        buf.append(line)

    for r in rows:
        asset = r["asset_text"]
        owner = asset.split(" ", 1)[0] if asset.split(" ", 1)[0] in ("SP", "JT", "DC") else ""
        if owner:
            asset = asset[len(owner):].strip()
        t = _TICKER.search(asset)
        r["owner"]      = owner
        r["ticker"]     = t.group(1) if t else None
        r["asset_type"] = t.group(2) if t else (_TYPE_ONLY.search(asset).group(1) if _TYPE_ONLY.search(asset) else None)
        r["asset"]      = re.sub(r"\s*\([A-Z][A-Z0-9.\-]{0,7}\)\s*|\s*\[[A-Z]{2}\]", " ", asset).strip(" -")
    return rows


def _kind(r: dict) -> str:
    if r["asset_type"] != "OP":
        return "stock"
    desc = r["description"].lower()
    return "call options" if "call" in desc else "put options" if "put" in desc else "options"


def _bounds(amount: str) -> tuple[int, int | None]:
    """'$15,001 - $50,000' → (15001, 50000); 'Over $50,000,000' → (50000000, None)."""
    nums = [int(v.replace("$", "").replace(",", "")) for v in _DOLLARS.findall(amount)]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return (nums[0], None) if nums else (0, None)


def _fmt_range(lo: int, hi: int | None) -> str:
    return _short_amount(f"${lo:,} - ${hi:,}" if hi is not None else f"${lo:,}")


def merge_rows(rows: list[dict]) -> list[dict]:
    """
    Collapse a PTR's rows to one per (ticker, direction, owner, stock/options):
    an account sold down in five lots is one decision, not five. Amount ranges
    are summed (low+low, high+high), trade dates become a span, and the first
    row's position supplies the stable accession index `n`.
    """
    groups: dict[tuple, list] = {}
    for n, r in enumerate(rows, 1):
        key = (r["ticker"], TX_DIRECTION.get(r["tx_type"], "OTHER"), r["owner"], _kind(r))
        groups.setdefault(key, []).append((n, r))
    out = []
    for (ticker, direction, owner, kind), items in groups.items():
        first_n, first = items[0]
        trades = [r for _, r in items]
        lo = sum(_bounds(r["amount"])[0] for r in trades)
        his = [_bounds(r["amount"])[1] for r in trades]
        hi = None if any(h is None for h in his) else sum(his)
        types = {r["tx_type"] for r in trades}
        verb = TX_VERBS.get(first["tx_type"], first["tx_type"]) if len(types) == 1 else (
            "sold" if direction == "SELL" else "bought" if direction == "BUY" else "exchanged")
        dates = sorted(r["trade_date"][:5] for r in trades)
        when = f"traded {dates[0]}" if dates[0] == dates[-1] else f"traded {dates[0]}–{dates[-1]}"
        count = f" in {len(trades)} trades" if len(trades) > 1 else ""
        summary = (f"{OWNER_LABELS.get(owner, owner)} {verb} {_fmt_range(lo, hi)} of {kind}"
                   f"{count} ({when})")
        if len(trades) == 1 and first["description"]:
            summary += f": {first['description'][:110]}"
        out.append({"n": first_n, "ticker": ticker, "issuer": first["asset"],
                    "direction": direction, "upper": hi, "summary": summary})
    return out


def load_house_index(session: requests.Session, years: list[int]) -> list[dict]:
    """PTR rows from the yearly index files. A year's file can list filings made
    early the next year, so callers pass the current and previous year."""
    out = []
    for y in years:
        resp = session.get(f"{HOUSE_BASE}/financial-pdfs/{y}FD.zip", timeout=60)
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        root = ET.fromstring(z.read(f"{y}FD.xml"))
        for m in root:
            row = {c.tag: (c.text or "").strip() for c in m}
            if row.get("FilingType") == "P":
                out.append(row)
    return out


def _iso(us_date: str) -> str:
    return datetime.datetime.strptime(us_date, "%m/%d/%Y").date().isoformat()


def fetch_member_trades(session: requests.Session, member: dict, index: list[dict],
                        known_docs: set[str], since: str, run_id: str) -> list[dict]:
    """investor_filings rows for the member's PTRs filed since `since` that are not already stored."""
    filings = [r for r in index
               if r["Last"] == member["last"] and r["StateDst"] == member["district"]
               and _iso(r["FilingDate"]) >= since and r["DocID"] not in known_docs]
    out = []
    for f in sorted(filings, key=lambda r: _iso(r["FilingDate"])):
        url = f"{HOUSE_BASE}/ptr-pdfs/{f['Year']}/{f['DocID']}.pdf"
        resp = session.get(url, timeout=60)
        if resp.status_code != 200:
            log.warning(f"    skip PTR {f['DocID']}: HTTP {resp.status_code}")
            continue
        reader = PdfReader(io.BytesIO(resp.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        base = {"run_id": run_id, "cik": member["id"], "manager": member["manager"],
                "fund": f"U.S. House · {member['district'][:2]}-{member['district'][2:]}",
                "form": "PTR", "filing_date": _iso(f["FilingDate"]), "url": url,
                "shares_bought": None, "shares_sold": None, "avg_price": None,
                "shares_after": None, "pct_class": None}

        if len(text.strip()) < SCANNED_TEXT_THRESHOLD:
            out.append({**base, "accession": f"PTR-{f['DocID']}-000", "ticker": None,
                        "issuer": "Scanned paper filing", "direction": "OTHER",
                        "summary": f"Paper PTR ({len(reader.pages)} pages), scanned image, "
                                   f"not machine-readable; open the PDF for the trades"})
            continue

        rows = parse_ptr_text(text)
        # The size filter applies per trade, before merging, so many small lots
        # cannot add up past it.
        sized = [r for r in rows if r["ticker"] and
                 ((_amount_upper(r["amount"]) or MIN_AMOUNT_UPPER + 1) > MIN_AMOUNT_UPPER)]
        kept = 0
        for g in merge_rows(sized):
            kept += 1
            out.append({**base, "accession": f"PTR-{f['DocID']}-{g['n']:03d}",
                        "ticker": re.sub(r"[./]", "-", g["ticker"]), "issuer": g["issuer"],
                        "direction": g["direction"], "summary": g["summary"]})
        if kept == 0:
            # Keep a marker so the DocID counts as processed and is not re-downloaded.
            # direction SKIPPED keeps it out of the dashboard feed and the newsletter.
            out.append({**base, "accession": f"PTR-{f['DocID']}-000", "ticker": None,
                        "issuer": "PTR", "direction": "SKIPPED",
                        "summary": f"PTR with {len(rows)} transaction(s), none in listed "
                                   f"stocks above ${MIN_AMOUNT_UPPER:,}"})
    return out


def known_ptr_docs(sb, member_id: int) -> set[str]:
    """DocIDs already stored for the member (accession = PTR-<DocID>-<n>)."""
    if sb is None:
        return set()
    resp = sb.table("investor_filings").select("accession").eq("cik", member_id).execute()
    return {a["accession"].split("-")[1] for a in resp.data or [] if a["accession"].startswith("PTR-")}
