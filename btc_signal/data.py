"""Market data loaders.

Primary source is Binance's public market-data mirror (data-api.binance.vision),
which is reachable from GitHub-hosted runners (api.binance.com returns HTTP 451
for US IPs). OKX is used as a fallback. Every frame is indexed by bar *close*
time in UTC so higher-timeframe data can be joined without look-ahead.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
OKX_BAR = {"1h": "1H", "4h": "4H", "1d": "1Dutc"}
UA = {"User-Agent": "btc-signal/1.0"}


def _get(url: str, params: dict, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20, headers=UA)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (403, 451):  # geo-blocked: retrying will not help
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _frame(rows: list[list], interval: str) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df = df.astype({"open_time": "int64", "open": float, "high": float, "low": float,
                    "close": float, "volume": float})
    df = df.drop_duplicates("open_time").sort_values("open_time")
    df["time"] = pd.to_datetime(df["open_time"] + INTERVAL_MS[interval], unit="ms", utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]]


def fetch_binance(symbol: str, interval: str, start_ms: int) -> pd.DataFrame:
    for host in BINANCE_HOSTS:
        try:
            rows, cursor = [], start_ms
            while True:
                batch = _get(f"{host}/api/v3/klines",
                             {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000})
                if not batch:
                    break
                rows += [b[:6] for b in batch]
                cursor = batch[-1][0] + INTERVAL_MS[interval]
                if len(batch) < 1000:
                    break
            return _frame(rows, interval)
        except RuntimeError:
            continue
    raise RuntimeError("all Binance hosts failed")


def fetch_okx(symbol: str, interval: str, start_ms: int) -> pd.DataFrame:
    inst = symbol.replace("USDT", "-USDT")
    rows, after = [], None
    while True:
        params = {"instId": inst, "bar": OKX_BAR[interval], "limit": 100}
        if after:
            params["after"] = after
        data = _get("https://www.okx.com/api/v5/market/history-candles", params)["data"]
        if not data:
            break
        rows += [[int(d[0]), *d[1:6]] for d in data if d[8] == "1"]  # confirmed bars only
        after = data[-1][0]
        if int(after) <= start_ms:
            break
        time.sleep(0.12)
    df = _frame(rows, interval)
    return df[df.index >= pd.Timestamp(start_ms + INTERVAL_MS[interval], unit="ms", tz="UTC")]


def load_klines(interval: str, start: str = "2019-01-01", symbol: str = "BTCUSDT",
                cache_dir: str | Path | None = None) -> pd.DataFrame:
    """Closed bars from `start` to now, using an incremental CSV cache if given."""
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    cached = None
    if cache_dir:
        path = Path(cache_dir) / f"{symbol}_{interval}.csv"
        if path.exists():
            cached = pd.read_csv(path, index_col="time", parse_dates=["time"])
            if len(cached) and cached.index[0] <= pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=2):
                last_open_ms = int(cached.index[-1].timestamp() * 1000) - INTERVAL_MS[interval]
                start_ms = last_open_ms  # refetch the last bar too
            else:
                cached = None
    try:
        fresh = fetch_binance(symbol, interval, start_ms)
    except RuntimeError:
        fresh = fetch_okx(symbol, interval, start_ms)
    df = fresh if cached is None else pd.concat([cached, fresh])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index <= pd.Timestamp.now(tz="UTC")]  # drop the still-forming bar
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        df.to_csv(Path(cache_dir) / f"{symbol}_{interval}.csv", index_label="time")
    return df


def load_fear_greed() -> pd.Series:
    """Crypto Fear & Greed index (0-100), daily, indexed by the end of its day."""
    try:
        data = _get("https://api.alternative.me/fng/", {"limit": 0, "format": "json"})["data"]
    except RuntimeError:
        return pd.Series(dtype=float, name="fng")
    s = pd.Series({pd.Timestamp(int(d["timestamp"]), unit="s", tz="UTC") + pd.Timedelta(days=1):
                   float(d["value"]) for d in data}, name="fng")
    return s.sort_index()


def load_funding_now(symbol: str = "BTCUSDT") -> dict | None:
    """Latest perpetual funding rate; Binance first, OKX as fallback. Context only."""
    try:
        d = _get("https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": symbol}, retries=1)
        return {"rate": float(d["lastFundingRate"]), "source": "Binance"}
    except RuntimeError:
        pass
    try:
        d = _get("https://www.okx.com/api/v5/public/funding-rate",
                 {"instId": symbol.replace("USDT", "-USDT-SWAP")}, retries=2)["data"][0]
        return {"rate": float(d["fundingRate"]), "source": "OKX"}
    except (RuntimeError, KeyError, IndexError):
        return None


FUNDING_SEED = Path(__file__).resolve().parents[1] / "data" / "funding_seed.csv"


def _funding_binance(symbol: str, start_ms: int) -> dict:
    out, cursor = {}, start_ms
    while True:
        batch = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                     {"symbol": symbol, "startTime": cursor, "limit": 1000}, retries=2)
        if not batch:
            break
        out.update({int(r["fundingTime"]): float(r["fundingRate"]) for r in batch})
        cursor = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
    return out


def _funding_okx(symbol: str, start_ms: int) -> dict:
    """OKX keeps about three months of funding history; enough to fill gaps between runs."""
    out, after = {}, None
    while True:
        params = {"instId": symbol.replace("USDT", "-USDT-SWAP"), "limit": 100}
        if after:
            params["after"] = after
        data = _get("https://www.okx.com/api/v5/public/funding-rate-history", params, retries=2)["data"]
        if not data:
            break
        out.update({int(d["fundingTime"]): float(d["realizedRate"] or d["fundingRate"]) for d in data})
        after = data[-1]["fundingTime"]
        if int(after) <= start_ms:
            break
        time.sleep(0.12)
    return out


def load_funding_history(symbol: str = "BTCUSDT", cache_dir: str | Path | None = None) -> pd.Series:
    """8h perpetual funding rates since 2019-09, used to charge leveraged backtests.

    Starts from the newest of the cache and the seed file in the repo, then adds
    newer rates from Binance futures, or OKX where Binance is geo-blocked (GitHub
    runners). Returns whatever is available; callers fill gaps with a default.
    """
    frames = []
    for path in ([Path(cache_dir) / "funding.csv"] if cache_dir else []) + [FUNDING_SEED]:
        if path.exists():
            f = pd.read_csv(path, index_col=0).iloc[:, 0]
            f.index = pd.to_datetime(f.index, utc=True, format="ISO8601")
            frames.append(f)
    s = pd.concat(frames) if frames else pd.Series(dtype=float)
    last_ms = int(s.index.max().timestamp() * 1000) if len(s) else \
        int(pd.Timestamp("2019-09-10", tz="UTC").timestamp() * 1000)
    new: dict = {}
    for fetch in (_funding_binance, _funding_okx):
        try:
            new = fetch(symbol, last_ms + 1)
            break
        except (RuntimeError, KeyError, ValueError):
            continue
    if new:
        s = pd.concat([s, pd.Series({pd.Timestamp(k, unit="ms", tz="UTC"): v for k, v in new.items()})])
    s.index = s.index.round("min")  # some early Binance timestamps carry stray milliseconds
    s = s[~s.index.duplicated(keep="last")].sort_index().rename("funding")
    if cache_dir and len(s):
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        s.to_csv(Path(cache_dir) / "funding.csv", index_label="time")
    return s
