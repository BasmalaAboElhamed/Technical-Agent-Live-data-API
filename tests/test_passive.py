"""PassivePageCollector with a FAKE page (no Chromium). Proves the cycle logic, pacing, response matching and failure
handling. It does NOT prove the real page's 'rows per page' / 'Next' controls are found: run scripts\\discover_ui.py and
scripts\\test_source.py on your machine for that."""
from __future__ import annotations

import asyncio
import builtins
import json
import time
import types
from pathlib import Path

import pytest

from app.collectors.base import SourceError, SourceUnavailable
from app.collectors.passive import PassivePageCollector
from tests.fixtures import synth

SIZE = 2


class FakeResp:
    def __init__(self, url, body, status=200):
        self.url, self._body, self.status = url, body, status

    async def json(self):
        return self._body


class FakeCtx:
    def __init__(self, page, pred):
        self.page, self.pred = page, pred

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        self.page.pending = (self.pred, loop.create_future())
        fut = self.page.pending[1]

        async def value():
            if self.page.drop:
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(fut, 1)
        return types.SimpleNamespace(value=value())

    async def __aexit__(self, *a):
        return False


class FakePage:
    """Mock site: `emit` plays the response the real page would receive after a UI action."""
    def __init__(self, items, size=SIZE):
        self.items, self.size, self.pending, self.drop = items, size, None, False
        self.reloads, self.next_clicks, self.size_selected, self.fail_page, self.status = 0, 0, 0, None, 200

    def expect_response(self, pred, timeout=None):
        return FakeCtx(self, pred)

    def emit(self, page_no, size=None):
        size = size or self.size
        pages = -(-len(self.items) // size)
        body = {"success": True, "data": {"data": self.items[(page_no - 1) * size: page_no * size], "pageNumber": page_no,
                                          "pageSize": size, "totalCount": len(self.items), "totalPages": pages}}
        url = f"https://beta.egx.com.eg/api/bff/egx/market-watch?Page={page_no}&PageSize={size}&SortBy=value&SortDescending=true"
        if self.fail_page == page_no:
            return
        pred, fut = self.pending
        r = FakeResp(url, body, self.status)
        if pred(r) and not fut.done():
            fut.set_result(r)


class Fake(PassivePageCollector):
    def __init__(self, settings, items, recorder):
        super().__init__(settings, sleep=recorder.sleep, rng=lambda: 0.5)
        self.fp = FakePage(items, settings.page_size)
        self._cur = 1

    async def _ensure_page(self):
        self._page, self._started = self.fp, True

    async def _ui_reload(self):
        self.fp.reloads += 1
        self._cur = 1

    async def _ui_set_rows_per_page(self, size):
        self.fp.size_selected += 1
        self.fp.emit(1)

    async def _ui_click_next(self):
        self.fp.next_clicks += 1
        self._cur += 1
        self.fp.emit(self._cur)


def make(settings, recorder, n=5):
    return Fake(settings, synth(n), recorder)


def test_one_cycle_sets_size_once_then_clicks_next_until_last_page(settings, recorder):
    c = make(settings, recorder)
    res = asyncio.run(c.collect())
    assert len(res.quotes) == 5 and res.complete and res.total_reported == 5
    assert (c.fp.reloads, c.fp.size_selected, c.fp.next_clicks) == (1, 1, 2)       # 3 pages -> 2 clicks, no extra request
    assert res.requests_made == 3


def test_actions_are_paced_with_a_delay_between_ui_actions(settings, recorder):
    asyncio.run(make(settings, recorder).collect())
    assert len(recorder.sleeps) == 3 and all(0.8 <= x <= 1.8 for x in recorder.sleeps)


def test_every_cycle_reloads_the_page_like_a_visitor_pressing_f5(settings, recorder):
    c = make(settings, recorder)
    asyncio.run(c.collect())
    asyncio.run(c.collect())
    assert c.fp.reloads == 2 and c.fp.size_selected == 2


def test_missing_later_page_gives_partial_collection_not_failure(settings, recorder):
    c = make(settings, recorder)
    c.fp.fail_page = 3
    c.fp.drop = False

    async def scenario():
        orig = c._act_and_capture

        async def flaky(action, page_no):
            if page_no == 3:
                await action()
                raise SourceUnavailable("the page did not return page 3")
            return await orig(action, page_no)
        c._act_and_capture = flaky
        return await c.collect()
    res = asyncio.run(scenario())
    assert len(res.quotes) == 4 and not res.complete and any("page 3" in e for e in res.errors)


def test_no_response_for_first_page_fails_and_restarts_browser(settings, recorder):
    c = make(settings, recorder)
    c.fp.drop = True
    with pytest.raises(SourceUnavailable) as e:
        asyncio.run(c.collect())
    assert "challenge" in str(e.value) and c._started is False


def test_non_200_response_to_the_page_is_an_error(settings, recorder):
    c = make(settings, recorder)
    c.fp.status = 404
    with pytest.raises(SourceError):
        asyncio.run(c.collect())


def test_response_matching_ignores_other_widgets_and_pages(settings, recorder):
    s = settings.__class__(**{**settings.__dict__, "page_size": 50})
    c = PassivePageCollector(s)
    ok = lambda q: c._wanted(types.SimpleNamespace(url="https://beta.egx.com.eg/api/bff/egx/market-watch?" + q), 2)
    assert ok("Page=2&PageSize=50&SortBy=value&SortDescending=true")
    assert ok("page=2&pagesize=50&sortby=Value")                        # case-insensitive
    assert not ok("Page=1&PageSize=50&SortBy=value")                    # wrong page
    assert not ok("Page=2&PageSize=10&SortBy=Volume&SortDescending=true")   # the page's other widget
    assert not c._wanted(types.SimpleNamespace(url="https://beta.egx.com.eg/api/bff/egx/market-status"), 1)


def test_market_status_is_captured_passively_and_expires(settings, recorder):
    c = make(settings, recorder)
    body = json.loads((Path(__file__).parent / "data" / "market_status_real.json").read_text(encoding="utf-8"))
    asyncio.run(c._on_response(FakeResp("https://beta.egx.com.eg/api/bff/egx/market-status", body)))
    ms = asyncio.run(c.fetch_market_status())
    assert ms.status == "Closed" and ms.is_open is False
    c._status_seen_at = time.monotonic() - 1000
    assert asyncio.run(c.fetch_market_status()) is None


def test_missing_playwright_gives_actionable_error(settings):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("x")
        return real(name, *a, **k)
    c = PassivePageCollector(settings)
    builtins.__import__ = fake
    try:
        with pytest.raises(SourceError) as e:
            asyncio.run(c._ensure_page())
    finally:
        builtins.__import__ = real
    assert "playwright install chromium" in str(e.value)


def test_rows_option_selector_is_tight_and_never_targets_table_cells_or_plain_spans():
    xp = PassivePageCollector.option_xpath(50)
    assert "not(ancestor::table)" in xp and "normalize-space(.)='50'" in xp
    assert "self::span" not in xp and "self::div" not in xp and "self::td" not in xp


def test_observed_egx_dropdown_option_contract():
    """Verify that the option XPath targets only valid option roles/elements outside tables."""
    xp = PassivePageCollector.option_xpath(50)
    # Must allow role='option' and button
    assert "@role='option'" in xp
    assert "self::button" in xp
    assert "not(ancestor::table)" in xp

