import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

yaml = pytest.importorskip("yaml")
WORKFLOWS = sorted((ROOT / "deploy" / "github-workflows").glob("*.yml"))


# A workflow that doesn't parse is rejected by GitHub without a run: every pg_cron
# dispatch to it fails silently (the 2026-10-17 long-term run was lost this way).
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses(path):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert wf.get("jobs"), f"{path.name} has no jobs"
    assert True in wf or "on" in wf, f"{path.name} has no triggers"   # YAML 1.1 reads `on:` as True


def test_dispatched_workflows_accept_workflow_dispatch():
    schema = (ROOT / "automation_schema.sql").read_text(encoding="utf-8")
    for path in WORKFLOWS:
        if path.name in schema:
            wf = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "workflow_dispatch" in (wf.get(True) or wf.get("on")), path.name


# GitHub cron is UTC. The Saturday fallback must run after the 9:00 ET dispatch in winter too
# (14:00 UTC), or it beats the 8:30 holdings snapshot and the 9:00 dispatch runs a second book.
def test_saturday_fallback_runs_after_the_dispatch_in_both_seasons():
    wf = yaml.safe_load((ROOT / "deploy" / "github-workflows" / "orblex_pipeline.yml").read_text(encoding="utf-8"))
    crons = [s["cron"].split() for s in (wf.get(True) or wf.get("on"))["schedule"]]
    saturday = [c for c in crons if c[4] == "6"]
    assert saturday and all(int(c[1]) > 14 for c in saturday)


# The dashboard's public key ships in its JavaScript: anon must never get account data back.
@pytest.mark.parametrize("table", ["trade_orders", "trade_fills", "exec_events", "trading_mandates", "trading_controls",
                                   "exec_dryrun", "job_runs", "universe_runs", "longterm_advice"])
def test_account_tables_have_no_public_read_policy(table):
    for path in ROOT.glob("*.sql"):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert not (f"ON {table} " in line and "CREATE POLICY" in line and "anon" in line), f"{path.name}: {line}"


# Going public: the public key gets nothing, on any table or view, however a table was created.
def test_public_key_is_locked_out_of_the_whole_database():
    private = (ROOT / "private_access_schema.sql").read_text(encoding="utf-8")
    for needed in ("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon",
                   "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon",
                   "SET (security_invoker = true)"):
        assert needed in private
    assert "TO anon" not in private
    for secret in ("broker_secrets", "trading_admins", "account_snapshots"):
        assert f"'{secret}'" in private.split("NOT IN")[1].split("THEN")[0]


class _Q:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def select(self, *_): return self
    def limit(self, *_): return self
    def insert(self, row): self.log.append(row); return self
    def execute(self): return type("R", (), {"data": self.rows})()

    def eq(self, k, v):
        self.rows = [r for r in self.rows if r.get(k) == v]
        return self


class FakeSB:
    def __init__(self, rows):
        self.rows, self.inserted = rows, []

    def table(self, name):
        assert name == "job_runs"
        return _Q(list(self.rows), self.inserted)


@pytest.fixture
def nl(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", os.environ.get("SUPABASE_URL", "http://x"))
    monkeypatch.setenv("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", "k"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "k"))
    return pytest.importorskip("run_newsletter")


def test_newsletter_sent_once_per_day(nl):
    sb = FakeSB([{"job": "newsletter", "run_date": "2026-10-15", "status": "OK", "started_at": "t"}])
    assert nl.already_sent(sb, "newsletter", "2026-10-15")
    assert not nl.already_sent(sb, "newsletter", "2026-10-16")
    assert not nl.already_sent(sb, "newsletter_longterm", "2026-10-15")


def test_failed_send_does_not_count(nl):
    sb = FakeSB([{"job": "newsletter", "run_date": "2026-10-15", "status": "FAILED", "started_at": "t"}])
    assert not nl.already_sent(sb, "newsletter", "2026-10-15")


def test_record_then_skip(nl):
    sb = FakeSB([])
    nl.record_sent(sb, "newsletter", "2026-10-16", "Orblex Industries — x", "pipeline-2026-10-16-abc")
    row = sb.inserted[0]
    assert (row["job"], row["run_date"], row["status"]) == ("newsletter", "2026-10-16", "OK")
    assert nl.already_sent(FakeSB(sb.inserted), "newsletter", "2026-10-16")


def test_longterm_book_is_tracked_separately(nl, monkeypatch):
    monkeypatch.setenv("BOOK", "longterm")
    assert nl._newsletter_job() == "newsletter_longterm"
    monkeypatch.setenv("BOOK", "daily")
    assert nl._newsletter_job() == "newsletter"


# The two emails must be told apart by their words: swing brief on weekdays,
# advice letter for the main account on Saturday.
SWING = {"available": True, "plans": [], "exits": [], "overridden": [], "level": "LOW",
         "record": None, "fill_rate": None}
LT_ROWS = [
    {"ticker": "AAA", "action": "ADD", "target_pct": 6.0, "prev_target_pct": 4.0, "conviction": 2,
     "why": [{"source": "Analyst", "text": "trend intact"}], "wrong_if": "risk rises to 8+/10",
     "changes": ["target 4.0% → 6.0%"], "week_of": "2026-10-26", "regime": "BULL", "cash_pct": 20.0},
    {"ticker": "BBB", "action": "HOLD", "target_pct": 5.0, "prev_target_pct": 5.0, "conviction": 1,
     "why": [], "wrong_if": None, "changes": [], "week_of": "2026-10-26", "regime": "BULL", "cash_pct": 20.0},
]


def test_swing_email_is_the_swing_brief(nl):
    summary = {"headline": "h", "risk_watch": "r"}
    html = nl.build_html_v2({}, summary, swing=SWING)
    text = nl.build_plaintext({}, summary, swing=SWING)
    for body in (html, text):
        assert "SWING BRIEF" in body and "SWING TRADE PLANS" in body
        assert "ADVICE ONLY" not in body and "WEEKLY REVIEW" not in body and "SMART MONEY" not in body


def test_longterm_email_is_the_advice_letter(nl):
    html, text = nl.build_longterm_html(LT_ROWS), nl.build_longterm_plaintext(LT_ROWS)
    for body in (html, text):
        assert "WEEKLY REVIEW" in body and "ADVICE ONLY" in body and "1 change suggested" in body
        assert "Everything else holds: BBB" in body
        assert "SWING TRADE PLANS" not in body and "SWING BRIEF" not in body and "LIVE PIPELINE" not in body
    empty = nl.build_longterm_html([])
    assert "No advice this week" in empty and "SWING" not in empty


def test_subjects_name_their_book(nl):
    import datetime
    day = datetime.date(2026, 10, 26)
    assert nl.swing_subject(SWING, day) == "Swing — 0 new entries, 0 exits — Oct 26, 2026"
    assert nl.swing_subject({"available": False}, day).startswith("Swing — no new plans")
    assert nl.longterm_subject(LT_ROWS) == "Main account — 1 change — week of Oct 26"
    assert nl.longterm_subject(LT_ROWS[1:]).startswith("Main account — no changes")


def test_closed_market_skips_only_the_swing_email(nl, monkeypatch):
    import datetime
    monkeypatch.setattr(nl, "_market_open", lambda d: False)
    saturday = datetime.date(2026, 10, 24)
    assert nl._skip_closed_market("newsletter", saturday)
    assert not nl._skip_closed_market("newsletter_longterm", saturday)
