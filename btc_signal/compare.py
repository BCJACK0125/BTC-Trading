"""Like-for-like comparisons against buy & hold.

The strategy is out of the market most of the time and risks a fixed fraction
per trade, so raw returns understate it and Sharpe flatters it. These helpers
put both on the same footing:

- leverage: strategy at several risk-per-trade levels (which sets its
  leverage) vs. buy & hold at 0.5x-3x, both paying perpetual funding when
  leveraged;
- equal drawdown: the strategy risk level whose max drawdown matches a buy &
  hold variant, and what each earned at that drawdown;
- monthly DCA from the same start date, as the "what most people do" reference.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest, stats
from .signals import Config

RISKS = (0.01, 0.02, 0.03, 0.05, 0.08)
HOLD_LEVERAGE = (0.5, 1.0, 2.0, 3.0)
MAX_LEV = 10.0   # exchange-style cap; at these risk levels the stop distance sets leverage


def _summary(rets: pd.Series) -> dict:
    eq = (1 + rets).cumprod()
    years = max((rets.index[-1] - rets.index[0]).days / 365.25, 1e-9)
    final = max(float(eq.iloc[-1]), 0.0)
    return {"cagr_pct": round((final ** (1 / years) - 1) * 100, 1),
            "total_return_pct": round((final - 1) * 100, 1),
            "max_dd_pct": round(stats.max_drawdown(rets), 1),
            "sharpe": round(stats.sharpe(rets) * np.sqrt(365), 2)}


def daily_funding(index: pd.DatetimeIndex, funding: pd.Series | None) -> pd.Series:
    """Funding paid per daily bar (index = daily close times)."""
    if funding is None or index.empty:
        return pd.Series(0.0, index=index)
    return pd.Series(backtest.funding_per_bar(index, funding), index=index)


def leveraged_hold(daily: pd.DataFrame, lev: float, funding: pd.Series | None) -> pd.Series:
    """Daily-rebalanced constant leverage on BTC. Above 1x it is held as a perpetual
    and pays funding; a day whose low would erase the equity liquidates it."""
    c = daily["close"]
    prev = c.shift(1)
    r = lev * (c / prev - 1)
    if lev > 1:
        r -= lev * daily_funding(daily.index, funding)
    wiped = lev * (daily["low"] / prev - 1) <= -1
    r = r.where(~wiped, -1.0).iloc[1:]
    dead = (r <= -1).cummax().shift(1, fill_value=False)
    return r.where(~dead, 0.0).clip(lower=-1)


def dca(daily: pd.DataFrame) -> dict:
    """Buy a fixed amount on the first day of every month; money-weighted results."""
    c = daily["close"].iloc[1:]
    buys = c.groupby([c.index.year, c.index.month]).head(1)
    units = (1 / buys).cumsum().reindex(c.index).ffill()
    invested = pd.Series(1.0, index=buys.index).cumsum().reindex(c.index).ffill()
    value = units * c
    pnl = value / invested - 1
    flows = [(t, -1.0) for t in buys.index] + [(c.index[-1], float(value.iloc[-1]))]
    t0 = flows[0][0]

    def npv(rate: float) -> float:
        return sum(v / (1 + rate) ** ((t - t0).days / 365.25) for t, v in flows)

    lo, hi = -0.99, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    return {"months": int(len(buys)), "irr_pct": round(mid * 100, 1),
            "return_on_invested_pct": round(float(pnl.iloc[-1]) * 100, 1),
            "worst_vs_invested_pct": round(float(pnl.min()) * 100, 1),
            "value_dd_pct": round(float((value / value.cummax() - 1).min()) * 100, 1)}


def _strategy(f: pd.DataFrame, cfg: Config, risk: float, start: str, funding: pd.Series | None) -> dict:
    return backtest.run(f, cfg, risk=risk, max_lev=MAX_LEV, start=start, funding=funding)


def match_drawdown(f: pd.DataFrame, cfg: Config, start: str, funding: pd.Series | None,
                   target_dd: float, lo: float = 0.002, hi: float = 0.25) -> dict:
    """Risk per trade whose max drawdown is closest to `target_dd` (negative %), by bisection."""
    best = None
    for _ in range(10):
        mid = (lo + hi) / 2
        res = _strategy(f, cfg, mid, start, funding)
        m = res["metrics"]
        if best is None or abs(m["max_dd_pct"] - target_dd) < abs(best[1]["max_dd_pct"] - target_dd):
            best = (mid, m, res)
        if m["max_dd_pct"] > target_dd:   # shallower than the target: take more risk
            lo = mid
        else:
            hi = mid
    risk, m, res = best
    eq = res["equity"]
    mc = stats.bootstrap_trades(np.array([t["ret_pct"] for t in res["trades"]]),
                                (eq.index[-1] - eq.index[0]).days / 365.25, n_sims=2000)
    return {"risk_pct": round(risk * 100, 1), "cagr_pct": m["cagr_pct"], "max_dd_pct": m["max_dd_pct"],
            "sharpe": m["sharpe"], "avg_lev": m["avg_lev"], "max_lev": m["max_lev"], "mc_dd_p5": mc.get("max_dd_p5")}


def period_report(f: pd.DataFrame, cfg: Config, daily: pd.DataFrame, funding: pd.Series | None,
                  start: str, chart_risks: tuple = (0.01, 0.02, 0.03, 0.05)) -> dict:
    """Everything the dashboard's comparison panel shows for one start date."""
    d = daily.loc[start:]                      # first row = the close right at `start`
    risk_rows, curves = [], {}
    for risk in RISKS:
        perp = _strategy(f, cfg, risk, start, funding)
        spot = _strategy(f, cfg, risk, start, None)["metrics"]
        m = perp["metrics"]
        eq = perp["equity"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        mc = stats.bootstrap_trades(np.array([t["ret_pct"] for t in perp["trades"]]), years, n_sims=2000)
        risk_rows.append({"risk_pct": risk * 100, "avg_lev": m["avg_lev"], "max_lev": m["max_lev"],
                          "cagr_no_funding_pct": spot["cagr_pct"], "cagr_pct": m["cagr_pct"],
                          "max_dd_pct": m["max_dd_pct"], "sharpe": m["sharpe"], "trades": m["trades"],
                          "mc_dd_p5": mc.get("max_dd_p5")})
        if risk in chart_risks:
            curves[f"{risk * 100:g}"] = eq.resample("1D").last().dropna()

    holds, hold_curves = [], {}
    for lev in HOLD_LEVERAGE:
        r = leveraged_hold(d, lev, funding)
        s = _summary(r)
        s["lev"] = lev
        s["liquidated"] = bool((r <= -1).any())
        holds.append(s)
        if lev in (1.0, 2.0):
            hold_curves[f"{lev:g}"] = (1 + r).cumprod()

    matched = []
    for h in holds:
        if h["lev"] > 1 or h["max_dd_pct"] > -3:
            continue  # leveraged holds draw down far beyond any sensible risk level
        matched.append({"hold_lev": h["lev"], "hold": h,
                        "strategy": match_drawdown(f, cfg, start, funding, h["max_dd_pct"])})

    # one shared daily axis for the chart, rebased to 1 at the start
    axis = next(iter(curves.values())).index
    chart = {"time": [int(t.timestamp()) for t in axis]}
    for k, s in curves.items():
        chart[f"strategy_{k}"] = [round(float(v), 4) for v in s.reindex(axis).ffill()]
    for k, s in hold_curves.items():
        s = pd.concat([pd.Series([1.0], index=[d.index[0]]), s])
        s.index = s.index - pd.Timedelta(days=1)  # daily close time -> the day it closes, like resample("1D")
        v = s.reindex(s.index.union(axis)).ffill().reindex(axis).bfill()
        chart[f"hold_{k}"] = [round(float(x), 4) for x in v]
    return {"start": start, "end": str(f.index[-1].date()), "risk": risk_rows, "hold": holds,
            "matched": matched, "dca": dca(d), "chart": chart}
