# EGX Market Data API

A local REST API that reads the **EGX Beta Market Watch** data, stores it in SQLite and serves quotes and **daily OHLCV bars**
through FastAPI (Swagger at `/docs`), for your investment-analysis project.

> **Market-data collection only. Not investment advice.** Check EGX's terms of use before polling continuously or sharing the data.

## What was verified, and what was not

**Observed in a real capture (2026-10-25, market closed, recorded with `scripts/probe_browser.py`):**

| Fact | Evidence |
|---|---|
| `GET /api/bff/egx/market-watch?Page=&PageSize=&SortBy=value&SortDescending=true` returns HTTP 200 **to the site's own page in a browser** | probe output |
| The same URL returns an **empty HTTP 404 to plain `curl.exe`, to `httpx`, to the collector's in-page `fetch()`, and to `fetch()` typed into Chrome's own DevTools console on the site** (with and without `Accept`). Only requests made by the site's own page code get 200. The reason (a header or value the page adds) is unknown | your diagnostic runs and console test |
| Envelope: `{success, message, totalCount, ..., data: {data: [records], pageNumber, pageSize, totalCount, totalPages}}`; 224 records, 9 pages of 25 or 5 pages of 50. `PageSize` 10, 25, 50 were served; 100+ untested | probe files |
| Sorting by `value` desc gives a stable order: 9 pages x 25 = 224 **unique** ISINs; identical repeat fetches | analysed |
| 48 fields per record, **no bid / ask / change** fields | analysed |
| `GET /api/bff/egx/market-status` -> `{"data": {"status": "Closed", "status_Ar": "...", "statusDate": "2026-10-25T19:45:36"}}`; the page polls it every **30 s**. Only `Closed` has been seen | probe output |
| `statusDate` matches Cairo local time; `writeTime` (`202610251534`) is the same for all 224 records (snapshot-wide) | analysed |

**Facts about the fields that shape the OHLCV output:**

* `openPrice == prevClose` in **224/224** records, and lies outside `[low, high]` in 90 of them. It is a placeholder, so **`open` is `null`** (opt in with `EGX_TRUST_SOURCE_OPEN=true` only after verifying in-session).
* `closePrice` differs from `lastPrice` in 39/224 records; the source's `chgPer` is consistent with `closePrice` (39/39), so both are exposed (`close_price`, `last_price`).
* `lastTradeDate` is the **previous** session's date (it matches the date of `prevClose`), not the date of the current figures. Bars use the Cairo date of `writeTime` instead.
* All 224 records have `volume > 0`: the feed seems to list only instruments that **traded** in the session. That is an inference; the feed is **not** the full listed universe.
* 9 records are priced in USD, 1 is a corporate bond (`symbolType B`); both are kept and labelled.

**NOT verified:**

