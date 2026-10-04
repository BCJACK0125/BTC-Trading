"""Tests for the cooldown state, the forward signal log, overfitting statistics and SMC events."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from btc_signal import backtest, history, signals, stats  # noqa: E402
from btc_signal.signals import Config  # noqa: E402
from btc_signal.smc import market_structure  # noqa: E402
from test_signal import synthetic  # noqa: E402

import update  # noqa: E402


@pytest.fixture(scope="module")
def frame():
    h4 = synthetic(3000, "4h", seed=11)
    d1 = h4.resample("1D", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    cfg = Config(swing_len=5, threshold=20)
    f, _ = signals.compute(h4, d1, cfg)
    return f, cfg


def test_cooldown_after_exit_on_last_bar(frame):
    f, cfg = frame
    trades = backtest.run(f, cfg)["trades"]
    assert trades
    exit_t = pd.Timestamp(trades[len(trades) // 2]["exit_time"], unit="s", tz="UTC")
    res = backtest.run(f.loc[:exit_t], cfg)
    assert res["open"] is None
    assert res["cooldown_left"] == cfg.cooldown

    row = f.loc[exit_t].copy()
    row["signal"], row["htf_trend"] = 1, 1.0
    assert update.decide(row, cfg, None, res["cooldown_left"])["action"] == "COOLDOWN"
    assert update.decide(row, cfg, None, 0)["action"] == "ENTER_LONG"


def test_history_merge_and_change_detection():
    a = [{"bar": 1, "action": "WAIT", "generated": "t1"}, {"bar": 2, "action": "WAIT", "generated": "t2"}]
    b = [{"bar": 2, "action": "WATCH", "generated": "t3"}, {"bar": 3, "action": "WATCH", "generated": "t4"}]
    m = history.merge(a, b)
    assert [e["bar"] for e in m] == [1, 2, 3]
    assert m[1]["action"] == "WATCH"  # newer publication of the same bar wins

    h, prev, changed = history.record(m, {"bar": 4, "action": "ENTER_LONG", "generated": "t5"})
    assert changed and prev["bar"] == 3 and h[-1]["bar"] == 4
    # re-running the same bar must not notify again
    _, _, again = history.record(h, {"bar": 4, "action": "ENTER_LONG", "generated": "t6"})
    assert not again
    # first ever entry: nothing to compare against
    assert history.record([], {"bar": 1, "action": "WAIT"})[2] is False


def test_psr_and_dsr():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, 2000)
    noise -= noise.mean()
    assert stats.psr(noise) == pytest.approx(0.5, abs=1e-6)
    good = rng.normal(0.002, 0.01, 2000)
    assert stats.psr(good) > 0.99
    # more trials -> higher bar -> lower deflated probability
    assert stats.expected_max_sharpe(1000, 1e-3) > stats.expected_max_sharpe(10, 1e-3) > 0
    d10, _ = stats.dsr(good, 10, 1e-3)
    d1000, _ = stats.dsr(good, 1000, 1e-3)
    assert d1000 < d10 <= stats.psr(good)


def test_bootstrap_trades():
    r = np.array([1.0, -1.0, 1.5, -1.0, 2.0, -0.5, 1.0, -1.0] * 10)
    mc = stats.bootstrap_trades(r, years=4, n_sims=500)
    assert mc["max_dd_p5"] <= mc["max_dd_p50"] <= mc["max_dd_p95"] <= 0
    assert mc["cagr_p5"] <= mc["cagr_p50"] <= mc["cagr_p95"]
    assert 0 <= mc["prob_loss_pct"] <= 100


def test_smc_bos_and_choch():
    # swing high at bar 10 (confirmed at 13), swing low at bar 20, then a break below it
    # (CHoCH from bullish) and a later break above the newer swing high (CHoCH back)
    up = list(np.linspace(100, 120, 11))
    down = list(np.linspace(119, 105, 10))
    bounce = list(np.linspace(106, 112, 6))
    crash = list(np.linspace(110, 95, 8))
    rally = list(np.linspace(96, 125, 12))
    close = np.array(up + down + bounce + crash + rally)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="4h", tz="UTC")
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}, index=idx)
    res = market_structure(df, length=3)
    kinds = [(e["type"], e["dir"]) for e in res.events]
    assert ("BOS", 1) in kinds or ("CHoCH", 1) in kinds
    assert any(d == -1 for _, d in kinds)
    # each break must happen after the pivot it breaks was confirmed (no look-ahead)
    for e in res.events:
        assert e["i"] >= 2 * 3
    assert res.frame["struct"].iloc[-1] == 1
