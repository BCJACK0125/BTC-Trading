"""Bar-by-bar backtest of the signal frame.

Conservative assumptions:
- a signal on bar i is filled at the open of bar i+1 (no same-bar entry);
- if a bar touches both the stop and a target, the stop is assumed first;
- every fill pays `fee` per side, stop/market fills also pay `slip`;
- position size risks `risk` of current equity, capped at `max_lev` x equity;
- with `funding` (8h perpetual rates) an open position pays side x notional x rate
  at every funding time it is held through; missing history (before the
  perpetual listed) is charged at the 0.01% base rate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .signals import Config, stop_distance

BASE_FUNDING = 0.0001  # per 8h


def funding_per_bar(index: pd.DatetimeIndex, funding: pd.Series) -> np.ndarray:
    """Sum of funding rates charged during each bar (bars indexed by close time)."""
    out = np.zeros(len(index))
    if len(index) < 2:
        return out
    sched = pd.date_range(index[0].floor("8h"), index[-1], freq="8h")
    rates = funding.sort_index().reindex(sched, method="nearest", tolerance=pd.Timedelta("1h")).fillna(BASE_FUNDING)
    k = index.searchsorted(sched)  # first bar whose close is at or after the funding time
    ok = (k < len(index)) & (sched > index[0] - (index[1] - index[0]))
    np.add.at(out, k[ok], rates.to_numpy()[ok])
    return out


def run(f: pd.DataFrame, cfg: Config, risk: float = 0.01, fee: float = 0.0005,
        slip: float = 0.0002, max_lev: float = 2.0, start: str | None = None,
        end: str | None = None, funding: pd.Series | None = None) -> dict:
    if start or end:
        f = f.loc[start:end]
    o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr, sig = f["atr"].to_numpy(), f["signal"].to_numpy()
    idx = f.index
    n = len(f)
    fund = funding_per_bar(idx, funding) if funding is not None else np.zeros(n)

    equity = 1.0
    eq = np.empty(n)
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    pending = None  # (side, stop_dist) decided on the previous bar

    for i in range(n):
        if pending and pos is None:
            side, d = pending
            entry = o[i] * (1 + side * slip)
            qty = min(equity * risk / d, equity * max_lev / entry)
            equity -= qty * entry * fee
            pos = {"side": side, "entry": entry, "qty": qty, "qty0": qty, "R": d,
                   "stop": entry - side * d, "tp1": entry + side * cfg.tp1_r * d,
                   "tp2": entry + side * cfg.tp2_r * d, "i": i, "tp1_hit": False,
                   "extreme": entry, "pnl": 0.0, "eq0": equity, "lev": qty * entry / equity}
        pending = None

        if pos:
            s = pos["side"]
            exit_px, exit_qty, reason = None, 0.0, None
            hit_stop = (l[i] <= pos["stop"]) if s == 1 else (h[i] >= pos["stop"])
            if hit_stop:
                # gap through the stop fills at the open
                px = min(o[i], pos["stop"]) if s == 1 else max(o[i], pos["stop"])
                exit_px, exit_qty = px * (1 - s * slip), pos["qty"]
                reason = ("trail" if cfg.exit_mode == "trail" else "breakeven") if pos["tp1_hit"] else "stop"
            else:
                reach = (lambda lvl: h[i] >= lvl) if s == 1 else (lambda lvl: l[i] <= lvl)
                if cfg.exit_mode == "fixed":
                    if reach(pos["tp1"]):
                        exit_px, exit_qty, reason = pos["tp1"], pos["qty"], "target"
                else:
                    if not pos["tp1_hit"] and reach(pos["tp1"]):
                        part = pos["qty"] * 0.5
                        pnl = s * (pos["tp1"] - pos["entry"]) * part - part * pos["tp1"] * fee
                        equity += pnl; pos["pnl"] += pnl
                        pos["qty"] -= part
                        pos["tp1_hit"] = True
                        pos["stop"] = pos["entry"]
                    if pos["tp1_hit"] and cfg.exit_mode == "partial" and reach(pos["tp2"]):
                        exit_px, exit_qty, reason = pos["tp2"], pos["qty"], "target"
                if exit_px is None and i - pos["i"] >= cfg.max_bars:
                    exit_px, exit_qty, reason = c[i] * (1 - s * slip), pos["qty"], "time"

            if exit_px is not None:
                pnl = s * (exit_px - pos["entry"]) * exit_qty - exit_qty * exit_px * fee
                equity += pnl; pos["pnl"] += pnl
                trades.append({
                    "entry_time": int(idx[pos["i"]].timestamp()), "exit_time": int(idx[i].timestamp()),
                    "side": "long" if s == 1 else "short", "entry": round(pos["entry"], 2),
                    "exit": round(exit_px, 2), "reason": reason,
                    "r": round(pos["pnl"] / (pos["qty0"] * pos["R"]), 3),
                    "ret_pct": round(pos["pnl"] / pos["eq0"] * 100, 3), "bars": i - pos["i"],
                    "lev": round(pos["lev"], 2),
                })
                pos = None
                cooldown_until = i + cfg.cooldown
            elif cfg.exit_mode == "trail" and pos["tp1_hit"]:
                pos["extreme"] = max(pos["extreme"], h[i]) if s == 1 else min(pos["extreme"], l[i])
                trail = pos["extreme"] - s * cfg.trail_atr * atr[i]
                pos["stop"] = max(pos["stop"], trail) if s == 1 else min(pos["stop"], trail)

        if pos and fund[i]:
            cost = pos["side"] * pos["qty"] * c[i] * fund[i]
            equity -= cost
            pos["pnl"] -= cost

        mark = equity
        if pos:
            mark += pos["side"] * (c[i] - pos["entry"]) * pos["qty"]
        eq[i] = mark

        if pos is None and i > cooldown_until and sig[i] != 0 and i + 1 < n and not np.isnan(atr[i]):
            d = stop_distance(f.iloc[i], int(sig[i]), cfg)
            if d > 0:
                pending = (int(sig[i]), d)

    equity_curve = pd.Series(eq, index=idx)
    open_pos = None
    if pos:
        open_pos = {"side": "long" if pos["side"] == 1 else "short", "entry": round(pos["entry"], 2),
                    "stop": round(pos["stop"], 2), "tp1": round(pos["tp1"], 2), "tp2": round(pos["tp2"], 2),
                    "tp1_hit": pos["tp1_hit"], "entry_time": int(idx[pos["i"]].timestamp()),
                    "bars": n - 1 - pos["i"], "r_now": round(pos["side"] * (c[-1] - pos["entry"]) / pos["R"], 2)}
    # bars at the end of the data during which a new signal would still be ignored
    cooldown_left = max(0, cooldown_until - (n - 1)) if pos is None else 0
    return {"equity": equity_curve, "trades": trades, "open": open_pos, "cooldown_left": cooldown_left,
            "metrics": metrics(equity_curve, trades, f)}


def metrics(eq: pd.Series, trades: list[dict], f: pd.DataFrame) -> dict:
    if eq.empty:
        return {}
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    dd = eq / eq.cummax() - 1
    rs = np.array([t["r"] for t in trades]) if trades else np.array([])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    bh = f["close"].iloc[-1] / f["open"].iloc[0]
    in_mkt = sum(t["bars"] for t in trades) / max(len(f), 1)
    return {
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
        "cagr_pct": round((eq.iloc[-1] ** (1 / years) - 1) * 100, 2),
        "max_dd_pct": round(dd.min() * 100, 2),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(rs) * 100, 1) if len(rs) else 0.0,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() < 0 else None,
        "avg_r": round(float(rs.mean()), 3) if len(rs) else 0.0,
        "exposure_pct": round(in_mkt * 100, 1),
        "avg_lev": round(float(np.mean([t["lev"] for t in trades])), 2) if trades else 0.0,
        "max_lev": round(float(np.max([t["lev"] for t in trades])), 2) if trades else 0.0,
        "buy_hold_pct": round((bh - 1) * 100, 2),
    }
