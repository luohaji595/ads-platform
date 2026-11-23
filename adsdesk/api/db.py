"""数据库连接与写白名单。

两条铁律（计划书 §3 三道边界）：
1. 读：只读事实表与规则输出，不改广告后台。
2. 写：只允许写审批相关的 6 张表。任何其他表的写入直接 403。

为什么要有写白名单而不是靠自觉：这个工作台未来会接 agent、接对话，
「agent 只出建议不执行」必须由代码保证，不能靠提示词保证。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADS_DESK = HERE.parent
ROOT = HERE.parents[1]                          # 项目根
import sys as _sys
for _p in (str(ADS_DESK), str(ROOT)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
DB_PATH = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))

# 工作台允许写的表 —— 只有审批与留痕，没有一张是广告配置表
WRITE_ALLOWLIST = frozenset({
    "decision_log",      # 只允许改审批列，列级限制见 ALLOWED_COLUMNS
    "exec_order",
    "exec_order_item",
    "memorial",          # agent_state 状态位
    "agent_run",
    "agent_objection",
    "agent_eval",
    "chat_turn",
    "ticket",
    "tell_outbox",
    # 打钩沉淀的回落表（原因词典 v1.3）：policy / param_override /
    # observation_task / experiment 是「打钩即沉淀」的落点，reason_hit /
    # reason_excluded / agent_assumption 是配账。它们只由
    # routes/approve.py 的 /question 与归因 agent 写，仍然没有一张广告配置表。
    "policy",
    "param_override",
    "observation_task",
    "experiment",
    "reason_hit",
    "reason_excluded",
    "agent_assumption",
})

# decision_log 只允许改这些列（防止绕过审批改 proposed_value 等）
# 注意：gate_reason 不在名单里 —— 那是引擎对「为什么按住这条」的判断，
# 审批侧能改它就能把「证据不够」洗成「证据够」。override_* 是人的动作，所以放行。
DECISION_WRITABLE_COLUMNS = frozenset({
    "status", "checked_at", "checked_by",
    "reject_reason", "reject_reason_code",
    "executed_at", "executed_value", "executed_by",
    "attribution_due_at", "ticket_id",
    "override_by", "override_reason", "overridden_at",
    "outcome_7d_json", "outcome_14d_json", "outcome_filled_at",
})


class WriteDenied(Exception):
    """越界写入。调用方应转成 403。"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"库不存在：{DB_PATH}")
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=WAL")
    conn.execute("pragma busy_timeout=5000")
    return conn


@contextmanager
def read_conn():
    conn = connect(readonly=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn():
    conn = connect(readonly=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def guard_table(table: str) -> None:
    if table not in WRITE_ALLOWLIST:
        raise WriteDenied(f"表 {table} 不在写白名单内")


def update_decision(conn: sqlite3.Connection, decision_id: int, **fields) -> int:
    """更新 decision_log 的审批列，列名与表名双重校验。"""
    guard_table("decision_log")
    bad = set(fields) - DECISION_WRITABLE_COLUMNS
    if bad:
        raise WriteDenied(f"decision_log 不允许写这些列：{sorted(bad)}")
    if not fields:
        return 0
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(
        f"update decision_log set {sets} where decision_id=?",
        (*fields.values(), decision_id),
    )
    return cur.rowcount


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
