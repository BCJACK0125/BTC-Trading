"""Fetch data, compute the live signal + backtest, write docs/data/latest.json
and append to the forward signal log docs/data/history.json.

    python scripts/update.py [--cache data/cache]

Runs in GitHub Actions after every 4h candle close (see .github/workflows).
Environment (all optional): SITE_URL (deployed dashboard, used to recover the
signal log), TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import data, signals, backtest, compare, history, notify  # noqa: E402
from btc_signal.signals import Config, FACTORS, FACTOR_LABELS  # noqa: E402

OUT = ROOT / "docs" / "data" / "latest.json"
CHART_BARS = 400
OOS_START = "2024-01-01"
# start dates offered by the dashboard's comparison panel
PERIODS = [("full", "全期間", "2019-01-01"), ("oos", "樣本外 2024 起", OOS_START), ("y2025", "2025 起", "2025-01-01")]


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


def decide(row: pd.Series, cfg: Config, open_pos: dict | None, cooldown_left: int = 0,
           bar_hours: int = 4) -> dict:
    """Mirror exactly what the backtest would do at the next bar open."""
    s = float(row["score"])
    htf = row["htf_trend"]
    if open_pos:
        return {"action": "IN_POSITION", "label": "持倉中", "tone": "hold",
                "summary": "系統已在一筆多單中，依移動停損管理；若你尚未進場，屬於追價，建議等下一次訊號或回測支撐區。"}
    if row["signal"] == 1 and cooldown_left > 0:
        return {"action": "COOLDOWN", "label": "冷卻中", "tone": "watch",
                "summary": f"分數 {s:.0f} 已達門檻，但上一筆交易剛出場。規則要求出場後等 {cfg.cooldown} 根 K 線，"
                           f"還剩 {cooldown_left} 根（約 {cooldown_left * bar_hours} 小時）；屆時條件仍成立才進場。"}
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
    funding_hist = data.load_funding_history(cache_dir=args.cache)

    f, smc = signals.compute(d[tf], d["1d"], cfg, fng)
    others = {iv: signals.compute(d[iv], d["1d"], cfg, fng)[0] for iv in ("1h", "4h", "1d") if iv != tf}
    frames = {tf: f, **others}

    bt = backtest.run(f, cfg)
    row = f.iloc[-1]
    bar_hours = data.INTERVAL_MS[tf] // 3_600_000
    decision = decide(row, cfg, bt["open"], bt["cooldown_left"], bar_hours)
    plan = signals.trade_plan(row, 1, cfg)
    plan = {k: (r2(v) if isinstance(v, (float, int)) and not isinstance(v, bool) else v) for k, v in plan.items()}
    plan["tp1_note"] = "到價先平 50%，停損移到進場價"
    plan["trail_note"] = (f"剩餘 50% 用移動停損：最高價 − {cfg.trail_atr:g}×ATR（目前約 "
                          f"{cfg.trail_atr * row['atr']:,.0f} USDT）") if cfg.exit_mode == "trail" else None
    plan["time_stop_note"] = f"持有超過 {cfg.max_bars} 根 {tf} K 線（約 {cfg.max_bars * bar_hours / 24:g} 天）未出場則平倉"

    comparisons = [{"key": k, "label": label, **compare.period_report(f, cfg, d["1d"], funding_hist, start)}
                   for k, label, start in PERIODS]

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
            "yearly": yearly(bt["equity"], d["1d"]["close"]),
            "score_buckets": score_buckets(bt["trades"], f, cfg.threshold),
            "recent_trades": bt["trades"][-25:][::-1],
        },
        "compare": {
            "funding_avg_annual_pct": round(float(funding_hist.mean() * 3 * 365 * 100), 1) if len(funding_hist) else None,
            "kelly_risk_pct": kelly_risk_pct([t["r"] for t in bt["trades"]]),
            "periods": comparisons,
        },
    }
    out["signal"]["steps"] = order_steps(out, cfg, bt["trades"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    OUT.write_text(payload, encoding="utf-8")
    # same data as a script so docs/index.html also works when opened from disk (file://)
    OUT.with_suffix(".js").write_text(f"window.BTC_DATA={payload};", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {decision['action']} score={row['score']:.1f} price={row['close']:.2f}")

    update_history(out, bt["trades"], Path(args.cache), os.environ.get("SITE_URL", ""))


def history_entry(out: dict, trades: list[dict]) -> dict:
    pos = out["position"]
    e = {"bar": out["last_bar_close"], "generated": out["generated_at"], "action": out["signal"]["action"],
         "label": out["signal"]["label"], "score": out["signal"]["score"], "price": out["price"],
         "stop": pos["stop"] if pos else out["plan"]["stop"], "tp1": pos["tp1"] if pos else out["plan"]["tp1"]}
    if pos:
        e["tp1_hit"] = bool(pos["tp1_hit"])
    closed = closed_now(out, trades)
    if closed:
        e["closed"] = {k: closed[k] for k in ("entry", "exit", "r", "reason")}
    return e


def closed_now(out: dict, trades: list[dict]) -> dict | None:
    closed = [t for t in trades if t["exit_time"] == out["last_bar_close"]]
    return closed[-1] if closed else None


def kelly_risk_pct(rs: list[float]) -> float | None:
    """Risk per trade that maximises long-run growth given the backtest's R multiples (full Kelly)."""
    r = np.asarray(rs, dtype=float)
    if len(r) < 20:
        return None
    grid = np.linspace(0.001, 0.6, 600)
    growth = [np.mean(np.log1p(np.maximum(f * r, -0.999))) for f in grid]
    return round(float(grid[int(np.argmax(growth))]) * 100, 1)


