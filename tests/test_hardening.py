from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from app.collectors.base import CollectionResult
from app.config import Settings
from app.logging_config import SecretMaskingFilter
from app.main import create_app
from app.services.polling import Poller


def test_secret_masking_filter():
    f = SecretMaskingFilter()
    r = logging.LogRecord("test", logging.INFO, "test.py", 10, "User API key: x-api-key=secret123456 in request", (), None)
    assert f.filter(r)
    assert "secret123456" not in r.msg
    assert "[REDACTED]" in r.msg

    r2 = logging.LogRecord("test", logging.INFO, "test.py", 10, "Auth header: Bearer abcdef123456789", (), None)
    assert f.filter(r2)
    assert "abcdef123456789" not in r2.msg
    assert "Bearer [REDACTED]" in r2.msg


def test_metrics_endpoints(settings, repo):
    app = create_app(settings)
    with TestClient(app) as client:
        res1 = client.get("/api/v1/metrics")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["service"] == "egx-live-api"
        assert "uptime_seconds" in data1
        assert "poller" in data1
        assert "database" in data1
        assert data1["data_timing"] == "unverified"

        # Alias /metrics
        res2 = client.get("/metrics")
        assert res2.status_code == 200
        assert res2.json()["service"] == "egx-live-api"


def test_feature_flags_disabled_by_default(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        res_stream = client.get("/api/v1/stream")
        assert res_stream.status_code == 501
        assert res_stream.json()["error"]["code"] == "feature_disabled"

        res_intra = client.get("/api/v1/intraday/COMI")
        assert res_intra.status_code == 501
        assert res_intra.json()["error"]["code"] == "feature_disabled"


def test_poller_watchdog_timeout(settings, repo):
    collector = AsyncMock()

    async def hang():
        await asyncio.sleep(100)


    collector.collect = hang
    collector.fetch_market_status = AsyncMock(return_value=None)
    collector.aclose = AsyncMock()

    # set very short cycle timeout for test
    test_settings = settings.__class__(**{**settings.__dict__, "cycle_timeout": 0.05})
    poller = Poller(collector, repo, test_settings)

    status = asyncio.run(poller.run_once())
    assert status == "failed"
    assert "watchdog timeout" in poller.last_error
    collector.aclose.assert_awaited()
    assert poller.failure_count == 1
    assert poller.total_cycles == 1
