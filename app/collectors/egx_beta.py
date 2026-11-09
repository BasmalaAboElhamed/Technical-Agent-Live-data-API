"""Collector for the EGX Beta Market Watch JSON endpoint observed in the browser's Fetch/XHR tab:

    GET /api/bff/egx/market-watch?Page=1&PageSize=25&SortBy=value&SortDescending=true

Field mapping is taken from two real records. Fields that were NOT seen in the source
(change, bid, ask) are left as null instead of being guessed or computed.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as _time
from collections import Counter
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.collectors.base import (
    CollectionResult,
    MalformedResponse,
    MarketStatus,
    QuoteCollector,
    SourceError,
    SourceRateLimited,
    SourceUnavailable,
)
from app.config import Settings
from app.schemas.stock import QuoteBase
from app.utils import normalize_symbol, normalize_text, parse_number, utcnow

log = logging.getLogger(__name__)

# QuoteBase field -> source key (numeric)
NUMERIC_MAP = {
    "last_price": "lastPrice",
    "close_price": "closePrice",
    "previous_close": "prevClose",
    "change_percent": "chgPer",
    "open": "openPrice",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "traded_value": "value",
    "number_of_trades": "trades",
}
PRICE_FIELDS = {"last_price", "close_price", "previous_close", "open", "high", "low"}
NON_NEGATIVE = PRICE_FIELDS | {"volume", "traded_value", "number_of_trades"}

ITEM_KEYS = ("items", "data", "results", "records", "content", "rows", "result", "list")
TOTAL_KEYS = ("totalCount", "total", "totalItems", "totalRecords", "totalElements", "recordsTotal")
TOTAL_PAGES_KEYS = ("totalPages", "pageCount", "pages")


class RecordRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_write_time(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    """'202610221538' (yyyyMMddHHmm, optionally +ss) -> aware UTC datetime.
    ASSUMPTION: the source gives Cairo local time (not documented by the source)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s.isdigit() or len(s) not in (12, 14):
        return None
    try:
        fmt = "%Y%m%d%H%M" if len(s) == 12 else "%Y%m%d%H%M%S"
        return datetime.strptime(s, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_source_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _currency(raw: dict) -> Optional[str]:
    """Only currencies the source states in words. Observed: 'Egyptian Pound', 'US Dollar ($)'."""
    curr = (normalize_text(raw.get("curr")) or "").lower()
    short = (normalize_text(raw.get("currShort")) or "").replace(".", "").upper()
    if "egyptian pound" in curr or (not curr and short in {"LE", "EGP"}):
        return "EGP"
    if "us dollar" in curr:
        return "USD"
    return None


def normalize_record(raw: Any, fetched_at: datetime, tz: ZoneInfo, trust_open: bool = False) -> QuoteBase:
    if not isinstance(raw, dict):
        raise RecordRejected("not_an_object")
    isin = normalize_text(raw.get("isin"))
    isin = isin.upper() if isin else None
    symbol = normalize_symbol(raw.get("reuters")) or isin
    if not symbol:
        raise RecordRejected("missing_identifier")

    values: Dict[str, Any] = {}
    for field, src in NUMERIC_MAP.items():
        try:
            v = parse_number(raw.get(src))
        except ValueError:
            raise RecordRejected(f"invalid_number:{field}")
        if v is not None and field in NON_NEGATIVE and v < 0:
            raise RecordRejected(f"negative_value:{field}")
        values[field] = v

    trades = values["number_of_trades"]
    if trades is not None:
        if not float(trades).is_integer():
            raise RecordRejected("invalid_number:number_of_trades")
        values["number_of_trades"] = int(trades)
    vol = values["volume"]
    if vol is not None and float(vol).is_integer():
        values["volume"] = int(vol)

    if not trust_open:
        # In the real capture openPrice == prevClose for 224/224 records (and lay outside [low, high] for 90),
        # so it is a placeholder, not an opening price. Never present it as the Open.
        values["open"] = None

    return QuoteBase(
        symbol=symbol,
        isin=isin,
        company_name=normalize_text(raw.get("name")),
        company_name_ar=normalize_text(raw.get("nameA")),
        sector=normalize_text(raw.get("sector")),
        sector_ar=normalize_text(raw.get("sectorA")),
        symbol_type=normalize_text(raw.get("symbolType")),
        type_name=normalize_text(raw.get("typeName")),
        currency=_currency(raw),
        change=None,   # not provided by the source; deliberately not computed
        bid=None,      # not seen in the source sample
        ask=None,
        last_trade_date=parse_source_date(raw.get("lastTradeDate")),
        source_timestamp=parse_write_time(raw.get("writeTime"), tz),
        fetched_at=fetched_at,
        **values,
    )


def extract_page(payload: Any) -> Tuple[List[Any], Optional[int], Optional[int]]:
    """Return (items, total_count, total_pages). The envelope of the real response has not been
    confirmed, so a bare list and several common wrapper shapes are accepted."""
    if isinstance(payload, list):
        return payload, None, None
    if not isinstance(payload, dict):
        raise MalformedResponse(f"unexpected JSON type {type(payload).__name__}")

    def find(d: dict) -> Optional[list]:
        for k in ITEM_KEYS:
            if isinstance(d.get(k), list):
                return d[k]
        return None

    def first_int(d: dict, keys: Tuple[str, ...]) -> Optional[int]:
        for k in keys:
            v = d.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                return v
        return None

    if payload.get("success") is False:
        raise MalformedResponse(f"source reported success=false: {str(payload.get('message'))[:120]}")
    scopes = [payload]
    if isinstance(payload.get("data"), dict):
        scopes.append(payload["data"])      # real shape: {"data": {"data": [...records], "totalCount": N, ...}}
    for scope in scopes:
        items = find(scope)
        if items is not None:
            total = first_int(scope, TOTAL_KEYS) or first_int(payload, TOTAL_KEYS)
            pages = first_int(scope, TOTAL_PAGES_KEYS) or first_int(payload, TOTAL_PAGES_KEYS)
            return items, total, pages
    raise MalformedResponse(f"no list of records found; top-level keys: {sorted(payload)[:12]}")


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - (now or utcnow())).total_seconds())
    except (TypeError, ValueError):
        return None


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=settings.connect_timeout, read=settings.read_timeout, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
            "Accept-Language": "en",
        },
        follow_redirects=False,
    )


