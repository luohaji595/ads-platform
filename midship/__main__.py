"""手动排空收件箱或做台账投影。不新建定时器。

用法：
  python3 -m midship                 # 消费 pending
  python3 -m midship --dry-run       # 只打印待消费，不改状态
  python3 -m midship --project       # 消费后投影到 gbrain（失败不回写库）
  python3 -m midship --db 路径
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(ROOT, "ingest", "data", "ads.sqlite3")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _log_run(conn, dataset: str, status: str, note: str, started: str) -> None:
    """往 run_log 记一笔自己的运行账。

    为什么需要（2026-11-24）：chain.py 原来靠 `chain_run` 判 midship 是否完成，
    但 chain 是先复验、后 record，所以第一次成功也必然判 failed。run_log 是
    与 rank/market 一致的「真实状态」账本，由本进程在退出前自己写，链只要读它
    就行。表不存在（新库）时静默跳过，不让记账失败影响投影。
    """
    try:
        conn.execute(
            "INSERT INTO run_log (dataset, start_date, end_date, status, started_at,"
            " finished_at, note) VALUES (?,?,?,?,?,?,?)",
            (dataset, started[:10], started[:10], status, started,
             datetime.now(SHANGHAI).replace(microsecond=0).isoformat(), note))
        conn.commit()
    except sqlite3.Error as e:
        print(f"[midship] run_log 记账失败（忽略）：{e}", file=sys.stderr)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="本船事件中台")
    p.add_argument("--db", default=os.environ.get("ADS_DB", DEFAULT_DB))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--project", action="store_true", help="消费后投影台账到 gbrain")
    p.add_argument("--project-only", action="store_true", help="只投影，不消费")
    args = p.parse_args(argv)

    conn = connect(args.db)
    if args.dry_run:
        rows = conn.execute(
            """SELECT event_id, writer, type, object_id, summary, dedupe_key, status
               FROM event_inbox WHERE status='pending' ORDER BY event_id"""
        ).fetchall()
        print(f"pending {len(rows)} 条")
        for r in rows:
            print(f"  #{r['event_id']} {r['writer']} {r['type']} {r['dedupe_key']} | {r['summary']}")
        return 0

    # 记账用：整次运行开始时刻。run_log.dataset 用 'midship' / 'midship.project'
    # 两种前缀，方便链和报表分开看「只消费」和「投影」。
    started = datetime.now(SHANGHAI).replace(microsecond=0).isoformat()
    consumed = 0
    files: list[str] = []

    if not args.project_only:
        from midship.consume import consume
        results = consume(conn)
        consumed = len(results)
        print(f"消费 {consumed} 条")
        for r in results:
            print(
                f"  event #{r.get('event_id')} {r.get('type')} → "
                f"{r.get('rule') or '无规则'} / {r.get('status')} / {r.get('note') or ''}"
            )

    if args.project or args.project_only:
        from midship.project import project
        out = project(conn)
        if out.get("ok"):
            files = out.get("files") or []
            print("投影：")
            for f in files:
                print(f"  {f}")
        else:
            _log_run(conn, "midship.project", "failed",
                     f"投影失败：{out.get('error')}", started)
            print(f"投影失败（未回写数据库）：{out.get('error')}", file=sys.stderr)
            return 1
        _log_run(conn, "midship.project", "ok",
                 f"消费 {consumed} 条；写出 {len(files)} 个文件", started)
    else:
        _log_run(conn, "midship", "ok", f"消费 {consumed} 条（未投影）", started)
    return 0


if __name__ == "__main__":
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    sys.exit(main())
