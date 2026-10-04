"""Optional push notifications when the signal changes.

Configured through environment variables (GitHub repo secrets in Actions);
with none set this module does nothing:
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    DISCORD_WEBHOOK_URL

    python -m btc_signal.notify "test message"     # check the setup
"""
from __future__ import annotations

import os
import sys

import requests


def channels() -> list[str]:
    out = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        out.append("discord")
    return out


def send(text: str) -> list[str]:
    """Send `text` to every configured channel; returns the channels that accepted it.
    Never raises: a failed notification must not break the dashboard update."""
    ok = []
    for ch in channels():
        try:
            if ch == "telegram":
                r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                                        "disable_web_page_preview": True}, timeout=15)
            else:
                r = requests.post(os.environ["DISCORD_WEBHOOK_URL"], json={"content": text[:2000]}, timeout=15)
            if r.status_code < 300:
                ok.append(ch)
            else:
                print(f"notify {ch}: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"notify {ch}: {e}", file=sys.stderr)
    return ok


if __name__ == "__main__":
    if not channels():
        sys.exit("no channel configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL")
    sent = send(" ".join(sys.argv[1:]) or "BTC 訊號台：通知測試")
    print("sent via", sent or "nothing")
    sys.exit(0 if sent else 1)
