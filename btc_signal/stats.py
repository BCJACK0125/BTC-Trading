"""Overfitting-aware statistics for the parameter study.

- Probabilistic / Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014): the
  probability that the true Sharpe exceeds a benchmark, given sample length,
  skew and kurtosis. The deflated version raises the benchmark to the Sharpe
  you would expect from the best of N trials that are all pure noise.
- Trade-order bootstrap: how bad could drawdowns have been with the same
  trades in a different order.

Sharpe ratios here are per-period (daily, not annualised) unless noted.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def sharpe(rets: pd.Series | np.ndarray) -> float:
    r = np.asarray(rets, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd) if sd > 0 else 0.0


def psr(rets: pd.Series | np.ndarray, sr_benchmark: float = 0.0) -> float:
    """P(true per-period Sharpe > sr_benchmark)."""
    r = pd.Series(np.asarray(rets, dtype=float))
    t = len(r)
    if t < 3:
        return float("nan")
    sr = sharpe(r)
    skew = float(r.skew())
    kurt = float(r.kurt()) + 3.0  # pandas reports excess kurtosis
    var = (1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / (t - 1)
    if var <= 0:
        return float("nan")
    return _N.cdf((sr - sr_benchmark) / math.sqrt(var))


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """Expected maximum per-period Sharpe among n_trials skill-less strategies."""
    if n_trials < 2 or sr_var <= 0:
        return 0.0
    z1 = _N.inv_cdf(1 - 1 / n_trials)
    z2 = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_var) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def dsr(rets: pd.Series | np.ndarray, n_trials: int, sr_var: float) -> tuple[float, float]:
    """(deflated Sharpe probability, benchmark Sharpe it had to beat)."""
    sr0 = expected_max_sharpe(n_trials, sr_var)
    return psr(rets, sr0), sr0


def bootstrap_trades(ret_pct: np.ndarray, years: float, n_sims: int = 5000,
                     seed: int = 42) -> dict:
    """Resample per-trade returns (in % of equity) with replacement.

    Drawdowns are measured trade-to-trade, so they understate the intra-trade
    (mark-to-market) drawdown; compare against `actual_trade_dd_pct`.
    """
    r = np.asarray(ret_pct, dtype=float) / 100
    if len(r) < 5:
        return {}
    rng = np.random.default_rng(seed)
    sims = rng.choice(r, size=(n_sims, len(r)), replace=True)
    eq = np.cumprod(1 + sims, axis=1)
    peak = np.maximum.accumulate(np.maximum(eq, 1.0), axis=1)
    dd = (eq / peak - 1).min(axis=1) * 100
    cagr = (eq[:, -1] ** (1 / max(years, 1e-9)) - 1) * 100
    act = np.cumprod(1 + r)
    act_dd = float((act / np.maximum.accumulate(np.maximum(act, 1.0)) - 1).min() * 100)
    pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
    return {
        "n_sims": n_sims, "n_trades": int(len(r)),
        "cagr_p5": pct(cagr, 5), "cagr_p50": pct(cagr, 50), "cagr_p95": pct(cagr, 95),
        "max_dd_p5": pct(dd, 5), "max_dd_p50": pct(dd, 50), "max_dd_p95": pct(dd, 95),
        "prob_loss_pct": round(float((eq[:, -1] < 1).mean() * 100), 2),
        "actual_trade_dd_pct": round(act_dd, 2),
    }


def window_stats(daily_ret: pd.Series, trades: pd.DataFrame, start: str | None, end: str | None) -> dict:
    """Sharpe (annualised), trade count and profit factor of one config inside [start, end]."""
    r = daily_ret.loc[start:end]
    t = trades
    if len(t):
        ts = pd.to_datetime(t["entry_time"], unit="s", utc=True)
        mask = np.ones(len(t), dtype=bool)
        if start:
            mask &= ts >= pd.Timestamp(start, tz="UTC")
        if end:
            mask &= ts <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        t = t[mask]
    rs = t["r"].to_numpy() if len(t) else np.array([])
    gains, losses = rs[rs > 0].sum(), -rs[rs <= 0].sum()
    return {"sharpe": sharpe(r) * math.sqrt(365), "trades": int(len(rs)),
            "pf": float(gains / losses) if losses > 0 else (np.inf if gains > 0 else 0.0)}


def max_drawdown(rets: pd.Series) -> float:
    eq = (1 + rets).cumprod()
    return float((eq / eq.cummax() - 1).min() * 100) if len(eq) else 0.0
