from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.collectors.base import QuoteCollector
from app.collectors.egx_beta import EgxBetaCollector, build_http_client
from app.collectors.passive import PassivePageCollector
from app.config import Settings


@asynccontextmanager
async def open_collector(settings: Settings) -> AsyncIterator[QuoteCollector]:
    """EGX_FETCH_MODE=passive (default: a real page does the requests, we only read) or http."""
    if settings.fetch_mode == "http":
        coll: QuoteCollector = EgxBetaCollector(settings, build_http_client(settings))
    else:
        coll = PassivePageCollector(settings)           # the browser starts lazily on the first cycle
    try:
        yield coll
    finally:
        await coll.aclose()
