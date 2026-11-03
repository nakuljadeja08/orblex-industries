import pytest

from portfolio_stance import stance


@pytest.mark.parametrize("level,cash,flagged,n,buys,expected", [
    ("CRITICAL", 5, 0, 10, 8, "CASH_HEAVY"),
    ("MODERATE", 26, 0, 10, 2, "CASH_HEAVY"),
    ("HIGH", 15, 0, 10, 2, "DEFENSIVE"),
    ("MODERATE", 22.5, 12, 20, 5, "DEFENSIVE"),     # the Oct 14 case the model labelled CASH_HEAVY
    ("MODERATE", 15.6, 12, 20, 5, "DEFENSIVE"),     # the Oct 15 case the model labelled BALANCED
    ("LOW", 5, 1, 10, 6, "AGGRESSIVE"),
    ("LOW", 12, 1, 10, 6, "BALANCED"),
    ("MODERATE", 10, 3, 10, 2, "BALANCED"),
])
def test_rules(level, cash, flagged, n, buys, expected):
    assert stance(level, cash, flagged, n, buys) == expected


def test_incomplete_inputs():
    assert stance("UNKNOWN", 10, 0, 10, 0) is None
    assert stance("LOW", None, 0, 10, 0) is None
    assert stance("LOW", 10, 0, 0, 0) is None
