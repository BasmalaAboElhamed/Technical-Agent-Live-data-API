#!/usr/bin/env python3
"""
Egyptian Stock Market Historical Data Collection Pipeline
Source: Investing.com (via Browser-Authenticated Historical Financial Data API)

Collects maximum available daily OHLCV data for all 100 EGX target tickers.
Output schema: date, open, high, low, close, adjusted_close, change, volume

IMPORTANT DATA DISCLOSURE:
- 'adjusted_close' is NOT provided by Investing.com's historical equity data tables.
- Investing.com provides: Date, Price(close), Open, High, Low, Volume, Change%.
- 'adjusted_close' is left empty in all output files per project specifications.
- This is truthfully documented in the collection manifest.
"""

import asyncio
import csv
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from tqdm import tqdm

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_DIR = Path(r"C:\Users\bisoi\Downloads\Tech agent Investing data")
OUTPUT_DIR = BASE_DIR / "csv_output"
LOG_DIR = BASE_DIR / "logs"
CHECKPOINT_FILE = BASE_DIR / "checkpoint.json"
MANIFEST_FILE = BASE_DIR / "collection_manifest.csv"
CONSOLIDATED_FILE = BASE_DIR / "all_egypt_stocks_consolidated.csv"

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

START_DATE = "2005-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")

# Two chunks to prevent hitting the 5,000-row query cap
DATE_CHUNKS = [
    ("2005-01-01", "2015-12-31"),
    ("2016-01-01", END_DATE),
]

OUTPUT_COLUMNS = ["date", "open", "high", "low", "close", "adjusted_close", "change", "volume"]

BETWEEN_STOCKS_DELAY = 1.0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 3.0

# ── Logging ────────────────────────────────────────────────────────────────────

log_file = LOG_DIR / f"collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# ── 100 Target EGX Tickers ─────────────────────────────────────────────────────

TARGET_TICKERS = [
    "abuk", "acgc", "adib", "afmc", "ajwa", "alcn", "amer", "amia", "amoc", "arab",
    "arcc", "ascm", "atqa", "axph", "binv", "bioc", "btfh", "ccap", "ccrs", "cera",
    "cich", "cieb", "clho", "comi", "cosg", "csag", "dscw", "ealr", "east", "ecap",
    "efid", "egal", "egbe", "egch", "egts", "ehdr", "elka", "elsh", "emfd", "epco",
    "esrs", "etel", "expa", "ferc", "fwry", "gbco", "gdwa", "goco", "hdbk", "heli",
    "hrho", "isma", "isph", "jufo", "kabo", "masr", "mcqe", "mepa", "mfpc", "mhot",
    "miph", "moil", "mpco", "mprc", "mtie", "naho", "napr", "nccw", "niph", "obri",
    "ocdi", "oih", "oras", "orhd", "orwe", "phdc", "prdc", "prmh", "raya", "rmda",
    "roto", "rrei", "rtvc", "saib", "scem", "skpc", "spin", "spmd", "swdy", "talm",
    "taqa", "tmgh", "trto", "uasg", "uegc", "unip", "unit", "wcdf", "zeot", "zmid",
]

