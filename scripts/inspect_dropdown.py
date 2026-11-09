import asyncio
import json
import re
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await (await browser.new_context(locale="en-US")).new_page()
        
        responses = []
        page.on("response", lambda r: responses.append((r.status, r.url)) if "/api/bff/" in r.url else None)
        
        print("Navigating to Market Watch...")
        await page.goto("https://beta.egx.com.eg/en/market/market-watch", wait_until="commit", timeout=90000)
        
        btn = page.locator("button").filter(has_text=re.compile(r"ROWS PER PAGE", re.I)).first
        await btn.wait_for(timeout=60000)
        print("Found ROWS PER PAGE button!")
        btn_html = await btn.evaluate("el => el.outerHTML")
        print(f"Button HTML: {btn_html}")
        
        # Wait a bit for initial data requests to finish
        print("Waiting for page responses...")
        await page.wait_for_timeout(5000)
        print(f"BFF responses so far: {responses}")
        
        # Check table
        table_rows = await page.locator("table tr").count()
        print(f"table tr count: {table_rows}")
        
        # Check pagination controls
        pagination_buttons = await page.locator("button, a").evaluate_all(
            """els => els.map(e => ({
                tag: e.tagName,
                text: (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' '),
                aria: e.getAttribute('aria-label'),
                cls: e.className
            })).filter(x => /next|prev|page|\\d+/i.test(x.text) || /next|prev|page/i.test(x.aria || ''))"""
        )
        print("Pagination / numeric buttons:")
        for pb in pagination_buttons[:20]:
            print("  ", pb)
            
        # Click ROWS PER PAGE button
        print("\nClicking ROWS PER PAGE button...")
        # Get body children count before
        before_count = await page.evaluate("document.body.children.length")
        await btn.click()
        await page.wait_for_timeout(2000)
        after_count = await page.evaluate("document.body.children.length")
        print(f"Body children before: {before_count}, after: {after_count}")
        
        # Look for newly opened popovers, modals, dropdowns
        dropdown_info = await page.evaluate('''() => {
            const els = Array.from(document.querySelectorAll('*'));
            const visible = els.filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && (e.textContent || '').includes('50');
            });
            return visible.map(e => ({
                tag: e.tagName,
                text: (e.innerText || '').trim(),
                cls: (e.className || '').toString().slice(0, 80),
                role: e.getAttribute('role'),
                parent: e.parentElement ? e.parentElement.tagName : null,
                html: e.outerHTML.slice(0, 150)
            })).slice(0, 15);
        }''')
        print("\nElements containing '50':")
        for d in dropdown_info:
            print("  ", json.dumps(d, ensure_ascii=True))
            
        # Also let's inspect any [role=listbox] or [role=option]
        listbox_info = await page.evaluate('''() => {
            const options = Array.from(document.querySelectorAll('[role=option], [role=listbox] *'));
            return options.map(e => ({
                tag: e.tagName,
                role: e.getAttribute('role'),
                text: (e.innerText || e.textContent || '').trim(),
                cls: (e.className || '').toString().slice(0, 80),
                aria_selected: e.getAttribute('aria-selected')
            })).filter(x => x.text.length < 20);
        }''')
        print(f"\nListbox options found ({len(listbox_info)}):")
        for o in listbox_info:
            print("  ", json.dumps(o, ensure_ascii=True))
            
        await page.screenshot(path="scripts/page_inspect.png")
        print("Screenshot saved to scripts/page_inspect.png")
        await browser.close()

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())

