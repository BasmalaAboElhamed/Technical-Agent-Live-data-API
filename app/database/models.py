"""SQLite schema (plain SQL; the repository layer is the only code that touches it)."""

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS stocks (
        stock_key       TEXT PRIMARY KEY,
        symbol          TEXT NOT NULL,
        isin            TEXT,
        company_name    TEXT,
        company_name_ar TEXT,
        sector          TEXT,
        sector_ar       TEXT,
        symbol_type     TEXT,
        type_name       TEXT,
        exchange        TEXT NOT NULL DEFAULT 'EGX',
        first_seen_at   TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stocks_symbol ON stocks(symbol)",
    "CREATE INDEX IF NOT EXISTS ix_stocks_name ON stocks(company_name)",
    """
    CREATE TABLE IF NOT EXISTS quotes_latest (
        stock_key        TEXT PRIMARY KEY REFERENCES stocks(stock_key) ON DELETE CASCADE,
        currency         TEXT,
        last_price       REAL,
        close_price      REAL,
        previous_close   REAL,
        change           REAL,
        change_percent   REAL,
        open             REAL,
        high             REAL,
        low              REAL,
        volume           REAL,
        traded_value     REAL,
        number_of_trades INTEGER,
        bid              REAL,
        ask              REAL,
        last_trade_date  TEXT,
        source_timestamp TEXT,
        fetched_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quote_history (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_key        TEXT NOT NULL REFERENCES stocks(stock_key) ON DELETE CASCADE,
        source_timestamp TEXT NOT NULL,
        fetched_at       TEXT NOT NULL,
        last_price       REAL,
        previous_close   REAL,
        change_percent   REAL,
        open             REAL,
        high             REAL,
        low              REAL,
        volume           REAL,
        traded_value     REAL,
        number_of_trades INTEGER,
        UNIQUE(stock_key, source_timestamp)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_history_key_ts ON quote_history(stock_key, source_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_history_ts ON quote_history(source_timestamp)",
    """
    CREATE TABLE IF NOT EXISTS collection_runs (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at            TEXT NOT NULL,
        finished_at           TEXT NOT NULL,
        status                TEXT NOT NULL CHECK(status IN ('success','partial','failed')),
        complete              INTEGER,
        stock_count           INTEGER NOT NULL DEFAULT 0,
        pages                 INTEGER NOT NULL DEFAULT 0,
        requests              INTEGER NOT NULL DEFAULT 0,
        retries               INTEGER NOT NULL DEFAULT 0,
        rejected              INTEGER NOT NULL DEFAULT 0,
        duplicates            INTEGER NOT NULL DEFAULT 0,
        skipped_older         INTEGER NOT NULL DEFAULT 0,
        symbol_conflicts      INTEGER NOT NULL DEFAULT 0,
        total_reported        INTEGER,
        duration_ms           INTEGER NOT NULL DEFAULT 0,
        source_timestamp_max  TEXT,
        market_status         TEXT,
        error                 TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_runs_finished ON collection_runs(finished_at)",
    """
    CREATE TABLE IF NOT EXISTS daily_bars (
        stock_key        TEXT NOT NULL REFERENCES stocks(stock_key) ON DELETE CASCADE,
        trade_date       TEXT NOT NULL,
        open             REAL,
        open_observed    REAL,
        high             REAL,
        low              REAL,
        close            REAL,
        last_price       REAL,
        previous_close   REAL,
        volume           REAL,
        traded_value     REAL,
        number_of_trades INTEGER,
        currency         TEXT,
        is_final         INTEGER NOT NULL DEFAULT 0,
        source_timestamp TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (stock_key, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_bars_date ON daily_bars(trade_date)",
    """
    CREATE TABLE IF NOT EXISTS poller_lease (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        owner      TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
]


# Columns added after the first release: applied to existing databases by Repository.init_schema().
MIGRATIONS = [
    ("stocks", "symbol_type", "TEXT"),
    ("stocks", "type_name", "TEXT"),
    ("quotes_latest", "close_price", "REAL"),
    ("collection_runs", "market_status", "TEXT"),
]
