"""Central configuration. Every URL, timeout and interval lives here (env-overridable)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

try:  # optional convenience: read .env if python-dotenv is installed
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


def _s(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _t(name: str, default: str) -> time:
    h, m = _s(name, default).split(":")
    return time(int(h), int(m))


def _list(name: str, default: str = "") -> Tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # --- source (verified from the browser's Fetch/XHR request; see README) ---
    source_name: str = "EGX Beta Market Watch"
    source_base_url: str = "https://beta.egx.com.eg"
    source_path: str = "/api/bff/egx/market-watch"
    page_size: int = 50             # 25, 10 and 50 were all served with HTTP 200 to the real page (50 = fewest requests)
    sort_by: str = "value"          # stable ordering verified: 9 pages x 25 = 224 unique records
    sort_descending: bool = True
    max_pages: int = 50
    market_status_path: str = "/api/bff/egx/market-status"
    page_path: str = "/en/market/market-watch"
    fetch_mode: str = "passive"     # 'passive' = a real Chromium page does the requests, we only read its responses;
                                    # 'http' = plain HTTP (gets an empty 404 from the source)
    browser_headless: bool = False  # visible window is what was verified; try true at your own risk
    ui_next_selector: str = 'button[aria-label*="next" i], button:has-text("Next"), [aria-label*="next page" i]'
    ui_rows_selector: str = ""      # optional CSS selector of the rows-per-page control (auto-detected when empty)
    ui_action_delay_min: float = 0.8
    ui_action_delay_max: float = 1.8
    ui_response_timeout: float = 30.0
    ui_page_ready_timeout: float = 120.0   # how long to wait for the slow page to render its controls
    browser_max_cycles: int = 30           # gracefully recreate browser every N cycles to bound Chromium memory
    cycle_timeout: float = 180.0           # watchdog: maximum duration allowed per collection cycle
    enable_stream: bool = False            # SSE streaming endpoint feature flag
    enable_intraday_bars: bool = False     # locally derived intraday bars feature flag
    trust_source_open: bool = False # openPrice == prevClose in 224/224 captured records -> not a real open
    user_agent: str = "egx-live-api/1.0 (personal research client)"


    # --- HTTP behaviour ---
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    max_retry_after: float = 120.0  # longer Retry-After is handed to the poller instead of blocking

    # --- polling ---
    polling_enabled: bool = True
    poll_interval_open: float = 120.0     # UNVERIFIED minimum; each cycle replays UI actions, so keep it modest
    poll_interval_closed: float = 900.0
    poll_min_interval: float = 30.0       # hard floor, whatever the env says
    poll_failure_backoff_max: float = 600.0

    # --- market calendar / freshness (hours are configurable, not verified from the source) ---
    market_timezone: str = "Africa/Cairo"
    market_open: time = time(10, 0)
    market_close: time = time(14, 30)
    trading_weekdays: Tuple[int, ...] = (6, 0, 1, 2, 3)   # Python weekday(): Sun, Mon, Tue, Wed, Thu
    holidays: Tuple[date, ...] = ()
    fresh_max_age_open: float = 300.0     # source timestamp older than this during a session -> stale
    closed_session_grace_minutes: float = 30.0
    min_fetch_stale_seconds: float = 180.0
    future_tolerance_seconds: float = 300.0

    # --- storage ---
    database_path: Path = ROOT / "data" / "egx.sqlite3"
    history_enabled: bool = True
    history_retention_days: int = 30
    history_min_interval_seconds: float = 300.0

    # --- API ---
    default_limit: int = 100
    max_limit: int = 500
    cors_origins: Tuple[str, ...] = ()
    log_level: str = "INFO"
    api_keys: Tuple[str, ...] = ()
    rate_limit_per_minute: int = 120
    max_request_body_bytes: int = 1_048_576


    @property
    def source_url(self) -> str:
        return self.source_base_url.rstrip("/") + self.source_path

    @property
    def market_status_url(self) -> str:
        return self.source_base_url.rstrip("/") + self.market_status_path

    @property
    def page_url(self) -> str:
        return self.source_base_url.rstrip("/") + self.page_path

    @classmethod
    def from_env(cls) -> "Settings":
        if load_dotenv is not None:
            load_dotenv(ROOT / ".env", override=False)
        d = cls()
        holidays = tuple(date.fromisoformat(x) for x in _list("EGX_HOLIDAYS"))
        db = Path(_s("EGX_DATABASE_PATH", str(d.database_path)))
        if not db.is_absolute():
            db = ROOT / db
        return cls(
            source_base_url=_s("EGX_SOURCE_BASE_URL", d.source_base_url),
            source_path=_s("EGX_SOURCE_PATH", d.source_path),
            page_size=_i("EGX_PAGE_SIZE", d.page_size),
            sort_by=_s("EGX_SORT_BY", d.sort_by),
            sort_descending=_b("EGX_SORT_DESCENDING", d.sort_descending),
            max_pages=_i("EGX_MAX_PAGES", d.max_pages),
            market_status_path=_s("EGX_MARKET_STATUS_PATH", d.market_status_path),
            page_path=_s("EGX_PAGE_PATH", d.page_path),
            fetch_mode=_s("EGX_FETCH_MODE", d.fetch_mode).lower(),
            browser_headless=_b("EGX_BROWSER_HEADLESS", d.browser_headless),
            ui_next_selector=_s("EGX_UI_NEXT_SELECTOR", d.ui_next_selector),
            ui_rows_selector=_s("EGX_UI_ROWS_SELECTOR", d.ui_rows_selector),
            ui_action_delay_min=_f("EGX_UI_DELAY_MIN", d.ui_action_delay_min),
            ui_action_delay_max=_f("EGX_UI_DELAY_MAX", d.ui_action_delay_max),
            ui_response_timeout=_f("EGX_UI_RESPONSE_TIMEOUT", d.ui_response_timeout),
            ui_page_ready_timeout=_f("EGX_UI_PAGE_READY_TIMEOUT", d.ui_page_ready_timeout),
            browser_max_cycles=_i("EGX_BROWSER_MAX_CYCLES", d.browser_max_cycles),
            cycle_timeout=_f("EGX_CYCLE_TIMEOUT", d.cycle_timeout),
            enable_stream=_b("EGX_ENABLE_STREAM", d.enable_stream),
            enable_intraday_bars=_b("EGX_ENABLE_INTRADAY_BARS", d.enable_intraday_bars),
            trust_source_open=_b("EGX_TRUST_SOURCE_OPEN", d.trust_source_open),

            user_agent=_s("EGX_USER_AGENT", d.user_agent),
            connect_timeout=_f("EGX_CONNECT_TIMEOUT", d.connect_timeout),
            read_timeout=_f("EGX_READ_TIMEOUT", d.read_timeout),
            max_retries=_i("EGX_MAX_RETRIES", d.max_retries),
            backoff_base=_f("EGX_BACKOFF_BASE", d.backoff_base),
            backoff_max=_f("EGX_BACKOFF_MAX", d.backoff_max),
            max_retry_after=_f("EGX_MAX_RETRY_AFTER", d.max_retry_after),
            polling_enabled=_b("EGX_POLLING_ENABLED", d.polling_enabled),
            poll_interval_open=_f("EGX_POLL_INTERVAL_OPEN", d.poll_interval_open),
            poll_interval_closed=_f("EGX_POLL_INTERVAL_CLOSED", d.poll_interval_closed),
            poll_min_interval=_f("EGX_POLL_MIN_INTERVAL", d.poll_min_interval),
            poll_failure_backoff_max=_f("EGX_POLL_FAILURE_BACKOFF_MAX", d.poll_failure_backoff_max),
            market_timezone=_s("EGX_MARKET_TIMEZONE", d.market_timezone),
            market_open=_t("EGX_MARKET_OPEN", "10:00"),
            market_close=_t("EGX_MARKET_CLOSE", "14:30"),
            holidays=holidays,
            fresh_max_age_open=_f("EGX_FRESH_MAX_AGE_OPEN", d.fresh_max_age_open),
            closed_session_grace_minutes=_f("EGX_CLOSED_GRACE_MINUTES", d.closed_session_grace_minutes),
            database_path=db,
            history_enabled=_b("EGX_HISTORY_ENABLED", d.history_enabled),
            history_retention_days=_i("EGX_HISTORY_RETENTION_DAYS", d.history_retention_days),
            history_min_interval_seconds=_f("EGX_HISTORY_MIN_INTERVAL", d.history_min_interval_seconds),
            default_limit=_i("EGX_DEFAULT_LIMIT", d.default_limit),
            max_limit=_i("EGX_MAX_LIMIT", d.max_limit),
            cors_origins=_list("EGX_CORS_ORIGINS"),
            log_level=_s("EGX_LOG_LEVEL", d.log_level).upper(),
            api_keys=_list("EGX_API_KEYS"),
            rate_limit_per_minute=_i("EGX_RATE_LIMIT_PER_MINUTE", d.rate_limit_per_minute),
            max_request_body_bytes=_i("EGX_MAX_REQUEST_BODY_BYTES", d.max_request_body_bytes),
        )


    def effective_interval(self, market_open: bool) -> float:
        base = self.poll_interval_open if market_open else self.poll_interval_closed
        return max(base, self.poll_min_interval)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