# Additional mappings for tickers not directly in investpy's stocks.csv
ADDITIONAL_MAPPINGS = {
    "arab": {
        "ticker": "ARAB",
        "name": "Arab Developers Holding",
        "full_name": "Arab Developers Holding SAE",
        "pair_id": "960753",
        "tag": "porto-group-holding-sae",
        "isin": "EGS655L1C012",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/porto-group-holding-sae-historical-data",
    },
    "ferc": {
        "ticker": "FERC",
        "name": "Ferchem Egypt Fertilizers and Chemicals",
        "full_name": "Ferchem Egypt Fertilizers and Chemicals SAE",
        "pair_id": "1162332",
        "tag": "ferchem-egypt-fertilizers-and-chemi",
        "isin": "EGS38261C017",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/ferchem-egypt-fertilizers-and-chemi-historical-data",
    },
    "gbco": {
        "ticker": "GBCO",
        "name": "GB AUTO",
        "full_name": "GB Auto SAE",
        "pair_id": "12899",
        "tag": "gb-auto-s.a.e",
        "isin": "EGS673T1C012",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/gb-auto-s.a.e-historical-data",
    },
    "gdwa": {
        "ticker": "GDWA",
        "name": "Gadwa for Industrial Development",
        "full_name": "Gadwa for Industrial Development SAE",
        "pair_id": "1178527",
        "tag": "gadwa-for-industrial-development",
        "isin": "EGS69561C013",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/gadwa-for-industrial-development-historical-data",
    },
    "masr": {
        "ticker": "MASR",
        "name": "Madinet Masr",
        "full_name": "Madinet Masr for Housing and Development SAE",
        "pair_id": "12932",
        "tag": "medinet-nasr-h",
        "isin": "EGS65591C017",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/medinet-nasr-h-historical-data",
    },
    "napr": {
        "ticker": "NAPR",
        "name": "National Printing",
        "full_name": "National Printing Co SAE",
        "pair_id": "1233747",
        "tag": "national-printing",
        "isin": "EGS380B1C018",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/national-printing-historical-data",
    },
    "prdc": {
        "ticker": "PRDC",
        "name": "PREDCO",
        "full_name": "Pioneers Properties for Urban Development PRE Group",
        "pair_id": "1178528",
        "tag": "pioneer-properties-urban-dev",
        "isin": "EGS65851C015",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/pioneer-properties-urban-dev-historical-data",
    },
    "rmda": {
        "ticker": "RMDA",
        "name": "Rameda",
        "full_name": "Tenth of Ramadan for Pharmaceutical Industries and Diagnostic Reagents",
        "pair_id": "1156268",
        "tag": "tenth-of-ramadan-for-pharmaceutical",
        "isin": "EGS381B1C015",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/tenth-of-ramadan-for-pharmaceutical-historical-data",
    },
    "talm": {
        "ticker": "TALM",
        "name": "Taaleem Management Services",
        "full_name": "Taaleem Management Services SAE",
        "pair_id": "1172876",
        "tag": "taaleem-management-services",
        "isin": "EGS729J1C013",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/taaleem-management-services-historical-data",
    },
    "taqa": {
        "ticker": "TAQA",
        "name": "TAQA Arabia",
        "full_name": "TAQA Arabia SAE",
        "pair_id": "1204783",
        "tag": "taqa-arabia",
        "isin": "EGS02291C012",
        "currency": "EGP",
        "investing_url": "https://www.investing.com/equities/taqa-arabia-historical-data",
    },
}


# ── Company Registry ───────────────────────────────────────────────────────────

def build_egypt_registry() -> dict:
    """Build unified mapping registry for all 100 target tickers."""
    registry = {}
    try:
        import investpy
        pkg_path = Path(investpy.__file__).parent
        csv_path = pkg_path / "resources" / "stocks.csv"
        df = pd.read_csv(csv_path)
        egypt = df[df["country"] == "egypt"].copy()

        for _, row in egypt.iterrows():
            sym = str(row["symbol"]).strip().lower()
            tag = str(row["tag"]).strip()
            registry[sym] = {
                "ticker": str(row["symbol"]).strip().upper(),
                "name": str(row["name"]).strip(),
                "full_name": str(row["full_name"]).strip(),
                "pair_id": str(row["id"]).strip(),
                "tag": tag,
                "isin": str(row["isin"]).strip(),
                "currency": str(row["currency"]).strip(),
                "investing_url": f"https://www.investing.com/equities/{tag}-historical-data",
            }
    except Exception as e:
        logger.warning(f"investpy registry load error: {e}")

    # Merge additional mappings
    for sym, item in ADDITIONAL_MAPPINGS.items():
        registry[sym] = item

    logger.info(f"Registry ready: {len(registry)} stocks available")
    return registry


# ── Checkpointing ──────────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, encoding="utf-8") as f:
                data = json.load(f)
            n_done = len(data.get("completed", []))
            n_fail = len(data.get("failed", []))
            logger.info(f"Checkpoint loaded: {n_done} completed, {n_fail} failed")
            return data
        except Exception as e:
            logger.warning(f"Checkpoint load error: {e}")
    return {"completed": [], "failed": [], "results": {}}


def save_checkpoint(cp: dict):
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(cp, f, indent=2)
    except Exception as e:
        logger.warning(f"Checkpoint save error: {e}")


# ── Data Fetching via Investing.com Internal Financial API ────────────────────

