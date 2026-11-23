"""往 event_inbox 写事件。同一 dedupe_key 只留一条，不覆盖已有行。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.sql")
OBJECT_DEFAULT = "B0GLYW51HY"


def now_iso() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def ensure_schema(conn: sqlite3.Connection) -> None:
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def write_event(
    conn: sqlite3.Connection,
    *,
    writer: str,
    source: str,
    type: str,
    summary: str,
    object_id: str = OBJECT_DEFAULT,
    backlink: str | None = None,
    payload: Any = None,
    occurred_at: str | None = None,
    business_day: str | None = None,
    extra_key: str | None = None,
) -> dict:
    """写入一条事件。已存在同一 dedupe_key 时不改原行，返回 inserted=False。"""
    ensure_schema(conn)
    occurred_at = occurred_at or now_iso()
    received_at = now_iso()
    if not business_day:
        business_day = occurred_at[:10]
    dedupe = f"{type}|{object_id}|{business_day}"
    if extra_key:
        dedupe = f"{dedupe}|{extra_key}"
    payload_json = (
        json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
    )
    cur = conn.execute(
        """INSERT OR IGNORE INTO event_inbox
           (writer, source, type, object_id, summary, backlink,
            occurred_at, received_at, dedupe_key, payload_json, status)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')""",
        (
            writer,
            source,
            type,
            object_id,
            summary,
            backlink,
            occurred_at,
            received_at,
            dedupe,
            payload_json,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT event_id, status FROM event_inbox WHERE dedupe_key=?",
            (dedupe,),
        ).fetchone()
        event_id = row[0] if not isinstance(row, sqlite3.Row) else row["event_id"]
        status = row[1] if not isinstance(row, sqlite3.Row) else row["status"]
        return {
            "event_id": event_id,
            "dedupe_key": dedupe,
            "inserted": False,
            "status": status,
        }
    return {
        "event_id": cur.lastrowid,
        "dedupe_key": dedupe,
        "inserted": True,
        "status": "pending",
    }
