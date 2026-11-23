"""L2 动作层 —— 规则引擎。

每条规则做三件事：
  1. 从 Facts 里找出候选对象
  2. 算出建议动作 + 完整计算链（可复算）
  3. 过四道保护：置信度门控 / 目标可达性 / 地板校验 / 冷却期

规则不做的事：不写后台、不假装知道最优值、不隐藏样本不足。
样本不够就降级成 observe，让报告里写着「还不够，继续观察」。

规则编号沿用 05-模块3 的 30 类编号，保证文档与代码可对照。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any

from facts import (Facts, DEFAULT_TARGET_ACOS, RANK_SNAPSHOT_MAX_AGE_HOURS,
                   DEFAULT_CONFIDENCE, significance as binom_significance,
                   target_acos_required)

RULE_VERSION = "1.0"
BID_STEP_MAX = 0.20          # 单次 bid 变更幅度上限（IvyeaOps guardrail 20%）
BID_TICK = 0.01              # Amazon 最小 bid 步长
COOLDOWN_HOURS_BID = 72      # 同一对象 72 小时内不重复改 bid
EXPIRY_HOURS = 72            # 建议 72 小时未处理自动过期


# ---------------------------------------------------------------- 数据结构

@dataclass
class Decision:
    rule_id: str
    rule_version: str
    entity_type: str
    entity_id: str
    entity_label: str
    action_type: str
    confidence: str                      # low | medium | high
    context: dict
    calculation: list
    significance: dict
    rationale: str
    market_id: int | None = None
    campaign_id: str | None = None
    ad_group_id: str | None = None
    current_value: Any = None
    proposed_value: Any = None
    spend_impact: float | None = None    # 正 = 预计省下；负 = 预计多花
    status: str = "proposed"
    cooldown_until: str | None = None
    expires_at: str | None = None
    # 证据闸门：引擎按住这条的原因（人话）。只由引擎写，工作台写白名单不含此列。
    gate_reason: str | None = None
    # 被 hold_recent_observe 从待批挪进观察区的（你之前批过「先观察」）。
    # 这类条目不重新落库 —— 旧 run 里已有它的账本行，重复写只会让账本长噪声。
    held: bool = False
    # 落库后由 save_decisions 回填。规则计算阶段恒为 None；
    # 工作台与 agent 都要靠它把「一条建议」定位到「账本里的一行」。
    decision_id: int | None = None

    def to_row(self) -> dict:
        d = asdict(self)
        # decision_id 是库里的自增主键，写库时必须剔除
        d.pop("decision_id", None)
        # held 是运行期语义（「这条被观察期按住、本轮不落库」），不是账本列
        d.pop("held", None)
        d["context_json"] = json.dumps(d.pop("context"), ensure_ascii=False, default=str)
        d["calculation_json"] = json.dumps(d.pop("calculation"), ensure_ascii=False, default=str)
        d["significance_json"] = json.dumps(d.pop("significance"), ensure_ascii=False, default=str)
        d["decided_at"] = datetime.now().isoformat(timespec="seconds")
        return d


@dataclass
class RuleResult:
    decisions: list[Decision] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)   # 被保护拦下的，也要报出来

    def suppress(self, rule_id: str, entity: str, reason: str, detail: dict | None = None):
        self.suppressed.append({
            "rule_id": rule_id, "entity": entity, "reason": reason, "detail": detail or {},
        })


# ---------------------------------------------------------------- 工具

def _tick(x: float) -> float:
    """夹到 Amazon 的最小步长，并保证 ≥ 0.02。"""
    return max(0.02, round(round(x / BID_TICK) * BID_TICK, 2))


def _confidence(clicks: int, need: int) -> str:
    """旧口径：拿一个固定常数 need 去比所有对象。

    ⚠ 已不用于 R01 / R03 / R09 的方向判定，只保留给报告与历史字段。
    原因：这个常数只回答「零单是不是异常」，对已经出了 k 单的对象，
    真正该比的是「按它的订单数还需要多少点击」（facts.min_clicks_for_orders），
    而且降和加是两个方向，不能共用一个数字。改用 _evidence()。
    """
    if need <= 0:
        return "low"
    if clicks >= need:
        return "high"
    if clicks >= need / 2:
        return "medium"
    return "low"


# 加价比降价更严：即使统计显著，点击太少也不加，避免小样本假阳性
RAISE_MIN_CLICKS = 30

# 方向 → 允许的最高 p 值（等价于 confidence）
_EVIDENCE_ALPHA = 1.0 - DEFAULT_CONFIDENCE


def _evidence(clicks: int, orders: int, baseline_cvr: float,
              direction: str) -> dict:
    """按方向做二项检验，返回置信度分级 + 完整证据。

    direction:
      "worse"  —— 要证明它比基线差（R01 降 bid、R09 降位次加价）
      "better" —— 要证明它比基线好（R03 加 bid、R09 加位次加价）

    为什么必须分方向：同一个对象「偏少一侧 p=0.94、偏多一侧 p=0.32」是常态，
    拿一个数字同时管降和加，等于在两个方向上至少有一个是错的。
    """
    sig = binom_significance(clicks, orders, baseline_cvr)
    p = sig.get("p_worse") if direction == "worse" else sig.get("p_better")

    if p is None or clicks <= 0:
        conf = "low"
    elif p < _EVIDENCE_ALPHA:
        conf = "high"
    elif p < 0.5:
        # 方向对但没到判定线：只够观察，不够动手
        conf = "medium"
    else:
        conf = "low"

    # 加价额外要样本量下限：p 值在小样本下也会变小，不能只看 p
    if direction == "better" and clicks < RAISE_MIN_CLICKS:
        conf = "low"

    sig = dict(sig)
    sig["direction"] = direction
    sig["p_directional"] = p
    sig["confidence"] = conf
    return sig


def _now() -> datetime:
    return datetime.now()


def _expires() -> str:
    return (_now() + timedelta(hours=EXPIRY_HOURS)).isoformat(timespec="seconds")


def _cooldown() -> str:
    return (_now() + timedelta(hours=COOLDOWN_HOURS_BID)).isoformat(timespec="seconds")


def cpc_bid_ratio(kw: dict) -> float | None:
    """这个关键词的 cpc/bid。决定降 bid 是否真的会降 CPC。"""
    bid = kw.get("bid") or 0
    cpc = kw.get("cpc") or 0
    if bid <= 0 or cpc <= 0:
        return None
    return round(cpc / bid, 4)


def ratio_verdict(ratio: float | None) -> dict:
    """把 cpc/bid 翻译成「降 bid 会不会有效」。"""
    if ratio is None:
        return {"verdict": "unknown", "means": "缺数据", "bid_change_effective": None}
    if ratio > 1.15:
        return {
            "verdict": "uplifted",
            "means": f"cpc/bid={ratio:.2f}，有效出价被位次加价或动态竞价放大",
            "bid_change_effective": True,
            "caution": "降 base bid 会等比放大降幅，实际降幅可能超出预期",
        }
    if ratio >= 0.85:
        return {
            "verdict": "binding",
            "means": f"cpc/bid={ratio:.2f}，出价在约束成交价",
            "bid_change_effective": True,
            "caution": "降 bid 会等比降 CPC，但也会开始丢拍卖",
        }
    return {
        "verdict": "slack",
        "means": f"cpc/bid={ratio:.2f}，成交价明显低于你的出价，出价没在约束成交",
        "bid_change_effective": False,
        "caution": "降 bid 不会降 CPC（仍能赢同样的拍卖），只会让出头部位次",
    }


def _kw_context(f: Facts, kw: dict) -> dict:
    return {
        "window": {"start": f.start, "end": f.end, "days": f.window_days},
        "target_acos": f.target_acos,
        # 这里放的是**本组合**口径（引擎作业范围）。键名从 account 改成 scope，
        # 因为叫 account 会让人以为是全店 —— 而 f.account 其实是 account_funnel(scoped=True)。
        "scope": {
            "label": f.scope_label,
            "cvr": f.account["cvr"], "cpc": f.account["cpc"],
            "aov": f.account["aov"], "acos": f.account["acos"],
            "breakeven_cvr": f.breakeven.get("breakeven_cvr"),
        },
        "account_wide": {
            "cvr": (f.account_wide or {}).get("cvr"),
            "breakeven_cvr": (f.breakeven_wide or {}).get("breakeven_cvr"),
        },
        "cvr_baseline": f.cvr_baseline.get("cvr"),
        "min_clicks": f.min_clicks.get("min_clicks"),
        "cpc_bias": f.cpc_bias.get("overall"),
        "bid_floor": f.floor.get("floor"),
        "bid_floor_confidence": f.floor.get("confidence"),
        "target_reachable": f.target_reachable,
        "keyword": {k: kw[k] for k in
                    ("keyword_text", "match_type", "bid", "clicks", "cost",
                     "orders", "sales", "cpc", "cvr", "cpa", "acos") if k in kw},
        "cpc_bid_ratio": cpc_bid_ratio(kw),
    }


# ================================================================
# R30 目标不可达告警（账户级）—— 必须最先跑，它决定其他规则敢不敢动
# ================================================================

def rule_30_target_reachable(f: Facts) -> RuleResult:
    """账户级告警：目标 ACOS 是否可达。

    **口径（2026-11-18 修）**：以前这里拿 `f.account`（= 本组合漏斗）的数，却把
    `entity_label` 写成「全店」。诊断 agent 第 110 号反对指出来了：
    「账户自身的 CVR 1.69% 已经在盈亏平衡 1.47% 之上，把本组合的 0.45% 说成
    『全店目标不可达』，等于把一条组合级问题伪装成账户级结论；它每天出现一次，
    读者会学会不再看它。」

    名不副实比数字错更坏。现在：主结论用 `account_wide`（真·全店），
    并把本组合作为对照单独说 —— 两个口径都不可达，但理由和数字各算各的。
    """
    res = RuleResult()
    # 全店口径（主）
    wide = getattr(f, "account_wide", None) or {}
    be_w = (getattr(f, "breakeven_wide", None) or {}).get("breakeven_cvr")
    cvr_w = wide.get("cvr")
    # 本组合口径（对照）
    be = f.breakeven.get("breakeven_cvr")
    cvr = f.cvr_baseline.get("cvr")

    if be_w is None or cvr_w is None:
        res.suppress("R30", "account", "数据不足，无法判断目标可达性")
        return res

    # 两半各自判定。全店达标 ≠ 本组合达标 —— 诊断 agent 第 138 号反对：
    # 「按这个口径，目标一放宽到 46%，整条告警就消失 —— 但本组合要 85.9% 才打平，
    #  届时 86% 的 ACOS 在跑、却不再有任何规则报警。退出条件只覆盖它声称要看的
    #  两半问题里的全店那一半，本组合那一半会被这条的消失一起带走。」
    # 对。所以这里不再「全店可达就整条 return」，而是两半都达标才 return。
    wide_ok = cvr_w >= be_w
    scope_known = (be is not None and cvr is not None)
    # 数据不足时不假装它不可达 —— 缺数据是 suppress 的事，不是报警的理由
    scope_ok = (cvr >= be) if scope_known else True
    if wide_ok and scope_ok:
        return res

    gap_w = be_w - cvr_w
    need_multiple_w = be_w / cvr_w if cvr_w else None
    # 退出条件：目标 ACOS 放宽到多少，这套 CPC/客单价/CVR 才刚好打平。
    # 诊断 agent 第 135 号反对：「这条告警的结论是稳定算术……第一天它有信息量，
    # 从第二天起它不携带增量，只占阅读时间。真正想问的是：这条告警有没有一个
    # 『什么情况下该消失』的条件？」—— 有，就是这个数。
    exit_w = target_acos_required(wide["cpc"] or 0, wide["aov"] or 0, cvr_w or 0)
    exit_s = target_acos_required(f.account["cpc"] or 0, f.account["aov"] or 0, cvr or 0)
    _hist = (getattr(f, "alert_history", None) or {}).get("R30") or {}
    _days = _hist.get("days_seen") or 0

    # ---- 输入指纹：这条告警什么时候「不再携带新信息」 ----
    #
    # 诊断 agent 第 140 号反对把话说到位了：
    #   「算术我核过了……问题不在算错，在于它自己承认不回答『该做什么』，
    #     而唯一能清掉它的动作是把目标 ACOS 放宽到 45.9%/85.9% —— 那是定价与策略决策，
    #     任何广告动作都改变不了它。所以它必然天天出现、天天无法结案，
    #     从第 3 天起不再携带任何新信息。今天正文 0 条，它成了唯一可见结论，
    #     会让人把『引擎今天没算出可动对象』误读成『引擎只会念 ACOS 不可达』。」
    #
    # 对。一条结论只由 6 个慢变量决定（全店/本组合各三个：CPC、客单价、CVR，再加目标 ACOS）。
    # 这 6 个数不动，它今天就是昨天那张纸。所以要存指纹：**输入变了才重新展开**，
    # 没变就只留一行「输入未变、推理同前、见某日那条」—— 既不消失（消失等于骗人说问题解决了），
    # 也不占六段篇幅。
    #
    # 另外给一个兜底：满 7 天没变也重新展开一次。理由是读者会换、上下文会丢，
    # 一条只写「同前」的记录过一周就成了死链。
    _fp = {
        # **窗口必须进指纹。** 诊断 agent 第 146 号反对（第七轮）：
        # 「窗口是滚动的：11-16、11-17、11-18 三天的窗口末日应分别是 11-13、11-14、11-15，
        #  14 天滚动窗口的七个数不可能三天逐字相同。」
        # 它对 —— 实测 engine_run 的 window_end 确实是 11-13 → 11-14 → 11-15 每天滚一格。
        # 只比七个数字不比窗口，就会出现「窗口换了一天、数字碰巧一样」时误报「输入未变」，
        # 而换了窗口本身就是新信息（多了一天、少了一天）。
        "window_start": f.start, "window_end": f.end,
        "wide_cvr": round(cvr_w or 0, 6), "wide_cpc": round(wide.get("cpc") or 0, 4),
        "wide_aov": round(wide.get("aov") or 0, 2),
        "scope_cvr": round(cvr or 0, 6), "scope_cpc": round(f.account.get("cpc") or 0, 4),
        "scope_aov": round(f.account.get("aov") or 0, 2),
        "target_acos": round(f.target_acos or 0, 4),
        "wide_ok": wide_ok, "scope_ok": scope_ok,
    }
    _prev = _hist.get("inputs") or {}
    _same = bool(_prev) and all(_prev.get(k) == v for k, v in _fp.items())
    _recheck_days = 7
    _stale = False
    _next_recheck = None
    if _same and _hist.get("last_day"):
        try:
            _last = datetime.strptime(_hist["last_day"], "%Y-%m-%d").date()
            _stale = (datetime.now().date() - _last).days >= _recheck_days
            _next_recheck = (_last + timedelta(days=_recheck_days)).isoformat()
        except ValueError:
            _stale = True

    # 本组合对照（可能为 None —— 数据不足时就不说）
    gap = (be - cvr) if (be is not None and cvr is not None) else None
    need_multiple = (be / cvr) if (be and cvr) else None

    # ---- 短版：输入没变时只出这一行 ----
    #
    # 为什么不直接 suppress 掉：消失会让人以为问题解决了，而它没有。
    # 为什么不照旧出六段：「第一天有信息量，从第二天起不携带增量」（#135），
    # 到第三天它就成了「引擎只会念 ACOS 不可达」的证据（#140）。
    # 折中是——它还是出现，但只带两件此刻真的有用的信息：
    #   ① 今天和上次是**同一张纸**（附六个输入值，可自己核）
    #   ② 它什么时候重新展开（输入变了 / 满 7 天例行复核）
    #
    # **而且不写账本。** 这是诊断 agent 第 145 号反对（第六轮打 R30）逼出来的定性：
    #   「R30 的退出条件写明要把目标 ACOS 放宽到 45.9%/85.9%，这是主人的战略决定，
    #     引擎和 agent 都改不了。没人动目标 ACOS，它就每天重算同一结论、
    #     永远等不到自愈，今天不携带任何新信息。」
    # 它对。一条只在「变化时」才有意义的东西，每天往账本写一行，是把**状态**记成了**事件**。
    # 所以输入不变时 status 标成 "state"：仍然进 pack.alerts（工作台、奏折、agent 都看得见），
    # 但 run.py 不往 decision_log 写 —— 账本只记事件变更，不记「今天还是那样」。
    # 副作用是 alert_history 的 days_seen 从此只数「事件日」，这正是想要的。
    _is_state = _same and not _stale
    _short_rationale = (
        f"**目标仍不可达**，且**输入与上次完全相同** —— "
        f"全店 CVR {cvr_w:.2%} / CPC ${wide['cpc']:.2f} / 客单价 ${wide['aov']:.0f}，"
        f"本组合 CVR {(cvr if cvr is not None else 0):.2%} / "
        f"CPC ${f.account['cpc']:.2f} / 客单价 ${f.account['aov']:.0f}，"
        f"目标 ACOS {f.target_acos:.0%}。这七个数和窗口（{f.start}~{f.end}）一个没动，"
        # 指出「上一次是什么时候记的」，读者才知道这条状态挂了多久。
        # 注意别写成「自今天起」——今天没写账本，last_day 是上一次**事件**那天。
        + (f"上一次记进账本是 {_hist.get('last_day')}，之后一直没变。"
           if _hist.get("last_day") else "")
        # 别写「放宽目标就消失」——那个数就是当前 ACOS，等于「接受现状」。
        # 诊断 agent 第 146 号反对（第七轮）验算出来的：cpc/(aov*cvr) ≡ cost/sales。
        + (f"要它不成立，得让 CVR 从 {cvr_w:.2%} 涨到 {be_w:.2%}（全店）"
           f"或从 {(cvr if cvr is not None else 0):.2%} 涨到 {(be if be is not None else 0):.2%}（本组合）。"
           if (not wide_ok and not scope_ok) else "")
        + "**什么时候会重新展开**：上面任一个数变了，或者满 "
        f"{_recheck_days} 天例行复核"
        # 写出具体哪天，不写「上次那天 + 7 天」—— 让人自己去加是没必要的负担，
        # 而且「2026-11-18 + 7 天」这种字面写法第一版真的出现过。
        + (f"（{_next_recheck}）" if _next_recheck else "")
        + "。"
    )

    # 这一条**不算省钱幅度**。
    #
    # 试过三版都错，记在这里免得再试第四遍：
    #   ① 直接拿 bid 当 CPC → 把「省 11%」说成「省 51%」。agent 拿结构冲突里
    #      bid 0.4 的活动实际 CPC 1.571 顶回来。
    #   ② 乘上账户 CPC/出价中位数 1.79 → 算「省 12%」。agent 又顶回来：这个倍数在账户内
    #      从 0.83 到 3.93 都有，不是常数，乘出来的单一数字是编的。
    #   ③ 改用 p10/p90 给区间 → 错得更隐蔽：p10/p90 是**活动之间**的截面差异，每个活动的
    #      倍数由它自己的位次溢价（0%/100%/350%）决定，同一个对象基本固定。拿截面离散度
    #      冒充「单对象不确定性」，得出的「省 -94%~61%」连方向都自相矛盾。
    #
    # 结论：R30 是账户级告警，spend_impact=0，它没有单对象、也就没有可算的止损幅度。
    # 止损幅度归 R01 —— 那里每个关键词有自己的 bid 和自己的实测 CPC，才算得出来。

    res.decisions.append(Decision(
        rule_id="R30",
        rule_version=RULE_VERSION,
        entity_type="account",
        entity_id="all",
        entity_label="全店",
        action_type="alert",
        confidence="high",
        context={
            "window": {"start": f.start, "end": f.end, "days": f.window_days},
            "target_acos": f.target_acos,
            # 全店口径（这条告警的主口径）
            "account_wide": wide,
            # 本组合口径（引擎作业范围，作对照）
            "scope": f.account,
            # 退出条件：目标 ACOS 放宽到这个数，告警就不再成立
            "exit_condition": {
                "account_wide_target_acos_required": exit_w.get("target_acos"),
                "scope_target_acos_required": exit_s.get("target_acos"),
                "current_target_acos": f.target_acos,
            },
            # 按 distinct 日期算的出现历史，不是行数
            "history": _hist,
            # 输入指纹（上次告警时的六个慢变量 + 两个达标位）。
            # 下一次运行拿它和今天比：一样就不再展开六段算术。
            "alert_inputs": _fp,
        },
        calculation=[{
            "step": "目标可达性（全店口径）",
            "formula": "breakeven_cvr = cpc / (aov * target_acos)",
            "params": {"cpc": wide["cpc"], "aov": wide["aov"],
                       "target_acos": f.target_acos},
            "result": {"breakeven_cvr": be_w, "actual_cvr": cvr_w,
                       "gap": round(gap_w, 6),
                       "need_multiple": round(need_multiple_w, 3) if need_multiple_w else None,
                       "target_acos_required": exit_w.get("target_acos")},
            "meaning": (f"全店：目标 ACOS {f.target_acos:.0%} 要求 CVR ≥ {be_w:.2%}，"
                        f"实际 {cvr_w:.2%}，差 {gap_w:.2%}"
                        # 倍数 ≠ 提升幅度。2.62/1.69=1.55 倍，也就是 +55%，不是「提升 155%」。
                        f"（要达到 {need_multiple_w:.2f} 倍，即提升 {need_multiple_w - 1:.0%}）"),
        }, {
            "step": "目标可达性（本组合对照）",
            "formula": "breakeven_cvr = cpc / (aov * target_acos)",
            "params": {"cpc": f.account["cpc"], "aov": f.account["aov"],
                       "target_acos": f.target_acos},
            "result": {"breakeven_cvr": be, "actual_cvr": cvr,
                       "gap": round(gap, 6) if gap is not None else None,
                       "need_multiple": round(need_multiple, 3) if need_multiple else None},
            "meaning": (f"本组合：要求 CVR ≥ {be:.2%}，实际 {cvr:.2%}"
                        if be is not None and cvr is not None else "本组合数据不足"),
        }],
        significance={
            "clicks": wide["clicks"],
            "orders": wide["orders"],
            "note": "全店样本，点击量足够判断 CVR 量级",
        },
        rationale=(
            # 输入没变 → 只出一行。变了或满 7 天 → 完整展开。
            # 这是诊断 agent 第 140 号反对（第五轮打 R30）的直接答案：
            # 「它必然天天出现、天天无法结案，从第 3 天起不再携带任何新信息」。
            # 不 suppress 是因为消失等于骗人说问题解决了；不照旧是因为六段算术
            # 第二天起就是重复阅读。折中：短版只带「今天和上次是同一张纸」+「何时重新展开」。
            _short_rationale if _is_state else (
            # 两半各自陈述：达标的那半不能还写着「不可达」。
            # 诊断 agent 第 138 号反对逼出来的 —— 原来整条只在全店不可达时才生成，
            # 于是本组合那半永远挂着「更差」，而一旦全店达标整条消失、本组合也没人管了。
            ((f"**全店口径**：目标 ACOS {f.target_acos:.0%} 在当前 CPC ${wide['cpc']:.2f} 与客单价 "
              f"${wide['aov']:.0f} 下不可达 —— 需要 CVR ≥ {be_w:.2%}，实际只有 {cvr_w:.2%}"
              # 倍数与提升幅度是两件事：2.62/1.69 = 1.55 倍 = +55%，写成「提升 155%」会把量级放大 3 倍。
              # 诊断 agent 第 118 号反对专门指出过这一点。
              f"（差 {gap_w:.2%}，要达到 {need_multiple_w:.2f} 倍，即提升 {need_multiple_w - 1:.0%}）。")
             if not wide_ok else
             (f"**全店口径已达标**：目标 ACOS {f.target_acos:.0%} 要求 CVR ≥ {be_w:.2%}，"
              f"实际 {cvr_w:.2%}。这半不再报警。"))
            + ((f"本组合更差：CPC ${f.account['cpc']:.2f}、客单价 ${f.account['aov']:.0f}，"
                f"要求 CVR ≥ {be:.2%}，实际 {cvr:.2%}"
                f"（差 {gap:.2%}，要达到 {need_multiple:.2f} 倍，即提升 {need_multiple - 1:.0%}）。")
               if (gap is not None and need_multiple is not None and not scope_ok) else
               (f"本组合已达标：要求 CVR ≥ {be:.2%}，实际 {cvr:.2%}。"
                if (be is not None and cvr is not None and scope_ok) else ""))
            # #128：并排写两个「差 0.93%」会让人以为两个口径一样糟。剔掉本组合之后
            # 剩多少、还差不差，这才是账户侧真正该看的靶心 —— 全店口径的不可达
            # 有大半是这一个组合拖出来的。
            + ((lambda ex: (
                f"对面另一半不是这样：把本组合剔掉，其余花费 CVR {ex['cvr']:.2%}、"
                f"盈亏线 {ex['breakeven_cvr']:.2%}，"
                + (f"仍差 {ex['breakeven_cvr'] - ex['cvr']:.2%}"
                   f"（{ex['breakeven_cvr'] / ex['cvr']:.2f} 倍）。"
                   if ex.get("cvr") else "（数据不足，无法比较）。")
              ))(getattr(f, "account_ex_scope", None) or {})
               if ((getattr(f, "account_ex_scope", None) or {}).get("cvr") and not wide_ok)
               else "")
            # #134：两个「差 0.93%」并列会让人以为是一个问题。它们要动的地方完全不同：
            # 全店那块 CPC $1.06 已经不低，再压也有限；本组合 CVR 只有全店的 1/3，
            # 是产品/Listing 的事。写清楚「这是两个问题」，比给一个大结论有用。
            + ("**这是两个问题，不是一个大问题**：全店那块要动的是 CPC 与定价（那部分 CPC 已经不便宜），"
               "本组合要动的是转化（Listing / 价格 / 评论 / 主图）。"
               "把两件事合成一条，会让人以为「不可达」是一个可以一次解决的东西。"
               if (not wide_ok and not scope_ok) else
               "**这只是一半的问题**：全店那半已经达标，剩下的本组合那半要动的是转化"
               "（Listing / 价格 / 评论 / 主图），不是出价。"
               if wide_ok else
               "**这只是一半的问题**：本组合那半已经达标，剩下的全店那半要动的是 CPC 与定价。")
            + 
            f"**降出价改变不了这件事** —— ACOS 的上限由 CVR 决定，出价只决定每击多少钱，"
            f"把 CPC 压到 0 也只是让亏损变慢，不会让 ACOS 达标。"
            f"要真到 {f.target_acos:.0%}，动的是转化链路（Listing / 价格 / 评论 / 主图）或放宽目标 ACOS。"
            # 这条**不承诺**今天有止损动作可做。
            # 曾经写过「止损由 R01 按每个词自己的出价去算」，agent 连续两轮顶回来：
            #   #107「R01 今天 5 条全被 suppressed，正文 0 条，这条指向的出口是空的」
            #   #109「R01 的产出全是 observing 或冻结（lat pulldown 冻结到 11-30），
            #         这条告警的潜台词是『止损有人管』，可今天一条可执行的止损都没有」
            # 两次都对。R30 是账户级告警，它不知道今天有没有可执行动作 —— 那是 rules.run_all
            # 之后才决定的事（置信度门控、配额、观察期锁）。所以这里只陈述算术，不指路。
            #
            # 第三版还试过「正文为空，意味着今天要么证据不够、要么对象在观察期」——
            # #120 又顶回来：「R01 对 lat pulldown 的 1.0→0.8 是 medium 置信、$44.6 影响，
            # 卡住它的是 11-16 的人工批准，不是证据。把『人按住的』和『数学按住的』混成一句，
            # 读者会以为今天没动作是数据的问题。」
            # 对。三种拦截原因（证据不够 / 配额挤下 / 人工观察期）在 R30 这一层无法区分，
            # 所以它连「为什么空」都不该说，只列可能性、并明确「我不猜」。
            #
            # 第四版写成「这条分不清是哪一种，所以它不猜」—— #122 再顶回来：
            # 「这份 payload 里已经能分清：4 条 observing 中 1 条明写被 11-16 的观察批准锁到
            # 11-30，另 3 条是 confidence=low，overflow 一条都没有。它唯一声称分不清的那一档，
            # 答案就写在它自己的输入里；而真正该被指出的事 —— 花费最高、0 单的那条 ($223)
            # 恰恰是被锁定、14 天内不会被任何规则碰的那一条 —— 它反而没说。」
            # 架构上的答案：**R30 别说，让看得见的说**。r30 跑在 run_all 里，看不见门控结果；
            # 报告层的「今日只观察」表同时看得到三个桶，由它逐条写原因。
            # 所以第四版那句「分不清」也删掉了 —— 不猜就别提这事，别提了还声明不猜。
            f"**这条只陈述算术，不回答「那现在该做什么」，也不解释正文为什么是空的** —— "
            f"它跑在 rules.run_all 里，看不见置信度门控、配额外挤和观察期锁的结果。"
            f"具体每条为什么没进正文，由奏折的「今日只观察」区逐条写；这条不猜，也不替它猜。"
            # 退出条件。诊断 agent 连打四轮（#130 挤占正文 / #134 混淆两种不可达 /
            # #135 无退出条件 / #138 只覆盖全店那半），第四轮之后我以为写对了。
            # 第五轮 #140：「唯一能清掉它的动作是把目标 ACOS 放宽到 45.9%/85.9%——
            # 那是定价与策略决策，任何广告动作都改变不了它。」
            # 第六轮 #145：「没人动目标 ACOS，它就每天重算同一结论、永远等不到自愈。」
            # 第七轮 #146 把最后一层捅破：「这两个数正好是当前实际 ACOS。」
            # 验算：cpc/(aov*cvr) ≡ cost/sales，实测差 7e-6。
            # **所以「放宽到 45.9%」根本不是退出条件，它是「接受现状」的同义句。**
            # 一条只能靠放弃目标才消失的告警，本来就没有退出条件。
            # 现在只陈述「它凭什么还在这儿」和「它凭什么会重新算」，不再许诺一个假的出口。
            + (lambda: (
                "**它凭什么还在这儿**（两半各自判，一半达标不会让另一半跟着消失）："
                + (f"全店这半要 CVR 从 {cvr_w:.2%} 涨到 {be_w:.2%}"
                   if not wide_ok else "全店这半已经达标；")
                + (f"，本组合这半要从 {cvr:.2%} 涨到 {be:.2%}。"
                   if (not scope_ok and be is not None and cvr is not None) else "本组合这半已经达标。")
                # 明写「放宽目标不是出口」，免得读者以为那是个可执行动作。
                + (f"把目标 ACOS 放宽到 {exit_w['target_acos']:.1%}（全店）/"
                   f"{exit_s['target_acos']:.1%}（本组合）确实会让它不再出现 —— "
                   f"但这两个数**正好等于当前实际 ACOS**，所以那不是退出条件，是接受现状。"
                   if (exit_w.get("target_acos") and exit_s.get("target_acos")) else "")
                + "**真正会让它重新算的**：CVR、CPC、客单价任一项变了。"
            ))()
            # 出现天数按 distinct 日期算。一天多行是引擎重跑 + 文案改动造成的，
            # 说「已出现 43 次」是假的（实测三天 43 行）。
            + (f"**这条已经是第 {_days} 天了**（{_hist.get('first_day')} 起，按天算）。"
               if _days >= 2 and not _stale else "")
            # 满 7 天输入仍无变化 → 重新展开，并说明这是例行复核不是新情况。
            # 不写这句，读者会以为数变了（否则为什么今天又展开一次）。
            + (f"**输入已经 {_recheck_days} 天没有变化，这次是例行复核，不是新情况。**"
               if (_same and _stale) else "")
            )
        ),
        spend_impact=0.0,
        # 输入没变 = 状态，不是事件。run.py 只把非 state 的告警写进 decision_log。
        # 这里必须让状态可被下游识别，否则「不写账本」就变成「悄悄消失」。
        status="state" if _is_state else "proposed",
        expires_at=None,
    ))
    return res


# ================================================================
# R01 降 bid —— 无单衰减 + 花费闸门 + CPC 校准 + 地板校验 + cpc/bid 判据
# ================================================================

def rule_01_reduce_bid(f: Facts) -> RuleResult:
    res = RuleResult()

    bias = f.cpc_bias.get("overall") or 1.0
    camp_adj = f.cpc_bias.get("campaign_adj") or {}
    floor = f.floor.get("floor")
    baseline_cvr = f.cvr_baseline.get("cvr") or 0     # 方向判定的真实基线
    aov = f.account["aov"] or 0

    def _adj(cid: str) -> float:
        """这个活动的点击加权位次溢价（%）。拿不到就按 0 算 —— 按 0 算是保守的：
        分母变小 → 目标名义 bid 变大 → 更不容易误判成「该降」。"""
        return float((camp_adj.get(cid) or {}).get("adj_pct") or 0.0)

    def _to_nominal(target_cpc: float, adj_pct: float) -> float:
        """目标 CPC → 目标**名义 bid**。

        两步，都不能省：
          1) ÷ bias  —— CPC 与有效出价的比例（实测 0.96，即 CPC ≈ 有效出价）
          2) ÷ (1+溢价/100) —— 有效出价 = 名义 bid × (1+溢价)

        省掉第 2 步就是原来的 bug：把 280%/350% 溢价活动的目标价按无溢价算，
        名义 bid 会虚高到 3.8–4.2 倍，然后被「该降吗」的比较挡掉或算错幅度。
        """
        eff = target_cpc / bias if bias else target_cpc
        return eff / (1 + adj_pct / 100) if adj_pct else eff

    # 「账户」基线必须是全店口径。用 f.account（=本组合）会让核心词保护的门槛
    # 变成「CPA < 本组合 CPA × 0.7 = $114.67」，等于任何词都被保护。
    # 实测：本组合 CPA $163.81，全店 CPA $57.17，差 2.9 倍。
    _baseline = f.account_baseline
    acct_cpa = _baseline.get("cpa") or f.account.get("cpa")   # 全店拿不到才退回组合
    acct_label = _baseline.get("label") or "本组合（全店数据缺失）"
    rank_owned = f.rank_owned_keywords()
    stop_loss_ids = f.stop_loss_keywords()

    for kw in f.keywords:
        clicks = kw["clicks"]
        cost = kw["cost"]
        orders = kw["orders"]
        bid = kw["bid"]

        if clicks <= 0 or bid <= 0:
            continue

        # ---- 互斥：这个词撞了止损线（R32），降 bid 只是把同样的亏慢一点亏完。
        # 钱是按点击花的，降 20% bid 不改变「这个意图不出单」这件事，
        # 所以同一轮里只留 R32 那一条更狠的建议，别让主人挑便宜的那条。
        if kw["keyword_id"] in stop_loss_ids:
            res.suppress("R01", kw["keyword_text"], "止损线接管",
                         {"keyword_id": kw["keyword_id"],
                          "note": "该词撞了 R32 止损线（花掉 4 单的钱、0 单）；"
                                  "同一个对象只给一条建议，避免主人挑「降 20% bid」"
                                  "这种不解决问题的便宜选项"})
            continue

        # ---- 互斥：这个词已被卡位接管，降 bid 会和卡位抬价对着干
        if kw["keyword_id"] in rank_owned:
            res.suppress("R01", kw["keyword_text"], "卡位接管",
                         {"keyword_id": kw["keyword_id"],
                          "note": "该词在 rank_target 里，出价由 R31 卡位驱动；"
                                  "同时降 bid 会与卡位加价互相打脸"})
            continue

        # ---- 算目标 bid P*
        if orders > 0 and kw["sales"] > 0:
            branch = "有销售额"
            formula = "销售额 × 目标ACOS ÷ 点击"
            raw = kw["sales"] * f.target_acos / clicks
        elif orders > 0:
            branch = "有订单无销售额"
            formula = "客单价 × 目标ACOS × (订单 ÷ 点击)"
            raw = aov * f.target_acos * (orders / clicks)
        else:
            branch = "无订单衰减"
            formula = "客单价 × 目标ACOS ÷ 点击"
            raw = aov * f.target_acos / clicks

        optimum = _to_nominal(raw, _adj(kw["campaign_id"]))

        # ---- 花费闸门：花的钱还没到「一单能覆盖」的量，动了没意义
        #
        # 闸门 = 客单价 × 目标 ACOS，含义是「在目标 ACOS 下，一单允许的广告花费上限」。
        # 它**不是** CPA，也**不是**账户的每单成本 —— 诊断 agent 两次把它读成 CPA：
        #   #Q37-5「闸门 $57.2 正好等于全店 CPA $57.17，该按哪个口径判？」
        #   实际 $57.2 = 本组合 AOV $190.66 × 30%，与全店 CPA $57.17 只是数字撞在一起。
        # 所以 note 里把公式和两个输入都写出来，让它没法被读成别的意思。
        spend_gate = aov * f.target_acos
        if cost < spend_gate and bid <= optimum:
            res.suppress("R01", kw["keyword_text"], "花费闸门",
                         {"cost": cost, "gate": round(spend_gate, 2),
                          "gate_formula": "客单价 × 目标ACOS",
                          "gate_inputs": {"aov": round(aov, 2),
                                          "target_acos": f.target_acos},
                          "note": (f"花了 ${cost:.2f}，还没到「一单允许的广告花费上限」"
                                   f"（客单价 ${aov:.2f} × 目标 ACOS {f.target_acos:.0%} "
                                   f"= ${spend_gate:.2f}），且 bid 已低于目标价，动它没意义。"
                                   f"这不是在比 CPA。")})
            continue

        # ---- 核心词保护（07 §3.1）：CPA 显著优于账户的词不许降，哪怕 ACOS 超标
        # 理由：ACOS 高可能只是客单价低，CPA 才是真金白银。
        # ab machine 就是这种情况：ACOS 44.7% 看着差，但 CPA $22.37 只有账户的 41%。
        kw_cpa = kw.get("cpa")
        if orders >= 2 and kw_cpa and acct_cpa and kw_cpa < acct_cpa * 0.7:
            res.suppress("R01", kw["keyword_text"], "核心词保护",
                         {"kw_cpa": kw_cpa, "account_cpa": acct_cpa,
                          "baseline": acct_label,
                          "scope_cpa": f.account.get("cpa"),
                          "kw_acos": kw["acos"],
                          "note": f"CPA 显著优于{acct_label}平均（门槛 = {acct_label} CPA × 0.7 = "
                                  f"${acct_cpa * 0.7:.2f}），ACOS 高是客单价造成的，不动 bid"})
            continue

        # ---- 只处理「该降」的
        if optimum >= bid:
            continue

        # ---- 地板校验：目标价低于地板时，算「压到地板能做到什么」
        #
        # **地板是有效出价口径，必须换成名义价再比。** 诊断 agent 第 150 号问题（Q D4）：
        # 「地板 $0.40 说的是名义 bid。这个活动名义 bid 已经在地板上，实际 CPC 却是 $1.60
        #  （该活动 adj_pct 320%）。地板该按名义价算，还是按有效出价算？」
        # 按有效出价。旧代码 `optimum < floor` 拿名义目标价去比有效地板 —— 对 320% 溢价
        # 的活动等于把地板虚高了 4.2 倍，于是「名义还能降」被判成「已在地板上」。
        # 实测：名义 $0.40 那三个词的溢价是 142%/280%/321%，有效出价 $0.97/$1.52/$1.68，
        # 全账户没有任何一个词真的用 $0.40 买过点击。
        adj_pct = _adj(kw["campaign_id"])
        floor_nominal = (floor / (1 + adj_pct / 100)) if (floor and adj_pct) else floor
        floor_hit = floor_nominal is not None and optimum < floor_nominal
        floor_cpa = None
        if floor_hit:
            cvr = kw["cvr"] or 0
            # 压到地板时的预期 CPC = 有效地板 × 校准系数（不再乘 (1+溢价)，地板已是有效价）
            est_cpc = floor * bias
            floor_cpa = round(est_cpc / cvr, 2) if cvr else None

        target_bid = _tick(max(optimum, floor_nominal) if floor_hit else optimum)
        step_bid = _tick(max(target_bid, bid * (1 - BID_STEP_MAX)))

        if step_bid >= bid:
            res.suppress("R01", kw["keyword_text"], "空转",
                         {"bid": bid, "computed": step_bid,
                          "note": "按 0.01 步长四舍五入后与当前 bid 相同，改了等于没改"})
            continue

        ratio = cpc_bid_ratio(kw)
        verdict = ratio_verdict(ratio)
        # 降 bid 要证明「它比基线差」，用偏少一侧的 p
        ev = _evidence(clicks, orders, baseline_cvr, "worse")
        conf = ev["confidence"]

        # ---- 动作判定
        if conf == "low":
            action = "observe"
        elif floor_hit:
            # 压到地板仍贵过账户 → 这个对象在目标下没有生存空间
            if acct_cpa and floor_cpa and floor_cpa > acct_cpa * 1.5:
                action = "pause_candidate"
            else:
                action = "floor_breach"
        else:
            action = "reduce_bid"

        if action == "pause_candidate":
            # 停用省下的是全窗口花费，跟 bid 有没有在约束成交无关
            saving = cost
        elif verdict.get("bid_change_effective"):
            saving = cost * (1 - step_bid / bid)
        else:
            saving = 0.0

        steps_needed = None
        if target_bid < bid:
            steps_needed = math.ceil(math.log(target_bid / bid) / math.log(1 - BID_STEP_MAX))

        rationale_parts = [
            f"{branch}：{formula} = ${raw:.4f}",
            (f"÷ CPC 校准系数 {bias:.3f}" if not adj_pct else
             f"÷ CPC 校准系数 {bias:.3f} ÷ 位次溢价 {adj_pct:.0f}%"
             f"（这个活动的点击加权溢价，不是最高档）")
            + f" → 目标 bid ${optimum:.4f}",
        ]
        if floor_hit:
            rationale_parts.append(
                f"**目标价低于地板**（有效地板 ${floor:.2f}"
                + (f" ÷ 本活动溢价 {adj_pct:.0f}% = 名义 ${floor_nominal:.2f}"
                   if adj_pct else "，与名义价同口径")
                + f"）：这个价拿不到量。压到地板时预期 CPC ${floor * bias:.2f}，"
                + (f"预期每单 ${floor_cpa:.2f}（{acct_label} ${acct_cpa:.2f}）"
                   if floor_cpa and acct_cpa else "CVR 为 0，算不出每单成本")
            )
        rationale_parts.append(f"单次最多降 {BID_STEP_MAX:.0%} → ${bid:.2f} → ${step_bid:.2f}")
        if steps_needed and steps_needed > 1:
            rationale_parts.append(f"要走 {steps_needed} 步才到目标价")
        rationale_parts.append(verdict["means"])
        if verdict.get("caution"):
            rationale_parts.append(verdict["caution"])
        if not f.target_reachable:
            rationale_parts.append(
                "**注意**：本组合的目标 ACOS 不可达（CVR 低于盈亏平衡线），"
                "降 bid 只能减少无效花费，不能把 ACOS 拉到达标"
            )

        res.decisions.append(Decision(
            rule_id="R01",
            rule_version=RULE_VERSION,
            entity_type="keyword",
            entity_id=kw["keyword_id"],
            entity_label=kw["keyword_text"],
            market_id=kw["market_id"],
            campaign_id=kw["campaign_id"],
            ad_group_id=kw["ad_group_id"],
            action_type=action,
            current_value=bid,
            proposed_value="pause" if action == "pause_candidate" else step_bid,
            confidence=conf,
            context=_kw_context(f, kw),
            calculation=[
                {"step": "目标 bid",
                 "branch": branch, "formula": formula,
                 "params": {"sales": kw["sales"], "orders": orders, "clicks": clicks,
                            "aov": aov, "target_acos": f.target_acos},
                 "result": round(raw, 4)},
                {"step": "CPC 校准",
                 "formula": "目标bid ÷ cpc_bias ÷ (1 + 位次溢价/100)",
                 "params": {"raw": round(raw, 4), "cpc_bias": bias,
                            "campaign_adj_pct": adj_pct,
                            "cpc_bias_nominal": f.cpc_bias.get("nominal_overall"),
                            "cpc_bias_n": f.cpc_bias.get("n"),
                            "cpc_bias_p10": f.cpc_bias.get("p10"),
                            "cpc_bias_p90": f.cpc_bias.get("p90")},
                 "result": round(optimum, 4)},
                {"step": "花费闸门",
                 "formula": "cost < aov × target_acos",
                 "params": {"cost": cost, "gate": round(spend_gate, 2)},
                 "result": cost >= spend_gate},
                {"step": "地板校验",
                 "formula": ("floor_nominal = floor_effective / (1 + 溢价/100); "
                             "目标bid >= floor_nominal；否则算压到地板的预期每单成本"),
                 "params": {"optimum": round(optimum, 4),
                            "floor_effective": floor,
                            "floor_nominal": (round(floor_nominal, 4)
                                              if floor_nominal is not None else None),
                            "campaign_adj_pct": adj_pct,
                            "floor_basis": f.floor.get("basis"),
                            "floor_confidence": f.floor.get("confidence"),
                            "floor_estimable": f.floor.get("estimable"),
                            "kw_cvr": kw["cvr"]},
                 "result": {"floor_hit": floor_hit, "floor_cpa": floor_cpa},
                 "meaning": (f"压到地板每单 ${floor_cpa:.2f}" if floor_cpa
                             else ("目标价可执行" if not floor_hit else "CVR 为 0，压到地板也不会有单"))},
                {"step": "单步限制",
                 "formula": f"new = max(目标bid, bid × (1-{BID_STEP_MAX}))，夹到 0.01",
                 "params": {"bid": bid, "target": target_bid},
                 "result": step_bid},
                {"step": "cpc/bid 判据",
                 "formula": "cpc ÷ bid",
                 "params": {"cpc": kw["cpc"], "bid": bid},
                 "result": ratio, **verdict},
            ],
            significance={
                "clicks": clicks, "orders": orders,
                "min_clicks_needed": ev.get("need_for_orders"),
                "need_if_zero": ev.get("need_if_zero"),
                "p_worse": ev.get("p_worse"),
                "p_better": ev.get("p_better"),
                "p_directional": ev.get("p_directional"),
                "direction": "worse",
                "verdict": ev.get("verdict"),
                "expected_orders": ev.get("expected_orders"),
                "confidence_basis": (
                    f"{clicks} 击 {orders} 单，按基线应出 "
                    f"{ev.get('expected_orders')} 单；"
                    f"判「更差」需 {ev.get('need_for_orders')} 击"
                ),
                "statistically_conclusive": ev.get("confidence") != "low",
            },
            rationale="；".join(rationale_parts),
            spend_impact=round(saving, 2),
            cooldown_until=_cooldown() if action in ("reduce_bid", "floor_breach") else None,
            expires_at=_expires(),
        ))

    return res


# ================================================================
# R03 加 bid —— 目标价高于现价时才有资格，且必须过 CVR 闸
# ================================================================

def rule_03_scale_up(f: Facts) -> RuleResult:
    """只有「已经在赚钱」的关键词才配加价。

    闸门顺序（顺序错了就会给烂词加价）：
      1. 必须出过单（无单词不存在"加价放量"这回事）
      2. 该词自身 CVR 必须 ≥ 盈亏平衡 CVR
      3. 目标 bid 必须明显高于现价（≥15%，否则是噪声）
      4. 所在活动预算不能已经打满（打满了加价只是抢自己的量）
    """
    res = RuleResult()
    bias = f.cpc_bias.get("overall") or 1.0
    camp_adj = f.cpc_bias.get("campaign_adj") or {}
    be = f.breakeven.get("breakeven_cvr")
    aov = f.account["aov"] or 0
    baseline_cvr = f.cvr_baseline.get("cvr") or 0     # 方向判定的真实基线

    camp = {c["campaign_id"]: c for c in f.campaigns}
    rank_owned = f.rank_owned_keywords()

    for kw in f.keywords:
        clicks = kw["clicks"]
        orders = kw["orders"]
        bid = kw["bid"]
        if clicks <= 0 or bid <= 0 or orders <= 0:
            continue

        # ---- 互斥：卡位在管的词，加价由 R31 负责，R03 让路
        if kw["keyword_id"] in rank_owned:
            res.suppress("R03", kw["keyword_text"], "卡位接管",
                         {"keyword_id": kw["keyword_id"],
                          "note": "该词出价由 R31 卡位驱动，R03 不再叠加加价"})
            continue

        cvr = kw["cvr"] or 0
        # 闸门 2：自身 CVR 必须过盈亏平衡线
        if be and cvr < be:
            res.suppress("R03", kw["keyword_text"], "CVR 低于盈亏平衡",
                         {"kw_cvr": round(cvr, 4), "breakeven_cvr": be,
                          "note": "加价会放大亏损，不是放量"})
            continue

        if kw["sales"] > 0:
            raw = kw["sales"] * f.target_acos / clicks
            formula = "销售额 × 目标ACOS ÷ 点击"
        else:
            raw = aov * f.target_acos * (orders / clicks)
            formula = "客单价 × 目标ACOS × (订单 ÷ 点击)"
        # 加价方向：目标 CPC → 目标名义 bid，同样要除 (1+溢价)。
        # 这里方向是「加」，溢价被漏掉会让目标价虚高 3–4 倍（高溢价活动），
        # 于是「该不该加」的判据被虚高的数带偏 —— 与 R01 是同一个错误的另一面。
        _a = float((camp_adj.get(kw["campaign_id"]) or {}).get("adj_pct") or 0.0)
        optimum = (raw / bias if bias else raw) / (1 + _a / 100) if _a else (raw / bias if bias else raw)

        # 闸门 3：必须明显高于现价
        if optimum < bid * 1.15:
            continue

        step_bid = _tick(min(optimum, bid * (1 + BID_STEP_MAX)))
        if step_bid <= bid:
            res.suppress("R03", kw["keyword_text"], "空转",
                         {"bid": bid, "computed": step_bid})
            continue

        # 加价要证明「它比基线好」，用偏多一侧的 p；且点击太少一律不加
        ev = _evidence(clicks, orders, baseline_cvr, "better")
        conf = ev["confidence"]
        # 加价必须「高」证据才放行。降价是保守方向，medium 可以先动；
        # 加价是花钱方向，medium（p<0.5，等于抛硬币）不足以支撑多花钱。
        if conf != "high":
            # note 必须说清**是哪一道闸**拦的。原来一句话写死「medium 说明方向证据
            # 还不够（p_better 未低于 20%）」，两种情况下都是错的：
            #   · conf 可能是被 RAISE_MIN_CLICKS 拉成 low 的（p 已经够，样本不够）
            #   · p_better 可能确实低于 20%，此时「未低于 20%」是假话
            # 诊断 agent #Q_D4 拿 pulldown machine 抓住了这一点：
            # 20 击 1 单、p_better=0.0956（已低于 20%），却被说成「未低于 20%」。
            p_b = ev.get("p_better")
            why = []
            if clicks < RAISE_MIN_CLICKS:
                why.append(f"点击只有 {clicks}，不到加价下限 {RAISE_MIN_CLICKS}"
                           f"（小样本下 p 会虚低，不能只看 p）")
            if p_b is None:
                why.append("算不出 p_better")
            elif p_b >= _EVIDENCE_ALPHA:
                why.append(f"p_better={p_b:.3f}，没低于 {_EVIDENCE_ALPHA:.0%}"
                           f"（方向证据不够）")
            res.suppress("R03", kw["keyword_text"], "证据不足",
                         {"clicks": clicks, "orders": orders,
                          "p_better": p_b,
                          "p_alpha": _EVIDENCE_ALPHA,
                          "need_for_orders": ev.get("need_for_orders"),
                          "raise_min_clicks": RAISE_MIN_CLICKS,
                          "note": ("加价要求比降价更严：只有「高」证据才加。" +
                                   ("；".join(why) if why else
                                    "本条的 p 和样本都够，但没有通过其它闸门"))})
            continue

        # 闸门 4：预算打满的活动加价无意义
        c = camp.get(kw["campaign_id"]) or {}
        util = c.get("budget_util")
        budget_blocked = util is not None and util >= 0.85

        ratio = cpc_bid_ratio(kw)
        verdict = ratio_verdict(ratio)

        rationale = [
            f"该词 CVR {cvr:.2%} 高于盈亏平衡 {be:.2%}，是真正在赚钱的词",
            f"{formula} = ${raw:.4f} ÷ 校准 {bias:.3f}"
            + (f" ÷ 位次溢价 {_a:.0f}%" if _a else "")
            + f" → 目标 bid ${optimum:.4f}",
            f"现价 ${bid:.2f} 低于目标价，单次 +{BID_STEP_MAX:.0%} → ${step_bid:.2f}",
            verdict["means"],
        ]
        if budget_blocked:
            rationale.append(
                f"**但活动「{c.get('campaign_name')}」预算已用 {util:.0%}** —— "
                f"加价不会带来更多量，只会让同样的钱买更少的点击。"
                f"应先做 R08 加预算，再做这条。"
            )

        res.decisions.append(Decision(
            rule_id="R03",
            rule_version=RULE_VERSION,
            entity_type="keyword",
            entity_id=kw["keyword_id"],
            entity_label=kw["keyword_text"],
            market_id=kw["market_id"],
            campaign_id=kw["campaign_id"],
            ad_group_id=kw["ad_group_id"],
            action_type="raise_bid_blocked" if budget_blocked else "raise_bid",
            current_value=bid,
            proposed_value=step_bid,
            confidence=conf,
            context=_kw_context(f, kw),
            calculation=[
                {"step": "目标 bid", "formula": formula,
                 "params": {"sales": kw["sales"], "orders": orders, "clicks": clicks,
                            "aov": aov, "target_acos": f.target_acos},
                 "result": round(raw, 4)},
                {"step": "CPC 校准", "formula": "目标bid ÷ cpc_bias ÷ (1 + 位次溢价/100)",
                 "params": {"cpc_bias": bias, "campaign_adj_pct": _a},
                 "result": round(optimum, 4)},
                {"step": "CVR 闸门", "formula": "kw_cvr >= breakeven_cvr",
                 "params": {"kw_cvr": round(cvr, 6), "breakeven_cvr": be},
                 "result": True},
                {"step": "加价门槛", "formula": "目标bid >= bid × 1.15",
                 "params": {"optimum": round(optimum, 4), "bid": bid},
                 "result": round(optimum / bid, 3)},
                {"step": "预算闸门", "formula": "campaign.budget_util < 0.85",
                 "params": {"util": util, "campaign": c.get("campaign_name")},
                 "result": not budget_blocked},
                {"step": "单步限制", "formula": f"new = min(目标bid, bid×(1+{BID_STEP_MAX}))",
                 "params": {"bid": bid, "optimum": round(optimum, 4)}, "result": step_bid},
            ],
            significance={
                "clicks": clicks, "orders": orders,
                "min_clicks_needed": ev.get("need_for_orders"),
                "need_if_zero": ev.get("need_if_zero"),
                "p_worse": ev.get("p_worse"),
                "p_better": ev.get("p_better"),
                "p_directional": ev.get("p_directional"),
                "direction": "better",
                "verdict": ev.get("verdict"),
                "expected_orders": ev.get("expected_orders"),
                "raise_min_clicks": RAISE_MIN_CLICKS,
                "confidence_basis": (
                    f"{clicks} 击 {orders} 单，按基线应出 "
                    f"{ev.get('expected_orders')} 单；"
                    f"判「更好」需 {ev.get('need_for_orders')} 击"
                ),
                "statistically_conclusive": ev.get("confidence") != "low",
            },
            rationale="；".join(rationale),
            spend_impact=round(-(step_bid - bid) * clicks / max(1, f.window_days) * f.window_days, 2),
            cooldown_until=_cooldown() if not budget_blocked else None,
            expires_at=_expires(),
        ))
    return res


# ================================================================
# R02 否词候选 —— 只出候选，不自动否
# ================================================================

def rule_02_negative_candidates(f: Facts) -> RuleResult:
    res = RuleResult()
    need = f.min_clicks.get("min_clicks") or 0
    baseline_cvr = f.cvr_baseline.get("cvr") or 0
    if need <= 0:
        res.suppress("R02", "all search terms", "无 CVR 基准，算不出判定阈值")
        return res

    # 已有关键词文本，避免把正在跑的词否掉
    existing = {(k["keyword_text"] or "").strip().lower() for k in f.keywords}

    # 按搜索词聚合（同一搜索词可能跨多个 ad group）
    agg: dict[str, dict] = {}
    for st in f.searchterms:
        term = (st["search_term"] or "").strip().lower()
        if not term:
            continue
        a = agg.setdefault(term, {
            "term": st["search_term"], "clicks": 0, "cost": 0.0,
            "orders": 0, "sales": 0.0, "impressions": 0,
            "campaigns": set(), "ad_groups": set(),
        })
        a["clicks"] += st["clicks"]
        a["cost"] += st["cost"]
        a["orders"] += st["orders"]
        a["sales"] += st["sales"]
        a["impressions"] += st["impressions"]
        a["campaigns"].add(st["campaign_id"])
        a["ad_groups"].add(st["ad_group_id"])

    # 有过单的搜索词 → 不能否（保护）
    converting = {t for t, a in agg.items() if a["orders"] > 0}
    # 有过单的词，其包含的子串也保护（避免否掉长尾变体）
    protected_terms = set()
    for t in converting:
        for t2 in agg:
            if t in t2:
                protected_terms.add(t2)

    for term, a in agg.items():
        if a["orders"] > 0:
            continue
        clicks = a["clicks"]
        if clicks < max(10, need / 2):
            continue   # 连一半阈值都没到，不值得占版面

        # R02 只处理零单词，所以这里 k=0，need_for_orders 就等于 need_if_zero（253）。
        # 但动作必须严格对齐文案：只有真过了判定线（high）才允许「可以否」，
        # 到一半（medium）只能列候选观察 —— 否则会出现「文字说未达线、动作却是否词」。
        ev = _evidence(clicks, 0, baseline_cvr, "worse")
        conf = ev["confidence"]

        if term in existing:
            res.suppress("R02", a["term"], "已是投放中关键词",
                         {"note": "要处理它应该用 R01 降 bid，不是否词"})
            continue
        if term in protected_terms:
            res.suppress("R02", a["term"], "与其他出单词重叠",
                         {"note": "该词是某个出单搜索词的子串/超串，否掉会连带砍掉出单流量"})
            continue

        # 如果这条搜索词来自自动活动，否词应该否在自动活动上
        is_auto_source = False

        res.decisions.append(Decision(
            rule_id="R02",
            rule_version=RULE_VERSION,
            entity_type="search_term",
            entity_id=term,
            entity_label=a["term"],
            action_type="add_negative" if conf == "high" else "observe",
            current_value=None,
            proposed_value="negative_exact",
            confidence=conf,
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "cvr_baseline": f.cvr_baseline.get("cvr"),
                "min_clicks": need,
                "search_term": {"clicks": clicks, "cost": round(a["cost"], 2),
                                "orders": 0, "impressions": a["impressions"]},
                "campaigns": sorted(a["campaigns"]),
                "ad_groups": sorted(a["ad_groups"]),
            },
            calculation=[
                {"step": "聚合",
                 "formula": "按 search_term 跨 ad group 求和",
                 "params": {"rows": len(a["ad_groups"])},
                 "result": {"clicks": clicks, "cost": round(a["cost"], 2),
                            "impressions": a["impressions"]}},
                {"step": "显著性",
                 "formula": "min n s.t. P(X<=0 | n, baseline_cvr) < 1-confidence",
                 "params": {"baseline_cvr": f.cvr_baseline.get("cvr"),
                            "confidence": 0.80, "orders": 0},
                 "result": ev.get("need_for_orders"),
                 "meaning": f"{ev.get('need_for_orders')} 击零单才有 80% 把握说它比基准差"
                            f"（零单情形下与旧公式结果一致，退化验证通过）"},
                {"step": "保护检查",
                 "params": {"is_existing_keyword": term in existing,
                            "overlaps_converting": term in protected_terms},
                 "result": "pass"},
            ],
            significance={
                "clicks": clicks, "orders": 0,
                "min_clicks_needed": ev.get("need_for_orders"),
                "need_if_zero": ev.get("need_if_zero"),
                "p_worse": ev.get("p_worse"),
                "p_better": ev.get("p_better"),
                "p_directional": ev.get("p_directional"),
                "direction": "worse",
                "verdict": ev.get("verdict"),
                "expected_orders": ev.get("expected_orders"),
                "confidence_basis": (
                    f"{clicks} 击零单，按基线应出 "
                    f"{ev.get('expected_orders')} 单；"
                    f"判「更差」需 {ev.get('need_for_orders')} 击"
                ),
                "statistically_conclusive": ev.get("confidence") == "high",
            },
            rationale=(
                f"{clicks} 次点击 / ${a['cost']:.2f} 花费 / 0 单。"
                + (f"已达 {ev.get('need_for_orders')} 击的判定线，可以否。"
                   if conf == "high"
                   else f"**未达判定线（需 {ev.get('need_for_orders')} 击）**，"
                        f"只能列为候选，建议先降 bid 观察而不是直接否。")
            ),
            spend_impact=round(a["cost"], 2) if conf != "low" else 0.0,
            expires_at=_expires(),
        ))

    return res


# ================================================================
# R04 收割 —— 把出单的搜索词提成关键词
# ================================================================

def rule_04_harvest(f: Facts, min_orders: int = 2) -> RuleResult:
    res = RuleResult()
    existing = {(k["keyword_text"] or "").strip().lower() for k in f.keywords}
    bias = f.cpc_bias.get("overall") or 1.0

    agg: dict[str, dict] = {}
    for st in f.searchterms:
        term = (st["search_term"] or "").strip().lower()
        if not term or term in existing:
            continue
        a = agg.setdefault(term, {
            "term": st["search_term"], "clicks": 0, "cost": 0.0,
            "orders": 0, "sales": 0.0,
            "campaigns": set(), "ad_groups": set(),
        })
        a["clicks"] += st["clicks"]
        a["cost"] += st["cost"]
        a["orders"] += st["orders"]
        a["sales"] += st["sales"]
        a["campaigns"].add(st["campaign_id"])
        a["ad_groups"].add(st["ad_group_id"])

    for term, a in agg.items():
        if a["orders"] < min_orders:
            continue
        clicks = a["clicks"]
        avg_cpc = a["cost"] / clicks if clicks else 0
        init_bid = _tick(avg_cpc)          # 初始 bid = 历史平均 CPC
        cpa = a["cost"] / a["orders"]
        acos = a["cost"] / a["sales"] if a["sales"] else None

        res.decisions.append(Decision(
            rule_id="R04",
            rule_version=RULE_VERSION,
            entity_type="search_term",
            entity_id=term,
            entity_label=a["term"],
            action_type="harvest",
            current_value=None,
            proposed_value=init_bid,
            confidence="high" if a["orders"] >= 3 else "medium",
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "target_acos": f.target_acos,
                "account_cpa": f.account["cpa"],
                "search_term": {"clicks": clicks, "cost": round(a["cost"], 2),
                                "orders": a["orders"], "sales": round(a["sales"], 2),
                                "cpa": round(cpa, 2), "acos": round(acos, 4) if acos else None},
                "source_campaigns": sorted(a["campaigns"]),
            },
            calculation=[
                {"step": "初始 bid",
                 "formula": "历史平均 CPC（不是拍脑袋给一个价）",
                 "params": {"cost": round(a["cost"], 2), "clicks": clicks},
                 "result": init_bid},
                {"step": "CPA 对比",
                 "formula": "cpa vs 账户 cpa",
                 "params": {"term_cpa": round(cpa, 2), "account_cpa": f.account["cpa"]},
                 "result": "优于账户" if f.account["cpa"] and cpa < f.account["cpa"] else "劣于账户"},
            ],
            significance={
                "clicks": clicks, "orders": a["orders"],
                "statistically_conclusive": a["orders"] >= 3,
                "confidence_basis": f"{a['orders']} 单（≥3 单才算稳）",
            },
            rationale=(
                f"{clicks} 击 / {a['orders']} 单 / CPA ${cpa:.2f}"
                f"（账户 CPA ${f.account['cpa']:.2f}）。"
                f"建议提为精确匹配关键词，初始 bid ${init_bid:.2f}（= 历史平均 CPC）。"
                f"**必须配套**：在来源活动加精确否定，否则同一个词会双跑抢自己的流量。"
            ),
            spend_impact=round(-a["cost"] * 0.2, 2),   # 预计多花 20%（扩量）
            expires_at=_expires(),
        ))
    return res


# ================================================================
# R08 预算 —— 卡预算且表现好的活动加预算
# ================================================================

def rule_08_budget(f: Facts, util_threshold: float = 0.85) -> RuleResult:
    res = RuleResult()
    # 「表现好」是相对**全店**说的，不是相对这个组合。拿组合当门槛，
    # 在一个转化率只有账户 1/3 的组合里，没有活动算得上「好」——
    # 门槛本身错了，不是标准太严。
    _baseline = f.account_baseline
    acct_roas = _baseline.get("roas") or f.account.get("roas")
    acct_cpa = _baseline.get("cpa") or f.account.get("cpa")
    acct_label = _baseline.get("label") or "本组合（全店数据缺失）"
    baseline_cvr = f.cvr_baseline.get("cvr") or 0     # 方向判定的真实基线

    for c in f.campaigns:
        util = c.get("budget_util")
        if util is None or c["state"] != "ENABLED":
            continue
        if util < util_threshold:
            continue
        if c["orders"] == 0:
            continue

        # 表现好的才加：ROAS 高于账户且 CPA 低于账户
        good = ((acct_roas and c["roas"] and c["roas"] > acct_roas)
                or (acct_cpa and c["cpa"] and c["cpa"] < acct_cpa))
        if not good:
            # 卡预算但表现差 → 不是加预算，是降 bid 或停
            continue

        # 加预算是花钱动作，与 R03 加 bid 同一条规矩：只「证据够」才放行。
        # 旧口径「orders >= 5 就算 high」是拿单数当证据 —— 2 单也可能是运气，
        # 而「这个活动真的比基线好」要用二项检验按它自己的点击量说话。
        ev = _evidence(c["clicks"], c["orders"], baseline_cvr, "better")
        if ev["confidence"] != "high":
            res.suppress("R08", c["campaign_name"], "证据不足", {
                "clicks": c["clicks"], "orders": c["orders"],
                "p_better": ev["p_better"],
                "need_for_orders": ev["need_for_orders"],
                "note": (f"{c['clicks']} 击 {c['orders']} 单，"
                         f"判「确实比基线好」需 {ev['need_for_orders']} 击，"
                         f"加预算是花钱动作，不到「证据够」不放行"),
            })
            continue

        old = c["daily_budget"]
        # 步长来自 facts：被 param_override(max_budget_step) 覆盖过就是覆盖值。
        # 「加，但幅度再小一点」这条钩子若不落到这里，就只是句客气话。
        step = float(getattr(f, "budget_step", 1.2) or 1.2)
        new = round(old * step, 2)
        headroom = c["roas"] and acct_roas and (c["roas"] / acct_roas)

        res.decisions.append(Decision(
            rule_id="R08",
            rule_version=RULE_VERSION,
            entity_type="campaign",
            entity_id=c["campaign_id"],
            entity_label=c["campaign_name"],
            market_id=c["market_id"],
            campaign_id=c["campaign_id"],
            action_type="raise_budget",
            current_value=old,
            proposed_value=new,
            confidence=ev["confidence"],
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "account_roas": acct_roas, "account_cpa": acct_cpa,
                "campaign": {k: c[k] for k in
                             ("campaign_name", "portfolio_name", "is_auto", "daily_budget",
                              "avg_daily_cost", "budget_util", "cost", "clicks", "orders",
                              "sales", "cpa", "roas")},
            },
            calculation=[
                {"step": "预算使用率",
                 "formula": "avg_daily_cost ÷ daily_budget",
                 "params": {"avg_daily_cost": c["avg_daily_cost"], "daily_budget": old},
                 "result": util},
                {"step": "表现对比",
                 "formula": "campaign roas/cpa vs account",
                 "params": {"roas": c["roas"], "account_roas": acct_roas,
                            "cpa": c["cpa"], "account_cpa": acct_cpa},
                 "result": {"roas_multiple": round(headroom, 2) if headroom else None}},
                {"step": "新预算",
                 "formula": f"daily_budget × {step}",
                 "params": {"old": old, "step": step}, "result": new},
            ],
            significance={
                "clicks": c["clicks"], "orders": c["orders"],
                "min_clicks_needed": ev["need_for_orders"],
                "need_if_zero": ev["need_if_zero"],
                "p_worse": ev["p_worse"], "p_better": ev["p_better"],
                "p_directional": ev["p_directional"],
                "direction": "better",
                "verdict": ev["verdict"],
                "expected_orders": ev["expected_orders"],
                "confidence_basis": (
                    f"{c['clicks']} 击 {c['orders']} 单，"
                    f"按基线应出 {ev['expected_orders']} 单；"
                    f"判「确实更好」需 {ev['need_for_orders']} 击"
                ),
                "statistically_conclusive": ev["confidence"] != "low",
            },
            rationale=(
                f"预算 ${old:.0f}/天已用 {util:.0%}，"
                f"ROAS {c['roas']} vs {acct_label} {acct_roas}，CPA ${c['cpa']} vs {acct_label} ${acct_cpa}。"
                f"**这个活动是被预算卡住，不是被市场卡住** —— 加预算是当前性价比最高的动作。"
            ),
            spend_impact=round(-(new - old) * f.window_days, 2),   # 负 = 多花
            expires_at=_expires(),
        ))
    return res


# ================================================================
# R09 位次 —— 按位次实际 ROAS 决定加/减，不无脑加 TOS
# ================================================================

def rule_09_placement(f: Facts) -> RuleResult:
    """位次加价调整 —— 必须做到「活动 × 位次」粒度。

    为什么不能按 placement 汇总给建议：位次加价是**每个活动单独设的**，
    后台没有「全店 TOS 加价」这个开关。按 placement 汇总得出的
    「降低商品页加价」根本没法执行 —— 你得说清改哪个活动。

    实测本账户（2026-10-31~11-13）：TOS 加价从 0% 到 900% 都有，
    900% 出现在两个活动上，一个 ACOS 91%、一个 ACOS 11%，
    同一个数字结果差 8 倍 —— 说明「加价高低」本身不携带信息，
    要比的是「这个位次在这个活动里的 ROAS vs 这个活动的整体 ROAS」。

    两个硬约束：
      1) 加价为 0% 时无法再降 —— 只能去调基础出价（R01），这里必须说清而不是给假建议。
      2) 位次加价上限 900%（Amazon 物理约束），加价到顶就不能再加。
    """
    res = RuleResult()
    if not f.campaign_placements:
        return res

    baseline_cvr = f.cvr_baseline.get("cvr") or 0      # 方向判定的真实基线
    MOD_MAX = 900.0
    STEP_PP = 20.0   # 一次动 20 个百分点，不动幅度过大

    for cp in f.campaign_placements:
        cid = cp["campaign_id"]
        label = f"{cp['campaign_name']} · {cp['placement']}"
        adj = cp["bid_adjustment"]
        adj_name = cp["bid_adjustment_name"] or "未设置"
        camp_roas = cp["campaign_roas"]
        share = cp["cost_share_of_campaign"] or 0

        # 活动整体都没卖出东西 → 这是活动级问题，不是位次问题
        if not camp_roas or camp_roas <= 0:
            res.suppress("R09", label, "活动整体无销售额",
                         {"campaign_roas": camp_roas, "cost": cp["cost"]})
            continue
        # 这个位次在这个活动里占比太小，动了也没用
        if share < 0.15:
            res.suppress("R09", label, "占活动花费比例过小",
                         {"share": share, "cost": cp["cost"]})
            continue
        if cp["orders"] == 0 and cp["cost"] < 20:
            res.suppress("R09", label, "花费还不够判断", {"cost": cp["cost"]})
            continue

        ratio = (cp["roas"] / camp_roas) if cp["roas"] is not None else None
        # 方向不同，证据也不同：降看「偏少一侧」，加看「偏多一侧」。
        # 不能在这里统一算一个 conf —— 同一个对象两个方向的 p 值常常差很远。
        ev_down = _evidence(cp["clicks"], cp["orders"], baseline_cvr, "worse")
        ev_up = _evidence(cp["clicks"], cp["orders"], baseline_cvr, "better")

        if cp["orders"] == 0 or (ratio is not None and ratio < 0.7):
            # 想降
            ev = ev_down
            if adj is not None and adj <= 0:
                res.suppress("R09", label, "位次加价已为 0，无法再降",
                             {"bid_adjustment": adj,
                              "note": "只能去调基础出价（R01），或直接停用这个活动"})
                continue
            new_adj = max(0.0, (adj or 0.0) - STEP_PP)
            action = "adjust_placement"
            ord_str = "零单" if cp["orders"] == 0 else f"{cp['orders']} 单"
            impact = round(cp["cost"] * (STEP_PP / max(1.0, (adj or STEP_PP))) * 0.5, 2)
            rationale = (
                f"{cp['campaign_name']} 的 {cp['placement']}：花 ${cp['cost']:.0f}"
                f"（占这个活动 {share:.0%}），{cp['clicks']} 次点击 "
                f"{ord_str}，"
                f"ROAS {cp['roas']} vs 活动整体 {camp_roas}。"
                f"当前位次加价 {adj_name}，建议降到 {new_adj:.0f}%。"
                f"**注意：降的是这个活动在这个位次的加价，不是全店。**"
            )
            proposed = f"{new_adj:.0f}%（从 {adj_name} 降）"
            current = adj_name
        elif ratio is not None and ratio > 1.3 and cp["orders"] >= 1:
            # 想加
            ev = ev_up
            # 活动整体都不如全店平均，就别往里加钱了 —— 加价只会放大亏损
            # 同上：位次的好坏是相对全店，不是相对本组合。
            acct_roas = (f.account_baseline or {}).get("roas") or f.account.get("roas")
            if acct_roas and camp_roas < acct_roas * 0.9:
                res.suppress("R09", label, "活动整体不如全店平均，不该加价",
                             {"campaign_roas": camp_roas, "account_roas": acct_roas,
                              "note": "先解决活动本身（R01 降基础出价或停用），而不是在这个活动里加位次"})
                continue
            if adj is not None and adj >= MOD_MAX:
                res.suppress("R09", label, "位次加价已达上限 900%", {"bid_adjustment": adj})
                continue
            new_adj = min(MOD_MAX, (adj or 0.0) + STEP_PP)
            action = "adjust_placement"
            impact = round(-cp["cost"] * 0.2, 2)
            rationale = (
                f"{cp['campaign_name']} 的 {cp['placement']}：花 ${cp['cost']:.0f}"
                f"（占这个活动 {share:.0%}），{cp['clicks']} 次点击 {cp['orders']} 单，"
                f"ROAS {cp['roas']} 高于活动整体 {camp_roas}，每单 ${cp['cpa']}。"
                f"当前位次加价 {adj_name}，建议提到 {new_adj:.0f}% 试探。"
                f"**加价会推高整体 ACOS，一次只动 20 个点，观察 7 天。**"
            )
            proposed = f"{new_adj:.0f}%（从 {adj_name} 提）"
            current = adj_name
        else:
            continue

        # 位次加价和关键词加价是同一类花钱动作，门槛要一致：
        # 降位次（省钱方向）low 才降级为观察；加位次（花钱方向）不到 high 就不放行。
        is_up = ev is ev_up
        if ev["confidence"] == "low" or (is_up and ev["confidence"] != "high"):
            action = "observe"
            rationale = (
                f"{rationale}（{cp['clicks']} 击 {cp['orders']} 单，"
                f"判「{'更好' if is_up else '更差'}」需 "
                f"{ev.get('need_for_orders')} 击，"
                + ("证据不足，先观察不动）" if ev["confidence"] == "low"
                   else "只到「证据一般」，加位次是花钱动作，不到「证据够」不放行，先观察）")
            )

        res.decisions.append(Decision(
            rule_id="R09",
            rule_version=RULE_VERSION,
            entity_type="campaign_placement",
            entity_id=f"{cid}::{cp['placement']}",
            entity_label=label,
            action_type=action,
            market_id=None,
            campaign_id=cid,
            current_value=current,
            proposed_value=proposed,
            confidence=ev["confidence"],
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "campaign_placement": cp,
            },
            calculation=[
                {"step": "参照系选择",
                 "formula": "同一个活动内部的位次对比（不是全店对比）",
                 "params": {"campaign_roas": camp_roas, "placement_roas": cp["roas"]},
                 "result": round(ratio, 3) if ratio is not None else None,
                 "meaning": "位次加价只能按活动设，所以参照系必须是这个活动自己"},
                {"step": "占活动花费比例",
                 "formula": "placement.cost ÷ campaign.cost",
                 "params": {"placement_cost": cp["cost"], "campaign_cost": cp["campaign_cost"]},
                 "result": round(share, 4),
                 "meaning": "低于 15% 时改了也影响不大，直接跳过"},
                {"step": "加价调整",
                 "formula": "new_adj = clamp(old_adj ± 20pp, 0, 900)",
                 "params": {"old_adj": adj, "step_pp": STEP_PP},
                 "result": {"current": adj_name, "proposed": proposed}},
            ],
            significance={
                "clicks": cp["clicks"], "orders": cp["orders"],
                "min_clicks_needed": ev.get("need_for_orders"),
                "need_if_zero": ev.get("need_if_zero"),
                "p_worse": ev.get("p_worse"),
                "p_better": ev.get("p_better"),
                "p_directional": ev.get("p_directional"),
                "direction": ev.get("direction"),
                "verdict": ev.get("verdict"),
                "expected_orders": ev.get("expected_orders"),
                "confidence_basis": (
                    f"{cp['clicks']} 击 {cp['orders']} 单，按基线应出 "
                    f"{ev.get('expected_orders')} 单；"
                    f"判「{'更差' if ev is ev_down else '更好'}」需 "
                    f"{ev.get('need_for_orders')} 击"
                ),
                "statistically_conclusive": ev.get("confidence") != "low",
            },
            rationale=rationale,
            spend_impact=impact,
            expires_at=_expires(),
        ))
    return res


# ================================================================
# R21 拓词入组 —— 从搜索词里找还没投的、有点击没出单的词
# ================================================================

def rule_21_expand(f: Facts, min_clicks: int = 5) -> RuleResult:
    """把「有点击但还没投」的搜索词列为待入组候选。

    注意：这类词没出单，所以不是「收割」（R04 要 ≥2 单），
    而是「值得单独给它一个位置去试」——但必须明确写清它还没被验证。
    """
    res = RuleResult()
    existing = {(k["keyword_text"] or "").strip().lower() for k in f.keywords}

    agg: dict[str, dict] = {}
    for st in f.searchterms:
        term = (st["search_term"] or "").strip().lower()
        if not term or term in existing:
            continue
        a = agg.setdefault(term, {"term": st["search_term"], "clicks": 0,
                                  "cost": 0.0, "orders": 0, "sales": 0.0})
        a["clicks"] += st["clicks"]
        a["cost"] += st["cost"]
        a["orders"] += st["orders"]
        a["sales"] += st["sales"]

    # 只取「有点击、零单、但花费已经值得单独看」的
    cands = [a for a in agg.values()
             if a["orders"] == 0 and a["clicks"] >= min_clicks and a["cost"] >= 5]
    cands.sort(key=lambda x: -x["cost"])

    for a in cands[:15]:   # 一次最多 15 条，避免淹没
        res.decisions.append(Decision(
            rule_id="R21",
            rule_version=RULE_VERSION,
            entity_type="search_term",
            entity_id=a["term"].lower(),
            entity_label=a["term"],
            action_type="observe",
            current_value=None,
            proposed_value="consider_new_ad_group",
            confidence="low",
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "search_term": {"clicks": a["clicks"], "cost": round(a["cost"], 2),
                                "orders": 0},
            },
            calculation=[
                {"step": "筛选",
                 "formula": "orders==0 and clicks>=5 and cost>=5 and not existing keyword",
                 "params": {"clicks": a["clicks"], "cost": round(a["cost"], 2)},
                 "result": True},
            ],
            significance={
                "clicks": a["clicks"], "orders": 0,
                "statistically_conclusive": False,
                "confidence_basis": "零单，样本不足以判断好坏",
            },
            rationale=(
                f"{a['clicks']} 击 / ${a['cost']:.2f} / 0 单，还没作为关键词投放。"
                f"**未验证** —— 建议先观察，或在低预算活动里试投，不要直接给主力预算。"
            ),
            spend_impact=0.0,
            expires_at=_expires(),
        ))
    return res


# ================================================================
# R31 关键词卡位 —— 由「排名目标」反推 bid（优麦云那类，海外平台没有）
#
# 与 R01/R03 的根本区别：它们由表现反推 bid，R31 由排名反推 bid。
# 所以它必须带两个安全阀，否则就是个烧钱机器：
#   阀一 ACOS 闸：窗口 ACOS 超 target×acos_guard_mult，或零单花费已超闸 → 停卡转人工
#   阀二 互斥：同一个词被卡位接管时，R01/R03 必须让路（在它们内部判定）
# 另外三条优麦云有的：自我竞争检测、加价次数上限、连续到位后降价。
# ================================================================

def rule_31_rank_position_push(f: Facts) -> RuleResult:
    res = RuleResult()

    if not f.rank_targets:
        return res

    kws = {k["keyword_id"]: k for k in f.keywords}
    camp_pl = {}
    for p in f.campaign_placements:
        camp_pl.setdefault(p["campaign_id"], {})[p["placement"]] = p

    aov = f.account["aov"] or 0
    target = f.target_acos

    for t in f.rank_targets:
        kid = t["keyword_id"]
        label = t.get("keyword_text") or kid
        st = f.rank_state.get(kid) or {}
        kw = kws.get(kid)

        # ---- 阀零：没有这个词的数据，就没有加价的依据
        # 这里必须区分两种「没数据」，否则读的人会得出完全相反的结论：
        #   ① 这个词压根没在本窗口投过 → 真的无从计算；
        #   ② 这个词在投，但不在本次作业范围（如卡位目标种自别的投放组合）→
        #      数据在库里、只是引擎这次不看它，换个 scope 就能算。
        # 2026-11-23 实测：12 个卡位目标里 10 个是 ②，全被写成 ①，
        # 看起来像「卡位规则跑不起来」，实际是「这些词不归这个组合管」。
        if not kw:
            in_scope = f.keyword_seen_elsewhere(kid) if hasattr(f, "keyword_seen_elsewhere") else None
            if in_scope:
                unassigned = in_scope == "活动未归投放组合"
                where = ("该词在投，但它所在的活动没归进任何投放组合"
                         if unassigned else f"该词在投（组合 {in_scope}）")
                fix = ("先在积加里把这个活动归到某个投放组合，或把作业范围改成按活动选"
                       if unassigned else f"把作业范围扩到 {in_scope} 后这条才会出建议")
                res.suppress("R31", label, "不在本次作业范围内",
                             {"keyword_id": kid, "portfolio": in_scope,
                              "note": f"{where}，而本次作业范围是 {f.scope_label}；{fix}"})
            else:
                res.suppress("R31", label, "关键词不在本窗口数据里",
                             {"keyword_id": kid, "note": "没有 bid 与表现，卡位无从计算"})
            continue

        bid = kw["bid"]
        clicks = kw["clicks"]
        cost = kw["cost"]
        orders = kw["orders"]
        acos = kw["acos"]

        # ---- 阀零二：排名观测缺失/过期，宁可不动
        if not st.get("observations"):
            res.suppress("R31", label, "无排名观测",
                         {"keyword_id": kid,
                          "note": "引擎不抓排名，只消费 rank_snapshot；缺数据就不出建议"})
            continue
        if st.get("stale"):
            res.suppress("R31", label, "排名观测过期",
                         {"age_hours": st.get("age_hours"),
                          "max_age_hours": RANK_SNAPSHOT_MAX_AGE_HOURS,
                          "note": "过期观测不能当当前位次用"})
            continue

        # ---- 自我竞争：目标位被自己另一个 ASIN 占着，再加价是自己打自己
        if st.get("self_competing"):
            res.suppress("R31", label, "自我竞争",
                         {"blocker": st.get("blocker"),
                          "own": st.get("own"),
                          "note": "目标位置出现自家另一个 ASIN，再加价只是自己打自己，停止加价（优麦云同款保护）"})
            continue

        latest = st.get("latest") or {}
        goal = f"第{t['goal_page']}页第{t['goal_position']}位"

        # 本次抓取范围内没看到自己（插件只抓前 N 位）：位次未知，但方向明确——比抓取深度还深。
        # 不能当成「已到位」，也不能当成「无数据」，按未到位处理并在计算链里写明。
        unseen = latest.get("page") is None and latest.get("position") is None

        # ---- 阀一：ACOS 闸（优麦云没有这条，必须补）
        guard = target * t["acos_guard_mult"]
        # 零值语义保护：orders==0 时 acos 也返回 0，不能当成"表现好"
        if orders == 0 and cost > aov * guard and cost > 0:
            res.suppress("R31", label, "ACOS 闸（零单超支）",
                         {"cost": cost, "gate": round(aov * guard, 2), "orders": 0,
                          "note": "零单花费已超闸值，ACOS=0 是零值陷阱，不是表现好"})
            continue
        if orders >= 1 and acos is not None and acos > guard:
            res.suppress("R31", label, "ACOS 闸",
                         {"acos": acos, "target_acos": target,
                          "guard": round(guard, 4),
                          "note": "ACOS 已超目标×闸倍数，停卡转人工，不再加价"})
            continue

        # ---- 阀三：加价次数上限
        used = int(st.get("pushes_used") or 0)
        if used >= t["max_pushes"]:
            res.suppress("R31", label, "加价次数上限",
                         {"pushes_used": used, "max_pushes": t["max_pushes"],
                          "note": "已加价这么多次还没到位，说明不是钱的问题（词/价/图/竞争）"})
            continue

        push_mode = t["push_mode"]
        step_mode = t["step_mode"]
        step_value = t["step_value"]

        # ---- 已到位：连续成功 N 次后按配置降价，否则只看不动
        if st.get("hit"):
            streak = int(st.get("streak") or 0)
            need = t["success_streak_required"]
            if t.get("decrease_mode") and streak >= need:
                if push_mode == "bid":
                    cur = bid
                    dec = (t["decrease_value"] if step_mode == "percent"
                           else t["decrease_value"])
                    if t["decrease_mode"] == "percent":
                        new = _tick(cur * (1 - (t["decrease_value"] or 0) / 100))
                    else:
                        new = _tick(cur - (t["decrease_value"] or 0))
                    if new >= cur:
                        res.suppress("R31", label, "空转",
                                     {"bid": cur, "computed": new,
                                      "note": "降价幅度小于 0.01，改了等于没改"})
                        continue
                    res.decisions.append(Decision(
                        rule_id="R31",
                        rule_version=RULE_VERSION,
                        entity_type="keyword",
                        entity_id=kid,
                        entity_label=label,
                        market_id=kw["market_id"],
                        campaign_id=kw["campaign_id"],
                        ad_group_id=kw["ad_group_id"],
                        action_type="rank_step_down",
                        current_value=cur,
                        proposed_value=new,
                        confidence="high" if streak >= need * 2 else "medium",
                        context=_rank_context(f, kw, t, st, goal),
                        calculation=[
                            {"step": "到位判定",
                             "formula": f"page <= {t['goal_page']} 且 position <= {t['goal_position']}",
                             "params": {"page": latest.get("page"),
                                        "position": latest.get("position")},
                             "result": True},
                            {"step": "连续到位",
                             "formula": f"streak >= {need}",
                             "params": {"streak": streak, "required": need},
                             "result": True},
                            {"step": "降价",
                             "formula": ("bid × (1 - 幅度%)" if t["decrease_mode"] == "percent"
                                         else "bid - 固定值"),
                             "params": {"mode": t["decrease_mode"],
                                        "value": t["decrease_value"], "bid": cur},
                             "result": new},
                        ],
                        significance={
                            "rank_observations": st.get("observations"),
                            "streak": streak,
                            "statistically_conclusive": streak >= need,
                            "confidence_basis": f"连续 {streak} 次观测都在目标位",
                        },
                        rationale=(
                            f"已连续 {streak} 次观测到在{goal}（要求 {need} 次）。"
                            f"到位后不降价等于把竞价焊死在高位，按配置降 "
                            f"${cur:.2f} → ${new:.2f}，用更低的价维持同一个位置。"
                        ),
                        spend_impact=round(cost * (1 - new / cur), 2) if cur > 0 else 0.0,
                        cooldown_until=_cooldown(),
                        expires_at=_expires(),
                    ))
                else:
                    res.suppress("R31", label, "TOS 模式降价未实现",
                                 {"push_mode": "tos",
                                  "note": "位次加价降价要按活动改，本期只做竞价模式"})
            else:
                res.suppress("R31", label, "已到位",
                             {"latest": latest, "goal": goal, "streak": streak,
                              "required": need,
                              "note": "已在目标位，未达连续成功次数，不动"})
            continue

        # ---- 未到位：算加价
        calc = []
        if push_mode == "bid":
            cur = bid
            if cur <= 0:
                res.suppress("R31", label, "无当前竞价", {"bid": cur})
                continue
            if t["max_bid"] is not None and cur >= t["max_bid"]:
                res.suppress("R31", label, "触顶",
                             {"bid": cur, "max_bid": t["max_bid"],
                              "note": "已到设定的最高竞价，再加就超预算意图"})
                continue
            step = (cur * step_value / 100) if step_mode == "percent" else step_value
            raw_new = cur + step
            if t["max_bid"] is not None:
                raw_new = min(raw_new, t["max_bid"])
            new = _tick(raw_new)
            if new <= cur:
                res.suppress("R31", label, "空转",
                             {"bid": cur, "computed": new,
                              "note": "加价幅度被最高竞价或 0.01 步长吃掉"})
                continue
            unit, cur_label = "bid", "竞价"
            calc = [
                {"step": "当前位次",
                 "formula": ("最新批次内未出现该 ASIN → 位次深于抓取深度"
                             if unseen else
                             "rank_snapshot 最新批次里该 ASIN 的位次"),
                 "params": {"observed_at": latest.get("observed_at"),
                            "page": latest.get("page"), "position": latest.get("position"),
                            "asin": latest.get("asin"), "postal_code": latest.get("postal_code"),
                            "batches": st.get("batches")},
                 "result": {"in_goal": False, "goal": goal, "unseen": unseen}},
                {"step": "加价",
                 "formula": ("bid × (1 + 幅度%)" if step_mode == "percent" else "bid + 固定值"),
                 "params": {"mode": step_mode, "value": step_value, "bid": cur,
                            "max_bid": t["max_bid"]},
                 "result": new},
            ]
        elif push_mode == "tos":
            # 走 rank_tos（卡位专用读取），不走 campaign_placements：
            # 后者带花费/点击门槛，刚开不久的活动读不到配置，会被误判成「无数据」。
            cur = f.rank_tos.get(str(kw["campaign_id"]))
            if cur is None:
                res.suppress("R31", label, "活动无 TOS 加价数据",
                             {"campaign_id": kw["campaign_id"],
                              "note": "窗口内该活动没有 TOS 位次行，位次加价是活动级配置，缺数据不能瞎调"})
                continue
            cap = t["max_tos_pct"] or 900.0
            if cur >= cap:
                res.suppress("R31", label, "触顶",
                             {"tos_pct": cur, "cap": cap,
                              "note": "已到 TOS 加价上限（亚马逊规则上限 900%）"})
                continue
            step = step_value  # 位次加价只按百分点
            new = round(min(cur + step, cap), 2)
            if new <= cur:
                res.suppress("R31", label, "空转", {"tos_pct": cur, "computed": new})
                continue
            unit, cur_label = "tos_pct", "TOS 加价"
            calc = [
                {"step": "当前位次",
                 "formula": ("最新批次内未出现该 ASIN → 位次深于抓取深度"
                             if unseen else
                             "rank_snapshot 最新批次里该 ASIN 的位次"),
                 "params": {"observed_at": latest.get("observed_at"),
                            "page": latest.get("page"), "position": latest.get("position"),
                            "batches": st.get("batches")},
                 "result": {"in_goal": False, "goal": goal, "unseen": unseen}},
                {"step": "加价",
                 "formula": "TOS 加价 + 百分点，夹到上限",
                 "params": {"cur": cur, "step": step, "cap": cap},
                 "result": new},
            ]
        else:
            res.suppress("R31", label, "未知卡位方式", {"push_mode": push_mode})
            continue

        # ---- 样本判据：卡位的样本是"排名观测次数"，不是点击
        obs_n = int(st.get("observations") or 0)
        conf = "high" if obs_n >= 3 else ("medium" if obs_n >= 2 else "low")

        extra = None
        if unit == "bid" and cur > 0:
            # 加价导致的额外花费 ≈ 窗口花费 × 涨幅（粗估，报告里标估算）
            extra = round(cost * (new / cur - 1), 2)

        res.decisions.append(Decision(
            rule_id="R31",
            rule_version=RULE_VERSION,
            entity_type="keyword",
            entity_id=kid,
            entity_label=label,
            market_id=kw["market_id"],
            campaign_id=kw["campaign_id"],
            ad_group_id=kw["ad_group_id"],
            action_type="rank_push",
            current_value=cur,
            proposed_value=new,
            confidence=conf,
            context=_rank_context(f, kw, t, st, goal),
            calculation=calc + [
                {"step": "ACOS 闸",
                 "formula": f"acos <= target × {t['acos_guard_mult']}",
                 "params": {"acos": acos, "target_acos": target,
                            "guard": round(guard, 4), "orders": orders, "cost": cost},
                 "result": True},
                {"step": "次数上限",
                 "formula": f"pushes_used < {t['max_pushes']}",
                 "params": {"pushes_used": used, "max_pushes": t["max_pushes"]},
                 "result": True},
            ],
            significance={
                "rank_observations": obs_n,
                "latest_observed_at": latest.get("observed_at"),
                "pushes_used": used,
                "max_pushes": t["max_pushes"],
                "statistically_conclusive": obs_n >= 2,
                "confidence_basis": f"{obs_n} 次排名观测；已加价 {used}/{t['max_pushes']} 次",
            },
            rationale=(
                f"目标{goal}，{_where(latest)}（{latest.get('observed_at')} 抓取）。"
                f"这是**由排名反推{cur_label}**，不是由表现反推："
                f"{cur_label} ${cur:.2f} → ${new:.2f}。" if unit == "bid" else
                f"目标{goal}，{_where(latest)}。"
                f"TOS 加价 {cur:.0f}% → {new:.0f}%（上限 {cap:.0f}%）。"
            ) + (
                f" 已加价 {used}/{t['max_pushes']} 次，"
                f"ACOS 闸设在目标×{t['acos_guard_mult']:.1f}={guard:.0%}。"
                f" **本词已被卡位接管，R01/R03 本轮不再对它出价。**"
            ),
            spend_impact=(-extra if extra is not None else 0.0),
            cooldown_until=_cooldown(),
            expires_at=_expires(),
        ))
    return res


def _where(latest: dict) -> str:
    """位次的人话描述，兼容「抓取深度内没看到自己」。"""
    page, pos = latest.get("page"), latest.get("position")
    if page is None and pos is None:
        return "本次抓取深度内没有出现该 ASIN（位次更深）"
    return f"当前第 {page} 页第 {pos} 位"


def _rank_context(f: Facts, kw: dict, t: dict, st: dict, goal: str) -> dict:
    ctx = _kw_context(f, kw)
    ctx["rank"] = {
        "goal": goal,
        "goal_page": t["goal_page"],
        "goal_position": t["goal_position"],
        "push_mode": t["push_mode"],
        "max_bid": t["max_bid"],
        "max_tos_pct": t["max_tos_pct"],
        "step_mode": t["step_mode"],
        "step_value": t["step_value"],
        "acos_guard_mult": t["acos_guard_mult"],
        "guard_acos": round(f.target_acos * t["acos_guard_mult"], 4),
        "max_pushes": t["max_pushes"],
        "pushes_used": st.get("pushes_used"),
        "latest": st.get("latest"),
        "observations": st.get("observations"),
        "streak": st.get("streak"),
        "note": "卡位与 R01/R03 互斥：同一个词只由一方驱动",
    }
    return ctx


# ================================================================
# R32 止损线 —— 按「花掉几单的钱」判，不按「显著吗」判
#
# 为什么必须有这条规则（2026-11-19 加，起因是一句真话：
# 85 个 memorial 的 front 一次都没非空过）：
#
# 平台的判定线是统计学问题：「这个词的转化率是否真的低于基线」。
# 在这个账户的规模上（日均 19 击、14 天滚动窗口），0 单需要 321 击、
# 1 单需要 597 击，而窗口内最大的词只 257–314 击 —— 滚走的天数和进来的
# 一样多，所以**统计学上永远给不出答案**。R01 因此只能一直出 observing。
#
# 但主人每天面对的是另一个问题：「这个词花掉的钱值不值」。这个问题不需要
# 显著性。`lat pulldown` 花 $226.43 / 257 击 / 0 单，按客单价 $186.99 与
# 目标 ACOS 30%，一单允许花 $56.10 —— 已经花掉 4 单的钱。
#
# 关键在诚实：即使它的转化率和全店一样（0.5013%），257 击出 0 单的概率是
# 27.6%，统计上证明不了它更差。所以这条规则**不下「它更差」的结论**，
# 只把「钱已经花掉 4 单」这个事实摆出来，处置权交给主人。
# ================================================================

def rule_32_stop_loss(f: Facts) -> RuleResult:
    res = RuleResult()
    cc = f.cost_ceiling or {}
    units_mult = cc.get("multiple")
    unit_cost = cc.get("unit_cost")
    if not cc.get("keywords") or not unit_cost:
        return res

    acct_clicks = f.account.get("clicks") or 0
    for kw in cc["keywords"]:
        share = (kw["clicks"] / acct_clicks) if acct_clicks else None
        # 停掉这个词省下的是全窗口花费；代价是这些点击带来的流量与可能的单。
        # 两个数都要说：只说省多少会让主人以为免费。
        rationale = (
            f"**止损线不是统计结论**：这个词在窗口内 {kw['clicks']} 击、花 "
            f"${kw['cost']:.2f}、**0 单**。按客单价 ${f.account['aov']:.2f} 与目标 "
            f"ACOS {f.target_acos:.0%}，一单允许花 ${unit_cost:.2f} —— 已经花掉 "
            f"**{kw['units']:.2f} 单的钱**（门槛 {units_mult:.0f} 单 = ${cc['threshold']:.2f}）。"
        )
        followed = (
            f" 按这个速度再跑 {kw['days']} 天还要再花 ${kw['still_open']:.2f}。"
            if kw.get("still_open") else ""
        )
        # 基线是**这个 ASIN/组合**的，不是全店 —— 全店 CVR 1.64% 与组合 0.33%
        # 差 5 倍，写成「全店基线」会让人以为组合不该这么差（那是 R30 在说的是）。
        _p0 = f.cvr_baseline.get("cvr") or 0
        _scope = f.cvr_baseline.get("scope") or "本组合"
        honest = (
            f" **但它不构成「这个词更差」的证据**：即使它的转化率就等于 "
            f"{_scope} 的基线 {_p0:.4%}，{kw['clicks']} 击出 0 单也有 "
            f"{_zero_order_prob(kw['clicks'], _p0):.1%} 的概率发生；"
            f"要判「更差」需要 {_need_clicks(_p0, 0)} 击，{f.window_days} 天窗口到不了。"
        )
        cost_side = (
            f" **停它的代价**：这些点击是组合里 "
            f"{share:.0%} 的流量" if share else ""
        )
        options = (
            " 三种处置都合理：停（省下这条钱、放弃这部分流量）、"
            "否词/换匹配方式（保留流量但换更准的意图）、"
            "或者认为这是冷启动期该付的学费继续观察。**引擎不下这个判断。**"
        )

        res.decisions.append(Decision(
            rule_id="R32",
            rule_version=RULE_VERSION,
            entity_type="keyword",
            entity_id=kw["keyword_id"],
            entity_label=kw["keyword_text"],
            action_type="pause_candidate",
            # current_value 必须记「要动的那个东西」，不是它的衍生指标。
            # 原来填的是 CPC（$0.8811），台账读起来像「把出价从 0.88 改掉」，
            # 但这条要动的是关键词本身；而该词真实 bid 是 0.8（冲突表还记 1.0）。
            # 诊断 agent 第 177 号反对：「这行台账的 current_value 记的不是配置。」
            # 记 bid 才对得上后台那格；CPC 放进 context 供参考。
            current_value=kw.get("bid"),
            proposed_value="pause_or_restructure",
            confidence="low",
            # 必须填 campaign_id（不只是塞进 context）：报告层「最贵的三笔钱谁在管」
            # 只认 Decision.campaign_id。塞进 context 而这里留空，那张表会说
            # 「今天没有任何规则对它出手」—— 而这条恰恰就是在对它出手。
            campaign_id=kw["campaign_id"],
            context={
                "window": {"start": f.start, "end": f.end, "days": f.window_days},
                "campaign_id": kw["campaign_id"],
                "ad_group_id": kw.get("ad_group_id"),
                "keyword_id": kw["keyword_id"],
                "cost_ceiling": {
                    "spend": kw["cost"], "clicks": kw["clicks"], "orders": 0,
                    # bid 与 cpc 一起给：bid 是后台那一格（要动的对象），
                    # cpc 是它实际买到的价（衍生指标）。只给 cpc 会让人以为
                    # 「把出价从 0.88 改掉」就是在动这条词。
                    "bid": kw.get("bid"), "cpc": kw.get("cpc"),
                    "units": kw["units"], "multiple": units_mult,
                    "unit_cost": unit_cost, "threshold": cc["threshold"],
                    "still_open": kw.get("still_open"),
                    "traffic_share": round(share, 4) if share else None,
                    # 前端用它解释「为什么这条不在待批但值得你拍板」
                    "gate_reason": (
                        f"止损线：花掉 {kw['units']:.2f} 单的钱（门槛 {units_mult:.0f} 单）、"
                        f"0 单。这不是显著性判定 —— 判「更差」需 "
                        f"{_need_clicks(f.cvr_baseline.get('cvr') or 0, 0)} 击，"
                        f"14 天窗口到不了"
                    ),
                },
            },
            calculation=[
                {"step": "花了多少单的钱",
                 "formula": "窗口花费 ÷ (客单价 × 目标ACOS)",
                 "params": {"cost": kw["cost"], "aov": f.account["aov"],
                            "target_acos": f.target_acos,
                            "unit_cost": unit_cost, "multiple": units_mult},
                 "result": kw["units"]},
                {"step": "门槛",
                 "formula": "客单价 × 目标ACOS × 倍数",
                 "params": {"unit_cost": unit_cost, "multiple": units_mult},
                 "result": cc["threshold"]},
                {"step": "显著性（诚实标注：不构成证据）",
                 "formula": "P(X=0 | clicks, 基线CVR) vs 判定线",
                 "params": {"clicks": kw["clicks"],
                            "baseline_cvr": f.cvr_baseline.get("cvr"),
                            "need_clicks": _need_clicks(f.cvr_baseline.get("cvr") or 0, 0)},
                 "result": "insufficient"},
            ],
            significance={
                "clicks": kw["clicks"], "orders": 0,
                # 这条**不做假设检验**。显式写 False，别让下游误以为它是统计结论；
                # 够格进待批靠的是 basis='cost_ceiling'（见 report._is_conclusive）。
                "statistically_conclusive": False,
                "basis": "cost_ceiling",
                "cost_evidence": {
                    "spend": kw["cost"], "units": kw["units"],
                    "multiple": units_mult, "threshold": cc["threshold"],
                    "unit_cost": unit_cost,
                },
                "confidence_basis": (
                    f"零单，{kw['clicks']} 击低于 0 单判定线 "
                    f"{_need_clicks(f.cvr_baseline.get('cvr') or 0, 0)} 击，"
                    f"统计上判不了；这条建议的依据是**已花掉 "
                    f"{kw['units']:.2f} 单的钱**，不是转化差异的显著性"
                ),
            },
            rationale=rationale + followed + honest + cost_side + options,
            spend_impact=kw["cost"],
            expires_at=_expires(),
        ))
    return res


def _zero_order_prob(clicks: int, baseline_cvr: float) -> float:
    """在这个基线 CVR 下，clicks 次点击一单不出的概率。用泊松近似，
    与 facts.significance 的算法一致（pv 很小时二项与泊松几乎相同）。"""
    lam = clicks * baseline_cvr
    if lam <= 0:
        return 1.0
    return math.exp(-lam)


def _need_clicks(baseline_cvr: float, orders: int = 0) -> int:
    """判「更差」需要的点击数（0 单判定线）。拿不到基线时返回 0，
    调用方拼文案时不会崩，只会显示 0。"""
    if not baseline_cvr or baseline_cvr <= 0:
        return 0
    from facts import min_clicks_for_orders
    n = min_clicks_for_orders(baseline_cvr, orders)
    return int(n) if n else 0


# ================================================================
# 编排
# ================================================================

ALL_RULES = {
    "R30": rule_30_target_reachable,
    "R01": rule_01_reduce_bid,
    "R03": rule_03_scale_up,
    "R02": rule_02_negative_candidates,
    "R04": rule_04_harvest,
    "R08": rule_08_budget,
    "R09": rule_09_placement,
    "R21": rule_21_expand,
    "R31": rule_31_rank_position_push,
    "R32": rule_32_stop_loss,
}

# 执行顺序有意义：R30 先跑（它定全局基调），
# R08 先于 R03（预算打满时加价无意义，要先看到加预算建议）
# R31 排在 R03 之后：卡位已经接管词的话，R03 会自己让路，顺序不影响结论
RULE_ORDER = ["R30", "R32", "R01", "R08", "R03", "R31", "R09", "R04", "R02", "R21"]


# 动作分组：先止血，再修结构，最后扩张
_ACTION_ORDER = {
    "alert": 0,             # 目标不可达这类全局告警排最前
    "pause_candidate": 1,
    "floor_breach": 1,
    "reduce_bid": 1,
    "add_negative": 1,
    "raise_budget": 2,
    "adjust_placement": 2,
    "rank_step_down": 2,     # 到位后降价：省钱，归止血侧
    "raise_bid": 3,
    "raise_bid_blocked": 3,
    "harvest": 3,
    "rank_push": 3,          # 卡位加价：扩张侧，跟加出价同组
    "observe": 4,
}


def run_all(f: Facts, enabled: list[str] | None = None) -> RuleResult:
    enabled = enabled or RULE_ORDER
    out = RuleResult()
    for rid in RULE_ORDER:
        if rid not in enabled:
            continue
        fn = ALL_RULES.get(rid)
        if not fn:
            continue
        r = fn(f)
        out.decisions.extend(r.decisions)
        out.suppressed.extend(r.suppressed)
    # 排序：先止血（省钱的）后扩张，同组内按影响金额
    out.decisions.sort(
        key=lambda d: (
            _ACTION_ORDER.get(d.action_type, 9),
            -abs(d.spend_impact or 0),
        )
    )
    return out
