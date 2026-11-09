"""All SQL lives here. Parameterised statements only; sort columns come from a whitelist."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.database.connection import connect, transaction
from app.database.models import MIGRATIONS, SCHEMA
from app.schemas.stock import QuoteBase, StockEntry
from app.utils import iso_utc, parse_iso

SORTABLE = {
    "symbol": "s.symbol",
    "company_name": "s.company_name",
    "last_price": "q.last_price",
    "close_price": "q.close_price",
    "previous_close": "q.previous_close",
    "change_percent": "q.change_percent",
    "open": "q.open",
    "high": "q.high",
    "low": "q.low",
    "volume": "q.volume",
    "traded_value": "q.traded_value",
    "number_of_trades": "q.number_of_trades",
    "source_timestamp": "q.source_timestamp",
}

_QUOTE_SELECT = """
SELECT s.symbol, s.isin, s.company_name, s.company_name_ar, s.sector, s.sector_ar, s.symbol_type, s.type_name, s.exchange,
       q.currency, q.last_price, q.close_price, q.previous_close, q.change, q.change_percent, q.open, q.high, q.low,
       q.volume, q.traded_value, q.number_of_trades, q.bid, q.ask, q.last_trade_date,
       q.source_timestamp, q.fetched_at
FROM quotes_latest q JOIN stocks s ON s.stock_key = q.stock_key
"""


@dataclass
class RunRecord:
    started_at: datetime
    finished_at: datetime
    status: str                      # success | partial | failed
    complete: Optional[bool] = None
    stock_count: int = 0
    pages: int = 0
    requests: int = 0
    retries: int = 0
    rejected: int = 0
    duplicates: int = 0
    total_reported: Optional[int] = None
    duration_ms: int = 0
    source_timestamp_max: Optional[datetime] = None
    market_status: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BarPolicy:
    """When a snapshot may create/update a daily bar (hours are configuration, not verified)."""
    tz: str = "Africa/Cairo"
    session_open: dtime = dtime(10, 0)
    session_close: dtime = dtime(14, 30)
    trading_weekdays: Tuple[int, ...] = (6, 0, 1, 2, 3)


@dataclass
class ApplyResult:
    upserted: int = 0
    skipped_older: int = 0
    symbol_conflicts: int = 0
    history_rows: int = 0
    bars_written: int = 0


def _like(term: str) -> str:
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _num(v: Optional[float]) -> Optional[Any]:
    if v is None:
        return None
    return int(v) if float(v).is_integer() else v


class Repository:
    def __init__(self, db_path: Path, *, history_enabled: bool = True, history_min_interval: float = 300.0,
                 bar_policy: Optional[BarPolicy] = None):
        self.db_path = Path(db_path)
        self.history_enabled = history_enabled
        self.history_min_interval = history_min_interval
        self.bars = bar_policy or BarPolicy()
        self._tz = ZoneInfo(self.bars.tz)

    @classmethod
    def from_settings(cls, s) -> "Repository":
        return cls(s.database_path, history_enabled=s.history_enabled,
                   history_min_interval=s.history_min_interval_seconds,
                   bar_policy=BarPolicy(s.market_timezone, s.market_open, s.market_close, s.trading_weekdays))

    # ------------------------------------------------------------ lifecycle
    def init_schema(self) -> None:
        with connect(self.db_path) as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            for table, column, decl in MIGRATIONS:      # upgrade databases created by an older release
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def ping(self) -> bool:
        try:
            with connect(self.db_path) as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------ writes
    def _retry_locked(self, fn):
        for attempt in range(3):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))

    def apply_collection(self, quotes: Sequence[QuoteBase], run: RunRecord) -> ApplyResult:
        """Upsert all quotes and log the run in ONE transaction. A failure leaves the previous data intact."""
        return self._retry_locked(lambda: self._apply(quotes, run))

    def _apply(self, quotes: Sequence[QuoteBase], run: RunRecord) -> ApplyResult:
        res = ApplyResult()
        now_iso = iso_utc(run.finished_at)
        with connect(self.db_path) as conn:
            with transaction(conn):
                for q in quotes:
                    key = q.key()
                    clash = conn.execute("SELECT stock_key FROM stocks WHERE symbol = ?", (q.symbol,)).fetchone()
                    if clash and clash["stock_key"] != key:
                        res.symbol_conflicts += 1
                        continue
                    new_ts = iso_utc(q.source_timestamp) if q.source_timestamp else None
                    prev = conn.execute(
                        "SELECT source_timestamp FROM quotes_latest WHERE stock_key = ?", (key,)
                    ).fetchone()
                    if prev and prev["source_timestamp"] and new_ts and new_ts < prev["source_timestamp"]:
                        res.skipped_older += 1   # never let an older observation replace a newer one
                        continue
                    conn.execute(
                        """
                        INSERT INTO stocks(stock_key, symbol, isin, company_name, company_name_ar, sector, sector_ar,
                                           symbol_type, type_name, exchange, first_seen_at, last_seen_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(stock_key) DO UPDATE SET
                            symbol = excluded.symbol,
                            isin = COALESCE(excluded.isin, stocks.isin),
                            company_name = COALESCE(excluded.company_name, stocks.company_name),
                            company_name_ar = COALESCE(excluded.company_name_ar, stocks.company_name_ar),
                            sector = COALESCE(excluded.sector, stocks.sector),
                            sector_ar = COALESCE(excluded.sector_ar, stocks.sector_ar),
                            symbol_type = COALESCE(excluded.symbol_type, stocks.symbol_type),
                            type_name = COALESCE(excluded.type_name, stocks.type_name),
                            last_seen_at = excluded.last_seen_at
                        """,
                        (key, q.symbol, q.isin, q.company_name, q.company_name_ar, q.sector, q.sector_ar,
                         q.symbol_type, q.type_name, q.exchange, now_iso, now_iso),
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO quotes_latest(
                            stock_key, currency, last_price, close_price, previous_close, change, change_percent, open,
                            high, low, volume, traded_value, number_of_trades, bid, ask, last_trade_date,
                            source_timestamp, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (key, q.currency, q.last_price, q.close_price, q.previous_close, q.change, q.change_percent,
                         q.open, q.high, q.low, q.volume, q.traded_value, q.number_of_trades, q.bid, q.ask,
                         q.last_trade_date.isoformat() if q.last_trade_date else None,
                         new_ts, iso_utc(q.fetched_at)),
                    )
                    res.upserted += 1
                    if self.history_enabled and new_ts:
                        res.history_rows += self._maybe_history(conn, key, q, new_ts)
                    if new_ts:
                        res.bars_written += self._upsert_bar(conn, key, q, new_ts, now_iso)
                self._insert_run(conn, run, res)
        return res

    def _maybe_history(self, conn: sqlite3.Connection, key: str, q: QuoteBase, new_ts: str) -> int:
        last = conn.execute(
            "SELECT MAX(source_timestamp) AS ts FROM quote_history WHERE stock_key = ?", (key,)
        ).fetchone()["ts"]
        if last:
            gap = (parse_iso(new_ts) - parse_iso(last)).total_seconds()  # type: ignore[operator]
            if gap < self.history_min_interval:
                return 0
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO quote_history(stock_key, source_timestamp, fetched_at, last_price, previous_close,
                change_percent, open, high, low, volume, traded_value, number_of_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (key, new_ts, iso_utc(q.fetched_at), q.last_price, q.previous_close, q.change_percent, q.open, q.high,
             q.low, q.volume, q.traded_value, q.number_of_trades),
        )
        return cur.rowcount

    def _insert_run(self, conn: sqlite3.Connection, run: RunRecord, res: Optional[ApplyResult] = None) -> None:
        conn.execute(
            """
            INSERT INTO collection_runs(started_at, finished_at, status, complete, stock_count, pages, requests,
                retries, rejected, duplicates, skipped_older, symbol_conflicts, total_reported, duration_ms,
                source_timestamp_max, market_status, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (iso_utc(run.started_at), iso_utc(run.finished_at), run.status,
             None if run.complete is None else int(run.complete), run.stock_count, run.pages, run.requests,
             run.retries, run.rejected, run.duplicates, res.skipped_older if res else 0,
             res.symbol_conflicts if res else 0, run.total_reported, run.duration_ms,
             iso_utc(run.source_timestamp_max) if run.source_timestamp_max else None, run.market_status, run.error),
        )

    def record_failed_run(self, run: RunRecord) -> None:
        """Log a failure WITHOUT touching stocks / quotes (last known good data is preserved)."""
        def op():
            with connect(self.db_path) as conn:
                with transaction(conn):
                    self._insert_run(conn, run)
        self._retry_locked(op)

    # ------------------------------------------------------------ daily OHLCV bars
    def _bar_date(self, ts_iso: str) -> Optional[str]:
        """Trade date = Cairo calendar date of the source's writeTime, only for snapshots written inside a
        trading day at/after the session open. (The source's lastTradeDate is the PREVIOUS session, not this one.)"""
        local = parse_iso(ts_iso).astimezone(self._tz)  # type: ignore[union-attr]
        if local.weekday() not in self.bars.trading_weekdays or local.time() < self.bars.session_open:
            return None
        return local.date().isoformat()

    def _upsert_bar(self, conn: sqlite3.Connection, key: str, q: QuoteBase, ts_iso: str, now_iso: str) -> int:
        trade_date = self._bar_date(ts_iso)
        if trade_date is None or q.last_price is None:
            return 0
        final = int(parse_iso(ts_iso).astimezone(self._tz).time() >= self.bars.session_close)  # type: ignore[union-attr]
        prev = conn.execute("SELECT source_timestamp, open_observed, is_final FROM daily_bars WHERE stock_key=? AND trade_date=?",
                            (key, trade_date)).fetchone()
        if prev and prev["source_timestamp"] > ts_iso:
            return 0
        observed = prev["open_observed"] if prev and prev["open_observed"] is not None else q.last_price
        final = max(final, prev["is_final"]) if prev else final
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_bars(stock_key, trade_date, open, open_observed, high, low, close, last_price,
                previous_close, volume, traded_value, number_of_trades, currency, is_final, source_timestamp, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (key, trade_date, q.open, observed, q.high, q.low, q.close_price, q.last_price, q.previous_close, q.volume,
             q.traded_value, q.number_of_trades, q.currency, final, ts_iso, now_iso),
        )
        return 1

    @staticmethod
    def _bar_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["source_timestamp"] = parse_iso(d["source_timestamp"])
        d["trade_date"] = date.fromisoformat(d["trade_date"])
        d["volume"] = _num(d["volume"])
        d["is_final"] = bool(d["is_final"])
        d.pop("updated_at", None)
        return d

    _BAR_SELECT = (
        "SELECT s.symbol, s.isin, s.company_name, b.trade_date, b.open, b.open_observed, b.high, b.low, b.close, "
        "b.last_price, b.previous_close, b.volume, b.traded_value, b.number_of_trades, b.currency, b.is_final, "
        "b.source_timestamp, b.updated_at FROM daily_bars b JOIN stocks s ON s.stock_key = b.stock_key"
    )

    def latest_bar_date(self) -> Optional[date]:
        with connect(self.db_path) as conn:
            r = conn.execute("SELECT MAX(trade_date) AS d FROM daily_bars").fetchone()["d"]
        return date.fromisoformat(r) if r else None

    def list_daily_bars(self, trade_date: date, search: Optional[str], limit: int, offset: int,
                        ) -> Tuple[List[Dict[str, Any]], int]:
        where, params = self._search_clause(search)
        where = (where + " AND" if where else " WHERE") + " b.trade_date = ?"
        params = [*params, trade_date.isoformat()]
        with connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM daily_bars b JOIN stocks s ON s.stock_key = b.stock_key"
                                 + where, params).fetchone()["n"]
            rows = conn.execute(f"{self._BAR_SELECT}{where} ORDER BY s.symbol ASC LIMIT ? OFFSET ?",
                                [*params, limit, offset]).fetchall()
        return [self._bar_row(r) for r in rows], total

    def symbol_daily_bars(self, symbol: str, start: Optional[date], end: Optional[date], limit: int, offset: int,
                          ) -> Tuple[List[Dict[str, Any]], int]:
        clauses, params = ["s.symbol = ?"], [symbol]
        if start:
            clauses.append("b.trade_date >= ?"); params.append(start.isoformat())
        if end:
            clauses.append("b.trade_date <= ?"); params.append(end.isoformat())
        where = " WHERE " + " AND ".join(clauses)
        with connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM daily_bars b JOIN stocks s ON s.stock_key = b.stock_key"
                                 + where, params).fetchone()["n"]
            rows = conn.execute(f"{self._BAR_SELECT}{where} ORDER BY b.trade_date ASC LIMIT ? OFFSET ?",
                                [*params, limit, offset]).fetchall()
        return [self._bar_row(r) for r in rows], total

    def purge_history(self, retention_days: int, now: datetime) -> int:
        cutoff = iso_utc(now - timedelta(days=retention_days))
        with connect(self.db_path) as conn:
            with transaction(conn):
                cur = conn.execute("DELETE FROM quote_history WHERE source_timestamp < ?", (cutoff,))
                conn.execute(
                    "DELETE FROM collection_runs WHERE finished_at < ?", (cutoff,)
                )
                return cur.rowcount

    # ------------------------------------------------------------ lease (multi-process guard)
    def try_acquire_lease(self, owner: str, ttl_seconds: float, now: datetime) -> bool:
        with connect(self.db_path) as conn:
            with transaction(conn):
                row = conn.execute("SELECT owner, expires_at FROM poller_lease WHERE id = 1").fetchone()
                if row is None or row["owner"] == owner or row["expires_at"] <= iso_utc(now):
                    conn.execute(
                        "INSERT OR REPLACE INTO poller_lease(id, owner, expires_at) VALUES (1, ?, ?)",
                        (owner, iso_utc(now + timedelta(seconds=ttl_seconds))),
                    )
                    return True
                return False

    def release_lease(self, owner: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM poller_lease WHERE id = 1 AND owner = ?", (owner,))

    # ------------------------------------------------------------ reads
    @staticmethod
    def _row_to_quote(r: sqlite3.Row) -> QuoteBase:
        return QuoteBase(
            symbol=r["symbol"], isin=r["isin"], company_name=r["company_name"], company_name_ar=r["company_name_ar"],
            sector=r["sector"], sector_ar=r["sector_ar"], symbol_type=r["symbol_type"], type_name=r["type_name"],
            exchange=r["exchange"], currency=r["currency"],
            last_price=r["last_price"], close_price=r["close_price"], previous_close=r["previous_close"], change=r["change"],
            change_percent=r["change_percent"], open=r["open"], high=r["high"], low=r["low"],
            volume=_num(r["volume"]), traded_value=r["traded_value"], number_of_trades=r["number_of_trades"],
            bid=r["bid"], ask=r["ask"],
            last_trade_date=date.fromisoformat(r["last_trade_date"]) if r["last_trade_date"] else None,
            source_timestamp=parse_iso(r["source_timestamp"]), fetched_at=parse_iso(r["fetched_at"]),
        )

    @staticmethod
    def _search_clause(search: Optional[str]) -> Tuple[str, List[Any]]:
        if not search or not search.strip():
            return "", []
        t = _like(search.strip())
        return (
            " WHERE (s.symbol LIKE ? ESCAPE '\\' OR s.company_name LIKE ? ESCAPE '\\' "
            "OR s.company_name_ar LIKE ? ESCAPE '\\' OR s.isin LIKE ? ESCAPE '\\')",
            [t, t, t, t],
        )

    def list_quotes(self, search: Optional[str], limit: int, offset: int,
                    sort_by: str = "symbol", order: str = "asc") -> Tuple[List[QuoteBase], int]:
        col = SORTABLE[sort_by]                      # KeyError -> programming error, validated at the API layer
        direction = "DESC" if order == "desc" else "ASC"
        where, params = self._search_clause(search)
        with connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM quotes_latest q JOIN stocks s ON s.stock_key = q.stock_key" + where, params
            ).fetchone()["n"]
            rows = conn.execute(
                f"{_QUOTE_SELECT}{where} ORDER BY ({col} IS NULL), {col} {direction}, s.symbol ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._row_to_quote(r) for r in rows], total

    def get_quote(self, symbol: str) -> Optional[QuoteBase]:
        with connect(self.db_path) as conn:
            r = conn.execute(_QUOTE_SELECT + " WHERE s.symbol = ?", (symbol,)).fetchone()
        return self._row_to_quote(r) if r else None

    def list_all_quotes(self) -> List[QuoteBase]:
        with connect(self.db_path) as conn:
            rows = conn.execute(_QUOTE_SELECT).fetchall()
        return [self._row_to_quote(r) for r in rows]

    @staticmethod
    def _row_to_stock(r: sqlite3.Row) -> StockEntry:
        return StockEntry(
            symbol=r["symbol"], isin=r["isin"], company_name=r["company_name"], company_name_ar=r["company_name_ar"],
            sector=r["sector"], sector_ar=r["sector_ar"], symbol_type=r["symbol_type"], type_name=r["type_name"],
            exchange=r["exchange"], first_seen_at=parse_iso(r["first_seen_at"]), last_seen_at=parse_iso(r["last_seen_at"]),
        )

    def list_stocks(self, search: Optional[str], limit: int, offset: int) -> Tuple[List[StockEntry], int]:
        where, params = self._search_clause(search)
        with connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM stocks s" + where, params).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM stocks s" + where + " ORDER BY s.symbol ASC LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()
        return [self._row_to_stock(r) for r in rows], total

    def get_stock(self, symbol: str) -> Optional[StockEntry]:
        with connect(self.db_path) as conn:
            r = conn.execute("SELECT * FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_stock(r) if r else None

    def history(self, symbol: str, start: Optional[datetime], end: Optional[datetime],
                limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int, Optional[datetime]]:
        clauses, params = ["s.symbol = ?"], [symbol]
        if start:
            clauses.append("h.source_timestamp >= ?"); params.append(iso_utc(start))
        if end:
            clauses.append("h.source_timestamp <= ?"); params.append(iso_utc(end))
        where = " WHERE " + " AND ".join(clauses)
        base = " FROM quote_history h JOIN stocks s ON s.stock_key = h.stock_key"
        with connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS n" + base + where, params).fetchone()["n"]
            earliest = conn.execute(
                "SELECT MIN(h.source_timestamp) AS t" + base + " WHERE s.symbol = ?", (symbol,)
            ).fetchone()["t"]
            rows = conn.execute(
                "SELECT h.*" + base + where + " ORDER BY h.source_timestamp ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_timestamp"] = parse_iso(d["source_timestamp"])
            d["fetched_at"] = parse_iso(d["fetched_at"])
            d["volume"] = _num(d["volume"])
            out.append(d)
        return out, total, parse_iso(earliest)

    # ------------------------------------------------------------ run diagnostics
    @staticmethod
    def _run(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if r is None:
            return None
        d = dict(r)
        for k in ("started_at", "finished_at", "source_timestamp_max"):
            d[k] = parse_iso(d[k])
        d["complete"] = None if d["complete"] is None else bool(d["complete"])
        return d

    def _one_run(self, where: str = "") -> Optional[Dict[str, Any]]:
        with connect(self.db_path) as conn:
            r = conn.execute(f"SELECT * FROM collection_runs {where} ORDER BY id DESC LIMIT 1").fetchone()
        return self._run(r)

    def latest_run(self) -> Optional[Dict[str, Any]]:
        return self._one_run()

    def latest_data_run(self) -> Optional[Dict[str, Any]]:
        """Latest run that stored data (success or partial)."""
        return self._one_run("WHERE status IN ('success','partial')")

    def latest_complete_run(self) -> Optional[Dict[str, Any]]:
        return self._one_run("WHERE status = 'success'")

    def count_stocks(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM stocks").fetchone()["n"]

    def count_quotes(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM quotes_latest").fetchone()["n"]

    def count_daily_bars(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM daily_bars").fetchone()["n"]

