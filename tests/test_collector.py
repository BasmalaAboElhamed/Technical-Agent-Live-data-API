"""Collector tests use MOCKED HTTP (httpx.MockTransport). They prove parsing/retry logic only; they say
nothing about whether the real EGX endpoint is reachable (see tests/integration and scripts/test_source.py)."""
from __future__ import annotations

import asyncio
import json
from zoneinfo import ZoneInfo
from datetime import datetime

import httpx
import pytest

from app.collectors.base import MalformedResponse, SourceError, SourceRateLimited, SourceUnavailable
from app.collectors.egx_beta import RecordRejected, extract_page, normalize_record, parse_retry_after
from app.utils import normalize_symbol, parse_number, utcnow
from tests.fixtures import REAL_CIB, REAL_ETEL, rec, synth

TZ = ZoneInfo("Africa/Cairo")


def run(coro):
    return asyncio.run(coro)


def paged(items, page_size=2, envelope="list", total=None):
    """Mock server: slices `items` by Page/PageSize query params."""
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        page, size = int(q["Page"]), int(q["PageSize"])
        chunk = items[(page - 1) * size: page * size]
        if envelope == "list":
            body = chunk
        else:
            body = {"items": chunk, "totalCount": total if total is not None else len(items)}
        return httpx.Response(200, json=body)
    return handler


# ------------------------------------------------------------------ 1-4: parsing
def test_real_record_mapping():
    q = normalize_record(REAL_CIB, utcnow(), TZ)
    assert q.symbol == "COMI" and q.isin == "EGS60121C018" and q.exchange == "EGX"
    assert q.currency == "EGP"
    assert (q.last_price, q.close_price, q.previous_close, q.high, q.low) == (125.15, 125.15, 129.0, 128.68, 125.15)
    assert q.open is None                                   # openPrice is a prevClose placeholder (see test_real_capture)
    assert q.symbol_type == "C"
    assert q.change_percent == -2.98
    assert q.volume == 5110068 and q.number_of_trades == 2555 and q.traded_value == 648266144.71
    assert q.change is None and q.bid is None and q.ask is None       # not in source -> null, never computed
    assert q.source_timestamp == datetime(2026, 10, 22, 15, 38, tzinfo=TZ)
    assert q.last_trade_date.isoformat() == "2026-10-21"


def test_missing_fields_become_null_not_zero():
    q = normalize_record(rec(lastPrice=None, openPrice=None, volume=None, trades=None, writeTime=None), utcnow(), TZ)
    assert q.last_price is None and q.open is None and q.volume is None and q.number_of_trades is None
    assert q.source_timestamp is None


def test_valid_zero_is_preserved_and_distinct_from_missing():
    q = normalize_record(rec(volume=0, value=0, trades=0, chgPer=0, lastPrice=0), utcnow(), TZ)
    assert q.volume == 0 and q.traded_value == 0.0 and q.number_of_trades == 0
    assert q.change_percent == 0.0 and q.last_price == 0.0
    absent = REAL_CIB.copy(); absent.pop("volume")
    assert normalize_record(absent, utcnow(), TZ).volume is None


def test_last_price_never_derived_from_close_price():
    q = normalize_record(rec(lastPrice=None, closePrice=99.0), utcnow(), TZ)
    assert q.last_price is None


def test_arabic_and_english_names_and_unicode_normalisation():
    q = normalize_record(REAL_CIB, utcnow(), TZ)
    assert q.company_name_ar == "البنك التجاري الدولي -مصر (سى اى بى )".strip()
    assert q.company_name == "Commercial International Bank-Egypt (CIB)"
    decomposed = "Caf\u0065\u0301   Co"
    assert normalize_record(rec(name=decomposed), utcnow(), TZ).company_name == "Caf\u00e9 Co"


def test_currency_only_when_source_confirms():
    assert normalize_record(rec(curr=None, currShort=None), utcnow(), TZ).currency is None
    assert normalize_record(rec(curr="US Dollar ($)"), utcnow(), TZ).currency == "USD"
    assert normalize_record(rec(curr="Euro"), utcnow(), TZ).currency is None


