from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import MarketStatus
from app.config import Settings
from app.database.repository import Repository
from app.schemas.responses import (
    DatabaseMetrics, DatasetMeta, HealthResponse, MetricsResponse, PollerMetrics,
    StatusResponse, SummaryEstimate, SummaryResponse,
)

from app.schemas.stock import QuoteBase, QuoteOut
from app.services.freshness import Freshness, MarketCalendar, evaluate_freshness
from app.services.polling import Poller
from app.utils import utcnow

TIMING_NOTE = (
    "Whether EGX Beta Market Watch data is real-time or delayed is not confirmed by the source; "
    "rely on source_timestamp and data_status, not on the word 'live'."
)


class StockService:
    def __init__(self, settings: Settings, repo: Repository, poller: Optional[Poller] = None, clock=utcnow):
        self.s = settings
        self.repo = repo
        self.poller = poller
        self.calendar = MarketCalendar(settings)
        self._clock = clock
        self._ov_cache = (0.0, None)

    def now(self) -> datetime:
        return self._clock()

    def session_override(self, now: datetime) -> Optional[bool]:
        """Source-reported open/closed state from the latest collection run, if recent and recognised."""
        t, cached = self._ov_cache
        if _time.monotonic() - t < 2.0:
            return cached
        run = self.repo.latest_run()
        value = None
        if run and run.get("market_status"):
            is_open = MarketStatus(run["market_status"], None, None, run["finished_at"]).is_open
            if is_open is not None:
                window = max(3 * self.s.effective_interval(is_open), 180)
                if (now - run["finished_at"]).total_seconds() <= window:
                    value = is_open
        self._ov_cache = (_time.monotonic(), value)
        return value

    def freshness(self, q_ts: Optional[datetime], fetched: Optional[datetime], has_price: bool, now: datetime) -> Freshness:
        return evaluate_freshness(source_timestamp=q_ts, fetched_at=fetched, has_price=has_price,
                                  now=now, settings=self.s, calendar=self.calendar,
                                  market_open=self.session_override(now))

    def to_out(self, q: QuoteBase, now: datetime) -> QuoteOut:
        f = self.freshness(q.source_timestamp, q.fetched_at, q.last_price is not None, now)
        return QuoteOut(**q.model_dump(), data_status=f.status, status_reason=f.reason, age_seconds=f.age_seconds)

    def meta(self, extra_notes: Optional[List[str]] = None) -> DatasetMeta:
        run = self.repo.latest_data_run()
        notes = [TIMING_NOTE, "Missing values are null; null is never zero."]
        if run and run["complete"] is False:
            notes.append("Latest collection was PARTIAL: some stocks may be missing or older.")
        if run is None:
            notes.append("No successful collection yet; no data is available.")
        return DatasetMeta(
            source=self.s.source_name,
            last_successful_fetch=run["finished_at"] if run else None,
            dataset_complete=run["complete"] if run else None,
            notes=notes + (extra_notes or []),
        )

    # ------------------------------------------------------------ status / health
    def status(self) -> StatusResponse:
        now = self._clock()
        latest = self.repo.latest_run()
        data_run = self.repo.latest_data_run()
        complete_run = self.repo.latest_complete_run()
        ov = self.session_override(now)
        open_ = self.calendar.is_open(now) if ov is None else ov
        if data_run and data_run["source_timestamp_max"]:
            f = self.freshness(data_run["source_timestamp_max"], data_run["finished_at"], True, now)
        elif data_run:
            f = Freshness("stale", "no_source_timestamp", None, open_)
        else:
            f = Freshness("unavailable", "no_successful_collection", None, open_)
        if latest is None:
            conn = "unknown"
        else:
            conn = "error" if latest["status"] == "failed" else "ok"
        state = "empty" if data_run is None else ("complete" if data_run["complete"] else "partial")
        p = self.poller
        return StatusResponse(
            source_name=self.s.source_name,
            source_connectivity=conn,
            data_freshness=f.status, data_freshness_reason=f.reason, market_session_open=open_,
            market_status_reported=latest["market_status"] if latest else None,
            market_status_reported_at=latest["finished_at"] if latest and latest["market_status"] else None,
            last_successful_fetch=data_run["finished_at"] if data_run else None,
            last_successful_complete_collection=complete_run["finished_at"] if complete_run else None,
            last_attempted_fetch=latest["finished_at"] if latest else None,
            latest_source_timestamp=data_run["source_timestamp_max"] if data_run else None,
            stocks_in_latest_collection=data_run["stock_count"] if data_run else None,
            stocks_in_database=self.repo.count_stocks(),
            last_collection_duration_seconds=(latest["duration_ms"] / 1000.0) if latest else None,
            polling_enabled=bool(p) and self.s.polling_enabled,
            polling_interval_seconds=p.current_interval() if p else self.s.effective_interval(open_),
            consecutive_failures=p.consecutive_failures if p else 0,
            most_recent_error=(latest["error"] if latest and latest["status"] == "failed" else None),
            dataset_complete=data_run["complete"] if data_run else None,
            dataset_state=state,
        )

    def health(self) -> Tuple[HealthResponse, int]:
        db_ok = self.repo.ping()
        if not db_ok:
            return HealthResponse(status="unavailable", database={"ok": False},
                                  source={"status": "unknown"}, last_successful_collection=None), 503
        st = self.status()
        overall = "ok" if (st.source_connectivity == "ok" and st.data_freshness == "fresh") else "degraded"
        return HealthResponse(
            status=overall,
            database={"ok": True},
            source={"status": st.source_connectivity, "data_freshness": st.data_freshness,
                    "last_error": st.most_recent_error},
            last_successful_collection=st.last_successful_fetch,
        ), 200

    # ------------------------------------------------------------ summary (estimate only)
    def summary(self) -> Optional[SummaryResponse]:
        run = self.repo.latest_data_run()
        if run is None:
            return None
        now = self._clock()
        quotes = self.repo.list_all_quotes()
        priced = [q for q in quotes if q.last_price is not None]
        adv = sum(1 for q in priced if (q.change_percent or 0) > 0)
        dec = sum(1 for q in priced if (q.change_percent or 0) < 0)
        unch = sum(1 for q in priced if q.change_percent == 0)
        vals = [q.traded_value for q in quotes if q.traded_value is not None]
        vols = [q.volume for q in quotes if q.volume is not None]
        trades = [q.number_of_trades for q in quotes if q.number_of_trades is not None]
        ts = max((q.source_timestamp for q in quotes if q.source_timestamp), default=None)
        f = self.freshness(ts, run["finished_at"], bool(priced), now)
        return SummaryResponse(
            basis=("ESTIMATE computed locally from the quotes in this feed. EGX's Beta Market Watch response "
                   "for this collector does not provide exchange-wide totals, so these are not official figures "
                   "and exclude anything absent from the feed."),
            dataset_complete=run["complete"], freshness=f.status,
            estimate=SummaryEstimate(
                instruments_in_feed=len(quotes), instruments_with_price=len(priced),
                advancers=adv, decliners=dec, unchanged=unch,
                total_traded_value=sum(vals) if vals else None,
                total_volume=sum(vols) if vols else None,
                total_trades=sum(trades) if trades else None,
                records_missing_traded_value=len(quotes) - len(vals),
            ),
            meta=self.meta(),
        )

    # ------------------------------------------------------------ metrics
    def metrics(self) -> MetricsResponse:
        now = self._clock()
        p = self.poller
        uptime = (now - p.service_started_at).total_seconds() if p and hasattr(p, "service_started_at") else 0.0
        poller_status = "running" if (p and p._task and not p._task.done()) else ("stopped" if p else "idle")
        pm = PollerMetrics(
            enabled=bool(p) and self.s.polling_enabled,
            status=poller_status,
            consecutive_failures=p.consecutive_failures if p else 0,
            last_attempt_at=p.last_attempt_at if p else None,
            last_success_at=p.last_success_at if p else None,
            last_duration_seconds=(p.last_duration_ms / 1000.0) if (p and p.last_duration_ms) else None,
            total_cycles=p.total_cycles if p else 0,
            success_count=p.success_count if p else 0,
            failure_count=p.failure_count if p else 0,
        )
        dm = DatabaseMetrics(
            stocks_count=self.repo.count_stocks(),
            quotes_count=self.repo.count_quotes(),
            daily_bars_count=self.repo.count_daily_bars(),
        )
        return MetricsResponse(
            service="egx-live-api",
            version="1.0.0",
            uptime_seconds=max(uptime, 0.0),
            data_timing="unverified",
            poller=pm,
            database=dm,
        )

