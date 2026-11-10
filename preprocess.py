"""
EGX Technical Agent - shared preprocessing for HISTORICAL and LIVE data.

Target : next-day direction (1 = next close > today's close, 0 = lower; flat days dropped).
Rule   : historical and live go through the SAME functions (clean_bars -> compute_features
         -> transform) so the model sees identical features in training and in production.

Usage
-----
  python preprocess.py historical --input csv_output --out processed
  python preprocess.py live --live egx.sqlite3|daily.json --out processed [--update-history]
  python preprocess.py parity --out processed
"""
import argparse
import glob
import json
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

# ----------------------------------------------------------------------------- config
USE_VOLUME = True       # the new EGX feed gives real daily volume
USE_OPEN = False        # the feed has NO real open (open is null) -> open-based features are off
MAX_LIVE_GAP_DAYS = 7   # live bar vs last stored bar; larger gap = missing days -> no features
MAX_GAP_DAYS = 45       # calendar-day gap that breaks a series (e.g. the 2011 market closure)
MAX_JUMP = 0.35         # |close-to-close return| above this = corporate action -> break series
WARMUP = 100            # rows needed inside a segment before features are valid (SMA100)
MIN_ROWS = 250         # symbols with fewer usable rows are excluded from training
TRAIN_END = "2023-12-31"
VAL_END = "2025-06-30"  # test = everything after
CLIP_Q = (0.005, 0.995)  # winsorize bounds, fitted on train only
LIVE_CLOSE_COL = "close"  # which live column is used as the close ("close" or "implied")
TZ = "Africa/Cairo"

OHLC = ["open", "high", "low", "close"]


# ----------------------------------------------------------------------------- loading
def _num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(),
                         errors="coerce")


def load_historical(folder):
    frames = []
    for f in sorted(glob.glob(os.path.join(folder, "*_history.csv"))):
        sym = os.path.basename(f).split("_")[0].upper()
        d = pd.read_csv(f, dtype=str)
        d["symbol"] = sym
        d["date"] = pd.to_datetime(d["date"], format="%m/%d/%Y", errors="coerce")
        for c in OHLC + ["volume"]:
            d[c] = _num(d[c])
        frames.append(d[["symbol", "date"] + OHLC + ["volume"]])  # 'change' is recomputed, not trusted
    return pd.concat(frames, ignore_index=True)


# ----------------------------------------------------------------------------- cleaning
def clean_bars(df, drop_zero_volume=True):
    """Validate bars and tag continuous segments. Returns (clean_df, stats)."""
    n0 = len(df)
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d = d.dropna(subset=["symbol", "date", "high", "low", "close"])   # open is optional
    d = d[(d[["high", "low", "close"]] > 0).all(axis=1) & ((d["open"] > 0) | d["open"].isna())
          & (d["high"] >= d["low"])]
    n_invalid = n0 - len(d)
    if drop_zero_volume:
        d = d[~(d["volume"] == 0)]  # NaN volume (live) is kept
    n_novol = n0 - n_invalid - len(d)
    d = d.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    # repair OHLC envelope instead of dropping the row
    hi = d[OHLC].max(axis=1)
    lo = d[OHLC].min(axis=1)
    n_repaired = int(((hi > d["high"]) | (lo < d["low"])).sum())
    d["high"], d["low"] = hi, lo
    # continuity segments
    g = d.groupby("symbol")
    gap = g["date"].diff().dt.days
    ret = g["close"].pct_change()
    brk = (gap > MAX_GAP_DAYS) | (ret.abs() > MAX_JUMP)
    d["seg"] = brk.groupby(d["symbol"]).cumsum().astype(int)
    stats = dict(rows_in=n0, dropped_invalid=int(n_invalid), dropped_zero_volume=int(n_novol),
                 repaired_envelope=n_repaired, breaks=int(brk.sum()), rows_out=len(d))
    return d.reset_index(drop=True), stats


# ----------------------------------------------------------------------------- features
def _rsi(c, n=14):
    d = c.diff()
    au = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rsi = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    return rsi.where(ad != 0, 100.0)


