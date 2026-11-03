from watchdog_saver import save_watchdog_audit


class _FakeQuery:
    def __init__(self, calls, table):
        self._calls, self._table = calls, table

    def upsert(self, rows, on_conflict=None):
        self._calls.append((self._table, rows, on_conflict))
        return self

    def execute(self):
        return None


class _FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self.calls, name)


CTX = {
    "watchdog_date": "2026-10-14",
    "report_run_id": "r",
    "alloc_run_id": "a",
    "risk_run_id": "k",
    "analyst_run_id": "n",
}


def test_duplicate_issues_are_merged_before_upsert():
    audit = {"agent_consistency_issues": [
        {"issue_type": "SIGNAL_MISMATCH", "severity": "LOW", "ticker": "UMC", "description": "first"},
        {"issue_type": "SIGNAL_MISMATCH", "severity": "HIGH", "ticker": "UMC", "description": "second"},
        {"issue_type": "SIGNAL_MISMATCH", "severity": "MEDIUM", "ticker": "UMC", "description": "first"},
        {"issue_type": "CASH_BUFFER", "severity": "MEDIUM", "ticker": None, "description": "x"},
        {"issue_type": "CASH_BUFFER", "severity": "LOW", "description": "y"},
    ]}
    client = _FakeClient()

    assert save_watchdog_audit(client, "wd-1", CTX, audit) is True

    issues = next(rows for table, rows, _ in client.calls if table == "watchdog_issues")
    keys = [(r["watchdog_run_id"], r["ticker"], r["issue_type"]) for r in issues]
    assert len(keys) == len(set(keys)) == 2

    umc = next(r for r in issues if r["ticker"] == "UMC")
    assert umc["severity"] == "HIGH"
    assert umc["description"] == "first | second"

    portfolio = next(r for r in issues if r["ticker"] == "PORTFOLIO")
    assert portfolio["severity"] == "MEDIUM"
    assert portfolio["description"] == "x | y"
