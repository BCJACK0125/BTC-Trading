"""Do leverage flushes / crowding (open-interest proxies for liquidations) help?

    python scripts/oi_research.py

1. Event study: what does price do after a "long flush" (sharp drop + large OI
   drop) inside a daily uptrend, compared with any bar in a daily uptrend?
2. Strategy test, on top of the production config:
     flush_add     also enter after a long flush in a daily uptrend;
     crowd_filter  skip entries while OI is building fast into a rally.
OI history starts 2020-09, so in-sample is 2020-09 ~ 2023 (baseline re-measured on
the same window) and 2024 onward is out-of-sample. Event thresholds are OI-change
percentiles of the in-sample bars only. Writes reports/oi_research.md and
docs/data/oi.json (+ .js).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import backtest, data, positioning, signals, stats  # noqa: E402
from btc_signal.signals import Config  # noqa: E402

CACHE = ROOT / "data" / "cache"
IS_START, IS_END, OOS_START = "2020-09-01", "2023-12-31", "2024-01-01"
HORIZONS = {"24h": 6, "72h": 18}


def flush_mask(feat, f, w, drop_atr, oi_thr):
    return (f["htf_trend"] > 0) & (feat[f"px_atr_{w}"] <= -drop_atr) & (feat[f"oi_chg_{w}"] <= oi_thr)


def event_study(f: pd.DataFrame, feat: pd.DataFrame, q: dict) -> list[dict]:
    """Forward returns after flush events vs. all daily-uptrend bars, per period.
    Events closer than 6 bars to the previous one are merged (first bar kept)."""
    out = []
    fwd = {h: (f["close"].shift(-n) / f["close"] - 1) * 100 for h, n in HORIZONS.items()}
    up = f["htf_trend"] > 0
    for w, drop, pct in ((1, 1.5, 5), (2, 2.5, 5), (3, 2.5, 2)):
        ev = flush_mask(feat, f, w, drop, q[(w, pct)])
        idx = np.flatnonzero(ev.to_numpy())
        keep = [i for k, i in enumerate(idx) if k == 0 or i - idx[k - 1] > 6]
        ev = pd.Series(False, index=f.index)
        ev.iloc[keep] = True
        for tag, s, e in (("is", IS_START, IS_END), ("oos", OOS_START, None)):
            sl = slice(s, e)
            row = {"rule": f"{w * 4}h 內跌 ≥ {drop:g} ATR 且 OI 跌幅前 {pct}%", "period": tag,
                   "events": int(ev.loc[sl].sum())}
            for h in HORIZONS:
                a, b = fwd[h].loc[sl][ev.loc[sl]].dropna(), fwd[h].loc[sl][up.loc[sl]].dropna()
                row[f"event_{h}"] = round(float(a.mean()), 2) if len(a) else None
                row[f"event_{h}_up_pct"] = round(float((a > 0).mean() * 100), 0) if len(a) else None
                row[f"base_{h}"] = round(float(b.mean()), 2)
            out.append(row)
    return out


def evaluate(g: pd.DataFrame, cfg: Config) -> dict:
    out = {}
    for tag, s, e in (("is", IS_START, IS_END), ("oos", OOS_START, None)):
        m = backtest.run(g, cfg, start=s, end=e)["metrics"]
        out.update({f"{tag}_{k}": m[k] for k in ("sharpe", "cagr_pct", "max_dd_pct", "trades", "win_rate_pct", "avg_r")})
    return out


def main():
    spec = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    cfg = Config(**spec["config"])
    d = {iv: data.load_klines(iv, "2018-06-01" if iv == "1d" else "2019-01-01", cache_dir=CACHE) for iv in ("1d", "4h")}
    f, _ = signals.compute(d["4h"], d["1d"], cfg, data.load_fear_greed())
    oi = positioning.load_oi(cache_dir=CACHE)
    feat = positioning.oi_features(oi, f)
    ins = feat.loc[IS_START:IS_END]
    q = {(w, p): float(ins[f"oi_chg_{w}"].quantile(p / 100)) for w in (1, 2, 3, 6, 18) for p in (2, 5, 90, 95, 98)}

    rows = [{"family": "baseline", "label": "目前策略", **evaluate(f, cfg), "added_or_blocked": 0}]
    base_sig = f["signal"] == 1
    for w in (1, 2, 3):
        for drop in (1.5, 2.5):
            for pct in (5, 2):
                fl = flush_mask(feat, f, w, drop, q[(w, pct)])
                g = f.copy()
                g["signal"] = np.where(base_sig | fl, 1, 0)
                rows.append({"family": "flush_add", "label": f"多單洗盤後也進場：{w * 4}h 內跌 ≥ {drop:g} ATR 且 OI 跌幅前 {pct}%",
                             **evaluate(g, cfg), "added_or_blocked": int((fl & ~base_sig).sum())})
    for w in (6, 18):
        for pct in (90, 95, 98):
            crowd = (feat[f"oi_chg_{w}"] >= q[(w, pct)]) & (feat[f"px_atr_{w}"] > 0)
            g = f.copy()
            g["signal"] = np.where(base_sig & ~crowd, 1, 0)
            rows.append({"family": "crowd_filter", "label": f"槓桿擁擠時不進場：{w * 4}h OI 增幅前 {100 - pct}% 且價格上漲",
                         **evaluate(g, cfg), "added_or_blocked": int((base_sig & crowd).sum())})
    res = pd.DataFrame(rows)
    base, tested = res.iloc[0], res.iloc[1:]

    best = tested.sort_values("is_sharpe", ascending=False).iloc[0]
    dsr = None
    grid_path = ROOT / "reports" / "grid_results.csv"
    if grid_path.exists():
        # rebuild the best variant's in-sample returns to deflate it against every trial so far
        g = f.copy()
        if best["family"] == "flush_add":
            w, drop, pct = next((w, dr, p) for w in (1, 2, 3) for dr in (1.5, 2.5) for p in (5, 2)
                                if best["label"].startswith(f"多單洗盤後也進場：{w * 4}h 內跌 ≥ {dr:g} ATR 且 OI 跌幅前 {p}%"))
            g["signal"] = np.where(base_sig | flush_mask(feat, f, w, drop, q[(w, pct)]), 1, 0)
        else:
            w, pct = next((w, p) for w in (6, 18) for p in (90, 95, 98)
                          if best["label"].startswith(f"槓桿擁擠時不進場：{w * 4}h OI 增幅前 {100 - p}%"))
            crowd = (feat[f"oi_chg_{w}"] >= q[(w, pct)]) & (feat[f"px_atr_{w}"] > 0)
            g["signal"] = np.where(base_sig & ~crowd, 1, 0)
        rets = backtest.run(g, cfg, start=IS_START, end=IS_END)["equity"].resample("1D").last().dropna().pct_change().dropna()
        sharpes = pd.concat([pd.read_csv(grid_path)["is_sharpe"], tested["is_sharpe"]]) / np.sqrt(365)
        p, sr0 = stats.dsr(rets, len(sharpes), float(sharpes.var()))
        dsr = {"label": best["label"], "n_trials": int(len(sharpes)), "dsr": round(p * 100, 1)}

    fam = tested.groupby("family")
    summary = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
        "is_period": f"{IS_START} ~ {IS_END}", "oos_period": f"{OOS_START} ~ {f.index[-1].date()}",
        "oi_start": str(oi.index[0].date()), "n_variants": int(len(tested)),
        "baseline": json.loads(base.to_json()),
        "best_by_family": {k: json.loads(g.sort_values("is_sharpe", ascending=False).iloc[0].to_json()) for k, g in fam},
        "median_by_family": {k: {"is_sharpe": round(float(g["is_sharpe"].median()), 2),
                                 "oos_sharpe": round(float(g["oos_sharpe"].median()), 2)} for k, g in fam},
        "beat_is": int((tested["is_sharpe"] > base["is_sharpe"]).sum()),
        "beat_oos": int((tested["oos_sharpe"] > base["oos_sharpe"]).sum()),
        "beat_both": int(((tested["is_sharpe"] > base["is_sharpe"]) & (tested["oos_sharpe"] > base["oos_sharpe"])).sum()),
        "deflated_best": dsr,
        "events": event_study(f, feat, q),
        "variants": json.loads(res.to_json(orient="records")),
    }
    payload = json.dumps(summary, ensure_ascii=False)
    (ROOT / "docs" / "data" / "oi.json").write_text(payload, encoding="utf-8")
    (ROOT / "docs" / "data" / "oi.js").write_text(f"window.BTC_OI={payload};", encoding="utf-8")
    write_report(summary, res)
    pd.set_option("display.width", 220)
    print(pd.DataFrame(summary["events"]).to_string(index=False))
    print(res.sort_values("is_sharpe", ascending=False).to_string(index=False))
    print({k: summary[k] for k in ("beat_is", "beat_oos", "beat_both", "deflated_best")})


def write_report(s: dict, res: pd.DataFrame):
    b = s["baseline"]
    cols = ["family", "label", "is_sharpe", "oos_sharpe", "is_cagr_pct", "oos_cagr_pct", "is_max_dd_pct",
            "oos_max_dd_pct", "is_trades", "oos_trades", "added_or_blocked"]
    ev_cols = ["rule", "period", "events", "event_24h", "event_24h_up_pct", "base_24h", "event_72h", "event_72h_up_pct", "base_72h"]
    lines = [
        "# Leverage flush / crowding study (open-interest proxies)", "",
        f"Generated {s['generated']}. OI from Binance's public USDT-M metrics archive since {s['oi_start']}. "
        f"In-sample {s['is_period']} (baseline re-measured on it), out-of-sample {s['oos_period']}.", "",
        "## Event study: forward return (%) after a long flush inside a daily uptrend", "",
        "| " + " | ".join(ev_cols) + " |", "|" + "---|" * len(ev_cols),
        *["| " + " | ".join(str(e[c]) for c in ev_cols) + " |" for e in s["events"]], "",
        "`base_*` is the average forward return of every bar in a daily uptrend over the same period.", "",
        "## Strategy variants", "",
        f"Baseline: IS Sharpe {b['is_sharpe']}, OOS Sharpe {b['oos_sharpe']}. Of {s['n_variants']} variants, "
        f"{s['beat_is']} beat it in-sample, {s['beat_oos']} out-of-sample, {s['beat_both']} in both.",
    ]
    if s["deflated_best"]:
        d = s["deflated_best"]
        lines += [f"Best in-sample variant ({d['label']}): deflated Sharpe {d['dsr']}% over {d['n_trials']} trials."]
    lines += ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
              *["| " + " | ".join(str(r[c]) for c in cols) + " |"
                for _, r in res.sort_values("is_sharpe", ascending=False).iterrows()]]
    (ROOT / "reports" / "oi_research.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
