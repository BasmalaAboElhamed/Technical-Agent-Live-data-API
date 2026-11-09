from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import ApiError, Pagination, get_service, get_settings_dep, pagination
from app.config import Settings
from app.schemas.responses import HistoryPoint, HistoryResponse, PageInfo, QuoteListResponse, QuoteResponse
from app.services.stock_service import StockService
from app.utils import normalize_symbol

router = APIRouter(prefix="/api/v1/quotes", tags=["quotes"])

SortField = Literal[
    "symbol", "company_name", "last_price", "close_price", "previous_close", "change_percent", "open", "high", "low",
    "volume", "traded_value", "number_of_trades", "source_timestamp",
]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)   # naive input is interpreted as UTC
    return dt


@router.get("", response_model=QuoteListResponse, summary="Latest quote for every tracked stock")
def list_quotes(
    search: Optional[str] = Query(None, max_length=100),
    sort_by: SortField = Query("symbol"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: Pagination = Depends(pagination),
    svc: StockService = Depends(get_service),
):
    rows, total = svc.repo.list_quotes(search, page.limit, page.offset, sort_by, order)
    now = svc.now()
    return QuoteListResponse(
        data=[svc.to_out(q, now) for q in rows],
        pagination=PageInfo(total=total, limit=page.limit, offset=page.offset, returned=len(rows),
                            has_more=page.offset + len(rows) < total),
        meta=svc.meta(),
    )


@router.get("/{symbol}", response_model=QuoteResponse, summary="Latest quote for one stock")
def get_quote(symbol: str, svc: StockService = Depends(get_service)):
    sym = normalize_symbol(symbol)
    q = svc.repo.get_quote(sym) if sym else None
    if q is None:
        raise ApiError(404, "symbol_not_found", f"No quote for symbol: {symbol!r}")
    return QuoteResponse(data=svc.to_out(q, svc.now()), meta=svc.meta())


@router.get("/{symbol}/history", response_model=HistoryResponse,
            summary="Locally stored observations (starts when YOUR collector started)")
def quote_history(
    symbol: str,
    start: Optional[datetime] = Query(None, description="ISO 8601; naive values are treated as UTC."),
    end: Optional[datetime] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    svc: StockService = Depends(get_service),
    settings: Settings = Depends(get_settings_dep),
):
    sym = normalize_symbol(symbol)
    if not sym or svc.repo.get_stock(sym) is None:
        raise ApiError(404, "symbol_not_found", f"Unknown symbol: {symbol!r}")
    start, end = _aware(start), _aware(end)
    if start and end and start > end:
        raise ApiError(422, "invalid_range", "start must not be after end")
    limit = limit or settings.default_limit
    if limit > settings.max_limit:
        raise ApiError(422, "limit_too_large", f"limit must be <= {settings.max_limit}")
    rows, total, earliest = svc.repo.history(sym, start, end, limit, offset)
    return HistoryResponse(
        symbol=sym,
        data=[HistoryPoint(**r) for r in rows],
        pagination=PageInfo(total=total, limit=limit, offset=offset, returned=len(rows),
                            has_more=offset + len(rows) < total),
        history_enabled=settings.history_enabled,
        retention_days=settings.history_retention_days,
        earliest_local_observation=earliest,
        note=("History contains only observations stored by this installation while the collector was running, "
              f"thinned to at most one per {int(settings.history_min_interval_seconds)}s per stock and kept "
              f"for {settings.history_retention_days} days. It does not include data from before installation."),
    )
