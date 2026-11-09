"""Test fixtures.

REAL_* records are the two records the user captured from the real endpoint in browser DevTools
(only the fields visible in that capture; fields before "case30" were cut off in the paste).
Everything else here is SYNTHETIC test data and says so.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

REAL_CIB = {
    "case30": "Y", "chgPer": -2.98, "closePrice": 125.15, "curr": "Egyptian Pound", "currA": "جنيه مصري",
    "currShort": "L.E", "currShortA": "ج.م.", "high": 128.68, "isin": "EGS60121C018", "lastPrice": 125.15,
    "lastTradeDate": "2026-10-21T00:00:00", "low": 125.15, "marketCode": "NOPL", "mc": 426153158365,
    "name": "Commercial International Bank-Egypt (CIB)", "nameA": "البنك التجاري الدولي -مصر (سى اى بى )",
    "openPrice": 129, "prevClose": 129, "reuters": "COMI.CA", "sector": "Banks", "sectorA": "بنوك", "secId": 1,
    "symbolType": "C", "trades": 2555, "value": 648266144.71, "volume": 5110068, "writeTime": "202610221538",
}
REAL_ETEL = {
    "case30": "Y", "chgPer": -5, "closePrice": 133.9, "curr": "Egyptian Pound", "currShort": "L.E",
    "high": 140.68, "isin": "EGS48031C016", "lastPrice": 133.9, "lastTradeDate": "2026-10-21T00:00:00",
    "low": 128.6, "name": "Telecom Egypt", "nameA": "المصرية للاتصالات", "openPrice": 140.95,
    "prevClose": 140.95, "reuters": "ETEL.CA", "sector": "IT , Media & Communication Services",
    "secId": 18, "symbolType": "C", "trades": 15143, "value": 590673851.73, "volume": 4461086,
    "writeTime": "202610221538",
}


def rec(base=None, **over):
    """SYNTHETIC variant of a real record."""
    d = copy.deepcopy(base or REAL_CIB)
    d.update(over)
    return d


def synth(n: int, **over):
    """SYNTHETIC records with distinct ISIN/symbol."""
    return [rec(REAL_ETEL, isin=f"EGS{i:08d}C01", reuters=f"T{i:03d}.CA", name=f"Test Co {i}", **over) for i in range(n)]


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)