@pytest.mark.parametrize("raw,expected", [
    (5, 5.0), (0, 0.0), ("1,234.5", 1234.5), ("1,234", 1234.0), ("12,5", 12.5), ("٣٫١٤", 3.14),
    ("١٢٣", 123.0), ("", None), ("  ", None), ("-", None), (None, None), ("3.5%", 3.5),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("bad", ["abc", True, float("nan"), float("inf"), "1,2,3", [1], {}])
def test_parse_number_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_number(bad)


def test_symbol_normalisation_does_not_merge_distinct_names():
    assert normalize_symbol(" comi.ca ") == "COMI" and normalize_symbol("COMI") == "COMI"
    assert normalize_symbol("ABC.CAD") == "ABC.CAD"        # only a trailing '.CA' is stripped


def test_malformed_records_are_rejected_safely():
    for bad, reason in [("str", "not_an_object"), (rec(reuters=None, isin=None), "missing_identifier"),
                        (rec(lastPrice="abc"), "invalid_number:last_price"),
                        (rec(lastPrice=-1), "negative_value:last_price"),
                        (rec(trades=1.5), "invalid_number:number_of_trades")]:
        with pytest.raises(RecordRejected) as e:
            normalize_record(bad, utcnow(), TZ)
        assert e.value.reason == reason


def test_envelope_shapes():
    assert extract_page([1, 2])[0] == [1, 2]
    items, total, pages = extract_page({"data": {"items": [1], "totalCount": 9, "totalPages": 3}})
    assert (items, total, pages) == ([1], 9, 3)
    with pytest.raises(MalformedResponse):
        extract_page({"foo": 1})
    with pytest.raises(MalformedResponse):
        extract_page("nope")


# ------------------------------------------------------------------ 5-6: pagination, duplicates
def test_full_list_collection_across_pages_bare_list(make_collector):
    items = synth(5)
    res = run(make_collector(paged(items)).collect())
    assert len(res.quotes) == 5 and res.complete and res.pages_fetched == 4
    assert res.requests_made == 4          # 3 data pages + 1 empty terminating page
    assert res.total_reported is None and res.notes


def test_pagination_uses_reported_total_and_stops_early(make_collector):
    res = run(make_collector(paged(synth(5), envelope="dict")).collect())
    assert len(res.quotes) == 5 and res.complete and res.requests_made == 3 and res.total_reported == 5


def test_reported_total_larger_than_collected_is_incomplete(make_collector):
    res = run(make_collector(paged(synth(4), envelope="dict", total=9)).collect())
    assert not res.complete and any("reported 9" in e for e in res.errors)


def test_pagination_ignored_by_server_is_detected(make_collector):
    handler = lambda req: httpx.Response(200, json=synth(2))
    res = run(make_collector(handler).collect())
    assert not res.complete and len(res.quotes) == 2 and any("repeated" in e for e in res.errors)


def test_duplicates_and_conflicts_counted_not_stored(make_collector):
    a = rec(REAL_ETEL); b = rec(REAL_ETEL, lastPrice=1.0)
    res = run(make_collector(paged([a, a, b], page_size=10)).collect())
    assert len(res.quotes) == 1 and res.duplicates == 2 and res.duplicate_conflicts == 1
    assert res.quotes[0].last_price == 133.9           # first occurrence wins


def test_two_securities_with_same_symbol_are_not_merged(make_collector):
    a = rec(REAL_ETEL); b = rec(REAL_ETEL, isin="EGS99999C999")
    res = run(make_collector(paged([a, b], page_size=10)).collect())
    assert len(res.quotes) == 1 and res.rejected == {"symbol_conflict": 1}


# ------------------------------------------------------------------ 7-12: failures
def test_timeouts_retry_with_backoff_then_raise(make_collector, recorder):
    def handler(req): raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(SourceUnavailable):
        run(make_collector(handler).collect())
    assert recorder.sleeps == [1.0, 2.0]               # rng=1.0 -> full ceiling; exponential; 2 retries


def test_http_429_honours_retry_after_then_recovers(make_collector, recorder):
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=[] if calls["n"] > 2 else synth(1))
    res = run(make_collector(handler, page_size=10).collect())
    assert recorder.sleeps[0] == 7.0 and len(res.quotes) == 1 and res.retries == 1


def test_http_429_with_huge_retry_after_is_not_waited_out(make_collector, recorder):
    handler = lambda req: httpx.Response(429, headers={"Retry-After": "3600"})
    with pytest.raises(SourceRateLimited) as e:
        run(make_collector(handler).collect())
    assert e.value.retry_after == 3600 and recorder.sleeps == []


def test_http_5xx_retries_and_gives_up(make_collector, recorder):
    handler = lambda req: httpx.Response(503)
    with pytest.raises(SourceUnavailable):
        run(make_collector(handler).collect())
    assert len(recorder.sleeps) == 2


def test_http_5xx_then_success(make_collector):
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, json=[])
    res = run(make_collector(handler).collect())
    assert res.retries == 1


