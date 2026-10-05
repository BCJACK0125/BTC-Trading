"""Tests for the cooldown state, the forward signal log, overfitting statistics and SMC events."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from btc_signal import backtest, history, signals, stats  # noqa: E402
from btc_signal.signals import Config  # noqa: E402
from btc_signal.smc import market_structure  # noqa: E402
from test_signal import synthetic  # noqa: E402

import update  # noqa: E402


@pytest.fixture(scope="module")
def frame():
    h4 = synthetic(3000, "4h", seed=11)
    d1 = h4.resample("1D", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    cfg = Config(swing_len=5, threshold=20)
    f, _ = signals.compute(h4, d1, cfg)
    return f, cfg


def test_cooldown_after_exit_on_last_bar(frame):
    f, cfg = frame
    trades = backtest.run(f, cfg)["trades"]
    assert trades
    exit_t = pd.Timestamp(trades[len(trades) // 2]["exit_time"], unit="s", tz="UTC")
    res = backtest.run(f.loc[:exit_t], cfg)
    assert res["open"] is None
    assert res["cooldown_left"] == cfg.cooldown

    row = f.loc[exit_t].copy()
    row["signal"], row["htf_trend"] = 1, 1.0
    assert update.decide(row, cfg, None, res["cooldown_left"])["action"] == "COOLDOWN"
    assert update.decide(row, cfg, None, 0)["action"] == "ENTER_LONG"


def test_history_merge_and_change_detection():
    a = [{"bar": 1, "action": "WAIT", "generated": "t1"}, {"bar": 2, "action": "WAIT", "generated": "t2"}]
    b = [{"bar": 2, "action": "WATCH", "generated": "t3"}, {"bar": 3, "action": "WATCH", "generated": "t4"}]
    m = history.merge(a, b)
    assert [e["bar"] for e in m] == [1, 2, 3]
    assert m[1]["action"] == "WATCH"  # newer publication of the same bar wins

    h, prev, changed = history.record(m, {"bar": 4, "action": "ENTER_LONG", "generated": "t5"})
    assert changed and prev["bar"] == 3 and h[-1]["bar"] == 4
    # re-running the same bar must not notify again
    _, _, again = history.record(h, {"bar": 4, "action": "ENTER_LONG", "generated": "t6"})
    assert not again
    # first ever entry: nothing to compare against
    assert history.record([], {"bar": 1, "action": "WAIT"})[2] is False


def test_psr_and_dsr():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, 2000)
    noise -= noise.mean()
    assert stats.psr(noise) == pytest.approx(0.5, abs=1e-6)
    good = rng.normal(0.002, 0.01, 2000)
    assert stats.psr(good) > 0.99
    # more trials -> higher bar -> lower deflated probability
    assert stats.expected_max_sharpe(1000, 1e-3) > stats.expected_max_sharpe(10, 1e-3) > 0
    d10, _ = stats.dsr(good, 10, 1e-3)
    d1000, _ = stats.dsr(good, 1000, 1e-3)
    assert d1000 < d10 <= stats.psr(good)


def test_bootstrap_trades():
    r = np.array([1.0, -1.0, 1.5, -1.0, 2.0, -0.5, 1.0, -1.0] * 10)
    mc = stats.bootstrap_trades(r, years=4, n_sims=500)
    assert mc["max_dd_p5"] <= mc["max_dd_p50"] <= mc["max_dd_p95"] <= 0
    assert mc["cagr_p5"] <= mc["cagr_p50"] <= mc["cagr_p95"]
    assert 0 <= mc["prob_loss_pct"] <= 100


def test_smc_bos_and_choch():
    # swing high at bar 10 (confirmed at 13), swing low at bar 20, then a break below it
    # (CHoCH from bullish) and a later break above the newer swing high (CHoCH back)
    up = list(np.linspace(100, 120, 11))
    down = list(np.linspace(119, 105, 10))
    bounce = list(np.linspace(106, 112, 6))
    crash = list(np.linspace(110, 95, 8))
    rally = list(np.linspace(96, 125, 12))
    close = np.array(up + down + bounce + crash + rally)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="4h", tz="UTC")
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}, index=idx)
    res = market_structure(df, length=3)
    kinds = [(e["type"], e["dir"]) for e in res.events]
    assert ("BOS", 1) in kinds or ("CHoCH", 1) in kinds
    assert any(d == -1 for _, d in kinds)
    # each break must happen after the pivot it breaks was confirmed (no look-ahead)
    for e in res.events:
        assert e["i"] >= 2 * 3
    assert res.frame["struct"].iloc[-1] == 1


def test_funding_charged_only_while_holding(frame):
    f, cfg = frame
    flat = pd.Series(0.001, index=pd.date_range(f.index[0].floor("8h"), f.index[-1], freq="8h"))
    per_bar = backtest.funding_per_bar(f.index, flat)
    assert per_bar.sum() == pytest.approx(0.001 * len(flat), rel=0.01)  # 4h bars: one charge every other bar
    base = backtest.run(f, cfg, risk=0.03, max_lev=10)
    paid = backtest.run(f, cfg, risk=0.03, max_lev=10, funding=flat)
    assert paid["equity"].iloc[-1] < base["equity"].iloc[-1]
    assert [t["entry_time"] for t in paid["trades"]] == [t["entry_time"] for t in base["trades"]]
    assert all(t["lev"] <= 10 for t in paid["trades"])


def test_leveraged_hold_and_dca():
    from btc_signal import compare
    idx = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
    close = pd.Series(np.r_[np.linspace(100, 200, 100), np.linspace(200, 120, 100)], index=idx)
    daily = pd.DataFrame({"close": close, "low": close * 0.99})
    one = compare.leveraged_hold(daily, 1.0, None)
    assert (1 + one).prod() == pytest.approx(close.iloc[-1] / close.iloc[0])
    crash = daily.copy()
    crash.iloc[150, crash.columns.get_loc("low")] = crash["close"].iloc[149] * 0.6  # -40% wick
    three = compare.leveraged_hold(crash, 3.0, None)
    assert (three <= -1).any() and (1 + three).prod() == 0  # liquidated and stays dead
    d = compare.dca(daily)
    assert d["months"] == 7 and d["worst_vs_invested_pct"] <= d["return_on_invested_pct"]


@pytest.fixture(scope="module")
def hourly():
    h1 = synthetic(12000, "1h", seed=5)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    h4 = h1.resample("4h", label="right", closed="right").agg(agg).dropna()
    d1 = h1.resample("1D", label="right", closed="right").agg(agg).dropna()
    cfg = Config(swing_len=5, threshold=20, exit_mode="trail", sl_mode="atr")
    f4, _ = signals.compute(h4, d1, cfg)
    h1 = h1.loc[f4.index[0]:]
    return f4, h1, cfg


def test_ltf_entries_respect_rules(hourly):
    from btc_signal import ltf
    from btc_signal.ltf import EntryRule
    f4, h1, cfg = hourly
    feats = ltf.ltf_features(h1, 3)
    sig_times = set(f4.index[f4["signal"] == 1])
    market = ltf.run(f4, h1, feats, cfg, EntryRule("market"))
    assert market["trades"] and market["metrics"]["fill_rate_pct"] == 100.0
    for t in market["trades"]:
        signal_bar = pd.Timestamp(t["entry_time"] - 3600, unit="s", tz="UTC")
        assert signal_bar in sig_times          # entered on the 1h bar right after a 4h signal close
    rule = EntryRule("limit", pullback_atr=0.5, window_h=8)
    for t in ltf.run(f4, h1, feats, cfg, rule)["trades"]:
        assert 1 <= t["wait_h"] <= rule.window_h
        assert t["stop_atr"] < 2.0 + 1e-9     # stop stays at the 4h level, so it is closer to a lower fill
    rule = EntryRule("structure", window_h=12, ltf_stop=True)
    for t in ltf.run(f4, h1, feats, cfg, rule)["trades"]:
        assert 2 <= t["wait_h"] <= rule.window_h + 1   # trigger confirmed on a later close, entered next open
        assert rule.min_stop_atr - 1e-9 <= t["stop_atr"] <= rule.max_stop_atr + 1e-9


def test_flow_features_no_lookahead():
    from btc_signal import flows
    hours = pd.date_range("2024-01-01 01:00", periods=24 * 20, freq="1h", tz="UTC")
    bn = pd.DataFrame({"close": np.full(len(hours), 100.0)}, index=hours)
    cb = pd.Series(100.05, index=hours)                     # steady +0.05% premium
    bars = pd.date_range("2024-01-02 04:00", periods=100, freq="4h", tz="UTC")
    cut = bars[50]
    spiked = cb.copy()
    spiked[spiked.index > cut] = 101.0                     # +1% after the cut
    a = flows.premium_features(cb, bn, bars)
    b = flows.premium_features(spiked, bn, bars)
    pd.testing.assert_frame_equal(a.loc[:cut], b.loc[:cut])  # bars up to the cut never see later hours
    assert a["cb_prem_24h"].loc[cut] == pytest.approx(0.05)
    assert b["cb_prem_24h"].iloc[-1] > 0.5

    fund = pd.Series(0.0001, index=pd.date_range("2024-01-01", periods=60, freq="8h", tz="UTC"))
    fund.iloc[40:] = 0.001
    ff = flows.funding_features(fund, bars)
    first_hot = fund.index[40]
    before = ff.loc[:first_hot - pd.Timedelta(minutes=1), "fund_3"].dropna()
    assert len(before) and np.allclose(before, 0.01)        # % per 8h, no hot payment leaks in early

    f = pd.DataFrame({"signal": [1, 1, 1, 0]})
    g = flows.with_filter(f, pd.Series([True, False, np.nan, True]))
    assert g["signal"].tolist() == [1, 0, 1, 0]             # missing data never blocks a signal


def test_email_channel_and_message(monkeypatch):
    from btc_signal import notify
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert notify.channels() == []
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_TO", "a@example.com, b@example.com")
    assert notify.channels() == ["email"]
    msg = notify.build_email("【BTC 訊號台】進場做多", "1. 市價買入")
    assert msg["To"] == "a@example.com, b@example.com" and "bot@example.com" in msg["From"]
    assert "市價買入" in msg.get_content()

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, **kw):
            sent.append((host, port))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, pw):
            assert (user, pw) == ("bot@example.com", "app-password")
        def send_message(self, m):
            sent.append(m["Subject"])

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    assert notify.send("body", "subject") == ["email"]
    assert sent == [("smtp.gmail.com", 465), "subject"]


def test_tp1_notification_once():
    h = [{"bar": 1, "action": "IN_POSITION", "tp1_hit": False, "generated": "a"}]
    hit = {"bar": 2, "action": "IN_POSITION", "tp1_hit": True, "generated": "b"}
    assert history.tp1_reached(h, hit)
    h2, _, _ = history.record(h, hit)
    assert not history.tp1_reached(h2, dict(hit, generated="c"))           # same bar re-run
    assert not history.tp1_reached(h2, {"bar": 3, "action": "IN_POSITION", "tp1_hit": True})  # already taken


def test_order_steps_follow_the_state():
    cfg = Config()
    base = {"last_bar_close": 1_790_000_000, "chart": {"bar_seconds": 14400},
            "plan": {"entry": 100_000.0, "stop": 97_000.0, "tp1": 104_500.0}, "position": None}
    enter = update.order_steps({**base, "signal": {"action": "ENTER_LONG"}}, cfg, [])
    text = " ".join(enter)
    assert "97,000" in text and "104,500" in text and "3,000" in text   # stop, TP1 and stop distance
    pos = {"entry": 100_000.0, "stop": 101_000.0, "tp1": 104_500.0, "tp1_hit": True, "entry_time": 1_789_000_000}
    held = update.order_steps({**base, "position": pos, "signal": {"action": "IN_POSITION"}}, cfg, [])
    assert "101,000" in held[0] and "移動停損" in held[0]
    idle = update.order_steps({**base, "signal": {"action": "WAIT"}}, cfg, [])
    assert idle == ["目前不需要下單。訊號成立時會發出通知。"]
    exited = update.order_steps({**base, "signal": {"action": "COOLDOWN"}}, cfg,
                                [{"exit_time": base["last_bar_close"], "exit": 99_000.0, "r": -0.33, "reason": "time"}])
    assert "已出場" in exited[0] and len(exited) == 1


def test_oi_features_no_lookahead_and_stale():
    from btc_signal import positioning
    bars = pd.date_range("2024-01-01 04:00", periods=60, freq="4h", tz="UTC")
    f = pd.DataFrame({"close": np.linspace(100, 110, 60), "atr": 1.0}, index=bars)
    snaps = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), bars[-1], freq="5min")
    oi = pd.Series(1000.0, index=snaps)
    cut = bars[30]
    later = oi.copy()
    later[later.index > cut] = 500.0                      # OI collapses after the cut
    a, b = positioning.oi_features(oi, f), positioning.oi_features(later, f)
    pd.testing.assert_frame_equal(a.loc[:cut], b.loc[:cut])
    assert b["oi_chg_1"].iloc[31] == pytest.approx(-50.0)
    gap = oi.drop(oi.index[(oi.index > bars[40] - pd.Timedelta("3h")) & (oi.index <= bars[40])])
    assert np.isnan(positioning.oi_features(gap, f)["oi_chg_1"].iloc[40])   # no fresh snapshot -> no value
