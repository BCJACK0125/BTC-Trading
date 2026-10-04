"""Do flow / positioning data improve the strategy?

    python scripts/flow_research.py

Tests the Coinbase premium (US spot demand) and perpetual funding (leveraged
long crowding) as entry filters and as an extra score factor on top of the
production config. Selection on 2019-2023 only, 2024 onward out-of-sample,
and the deflated Sharpe is recomputed with these extra trials added to the
original grid. Writes reports/flow_research.md and docs/data/flows.json (+ .js).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import backtest, data, flows, signals, stats  # noqa: E402
from btc_signal.signals import Config  # noqa: E402

CACHE = ROOT / "data" / "cache"
IS_END, OOS_START = "2023-12-31", "2024-01-01"
PREM_SCALE = 0.10      # a 0.10% average premium maps to a full +1 factor
FUND_NEUTRAL = 0.01    # % per 8h: the base rate most exchanges pay in quiet markets
FUND_SCALE = 0.03


def _cond(x: pd.Series, pred) -> pd.Series:
    """Rule result, left missing where the feature is missing (e.g. funding before Sept 2019)."""
    return pred(x).astype("boolean").mask(x.isna())


def variants(feat: pd.DataFrame) -> list[dict]:
    out = []
    for w in (24, 72, 168):
        for thr in (-0.10, -0.05, 0.0):
            out.append({"family": "prem_filter", "label": f"Coinbase 溢價 {w}h 均值 > {thr:+.2f}%",
                        "kind": "filter", "allow": _cond(feat[f"cb_prem_{w}h"], lambda x: x > thr)})
        for wt in (5, 10, 15):
            out.append({"family": "prem_factor", "label": f"Coinbase 溢價 {w}h 因子，權重 {wt}",
                        "kind": "factor", "weight": wt, "factor": feat[f"cb_prem_{w}h"] / PREM_SCALE})
    for n in (3, 9, 21):
        for thr in (0.03, 0.05, 0.10):
            out.append({"family": "fund_filter", "label": f"資金費率近 {n} 期均值 < {thr:.2f}%",
                        "kind": "filter", "allow": _cond(feat[f"fund_{n}"], lambda x: x < thr)})
        for wt in (5, 10):
            out.append({"family": "fund_factor", "label": f"資金費率近 {n} 期反向因子，權重 {wt}",
                        "kind": "factor", "weight": wt, "factor": (FUND_NEUTRAL - feat[f"fund_{n}"]) / FUND_SCALE})
    return out


def evaluate(f: pd.DataFrame, cfg: Config) -> dict:
    out = {}
    for tag, s, e in (("is", None, IS_END), ("oos", OOS_START, None)):
        m = backtest.run(f, cfg, start=s, end=e)["metrics"]
        out.update({f"{tag}_{k}": m[k] for k in ("sharpe", "cagr_pct", "max_dd_pct", "trades", "win_rate_pct",
                                                  "profit_factor", "avg_r")})
    return out


def main():
    spec = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    cfg = Config(**spec["config"])
    d = {iv: data.load_klines(iv, "2018-06-01" if iv == "1d" else "2019-01-01", cache_dir=CACHE)
         for iv in ("1d", "4h", "1h")}
    f, _ = signals.compute(d["4h"], d["1d"], cfg, data.load_fear_greed())
    cb = flows.load_coinbase_1h(cache_dir=CACHE)
    funding = data.load_funding_history(cache_dir=CACHE)
    feat = pd.concat([flows.premium_features(cb, d["1h"], f.index), flows.funding_features(funding, f.index)], axis=1)

    rows = [{"family": "baseline", "label": "目前策略", **evaluate(f, cfg), "changed_pct": 0.0}]
    base_signals = int((f["signal"] == 1).sum())
    for v in variants(feat):
        g = (flows.with_filter(f, v["allow"]) if v["kind"] == "filter" else
             flows.with_factor(f, cfg.weights, cfg.threshold, cfg.require_htf, v["factor"], v["weight"]))
        changed = int(((g["signal"] == 1) != (f["signal"] == 1)).sum())
        rows.append({"family": v["family"], "label": v["label"], **evaluate(g, cfg),
                     "changed_pct": round(changed / max(base_signals, 1) * 100, 1)})
    res = pd.DataFrame(rows)
    base = res.iloc[0]
    tested = res.iloc[1:]

    # deflated Sharpe of the best in-sample variant, counting the original 864 trials plus these
    grid = pd.read_csv(ROOT / "reports" / "grid_results.csv") if (ROOT / "reports" / "grid_results.csv").exists() else None
    best = tested.sort_values("is_sharpe", ascending=False).iloc[0]
    dsr = None
    if grid is not None:
        v = next(x for x in variants(feat) if x["label"] == best["label"])
        g = (flows.with_filter(f, v["allow"]) if v["kind"] == "filter" else
             flows.with_factor(f, cfg.weights, cfg.threshold, cfg.require_htf, v["factor"], v["weight"]))
        rets = backtest.run(g, cfg, end=IS_END)["equity"].resample("1D").last().dropna().pct_change().dropna()
        sharpes = pd.concat([grid["is_sharpe"], tested["is_sharpe"]]) / np.sqrt(365)
        p, sr0 = stats.dsr(rets, len(sharpes), float(sharpes.var()))
        dsr = {"label": best["label"], "n_trials": int(len(sharpes)), "dsr": round(p * 100, 1),
               "benchmark_sharpe": round(sr0 * np.sqrt(365), 2)}

    fam = tested.groupby("family")
    summary = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
        "is_period": f"2019-01-01 ~ {IS_END}", "oos_period": f"{OOS_START} ~ {f.index[-1].date()}",
        "n_variants": int(len(tested)),
        "baseline": json.loads(base.to_json()),
        "best_by_family": {k: json.loads(g.sort_values("is_sharpe", ascending=False).iloc[0].to_json()) for k, g in fam},
        "median_by_family": {k: {"is_sharpe": round(float(g["is_sharpe"].median()), 2),
                                 "oos_sharpe": round(float(g["oos_sharpe"].median()), 2)} for k, g in fam},
        "beat_is": int((tested["is_sharpe"] > base["is_sharpe"]).sum()),
        "beat_oos": int((tested["oos_sharpe"] > base["oos_sharpe"]).sum()),
        "beat_both": int(((tested["is_sharpe"] > base["is_sharpe"]) & (tested["oos_sharpe"] > base["oos_sharpe"])).sum()),
        "deflated_best": dsr,
        "premium_by_year": {str(y): round(float(v), 3) for y, v in
                            feat["cb_prem_168h"].groupby(feat.index.year).mean().items()},
        "variants": json.loads(res.to_json(orient="records")),
    }
    payload = json.dumps(summary, ensure_ascii=False)
    (ROOT / "docs" / "data" / "flows.json").write_text(payload, encoding="utf-8")
    (ROOT / "docs" / "data" / "flows.js").write_text(f"window.BTC_FLOWS={payload};", encoding="utf-8")
    write_report(summary, res)
    pd.set_option("display.width", 220)
    print(res.sort_values("is_sharpe", ascending=False).to_string(index=False))
    print({k: summary[k] for k in ("beat_is", "beat_oos", "beat_both", "deflated_best")})


def write_report(s: dict, res: pd.DataFrame):
    cols = ["family", "label", "is_sharpe", "oos_sharpe", "is_cagr_pct", "oos_cagr_pct", "is_max_dd_pct",
            "oos_max_dd_pct", "is_trades", "oos_trades", "changed_pct"]
    b = s["baseline"]
    lines = [
        "# Flow / positioning study", "",
        f"Generated {s['generated']}. Production config plus one flow rule at a time; selection on {s['is_period']}, "
        f"{s['oos_period']} out-of-sample. 1% risk, spot (no funding cost), fees and slippage as in the main backtest.", "",
        f"Baseline: IS Sharpe {b['is_sharpe']}, OOS Sharpe {b['oos_sharpe']}.",
        f"Of {s['n_variants']} variants, {s['beat_is']} beat it in-sample, {s['beat_oos']} out-of-sample, "
        f"{s['beat_both']} in both.", "",
    ]
    if s["deflated_best"]:
        d = s["deflated_best"]
        lines += [f"Best in-sample variant ({d['label']}): deflated Sharpe {d['dsr']}% over {d['n_trials']} trials "
                  f"(benchmark Sharpe {d['benchmark_sharpe']}).", ""]
    lines += ["## Median Sharpe by family", "", "| family | IS | OOS |", "|---|---|---|",
              *[f"| {k} | {v['is_sharpe']} | {v['oos_sharpe']} |" for k, v in s["median_by_family"].items()], "",
              "## All variants (sorted by in-sample Sharpe)", "",
              "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
              *["| " + " | ".join(str(r[c]) for c in cols) + " |"
                for _, r in res.sort_values("is_sharpe", ascending=False).iterrows()]]
    (ROOT / "reports" / "flow_research.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
