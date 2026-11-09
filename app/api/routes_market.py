from fastapi import APIRouter, Depends

from app.api.deps import ApiError, get_service
from app.schemas.responses import SummaryResponse
from app.services.stock_service import StockService

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/summary", response_model=SummaryResponse, summary="Market summary (locally computed ESTIMATE)")
def market_summary(svc: StockService = Depends(get_service)):
    s = svc.summary()
    if s is None:
        raise ApiError(503, "no_data", "No successful collection yet; no summary can be produced.")
    return s