* **"Live".** In 2 minutes with the market closed, `market-watch` never refreshed by itself, so nothing is known about real-time vs delayed or the update rate. `data_timing` is reported as `"unverified"` everywhere. Run `scripts\test_source.py` **during a session**.
* Whether the default selectors find the page's **"Rows per page"** and **"Next"** controls (the author cannot see the page). Run `.\run.ps1 -Discover` and then `-Diagnose`.
* That the collector works end to end on your machine (the author's sandbox cannot reach the site or install Chromium).
* Headless Chromium, `PageSize` above 50, other `market-status` values (only `Closed`), trading hours (Sun-Thu 10:00-14:30 Cairo is configuration), holidays.
* EGX's **terms of use** on automated access and redistribution (not reviewed). No access control is bypassed; on HTTP 401/403 the collector stops.

Unit tests use mocked HTTP, a fake browser page, and a recorded real response (`tests/data/`). Example JSON below is **illustrative**, not a market response.

## How data is read ("passive mode")

The endpoints refuse every request that is not made by the site's own page (see the table above), so this project does **not** call them.
`EGX_FETCH_MODE=passive` (default) opens the public Market Watch page in a real Chromium window and **uses it like a visitor**:
reload, choose "Rows per page" = `EGX_PAGE_SIZE`, click "Next" until the last page, with a short random pause between actions.
The collector only **reads the JSON responses the page itself receives** (and the `market-status` the page polls every ~30 s).
It adds no headers, copies no cookies or tokens, and does not solve CAPTCHAs: if the page does not answer, the cycle fails and you stop.

* One cycle = 1 page reload (the page makes a few requests of its own) + 1 rows-per-page change + (pages - 1) "Next" clicks. At size 50 that is roughly 10 requests per cycle, hence the 120 s in-session default.
* The Chromium window must stay open while the API runs. Do not click in it while a cycle runs.
* The controls are found by guesses (a native `<select>` or the control after the "Rows per page" label; a "Next" button). If `-Discover` says they are not found, set `EGX_UI_ROWS_SELECTOR` / `EGX_UI_NEXT_SELECTOR` or send the output of `-Discover`.
* `EGX_FETCH_MODE=http` (plain HTTP) is kept for completeness; it gets the empty 404.

## Setup (Windows 11, PowerShell)

```powershell
cd egx_live_api
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned   # this window only
Unblock-File .\run.ps1                                             # if the folder came from a downloaded zip
.\run.ps1 -Setup        # venv, requirements, Chromium for Playwright, .env
.\run.ps1 -Discover     # read-only check that the page's controls are found (no clicks)
```

Manual equivalent: `python -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, `pip install -r requirements.txt`, `playwright install chromium`, `Copy-Item .env.example .env`.

## Run

| Goal | Command |
|---|---|
| Check the page's controls (read-only) | `.\run.ps1 -Discover` |
| Verify the real source (best **during a session**) | `.\run.ps1 -Diagnose` |
| One real collection into SQLite | `.\run.ps1 -Once` |
| Start the API | `.\run.ps1` or `uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| Swagger UI | http://127.0.0.1:8000/docs |
| Unit tests (mocked / recorded) | `.\run.ps1 -Test` |
| Real-source integration tests | `.\run.ps1 -Integration` |
| Stop | `Ctrl+C` (the poller finishes its cycle, the browser closes) |

## Endpoints

Errors: `{"error": {"code", "message", "details"}}`. `limit` default 100, max 500.

| Endpoint | Parameters | Notes |
|---|---|---|
| `GET /health` | | ok / degraded / unavailable |
| `GET /api/v1/status` | | connectivity, freshness, market status as reported by the source, last fetch, counts, errors, complete vs partial |
| `GET /api/v1/stocks`, `/stocks/{symbol}` | `search`, `limit`, `offset` | directory; `COMI`, `comi`, `COMI.CA` all work |
| `GET /api/v1/quotes`, `/quotes/{symbol}` | `search`, `limit`, `offset`, `sort_by`, `order` | latest quote with `data_status` |
| `GET /api/v1/quotes/{symbol}/history` | `start`, `end`, `limit`, `offset` | thinned local snapshots |
| **`GET /api/v1/ohlcv/daily`** | `date` (default latest), `search`, `limit`, `offset` | **OHLCV for all stocks on one trade date** |
| **`GET /api/v1/ohlcv/daily/{symbol}`** | `start`, `end`, `limit`, `offset` | one stock's daily bars |
| `GET /api/v1/market/summary` | | locally computed **estimate** (`official: false`) |

### OHLCV bar fields

`symbol, isin, company_name, trade_date, open, open_observed, high, low, close, last_price, previous_close, volume, traded_value, number_of_trades, currency, is_final, source_timestamp`

* `high`, `low`, `volume`, `traded_value`, `number_of_trades`: the source's day-aggregate values.
* `close` = source `closePrice`; `last_price` = source `lastPrice` (last trade).
* `open` = **null** (see above). `open_observed` = the first `last_price` this installation saw that day: a proxy that equals the true open only if the collector was running at the session open. Never treat it as the official open.
* `trade_date` = Cairo date of `writeTime`; a bar is only created for snapshots written on a trading day at/after the session open. `is_final` = written at/after the configured close.
* History starts when you first run the collector. **Daily bars only; intraday candles are not built.**

### Quote fields

`symbol` (Reuters code without `.CA`), `isin`, `company_name`, `company_name_ar`, `sector`, `sector_ar`, `symbol_type`, `type_name`, `exchange`, `currency` (`EGP`/`USD` only when the source says so), `last_price`, `close_price`, `previous_close`, `change` (always null), `change_percent`, `open` (null), `high`, `low`, `volume`, `traded_value`, `number_of_trades`, `bid`/`ask` (always null: the fields do not exist), `last_trade_date` (previous session; informational), `source_timestamp`, `fetched_at`, `data_status`, `status_reason`, `age_seconds`.

`null` = not supplied; `0` = a genuine zero. Identity is the ISIN; two ISINs sharing a symbol are never merged.

```json
{"data": [{"symbol": "EXMP", "isin": "EGS00000X000", "trade_date": "2026-01-01", "open": null, "open_observed": 10.1,
  "high": 10.8, "low": 10.0, "close": 10.5, "last_price": 10.5, "previous_close": 10.0, "volume": 120000,
  "is_final": true, "source_timestamp": "2026-01-01T12:34:00Z"}], "pagination": {"total": 1, "limit": 100, "offset": 0, "returned": 1, "has_more": false}}
```
(illustrative example, not a real market response)

## Freshness

A successful request never makes data fresh. At read time: `unavailable` (no price) -> `stale` (no timestamp, collector not confirming recently, or timestamp in the future) -> during a session `fresh` if `source_timestamp` is within 300 s, else `stale` -> when closed, `fresh` if the timestamp is at/after the last session close (30 min grace). "Open/closed" comes from the source's `market-status` when recent and recognised (`Closed`; `Open` is assumed), otherwise from the configured calendar; list holidays in `EGX_HOLIDAYS`. Dataset state: `complete` (all pages, total matched), `partial`, `empty`.

## Polling

120 s in session, 900 s outside it (wakes at the open), hard floor 30 s: **cautious guesses, not documented limits.** One cycle at a time, backoff with jitter, `Retry-After` honoured, failures and empty/malformed responses never overwrite stored data, a SQLite lease prevents a second process from polling, run **one** worker.

## Database

`data\egx.sqlite3` (WAL; never expose it). Tables: `stocks`, `quotes_latest`, `quote_history` (thinned, 30 days), `daily_bars` (kept), `collection_runs`, `poller_lease`. Databases from earlier releases are upgraded automatically. Daily bars are small (about 224 rows per trading day).

## Use from your project

```powershell
$env:EGX_API_BASE_URL = "http://127.0.0.1:8000"
python examples\egx_client.py
```

`examples/egx_client.py` has `EgxClient`, `is_usable()` and error handling (connection errors raise instead of returning stale or zero prices). Treat `last_price is None` as unavailable, use `data_status == "fresh"`, check `meta.dataset_complete`. To use a licensed provider later, implement `QuoteCollector` (`app/collectors/base.py`).

## Configuration and security

Environment variables only (`.env.example`); no secrets. No authentication: keep it on `127.0.0.1`. CORS off by default.

## Troubleshooting

| Symptom | Action |
|---|---|
| "Playwright is not installed" / Chromium missing | `pip install playwright`, `playwright install chromium` |
| "rows-per-page control ... not found" / "'Next' control not found" | `.\run.ps1 -Discover`, then set `EGX_UI_ROWS_SELECTOR` / `EGX_UI_NEXT_SELECTOR`, or send me its output |
| "the page did not return page N" | a challenge in the window, a changed layout or a slow connection; look at the window; if a CAPTCHA appears, stop |
| CAPTCHA / challenge in the window | stop |
| Everything `stale` at night | expected only if older than the last session close; check `/api/v1/status` |
| `partial` datasets | read `most_recent_error`; lower `EGX_PAGE_SIZE` |
| `ZoneInfoNotFoundError` | `pip install tzdata` |

## Tests

`pytest -v`: 134 tests (parsing, nulls vs zeros, Arabic/English, pagination, duplicates, retries, 429/Retry-After, 5xx, malformed/empty/partial, DB upsert and migration, daily bars, freshness, every endpoint, overlap prevention, recovery, passive collector with a fake page, recorded real response). 3 integration tests are skipped unless `EGX_RUN_INTEGRATION=1` and are the only ones that touch the real source.
#   T e c h n i c a l - A g e n t - L i v e - d a t a - A P I  
 