def local_time(sec: int) -> str:
    return pd.Timestamp(sec, unit="s", tz="UTC").tz_convert("Asia/Taipei").strftime("%m/%d %H:%M")


def order_steps(out: dict, cfg: Config, trades: list[dict]) -> list[str]:
    """What to actually do at the exchange right now; shown on the page and sent in
    notifications. Times are Taiwan time (UTC+8)."""
    a, p, pos, bar = out["signal"]["action"], out["plan"], out["position"], out["chart"]["bar_seconds"]
    steps = []
    closed = closed_now(out, trades)
    if closed:
        how = {"stop": "止損觸發", "breakeven": "保本止損觸發", "trail": "移動停損觸發", "time": "持有時間到", "target": "止盈"}
        steps.append(f"系統這根 K 線已出場（{how.get(closed['reason'], closed['reason'])}，{closed['exit']:,.0f}，"
                     f"{closed['r']:+.2f}R）。止損單已觸發就不用動作；若是持有時間到，或你的止損沒有跟上，請市價平掉剩餘部位。")
    if a == "ENTER_LONG":
        d = p["entry"] - p["stop"]
        steps += [
            f"{local_time(out['last_bar_close'])}（台灣時間）這根 4h K 線已開盤，盡快以市價買入，或掛比現價高約 0.1% 的限價單確保成交。"
            "不要掛低等回檔：研究顯示會錯過最賺錢的交易；晚 1 小時進場平均只差約 0.03R。",
            f"數量＝帳戶資金 × 每筆風險 ÷ {d:,.0f}（止損距離，USDT）。例：1 萬 USDT、風險 1% → {100 / d:.4f} BTC。",
            f"成交後立刻掛「停損市價單」賣出全部，觸發價 {p['stop']:,.0f}。成交價和 {p['entry']:,.0f} 不同時，觸發價改為成交價 − {d:,.0f}。",
            f"同時掛「限價賣單」一半數量（勾選只減倉 / reduce-only）在 {p['tp1']:,.0f}（{cfg.tp1_r:g}R）。",
            "TP1 成交後，把停損改到你的進場價（保本），屆時會另外通知。",
            f"之後每根 4h K 線收盤，把停損上移到「最高價 − {cfg.trail_atr:g}×ATR」，只上移不下移；本頁「交易計畫」顯示最新數字。",
            f"若到 {local_time(out['last_bar_close'] + (cfg.max_bars + 1) * bar)} 仍未出場，市價平掉剩餘部位。",
        ]
    elif a == "IN_POSITION" and pos:
        steps.append(f"止損應設在 {pos['stop']:,.0f}（{'移動停損' if pos['tp1_hit'] else '初始止損'}），只上移不下移。")
        if pos["tp1_hit"]:
            steps.append(f"TP1 已到並平掉一半；剩下一半用移動停損，每根 4h 收盤後上移到「最高價 − {cfg.trail_atr:g}×ATR」。")
        else:
            steps.append(f"TP1 限價賣單（一半，只減倉）在 {pos['tp1']:,.0f}；成交後把止損移到進場價 {pos['entry']:,.0f}。")
        steps.append(f"若到 {local_time(pos['entry_time'] + cfg.max_bars * bar)} 仍未出場，市價平掉剩餘部位。")
        steps.append(f"以上以系統進場價 {pos['entry']:,.0f} 計算；你的成交價不同時，止損距離維持一樣。")
    elif not closed:
        steps.append("目前不需要下單。" + ("冷卻結束後條件仍成立會發出進場通知。" if a == "COOLDOWN" else "訊號成立時會發出通知。"))
    return steps


def notify_message(out: dict, e: dict, prev: dict | None, site_url: str, tp1_only: bool = False) -> tuple[str, str]:
    """(subject, body) for a state change or a TP1 fill."""
    if tp1_only:
        title = "TP1 已到，止損移到保本"
    elif e.get("closed") and e["action"] != "IN_POSITION":
        title = f"已出場 {e['closed']['r']:+.2f}R，現在：{e['label']}"
    else:
        title = e["label"]
    lines = [f"BTC 訊號台｜{title}",
             f"{out['timeframe']} K 線 {local_time(e['bar'])}（台灣時間）收盤，價格 {e['price']:,.0f}，分數 {e['score']:.1f}"
             + (f"（前一根：{prev['label']}）" if prev and not tp1_only else ""),
             "", out["signal"]["summary"], "", "要做的事："]
    lines += [f"{i}. {s}" for i, s in enumerate(out["signal"]["steps"], 1)]
    if site_url:
        lines += ["", site_url]
    lines += ["", "本通知僅供研究參考，不構成投資建議。"]
    return f"【BTC 訊號台】{title}｜{e['price']:,.0f}", "\n".join(lines)


def update_history(out: dict, trades: list[dict], cache: Path, site_url: str):
    """Merge the forward signal log from the deployed site, the cache and the repo, then
    append this bar. Sends a notification when the action changed since the last bar."""
    local = OUT.with_name("history.json")
    remote = history.read_url(site_url.rstrip("/") + "/data/history.json") if site_url else []
    hist = history.merge(remote, history.read_file(cache / "history.json"), history.read_file(local))
    e = history_entry(out, trades)
    tp1 = history.tp1_reached(hist, e)
    hist, prev, changed = history.record(hist, e)
    payload = json.dumps(hist, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    for path in (local, cache / "history.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    local.with_suffix(".js").write_text(f"window.BTC_HISTORY={payload};", encoding="utf-8")
    print(f"history: {len(hist)} entries ({len(remote)} from site)" + (f", changed from {prev['action']}" if changed else ""))
    if (changed or tp1) and notify.channels():
        subject, body = notify_message(out, e, prev, site_url, tp1_only=not changed)
        print("notified:", notify.send(body, subject) or "failed")


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
