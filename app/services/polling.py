"""In-process polling scheduler. Suitable for ONE application process; a DB lease additionally
stops a second process pointed at the same database from polling concurrently."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from app.collectors.base import CollectionResult, MarketStatus, QuoteCollector, SourceError, SourceRateLimited
from app.config import Settings
from app.database.repository import Repository, RunRecord
from app.services.freshness import MarketCalendar
from app.utils import utcnow

log = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        collector: QuoteCollector,
        repo: Repository,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: Callable[[], float] = random.random,
        owner_id: Optional[str] = None,
    ):
        self.collector = collector
        self.repo = repo
        self.settings = settings
        self.calendar = MarketCalendar(settings)
        self._clock = clock
        self._rng = rng
        self.owner_id = owner_id or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_purge: Optional[datetime] = None
        # observable state
        self.consecutive_failures = 0
        self.total_cycles = 0
        self.success_count = 0
        self.failure_count = 0
        self.last_duration_ms = 0
        self.service_started_at = clock()
        self.last_attempt_at: Optional[datetime] = None
        self.last_success_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.retry_after_until: Optional[datetime] = None
        self.market_status: Optional[MarketStatus] = None

    # ------------------------------------------------------------ timing
    def _is_open(self, now: datetime) -> bool:
        """The source's own status wins when it is recent and recognised; otherwise the configured calendar."""
        ms = self.market_status
        if ms is not None and ms.is_open is not None:
            if (now - ms.fetched_at).total_seconds() <= max(3 * self.settings.effective_interval(ms.is_open), 180):
                return ms.is_open
        return self.calendar.is_open(now)

    def current_interval(self) -> float:
        now = self._clock()
        return self.settings.effective_interval(self._is_open(now))

    def next_delay(self) -> float:
        now = self._clock()
        s = self.settings
        if self._is_open(now):
            base = s.effective_interval(True)
        else:
            until_open = (self.calendar.next_open(now) - now).total_seconds()
            base = min(s.effective_interval(False), max(until_open, s.poll_min_interval))
        if self.consecutive_failures:
            base = min(s.poll_failure_backoff_max, base * (2 ** min(self.consecutive_failures, 10)))
            base *= 0.8 + 0.4 * self._rng()
        if self.retry_after_until:
            base = max(base, (self.retry_after_until - now).total_seconds())
        return max(base, s.poll_min_interval)

    # ------------------------------------------------------------ one cycle
    async def run_once(self) -> str:
        """Returns: success | partial | failed | skipped_overlap | skipped_not_leader"""
        if self._lock.locked():
            log.info("previous collection still running; skipping this cycle")
            return "skipped_overlap"
        async with self._lock:
            now = self._clock()
            ttl = max(2 * self.next_delay() + 60, 120)
            if not await asyncio.to_thread(self.repo.try_acquire_lease, self.owner_id, ttl, now):
                log.info("another process holds the polling lease; not polling")
                return "skipped_not_leader"
            self.total_cycles += 1
            self.last_attempt_at = now
            started = now
            try:
                self.market_status = await self.collector.fetch_market_status()
            except Exception:  # advisory only
                self.market_status = None
            try:
                cycle_timeout = max(self.settings.cycle_timeout, 0.01)
                result = await asyncio.wait_for(self.collector.collect(), timeout=cycle_timeout)
            except asyncio.TimeoutError:

                if hasattr(self.collector, "aclose"):
                    await self.collector.aclose()
                return await self._fail(started, f"watchdog timeout after {self.settings.cycle_timeout:.0f}s")
            except SourceRateLimited as exc:
                if exc.retry_after:
                    self.retry_after_until = self._clock() + timedelta(seconds=exc.retry_after)
                return await self._fail(started, f"rate limited: {exc}")
            except SourceError as exc:
                return await self._fail(started, f"source error: {exc}")
            except Exception as exc:  # unexpected bug: keep the service alive
                log.exception("unexpected collector failure")
                return await self._fail(started, f"unexpected error: {type(exc).__name__}: {exc}")

            if not result.quotes:
                return await self._fail(started, "source returned no usable records; stored data kept", result)

            status = "success" if result.complete else "partial"
            error = "; ".join(result.errors)[:500] or None
            run = self._run_record(started, status, result, error)
            try:
                await asyncio.to_thread(self.repo.apply_collection, result.quotes, run)
            except Exception as exc:
                log.exception("database write failed")
                return await self._fail(started, f"database error: {type(exc).__name__}", result)

            self.success_count += 1
            self.last_duration_ms = int(result.duration_seconds * 1000)
            self.consecutive_failures = 0
            self.retry_after_until = None
            self.last_error = error
            self.last_success_at = self._clock()
            await self._maybe_purge()
            return status


    def _run_record(self, started: datetime, status: str, r: CollectionResult, error: Optional[str]) -> RunRecord:
        return RunRecord(
            started_at=started, finished_at=self._clock(), status=status,
            complete=r.complete if status != "failed" else None,
            stock_count=len(r.quotes), pages=r.pages_fetched, requests=r.requests_made, retries=r.retries,
            rejected=r.rejected_total, duplicates=r.duplicates, total_reported=r.total_reported,
            duration_ms=int(r.duration_seconds * 1000), source_timestamp_max=r.source_timestamp_max, error=error,
            market_status=self.market_status.status if self.market_status else None,
        )

    async def _fail(self, started: datetime, message: str, result: Optional[CollectionResult] = None) -> str:
        self.consecutive_failures += 1
        self.failure_count += 1
        self.last_error = message
        log.warning("collection failed (%d in a row): %s", self.consecutive_failures, message)
        finished = self._clock()
        self.last_duration_ms = int((finished - started).total_seconds() * 1000)
        run = RunRecord(
            started_at=started, finished_at=finished, status="failed", complete=None,
            requests=result.requests_made if result else 0, retries=result.retries if result else 0,
            rejected=result.rejected_total if result else 0,
            duration_ms=self.last_duration_ms, error=message[:500],
            market_status=self.market_status.status if self.market_status else None,
        )
        try:
            await asyncio.to_thread(self.repo.record_failed_run, run)
        except Exception:
            log.exception("could not record failed run")
        return "failed"

    async def _maybe_purge(self) -> None:
        now = self._clock()
        if not self.settings.history_enabled:
            return
        if self._last_purge and (now - self._last_purge) < timedelta(hours=6):
            return
        self._last_purge = now
        try:
            n = await asyncio.to_thread(self.repo.purge_history, self.settings.history_retention_days, now)
            if n:
                log.info("purged %d history rows older than %d days", n, self.settings.history_retention_days)
        except Exception:
            log.exception("history purge failed")

    # ------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        log.info("poller started (open=%ss closed=%ss, min=%ss)", self.settings.poll_interval_open,
                 self.settings.poll_interval_closed, self.settings.poll_min_interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poller cycle crashed")
                self.consecutive_failures += 1
                self.failure_count += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.next_delay())
            except asyncio.TimeoutError:
                pass
        log.info("poller stopped")

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever(), name="egx-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if hasattr(self.collector, "aclose"):
            try:
                await self.collector.aclose()
            except Exception:
                pass
        try:
            await asyncio.to_thread(self.repo.release_lease, self.owner_id)
        except Exception:
            pass

