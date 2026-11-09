from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.database.repository import RunRecord
from app.main import create_app
from app.schemas.stock import QuoteBase
from tests.fixtures import utc

NOW = utc(2026, 10, 22, 8, 5)         # Thursday 11:05 Cairo: market open
TS = utc(2026, 10, 22, 8, 4)          # a minute old


def mkq(sym, isin, price, name, ts=TS, **kw):
    return QuoteBase(symbol=sym, isin=isin, company_name=name, last_price=price, source_timestamp=ts,
                     fetched_at=NOW - timedelta(seconds=20), currency="EGP", **kw)


@pytest.fixture
def seeded(settings, repo):
    quotes = [
        mkq("COMI", "E1", 125.15, "Commercial International Bank", volume=5110068, change_percent=-2.98,
            traded_value=648266144.71, number_of_trades=2555, company_name_ar="البنك التجاري الدولي"),
        mkq("ETEL", "E2", 133.9, "Telecom Egypt", volume=4461086, change_percent=1.5, traded_value=590673851.73,
            number_of_trades=15143),
        mkq("ZERO", "E3", 0.0, "Zero Co", volume=0, change_percent=0.0, traded_value=0.0, number_of_trades=0),
        mkq("NOPX", "E4", None, "No Price Co"),
    ]
    repo.apply_collection(quotes, RunRecord(started_at=NOW - timedelta(seconds=22), finished_at=NOW - timedelta(seconds=20),
                                            status="success", complete=True, stock_count=4, source_timestamp_max=TS,
                                            duration_ms=1800))
    app = create_app(settings)
    app.state.service._clock = lambda: NOW
    with TestClient(app) as c:
        yield c, app, repo


def test_health_ok(seeded):
    c, *_ = seeded
    r = c.get("/health"); b = r.json()
    assert r.status_code == 200 and b["status"] == "ok" and b["database"]["ok"] is True
    assert b["last_successful_collection"] is not None


def test_status_fields(seeded):
    c, *_ = seeded
    b = c.get("/api/v1/status").json()
    assert b["source_name"] == "EGX Beta Market Watch" and b["source_connectivity"] == "ok"
    assert b["data_freshness"] == "fresh" and b["market_session_open"] is True
    assert b["stocks_in_latest_collection"] == 4 and b["dataset_state"] == "complete" and b["dataset_complete"] is True
    assert b["last_collection_duration_seconds"] == 1.8 and b["most_recent_error"] is None
    assert b["data_timing"] == "unverified" and b["polling_interval_seconds"] >= 30
    assert "database_path" not in json.dumps(b)           # no internal config leaks


def test_stocks_directory_search_pagination(seeded):
    c, *_ = seeded
    b = c.get("/api/v1/stocks").json()
    assert b["pagination"] == {"total": 4, "limit": 100, "offset": 0, "returned": 4, "has_more": False}
    assert [s["symbol"] for s in b["data"]] == ["COMI", "ETEL", "NOPX", "ZERO"]
    b = c.get("/api/v1/stocks", params={"limit": 2, "offset": 1}).json()
    assert [s["symbol"] for s in b["data"]] == ["ETEL", "NOPX"] and b["pagination"]["has_more"] is True
    assert [s["symbol"] for s in c.get("/api/v1/stocks", params={"search": "telecom"}).json()["data"]] == ["ETEL"]
    assert [s["symbol"] for s in c.get("/api/v1/stocks", params={"search": "البنك"}).json()["data"]] == ["COMI"]


