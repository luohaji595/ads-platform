"""Agent 面板的只读接口。

这个面板回答一个问题：**agent 当时说的话，后来对不对**。
不是投放操作面板 —— 这里没有任何写接口，全部走 `read_conn()` 只读。

四块数据：
  1. summary      运行 / 反对 / 断言 / 假设 的计数总表
  2. runs         最近运行（含每条 run 产出的断言数与反对数）
  3. objections   反对与到期兑现
  4. evals        断言兑现率（按 agent 分组）
  5. assumptions  待复核假设

容错约定：每个接口自己吞掉 sqlite 错误，返回空列表 + note，绝不 500；
summary 里每一块单独 try，单块失败不影响其它块。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Query

from api.db import read_conn

router = APIRouter()

CST = timezone(timedelta(hours=8))

NOTE_EMPTY = "还没跑过 agent，这里暂时是空的。"
NOTE_ASSUMPTIONS_EMPTY = "还没有被跳过的建议，暂无假设待复核。"


def _today() -> str:
    return datetime.now(CST).date().isoformat()


def _elapsed_s(started_at: str | None, finished_at: str | None) -> float | None:
    """两次 ISO 时间戳之差。解析不了就返回 null —— 前端显示「—」，不猜。"""
    if not started_at or not finished_at:
        return None
    try:
        a = datetime.fromisoformat(started_at)
        b = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return round((b - a).total_seconds(), 1)


def _parse_predicts(raw: str | None) -> list[dict]:
    """predicts_json 解析成数组。历史行大多是空串，坏了就返回 []，不抛。"""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        return [v]
    return []


def _n(v) -> int:
    return int(v or 0)


def _hit_rate(correct: int, wrong: int) -> float | None:
    """命中率 = correct /(correct+wrong)。分母 0 返回 null —— 给 0 会误导成「全错」。"""
    denom = correct + wrong
    return round(correct / denom, 4) if denom else None


@router.get("/summary")
def summary():
    """四块计数总表。每块独立 try：一块查询失败只标记自己 error，不拖垮整页。"""
    out: dict = {}

    with read_conn() as conn:
        # ---- 运行 ----
        try:
            r = conn.execute(
                """select count(*) total,
                          sum(case when status='ok' then 1 else 0 end) ok,
                          sum(case when status='failed' then 1 else 0 end) failed,
                          sum(case when status='running' then 1 else 0 end) running,
                          max(started_at) last_at
                   from agent_run"""
            ).fetchone()
            out["runs"] = {
                "total": _n(r["total"]),
                "ok": _n(r["ok"]),
                "failed": _n(r["failed"]),
                "running": _n(r["running"]),
                "last_at": r["last_at"],
            }
        except sqlite3.Error as e:
            out["runs"] = {"total": 0, "ok": 0, "failed": 0, "running": 0,
                           "last_at": None, "error": f"{type(e).__name__}: {e}"}

        # ---- 反对（按 outcome 分） ----
        try:
            r = conn.execute(
                """select count(*) total,
                          sum(case when outcome='pending' then 1 else 0 end) pending,
                          sum(case when outcome='correct' then 1 else 0 end) correct,
                          sum(case when outcome='wrong' then 1 else 0 end) wrong,
                          sum(case when outcome='unverifiable' then 1 else 0 end) unverifiable
                   from agent_objection"""
            ).fetchone()
            pending, correct, wrong, unverifiable = (
                _n(r["pending"]), _n(r["correct"]), _n(r["wrong"]), _n(r["unverifiable"]))
            out["objections"] = {
                "total": _n(r["total"]),
                "pending": pending,
                "checked": correct + wrong + unverifiable,
                "correct": correct,
                "wrong": wrong,
                "unverifiable": unverifiable,
            }
        except sqlite3.Error as e:
            out["objections"] = {"total": 0, "pending": 0, "checked": 0,
                                 "correct": 0, "wrong": 0, "unverifiable": 0,
                                 "error": f"{type(e).__name__}: {e}"}

        # ---- 断言 ----
        try:
            r = conn.execute(
                """select count(*) total,
                          sum(case when verdict='pending' then 1 else 0 end) pending,
                          sum(case when verdict='correct' then 1 else 0 end) correct,
                          sum(case when verdict='wrong' then 1 else 0 end) wrong,
                          sum(case when verdict='unverifiable' then 1 else 0 end) unverifiable
                   from agent_eval"""
            ).fetchone()
            correct, wrong = _n(r["correct"]), _n(r["wrong"])
            out["evals"] = {
                "total": _n(r["total"]),
                "pending": _n(r["pending"]),
                "correct": correct,
                "wrong": wrong,
                "unverifiable": _n(r["unverifiable"]),
                "hit_rate": _hit_rate(correct, wrong),
            }
        except sqlite3.Error as e:
            out["evals"] = {"total": 0, "pending": 0, "correct": 0, "wrong": 0,
                            "unverifiable": 0, "hit_rate": None,
                            "error": f"{type(e).__name__}: {e}"}

        # ---- 待复核假设 ----
        try:
            r = conn.execute(
                "select count(*) n from agent_assumption where result is null"
            ).fetchone()
            out["assumptions"] = {"open": _n(r["n"])}
        except sqlite3.Error as e:
            out["assumptions"] = {"open": 0, "error": f"{type(e).__name__}: {e}"}

    # 有历史、今天没跑，也要说清楚 —— 否则「最近运行」看着像坏了
    runs = out.get("runs") or {}
    if runs.get("total", 0) == 0:
        out["note"] = NOTE_EMPTY
    elif runs.get("last_at") and not str(runs["last_at"]).startswith(_today()):
        out["note"] = (
            f"今天（{_today()}）还没有新的 agent 运行；下面是历史留痕"
            f"（最后一次 {str(runs['last_at'])[:10]}）。"
        )
    else:
        out["note"] = None
    return out


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=200)):
    """最近运行。n_objections / n_evals 用子查询数，不 join 以免行数翻倍。"""
    try:
        with read_conn() as conn:
            rows = conn.execute(
                """select r.agent_run_id, r.job, r.run_id, r.status,
                          r.started_at, r.finished_at, r.model,
                          r.tokens_in, r.tokens_out, r.error,
                          (select count(*) from agent_objection o
                            where o.agent_run_id = r.agent_run_id) n_objections,
                          (select count(*) from agent_eval e
                            where e.agent_run_id = r.agent_run_id) n_evals
                   from agent_run r
                   order by r.started_at desc, r.agent_run_id desc
                   limit ?""",
                (limit,),
            ).fetchall()
    except sqlite3.Error as e:
        return {"runs": [], "note": f"读不到 agent_run：{type(e).__name__}: {e}"}

    items = [{
        "agent_run_id": r["agent_run_id"],
        "job": r["job"],
        "run_id": r["run_id"],
        "status": r["status"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "elapsed_s": _elapsed_s(r["started_at"], r["finished_at"]),
        "model": r["model"],
        "tokens_in": r["tokens_in"],
        "tokens_out": r["tokens_out"],
        "error": r["error"],
        "n_objections": _n(r["n_objections"]),
        "n_evals": _n(r["n_evals"]),
    } for r in rows]
    return {"runs": items, "note": None if items else NOTE_EMPTY}


@router.get("/objections")
def objections(
    limit: int = Query(20, ge=1, le=200),
    outcome: Literal["all", "pending", "correct", "wrong", "unverifiable"] = "all",
):
    """反对与兑现。summary 按全表算（不是按本页），否则「共 N 条」会随 limit 变。"""
    where, params = "", []
    if outcome != "all":
        where = "where outcome = ?"
        params.append(outcome)
    try:
        with read_conn() as conn:
            rows = conn.execute(
                f"""select objection_id, decision_id, verdict, why, evidence,
                           created_at, outcome, predicts_json, verifiable_at,
                           actual, judged_by
                    from agent_objection {where}
                    order by objection_id desc
                    limit ?""",
                (*params, limit),
            ).fetchall()
            agg = conn.execute(
                """select count(*) total,
                          sum(case when outcome='pending' then 1 else 0 end) pending,
                          sum(case when outcome='correct' then 1 else 0 end) correct,
                          sum(case when outcome='wrong' then 1 else 0 end) wrong,
                          sum(case when outcome='unverifiable' then 1 else 0 end) unverifiable
                   from agent_objection"""
            ).fetchone()
    except sqlite3.Error as e:
        return {"objections": [], "summary": None,
                "note": f"读不到 agent_objection：{type(e).__name__}: {e}"}

    items = [{
        "objection_id": r["objection_id"],
        "decision_id": r["decision_id"],
        "verdict": r["verdict"],
        "why": r["why"],
        "evidence": r["evidence"],
        "created_at": r["created_at"],
        "outcome": r["outcome"] or "pending",
        "predicts": _parse_predicts(r["predicts_json"]),
        "verifiable_at": r["verifiable_at"],
        "actual": r["actual"],
        "judged_by": r["judged_by"],
    } for r in rows]

    correct, wrong, unverifiable = _n(agg["correct"]), _n(agg["wrong"]), _n(agg["unverifiable"])
    return {
        "objections": items,
        "summary": {
            "total": _n(agg["total"]),
            "pending": _n(agg["pending"]),
            "checked": correct + wrong + unverifiable,
            "correct": correct,
            "wrong": wrong,
            "unverifiable": unverifiable,
        },
        # 三种空要分清：整表没数据 / 有数据但当前筛选没命中 / 有数据（note=None）
        "note": None if items else (
            NOTE_EMPTY if not _n(agg["total"])
            else f"当前筛选（outcome={outcome}）下没有反对记录；整表共 {_n(agg['total'])} 条。"),
    }


@router.get("/evals")
def evals(
    limit: int = Query(30, ge=1, le=200),
    verdict: Literal["all", "pending", "correct", "wrong", "unverifiable"] = "all",
):
    """断言列表 + 按 agent 分组的兑现率。

    by_agent 按全表聚合 —— 列表被 limit 截断时，比例仍要是全量口径。
    """
    where, params = "", []
    if verdict != "all":
        where = "where verdict = ?"
        params.append(verdict)
    try:
        with read_conn() as conn:
            rows = conn.execute(
                f"""select eval_id, agent, claim, method, expected,
                           verifiable_at, verdict, actual, checked_at
                    from agent_eval {where}
                    order by eval_id desc
                    limit ?""",
                (*params, limit),
            ).fetchall()
            groups = conn.execute(
                """select agent,
                          count(*) total,
                          sum(case when verdict='pending' then 1 else 0 end) pending,
                          sum(case when verdict='correct' then 1 else 0 end) correct,
                          sum(case when verdict='wrong' then 1 else 0 end) wrong,
                          sum(case when verdict='unverifiable' then 1 else 0 end) unverifiable
                   from agent_eval
                   group by agent
                   order by count(*) desc, agent"""
            ).fetchall()
            total_row = conn.execute(
                """select count(*) total,
                          sum(case when verdict='pending' then 1 else 0 end) pending,
                          sum(case when verdict='correct' then 1 else 0 end) correct,
                          sum(case when verdict='wrong' then 1 else 0 end) wrong,
                          sum(case when verdict='unverifiable' then 1 else 0 end) unverifiable
                   from agent_eval"""
            ).fetchone()
    except sqlite3.Error as e:
        return {"evals": [], "by_agent": [], "summary": None,
                "note": f"读不到 agent_eval：{type(e).__name__}: {e}"}

    items = [{
        "eval_id": r["eval_id"],
        "agent": r["agent"],
        "claim": r["claim"],
        "method": r["method"],
        "expected": r["expected"],
        "verifiable_at": r["verifiable_at"],
        "verdict": r["verdict"],
        "actual": r["actual"],
        "checked_at": r["checked_at"],
    } for r in rows]

    by_agent = []
    for g in groups:
        c, w = _n(g["correct"]), _n(g["wrong"])
        by_agent.append({
            "agent": g["agent"],
            "total": _n(g["total"]),
            "pending": _n(g["pending"]),
            "correct": c,
            "wrong": w,
            "unverifiable": _n(g["unverifiable"]),
            "hit_rate": _hit_rate(c, w),
        })

    c, w = _n(total_row["correct"]), _n(total_row["wrong"])
    return {
        "evals": items,
        "by_agent": by_agent,
        "summary": {
            "total": _n(total_row["total"]),
            "pending": _n(total_row["pending"]),
            "correct": c,
            "wrong": w,
            "unverifiable": _n(total_row["unverifiable"]),
            "hit_rate": _hit_rate(c, w),
        },
        "note": None if items else (
            NOTE_EMPTY if not _n(total_row["total"])
            else f"当前筛选（verdict={verdict}）下没有断言记录；整表共 {_n(total_row['total'])} 条。"),
    }


@router.get("/assumptions")
def assumptions():
    """待复核假设：result IS NULL，按 review_at 升序（最该先看的排前面）。"""
    try:
        with read_conn() as conn:
            rows = conn.execute(
                """select assumption_id, object_id, question_code, default_action,
                          created_at, review_at, result
                   from agent_assumption
                   where result is null
                   order by review_at asc, assumption_id asc"""
            ).fetchall()
    except sqlite3.Error as e:
        return {"assumptions": [],
                "note": f"读不到 agent_assumption：{type(e).__name__}: {e}"}

    items = [{
        "assumption_id": r["assumption_id"],
        "object_id": r["object_id"],
        "question_code": r["question_code"],
        "default_action": r["default_action"],
        "created_at": r["created_at"],
        "review_at": r["review_at"],
        "result": r["result"],
    } for r in rows]
    return {"assumptions": items,
            "note": None if items else NOTE_ASSUMPTIONS_EMPTY}