async def fetch_stock_history(page, pair_id: str) -> tuple[list, str]:
    """
    Fetch historical daily data for a given pair_id across multiple date chunks
    directly using Investing.com's financial data endpoint in authenticated browser context.
    """
    all_raw_rows = []

    for start_dt, end_dt in DATE_CHUNKS:
        api_url = (
            f"https://api.investing.com/api/financialdata/historical/{pair_id}"
            f"?start-date={start_dt}&end-date={end_dt}&time-frame=Daily&add-missing-rows=false"
        )
        try:
            data = await page.evaluate("""
                async (url) => {
                    try {
                        const resp = await fetch(url, {
                            headers: {
                                'Accept': 'application/json',
                                'domain-id': 'www'
                            }
                        });
                        if (resp.status !== 200) return {status: resp.status, data: []};
                        const json = await resp.json();
                        return {status: 200, data: json.data || []};
                    } catch(e) {
                        return {error: e.toString(), data: []};
                    }
                }
            """, api_url)

            if data.get("error"):
                logger.warning(f"    Chunk ({start_dt}->{end_dt}) fetch error: {data['error']}")
            elif data.get("status") == 200:
                chunk_rows = data.get("data", [])
                all_raw_rows.extend(chunk_rows)
            else:
                logger.warning(f"    Chunk ({start_dt}->{end_dt}) returned HTTP {data.get('status')}")

        except Exception as e:
            logger.warning(f"    Chunk evaluate error: {e}")

    if not all_raw_rows:
        return [], "NO_DATA_RETURNED"

    # Deduplicate and sort chronologically
    dedup = {}
    for r in all_raw_rows:
        # Date string from timestamp or rowDateTimestamp
        ts = r.get("rowDateTimestamp", "")
        if ts and len(ts) >= 10:
            date_str = ts[:10]
        else:
            date_raw = r.get("rowDate", "")
            try:
                date_str = datetime.strptime(date_raw, "%b %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                continue

        if date_str in dedup:
            continue

        # Format change
        chg = r.get("change_precent")
        if chg is not None and chg != "":
            chg_str = str(chg).strip()
            if not chg_str.startswith(("+", "-")):
                try:
                    val = float(chg_str)
                    chg_str = f"+{val:.2f}%" if val >= 0 else f"{val:.2f}%"
                except ValueError:
                    chg_str = f"{chg_str}%"
            elif not chg_str.endswith("%"):
                chg_str = f"{chg_str}%"
        else:
            chg_str = ""

        # Format volume
        vol_raw = r.get("volumeRaw")
        if vol_raw is not None and vol_raw != "":
            try:
                vol_val = int(vol_raw)
            except ValueError:
                vol_val = vol_raw
        else:
            vol_val = 0

        dedup[date_str] = {
            "date": date_str,
            "open": r.get("last_open", ""),
            "high": r.get("last_max", ""),
            "low": r.get("last_min", ""),
            "close": r.get("last_close", ""),
            "adjusted_close": "",   # NOT provided by Investing.com
            "change": chg_str,
            "volume": vol_val,
        }

    sorted_rows = sorted(dedup.values(), key=lambda x: x["date"])
    return sorted_rows, "success"


# ── CSV Output ─────────────────────────────────────────────────────────────────

def save_csv(ticker: str, rows: list) -> Path:
    out = OUTPUT_DIR / f"{ticker.lower()}_history.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return out


# ── Manifest ───────────────────────────────────────────────────────────────────

MANIFEST_COLS = [
    "ticker", "company_name", "investing_url", "instrument_id", "earliest_date",
    "latest_date", "total_rows", "status", "notes"
]


def write_manifest(results: dict):
    with open(MANIFEST_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS, extrasaction="ignore")
        w.writeheader()
        for _, info in sorted(results.items()):
            w.writerow({
                "ticker": info.get("ticker", ""),
                "company_name": info.get("full_name") or info.get("name", ""),
                "investing_url": info.get("investing_url", ""),
                "instrument_id": info.get("pair_id", ""),
                "earliest_date": info.get("date_start", ""),
                "latest_date": info.get("date_end", ""),
                "total_rows": info.get("rows_collected", 0),
                "status": info.get("status", ""),
                "notes": info.get("notes", ""),
            })
    logger.info(f"Manifest written -> {MANIFEST_FILE}")


# ── Consolidated Dataset ───────────────────────────────────────────────────────

def consolidate(results: dict) -> tuple:
    dfs = []
    for tk, info in results.items():
        if info.get("status") == "success" and info.get("output_file"):
            fpath = Path(info["output_file"])
            if fpath.exists():
                try:
                    df = pd.read_csv(fpath)
                    df.insert(0, "ticker", info["ticker"])
                    dfs.append(df)
                except Exception as e:
                    logger.warning(f"Consolidation skip {tk}: {e}")
    if dfs:
        combined = pd.concat(dfs, ignore_index=True)
        combined.to_csv(CONSOLIDATED_FILE, index=False)
        logger.info(f"Consolidated: {len(combined):,} rows across {len(dfs)} tickers -> {CONSOLIDATED_FILE}")
        return len(combined), len(dfs)
    return 0, 0


# ── Main Collection Runner ────────────────────────────────────────────────────

async def main():
    from playwright.async_api import async_playwright

    registry = build_egypt_registry()
    cp = load_checkpoint()
    completed = set(cp.get("completed", []))
    results = cp.get("results", {})

    to_do = [t for t in TARGET_TICKERS if t not in completed]
    logger.info(f"Targets: {len(TARGET_TICKERS)} | Completed: {len(completed)} | Remaining: {len(to_do)}")

    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--lang=en-US,en",
    ]

    async with async_playwright() as pw:
        if os.path.exists(chrome):
            browser = await pw.chromium.launch(executable_path=chrome, headless=True, args=args)
            logger.info("Launched system Chrome 155")
        else:
            browser = await pw.chromium.launch(headless=True, args=args)
            logger.info("Launched Playwright Chromium")

        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.39 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Africa/Cairo",
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        # Warm up session with Investing.com
        logger.info("Connecting to Investing.com to establish authenticated session...")
        try:
            await page.goto("https://www.investing.com/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            logger.info(f"Session established: {await page.title()!r}")
        except Exception as e:
            logger.warning(f"Session warm-up error: {e}")

        for idx, ticker in enumerate(tqdm(to_do, desc="Collecting EGX Equities", unit="stock")):
            tk = ticker.lower()
            info = registry.get(tk)

            if not info:
                logger.error(f"[{idx+1}/{len(to_do)}] {ticker.upper()}: not mapped in registry")
                results[tk] = {
                    "ticker": ticker.upper(),
                    "name": "UNKNOWN",
                    "full_name": "UNKNOWN",
                    "investing_url": "N/A",
                    "pair_id": "N/A",
                    "status": "NOT_MAPPED",
                    "rows_collected": 0,
                    "date_start": "",
                    "date_end": "",
                    "output_file": "",
                    "notes": "Ticker unmapped",
                }
                cp.setdefault("failed", []).append(tk)
                save_checkpoint(cp)
                continue

            pair_id = info["pair_id"]
            logger.info(f"[{idx+1}/{len(to_do)}] {ticker.upper()} (pair_id={pair_id}) — {info['name']}")

            rows = []
            status_msg = ""
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    rows, status_msg = await fetch_stock_history(page, pair_id)
                    if rows:
                        break
                    logger.warning(f"  Attempt {attempt} returned 0 rows: {status_msg}")
                except Exception as e:
                    status_msg = str(e)
                    logger.warning(f"  Attempt {attempt} failed: {e}")

                if attempt < RETRY_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)

            out_file = ""
            d_start = d_end = ""
            is_success = bool(rows)

            if is_success:
                out_path = save_csv(tk, rows)
                out_file = str(out_path)
                d_start = rows[0]["date"]
                d_end = rows[-1]["date"]
                logger.info(f"  -> SUCCESS: {len(rows):,} rows | {d_start} to {d_end}")
                if tk not in cp.get("completed", []):
                    cp.setdefault("completed", []).append(tk)
                if tk in cp.get("failed", []):
                    cp["failed"].remove(tk)
            else:
                logger.error(f"  -> FAILED: {status_msg}")
                if tk not in cp.get("failed", []):
                    cp.setdefault("failed", []).append(tk)

            results[tk] = {
                "ticker": info["ticker"],
                "name": info.get("name", ""),
                "full_name": info.get("full_name") or info.get("name", ""),
                "investing_url": info["investing_url"],
                "pair_id": pair_id,
                "status": "success" if is_success else "failed",
                "rows_collected": len(rows),
                "date_start": d_start,
                "date_end": d_end,
                "output_file": out_file,
                "notes": (
                    "Full history retrieved from Investing.com financial data API. "
                    "'adjusted_close' left blank as Investing.com does not publish adjusted prices."
                    if is_success else f"Collection failed: {status_msg}"
                ),
            }

            cp["results"] = results
            save_checkpoint(cp)

            if idx < len(to_do) - 1:
                await asyncio.sleep(BETWEEN_STOCKS_DELAY)

        await browser.close()

    write_manifest(results)
    total_rows, n_ok = consolidate(results)

    ok_list = [t for t, r in results.items() if r.get("status") == "success"]
    fail_list = [t for t, r in results.items() if r.get("status") != "success"]

    print("\n" + "=" * 70)
    print("COLLECTION COMPLETED")
    print("=" * 70)
    print(f"  Total Targets:     {len(TARGET_TICKERS)}")
    print(f"  Successful:        {len(ok_list)}")
    print(f"  Failed:            {len(fail_list)}")
    print(f"  Total Data Rows:   {total_rows:,}")
    print(f"  Output CSV Folder: {OUTPUT_DIR}")
    print(f"  Manifest File:     {MANIFEST_FILE}")
    print(f"  Consolidated File: {CONSOLIDATED_FILE}")
    print(f"  Log File:          {log_file}")
    if fail_list:
        print(f"\nFailed Tickers ({len(fail_list)}):")
        for t in sorted(fail_list):
            print(f"  {t.upper()}: {results.get(t, {}).get('notes', '')}")
    print("\nDISCLOSURE:")
    print("  'adjusted_close' is empty across all files because Investing.com")
    print("  does not publish dividend/split-adjusted close prices.")
    print("=" * 70)


if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("Egyptian Stock Market Historical Data Collection — Investing.com")
    logger.info(f"Target Coverage: {START_DATE} to {END_DATE} (all available trading days)")
    logger.info(f"Targets: {len(TARGET_TICKERS)} EGX Equities")
    logger.info("=" * 70)
    asyncio.run(main())