def test_stock_detail_and_symbol_normalisation(seeded):
    c, *_ = seeded
    for path in ("COMI", "comi", "COMI.CA", " comi "):
        r = c.get(f"/api/v1/stocks/{path}")
        assert r.status_code == 200 and r.json()["data"]["quote"]["last_price"] == 125.15
    r = c.get("/api/v1/stocks/NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "symbol_not_found"


def test_quotes_list_schema_and_freshness(seeded):
    c, *_ = seeded
    b = c.get("/api/v1/quotes").json()
    q = {x["symbol"]: x for x in b["data"]}["COMI"]
    expected = {"symbol", "isin", "company_name", "company_name_ar", "sector", "sector_ar", "exchange", "currency",
                "last_price", "close_price", "previous_close", "change", "change_percent", "open", "high", "low", "volume",
                "traded_value", "number_of_trades", "bid", "ask", "last_trade_date", "source_timestamp", "fetched_at",
                "symbol_type", "type_name", "data_status", "status_reason", "age_seconds"}
    assert set(q) == expected and q["exchange"] == "EGX" and q["currency"] == "EGP"
    assert q["data_status"] == "fresh" and q["source_timestamp"] == "2026-10-22T08:04:00Z" and q["change"] is None
    assert b["meta"]["data_timing"] == "unverified" and b["meta"]["dataset_complete"] is True


def test_null_is_not_zero_and_zero_is_not_null(seeded):
    c, *_ = seeded
    d = {x["symbol"]: x for x in c.get("/api/v1/quotes").json()["data"]}
    assert d["NOPX"]["last_price"] is None and d["NOPX"]["data_status"] == "unavailable"
    assert d["ZERO"]["last_price"] == 0.0 and d["ZERO"]["volume"] == 0 and d["ZERO"]["data_status"] == "fresh"


def test_quotes_sorting_and_validation(seeded):
    c, *_ = seeded
    syms = lambda **p: [x["symbol"] for x in c.get("/api/v1/quotes", params=p).json()["data"]]
    assert syms(sort_by="last_price", order="desc") == ["ETEL", "COMI", "ZERO", "NOPX"]    # null last
    assert syms(sort_by="traded_value", order="asc")[0] == "ZERO"
    for bad in ({"sort_by": "password"}, {"order": "sideways"}, {"limit": 0}, {"limit": -1}, {"offset": -1},
                {"limit": 501}, {"limit": "abc"}):
        r = c.get("/api/v1/quotes", params=bad)
        assert r.status_code == 422 and "error" in r.json(), bad
    assert c.get("/api/v1/quotes", params={"sort_by": "x"}).json()["error"]["code"] == "validation_error"
    assert c.get("/api/v1/quotes", params={"limit": 501}).json()["error"]["code"] == "limit_too_large"


def test_single_quote_and_404(seeded):
    c, *_ = seeded
    assert c.get("/api/v1/quotes/etel").json()["data"]["last_price"] == 133.9
    r = c.get("/api/v1/quotes/NOPE")
    assert r.status_code == 404 and set(r.json()["error"]) >= {"code", "message"}


def test_history_range_pagination_and_honest_note(seeded):
    c, app, repo = seeded
    for i in range(1, 4):
        t = TS + timedelta(minutes=10 * i)
        repo.apply_collection([mkq("COMI", "E1", 125.15 + i, "Commercial International Bank", ts=t)],
                              RunRecord(started_at=t, finished_at=t, status="success", complete=True, stock_count=1))
    b = c.get("/api/v1/quotes/COMI/history").json()
    assert b["pagination"]["total"] == 4 and b["history_enabled"] is True
    assert "before installation" in b["note"] and b["earliest_local_observation"].startswith("2026-10-22T08:04")
    r = c.get("/api/v1/quotes/COMI/history", params={"start": (TS + timedelta(minutes=5)).isoformat(), "limit": 2})
    assert [p["last_price"] for p in r.json()["data"]] == [126.15, 127.15] and r.json()["pagination"]["has_more"] is True
    r = c.get("/api/v1/quotes/COMI/history", params={"start": "2026-10-23T00:00:00", "end": "2026-10-22T00:00:00"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_range"
    assert c.get("/api/v1/quotes/COMI/history", params={"start": "not-a-date"}).status_code == 422
    assert c.get("/api/v1/quotes/NOPE/history").status_code == 404


def test_market_summary_is_labelled_estimate(seeded):
    c, *_ = seeded
    b = c.get("/api/v1/market/summary").json()
    assert b["official"] is False and b["source_provides_summary"] is False and "ESTIMATE" in b["basis"]
    e = b["estimate"]
    assert e["instruments_in_feed"] == 4 and e["instruments_with_price"] == 3
    assert (e["advancers"], e["decliners"], e["unchanged"]) == (1, 1, 1)
    assert e["total_traded_value"] == pytest.approx(648266144.71 + 590673851.73) and e["records_missing_traded_value"] == 1


def test_stale_data_is_marked_stale_not_fresh(seeded):
    c, app, _ = seeded
    app.state.service._clock = lambda: NOW + timedelta(hours=1)       # source stopped updating
    d = c.get("/api/v1/quotes/COMI").json()["data"]
    assert d["data_status"] == "stale" and d["last_price"] == 125.15   # kept, but not presented as current
    assert c.get("/api/v1/status").json()["data_freshness"] == "stale"
    assert c.get("/health").json()["status"] == "degraded"


def test_failed_collection_is_visible_but_keeps_data(seeded):
    c, _, repo = seeded
    repo.record_failed_run(RunRecord(started_at=NOW, finished_at=NOW, status="failed", error="source error: HTTP 503"))
    s = c.get("/api/v1/status").json()
    assert s["source_connectivity"] == "error" and s["most_recent_error"] == "source error: HTTP 503"
    assert c.get("/api/v1/quotes/COMI").json()["data"]["last_price"] == 125.15


def test_partial_dataset_is_reported(settings, repo):
    repo.apply_collection([mkq("COMI", "E1", 1.0, "X")], RunRecord(
        started_at=NOW, finished_at=NOW, status="partial", complete=False, stock_count=1, source_timestamp_max=TS))
    app = create_app(settings); app.state.service._clock = lambda: NOW
    with TestClient(app) as c:
        s = c.get("/api/v1/status").json()
        assert s["dataset_state"] == "partial" and s["dataset_complete"] is False
        assert any("PARTIAL" in n for n in c.get("/api/v1/quotes").json()["meta"]["notes"])


def test_empty_database_behaviour(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        b = c.get("/api/v1/quotes").json()
        assert b["data"] == [] and b["pagination"]["total"] == 0 and b["meta"]["last_successful_fetch"] is None
        s = c.get("/api/v1/status").json()
        assert s["dataset_state"] == "empty" and s["data_freshness"] == "unavailable"
        assert c.get("/api/v1/market/summary").status_code == 503
        assert c.get("/health").status_code == 200


def test_no_endpoint_claims_live_or_realtime(seeded):
    c, *_ = seeded
    for path in ("/api/v1/quotes", "/api/v1/status", "/api/v1/stocks/COMI", "/api/v1/market/summary"):
        text = c.get(path).text
        assert '"live"' not in text.lower() and '"realtime"' not in text.lower() and '"real_time"' not in text.lower()


def test_documented_endpoints_in_openapi_and_docs(seeded):
    c, *_ = seeded
    paths = set(c.get("/openapi.json").json()["paths"])
    assert {"/health", "/api/v1/status", "/api/v1/stocks", "/api/v1/stocks/{symbol}", "/api/v1/quotes",
            "/api/v1/quotes/{symbol}", "/api/v1/quotes/{symbol}/history", "/api/v1/market/summary"} <= paths
    assert c.get("/docs").status_code == 200


def test_unknown_route_and_method_use_structured_errors(seeded):
    c, *_ = seeded
    assert c.get("/nope").json()["error"]["code"] == "not_found"
    r = c.post("/api/v1/quotes")
    assert r.status_code == 405 and "error" in r.json()


def test_cors_is_off_by_default_and_configurable(settings, seeded):
    c, *_ = seeded
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "http://x.test"}).headers
    app = create_app(dataclasses.replace(settings, cors_origins=("http://localhost:3000",)))
    with TestClient(app) as c2:
        h = c2.get("/health", headers={"Origin": "http://localhost:3000"}).headers
        assert h["access-control-allow-origin"] == "http://localhost:3000"


def test_app_lifespan_starts_polls_and_shuts_down(settings):
    """Whole stack with a scripted (SYNTHETIC) collector: startup -> poll -> data served -> clean shutdown."""
    import time
    from tests.test_polling import Script, result, quote
    from app.utils import utcnow
    now = utcnow()
    q = QuoteBase(symbol="LIVE", isin="EGSLIVE", last_price=5.0, source_timestamp=now, fetched_at=now)
    app = create_app(dataclasses.replace(settings, polling_enabled=True, poll_min_interval=1, poll_interval_open=60),
                     collector=Script(result([q])))
    with TestClient(app) as c:
        for _ in range(50):
            if c.get("/api/v1/quotes").json()["pagination"]["total"]:
                break
            time.sleep(0.1)
        body = c.get("/api/v1/quotes/LIVE").json()
        assert body["data"]["last_price"] == 5.0
        assert c.get("/api/v1/status").json()["polling_enabled"] is True
