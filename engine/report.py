"""
奏折报告（P1）

按 docs/奏折报告改版规格-v0.1.md 把规则结果压成一份可批的奏折：
  一、今日结论
  二、今日策略（压缩后的建议，不够线的不占名额）
  三、需要你判断的问题（最多 5 条，未勾 = 未表态）
  四、结构冲突（同词多活动，Q13）
  五、今日只观察（不够线、溢出、只看不动）
  六、依据与计算（可展开）+ 配置变更

它不写广告后台。批了只改 decision_log。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import facts
import reason_routes
import record
import rules
import scope

ACTION_LABEL = {
    "alert": "全局告警",
    "pause_candidate": "建议停用（待定）",
    "floor_breach": "降到底价",
    "reduce_bid": "降出价",
    "add_negative": "加否词",
    "raise_budget": "加预算",
    "adjust_placement": "调位次加价",
    "raise_bid": "加出价",
    "raise_bid_blocked": "加出价（被预算卡住）",
    "harvest": "收割成词",
    "rank_push": "卡位加价",
    "rank_step_down": "卡位到位降价",
    "observe": "只看不动",
}

CONF_LABEL = {"high": "证据够", "medium": "证据一般", "low": "证据不足"}


def _evidence_label(d) -> str:
    """「证据」这一列该说什么。

    R32 的结论不是从假设检验来的，它的置信度必然是 low（统计上判不了），
    如果只按 confidence 渲染，主人看到的是「证据不足」—— 这恰好把它的
    真实依据说反了：它依据的是**已经花掉的钱**，而不是「证据还不够」。
    两种够格方式（显著性 / 止损线）在表里必须长得不一样。
    """
    sig = d.significance or {}
    if sig.get("basis") == "cost_ceiling":
        ev = sig.get("cost_evidence") or {}
        units = ev.get("units")
        return (f"止损线（已花 {units:.2f} 单的钱）" if units is not None else "止损线")
    return CONF_LABEL.get(d.confidence, d.confidence)

# 规则配额：未单独配置的默认最多 2 条进奏折正文
QUOTA = {"R01": 3, "R30": 1}
DEFAULT_QUOTA = 2
SOFT_CAP = 8


def _money(x) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def _pct(x) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.2f}%"


def _impact(x) -> str:
    if not x:
        return "—"
    return f"省 {_money(x)}" if x > 0 else f"多花 {_money(-x)}"


# 动作的**目标值**是枚举（pause_or_restructure / consider_new_ad_group …），
# 直接印出来是一串英文，和「做什么」那列的中文对不上。只翻译「整个值就是这个枚举」
# 的情况；「20%（从 0% 提）」这类已经说清楚的中文串原样放行 ——
# 翻译的边界是「英文枚举→中文」，不是改写引擎的话。
VALUE_LABEL = {
    "pause": "停用",
    "pause_or_restructure": "停用或重构（否词 / 换匹配方式）",
    "consider_new_ad_group": "考虑拆到独立广告组",
    "increase": "提高",
    "reduce": "降低",
}


def _value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return _money(float(v))
    return VALUE_LABEL.get(str(v), str(v))


def _is_conclusive(d: rules.Decision) -> bool:
    """这条建议够不够格进「待批」。

    两种够格的方式，必须分开说，否则会有人以为「成本止损」也是统计结论：

    1) `statistically_conclusive=True` —— 走的是假设检验，p < alpha。
       R01/R03/R09 这些「它比基线好吗/差吗」的规则用这一条。
    2) `basis == 'cost_ceiling'` —— **不做假设检验**，判据是已经花掉的钱。
       R32 止损线用这一条：`lat pulldown` 花掉 4 单的钱、0 单，这个事实不需要
       显著性（即使 CVR 等于基线，257 击 0 单也有 42.6% 概率发生 —— 统计上
       永远证明不了它更差，但钱是真的花掉了）。

    为什么必须让第 2 种也进 front：这个账户日均 19 击、14 天窗口，统计学
    永远给不出答案（0 单要 321 击、1 单要 597 击，而最大的词只 257–314 击），
    只认第 1 种的话 front 会永久为 0 —— 85 个 memorial 实测一次都没非空过。
    """
    sig = d.significance or {}
    if sig.get("statistically_conclusive"):
        return True
    return sig.get("basis") == "cost_ceiling" and bool(sig.get("cost_evidence"))


@dataclass
class MemorialPack:
    alerts: list
    front: list
    overflow: list
    below: list
    observing: list
    questions: list
    conflicts: list
    saved: float = 0.0
    spent: float = 0.0
    notes: list = field(default_factory=list)


def compress(f: facts.Facts, res: rules.RuleResult,
             conn: sqlite3.Connection | None = None) -> MemorialPack:
    """压成奏折。conn 给出时才做「有效期内不再问」的去重（打钩沉淀过的码不重复问）。"""
    alerts = [d for d in res.decisions if d.action_type == "alert"]
    observing = [d for d in res.decisions if d.action_type == "observe"]
    raw = [d for d in res.decisions if d.action_type not in ("observe", "alert")]

    below, eligible = [], []
    for d in raw:
        if _is_conclusive(d):
            eligible.append(d)
        else:
            below.append(d)

    eligible.sort(key=lambda d: abs(d.spend_impact or 0), reverse=True)

    used = Counter()
    front, overflow = [], []
    for d in eligible:
        cap = QUOTA.get(d.rule_id, DEFAULT_QUOTA)
        # 反复改坏的规则少占一格：不是封杀，是别让它天天霸占注意力。
        cap = record.quota_cap(d.rule_id, cap, getattr(f, "record", None) or {})
        if used[d.rule_id] >= cap or len(front) >= SOFT_CAP:
            overflow.append(d)
            continue
        front.append(d)
        used[d.rule_id] += 1

    notes = []
    # 「不足 3 条正常」与「未达判定线」已在一、今日结论 正文说过，不重复
    if overflow:
        notes.append(f"另有 {len(overflow)} 条已达统计要求，按配额折进「只观察」，不占今天名额。")

    # 出题要 conn：同一个原因码已经沉淀成策略/覆盖/观察/实验且没过期，就不再问第二遍。
    questions, qnotes = _build_questions(f, front, alerts, below=below, conn=conn)
    notes.extend(qnotes)

    saved = sum(d.spend_impact for d in front if d.spend_impact and d.spend_impact > 0)
    spent = sum(-d.spend_impact for d in front if d.spend_impact and d.spend_impact < 0)

    return MemorialPack(
        alerts=alerts,
        front=front,
        overflow=overflow,
        below=below,
        observing=observing,
        questions=questions,
        conflicts=_structure_conflicts(f),
        saved=saved,
        spent=spent,
        notes=notes,
    )


def _campaign_name(f: facts.Facts, campaign_id) -> str:
    for c in f.campaigns:
        if str(c.get("campaign_id")) == str(campaign_id):
            return c.get("campaign_name") or str(campaign_id)
    return str(campaign_id) if campaign_id is not None else "—"


def _structure_conflicts(f: facts.Facts) -> list[dict]:
    groups = defaultdict(list)
    for k in f.keywords:
        text = (k.get("keyword_text") or "").strip()
        if not text:
            continue
        groups[text.lower()].append(k)

    out = []
    for key, rows in groups.items():
        camps = {}
        for r in rows:
            cid = str(r.get("campaign_id") or "")
            if not cid:
                continue
            if cid not in camps:
                camps[cid] = {
                    "campaign_id": cid,
                    "campaign_name": _campaign_name(f, cid),
                    "keyword_text": r.get("keyword_text"),
                    "keyword_ids": [],
                    "clicks": 0,
                    "orders": 0,
                    "cost": 0.0,
                    "sales": 0.0,
                    "bid": r.get("bid"),
                }
            camps[cid]["keyword_ids"].append(str(r.get("keyword_id")))
            camps[cid]["clicks"] += int(r.get("clicks") or 0)
            camps[cid]["orders"] += int(r.get("orders") or 0)
            camps[cid]["cost"] += float(r.get("cost") or 0)
            camps[cid]["sales"] += float(r.get("sales") or 0)
            if r.get("bid"):
                camps[cid]["bid"] = r.get("bid")
        if len(camps) < 2:
            continue
        members = list(camps.values())
        total_cost = sum(m["cost"] for m in members)
        # 两边都几乎没花钱，不算冲突
        if total_cost < 5 and sum(m["clicks"] for m in members) < 20:
            continue
        for m in members:
            m["cost"] = round(m["cost"], 2)
            m["cpc"] = round(m["cost"] / m["clicks"], 4) if m["clicks"] else None
            m["cvr"] = round(m["orders"] / m["clicks"], 6) if m["clicks"] else None
            m["acos"] = round(m["cost"] / m["sales"], 4) if m["sales"] else None
            m["share"] = round(m["cost"] / total_cost, 4) if total_cost else 0
            # 位次溢价上限：出价 × (1 + 最大 bid_adjustment)。
            # 注意必须用 placement 表里的真实 adjustment，不能信活动名
            # （「溢价1.4」实际 350%，见计划书 §7.3 勘误）。
            adj = getattr(f, "placement_max_adjustment", {}).get(m["campaign_id"])
            if adj is not None and m.get("bid"):
                m["placement_cap_pct"] = round(float(adj), 1)
                m["placement_cap"] = round(float(m["bid"]) * (1 + float(adj) / 100.0), 4)
            else:
                m["placement_cap_pct"] = None
                m["placement_cap"] = None
        flags = []
        cpcs = [m["cpc"] for m in members if m["cpc"]]
        if len(cpcs) >= 2 and min(cpcs) > 0 and (max(cpcs) / min(cpcs) >= 1.3):
            flags.append("CPC 差距超过 30%")
        with_orders = [m for m in members if m["orders"] > 0]
        zero_orders = [m for m in members if m["orders"] == 0 and (m["cost"] >= 5 or m["clicks"] >= 10)]
        if with_orders and zero_orders:
            flags.append("一边有单、一边花了钱还没单")
        out.append({
            "keyword_text": members[0]["keyword_text"],
            "campaigns": members,
            "total_cost": round(total_cost, 2),
            "flags": flags,
        })
    out.sort(key=lambda x: x["total_cost"], reverse=True)
    return out


# 第一版问题：只从今天要动的对象里出「人因」题，不做完整词典引擎。
# 选项来自原因词典 v1.3，未勾 = 未表态，不视为排除。
def _relaxed_target(f: facts.Facts) -> float:
    """目标 ACOS 放宽到哪：至少比现目标高一档，不低于当前实际 ACOS，封顶 80%。

    放宽到「比实际还严」等于没放，放宽到「实际做不到」还是做不到。
    取「现目标 × 1.25」与「当前实际 ACOS」里更宽松的那个，就是一次真正能落地的放宽。
    """
    cur = float(f.target_acos or 0.30)
    actual = float((f.account or {}).get("acos") or 0.0)
    return round(min(0.80, max(cur * 1.25, actual)), 2)


def _budget_step(f: facts.Facts) -> float:
    """加预算步长（倍数）。被 param_override(max_budget_step) 覆盖过就用覆盖值。"""
    return float(getattr(f, "budget_step", facts.DEFAULT_BUDGET_STEP))


_EXPLORE_FIELD = {
    "raise_budget": "daily_budget",
    "reduce_budget": "daily_budget",
    "raise_bid": "bid",
    "reduce_bid": "bid",
    "rank_push": "bid",
    "rank_step_down": "bid",
    "adjust_placement": "placement_bid",
}


def _control_for(f: facts.Facts, d: rules.Decision) -> dict | None:
    """给一条候选找对照组：本次不动、且与它可比的那个对象。

    活动级候选拿「同组合、日均花费最接近的另一个活动」当对照；
    词级候选拿「同活动内点击量最接近的另一个词」当对照。
    找不到就不开实验 —— 没有对照的实验只是「改了再看心情」。
    """
    if d.entity_type == "campaign":
        me = next((c for c in f.campaigns
                   if str(c.get("campaign_id")) == str(d.campaign_id)), {})
        pool = [c for c in f.campaigns
                if str(c.get("campaign_id")) != str(d.campaign_id)
                and (c.get("state") or "ENABLED") == "ENABLED"]
        if not pool:
            return None
        port = me.get("portfolio_name")
        same = [c for c in pool if port and c.get("portfolio_name") == port]
        cands = same or pool
        base = me.get("avg_daily_cost") or me.get("cost") or 0.0
        cands.sort(key=lambda c: abs((c.get("avg_daily_cost") or c.get("cost") or 0.0) - base))
        c = cands[0]
        return {"campaign_id": c.get("campaign_id"),
                "label": c.get("campaign_name") or str(c.get("campaign_id")),
                "rationale": "同组合、日均花费最接近，本次不动它",
                "clicks": c.get("clicks"), "orders": c.get("orders"),
                "cost": c.get("cost")}

    me = next((k for k in f.keywords
               if str(k.get("keyword_text")) == str(d.entity_id)
               and str(k.get("campaign_id")) == str(d.campaign_id)), {})
    pool = [k for k in f.keywords
            if str(k.get("campaign_id")) == str(d.campaign_id)
            and str(k.get("keyword_text")) != str(d.entity_id)
            and (k.get("clicks") or 0) >= 5]
    if not pool:
        return None
    base = me.get("clicks") or 0
    pool.sort(key=lambda k: abs((k.get("clicks") or 0) - base))
    k = pool[0]
    return {"campaign_id": k.get("campaign_id"),
            "keyword_text": k.get("keyword_text"),
            "match_type": k.get("match_type"),
            "label": f"{k.get('keyword_text')}（{k.get('match_type') or '—'}）",
            "rationale": "同活动、点击量最接近，本次不动它",
            "clicks": k.get("clicks"), "orders": k.get("orders")}


def _explore_candidate(f: facts.Facts, below: list) -> tuple | None:
    """从「差一点到判定线」的候选里挑一条值得小步试的，配好对照组。

    探索类（experiment）是四类里唯一还没有生产者的，缺的就是这个函数：
    正式的加预算/降出价要「证据够」，而证据不够的那批正好是最该做实验的
    —— 拿最小步长试一次，代价有限，还能把「够不够」这件事一次问清。
    """
    pool = [d for d in below
            if d.action_type in _EXPLORE_FIELD
            and isinstance(d.current_value, (int, float))
            and isinstance(d.proposed_value, (int, float))
            and float(d.current_value or 0) > 0
            and abs(d.spend_impact or 0) > 0]
    if not pool:
        return None
    pool.sort(key=lambda d: abs(d.spend_impact or 0), reverse=True)
    for d in pool:
        ctrl = _control_for(f, d)
        if not ctrl:
            continue
        cur, prop = float(d.current_value), float(d.proposed_value)
        half = round(cur + (prop - cur) * 0.5, 4)
        if half == cur:
            continue
        delta = round((half / cur - 1) * 100, 1)
        change = {
            "campaign_id": d.campaign_id,
            "entity_type": d.entity_type,
            "entity_id": d.entity_id,
            "field": _EXPLORE_FIELD[d.action_type],
            "from": cur, "to": half, "delta_pct": delta,
            "note": f"最小步长 = 建议幅度的一半（{_value(cur)} → {_value(prop)}）",
        }
        sig = d.significance or {}
        fact = (f"{sig.get('clicks')} 击 / {sig.get('orders')} 单"
                if sig.get("clicks") is not None else "证据只到一般")
        ask_text = (f"「{d.entity_label}」{fact}，只到「证据一般」，规则不敢直接推正式建议。"
                    f"要不要按最小步长（{_value(cur)} → {_value(half)}，{delta:+.0f}%）"
                    f"先试一次，留「{ctrl['label']}」当对照？")
        return d, change, ctrl, ask_text
    return None


def _build_questions(f: facts.Facts, front: list, alerts: list,
                     below: list | None = None,
                     conn=None) -> tuple[list[dict], list[str]]:
    """出题：返回 (题目, 备注)。

    每一题的每个选项都带机器可读的去向（option_meta），审批侧照抄进库，
    不再自己算业务口径；原因词典 v1.3 的四类（阻塞/校准/归因/探索）在出题
    这一层就定了，打钩只是选择走哪一条。

    conn 给出时按「有效期内不再问」去重：同一个原因码已经沉淀成 policy /
    param_override / observation_task / experiment 且没过期，就不是「没表态」，
    再问一遍等于问人家「你上次说的话还算不算数」。
    """
    qs: list[dict] = []
    notes: list[str] = []

    def _active(object_id) -> dict:
        if conn is None or not object_id:
            return {}
        try:
            return reason_routes.active_codes(conn, str(object_id))
        except sqlite3.Error:
            return {}      # 去重查不动，不能让今天的机会清单消失

    def _crossed(object_id) -> dict:
        """这个对象上「还没出静默期」的显式划掉：原因码 → 静默到哪天。"""
        if conn is None or not object_id:
            return {}
        try:
            return reason_routes.excluded_until(conn, str(object_id))
        except sqlite3.Error:
            return {}      # 同上：查不动不能让清单消失

    acct = _active(scope.SCOPE_ASIN)

    def ask(qid: str, obj: str, ask_text: str, entity_id, options: list,
            decision_ref: str | None = None) -> None:
        """options: [(选项文本, 原因码, 去向细节)]，下标语义由顺序决定，不要重排。"""
        act = dict(acct)
        act.update(_active(entity_id))
        crossed_until = _crossed(entity_id)
        actionable = [c for _, c, _ in options if c and not c.endswith("_other")]
        # 已经沉淀过的（生效期内）和明确划掉过的，都算「这件事不用再问」。
        # 划掉是「这个方向不要」，沉淀是「这个方向已定」—— 两种情况都别重复问，
        # 但备注里要说清是哪一种，否则人分不清「系统记住了」还是「系统放弃了这个方向」。
        covered = [c for c in actionable if c in act or c in crossed_until]
        if actionable and len(covered) == len(actionable):
            if all(c in act for c in actionable):
                until = max((act[c].get("until") or "") for c in actionable)
                notes.append(f"「{obj}」的处理方式都在生效期内（到 {until or '未设到期'}），"
                             f"本次不再问。")
            else:
                notes.append(f"「{obj}」这个方向你划掉过（静默期内），本次不再问。")
            return
        metas = []
        for text, code, meta in options:
            m = {"reason_code": code, "destination": reason_routes.destination(code)}
            m.update(meta or {})
            if code in act:
                m["settled_table"] = act[code].get("table")
                m["settled_id"] = act[code].get("id")
                m["settled_until"] = act[code].get("until")
            if code in crossed_until:
                m["excluded"] = True
                m["exclude_until"] = crossed_until.get(code)
            metas.append(m)
        q = {"id": qid, "object": obj, "ask": ask_text,
             "options": [t for t, _, _ in options],
             "reason_codes": [c for _, c, _ in options],
             "option_meta": metas}
        if decision_ref:
            q["decision_ref"] = decision_ref
        qs.append(q)

    if not f.target_reachable:
        be = f.breakeven.get("breakeven_cvr")
        relax = _relaxed_target(f)
        opts = [
            # 别写死 30%：目标可能已被 param_override 改过，
            # 文案要跟当前生效的目标一致，否则人看到的和系统算的是两个数。
            (f"先维持 {_pct(f.target_acos)}，下面降价建议只用来控损",
             "goal_keep_acos30_control_loss",
             {"scope": "account", "object_id": scope.SCOPE_ASIN,
              "policy_type": "acos_cap_accept_loss", "target_acos": f.target_acos}),
        ]
        # 目标已经顶到上限时「再放宽」是空话：放宽值算出来等于当前值，
        # 摆出去只会让人以为还有余地。这种情况就不出这一项。
        if relax > float(f.target_acos or 0) + 0.005:
            opts.append(
                (f"把目标 ACOS 放宽到 {_pct(relax)}，重新算一遍建议", "goal_relax_acos",
                 {"scope": "account", "object_id": scope.SCOPE_ASIN,
                  "param_key": "target_acos", "param_value": relax, "ttl_days": 90,
                  "hypothesis": f"按 {_pct(relax)} 算还能不亏",
                  "judge_rule": "到期实际 ACOS 落进新目标 → 成立；否则回调"}))
        opts.append(
            ("先不动出价，去查主图 / 价格 / 落地页", "goal_product_side_problem",
             {"scope": "account", "object_id": scope.SCOPE_ASIN,
              "metric": "cvr", "ttl_days": 14}))
        opts.append(("其他（请补一句依据）", "goal_other", {}))
        ask(
            "Q08", "本组合目标",
            (
                # 这里说的是本组合（f.cvr_baseline 是 scope 口径），不是全店。
                # 全店的数在 R30 告警里，两个口径别混着说。
                f"本组合窗口转化率 {_pct(f.cvr_baseline.get('cvr'))}，"
                f"达不到目标 ACOS {_pct(f.target_acos)} 所需的 "
                f"{_pct(be)}。降出价只能少花钱，不能把转化率抬上去。"
                "今天怎么处理这个目标？"
            ),
            scope.SCOPE_ASIN,
            opts,
        )

    step = _budget_step(f)
    for d in front:
        if len(qs) >= 5:
            break
        ref = f"{d.rule_id}:{d.entity_id}:{d.action_type}"
        if d.action_type in ("pause_candidate", "floor_breach") or d.rule_id == "R01":
            sig = d.significance or {}
            ask(
                "Q01", d.entity_label or d.entity_id,
                (f"「{d.entity_label}」窗口 {sig.get('clicks')} 击 / {sig.get('orders')} 单，"
                 f"系统建议{ACTION_LABEL.get(d.action_type, d.action_type)}。"
                 "更可能是哪一种？"),
                d.entity_id,
                [
                    ("词本身匹配差或意图不对，可以按建议停或降", "kw_mismatch_ok_to_pause",
                     {"scope": "object", "object_id": d.entity_id,
                      "policy_type": "allow_pause_no_orders",
                      "current_value": d.current_value,
                      "proposed_value": d.proposed_value}),
                    ("词没问题，是主图 / 价格 / 落地页把转化拉下来了", "kw_ok_listing_problem",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "cvr", "ttl_days": 14}),
                    ("样本还在波动，先观察，今天不执行", "kw_sample_noise_observe",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "clicks,orders", "ttl_days": 7}),
                    ("其他（请补一句依据）", "kw_other", {}),
                ],
                ref,
            )
        elif d.action_type == "raise_budget":
            ask(
                "Q03", d.entity_label or d.entity_id,
                (f"「{d.entity_label}」建议把日预算从 {_value(d.current_value)} "
                 f"加到 {_value(d.proposed_value)}。确认加吗？"),
                d.entity_id,
                [
                    ("按建议加", "budget_raise_as_proposed",
                     {"scope": "object", "object_id": d.entity_id,
                      "policy_type": "allow_raise_budget",
                      "current_value": d.current_value,
                      "proposed_value": d.proposed_value}),
                    (f"加，但每次只加 {round((step - 1) * 100)}%"
                     f"（现规则一次 +{round((facts.DEFAULT_BUDGET_STEP - 1) * 100)}%）",
                     "budget_raise_smaller",
                     {"scope": "account", "object_id": scope.SCOPE_ASIN,
                      "param_key": "max_budget_step", "param_value": step,
                      "ttl_days": 90, "max_budget_step": step}),
                    ("先不加，观察", "budget_hold_observe",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "roas,budget_util", "ttl_days": 14}),
                    ("其他（请补一句依据）", "budget_other", {}),
                ],
                ref,
            )
        elif d.action_type == "adjust_placement":
            ask(
                "Q04", d.entity_label or d.entity_id,
                (f"「{d.entity_label}」建议调整位次加价："
                 f"{_value(d.current_value)} → {_value(d.proposed_value)}。确认改吗？"),
                d.entity_id,
                [
                    ("按建议改", "placement_apply",
                     {"scope": "object", "object_id": d.entity_id,
                      "policy_type": "allow_placement_change",
                      "current_value": d.current_value,
                      "proposed_value": d.proposed_value}),
                    ("先不改，观察", "placement_hold_observe",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "acos,top_of_search_share", "ttl_days": 14}),
                    ("其他（请补一句依据）", "placement_other", {}),
                ],
                ref,
            )

    # 探索题放最后：它问的是「要不要花两周试一次」，不如「今天停不停」急。
    if len(qs) < 5:
        cand = _explore_candidate(f, list(below or []))
        if cand:
            d, change, ctrl, ask_text = cand
            ask(
                "Q05", d.entity_label or d.entity_id, ask_text, d.entity_id,
                [
                    (f"试：{change['field']} 只动 {change['delta_pct']:+.0f}%，"
                     f"留「{ctrl['label']}」做对照",
                     "explore_run_experiment",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "roas,cpa", "ttl_days": 14,
                      "change": change, "control": ctrl}),
                    ("不试，继续攒证据", "explore_hold",
                     {"scope": "object", "object_id": d.entity_id,
                      "metric": "clicks,orders", "ttl_days": 14}),
                    ("其他（请补一句依据）", "explore_other", {}),
                ],
                f"{d.rule_id}:{d.entity_id}:{d.action_type}",
            )

    return qs[:5], notes


def _layer_lines(f: facts.Facts) -> list[str]:
    """把分层齐性检验翻成人话（P1.0 核心）。

    报告不再靠「今天 0 条建议」表达无证据，而是明说哪层有差异、哪层没有。
    """
    ev = getattr(f, "layer_evidence", None) or []
    if not ev:
        return []
    out: list[str] = []
    sig = [e for e in ev if (e.get("stats") or {}).get("significant")]
    nonsig = [e for e in ev if e.get("stats") and not e["stats"]["significant"]]
    errs = [e for e in ev if e.get("error")]
    if errs:
        out.append(f"**分层证据算不出来：{errs[0]['error']}。** 报告其余部分照常，这一块先空着。")
        out.append("")
    empty = [e for e in ev if not e.get("stats") and not e.get("error")]

    def _cov(e: dict) -> str:
        """这层覆盖了多少击、占活动层多少 —— 各层分母不同的原因，必须写出来。"""
        cov = e.get("coverage")
        base = e.get("base_clicks")
        if cov is None or not base:
            return ""
        if e.get("scoped") is False:
            # 组合层是**超集**：它不受 scope 限制，覆盖全店，所以占比会 >100%
            return f"（这一层不受组合限制，覆盖全店，{e['covered_clicks']} 击 = 本组合的 {cov:.0%}）"
        if cov >= 0.995:
            return ""
        return (f"（覆盖 {e['covered_clicks']} 击 = 活动层的 {cov:.0%}"
                f"，其余 {base - e['covered_clicks']} 击在这张表里没有对应行 —— "
                f"大多是自动/ASIN 定向的活动，它们本来就没有关键词）")

    if sig:
        names = "、".join(e["label"] for e in sig)
        out.append(f"**有可辨差异的层：{names}。**")
        for e in sig:
            st = e["stats"]
            out.append(
                f"- {e['label']}：{st['groups']} 组、{st['clicks']} 击、{st['orders']} 单，"
                f"转化率 {_pct(st['cvr'])}{_cov(e)}。组间差异超出随机波动（p={st['p_value']:.3f}），"
                f"这一层可以说「谁好谁差」。"
            )
    else:
        out.append("**没有任何一层显示出超出随机波动的差异。**")

    if nonsig:
        out.append("")
        out.append(f"**没有可辨差异的层（{len(nonsig)} 个）：**")
        for e in nonsig:
            st = e["stats"]
            det = e.get("detectable") or {}
            r = det.get("detectable_ratio")
            out.append(
                f"- {e['label']}：{st['groups']} 组、{st['clicks']} 击、{st['orders']} 单，"
                f"转化率 {_pct(st['cvr'])}{_cov(e)}。组间差异在随机波动范围内（p={st['p_value']:.3f}），"
                f"**这一层判断不了谁好谁差**——不是没算，是数据不够分辨。"
                + (f"按 {st['clicks']} 击 / {st['orders']} 单 / {st['groups']} 组，这一层只能检出"
                   f"「某组转化率约为其余 **{r:.1f} 倍**」这种量级的差异；比这更小的真实差异，"
                   f"p>0.05 是**检不出**，不是**不存在**。"
                   if r else
                   "样本小到任何量级的差异都检不出。")
            )

    if empty:
        out.append("")
        out.append(f"**样本不足、无法检验的层：{'、'.join(e['label'] for e in empty)}。**")

    # 各层分母不同的原因 —— 诊断 agent 连问三轮的那个问题，写在这里一次说清
    if nonsig or sig:
        out.append("")
        out.append(
            "这几层的点击数不一样，不是因为算错了：关键词层与匹配方式层只覆盖手动投放的活动"
            "（自动/ASIN 定向的活动在 `fact_keyword_daily` 里没有行），活动层覆盖全部，"
            "组合层不受本组合限制、覆盖全店。"
            "**要比较的是同一层内部的组间差异，不是层与层之间的总数。**"
        )

    return out


def _account_wide(f: facts.Facts) -> dict:
    """全店（不限组合）的漏斗。

    2026-11-18 改：以前是从 portfolio 层统计里抠 clicks/orders/cvr，
    那层只有三个数、没有 cost/sales，所以算不出全店自己的盈亏平衡线。
    现在直接用 `f.account_wide`（`account_funnel(..., scoped=False)` 的结果），
    cost/sales/cpc/aov 全都有，和本组合口径完全同构 —— 这样「全店」这个标签
    才有对应的数字支撑。
    """
    wide = getattr(f, "account_wide", None) or {}
    if wide.get("cvr") is None:
        return {}
    be_w = (getattr(f, "breakeven_wide", None) or {}).get("breakeven_cvr")
    return {
        "clicks": wide.get("clicks"),
        "orders": wide.get("orders"),
        "cvr": wide.get("cvr"),
        "cost": wide.get("cost"),
        "sales": wide.get("sales"),
        "cpc": wide.get("cpc"),
        "aov": wide.get("aov"),
        "acos": wide.get("acos"),
        "breakeven_cvr": be_w,
        "note": "全店（所有投放组合合计），不是本组合的数",
    }


def _portfolio_headline(f: facts.Facts) -> str | None:
    """组合层的唯一用法：一句已知事实，不是待办。

    不出「停投/挪预算」提案——跨组合挪预算超出 P0–P1 的 scope，
    且臂力器/美腰周转天数过长、跑步机/划船器已搁置，财务上不宜加投。
    """
    port = next((e for e in (getattr(f, "layer_evidence", None) or [])
                 if e["layer"] == "portfolio"), None)
    if not port or not port.get("stats") or not port["stats"]["significant"]:
        return None
    mine = f.cvr_baseline.get("cvr")
    overall = port["stats"]["cvr"]
    if not mine or not overall:
        return None
    ratio = mine / overall
    if ratio >= 1:
        cmp_text = f"是账户的 **{ratio:.2f} 倍**"
    elif ratio >= 0.9:
        cmp_text = "与账户整体基本持平"
    else:
        cmp_text = f"只有账户的 **{ratio:.2f} 倍**"
    return (
        f"本组合转化率 {_pct(mine)}，账户整体 {_pct(overall)}，{cmp_text}。"
        f"这不是出价能修的问题，问题在产品/Listing/价格层——广告层只能止损，不能做增长。"
    )


def _line_short(f: facts.Facts) -> str:
    """一句话版判定线：只列 0 单与 1 单两档，正文里够用。"""
    mc = f.min_clicks or {}
    p0 = mc.get("baseline_cvr")
    zero = mc.get("min_clicks")
    one = None
    if p0:
        try:
            one = facts.min_clicks_for_orders(p0, 1, confidence=mc.get("confidence") or facts.DEFAULT_CONFIDENCE)
        except Exception:
            one = None
    if zero and one:
        return f"{zero} 击（0 单）/ {one} 击（1 单）"
    return f"{zero} 击" if zero else "样本不足"


def _confidence_basis(f: facts.Facts) -> str:
    """判定线不是一个常数，随「已经出了几单」而变。这里把档位摊开写。

    旧写法给单一数字（274），只在「零单」时才对；出过 1 单的对象真实要 509 击，
    拿 274 去比会过早下结论。见 facts.min_clicks_for_orders。
    """
    mc = f.min_clicks
    if not mc:
        return "样本不足"
    p0 = mc.get("baseline_cvr")
    conf = mc.get("confidence") or facts.DEFAULT_CONFIDENCE
    if not p0:
        return f"用 {conf:.0%} 把握判定「这个词真的不行」，需要 {mc['min_clicks']} 次点击"
    try:
        tbl = facts.min_clicks_table(p0, max_orders=3, confidence=conf)
        rows = tbl.get("rows") or []
    except Exception:
        rows = []
    if not rows:
        return f"用 {conf:.0%} 把握判定「这个词真的不行」，需要 {mc['min_clicks']} 次点击"
    parts = [f"{r['orders']} 单→{r['clicks_needed'] or '算不出'}" for r in rows]
    return (f"用 {conf:.0%} 把握判定「这个词真的不行」，点击线随已经出的单数变："
            + "、".join(parts) + "（单数越多，要证明「确实更差」越难）")


def _who_rows(f: facts.Facts, pack: MemorialPack) -> list[dict]:
    """今天最贵的几笔钱，各自有没有人在管（结构化，md 和 pack 共用一份）。

    为什么需要这个：诊断 agent #124 指出 R01 与 R09 会互相推诿 ——
    R09 说「活动整体无销售额，先解决活动本身（R01 降基础出价或停用）」，
    而 R01 对那个词的处理恰好被 11-16 的观察期锁到 11-30。两条规则都不是错，
    但净效果是花费最高、0 单的那条线 15 天没有任何规则碰它。
    规则层各自看不见这个合成结果，只有报告层同时看得到决策桶和抑制表。
    """
    if not getattr(f, "campaigns", None):
        return []
    # observing 里两种原因必须分开：被人按住的（已批先观察）和数学按住的（证据不够）。
    # 合成一个集合会把「你自己决定先看着」说成「没有规则管」，那是冤枉规则。
    locked_ids = {str(d.campaign_id) for d in pack.observing
                  if d.campaign_id and "已批先观察" in (d.rationale or "")}
    weak_ids = {str(d.campaign_id) for d in pack.observing
                if d.campaign_id and "已批先观察" not in (d.rationale or "")}
    front_ids = {str(d.campaign_id) for d in (pack.front or []) if d.campaign_id}
    below_ids = {str(d.campaign_id) for d in (pack.below or []) if d.campaign_id}
    over_ids = {str(d.campaign_id) for d in (pack.overflow or []) if d.campaign_id}

    out = []
    for c in sorted(f.campaigns, key=lambda c: -(c.get("cost") or 0))[:3]:
        cid = str(c["campaign_id"])
        if cid in front_ids:
            kind, how = "有动作", "有，在待批里"
        elif cid in over_ids:
            kind, how = "有动作", "有，但被配额挤到溢出区"
        elif cid in locked_ids:
            kind, how = "被锁", "没有 —— 你批过「先观察」，锁住"
        elif cid in weak_ids:
            kind, how = "证据不够", "没有 —— 证据不够，只看不动"
        elif cid in below_ids:
            kind, how = "未达线", "没有 —— 未达判定线"
        else:
            kind, how = "无人管", "没有 —— 今天没有任何规则对它出手"
        out.append({
            "campaign_id": cid,
            "campaign": (c.get("campaign_name") or cid),
            "cost": round(c.get("cost") or 0.0, 2),
            "orders": c.get("orders") or 0,
            "kind": kind,
            "how": how,
        })
    return out


def _who_manages(f: facts.Facts, pack: MemorialPack, res: rules.RuleResult) -> list[str]:
    rows = _who_rows(f, pack)
    if not rows:
        return []
    n_locked = sum(1 for r in rows if r["kind"] == "被锁")
    n_weak = sum(1 for r in rows if r["kind"] == "证据不够")
    n_bare = sum(1 for r in rows if r["kind"] == "无人管")
    n_act = sum(1 for r in rows if r["kind"] == "有动作")
    L: list[str] = []
    L.append("**今天最贵的三笔钱，各自谁在管：**")
    L.append("")
    L.append("| 活动 | 花费 | 单 | 今天有没有动作 |")
    L.append("|---|---:|---:|---|")
    for r in rows:
        bold = "**" if r["kind"] in ("被锁", "无人管") else ""
        L.append(f"| {r['campaign'][:44]} | {_money(r['cost'])} | {r['orders']} "
                 f"| {bold}{r['how']}{bold} |")
    L.append("")
    if not n_act:
        parts = []
        if n_locked:
            parts.append(f"{n_locked} 个被你自己批的「先观察」锁住")
        if n_weak:
            parts.append(f"{n_weak} 个是证据不够只看不动")
        if n_bare:
            parts.append(f"{n_bare} 个连一条记录都没有")
        tail = ("；".join(parts) + "。") if parts else ""
        L.append(f"这三笔钱今天一条可执行动作都没有。{tail}"
                 f"规则各自都有理由，但**叠起来的结果是最大的花费没人动** —— "
                 f"这段话不是为了催你动手，是让你知道「今天没动作」不等于「这些钱没问题」。")
        L.append("")
    return L


def render_report(f: facts.Facts, res: rules.RuleResult, run_id: int | None,
                  snap_info: dict | None, diff_info: dict | None,
                  pack: MemorialPack | None = None) -> str:
    pack = pack or compress(f, res)
    L: list[str] = []
    a = f.account
    mc = f.min_clicks.get("min_clicks")

    L.append(f"# 广告奏折 · {f.end}")
    L.append("")
    if getattr(f, "scope_label", ""):
        L.append(f"范围 **{f.scope_label}**。")
    L.append(f"数据窗口 **{f.start} ~ {f.end}**（{f.window_days} 天），"
             f"已排除最近 {facts.DEFAULT_ATTR_EXCLUDE_DAYS} 天归因未完成的点击。")
    if run_id:
        L.append(f"运行编号 `#{run_id}`。")
    L.append("批了只记台账，**不代写广告后台**。")
    L.append("")

    # ---------- 一、今日结论 ----------
    L.append("## 一、今日结论")
    L.append("")
    L.append(f"窗口花 {_money(a['cost'])}、{a['clicks']} 击、{a['orders']} 单，"
             f"ACOS {_pct(a['acos'])}（目标 {_pct(f.target_acos)}），"
             f"转化率 {_pct(a['cvr'])}，盈亏平衡线 {_pct(f.breakeven.get('breakeven_cvr'))}。")
    if f.target_reachable:
        L.append("按现在的点击成本和客单价，这个目标**够得着**。")
    else:
        L.append("按现在的点击成本和客单价，这个目标**够不着**。降出价只能少花冤枉钱，不能把转化率抬上去。")
    L.append("")
    # 今日无动作时用正式表述，不再留空白让人以为系统坏了
    if pack.front:
        L.append(f"今天建议动 **{len(pack.front)}** 条"
                 f"（预计省 {_money(pack.saved)}，预计多花 {_money(pack.spent)}）；"
                 f"需要你判断的问题 **{len(pack.questions)}** 条；"
                 f"判定线 {_line_short(f)}。")
    else:
        L.append(f"**今天没有达到判定线的动作。** 判定线 {_line_short(f)}，"
                 f"需要你判断的问题 {len(pack.questions)} 条。")
        below_n = len(pack.below)
        if below_n:
            L.append(f"有 {below_n} 条已经算过、但样本没攒够，列在第五节，只观察不动。"
                     f"这不是系统没跑，是数据还不够分辨好坏。")
    for n in pack.notes:
        L.append(n)
    L.append("")

    # 组合层：只作已知事实，不出动作
    headline = _portfolio_headline(f)
    if headline:
        L.append(f"> **已知事实**：{headline}")
        L.append(">")
        L.append("> 这条不构成停投或挪预算建议。")
        L.append("")

    if pack.alerts:
        for al in pack.alerts:
            L.append(f"> {al.rationale}")
        L.append("")

    # 分层证据：哪层有差异、哪层没有
    layer_lines = _layer_lines(f)
    if layer_lines:
        L.append("### 哪一层才真的有差异")
        L.append("")
        L.extend(layer_lines)
        L.append("")

    # 今天最贵的钱谁在管（放在结论后、策略前 —— 先说钱，再说动作）
    who = _who_manages(f, pack, res)
    if who:
        L.extend(who)

    # ---------- 二、今日策略 ----------
    L.append("## 二、今日策略")
    L.append("")
    if not pack.front:
        L.append("今天没有达到判定线、又排进配额的动作。不凑数。")
        L.append("")
    else:
        L.append("| # | 做什么 | 对象 | 现在 | 改成 | 影响金额 | 证据 |")
        L.append("|---:|---|---|---|---|---:|---|")
        for i, d in enumerate(pack.front, 1):
            L.append(
                f"| {i} | {ACTION_LABEL.get(d.action_type, d.action_type)} "
                f"| {d.entity_label or d.entity_id} "
                f"| {_value(d.current_value)} | {_value(d.proposed_value)} "
                f"| {_impact(d.spend_impact)} | {_evidence_label(d)} |"
            )
        L.append("")
        L.append("请按条批：同意、改数字、或今天不执行。没表态的先留在台账里，不视为否决。")
        L.append("")

    # ---------- 三、问题 ----------
    L.append("## 三、需要你判断的问题")
    L.append("")
    if not pack.questions:
        L.append("今天没有必须你拍板的人因问题。")
        L.append("")
    else:
        L.append("未勾选视为尚未表态，不视为排除。跳过整题时，按保守项执行并记观察。")
        L.append("")
        for i, q in enumerate(pack.questions, 1):
            L.append(f"**{i}. [{q['id']}] {q['object']}**")
            L.append("")
            L.append(q["ask"])
            L.append("")
            for opt in q["options"]:
                L.append(f"- [ ] {opt}")
            L.append("")

    # ---------- 四、结构冲突 ----------
    L.append("## 四、结构冲突（同词多活动）")
    L.append("")
    if not pack.conflicts:
        L.append("窗口里没有「同一个词同时出现在两个及以上活动、且两边都有量」的情况。")
        L.append("")
    else:
        L.append(f"共 {len(pack.conflicts)} 组。先看带标记的。")
        L.append("")
        for c in pack.conflicts:
            flag = "；".join(c["flags"]) if c["flags"] else "待看"
            L.append(f"### {c['keyword_text']} · 合计 {_money(c['total_cost'])} · {flag}")
            L.append("")
            L.append("| 活动 | 点击 | 订单 | 花费 | CPC | 转化率 | ACOS | 当前出价 | 花费占比 |")
            L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
            for m in c["campaigns"]:
                L.append(
                    f"| {m['campaign_name']} | {m['clicks']} | {m['orders']} "
                    f"| {_money(m['cost'])} | {_money(m['cpc'])} | {_pct(m['cvr'])} "
                    f"| {_pct(m['acos'])} | {_money(m['bid'])} | {_pct(m['share'])} |"
                )
            L.append("")

    # ---------- 五、只观察 ----------
    L.append("## 五、今日只观察")
    L.append("")
    if pack.overflow:
        L.append(f"**已达判定线、但超出配额的 {len(pack.overflow)} 条**（不占今天名额）：")
        L.append("")
        for d in pack.overflow:
            L.append(f"- {ACTION_LABEL.get(d.action_type, d.action_type)} · "
                     f"{d.entity_label or d.entity_id} · {_impact(d.spend_impact)}")
        L.append("")
    if pack.below:
        L.append(f"**未达判定线的 {len(pack.below)} 条**：")
        L.append("")
        for d in pack.below:
            sig = d.significance or {}
            need = sig.get("min_clicks_needed")
            if need:
                sample = f"{sig.get('clicks')} 击 / 线 {need}"
            else:
                sample = f"{sig.get('clicks')} 击（这类动作不设点击判定线）"
            tail = ""
            if "已批先观察" in (d.rationale or ""):
                # 已批过的不用再劝一次，标出来免得以为系统没记住
                import re as _re
                m = _re.search(r"(\d{4}-\d{2}-\d{2}) 已批先观察[^，]*，观察期至 (\d{4}-\d{2}-\d{2})",
                               d.rationale or "")
                if m:
                    tail = f"。**{m.group(1)} 已批先观察，观察期至 {m.group(2)}**"
            L.append(f"- {d.entity_label or d.entity_id} — "
                     f"{ACTION_LABEL.get(d.action_type, d.action_type)}，{sample}{tail}，先记着")
        L.append("")
    if pack.observing:
        # 这里必须逐条说清「为什么在这里」。R30 那条告警**不能**说 ——
        # 它跑在 rules.run_all 里，看不见门控和观察期锁的结果。
        # 诊断 agent #122 指出：payload 里本来就分得清（哪条被人工锁、哪条是
        # confidence=low、有没有 overflow），告警说自己"分不清"是假免责。
        # 所以这份解释放在报告层，只有这里同时看得到三个桶和 suppressed。
        L.append(f"**只看不动的 {len(pack.observing)} 条**（按在这里的原因分开）：")
        L.append("")
        L.append("| 对象 | 动作 | 为什么在这里 | 涉及花费 |")
        L.append("|---|---|---|---:|")
        held_total = 0.0
        for d in pack.observing:
            r = d.rationale or ""
            if "已批先观察" in r:
                import re as _re
                m = _re.search(r"(\d{4}-\d{2}-\d{2}) 已批先观察[^，]*，观察期至 (\d{4}-\d{2}-\d{2})", r)
                why = (f"**你在 {m.group(1)} 批了「先观察」，锁到 {m.group(2)}** —— 不是证据不够，"
                       f"是已经决定先看着" if m else "你在观察期内批过「先观察」")
            elif d.confidence == "low":
                sig = d.significance or {}
                base = (f"证据不够（{d.confidence}）：{sig.get('clicks')} 击 / "
                        f"{sig.get('orders')} 单")
                if sig.get("min_clicks_needed"):
                    base += f"，要判「确实更差」得攒到 {sig['min_clicks_needed']} 击"
                why = base
            else:
                why = "规则标成只看不动"
            if d.spend_impact:
                held_total += abs(d.spend_impact)
            L.append(f"| {d.entity_label or d.entity_id} | {ACTION_LABEL.get(d.action_type, d.action_type)} "
                     f"| {why} | {_impact(d.spend_impact)} |")
        L.append("")
        if held_total:
            L.append(f"这 {len(pack.observing)} 条合计涉及 **{_impact(held_total)}**，"
                     f"今天一条都不会动。")
            L.append("")
    if res.suppressed:
        c = Counter(s["reason"] for s in res.suppressed)
        L.append("**被保护拦下的：**")
        L.append("")
        L.append("| 原因 | 数量 | 含义 |")
        L.append("|---|---:|---|")
        meanings = {
            "花费闸门": "花的钱还不够一单的利润，动了也是噪声",
            "核心词保护": "每单成本明显优于账户平均，ACOS 高只是客单价低，不许降",
            "CVR 低于盈亏平衡": "这个词加价只会放大亏损",
            "空转": "按 0.01 步长算完和现在一样，改了等于没改",
            "目标不可达": "全店目标不现实，降出价解决不了",
            "证据不足": "按它现在的订单数，要证明「确实更好/更差」还得再攒点击；加钱方向一律要「证据够」才放行",
            "活动整体无销售额": "整个活动一单没有，先解决活动本身，不是在活动里调位次",
            "位次加价已为 0，无法再降": "这个位次已经不加价了，只能去调基础出价或停用活动",
            "占活动花费比例过小": "这一块花的钱太少，动了也是噪声",
            "活动整体不如全店平均，不该加价": "活动本身就跑输大盘，往里加钱只会放大亏损",
        }
        for reason, n in c.most_common():
            L.append(f"| {reason} | {n} | {meanings.get(reason, '')} |")
        L.append("")
        L.append("<details><summary>展开被拦下的对象</summary>")
        L.append("")
        for s in res.suppressed:
            L.append(f"- `{s['rule_id']}` {s['entity']} — **{s['reason']}** "
                     f"{json.dumps(s.get('detail') or {}, ensure_ascii=False)}")
        L.append("")
        L.append("</details>")
        L.append("")
    if not (pack.overflow or pack.below or pack.observing or res.suppressed):
        L.append("没有观察项。")
        L.append("")

    # ---------- 六、依据与计算 ----------
    L.append("## 六、依据与计算")
    L.append("")
    L.append(f"**判定线（随单数变）**：{_confidence_basis(f)}。")
    _bias = f.cpc_bias.get("overall")
    _nominal = f.cpc_bias.get("nominal_overall")
    if _bias is not None:
        L.append(f"**CPC 校准系数 {_bias:.3f}**（相对**有效出价**，样本 {f.cpc_bias.get('n')}）"
                 + (f"。名义口径 median(cpc/bid) 是 {_nominal}，但那是把 0% 和 350% 溢价的活动"
                    f"混在一起取中位数的产物，不代表换算关系 —— 溢价已计入分母，不能再乘一次。"
                    if _nominal and abs(_nominal - _bias) > 0.3 else "。"))
        _ca = f.cpc_bias.get("campaign_adj") or {}
        _by_camp = f.cpc_bias.get("by_campaign") or {}
        if _by_camp:
            # 只列**有手动关键词行**的活动 —— 自动/ASIN 定向活动没有 keyword 行，
            # 列进来只会得到一排「—」，让人以为那些活动的比值算不出来。
            _rows = sorted(((c, _ca.get(c) or {}) for c in _by_camp),
                           key=lambda kv: -kv[1].get("clicks", 0))[:4]
            L.append("")
            L.append("| 活动 | 关键词点击 | 点击加权溢价 | cpc/有效出价 |")
            L.append("|---|---:|---:|---:|")
            for _cid, _v in _rows:
                _med = (_by_camp.get(_cid) or {}).get("median")
                _med_s = f"{_med:.3f}" if _med is not None else "—"
                L.append(f"| {_cid} | {_v.get('clicks', 0)} | {(_v.get('adj_pct') or 0):.1f}% | {_med_s} |")
            L.append("")
            L.append("溢价高的活动 `cpc/有效出价` 一样落在 0.8–1.0，说明溢价被完整传递到成交价，"
                     "不存在额外的神秘放大。**降 base bid 的目标价必须先除 (1+溢价)**，"
                     "否则对 280% 溢价的活动会把目标名义价算高 3.8 倍。")
    L.append("")
    fl = f.floor
    L.append(f"**出价地板 ≈ {_money(fl.get('floor'))}**（可信度 {fl.get('confidence')}；"
             f"口径是**有效出价** = 名义 bid × (1+位次溢价)）。")
    if not fl.get("estimable"):
        L.append("这个地板是粗估，不是对照实验测出来的。")
    L.append("按名义 bid 分桶会算出一个假的低地板：名义 $0.40 那三个词的溢价是 "
             "142%/280%/321%，有效出价 $0.97/$1.52/$1.68 —— **没有任何一个词真的用 "
             "$0.40 买过点击**。所以拿有效地板约束某个活动时，要先除以该活动的 "
             "`(1+点击加权溢价)` 才是它的名义 bid 下限。")
    L.append("")

    L.append("### 配置最近变过什么")
    L.append("")
    if diff_info and diff_info.get("changes"):
        L.append(f"对比 {diff_info['from_snap']} → {diff_info['to_snap']}，"
                 f"发现 {diff_info['changes']} 处变化"
                 f"（{'两次快照来自不同时刻的抓取，可信' if diff_info.get('reliable') else '**两次快照是同一时刻抓的，无法证明中间真的变过**'}）。")
    elif diff_info:
        L.append(f"对比 {diff_info['from_snap']} → {diff_info['to_snap']}，没有变化。")
        if not diff_info.get("reliable"):
            L.append("但这两次快照是同一时刻抓的，所以「没变化」不能当作事实。")
    else:
        L.append("只有一份快照，还比不了。")
    if snap_info:
        L.append(f"本次快照：{snap_info.get('snap_date')}，{snap_info.get('rows')} 行配置。")
    L.append("")

    if pack.front:
        L.append("### 每条建议怎么算的")
        L.append("")
        for i, d in enumerate(pack.front, 1):
            L.append(f"<details><summary>{i}. {ACTION_LABEL.get(d.action_type, d.action_type)} · {d.entity_label}</summary>")
            L.append("")
            L.append(d.rationale or "（无说明）")
            L.append("")
            L.append("```")
            for step in d.calculation or []:
                title = step.get("step", "")
                formula = step.get("formula", "")
                params = step.get("params", {})
                result = step.get("result")
                meaning = step.get("meaning")
                L.append(f"{title}")
                if formula:
                    L.append(f"  公式：{formula}")
                if params:
                    L.append(f"  参数：{json.dumps(params, ensure_ascii=False)}")
                if isinstance(result, (int, float, str)) or result is None:
                    L.append(f"  结果：{result}")
                else:
                    L.append(f"  结果：{json.dumps(result, ensure_ascii=False)}")
                if meaning:
                    L.append(f"  含义：{meaning}")
                L.append("")
            L.append("```")
            L.append("")
            sig = d.significance or {}
            L.append(f"样本：{sig.get('clicks')} 次点击 / {sig.get('orders')} 单，"
                     f"判定线 {sig.get('min_clicks_needed')} 次点击 → "
                     f"{'达到统计要求' if sig.get('statistically_conclusive') else '未达统计要求'}。")
            L.append("")
            L.append("</details>")
            L.append("")

    # ---------- 七、agent 反对 ----------
    # 占位。真正的反对内容由 diagnose 跑完后回填（update_agent_block）——
    # 链路顺序是 engine 先出奏折、diagnose 后挑刺，engine 阶段还读不到反对。
    # 但区块标题必须现在就写：没有它，回填时无从定位，只能整份重排。
    L.append(agent_block(None, run_id))
    L.append("")
    L.append(FOOTER.strip())
    return "\n".join(L)


# ------------------------------------------------------------------ agent 反对区
#
# 为什么要有标记注释：奏折是 engine 写的，反对是 diagnose 之后才产生的。
# 两个进程、两个时刻、同一个文件。靠「重新渲染整份」不行（engine 的 Facts
# 已经不在内存里），只能定位替换。用显式标记比正则猜标题可靠 —— 标题文案
# 以后要改，标记不用动。

AGENT_MD_START = "<!-- AGENT_OBJECTIONS_START -->"
AGENT_MD_END = "<!-- AGENT_OBJECTIONS_END -->"

FOOTER = """
---

