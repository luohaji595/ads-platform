"""奏折读取。工作台与 md 底稿同源于 memorial.pack_json。"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException

from api.db import read_conn

router = APIRouter()

# 与 adsdesk/agents/diagnose.py 的 STALE_RUN_MINUTES 保持一致。
# 这里是读侧的第二道保险：写侧的回收只在下次跑 agent 时才触发，
# 而工作台可能在两次跑之间被打开很多次。
STALE_RUN_MINUTES = 30


def _load_pack(row) -> dict | None:
    try:
        return json.loads(row["pack_json"])
    except Exception:
        return None


@router.get("/today")
def today():
    """最近一次运行（不按日期找，按 run 找 —— 重跑后日期可能不变但内容变了）。"""
    with read_conn() as conn:
        row = conn.execute(
            "select run_id, business_day, pack_json, report_path, agent_state, created_at "
            "from memorial order by run_id desc limit 1"
        ).fetchone()
        if not row:
            raise HTTPException(404, "还没有 memorial。先跑一次 engine/run.py")
        pack = _load_pack(row) or {}
        decisions = _decorate(conn, pack)
        agent = _agent_view(conn, row["run_id"])
    return {
        "run_id": row["run_id"],
        "business_day": row["business_day"],
        "report_path": row["report_path"],
        "agent_state": row["agent_state"],
        "created_at": row["created_at"],
        "pack": pack,
        "decisions": decisions,
        "agent": agent,
    }


@router.get("/list")
def list_runs(limit: int = 30):
    with read_conn() as conn:
        rows = conn.execute(
            "select run_id, business_day, created_at, agent_state, length(pack_json) as bytes, report_path "
            "from memorial order by run_id desc limit ?", (max(1, min(limit, 200)),)
        ).fetchall()
    return {"runs": [dict(r) for r in rows]}


@router.get("/{run_id}")
def get_run(run_id: int):
    with read_conn() as conn:
        row = conn.execute(
            "select run_id, business_day, pack_json, report_path, agent_state, created_at "
            "from memorial where run_id=?", (run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"run #{run_id} 不存在")
        pack = _load_pack(row) or {}
        decisions = _decorate(conn, pack)
        agent = _agent_view(conn, row["run_id"])
    return {
        "run_id": row["run_id"],
        "business_day": row["business_day"],
        "report_path": row["report_path"],
        "agent_state": row["agent_state"],
        "created_at": row["created_at"],
        "pack": pack,
        "decisions": decisions,
        "agent": agent,
    }


def _agent_view(conn, run_id: int) -> dict:
    """这个 run 上诊断 agent 说了什么。

    只读 agent_run / agent_eval / agent_objection 三张表 —— agent 的输出从不写
    decision_log，所以这里不存在「agent 的建议」这种东西，只有「agent 的反对」和
    「agent 的疑问」。
    """
    # 先找这个 run 上的 agent 记录；找不到就退到「同一个业务日里最近的一次」。
    #
    # 为什么必须退：引擎每次重跑都会造一个新的 memorial.run_id（调一次参数就 +1），
    # 而 agent 是单独跑的、绑定旧 run_id。不退一步的话，只要引擎重跑过一次，
    # 工作台就会显示「诊断 agent 没跑过」—— 明明十分钟前刚跑完、输出都在库里。
    # 退的时候必须**标明它based on 哪个 run**，否则会让人以为它评的是这一版输入。
    row = conn.execute(
        """select agent_run_id, run_id, model, prompt_ver, started_at, finished_at, status,
                  tokens_in, tokens_out, input_digest, output_json, error
           from agent_run where job='diagnose' and run_id=?
           order by agent_run_id desc limit 1""",
        (run_id,),
    ).fetchone()
    based_on_run = run_id
    fallback = False
    if not row:
        row = conn.execute(
            """select a.agent_run_id, a.run_id, a.model, a.prompt_ver, a.started_at,
                      a.finished_at, a.status, a.tokens_in, a.tokens_out,
                      a.input_digest, a.output_json, a.error
               from agent_run a
               join memorial m on m.run_id = a.run_id
               join memorial cur on cur.run_id = ?
              where a.job='diagnose' and m.business_day = cur.business_day
              order by a.agent_run_id desc limit 1""",
            (run_id,),
        ).fetchone()
        if row:
            based_on_run = row["run_id"]
            fallback = True
    if not row:
        return {"state": "never_ran"}

    out = {}
    try:
        out = json.loads(row["output_json"] or "{}")
    except Exception:
        out = {}

    evals = [
        dict(r) for r in conn.execute(
            """select eval_id, claim, verifiable_at, method, expected, verdict, actual
               from agent_eval where agent_run_id=? order by eval_id""",
            (row["agent_run_id"],),
        )
    ]
    objections = [
        dict(r) for r in conn.execute(
            """select objection_id, decision_id, verdict, why, evidence
               from agent_objection where agent_run_id=? order by objection_id""",
            (row["agent_run_id"],),
        )
    ]
    # running 不能一律当 failed：刚启动 30 秒的 agent 是正常的。
    # 但也不能一律当 running：进程被 kill -9 或机器休眠冻结时不会走 except 分支，
    # 记录会永远停在 running。判据用墙钟 —— 比 gateway 的 600s 硬截止宽 3 倍，
    # 超过这个点还在 running 的，就是进程已经死了。
    status = row["status"]
    stale = False
    if status == "running":
        try:
            started = datetime.fromisoformat(row["started_at"])
            stale = (datetime.now(started.tzinfo) - started).total_seconds() > STALE_RUN_MINUTES * 60
        except Exception:
            stale = True
        status = "failed" if stale else "running"

    # 兑现率跨全部 run 统计，不是只统计这一轮 —— 「这个 agent 准不准」是累积问题，
    # 只看今天的四条断言，样本永远是 0。
    # 分母只算 correct + wrong：unverifiable 是「判不了」（窗口没走完、数据没追平），
    # 把它算进分母等于拿天气赖账；pending 还没到判定日，更不该算。
    sc = conn.execute(
        """select
             sum(case when verdict='correct' then 1 else 0 end) as correct,
             sum(case when verdict='wrong'   then 1 else 0 end) as wrong,
             sum(case when verdict='unverifiable' then 1 else 0 end) as unverifiable,
             sum(case when verdict='pending' then 1 else 0 end) as pending,
             max(case when verdict in ('correct','wrong','unverifiable')
                      then checked_at else null end) as last_checked
           from agent_eval"""
    ).fetchone()
    c_n = sc["correct"] or 0
    w_n = sc["wrong"] or 0
    u_n = sc["unverifiable"] or 0
    judged = c_n + w_n
    # 可验证率 = 判出对错的 / 所有真的进过判定的。
    # 为什么单列这条：unverifiable 不是「它错了」，是「它把断言写成了没法验的形式」——
    # 实测把 --as-of 推到 2026-12-31，39 条到期里 36 条判不了。这个数比「准不准」更早暴露问题。
    touched = judged + u_n
    score = {
        "correct": c_n,
        "wrong": w_n,
        "judged": judged,
        "unverifiable": u_n,
        "verifiable": touched,
        "verifiable_rate": (judged / touched) if touched else None,
        "pending": sc["pending"] or 0,
        # 一条都还没判出来时返回 None —— 返回 0.0 会被读成「全错」，
        # 返回 1.0 会被读成「全对」，两个都是撒谎。
        "rate": (c_n / judged) if judged else None,
        "last_checked": sc["last_checked"],
    }

    return {
        "state": "ok" if status == "ok" else ("running" if status == "running" else "failed"),
        "score": score,
        "agent_run_id": row["agent_run_id"],
        # fallback=True 表示这条是「同一天、更早那个 run」上跑的，输入可能和这一版不同
        "based_on_run": based_on_run,
        "fallback": fallback,
        "model": row["model"],
        "prompt_ver": row["prompt_ver"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "tokens": [row["tokens_in"], row["tokens_out"]],
        "input_digest": row["input_digest"],
        "error": (row["error"]
                  or (f"超过 {STALE_RUN_MINUTES} 分钟仍停在 running，"
                      f"进程可能已被 kill 或机器休眠冻结（agent_run #{row['agent_run_id']}）"
                      if stale
                      else ("标记为失败但没有写入错误信息 —— "
                            f"看 agent_run #{row['agent_run_id']} 的日志"
                            if status == "failed" else None))),
        "questions": out.get("questions") or [],
        "objections": objections,
        "evals": evals,
    }


def _decorate(conn, pack: dict) -> list[dict]:
    """把 pack 里的建议与库里的审批状态、agent 反对合并成一张待批表。

    为什么必须回库：pack_json 是运行时的快照，审批状态会变（打钩、执行、过期），
    只读 pack 会看到「已经批过的还在那儿等着批」。

    observing / below 也要装饰：它们现在有 decision_id（引擎把「证据不够」
    也记进了账本），人可以显式强制批准，所以必须带上 override 的四个字段，
    否则前端点完「强制批准」刷新回来还是原样。
    """
    # alerts 也要装饰：账户级告警不能打钩，但 agent 可以对它说 doubt，
    # 工作台得把那条 doubt 显示出来，否则反对意见落库了没人看得见。
    front = ((pack.get("front") or []) + (pack.get("alerts") or [])
             + (pack.get("observing") or []) + (pack.get("below") or []))
    ids = [d.get("decision_id") for d in front if d.get("decision_id")]
    meta = {}
    objections = {}
    if ids:
        qs = ",".join("?" * len(ids))
        for r in conn.execute(
            f"""select decision_id, status, checked_at, checked_by,
                       reject_reason, reject_reason_code,
                       executed_at, executed_value, executed_by,
                       attribution_due_at, outcome_7d_json, outcome_14d_json, outcome_filled_at,
                       gate_reason, override_by, override_reason, overridden_at
                from decision_log where decision_id in ({qs})""", ids
        ):
            meta[r["decision_id"]] = dict(r)
        for r in conn.execute(
            f"""select decision_id, verdict, why, evidence, agent_run_id, created_at
                from agent_objection where decision_id in ({qs})
                order by objection_id""", ids
        ):
            objections.setdefault(r["decision_id"], []).append(dict(r))

    out = []
    for d in front:
        did = d.get("decision_id")
        m = meta.get(did) or {}
        out.append({
            **d,
            # 库里没有这一行（例如「已批先观察」被挪走的，本轮不落库）时，
            # 回落到 pack 自己带的 status —— 否则会把「观察期内」误标成待批。
            "status": m.get("status") or d.get("status") or "proposed",
            "checked_at": m.get("checked_at"),
            "checked_by": m.get("checked_by"),
            "reject_reason": m.get("reject_reason"),
            "reject_reason_code": m.get("reject_reason_code"),
            "executed_at": m.get("executed_at"),
            "executed_value": m.get("executed_value"),
            "executed_by": m.get("executed_by"),
            "attribution_due_at": m.get("attribution_due_at"),
            "outcome_7d": m.get("outcome_7d_json"),
            "outcome_14d": m.get("outcome_14d_json"),
            "outcome_filled_at": m.get("outcome_filled_at"),
            "gate_reason": m.get("gate_reason") or d.get("gate_reason"),
            "override_by": m.get("override_by"),
            "override_reason": m.get("override_reason"),
            "overridden_at": m.get("overridden_at"),
            "objections": objections.get(did) or [],
        })
    return out