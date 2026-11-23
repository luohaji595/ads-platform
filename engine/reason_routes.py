"""原因码 → 四类沉淀去向（原因词典 v1.3 第五节）。

这张表是「打钩之后写哪张表、写什么、多久失效」的唯一真源：

    阻塞 → policy            人明确表态「就这么办」，到期自动复核
    校准 → param_override    覆盖某个参数的默认值（target_acos / max_budget_step）
    归因 → observation_task  观察期 7–14 天，到期拿数据判成立/不成立
    探索 → experiment        带对照组的实验，到期比较实验组与对照组

另有三个不留沉淀的去向：

    reason_hit        只登记「你说过这句话」（其他 / 以上都不是）
    reason_excluded   显式划掉：同 scope 内同码 90 天不再问
    agent_assumption  整题跳过：按默认假设走 + 观察期 + 到期复核

写侧是 adsdesk/api/routes/approve.py（打钩那一刻）；
读侧有三处：
    engine/report.py     出题前查「这个码在有效期内吗」，有效期内不再问
    engine/run.py        读出 param_override，把 target_acos / 预算步长换掉
    adsdesk/agents/context.py  把生效的沉淀喂给诊断 agent

两张表没有 reason_code 列（policy / param_override），所以：
    policy   把 reason_code 塞在 params_json 里（本模块写、本模块读）
    param_override 靠 PARAM_CODE 反查（param_key ↔ reason_code 本就一对一）

日期口径：expires_at / due_at / ends_at 一律写 `YYYY-MM-DD`，与「今天」直接
字符串比较；created_at 写带时区的秒级时间戳。rules.py 里 Decision.expires_at
是「几小时后过期」的时序语义，与沉淀表的「哪天复核」不是一回事，不要混用。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))

# 显式划掉的静默期、整题跳过的复核期（天）
EXCLUDE_DAYS = 90
SKIP_REVIEW_DAYS = 14

# param_key ↔ reason_code 的一对一映射（param_override 表没有 reason_code 列）
PARAM_CODE = {
    "target_acos": "goal_relax_acos",
    "max_budget_step": "budget_raise_smaller",
}


# ----------------------------------------------------------------------
# 路由表
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """一个原因码的去向。"""
    table: str                      # policy / param_override / observation_task / experiment / reason_hit
    scope: str = "object"           # object / family / account
    ttl_days: int = 90              # policy / param_override 的有效期，observation / experiment 的观察期
    policy_type: str = ""
    param_key: str = ""
    metric: str = ""
    hypothesis: str = ""
    judge_rule: str = ""


# 中文标签只用于报告与证据串，不进机器判断
ROUTES: dict[str, Route] = {
    # ---- Q08 目标 ACOS 不可达 -------------------------------------------
    "goal_keep_acos30_control_loss": Route(
        table="policy", scope="account", ttl_days=90,
        policy_type="acos_cap_accept_loss",
    ),
    "goal_relax_acos": Route(
        table="param_override", scope="account", ttl_days=90,
        param_key="target_acos",
    ),
    "goal_product_side_problem": Route(
        table="observation_task", scope="object", ttl_days=14, metric="cvr",
        hypothesis="转化率不足的瓶颈在主图/价格/落地页，不在出价",
        judge_rule="到期窗口 CVR ≥ 盈亏平衡 CVR 即成立（外围改对了）；"
                   "仍低于基线 → 不成立（问题在词或出价）",
    ),
    "goal_other": Route(table="reason_hit"),

    # ---- Q01 无单词 / 地板词 / R01 --------------------------------------
    "kw_mismatch_ok_to_pause": Route(
        table="policy", scope="object", ttl_days=90,
        policy_type="allow_pause_no_orders",
    ),
    "kw_ok_listing_problem": Route(
        table="observation_task", scope="object", ttl_days=14, metric="cvr",
        hypothesis="词本身没问题，是主图/价格/落地页把转化拉下来了",
        judge_rule="到期该词 CVR ≥ 盈亏平衡 CVR 即成立；仍为 0 单 → 不成立",
    ),
    "kw_sample_noise_observe": Route(
        table="observation_task", scope="object", ttl_days=7, metric="clicks,orders",
        hypothesis="这几十击的 0 单是样本波动，不是真差",
        judge_rule="攒够判定线点击数后重判：出单且 ACOS ≤ 目标 → 成立",
    ),
    "kw_other": Route(table="reason_hit"),

    # ---- Q03 加预算 ------------------------------------------------------
    "budget_raise_as_proposed": Route(
        table="policy", scope="object", ttl_days=90,
        policy_type="allow_raise_budget",
    ),
    "budget_raise_smaller": Route(
        table="param_override", scope="account", ttl_days=90,
        param_key="max_budget_step",
    ),
    "budget_hold_observe": Route(
        table="observation_task", scope="object", ttl_days=14,
        metric="roas,budget_util",
        hypothesis="现在按 20% 加预算会把钱花在还没验证的边际上",
        judge_rule="到期 ROAS 仍 > 账户 ROAS 且预算使用率仍 >85% → 不成立（该加）；"
                   "否则成立（不加是对的）",
    ),
    "budget_other": Route(table="reason_hit"),

    # ---- Q04 位次加价 ----------------------------------------------------
    "placement_apply": Route(
        table="policy", scope="object", ttl_days=90,
        policy_type="allow_placement_change",
    ),
    "placement_hold_observe": Route(
        table="observation_task", scope="object", ttl_days=14,
        metric="acos,top_of_search_share",
        hypothesis="位次加价现在不该动，先把数据攒厚",
        judge_rule="到期该位次 ACOS 已超目标 → 不成立（该压）；否则成立",
    ),
    "placement_other": Route(table="reason_hit"),

    # ---- Q05 探索（唯一通向 experiment 的一类）--------------------------
    "explore_run_experiment": Route(
        table="experiment", scope="object", ttl_days=14, metric="roas,cpa",
        hypothesis="按最小步长改，效果会比按建议幅度改更划算",
        judge_rule="到期实验组 ROAS/CPA 优于对照组 ≥20% → 成立；"
                   "差距 <20% → 不成立（直接按建议幅度做）",
    ),
    "explore_hold": Route(
        table="observation_task", scope="object", ttl_days=14, metric="clicks,orders",
        hypothesis="证据还不够开实验，先继续攒",
        judge_rule="到期攒够判定线点击 → 不成立（该做实验了）；否则成立",
    ),
    "explore_other": Route(table="reason_hit"),
}


def spec(code: str | None) -> Route | None:
    """取一个原因码的去向；未知码返回 None（当作只进 reason_hit）。"""
    if not code:
        return None
    return ROUTES.get(code)


def destination(code: str | None) -> str:
    """出题时写进 pack 的机器可读去向（approve.py 据此分流，不用再猜）。"""
    r = spec(code)
    return r.table if r else "reason_hit"


# ----------------------------------------------------------------------
# 时间与 JSON 小工具
# ----------------------------------------------------------------------

def _today(as_of: date | datetime | str | None = None) -> str:
    if as_of is None:
        d = datetime.now(CST).date()
    elif isinstance(as_of, datetime):
        d = as_of.date()
    elif isinstance(as_of, date):
        d = as_of
    else:
        d = date.fromisoformat(str(as_of)[:10])
    return d.isoformat()


def _plus(days: int, as_of: date | datetime | str | None = None) -> str:
    base = date.fromisoformat(_today(as_of))
    return (base + timedelta(days=days)).isoformat()


def _stamp(as_of: date | datetime | str | None = None) -> str:
    if isinstance(as_of, datetime):
        return as_of.isoformat(timespec="seconds")
    return datetime.now(CST).isoformat(timespec="seconds")


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(text: str | None) -> float | None:
    """从人的手填话里捞一个数：「放宽到 45%」「0.35」「40 个点」。
    百分号按 /100 处理；>1 的数当百分数（45 → 0.45），<=1 的数原样。"""
    if not text:
        return None
    m = _NUM.search(text.replace("％", "%"))
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    if v <= 0:
        return None
    if "%" in text:
        return round(v / 100.0, 4)
    if v > 1:
        return round(v / 100.0, 4) if v <= 100 else None
    return round(v, 4)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


# ----------------------------------------------------------------------
# 生效判定（读侧「有效期内不再问」都走这里）
# ----------------------------------------------------------------------

def _policy_rows(conn: sqlite3.Connection, object_id: str, today: str) -> list[dict]:
    rows = conn.execute(
        """select policy_id, scope, object_id, policy_type, params_json,
                  source, evidence_text, status, created_at, expires_at
           from policy
           where object_id=? and status='active'
             and (expires_at is null or expires_at >= ?)
           order by policy_id""", (object_id, today),
    ).fetchall()
    return [dict(r) for r in rows]


def _param_rows(conn: sqlite3.Connection, object_id: str, today: str) -> list[dict]:
    rows = conn.execute(
        """select override_id, scope, object_id, param_key, param_value,
                  source, evidence_text, created_at, expires_at
           from param_override
           where object_id=?
             and (expires_at is null or expires_at >= ?)
           order by override_id""", (object_id, today),
    ).fetchall()
    return [dict(r) for r in rows]


def _task_rows(conn: sqlite3.Connection, object_id: str | None, today: str) -> list[dict]:
    sql = """select task_id, object_id, reason_code, hypothesis, metric, judge_rule,
                    created_at, due_at, result, resolved_at
             from observation_task
             where result is null and due_at >= ?"""
    args: list[Any] = [today]
    if object_id:
        sql += " and object_id=?"
        args.append(object_id)
    return [dict(r) for r in conn.execute(sql + " order by task_id", args).fetchall()]


def _exp_rows(conn: sqlite3.Connection, object_id: str | None, today: str) -> list[dict]:
    sql = """select experiment_id, object_id, reason_code, hypothesis, change_json,
                    control_json, metric, created_at, ends_at, result, evidence_level
             from experiment
             where result is null and ends_at >= ?"""
    args: list[Any] = [today]
    if object_id:
        sql += " and object_id=?"
        args.append(object_id)
    return [dict(r) for r in conn.execute(sql + " order by experiment_id", args).fetchall()]


# param_key → reason_code 就是 PARAM_CODE 本身的方向，不需要再翻一次。
# 这里起一个别名只是让调用处的意图（"从这个参数反查它属于哪个原因码"）读得出来。
_CODE_FROM_PARAM = dict(PARAM_CODE)


def active_codes(conn: sqlite3.Connection, object_id: str,
                 as_of: date | datetime | str | None = None) -> dict[str, dict]:
    """这个对象上「还在有效期内」的原因码 → 生效凭据。

    出题侧用它做去重：同一个码还在有效期内，就不再拿同样的题问第二遍。
    也用于把「已沉淀过什么」写进奏折的备注里。
    """
    today = _today(as_of)
    out: dict[str, dict] = {}

    for r in _policy_rows(conn, object_id, today):
        params = _loads(r.get("params_json"))
        code = params.get("reason_code")
        if not code:
            code = f"policy:{r['policy_type']}"
        out.setdefault(code, {
            "table": "policy", "id": r["policy_id"], "until": r.get("expires_at"),
            "policy_type": r["policy_type"], "detail": r.get("evidence_text"),
        })

    for r in _param_rows(conn, object_id, today):
        code = _CODE_FROM_PARAM.get(r["param_key"]) or f"param:{r['param_key']}"
        out[code] = {
            "table": "param_override", "id": r["override_id"],
            "until": r.get("expires_at"), "param_key": r["param_key"],
            "param_value": r["param_value"], "detail": r.get("evidence_text"),
        }

    for r in _task_rows(conn, object_id, today):
        out.setdefault(r["reason_code"], {
            "table": "observation_task", "id": r["task_id"], "until": r["due_at"],
            "metric": r["metric"], "detail": r["hypothesis"],
        })

    for r in _exp_rows(conn, object_id, today):
        out.setdefault(r["reason_code"], {
            "table": "experiment", "id": r["experiment_id"], "until": r["ends_at"],
            "metric": r["metric"], "detail": r["hypothesis"],
        })

    return out


def excluded(conn: sqlite3.Connection, object_id: str,
             as_of: date | datetime | str | None = None) -> set[str]:
    """还没出静默期的「显式划掉」原因码。"""
    return set(excluded_until(conn, object_id, as_of))


def excluded_until(conn: sqlite3.Connection, object_id: str,
                   as_of: date | datetime | str | None = None) -> dict[str, str]:
    """划掉的原因为码 → 静默到哪天（给前端显示「到 X 之前不会再问」）。"""
    today = _today(as_of)
    rows = conn.execute(
        """select reason_code, max(expires_at) as until from reason_excluded
           where object_id=? and expires_at >= ? group by reason_code""",
        (object_id, today),
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if isinstance(r, sqlite3.Row):
            out[r["reason_code"]] = r["until"]
        else:
            out[r[0]] = r[1]
    return out


# ----------------------------------------------------------------------
# 写侧
# ----------------------------------------------------------------------

def _option_scope(route: Route, option: dict | None) -> str:
    if option and option.get("scope"):
        return str(option["scope"])
    return route.scope


def _option_object(route: Route, option: dict | None, object_id: str) -> str:
    """账户级沉淀落在账户对象上（scope.SCOPE_ASIN），否则落被打钩的那个对象。"""
    if option and option.get("object_id"):
        return str(option["object_id"])
    return str(object_id)


# 同一个「数」在不同参数上意思不一样：max_budget_step 存的是乘数（1.1），
# 而人在依据里写的是幅度（「只加 5%」）。这一步把幅度换成乘数，
# 不做的话「只加 5%」会被原样存成 0.05，下次加预算反而是把预算砍掉 95%。
_ADDITIVE = {"max_budget_step"}


def _explicit_ratio(text: str | None) -> float | None:
    """人「明确写了比例」才采信手填数值：带 % 或写了小数（0.45）或「…个点」。

    为什么不能见数就收：人话里的数字多数不是参数 ——「先查主图，2 周后再说」
    里的 2 会被 parse_number 读成 2%。宁可退回 pack 的默认值，也不能把
    「2 周」写成「目标 ACOS 2%」。
    """
    if not text:
        return None
    raw = text.replace("％", "%")
    if "%" in raw:
        return parse_number(raw)
    if re.search(r"(?<![\d.])\d*\.\d+(?![\d])", raw):
        return parse_number(raw)
    if "个点" in raw or "个百分点" in raw:
        return parse_number(raw)
    return None


def _value_for(route: Route, option: dict | None, free_text: str | None,
               key: str | None = None) -> str | None:
    """参数覆盖的取值：手填数值 > pack 带的默认值（人写的更权威）。

    手填只在「明确是比例」时才算数（见 _explicit_ratio）；否则用 pack 默认。
    """
    n = _explicit_ratio(free_text)
    if n is not None:
        if key in _ADDITIVE and n < 1:
            n = round(1.0 + n, 4)
        return str(n)
    if option and option.get("param_value") not in (None, ""):
        return str(option["param_value"])
    return None


def _policy(conn, route, code, object_id, scope, option, free_text, question_id,
            run_id, as_of, today) -> dict:
    ptype = (option or {}).get("policy_type") or route.policy_type
    if not ptype:
        raise ValueError(f"原因码 {code} 是 policy 去向，但没有 policy_type")
    until = _plus(route.ttl_days, as_of)
    params = {
        "reason_code": code,
        "question_id": question_id,
        "run_id": run_id,
        "option": (option or {}).get("option_text"),
    }
    for k in ("target_acos", "current_value", "proposed_value", "max_budget_step"):
        if option and k in option:
            params[k] = option[k]
    if free_text:
        params["free_text"] = free_text
    evidence = (option or {}).get("option_text") or code
    if free_text:
        evidence = f"{evidence}｜{free_text}"
    source = f"打钩#{question_id}@run{run_id}"

    # 同键在有效期内 → 刷新，不再堆一行（否则打三次钩会留下三条 active 互相打架）
    hit = conn.execute(
        """select policy_id from policy
           where object_id=? and policy_type=? and status='active'
             and (expires_at is null or expires_at >= ?)
           order by policy_id desc limit 1""", (object_id, ptype, today),
    ).fetchone()
    if hit:
        conn.execute(
            """update policy set scope=?, params_json=?, source=?, evidence_text=?,
                      expires_at=?, reviewed_at=?
               where policy_id=?""",
            (scope, _dumps(params), source, evidence, until, _stamp(as_of), hit[0]),
        )
        return {"table": "policy", "id": hit[0], "until": until, "replaced": True,
                "policy_type": ptype}
    cur = conn.execute(
        """insert into policy (scope, object_id, policy_type, params_json, source,
                               evidence_text, status, created_at, expires_at)
           values (?,?,?,?,?,?, 'active', ?, ?)""",
        (scope, object_id, ptype, _dumps(params), source, evidence, _stamp(as_of), until),
    )
    return {"table": "policy", "id": cur.lastrowid, "until": until, "replaced": False,
            "policy_type": ptype}


def _param_override(conn, route, code, object_id, scope, option, free_text, question_id,
                    run_id, as_of, today) -> dict:
    key = (option or {}).get("param_key") or route.param_key
    if not key:
        raise ValueError(f"原因码 {code} 是 param_override 去向，但没有 param_key")
    value = _value_for(route, option, free_text, key)
    if value is None:
        # 人打了钩但没给数：不猜，登记一条「等数值」的观察任务，钩子不丢
        return _observation_task(
            conn, Route(table="observation_task", scope=scope, ttl_days=7,
                        metric="param_value",
                        hypothesis=f"主人同意覆盖 {key}，但没写下数值",
                        judge_rule=f"补齐 {key} 的数值后改写成 param_override；"
                                   f"到期仍无数值 → 该覆盖作废"),
            code, object_id, scope, None, free_text, question_id, run_id, as_of, today,
        )
    until = _plus(route.ttl_days, as_of)
    evidence = (option or {}).get("option_text") or f"{key}={value}"
    if free_text:
        evidence = f"{evidence}｜{free_text}"
    source = f"打钩#{question_id}@run{run_id}"
    hit = conn.execute(
        """select override_id from param_override
           where object_id=? and param_key=?
             and (expires_at is null or expires_at >= ?)
           order by override_id desc limit 1""", (object_id, key, today),
    ).fetchone()
    if hit:
        conn.execute(
            """update param_override set scope=?, param_value=?, source=?,
                      evidence_text=?, expires_at=?
               where override_id=?""",
            (scope, value, source, evidence, until, hit[0]),
        )
        return {"table": "param_override", "id": hit[0], "until": until,
                "replaced": True, "param_key": key, "param_value": value}
    cur = conn.execute(
        """insert into param_override (scope, object_id, param_key, param_value,
                                       source, evidence_text, created_at, expires_at)
           values (?,?,?,?,?,?,?,?)""",
        (scope, object_id, key, value, source, evidence, _stamp(as_of), until),
    )
    return {"table": "param_override", "id": cur.lastrowid, "until": until,
            "replaced": False, "param_key": key, "param_value": value}


def _observation_task(conn, route, code, object_id, scope, option, free_text,
                      question_id, run_id, as_of, today) -> dict:
    metric = (option or {}).get("metric") or route.metric or "clicks,orders"
    hyp = (option or {}).get("hypothesis") or route.hypothesis or code
    judge = (option or {}).get("judge_rule") or route.judge_rule or "到期人工复核"
    days = int((option or {}).get("ttl_days") or route.ttl_days or 14)
    due = _plus(days, as_of)
    hit = conn.execute(
        """select task_id from observation_task
           where object_id=? and reason_code=? and result is null and due_at >= ?
           order by task_id desc limit 1""", (object_id, code, today),
    ).fetchone()
    if hit:
        conn.execute(
            """update observation_task set hypothesis=?, metric=?, judge_rule=?, due_at=?
               where task_id=?""", (hyp, metric, judge, due, hit[0]),
        )
        return {"table": "observation_task", "id": hit[0], "until": due,
                "replaced": True, "metric": metric}
    cur = conn.execute(
        """insert into observation_task (object_id, reason_code, hypothesis, metric,
                                         judge_rule, created_at, due_at)
           values (?,?,?,?,?,?,?)""",
        (object_id, code, hyp, metric, judge, _stamp(as_of), due),
    )
    return {"table": "observation_task", "id": cur.lastrowid, "until": due,
            "replaced": False, "metric": metric}


def _experiment(conn, route, code, object_id, scope, option, free_text,
                question_id, run_id, as_of, today) -> dict:
    option = option or {}
    change = option.get("change")
    control = option.get("control")
    if not change or not control:
        # 没有对照组就不是实验，只登记观察任务（对照是实验的底线）
        return _observation_task(
            conn, Route(table="observation_task", scope=scope, ttl_days=route.ttl_days,
                        metric=route.metric,
                        hypothesis="想试但凑不出对照组，先按观察处理",
                        judge_rule="找到可对照的同组活动后再开实验"), code, object_id,
            scope, option, free_text, question_id, run_id, as_of, today,
        )
    metric = option.get("metric") or route.metric or "roas,cpa"
    hyp = option.get("hypothesis") or route.hypothesis or code
    days = int(option.get("ttl_days") or route.ttl_days or 14)
    ends = _plus(days, as_of)
    hit = conn.execute(
        """select experiment_id from experiment
           where object_id=? and reason_code=? and result is null and ends_at >= ?
           order by experiment_id desc limit 1""", (object_id, code, today),
    ).fetchone()
    if hit:
        conn.execute(
            """update experiment set hypothesis=?, change_json=?, control_json=?,
                      metric=?, ends_at=? where experiment_id=?""",
            (hyp, _dumps(change), _dumps(control), metric, ends, hit[0]),
        )
        return {"table": "experiment", "id": hit[0], "until": ends,
                "replaced": True, "metric": metric}
    cur = conn.execute(
        """insert into experiment (object_id, reason_code, hypothesis, change_json,
                                   control_json, metric, created_at, ends_at)
           values (?,?,?,?,?,?,?,?)""",
        (object_id, code, hyp, _dumps(change), _dumps(control), metric,
         _stamp(as_of), ends),
    )
    return {"table": "experiment", "id": cur.lastrowid, "until": ends,
            "replaced": False, "metric": metric}


def apply_answer(conn: sqlite3.Connection, *,
                 code: str | None,
                 object_id: str,
                 option: dict | None = None,
                 free_text: str | None = None,
                 question_id: str = "",
                 run_id: int = 0,
                 as_of: date | datetime | str | None = None) -> dict:
    """把一次打钩落到它该去的那张表，返回回执（给前端显示「已沉淀到哪里」）。

    option 是 pack 里那个选项的字典，字段由 engine/report.py 出题时算好：
        destination / scope / object_id / param_key / param_value / ttl_days /
        metric / judge_rule / hypothesis / change / control / option_text
    这样审批侧不需要重算业务口径 —— 它只负责「把奏折里写的写进库」。
    """
    route = spec(code)
    if route is None:
        return {"table": None, "code": code, "note": "未知原因码，只登记 reason_hit"}

    today = _today(as_of)
    scope = _option_scope(route, option)
    target = _option_object(route, option, object_id)

    if route.table == "policy":
        return _policy(conn, route, code, target, scope, option, free_text,
                       question_id, run_id, as_of, today)
    if route.table == "param_override":
        return _param_override(conn, route, code, target, scope, option, free_text,
                               question_id, run_id, as_of, today)
    if route.table == "observation_task":
        return _observation_task(conn, route, code, target, scope, option, free_text,
                                 question_id, run_id, as_of, today)
    if route.table == "experiment":
        return _experiment(conn, route, code, target, scope, option, free_text,
                           question_id, run_id, as_of, today)
    return {"table": None, "code": code, "note": "只登记 reason_hit"}


def exclude(conn: sqlite3.Connection, *, object_id: str, code: str | None,
            question_id: str = "", as_of: date | datetime | str | None = None,
            days: int = EXCLUDE_DAYS) -> dict:
    """显式划掉：这个码在这个对象上静默 N 天（不过期就自动恢复追问题）。"""
    if not code:
        return {"table": None, "code": None, "note": "没有原因码可划掉"}
    today = _today(as_of)
    until = _plus(days, as_of)
    hit = conn.execute(
        """select excluded_id from reason_excluded
           where object_id=? and reason_code=? and expires_at >= ?""",
        (object_id, code, today),
    ).fetchone()
    if hit:
        conn.execute("update reason_excluded set expires_at=? where excluded_id=?",
                     (until, hit[0]))
        return {"table": "reason_excluded", "id": hit[0], "until": until,
                "replaced": True, "question_id": question_id}
    cur = conn.execute(
        """insert into reason_excluded (object_id, reason_code, created_at, expires_at)
           values (?,?,?,?)""", (object_id, code, _stamp(as_of), until),
    )
    return {"table": "reason_excluded", "id": cur.lastrowid, "until": until,
            "replaced": False, "question_id": question_id}


def assume(conn: sqlite3.Connection, *, object_id: str, question_id: str,
           default_action: str = "按 agent 默认假设执行",
           as_of: date | datetime | str | None = None,
           days: int = SKIP_REVIEW_DAYS) -> dict:
    """整题跳过：登记假设 + 观察期，到期必须复核。"""
    review = _plus(days, as_of)
    cur = conn.execute(
        """insert into agent_assumption (object_id, question_code, default_action,
                                         created_at, review_at, result)
           values (?,?,?,?,?,null)""",
        (object_id, question_id, default_action, _stamp(as_of), review),
    )
    return {"table": "agent_assumption", "id": cur.lastrowid, "until": review}


# ----------------------------------------------------------------------
# 参数覆盖的读侧（engine/run.py 用）
# ----------------------------------------------------------------------

def overrides(conn: sqlite3.Connection, object_id: str,
              as_of: date | datetime | str | None = None) -> dict[str, str]:
    """这个对象上生效的参数覆盖：param_key → param_value（后写的覆盖先写的）。"""
    today = _today(as_of)
    out: dict[str, str] = {}
    for r in _param_rows(conn, object_id, today):
        out[r["param_key"]] = r["param_value"]
    return out


def override_float(conn: sqlite3.Connection, object_id: str, key: str,
                   as_of: date | datetime | str | None = None) -> float | None:
    raw = overrides(conn, object_id, as_of).get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# 到期复核（观察任务 / 实验）
# ----------------------------------------------------------------------

def due_tasks(conn: sqlite3.Connection, as_of: date | datetime | str | None = None,
              object_id: str | None = None) -> dict[str, list[dict]]:
    """已到期但还没结案的观察任务与实验。

    引擎不替人下结论：它把到期的钩子标成 result='due' 并写 resolved_at，
    然后进奏折的备注与 context 的 sediment 段，等下一次诊断/人打钩给结论。
    这样「到期没人管」不会悄悄消失，也不会被假装判过。
    """
    today = _today(as_of)
    sql_t = """select task_id, object_id, reason_code, hypothesis, metric, judge_rule,
                      created_at, due_at from observation_task
               where result is null and due_at < ?"""
    sql_e = """select experiment_id, object_id, reason_code, hypothesis, change_json,
                      control_json, metric, created_at, ends_at from experiment
               where result is null and ends_at < ?"""
    args_t: list[Any] = [today]
    args_e: list[Any] = [today]
    if object_id:
        sql_t += " and object_id=?"
        sql_e += " and object_id=?"
        args_t.append(object_id)
        args_e.append(object_id)
    tasks = [dict(r) for r in conn.execute(sql_t, args_t).fetchall()]
    exps = [dict(r) for r in conn.execute(sql_e, args_e).fetchall()]
    return {"tasks": tasks, "experiments": exps}


def mark_due(conn: sqlite3.Connection, as_of: date | datetime | str | None = None,
             object_id: str | None = None) -> dict[str, int]:
    """把到期的观察任务/实验标成 result='due'（幂等），返回标记条数。"""
    today = _today(as_of)
    stamp = _stamp(as_of)
    n_t = conn.execute(
        """update observation_task set result='due', resolved_at=?
           where result is null and due_at < ?"""
        + (" and object_id=?" if object_id else ""),
        ([stamp, today, object_id] if object_id else [stamp, today]),
    ).rowcount
    n_e = conn.execute(
        """update experiment set result='due'
           where result is null and ends_at < ?"""
        + (" and object_id=?" if object_id else ""),
        ([today, object_id] if object_id else [today]),
    ).rowcount
    return {"tasks": n_t, "experiments": n_e}


def awaiting_verdict(conn: sqlite3.Connection, object_id: str | None = None) -> dict[str, list[dict]]:
    """已到期结案（result='due'）但还没给结论的观察任务与实验。

    与 due_tasks 的区别：due_tasks 是「还没标记的到期项」，本函数是「标记过、
    等一句结论」的。两者分开，mark_due 才能保持幂等。
    """
    sql_t = """select task_id, object_id, reason_code, hypothesis, metric, judge_rule,
                      created_at, due_at, result, resolved_at
               from observation_task where result='due'"""
    sql_e = """select experiment_id, object_id, reason_code, hypothesis, metric,
                      created_at, ends_at, result, evidence_level
               from experiment where result='due'"""
    args_t: list[Any] = []
    args_e: list[Any] = []
    if object_id:
        sql_t += " and object_id=?"
        sql_e += " and object_id=?"
        args_t.append(object_id)
        args_e.append(object_id)
    return {
        "tasks": [dict(r) for r in conn.execute(
            sql_t + " order by task_id", args_t).fetchall()],
        "experiments": [dict(r) for r in conn.execute(
            sql_e + " order by experiment_id", args_e).fetchall()],
    }


def settle(conn: sqlite3.Connection, *, kind: str, row_id: int, verdict: str,
           evidence: str | None = None, as_of: date | datetime | str | None = None) -> dict:
    """给到期的观察任务/实验下结论：verdict ∈ {成立, 不成立}（或 held/refuted）。

    「成立」= 假设被数据支持（观察任务：问题在外围；实验：小步改更好）。
    结论落 result 列，证据落 evidence 列（实验没有 evidence 列，进 evidence_level）。
    """
    if kind not in ("observation_task", "experiment"):
        raise ValueError(f"kind 只能是 observation_task / experiment，收到 {kind}")
    table, pk = (("observation_task", "task_id") if kind == "observation_task"
                 else ("experiment", "experiment_id"))
    if kind == "observation_task":
        conn.execute(
            f"update observation_task set result=?, resolved_at=? where {pk}=?",
            (verdict, _stamp(as_of), row_id),
        )
    else:
        conn.execute(
            f"update experiment set result=?, evidence_level=? where {pk}=?",
            (verdict, evidence, row_id),
        )
    return {"table": kind, "id": row_id, "verdict": verdict}


# ----------------------------------------------------------------------
# 给诊断 agent 的摘要
# ----------------------------------------------------------------------

def summary(conn: sqlite3.Connection, object_id: str,
            as_of: date | datetime | str | None = None) -> dict:
    """生效中的沉淀，压成一段结构化事实喂给 context.build_input。"""
    today = _today(as_of)
    policies = []
    for r in _policy_rows(conn, object_id, today):
        params = _loads(r.get("params_json"))
        policies.append({
            "policy_id": r["policy_id"], "policy_type": r["policy_type"],
            "reason_code": params.get("reason_code"),
            "until": r.get("expires_at"), "source": r.get("source"),
            "params": {k: v for k, v in params.items()
                       if k not in ("free_text",)},
        })
    tasks = [{
        "task_id": t["task_id"], "reason_code": t["reason_code"],
        "hypothesis": t["hypothesis"], "metric": t["metric"],
        "judge_rule": t["judge_rule"], "due_at": t["due_at"],
    } for t in _task_rows(conn, object_id, today)]
    exps = [{
        "experiment_id": e["experiment_id"], "reason_code": e["reason_code"],
        "hypothesis": e["hypothesis"], "metric": e["metric"],
        "ends_at": e["ends_at"], "change": _loads(e["change_json"]),
        "control": _loads(e["control_json"]),
    } for e in _exp_rows(conn, object_id, today)]
    overdue = awaiting_verdict(conn, object_id)
    return {
        "object_id": object_id,
        "as_of": today,
        "param_overrides": overrides(conn, object_id, as_of),
        "policies": policies,
        "open_tasks": tasks,
        "open_experiments": exps,
        "overdue": {
            "tasks": [{"task_id": t["task_id"], "reason_code": t["reason_code"],
                       "due_at": t["due_at"], "judge_rule": t["judge_rule"]}
                      for t in overdue["tasks"]],
            "experiments": [{"experiment_id": e["experiment_id"],
                             "reason_code": e["reason_code"],
                             "ends_at": e["ends_at"]} for e in overdue["experiments"]],
        },
        "excluded": sorted(excluded(conn, object_id, as_of)),
    }