以上由规则引擎从积加报表算出。这些建议还没有执行；执行须另批，且每次改动留快照可回滚。
"""


def _objection_lines(objs: list[dict] | None, agent_state: str | None = None,
                     err: str | None = None) -> list[str]:
    """把反对清单渲染成 md 行。objs 为 None 表示「还没跑」或「跑失败」。"""
    L: list[str] = []
    if objs is None:
        if err:
            L.append(f"诊断 agent **今天没跑成**：{err}")
        elif agent_state == "failed":
            L.append("诊断 agent **今天没跑成**（agent_run 记了 failed，原因见 Agent 面板）。")
        else:
            L.append("诊断 agent **今天还没跑**。规则引擎的结论不受影响。")
        return L

    if not objs:
        L.append("诊断 agent 今天看过了，**没有要反对的**。")
        return L

    n_oppose = sum(1 for o in objs if o.get("verdict") == "oppose")
    n_doubt = len(objs) - n_oppose
    L.append(f"诊断 agent 今天提了 **{len(objs)} 条**（反对 {n_oppose} / 存疑 {n_doubt}）。"
             f"它不提案，只质疑；下面是它的原话与它自己下的兑现承诺。")
    L.append("")
    for o in objs:
        verdict = o.get("verdict") or "doubt"
        tag = "反对" if verdict == "oppose" else "存疑"
        did = o.get("decision_id")
        L.append(f"**{tag} · decision #{did}**")
        L.append("")
        L.append(o.get("why") or "（没写理由）")
        L.append("")
        preds = o.get("predicts") or []
        if preds:
            L.append("<details><summary>它承诺的兑现条件（{n} 条）</summary>".format(n=len(preds)))
            L.append("")
            L.append("| 断言 | 何时可验 | 怎么验 | 预期 |")
            L.append("|---|---|---|---|")
            for pr in preds:
                L.append("| {c} | {v} | {m} | {e} |".format(
                    c=(pr.get("claim") or "").replace("|", "\\|"),
                    v=pr.get("verifiable_at") or "—",
                    m=(pr.get("method") or "").replace("|", "\\|"),
                    e=(pr.get("expected") or "").replace("|", "\\|"),
                ))
            L.append("")
            L.append("</details>")
            L.append("")
        outcome = o.get("outcome")
        if outcome and outcome != "pending":
            label = {"correct": "已兑现（它说对了）", "wrong": "没兑现（它说错了）",
                     "unverifiable": "判不了"}.get(outcome, outcome)
            L.append(f"兑现判定：**{label}**" + (f" —— {o['actual']}" if o.get("actual") else ""))
            L.append("")
    return L


def agent_block(objs: list[dict] | None, run_id: int | None,
                agent_state: str | None = None, err: str | None = None) -> str:
    """渲染「七、agent 反对」整块，含定位标记。"""
    L = [AGENT_MD_START, "## 七、agent 反对", ""]
    L.extend(_objection_lines(objs, agent_state=agent_state, err=err))
    L.append(AGENT_MD_END)
    return "\n".join(L)


def rewrite_agent_block(md_path: str, objs: list[dict] | None, run_id: int | None,
                        agent_state: str | None = None,
                        err: str | None = None) -> bool:
    """把奏折 md 里的 agent 反对区替换成最新内容。找不到标记返回 False。

    diagnose 跑完后调它。失败不是错误：奏折照旧，只是少了这一块。
    """
    import os
    if not os.path.exists(md_path):
        return False
    with open(md_path, "r", encoding="utf-8") as fh:
        txt = fh.read()
    i = txt.find(AGENT_MD_START)
    j = txt.find(AGENT_MD_END)
    if i < 0 or j < 0 or j < i:
        return False
    new = agent_block(objs, run_id, agent_state=agent_state, err=err)
    txt = txt[:i] + new + txt[j + len(AGENT_MD_END):]
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(txt)
    return True


def pack_json(f: facts.Facts, pack: MemorialPack, run_id: int | None,
              suppressed: list[dict] | None = None) -> dict:
    def dec(d: rules.Decision) -> dict:
        return {
            # decision_id 由 run.save_decisions 回填；dry-run 时是 None
            "decision_id": getattr(d, "decision_id", None),
            "rule_id": d.rule_id,
            "entity_type": d.entity_type,
            "entity_id": d.entity_id,
            "entity_label": d.entity_label,
            "action_type": d.action_type,
            "action_label": ACTION_LABEL.get(d.action_type, d.action_type),
            "current_value": d.current_value,
            "proposed_value": d.proposed_value,
            "spend_impact": d.spend_impact,
            "confidence": d.confidence,
            "rationale": d.rationale,
            # 状态 vs 事件。R30 输入未变时 status="state"：它照样要出现在工作台与
            # agent 的输入里（消失等于骗人说问题解决了），但不写 decision_log。
            # 少了这一个字段，前端没法把「今天还是那样」和「今天出了新告警」分开显示，
            # 诊断 agent 也没法解释「为什么这条没有 decision_id」。
            "status": d.status,
            # 工作台要能展开「这条是怎么算的」，不能只给一句结论
            "calculation": d.calculation,
            "significance": d.significance,
            "campaign_id": d.campaign_id,
            "ad_group_id": d.ad_group_id,
            # 证据闸门：为什么按住这条（只由引擎写），以及它是否属于「已批先观察」
            # 而本轮不重新落库的。前端靠 held 把 locked 与 weak 分开：
            # locked 只能看，weak 才能显式强制批准。
            "gate_reason": getattr(d, "gate_reason", None),
            "held": bool(getattr(d, "held", False)),
            # 历史战绩：这条规则过去几次执行的结果、这个对象自己几次被改坏过。
            # 没有就整条不出现 —— 空字符串会让前端多画一行没内容的字。
            "rule_record": (d.context or {}).get("rule_record") if isinstance(d.context, dict) else None,
            "object_record": (d.context or {}).get("object_record") if isinstance(d.context, dict) else None,
        }

    def conflict(c: dict) -> dict:
        """结构冲突明细。工作台面板③要按「实际 CPC / 该位次溢价上限」判断。"""
        return {
            "keyword_text": c.get("keyword_text"),
            "campaigns": [
                {
                    "campaign_id": cp.get("campaign_id"),
                    "campaign_name": cp.get("campaign_name"),
                    "keyword_ids": cp.get("keyword_ids"),
                    "clicks": cp.get("clicks"),
                    "orders": cp.get("orders"),
                    "cost": cp.get("cost"),
                    "sales": cp.get("sales"),
                    "bid": cp.get("bid"),
                    "cpc": cp.get("cpc"),
                    "cvr": cp.get("cvr"),
                    "acos": cp.get("acos"),
                    "share": cp.get("share"),
                    # 位次溢价上限：由 placement 表的 bid_adjustment 现算，见 §7.3 勘误
                    "placement_cap": cp.get("placement_cap"),
                    "placement_cap_pct": cp.get("placement_cap_pct"),
                }
                for cp in (c.get("campaigns") or [])
            ],
        }

    def question(q: dict) -> dict:
        return {
            "id": q.get("id"),
            "object": q.get("object"),
            "ask": q.get("ask"),
            "options": q.get("options") or [],
            # 每个选项对应的沉淀去向（policy / param_override / observation_task / experiment）
            "reason_codes": q.get("reason_codes") or [],
            # 与 options 同下标的机器可读去向：destination/scope/object_id/
            # param_key/param_value/ttl_days/metric/judge_rule/hypothesis/change/control。
            # 审批侧照这个写库，不在接口层重算业务口径；settled_* 非空 = 该码已在生效期内。
            "option_meta": q.get("option_meta") or [],
            # 这题挂在哪条建议上（rule_id:entity_id:action_type），Q08/Q05 之外都有
            "decision_ref": q.get("decision_ref"),
        }

    return {
        "window_start": f.start,
        "window_end": f.end,
        "run_id": run_id,
        "front": [dec(d) for d in pack.front],
        # 另外三个桶也要进 pack：只给 front 会让读的人（和 agent）以为「今天只有这几条」，
        # 而 observe / overflow / below 是引擎真的算过、只是被置信度或配额挡下的东西。
        # 看不见它们，就会把「引擎判了只观察」误读成「引擎漏了」。
        "observing": [dec(d) for d in pack.observing],
        "overflow": [dec(d) for d in pack.overflow],
        "below": [dec(d) for d in pack.below],
        "questions": [question(q) for q in pack.questions],
        "conflicts": [conflict(c) for c in pack.conflicts],
        "conflict_count": len(pack.conflicts),
        "overflow_count": len(pack.overflow),
        "below_count": len(pack.below),
        # 引擎自己的旁注：哪些对象被观察期挡下、哪些已经表过态不再重复列出。
        # 只进 md 的话，页面读的人会以为「观察区怎么少了一条」。
        "notes": list(pack.notes or []),
        "saved": pack.saved,
        "spent": pack.spent,
        "target_reachable": f.target_reachable,
        "layer_evidence": [
            {
                "layer": e["layer"],
                "label": e["label"],
                "significant": (e["stats"] or {}).get("significant"),
                "p_value": (e["stats"] or {}).get("p_value"),
                "groups": (e["stats"] or {}).get("groups"),
                "clicks": (e["stats"] or {}).get("clicks"),
                "orders": (e["stats"] or {}).get("orders"),
                # 各层分母不同的原因：keyword/match_type 只覆盖手动投放的活动，
                # portfolio 不受 scope 限制。不写出来，读的人会一直问
                # 「816 击和 1197 击是不是不在同一批数据上算的」。
                "scoped": e.get("scoped"),
                "covered_clicks": e.get("covered_clicks"),
                "base_clicks": e.get("base_clicks"),
                "coverage": e.get("coverage"),
                # p>0.05 只说明「没辨认出差异」。这一层能检出多大倍数，决定了
                # 「无差异」该当结论还是当证据不足 —— agent 的 Q D1 就是问这个。
                "detectable_ratio": (e.get("detectable") or {}).get("detectable_ratio"),
            }
            for e in (getattr(f, "layer_evidence", None) or [])
        ],
        "portfolio_headline": _portfolio_headline(f),
        # 今天最贵的几笔钱各自谁在管。agent #124：R01 与 R09 会互相推诿，
        # 净效果是最大的花费没人动。规则各自看不见这个合成结果，只有这里看得见。
        "who_manages": _who_rows(f, pack),
        # 诊断 agent 的输入契约要这两块（计划书 §8.3）。suppressed 此前只进 md，
        # 落库后 agent 才能知道「哪些对象被什么理由挡在门外」——这正是它该质疑的地方。
        # 账户级告警（R30）没有可打钩的对象，但它照样是「已经存在的一条建议」，
        # agent 要对它说 doubt 就得看得见它。单独一块，不混进 front。
        "alerts": [dec(d) for d in pack.alerts],
        # 注意区分两个「账户」：f.account 是**范围内**（只算 scope 里的活动）的漏斗，
        # 而 portfolio 层的统计是**全账户**的。两者同名会让读的人（和 agent）当场问
        # 「同一页为什么两个 CVR」，所以这里显式分成 scope 与 account 两块。
        "scope": {
            "label": f.scope_label,
            "cost": f.account.get("cost"),
            "clicks": f.account.get("clicks"),
            "orders": f.account.get("orders"),
            "sales": f.account.get("sales"),
            "impressions": f.account.get("impressions"),
            "acos": f.account.get("acos"),
            "cvr": f.account.get("cvr"),
            "cpc": f.account.get("cpc"),
            "aov": f.account.get("aov"),
        },
        "account": _account_wide(f),
        "breakeven": {
            "breakeven_cvr": f.breakeven.get("breakeven_cvr"),
            "target_reachable": f.target_reachable,
        },
        # CPC 校准系数。此前只在 md 第六节里出现、没进 pack，于是诊断 agent
        # （Q D3）连问「2.260 这个系数按什么口径算的，还站得住吗」——
        # 而真相是那个数根本不该是 2.26：它是把 0% 和 350% 溢价的活动混在一起
        # 取中位数的产物。现在有效口径与名义口径都给出，并附逐活动分解，
        # 让 agent 能自己看出「溢价高的活动 cpc/有效出价 一样落在 0.8–1.0」。
        "cpc_bias": {
            "overall": f.cpc_bias.get("overall"),
            "nominal_overall": f.cpc_bias.get("nominal_overall"),
            "n": f.cpc_bias.get("n"),
            "p10": f.cpc_bias.get("p10"),
            "p90": f.cpc_bias.get("p90"),
            "note": ("overall 是相对**有效出价**（bid × (1+位次溢价)）的比值；"
                     "nominal_overall 是相对名义 bid 的比值，把 0% 与 350% 溢价的活动"
                     "混在一起取中位数，不代表换算关系。降 bid 的目标价要先除 "
                     "cpc_bias 再除 (1+溢价)。"),
            "by_campaign": [
                {
                    "campaign_id": cid,
                    "median": (m or {}).get("median"),
                    "n": (m or {}).get("n"),
                    "adj_pct": (m or {}).get("adj_pct"),
                }
                for cid, m in (f.cpc_bias.get("by_campaign") or {}).items()
            ],
        },
        "floor": {
            "floor": f.floor.get("floor"),
            "basis": f.floor.get("basis"),
            "estimable": f.floor.get("estimable"),
            "confidence": f.floor.get("confidence"),
            "note": ("floor 是**有效出价**口径（bid × (1+位次溢价)），不是名义 bid。"
                     "约束某活动的名义 bid 时要先除以它的 (1+点击加权溢价/100)。"),
        },
        "suppressed": [
            {
                "rule_id": s.get("rule_id"),
                "entity": s.get("entity"),
                "reason": s.get("reason"),
                "detail": s.get("detail") or {},
            }
            for s in (suppressed or [])
        ],
    }
