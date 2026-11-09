from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict

DataStatus = Literal["fresh", "stale", "unavailable"]
Number = Union[int, float]


class QuoteBase(BaseModel):
    """Normalised quote as produced by a collector. Missing values are None (never 0)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    isin: Optional[str] = None
    company_name: Optional[str] = None
    company_name_ar: Optional[str] = None
    sector: Optional[str] = None
    sector_ar: Optional[str] = None
    symbol_type: Optional[str] = None
    type_name: Optional[str] = None
    exchange: Literal["EGX"] = "EGX"
    currency: Optional[str] = None
    last_price: Optional[float] = None
    close_price: Optional[float] = None
    previous_close: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[Number] = None
    traded_value: Optional[float] = None
    number_of_trades: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_trade_date: Optional[date] = None
    source_timestamp: Optional[datetime] = None
    fetched_at: datetime

    def key(self) -> str:
        """Stable identity: ISIN when present, otherwise the symbol. Never merges distinct ISINs."""
        return self.isin or f"SYM:{self.symbol}"


class QuoteOut(QuoteBase):
    data_status: DataStatus
    status_reason: str
    age_seconds: Optional[float] = None


class StockEntry(BaseModel):
    symbol: str
    isin: Optional[str] = None
    company_name: Optional[str] = None
    company_name_ar: Optional[str] = None
    sector: Optional[str] = None
    sector_ar: Optional[str] = None
    symbol_type: Optional[str] = None
    type_name: Optional[str] = None
    exchange: Literal["EGX"] = "EGX"
    first_seen_at: datetime
    last_seen_at: datetime
