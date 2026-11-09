"""Real-source diagnostic for the EGX Beta Market Watch endpoints (read-only, polite).

    python scripts\\test_source.py                       # market-status + full collection + 3 spaced repeats
    python scripts\\test_source.py --repeats 5 --gap 60
    python scripts\\test_source.py --probe-page-size     # only with EGX_FETCH_MODE=http (passive mode uses the page's own sizes)
    $env:EGX_FETCH_MODE="http"                          # PowerShell: test plain HTTP instead (known to give an empty 404)

Default (passive) mode drives a real Chromium window like a visitor and only reads the responses the page receives
(needs `playwright install chromium`). Prints no cookies, tokens or
request headers. Nothing here is mocked: if the source is unreachable it says so and exits non-zero.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.base import SourceError  # noqa: E402
from app.collectors.egx_beta import EgxBetaCollector, extract_page  # noqa: E402
from app.collectors.passive import PassivePageCollector  # noqa: E402
from app.collectors.factory import open_collector  # noqa: E402
from app.config import Settings  # noqa: E402
from app.utils import utcnow  # noqa: E402

NEW = {"requests": 0, "retries": 0, "latencies_ms": []}


async def main(a) -> int:
    s = Settings.from_env()
    problems = []
    print(f"== mode={s.fetch_mode} headless={s.browser_headless} endpoint={s.source_url} page_size={s.page_size}")
    async with open_collector(s) as coll:
        # 2. raw first page ----------------------------------------------------------------------------
        stats = {"latencies_ms": [], "requests": 0, "retries": 0}
        t0 = time.perf_counter()
        try:
            payload = await coll._get_page(1, stats)     # passive: reload + choose rows-per-page, read the page's response
            items, total, pages = extract_page(payload)
        except SourceError as exc:
            print(f"[FAIL] first page failed: {exc}")
            print("  404/403 even inside the browser would mean the site gates this endpoint: do not bypass; ask EGX or use a licensed provider.")
            return 3
        print(f"page 1: {len(items)} records in {(time.perf_counter() - t0) * 1000:.0f} ms; reported totalCount={total} totalPages={pages}")
        ms = await coll.fetch_market_status()      # passive mode: the status the page itself fetched (may need ~30 s to appear)
        print(f"market-status: {ms.status if ms else 'not seen yet'}"
              + (f" (source local time {ms.status_date.isoformat()}; is_open={ms.is_open})" if ms else ""))
        if items:
            r0 = items[0]
            print("fields:", len(r0), sorted(r0))
            same_open = sum(1 for r in items if r.get("openPrice") == r.get("prevClose"))
            diff_close = sum(1 for r in items if r.get("lastPrice") != r.get("closePrice"))
            print(f"openPrice == prevClose in {same_open}/{len(items)} records  (if this is < 100% during a session, "
                  f"set EGX_TRUST_SOURCE_OPEN=true to expose a real Open)")
            print(f"lastPrice != closePrice in {diff_close}/{len(items)} records")
            print("sample:", json.dumps({k: r0.get(k) for k in ("reuters", "isin", "name", "lastPrice", "closePrice", "prevClose",
                  "openPrice", "high", "low", "volume", "value", "trades", "writeTime", "lastTradeDate")}, ensure_ascii=False))

        # 3. optional page-size probe ----------------------------------------------------------------
        if a.probe_page_size and isinstance(coll, PassivePageCollector):
            print("--probe-page-size is only for EGX_FETCH_MODE=http; passive mode can only use the sizes the page offers")
        elif a.probe_page_size:
            for size in (50, 100, 200):
                await asyncio.sleep(2)
                c2 = EgxBetaCollector(dataclasses.replace(s, page_size=size), coll.client)   # same session
                try:
                    p = await c2._get_page(1, {"latencies_ms": [], "requests": 0, "retries": 0})
                    print(f"PageSize={size}: {len(extract_page(p)[0])} records returned")
                except SourceError as exc:
                    print(f"PageSize={size}: refused ({exc})")

        # 4. full collection -------------------------------------------------------------------------------
        print("\n== full collection")
        try:
            res = await coll.collect()
        except SourceError as exc:
            print(f"[FAIL] collection failed: {exc}")
            return 5
        syms = {q.symbol for q in res.quotes}
        print(f"unique stock identifiers: {len(syms)} (rejected={res.rejected}, duplicates={res.duplicates}, conflicts={res.duplicate_conflicts})")
        print(f"pages={res.pages_fetched} requests={res.requests_made} retries={res.retries} reported_total={res.total_reported} "
              f"complete={res.complete} errors={res.errors}")
        if res.latencies_ms:
            print(f"latency ms: min={min(res.latencies_ms):.0f} median={statistics.median(res.latencies_ms):.0f} max={max(res.latencies_ms):.0f}")
        priced = [q for q in res.quotes if q.last_price is not None]
        print(f"numeric last_price: {len(priced)}/{len(res.quotes)} | close_price: {sum(q.close_price is not None for q in res.quotes)} | "
              f"high/low: {sum(q.high is not None and q.low is not None for q in res.quotes)} | volume: {sum(q.volume is not None for q in res.quotes)}")
        print(f"records with volume == 0: {sum(q.volume == 0 for q in res.quotes)} (an all-traded feed has none: coverage may be 'traded today' only)")
        types = {}
        for q in res.quotes:
            types[(q.symbol_type, q.currency)] = types.get((q.symbol_type, q.currency), 0) + 1
        print("instrument types (symbol_type, currency):", types)
        now = utcnow()
        if res.source_timestamp_max:
            print(f"newest writeTime: {res.source_timestamp_max.isoformat()}  local fetch time: {now.isoformat(timespec='seconds')}  age={now - res.source_timestamp_max}")
            ts = {q.source_timestamp for q in res.quotes if q.source_timestamp}
            print(f"distinct writeTime values: {len(ts)} ({'one snapshot-wide time' if len(ts) == 1 else 'per-record times'})")
        else:
            problems.append("no source timestamps")
        if not priced:
            problems.append("no numeric prices")

        # 5. repeats -------------------------------------------------------------------------------------------
        if a.repeats > 0:
            print(f"\n== {a.repeats} repeat(s), {max(a.gap, 30):.0f}s apart: does the source change? (judge only DURING a session)")
            base = {q.symbol: (q.last_price, q.close_price, q.high, q.low, q.volume, q.source_timestamp) for q in res.quotes}
            for i in range(a.repeats):
                await asyncio.sleep(max(a.gap, 30))
                try:
                    again = await coll.collect()
                    ms2 = await coll.fetch_market_status()
                except SourceError as exc:
                    print(f"  repeat {i + 1}: FAILED {exc}")
                    problems.append("repeat failed")
                    continue
                changed = sum(1 for q in again.quotes if base.get(q.symbol) != (q.last_price, q.close_price, q.high, q.low, q.volume, q.source_timestamp))
                print(f"  repeat {i + 1}: {len(again.quotes)} stocks, {changed} changed, newest writeTime={again.source_timestamp_max}, "
                      f"market-status={ms2.status if ms2 else '?'}, {again.duration_seconds:.1f}s")
                base = {q.symbol: (q.last_price, q.close_price, q.high, q.low, q.volume, q.source_timestamp) for q in again.quotes}

    print("\nNOTE: robots.txt/terms were NOT checked by this script. Read EGX's terms of use on automated access and redistribution.")
    print("== RESULT")
    if problems:
        print("PROBLEMS:", "; ".join(problems))
        return 6
    print("source reachable and parsed. Live-ness during a trading session still needs your own repeat check.")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--gap", type=float, default=60.0, help="seconds between repeats (minimum enforced: 30)")
    ap.add_argument("--probe-page-size", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args())))

