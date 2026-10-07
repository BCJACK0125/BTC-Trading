"""Forward record of every published signal (one entry per closed bar).

The backtest shows how the rules *would* have done; this log shows what the
dashboard actually said at the time. In GitHub Actions the runner starts from a
fresh checkout, so the previous log is pulled back from the deployed site (and
the kline cache) and merged with the copy in the repo.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

MAX_ENTRIES = 3000  # ~500 days of 4h bars


def read_file(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def read_url(url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=15, headers={"Cache-Control": "no-cache"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError):
        pass
    return []


def merge(*sources: list[dict]) -> list[dict]:
    """Union by bar time; for the same bar the most recently generated entry wins."""
    by_bar: dict[int, dict] = {}
    for src in sources:
        for e in src:
            if not isinstance(e, dict) or "bar" not in e:
                continue
            old = by_bar.get(e["bar"])
            if old is None or str(e.get("generated", "")) >= str(old.get("generated", "")):
                by_bar[e["bar"]] = e
    return [by_bar[k] for k in sorted(by_bar)][-MAX_ENTRIES:]


def record(history: list[dict], entry: dict) -> tuple[list[dict], dict | None, bool]:
    """Add `entry` and return (new history, previous bar's entry, is_new_state).

    `is_new_state` is True only when the action differs from the previous bar's
    and this bar had not already been published with the same action, so a
    re-run on the same bar does not notify twice.
    """
    prev = next((e for e in reversed(history) if e["bar"] < entry["bar"]), None)
    same_bar = next((e for e in history if e["bar"] == entry["bar"]), None)
    changed = prev is not None and prev.get("action") != entry["action"]
    already_sent = same_bar is not None and same_bar.get("action") == entry["action"]
    return merge(history, [entry]), prev, changed and not already_sent


def tp1_reached(history: list[dict], entry: dict) -> bool:
    """True on the first bar where the open position has taken TP1, unless this bar
    was already published with TP1 taken (so a re-run does not notify twice)."""
    if entry.get("action") != "IN_POSITION" or not entry.get("tp1_hit"):
        return False
    prev = next((e for e in reversed(history) if e["bar"] < entry["bar"]), None)
    same_bar = next((e for e in history if e["bar"] == entry["bar"]), None)
    was_hit = bool(prev and prev.get("action") == "IN_POSITION" and prev.get("tp1_hit"))
    sent = bool(same_bar and same_bar.get("tp1_hit"))
    return prev is not None and not was_hit and not sent


def attach_exits(history: list[dict], trades: list[dict], bar_seconds: int) -> list[dict]:
    """Record trades that closed on a bar nobody published (a missed or failed run).

    The exit goes on the first published bar at or after it, with `exit_bar` set when
    that is a later bar. Only trades the log actually showed (ENTER_LONG or
    IN_POSITION while open) are attached, and a bar keeps any exit it already has.
    """
    if not history:
        return history
    held = {"ENTER_LONG", "IN_POSITION"}
    done = {(c["entry"], c["exit"]) for c in (e.get("closed") for e in history) if c}
    for t in trades:
        if t["exit_time"] < history[0]["bar"] or (t["entry"], t["exit"]) in done:
            continue
        if not any(e.get("action") in held and t["entry_time"] - bar_seconds <= e["bar"] < t["exit_time"]
                   for e in history):
            continue
        target = next((e for e in history if e["bar"] >= t["exit_time"]), None)
        if target is None or target.get("closed"):
            continue
        target["closed"] = {k: t[k] for k in ("entry", "exit", "r", "reason")}
        if target["bar"] != t["exit_time"]:
            target["closed"]["exit_bar"] = t["exit_time"]
        done.add((t["entry"], t["exit"]))
    return history
