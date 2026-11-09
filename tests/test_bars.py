from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi.testclient import TestClient

from app.database.connection import connect
from app.database.models import SCHEMA
from app.database.repository import Repository, RunRecord
from app.main import create_app
from app.schemas.stock import QuoteBase
from tests.fixtures import utc

# Cairo is UTC+3 on these dates. Sunday 2026-10-25, Thursday 2026-10-22, Friday 2026-10-23.
SUN_1534 = utc(2026, 10, 25, 12, 34)      # 15:34 Cairo, after close  -> final bar
SUN_1130 = utc(2026, 10, 25, 8, 30)       # 11:30 Cairo, in session   -> non-final
FRI_1200 = utc(2026, 10, 23, 9, 0)        # Friday: no session
SUN_0900 = utc(2026, 10, 25, 6, 0)        # 09:00 Cairo: before open


def q(ts, price=100.0, close=101.0, high=105.0, low=95.0, vol=1000, sym="COMI", isin="E1", **kw):
    return QuoteBase(symbol=sym, isin=isin, company_name=f"{sym} Co", last_price=price, close_price=close, high=high,
                     low=low, previous_close=99.0, volume=vol, traded_value=1e6, number_of_trades=10, currency="EGP",
                     source_timestamp=ts, fetched_at=ts, **kw)


def apply(repo, quotes):
    now = max(x.fetched_at for x in quotes)
    return repo.apply_collection(quotes, RunRecord(started_at=now, finished_at=now, status="success", complete=True, stock_count=len(quotes)))


def test_bar_uses_cairo_date_of_write_time_and_marks_final(repo):
    apply(repo, [q(SUN_1534)])
    rows, total = repo.list_daily_bars(__import__("datetime").date(2026, 10, 25), None, 10, 0)
    b = rows[0]
    assert total == 1 and str(b["trade_date"]) == "2026-10-25" and b["is_final"] is True
    assert (b["high"], b["low"], b["close"], b["last_price"], b["volume"]) == (105.0, 95.0, 101.0, 100.0, 1000)
    assert b["open"] is None and b["open_observed"] == 100.0           # open stays null; proxy is labelled separately


def test_intraday_updates_replace_bar_keep_first_observed_open_and_finalise(repo):
    apply(repo, [q(SUN_1130, price=100.0, vol=500)])
    apply(repo, [q(SUN_1130 + timedelta(minutes=10), price=103.0, vol=800, high=106.0)])
    rows, _ = repo.symbol_daily_bars("COMI", None, None, 10, 0)
    assert len(rows) == 1 and rows[0]["is_final"] is False
    assert rows[0]["open_observed"] == 100.0 and rows[0]["last_price"] == 103.0 and rows[0]["high"] == 106.0
    apply(repo, [q(SUN_1534, price=104.0, vol=900)])
    rows, _ = repo.symbol_daily_bars("COMI", None, None, 10, 0)
    assert len(rows) == 1 and rows[0]["is_final"] is True and rows[0]["volume"] == 900 and rows[0]["open_observed"] == 100.0


def test_older_snapshot_does_not_overwrite_bar(repo):
    apply(repo, [q(SUN_1534, vol=900)])
    apply(repo, [q(SUN_1130, vol=100)])
    assert repo.symbol_daily_bars("COMI", None, None, 10, 0)[0][0]["volume"] == 900


def test_no_bar_on_non_trading_day_or_before_open(repo):
    res = apply(repo, [q(FRI_1200, sym="A", isin="E2"), q(SUN_0900, sym="B", isin="E3")])
    assert res.bars_written == 0 and repo.latest_bar_date() is None


def test_one_bar_per_day_per_stock_and_history_across_days(repo):
    apply(repo, [q(utc(2026, 10, 22, 12, 38), price=90.0)])       # Thursday 15:38 Cairo
    apply(repo, [q(SUN_1534, price=100.0)])
    rows, total = repo.symbol_daily_bars("COMI", None, None, 10, 0)
    assert total == 2 and [str(r["trade_date"]) for r in rows] == ["2026-10-22", "2026-10-25"]
    assert repo.latest_bar_date().isoformat() == "2026-10-25"


