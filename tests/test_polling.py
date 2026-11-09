from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.collectors.base import CollectionResult, QuoteCollector, SourceUnavailable, SourceRateLimited
from app.schemas.stock import QuoteBase
from app.services.polling import Poller
from tests.fixtures import utc

NOW = utc(2026, 10, 22, 8, 0)    # Thursday 11:00 Cairo, market open


class Clock:
    def __init__(self, t=NOW): self.t = t
    def __call__(self): return self.t


def result(quotes: List[QuoteBase], complete=True, errors=None) -> CollectionResult:
    return CollectionResult(quotes=quotes, complete=complete, pages_fetched=1, requests_made=1, retries=0,
                            total_reported=None, duplicates=0, duplicate_conflicts=0, rejected={}, errors=errors or [],
                            duration_seconds=0.1, latencies_ms=[10.0], started_at=NOW, finished_at=NOW,
                            source_timestamp_max=max((q.source_timestamp for q in quotes if q.source_timestamp), default=None))


def quote(sym="COMI", price=125.0, ts=NOW - timedelta(seconds=30)):
    return QuoteBase(symbol=sym, isin=f"EGS{sym}", last_price=price, source_timestamp=ts, fetched_at=NOW)


class Script(QuoteCollector):
    """Collector that plays back scripted outcomes (values or exceptions)."""
    def __init__(self, *steps): self.steps, self.calls = list(steps), 0
    async def collect(self):
        self.calls += 1
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def mk(settings, repo, collector, clock=None, **kw):
    return Poller(collector, repo, settings, clock=clock or Clock(), rng=lambda: 0.5, **kw)


def test_success_stores_data_and_resets_failures(settings, repo):
    p = mk(settings, repo, Script(result([quote()])))
    p.consecutive_failures = 3
    assert asyncio.run(p.run_once()) == "success"
    assert p.consecutive_failures == 0 and p.last_success_at == NOW
    assert repo.get_quote("COMI").last_price == 125.0 and repo.latest_run()["status"] == "success"


def test_partial_collection_is_flagged_not_called_success(settings, repo):
    p = mk(settings, repo, Script(result([quote()], complete=False, errors=["page 2: HTTP 500"])))
    assert asyncio.run(p.run_once()) == "partial"
    run = repo.latest_data_run()
    assert run["status"] == "partial" and run["complete"] is False and "page 2" in run["error"]
    assert repo.latest_complete_run() is None


def test_failure_keeps_last_known_good_data_and_logs_run(settings, repo):
    p = mk(settings, repo, Script(result([quote(price=111.0)]), SourceUnavailable("down")))
    asyncio.run(p.run_once())
    assert asyncio.run(p.run_once()) == "failed"
    assert repo.get_quote("COMI").last_price == 111.0                 # not wiped, not replaced
    assert repo.latest_run()["status"] == "failed" and p.consecutive_failures == 1


def test_empty_response_never_overwrites_valid_data(settings, repo):
    p = mk(settings, repo, Script(result([quote(price=111.0)]), result([])))
    asyncio.run(p.run_once())
    assert asyncio.run(p.run_once()) == "failed"
    assert repo.get_quote("COMI").last_price == 111.0 and "no usable records" in repo.latest_run()["error"]


def test_no_fabricated_data_when_source_never_worked(settings, repo):
    p = mk(settings, repo, Script(SourceUnavailable("down")))
    asyncio.run(p.run_once())
    assert repo.list_all_quotes() == [] and repo.count_stocks() == 0


def test_backoff_grows_then_resets_after_recovery(settings, repo):
    p = mk(settings, repo, Script(SourceUnavailable("x"), SourceUnavailable("x"), result([quote()])))
    base = p.next_delay()
    asyncio.run(p.run_once()); d1 = p.next_delay()
    asyncio.run(p.run_once()); d2 = p.next_delay()
    assert d1 > base and d2 > d1
    assert asyncio.run(p.run_once()) == "success"
    assert p.next_delay() == base and p.consecutive_failures == 0


def test_backoff_is_capped(settings, repo):
    p = mk(settings, repo, Script(SourceUnavailable("x")))
    p.consecutive_failures = 50
    assert p.next_delay() <= settings.poll_failure_backoff_max * 1.2


def test_rate_limit_retry_after_extends_delay(settings, repo):
    p = mk(settings, repo, Script(SourceRateLimited("429", retry_after=900)))
    asyncio.run(p.run_once())
    assert p.next_delay() >= 899


def test_overlapping_cycles_are_prevented(settings, repo):
    gate = asyncio.Event()

    class Slow(QuoteCollector):
        calls = 0
        async def collect(self):
            Slow.calls += 1
            await gate.wait()
            return result([quote()])

    async def scenario():
        p = mk(settings, repo, Slow())
        first = asyncio.create_task(p.run_once())
        await asyncio.sleep(0.05)
        second = await p.run_once()
        gate.set()
        return second, await first

    second, first = asyncio.run(scenario())
    assert second == "skipped_overlap" and first == "success" and Slow.calls == 1


def test_second_process_does_not_poll_while_lease_held(settings, repo):
    a = mk(settings, repo, Script(result([quote()])), owner_id="proc-a")
    b_collector = Script(result([quote()]))
    b = mk(settings, repo, b_collector, owner_id="proc-b")
    assert asyncio.run(a.run_once()) == "success"
    assert asyncio.run(b.run_once()) == "skipped_not_leader" and b_collector.calls == 0


def test_market_closed_polls_slowly_but_wakes_for_the_open(settings, repo):
    fri = Clock(utc(2026, 10, 23, 9, 0))                       # Friday: closed
    p = mk(dataclasses.replace(settings, poll_interval_closed=900, poll_min_interval=1), repo, Script(result([])), clock=fri)
    assert p.next_delay() == 900
    sat_night = Clock(utc(2026, 10, 25, 6, 55))                # Sunday 09:55 Cairo, 5 min before open
    p2 = mk(settings, repo, Script(result([])), clock=sat_night)
    assert 290 <= p2.next_delay() <= 310


def test_interval_never_below_hard_floor(settings, repo):
    s = dataclasses.replace(settings, poll_interval_open=1, poll_min_interval=30)
    assert mk(s, repo, Script(result([]))).current_interval() == 30


def test_start_and_stop_are_clean(settings, repo):
    async def scenario():
        s = dataclasses.replace(settings, poll_interval_open=60, poll_min_interval=30)
        p = mk(s, repo, Script(result([quote()])))
        p.start()
        await asyncio.sleep(0.2)
        await p.stop()
        return p._task, p.last_success_at

    task, ok = asyncio.run(scenario())
    assert task is None and ok is not None
