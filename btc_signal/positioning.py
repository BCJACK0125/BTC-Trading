"""Futures positioning from Binance's public archive (data.binance.vision), 2020-09 onward.

The 5-minute USDT-M "metrics" files hold total open interest (OI). A sharp price
drop with a large OI drop is the footprint of leveraged longs being liquidated
(a "flush"); a fast OI build-up into a rally means new leverage is crowding in.
Liquidation heatmaps are modelled estimates with no free history, so these OI
proxies are what can actually be tested.

Every feature is joined to the signal bars as of their close.
"""
from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

URL = "https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"
START = "2020-09-01"


def _fetch_day(symbol: str, day: str) -> pd.DataFrame | None:
    try:
        r = requests.get(URL.format(s=symbol, d=day), timeout=30, headers={"User-Agent": "btc-signal/1.0"})
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]))
        return df[["create_time", "sum_open_interest"]]
    except (requests.RequestException, zipfile.BadZipFile, KeyError, ValueError):
        return None


def load_oi(symbol: str = "BTCUSDT", cache_dir: str | Path | None = None, workers: int = 8) -> pd.Series:
    """5-minute open interest (in BTC), indexed by snapshot time (UTC); cached incrementally.
    The archive publishes each day about a day later."""
    path = Path(cache_dir) / f"{symbol}_oi_5m.csv" if cache_dir else None
    s = pd.Series(dtype=float)
    if path and path.exists():
        s = pd.read_csv(path, index_col=0).iloc[:, 0]
        s.index = pd.to_datetime(s.index, utc=True, format="ISO8601")
    first = (s.index[-1].normalize() if len(s) else pd.Timestamp(START, tz="UTC"))
    days = pd.date_range(first, pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), freq="D")
    with ThreadPoolExecutor(workers) as ex:
        frames = [f for f in ex.map(lambda d: _fetch_day(symbol, d.strftime("%Y-%m-%d")), days) if f is not None]
    if frames:
        new = pd.concat(frames)
        new = pd.Series(new["sum_open_interest"].astype(float).to_numpy(),
                        index=pd.to_datetime(new["create_time"], utc=True, format="ISO8601"))
        s = pd.concat([s, new]) if len(s) else new
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s > 0].rename("oi")
    if path is not None and len(s):
        path.parent.mkdir(parents=True, exist_ok=True)
        s.to_csv(path, index_label="time")
    return s


def _asof(s: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    return s.reindex(s.index.union(index)).ffill().reindex(index)


def oi_features(oi: pd.Series, f: pd.DataFrame, windows=(1, 2, 3, 6, 18)) -> pd.DataFrame:
    """Per signal bar: % change of OI and price over the last `w` bars, and the price
    move in units of ATR. OI is taken as of each bar close; bars before the archive
    starts (or after a long gap) are NaN."""
    o = _asof(oi, f.index)
    stale = _asof(pd.Series(oi.index, index=oi.index), f.index)
    o[(f.index.to_series() - stale) > pd.Timedelta("1h")] = np.nan   # no fresh snapshot near the close
    out = {}
    for w in windows:
        out[f"oi_chg_{w}"] = (o / o.shift(w) - 1) * 100
        out[f"px_atr_{w}"] = (f["close"] - f["close"].shift(w)) / f["atr"]
    return pd.DataFrame(out, index=f.index)
