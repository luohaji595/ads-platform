#!/usr/bin/env python3
"""Sorftime MCP 连通性探针（不依赖第三方库，直连 Streamable HTTP）。

用法:
  python3 tools/sorftime_probe.py                    # initialize + tools/list，打印工具数
  python3 tools/sorftime_probe.py --list             # 额外打印全部工具名
  python3 tools/sorftime_probe.py --call get_time    # 真实调用一个工具
  python3 tools/sorftime_probe.py --call common_usage_query

环境变量:
  SORFTIME_MCP_URL  覆盖默认端点（默认 /amazon 版）
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.request

DEFAULT_URL = "https://mcp.sorftime.com/amazon?key=rdd6qu01y01huwnqugtkb1nzvk5nqt09"
UA = "dsh-sorftime-probe/1.0"


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
    ap.add_argument("--url", default=os.environ.get("SORFTIME_MCP_URL", DEFAULT_URL))
    ap.add_argument("--list", action="store_true", help="打印全部工具名")
    ap.add_argument("--call", metavar="TOOL", help="调用指定工具")
    ap.add_argument("--args", default="{}", help="工具参数 JSON，默认 {}")
    a = ap.parse_args()

    init = rpc(a.url, {
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

    tools = rpc(a.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).get("tools", [])
    print(f"[ok] tools/list  count={len(tools)}")
    if a.list:
        for t in sorted(tools, key=lambda x: x.get("name", "")):
            print("   -", t.get("name"))

    if a.call:
        res = rpc(a.url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": a.call, "arguments": json.loads(a.args)},
        })
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        print(f"[ok] tools/call {a.call}")
        for t in texts:
            print(t)


if __name__ == "__main__":
    main()
