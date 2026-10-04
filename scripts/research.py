"""Parameter study with an in-sample / out-of-sample split.

    python scripts/research.py            # uses data/cache, refreshes it first

Selects the production config on 2019-2023 only, then reports how it (and the
rest of the top in-sample configs) did on 2024 onward. Writes:
    config/strategy.json        chosen config (read by scripts/update.py)
    docs/data/research.json     study summary shown on the dashboard
    reports/research.md         human-readable report

Robustness checks on top of the single split:
    walk-forward     re-run the same selection rule every year on data up to the
                     previous year end, trade the winner for one year, stitch;
    deflated Sharpe  probability the chosen Sharpe beats the best of N noise trials;
    Monte Carlo      trade-order bootstrap of the chosen config's drawdowns.
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

from btc_signal import data, signals, backtest, stats  # noqa: E402
from btc_signal.signals import Config  # noqa: E402

IS_START, IS_END, OOS_START = "2019-01-01", "2023-12-31", "2024-01-01"
WF_FIRST_YEAR = 2021          # first walk-forward test year (needs >= 2 training years)
MIN_TRADES_PER_YEAR = 8       # same bar as the main filter: 40 trades over 5 years
MIN_PF = 1.1
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


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("1D").last().dropna().pct_change().fillna(0.0)


def _evaluate(p: dict) -> tuple[dict, pd.Series, pd.DataFrame]:
    cfg = make_cfg(p)
    f = signals.apply_rules(_FEATURES[(p["tf"], p["swing_len"])], cfg)
    ins = backtest.run(f, cfg, end=IS_END)["metrics"]
    oos = backtest.run(f, cfg, start=OOS_START)["metrics"]
    full = backtest.run(f, cfg)
    trades = pd.DataFrame([(t["entry_time"], t["r"]) for t in full["trades"]], columns=["entry_time", "r"])
    row = {**p, **{f"is_{k}": v for k, v in ins.items()}, **{f"oos_{k}": v for k, v in oos.items()}}
    return row, daily_returns(full["equity"]).astype("float32"), trades


def rank_configs(tab: pd.DataFrame, keys: list[str], sharpe_col: str) -> pd.DataFrame:
    """Selection rule shared by the main split and walk-forward: Sharpe blended with
    the average Sharpe of the same config at the other thresholds (robustness)."""
    tab = tab.copy()
    group = [k for k in keys if k != "threshold"]
    tab["nbhd"] = tab.groupby(group)[sharpe_col].transform("mean")
    tab["rank_score"] = 0.6 * tab[sharpe_col] + 0.4 * tab["nbhd"]
    return tab.sort_values("rank_score", ascending=False)


def walk_forward(res: pd.DataFrame, daily: list[pd.Series], trades: list[pd.DataFrame], keys: list[str],
                 bh: pd.Series, prod: int) -> dict:
    """Anchored walk-forward of the whole selection procedure, one test year at a time.

    Test-year returns come from each config's full-history run, so a position opened
    late in the training year is carried into the test year as it would be live.
    """
    last_year = max(s.index[-1].year for s in daily)
    rows, pieces, prod_pieces = [], [], []
    for y in range(WF_FIRST_YEAR, last_year + 1):
        train_end = f"{y - 1}-12-31"
        st = pd.DataFrame([stats.window_stats(daily[i], trades[i], IS_START, train_end) for i in range(len(res))])
        tab = pd.concat([res[keys].reset_index(drop=True), st], axis=1)
        tab = tab[(tab["trades"] >= MIN_TRADES_PER_YEAR * (y - 2019)) & (tab["pf"] >= MIN_PF)]
        if tab.empty:
            continue
        best = rank_configs(tab, keys, "sharpe").index[0]
        test = daily[best].loc[str(y)].astype(float)
        prod_test = daily[prod].loc[str(y)].astype(float)
        pieces.append(test)
        prod_pieces.append(prod_test)
        rows.append({
            "year": y, "train": f"2019–{y - 1}", "partial": bool(test.index[-1].month < 12),
            "params": {k: (v.item() if hasattr(v, "item") else v) for k, v in res.loc[best, keys].items()},
            "same_as_prod": bool(best == prod),
            "train_sharpe": round(float(tab.loc[best, "sharpe"]), 2),
            "return_pct": round(float((1 + test).prod() - 1) * 100, 1),
            "sharpe": round(stats.sharpe(test) * np.sqrt(365), 2),
            "max_dd_pct": round(stats.max_drawdown(test), 1),
            "prod_return_pct": round(float((1 + prod_test).prod() - 1) * 100, 1),
            "buy_hold_pct": round(float((1 + bh.loc[str(y)]).prod() - 1) * 100, 1),
        })
    if not rows:
        return {}

    def summary(r: pd.Series) -> dict:
        years = max((r.index[-1] - r.index[0]).days / 365.25, 1e-9)
        return {"cagr_pct": round(float((1 + r).prod() ** (1 / years) - 1) * 100, 1),
                "sharpe": round(stats.sharpe(r) * np.sqrt(365), 2), "max_dd_pct": round(stats.max_drawdown(r), 1)}

    wf = pd.concat(pieces)
    return {"years": rows, "period": f"{wf.index[0].date()} ~ {wf.index[-1].date()}",
            "stitched": summary(wf), "prod": summary(pd.concat(prod_pieces)),
            "buy_hold": summary(bh.loc[wf.index[0]:wf.index[-1]])}


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
        out = list(ex.map(_evaluate, combos, chunksize=8))
    res = pd.DataFrame([o[0] for o in out])
    daily = [o[1] for o in out]
    trades = [o[2] for o in out]
    (ROOT / "reports").mkdir(exist_ok=True)
    res.to_csv(ROOT / "reports" / "grid_results.csv", index=False)
    print(f"{len(res)} configs evaluated")

    # --- selection on in-sample only -----------------------------------------
    ok = res[(res["is_trades"] >= 40) & (res["is_profit_factor"].fillna(0) >= 1.1)].copy()
    ok = rank_configs(ok, keys, "is_sharpe")
    best = ok.iloc[0]
    top = ok.head(20)

    base = baselines(d["1d"])
    robustness = robustness_checks(res, daily, trades, keys, int(ok.index[0]), d, features)
    summary = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
        "is_period": f"2019-01-01 ~ {IS_END}", "oos_period": f"{OOS_START} ~ {d['4h'].index[-1].date()}",
        "n_configs": int(len(res)), "n_eligible": int(len(ok)),
        "oos_share_profitable_all": round(float((res["oos_total_return_pct"] > 0).mean() * 100), 1),
        "oos_share_profitable_top20": round(float((top["oos_total_return_pct"] > 0).mean() * 100), 1),
        "oos_median_sharpe_top20": round(float(top["oos_sharpe"].median()), 2),
        "baselines": base,
        "robustness": robustness,
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


def robustness_checks(res: pd.DataFrame, daily: list[pd.Series], trades: list[pd.DataFrame],
                      keys: list[str], prod: int, d: dict, features: dict) -> dict:
    p = res.loc[prod, keys].to_dict()
    cfg = make_cfg(p)
    f = signals.apply_rules(features[(p["tf"], int(p["swing_len"]))], cfg)
    is_rets = daily_returns(backtest.run(f, cfg, end=IS_END)["equity"])
    oos_rets = daily_returns(backtest.run(f, cfg, start=OOS_START)["equity"])
    full = backtest.run(f, cfg)

    # dispersion of the in-sample Sharpe across every trial, per day (not annualised)
    sr_var = float((res["is_sharpe"] / np.sqrt(365)).var())
    n_trials = len(res)
    dsr_p, sr0 = stats.dsr(is_rets, n_trials, sr_var)
    deflated = {
        "n_trials": n_trials,
        "is_sharpe": round(stats.sharpe(is_rets) * np.sqrt(365), 2),
        "benchmark_sharpe": round(sr0 * np.sqrt(365), 2),   # expected best of N noise configs, annualised
        "dsr": round(dsr_p * 100, 1),                       # P(true IS Sharpe > that benchmark)
        "psr_is": round(stats.psr(is_rets) * 100, 1),       # P(true IS Sharpe > 0), no deflation
        "psr_oos": round(stats.psr(oos_rets) * 100, 1),     # P(true OOS Sharpe > 0): config fixed beforehand
    }
    eq = full["equity"]
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    mc = stats.bootstrap_trades(np.array([t["ret_pct"] for t in full["trades"]]), years)
    # daily bars are indexed by close time (next midnight); label them by their own day like resample("1D")
    bh = d["1d"]["close"].pct_change().fillna(0.0)
    bh.index = bh.index - pd.Timedelta(days=1)
    wf = walk_forward(res, daily, trades, keys, bh, prod)
    print("deflated sharpe:", deflated)
    print("monte carlo:", mc)
    print("walk-forward:", json.dumps({k: v for k, v in wf.items() if k != "years"}))
    return {"deflated": deflated, "monte_carlo": mc, "walk_forward": wf}


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
    rb = s.get("robustness") or {}
    if rb:
        ds, mc, wf = rb["deflated"], rb["monte_carlo"], rb["walk_forward"]
        lines += [
            "## Overfitting checks", "",
            f"- **Deflated Sharpe** ({ds['n_trials']} trials): in-sample Sharpe {ds['is_sharpe']} vs. "
            f"{ds['benchmark_sharpe']} expected from the best of {ds['n_trials']} skill-less configs, "
            f"DSR {ds['dsr']}% (> 95% is strong evidence). Without deflation PSR is {ds['psr_is']}%; "
            f"out-of-sample PSR (config fixed in advance) is {ds['psr_oos']}%.",
        ]
        if mc:
            lines += [
                f"- **Monte Carlo** ({mc['n_sims']} trade-order bootstraps of {mc['n_trades']} trades, 1% risk): "
                f"CAGR 5th/50th/95th pct {mc['cagr_p5']} / {mc['cagr_p50']} / {mc['cagr_p95']}%, "
                f"max drawdown {mc['max_dd_p5']} / {mc['max_dd_p50']} / {mc['max_dd_p95']}% "
                f"(actual trade-to-trade {mc['actual_trade_dd_pct']}%), P(loss) {mc['prob_loss_pct']}%.",
            ]
        if wf:
            lines += [
                f"- **Walk-forward** {wf['period']}: the same selection rule re-run every year on data up to the "
                f"previous year end. Stitched: CAGR {wf['stitched']['cagr_pct']}%, Sharpe {wf['stitched']['sharpe']}, "
                f"max DD {wf['stitched']['max_dd_pct']}%. Fixed production config, same span: CAGR "
                f"{wf['prod']['cagr_pct']}%, Sharpe {wf['prod']['sharpe']}, max DD {wf['prod']['max_dd_pct']}%. "
                f"Buy & hold: CAGR {wf['buy_hold']['cagr_pct']}%, Sharpe {wf['buy_hold']['sharpe']}, "
                f"max DD {wf['buy_hold']['max_dd_pct']}%.", "",
                "| test year | trained on | selected config | return % | Sharpe | max DD % | production % | buy & hold % |",
                "|---|---|---|---|---|---|---|---|",
                *[f"| {r['year']}{' (partial)' if r['partial'] else ''} | {r['train']} | "
                  f"{' / '.join(str(v) for v in r['params'].values())} | {r['return_pct']} | {r['sharpe']} | "
                  f"{r['max_dd_pct']} | {r['prod_return_pct']} | {r['buy_hold_pct']} |" for r in wf["years"]],
                "", "Production-config returns before 2024 are in-sample.",
            ]
        lines += [""]
    lines += ["Strategy risk is 1% of equity per trade; returns scale roughly linearly with that choice, "
              "drawdowns too. Fees 0.05%/side + 0.02% slippage on market fills."]
    (ROOT / "reports" / "research.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
