"""Offline tests on synthetic data (no network)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc_signal import backtest, signals  # noqa: E402
from btc_signal.signals import Config  # noqa: E402


def synthetic(n: int, freq: str, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq=freq, tz="UTC")
    ret = rng.normal(0.0004, 0.012, n) + 0.002 * np.sin(np.arange(n) / 150)
    close = 10_000 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread,
                         "low": np.minimum(open_, close) - spread, "close": close,
                         "volume": rng.uniform(100, 1000, n)}, index=idx)


@pytest.fixture(scope="module")
def frames():
    h4 = synthetic(4000, "4h")
    d1 = h4.resample("1D", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    fng = pd.Series(np.random.default_rng(1).uniform(10, 90, len(d1)), index=d1.index, name="fng")
    return h4, d1, fng


def test_score_bounded(frames):
    h4, d1, fng = frames
    f, _ = signals.compute(h4, d1, Config(), fng)
    s = f["score"].dropna()
    assert s.between(-100, 100).all()
    assert set(np.unique(f["signal"])) <= {-1, 0, 1}


@pytest.mark.parametrize("cut", [1500, 2600, 3999])
def test_no_lookahead(frames, cut):
    """Values at bar `cut` must not change when later bars are added."""
    h4, d1, fng = frames
    cfg = Config(swing_len=5, sides="both")
    full, _ = signals.compute(h4, d1, cfg, fng)
    t = h4.index[cut - 1]
    part, _ = signals.compute(h4.loc[:t], d1.loc[:t], cfg, fng.loc[:t])
    cols = ["score", "signal", "struct", "swing_hi", "swing_lo", "in_bull_zone", "htf_trend", "location"]
    pd.testing.assert_frame_equal(full.loc[:t, cols].tail(300), part[cols].tail(300), check_dtype=False)


def test_backtest_respects_stops(frames):
    h4, d1, fng = frames
    cfg = Config(swing_len=5, threshold=20, exit_mode="fixed", tp1_r=2.0)
    f, _ = signals.compute(h4, d1, cfg, fng)
    res = backtest.run(f, cfg, fee=0, slip=0)
    assert res["trades"], "expected some trades on synthetic data"
    for t in res["trades"]:
        # fixed 2R exit: a trade can lose at most ~1R (gaps aside) and win at most 2R
        assert -1.5 <= t["r"] <= 2.0 + 1e-6
        assert t["exit_time"] > t["entry_time"] or t["bars"] == 0


def test_trade_plan_geometry(frames):
    h4, d1, fng = frames
    cfg = Config()
    f, _ = signals.compute(h4, d1, cfg, fng)
    row = f.dropna(subset=["atr"]).iloc[-1]
    p = signals.trade_plan(row, 1, cfg)
    assert p["stop"] < p["entry"] < p["tp1"] < p["tp2"]
    risk = p["entry"] - p["stop"]
    assert cfg.sl_min_atr * row["atr"] - 1e-6 <= risk <= cfg.sl_max_atr * row["atr"] + 1e-6