def test_403_is_not_retried_and_not_bypassed(make_collector, recorder):
    with pytest.raises(SourceError) as e:
        run(make_collector(lambda r: httpx.Response(403)).collect())
    assert "not bypassed" in str(e.value) and recorder.sleeps == []


def test_malformed_json_and_shape(make_collector):
    with pytest.raises(MalformedResponse):
        run(make_collector(lambda r: httpx.Response(200, content=b"<html>nope</html>")).collect())
    with pytest.raises(MalformedResponse):
        run(make_collector(lambda r: httpx.Response(200, json={"error": "x"})).collect())


def test_empty_response_yields_no_quotes(make_collector):
    res = run(make_collector(lambda r: httpx.Response(200, json=[])).collect())
    assert res.quotes == []


def test_bad_records_in_page_are_dropped_good_ones_kept(make_collector):
    items = [rec(REAL_ETEL), "junk", rec(REAL_CIB, lastPrice="abc")]
    res = run(make_collector(paged(items, page_size=10)).collect())
    assert len(res.quotes) == 1 and res.rejected_total == 2


def test_partial_collection_when_later_page_fails(make_collector):
    items = synth(4)
    def handler(req):
        if req.url.params["Page"] == "1":
            return httpx.Response(200, json=items[:2])
        return httpx.Response(500)
    res = run(make_collector(handler).collect())
    assert len(res.quotes) == 2 and not res.complete and any("page 2" in e for e in res.errors)


def test_parse_retry_after_http_date():
    from datetime import timezone
    assert parse_retry_after("5") == 5.0 and parse_retry_after("garbage") is None and parse_retry_after(None) is None
    now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 22 Oct 2026 12:00:30 GMT", now) == 30.0


# ------------------------------------------------------------------ market status + real envelope
def test_market_status_parsed_with_cairo_local_time(make_collector):
    body = {"data": {"status": "Closed", "status_Ar": "مغلق", "statusDate": "2026-10-25T19:45:36"}, "success": True}
    ms = run(make_collector(lambda r: httpx.Response(200, json=body)).fetch_market_status())
    assert ms.status == "Closed" and ms.is_open is False and ms.status_ar == "مغلق"
    assert ms.status_date == datetime(2026, 10, 25, 19, 45, 36, tzinfo=TZ)


def test_market_status_failure_is_non_fatal_and_unknown_values_fall_back(make_collector):
    assert run(make_collector(lambda r: httpx.Response(503)).fetch_market_status()) is None
    assert run(make_collector(lambda r: httpx.Response(200, json={"data": {}})).fetch_market_status()) is None
    from app.collectors.base import MarketStatus
    assert MarketStatus("Pre-Open", None, None, utcnow()).is_open is None


def test_nested_real_envelope_and_success_false(make_collector):
    items = synth(3)
    def handler(req):
        page, size = int(req.url.params["Page"]), int(req.url.params["PageSize"])
        return httpx.Response(200, json={"success": True, "data": {"data": items[(page - 1) * size: page * size],
                                                                    "totalCount": 3, "totalPages": 2}, "totalCount": 3})
    res = run(make_collector(handler).collect())
    assert len(res.quotes) == 3 and res.complete and res.total_reported == 3 and res.requests_made == 2
    with pytest.raises(MalformedResponse):
        run(make_collector(lambda r: httpx.Response(200, json={"success": False, "message": "boom"})).collect())
