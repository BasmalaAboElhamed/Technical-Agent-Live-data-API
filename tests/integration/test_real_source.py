"""REAL-SOURCE integration test. Skipped by default so CI/offline runs are never mistaken for live verification.

    $env:EGX_RUN_INTEGRATION = "1"; pytest tests/integration -v -s

If the source is unreachable the test is SKIPPED with the reason - it never passes silently.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.collectors.base import SourceError
from app.collectors.factory import open_collector
from app.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("EGX_RUN_INTEGRATION") != "1", reason="set EGX_RUN_INTEGRATION=1 to hit the real EGX source"),
]


@pytest.fixture(scope="module")
def result():
    async def go():
        s = Settings.from_env()
        async with open_collector(s) as coll:
            return await coll.collect()
    try:
        return asyncio.run(go())
    except SourceError as exc:
        pytest.skip(f"real source not usable right now (NOT a pass): {exc}")


def test_real_source_returns_unique_stocks(result):
    print(f"\nunique stocks={len(result.quotes)} pages={result.pages_fetched} complete={result.complete} "
          f"total_reported={result.total_reported} rejected={result.rejected} dup={result.duplicates}")
    assert len(result.quotes) > 0
    assert len({q.symbol for q in result.quotes}) == len(result.quotes)


def test_real_prices_are_numeric_or_null(result):
    priced = [q for q in result.quotes if q.last_price is not None]
    print(f"records with numeric last_price: {len(priced)}/{len(result.quotes)}")
    assert priced, "no record carried a numeric lastPrice"
    assert all(isinstance(q.last_price, float) and q.last_price >= 0 for q in priced)


def test_real_source_timestamps_present_and_not_in_future(result):
    from app.utils import utcnow
    stamped = [q for q in result.quotes if q.source_timestamp]
    print(f"records with writeTime: {len(stamped)}/{len(result.quotes)}; newest={result.source_timestamp_max}")
    assert stamped
    assert max(q.source_timestamp for q in stamped) <= utcnow().replace(microsecond=0) + __import__("datetime").timedelta(minutes=5)
