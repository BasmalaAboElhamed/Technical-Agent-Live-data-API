from __future__ import annotations

import dataclasses
from pathlib import Path

import httpx
import pytest

from app.collectors.egx_beta import EgxBetaCollector
from app.config import Settings
from app.database.repository import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(
        Settings(), database_path=tmp_path / "test.sqlite3", polling_enabled=False,
        page_size=2, max_retries=2, backoff_base=1.0, backoff_max=8.0, max_retry_after=30.0,
        poll_min_interval=1.0,
    )


@pytest.fixture
def repo(settings) -> Repository:
    r = Repository(settings.database_path, history_enabled=True, history_min_interval=300)
    r.init_schema()
    return r


class Recorder:
    """Records sleeps instead of waiting; deterministic jitter."""
    def __init__(self):
        self.sleeps = []

    async def sleep(self, s):
        self.sleeps.append(s)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_collector(settings, recorder):
    clients = []

    def _make(handler, **overrides):
        s = dataclasses.replace(settings, **overrides) if overrides else settings
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return EgxBetaCollector(s, client, sleep=recorder.sleep, rng=lambda: 1.0)

    yield _make
