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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(ROOT, "ingest", "data", "ads.sqlite3")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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

    if not args.project_only:
        from midship.consume import consume
        results = consume(conn)
        print(f"消费 {len(results)} 条")
        for r in results:
            print(
                f"  event #{r.get('event_id')} {r.get('type')} → "
                f"{r.get('rule') or '无规则'} / {r.get('status')} / {r.get('note') or ''}"
            )

    if args.project or args.project_only:
        from midship.project import project
        out = project(conn)
        if out.get("ok"):
            print("投影：")
            for f in out.get("files") or []:
                print(f"  {f}")
        else:
            print(f"投影失败（未回写数据库）：{out.get('error')}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    sys.exit(main())
