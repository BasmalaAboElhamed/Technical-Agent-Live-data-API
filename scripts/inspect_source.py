"""
Phase One discovery helper for the EGX Beta Market Watch page.

What it does (read-only, polite, no auth bypass):
  1. GET the public Market Watch page and report status / content-type.
  2. GET /robots.txt and print it (robots.txt is NOT permission by itself).
  3. List the page's public JavaScript chunks and scan them for API-looking
     strings (paths containing /api/, 'market', 'watch', 'stock', 'quote'...).
  4. Print the candidates so you can confirm them in the browser DevTools
     Network tab (Fetch/XHR) while the real page loads.

It does NOT call any discovered endpoint. Verify candidates manually first,
and read the site's terms of use before polling anything automatically.

Run (PowerShell):
    pip install httpx
    python scripts/inspect_source.py
"""
from __future__ import annotations

import re
import sys
import time
from urllib.parse import urljoin

import httpx

BASE = "https://beta.egx.com.eg"
PAGE = f"{BASE}/en/market/market-watch"
HEADERS = {"User-Agent": "egx-live-api-inspector/0.1 (personal research; contact: local user)"}
MAX_CHUNKS = 40          # hard cap on how many JS files we download
DELAY_SECONDS = 0.7      # be gentle with the source

KEYWORDS = re.compile(r"(market|watch|stock|quote|price|security|instrument|ticker|trade)", re.I)
API_PATH = re.compile(r"""["'`](/api/[A-Za-z0-9_\-./{}$?=&%:]*)["'`]""")
ABS_URL = re.compile(r"""["'`](https?://[A-Za-z0-9_\-./{}$?=&%:]+)["'`]""")
SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
CHUNK_REF = re.compile(r"""["'](/?_next/static/[A-Za-z0-9_\-./%]+\.js)["']""")


def main() -> int:
    with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(10.0, read=20.0), follow_redirects=True) as c:
        try:
            r = c.get(PAGE)
        except httpx.HTTPError as exc:
            print(f"[FAIL] Could not reach {PAGE}: {exc!r}")
            return 1
        print(f"[page] {PAGE}\n  status={r.status_code} content-type={r.headers.get('content-type')}")
        print(f"  server-rendered rows? -> inspect manually; body length={len(r.text)}")

        try:
            rb = c.get(f"{BASE}/robots.txt")
            print(f"\n[robots.txt] status={rb.status_code}")
            print(rb.text[:1500] if rb.status_code == 200 else "  (not available)")
        except httpx.HTTPError as exc:
            print(f"[robots.txt] failed: {exc!r}")

        chunks: list[str] = []
        for m in SCRIPT_SRC.finditer(r.text):
            chunks.append(urljoin(BASE, m.group(1)))
        for m in CHUNK_REF.finditer(r.text):
            chunks.append(urljoin(BASE + "/", m.group(1).lstrip("/")))
        chunks = list(dict.fromkeys(u for u in chunks if u.startswith(BASE)))[:MAX_CHUNKS]
        print(f"\n[js] {len(chunks)} same-origin script files to scan (cap {MAX_CHUNKS})")

        api_paths: dict[str, set[str]] = {}
        abs_urls: dict[str, set[str]] = {}
        for url in chunks:
            time.sleep(DELAY_SECONDS)
            try:
                js = c.get(url)
            except httpx.HTTPError as exc:
                print(f"  skip {url}: {exc!r}")
                continue
            if js.status_code != 200:
                continue
            for m in API_PATH.finditer(js.text):
                api_paths.setdefault(m.group(1), set()).add(url.rsplit("/", 1)[-1])
            for m in ABS_URL.finditer(js.text):
                u = m.group(1)
                if "/api" in u or KEYWORDS.search(u):
                    abs_urls.setdefault(u, set()).add(url.rsplit("/", 1)[-1])

        print("\n[candidates] relative /api/ paths referenced by the page's JS:")
        for p in sorted(api_paths):
            flag = "  <-- keyword match" if KEYWORDS.search(p) else ""
            print(f"  {p}{flag}")
        print("\n[candidates] absolute URLs with api/market-like keywords:")
        for u in sorted(abs_urls):
            print(f"  {u}")

    print(
        "\nNext step: open the page in your browser, press F12 -> Network -> Fetch/XHR, "
        "reload, and note the request that returns the stock rows (URL, method, query "
        "parameters, JSON shape). Do NOT copy cookies or Authorization headers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
