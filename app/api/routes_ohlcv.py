from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import ApiError, Pagination, get_service, pagination
from app.schemas.responses import DailyBar, DailyBarsResponse, PageInfo
from app.services.stock_service import StockService
from app.utils import normalize_symbol

router = APIRouter(prefix="/api/v1/ohlcv", tags=["ohlcv"])

NOTES = [
    "One bar per stock per trading day, built from the source's day-aggregate fields (high, low, closePrice, lastPrice, volume, value, trades).",
    "`open` is null: the source's openPrice equalled prevClose in every captured record, so it is not an opening price. "
    "`open_observed` is the first last_price this installation saw that day - a proxy that is only a true open if the collector was running at the session open.",
    "`close` is the source's closePrice; `last_price` is the last trade. They differ for some stocks after the closing auction.",
    "`is_final` = the snapshot was written at/after the configured session close; non-final bars are still moving.",
    "The feed only listed instruments that traded (every captured record had volume > 0), so a stock with no trades has no bar that day.",
    "History starts when this installation first collected; nothing earlier exists.",
    "Daily bars only. Intraday candles are not built.",
]


@router.get("/daily", response_model=DailyBarsResponse, summary="Daily OHLCV for ALL stocks on one trade date")
def daily_all(
    trade_date: Optional[date] = Query(None, alias="date", description="YYYY-MM-DD; default = latest date available."),
    search: Optional[str] = Query(None, max_length=100),
    page: Pagination = Depends(pagination),
    svc: StockService = Depends(get_service),
):
    d = trade_date or svc.repo.latest_bar_date()
    if d is None:
        return DailyBarsResponse(trade_date=None, data=[], pagination=PageInfo(total=0, limit=page.limit, offset=page.offset, returned=0, has_more=False),
                                 notes=NOTES + ["No bars stored yet: no successful collection inside a trading day."])
    rows, total = svc.repo.list_daily_bars(d, search, page.limit, page.offset)
    return DailyBarsResponse(
        trade_date=d, data=[DailyBar(**r) for r in rows], notes=NOTES,
        pagination=PageInfo(total=total, limit=page.limit, offset=page.offset, returned=len(rows),
                            has_more=page.offset + len(rows) < total))


@router.get("/daily/{symbol}", response_model=DailyBarsResponse, summary="Daily OHLCV history for one stock")
def daily_symbol(
    symbol: str,
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    page: Pagination = Depends(pagination),
    svc: StockService = Depends(get_service),
):
    sym = normalize_symbol(symbol)
    if not sym or svc.repo.get_stock(sym) is None:
        raise ApiError(404, "symbol_not_found", f"Unknown symbol: {symbol!r}")
    if start and end and start > end:
        raise ApiError(422, "invalid_range", "start must not be after end")
    rows, total = svc.repo.symbol_daily_bars(sym, start, end, page.limit, page.offset)
    return DailyBarsResponse(
        data=[DailyBar(**r) for r in rows], notes=NOTES,
        pagination=PageInfo(total=total, limit=page.limit, offset=page.offset, returned=len(rows),
                            has_more=page.offset + len(rows) < total))
