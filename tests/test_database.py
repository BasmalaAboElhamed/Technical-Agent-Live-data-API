from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.database.repository import RunRecord
from app.schemas.stock import QuoteBase
from app.database.connection import connect
from tests.fixtures import utc

T0 = utc(2026, 10, 22, 12, 38)      # 15:38 Cairo
NOW = utc(2026, 10, 22, 12, 40)


def q(symbol="COMI", isin="EGS60121C018", price=125.15, ts=T0, **kw):
    return QuoteBase(symbol=symbol, isin=isin, company_name=kw.pop("company_name", f"{symbol} Co"),
                     last_price=price, source_timestamp=ts, fetched_at=NOW, **kw)


def run_rec(status="success", n=1, finished=NOW, **kw):
    return RunRecord(started_at=finished - timedelta(seconds=2), finished_at=finished, status=status,
                     complete=status == "success", stock_count=n, **kw)


def count(repo, table):
    with connect(repo.db_path) as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def test_insert_then_update_does_not_duplicate(repo):
    repo.apply_collection([q()], run_rec())
    repo.apply_collection([q(price=126.0, ts=T0 + timedelta(minutes=10))], run_rec(finished=NOW + timedelta(minutes=10)))
    assert count(repo, "stocks") == 1 and count(repo, "quotes_latest") == 1
    assert repo.get_quote("COMI").last_price == 126.0


def test_null_and_zero_round_trip(repo):
    repo.apply_collection([q(volume=0, open=None, number_of_trades=0)], run_rec())
    got = repo.get_quote("COMI")
    assert got.volume == 0 and got.number_of_trades == 0 and got.open is None and got.bid is None


def test_older_observation_never_replaces_newer(repo):
    repo.apply_collection([q(price=130, ts=T0)], run_rec())
    res = repo.apply_collection([q(price=1, ts=T0 - timedelta(hours=1))], run_rec())
    assert res.skipped_older == 1 and repo.get_quote("COMI").last_price == 130


def test_symbol_conflict_between_different_isins_is_not_merged(repo):
    repo.apply_collection([q()], run_rec())
    res = repo.apply_collection([q(isin="EGS00000C000", price=1)], run_rec())
    assert res.symbol_conflicts == 1 and count(repo, "stocks") == 1 and repo.get_quote("COMI").last_price == 125.15


def test_failed_run_preserves_last_known_good_data(repo):
    repo.apply_collection([q()], run_rec())
    repo.record_failed_run(run_rec("failed", n=0, error="boom"))
    assert repo.get_quote("COMI").last_price == 125.15
    assert repo.latest_run()["status"] == "failed" and repo.latest_data_run()["status"] == "success"


def test_failed_transaction_rolls_back_everything(repo, monkeypatch):
    repo.apply_collection([q()], run_rec())
    boom = [q(symbol="AAA", isin="EGS1"), q(symbol="BBB", isin="EGS2")]
    orig = repo._maybe_history
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.DatabaseError("disk")
        return orig(*a, **k)
    monkeypatch.setattr(repo, "_maybe_history", flaky)
    with pytest.raises(sqlite3.DatabaseError):
        repo.apply_collection(boom, run_rec(n=2))
    assert count(repo, "stocks") == 1 and count(repo, "collection_runs") == 1


def test_history_throttled_and_deduplicated(repo):
    repo.apply_collection([q(ts=T0)], run_rec())
    repo.apply_collection([q(ts=T0)], run_rec())                                # same timestamp
    repo.apply_collection([q(ts=T0 + timedelta(seconds=60))], run_rec())        # inside min interval
    repo.apply_collection([q(ts=T0 + timedelta(seconds=400))], run_rec())       # outside
    assert count(repo, "quote_history") == 2


def test_history_query_range_and_pagination(repo):
    for i in range(5):
        repo.apply_collection([q(ts=T0 + timedelta(minutes=10 * i), price=100 + i)], run_rec())
    rows, total, earliest = repo.history("COMI", T0 + timedelta(minutes=10), T0 + timedelta(minutes=30), 2, 0)
    assert total == 3 and [r["last_price"] for r in rows] == [101, 102] and earliest == T0
    rows2, _, _ = repo.history("COMI", None, None, 10, 4)
    assert [r["last_price"] for r in rows2] == [104]


def test_history_disabled_stores_nothing(repo):
    repo.history_enabled = False
    repo.apply_collection([q()], run_rec())
    assert count(repo, "quote_history") == 0


def test_purge_history(repo):
    repo.apply_collection([q(ts=T0 - timedelta(days=40))], run_rec(finished=NOW - timedelta(days=40)))
    repo.apply_collection([q(ts=T0)], run_rec())
    assert repo.purge_history(30, NOW) == 1 and count(repo, "quote_history") == 1


def test_search_symbol_name_arabic_and_escaping(repo):
    repo.apply_collection([
        q("COMI", "E1", company_name="Commercial International Bank", company_name_ar="البنك التجاري الدولي"),
        q("ETEL", "E2", company_name="Telecom Egypt", company_name_ar="المصرية للاتصالات"),
        q("A_B", "E3", company_name="100% Co"),
    ], run_rec(n=3))
    assert [x.symbol for x in repo.list_quotes("comi", 10, 0)[0]] == ["COMI"]
    assert [x.symbol for x in repo.list_quotes("للاتصالات", 10, 0)[0]] == ["ETEL"]
    assert [x.symbol for x in repo.list_quotes("100%", 10, 0)[0]] == ["A_B"]
    assert repo.list_quotes("%", 10, 0)[1] == 1                                  # '%' is literal, not a wildcard
    assert repo.list_quotes("x'; DROP TABLE stocks;--", 10, 0)[1] == 0 and repo.count_stocks() == 3


def test_sort_puts_nulls_last_and_paginates(repo):
    repo.apply_collection([q("A", "E1", price=3), q("B", "E2", price=None), q("C", "E3", price=1)], run_rec(n=3))
    asc = [x.symbol for x in repo.list_quotes(None, 10, 0, "last_price", "asc")[0]]
    desc = [x.symbol for x in repo.list_quotes(None, 10, 0, "last_price", "desc")[0]]
    assert asc == ["C", "A", "B"] and desc == ["A", "C", "B"]
    assert [x.symbol for x in repo.list_quotes(None, 1, 1, "symbol", "asc")[0]] == ["B"]
    with pytest.raises(KeyError):
        repo.list_quotes(None, 1, 0, "price; DROP TABLE stocks", "asc")


def test_unique_symbol_constraint_exists(repo):
    with connect(repo.db_path) as c:
        sql = "INSERT INTO stocks(stock_key, symbol, first_seen_at, last_seen_at) VALUES (?, 'X', 'a', 'b')"
        c.execute(sql, ('k1',))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, ('k2',))


def test_lease_blocks_second_owner_until_expiry(repo):
    assert repo.try_acquire_lease("a", 60, NOW)
    assert not repo.try_acquire_lease("b", 60, NOW + timedelta(seconds=10))
    assert repo.try_acquire_lease("a", 60, NOW + timedelta(seconds=10))          # owner renews
    assert repo.try_acquire_lease("b", 60, NOW + timedelta(seconds=300))         # expired
    repo.release_lease("b")
    assert repo.try_acquire_lease("c", 60, NOW + timedelta(seconds=301))
