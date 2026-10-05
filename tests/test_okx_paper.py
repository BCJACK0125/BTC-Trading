"""Decision logic of the OKX demo trader (no network)."""
import base64
import hashlib
import hmac
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import okx_paper as P  # noqa: E402
from btc_signal.okx import Okx, sign  # noqa: E402

SPEC = {"ctVal": 0.01, "lotSz": 0.01, "minSz": 0.01, "tickSz": 0.1}
CLOSE = 1_790_000_000


def snap(action="WAIT", position=None):
    return {"signal": {"action": action}, "position": position, "last_bar_close": CLOSE,
            "chart": {"bar_seconds": 14400}, "strategy": {"tp1_r": 1.5},
            "plan": {"entry": 100_000.0, "stop": 98_000.0, "tp1": 103_000.0}}


def ex(pos=None, sl=None, last=100_000.0, tp1_pending=False, tp1_filled=False, equity=10_000.0):
    return {"equity": equity, "last": last, "spec": SPEC, "pos": pos, "sl": sl, "tp1_pending": tp1_pending,
            "tp1_filled": tp1_filled, "stray_algos": [], "stray_orders": []}


def test_enter_sizes_by_risk_and_caps_leverage():
    acts = P.decide(snap("ENTER_LONG"), ex(), CLOSE + 600, risk=0.01, max_lev=3)
    enter = [a for a in acts if a["type"] == "enter"][0]
    # 1% of 10,000 = 100 USDT risk over a 2,000 stop -> 0.05 BTC = 5 contracts of 0.01
    assert enter["contracts"] == pytest.approx(5.0) and enter["R"] == 2000 and enter["key"] == CLOSE + 14400
    big = P.decide(snap("ENTER_LONG"), ex(), CLOSE + 600, risk=0.10, max_lev=3)
    capped = [a for a in big if a["type"] == "enter"][0]
    assert capped["contracts"] * 0.01 * 100_000 <= 10_000 * 3 + 1e-6      # never above max leverage


def test_no_chasing():
    stale = P.decide(snap("ENTER_LONG"), ex(), CLOSE + 3 * 3600, 0.01, 3)
    assert [a["type"] for a in stale] == ["note"]
    sys_pos = {"entry": 100_000.0, "stop": 98_000.0, "tp1": 103_000.0, "tp1_hit": False, "entry_time": CLOSE}
    missed = P.decide(snap("IN_POSITION", sys_pos), ex(), CLOSE + 600, 0.01, 3)
    assert [a["type"] for a in missed] == ["note"]


def test_manage_position_stops_only_move_up():
    sys_pos = {"entry": 100_000.0, "stop": 98_000.0, "tp1": 103_000.0, "tp1_hit": False, "entry_time": CLOSE}
    pos = {"contracts": 5.0, "avg_px": 100_100.0}
    # no stop and no TP1 yet -> both placed from the actual fill price
    acts = P.decide(snap("IN_POSITION", sys_pos), ex(pos=pos), CLOSE + 600, 0.01, 3)
    assert {a["type"] for a in acts} == {"place_sl", "place_tp1"}
    tp1 = [a for a in acts if a["type"] == "place_tp1"][0]
    assert tp1["px"] == pytest.approx(103_100.0) and tp1["contracts"] == pytest.approx(2.5)
    # TP1 filled and the system trails to 101,500 -> raise the stop, never lower it
    trail = dict(sys_pos, stop=101_500.0, tp1_hit=True)
    acts = P.decide(snap("IN_POSITION", trail), ex(pos=pos, sl={"algo_id": "1", "trigger": 100_100.0}, last=104_000.0,
                                                    tp1_filled=True), CLOSE + 600, 0.01, 3)
    assert acts == [{"type": "amend_sl", "algo_id": "1", "trigger": 101_500.0, "from": 100_100.0, "key": CLOSE}]
    lower = P.decide(snap("IN_POSITION", trail), ex(pos=pos, sl={"algo_id": "1", "trigger": 102_000.0}, last=104_000.0,
                                                    tp1_filled=True), CLOSE + 600, 0.01, 3)
    assert lower == []


def test_close_when_system_exits_or_price_below_stop():
    pos = {"contracts": 5.0, "avg_px": 100_000.0}
    assert P.decide(snap("COOLDOWN"), ex(pos=pos), CLOSE + 600, 0.01, 3)[0]["type"] == "close"
    sys_pos = {"entry": 100_000.0, "stop": 98_000.0, "tp1": 103_000.0, "tp1_hit": False, "entry_time": CLOSE}
    gap = P.decide(snap("IN_POSITION", sys_pos), ex(pos=pos, last=97_000.0), CLOSE + 600, 0.01, 3)
    assert gap[0]["type"] == "close"


def test_signature_and_demo_header():
    expected = base64.b64encode(hmac.new(b"s", b"2020-01-01T00:00:00.000ZGET/api/v5/account/balance", hashlib.sha256).digest()).decode()
    assert sign("s", "2020-01-01T00:00:00.000Z", "get", "/api/v5/account/balance") == expected
    h = Okx("k", "s", "p", demo=True).headers("GET", "/api/v5/account/balance")
    assert h["x-simulated-trading"] == "1" and h["OK-ACCESS-KEY"] == "k" and h["OK-ACCESS-TIMESTAMP"].endswith("Z")


def test_journal_merge_dedupes():
    a = {"log": [{"time": "t1", "action": "enter", "detail": "x"}], "equity_curve": [[1, 100.0]]}
    b = {"log": [{"time": "t1", "action": "enter", "detail": "x"}, {"time": "t2", "action": "close", "detail": "y"}],
         "equity_curve": [[1, 100.0], [2, 101.0]]}
    m = P.merge_journal(a, b)
    assert len(m["log"]) == 2 and m["equity_curve"] == [[1, 100.0], [2, 101.0]]
