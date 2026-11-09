"""READ-ONLY look at the Market Watch page's controls (no clicks, no extra requests).

It lists selects, buttons, pagination elements and "rows per page"/"next" labels, then checks whether the collector's
default strategies would find the "rows per page" and "Next" controls. Output also goes to scripts\\ui_report.json.
Send me the printed output (it contains page text only: no cookies, tokens or headers).

    python scripts\\discover_ui.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.config import Settings  # noqa: E402

JS = """
() => {
  const txt = e => (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
  const info = e => ({tag: e.tagName.toLowerCase(), id: e.id || null,
    cls: (e.className && e.className.toString().slice(0, 80)) || null, role: e.getAttribute('role'),
    aria: e.getAttribute('aria-label'), text: txt(e),
    disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'});
  const re = /rows per page|page size|per page|next|previous|first page|last page/i;
  return {
    selects: [...document.querySelectorAll('select')].map(s => ({...info(s), options: [...s.options].map(o => o.text.trim()), value: s.value})),
    buttons: [...document.querySelectorAll('button,[role=button],[role=combobox],[role=option],[role=tab]')].map(info).slice(0, 150),
    labels: [...document.querySelectorAll('*')].filter(e => e.children.length === 0 && re.test(e.textContent || '') && (e.textContent || '').length < 40).map(info).slice(0, 40),
    navs: [...document.querySelectorAll('nav,[aria-label*=pagination i]')].map(info),
    table_rows: document.querySelectorAll('tbody tr').length,
    title: document.title,
  };
}
"""


OPTIONS_JS = """
() => {
  const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const chain = e => { const out = []; let n = e; for (let i = 0; i < 4 && n; i++, n = n.parentElement) out.push(n.tagName.toLowerCase() + (n.getAttribute('role') ? '[' + n.getAttribute('role') + ']' : '')); return out.join(' < '); };
  const numeric = [...document.querySelectorAll('*')].filter(e => e.children.length === 0 && /^\\s*\\d{1,3}\\s*$/.test(e.textContent || '') && vis(e));
  const inTable = e => !!e.closest('table');
  return {
    numeric_outside_tables: numeric.filter(e => !inTable(e)).map(e => ({tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), text: e.textContent.trim(), chain: chain(e)})).slice(0, 40),
    menus: [...document.querySelectorAll('[role=listbox],[role=menu],[role=option],[role=menuitem],[role=radio]')].filter(vis).map(e => ({tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), text: (e.innerText || '').trim().slice(0, 40)})).slice(0, 30),
  };
}
"""


async def open_rows(page, s) -> None:
    print("\n== --open-rows: clicking ONLY the 'ROWS PER PAGE' button (no option is selected)")
    trigger = page.locator("button").filter(has_text=re.compile("rows per page", re.I)).first
    if not await trigger.count():
        print("   trigger button not found")
        return
    await trigger.click()
    try:
        await page.locator("[role=listbox]").wait_for(timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)
    rep = await page.evaluate(OPTIONS_JS)
    print("   role-based menu items:"); [print("     ", x) for x in rep["menus"]]
    print("   short numeric elements outside tables (these include the options):"); [print("     ", x) for x in rep["numeric_outside_tables"]]
    xp = f"xpath=//*[(self::li or self::button or @role='option' or @role='menuitem' or @role='radio') and not(ancestor::table) and normalize-space(.)='{s.page_size}']"
    print(f"   collector's option selector matches for size {s.page_size}:", await page.locator(xp).count())
    await page.keyboard.press("Escape")



async def try_refresh(page) -> None:
    print("\n== --try-refresh: clicking the page's own 'Refresh' button once; listing the requests it makes")
    seen = []
    page.on("response", lambda r: seen.append((r.status, r.url.split("beta.egx.com.eg")[-1])) if "/api/bff/" in r.url else None)
    btn = page.locator("button").filter(has_text=re.compile(r"^\s*Refresh\s*$", re.I)).first
    if not await btn.count():
        print("   Refresh button not found")
        return
    await btn.click()
    await page.wait_for_timeout(6000)
    print("   requests after Refresh:"); [print("     ", x) for x in seen] if seen else print("      (none)")


async def main(args) -> int:
    s = Settings.from_env()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=s.browser_headless)
        page = await (await browser.new_context(locale="en-US")).new_page()
        print(f"opening {s.page_url} (do not click anything; this can take up to 2 minutes) ...")
        await page.goto(s.page_url, wait_until="commit", timeout=90_000)      # do not wait for DOMContentLoaded (slow page)
        try:
            await page.get_by_text(re.compile("rows per page", re.I)).first.wait_for(timeout=int(s.ui_page_ready_timeout * 1000))
        except Exception:  # noqa: BLE001
            print("no 'Rows per page' text appeared within the wait; reporting what is on the page anyway")
        
        # Wait for table and pagination controls (Next/Previous) to render
        try:
            await page.locator(s.ui_next_selector).first.wait_for(timeout=30_000)
        except Exception:
            try:
                await page.locator("tbody tr, table tr").first.wait_for(timeout=20_000)
            except Exception:
                pass
        await page.wait_for_timeout(2_000)
        report = await page.evaluate(JS)


        # would the collector's default strategies find the controls?
        next_css = await page.locator(s.ui_next_selector).count()
        next_role = await page.get_by_role("button", name=re.compile("next", re.I)).count()
        label = await page.get_by_text(re.compile("rows per page", re.I)).count()
        native = [x for x in report["selects"] if str(s.page_size) in x["options"]]
        report["strategy_check"] = {
            "next_by_selector": next_css, "next_by_role": next_role, "rows_per_page_label_matches": label,
            "native_selects_offering_page_size": len(native), "wanted_page_size": s.page_size,
        }
        if args.open_rows:
            await open_rows(page, s)
        if args.try_refresh:
            await try_refresh(page)
        await browser.close()

    Path(__file__).with_name("ui_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"title: {report['title']!r}   table rows visible: {report['table_rows']}")
    print("\n== <select> elements"); [print("  ", x) for x in report["selects"]] or None
    print("\n== labels mentioning rows/page/next/previous/first/last"); [print("  ", x) for x in report["labels"]]
    print("\n== pagination navs"); [print("  ", x) for x in report["navs"]]
    print("\n== buttons / comboboxes / options (first 150)")
    for x in report["buttons"]:
        print("  ", {k: v for k, v in x.items() if v not in (None, False, "")})
    print("\n== STRATEGY CHECK:", report["strategy_check"])
    ok = (next_css or next_role) and (native or label)
    print("RESULT:", "defaults should find both controls" if ok else "defaults will NOT find a control: send me this output")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--open-rows", action="store_true", help="click only the ROWS PER PAGE button and list the menu")
    ap.add_argument("--try-refresh", action="store_true", help="click the page's Refresh button once and list its requests")
    sys.exit(asyncio.run(main(ap.parse_args())))

