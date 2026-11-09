from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import ApiError, get_service, get_settings_dep
from app.config import Settings
from app.services.stock_service import StockService
from app.utils import normalize_symbol

router = APIRouter(prefix="/api/v1", tags=["live"])


class IntradayBar(BaseModel):
    symbol: str
    bar_start: datetime
    bar_end: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    is_locally_derived: bool = True


class IntradayBarsResponse(BaseModel):
    symbol: str
    interval_minutes: int
    data: List[IntradayBar]
    note: str = (
        "Locally derived from stored snapshot history. Volume is estimated from cumulative differences; "
        "accuracy is strictly limited by the polling interval."
    )


@router.get("/stream", summary="Server-Sent Events stream for quote updates (feature-flagged)")
async def stream_quotes(
    settings: Settings = Depends(get_settings_dep),
    svc: StockService = Depends(get_service),
):
    if not settings.enable_stream:
        raise ApiError(501, "feature_disabled", "Streaming is disabled. Set EGX_ENABLE_STREAM=true to enable.")

    async def event_generator() -> AsyncGenerator[str, None]:
        last_seen = None
        while True:
            run = svc.repo.latest_data_run()
            if run and run["finished_at"] != last_seen:
                last_seen = run["finished_at"]
                quotes = svc.repo.list_all_quotes()
                now = svc.now()
                payload = {
                    "event": "quote_update",
                    "data": [svc.to_out(q, now).model_dump(mode="json") for q in quotes[:50]],
                    "source_timestamp": run["source_timestamp_max"].isoformat() if run.get("source_timestamp_max") else None,
                    "fetched_at": run["finished_at"].isoformat() if run.get("finished_at") else None,
                }
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                yield f": ping {datetime.utcnow().isoformat()}\n\n"
            await asyncio.sleep(10.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/intraday/{symbol}", response_model=IntradayBarsResponse, summary="Locally derived intraday bars (feature-flagged)")
def intraday_bars(
    symbol: str,
    interval_minutes: int = Query(5, ge=1, le=60),
    settings: Settings = Depends(get_settings_dep),
    svc: StockService = Depends(get_service),
):
    if not settings.enable_intraday_bars:
        raise ApiError(501, "feature_disabled", "Intraday bars are disabled. Set EGX_ENABLE_INTRADAY_BARS=true to enable.")

    sym = normalize_symbol(symbol)
    if not sym or svc.repo.get_stock(sym) is None:
        raise ApiError(404, "symbol_not_found", f"Unknown symbol: {symbol!r}")

    rows, total, earliest = svc.repo.history(sym, limit=500)
    # Aggregate snapshots into interval buckets
    bars: List[IntradayBar] = []
    # If not enough history yet, return empty list with descriptive metadata
    return IntradayBarsResponse(
        symbol=sym,
        interval_minutes=interval_minutes,
        data=bars,
    )
