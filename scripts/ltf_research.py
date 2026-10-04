"""Does refining the 4h entry on 1h bars help?

    python scripts/ltf_research.py

Runs the production 4h signal through the 1h execution engine
(btc_signal/ltf.py) with every entry variant, selects on 2019-2023 only and
reports 2024 onward as out-of-sample. Writes reports/ltf_research.md and
docs/data/ltf.json (+ .js for the dashboard).
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import backtest, data, ltf, signals  # noqa: E402
from btc_signal.ltf import EntryRule  # noqa: E402
from btc_signal.signals import Config  # noqa: E402

CACHE = ROOT / "data" / "cache"
IS_END, OOS_START = "2023-12-31", "2024-01-01"

RULES = [EntryRule("market")]
RULES += [EntryRule("limit", window_h=w, pullback_atr=k) for k in (0.25, 0.5, 1.0) for w in (4, 8, 12)]
RULES += [EntryRule("limit", window_h=w, zone=True) for w in (4, 8, 12)]
RULES += [EntryRule("structure", window_h=w, ltf_len=L, ltf_stop=s, choch_only=ch)
          for L in (3, 5) for w in (8, 12, 24) for s in (True, False) for ch in (False, True)]

_CTX: dict = {}


def _init(ctx):
    _CTX.update(ctx)


def _evaluate(rule: EntryRule) -> dict:
    c = _CTX
    feats = c["feats"][rule.ltf_len]
    out = {"label": rule.label(), **rule.to_dict()}
    for tag, s, e in (("is", None, IS_END), ("oos", OOS_START, None), ("full", None, None)):
        m = ltf.run(c["f4"], c["h1"], feats, c["cfg"], rule, start=s, end=e, funding=c["funding"])["metrics"]
        out.update({f"{tag}_{k}": v for k, v in m.items()})
    return out


def main():
    spec = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    cfg = Config(**spec["config"])
    d = {iv: data.load_klines(iv, "2018-06-01" if iv == "1d" else "2019-01-01", cache_dir=CACHE)
         for iv in ("1d", "4h", "1h")}
    fng = data.load_fear_greed()
    funding = data.load_funding_history(cache_dir=CACHE)
    f4, _ = signals.compute(d["4h"], d["1d"], cfg, fng)
    h1 = d["1h"].loc[f4.index[0]:]
    feats = {L: ltf.ltf_features(h1, L) for L in sorted({r.ltf_len for r in RULES})}

    ref = {tag: backtest.run(f4, cfg, start=s, end=e, funding=funding, max_lev=10)["metrics"]
           for tag, s, e in (("is", None, IS_END), ("oos", OOS_START, None))}
    with ProcessPoolExecutor(initializer=_init,
                             initargs=({"f4": f4, "h1": h1, "feats": feats, "cfg": cfg, "funding": funding},)) as ex:
        res = pd.DataFrame(list(ex.map(_evaluate, RULES)))
    res.to_csv(ROOT / "reports" / "ltf_grid.csv", index=False)
    market_trades = ltf.run(f4, h1, feats[3], cfg, EntryRule("market"), funding=funding)["trades"]
    adverse = [adverse_selection(f4, h1, market_trades, k) for k in (0.25, 0.5, 1.0)]

    cols = ["label", "is_sharpe", "is_cagr_pct", "is_max_dd_pct", "is_trades", "is_win_rate_pct", "is_avg_r",
            "is_fill_rate_pct", "is_avg_stop_atr", "is_avg_lev", "oos_sharpe", "oos_cagr_pct", "oos_max_dd_pct",
            "oos_trades", "oos_win_rate_pct", "oos_avg_r", "oos_fill_rate_pct"]
    base = res[res["mode"] == "market"].iloc[0]
    best = {m: g.sort_values("is_sharpe", ascending=False).iloc[0] for m, g in res.groupby("mode")}
    fam = res.groupby("mode")[["is_sharpe", "oos_sharpe", "is_win_rate_pct", "is_avg_r", "is_trades"]].median().round(2)
    summary = {
        "generated": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
        "is_period": f"2019-01-01 ~ {IS_END}", "oos_period": f"{OOS_START} ~ {h1.index[-1].date()}",
        "engine_4h": ref,
        "variants": json.loads(res[cols + ["mode"]].to_json(orient="records")),
        "best_by_mode": {m: json.loads(r[cols].to_json()) for m, r in best.items()},
        "median_by_mode": {m: r.to_dict() for m, r in fam.iterrows()},
        "share_beating_market_is": round(float((res["is_sharpe"] > base["is_sharpe"]).mean() * 100), 1),
        "share_beating_market_oos": round(float((res["oos_sharpe"] > base["oos_sharpe"]).mean() * 100), 1),
        "n_variants": int(len(res) - 1),
        "adverse_selection": adverse,
    }
    payload = json.dumps(summary, ensure_ascii=False)
    (ROOT / "docs" / "data" / "ltf.json").write_text(payload, encoding="utf-8")
    (ROOT / "docs" / "data" / "ltf.js").write_text(f"window.BTC_LTF={payload};", encoding="utf-8")
    write_report(summary, res, cols)
    pd.set_option("display.width", 250)
    print(res[cols].sort_values("is_sharpe", ascending=False).to_string(index=False))
    print("4h engine reference:", ref)


def adverse_selection(f4: pd.DataFrame, h1: pd.DataFrame, trades: list[dict], k: float, window_h: int = 8) -> dict:
    """Split the market-entry trades by whether a limit k x ATR below the signal close
    would have filled within `window_h` hours, and compare what each group earned."""
    t = pd.DataFrame(trades)
    hit = []
    for entry in t["entry_time"]:
        sig_t = pd.Timestamp(entry - 3600, unit="s", tz="UTC")   # market entries are one 1h bar after the signal
        row = f4.loc[sig_t]
        w = h1.loc[sig_t + pd.Timedelta(hours=1):sig_t + pd.Timedelta(hours=window_h)]
        hit.append(bool((w["low"] <= row["close"] - k * row["atr"]).any()))
    hit = np.array(hit)
    return {"pullback_atr": k, "window_h": window_h, "would_fill_pct": round(float(hit.mean() * 100), 1),
            "avg_r_filled": round(float(t["r"][hit].mean()), 2), "avg_r_missed": round(float(t["r"][~hit].mean()), 2),
            "share_r_missed_pct": round(float(t["r"][~hit].sum() / t["r"].sum() * 100), 1)}


def write_report(s: dict, res: pd.DataFrame, cols: list[str]):
    fmt = lambda v: "" if pd.isna(v) else (f"{v:.2f}" if isinstance(v, float) else str(v))  # noqa: E731
    top = res.sort_values("is_sharpe", ascending=False)
    lines = [
        "# Lower-timeframe entry study", "",
        f"Generated {s['generated']}. The production 4h signal is executed on 1h bars with different entry rules. "
        f"Selection on {s['is_period']} only; {s['oos_period']} is out-of-sample. 1% risk per trade, "
        "fees 0.05%/side, 0.02% slippage on market fills, historical perpetual funding charged.", "",
        "Reference, original 4h engine: "
        f"IS Sharpe {s['engine_4h']['is']['sharpe']}, OOS Sharpe {s['engine_4h']['oos']['sharpe']}.", "",
        "## Median Sharpe by entry family", "",
        "| family | IS | OOS |", "|---|---|---|",
        *[f"| {m} | {v['is_sharpe']} | {v['oos_sharpe']} |" for m, v in s["median_by_mode"].items()], "",
        f"Variants beating plain market entry on Sharpe: {s['share_beating_market_is']}% in-sample, "
        f"{s['share_beating_market_oos']}% out-of-sample.", "",
        "## Why: adverse selection", "",
        "Market-entry trades split by whether a limit below the signal close would have filled within 8h:", "",
        "| limit | would fill | avg R if filled | avg R if never pulled back | share of total R never pulled back |",
        "|---|---|---|---|---|",
        *[f"| -{a['pullback_atr']:g} ATR | {a['would_fill_pct']}% | {a['avg_r_filled']} | {a['avg_r_missed']} | "
          f"{a['share_r_missed_pct']}% |" for a in s["adverse_selection"]], "",
        "The trades that run without looking back carry most of the edge; a limit order only fills the weaker ones, "
        "and a tighter 1h stop is hit more often by noise.", "",
        "## All variants (sorted by in-sample Sharpe)", "",
        "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
        *["| " + " | ".join(fmt(r[c]) for c in cols) + " |" for _, r in top.iterrows()],
    ]
    (ROOT / "reports" / "ltf_research.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
