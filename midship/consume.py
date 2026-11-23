"""消费 event_inbox 中 pending 的行：按 rules.yaml 匹配、去重、派单、留痕。

不自己定时，不拉积加，不写广告后台，不直接往聊天发消息。
需要送到本聊天的，只写入 tell_outbox，由船长作为单一出口送达。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from . import yamlutil
from .inbox import ensure_schema, now_iso

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "rules.yaml")
CHANNEL = "captain_chat"


def load_rules(path: str = RULES_PATH) -> list[dict]:
    data = yamlutil.load_path(path)
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        raise ValueError(f"rules.yaml 缺少 rules 列表：{path}")
    return rules


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def match_rule(rules: list[dict], event: dict) -> dict | None:
    for rule in rules:
        when = rule.get("when") or {}
        types = _as_list(when.get("type"))
        writers = _as_list(when.get("writer"))
        if types and event["type"] not in types:
            continue
        if writers and event["writer"] not in writers:
            continue
        return rule
    return None


def _row_to_event(row) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    # fallback positional — keep keys in table order
    keys = [
        "event_id", "writer", "source", "type", "object_id", "summary",
        "backlink", "occurred_at", "received_at", "dedupe_key",
        "payload_json", "status", "consumed_at",
    ]
    return {k: row[i] for i, k in enumerate(keys)}


def _payload(event: dict) -> dict:
    raw = event.get("payload_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"value": data}
    except json.JSONDecodeError:
        return {"raw": raw}


def _business_day(event: dict) -> str:
    payload = _payload(event)
    for key in ("business_day", "window_end", "report_date", "day"):
        if payload.get(key):
            return str(payload[key])[:10]
    dedupe = event.get("dedupe_key") or ""
    parts = dedupe.split("|")
    if len(parts) >= 3 and len(parts[2]) >= 10:
        return parts[2][:10]
    occurred = event.get("occurred_at") or ""
    return occurred[:10]


def _already_told(conn, business_day: str, action: str) -> bool:
    row = conn.execute(
        """SELECT outbox_id, status FROM tell_outbox
           WHERE channel=? AND business_day=? AND action=?""",
        (CHANNEL, business_day, action),
    ).fetchone()
    return row is not None


def _put_outbox(conn, dispatch_id, business_day, action, title, body, backlink, status, note) -> int | None:
    cur = conn.execute(
        """INSERT OR IGNORE INTO tell_outbox
           (dispatch_id, channel, business_day, action, title, body, backlink, status, created_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (dispatch_id, CHANNEL, business_day, action, title, body, backlink, status, now_iso(), note),
    )
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def _act(conn, event: dict, rule: dict, already_same_round: bool) -> dict:
    """执行规则动作。只写中台表，不发聊天、不改广告后台。"""
    action = rule.get("action") or "consume"
    tell = rule.get("tell") or "silent"
    assigned = rule.get("to") or "midship"
    payload = _payload(event)
    day = _business_day(event)
    note_bits = []
    status = "recorded"
    backlink = event.get("backlink") or payload.get("report_path")
    outbox_id = None
    outbox_status = None

    if already_same_round:
        status = "deduped"
        note_bits.append("同一对象同一类型本轮已消费，本条只留痕")

    if action == "pack_memorial":
        title = f"{day} 广告奏折"
        body = payload.get("summary") or event.get("summary")
        if _already_told(conn, day, action) or already_same_round:
            status = "deduped"
            note_bits.append("同一业务日已有奏折，不再重复入待送达")
        elif tell == "chat":
            # dispatch_id 稍后补写：先占位 0，下面插入 dispatch 后再更新
            outbox_status = "pending"
            note_bits.append("待船长送到本聊天")
        else:
            outbox_status = "suppressed"
            note_bits.append("规则未要求推聊天")
    elif action == "remind":
        title = f"{day} 奏折到期未批"
        if _already_told(conn, day, action) or already_same_round:
            status = "deduped"
            note_bits.append("同一张不重复催")
        elif tell == "chat":
            outbox_status = "pending"
            note_bits.append("待船长催批")
    elif action == "consume":
        note_bits.append(rule.get("note") or "静默消费")
    else:
        note_bits.append(rule.get("note") or f"动作 {action} 已登记，由对应岗位处理")

    note = "；".join(n for n in note_bits if n)
    cur = conn.execute(
        """INSERT INTO dispatch_log
           (event_id, rule_name, assigned_to, action, tell, status, note, backlink, dispatched_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            event["event_id"],
            rule.get("name") or "",
            assigned,
            action,
            tell,
            status,
            note,
            backlink,
            now_iso(),
        ),
    )
    dispatch_id = cur.lastrowid
    if outbox_status and status != "deduped":
        title = locals().get("title") or event.get("summary") or action
        body = locals().get("body") or event.get("summary")
        outbox_id = _put_outbox(
            conn, dispatch_id, day, action, title, body, backlink, outbox_status, note,
        )
    return {
        "dispatch_id": dispatch_id,
        "outbox_id": outbox_id,
        "action": action,
        "assigned_to": assigned,
        "status": status,
        "note": note,
        "business_day": day,
    }


def consume(conn: sqlite3.Connection, rules_path: str = RULES_PATH) -> list[dict]:
    """消费全部 pending 事件，返回本轮派单结果。"""
    ensure_schema(conn)
    rules = load_rules(rules_path)
    rows = conn.execute(
        """SELECT event_id, writer, source, type, object_id, summary, backlink,
                  occurred_at, received_at, dedupe_key, payload_json, status, consumed_at
           FROM event_inbox
           WHERE status='pending'
           ORDER BY event_id ASC"""
    ).fetchall()
    results = []
    seen: set[tuple] = set()
    for row in rows:
        event = _row_to_event(row)
        rule = match_rule(rules, event)
        now = now_iso()
        if rule is None:
            conn.execute(
                "UPDATE event_inbox SET status='ignored', consumed_at=? WHERE event_id=?",
                (now, event["event_id"]),
            )
            cur = conn.execute(
                """INSERT INTO dispatch_log
                   (event_id, rule_name, assigned_to, action, tell, status, note, backlink, dispatched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event["event_id"],
                    "",
                    "midship",
                    "none",
                    "silent",
                    "ignored",
                    "没有匹配的规则",
                    event.get("backlink"),
                    now,
                ),
            )
            results.append(
                {
                    "event_id": event["event_id"],
                    "type": event["type"],
                    "status": "ignored",
                    "dispatch_id": cur.lastrowid,
                    "note": "没有匹配的规则",
                }
            )
            continue
        key = (event["type"], event["object_id"], _business_day(event), rule.get("action"))
        already = key in seen
        acted = _act(conn, event, rule, already)
        seen.add(key)
        conn.execute(
            "UPDATE event_inbox SET status='consumed', consumed_at=? WHERE event_id=?",
            (now, event["event_id"]),
        )
        results.append(
            {
                "event_id": event["event_id"],
                "type": event["type"],
                "rule": rule.get("name"),
                **acted,
            }
        )
    conn.commit()
    return results
