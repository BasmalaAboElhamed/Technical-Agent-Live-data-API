"""
EGX live client for YOUR local API (http://127.0.0.1:8000/api/v1/ohlcv/daily).
Pulls the daily bars after the market close and runs them through the SAME preprocessing
as the historical data (preprocess.py).

.env (all optional - defaults shown)
----
EGX_API_BASE_URL=http://127.0.0.1:8000
EGX_API_DAILY_PATH=/api/v1/ohlcv/daily
EGX_API_PAGE_SIZE=100          # rows per request
EGX_API_LIMIT_PARAM=limit      # query-parameter names used by your API for paging
EGX_API_OFFSET_PARAM=offset

Use from your agent
-------------------
    from live_client import get_live_features
    feats, status = get_live_features("processed", finalize=True)

Service (every trading day after the close):   python live_client.py daily --out processed
One-shot:                                       python live_client.py once --out processed [--finalize]
Test during the session (dry run):             python live_client.py once --out processed --preview
"""
import argparse
import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

import joblib
import pandas as pd
import requests

import preprocess as pp

CAIRO = ZoneInfo("Africa/Cairo")        # handles Egypt's daylight-saving changes
CLOSE_TIME = dt.time(14, 45)            # EGX closes 14:30 Cairo; wait 15 min for final bars
GIVE_UP_TIME = dt.time(17, 30)          # stop retrying for today's final bars after this
TRADING_DAYS = {6, 0, 1, 2, 3}          # Sun..Thu (python weekday: Mon=0 ... Sun=6)


def load_env(path=".env"):
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except ImportError:
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


def _rows_of(payload):
    if isinstance(payload, dict):
        payload = next((payload[k] for k in ("data", "items", "results", "quotes")
                        if isinstance(payload.get(k), list)), [payload])
    return payload


def _get_json(s, url, params=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = s.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"could not read {url}: {last}")


def fetch_daily(session=None):
    """GET /api/v1/ohlcv/daily with pagination (limit/offset) -> DataFrame, one row per symbol.
    The endpoint returns only the first page by default, so we keep asking until no new
    symbols come back, then compare with /api/v1/status to make sure nothing is missing."""
    load_env()
    base = os.getenv("EGX_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    url = base + os.getenv("EGX_API_DAILY_PATH", "/api/v1/ohlcv/daily")
    limit_p = os.getenv("EGX_API_LIMIT_PARAM", "limit")
    offset_p = os.getenv("EGX_API_OFFSET_PARAM", "offset")
    page = int(os.getenv("EGX_API_PAGE_SIZE", "100"))
    s = session or requests.Session()
    rows, seen, offset = [], set(), 0
    while True:
        batch = _rows_of(_get_json(s, url, {limit_p: page, offset_p: offset}))
        new = [r for r in batch if r.get("symbol") not in seen]
        if not new:
            break                                  # params ignored or end of data
        rows += new
        seen.update(r["symbol"] for r in new)
        if len(batch) < page:
            break
        offset += page
    df = pd.DataFrame(rows)
    # completeness check against the API's own status endpoint
    try:
        st = _get_json(s, base + os.getenv("EGX_API_STATUS_PATH", "/api/v1/status"), tries=1)
        expected = st.get("stocks_in_latest_collection")
        if expected and len(df) < 0.95 * expected:
            print(f"[live] WARNING: got {len(df)} symbols but the API reports {expected} in the latest "
                  f"collection. Pagination probably is not working - check "
                  f"{base}/docs for the parameter names (EGX_API_LIMIT_PARAM / EGX_API_OFFSET_PARAM).")
    except Exception:
        pass
    print(f"[live] fetched {len(df)} symbols")
    return df


def get_live_features(out_dir="processed", finalize=False, session=None, save_raw=True, preview=False):
    """Fetch -> keep FINAL bars -> clean -> features -> scale.
    finalize=True also stores today's final bars in the history store (use after the close).
    preview=True is a DRY RUN for testing during the session: uses non-final bars, ignores the
    history-gap guard, never writes to the history store. Do NOT feed preview output to the model."""
    art = joblib.load(os.path.join(out_dir, "scaler.joblib"))
    hist_path = os.path.join(out_dir, "clean_history.csv.gz")
    hist = pd.read_csv(hist_path, parse_dates=["date"])
    raw = fetch_daily(session)
    if save_raw:
        os.makedirs(os.path.join(out_dir, "live_raw"), exist_ok=True)
        raw.to_csv(os.path.join(out_dir, "live_raw",
                                dt.datetime.now(CAIRO).strftime("%Y-%m-%d_%H%M") + ".csv"), index=False)
    bars, warns = pp.daily_to_bars(raw, final_only=not preview)
    for w in warns:
        print("[live]", w)
    if preview:
        print("[live] PREVIEW MODE - non-final bars, gap guard off, history not touched.")
        pp.MAX_LIVE_GAP_DAYS = 10 ** 6
        finalize = False
        today = bars["date"].max() if len(bars) else None
    else:
        today = pd.Timestamp(dt.datetime.now(CAIRO).date())
    if bars.empty or bars["date"].max() != today:
        print("[live] no final bars for today yet (holiday, or the collector has not finalized).")
        return pd.DataFrame(), pd.DataFrame(columns=["symbol", "status", "gap_days"])
    feats, status = pp.build_live_features(bars[bars.date == today], hist, art)
    if finalize:
        pd.concat([hist, bars]).drop_duplicates(["symbol", "date"], keep="last") \
            .sort_values(["symbol", "date"]).to_csv(hist_path, index=False)
    return feats, status


def next_run(now=None):
    now = now or dt.datetime.now(CAIRO)
    t = now.replace(hour=CLOSE_TIME.hour, minute=CLOSE_TIME.minute, second=0, microsecond=0)
    if now >= t or now.weekday() not in TRADING_DAYS:
        t += dt.timedelta(days=1)
        while t.weekday() not in TRADING_DAYS:
            t += dt.timedelta(days=1)
    return t


def serve_daily(out_dir, on_features=None):
    """Every trading day after the close: wait for final bars, preprocess, store, hand over."""
    while True:
        t = next_run()
        print(f"[live] next run at {t:%Y-%m-%d %H:%M} Cairo")
        time.sleep(max(0, (t - dt.datetime.now(CAIRO)).total_seconds()))
        while dt.datetime.now(CAIRO).time() < GIVE_UP_TIME:
            try:
                feats, status = get_live_features(out_dir, finalize=True)
            except Exception as e:
                print("[live] run failed:", e)
                time.sleep(300)
                continue
            if status.empty:                  # final bars not ready yet -> retry in 5 min
                time.sleep(300)
                continue
            print(f"[live] {len(feats)} feature rows | {status.status.value_counts().to_dict()}")
            if on_features and len(feats):
                on_features(feats)            # e.g. your Technical Agent's predict function
            break


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["once", "daily"])
    ap.add_argument("--out", default="processed")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--preview", action="store_true", help="dry run on non-final bars (testing only)")
    a = ap.parse_args()
    if a.mode == "once":
        f, s = get_live_features(a.out, finalize=a.finalize, preview=a.preview)
        f.to_csv(os.path.join(a.out, "live_features_preview.csv" if a.preview else "live_features.csv"),
                 index=False)
        print(s.status.value_counts().to_string() if len(s) else "no data", f"\nrows: {len(f)}")
    else:
        serve_daily(a.out)
