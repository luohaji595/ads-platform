#!/usr/bin/env python3
"""用 GitHub Git Data REST API 推送提交（绕开被掐断的 git://https 传输通道）。

为什么需要它：本机到 github.com:443 的 TLS 握手被中途重置（20.247.166.64 必超时），
`git push` 走不通；但 api.github.com 是通的。所以改用「内容寻址」的方式自己拼：
  blob（每个文件的字节）→ tree（目录结构）→ commit（带原作者/时间/信息）→ ref

因为 blob 与 tree 都是按内容哈希的，只要字节、模式、路径一致，重建出的 tree SHA
与本地完全相同，commit SHA 也会相同 —— 推上去的就是本地那个 commit，不产生分叉。

用法:
  python3 tools/push_via_api.py --dry-run     # 只算不写
  python3 tools/push_via_api.py               # 真推
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )


def _token() -> str:
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    tok = out.stdout.strip()
    if not tok:
        raise SystemExit(f"拿不到 gh token：{out.stderr.strip()[:200]}")
    return tok


class Api:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.op = _opener()
        self.calls = 0
        self.blobs: dict[str, str] = {}

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"https://api.github.com{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "ads-platform-push/1.0",
            },
        )
        self.calls += 1
        try:
            with self.op.open(req, timeout=120) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise SystemExit(f"API {method} {path} → HTTP {e.code}\n{detail}") from e
        return json.loads(raw) if raw else {}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def local_commit() -> dict:
    """读出要重建的那个 commit 的元数据。"""
    raw = git("cat-file", "commit", "HEAD")
    head, _, message = raw.partition("\n\n")
    meta = {}
    for line in head.splitlines():
        if line.startswith("tree "):
            meta["tree"] = line[5:].strip()
        elif line.startswith("author "):
            meta["author"] = line[7:].strip()
        elif line.startswith("committer "):
            meta["committer"] = line[10:].strip()
    meta["message"] = message
    meta["sha"] = git("rev-parse", "HEAD").strip()
    return meta


def parse_person(s: str) -> dict:
    """'Name <email> 1795433421 +0800' → GitHub 的 {name, email, date}。"""
    import datetime as dt

    name, _, rest = s.partition(" <")
    email, _, rest2 = rest.partition("> ")
    ts, _, tz = rest2.partition(" ")
    sign = -1 if tz.startswith("-") else 1
    tz = tz.lstrip("+-")
    offset = dt.timezone(sign * dt.timedelta(hours=int(tz[:2]), minutes=int(tz[2:])))
    when = dt.datetime.fromtimestamp(int(ts), offset).isoformat()
    return {"name": name, "email": email, "date": when}


def list_files() -> list[tuple[str, str, str]]:
    """git ls-files -s → [(mode, sha, path)]"""
    out = git("ls-files", "-s")
    rows = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode, sha, _stage = meta.split()
        rows.append((mode, sha, path))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="luohaji595/ads-platform")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()

    meta = local_commit()
    files = list_files()
    print(f"[本地] commit {meta['sha'][:12]}  tree {meta['tree'][:12]}  文件 {len(files)}")

    if a.dry_run:
        total = sum((ROOT / p).stat().st_size for _m, _s, p in files)
        print(f"[dry-run] 待上传 {total/1048576:.2f} MB，blob {len(files)} 个；未写入")
        return 0

    api = Api(_token(), a.repo)

    # 0) GitHub 不允许往「完全空的仓库」直接建 blob（409 Git Repository is empty），
    #    先用 Contents API 塞一个占位文件把默认分支开出来；稍后 force 更新 ref，
    #    那个占位 commit 会变成悬挂对象，不留在 main 的历史里。
    try:
        api.call("GET", f"/repos/{a.repo}/git/ref/heads/main")
    except SystemExit as e:
        # 空仓库对 GET ref 也回 409/404，两种都当「还没有分支」
        if "404" not in str(e) and "409" not in str(e):
            raise
        print("[0/4] 空仓库，先建占位提交把 main 开出来 …")
        api.call("PUT", f"/repos/{a.repo}/contents/.gitkeep",
                 {"message": "chore: init", "content": ""})

    # 1) 每个文件一个 blob（base64，二进制安全）
    def make_blob(row: tuple[str, str, str]) -> tuple[str, str]:
        mode, sha, path = row
        data = (ROOT / path).read_bytes()
        res = api.call("POST", f"/repos/{a.repo}/git/blobs",
                       {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        got = res["sha"]
        if got != sha:
            raise SystemExit(f"blob 对不上：{path} 本地 {sha[:10]} 远端 {got[:10]}")
        return path, got

    print(f"[1/4] 上传 blob（{a.jobs} 并发）…")
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for i, (path, got) in enumerate(ex.map(make_blob, files), 1):
            api.blobs[path] = got
            if i % 25 == 0 or i == len(files):
                print(f"      {i}/{len(files)}")

    # 2) 一棵树装全部路径（GitHub 允许同一棵树里写带 / 的嵌套路径）
    entries = [{"path": p, "mode": m, "type": "blob", "sha": api.blobs[p]}
               for m, _s, p in files]
    print(f"[2/4] 建 tree（{len(entries)} 条）…")
    tree = api.call("POST", f"/repos/{a.repo}/git/trees", {"tree": entries})
    if tree["sha"] != meta["tree"]:
        print(f"[warn] tree SHA 不一致：本地 {meta['tree'][:12]} 远端 {tree['sha'][:12]}"
              f"（内容仍会完整上传，只是 commit SHA 会变）")
    else:
        print(f"      tree SHA 完全一致 {tree['sha'][:12]} ✔")

    # 3) commit（沿用原作者、时间、信息与父提交）
    parents = [p for p in git("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]]
    print(f"[3/4] 建 commit（父 {len(parents)} 个）…")
    c = api.call("POST", f"/repos/{a.repo}/git/commits", {
        "message": meta["message"],
        "tree": tree["sha"],
        "parents": parents,
        "author": parse_person(meta["author"]),
        "committer": parse_person(meta["committer"]),
    })
    print(f"      commit {c['sha'][:12]}" + ("（与本地一致 ✔）" if c["sha"] == meta["sha"] else "（与本地不同）"))

    # 4) 建 ref
    print("[4/4] 建 ref refs/heads/main …")
    try:
        api.call("POST", f"/repos/{a.repo}/git/refs",
                 {"ref": "refs/heads/main", "sha": c["sha"]})
    except SystemExit as e:
        if "422" not in str(e) and "already exists" not in str(e):
            raise
        api.call("PATCH", f"/repos/{a.repo}/git/refs/heads/main",
                 {"sha": c["sha"], "force": True})
        print("      （ref 已存在，已更新）")

    print(f"\n[done] 共 {api.calls} 次 API 调用")
    print(f"       远端 main = {c['sha']}")
    print(f"       本地 HEAD = {meta['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
