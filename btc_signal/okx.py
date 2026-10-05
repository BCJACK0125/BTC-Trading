"""Minimal OKX V5 REST client (signed requests), demo trading by default.

Demo trading uses the same host with the header `x-simulated-trading: 1` and a
key created under Trade -> Demo Trading -> Personal Center -> Demo Trading API.
Signature: Base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

BASE_URL = "https://www.okx.com"


class OkxError(RuntimeError):
    def __init__(self, code: str, msg: str, data=None):
        super().__init__(f"OKX {code}: {msg}")
        self.code, self.msg, self.data = code, msg, data


def sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    mac = hmac.new(secret.encode(), (timestamp + method.upper() + path + body).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


class Okx:
    def __init__(self, key: str, secret: str, passphrase: str, demo: bool = True,
                 base_url: str = BASE_URL, timeout: int = 15):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.demo, self.base_url, self.timeout = demo, base_url.rstrip("/"), timeout

    def headers(self, method: str, path: str, body: str = "") -> dict:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        h = {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign(self.secret, ts, method, path, body),
             "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase,
             "Content-Type": "application/json"}
        if self.demo:
            h["x-simulated-trading"] = "1"
        return h

    def request(self, method: str, path: str, params: dict | None = None, body: dict | list | None = None,
                retries: int = 2) -> list:
        if params:
            path = f"{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.request(method, self.base_url + path, data=payload or None,
                                     headers=self.headers(method, path, payload), timeout=self.timeout)
                j = r.json()
            except (requests.RequestException, ValueError) as e:
                last = OkxError("network", str(e))
                time.sleep(1 + attempt)
                continue
            if j.get("code") == "0":
                return j.get("data", [])
            # per-item errors (e.g. order rejected) come back with code 1/2 and details in data
            detail = (j.get("data") or [{}])[0] if isinstance(j.get("data"), list) else {}
            raise OkxError(detail.get("sCode") or j.get("code", "?"), detail.get("sMsg") or j.get("msg", ""), j.get("data"))
        raise last

    # --- thin wrappers used by the paper trader -------------------------------------
    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def post(self, path, body):
        return self.request("POST", path, body=body)
