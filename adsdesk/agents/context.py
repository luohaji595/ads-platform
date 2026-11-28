"""给 agent 拼输入包。

铁律：agent 只读库。这个模块里的每个函数都是 SELECT，没有一个写操作。
输入的结构按 计划书 §8.3 的 11 个键来，不多不少 —— 多给字段等于给它自由发挥的空间，
少给字段等于逼它猜。
"""
from __future__ import annotations

import hashlib
import json
import datetime
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(__file__).resolve().parents[1] / ".." / ".." / "ingest" / "data" / "ads.sqlite3"
DB_PATH = (ROOT / "ingest" / "data" / "ads.sqlite3").resolve()

# engine/ 是扁平导入目录（reason_routes.py / scope.py 都在里面，没有包）。
# 这里显式插 path，理由与 agents/attribute.py 同一套：不依赖别的模块先导入过。
import sys as _sys  # noqa: E402

_ENGINE = str(ROOT / "engine")
if _ENGINE not in _sys.path:
    _sys.path.insert(0, _ENGINE)
import reason_routes  # noqa: E402
import scope  # noqa: E402


def connect(path: Path | None = None) -> sqlite3.Connection:
    """开一个 agent 用的连接。

    两个参数不是随手加的，都是被「database is locked」打出来的：

    - ``timeout=30``：库是 WAL，但 WAL 只允许**一个写者**。引擎、ingest、
      诊断 agent、收敛 agent 会同时跑，默认 5 秒 busy_timeout 根本等不到
      写锁 —— 实测 `converge.py` 长跑期间 `engine/run.py` 与 `diagnose.py`
      连续报 `sqlite3.OperationalError: database is locked`（分别在
      `engine/run.py:57` 的 ensure_schema 与 `diagnose.py:163` 的 _reap_stale）。
    - ``isolation_level=None``（autocommit）：默认模式下 `execute("update …")`
      就开启一个隐式事务，直到显式 `commit()` 才释放写锁。收敛 agent 一轮要
      处理几十条断言、每条之间还 sleep 1.5s 节流 —— 整轮都攥着写锁，
      别的进程全被挡在门外。改成 autocommit 后写一条放一条。
    """
    # ADS_DB 必须认：两个 launchd plist 里都写了 EnvironmentVariables.ADS_DB，
    # 但这里原来只认 DB_PATH 常量 —— 那行环境变量是个摆设，指向别的库时不会生效。
    # 顺序：显式传参 > 环境变量 > 默认路径，这样测试可以直接指临时库。
    target = path or os.environ.get("ADS_DB") or DB_PATH
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def latest_pack(conn: sqlite3.Connection) -> tuple[int, str, dict]:
    row = conn.execute(
        "select run_id, business_day, pack_json from memorial order by run_id desc limit 1"
    ).fetchone()
    if not row:
        raise RuntimeError("memorial 是空的，先跑一次 engine/run.py")
    return row["run_id"], row["business_day"], json.loads(row["pack_json"])


def active_policies(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"scope": r["scope"], "object_id": r["object_id"], "policy_type": r["policy_type"],
         "params": json.loads(r["params_json"]) if r["params_json"] else None,
         "evidence": r["evidence_text"]}
        for r in conn.execute(
            "select scope, object_id, policy_type, params_json, evidence_text "
            "from policy where status='active' order by policy_id"
        )
    ]


def sediment(conn: sqlite3.Connection, object_id: str | None = None) -> dict:
    """打钩沉淀下来的东西：参数覆盖 / 生效策略 / 未结观察与实验 / 到期待判。

    不给这块，agent 会建议「把目标 ACOS 调回 30%」—— 而主人上周刚打钩放宽到 38%；
    也会重复提案一件正在观察期里的事。沉淀表存在的意义，就是让下一轮看得见上一轮。
    """
    try:
        return reason_routes.summary(conn, object_id or scope.SCOPE_ASIN)
    except sqlite3.Error as e:      # 沉淀表没建好不该让整份输入塌掉
        return {"error": f"沉淀表读取失败：{e}"}


def reason_stats(conn: sqlite3.Connection) -> list[dict]:
    """每条原因出现过几次、被跳过几次。

    跳过比例就是 v1.3 词典里的「不确定性」——被跳过越多的原因，越该重新问，
    因为说明上次那个默认假设人说「先按这个来」但没真同意。
    """
    rows = conn.execute(
        """select reason_code,
                  count(*) as times_seen,
                  sum(case when answer_type='skip' then 1 else 0 end) as times_skipped
           from reason_hit group by reason_code
           order by times_seen desc limit 30"""
    ).fetchall()
    return [dict(r) for r in rows]


