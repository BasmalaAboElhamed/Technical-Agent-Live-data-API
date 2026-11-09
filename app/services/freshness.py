"""Freshness rules. A successful HTTP request alone NEVER makes data 'fresh'."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import Settings
from app.schemas.stock import DataStatus


@dataclass(frozen=True)
class Freshness:
    status: DataStatus
    reason: str
    age_seconds: Optional[float]
    market_open: bool


class MarketCalendar:
    """Trading hours are configuration, NOT verified from the source. Holidays are only
    known if listed in EGX_HOLIDAYS; an unlisted holiday makes data look stale (conservative)."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.tz = ZoneInfo(settings.market_timezone)

    def _trading_day(self, d: date) -> bool:
        return d.weekday() in self.s.trading_weekdays and d not in self.s.holidays

    def _at(self, d: date, t) -> datetime:
        return datetime.combine(d, t).replace(tzinfo=self.tz)

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return self._trading_day(local.date()) and self.s.market_open <= local.time() < self.s.market_close

    def last_session_close(self, now: datetime) -> datetime:
        local = now.astimezone(self.tz)
        d = local.date()
        for _ in range(21):
            if self._trading_day(d):
                close = self._at(d, self.s.market_close)
                if close <= local:
                    return close.astimezone(timezone.utc)
            d -= timedelta(days=1)
        raise RuntimeError("no trading day found; check EGX_HOLIDAYS / trading weekdays")

    def next_open(self, now: datetime) -> datetime:
        local = now.astimezone(self.tz)
        d = local.date()
        for _ in range(21):
            if self._trading_day(d):
                opn = self._at(d, self.s.market_open)
                if opn > local:
                    return opn.astimezone(timezone.utc)
            d += timedelta(days=1)
        raise RuntimeError("no trading day found; check EGX_HOLIDAYS / trading weekdays")


def evaluate_freshness(
    *,
    source_timestamp: Optional[datetime],
    fetched_at: Optional[datetime],
    has_price: bool,
    now: datetime,
    settings: Settings,
    calendar: MarketCalendar,
    market_open: Optional[bool] = None,     # the source's own session state, when known and recent
) -> Freshness:
    open_ = calendar.is_open(now) if market_open is None else market_open
    if not has_price:
        return Freshness("unavailable", "no_price_in_source", None, open_)
    if source_timestamp is None or fetched_at is None:
        return Freshness("stale", "no_source_timestamp", None, open_)

    age = (now - source_timestamp).total_seconds()

    interval = settings.effective_interval(open_)
    if (now - fetched_at).total_seconds() > max(3 * interval, settings.min_fetch_stale_seconds):
        return Freshness("stale", "collection_not_recent", age, open_)
    if age < -settings.future_tolerance_seconds:
        return Freshness("stale", "source_timestamp_in_future", age, open_)

    if open_:
        if age <= settings.fresh_max_age_open:
            return Freshness("fresh", "updated_within_session_window", age, open_)
        return Freshness("stale", "source_timestamp_old_during_session", age, open_)

    close = calendar.last_session_close(now)
    if source_timestamp >= close - timedelta(minutes=settings.closed_session_grace_minutes):
        return Freshness("fresh", "market_closed_data_from_last_session_close", age, open_)
    return Freshness("stale", "older_than_last_session_close", age, open_)
