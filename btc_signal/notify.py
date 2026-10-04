"""Optional push notifications when the signal changes.

Configured through environment variables (GitHub repo secrets in Actions);
with none set this module does nothing:
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    DISCORD_WEBHOOK_URL
    SMTP_USER + SMTP_PASSWORD + EMAIL_TO   (email; EMAIL_TO may list several, comma-separated)
        SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465 = SSL; 587 = STARTTLS),
        EMAIL_FROM (default SMTP_USER)

    python -m btc_signal.notify "test message"     # check the setup
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr

import requests


def channels() -> list[str]:
    env = os.environ.get
    out = []
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if env("DISCORD_WEBHOOK_URL"):
        out.append("discord")
    if env("SMTP_USER") and env("SMTP_PASSWORD") and env("EMAIL_TO"):
        out.append("email")
    return out


def build_email(subject: str, text: str) -> EmailMessage:
    env = os.environ.get
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("BTC 訊號台", env("EMAIL_FROM") or env("SMTP_USER")))
    msg["To"] = ", ".join(a.strip() for a in env("EMAIL_TO", "").split(",") if a.strip())
    msg.set_content(text)
    return msg


def _send_email(subject: str, text: str):
    env = os.environ.get
    host, port = env("SMTP_HOST") or "smtp.gmail.com", int(env("SMTP_PORT") or 465)
    msg = build_email(subject, text)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            s.send_message(msg)


def send(text: str, subject: str | None = None) -> list[str]:
    """Send `text` to every configured channel; returns the channels that accepted it.
    Never raises: a failed notification must not break the dashboard update."""
    subject = subject or text.splitlines()[0]
    ok = []
    for ch in channels():
        try:
            if ch == "telegram":
                r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                                        "disable_web_page_preview": True}, timeout=15)
            elif ch == "discord":
                r = requests.post(os.environ["DISCORD_WEBHOOK_URL"], json={"content": text[:2000]}, timeout=15)
            else:
                _send_email(subject, text)
                ok.append(ch)
                continue
            if r.status_code < 300:
                ok.append(ch)
            else:
                print(f"notify {ch}: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        except (requests.RequestException, smtplib.SMTPException, OSError) as e:
            print(f"notify {ch}: {e}", file=sys.stderr)
    return ok


if __name__ == "__main__":
    if not channels():
        sys.exit("no channel configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL, "
                 "and/or SMTP_USER + SMTP_PASSWORD + EMAIL_TO")
    sent = send(" ".join(sys.argv[1:]) or "BTC 訊號台：通知測試", subject="【BTC 訊號台】通知測試")
    print("sent via", sent or "nothing")
    sys.exit(0 if sent else 1)