def recent_questions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """最近问过的问题，让它不要重复问同一件事。"""
    rows = conn.execute(
        """select hit_id, object_id, reason_code, evidence_text, created_at, answer_type
           from reason_hit where reason_code like 'Q%' or reason_code like 'D%'
           order by hit_id desc limit ?""",
        (limit,),
    ).fetchall()
    return [
        {"id": r["reason_code"], "asked_at": (r["created_at"] or "")[:10],
         "object": r["object_id"],
         "answered": r["answer_type"] in ("answer", "reject"),
         "answer_type": r["answer_type"]}
        for r in rows
    ]


def open_assumptions(conn: sqlite3.Connection) -> list[dict]:
    """还没到期的默认假设。agent 要知道「哪些事人说别管了」，别反复问。"""
    rows = conn.execute(
        """select assumption_id, object_id, question_code, default_action, created_at, review_at
           from agent_assumption where result is null order by review_at"""
    ).fetchall()
    return [dict(r) for r in rows]


def _self_check(conn: sqlite3.Connection, agent: str = "diagnose",
                wrong_limit: int = 5, correct_limit: int = 3) -> dict:
    """你自己的断言到期后被判成什么样。

    为什么要给这块：diagnose 每轮写 4~7 条可验证断言（agent_eval），到期由
    converge.py 判真假。判定结果如果只躺在表里，它下一轮照样犯同一个错 ——
    11-18 那次它断言「ACOS ≥ 0.60」，实测 0.42，但没人把它念给下一轮的自己听。

    只给它自己和已判的：unverifiable 是「判不了」（窗口没走完、数据没追平），
    不是它错，喂进去只会让它莫名其妙地变保守。

    错的给 5 条、对的给 3 条 —— 不是对称的。只喂错会让它过度自我否定，
    全对称又会让「我大部分是对的」盖过那几条具体的教训。

    ⚠ 必须按 agent 过滤。agent_eval 是各 agent 共用的一张表，早先这里不过滤，
    等于把别的 agent 的错账念给当前这个听 —— 现在表里只有 diagnose 一个 agent
    在写（37 correct / 15 wrong），所以看不出问题；一旦 record/converge 也开始
    写断言，diagnose 就会背别人的锅，而它无从辩解。
    """
    sc = conn.execute(
        """select
             sum(case when verdict='correct' then 1 else 0 end) as correct,
             sum(case when verdict='wrong'   then 1 else 0 end) as wrong,
             sum(case when verdict='unverifiable' then 1 else 0 end) as unverifiable,
             sum(case when verdict='pending' then 1 else 0 end) as pending
           from agent_eval where agent=?""",
        (agent,),
    ).fetchone()
    correct = sc["correct"] or 0
    wrong = sc["wrong"] or 0
    unverifiable = sc["unverifiable"] or 0
    judged = correct + wrong
    # 可验证率：判不了（unverifiable）不是「答错」，是「断言写成了没法验的形式」——
    # 那恰恰是模型自己能改的东西，所以也给它看，别只报对错。
    touched = judged + unverifiable
    out = {
        "judged": judged,
        "correct": correct,
        "wrong": wrong,
        # judged=0 时给 null 而不是 0.0 —— 0.0 会被读成「全错」。
        "rate": (correct / judged) if judged else None,
        "verifiable_rate": (judged / touched) if touched else None,
        "unverifiable": unverifiable,
        "pending": sc["pending"] or 0,
    }
    if not judged:
        out["note"] = "还没有断言走到判定日，现在没有可对照的历史。"
        return out

    def _rows(verdict: str, limit: int) -> list[dict]:
        rows = conn.execute(
            """select claim, expected, method, actual, checked_at
               from agent_eval where verdict=? and agent=? and actual is not null
               order by checked_at desc, eval_id desc limit ?""",
            (verdict, agent, limit),
        ).fetchall()
        return [
            {"checked_on": (r["checked_at"] or "")[:10],
             "claim": (r["claim"] or "")[:160],
             "expected": (r["expected"] or "")[:160],
             "actual": (r["actual"] or "")[:300]}
            for r in rows
        ]

    if wrong:
        out["wrong_claims"] = _rows("wrong", wrong_limit)
    if correct:
        out["correct_claims"] = _rows("correct", correct_limit)
    out["note"] = (
        "分母只算判出对错的；unverifiable 是判不了，不算你错。"
        "样本小的时候别把这几条当定论 —— 但 wrong_claims 里每一条的 actual 都是真发生的事。"
    )
    return out


