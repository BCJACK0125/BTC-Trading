"""Flow / positioning features that can be tested over the full 2019+ history.

- Coinbase premium: (Coinbase BTC-USD - Binance BTC-USDT) / Binance, hourly.
  A common proxy for US (incl. institutional) spot demand. It is measured
  against USDT, so a USDT de-peg shows up in it too.
- Funding crowding: average perpetual funding over the last N payments; high
  values mean leveraged longs are crowded.

Every feature is joined to the signal bars "as of" their close: a 4h bar only
sees hourly candles and funding payments that were complete at its close.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from .data import _get

CB_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CB_BATCH_H = 300  # max candles per request


def load_coinbase_1h(start: str = "2019-01-01", cache_dir: str | Path | None = None) -> pd.Series:
    """Hourly Coinbase BTC-USD closes indexed by candle close time (UTC), cached incrementally."""
    path = Path(cache_dir) / "coinbase_1h.csv" if cache_dir else None
    s = pd.Series(dtype=float)
    if path and path.exists():
        s = pd.read_csv(path, index_col=0).iloc[:, 0]
        s.index = pd.to_datetime(s.index, utc=True, format="ISO8601")
    cursor = s.index[-1] - pd.Timedelta(hours=1) if len(s) else pd.Timestamp(start, tz="UTC")
    now = pd.Timestamp.now(tz="UTC").floor("h")
    rows = {}
    while cursor < now:
        end = min(cursor + pd.Timedelta(hours=CB_BATCH_H), now)
        batch = _get(CB_URL, {"granularity": 3600, "start": cursor.isoformat(), "end": end.isoformat()})
        for t, _lo, _hi, _op, close, _vol in batch:
            rows[pd.Timestamp(t + 3600, unit="s", tz="UTC")] = float(close)
        cursor = end
        time.sleep(0.15)
    if rows:
        s = pd.concat([s, pd.Series(rows)]) if len(s) else pd.Series(rows)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s.index <= now].rename("cb_close")
    if path is not None and len(s):
        path.parent.mkdir(parents=True, exist_ok=True)
        s.to_csv(path, index_label="time")
    return s


def _asof(s: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    return s.reindex(s.index.union(index)).ffill().reindex(index)


def premium_features(cb: pd.Series, binance_1h: pd.DataFrame, index: pd.DatetimeIndex,
                     windows=(24, 72, 168)) -> pd.DataFrame:
    """Rolling mean of the hourly Coinbase premium (in %) over each window, as of each bar."""
    bn = binance_1h["close"]
    both = pd.concat([cb, bn], axis=1, join="inner").dropna()
    prem = (both.iloc[:, 0] / both.iloc[:, 1] - 1) * 100
    # a missing or stale print can create absurd gaps; keep the hourly value in a sane range
    prem = prem.clip(-1.5, 1.5)
    out = {}
    for w in windows:
        roll = prem.rolling(f"{w}h", min_periods=max(4, w // 2)).mean()
        out[f"cb_prem_{w}h"] = _asof(roll, index)
    return pd.DataFrame(out, index=index)


def funding_features(funding: pd.Series, index: pd.DatetimeIndex, windows=(3, 9, 21)) -> pd.DataFrame:
    """Mean funding rate (in % per 8h) over the last N payments, as of each bar."""
    f = funding.sort_index() * 100
    return pd.DataFrame({f"fund_{n}": _asof(f.rolling(n, min_periods=n).mean(), index) for n in windows},
                        index=index)


def with_filter(f: pd.DataFrame, allow: pd.Series) -> pd.DataFrame:
    """Block new long signals where `allow` is False (NaN = no data = allowed)."""
    g = f.copy()
    g["signal"] = np.where(allow.astype("boolean").fillna(True).to_numpy(dtype=bool), g["signal"], 0)
    return g


def with_factor(f: pd.DataFrame, weights: dict, threshold: float, require_htf: bool,
                factor: pd.Series, weight: float) -> pd.DataFrame:
    """Add one more [-1, +1] factor to the score with `weight`, then re-apply the entry rule (long only)."""
    g = f.copy()
    total = sum(weights.values())
    g["score"] = (g["score"] * total / 100 + weight * factor.fillna(0).clip(-1, 1)) / (total + weight) * 100
    ok = g["score"] >= threshold
    if require_htf:
        ok &= g["htf_trend"] > 0
    g["signal"] = np.where(ok, 1, 0)
    return g
