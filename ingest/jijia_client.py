#!/usr/bin/env python3
"""积加 Open API 独立客户端（脱离 MCP 进程，可直接被脚本 / launchd 调用）。

关键约束（来自 ~/.codex/jijia-mcp/server.py 实测）：
  1. 积加白名单校验的是「请求出口 IP」→ 必须直连，走代理会白名单失效。
  2. 签名 = md5(json.dumps(payload, ensure_ascii=False) + appKey)
  3. accessToken 走 header，有效期默认 3600s，过期需重新 /api_token。
  4. 连续调用会触发接口错误 → 默认调用间隔 60s（JIJIA_MIN_INTERVAL 可覆盖）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Any

BASE_URL = os.environ.get("JIJIA_BASE_URL", "https://open.gerpgo.com/api/open")
KEYCHAIN_ACCOUNT = os.environ.get("JIJIA_KEYCHAIN_ACCOUNT", os.environ.get("USER", ""))
MIN_INTERVAL = float(os.environ.get("JIJIA_MIN_INTERVAL", "60"))
TIMEOUT = int(os.environ.get("JIJIA_TIMEOUT", "90"))


def keychain_value(service: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.strip()


def build_signature(payload: dict[str, Any], app_key: str) -> str:
    serialized = json.dumps(payload, ensure_ascii=False)
    return hashlib.md5((serialized + app_key).encode("utf-8")).hexdigest()


def _curl_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    args = ["/usr/bin/curl", "--silent", "--show-error", "--max-time", str(TIMEOUT), "--noproxy", "*",
            "--request", "POST", url]
    for key, value in headers.items():
        args.extend(["--header", f"{key}: {value}"])
    args.extend(["--data-binary", json.dumps(body, ensure_ascii=False)])
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"网络请求失败: {completed.stderr.strip()[:300]}")
    raw = completed.stdout
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"返回非 JSON（前 200 字）: {raw[:200]}") from error
    if not isinstance(value, dict):
        raise RuntimeError("返回格式不是 JSON 对象")
    return value


class JijiaClient:
    def __init__(self) -> None:
        self._app_id = ""
        self._app_key = ""
        self._token = ""
        self._token_expires_at = 0.0
        self._last_call_at = 0.0

    def _credentials(self) -> tuple[str, str]:
        if not self._app_id:
            self._app_id = keychain_value("jijia_app_id")
            self._app_key = keychain_value("jijia_app_key")
        return self._app_id, self._app_key

    def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires_at:
            return self._token
        app_id, app_key = self._credentials()
        response = _curl_json(
            BASE_URL + "/api_token",
            {"appId": app_id, "appKey": app_key},
            {"Content-Type": "application/json"},
        )
        data = response.get("data") or {}
        token = data.get("accessToken")
        if response.get("code") != 200 or not token:
            raise RuntimeError(f"Token 获取失败，code={response.get('code')} msg={response.get('msg')}")
        expires_in = int(data.get("expiresIn") or 3600)
        self._token = token
        self._token_expires_at = time.time() + max(60, expires_in - 60)
        return token

    def _throttle(self) -> None:
        if MIN_INTERVAL <= 0:
            return
        wait = MIN_INTERVAL - (time.time() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)

    def call(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        _, app_key = self._credentials()
        self._throttle()
        try:
            return self._call_once(endpoint, payload, app_key)
        except RuntimeError as error:
            text = str(error)
            if "401" not in text and "40005" not in text:
                raise
            self._token = ""
            return self._call_once(endpoint, payload, app_key)

    def _call_once(self, endpoint: str, payload: dict[str, Any], app_key: str) -> dict[str, Any]:
        token = self._get_token()
        headers = {
            "Content-Type": "application/json",
            "accessToken": token,
            "sign": build_signature(payload, app_key),
        }
        self._last_call_at = time.time()
        return _curl_json(BASE_URL + endpoint, payload, headers)


if __name__ == "__main__":
    import sys
    client = JijiaClient()
    ep = sys.argv[1] if len(sys.argv) > 1 else "/middle/base/market/page"
    body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {"page": 1, "pagesize": 5}
    print(json.dumps(client.call(ep, body), ensure_ascii=False, indent=2)[:3000])