def build_input(conn: sqlite3.Connection, run_id: int | None = None) -> dict:
    if run_id is None:
        run_id, _day, pack = latest_pack(conn)
    else:
        row = conn.execute(
            "select business_day, pack_json from memorial where run_id=?", (run_id,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"run #{run_id} 不在 memorial 里")
        pack = json.loads(row["pack_json"])

    # 四条桶全部进 decisions，每条带 bucket 标记。
    #
    # 只给 front 是不够的 —— 试过，结果 agent 连续三轮都在问「R01 为什么没对 lat pulldown
    # 出东西」，其实 R01 出了（observe low，落在 pack.observing），只是它看不见。
    # 一个只能看见「今天要批的」的 agent，会把「今天没批的」当成「引擎漏了」。
    # 反过来也不能不加区分地混在一起，否则它会把 observe 当成待批建议来反对。
    decisions = []
    buckets = (
        ("front", pack.get("front") or []),
        ("alert", pack.get("alerts") or []),
        ("observing", pack.get("observing") or []),
        ("overflow", pack.get("overflow") or []),
        ("below", pack.get("below") or []),
    )
    for bucket, rows in buckets:
        for d in rows:
            decisions.append({
            "decision_id": d.get("decision_id"),
            "rule_id": d.get("rule_id"),
            "entity_type": d.get("entity_type"),
            "entity_id": d.get("entity_id"),
            "entity_label": d.get("entity_label"),
            "action_type": d.get("action_type"),
            "action_label": d.get("action_label"),
            "current_value": d.get("current_value"),
            "proposed_value": d.get("proposed_value"),
            "spend_impact": d.get("spend_impact"),
            "confidence": d.get("confidence"),
            # "state" = 这条结论今天和上次一模一样，没写进 decision_log。
            # 说出来是因为 agent 会问「为什么这条没有 decision_id」——
            # 而且它连问六轮才逼出这个区分：一条只在变化时才有意义的东西，
            # 每天往账本写一行是把状态记成了事件。
            "status": d.get("status"),
            "rationale": d.get("rationale"),
            "campaign_id": d.get("campaign_id"),
            # front = 今天等你打钩的；observing = 引擎算过但置信度低、只观察不动的；
            # overflow = 达标但被每日配额挤下去的；below = 没到判定线的；alert = 账户级告警。
            "bucket": bucket,
        })

    return {
        "run_id": run_id,
        "window": {"start": pack.get("window_start"), "end": pack.get("window_end")},
        # scope = 只算这个投放组合；account = 全账户。两个 CVR 不一样是正常的，
        # 但读的人（和 agent）第一反应会问「为什么同一页两个数」，所以两块都给，
        # 并在 account 里带一句 note 说明它不受 scope 限制。
        "scope": pack.get("scope") or {},
        "account": pack.get("account") or {},
        "breakeven": pack.get("breakeven") or {},
        # CPC 校准系数与出价地板。不给这块，agent 会去 md 里找、或者凭记忆问
        # 「2.260 站不站得住」（Q D3 就是这么来的）—— 而那个数已经改成有效口径
        # 0.964，名义口径 2.26 保留作对照。两个都给，它才能看出差别在哪。
        "cpc_bias": pack.get("cpc_bias") or {},
        "floor": pack.get("floor") or {},
        # 数据结算状态。不给这块，agent 会反复问「11-15 是 T+1 还是 T+3」——
        # 它连问了三轮（Q37-1 / Q33-3 / Q31-3），因为从 pack 里根本看不出数据什么时候拉的。
        # 而「窗口末日是否已结算」直接决定它该不该信 6 单这个数。
        "data_status": _data_status(conn, pack.get("window_end")),
        # 覆盖率必须一起给。只给 clicks 不给 coverage，agent 会看到「关键词层 816 击、
        # 活动层 1197 击」然后连问三轮「这两层是不是不在同一批数据上算的」——
        # 而答案（816/1197 = 68%，其余是自动/ASIN 定向活动，本来就没有关键词）就在
        # 它没拿到的那几个字段里。
        "layer_evidence": [
            {k: e.get(k) for k in ("layer", "label", "significant", "p_value",
                                   "groups", "clicks", "orders", "error",
                                   "scoped", "covered_clicks", "base_clicks",
                                   "coverage", "detectable_ratio")}
            for e in (pack.get("layer_evidence") or [])
        ],
        "portfolio_headline": pack.get("portfolio_headline"),
        # 同一笔钱的三把刀。诊断 agent 第 177 号反对就是这么错的：
        # 它看到「关键词 lat pulldown $226.43」和「该活动 Detail Page 位次 $193.03」，
        # 把两个数加起来得 $419.46 说「较大的一处没进决策」。但 $193.03 是
        # $226.43 的一部分（同一个活动、同一批点击，只是按位次再切一刀），
        # 相加比活动总花费还大 —— 数学上就不可能。三张表是三个切面，不是三笔钱。
        "spend_views": _spend_views(conn, pack),
        # 告警出现过多少天。诊断 agent 第 135 号反对：「今天输入里没有它此前出现次数的
        # 记录，所以我不说『已经重复了几天』」—— 它说得对。补上，按 distinct 日期算
        # （一天多行是引擎重跑 + 文案改动，按行数会得出「已出现 43 次」这种假数字）。
        "alert_history": _alert_history(conn),
        "decisions": decisions,
        "suppressed": pack.get("suppressed") or [],
        "conflicts": pack.get("conflicts") or [],
        "policy": active_policies(conn),
        # 与 policy 并列：policy 是「说过什么并且生效了」，sediment 是「钩子落在了
        # 哪张表、到什么时候、有没有到期没判的」。两块一起看，才算看见了回路。
        "sediment": sediment(conn),
        "recent_questions": recent_questions(conn),
        "reason_stats": reason_stats(conn),
        "open_assumptions": open_assumptions(conn),
        # 放在最后：前面全是「今天发生了什么」，这块是「你上次说错了什么」。
        # agent 读 prompt 的顺序是先看事实再看自己，倒过来会让它先自我怀疑。
        "self_check": _self_check(conn),
    }


def _spend_views(conn: sqlite3.Connection, pack: dict) -> dict:
    """同一笔花费在活动/关键词/位次三张表里各是多少，以及它们之间的关系。

    这三张表不是三笔钱，是同一笔钱按三个维度各切一刀：
      - 活动表：整个活动花了多少
      - 关键词表：其中带关键词的那部分（自动/ASIN 定向活动本来没有关键词，是 0）
      - 位次表：其中按位次（TOS / 详情页 / 其他 / 站外）摊开的那部分

    所以 `关键词 + 位次` 永远大于活动总额，两者相加是无意义的。
    实测：活动 175723193023274 总额 $226.43，位次里 Detail Page $193.03 +
    TOS $26.00 + Other $4.07 + Off Amazon $2.56 = $225.66（差 0.77 是取整），
    词层 lat pulldown $226.43 —— 三组数说的是同一件事。

    agent 看不到这层关系时会把 $226.43 和 $193.03 加起来，得出「$419.46 没进决策」
    这种结论，比活动总花费还大。给了这个块，它自己能算出来。
    """
    start, end = pack.get("window_start"), pack.get("window_end")
    # pack["portfolio_headline"] 是一句话（str），不是 dict —— 拿它取 portfolio_name
    # 会 AttributeError。组合名从 scope.label 里取，取不到回落引擎里写死的那个。
    label = (pack.get("scope") or {}).get("label") or ""
    portfolio = "JL-高位下拉"
    if "（投放组合 " in label:
        portfolio = label.split("（投放组合 ", 1)[1].rstrip("）") or portfolio
    if not start or not end:
        return {}
    # 必须跟 pack.scope 同口径。不过滤 portfolio 就会拿全店活动跟
    # 「本组合 $998.66」并列 —— agent 会拿 $4872 的活动去对组合的数，又是 scope 混用。
    scoped = """and campaign_id in (select distinct campaign_id from fact_campaign_daily
                   where portfolio_name = ?)"""
    try:
        camp = conn.execute(
            f"""select campaign_id, campaign_name, sum(cost) cost, sum(clicks) clicks,
                       sum(ads_orders) orders
                  from fact_campaign_daily
                 where report_date between ? and ? {scoped}
                 group by campaign_id order by cost desc""",
            (start, end, portfolio)).fetchall()
        kw = {r["campaign_id"]: r["cost"] for r in conn.execute(
            f"""select campaign_id, sum(cost) cost from fact_keyword_daily
                 where report_date between ? and ? {scoped} group by campaign_id""",
            (start, end, portfolio)).fetchall()}
        pl = {r["campaign_id"]: r["cost"] for r in conn.execute(
            f"""select campaign_id, sum(cost) cost from fact_placement_daily
                 where report_date between ? and ? {scoped} group by campaign_id""",
            (start, end, portfolio)).fetchall()}
    except sqlite3.Error:
        return {}
    rows = []
    for r in camp:
        cid = r["campaign_id"]
        k, p_ = round(kw.get(cid) or 0.0, 2), round(pl.get(cid) or 0.0, 2)
        rows.append({
            "campaign_id": cid,
            "campaign_name": r["campaign_name"],
            "campaign_cost": round(r["cost"] or 0.0, 2),
            "keyword_cost": k,
            "placement_cost": p_,
            "clicks": r["clicks"], "orders": r["orders"],
            # 词层 + 位次是否已经超出活动总额。超出 = 这两组数在切同一块钱。
            "keyword_plus_placement": round(k + p_, 2),
            "overlaps": (k + p_) > (r["cost"] or 0) + 0.5,
        })
    return {
        "window": [start, end],
        "portfolio_name": portfolio,
        "note": (
            "活动 / 关键词 / 位次是同一笔钱的三个切面，**不能相加**。"
            "keywords 那层是「带关键词的部分」（自动和 ASIN 定向活动为 0），"
            "placement 那层是「按位次摊开的部分」；"
            "`overlaps=true` 的活动，词层 + 位次已经超过活动总额，"
            "说明两组数在描述同一批点击。要算「没被任何规则覆盖的钱」，"
            "只能按活动总额去重后算，不能把不同切面的金额加起来。"
        ),
        "campaigns": rows,
    }


def _alert_history(conn: sqlite3.Connection) -> dict:
    """每条规则此前在多少个不同日期出现过。按日期去重，不按行数。"""
    try:
        rows = conn.execute(
            """select rule_id,
                      count(distinct substr(decided_at, 1, 10)) as days_seen,
                      min(substr(decided_at, 1, 10)) as first_day,
                      max(substr(decided_at, 1, 10)) as last_day
                 from decision_log
                where action_type = 'alert'
                group by rule_id
                order by rule_id"""
        ).fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for r in rows:
        rid = r["rule_id"]
        try:
            days = [
                x[0] for x in conn.execute(
                    """select distinct substr(decided_at, 1, 10) d
                         from decision_log where rule_id = ? and action_type = 'alert'
                        order by d""", (rid,)
                ).fetchall()
            ]
        except sqlite3.Error:
            days = []
        out[rid] = {
            "days_seen": r["days_seen"],
            "first_day": r["first_day"],
            "last_day": r["last_day"],
            "days": days,
            "note": ("同一个日期里可能有多行（引擎重跑、文案改动都会插新行），"
                     "所以这里按天去重。"),
        }
    return out


def _data_status(conn: sqlite3.Connection, window_end: str | None) -> dict:
    """数据结算状态：窗口末日最后一次拉取是什么时候、拉过几次。

    Amazon 报表 T+1 只是「能查到」，不是「已结算」，同一天会持续回填 2–3 天。
    实测 2026-11-15 在 T+1 拉到 23 击，T+3 变成 103 击（低报 4.5 倍）。
    所以 agent 必须知道：这个窗口的末日，到底重拉过几次。
    """
    if not window_end:
        return {}
    try:
        rows = conn.execute(
            """select started_at, finished_at, rows_fetched, status
                 from run_log
                where dataset = 'campaign'
                  and start_date <= ? and end_date > ?
                  and status = 'ok'
                order by started_at""",
            (window_end, window_end)).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {"window_end": window_end, "fetches": 0,
                "note": "窗口末日没有拉取记录 —— 这个日期可能根本没进过库"}
    last = rows[-1]
    first = rows[0]
    # 「拉过几次」不如「最后一次离窗口末日隔了几天」直接 —— agent 问的就是
    # 「11-15 是 T+1 还是 T+3」。T+1 只保证接口有返回，不保证已结算。
    try:
        lag_days = (datetime.date.fromisoformat(last["started_at"][:10])
                    - datetime.date.fromisoformat(window_end)).days
    except Exception:  # noqa: BLE001
        lag_days = None
    if lag_days is None:
        verdict = "unknown"
    elif lag_days >= 2:
        verdict = "settled"
    else:
        verdict = "stale"
    return {
        "window_end": window_end,
        "fetches": len(rows),
        "first_fetch": first["started_at"],
        "last_fetch": last["started_at"],
        "last_rows": last["rows_fetched"],
        "lag_days": lag_days,
        "settled": verdict == "settled",
        "verdict": verdict,
        "note": (
            f"窗口末日最后一次拉取在 T+{lag_days}，"
            + ("已过结算期，这个数可以当结算值用。"
               if verdict == "settled"
               else "还在结算期内，最近几天的数可能偏低，别拿它下强结论。")
            if lag_days is not None else "无法判断结算状态"),
    }


def digest(payload: dict) -> str:
    """输入指纹。同一天同一份输入重跑，digest 一样 —— 用来查「到底喂了什么」。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
