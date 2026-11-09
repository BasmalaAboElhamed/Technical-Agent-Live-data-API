import asyncio
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from app.config import Settings
from app.collectors.passive import PassivePageCollector


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

async def test_robust_open():
    s = Settings.from_env()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=s.browser_headless)
        page = await (await browser.new_context(locale="en-US")).new_page()
        print("Navigating to", s.page_url)
        await page.goto(s.page_url, wait_until="commit", timeout=90000)
        
        trigger = page.locator("button").filter(has_text=re.compile(r"rows per page", re.I)).first
        await trigger.wait_for(timeout=60000)
        print("ROWS PER PAGE button found. Initial aria-expanded:", await trigger.get_attribute("aria-expanded"))
        
        # Wait until table renders (ensures page is hydrated and interactive)
        try:
            await page.locator("tbody tr, table tr").first.wait_for(timeout=30000)
            print("Table rows detected! Page is hydrated.")
        except Exception as e:
            print("Table rows wait timed out:", e)
            
        # Check Next button
        next_btn = page.locator(s.ui_next_selector).first
        print("Next button count by selector:", await next_btn.count())
        if await next_btn.count():
            print("Next button text:", await next_btn.inner_text(), "is_enabled:", await next_btn.is_enabled())
            
        # Try opening dropdown
        print("Clicking trigger...")
        await trigger.click()
        await page.wait_for_timeout(1000)
        expanded = await trigger.get_attribute("aria-expanded")
        print("aria-expanded after click:", expanded)
        
        opt_xpath = PassivePageCollector.option_xpath(50)
        opt = page.locator(opt_xpath).first
        print("Option 50 count with option_xpath:", await opt.count())
        if await opt.count():
            print("Option 50 visible:", await opt.is_visible())
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_robust_open())
