from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import ApiError, Pagination, get_service, pagination
from app.schemas.responses import PageInfo, StockDetail, StockListResponse, StockResponse
from app.services.stock_service import StockService
from app.utils import normalize_symbol

router = APIRouter(prefix="/api/v1/stocks", tags=["stocks"])


@router.get("", response_model=StockListResponse, summary="Stock directory")
def list_stocks(
    search: Optional[str] = Query(None, max_length=100, description="Substring of symbol, ISIN or company name (English/Arabic)."),
    page: Pagination = Depends(pagination),
    svc: StockService = Depends(get_service),
):
    rows, total = svc.repo.list_stocks(search, page.limit, page.offset)
    return StockListResponse(
        data=rows,
        pagination=PageInfo(total=total, limit=page.limit, offset=page.offset, returned=len(rows),
                            has_more=page.offset + len(rows) < total),
        meta=svc.meta(),
    )


@router.get("/{symbol}", response_model=StockResponse, summary="Directory entry plus latest quote")
def get_stock(symbol: str, svc: StockService = Depends(get_service)):
    sym = normalize_symbol(symbol)
    stock = svc.repo.get_stock(sym) if sym else None
    if stock is None:
        raise ApiError(404, "symbol_not_found", f"Unknown symbol: {symbol!r}")
    q = svc.repo.get_quote(stock.symbol)
    detail = StockDetail(**stock.model_dump(), quote=svc.to_out(q, svc.now()) if q else None)
    return StockResponse(data=detail, meta=svc.meta())
