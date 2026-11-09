from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_health, routes_live, routes_market, routes_ohlcv, routes_quotes, routes_stocks
from app.api.deps import RateLimiter, RequestSizeLimitMiddleware, check_rate_limit, install_error_handlers

from app.collectors.base import QuoteCollector
from app.collectors.factory import open_collector
from app.config import Settings, get_settings
from app.database.repository import Repository
from app.logging_config import setup_logging
from app.services.polling import Poller
from app.services.stock_service import StockService

log = logging.getLogger(__name__)

DESCRIPTION = """
Local REST API serving EGX stock prices collected from the official **EGX Beta Market Watch** feed.

* Data is a **cached snapshot** collected by a background poller; requests never hit the source.
* `data_status` is `fresh`, `stale` or `unavailable`. A successful request alone never means *fresh*.
* Whether the source is real-time or delayed is **unverified** - rely on `source_timestamp`.
* Missing values are `null`. `null` is not zero.
* Market data collection only; **not investment advice**. Check EGX's terms before redistributing data.
"""


def create_app(settings: Optional[Settings] = None, collector: Optional[QuoteCollector] = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    repo = Repository.from_settings(settings)
    repo.init_schema()
    holder = {"poller": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            if settings.polling_enabled:
                coll = collector or await stack.enter_async_context(open_collector(settings))
                poller = Poller(coll, repo, settings)
                holder["poller"] = poller
                app.state.service.poller = poller
                poller.start()
            try:
                yield
            finally:
                if holder["poller"]:
                    await holder["poller"].stop()

    app = FastAPI(title="EGX Market Data API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    app.state.service = StockService(settings, repo)
    app.state.rate_limiter = RateLimiter(limit_per_minute=settings.rate_limit_per_minute)
    install_error_handlers(app)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_methods=["GET"], allow_headers=["*"])
    app.include_router(routes_health.router)
    for r in (routes_stocks.router, routes_quotes.router, routes_ohlcv.router, routes_market.router, routes_live.router):
        app.include_router(r, dependencies=[Depends(check_rate_limit)])
    return app




def __getattr__(name: str):
    """Lazy `app` so `uvicorn app.main:app` works without building an app at import time."""
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
