"""Mirror the strategy on an OKX demo account (BTC-USDT-SWAP, isolated margin).

    python scripts/okx_paper.py [--dry-run]

Runs right after scripts/update.py and reads the signal it wrote
(docs/data/latest.json), so it trades exactly what the dashboard shows:

- system signals an entry (and the signal is fresh) -> market buy sized so the
  stop loses RISK_PCT of equity, then a stop that closes the whole position and a
  reduce-only TP1 limit for half;
- TP1 filled -> stop to break-even, then raised with the system's trailing stop
  (stops only ever move up);
- system exits (stop, trail, time) -> close what is left.
It never chases: a stale entry signal, or a system position the demo account
does not hold, is logged and skipped. Every run is idempotent.

Environment: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE (a *demo trading* key);
optional RISK_PCT (default 1), MAX_LEV (default 3), OKX_BASE_URL, SITE_URL and the
notification variables of btc_signal/notify.py. Without the keys it does nothing.
Writes docs/data/paper.json (+ .js) for the dashboard.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_signal import notify  # noqa: E402
from btc_signal.okx import Okx, OkxError  # noqa: E402

INST = "BTC-USDT-SWAP"
TD_MODE = "isolated"
MAX_ENTRY_DELAY_S = 2 * 3600       # do not enter on a signal older than this
LATEST = ROOT / "docs" / "data" / "latest.json"
JOURNAL = ROOT / "docs" / "data" / "paper.json"
MAX_LOG = 500


# --- pure decision logic -----------------------------------------------------------

def floor_to(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def round_to(x: float, step: float) -> float:
    return round(x / step) * step


def decide(snap: dict, ex: dict, now: float, risk: float, max_lev: float) -> list[dict]:
    """Actions that bring the demo account in line with the system.

    snap: docs/data/latest.json. ex: {"equity", "last", "spec": {ctVal, lotSz, minSz, tickSz},
    "pos": {"contracts", "avg_px"} | None, "sl": {"algo_id", "trigger"} | None,
    "tp1_pending": bool, "tp1_filled": bool, "stray_algos": [ids], "stray_orders": [ids]}.
    """
    acts: list[dict] = []
    sys_pos, action = snap.get("position"), snap["signal"]["action"]
    bar, tp1_r = snap["chart"]["bar_seconds"], snap["strategy"]["tp1_r"]
    spec, pos = ex["spec"], ex["pos"]

    if pos is None:
        acts += [{"type": "cancel_algo", "algo_id": a} for a in ex["stray_algos"] + ([ex["sl"]["algo_id"]] if ex["sl"] else [])]
        acts += [{"type": "cancel_order", "ord_id": o} for o in ex["stray_orders"]]
        if action == "ENTER_LONG":
            age = now - snap["last_bar_close"]
            if age > MAX_ENTRY_DELAY_S:
                return acts + [{"type": "note", "text": f"進場訊號已過 {age / 3600:.1f} 小時，不追價"}]
            p = snap["plan"]
            R = p["entry"] - p["stop"]
            if R <= 0:
                return acts + [{"type": "note", "text": "止損距離異常，不進場"}]
            qty = min(ex["equity"] * risk / R, ex["equity"] * max_lev / ex["last"])
            contracts = floor_to(qty / spec["ctVal"], spec["lotSz"])
            if contracts < spec["minSz"]:
                return acts + [{"type": "note", "text": f"資金太小，算出的口數 {contracts} 低於最小下單量"}]
            acts.append({"type": "enter", "contracts": contracts, "R": R, "key": snap["last_bar_close"] + bar,
                         "tp1_r": tp1_r})
        elif sys_pos:
            acts.append({"type": "note", "text": "系統持倉中但模擬帳戶沒有部位（止損較早觸發或錯過進場），不追價"})
        return acts

    # --- holding a position --------------------------------------------------------
    if sys_pos is None:
        return [{"type": "close", "reason": "系統已出場"}]
    R = (sys_pos["tp1"] - sys_pos["entry"]) / tp1_r
    entry = pos["avg_px"]
    if ex["tp1_filled"]:
        target = max(entry, sys_pos["stop"] if sys_pos.get("tp1_hit") else entry)
    else:
        target = entry - R
    target = round_to(target, spec["tickSz"])
    if ex["last"] <= target:
        return [{"type": "close", "reason": f"價格 {ex['last']:,.1f} 已低於應有止損 {target:,.1f}"}]
    if ex["sl"] is None:
        acts.append({"type": "place_sl", "trigger": target, "key": sys_pos["entry_time"]})
    elif target > ex["sl"]["trigger"] + spec["tickSz"] / 2:
        acts.append({"type": "amend_sl", "algo_id": ex["sl"]["algo_id"], "trigger": target, "from": ex["sl"]["trigger"],
                     "key": sys_pos["entry_time"]})
    if not ex["tp1_filled"] and not ex["tp1_pending"]:
        half = floor_to(pos["contracts"] / 2, spec["lotSz"])
        if half >= spec["minSz"]:
            acts.append({"type": "place_tp1", "px": round_to(entry + tp1_r * R, spec["tickSz"]), "contracts": half,
                         "key": sys_pos["entry_time"]})
    return acts


# --- exchange I/O ---------------------------------------------------------------------

def fmt_num(x: float, step: float) -> str:
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{x:.{dec}f}"


def instrument(okx: Okx) -> dict:
    d = okx.get("/api/v5/public/instruments", instType="SWAP", instId=INST)[0]
    return {k: float(d[k]) for k in ("ctVal", "lotSz", "minSz", "tickSz")}


def snapshot(okx: Okx, key: int | None) -> dict:
    spec = instrument(okx)
    bal = okx.get("/api/v5/account/balance", ccy="USDT")[0]
    usdt = next((d for d in bal.get("details", []) if d.get("ccy") == "USDT"), {})
    equity = float(usdt.get("eq") or bal.get("totalEq") or 0)
    last = float(okx.get("/api/v5/market/ticker", instId=INST)[0]["last"])
    pos = None
    for p in okx.get("/api/v5/account/positions", instId=INST):
        if float(p.get("pos") or 0) > 0:
            pos = {"contracts": float(p["pos"]), "avg_px": float(p["avgPx"])}
    algos = okx.get("/api/v5/trade/orders-algo-pending", ordType="conditional", instId=INST)
    ours = [a for a in algos if (a.get("algoClOrdId") or "").startswith("bsS") and a.get("slTriggerPx")]
    sl = {"algo_id": ours[0]["algoId"], "trigger": float(ours[0]["slTriggerPx"])} if ours else None
    orders = okx.get("/api/v5/trade/orders-pending", instType="SWAP", instId=INST)
    tp1_id = f"bsT{key}" if key else None
    tp1_pending = any(o.get("clOrdId") == tp1_id for o in orders)
    tp1_filled = False
    if tp1_id and not tp1_pending:
        try:
            tp1_filled = okx.get("/api/v5/trade/order", instId=INST, clOrdId=tp1_id)[0].get("state") == "filled"
        except OkxError:
            tp1_filled = False   # never placed (or too old to look up)
    return {"equity": equity, "last": last, "spec": spec, "pos": pos, "sl": sl, "tp1_pending": tp1_pending,
            "tp1_filled": tp1_filled,
            "stray_algos": [a["algoId"] for a in ours[1:]],
            "stray_orders": [o["ordId"] for o in orders if (o.get("clOrdId") or "").startswith("bs") and o.get("clOrdId") != tp1_id]}


def ensure_account(okx: Okx, max_lev: float, flat: bool):
    cfg = okx.get("/api/v5/account/config")[0]
    if cfg.get("acctLv") == "1":
        raise OkxError("account", "模擬帳戶是「現貨模式」，不能交易永續合約。請在 OKX 模擬交易 → 設定 → 帳戶模式 改成「單幣種保證金」後再執行。")
    if cfg.get("posMode") != "net_mode" and flat:
        okx.post("/api/v5/account/set-position-mode", {"posMode": "net_mode"})
    okx.post("/api/v5/account/set-leverage", {"instId": INST, "lever": f"{max_lev:g}", "mgnMode": TD_MODE})


def place_sl(okx: Okx, trigger: float, key: int, spec: dict, n: int = 0) -> str:
    return okx.post("/api/v5/trade/order-algo", {
        "instId": INST, "tdMode": TD_MODE, "side": "sell", "ordType": "conditional",
        "slTriggerPx": fmt_num(trigger, spec["tickSz"]), "slOrdPx": "-1", "slTriggerPxType": "last",
        "closeFraction": "1", "reduceOnly": True, "algoClOrdId": f"bsS{key}x{n}{int(time.time()) % 100000}"})[0]["algoId"]


def execute(okx: Okx, acts: list[dict], ex: dict, max_lev: float, dry: bool) -> list[dict]:
    """Run the actions; returns journal rows (with what actually happened)."""
    done, spec = [], ex["spec"]
    for a in acts:
        t = a["type"]
        row = {"action": t}
        if t == "note":
            row["detail"] = a["text"]
        elif dry:
            row["detail"] = f"[dry-run] {json.dumps({k: v for k, v in a.items() if k != 'type'}, ensure_ascii=False)}"
        elif t == "enter":
            ensure_account(okx, max_lev, flat=True)
            okx.post("/api/v5/trade/order", {"instId": INST, "tdMode": TD_MODE, "side": "buy", "ordType": "market",
                                             "sz": fmt_num(a["contracts"], spec["lotSz"]), "clOrdId": f"bsE{a['key']}"})
            pos = None
            for _ in range(10):
                time.sleep(1)
                pos = next((p for p in okx.get("/api/v5/account/positions", instId=INST) if float(p.get("pos") or 0) > 0), None)
                if pos:
                    break
            if not pos:
                raise OkxError("fill", "市價單送出後找不到部位")
            fill, size = float(pos["avgPx"]), float(pos["pos"])
            sl = round_to(fill - a["R"], spec["tickSz"])
            place_sl(okx, sl, a["key"], spec)
            half = floor_to(size / 2, spec["lotSz"])
            tp1 = round_to(fill + a["tp1_r"] * a["R"], spec["tickSz"])
            if half >= spec["minSz"]:
                okx.post("/api/v5/trade/order", {"instId": INST, "tdMode": TD_MODE, "side": "sell", "ordType": "limit",
                                                 "px": fmt_num(tp1, spec["tickSz"]), "sz": fmt_num(half, spec["lotSz"]),
                                                 "reduceOnly": True, "clOrdId": f"bsT{a['key']}"})
            row["detail"] = (f"市價買入 {size:g} 口（{size * spec['ctVal']:.4f} BTC），成交均價 {fill:,.1f}；"
                             f"止損 {sl:,.1f}（全部），TP1 {tp1:,.1f}（{half:g} 口）")
        elif t == "place_sl":
            place_sl(okx, a["trigger"], a["key"], spec)
            row["detail"] = f"補掛止損 {a['trigger']:,.1f}"
        elif t == "amend_sl":
            try:
                okx.post("/api/v5/trade/amend-algos", {"instId": INST, "algoId": a["algo_id"],
                                                       "newSlTriggerPx": fmt_num(a["trigger"], spec["tickSz"])})
            except OkxError:
                okx.post("/api/v5/trade/cancel-algos", [{"algoId": a["algo_id"], "instId": INST}])
                place_sl(okx, a["trigger"], a["key"], spec, n=1)
            row["detail"] = f"止損上移 {a['from']:,.1f} → {a['trigger']:,.1f}"
        elif t == "place_tp1":
            okx.post("/api/v5/trade/order", {"instId": INST, "tdMode": TD_MODE, "side": "sell", "ordType": "limit",
                                             "px": fmt_num(a["px"], spec["tickSz"]), "sz": fmt_num(a["contracts"], spec["lotSz"]),
                                             "reduceOnly": True, "clOrdId": f"bsT{a['key']}"})
            row["detail"] = f"補掛 TP1 {a['px']:,.1f}（{a['contracts']:g} 口）"
        elif t == "close":
            okx.post("/api/v5/trade/close-position", {"instId": INST, "mgnMode": TD_MODE, "autoCxl": True})
            for algo in okx.get("/api/v5/trade/orders-algo-pending", ordType="conditional", instId=INST):
                if (algo.get("algoClOrdId") or "").startswith("bsS"):
                    okx.post("/api/v5/trade/cancel-algos", [{"algoId": algo["algoId"], "instId": INST}])
            row["detail"] = f"市價平倉：{a['reason']}"
        elif t == "cancel_algo":
            okx.post("/api/v5/trade/cancel-algos", [{"algoId": a["algo_id"], "instId": INST}])
            row["detail"] = "取消殘留的止損單"
        elif t == "cancel_order":
            okx.post("/api/v5/trade/cancel-order", {"instId": INST, "ordId": a["ord_id"]})
            row["detail"] = "取消殘留的掛單"
        done.append(row)
    return done


# --- journal ---------------------------------------------------------------------------

def merge_journal(*sources: dict) -> dict:
    log, curve = {}, {}
    for s in sources:
        for r in s.get("log", []):
            log[(r.get("time"), r.get("action"), r.get("detail"))] = r
        for t, v in s.get("equity_curve", []):
            curve[t] = v
    return {"log": sorted(log.values(), key=lambda r: r["time"])[-MAX_LOG:],
            "equity_curve": [[t, curve[t]] for t in sorted(curve)]}


def load_journal(site_url: str) -> dict:
    local = {}
    try:
        local = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    remote = {}
    if site_url:
        try:
            import requests
            r = requests.get(site_url.rstrip("/") + "/data/paper.json", timeout=15)
            remote = r.json() if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001 - the journal is best effort
            remote = {}
    return merge_journal(remote, local)


def write_journal(j: dict):
    payload = json.dumps(j, ensure_ascii=False, separators=(",", ":"))
    JOURNAL.write_text(payload, encoding="utf-8")
    JOURNAL.with_suffix(".js").write_text(f"window.BTC_PAPER={payload};", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read the account and print the actions, send nothing")
    args = ap.parse_args()
    env = os.environ.get
    if not (env("OKX_API_KEY") and env("OKX_SECRET_KEY") and env("OKX_PASSPHRASE")):
        print("OKX demo keys not set (OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE): skipping paper trading")
        return 0
    risk, max_lev = float(env("RISK_PCT") or 1) / 100, float(env("MAX_LEV") or 3)
    okx = Okx(env("OKX_API_KEY"), env("OKX_SECRET_KEY"), env("OKX_PASSPHRASE"), demo=True,
              base_url=env("OKX_BASE_URL") or "https://www.okx.com")
    snap = json.loads(LATEST.read_text(encoding="utf-8"))
    site = env("SITE_URL", "")
    journal = load_journal(site)
    now = time.time()
    stamp = pd.Timestamp(now, unit="s", tz="UTC").isoformat(timespec="seconds")
    key = snap["position"]["entry_time"] if snap.get("position") else None
    rows, failed = [], None
    try:
        ex = snapshot(okx, key)
        acts = decide(snap, ex, now, risk, max_lev)
        rows = execute(okx, acts, ex, max_lev, args.dry_run)
        ex = snapshot(okx, key) if any(r["action"] != "note" for r in rows) and not args.dry_run else ex
    except OkxError as e:
        failed = str(e)
        rows.append({"action": "error", "detail": failed})
        ex = None
    for r in rows:
        r.update({"time": stamp, "bar": snap["last_bar_close"]})
    j = merge_journal(journal, {"log": rows})
    if ex is not None:
        j["equity_curve"] = merge_journal(j, {"equity_curve": [[int(now), round(ex["equity"], 2)]]})["equity_curve"]
        j.update({"equity": round(ex["equity"], 2), "last": ex["last"], "position": ex["pos"],
                  "stop": ex["sl"]["trigger"] if ex["sl"] else None, "tp1_pending": ex["tp1_pending"]})
    j.update({"inst": INST, "risk_pct": risk * 100, "max_lev": max_lev, "updated": stamp, "dry_run": args.dry_run})
    if not args.dry_run:
        write_journal(j)
    for r in rows:
        print(f"{r['action']}: {r['detail']}")
    print(f"equity {j.get('equity')} USDT, position {j.get('position')}, stop {j.get('stop')}")

    important = [r for r in rows if r["action"] in ("enter", "close", "error")]
    if important and not args.dry_run and notify.channels():
        title = {"enter": "模擬交易已進場", "close": "模擬交易已平倉", "error": "模擬交易出錯"}[important[0]["action"]]
        body = "\n".join([f"OKX 模擬交易（{INST}）", ""] + [f"- {r['detail']}" for r in rows] +
                         ["", f"模擬帳戶權益：{j.get('equity')} USDT", site])
        notify.send(body, f"【BTC 訊號台】{title}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