def _features_one(g):
    o, h, l, c, v = g["open"], g["high"], g["low"], g["close"], g["volume"]
    f = pd.DataFrame(index=g.index)
    lc = np.log(c)
    for k in (1, 2, 3, 5, 10, 20):
        f[f"ret_{k}"] = lc.diff(k)
    if USE_OPEN:
        f["gap_open"] = np.log(o / c.shift())
        f["intraday"] = np.log(c / o)
    f["range_pct"] = (h - l) / c
    f["close_pos"] = ((c - l) / (h - l).replace(0, np.nan)).fillna(0.5)
    for w in (5, 10, 20, 50, 100):
        f[f"sma_ratio_{w}"] = c / c.rolling(w).mean() - 1
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    f["macd"], f["macd_signal"], f["macd_hist"] = macd / c, sig / c, (macd - sig) / c
    f["rsi_14"] = _rsi(c)
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    k = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    f["stoch_k"] = k.fillna(50.0)
    f["stoch_d"] = f["stoch_k"].rolling(3).mean()
    m, s = c.rolling(20).mean(), c.rolling(20).std()
    f["bb_pctb"] = ((c - (m - 2 * s)) / (4 * s).replace(0, np.nan)).fillna(0.5)
    f["bb_width"] = 4 * s / m
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.ewm(alpha=1 / 14, adjust=False).mean() / c
    r1 = f["ret_1"]
    f["vol_10"], f["vol_20"] = r1.rolling(10).std(), r1.rolling(20).std()
    dow = g["date"].dt.dayofweek
    f["dow_sin"], f["dow_cos"] = np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7)
    if USE_VOLUME:
        f["vol_ratio_20"] = np.log((v + 1) / (v.rolling(20).mean() + 1))
        f["dlog_vol"] = np.log(v + 1).diff()
    nxt = c.shift(-1)
    f["next_ret"] = nxt / c - 1
    f["target_up"] = (nxt > c).astype(float).where(nxt.notna() & (nxt != c))
    f["pos_in_seg"] = np.arange(len(g))
    return f


def feature_names():
    names = [f"ret_{k}" for k in (1, 2, 3, 5, 10, 20)] + ["range_pct", "close_pos"]
    if USE_OPEN:
        names += ["gap_open", "intraday"]
    names += [f"sma_ratio_{w}" for w in (5, 10, 20, 50, 100)]
    names += ["macd", "macd_signal", "macd_hist", "rsi_14", "stoch_k", "stoch_d",
              "bb_pctb", "bb_width", "atr_pct", "vol_10", "vol_20", "dow_sin", "dow_cos"]
    if USE_VOLUME:
        names += ["vol_ratio_20", "dlog_vol"]
    return names


def compute_features(clean):
    """Features per (symbol, segment). Keeps rows past warm-up with all features finite.
    Target may be NaN (e.g. today's live bar)."""
    parts = []
    for (_, _), g in clean.groupby(["symbol", "seg"], sort=False):
        f = _features_one(g)
        f = pd.concat([g[["symbol", "date", "seg", "close"]], f], axis=1)
        parts.append(f)
    out = pd.concat(parts, ignore_index=True)
    cols = feature_names()
    out = out[(out["pos_in_seg"] >= WARMUP) & np.isfinite(out[cols]).all(axis=1)]
    return out.reset_index(drop=True)


# ----------------------------------------------------------------------------- scaling
def fit_artifact(train):
    cols = feature_names()
    lo = train[cols].quantile(CLIP_Q[0])
    hi = train[cols].quantile(CLIP_Q[1])
    sc = StandardScaler().fit(train[cols].clip(lo, hi, axis=1))
    return dict(scaler=sc, lo=lo, hi=hi, features=cols,
                config=dict(USE_VOLUME=USE_VOLUME, WARMUP=WARMUP, MAX_GAP_DAYS=MAX_GAP_DAYS,
                            MAX_JUMP=MAX_JUMP, TRAIN_END=TRAIN_END, VAL_END=VAL_END))


def transform(df, art):
    cols = art["features"]
    x = df[cols].clip(art["lo"], art["hi"], axis=1)
    out = df.drop(columns=cols).copy()
    out[cols] = art["scaler"].transform(x)
    return out


