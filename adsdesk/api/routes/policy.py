"""策略库认领：proposed → active / rejected。

为什么单开一个路由，而不是塞进 agents.py：
agent 那五个端点是「诊断结果只读」，认领 policy 是**写动作** —— 写完之后
engine 下次跑就按这条规矩行事（agents/context.py 只读 status='active' 的 policy）。
读和写混在一个文件里，将来加审计/二次确认时分不清哪几个端点是会改系统行为的。

三条口径，和归因 agent 的写入端对齐：
1. 归因 agent 只写 status='proposed'（agents/attribute.py 的 _policy_proposal），
   active 只能由人在这里点出来 —— 这就是「agent 不给自己发许可证」的落点。
2. weak 证据根本不进这条队列（同一个函数里就 return False 了），
   所以「队列里出现的东西」已经过了一道证据闸门。
3. 认领必须能追到源头：policy.source 里写着 `归因agent#<decision_id>-<窗>-<日>`，
   params_json 里也有 decision_id。前端展示证据原文 + 来源决策，
   让人看得到「这条规矩是从哪一次实测里长出来的」，而不是凭空多一条。

拒绝也要落 reviewed_at：库要能回答「这条提案谁看过、什么时候、为什么没认」，
否则三个月后同一结论会被反复提案（attribute.py 的去重键含 source 里的日期，
换一天就是新 source，不去重就会盖楼）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.db import guard_table, read_conn, write_conn

router = APIRouter()

CST = timezone(timedelta(hours=8))

# policy_type → 人话。认领时人看的是这里有意义的短语，不是 `attribution_bid_down`。
# 没命中就用兜底文案，绝不吞掉一条 —— 将来加新动作类型不该让整张卡变空白。
TYPE_LABEL = {
    "attribution_bid_down": "该对象降价后 7/14 天实测有效 → 下次同类信号直接降",
    "attribution_bid_up": "该对象加价后 7/14 天实测有效 → 下次同类信号直接加",
    "attribution_pause": "该对象暂停后实测有效 → 下次同类信号直接停",
    "attribution_budget": "该对象调预算后实测有效 → 下次同类信号直接调",
    "attribution_add_negative": "该对象否词后实测有效 → 下次同类信号直接否",
    # 下面这些是平台自己的元规矩（scope=platform），不是归因提案，但同样躺在 policy 表里
    "ledger_source_of_truth": "台账唯一来源：建议与审批只走 decision_log，策略只走 policy",
    "sqlite_until_p2": "P1 全程本机 SQLite，迁 Postgres 放到 P2 单独一步",
    "existing_schedulers": "沿用现有日更与 07:00 引擎调度，不另起钟点",
    "memorial_soft_cap": "每日建议 ≤8 条（软目标），按 spend_impact 降序排",
    "scope_p0_p1": "范围限 P0–P1：只做高位下拉机广告垂直",
    "baseline_semantics": "基准口径必须同源：写账户就看全店漏斗，写组合就看本组合",
    "settlement_recheck": "Amazon T+1 只是可查、不是已结算，结算数要回头复核",
    "db_concurrency": "写库用 autocommit，写一条放一条，别整轮攥着锁",
}


def _now() -> datetime:
    return datetime.now(CST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _source_decision(source: str | None) -> int | None:
    """从 `归因agent#123-14d-2026-11-24` 里抠出 123。

    抠不出来就返回 None（老账本行/人工写的 policy），前端据此不显示「回看决策」，
    而不是显示一个会 404 的链接。
    """
    if not source or "#" not in source:
        return None
    tail = source.split("#", 1)[1]
    head = tail.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def _row(r: sqlite3.Row) -> dict:
    params: dict = {}
    if r["params_json"]:
        try:
            params = json.loads(r["params_json"])
        except (ValueError, TypeError):
            params = {}
    return {
        "policy_id": r["policy_id"],
        "scope": r["scope"],
        "object_id": r["object_id"],
        "policy_type": r["policy_type"],
        # 库里还有一类不是归因结论的 policy（平台自身的元规矩，如
        # ledger_source_of_truth / sqlite_until_p2，来源写着「计划书…拍定」）。
        # 给它们硬套「归因策略 ·」是错的，会让人以为这条是从某次实测里长出来的。
        "type_label": TYPE_LABEL.get(r["policy_type"])
        or (f"归因策略 · {r['policy_type']}"
            if r["policy_type"].startswith("attribution_") else r["policy_type"]),
        "evidence_text": r["evidence_text"],
        "source": r["source"],
        "decision_id": params.get("decision_id") or _source_decision(r["source"]),
        "params": params,
        "status": r["status"],
        "created_at": r["created_at"],
        "reviewed_at": r["reviewed_at"],
        "expires_at": r["expires_at"],
    }


@router.get("/pending")
def pending():
    """待认领：归因 agent 攒出来的结论，等人点一下才生效。"""
    try:
        with read_conn() as conn:
            rows = conn.execute(
                """select policy_id, scope, object_id, policy_type, params_json, source,
                          evidence_text, status, created_at, reviewed_at, expires_at
                   from policy where status='proposed'
                   order by created_at desc, policy_id desc"""
            ).fetchall()
    except sqlite3.Error as e:
        return {"policies": [], "note": f"读不到 policy：{type(e).__name__}: {e}"}

    items = [_row(r) for r in rows]
    return {
        "policies": items,
        # 空态要说清「是因为没有，还是因为读不到」。
        # 这两种情况的处置完全相反（前者不用管，后者得去看库），不能都显示空白。
        "note": None if items else
                "没有待认领的策略。归因 agent 只在 7/14 天对照实测有效"
                "（strong/medium 且 improved/worsened）时才会提案，攒不够就不出现。",
    }


@router.get("/active")
def active():
    """已生效：engine 下次跑就会按这些规矩行事。给 AgentPanel 做「当前规矩」清单。"""
    with read_conn() as conn:
        rows = conn.execute(
            """select policy_id, scope, object_id, policy_type, params_json, source,
                      evidence_text, status, created_at, reviewed_at, expires_at
               from policy where status='active'
               order by reviewed_at desc, policy_id desc"""
        ).fetchall()
    items = [_row(r) for r in rows]
    return {"policies": items,
            "note": None if items else "策略库还没有生效条目，引擎仍按规则硬阈值判断。"}


class ActivateIn(BaseModel):
    actor: str = Field(default="owner", max_length=64)
    # 认领理由：为什么现在就让它生效（比如「这个活动已经稳了两周」）。
    # 选填，但写了就会追加进 evidence_text —— 三个月后回看时，
    # 「当时凭什么」比「什么时候」更值钱。
    note: str | None = Field(default=None, max_length=1000)


class RejectIn(BaseModel):
    actor: str = Field(default="owner", max_length=64)
    reason: str | None = Field(default=None, max_length=1000)


def _fetch(conn, policy_id: int) -> dict:
    r = conn.execute(
        """select policy_id, scope, object_id, policy_type, params_json, source,
                  evidence_text, status, created_at, reviewed_at, expires_at
           from policy where policy_id=?""", (policy_id,)
    ).fetchone()
    if not r:
        raise HTTPException(404, f"policy #{policy_id} 不存在")
    return dict(r)


def _close(conn, policy_id: int, status: str, actor: str,
           remark: str | None) -> dict:
    """把一条 proposed 收口成 active / rejected。"""
    guard_table("policy")
    d = _fetch(conn, policy_id)
    if d["status"] == status:
        # 重复点不是错，但不该悄悄刷新 reviewed_at —— 那个时间戳是「人的动作时刻」，
        # 刷了就看不出「其实三天前就认领了」。
        return {"policy_id": policy_id, "status": status, "noop": True,
                "reviewed_at": d["reviewed_at"]}
    if d["status"] != "proposed":
        raise HTTPException(
            409,
            f"policy #{policy_id} 当前状态 {d['status']}，只有 proposed 才能认领/驳回。"
            f"已生效的条目要改口径，直接改库里 status 或新提一条 ——"
            f"工作台不提供「生效中悄悄改内容」的入口。",
        )

    now = _iso(_now())
    evidence = d["evidence_text"] or ""
    if remark:
        # 追加而不是覆盖：evidence_text 是 agent 写的实测原文，
        # 人拍板的理由单独成行，两者的作者不该混成一段。
        evidence = (evidence + f"\n· {actor} {now[:10]}：{remark}").strip()

    conn.execute(
        "update policy set status=?, reviewed_at=?, evidence_text=? where policy_id=?",
        (status, now, evidence, policy_id),
    )
    return {"policy_id": policy_id, "status": status, "reviewed_at": now,
            "evidence_text": evidence, "noop": False}


@router.post("/{policy_id}/activate")
def activate(policy_id: int, body: ActivateIn):
    """认领：proposed → active。这一下之后，engine 再跑就会照这条行事。"""
    with write_conn() as conn:
        detail = _close(conn, policy_id, "active", body.actor, body.note)
    return {"ok": True, "detail": detail}


@router.post("/{policy_id}/reject")
def reject(policy_id: int, body: RejectIn):
    """驳回：proposed → rejected。不进 active，但留下 reviewed_at 说明有人看过。"""
    with write_conn() as conn:
        detail = _close(conn, policy_id, "rejected", body.actor, body.reason)
    return {"ok": True, "detail": detail}
