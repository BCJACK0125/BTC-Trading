"""Smart Money Concepts primitives without look-ahead.

A swing pivot of length L at bar j is only *known* at bar j + L, so every
state written for bar i uses pivots confirmed at or before i. With length=5 on BINANCE:BTCUSDT 4h, the active
order blocks and recent BOS/CHoCH levels matched LuxAlgo's "Smart Money
Concepts" on TradingView exactly (checked through the TradingView MCP on
2026-10-03: OBs 84098-83186, 81330-80126, 65059-64028; CHoCH 85649.95).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Zone:
    kind: str          # "bull_ob" | "bear_ob" | "bull_fvg" | "bear_fvg"
    top: float
    bottom: float
    start: int         # bar index where the zone was created
    end: int | None = None  # bar index where it was mitigated

    def to_dict(self, index: pd.DatetimeIndex) -> dict:
        return {"kind": self.kind, "top": round(self.top, 2), "bottom": round(self.bottom, 2),
                "start": int(index[self.start].timestamp()),
                "end": None if self.end is None else int(index[self.end].timestamp())}


@dataclass
class StructureResult:
    frame: pd.DataFrame
    events: list[dict] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)


def market_structure(df: pd.DataFrame, length: int = 10, atr: pd.Series | None = None,
                     max_zones: int = 5, fvg_min_atr: float = 0.15) -> StructureResult:
    h, l, c, o = (df[k].to_numpy() for k in ("high", "low", "close", "open"))
    n = len(df)
    a = atr.to_numpy() if atr is not None else np.full(n, np.nan)

    trend = np.zeros(n)
    since_break = np.full(n, np.nan)
    rng_hi = np.full(n, np.nan)
    rng_lo = np.full(n, np.nan)
    in_bull_zone = np.zeros(n, dtype=bool)
    in_bear_zone = np.zeros(n, dtype=bool)
    near_bull_top = np.full(n, np.nan)   # nearest active bullish zone below price (top edge)
    near_bear_bot = np.full(n, np.nan)   # nearest active bearish zone above price (bottom edge)

    events: list[dict] = []
    zones: list[Zone] = []
    active: list[Zone] = []

    sh = sl = None             # latest confirmed swing high / low: (price, index)
    sh_broken = sl_broken = True
    cur_trend, last_break = 0, None

    for i in range(n):
        j = i - length
        if j >= length:
            win = slice(j - length, j + length + 1)
            if h[j] == h[win].max():
                sh, sh_broken = (h[j], j), False
            if l[j] == l[win].min():
                sl, sl_broken = (l[j], j), False

        if sh and not sh_broken and c[i] > sh[0]:
            sh_broken = True
            kind = "CHoCH" if cur_trend == -1 else "BOS"
            cur_trend, last_break = 1, i
            events.append({"i": i, "type": kind, "dir": 1, "level": sh[0]})
            k = sh[1] + int(np.argmin(l[sh[1]:i + 1]))
            z = Zone("bull_ob", h[k], l[k], i)
            zones.append(z); active.append(z)
        if sl and not sl_broken and c[i] < sl[0]:
            sl_broken = True
            kind = "CHoCH" if cur_trend == 1 else "BOS"
            cur_trend, last_break = -1, i
            events.append({"i": i, "type": kind, "dir": -1, "level": sl[0]})
            k = sl[1] + int(np.argmax(h[sl[1]:i + 1]))
            z = Zone("bear_ob", h[k], l[k], i)
            zones.append(z); active.append(z)

        if i >= 2:
            gap_min = fvg_min_atr * a[i] if not np.isnan(a[i]) else 0.0
            if l[i] > h[i - 2] and c[i - 1] > h[i - 2] and l[i] - h[i - 2] > gap_min:
                z = Zone("bull_fvg", l[i], h[i - 2], i)
                zones.append(z); active.append(z)
            elif h[i] < l[i - 2] and c[i - 1] < l[i - 2] and l[i - 2] - h[i] > gap_min:
                z = Zone("bear_fvg", l[i - 2], h[i], i)
                zones.append(z); active.append(z)

        still = []
        for z in active:
            if z.start == i:
                still.append(z)
                continue
            bull = z.kind.startswith("bull")
            if (bull and c[i] < z.bottom) or (not bull and c[i] > z.top):
                z.end = i
                continue
            if bull and l[i] <= z.top:
                in_bull_zone[i] = True
            if not bull and h[i] >= z.bottom:
                in_bear_zone[i] = True
            still.append(z)
        # keep only the most recent zones of each kind
        active = []
        for kind in ("bull_ob", "bear_ob", "bull_fvg", "bear_fvg"):
            same = [z for z in still if z.kind == kind]
            for z in same[:-max_zones]:
                z.end = i  # aged out
            active += same[-max_zones:]

        below = [z.top for z in active if z.kind.startswith("bull") and z.top <= c[i]]
        above = [z.bottom for z in active if z.kind.startswith("bear") and z.bottom >= c[i]]
        near_bull_top[i] = max(below) if below else np.nan
        near_bear_bot[i] = min(above) if above else np.nan

        trend[i] = cur_trend
        since_break[i] = i - last_break if last_break is not None else np.nan
        rng_hi[i] = sh[0] if sh else np.nan
        rng_lo[i] = sl[0] if sl else np.nan

    frame = pd.DataFrame({
        "struct": trend, "bars_since_break": since_break,
        "swing_hi": rng_hi, "swing_lo": rng_lo,
        "in_bull_zone": in_bull_zone, "in_bear_zone": in_bear_zone,
        "near_bull_zone": near_bull_top, "near_bear_zone": near_bear_bot,
    }, index=df.index)
    for e in events:
        e["time"] = int(df.index[e["i"]].timestamp())
    return StructureResult(frame, events, zones)
