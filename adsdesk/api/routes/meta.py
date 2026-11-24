"""元信息：数据新鲜度、运行状态、库健康。

工作台打开第一件事是「这数据能信吗」。新鲜度不达标就别让人在上面做决定。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from api.db import read_conn

router = APIRouter()

CST = timezone(timedelta(hours=8))

# 新鲜度阈值（小时）。超过就不该拿来做决策，报告里也会降级标注。
FRESH_HOURS = {
    "campaign": 36,      # campaign 粒度日更，36h 内算新
    "keyword": 36,
    "placement": 36,
    "search_term": 72,   # 搜索词归因慢，72h 内算新
}


def _now() -> datetime:
    return datetime.now(CST)


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts.replace("+00:00", "Z"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST if fmt == "%Y-%m-%d" else timezone.utc)
            return round((_now() - dt).total_seconds() / 3600, 1)
        except ValueError:
            continue
    return None


@router.get("/freshness")
def freshness():
    """四张事实表的最后一天 + 距今小时数 + 是否达标。"""
    tables = {
        "campaign": ("fact_campaign_daily", "report_date"),
        "keyword": ("fact_keyword_daily", "report_date"),
        "placement": ("fact_placement_daily", "report_date"),
        "search_term": ("fact_search_term_daily", "report_date"),
    }
    out = {}
    with read_conn() as conn:
        for key, (table, col) in tables.items():
            row = conn.execute(f"select max({col}) as d from {table}").fetchone()
            last = row["d"] if row else None
            # report_date 是日期，按当日 23:59 收盘算，故 +1 天
            age = _hours_since((last + "T23:59:59+08:00") if last else None)
            limit = FRESH_HOURS[key]
            out[key] = {
                "last_date": last,
                "age_hours": age,
                "limit_hours": limit,
                "stale": (age is None) or (age > limit),
            }

        run = conn.execute(
            """select run_id, started_at, finished_at, status, decisions, suppressed, window_start, window_end
               from engine_run order by run_id desc limit 1"""
        ).fetchone()
        mem = conn.execute(
            """select run_id, business_day, created_at, agent_state, length(pack_json) as bytes
               from memorial order by run_id desc limit 1"""
        ).fetchone()
        pending = conn.execute(
            "select count(*) as n from decision_log where status='proposed'"
        ).fetchone()["n"]
        approved = conn.execute(
            "select count(*) as n from decision_log where status='approved'"
        ).fetchone()["n"]
        executed = conn.execute(
            "select count(*) as n from decision_log where status='executed'"
        ).fetchone()["n"]
        # 顶栏徽章要用的两个「等人处理」计数。
        # policy 表在老库可能不存在 —— 徽章显示 0 远好过让整个 /freshness 500：
        # 全局顶栏每个页面都吃这个接口，不能因为一张后加的表缺失就全站黑屏。
        try:
            pending_policy = conn.execute(
                "select count(*) as n from policy where status='proposed'"
            ).fetchone()["n"]
        except sqlite3.Error:
            pending_policy = 0
        try:
            open_objection = conn.execute(
                "select count(*) as n from agent_objection where verdict is null or verdict=''"
            ).fetchone()["n"]
        except sqlite3.Error:
            open_objection = 0

    stale_any = any(v["stale"] for v in out.values())
    return {
        "now": _now().isoformat(timespec="seconds"),
        "tables": out,
        "stale_any": stale_any,
        "last_run": dict(run) if run else None,
        "last_memorial": dict(mem) if mem else None,
        "counts": {
            "proposed": pending,
            "approved": approved,
            "executed": executed,
            "pending_policy": pending_policy,
            "open_objection": open_objection,
        },
    }


@router.get("/scope")
def scope_info():
    """当前分析范围。改范围要改 engine/scope.py，这里只回显，不改。"""
    with read_conn() as conn:
        active = conn.execute(
            "select scope, object_id, policy_type, params_json, source, created_at, expires_at "
            "from policy where status='active' order by policy_id"
        ).fetchall()
        portfolios = conn.execute(
            "select distinct portfolio_name from fact_campaign_daily "
            "where portfolio_name is not null order by portfolio_name"
        ).fetchall()
    return {
        "policies": [dict(r) for r in active],
        "portfolios": [r["portfolio_name"] for r in portfolios],
    }