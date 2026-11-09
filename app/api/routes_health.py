from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import check_rate_limit, get_service
from app.schemas.responses import HealthResponse, MetricsResponse, StatusResponse
from app.services.stock_service import StockService

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service, database and source health")
def health(svc: StockService = Depends(get_service)):
    body, code = svc.health()
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


@router.get("/api/v1/status", response_model=StatusResponse, summary="Collection status and data freshness",
            dependencies=[Depends(check_rate_limit)])
def status(svc: StockService = Depends(get_service)):
    return svc.status()


@router.get("/api/v1/metrics", response_model=MetricsResponse, summary="System metrics, poller cycle stats, and database counts",
            dependencies=[Depends(check_rate_limit)])
@router.get("/metrics", response_model=MetricsResponse, include_in_schema=False)
def metrics(svc: StockService = Depends(get_service)):
    return svc.metrics()


