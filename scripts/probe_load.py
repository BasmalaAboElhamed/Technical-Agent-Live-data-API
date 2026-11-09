"""Why is the page slow to load in automated Chromium? Read-only timeline (URLs without query strings, no headers/cookies).

    python scripts\\probe_load.py            # watches for 120 s
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.config import Settings  # noqa: E402


def short(url: str) -> str:
    u = urlsplit(url)
    return f"{u.netloc}{u.path[:70]}"


def main(seconds: int) -> None:
    s = Settings.from_env()
    t0 = time.time()
    started, finished, failed = {}, set(), []
    stamp = lambda: f"{time.time() - t0:6.1f}s"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=s.browser_headless)
        page = browser.new_page()
        page.on("request", lambda r: started.setdefault(r.url, (time.time() - t0, r.resource_type)))
        page.on("requestfinished", lambda r: finished.add(r.url))
        page.on("requestfailed", lambda r: (failed.append((short(r.url), r.failure)), print(f"{stamp()} FAILED {short(r.url)}: {r.failure}")))
        page.on("response", lambda r: print(f"{stamp()} {r.status} {r.request.resource_type:10s} {short(r.url)}") if r.status >= 400 or "/api/" in r.url else None)
        page.on("domcontentloaded", lambda _: print(f"{stamp()} -- DOMContentLoaded"))
        page.on("load", lambda _: print(f"{stamp()} -- load"))
        page.on("console", lambda m: print(f"{stamp()} console.{m.type}: {m.text[:100]}") if m.type == "error" else None)
        print(f"{stamp()} opening {s.page_url}")
        try:
            page.goto(s.page_url, wait_until="commit", timeout=60_000)
            print(f"{stamp()} -- server answered (commit)")
        except Exception as exc:  # noqa: BLE001
            print(f"{stamp()} goto failed: {type(exc).__name__}: {str(exc)[:150]}")
        page.wait_for_timeout(seconds * 1000)
        pending = [(u, v) for u, v in started.items() if u not in finished and short(u) not in {f[0] for f in failed}]
        browser.close()

    print(f"\n== {len(started)} requests started, {len(finished)} finished, {len(failed)} failed, {len(pending)} still pending")
    print("pending by host:", dict(Counter(urlsplit(u).netloc for u, _ in pending)))
    for u, (t, kind) in sorted(pending, key=lambda x: x[1][0])[:15]:
        print(f"   pending since {t:5.1f}s  {kind:10s} {short(u)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    main(ap.parse_args().seconds)
