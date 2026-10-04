"""Confluence score and trade plan.

Each factor is scaled to [-1, +1] (positive = bullish). The score is the
weighted mean of the factors times 100, so it always lies in [-100, +100].
Higher-timeframe frames are joined "as of" their bar close, so a 4h bar only
sees daily bars that had already closed.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import indicators as ta
from .smc import market_structure, StructureResult

FACTORS = ["htf_trend", "trend", "structure", "momentum", "location", "strength", "sentiment"]

FACTOR_LABELS = {
    "htf_trend": "日線趨勢",
    "trend": "主週期趨勢",
    "structure": "SMC 市場結構",
    "momentum": "動能 (RSI / MACD)",
    "location": "溢價 / 折價 + OB/FVG",
    "strength": "趨勢強度 (ADX)",
    "sentiment": "恐懼貪婪 (反向)",
}


@dataclass
class Config:
    swing_len: int = 10
    weights: dict = field(default_factory=lambda: {
        "htf_trend": 25, "trend": 15, "structure": 20, "momentum": 15,
        "location": 15, "strength": 10, "sentiment": 0})
    threshold: float = 50.0
    sides: str = "long_only"        # "long_only" | "both"
    require_htf: bool = True        # longs only when daily trend > 0, shorts only when < 0
    sl_mode: str = "structure"      # "structure" | "atr"
    sl_atr: float = 2.0             # used by sl_mode="atr"
    sl_min_atr: float = 1.0
    sl_max_atr: float = 3.0
    exit_mode: str = "partial"      # "fixed" | "partial" | "trail"
    tp1_r: float = 1.5
    tp2_r: float = 3.0
    trail_atr: float = 3.0
    max_bars: int = 60
    cooldown: int = 3

    def to_dict(self) -> dict:
        return asdict(self)


def _asof(base_index: pd.DatetimeIndex, frame: pd.DataFrame) -> pd.DataFrame:
    return frame.reindex(frame.index.union(base_index)).ffill().reindex(base_index)


def htf_features(daily: pd.DataFrame) -> pd.DataFrame:
    c = daily["close"]
    e50, e200 = ta.ema(c, 50), ta.ema(c, 200)
    trend = 0.5 * np.sign(c - e200) + 0.5 * np.sign(e50 - e200)
    return pd.DataFrame({"d_close": c, "d_ema50": e50, "d_ema200": e200,
                         "d_rsi": ta.rsi(c), "htf_trend": trend})


def compute(base: pd.DataFrame, daily: pd.DataFrame, cfg: Config,
            fng: pd.Series | None = None) -> tuple[pd.DataFrame, StructureResult]:
    f = base.copy()
    c = f["close"]
    f["ema21"], f["ema55"], f["ema200"] = ta.ema(c, 21), ta.ema(c, 55), ta.ema(c, 200)
    f["rsi"] = ta.rsi(c)
    f["atr"] = ta.atr(f)
    f = f.join(ta.adx(f)).join(ta.macd(c)[["hist"]])
    f["rvol"] = f["volume"] / f["volume"].rolling(20).mean()

    smc = market_structure(base, cfg.swing_len, f["atr"])
    f = f.join(smc.frame)
    f = f.join(_asof(f.index, htf_features(daily)))
    if fng is not None and len(fng):
        f["fng"] = _asof(f.index, fng.to_frame())["fng"]
    else:
        f["fng"] = np.nan

    # --- factors ------------------------------------------------------------
    f["trend"] = 0.5 * np.sign(f["ema21"] - f["ema55"]) + 0.5 * np.sign(c - f["ema55"])
    fresh = (f["bars_since_break"] <= 3 * cfg.swing_len).astype(float)
    f["structure"] = f["struct"] * (0.6 + 0.4 * fresh)

    rsi_part = ((f["rsi"] - 50) / 15).clip(-1, 1)
    # fade momentum when stretched: RSI > 75 (or < 25) is late to chase
    rsi_part = rsi_part.where(f["rsi"].between(25, 75), rsi_part * 0.3)
    f["momentum"] = 0.6 * rsi_part + 0.4 * np.sign(f["hist"])

    span = (f["swing_hi"] - f["swing_lo"]).where(lambda s: s > 0)
    pos = ((c - f["swing_lo"]) / span).clip(0, 1)
    zone = f["in_bull_zone"].astype(float) - f["in_bear_zone"].astype(float)
    f["location"] = (0.7 * (1 - 2 * pos) + 0.3 * zone).fillna(0).clip(-1, 1)
    f["range_pos"] = pos

    strength = ((f["adx"] - 15) / 15).clip(0, 1)
    f["strength"] = np.sign(f["pdi"] - f["mdi"]) * strength
    f["sentiment"] = ((50 - f["fng"]) / 40).clip(-1, 1).fillna(0)

    return apply_rules(f, cfg), smc


def apply_rules(f: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """(Re)compute score and entry signal from the factor columns."""
    f = f.copy()
    w = cfg.weights
    total = sum(w.values())
    f["score"] = sum(w[k] * f[k].fillna(0) for k in FACTORS) / total * 100

    long_ok = f["score"] >= cfg.threshold
    short_ok = f["score"] <= -cfg.threshold
    if cfg.require_htf:
        long_ok &= f["htf_trend"] > 0
        short_ok &= f["htf_trend"] < 0
    if cfg.sides == "long_only":
        short_ok &= False
    f["signal"] = np.where(long_ok, 1, np.where(short_ok, -1, 0))
    return f


def stop_distance(row: pd.Series, side: int, cfg: Config) -> float:
    """Distance from entry (row close) to the protective stop."""
    a = row["atr"]
    if cfg.sl_mode == "atr" or np.isnan(a):
        return cfg.sl_atr * a
    ref = row["swing_lo"] if side == 1 else row["swing_hi"]
    raw = (row["close"] - ref) * side + 0.25 * a if not np.isnan(ref) else cfg.sl_atr * a
    if raw <= 0:  # swing already on the wrong side of price
        raw = cfg.sl_atr * a
    return float(np.clip(raw, cfg.sl_min_atr * a, cfg.sl_max_atr * a))


def trade_plan(row: pd.Series, side: int, cfg: Config) -> dict:
    entry = float(row["close"])
    d = stop_distance(row, side, cfg)
    plan = {
        "side": "long" if side == 1 else "short",
        "entry": entry,
        "stop": entry - side * d,
        "tp1": entry + side * cfg.tp1_r * d,
        "tp2": entry + side * cfg.tp2_r * d,
        "risk_pct": d / entry * 100,
        "atr": float(row["atr"]),
    }
    # nearest opposing liquidity (zone / swing) as a reality check for TP2
    target = row["near_bear_zone"] if side == 1 else row["near_bull_zone"]
    plan["liquidity_target"] = None if np.isnan(target) else float(target)
    # a pullback entry: the nearest supporting zone, if close to price
    support = row["near_bull_zone"] if side == 1 else row["near_bear_zone"]
    if not np.isnan(support) and abs(entry - support) < 1.5 * row["atr"]:
        plan["pullback_entry"] = float(support)
    else:
        plan["pullback_entry"] = None
    return plan
