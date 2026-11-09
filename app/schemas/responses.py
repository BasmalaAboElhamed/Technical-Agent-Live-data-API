from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel

from app.schemas.stock import DataStatus, QuoteOut, StockEntry


class PageInfo(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int
    has_more: bool


class DatasetMeta(BaseModel):
    source: str
    data_timing: Literal["unverified"] = "unverified"
    last_successful_fetch: Optional[datetime] = None
    dataset_complete: Optional[bool] = None
    notes: List[str] = []


class QuoteListResponse(BaseModel):
    data: List[QuoteOut]
    pagination: PageInfo
    meta: DatasetMeta


class QuoteResponse(BaseModel):
    data: QuoteOut
    meta: DatasetMeta


class StockListResponse(BaseModel):
    data: List[StockEntry]
    pagination: PageInfo
    meta: DatasetMeta


class StockDetail(StockEntry):
    quote: Optional[QuoteOut] = None


class StockResponse(BaseModel):
    data: StockDetail
    meta: DatasetMeta


class HistoryPoint(BaseModel):
    source_timestamp: datetime
    fetched_at: datetime
    last_price: Optional[float] = None
    previous_close: Optional[float] = None
    change_percent: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    traded_value: Optional[float] = None
    number_of_trades: Optional[int] = None


class HistoryResponse(BaseModel):
    symbol: str
    data: List[HistoryPoint]
    pagination: PageInfo
    history_enabled: bool
    retention_days: int
    earliest_local_observation: Optional[datetime] = None
    note: str


class DailyBar(BaseModel):
    """One OHLCV bar per stock per trading day, built from the source's day-aggregate fields."""
    symbol: str
    isin: Optional[str] = None
    company_name: Optional[str] = None
    trade_date: date
    open: Optional[float] = None            # null unless the source gives a real open (it currently does not)
    open_observed: Optional[float] = None   # first last_price THIS installation saw that day (a proxy, not the open)
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None           # source closePrice
    last_price: Optional[float] = None      # source lastPrice (last trade)
    previous_close: Optional[float] = None
    volume: Optional[Union[int, float]] = None
    traded_value: Optional[float] = None
    number_of_trades: Optional[int] = None
    currency: Optional[str] = None
    is_final: bool                          # snapshot written at/after the configured session close
    source_timestamp: datetime


class DailyBarsResponse(BaseModel):
    trade_date: Optional[date] = None
    data: List[DailyBar]
    pagination: PageInfo
    notes: List[str]


class RunInfo(BaseModel):
    started_at: datetime
    finished_at: datetime
    status: str
    complete: Optional[bool] = None
    stock_count: int
    duration_seconds: float
    error: Optional[str] = None


class StatusResponse(BaseModel):
    source_name: str
    source_connectivity: Literal["ok", "error", "unknown"]
    data_timing: Literal["unverified"] = "unverified"
    data_freshness: DataStatus
    data_freshness_reason: str
    market_session_open: bool
    market_status_reported: Optional[str] = None
    market_status_reported_at: Optional[datetime] = None
    last_successful_fetch: Optional[datetime] = None
    last_successful_complete_collection: Optional[datetime] = None
    last_attempted_fetch: Optional[datetime] = None
    latest_source_timestamp: Optional[datetime] = None
    stocks_in_latest_collection: Optional[int] = None
    stocks_in_database: int
    last_collection_duration_seconds: Optional[float] = None
    polling_enabled: bool
    polling_interval_seconds: float
    consecutive_failures: int
    most_recent_error: Optional[str] = None
    dataset_complete: Optional[bool] = None
    dataset_state: Literal["complete", "partial", "empty"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    database: Dict[str, Any]
    source: Dict[str, Any]
    last_successful_collection: Optional[datetime] = None


class SummaryEstimate(BaseModel):
    instruments_in_feed: int
    instruments_with_price: int
    advancers: int
    decliners: int
    unchanged: int
    total_traded_value: Optional[float] = None
    total_volume: Optional[float] = None
    total_trades: Optional[int] = None
    records_missing_traded_value: int


class SummaryResponse(BaseModel):
    official: bool = False
    source_provides_summary: bool = False
    basis: str
    dataset_complete: Optional[bool]
    freshness: DataStatus
    estimate: SummaryEstimate
    meta: DatasetMeta


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class PollerMetrics(BaseModel):
    enabled: bool
    status: Literal["running", "idle", "stopped"]
    consecutive_failures: int
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_duration_seconds: Optional[float] = None
    total_cycles: int
    success_count: int
    failure_count: int


class DatabaseMetrics(BaseModel):
    stocks_count: int
    quotes_count: int
    daily_bars_count: int


class MetricsResponse(BaseModel):
    service: str = "egx-live-api"
    version: str = "1.0.0"
    uptime_seconds: float
    data_timing: Literal["unverified"] = "unverified"
    poller: PollerMetrics
    database: DatabaseMetrics

