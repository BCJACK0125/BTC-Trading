"""Fetch data, compute the live signal + backtest, write docs/data/latest.json.

    python scripts/update.py [--cache data/cache]

Runs in GitHub Actions after every 4h candle close (see .github/workflows).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import data, signals, backtest  # noqa: E402
from btc_signal.signals import Config, FACTORS, FACTOR_LABELS  # noqa: E402

OUT = ROOT / "docs" / "data" / "latest.json"
CHART_BARS = 400
OOS_START = "2024-01-01"


def r2(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), nd)


def load_strategy() -> tuple[str, Config]:
    spec = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    return spec["timeframe"], Config(**spec["config"])


def factor_details(row: pd.Series, cfg: Config) -> list[dict]:
    c = row["close"]
    pos = row["range_pos"]
    texts = {
        "htf_trend": f"日線收盤{'高於' if row['d_close'] > row['d_ema200'] else '低於'} EMA200（{row['d_ema200']:,.0f}），"
                     f"EMA50 {'在 EMA200 之上' if row['d_ema50'] > row['d_ema200'] else '跌破 EMA200'}",
        "trend": f"EMA21 {'>' if row['ema21'] > row['ema55'] else '<'} EMA55，價格{'站上' if c > row['ema55'] else '跌破'} EMA55 ({row['ema55']:,.0f})",
        "structure": ("多頭結構" if row["struct"] > 0 else "空頭結構" if row["struct"] < 0 else "尚無結構")
                     + (f"，最近一次突破在 {int(row['bars_since_break'])} 根 K 線前" if not np.isnan(row["bars_since_break"]) else ""),
        "momentum": f"RSI {row['rsi']:.1f}，MACD 柱狀體{'為正' if row['hist'] > 0 else '為負'}"
                    + ("（過熱，動能分數打折）" if row["rsi"] > 75 or row["rsi"] < 25 else ""),
        "location": (f"位於波段區間 {pos * 100:.0f}% 處（{'折價區' if pos < 0.5 else '溢價區'}）" if not np.isnan(pos) else "區間未定")
                    + ("，正在多方 OB/FVG 內" if row["in_bull_zone"] else "")
                    + ("，正在空方 OB/FVG 內" if row["in_bear_zone"] else ""),
        "strength": f"ADX {row['adx']:.1f}（{'有趨勢' if row['adx'] >= 20 else '盤整'}），+DI {'>' if row['pdi'] > row['mdi'] else '<'} -DI",
        "sentiment": f"恐懼貪婪指數 {row['fng']:.0f}" if not np.isnan(row["fng"]) else "無資料",
    }
    total = sum(cfg.weights.values())
    out = []
    for k in FACTORS:
        w = cfg.weights[k]
        if w == 0:
            continue
        v = float(np.nan_to_num(row[k]))
        out.append({"key": k, "label": FACTOR_LABELS[k], "value": round(v, 3), "weight": w,
                    "contribution": round(w * v / total * 100, 1), "detail": texts[k]})
    return out


def decide(row: pd.Series, cfg: Config, open_pos: dict | None) -> dict:
    s = float(row["score"])
    htf = row["htf_trend"]
    if open_pos and not open_pos.get("pending"):
        return {"action": "IN_POSITION", "label": "持倉中", "tone": "hold",
                "summary": "系統已在一筆多單中，依移動停損管理；若你尚未進場，屬於追價，建議等下一次訊號或回測支撐區。"}
    if row["signal"] == 1:
        return {"action": "ENTER_LONG", "label": "進場做多", "tone": "go",
                "summary": f"分數 {s:.0f} ≥ 門檻 {cfg.threshold:.0f} 且日線趨勢向上，條件成立。"}
    if htf <= 0:
        return {"action": "STAND_ASIDE", "label": "空手觀望", "tone": "stop",
                "summary": "日線趨勢不是多頭，此策略只做多，熊市階段不進場（回測中這是避開 2022 年大跌的關鍵）。"}
    if s >= cfg.threshold - 20:
        return {"action": "WATCH", "label": "接近訊號", "tone": "watch",
                "summary": f"日線多頭，分數 {s:.0f} 距離門檻 {cfg.threshold:.0f} 不遠，留意結構突破或回到折價區。"}
    return {"action": "WAIT", "label": "等待", "tone": "stop",
            "summary": f"分數 {s:.0f} 低於門檻 {cfg.threshold:.0f}，條件不足。"}


def tf_snapshot(f: pd.DataFrame) -> dict:
    row = f.iloc[-1]
    return {"time": int(f.index[-1].timestamp()), "close": r2(row["close"]), "score": r2(row["score"], 1),
            "trend": int(np.sign(row["trend"])), "structure": int(row["struct"]), "rsi": r2(row["rsi"], 1),
            "adx": r2(row["adx"], 1), "atr": r2(row["atr"]), "ema21": r2(row["ema21"]), "ema55": r2(row["ema55"]),
            "ema200": r2(row["ema200"]), "range_pos": r2(row["range_pos"], 2)}


def score_buckets(trades: list[dict], f: pd.DataFrame, threshold: float) -> list[dict]:
    if not trades:
        return []
    t = pd.DataFrame(trades)
    i = f.index.get_indexer(pd.to_datetime(t["entry_time"], unit="s", utc=True)) - 1
    t["score"] = f["score"].to_numpy()[i]
    out = []
    edges = sorted({int(threshold), 50, 60, 70, 101} - {e for e in (50, 60, 70) if e <= threshold})
    for lo, hi in zip(edges, edges[1:]):
        g = t[(t["score"] >= lo) & (t["score"] < hi)]
        if len(g):
            out.append({"range": f"{lo}–{min(hi, 100)}", "trades": len(g), "avg_r": round(g["r"].mean(), 2),
                        "win_rate": round((g["r"] > 0).mean() * 100, 1)})
    return out


def yearly(eq: pd.Series, close: pd.Series) -> list[dict]:
    out = []
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index.year < y]
        base = prev.iloc[-1] if len(prev) else 1.0
        cy = close[close.index.year == y]
        cprev = close[close.index.year < y]
        cbase = cprev.iloc[-1] if len(cprev) else cy.iloc[0]
        out.append({"year": int(y), "strategy": round((g.iloc[-1] / base - 1) * 100, 1),
                    "buy_hold": round((cy.iloc[-1] / cbase - 1) * 100, 1)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / "data" / "cache"))
    args = ap.parse_args()

    tf, cfg = load_strategy()
    d = {iv: data.load_klines(iv, "2018-06-01" if iv == "1d" else "2019-01-01", cache_dir=args.cache)
         for iv in ("1d", "4h", "1h")}
    fng = data.load_fear_greed()
    funding = data.load_funding_now()

    f, smc = signals.compute(d[tf], d["1d"], cfg, fng)
    others = {iv: signals.compute(d[iv], d["1d"], cfg, fng)[0] for iv in ("1h", "4h", "1d") if iv != tf}
    frames = {tf: f, **others}

    bt = backtest.run(f, cfg)
    row = f.iloc[-1]
    decision = decide(row, cfg, bt["open"])
    plan = signals.trade_plan(row, 1, cfg)
    plan = {k: (r2(v) if isinstance(v, (float, int)) and not isinstance(v, bool) else v) for k, v in plan.items()}
    plan["tp1_note"] = f"到價先平 50%，停損移到進場價"
    plan["trail_note"] = (f"剩餘 50% 用移動停損：最高價 − {cfg.trail_atr:g}×ATR（目前約 "
                          f"{cfg.trail_atr * row['atr']:,.0f} USDT）") if cfg.exit_mode == "trail" else None
    plan["time_stop_note"] = f"持有超過 {cfg.max_bars} 根 {tf} K 線（約 {cfg.max_bars * 4 // 24 if tf == '4h' else cfg.max_bars // 24} 天）未出場則平倉"

    risk_table = []
    for risk in (0.005, 0.01, 0.02, 0.03):
        m = backtest.run(f, cfg, risk=risk)["metrics"]
        risk_table.append({"risk_pct": risk * 100, "cagr_pct": m["cagr_pct"], "max_dd_pct": m["max_dd_pct"],
                           "total_return_pct": m["total_return_pct"]})

    eq_daily = bt["equity"].resample("1D").last().dropna()
    bh = d["1d"]["close"].reindex(eq_daily.index, method="ffill")
    bh = bh / bh.iloc[0]

    view = f.iloc[-CHART_BARS:]
    t0 = view.index[0]
    zones = [z.to_dict(f.index) for z in smc.zones
             if (z.end is None or f.index[z.end] >= t0) and z.kind.endswith("ob") or
             (z.end is None and z.kind.endswith("fvg") and f.index[z.start] >= t0)]
    events = [{k: e[k] for k in ("time", "type", "dir")} | {"level": r2(e["level"])}
              for e in smc.events if pd.Timestamp(e["time"], unit="s", tz="UTC") >= t0]
    trades_view = [t for t in bt["trades"] if t["exit_time"] >= int(t0.timestamp())]

    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "symbol": "BINANCE:BTCUSDT",
        "timeframe": tf,
        "last_bar_close": int(f.index[-1].timestamp()),
        "price": r2(row["close"]),
        "strategy": {"threshold": cfg.threshold, "sides": cfg.sides, "exit_mode": cfg.exit_mode,
                     "sl_mode": cfg.sl_mode, "sl_atr": cfg.sl_atr, "tp1_r": cfg.tp1_r, "tp2_r": cfg.tp2_r,
                     "trail_atr": cfg.trail_atr, "swing_len": cfg.swing_len, "weights": cfg.weights},
        "signal": {"score": r2(row["score"], 1), **decision, "factors": factor_details(row, cfg)},
        "plan": plan,
        "position": bt["open"],
        "timeframes": {iv: tf_snapshot(frames[iv]) for iv in ("1h", "4h", "1d")},
        "context": {
            "fear_greed": r2(fng.iloc[-1], 0) if len(fng) else None,
            "funding": funding,
        },
        "chart": {
            # all *_time values in this file are bar close times; candles use open time
            "bar_seconds": data.INTERVAL_MS[tf] // 1000,
            "candles": [[int(t.timestamp()) - data.INTERVAL_MS[tf] // 1000, r2(o), r2(h), r2(l), r2(c)]
                        for t, o, h, l, c in view[["open", "high", "low", "close"]].itertuples()],
            "ema21": [r2(v) for v in view["ema21"]],
            "ema55": [r2(v) for v in view["ema55"]],
            "ema200": [r2(v) for v in view["ema200"]],
            "score": [r2(v, 1) for v in view["score"]],
            "zones": zones,
            "events": events,
            "trades": trades_view,
        },
        "backtest": {
            "full": bt["metrics"],
            "is": backtest.run(f, cfg, end="2023-12-31")["metrics"],
            "oos": backtest.run(f, cfg, start=OOS_START)["metrics"],
            "equity": [[int(t.timestamp()), round(float(v), 4), round(float(b), 4)]
                       for t, v, b in zip(eq_daily.index, eq_daily, bh)],
            "yearly": yearly(bt["equity"], d["1d"]["close"]),
            "risk_table": risk_table,
            "score_buckets": score_buckets(bt["trades"], f, cfg.threshold),
            "recent_trades": bt["trades"][-25:][::-1],
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    OUT.write_text(payload, encoding="utf-8")
    # same data as a script so docs/index.html also works when opened from disk (file://)
    OUT.with_suffix(".js").write_text(f"window.BTC_DATA={payload};", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {decision['action']} score={row['score']:.1f} price={row['close']:.2f}")


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


if __name__ == "__main__":
    main()
