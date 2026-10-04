"""Execute the 4h signal on 1h bars, optionally refining the entry on the lower timeframe.

The 4h signal is unchanged: it is known at the 1h bar that shares its close
time. What changes is how the trade is entered:

    market     enter at the next 1h open (= the next 4h open); the baseline;
    limit      rest a buy limit below the signal close (a fraction of 4h ATR, or
               the nearest bullish OB/FVG); skip the trade if not filled in time;
    structure  wait for a bullish 1h break of structure (BOS/CHoCH) and enter at
               the next 1h open; the stop goes under the 1h swing low, or stays
               at 2x 4h ATR to isolate the timing effect.

Management after entry is the strategy's own (TP1 partial, breakeven, ATR
trail, time stop, cooldown), evaluated hour by hour with the 4h ATR, so the
only difference between variants is the entry. Same conservative rules as
backtest.py: a bar touching both stop and target counts as a stop, and a limit
fill bar is only checked for the stop.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import indicators as ta
from .backtest import funding_per_bar, metrics
from .signals import Config, stop_distance
from .smc import market_structure


@dataclass(frozen=True)
class EntryRule:
    mode: str = "market"         # "market" | "limit" | "structure"
    window_h: int = 8            # hours a setup stays armed before it is dropped
    pullback_atr: float = 0.5    # limit: distance below the signal close, in 4h ATR
    zone: bool = False           # limit: prefer the nearest bullish OB/FVG top if within 1.5 ATR
    ltf_len: int = 3             # structure: 1h swing length
    ltf_stop: bool = True        # structure: stop under the 1h swing low (else 2x 4h ATR)
    choch_only: bool = False     # structure: require a CHoCH (a pullback that reverses)
    min_stop_atr: float = 0.5    # clamp for 1h-based stops, in 4h ATR
    max_stop_atr: float = 2.0

    def label(self) -> str:
        if self.mode == "market":
            return "market"
        if self.mode == "limit":
            return f"limit {'zone' if self.zone else f'-{self.pullback_atr:g}ATR'} {self.window_h}h"
        return (f"structure L{self.ltf_len} {'CHoCH' if self.choch_only else 'BOS/CHoCH'} {self.window_h}h "
                f"{'1h-stop' if self.ltf_stop else '4h-stop'}")

    def to_dict(self) -> dict:
        return asdict(self)


def ltf_features(h1: pd.DataFrame, length: int) -> pd.DataFrame:
    """Per 1h bar: did a bullish BOS/CHoCH confirm at this close, and the last confirmed swing low."""
    a = ta.atr(h1)
    st = market_structure(h1, length, a)
    bos = np.zeros(len(h1), dtype=bool)
    choch = np.zeros(len(h1), dtype=bool)
    for e in st.events:
        if e["dir"] == 1:
            (choch if e["type"] == "CHoCH" else bos)[e["i"]] = True
    return pd.DataFrame({"bull_bos": bos, "bull_choch": choch, "swing_lo": st.frame["swing_lo"],
                         "atr1": a}, index=h1.index)


def _asof(frame: pd.DataFrame | pd.Series, index: pd.DatetimeIndex):
    return frame.reindex(frame.index.union(index)).ffill().reindex(index)


def run(f4: pd.DataFrame, h1: pd.DataFrame, feats: pd.DataFrame, cfg: Config, rule: EntryRule,
        risk: float = 0.01, fee: float = 0.0005, slip: float = 0.0002, max_lev: float = 10.0,
        start: str | None = None, end: str | None = None, funding: pd.Series | None = None) -> dict:
    h = h1.loc[start:end]
    ft = feats.loc[h.index]
    idx, n = h.index, len(h)
    o, hi, lo, c = (h[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr4 = _asof(f4["atr"], idx).to_numpy()
    sig = f4["signal"].reindex(idx).fillna(0).to_numpy()   # only on 1h bars sharing a 4h close
    trig = (ft["bull_choch"] if rule.choch_only else ft["bull_choch"] | ft["bull_bos"]).to_numpy()
    swing_lo, atr1 = ft["swing_lo"].to_numpy(), ft["atr1"].to_numpy()
    fund = funding_per_bar(idx, funding) if funding is not None else np.zeros(n)
    bar_h = 4                                    # the signal timeframe, in hours
    max_hold, cooldown = cfg.max_bars * bar_h, pd.Timedelta(hours=cfg.cooldown * bar_h)

    equity, eq = 1.0, np.empty(n)
    trades: list[dict] = []
    pos = pending = setup = None
    cool_until = idx[0] - pd.Timedelta(days=1)
    armed = filled = 0

    def open_pos(i: int, entry: float, d: float, setup_i: int) -> dict:
        nonlocal equity
        qty = min(equity * risk / d, equity * max_lev / entry)
        equity -= qty * entry * fee
        return {"entry": entry, "qty": qty, "qty0": qty, "R": d, "stop": entry - d,
                "tp1": entry + cfg.tp1_r * d, "tp2": entry + cfg.tp2_r * d, "i": i, "tp1_hit": False,
                "extreme": entry, "pnl": 0.0, "eq0": equity, "lev": qty * entry / equity,
                "stop_atr": d / atr4[setup_i], "wait_h": i - setup_i}

    for i in range(n):
        fresh_limit = False
        if pending is not None and pos is None:
            entry = o[i] * (1 + slip)
            d = pending["d"] if "d" in pending else entry - pending["stop"]
            lo_d, hi_d = rule.min_stop_atr * pending["atr"], rule.max_stop_atr * pending["atr"]
            d = d if "d" in pending else float(np.clip(d, lo_d, hi_d))
            if d > 0:
                pos = open_pos(i, entry, d, pending["j"])
                filled += 1
        pending = None
        if setup is not None and pos is None and setup["mode"] == "limit" and i > setup["j"] and lo[i] <= setup["limit"]:
            entry = min(o[i], setup["limit"])
            d = entry - setup["stop"]
            if d > 0:
                pos = open_pos(i, entry, d, setup["j"])
                filled += 1
                fresh_limit = True
            setup = None

        if pos:
            exit_px = reason = None
            if lo[i] <= pos["stop"]:
                exit_px = min(o[i], pos["stop"]) * (1 - slip) if not fresh_limit else pos["stop"] * (1 - slip)
                reason = ("trail" if cfg.exit_mode == "trail" else "breakeven") if pos["tp1_hit"] else "stop"
            elif not fresh_limit:
                if cfg.exit_mode == "fixed":
                    if hi[i] >= pos["tp1"]:
                        exit_px, reason = pos["tp1"], "target"
                else:
                    if not pos["tp1_hit"] and hi[i] >= pos["tp1"]:
                        part = pos["qty"] * 0.5
                        pnl = (pos["tp1"] - pos["entry"]) * part - part * pos["tp1"] * fee
                        equity += pnl; pos["pnl"] += pnl
                        pos["qty"] -= part
                        pos["tp1_hit"], pos["stop"] = True, pos["entry"]
                    if pos["tp1_hit"] and cfg.exit_mode == "partial" and hi[i] >= pos["tp2"]:
                        exit_px, reason = pos["tp2"], "target"
                if exit_px is None and i - pos["i"] >= max_hold:
                    exit_px, reason = c[i] * (1 - slip), "time"
            if exit_px is not None:
                q = pos["qty"]
                pnl = (exit_px - pos["entry"]) * q - q * exit_px * fee
                equity += pnl; pos["pnl"] += pnl
                trades.append({"entry_time": int(idx[pos["i"]].timestamp()), "exit_time": int(idx[i].timestamp()),
                               "side": "long", "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
                               "reason": reason, "r": round(pos["pnl"] / (pos["qty0"] * pos["R"]), 3),
                               "ret_pct": round(pos["pnl"] / pos["eq0"] * 100, 3), "bars": i - pos["i"],
                               "lev": round(pos["lev"], 2), "stop_atr": round(pos["stop_atr"], 2),
                               "wait_h": pos["wait_h"]})
                pos = None
                cool_until = idx[i] + cooldown
            elif cfg.exit_mode == "trail" and pos["tp1_hit"]:
                pos["extreme"] = max(pos["extreme"], hi[i])
                pos["stop"] = max(pos["stop"], pos["extreme"] - cfg.trail_atr * atr4[i])

        if pos and fund[i]:
            cost = pos["qty"] * c[i] * fund[i]
            equity -= cost; pos["pnl"] -= cost
        eq[i] = equity + ((c[i] - pos["entry"]) * pos["qty"] if pos else 0.0)

        # --- setups are decided on closed bars only ------------------------------------
        if pos is None and setup is not None:
            if i - setup["j"] >= rule.window_h:
                setup = None                                   # missed: never filled / triggered
            elif setup["mode"] == "structure" and trig[i] and i + 1 < n:
                if rule.ltf_stop and not np.isnan(swing_lo[i]):
                    pending = {"stop": swing_lo[i] - 0.1 * atr1[i], "atr": setup["atr"], "j": setup["j"]}
                else:
                    pending = {"d": setup["d"], "atr": setup["atr"], "j": setup["j"]}
                setup = None
        if (pos is None and setup is None and pending is None and sig[i] == 1 and idx[i] > cool_until
                and i + 1 < n and not np.isnan(atr4[i])):
            row = f4.loc[idx[i]]
            d4 = stop_distance(row, 1, cfg)
            if d4 <= 0:
                continue
            armed += 1
            if rule.mode == "market":
                pending = {"d": d4, "atr": atr4[i], "j": i}
            elif rule.mode == "limit":
                limit = row["close"] - rule.pullback_atr * atr4[i]
                z = row["near_bull_zone"]
                if rule.zone and not np.isnan(z) and row["close"] - z <= 1.5 * atr4[i]:
                    limit = min(z, row["close"])
                stop = row["close"] - d4                       # invalidation stays where the 4h plan put it
                if limit > stop:
                    setup = {"mode": "limit", "j": i, "limit": limit, "stop": stop, "atr": atr4[i]}
            else:
                setup = {"mode": "structure", "j": i, "d": d4, "atr": atr4[i]}

    equity_curve = pd.Series(eq, index=idx)
    m = metrics(equity_curve, trades, h)
    m.update({"setups": armed, "fill_rate_pct": round(filled / armed * 100, 1) if armed else 0.0,
              "avg_stop_atr": round(float(np.mean([t["stop_atr"] for t in trades])), 2) if trades else 0.0,
              "avg_wait_h": round(float(np.mean([t["wait_h"] for t in trades])), 1) if trades else 0.0})
    return {"equity": equity_curve, "trades": trades, "metrics": m}