class EgxBetaCollector(QuoteCollector):
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.settings = settings
        self.client = client
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        self._tz = ZoneInfo(settings.market_timezone)
        self.source_name = settings.source_name

    # ------------------------------------------------------------------ HTTP
    def _backoff(self, attempt: int) -> float:
        s = self.settings
        ceiling = min(s.backoff_max, s.backoff_base * (2 ** attempt))
        return ceiling * (0.5 + 0.5 * self._rng())  # jittered exponential backoff

    async def _get_page(self, page: int, stats: dict) -> Any:
        s = self.settings
        params = {
            "Page": page,
            "PageSize": s.page_size,
            "SortBy": s.sort_by,
            "SortDescending": str(s.sort_descending).lower(),
        }
        return await self._get_json(s.source_url, params, stats)

    async def _get_json(self, url: str, params: Optional[dict], stats: dict) -> Any:
        s = self.settings
        last_error = "unknown error"
        for attempt in range(s.max_retries + 1):
            t0 = _time.perf_counter()
            retry_after: Optional[float] = None
            try:
                resp = await self.client.get(url, params=params)
            except httpx.TimeoutException:
                last_error = "request timed out"
            except httpx.TransportError as exc:
                last_error = f"connection error ({type(exc).__name__})"
            else:
                stats["latencies_ms"].append((_time.perf_counter() - t0) * 1000)
                stats["requests"] += 1
                sc = resp.status_code
                if sc == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise MalformedResponse("response body is not valid JSON") from exc
                if sc in (429, 503) or sc >= 500:
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"), self._clock())
                    last_error = f"HTTP {sc}"
                    if sc == 429 and retry_after is not None and retry_after > s.max_retry_after:
                        raise SourceRateLimited(f"HTTP 429, Retry-After {retry_after:.0f}s", retry_after)
                    if attempt == s.max_retries:
                        if sc == 429:
                            raise SourceRateLimited("HTTP 429 after retries", retry_after)
                        raise SourceUnavailable(f"HTTP {sc} after {attempt + 1} attempts")
                elif sc in (401, 403):
                    raise SourceError(
                        f"HTTP {sc}: the source refused access. Access controls are not bypassed; "
                        "check the site's terms or use a licensed provider."
                    )
                else:
                    raise SourceError(f"HTTP {sc} from source")
            if attempt == s.max_retries:
                raise SourceUnavailable(f"{last_error} after {attempt + 1} attempts")
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            delay = min(delay, s.max_retry_after)
            stats["retries"] += 1
            log.warning("source %s; retry %d/%d in %.1fs", last_error, attempt + 1, s.max_retries, delay)
            await self._sleep(delay)
        raise SourceUnavailable(last_error)  # pragma: no cover

    async def fetch_market_status(self) -> Optional[MarketStatus]:
        """Non-fatal: any failure just means 'unknown'."""
        stats = {"latencies_ms": [], "requests": 0, "retries": 0}
        try:
            payload = await self._get_json(self.settings.market_status_url, None, stats)
            return self.parse_market_status(payload)
        except Exception as exc:  # noqa: BLE001 - status is advisory only
            log.warning("market-status unavailable: %s", exc)
            return None

    def parse_market_status(self, payload: Any) -> Optional[MarketStatus]:
        data = payload.get("data") if isinstance(payload, dict) else None
        status = normalize_text(data.get("status")) if isinstance(data, dict) else None
        if not status:
            return None
        sd = None
        raw_date = data.get("statusDate")
        if raw_date:
            try:  # naive local (Cairo) time, consistent with the HTTP Date header in the real capture
                sd = datetime.fromisoformat(str(raw_date)).replace(tzinfo=self._tz).astimezone(timezone.utc)
            except ValueError:
                sd = None
        return MarketStatus(status=status, status_ar=normalize_text(data.get("status_Ar")),
                            status_date=sd, fetched_at=self._clock())

    async def aclose(self) -> None:
        if self.client is not None and hasattr(self.client, "aclose"):
            await self.client.aclose()

    # ------------------------------------------------------------------ collection
    async def collect(self) -> CollectionResult:
        s = self.settings
        started = self._clock()
        t0 = _time.perf_counter()
        stats = {"latencies_ms": [], "requests": 0, "retries": 0}
        quotes: Dict[str, QuoteBase] = {}
        symbols: Dict[str, str] = {}
        rejected: Counter = Counter()
        errors: List[str] = []
        notes: List[str] = []
        duplicates = conflicts = pages = 0
        total_reported: Optional[int] = None
        terminated_cleanly = False
        prev_page_keys: Optional[frozenset] = None

        for page in range(1, s.max_pages + 1):
            try:
                payload = await self._get_page(page, stats)
                items, total, total_pages = extract_page(payload)
            except SourceError as exc:
                if page == 1:
                    raise
                errors.append(f"page {page}: {exc}")
                break
            pages += 1
            if total is not None:
                total_reported = total
            if not items:
                terminated_cleanly = True
                break

            fetched_at = self._clock()
            page_keys = set()
            for raw in items:
                try:
                    q = normalize_record(raw, fetched_at, self._tz, s.trust_source_open)
                except RecordRejected as exc:
                    rejected[exc.reason] += 1
                    continue
                key = q.key()
                page_keys.add(key)
                if key in quotes:
                    duplicates += 1
                    if quotes[key].model_dump(exclude={"fetched_at"}) != q.model_dump(exclude={"fetched_at"}):
                        conflicts += 1
                    continue
                if symbols.get(q.symbol, key) != key:
                    rejected["symbol_conflict"] += 1
                    continue
                symbols[q.symbol] = key
                quotes[key] = q

            if prev_page_keys is not None and page_keys and page_keys == prev_page_keys:
                errors.append(f"page {page} repeated page {page - 1}; pagination parameters may be ignored")
                break
            prev_page_keys = frozenset(page_keys)

            if total_reported is not None and len(quotes) >= total_reported:
                terminated_cleanly = True
                break
            if total_pages is not None and page >= total_pages:
                terminated_cleanly = True
                break
        else:
            errors.append(f"stopped at max_pages={s.max_pages}")

        complete = terminated_cleanly and not errors
        if total_reported is not None and len(quotes) < total_reported:
            complete = False
            errors.append(f"source reported {total_reported} records but {len(quotes)} unique were collected")
        if total_reported is None:
            notes.append("source did not report a total; completeness is inferred from an empty final page")
        if duplicates and total_reported is None:
            complete = False
            errors.append("duplicates across pages with no reported total: ordering may be unstable")

        ts = [q.source_timestamp for q in quotes.values() if q.source_timestamp]
        finished = self._clock()
        result = CollectionResult(
            quotes=list(quotes.values()),
            complete=complete,
            pages_fetched=pages,
            requests_made=stats["requests"],
            retries=stats["retries"],
            total_reported=total_reported,
            duplicates=duplicates,
            duplicate_conflicts=conflicts,
            rejected=dict(rejected),
            errors=errors,
            duration_seconds=_time.perf_counter() - t0,
            latencies_ms=stats["latencies_ms"],
            started_at=started,
            finished_at=finished,
            source_timestamp_max=max(ts) if ts else None,
            notes=notes,
        )
        log.info(
            "collected %d stocks in %.2fs (pages=%d complete=%s rejected=%d dup=%d retries=%d src_ts=%s)",
            len(result.quotes), result.duration_seconds, pages, complete, result.rejected_total,
            duplicates, stats["retries"], result.source_timestamp_max,
        )
        return result
