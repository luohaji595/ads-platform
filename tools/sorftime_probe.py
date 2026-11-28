#!/usr/bin/env python3
"""Sorftime MCP 连通性探针（不依赖第三方库，直连 Streamable HTTP）。

用法:
  python3 tools/sorftime_probe.py                    # initialize + tools/list，打印工具数
  python3 tools/sorftime_probe.py --list             # 额外打印全部工具名
  python3 tools/sorftime_probe.py --call get_time    # 真实调用一个工具
  python3 tools/sorftime_probe.py --call common_usage_query

端点与密钥（按优先级，先命中先用）:
  1. 环境变量 SORFTIME_MCP_URL   完整端点，含 ?key=...
  2. 环境变量 SORFTIME_MCP_KEY   只给 key，自动拼 /amazon?key=...
  3. 本地文件 ~/.config/sorftime/key   （chmod 600，不入库）
  4. 仓库根 .env 里的 SORFTIME_MCP_KEY / SORFTIME_MCP_URL

**密钥不再写进源码。** 旧版本曾把 key 硬编码在 DEFAULT_URL 里，已从工作区移除；
若该文件曾被推到远端，请到 sorftime 后台重置 key 并更新本地 ~/.config/sorftime/key。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.request
from pathlib import Path

ENDPOINT = "https://mcp.sorftime.com/amazon"
KEY_FILE = Path.home() / ".config" / "sorftime" / "key"
UA = "dsh-sorftime-probe/1.0"


def _read_key_file() -> str | None:
    try:
        v = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v or None


def _read_dotenv() -> dict[str, str]:
    """仓库根 .env 的极简解析（只取 KEY=VALUE，不展开变量、不覆盖已有环境变量）。"""
    root = Path(__file__).resolve().parent.parent
    f = root / ".env"
    if not f.is_file():
        return {}
    out: dict[str, str] = {}
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def default_url() -> str:
    """解析端点。找不到密钥就明确报错，不要静默用空 key 去撞 401。"""
    url = os.environ.get("SORFTIME_MCP_URL")
    if url:
        return url
    key = os.environ.get("SORFTIME_MCP_KEY") or _read_key_file()
    if not key:
        dotenv = _read_dotenv()
        url = dotenv.get("SORFTIME_MCP_URL", "")
        if url:
            return url
        key = dotenv.get("SORFTIME_MCP_KEY", "")
    if not key:
        raise RuntimeError(
            "sorftime 密钥未配置。任选其一：\n"
            f"  1) export SORFTIME_MCP_KEY=<key>\n"
            f"  2) mkdir -p {KEY_FILE.parent} && printf '%s' <key> > {KEY_FILE} && chmod 600 {KEY_FILE}\n"
            "  3) 在仓库根 .env 写 SORFTIME_MCP_KEY=<key>\n"
            "（key 在 sorftime 后台的 MCP 接入页）"
        )
    return f"{ENDPOINT}?key={key}"


def _ssl_context() -> ssl.SSLContext:
    """Python.framework 版默认没有 CA 包，优先用 certifi。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def rpc(url: str, payload: dict, timeout: float = 90.0) -> dict:
    """发一条 JSON-RPC，解析 SSE 或 JSON 响应，返回 result 字典。"""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": UA,
        },
        method="POST",
    )
    # 显式禁用系统代理：本机代理会掐断 SSE 长连接
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")

    # SSE: 取最后一条 data: 行；纯 JSON: 直接解析
    lines = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
    body = lines[-1] if lines else raw
    obj = json.loads(body)
    if "error" in obj:
        raise RuntimeError(f"MCP error: {obj['error']}")
    return obj.get("result", {})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="完整端点；不传则按环境变量/本地文件解析")
    ap.add_argument("--list", action="store_true", help="打印全部工具名")
    ap.add_argument("--call", metavar="TOOL", help="调用指定工具")
    ap.add_argument("--args", default="{}", help="工具参数 JSON，默认 {}")
    a = ap.parse_args()
    url = a.url or default_url()

    init = rpc(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "dsh-sorftime-probe", "version": "1.0"},
        },
    })
    info = init.get("serverInfo", {})
    print(f"[ok] initialize  server={info.get('name')} v{info.get('version')} "
          f"protocol={init.get('protocolVersion')}")

    tools = rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).get("tools", [])
    print(f"[ok] tools/list  count={len(tools)}")
    if a.list:
        for t in sorted(tools, key=lambda x: x.get("name", "")):
            print("   -", t.get("name"))

    if a.call:
        res = rpc(url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": a.call, "arguments": json.loads(a.args)},
        })
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        print(f"[ok] tools/call {a.call}")
        for t in texts:
            print(t)


if __name__ == "__main__":
    main()
