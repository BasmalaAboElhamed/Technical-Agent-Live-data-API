from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from app.schemas.stock import QuoteBase


class SourceError(Exception):
    """Source request failed (non-retryable or retries exhausted)."""


class SourceUnavailable(SourceError):
    """Timeouts / connection errors / 5xx after retries."""


class SourceRateLimited(SourceError):
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class MalformedResponse(SourceError):
    """Response was not the expected JSON shape."""


@dataclass
class MarketStatus:
    """Session state as reported by the source. Only the value 'Closed' has been observed so far."""
    status: str
    status_ar: Optional[str]
    status_date: Optional[datetime]
    fetched_at: datetime

    @property
    def is_open(self) -> Optional[bool]:
        s = self.status.strip().lower()
        if s == "closed":
            return False
        if s in {"open", "opened", "trading"}:   # ASSUMED spellings: never observed in a capture
            return True
        return None                               # unknown value -> caller falls back to the calendar


@dataclass
class CollectionResult:
    quotes: List[QuoteBase]
    complete: bool
    pages_fetched: int
    requests_made: int
    retries: int
    total_reported: Optional[int]
    duplicates: int
    duplicate_conflicts: int
    rejected: Dict[str, int]
    errors: List[str]
    duration_seconds: float
    latencies_ms: List[float]
    started_at: datetime
    finished_at: datetime
    source_timestamp_max: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


class QuoteCollector(ABC):
    """Swap this out to add a licensed provider; routes and repository do not change."""

    source_name: str = "unknown"

    @abstractmethod
    async def collect(self) -> CollectionResult:
        """Fetch every available quote. Raise SourceError if nothing usable was obtained."""

    async def fetch_market_status(self) -> Optional[MarketStatus]:
        """Optional: the source's own open/closed state. None when unsupported or unavailable."""
        return None

    async def aclose(self) -> None:
        """Release resources (browser, HTTP client)."""
        return None