def test_existing_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    with connect(path) as c:                                     # simulate the first-release schema
        c.execute("CREATE TABLE stocks (stock_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, isin TEXT, company_name TEXT, "
                  "company_name_ar TEXT, sector TEXT, sector_ar TEXT, exchange TEXT NOT NULL DEFAULT 'EGX', "
                  "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)")
        c.execute("CREATE TABLE quotes_latest (stock_key TEXT PRIMARY KEY, currency TEXT, last_price REAL, previous_close REAL, "
                  "change REAL, change_percent REAL, open REAL, high REAL, low REAL, volume REAL, traded_value REAL, "
                  "number_of_trades INTEGER, bid REAL, ask REAL, last_trade_date TEXT, source_timestamp TEXT, fetched_at TEXT NOT NULL)")
        c.execute("CREATE TABLE collection_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, "
                  "status TEXT NOT NULL, complete INTEGER, stock_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, "
                  "requests INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, "
                  "duplicates INTEGER NOT NULL DEFAULT 0, skipped_older INTEGER NOT NULL DEFAULT 0, symbol_conflicts INTEGER NOT NULL DEFAULT 0, "
                  "total_reported INTEGER, duration_ms INTEGER NOT NULL DEFAULT 0, source_timestamp_max TEXT, error TEXT)")
    repo = Repository(path)
    repo.init_schema()
    repo.init_schema()                                           # idempotent
    apply(repo, [q(SUN_1534, symbol_type="C")])
    got = repo.get_quote("COMI")
    assert got.close_price == 101.0 and got.symbol_type == "C"


def test_ohlcv_endpoints(settings, repo):
    apply(repo, [q(utc(2026, 10, 22, 12, 38), sym="COMI", isin="E1", price=90.0)])          # chronological order
    apply(repo, [q(SUN_1534, sym="COMI", isin="E1"), q(SUN_1534, sym="ETEL", isin="E2", price=50.0)])
    app = create_app(settings)
    app.state.service._clock = lambda: SUN_1534
    with TestClient(app) as c:
        b = c.get("/api/v1/ohlcv/daily").json()                  # default = latest date
        assert b["trade_date"] == "2026-10-25" and b["pagination"]["total"] == 2
        assert {x["symbol"] for x in b["data"]} == {"COMI", "ETEL"} and any("open` is null" in n for n in b["notes"])
        assert b["data"][0]["open"] is None and b["data"][0]["is_final"] is True and "open_observed" in b["data"][0]
        assert c.get("/api/v1/ohlcv/daily", params={"date": "2026-10-22"}).json()["pagination"]["total"] == 1
        assert c.get("/api/v1/ohlcv/daily", params={"date": "2026-10-25", "search": "etel"}).json()["pagination"]["total"] == 1
        h = c.get("/api/v1/ohlcv/daily/comi").json()
        assert [x["trade_date"] for x in h["data"]] == ["2026-10-22", "2026-10-25"]
        assert c.get("/api/v1/ohlcv/daily/COMI", params={"start": "2026-10-24"}).json()["pagination"]["total"] == 1
        assert c.get("/api/v1/ohlcv/daily/NOPE").status_code == 404
        assert c.get("/api/v1/ohlcv/daily/COMI", params={"start": "2026-10-30", "end": "2026-10-01"}).status_code == 422
        assert c.get("/api/v1/ohlcv/daily", params={"date": "garbage"}).status_code == 422


def test_ohlcv_empty_database_is_honest(settings):
    with TestClient(create_app(settings)) as c:
        b = c.get("/api/v1/ohlcv/daily").json()
        assert b["data"] == [] and b["trade_date"] is None and any("No bars stored" in n for n in b["notes"])
