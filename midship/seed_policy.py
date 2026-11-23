"""把计划书 v1.1 已拍定的平台决定写入 policy。同一 source+policy_type 不重复写。"""

from __future__ import annotations

from .inbox import now_iso

SEED = [
    {
        "scope": "platform",
        "object_id": "ads-platform",
        "policy_type": "ledger_source_of_truth",
        "evidence_text": "库内表为唯一写入点：建议和审批走 decision_log，策略走 policy 等表。gbrain 只做每日只读投影，不承担写入。",
        "source": "计划书v1.1-家洛拍定-2026-11-16",
    },
    {
        "scope": "platform",
        "object_id": "ads-platform",
        "policy_type": "sqlite_until_p2",
        "evidence_text": "P1 全程留本机 SQLite。迁 Postgres 放到 P2，并单独写迁移步骤。",
        "source": "计划书v1.1-家洛拍定-2026-11-16",
    },
    {
        "scope": "platform",
        "object_id": "ads-platform",
        "policy_type": "existing_schedulers",
        "evidence_text": "日更与 07:00 引擎视为既成事实。事件由 ingest / engine 收尾写入，中台只消费，不新建第三套调度器。",
        "source": "计划书v1.1-家洛拍定-2026-11-16",
    },
    {
        "scope": "platform",
        "object_id": "ads-platform",
        "policy_type": "memorial_soft_cap",
        "evidence_text": "≤8 条为软目标。按 spend_impact 绝对值降序再加规则配额。当日建议不足 3 条算正常，不为凑数而推。",
        "source": "计划书v1.1-家洛拍定-2026-11-16",
    },
    {
        "scope": "platform",
        "object_id": "B0GLYW51HY",
        "policy_type": "scope_p0_p1",
        "evidence_text": "范围是整船操作系统；P0–P1 只做高位下拉机 B0GLYW51HY 广告垂直。奏折走本聊天。预授权放 P3，默认不开。",
        "source": "计划书v1.1-家洛拍定-2026-11-16",
    },
]


def seed(conn) -> dict:
    created = 0
    skipped = 0
    now = now_iso()
    for item in SEED:
        exists = conn.execute(
            """SELECT policy_id FROM policy
               WHERE source=? AND policy_type=? AND object_id=?""",
            (item["source"], item["policy_type"], item["object_id"]),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO policy
               (scope, object_id, policy_type, params_json, source, evidence_text,
                status, created_at)
               VALUES (?,?,?,?,?,?, 'active', ?)""",
            (
                item["scope"],
                item["object_id"],
                item["policy_type"],
                None,
                item["source"],
                item["evidence_text"],
                now,
            ),
        )
        created += 1
    conn.commit()
    return {"created": created, "skipped": skipped}
