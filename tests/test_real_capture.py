"""Tests against a REAL response recorded on 2026-10-25 (market closed) from the live endpoint via the browser probe:
tests/data/market_watch_real_page1_size25.json (first 25 of 224 records) and market_status_real.json.
They pin down what was OBSERVED, so a future change in the source shows up as a failing test, not silent bad data."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from app.collectors.egx_beta import extract_page, normalize_record
from app.utils import utcnow

D = Path(__file__).parent / "data"
TZ = ZoneInfo("Africa/Cairo")
PAYLOAD = json.loads((D / "market_watch_real_page1_size25.json").read_text(encoding="utf-8"))


def test_real_envelope_is_nested_and_reports_totals():
    items, total, pages = extract_page(PAYLOAD)
    assert (len(items), total, pages) == (25, 224, 9)
    assert PAYLOAD["success"] is True and sorted(PAYLOAD["data"]) == ["data", "pageNumber", "pageSize", "totalCount", "totalPages"]


def test_every_real_record_parses_without_rejection():
    items, _, _ = extract_page(PAYLOAD)
    quotes = [normalize_record(r, utcnow(), TZ) for r in items]
    assert len({q.symbol for q in quotes}) == 25 and len({q.isin for q in quotes}) == 25
    assert all(q.last_price is not None and q.volume and q.source_timestamp for q in quotes)
    assert all(q.exchange == "EGX" for q in quotes)


def test_evidence_open_price_is_just_prev_close_so_open_is_not_exposed():
    items, _, _ = extract_page(PAYLOAD)
    assert all(r["openPrice"] == r["prevClose"] for r in items)          # the observed fact
    default = [normalize_record(r, utcnow(), TZ) for r in items]
    assert all(q.open is None for q in default)                          # therefore never presented as Open
    trusted = [normalize_record(r, utcnow(), TZ, trust_open=True) for r in items]
    assert all(q.open is not None for q in trusted)                      # opt-in override exists


def test_close_price_and_last_price_are_kept_separate():
    items, _, _ = extract_page(PAYLOAD)
    for r in items:
        q = normalize_record(r, utcnow(), TZ)
        assert q.close_price == r["closePrice"] and q.last_price == r["lastPrice"]


def test_write_time_is_snapshot_wide_and_in_cairo_time():
    items, _, _ = extract_page(PAYLOAD)
    assert Counter(r["writeTime"] for r in items) == {"202610251534": 25}
    q = normalize_record(items[0], utcnow(), TZ)
    assert q.source_timestamp.astimezone(TZ).strftime("%Y-%m-%d %H:%M") == "2026-10-25 15:34"


def test_market_status_real_payload_shape():
    st = json.loads((D / "market_status_real.json").read_text(encoding="utf-8"))
    assert st["success"] is True and st["data"]["status"] == "Closed" and st["data"]["status_Ar"] == "مغلق"
    assert st["data"]["statusDate"].startswith("2026-10-25T")


def test_non_egp_and_bond_variants_from_real_capture():
    base = extract_page(PAYLOAD)[0][0]
    usd = normalize_record({**base, "curr": "US Dollar ($)", "currShort": "$"}, utcnow(), TZ)
    assert usd.currency == "USD"
    bond = normalize_record({**base, "reuters": "EGBKRSK01CV", "symbolType": "B", "typeName": "Corporate Bonds", "marketCode": "CORP"}, utcnow(), TZ)
    assert bond.symbol == "EGBKRSK01CV" and bond.symbol_type == "B" and bond.type_name == "Corporate Bonds"
