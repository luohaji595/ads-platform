"""把库内台账投影成 gbrain 只读文件。投影失败不得回写数据库。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DIR = os.environ.get(
    "GBRAIN_DECISIONS_DIR",
    os.path.expanduser("~/gbrain/memory/decisions"),
)


def _today() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def project(conn: sqlite3.Connection, dest_dir: str = DEFAULT_DIR) -> dict:
    """导出三份 Markdown。目录不存在或写入失败时返回 error，不抛给调用方改库。"""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        decided = _render_policy(conn)
        excluded = _render_excluded(conn)
        approved = _render_daily(conn)
        paths = {
            "家洛定的事.md": decided,
            "定了不做的事.md": excluded,
            "当日审批摘要.md": approved,
        }
        written = []
        for name, text in paths.items():
            path = os.path.join(dest_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            written.append(path)
        return {"ok": True, "dir": dest_dir, "files": written}
    except Exception as e:
        return {"ok": False, "error": str(e), "dir": dest_dir}


def _render_policy(conn) -> str:
    rows = conn.execute(
        """SELECT policy_id, scope, object_id, policy_type, params_json, source,
                  evidence_text, status, created_at, expires_at
           FROM policy WHERE status='active' ORDER BY policy_id"""
    ).fetchall()
    lines = [f"# 家洛定的事", f"", f"- 投影日：{_today()} Asia/Shanghai", f"- 来源：policy（status=active）", ""]
    if not rows:
        lines.append("（目前没有生效中的策略行。）")
        return "\n".join(lines) + "\n"
    for r in rows:
        d = dict(r) if not isinstance(r, sqlite3.Row) else dict(r)
        # sqlite3.Row works with keys if row_factory set; otherwise tuple
        if not isinstance(r, sqlite3.Row) and not isinstance(r, dict):
            keys = ["policy_id", "scope", "object_id", "policy_type", "params_json",
                    "source", "evidence_text", "status", "created_at", "expires_at"]
            d = dict(zip(keys, r))
        lines.append(f"## {d['policy_type']} · {d['object_id']}")
        lines.append("")
        if d.get("evidence_text"):
            lines.append(d["evidence_text"].strip())
            lines.append("")
        lines.append(f"- 作用域：{d.get('scope') or '—'}")
        lines.append(f"- 来源：{d.get('source') or '—'}")
        lines.append(f"- 记下：{d.get('created_at') or '—'}")
        lines.append(f"- 到期：{d.get('expires_at') or '无'}")
        lines.append("")
    return "\n".join(lines)


def _render_excluded(conn) -> str:
    lines = [f"# 定了不做的事", f"", f"- 投影日：{_today()} Asia/Shanghai", ""]
    # 这张表只有 5 列：excluded_id / object_id / reason_code / created_at / expires_at。
    # 早先这里 SELECT 了不存在的 evidence_text，被下面的 except 吞掉后整段变成
    # 「reason_excluded 暂无行」—— 用户划掉的每一条方向静默消失，系统永不报错。
    # 现在按真实列查，且失败时把错误写进输出（不再伪装成「没有数据」）。
    err = ""
    try:
        ex = conn.execute(
            """SELECT object_id, reason_code, created_at, expires_at
               FROM reason_excluded ORDER BY excluded_id DESC"""
        ).fetchall()
    except sqlite3.Error as e:
        ex = []
        err = f"{type(e).__name__}: {e}"
    lines.append("## 显式排除")
    lines.append("")
    if err:
        lines.append(f"（读 reason_excluded 失败：{err}）")
        lines.append("")
    elif not ex:
        lines.append("（reason_excluded 暂无行。）")
        lines.append("")
    else:
        keys = ["object_id", "reason_code", "created_at", "expires_at"]
        for r in ex:
            d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(keys, r))
            lines.append(
                f"- {d['object_id']} / {d['reason_code']} "
                f"（{d.get('created_at') or ''}，到期 {d.get('expires_at') or '无'}）"
            )
        lines.append("")
    lines.append("## 已否决的建议")
    lines.append("")
    try:
        rej = conn.execute(
            """SELECT entity_label, entity_id, action_type, reject_reason_code,
                      reject_reason, checked_at, checked_by
               FROM decision_log
               WHERE status='rejected' AND (reject_reason IS NOT NULL OR reject_reason_code IS NOT NULL)
               ORDER BY checked_at DESC"""
        ).fetchall()
        rej_err = ""
    except sqlite3.Error as e:
        rej = []
        rej_err = f"{type(e).__name__}: {e}"
    if rej_err:
        lines.append(f"（读 decision_log 失败：{rej_err}）")
        lines.append("")
    elif not rej:
        lines.append("（没有带理由的否决。）")
        lines.append("")
    else:
        keys = ["entity_label", "entity_id", "action_type", "reject_reason_code",
                "reject_reason", "checked_at", "checked_by"]
        for r in rej:
            d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(keys, r))
            label = d.get("entity_label") or d.get("entity_id")
            lines.append(
                f"- {label} / {d.get('action_type')}：{d.get('reject_reason_code') or ''} "
                f"{d.get('reject_reason') or ''}（{d.get('checked_at') or ''}，{d.get('checked_by') or ''}）"
            )
        lines.append("")
    return "\n".join(lines)


def _render_daily(conn) -> str:
    today = _today()
    lines = [f"# 当日审批摘要", f"", f"- 日期：{today} Asia/Shanghai", ""]
    try:
        rows = conn.execute(
            """SELECT decision_id, entity_label, entity_id, action_type, status,
                      checked_at, checked_by, reject_reason_code, reject_reason
               FROM decision_log
               WHERE checked_at IS NOT NULL AND substr(checked_at, 1, 10)=?
               ORDER BY checked_at""",
            (today,),
        ).fetchall()
        day_err = ""
    except sqlite3.Error as e:
        rows = []
        day_err = f"{type(e).__name__}: {e}"
    if day_err:
        lines.append(f"（读 decision_log 失败：{day_err}）")
        lines.append("")
        return "\n".join(lines)
    if not rows:
        lines.append("（今日还没有打钩或否决。）")
        lines.append("")
        return "\n".join(lines)
    keys = ["decision_id", "entity_label", "entity_id", "action_type", "status",
            "checked_at", "checked_by", "reject_reason_code", "reject_reason"]
    for r in rows:
        d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(keys, r))
        label = d.get("entity_label") or d.get("entity_id")
        extra = d.get("reject_reason_code") or d.get("reject_reason") or ""
        lines.append(
            f"- #{d['decision_id']} {label} / {d.get('action_type')} → {d.get('status')} "
            f"{extra}（{d.get('checked_at')}，{d.get('checked_by') or ''}）"
        )
    lines.append("")
    return "\n".join(lines)
