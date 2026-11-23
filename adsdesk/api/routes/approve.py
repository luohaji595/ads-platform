"""审批写入。这是整个工作台唯一的写入口。

三条设计原则（计划书 §6）：
1. 打钩 ≠ 执行。打钩只写 decision_log.status='approved'，生成可复制的执行单，
   人去广告后台自己改。这个 API 永远不碰广告后台。
2. 归因窗从「已改」开始计时，不从打钩开始。批了不改是常态，
   从打钩起算会把「没动」的时间算进效果窗口，造成系统性归因偏差。
3. 打叉要给原因码。原因码沉淀进 reason_hit，是未来判断的原料。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.db import guard_table, read_conn, update_decision, write_conn
from api.models import (
    ApproveBatchIn,
    ApproveIn,
    ExecOrderIn,
    ExecutedIn,
    Ok,
    QuestionAnswerIn,
)

router = APIRouter()

CST = timezone(timedelta(hours=8))

# 归因窗长度（天）：按动作类型分，不是一刀切 7 天。
# 常量**只在 agents/attribute.py 定义**（计划书 §4.4 指定的落点）—— 这里导入，
# 绝不在本地再写一份：两边各写一份就会漂，而它决定 attribution_due_at 落在哪天，
# 归因 agent 又按 due − executed 反推窗口，一旦漂了就会出现「用 4 天窗口去判
# 一个 7 天才见效的 bid 改动」，等于在效应还没显现时判无效。
from agents.attribute import days_for  # noqa: E402

# 原因码 → 沉淀去向的唯一真源在 engine/reason_routes.py。engine/ 通常已在 sys.path
# （上面导入 agents.attribute 时它自己插过），这里再显式插一次，不依赖导入顺序。
_ENGINE = str(Path(__file__).resolve().parents[3] / "engine")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)
import reason_routes  # noqa: E402


def _now() -> datetime:
    return datetime.now(CST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# 账户级 entity —— 这些建议没有可打钩的落点（改哪个活动？改哪个词？）
ACCOUNT_LEVEL = frozenset({"account", "store", ""})


def _fetch_decision(conn, decision_id: int) -> dict:
    row = conn.execute(
        """select decision_id, run_id, rule_id, action_type, entity_type, entity_id,
                  entity_label, campaign_id, current_value, proposed_value,
                  status, confidence, spend_impact, rationale, ticket_id,
                  gate_reason, override_by, override_reason, overridden_at
           from decision_log where decision_id=?""", (decision_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"decision #{decision_id} 不存在")
    d = dict(row)
    # 账户级告警（R30）只用于知情，不可审批、不可执行 ——
    # 它在库里状态是 notified，不是 proposed。允许它进审批队列就等于
    # 制造「批了也不知道改什么」的僵尸，11-17 刚清掉 5 条这种。
    if d["entity_type"] in ACCOUNT_LEVEL or d["action_type"] == "alert":
        raise HTTPException(
            409,
            f"#{decision_id} 是账户级告警（{d['entity_type']}/{d['action_type']}），"
            f"没有可执行的对象，不能审批。它只是告诉你全店的目标不可达。",
        )
    return d


@router.post("/decision", response_model=Ok)
def approve_one(body: ApproveIn):
    with write_conn() as conn:
        detail = _apply_approval(conn, body)
    return Ok(detail=detail)


@router.post("/batch", response_model=Ok)
def approve_batch(body: ApproveBatchIn):
    """批量打钩。任一条失败整批回滚 —— 半批状态最难排查。"""
    results = []
    with write_conn() as conn:
        for item in body.items:
            results.append(_apply_approval(conn, item))
    return Ok(detail={"items": results})


def _close_exec_items(conn, decision_id: int, ts: str, executed_by: str) -> list[dict]:
    """把「已改」回写到执行单：勾掉条目、重算单子状态。

    为什么必须有这一步：执行单是给人照着改的清单，而 `exec_order_item.executed_at`
    与 `exec_order.status` 之前没有任何代码写过 —— 实测整条链路时，点完四条「已改」
    decision_log 全部 executed、归因窗也有了，但执行单里四条的 executed_at 全是 NULL、
    单子状态一直是 open，前端（Approval.tsx 靠 item.executed_at 决定显不显示按钮）
    于是做完的条目还在挂着「已改」按钮，人会当没生效再点一次（第二次撞 409 才发觉）。
    单子永远 open 的另一个后果：没人知道这单还剩几条没改。

    只动 `executed_at is null` 的行 —— 重复调用不会把时间戳往后刷；
    cancelled 的单子不参与（已经作废的单子不因为有条目被执行就复活）。
    """
    guard_table("exec_order_item")
    guard_table("exec_order")
    order_ids = [
        r["order_id"] for r in conn.execute(
            """select distinct i.order_id from exec_order_item i
               join exec_order o using (order_id)
               where i.decision_id=? and i.executed_at is null and o.status != 'cancelled'""",
            (decision_id,),
        )
    ]
    if not order_ids:
        return []
    conn.execute(
        """update exec_order_item set executed_at=?, executed_by=?
           where decision_id=? and executed_at is null""",
        (ts, executed_by, decision_id),
    )
    out = []
    for oid in order_ids:
        n = conn.execute(
            "select count(*) n, sum(executed_at is not null) done from exec_order_item "
            "where order_id=?", (oid,),
        ).fetchone()
        status = "done" if n["done"] == n["n"] else "partial"
        conn.execute("update exec_order set status=? where order_id=?", (status, oid))
        out.append({"order_id": oid, "status": status,
                    "executed": n["done"], "total": n["n"]})
    return out


@router.post("/executed", response_model=Ok)
def mark_executed(body: ExecutedIn):
    """用户改完后台回来点「已改」。归因窗从这一刻开始算。"""
    with write_conn() as conn:
        d = _fetch_decision(conn, body.decision_id)
        if d["status"] not in ("approved", "proposed"):
            raise HTTPException(
                409, f"#{body.decision_id} 当前状态 {d['status']}，只有已批的才能标已改")
        days = body.attribution_days or days_for(d["action_type"])
        now = _now()
        update_decision(
            conn, body.decision_id,
            status="executed",
            executed_at=_iso(now),
            executed_by=body.executed_by,
            executed_value=body.executed_value or d["proposed_value"],
            attribution_due_at=_iso(now + timedelta(days=days)),
        )
        orders = _close_exec_items(conn, body.decision_id, _iso(now), body.executed_by)
    return Ok(detail={
        "decision_id": body.decision_id,
        "status": "executed",
        "attribution_days": days,
        "attribution_due_at": _iso(now + timedelta(days=days)),
        # 回执带上执行单进度：前端据此把「已改」按钮换掉，不用再拉一次单子。
        "exec_orders": orders,
    })


@router.post("/question", response_model=Ok)
def answer_question(body: QuestionAnswerIn):
    """问题打钩。打钩即沉淀 —— 不留在这段对话里。

    四种动作分开落（原因词典 v1.3 第五节）：
      勾选   → 按 option_meta 的去向写 policy / param_override /
               observation_task / experiment
      划掉   → reason_excluded，同码同对象静默 90 天
      跳过   → agent_assumption（按默认假设走 + 14 天复核）
      未表态 → 什么都不写（未勾 ≠ 排除，下次照问）

    勾选仍写一条 reason_hit 留原话：策略表记「以后按什么办」，
    reason_hit 记「主人当时怎么说的」，这是两件事。
    """
    with write_conn() as conn:
        row = conn.execute(
            "select pack_json from memorial where run_id=?", (body.run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"run #{body.run_id} 的奏折不存在")
        pack = json.loads(row["pack_json"])
        q = next((x for x in (pack.get("questions") or [])
                  if x.get("id") == body.question_id), None)
        if not q:
            raise HTTPException(404, f"问题 {body.question_id} 不在 run #{body.run_id} 里")

        options = q.get("options") or []
        codes = q.get("reason_codes") or []
        metas = q.get("option_meta") or []
        target = body.object_id or q.get("object")

        def meta_of(idx: int) -> dict:
            """选项的机器可读去向。

            老奏折（还没升级出题器的 run）没有 option_meta，退化成只有
            reason_code —— 这时仍能写对表，只是参数值要靠 free_text 或默认值。
            """
            m = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
            out = dict(m)
            out.setdefault("reason_code", codes[idx] if idx < len(codes) else None)
            out["option_text"] = options[idx]
            return out

        for idx in list(body.selected) + list(body.excluded):
            if idx < 0 or idx >= len(options):
                raise HTTPException(422, f"选项下标 {idx} 越界")

        picked = [{"option_index": i, "option_text": options[i],
                   "reason_code": meta_of(i)["reason_code"]} for i in body.selected]
        crossed = [{"option_index": i, "option_text": options[i],
                    "reason_code": meta_of(i)["reason_code"]} for i in body.excluded]

        if not target and (picked or crossed or body.skipped):
            raise HTTPException(422, f"问题 {body.question_id} 没有对象，无处沉淀")

        now = _iso(_now())
        sediment: list[dict] = []

        if not body.skipped:
            # 1) 勾选 → 落到它该去的那张表（去向、参数值、到期日都是出题时算好的）
            for p in picked:
                rc = reason_routes.apply_answer(
                    conn, code=p["reason_code"], object_id=target,
                    option=meta_of(p["option_index"]), free_text=body.free_text,
                    question_id=body.question_id, run_id=body.run_id)
                sediment.append({"option_index": p["option_index"], **rc})
            # 2) 划掉 → reason_excluded：这个码在这个对象上静默 90 天
            for c in crossed:
                rc = reason_routes.exclude(
                    conn, object_id=target, code=c["reason_code"],
                    question_id=body.question_id)
                sediment.append({"option_index": c["option_index"], **rc})

        # 3) 原话留档。只有勾了/跳过的才进 reason_hit —— 划掉的不进，
        #    否则「划掉」会被算成「这个码又出现一次」，把跳过率算歪。
        for p in picked:
            conn.execute(
                """insert into reason_hit (object_id, reason_code, scope, evidence_text,
                                           created_at, answer_type, decision_id)
                   values (?,?,?,?,?,?,?)""",
                (target, p["reason_code"], body.question_id,
                 p["option_text"] + (f"｜{body.free_text}" if body.free_text else ""),
                 now, "skip" if body.skipped else "answer", None),
            )

        # 4) 整题跳过 → 登记假设 + 观察期，到期用数据验。未表态不写任何表。
        if body.skipped:
            sediment.append(reason_routes.assume(
                conn, object_id=target, question_id=body.question_id))

    return Ok(detail={
        "question_id": body.question_id,
        "picked": picked,
        "excluded": crossed,
        "skipped": body.skipped,
        # 每条钩子落到哪张表、哪个 id、什么时候到期 —— 前端据此显示「已沉淀」，
        # 也让「打钩没用」这种怀疑一查就破。
        "sediment": sediment,
        "note": None if (picked or crossed or body.skipped) else "没有表态，未写任何沉淀",
    })


@router.post("/exec-order", response_model=Ok)
def create_exec_order(body: ExecOrderIn):
    """把已批的建议生成一张执行单（可复制到广告后台照做）。

    只收 approved 状态的建议 —— 未经审批的东西不能出现在执行单上。
    """
    now = _now()
    with write_conn() as conn:
        rows = []
        for did in body.decision_ids:
            d = _fetch_decision(conn, did)
            if d["status"] != "approved":
                raise HTTPException(
                    409, f"#{did} 状态是 {d['status']}，只有 approved 能进执行单")
            if d.get("ticket_id"):
                raise HTTPException(
                    409, f"#{did} 已经在执行单 {d['ticket_id']} 里了，不要重复下单")
            rows.append(d)
        ticket_id = body.ticket_id
        if not ticket_id:
            # 序号取自增计数器，不是 count(*) 也不是 max(order_id)。
            # 为什么：count 和 max 在「只剩空表」时都回退到 0 —— 自测/清理删光单子后
            # 再跑一轮就又把 T20261119-001 发一次，而 decision_log.ticket_id 只存字符串，
            # 两张单同号之后再没法按单号分辨「这条决策在哪张单上」。
            # sqlite_sequence 记的是 AUTOINCREMENT 用过的最大值，删行也不回退，
            # 所以序号只会往上走，且等于这一单即将拿到的 order_id。
            seq_row = conn.execute(
                "select seq from sqlite_sequence where name='exec_order'").fetchone()
            last = seq_row["seq"] if seq_row else 0
            ticket_id = f"T{now:%Y%m%d}-{last + 1:03d}"
        elif conn.execute(
            "select 1 from exec_order where ticket_id=?", (ticket_id,)
        ).fetchone():
            # 调用方自带单号时必须自己保证唯一，否则同样出现「一单多号、一号多单」。
            raise HTTPException(409, f"执行单号 {ticket_id} 已存在，换一个或留空自动生成")
        cur = conn.execute(
            """insert into exec_order (ticket_id, run_id, created_at, created_by, status, note)
               values (?,?,?,?, 'open', ?)""",
            (ticket_id, body.run_id or rows[0]["run_id"], _iso(now),
             "owner", body.note),
        )
        order_id = cur.lastrowid
        for d in rows:
            conn.execute(
                """insert into exec_order_item (order_id, decision_id, campaign_name,
                                                entity_label, action_type, current_value, target_value)
                   values (?,?,?,?,?,?,?)""",
                (order_id, d["decision_id"], _campaign_label(conn, d["campaign_id"]),
                 d["entity_label"], d["action_type"], d["current_value"],
                 d["proposed_value"]),
            )
            update_decision(conn, d["decision_id"], ticket_id=ticket_id)
        items = [dict(r) for r in conn.execute(
            """select item_id, decision_id, campaign_name, entity_label, action_type,
                      current_value, target_value, executed_at
               from exec_order_item where order_id=? order by item_id""", (order_id,)
        )]
        # ticket 表是「一张单的摘要」（标题、条数、总影响金额），exec_order 是「这张单
        # 本身」。原来只有前者有 DDL、没有写入方 —— exec_order.ticket_id 的注释写着
        # 「关联 ticket.ticket_id」，可从来没有人往里写，于是「按单号查摘要」永远查空。
        # 在同一事务里补上：要么两张表都有这张单，要么都没有。
        _upsert_ticket(conn, ticket_id=ticket_id, order_id=order_id, rows=rows,
                       run_id=body.run_id or rows[0]["run_id"], now=now)
    return Ok(detail={
        "order_id": order_id,
        "ticket_id": ticket_id,
        "status": "open",
        "items": items,
        # 回执带上整张单：前端 `order.order?.ticket_id` 读的就是这个键，
        # 原来只回 detail 里的平铺字段，那一行永远是 undefined。
        "order": {
            "order_id": order_id,
            "ticket_id": ticket_id,
            "status": "open",
            "run_id": body.run_id or rows[0]["run_id"],
            "note": body.note,
        },
    })


def _campaign_label(conn, campaign_id) -> str:
    """执行单上写给人看的活动名。

    原来直接存 campaign_id 数字串，而 campaign_id 为 NULL 的行落成空串 ——
    结果是「无活动归属」的条目和某个活动名恰好为空的行挤进同一组，
    `groups` 分组时看不出这是两码事。
    """
    if not campaign_id:
        return "（无活动归属）"
    row = conn.execute(
        "select campaign_name from fact_campaign_daily where campaign_id=? "
        "and campaign_name is not null and campaign_name<>'' "
        "order by report_date desc limit 1", (str(campaign_id),)
    ).fetchone()
    name = (row["campaign_name"] if row else "") or ""
    return name or str(campaign_id)


def _upsert_ticket(conn, *, ticket_id: str, order_id: int, rows: list[dict],
                   run_id, now) -> None:
    """把这张执行单的摘要写进 ticket 表（幂等）。"""
    spend = sum(float(r.get("spend_impact") or 0) for r in rows)
    rules = sorted({r["rule_id"] for r in rows if r.get("rule_id")})
    title = (f"{len(rows)} 条 · " + "、".join(rules)) if rules else f"{len(rows)} 条"
    summary = "；".join(
        f'{r["entity_label"]} {r["current_value"]}→{r["proposed_value"]}' for r in rows[:6]
    )
    if len(rows) > 6:
        summary += f"…（共 {len(rows)} 条）"
    conn.execute(
        """insert into ticket (ticket_id, run_id, created_at, title, status,
                               rule_count, decision_count, spend_impact, summary)
           values (?,?,?,?, 'open', ?,?,?,?)
           on conflict(ticket_id) do update set
               run_id=excluded.run_id, title=excluded.title,
               rule_count=excluded.rule_count, decision_count=excluded.decision_count,
               spend_impact=excluded.spend_impact, summary=excluded.summary""",
        (ticket_id, run_id, _iso(now), title, len(rules), len(rows), round(spend, 2), summary),
    )


@router.get("/exec-order/{order_id}")
def get_exec_order(order_id: int):
    with read_conn() as conn:
        order = conn.execute(
            "select * from exec_order where order_id=?", (order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(404, f"执行单 {order_id} 不存在")
        items = conn.execute(
            """select i.*, d.confidence, d.spend_impact, d.rationale
               from exec_order_item i left join decision_log d using (decision_id)
               where i.order_id=? order by i.item_id""", (order_id,)
        ).fetchall()
    item_rows = [dict(r) for r in items]
    # 按活动分组：后台是按 campaign 一屏一屏改的，平铺列表切后台会来回跳。
    # 这里只加输出、不改 items 的形状（老读者照旧），分组按 item_id 首次出现排序，
    # 保序是为了让人照着单子的顺序改，而不是跳到别的活动再跳回来。
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for it in item_rows:
        key = it.get("campaign_name") or ""
        g = index.get(key)
        if g is None:
            g = {"campaign_name": key, "count": 0, "executed": 0, "item_ids": []}
            index[key] = g
            groups.append(g)
        g["count"] += 1
        g["item_ids"].append(it["item_id"])
        if it.get("executed_at"):
            g["executed"] += 1
    return Ok(detail={"order": dict(order), "items": item_rows, "groups": groups})


@router.get("/pending")
def pending():
    """待批队列：只回 proposed。工作台首屏靠这个。"""
    with read_conn() as conn:
        rows = conn.execute(
            """select decision_id, run_id, rule_id, entity_type, entity_id, entity_label,
                      action_type, confidence, current_value, proposed_value,
                      spend_impact, rationale, decided_at, expires_at
               from decision_log where status='proposed'
               order by abs(coalesce(spend_impact,0)) desc"""
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _record_reason(conn, d: dict, code: str, text: str | None) -> None:
    """打叉原因进 reason_hit —— 这是「为什么不动」的语料库，比「为什么动」更值钱。"""
    conn.execute(
        """insert into reason_hit (object_id, reason_code, scope, evidence_text,
                                   created_at, answer_type, decision_id)
           values (?,?,?,?,?,?,?)""",
        (d["entity_id"], code, d["rule_id"], text or code,
         _iso(_now()), "reject", d["decision_id"]),
    )


# 强制批准的理由下限。不是为了卡人，是为了让三个月后的自己能看懂
# 「当时为什么没等证据」—— 少于 8 个字基本等于没写。
OVERRIDE_MIN_REASON = 8


def _record_override(conn, d: dict, reason: str) -> None:
    """强制批准进 reason_hit，answer_type='override'。

    与打叉分开记：打叉是「我看过、不同意」，override 是「我知道证据不够、但我拍板」。
    混在一起会让原因词典把两种完全不同的判断算进同一个分母。
    """
    conn.execute(
        """insert into reason_hit (object_id, reason_code, scope, evidence_text,
                                   created_at, answer_type, decision_id)
           values (?,?,?,?,?,?,?)""",
        (d["entity_id"], "owner_override", d["rule_id"], reason,
         _iso(_now()), "override", d["decision_id"]),
    )


def _apply_observing(conn, d: dict, body: ApproveIn) -> dict:
    """证据不够的条目：只能「强制批准」或「确认不动」，没有第三条路。

    引擎的判断不变 —— 它仍然认为这条不够线，gate_reason 原样保留在库里。
    这里只做两件事：① 把人的决定与理由写进 override_*；
    ② 让它进入正常的「已批 → 已改 → 归因」链路。
    """
    did = body.decision_id
    if body.status == "approved":
        if not body.override:
            raise HTTPException(
                409,
                f"#{did} 证据不够（{d['gate_reason'] or '未达判定线'}），"
                f"引擎没有把它放进待批。要批就显式强制批准：override=true + 理由。",
            )
        reason = (body.override_reason or "").strip()
        if len(reason) < OVERRIDE_MIN_REASON:
            raise HTTPException(
                422,
                f"强制批准必须写理由（至少 {OVERRIDE_MIN_REASON} 个字）。"
                f"这是以后复盘「为什么当时没等证据」的唯一依据。",
            )
        now = _iso(_now())
        update_decision(
            conn, did,
            status="approved",
            checked_at=now,
            checked_by=body.checked_by,
            override_by=body.checked_by,
            override_reason=reason,
            overridden_at=now,
        )
        _record_override(conn, d, reason)
        return {
            "decision_id": did,
            "status": "approved",
            "overridden": True,
            "gate_reason": d["gate_reason"],
            "override_reason": reason,
        }

    # rejected：允许（等于「我看过了，就不动」），不需要 override。
    if not body.reject_reason_code:
        raise HTTPException(422, "打叉必须给原因码（reject_reason_code）")
    update_decision(
        conn, did,
        status="rejected",
        checked_at=_iso(_now()),
        checked_by=body.checked_by,
        reject_reason_code=body.reject_reason_code,
        reject_reason=body.reject_reason,
    )
    _record_reason(conn, d, body.reject_reason_code, body.reject_reason)
    return {"decision_id": did, "status": "rejected", "overridden": False}


def _apply_approval(conn, body: ApproveIn) -> dict:
    """一条审批的落库规则。approve_one 与 approve_batch 共用，避免两条路径走岔。"""
    did = body.decision_id
    d = _fetch_decision(conn, did)
    if d["status"] in ("executed", "expired"):
        raise HTTPException(409, f"#{did} 当前状态 {d['status']}，不能再改")
    if d["status"] == body.status:
        raise HTTPException(409, f"#{did} 已经是 {body.status}，不用再提交一次")

    if d["status"] == "observing":
        return _apply_observing(conn, d, body)

    if body.override:
        raise HTTPException(
            422,
            f"#{did} 的状态是 {d['status']}，不需要强制批准。"
            f"强制批准只用于「引擎说证据不够、但你决定现在就动」的条目。",
        )
    if body.status == "rejected" and not body.reject_reason_code:
        raise HTTPException(422, "打叉必须给原因码（reject_reason_code）")
    fields = {
        "status": body.status,
        "checked_at": _iso(_now()),
        "checked_by": body.checked_by,
    }
    if body.status == "rejected":
        fields["reject_reason_code"] = body.reject_reason_code
        fields["reject_reason"] = body.reject_reason
        _record_reason(conn, d, body.reject_reason_code, body.reject_reason)
    update_decision(conn, did, **fields)
    return {"decision_id": did, "status": body.status}