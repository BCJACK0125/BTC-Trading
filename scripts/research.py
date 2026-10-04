"""Parameter study with an in-sample / out-of-sample split.

    python scripts/research.py            # uses data/cache, refreshes it first

Selects the production config on 2019-2023 only, then reports how it (and the
rest of the top in-sample configs) did on 2024 onward. Writes:
    config/strategy.json        chosen config (read by scripts/update.py)
    docs/data/research.json     study summary shown on the dashboard
    reports/research.md         human-readable report
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import data, signals, backtest  # noqa: E402
from btc_signal.signals import Config  # noqa: E402

IS_END, OOS_START = "2023-12-31", "2024-01-01"
CACHE = ROOT / "data" / "cache"

WEIGHTS = {
    "base":        {"htf_trend": 25, "trend": 15, "structure": 20, "momentum": 15, "location": 15, "strength": 10, "sentiment": 0},
    "no_location": {"htf_trend": 25, "trend": 20, "structure": 25, "momentum": 20, "location": 0, "strength": 10, "sentiment": 0},
    "sentiment":   {"htf_trend": 25, "trend": 15, "structure": 20, "momentum": 10, "location": 10, "strength": 10, "sentiment": 10},
}
EXITS = {
    "fixed_2R":   {"exit_mode": "fixed", "tp1_r": 2.0},
    "partial":    {"exit_mode": "partial", "tp1_r": 1.5, "tp2_r": 3.0},
    "trail":      {"exit_mode": "trail", "tp1_r": 1.5, "trail_atr": 3.0},
}
GRID = {
    "tf": ["4h", "1h"],
    "swing_len": [5, 10, 20],
    "weights": list(WEIGHTS),
    "threshold": [30, 40, 50, 60],
    "sides": ["long_only", "both"],
    "exit": list(EXITS),
    "sl_mode": ["structure", "atr"],
}
MAX_BARS = {"4h": 60, "1h": 240}   # ~10 days either way

_FEATURES: dict = {}


def load_all():
    d = {iv: data.load_klines(iv, "2018-06-01" if iv == "1d" else "2019-01-01", cache_dir=CACHE)
         for iv in ("1d", "4h", "1h")}
    fng = data.load_fear_greed()
    return d, fng


def make_cfg(p: dict) -> Config:
    return Config(swing_len=p["swing_len"], weights=WEIGHTS[p["weights"]], threshold=p["threshold"],
                  sides=p["sides"], sl_mode=p["sl_mode"], max_bars=MAX_BARS[p["tf"]], **EXITS[p["exit"]])


def _init(features):
    _FEATURES.update(features)


def _evaluate(p: dict) -> dict:
    cfg = make_cfg(p)
    f = signals.apply_rules(_FEATURES[(p["tf"], p["swing_len"])], cfg)
    ins = backtest.run(f, cfg, end=IS_END)["metrics"]
    oos = backtest.run(f, cfg, start=OOS_START)["metrics"]
    return {**p, **{f"is_{k}": v for k, v in ins.items()}, **{f"oos_{k}": v for k, v in oos.items()}}


def baselines(daily: pd.DataFrame) -> dict:
    """Buy & hold and a 1x 'close above daily EMA200' trend filter."""
    out = {}
    c = daily["close"]
    above = (c > c.ewm(span=200, adjust=False, min_periods=200).mean()).shift(1, fill_value=False)
    rets = c.pct_change().fillna(0)
    for name, r in {"buy_hold": rets, "ema200_filter": rets.where(above, 0)}.items():
        for label, s, e in (("is", "2019-01-01", IS_END), ("oos", OOS_START, None)):
            rr = r.loc[s:e]
            eq = (1 + rr).cumprod()
            years = (eq.index[-1] - eq.index[0]).days / 365.25
            out[f"{name}_{label}"] = {
                "total_return_pct": round((eq.iloc[-1] - 1) * 100, 1),
                "cagr_pct": round((eq.iloc[-1] ** (1 / years) - 1) * 100, 1),
                "max_dd_pct": round((eq / eq.cummax() - 1).min() * 100, 1),
                "sharpe": round(float(rr.mean() / rr.std() * np.sqrt(365)), 2),
            }
    return out


def main():
    d, fng = load_all()
    print("data:", {k: len(v) for k, v in d.items()}, "fng:", len(fng))

    features = {}
    for tf in GRID["tf"]:
        for sl in GRID["swing_len"]:
            f, _ = signals.compute(d[tf], d["1d"], Config(swing_len=sl), fng)
            features[(tf, sl)] = f
    print("features ready")

    keys = list(GRID)
    combos = [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]
    with ProcessPoolExecutor(initializer=_init, initargs=(features,)) as ex:
        rows = list(ex.map(_evaluate, combos, chunksize=8))
    res = pd.DataFrame(rows)
    (ROOT / "reports").mkdir(exist_ok=True)
    res.to_csv(ROOT / "reports" / "grid_results.csv", index=False)
    print(f"{len(res)} configs evaluated")

    # --- selection on in-sample only -----------------------------------------
    ok = res[(res["is_trades"] >= 40) & (res["is_profit_factor"].fillna(0) >= 1.1)].copy()
    ok["is_calmar"] = ok["is_cagr_pct"] / ok["is_max_dd_pct"].abs().clip(lower=1)
    # robustness: average in-sample Sharpe of the config's threshold neighbours
    group = [k for k in keys if k != "threshold"]
    ok["is_sharpe_nbhd"] = ok.groupby(group)["is_sharpe"].transform("mean")
    ok["rank_score"] = 0.6 * ok["is_sharpe"] + 0.4 * ok["is_sharpe_nbhd"]
    ok = ok.sort_values("rank_score", ascending=False)
    best = ok.iloc[0]
    top = ok.head(20)

    base = baselines(d["1d"])
    summary = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
        "is_period": f"2019-01-01 ~ {IS_END}", "oos_period": f"{OOS_START} ~ {d['4h'].index[-1].date()}",
        "n_configs": int(len(res)), "n_eligible": int(len(ok)),
        "oos_share_profitable_all": round(float((res["oos_total_return_pct"] > 0).mean() * 100), 1),
        "oos_share_profitable_top20": round(float((top["oos_total_return_pct"] > 0).mean() * 100), 1),
        "oos_median_sharpe_top20": round(float(top["oos_sharpe"].median()), 2),
        "baselines": base,
        "by_dimension": {},
        "top": json.loads(top[keys + ["is_sharpe", "is_cagr_pct", "is_max_dd_pct", "is_trades", "is_win_rate_pct",
                                       "is_profit_factor", "oos_sharpe", "oos_cagr_pct", "oos_max_dd_pct",
                                       "oos_trades", "oos_win_rate_pct", "oos_profit_factor"]].to_json(orient="records")),
        "chosen": json.loads(best[keys + [c for c in res.columns if c.startswith(("is_", "oos_"))]].to_json()),
    }
    for k in keys:
        g = res.groupby(k)[["is_sharpe", "oos_sharpe"]].median().round(2)
        summary["by_dimension"][k] = {str(i): r.to_dict() for i, r in g.iterrows()}

    p = {k: (int(best[k]) if isinstance(best[k], (np.integer,)) else best[k]) for k in keys}
    cfg = make_cfg({**p, "swing_len": int(p["swing_len"]), "threshold": float(p["threshold"])})
    (ROOT / "config").mkdir(exist_ok=True)
    (ROOT / "config" / "strategy.json").write_text(json.dumps(
        {"timeframe": p["tf"], "weights_name": p["weights"], "exit_name": p["exit"], "config": cfg.to_dict(),
         "selected_on": summary["is_period"]}, indent=2, ensure_ascii=False), encoding="utf-8")
    (ROOT / "docs" / "data").mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary, ensure_ascii=False)
    (ROOT / "docs" / "data" / "research.json").write_text(payload, encoding="utf-8")
    (ROOT / "docs" / "data" / "research.js").write_text(f"window.BTC_RESEARCH={payload};", encoding="utf-8")
    write_report(summary, res)
    print(json.dumps(summary["chosen"], indent=1))
    print("baselines:", json.dumps(base, indent=1))


def write_report(s: dict, res: pd.DataFrame):
    c = s["chosen"]
    lines = [
        "# Strategy research", "",
        f"Generated {s['generated']}. In-sample {s['is_period']}, out-of-sample {s['oos_period']}.",
        f"{s['n_configs']} configurations evaluated, {s['n_eligible']} passed the in-sample filter "
        "(>= 40 trades, profit factor >= 1.1).", "",
        "## Chosen configuration (selected on in-sample only)", "",
        "| param | value |", "|---|---|",
        *[f"| {k} | {c[k]} |" for k in GRID],
        "", "| metric | in-sample | out-of-sample |", "|---|---|---|",
        *[f"| {m} | {c['is_' + m]} | {c['oos_' + m]} |" for m in
          ("total_return_pct", "cagr_pct", "max_dd_pct", "sharpe", "trades", "win_rate_pct", "profit_factor", "avg_r",
           "exposure_pct")],
        "", "## Baselines", "", "| baseline | period | return % | CAGR % | max DD % | Sharpe |", "|---|---|---|---|---|---|",
        *[f"| {k.rsplit('_', 1)[0]} | {k.rsplit('_', 1)[1]} | {v['total_return_pct']} | {v['cagr_pct']} | "
          f"{v['max_dd_pct']} | {v['sharpe']} |" for k, v in s["baselines"].items()],
        "", "## Robustness", "",
        f"- Out-of-sample profitable: {s['oos_share_profitable_all']}% of all configs, "
        f"{s['oos_share_profitable_top20']}% of the in-sample top 20.",
        f"- Median out-of-sample Sharpe of the in-sample top 20: {s['oos_median_sharpe_top20']}.",
        "", "## Median Sharpe by dimension", "",
    ]
    for k, g in s["by_dimension"].items():
        lines += [f"**{k}**: " + ", ".join(f"{i} → IS {v['is_sharpe']} / OOS {v['oos_sharpe']}" for i, v in g.items()), ""]
    lines += ["Strategy risk is 1% of equity per trade; returns scale roughly linearly with that choice, "
              "drawdowns too. Fees 0.05%/side + 0.02% slippage on market fills."]
    (ROOT / "reports" / "research.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