# ----------------------------------------------------------------------------- historical
def run_historical(inp, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    raw = load_historical(inp)
    clean, st = clean_bars(raw, drop_zero_volume=True)
    feats = compute_features(clean)
    cnt = feats.groupby("symbol").size()
    keep = cnt[cnt >= MIN_ROWS].index
    dropped_syms = sorted(set(raw.symbol) - set(keep))
    feats = feats[feats.symbol.isin(keep)]
    data = feats.dropna(subset=["target_up"]).copy()
    d = data["date"]
    tr, va, te = data[d <= TRAIN_END], data[(d > TRAIN_END) & (d <= VAL_END)], data[d > VAL_END]
    art = fit_artifact(tr)
    cols = ["symbol", "date", "target_up", "next_ret"] + art["features"]
    for name, part in (("train", tr), ("val", va), ("test", te)):
        transform(part, art)[cols].to_csv(os.path.join(out_dir, f"{name}.csv.gz"), index=False)
    clean[clean.symbol.isin(keep)].drop(columns="seg").to_csv(
        os.path.join(out_dir, "clean_history.csv.gz"), index=False)
    joblib.dump(art, os.path.join(out_dir, "scaler.joblib"))
    report = dict(cleaning=st, symbols_used=len(keep), symbols_excluded=dropped_syms,
                  rows=dict(train=len(tr), val=len(va), test=len(te)),
                  up_rate=dict(train=round(tr.target_up.mean(), 4), val=round(va.target_up.mean(), 4),
                               test=round(te.target_up.mean(), 4)),
                  date_range=[str(clean.date.min().date()), str(clean.date.max().date())],
                  n_features=len(art["features"]), features=art["features"])
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    return report


# ----------------------------------------------------------------------------- live
def _truthy(x):
    return x.astype(str).str.lower().isin(["1", "true", "1.0"])


def load_daily_bars(source):
    """Daily OHLCV rows from a DataFrame, a .json/.csv export of /api/v1/ohlcv/daily,
    or the collector's egx.sqlite3 (table daily_bars)."""
    if isinstance(source, pd.DataFrame):
        return source.copy()
    p = str(source).lower()
    if p.endswith((".sqlite3", ".sqlite", ".db")):
        import sqlite3
        with sqlite3.connect(source) as con:
            return pd.read_sql("SELECT s.symbol, d.* FROM daily_bars d "
                               "JOIN stocks s USING(stock_key)", con)
    if p.endswith(".json"):
        with open(source, encoding="utf-8") as fh:
            obj = json.load(fh)
        if isinstance(obj, dict):
            obj = next((obj[k] for k in ("data", "items", "results", "quotes")
                        if isinstance(obj.get(k), list)), [obj])
        return pd.DataFrame(obj)
    return pd.read_csv(source)


def daily_to_bars(source, final_only=True):
    """/api/v1/ohlcv/daily rows -> standard bars (symbol, date, OHLC, volume) + warnings."""
    d = load_daily_bars(source)
    warns = []
    d["symbol"] = d["symbol"].astype(str).str.upper().str.strip()
    d["date"] = pd.to_datetime(d["trade_date"]).dt.normalize()
    for c in ("high", "low", "close", "last_price", "volume"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    if final_only:
        fin = _truthy(d["is_final"])
        if (~fin).any():
            warns.append(f"{int((~fin).sum())} rows are not final yet (session still open) - skipped.")
        d = d[fin]
    # close_price is sometimes a stale previous close that lies outside [low, high] -> use last_price
    bad = d["close"].isna() | (d["close"] < d["low"]) | (d["close"] > d["high"])
    if bad.any():
        warns.append(f"{int(bad.sum())} rows had close outside [low, high]; used last_price instead.")
    d["close"] = d["close"].where(~bad, d["last_price"])
    d["open"] = np.nan  # no real open in the feed (open is null; open_observed is only the first poll)
    return d[["symbol", "date"] + OHLC + ["volume"]].reset_index(drop=True), warns


def build_live_features(live_bars, history, art):
    """history = clean_history store; returns (scaled feature rows for the live date, status table)."""
    status, rows = [], []
    for s in live_bars["symbol"].unique():
        h = history[history.symbol == s]
        if h.empty:
            status.append((s, "no_history", None))
            continue
        lb = live_bars[live_bars.symbol == s]
        ld = lb["date"].iloc[-1]
        gap = int((ld - pd.to_datetime(h["date"]).max()).days)
        if gap > MAX_LIVE_GAP_DAYS:
            status.append((s, "gap_too_large", gap))   # days are missing -> indicators would be wrong
            continue
        comb = pd.concat([h, lb], ignore_index=True)
        comb["date"] = pd.to_datetime(comb["date"])
        comb = comb.drop_duplicates(["symbol", "date"], keep="last")
        clean, _ = clean_bars(comb, drop_zero_volume=True)
        if clean.empty or clean["date"].max() != ld:
            status.append((s, "bar_rejected", gap))
            continue
        f = compute_features(clean)
        f = f[f["date"] == ld]
        if f.empty:
            status.append((s, "insufficient_history", gap))
            continue
        status.append((s, "ok", gap))
        rows.append(f)
    status = pd.DataFrame(status, columns=["symbol", "status", "gap_days"])
    if not rows:
        return pd.DataFrame(), status
    feats = pd.concat(rows, ignore_index=True)
    return transform(feats, art)[["symbol", "date"] + art["features"]], status


def run_live(source, out_dir, update_history=False):
    art = joblib.load(os.path.join(out_dir, "scaler.joblib"))
    hist = pd.read_csv(os.path.join(out_dir, "clean_history.csv.gz"), parse_dates=["date"])
    bars, warns = daily_to_bars(source)
    if bars.empty:
        return pd.DataFrame(), pd.DataFrame(columns=["symbol", "status", "gap_days"]), warns
    feats, status = build_live_features(bars[bars.date == bars.date.max()], hist, art)
    feats.to_csv(os.path.join(out_dir, "live_features.csv"), index=False)
    status.to_csv(os.path.join(out_dir, "live_status.csv"), index=False)
    if update_history:   # store every FINAL bar (new symbols start building history too)
        pd.concat([hist, bars]).drop_duplicates(["symbol", "date"], keep="last").sort_values(
            ["symbol", "date"]).to_csv(os.path.join(out_dir, "clean_history.csv.gz"), index=False)
    return feats, status, warns


# ----------------------------------------------------------------------------- parity test
def parity_test(out_dir, n_symbols=5, n_days=25):
    """Replay the last days of history through the LIVE path and compare with the batch path."""
    art = joblib.load(os.path.join(out_dir, "scaler.joblib"))
    hist = pd.read_csv(os.path.join(out_dir, "clean_history.csv.gz"), parse_dates=["date"])
    clean, _ = clean_bars(hist, drop_zero_volume=True)
    batch = transform(compute_features(clean), art)
    worst, checked = 0.0, 0
    for s in sorted(hist.symbol.unique())[:n_symbols]:
        b = batch[batch.symbol == s].tail(n_days)
        for _, row in b.iterrows():
            past = hist[(hist.symbol == s) & (hist.date < row["date"])]
            bar = hist[(hist.symbol == s) & (hist.date == row["date"])].copy()
            bar["open"] = np.nan     # the live feed has no open
            live, _ = build_live_features(bar[["symbol", "date"] + OHLC + ["volume"]], past, art)
            if live.empty:
                continue
            diff = (live[art["features"]].iloc[0] - row[art["features"]]).abs().max()
            worst, checked = max(worst, float(diff)), checked + 1
    return dict(rows_checked=checked, max_abs_diff=worst, passed=bool(checked and worst < 1e-8))


# ----------------------------------------------------------------------------- CLI
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["historical", "live", "parity"])
    ap.add_argument("--input", default="csv_output")
    ap.add_argument("--live", help="json/csv export of /api/v1/ohlcv/daily, or egx.sqlite3")
    ap.add_argument("--out", default="processed")
    ap.add_argument("--update-history", action="store_true")
    a = ap.parse_args()
    if a.mode == "historical":
        print(json.dumps(run_historical(a.input, a.out), indent=2, default=str))
    elif a.mode == "live":
        f, s, w = run_live(a.live, a.out, a.update_history)
        print("WARNINGS:", *w, sep="\n  ")
        print(s.status.value_counts().to_string() if len(s) else "no final bars")
        print(f"live feature rows: {len(f)}")
    else:
        print(parity_test(a.out))
