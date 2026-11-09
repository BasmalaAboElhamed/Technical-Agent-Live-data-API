"""Passive page collector.

The source's JSON endpoints answer ordinary HTTP clients, and even fetch() calls made from the page's console, with an
empty 404, but they answer the site's OWN page. So this collector never calls the endpoints. It opens the public Market
Watch page in a real Chromium window, uses the page like a visitor (choose "rows per page", click "Next") and only READS the
JSON responses the page itself receives. No headers, cookies or tokens are touched; no CAPTCHA is solved: if the page does
not answer, the collector reports failure and you stop.

One collection cycle = reload the page, choose the page size, click "Next" until the last page. Clicks are paced with a short
random delay. All parsing, de-duplication and completeness logic is inherited from EgxBetaCollector (it just calls _get_page).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time as _time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from app.collectors.base import MalformedResponse, MarketStatus, SourceError, SourceUnavailable
from app.collectors.egx_beta import EgxBetaCollector
from app.config import Settings
from app.utils import utcnow

log = logging.getLogger(__name__)

STATUS_MAX_AGE_SECONDS = 150      # the page itself polls market-status every ~30 s


class PassivePageCollector(EgxBetaCollector):
    def __init__(
        self,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = utcnow,
    ):
        super().__init__(settings, client=None, sleep=sleep, rng=rng, clock=clock)  # type: ignore[arg-type]
        self._pw = None
        self._browser = None
        self._page = None
        self._status: Optional[MarketStatus] = None
        self._status_seen_at = 0.0
        self._started = False
        self._cycle_count = 0

    # ------------------------------------------------------------------ browser lifecycle
    async def _on_crash(self) -> None:
        log.warning("Chromium page crashed; resetting browser state")
        await self.aclose()

    async def _on_disconnect(self) -> None:
        log.warning("Chromium browser disconnected; resetting browser state")
        await self.aclose()

    async def _ensure_page(self) -> None:
        # Bounded memory: gracefully recycle browser after configured max cycles
        if self._started and self._cycle_count >= self.settings.browser_max_cycles:
            log.info("recycling browser after %d cycles to bound memory", self._cycle_count)
            await self.aclose()

        if self._started:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceError("Playwright is not installed: `pip install playwright` then `playwright install chromium`") from exc
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.settings.browser_headless)
            self._browser.on("disconnected", lambda: asyncio.create_task(self._on_disconnect()))
            ctx = await self._browser.new_context(locale="en-US")
            self._page = await ctx.new_page()
            self._page.on("crash", lambda: asyncio.create_task(self._on_crash()))
            self._page.on("response", lambda r: asyncio.ensure_future(self._on_response(r)))
            # "commit" = the server answered; the page itself can take a long time to render, so we wait for the CONTROLS
            # (see _wait_ready) instead of the DOMContentLoaded event, which may not fire for a very long time.
            await self._page.goto(self.settings.page_url, wait_until="commit", timeout=90_000)
        except Exception as exc:  # noqa: BLE001
            await self.aclose()
            raise SourceError(f"could not open the Market Watch page in Chromium: {exc!r} "
                              "(did you run `playwright install chromium`?)") from exc
        self._started = True
        self._cycle_count = 0
        log.info("passive browser session ready (headless=%s, max_cycles=%d)",
                 self.settings.browser_headless, self.settings.browser_max_cycles)

    async def aclose(self) -> None:
        self._started = False
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = self._pw = self._page = None


    async def _on_response(self, resp) -> None:
        """Listen only: remember the market-status the page itself fetched."""
        try:
            if urlsplit(resp.url).path.rstrip("/").endswith("/market-status") and resp.status == 200:
                ms = self.parse_market_status(await resp.json())
                if ms:
                    self._status, self._status_seen_at = ms, _time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    async def fetch_market_status(self) -> Optional[MarketStatus]:
        if self._status and _time.monotonic() - self._status_seen_at <= STATUS_MAX_AGE_SECONDS:
            return self._status
        return None

    # ------------------------------------------------------------------ page-level steps (overridable in tests)
    async def _pace(self) -> None:
        s = self.settings
        await self._sleep(s.ui_action_delay_min + (s.ui_action_delay_max - s.ui_action_delay_min) * self._rng())

    async def _ui_reload(self) -> None:
        await self._page.reload(wait_until="commit", timeout=90_000)

    async def _wait_ready(self) -> None:
        """Wait (long) until the page has rendered the pagination controls and table rows."""
        p = self._page
        try:
            await p.get_by_text(re.compile(r"rows per page", re.I)).first.wait_for(
                timeout=int(self.settings.ui_page_ready_timeout * 1000))
        except Exception:  # noqa: BLE001 - maybe a native <select> without that label; the strategies below decide
            log.warning("did not see a 'Rows per page' label within %.0fs", self.settings.ui_page_ready_timeout)
        try:
            # Wait for table rows to confirm React hydration is complete
            await p.locator("tbody tr, table tr").first.wait_for(timeout=30_000)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def option_xpath(size: int) -> str:
        """An OPTION of the rows-per-page menu: a list item / button / role=option|menuitem whose whole text is the size.
        Deliberately excludes table cells and plain spans/divs, so a price or volume equal to the size is never clicked."""
        return (f"xpath=//*[(self::li or self::button or @role='option' or @role='menuitem' or @role='radio')"
                f" and not(ancestor::table) and normalize-space(.)='{size}']")

    async def _ui_set_rows_per_page(self, size: int) -> None:
        """The real page has a custom dropdown BUTTON whose text is 'ROWS PER PAGE 25' (verified with discover_ui.py)."""
        p = self._page
        await self._wait_ready()
        sel_css = self.settings.ui_rows_selector
        trigger = p.locator(sel_css).first if sel_css else p.locator("button").filter(
            has_text=re.compile(r"rows per page", re.I)).first
        if not await trigger.count():
            raise SourceError("rows-per-page button not found: run `python scripts\\discover_ui.py` and set EGX_UI_ROWS_SELECTOR")
        current = re.search(r"(\d+)\s*$", (await trigger.inner_text()).strip())
        if current and int(current.group(1)) == size:
            raise SourceError(f"the page already shows {size} rows, so changing it would not trigger a request")
        await trigger.click()
        await self._pace()

        # If aria-expanded is still false, retry clicking once to ensure dropdown opens
        if await trigger.get_attribute("aria-expanded") == "false":
            await trigger.click()
            await self._pace()

        option = p.locator(self.option_xpath(size)).first
        if not await option.count():
            # Scoped fallback strictly inside [role=listbox] to prevent clicking any table cells or prices
            option = p.locator(f"[role=listbox] [role=option]:has-text('{size}'), [role=listbox] button:has-text('{size}')").first

        try:
            await option.wait_for(state="visible", timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"no '{size}' option appeared after opening rows-per-page: run "
                              "`python scripts\\discover_ui.py --open-rows` and send the output") from exc
        await option.click()


    async def _ui_click_next(self) -> None:
        p = self._page
        btn = p.locator(self.settings.ui_next_selector).first
        if not await btn.count():
            btn = p.get_by_role("button", name=re.compile(r"next", re.I)).first
        if not await btn.count():
            raise SourceError("'Next' control not found: run `python scripts\\discover_ui.py` and set EGX_UI_NEXT_SELECTOR")
        if not await btn.is_enabled():
            raise SourceError("'Next' is disabled before the last page was reached")
        await btn.click()

    def _wanted(self, resp, page_no: int) -> bool:
        u = urlsplit(resp.url)
        if not u.path.rstrip("/").endswith("/market-watch"):
            return False
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        return (q.get("page") == str(page_no) and q.get("pagesize") == str(self.settings.page_size)
                and q.get("sortby", "").lower() == self.settings.sort_by.lower())

    async def _act_and_capture(self, action: Callable[[], Awaitable[None]], page_no: int) -> Any:
        """Run one UI action and return the JSON of the matching response the PAGE received."""
        timeout_ms = int(self.settings.ui_response_timeout * 1000)
        try:
            async with self._page.expect_response(lambda r: self._wanted(r, page_no), timeout=timeout_ms) as info:
                await action()
            resp = await info.value
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - timeout, closed page, ...
            await self.aclose()        # ensure old browser is cleanly killed and freed
            raise SourceUnavailable(f"the page did not return page {page_no} ({type(exc).__name__}): "
                                    "a challenge, a changed layout or a slow connection?") from exc
        if resp.status != 200:
            raise SourceError(f"HTTP {resp.status} returned to the page for page {page_no}")
        try:
            return await resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MalformedResponse("page response was not valid JSON") from exc

    # ------------------------------------------------------------------ the hook EgxBetaCollector.collect() calls
    async def _get_page(self, page: int, stats: dict) -> Any:
        t0 = _time.perf_counter()
        if page == 1:
            self._cycle_count += 1
            await self._ensure_page()
            await self._ui_reload()
            await self._pace()
            payload = await self._act_and_capture(lambda: self._ui_set_rows_per_page(self.settings.page_size), 1)
        else:
            await self._pace()
            payload = await self._act_and_capture(self._ui_click_next, page)

        stats["requests"] += 1
        stats["latencies_ms"].append((_time.perf_counter() - t0) * 1000)
        return payload
