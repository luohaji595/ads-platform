"""L1 事实层 —— 所有阈值在这里现算，代码里不写死任何数字。

设计原则
--------
1. 任何阈值都必须能追溯到一条 SQL + 一个公式，返回值带 calculation 链。
2. 零值语义保护：判断「有没有单」只看 ads_orders / clicks，
   绝不看 acos / cpa —— 本账户 campaign 无单时 acos=0.0、searchterm 无单时 acos=null，
   同一个「没有订单」在两张表里是两种值，直接比较必然出错。
3. 归因窗口排除：Amazon 订单会在点击后 1-7 天内回填，
   最近 N 天（默认 2）的数据还没归因完，拿它做判断会系统性高估"无单"。

所有函数只读，不改数据库。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
import scope
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

# 这些是「默认值」，不是「阈值」。阈值一律现算，默认值只是算法参数。
DEFAULT_TARGET_ACOS = 0.30
# 加预算步长（倍数）：R08 现在一次 +20%。被 param_override(max_budget_step) 覆盖后
# 以覆盖值为准 —— 那条覆盖就是主人打钩说「幅度小一点」沉淀下来的东西。
DEFAULT_BUDGET_STEP = 1.2
DEFAULT_ATTR_EXCLUDE_DAYS = 2
DEFAULT_CONFIDENCE = 0.80
DEFAULT_WINDOW_DAYS = 14


# ---------------------------------------------------------------- 基础工具

def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    conn.row_factory = sqlite3.Row
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else {}


def date_window(
    days: int = DEFAULT_WINDOW_DAYS,
    exclude_days: int = DEFAULT_ATTR_EXCLUDE_DAYS,
    today: date | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[str, str]:
    """返回 (start, end)。

    end = 数据里最新一天 - exclude_days。若给了 conn，就用库里的实际最大日期，
    否则用 today - 1（Amazon 报表 T+1 才可用）。
    """
    if conn is not None:
        r = _one(conn, "select max(report_date) d from fact_campaign_daily where " + scope.campaign_predicate())
        latest = r.get("d")
    else:
        latest = None
    if latest:
        end_d = date.fromisoformat(latest)
    else:
        end_d = (today or date.today()) - timedelta(days=1)
    end_d = end_d - timedelta(days=exclude_days)
    start_d = end_d - timedelta(days=days - 1)
    return start_d.isoformat(), end_d.isoformat()


def _safe_div(a: float, b: float) -> float | None:
    return (a / b) if b else None


def _r(x: float | None, nd: int = 4) -> float | None:
    return round(x, nd) if x is not None else None


# ---------------------------------------------------------------- 账户漏斗

def account_funnel(conn: sqlite3.Connection, start: str, end: str,
                   scoped: bool = True) -> dict:
    """漏斗。以 campaign 表为准（keyword 表只有手动关键词，会漏掉自动活动）。

    `scoped=True`（默认）只算当前投放组合 —— 这是引擎的作业范围，R01/R03 等
    逐对象规则都按它算。`scoped=False` 算全店所有组合，用于 R30 这类
    「全店口径」的账户级判断。

    为什么要区分：曾经 R30 拿 `scoped=True` 的数却标 `entity_label="全店"`，
    诊断 agent 一眼看出「三个数字全来自本组合，账户自身 CVR 1.69% 在盈亏平衡
    1.47% 之上，这是把组合级问题伪装成账户级结论」。名不副实比数字错更坏 ——
    读者会学会不看这条告警。
    """
    scope_clause = ("and campaign_id IN (SELECT DISTINCT campaign_id "
                    "FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))"
                    if scoped else "")
    r = _one(conn, f"""
        select coalesce(sum(impressions),0) imp,
               coalesce(sum(clicks),0)      ck,
               coalesce(sum(cost),0.0)      cost,
               coalesce(sum(ads_orders),0)  od,
               coalesce(sum(ads_sales),0.0) sales
        from fact_campaign_daily
        where report_date between ? and ?
          {scope_clause}
    """, (start, end))

    imp = int(r.get("imp") or 0)
    ck = int(r.get("ck") or 0)
    cost = float(r.get("cost") or 0.0)
    od = int(r.get("od") or 0)
    sales = float(r.get("sales") or 0.0)

    out = {
        "impressions": imp,
        "clicks": ck,
        "cost": round(cost, 2),
        "orders": od,
        "sales": round(sales, 2),
        "ctr": _r(_safe_div(ck, imp), 6),
        "cvr": _r(_safe_div(od, ck), 6),
        "cpc": _r(_safe_div(cost, ck), 4),
        "aov": _r(_safe_div(sales, od), 2),
        # 零值保护：无销售 → None，不是 0.0
        "acos": _r(_safe_div(cost, sales), 4),
        "roas": _r(_safe_div(sales, cost), 4),
        "cpa": _r(_safe_div(cost, od), 2),
        "window": {"start": start, "end": end},
    }
    out["scoped"] = scoped
    out["label"] = scope.SCOPE_LABEL if scoped else "全店（所有投放组合合计）"
    out["calculation"] = [{
        "step": "账户漏斗" if not scoped else "组合漏斗",
        "sql": ("sum(impressions/clicks/cost/ads_orders/ads_sales) from fact_campaign_daily "
                "where report_date between :start and :end"
                + ("" if not scoped else " and campaign_id in (本组合的活动)")),
        "params": {"start": start, "end": end, "scoped": scoped},
        "result": {k: out[k] for k in ("impressions", "clicks", "cost", "orders", "sales")},
        "note": "acos/cpa/roas 在 orders 或 sales 为 0 时返回 null，不返回 0",
    }]
    return out


def asin_cvr_baseline(conn: sqlite3.Connection, start: str, end: str,
                      campaign_id: str | None = None) -> dict:
    """CVR 基准。

    07 §3.1 IvyeaOps 明确规定：CVR 基准必须用「该 ASIN 自身的广告 CVR」，
    不能用全店 CVR —— 不同产品线转化率差好几倍，混在一起会把好词判成坏词。
    这里用 campaign 作为 ASIN 的代理（本账户一个 campaign 基本对应一个产品线）。
    """
    if campaign_id:
        r = _one(conn, """
            select coalesce(sum(clicks),0) ck, coalesce(sum(ads_orders),0) od
            from fact_campaign_daily
            where report_date between ? and ? and campaign_id = ? and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        """, (start, end, campaign_id))
        scope_name = f"campaign:{campaign_id}"
    else:
        r = _one(conn, """
            select coalesce(sum(clicks),0) ck, coalesce(sum(ads_orders),0) od
            from fact_campaign_daily where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        """, (start, end))
        scope_name = scope.SCOPE_LABEL

    ck = int(r.get("ck") or 0)
    od = int(r.get("od") or 0)
    return {
        "scope": scope_name,
        "clicks": ck,
        "orders": od,
        "cvr": _r(_safe_div(od, ck), 6),
        "calculation": [{
            "step": "CVR 基准",
            "formula": "orders / clicks",
            "params": {"scope": scope_name, "start": start, "end": end},
            "result": {"clicks": ck, "orders": od, "cvr": _r(_safe_div(od, ck), 6)},
            "note": "基准必须取同 ASIN/同 campaign，不能用全店",
        }],
    }


def breakeven_cvr(cpc: float, aov: float, target_acos: float) -> dict:
    """盈亏平衡 CVR = CPC / (AOV × 目标ACOS)。

    CVR 低于这条线时，无论怎么调 bid 都不可能达标 —— 因为 CVR 和 bid 无关。
    这是 R30「目标不可达告警」的核心。
    """
    if not aov or not target_acos:
        return {"breakeven_cvr": None, "calculation": []}
    be = cpc / (aov * target_acos)
    return {
        "breakeven_cvr": _r(be, 6),
        "inputs": {"cpc": cpc, "aov": aov, "target_acos": target_acos},
        "calculation": [{
            "step": "盈亏平衡 CVR",
            "formula": "cpc / (aov * target_acos)",
            "params": {"cpc": cpc, "aov": aov, "target_acos": target_acos},
            "result": _r(be, 6),
            "meaning": f"目标 ACOS {target_acos:.0%} 要求 CVR ≥ {be:.2%}；低于此线调 bid 无效",
        }],
    }


def target_acos_required(cpc: float, aov: float, cvr: float) -> dict:
    """目标 ACOS 提到多少，当前这套 CPC/客单价/转化率才刚好达标。

    ⚠ **这个数恒等于当前 ACOS，它不是一个退出条件。**

    诊断 agent 第 146 号反对（第七轮打 R30）指出：
    「它的退出条件等于『放弃目标』：全店放宽到 45.9%、本组合放宽到 85.9%，
     这两个数正好是当前实际 ACOS，所以它只能靠改目标才消失。」

    验算：`breakeven_cvr = cpc/(aov*target_acos)` 反解出
    `target_acos = cpc/(aov*cvr)`；而 `ACOS = cost/sales = (clicks*cpc)/(orders*aov)`
    `= cpc/(aov*cvr)`。**两个式子是同一个式子**，实测差 7e-6（浮点误差）。
    所以「目标放宽到 45.9%」等价于「接受现在这个样子」。

    留这个函数是因为它仍然有用：它把「当前 ACOS」换算成「要认命的那个目标值」，
    让读者看见「降到 30% 需要什么」。但**不能拿它当退出条件**——
    那等于说「这条告警在放弃目标时消失」，任何广告动作都触发不了它。
    真正的退出条件只有一个：CVR / CPC / 客单价真的变了（见 R30 的 `alert_inputs`）。
    """
    if not aov or not cvr:
        return {"target_acos": None, "calculation": []}
    need = cpc / (aov * cvr)
    return {
        "target_acos": _r(need, 6),
        # 明写它等于当前 ACOS，免得下游再把它当「退出条件」用一遍。
        "equals_current_acos": True,
        "inputs": {"cpc": cpc, "aov": aov, "cvr": cvr},
        "calculation": [{
            "step": "达标所需的目标 ACOS（= 当前 ACOS）",
            "formula": "cpc / (aov * cvr)  ≡  cost / sales",
            "params": {"cpc": cpc, "aov": aov, "cvr": cvr},
            "result": _r(need, 6),
            "meaning": (f"目标 ACOS 放宽到 {need:.1%} 时，当前 CPC ${cpc:.2f}、"
                        f"客单价 ${aov:.0f}、CVR {cvr:.2%} 刚好打平。"
                        f"注意 {need:.1%} 就是当前实际 ACOS —— 这不是退出条件，"
                        f"是「认命」的另一种写法。"),
        }],
    }


def alert_history(conn: sqlite3.Connection, rule_id: str,
                  since: str | None = None) -> dict:
    """某条规则此前出现过多少天、从哪天起、最近哪天。

    **按 distinct 日期算，不按行数。** 同一个业务日里引擎可能重跑多次，
    而 `_dedupe_alerts` 比的是 rationale 文本 —— 文案一改就插新行。
    实测 R30 在 2026-11-16~18 三天里有 43 行，按行数说「已出现 43 次」是假的。

    用途：诊断 agent 第 135 号反对指出「今天输入里没有它此前出现次数的记录，
    所以我不说『已经重复了几天』」—— 它说得对，那就把这份历史喂给它。
    """
    try:
        rows = _rows(conn,
                     """SELECT DISTINCT substr(decided_at, 1, 10) AS d
                          FROM decision_log
                         WHERE rule_id = ?
                         ORDER BY d""",
                     (rule_id,))
    except sqlite3.Error:
        return {}
    days = [r["d"] for r in rows if r["d"]]
    if since:
        days = [d for d in days if d >= since]
    if not days:
        return {"rule_id": rule_id, "days_seen": 0, "first_day": None,
                "last_day": None, "days": [], "inputs": None}
    return {
        "rule_id": rule_id,
        "days_seen": len(days),
        "first_day": days[0],
        "last_day": days[-1],
        "days": days,
        "inputs": _last_alert_inputs(conn, rule_id),
    }


def _last_alert_inputs(conn: sqlite3.Connection, rule_id: str) -> dict | None:
    """上一次这条告警告的是**什么数**。

    存在 `decision_log.context_json` 的 `alert_inputs` 里（R30 自己写）。
    没有它，就分不清「今天的不可达」和「昨天那条不可达」是不是同一件事 ——
    而诊断 agent 第 140 号反对要的正是这个：
    「它必然天天出现、天天无法结案，从第 3 天起不再携带任何新信息」。

    携带新信息的判据就是输入变了。所以要把上次的输入存下来。
    """
    try:
        rows = _rows(conn,
                     """SELECT context_json FROM decision_log
                         WHERE rule_id = ?
                         ORDER BY decision_id DESC LIMIT 1""",
                     (rule_id,))
    except sqlite3.Error:
        return None
    if not rows:
        return None
    raw = rows[0].get("context_json")
    if not raw:
        return None
    try:
        ctx = json.loads(raw)
    except (ValueError, TypeError):
        return None
    got = ctx.get("alert_inputs")
    return got if isinstance(got, dict) else None


def min_clicks(baseline_cvr: float, confidence: float = DEFAULT_CONFIDENCE) -> dict:
    """判定「这个词真的不行」所需的最少点击 —— **只对零单成立**。

    min_clicks = ceil( ln(1-confidence) / ln(1-baseline_cvr) )
    含义：如果一个词的 CVR 等于基准，连续 N 次点击都不出单的概率 < 1-confidence，
    那么 N 次点击零单就可以认为「它比基准差」。

    ⚠ 这个式子隐含了「观测到的订单数恰好是 0」。
    一个已经出了 k 单的对象要判「比基准差」，需要的点击远不止这个数
    （k=1 约 470 击，k=2 约 672 击，见 min_clicks_for_orders）。
    要判具体对象请用 significance() / min_clicks_for_orders()，不要用这个常数。
    """
    if not baseline_cvr or baseline_cvr <= 0 or baseline_cvr >= 1:
        return {"min_clicks": None, "calculation": []}
    n = math.ceil(math.log(1 - confidence) / math.log(1 - baseline_cvr))
    return {
        "min_clicks": n,
        "confidence": confidence,
        "baseline_cvr": baseline_cvr,
        "applies_to_orders": 0,
        "calculation": [{
            "step": "最小点击量",
            "formula": "ceil( ln(1-confidence) / ln(1-baseline_cvr) )",
            "params": {"confidence": confidence, "baseline_cvr": baseline_cvr},
            "result": n,
            "meaning": f"{n} 次点击**零单**，才有 {confidence:.0%} 把握说它比基准差",
            "caveat": "只对零单成立；已出 k 单的对象需要更多点击，见 min_clicks_for_orders",
        }],
    }


# ---------------------------------------------------------------- 二项检验
#
# 为什么需要这一节：
#   min_clicks 那个常数只回答「零单是不是异常」，而实战里绝大多数对象不是零单
#   （lat pulldown 248 击 1 单、lat pulldown machines 235 击 1 单）。
#   对这些对象，「需要多少次点击才能下结论」必须把已观测的订单数一起放进检验，
#   否则等于用零单的尺子去量出了一单的词，结论必然偏松。
#
# 两个方向都要算：
#   p_worse  = P(X ≤ k | n, p0)  —— 出得比基线少，小才算差
#   p_better = P(X ≥ k | n, p0)  —— 出得比基线多，小才算好
#   R01 降 bid / R02 否词要的是 p_worse，R03 加 bid 要的是 p_better。
#   同一个数字不能同时服务两个方向。

def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X ≤ k)，X ~ Binomial(n, p)。精确二项，不依赖 scipy。"""
    if n <= 0:
        return 1.0
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    # 对称性：k 超过中位时改用补集，避免循环 n 次
    if k > (n - 1) / 2.0:
        return 1.0 - _binom_cdf(n - k - 1, n, 1.0 - p)
    lp, lq = math.log(p), math.log1p(-p)
    lg = math.lgamma(n + 1)
    terms = [
        lg - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        for i in range(k + 1)
    ]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X ≥ k)，X ~ Binomial(n, p)。"""
    if k <= 0:
        return 1.0
    return max(0.0, 1.0 - _binom_cdf(k - 1, n, p))


def min_clicks_for_orders(baseline_cvr: float, orders: int,
                          confidence: float = DEFAULT_CONFIDENCE,
                          cap: int = 200_000) -> int | None:
    """已经观测到 `orders` 单时，要多少次点击才能判「它比基准差」。

    求最小的 n，使 P(X ≤ orders | n, p0) < 1-confidence。
    orders=0 时退化为 min_clicks（253 击），这是唯一与原式重合的情形。
    返回 None 表示即使 cap 次点击也判不出来（说明它出单出得不差）。
    """
    if not baseline_cvr or baseline_cvr <= 0 or baseline_cvr >= 1:
        return None
    alpha = 1.0 - confidence
    k = max(0, int(orders or 0))
    if _binom_cdf(k, cap, baseline_cvr) >= alpha:
        return None
    lo, hi = 1, cap
    while lo < hi:
        mid = (lo + hi) // 2
        if _binom_cdf(k, mid, baseline_cvr) < alpha:
            hi = mid
        else:
            lo = mid + 1
    return lo


def significance(clicks: int, orders: int, baseline_cvr: float,
                 confidence: float = DEFAULT_CONFIDENCE) -> dict:
    """对单个对象的转化率做二项检验，给出方向与把握。

    这是替代「拿一个常数 need 去比所有对象」的正确做法：
    判定线随该对象已出的订单数自适应，且两个方向分开算。
    """
    clicks = int(clicks or 0)
    orders = int(orders or 0)
    base = {"clicks": clicks, "orders": orders, "baseline_cvr": baseline_cvr,
            "confidence": confidence}
    if clicks <= 0 or not baseline_cvr or not (0 < baseline_cvr < 1):
        return {**base, "p_worse": None, "p_better": None, "verdict": "unknown",
                "need_for_orders": None, "need_if_zero": None,
                "expected_orders": None, "statistically_conclusive": False,
                "note": "无基线或零点击，判不了"}

    p_worse = _binom_cdf(orders, clicks, baseline_cvr)
    p_better = _binom_sf(orders, clicks, baseline_cvr)
    alpha = 1.0 - confidence

    if p_worse < alpha:
        verdict = "worse"
    elif p_better < alpha:
        verdict = "better"
    else:
        verdict = "inconclusive"

    need = min_clicks_for_orders(baseline_cvr, orders, confidence)
    expected = clicks * baseline_cvr
    return {
        **base,
        "p_worse": round(p_worse, 6),
        "p_better": round(p_better, 6),
        "verdict": verdict,
        "need_for_orders": need,
        "need_if_zero": min_clicks(baseline_cvr, confidence).get("min_clicks"),
        "expected_orders": round(expected, 2),
        "statistically_conclusive": verdict != "inconclusive",
        "note": (
            f"{clicks} 击 {orders} 单，按基线 {baseline_cvr:.2%} 应出 {expected:.2f} 单；"
            f"偏少一侧 p={p_worse:.3f}，偏多一侧 p={p_better:.3f}"
            + ("" if need is None
               else f"；该订单数下判「更差」需 {need} 击")
        ),
    }


def min_clicks_table(baseline_cvr: float, max_orders: int = 5,
                     confidence: float = DEFAULT_CONFIDENCE) -> dict:
    """把「出 k 单时需要多少点击」列成表，供报告解释用。"""
    rows = []
    for k in range(0, max_orders + 1):
        rows.append({"orders": k,
                     "clicks_needed": min_clicks_for_orders(baseline_cvr, k, confidence)})
    return {
        "baseline_cvr": baseline_cvr,
        "confidence": confidence,
        "rows": rows,
        "calculation": [{
            "step": "按订单数自适应的判定线",
            "formula": "min n s.t. P(X≤k | n, p0) < 1-confidence",
            "params": {"baseline_cvr": baseline_cvr, "confidence": confidence},
            "result": rows,
            "note": "旧式 ceil(ln(1-conf)/ln(1-p0)) 只等于 k=0 这一行",
        }],
    }


def detect_power_orders(baseline_orders_per_day: float, relative_effect: float,
                        power: float = 0.80, alpha: float = 0.05) -> dict:
    """检测「规则改动的效果」需要多少订单。

    两样本比例检验的近似：n ≈ 16 / (effect^2) （effect 为相对变化，取 80% power/5% alpha 的常用近似）
    这里用更精确一点的 2*(z_{1-a/2}+z_{1-b})^2 / (ln(OR))^2 形式，
    但为了可解释，直接用正态近似的样本量公式并在结果里注明是近似。
    """
    if relative_effect <= 0:
        return {"orders_per_group": None, "calculation": []}
    z_alpha = 1.959964  # 双侧 5%
    z_beta = 0.841621   # 80% power
    # 对比例 p 检测相对变化 d，n = 2*(z_a+z_b)^2 * p(1-p) / (p*d)^2，取 p=0.5 的最坏情形简化
    n = 2 * (z_alpha + z_beta) ** 2 * 0.25 / ((0.5 * relative_effect) ** 2)
    n = math.ceil(n)
    days = math.ceil(n / baseline_orders_per_day) if baseline_orders_per_day else None
    return {
        "orders_per_group": n,
        "days_needed": days,
        "calculation": [{
            "step": "检测效果所需样本",
            "formula": "n = 2*(z_0.975+z_0.80)^2 * p(1-p) / (p*effect)^2, p=0.5",
            "params": {"relative_effect": relative_effect, "power": power, "alpha": alpha},
            "result": {"orders_per_group": n, "days_needed": days},
            "note": "近似值，用于判断「这个改动能不能被验证」，不是精确样本量",
        }],
    }


# ---------------------------------------------------------------- CPC 校准

def campaign_weighted_adjustment(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """每个活动在窗口内的**点击加权位次溢价**（%）。

    为什么必须按点击加权、必须逐日：位次溢价是分位次设的，而同一个活动在
    TOS 和详情页的量完全不同。拿「最高溢价」当活动溢价，等于假设所有点击
    都发生在最贵的那个位次上 —— 会把目标价算到不存在的价位。

    返回 {campaign_id: {"adj_pct", "clicks", "days", "by_placement"}}。
    `adj_pct` 是 `sum(bid_adjustment*clicks)/sum(clicks)`，null 视为 0
    （「Off Amazon」这类行没有溢价字段，那是没有这个位次加价，不是缺数据）。
    """
    rows = _rows(conn, """
        select campaign_id,
               sum(coalesce(bid_adjustment, 0) * clicks) / sum(clicks) adj_pct,
               sum(clicks) clicks, count(distinct report_date) days
        from fact_placement_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
          and clicks > 0
        group by campaign_id
    """, (start, end))

    by_pl = _rows(conn, """
        select campaign_id, placement,
               sum(coalesce(bid_adjustment, 0) * clicks) / sum(clicks) adj_pct,
               sum(clicks) clicks
        from fact_placement_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
          and clicks > 0
        group by campaign_id, placement
    """, (start, end))

    detail: dict[str, list[dict]] = {}
    for r in by_pl:
        detail.setdefault(r["campaign_id"], []).append({
            "placement": r["placement"],
            "adj_pct": round(float(r["adj_pct"] or 0.0), 1),
            "clicks": int(r["clicks"] or 0),
        })

    out: dict[str, dict] = {}
    for r in rows:
        out[r["campaign_id"]] = {
            "adj_pct": round(float(r["adj_pct"] or 0.0), 1),
            "clicks": int(r["clicks"] or 0),
            "days": int(r["days"] or 0),
            "by_placement": sorted(detail.get(r["campaign_id"], []),
                                   key=lambda d: -d["clicks"]),
        }
    return out


def cpc_bias(conn: sqlite3.Connection, start: str, end: str,
             campaign_adj: dict | None = None) -> dict:
    """CPC / **有效出价** 比值 —— 这是「降 bid 会不会降 CPC」的换算系数。

    曾经的错误（2026-11-18 诊断 agent Q D3 抓出，实测推翻）
    ------------------------------------------------------
    原来算的是 `median(cpc / 名义 bid)`，全账户得 **2.26**。这个数看起来像
    「CPC 是出价的 2.26 倍」，实际是**把三档溢价的活动混在一起取中位数**：

        活动 87365978371695  溢价   4.5%  cpc/bid = 1.031   点击 264（32%）
        活动 175723193023274 溢价   0.2%  cpc/bid = 0.785   点击 255（31%）
        活动 47309998237757  溢价 280.0%  cpc/bid = 3.300   点击 115（14%）
        活动 237761921757002 溢价 320.3%  cpc/bid = 4.000   点击  59（ 7%）

    把 `bid × (1+溢价/100)` 叫「有效出价」之后，逐活动的 `cpc/有效出价`
    全部落在 0.79–1.03，**全账户中位数 0.97** —— 也就是 CPC ≈ 有效出价，
    溢价部分被完整地传递到了成交价里，没有额外的神秘放大。

    后果（这是真 bug，不是精度问题）：`optimum = raw / bias` 里 raw 是目标 CPC，
    除 2.26 等于把目标 bid 压到应有值的 1/2.26，三条 R01 全部因此撞上地板
    $0.40 变成 `floor_hit` → 只出 observe，然后单步 20% 上限把结果拉回去。
    也就是说**「目标价」这个数从来没被真正用过**，降 bid 全靠单步上限。
    换成 0.97 后：lat pulldown machines 目标 $0.443（> 地板，不再 floor_hit）、
    cable machine $0.640、lat pulldown $0.231（仍 < 地板，确实该 floor_hit）。

    语义（调用方必须按这个理解）
    ----------------------------
    `overall` = median(cpc / 有效出价) ≈ 1.0，含义是「目标 CPC → 目标有效出价」。
    要落成**名义 bid** 还得再除 `(1 + 该活动的溢价/100)`：
        名义 bid = 目标 CPC / bias / (1 + adj/100)
    高溢价活动（280%/350%）的这一步会把名义目标价压到 $0.12 量级，
    低于 Amazon 的地板 —— 那不是「该降 base bid」，是**溢价本身太贵**，
    真正的杠杆是降溢价。调用方要把这两种情况分开说。

    用中位数不用均值 —— 均值被 $9.0 这类极端值拉高。
    """
    rows = _rows(conn, """
        select cpc, bid, clicks, ad_group_id, campaign_id, report_date
        from fact_keyword_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
          and bid > 0 and cpc > 0 and clicks > 0
    """, (start, end))

    if not rows:
        return {"overall": None, "nominal_overall": None, "n": 0, "calculation": []}

    if campaign_adj is None:
        campaign_adj = campaign_weighted_adjustment(conn, start, end)

    def adj_of(cid: str) -> float:
        return float((campaign_adj.get(cid) or {}).get("adj_pct") or 0.0)

    nominal = [r["cpc"] / r["bid"] for r in rows]
    eff_ratios: list[float] = []
    pairs: list[tuple[float, float]] = []      # (名义比值, 有效比值)
    by_campaign: dict[str, list[float]] = {}
    by_group: dict[str, list[float]] = {}
    for r in rows:
        eff_bid = r["bid"] * (1 + adj_of(r["campaign_id"]) / 100)
        if eff_bid <= 0:
            continue
        v = r["cpc"] / eff_bid
        eff_ratios.append(v)
        pairs.append((r["cpc"] / r["bid"], v))
        by_campaign.setdefault(str(r["campaign_id"]), []).append(v)
        by_group.setdefault(str(r["ad_group_id"]), []).append(v)

    if not eff_ratios:
        return {"overall": None, "nominal_overall": None, "n": 0, "calculation": []}

    eff_sorted = sorted(eff_ratios)

    def pct(p: float) -> float:
        idx = min(len(eff_sorted) - 1, max(0, int(round(p * (len(eff_sorted) - 1)))))
        return round(eff_sorted[idx], 4)

    overall = round(median(eff_ratios), 4)
    nominal_overall = round(median(nominal), 4)

    return {
        "overall": overall,
        "nominal_overall": nominal_overall,
        "mean": round(sum(eff_ratios) / len(eff_ratios), 4),
        "p10": pct(0.10),
        "p90": pct(0.90),
        "n": len(eff_ratios),
        "by_ad_group": {
            g: {"median": round(median(v), 4), "n": len(v)}
            for g, v in by_group.items() if len(v) >= 5
        },
        "by_campaign": {
            c: {"median": round(median(v), 4), "n": len(v),
                "adj_pct": adj_of(c)}
            for c, v in by_campaign.items()
        },
        "campaign_adj": campaign_adj,
        "calculation": [{
            "step": "CPC 校准系数（相对有效出价）",
            "formula": "median(cpc / (bid × (1 + 位次溢价/100)))",
            "params": {"start": start, "end": end, "n": len(eff_ratios)},
            "result": {"median": overall, "p10": pct(0.10), "p90": pct(0.90),
                       "nominal_median": nominal_overall},
            "note": ("名义口径 median(cpc/bid) 是 "
                     f"{nominal_overall}，但那是把 0% 和 350% 溢价的活动混在一起取中位数的产物，"
                     "不代表任何真实换算关系。溢价已经计入分母，不能再乘一次。"),
        }],
    }


# ---------------------------------------------------------------- bid 地板

def bid_floor(conn: sqlite3.Connection, start: str, end: str,
              bucket_width: float = 0.10,
              campaign_adj: dict | None = None) -> dict:
    """bid 地板：只报「能证实的部分」，不假装能算出精确值。

    **分母必须是有效出价，不是名义 bid。** 诊断 agent 第 150 号问题（Q D4）：
    「地板 $0.40 说的是名义 bid。这个活动名义 bid 已经在地板上，实际 CPC 却是 $1.60
     （cpc_bias 里该活动 adj_pct 320%）。地板该按名义价算，还是按有效出价算？」

    按有效出价。实测按名义 bid 分桶，`bid=0.40` 那一桶有 221 击 —— 但那三个词的溢价是
    142% / 280% / 321%，有效出价分别是 $0.97 / $1.52 / $1.68。也就是说
    「$0.40 这个价位有量」这句话是假的：**没有任何一个词真的用 $0.40 买过点击**。
    拿它当地板去卡别的活动，会得出「你已在地板上，不能再降」的错误结论 ——
    而实际那个活动的名义价还能从 $0.40 降到 $0.13 才碰到真实地板。

    为什么不能直接估精确值
    ----------------------
    直觉做法是「按有效出价分桶、看每桶的点击量、找最低还能出量的桶」。
    但本账户数据里这仍被关键词热度混淆：
      · 有效 $0.80 的 26 个词共 87 击，只有 8% 的词拿到 5+ 击 —— 不是价格问题，是没人搜
      · 有效 $0.70 的 1 个词拿了 325 击 —— 不是因为 $0.70 合适，是因为它是热门词
    分不清「拿不到量」是价格问题还是需求问题，就算不出精确地板。

    所以只给三个能站住的结论
    ------------------------
    1. hard_lower_bound —— 低于这个有效价，无论什么词都拿不到量（跨热度都成立，所以可信）
    2. observed_working_min —— 实际观察到有正常出量的最低**有效**出价（下界，不是地板）
    3. estimable=False + 需要实验 —— 精确地板只能靠单变量实验测

    返回的 `floor` 是**有效出价**口径。调用方要拿它约束名义 bid 时，
    必须除以 `(1 + 该活动的点击加权溢价/100)`。
    """
    adj_map = campaign_adj if campaign_adj is not None else campaign_weighted_adjustment(
        conn, start, end)
    rows = _rows(conn, """
        select bid, keyword_id, campaign_id, sum(clicks) ck, sum(impressions) imp
        from fact_keyword_daily
        where report_date between ? and ? and bid > 0
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by bid, keyword_id, campaign_id
    """, (start, end))

    if not rows:
        return {"floor": None, "estimable": False, "confidence": "none",
                "basis": "effective_bid", "buckets": [], "calculation": []}

    buckets: dict[float, list[dict]] = {}
    for r in rows:
        a = float((adj_map.get(r["campaign_id"]) or {}).get("adj_pct") or 0.0)
        eff = float(r["bid"]) * (1 + a / 100)
        b = round(round(eff / bucket_width) * bucket_width, 2)
        buckets.setdefault(b, []).append({
            "kw": r["keyword_id"],
            "ck": int(r["ck"] or 0),
            "imp": int(r["imp"] or 0),
            "nominal_bid": float(r["bid"]),
            "adj_pct": a,
            "effective_bid": round(eff, 4),
        })

    detail = []
    for b in sorted(buckets):
        items = buckets[b]
        cks = [i["ck"] for i in items]
        imps = [i["imp"] for i in items]
        detail.append({
            "bid_bucket": b,
            "keywords": len(items),
            "total_impressions": sum(imps),
            "total_clicks": sum(cks),
            "median_clicks": median(cks),
            "max_clicks": max(cks),
            "share_kw_with_5plus_clicks": round(sum(1 for c in cks if c >= 5) / len(cks), 3),
        })

    # 1) 硬下界：从最低桶往上，连续「整桶点击 < 10」的最高桶 → 这个价位基本拿不到量
    hard_lower = None
    for d in detail:
        if d["total_clicks"] < 10:
            hard_lower = d["bid_bucket"]
        else:
            break  # 一出现有量的桶就停

    # 2) 观察到的可工作最低价：至少一半关键词拿到 5+ 点击的最低桶
    observed_min = None
    for d in detail:
        if d["keywords"] >= 2 and d["share_kw_with_5plus_clicks"] >= 0.5:
            observed_min = d["bid_bucket"]
            break

    total_kw = len({r["keyword_id"] for r in rows})
    confidence = "high" if total_kw >= 50 else ("medium" if total_kw >= 20 else "low")

    return {
        # 对外统一用 floor 字段，取「观察到的可工作最低价」作为保守下界。
        # **口径是有效出价**（bid × (1+溢价)），不是名义 bid。
        "floor": observed_min,
        "basis": "effective_bid",
        "hard_lower_bound": hard_lower,
        "observed_working_min": observed_min,
        "estimable": False,
        "confidence": confidence,
        "keywords_sampled": total_kw,
        "buckets": detail,
        "calculation": [{
            "step": "bid 地板下界",
            "formula": ("effective_bid = bid × (1 + 点击加权溢价/100); "
                        "observed_working_min = min(bucket) where keywords>=2 "
                        "and share(clicks>=5)>=0.5"),
            "params": {"start": start, "end": end, "bucket_width": bucket_width,
                       "basis": "effective_bid"},
            "result": {"hard_lower_bound": hard_lower, "observed_working_min": observed_min,
                       "keywords_sampled": total_kw},
            "note": ("分母是**有效出价**不是名义 bid —— 按名义分桶时 $0.40 那桶有 221 击，"
                     "但那些词的溢价是 142%/280%/321%，没有任何一个真的用 $0.40 买过点击。"
                     "拿它当地板会误判「已在地板上」。另外 bid 与出量仍被关键词热度混淆"
                     "（有效 $0.80 的 26 个词只有 8% 拿到 5+ 击是需求问题不是价格问题），"
                     "所以这里只给下界不给精确地板；精确地板必须靠单变量实验"),
        }],
    }


# ---------------------------------------------------------------- 止损线（按钱，不按击）

def cost_ceiling(conn: sqlite3.Connection, start: str, end: str,
                 aov: float, target_acos: float,
                 multiple: float = 3.0) -> dict:
    """零成交关键词的**成本上限**判定 —— 这条线不问「显著吗」，只问「花了多少钱」。

    为什么需要它（这不是「把统计门槛降低」，是另一条完全不同的问题）：

    平台的判定线是统计学问题：「这个词的转化率是否真的低于基线」。在日均 19 击、
    14 天滚动窗口的账户上，0 单需要 321 击、1 单需要 597 击，而窗口内最大的词只
    257–314 击 —— **统计学上永远给不出答案**，因为滚走的天数和进来的一样多。
    由此 `front`（待批）在 85 个 memorial 里一次都没非空过。

    但主人每天面对的是另一个问题：「这个词花掉的钱值不值」。这个问题不需要显著性：
    `lat pulldown` 在 2026-11-03~11-16 花了 $226.43、257 击、0 单；按客单价 $191 与
    目标 ACOS 30%，一单允许花 $57.30 —— **已经花掉 3.95 单的钱，一单没有**。
    而且诚实地说：即使它的转化率和全店一样（0.5013%），257 击出 0 单的概率是 27.6%，
    统计上证明不了它更差。所以这条规则**不给「它更差」的结论**，只给「钱已经花掉
    3.95 单」这个事实，处置方式交给主人（停 / 否词 / 换匹配方式 / 继续观察）。

    参数 `multiple` 是「花掉几单的钱才值得中断」的倍数。取 3.0 的理由：3 单的钱
    在 `0 单` 情形下，若真实转化率等于基线，出现概率约 e^-3 ≈ 5% —— 与统计判定线
    同一个量级的谨慎度，但它用**钱**表达，所以在这类小额账户上仍然够得着。

    返回：
        {"multiple": 3.0, "unit_cost": 57.30, "threshold": 171.90,
         "spend_total": ..., "keywords": [...], "note": ...}
    每个 keyword 带 `units`（花掉几单的钱）与 `still_open`（按这个花费速度，
    再跑 14 天预计还要花多少）。
    """
    unit = (aov or 0) * (target_acos or 0)
    if unit <= 0:
        return {"multiple": multiple, "unit_cost": None, "threshold": None,
                "spend_total": 0.0, "keywords": [],
                "note": "客单价或目标 ACOS 缺失，算不出「一单允许花多少」，这条线不启用"}

    threshold = unit * multiple
    rows = _rows(conn, """
        select keyword_id, keyword_text, campaign_id, ad_group_id,
               sum(clicks) clicks, sum(cost) cost, sum(ads_orders) orders,
               sum(ads_sales) sales,
               round(sum(cost) / nullif(sum(clicks), 0), 4) cpc,
               count(distinct report_date) days
        from fact_keyword_daily
        where report_date between ? and ?
        group by keyword_id, keyword_text, campaign_id, ad_group_id
        having sum(ads_orders) = 0
           and sum(cost) >= ?
           and max(bid) > 0
        order by sum(cost) desc
    """, (start, end, threshold))

    out = []
    for r in rows:
        cost = r["cost"] or 0.0
        clicks = r["clicks"] or 0
        days = r["days"] or 1
        per_day = cost / days if days else cost
        # 台账上要记「后台那一格现在是什么」，所以取窗口内**最后一天**的 bid，
        # 不是 max(bid)：这个词 11-13 还是 1.0、11-14 已改成 0.8，记 1.0 会让人
        # 以为没改过。诊断 agent 第 177 号反对：「这行台账的 current_value
        # 记的不是配置」—— 记 bid 才对得上后台，CPC 只是它的衍生指标。
        b = conn.execute(
            """select bid from fact_keyword_daily
                where keyword_id=? and campaign_id=? and ad_group_id=?
                  and report_date between ? and ? and bid > 0
                order by report_date desc limit 1""",
            (r["keyword_id"], r["campaign_id"], r["ad_group_id"], start, end),
        ).fetchone()
        out.append({
            "keyword_id": r["keyword_id"],
            "keyword_text": r["keyword_text"],
            "campaign_id": r["campaign_id"],
            "ad_group_id": r["ad_group_id"],
            "clicks": clicks,
            "cost": _r(cost, 2),
            "orders": 0,
            "bid": b["bid"] if b else None,
            "cpc": r["cpc"],
            "days": days,
            # 花掉几单的钱 —— 主人最直观的那个数
            "units": _r(cost / unit, 2),
            # 按当前速度再跑一整个窗口要再花多少
            "still_open": _r(per_day * 14, 2),
        })

    return {
        "multiple": multiple,
        "unit_cost": _r(unit, 2),
        "threshold": _r(threshold, 2),
        "spend_total": _r(sum(k["cost"] for k in out), 2),
        "keywords": out,
        "note": ("这条线判的是**钱**不是**显著性**：花掉 {} 单的钱、一单没有就够格。"
                 "它不下「转化更差」的结论 —— 在小额账户上那需要 321–597 击，"
                 "14 天窗口永远到不了。所以处置权完全在主人：停、否词、换匹配方式，"
                 "或者认为这是冷启动期该付的学费继续观察。"
                 ).format(multiple),
    }


# ---------------------------------------------------------------- 位次

def placement_max_adjustment(conn: sqlite3.Connection, start: str, end: str) -> dict[str, float]:
    """每个活动在本窗口内用过的**最大** bid_adjustment（百分比）。

    用途只有一个：算「这个活动的位次溢价上限 = 出价 × (1 + adj/100)」，
    让工作台能判断实际 CPC 是否贴到/超过上限。

    为什么不信活动名：实测 `L3-基础bid0.4 溢价1.4 top-lat pulldown-fix`
    真实 bid_adjustment = 350%（不是 40%），`头部200%` 实际 280%。
    名字是人起的，会过期；这里只认表里的数字。

    注意：只取最大 adjustment 而不是加权平均 —— 判「有没有贴上限」要看顶格那档，
    平均会被低溢价位次稀释，得出「远没到上限」的假安全结论。
    """
    rows = _rows(conn, """
        select campaign_id, max(bid_adjustment) adj
        from fact_placement_daily
        where report_date between ? and ?
        group by campaign_id
    """, (start, end))
    out: dict[str, float] = {}
    for r in rows:
        if r["adj"] is None:
            continue
        out[str(r["campaign_id"])] = float(r["adj"])
    return out


def placement_stats(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """位次四档表现。

    位次不是「加价就多拿」——实测 Other 的 ROAS 高于 Top of Search，
    所以任何「无脑加 TOS」的建议都是错的。
    """
    rows = _rows(conn, """
        select placement,
               count(distinct campaign_id) campaigns,
               sum(impressions) imp, sum(clicks) ck,
               sum(cost) cost, sum(ads_orders) od, sum(ads_sales) sales
        from fact_placement_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by placement
        order by cost desc
    """, (start, end))

    total_cost = sum(float(r["cost"] or 0) for r in rows) or 1.0
    out = []
    for r in rows:
        ck = int(r["ck"] or 0)
        cost = float(r["cost"] or 0.0)
        od = int(r["od"] or 0)
        sales = float(r["sales"] or 0.0)
        out.append({
            "placement": r["placement"],
            "campaigns": int(r["campaigns"] or 0),
            "impressions": int(r["imp"] or 0),
            "clicks": ck,
            "cost": round(cost, 2),
            "orders": od,
            "sales": round(sales, 2),
            "cpc": _r(_safe_div(cost, ck), 4),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpa": _r(_safe_div(cost, od), 2),
            "roas": _r(_safe_div(sales, cost), 4),
            "cost_share": _r(cost / total_cost, 4),
        })
    return out


def campaign_placement_stats(conn: sqlite3.Connection, start: str, end: str,
                             min_cost: float = 10.0, min_clicks: int = 20) -> list[dict]:
    """活动 × 位次 的明细。

    为什么必须拆到活动级：位次加价是**每个活动单独设的**，没有「全店 TOS 加价」这个开关。
    只按 placement 汇总得到的「降低商品页加价」根本没法执行 —— 你得告诉人改哪个活动。

    实测本账户的位次加价分布（2026-10-31~11-13）：
      TOS 加价从 0% 一路到 900%，900% 出现在 sp-二合一跑步机-自动（$319 花费 ACOS 91%）
      和 auto（$48 花费 ACOS 11%）—— 两个都在赚钱，但一个亏一个赚 8 倍。
      这就是「加价高低」本身没有意义的证据：同一个 900%，结果差 8 倍。
    """
    rows = _rows(conn, """
        select p.campaign_id, p.placement,
               max(p.bid_adjustment) adj, max(p.bid_adjustment_name) adj_name,
               max(p.bid_strategy_name) strategy,
               count(distinct p.report_date) days,
               sum(p.impressions) imp, sum(p.clicks) ck,
               sum(p.cost) cost, sum(p.ads_orders) od, sum(p.ads_sales) sales
        from fact_placement_daily p
        where p.report_date between ? and ?
          and p.campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by p.campaign_id, p.placement
        having sum(p.cost) >= ? and sum(p.clicks) >= ?
        order by sum(p.cost) desc
    """, (start, end, min_cost, min_clicks))

    names = {r["campaign_id"]: r["campaign_name"] for r in _rows(conn, """
        select campaign_id, max(campaign_name) campaign_name
        from fact_campaign_daily where report_date between ? and ? and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉')) group by campaign_id
    """, (start, end))}
    budgets = {r["campaign_id"]: float(r["daily_budget"] or 0) for r in _rows(conn, """
        select campaign_id, max(daily_budget) daily_budget
        from fact_campaign_daily where report_date between ? and ? and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉')) group by campaign_id
    """, (start, end))}

    # 活动自身总表现 —— 位次好坏的参照系是「同一个活动内部」，不是全店
    camp = {r["campaign_id"]: r for r in _rows(conn, """
        select campaign_id, sum(cost) cost, sum(clicks) ck,
               sum(ads_orders) od, sum(ads_sales) sales
        from fact_campaign_daily where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by campaign_id
    """, (start, end))}

    out = []
    for r in rows:
        cid = r["campaign_id"]
        ck = int(r["ck"] or 0)
        cost = float(r["cost"] or 0.0)
        od = int(r["od"] or 0)
        sales = float(r["sales"] or 0.0)
        c = camp.get(cid)
        camp_cost = float(c["cost"] or 0) if c else 0.0
        camp_sales = float(c["sales"] or 0) if c else 0.0
        camp_od = int(c["od"] or 0) if c else 0
        adj = r["adj"]
        out.append({
            "campaign_id": cid,
            "campaign_name": names.get(cid, cid),
            "daily_budget": budgets.get(cid, 0.0),
            "placement": r["placement"],
            "bid_adjustment": None if adj is None else float(adj),
            "bid_adjustment_name": r["adj_name"],
            "bid_strategy": r["strategy"],
            "days": int(r["days"] or 0),
            "impressions": int(r["imp"] or 0),
            "clicks": ck,
            "cost": round(cost, 2),
            "orders": od,
            "sales": round(sales, 2),
            "cpc": _r(_safe_div(cost, ck), 4),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpa": _r(_safe_div(cost, od), 2),
            "roas": _r(_safe_div(sales, cost), 4),
            "campaign_cost": round(camp_cost, 2),
            "campaign_sales": round(camp_sales, 2),
            "campaign_orders": camp_od,
            "campaign_roas": _r(_safe_div(camp_sales, camp_cost), 4),
            "campaign_cpa": _r(_safe_div(camp_cost, camp_od), 2),
            "cost_share_of_campaign": _r(_safe_div(cost, camp_cost), 4),
        })
    return out


# ---------------------------------------------------------------- 关键词 / 搜索词 / 活动

def keyword_stats(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = _rows(conn, """
        select market_id, keyword_id, keyword_text, match_type, ad_group_id, campaign_id,
               max(bid) bid, max(serving_status) serving_status,
               sum(impressions) imp, sum(clicks) ck, sum(cost) cost,
               sum(ads_orders) od, sum(ads_sales) sales
        from fact_keyword_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by market_id, keyword_id
        order by cost desc
    """, (start, end))

    out = []
    for r in rows:
        ck = int(r["ck"] or 0)
        cost = float(r["cost"] or 0.0)
        od = int(r["od"] or 0)
        sales = float(r["sales"] or 0.0)
        out.append({
            "market_id": r["market_id"],
            "keyword_id": r["keyword_id"],
            "keyword_text": r["keyword_text"],
            "match_type": r["match_type"],
            "ad_group_id": r["ad_group_id"],
            "campaign_id": r["campaign_id"],
            "bid": float(r["bid"] or 0),
            "serving_status": r["serving_status"],
            "impressions": int(r["imp"] or 0),
            "clicks": ck,
            "cost": round(cost, 2),
            "orders": od,
            "sales": round(sales, 2),
            "cpc": _r(_safe_div(cost, ck), 4),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpa": _r(_safe_div(cost, od), 2),
            "acos": _r(_safe_div(cost, sales), 4),
        })
    return out


def searchterm_stats(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = _rows(conn, """
        select market_id, search_term, campaign_id, ad_group_id, keyword_id, keyword_text, match_type,
               sum(impressions) imp, sum(clicks) ck, sum(cost) cost,
               sum(ads_orders) od, sum(ads_sales) sales
        from fact_search_term_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by market_id, search_term, campaign_id, ad_group_id
        order by cost desc
    """, (start, end))

    out = []
    for r in rows:
        ck = int(r["ck"] or 0)
        cost = float(r["cost"] or 0.0)
        od = int(r["od"] or 0)
        sales = float(r["sales"] or 0.0)
        out.append({
            "market_id": r["market_id"],
            "search_term": r["search_term"],
            "campaign_id": r["campaign_id"],
            "ad_group_id": r["ad_group_id"],
            "keyword_id": r["keyword_id"],
            "keyword_text": r["keyword_text"],
            "match_type": r["match_type"],
            "impressions": int(r["imp"] or 0),
            "clicks": ck,
            "cost": round(cost, 2),
            "orders": od,
            "sales": round(sales, 2),
            "cpc": _r(_safe_div(cost, ck), 4),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpa": _r(_safe_div(cost, od), 2),
            "acos": _r(_safe_div(cost, sales), 4),
        })
    return out


def campaign_stats(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = _rows(conn, """
        select market_id, campaign_id, campaign_name, portfolio_name, targeting_type,
               max(campaign_state) state, max(daily_budget) daily_budget,
               sum(impressions) imp, sum(clicks) ck, sum(cost) cost,
               sum(ads_orders) od, sum(ads_sales) sales
        from fact_campaign_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
        group by market_id, campaign_id
        order by cost desc
    """, (start, end))

    out = []
    for r in rows:
        ck = int(r["ck"] or 0)
        cost = float(r["cost"] or 0.0)
        od = int(r["od"] or 0)
        sales = float(r["sales"] or 0.0)
        budget = float(r["daily_budget"] or 0.0)
        # 预算使用率：用窗口内日均花费 / 日预算
        days = _count_days(conn, start, end)
        avg_cost = cost / days if days else 0
        out.append({
            "market_id": r["market_id"],
            "campaign_id": r["campaign_id"],
            "campaign_name": r["campaign_name"],
            "portfolio_name": r["portfolio_name"],
            "targeting_type": r["targeting_type"],
            # 实测：targeting_type 0=手动（有具体关键词） 1=自动（无关键词，只有投放组）
            # 存的是 TEXT，不能和 int 直接比
            "is_auto": str(r["targeting_type"]) == "1",
            # 实测：campaign_state 0=在跑 1=已暂停
            "state": {"0": "ENABLED", "1": "PAUSED"}.get(str(r["state"]), str(r["state"])),
            "daily_budget": budget,
            "avg_daily_cost": round(avg_cost, 2),
            "budget_util": _r(avg_cost / budget, 4) if budget else None,
            "impressions": int(r["imp"] or 0),
            "clicks": ck,
            "cost": round(cost, 2),
            "orders": od,
            "sales": round(sales, 2),
            "cpc": _r(_safe_div(cost, ck), 4),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpa": _r(_safe_div(cost, od), 2),
            "roas": _r(_safe_div(sales, cost), 4),
            "acos": _r(_safe_div(cost, sales), 4),
        })
    return out


# ---------------------------------------------------------------- 分层证据（P1.0）

def _gammainc_upper_reg(a: float, x: float) -> float:
    """正则化上不完全伽马 Q(a, x)，用于卡方生存函数。

    Numerical Recipes 的 gser/gcf 两分支实现，不用 scipy。
    """
    if x <= 0:
        return 1.0
    if x < a + 1.0:
        # 级数展开求 P(a,x)，再用 1-P 得 Q
        ap = a
        total = 1.0 / a
        delta = total
        for _ in range(500):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-12:
                break
        return 1.0 - total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    # 连分式直接求 Q(a,x)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-12:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def _chi2_sf(chi2: float, df: int) -> float:
    """卡方分布的生存函数 P(X > chi2)。"""
    if df <= 0:
        return 1.0
    try:
        return _gammainc_upper_reg(df / 2.0, chi2 / 2.0)
    except (ValueError, OverflowError):
        return 1.0


def detectable_ratio(clicks: int, orders: int, groups: int,
                     alpha: float = 0.05) -> dict | None:
    """这个样本量**能**检出多大的差异？—— 用来区分「没差异」和「检不出」。

    诊断 agent 第 148 号问题（Q D1）：
    「词层、活动层、位次层、匹配方式层全判『无差异』，但整层只有 6 单；
     portfolio 层却显著。这个『无差异』我该当结论用，还是当证据不足？」

    **是证据不足。** `homogeneity()` 返回 `p > 0.05` 只说明「没能在随机波动中辨认出差异」，
    不说明「差异不存在」。要分清这两件事，得知道当前样本量能检出多大的效应。

    做法：假设真实差异存在（一组 CVR 是其余的 `r` 倍），用二项分布算「至少出现观测到的
    订单数」的概率，找最小的 `r` 使该概率 < alpha。这就是本样本量的可检出下限。

    返回 {detectable_ratio, note}；样本太小时 `detectable_ratio` 会很大（甚至 None=检不出任何差异）。
    """
    if clicks <= 0 or orders <= 0 or groups < 2:
        return None
    p0 = orders / clicks
    # 逐档试 r，从 1.2 倍到 100 倍；每组等样本假设（保守：实际更不均匀，检出更难）
    for r_int in range(12, 1001):
        r = r_int / 10.0
        p1 = min(p0 * r, 0.999)
        # 一组抬高、其余保持 p0；抬高那组的期望订单数
        c1 = clicks / groups
        expect1 = c1 * p1
        rest = clicks - c1
        expect_rest = rest * p0
        # 抬高组观测到 >= ceil(expect1) 单的概率（用 Poisson 近似，订单稀疏）
        import math as _m
        k = int(_m.ceil(expect1))
        if k <= 0:
            continue
        # P(X >= k | lambda=expect_rest_per_group_scaled) —— 用整层 lambda 比较
        lam = clicks * p0  # 全层期望订单数
        # 抬高组贡献 expect1，其余 expect_rest；若差异为真，全层期望 = expect1 + expect_rest
        # 在 H0（全层同率）下，抬高组期望 = c1 * p0
        lam0 = c1 * p0
        # P(X >= k | lam0) 小 → 这个差异可被辨认
        prob = 1.0 - sum(_m.exp(-lam0) * lam0 ** i / _m.factorial(i)
                         for i in range(k))
        if prob < alpha:
            return {
                "detectable_ratio": round(r, 1),
                "basis": "一组 CVR 是其余的 r 倍时可被检出（alpha=0.05，等样本假设）",
                "clicks": clicks, "orders": orders, "groups": groups,
                "note": (f"这一层 {clicks} 击 / {orders} 单 / {groups} 组，"
                         f"只能检出「某组转化率约为其余 {r:.1f} 倍」这种量级的差异；"
                         f"比这更小的真实差异，p>0.05 是**检不出**，不是**不存在**"),
            }
    return {
        "detectable_ratio": None,
        "basis": "样本太小，任何量级的差异都检不出",
        "clicks": clicks, "orders": orders, "groups": groups,
        "note": f"这一层 {clicks} 击 / {orders} 单 / {groups} 组，样本小到连 100 倍差异都检不出",
    }


def homogeneity(rows: list[tuple[int, int]]) -> dict | None:
    """齐性检验：各组转化率是否真的不同。

    rows = [(clicks, orders), ...]
    返回 None 表示样本不足以做检验（无点击或全无单）。

    p < 0.05 说明「组间差异超出随机波动」，即这一层有可辨信号。
    """
    pairs = [(int(c or 0), int(o or 0)) for c, o in rows]
    n = sum(c for c, _ in pairs)
    k = sum(o for _, o in pairs)
    if n <= 0 or k <= 0 or len(pairs) < 2:
        return None
    p0 = k / n
    if p0 >= 1.0 or p0 <= 0.0:
        return None
    chi2 = 0.0
    for c, o in pairs:
        if c > 0:
            chi2 += (o - c * p0) ** 2 / (c * p0 * (1.0 - p0))
    df = len(pairs) - 1
    p = _chi2_sf(chi2, df)
    return {
        "groups": len(pairs),
        "clicks": n,
        "orders": k,
        "cvr": round(p0, 6),
        "chi2": round(chi2, 2),
        "df": df,
        "p_value": round(p, 4),
        "significant": p < 0.05,
    }


def _safe_layer_evidence(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """学习层失败不该拖垮引擎：算不出证据时返回空列表，报告退化为不显示该区块。"""
    try:
        return layer_evidence(conn, start, end)
    except Exception as e:  # noqa: BLE001 —— 故意兜住一切，证据缺失不能阻断出报告
        return [{"layer": "error", "label": "分层证据", "scoped": False,
                 "table": "", "stats": None, "error": f"{type(e).__name__}: {e}"}]


def layer_evidence(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """逐层跑齐性检验，回答「哪一层才真的有可辨差异」。

    这是 P1.0 的核心：报告不再靠「今天 0 条建议」表达无证据，
    而是明说「词层没有可辨差异，组合层有」。

    只读，不改库。
    """
    scope_where = scope.campaign_predicate()
    specs = [
        # (层级键, 中文名, 表, 分组列, 是否受 scope 限制)
        ("keyword_text", "关键词层", "fact_keyword_daily", "keyword_text", True),
        ("campaign", "活动层", "fact_campaign_daily", "campaign_id", True),
        ("placement", "位次层", "fact_placement_daily", "placement", True),
        ("match_type", "匹配方式层", "fact_keyword_daily", "match_type", True),
        ("portfolio", "投放组合层", "fact_campaign_daily", "portfolio_name", False),
    ]
    out: list[dict] = []
    # 分母基准：活动层（scope 内）的总点击数。各层的点击数天生不同 ——
    # 关键词层只覆盖手动投放的活动，自动/ASIN 定向那些活动没有关键词行；
    # 位次层的表本来就可能和活动层差几个击（聚合取整）。
    # 不把覆盖率写出来的话，读报告的人（和诊断 agent）会一直问
    # 「816 击和 1197 击是不是不在同一批数据上算的」。
    base = _one(conn, f"select sum(clicks) c from fact_campaign_daily "
                      f"where report_date between ? and ? and {scope_where}",
                (start, end))
    base_clicks = int(base.get("c") or 0)
    for key, label, table, group_col, scoped in specs:
        where = f"where report_date between ? and ?"
        if scoped:
            where += f" and {scope_where}"
        sql = (
            f"select sum(clicks) c, sum(ads_orders) o from {table} {where} "
            f"group by {group_col} having sum(clicks) > 0"
        )
        rows = _rows(conn, sql, (start, end))
        stats = homogeneity([(r["c"], r["o"]) for r in rows])
        covered = int(stats.get("clicks") or 0) if stats else 0
        # 这一层的「检出力」：p>0.05 只说明没辨认出差异，不说明差异不存在。
        # 诊断 agent 第 148 号问题（Q D1）就是问这个 —— 不给它，agent 会把
        # 「无差异」当结论用，而实际只是样本不够。
        power = (detectable_ratio(int(stats["clicks"]), int(stats["orders"]),
                                  int(stats["groups"])) if stats else None)
        out.append({
            "layer": key,
            "label": label,
            "scoped": scoped,
            "table": table,
            "stats": stats,
            "detectable": power,
            # 这一层实际覆盖了多少击、占活动层的多少 —— 各层分母不同的原因
            "covered_clicks": covered,
            "base_clicks": base_clicks,
            "coverage": (covered / base_clicks) if base_clicks else None,
        })
    return out


def _count_days(conn: sqlite3.Connection, start: str, end: str) -> int:
    r = _one(conn, """
        select count(distinct report_date) n from fact_campaign_daily
        where report_date between ? and ?
          and campaign_id IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily WHERE portfolio_name IN ('JL-高位下拉'))
    """, (start, end))
    return int(r.get("n") or 1)


# ---------------------------------------------------------------- 卡位（R31）

RANK_SNAPSHOT_MAX_AGE_HOURS = 6   # 排名观测超过这么久，就不许再据此加价


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def rank_targets(conn: sqlite3.Connection) -> list[dict]:
    """在读的卡位配置。表不存在（未迁移的老库）时返回空，不阻断引擎。"""
    try:
        rows = _rows(conn, """
            select target_id, market_id, keyword_id, keyword_text, campaign_id, ad_group_id, asin,
                   goal_page, goal_position, push_mode, max_bid, max_tos_pct,
                   step_mode, step_value, success_streak_required,
                   decrease_mode, decrease_value, restore_on_end,
                   acos_guard_mult, max_pushes, created_at, expires_at
            from rank_target
            where status = 'active'
            order by target_id
        """)
    except sqlite3.OperationalError:
        return []

    out = []
    for r in rows:
        out.append({
            "target_id": r["target_id"],
            "market_id": r["market_id"],
            "keyword_id": r["keyword_id"],
            "keyword_text": r["keyword_text"],
            "campaign_id": r["campaign_id"],
            "ad_group_id": r["ad_group_id"],
            "asin": (r["asin"] or "").strip() or None,
            "goal_page": int(r["goal_page"] or 1),
            "goal_position": int(r["goal_position"] or 4),
            "push_mode": (r["push_mode"] or "bid").lower(),
            "max_bid": float(r["max_bid"]) if r["max_bid"] is not None else None,
            "max_tos_pct": float(r["max_tos_pct"]) if r["max_tos_pct"] is not None else 900.0,
            "step_mode": (r["step_mode"] or "fixed").lower(),
            "step_value": float(r["step_value"] or 0.0),
            "success_streak_required": int(r["success_streak_required"] or 5),
            "decrease_mode": ((r["decrease_mode"] or "").lower() or None),
            "decrease_value": float(r["decrease_value"]) if r["decrease_value"] is not None else None,
            "restore_on_end": bool(r["restore_on_end"]),
            "acos_guard_mult": float(r["acos_guard_mult"] or 1.5),
            "max_pushes": int(r["max_pushes"] or 6),
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        })
    return out


def keyword_portfolio_map(conn: sqlite3.Connection, keyword_ids: list[str]) -> dict[str, str]:
    """这批关键词实际落在哪个投放组合。查不到就返回空 dict（不猜）。

    存在的理由见 Facts.keyword_portfolio 的注释：R31 要把「没数据」拆成两句话。
    """
    ids = [str(k) for k in keyword_ids if k]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    try:
        rows = _rows(conn, f"""
            select f.keyword_id,
                   coalesce(nullif(trim(max(c.portfolio_name)), ''), '活动未归投放组合') portfolio
            from fact_keyword_daily f
            join fact_campaign_daily c
              on c.campaign_id = f.campaign_id and c.report_date = f.report_date
            where f.keyword_id in ({marks})
            group by f.keyword_id
        """, tuple(ids))
    except sqlite3.OperationalError:
        return {}
    return {str(r["keyword_id"]): r["portfolio"] for r in rows if r["portfolio"]}


def rank_tos_pct(conn: sqlite3.Connection, campaign_ids: list[str],
                 start: str, end: str) -> dict[str, float]:
    """卡位专用的 TOS 加价读取：只问「这个活动现在 TOS 加价多少」，不问它花了多少钱。

    为什么不复用 campaign_placement_stats：那个函数有 min_cost / min_clicks 门槛，
    是为了「位次值不值得投」的分析场景设计的。卡位场景不同 —— 卡位只关心一个活动的一个位次，
    一个刚开 3 天、花费还没到线的活动，TOS 加价照样是既有配置，必须能读到。
    实测：campaign 46818486452618 窗口内 TOS=150% 连续 13 天，但因 TOS 位点击不足 20
    被 campaign_placement_stats 过滤掉，导致卡位读不到配置而放弃 —— 这是假缺失。

    取窗口内最后一个有数据的日期的值（配置会变，要当前生效的那个）。
    """
    if not campaign_ids:
        return {}
    ph = ",".join("?" * len(campaign_ids))
    rows = _rows(conn, f"""
        select campaign_id, bid_adjustment, max(report_date) d
        from fact_placement_daily
        where placement = 'Top of Search on-Amazon'
          and report_date between ? and ?
          and campaign_id in ({ph})
        group by campaign_id, bid_adjustment
        order by campaign_id, d asc
    """, (start, end, *campaign_ids))
    out: dict[str, float] = {}
    for r in rows:
        if r["bid_adjustment"] is None:
            continue
        out[str(r["campaign_id"])] = float(r["bid_adjustment"])
    return out


def _rows_for(rows: list[dict], kid: str) -> list[dict]:
    return [r for r in rows if r["keyword_id"] == kid]


def rank_state(conn: sqlite3.Connection, targets: list[dict],
               max_age_hours: float = RANK_SNAPSHOT_MAX_AGE_HOURS) -> dict[str, dict]:
    """每个卡位关键词的排名现状：自己的位次、是否到位、连续到位批次、是否自我竞争、已加价次数。

    为什么按「批次」而不是按「行」：插件一次抓取会把前 N 位逐个 ASIN 写进来，
    同一个 observed_at 下有多行（第 1 位 A、第 2 位 B、第 5 位自己）。
    如果只取第一行，拿到的是「排在第一位的那个人」，不是自己 —— 会把自己的排名判成别人的，
    也会把自己的另一条记录误判成自我竞争。所以：
      · 批次 = (keyword_id, observed_at)
      · 自己的位次 = 批次里 asin == 卡位对象 asin 的那一行；没有这行 = 本次抓取范围内没看到自己
      · 自我竞争 = 最新批次里，目标位区间内出现了**另一个**自家 ASIN（is_self=1 且 asin 不等于卡位对象）
      · 连续到位 = 从最新批次往前，自己连续在目标位区间内的批次数

    只读广告位（sponsored）观测；自然位仅作对照，不参与判定。
    数据来自 rank_snapshot（插件/上游写入），引擎自己不抓排名。
    """
    if not targets:
        return {}

    ids = [t["keyword_id"] for t in targets]
    ph = ",".join("?" * len(ids))
    try:
        rows = _rows(conn, f"""
            select keyword_id, observed_at, page, position, asin, is_self, postal_code, source
            from rank_snapshot
            where placement = 'sponsored' and keyword_id in ({ph})
            order by observed_at desc, position asc
        """, tuple(ids))
    except sqlite3.OperationalError:
        return {}

    # 按 (keyword_id, observed_at) 分批；同一批内按位次升序
    batches: dict[str, list[tuple[str, list[dict]]]] = {}
    seen: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        kid = r["keyword_id"]
        seen.setdefault(kid, {}).setdefault(str(r["observed_at"]), []).append(r)
    for kid, by_ts in seen.items():
        batches[kid] = sorted(by_ts.items(), key=lambda kv: kv[0], reverse=True)

    # 已加价次数：每个 R31 加价建议算一次。这是「次数上限」的唯一依据。
    pushes: dict[str, int] = {}
    try:
        for r in _rows(conn, """
            select entity_id, count(*) n from decision_log
            where rule_id = 'R31' and action_type = 'rank_push'
              and status in ('proposed','approved','executed')
            group by entity_id
        """):
            pushes[str(r["entity_id"])] = int(r["n"] or 0)
    except sqlite3.OperationalError:
        pushes = {}

    now = datetime.now()
    out: dict[str, dict] = {}

    for t in targets:
        kid = t["keyword_id"]
        want_asin = (t.get("asin") or "").strip() or None
        bl = batches.get(kid, [])

        def in_goal(page, position) -> bool:
            return ((page or 99) <= t["goal_page"]
                    and (position or 999) <= t["goal_position"])

        if not bl:
            out[kid] = {
                "observations": 0, "batches": 0, "latest": None, "own": None,
                "hit": False, "streak": 0, "stale": True, "self_competing": False,
                "blocker": None, "pushes_used": pushes.get(kid, 0),
            }
            continue

        latest_ts, latest_rows = bl[0]
        latest_at = _parse_ts(latest_ts)
        age_h = round((now - latest_at).total_seconds() / 3600, 2) if latest_at else None
        stale = age_h is None or age_h > max_age_hours

        def own_row(batch_rows: list[dict]) -> dict | None:
            if want_asin:
                for r in batch_rows:
                    if (r["asin"] or "").strip() == want_asin:
                        return r
                return None
            # 没配 asin 时退回 is_self（兼容手工录入）
            for r in batch_rows:
                if r["is_self"]:
                    return r
            return None

        def other_self_in_goal(batch_rows: list[dict]) -> dict | None:
            for r in batch_rows:
                if not r["is_self"]:
                    continue
                a = (r["asin"] or "").strip()
                if want_asin and a == want_asin:
                    continue          # 自己
                if in_goal(r["page"], r["position"]):
                    return r
            return None

        latest_own = own_row(latest_rows)
        hit = bool(latest_own and in_goal(latest_own["page"], latest_own["position"]))

        streak = 0
        for _ts, brows in bl:
            o = own_row(brows)
            if o and in_goal(o["page"], o["position"]):
                streak += 1
            else:
                break

        # 自己没到位、而目标位上是自家另一个 ASIN → 再加价只是自己打自己
        blocker = None if hit else other_self_in_goal(latest_rows)
        self_competing = blocker is not None

        def project(r: dict | None) -> dict | None:
            if not r:
                return None
            return {
                "observed_at": r["observed_at"],
                "page": r["page"],
                "position": r["position"],
                "asin": r["asin"],
                "is_self": bool(r["is_self"]),
                "postal_code": r["postal_code"],
                "source": r["source"],
            }

        out[kid] = {
            "observations": len(_rows_for(rows, kid)),
            "batches": len(bl),
            "latest": {
                "observed_at": latest_ts,
                "page": latest_own["page"] if latest_own else None,
                "position": latest_own["position"] if latest_own else None,
                "asin": latest_own["asin"] if latest_own else want_asin,
                "is_self": True if latest_own else False,
                "postal_code": latest_own["postal_code"] if latest_own else None,
                "source": latest_own["source"] if latest_own else None,
            },
            "own": project(latest_own),
            "blocker": project(blocker),
            "age_hours": age_h,
            "stale": stale,
            "hit": hit,
            "streak": streak,
            "self_competing": self_competing,
            "pushes_used": pushes.get(kid, 0),
        }
    return out


# ---------------------------------------------------------------- 上下文打包

@dataclass
class Facts:
    """一次运行所需的全部事实，一次算完传下去，避免每条规则重复查库。"""
    start: str
    end: str
    window_days: int
    target_acos: float
    account: dict
    cvr_baseline: dict
    breakeven: dict
    min_clicks: dict
    cpc_bias: dict
    floor: dict
    placements: list[dict]
    campaign_placements: list[dict]
    keywords: list[dict]
    searchterms: list[dict]
    campaigns: list[dict]
    # 加预算步长（倍数）。默认 1.2 = 一次 +20%，可被 param_override(max_budget_step) 覆盖。
    budget_step: float = DEFAULT_BUDGET_STEP
    rank_targets: list[dict] = field(default_factory=list)
    rank_state: dict[str, dict] = field(default_factory=dict)
    # keyword_id -> 该词实际所属投放组合（只在「本窗口没有它的数据」时才查，用来区分
    # 「这个词没投过」和「这个词在投但不归本次作业范围」）。两句话读起来差不多，
    # 结论相反：前者是数据缺口，后者是范围设定。
    keyword_portfolio: dict[str, str] = field(default_factory=dict)
    rank_tos: dict[str, float] = field(default_factory=dict)   # campaign_id -> TOS 加价%
    layer_evidence: list[dict] = field(default_factory=list)   # 逐层齐性检验（P1.0）
    # 全店口径（不限投放组合）。与 account 的区别：account 是引擎作业范围（本组合），
    # 这个才是「全店」。R30 要说「全店目标不可达」就必须用这个数，不能拿 account 冒充。
    account_wide: dict = field(default_factory=dict)
    # 全店剔掉本组合之后的部分。诊断 agent #128：全店口径的「不可达」有大半是
    # 本组合拖出来的 —— 只看全店会让人以为整个账户都在亏，看这个才知道靶心在哪。
    account_ex_scope: dict = field(default_factory=dict)
    breakeven_wide: dict = field(default_factory=dict)
    # rule_id -> {days_seen, first_day, last_day, days}，按 distinct 日期算。
    # 给「这条告警是不是已经重复了很多天」提供事实依据，别让规则自己猜、也别让 agent 猜。
    alert_history: dict = field(default_factory=dict)
    # campaign_id -> 窗口内最大 bid_adjustment(%)，用于算位次溢价上限
    placement_max_adjustment: dict[str, float] = field(default_factory=dict)
    # 零成交关键词的「花掉几单的钱」判定（R32）。和 min_clicks 是两个不同的问题：
    # 后者问「显著吗」，前者问「花了多少」。85 个 memorial 的 front 恒 0 就是因为
    # 只有前者在起作用，而前者在这个账户规模上永远够不着。
    cost_ceiling: dict = field(default_factory=dict)
    # engine/record.py 算出的规则战绩与对象历史。由 run.py 填，不在这里算 ——
    # facts 的职责是「这次窗口的数据」，战绩是「跨运行的历史」，两件事。
    record: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    scope_label: str = ""

    def keyword_seen_elsewhere(self, keyword_id: str) -> str | None:
        """这个词在投、但不归本次作业范围时返回它所属的投放组合，否则 None。"""
        return (self.keyword_portfolio or {}).get(str(keyword_id))

    def rank_owned_keywords(self) -> set[str]:
        """被卡位接管的关键词 —— R01/R03 必须让路，否则一天内互相打脸。"""
        return {t["keyword_id"] for t in self.rank_targets}

    def stop_loss_keywords(self) -> set[str]:
        """撞上止损线（R32）的关键词 —— R01/R03 必须让路。

        为什么让路：同一个词同时给「停下来」和「降 20% bid」两条建议，
        主人只会挑便宜的那条，而便宜的那条（降 bid）在这类词上不解决问题 ——
        钱是按点击花的，降 20% 只是把同样的亏慢一点亏完。
        """
        return {k["keyword_id"] for k in (self.cost_ceiling or {}).get("keywords", [])}

    @property
    def account_baseline(self) -> dict:
        """「账户」这个基线到底是谁 —— 全店口径，不是本组合。

        为什么要有这个 property：`self.account` 是**引擎作业范围（本组合）**，
        但 R01 的核心词保护、R08 的预算、R09 的位次判断都写着「优于账户」。
        拿本组合的数去比，名字叫账户、数其实是组合 —— 与 R30 那次同样的病。
        实测后果：本组合 CPA $163.81，全店 CPA $57.17（差 2.9 倍），
        拿 163.81 当门槛，任何 CPA < $114.67 的词都算「优于账户」，
        保护等于不保护。

        返回 {} 表示全店口径拿不到（比如没有全店数据），调用方自行 fallback。
        """
        wide = self.account_wide or {}
        if not wide.get("orders"):
            return {}
        return {"label": wide.get("label") or "全店",
                "cpa": wide.get("cpa"), "roas": wide.get("roas"),
                "aov": wide.get("aov"), "cvr": wide.get("cvr"),
                "acos": wide.get("acos")}

    @property
    def target_reachable(self) -> bool:
        """目标 ACOS 是否可达：实际 CVR 必须 ≥ 盈亏平衡 CVR。

        不可达时所有「降 bid」类建议都必须被抑制 ——
        因为 CVR 和 bid 无关，降 bid 只会减少订单，不会提高 CVR。
        """
        be = self.breakeven.get("breakeven_cvr")
        cvr = self.cvr_baseline.get("cvr")
        if be is None or cvr is None:
            return True  # 数据不足时不禁用，交给置信度门控
        return cvr >= be

    def to_summary(self) -> dict:
        return {
            "scope": self.scope_label,
            "window": {"start": self.start, "end": self.end, "days": self.window_days},
            "target_acos": self.target_acos,
            # 口径必须分开写：「scope」是引擎作业范围（本组合），「account」是全店。
            # 合并成一个键就会出现「标着全店、数字是本组合」那种名不副实。
            "scope": self.account,
            "account": self.account_wide,
            "cvr_baseline": self.cvr_baseline.get("cvr"),
            "breakeven_cvr": self.breakeven.get("breakeven_cvr"),
            "breakeven_cvr_wide": (self.breakeven_wide or {}).get("breakeven_cvr"),
            "target_reachable": self.target_reachable,
            "min_clicks": self.min_clicks.get("min_clicks"),
            "cpc_bias": self.cpc_bias.get("overall"),
            "bid_floor": self.floor.get("floor"),
            "bid_floor_confidence": self.floor.get("confidence"),
            "layer_evidence": [
                {
                    "layer": e["layer"],
                    "label": e["label"],
                    "significant": (e["stats"] or {}).get("significant"),
                    "p_value": (e["stats"] or {}).get("p_value"),
                    "groups": (e["stats"] or {}).get("groups"),
                    "clicks": (e["stats"] or {}).get("clicks"),
                    "orders": (e["stats"] or {}).get("orders"),
                }
                for e in self.layer_evidence
            ],
            "counts": {
                "keywords": len(self.keywords),
                "search_terms": len(self.searchterms),
                "campaigns": len(self.campaigns),
                "placements": len(self.placements),
                "campaign_placements": len(self.campaign_placements),
                "rank_targets": len(self.rank_targets),
            },
        }


def build_facts(conn: sqlite3.Connection,
                window_days: int = DEFAULT_WINDOW_DAYS,
                target_acos: float = DEFAULT_TARGET_ACOS,
                exclude_days: int = DEFAULT_ATTR_EXCLUDE_DAYS,
                confidence: float = DEFAULT_CONFIDENCE,
                budget_step: float = DEFAULT_BUDGET_STEP) -> Facts:
    """一次算完全部事实。"""
    start, end = date_window(window_days, exclude_days, conn=conn)

    account = account_funnel(conn, start, end)
    account_wide = account_funnel(conn, start, end, scoped=False)
    cvr_baseline = asin_cvr_baseline(conn, start, end)          # 账户级先做基线
    be = breakeven_cvr(account["cpc"] or 0, account["aov"] or 0, target_acos)
    be_wide = breakeven_cvr(account_wide["cpc"] or 0, account_wide["aov"] or 0, target_acos)

    # 全店 减 本组合 = 其余组合。四个可加的只有 clicks/orders/cost/sales，
    # 比率一律重算，不做相减（比率不可减）。
    def _minus(wide: dict, sc: dict) -> dict:
        if not wide or not sc:
            return {}
        ck = (wide.get("clicks") or 0) - (sc.get("clicks") or 0)
        od = (wide.get("orders") or 0) - (sc.get("orders") or 0)
        cost = (wide.get("cost") or 0.0) - (sc.get("cost") or 0.0)
        sales = (wide.get("sales") or 0.0) - (sc.get("sales") or 0.0)
        if ck <= 0:
            return {}
        out = {
            "label": "全店除本组合外",
            "clicks": ck, "orders": od,
            "cost": round(cost, 2), "sales": round(sales, 2),
            "cvr": _r(_safe_div(od, ck), 6),
            "cpc": _r(_safe_div(cost, ck), 4),
            "aov": _r(_safe_div(sales, od), 2),
        }
        out["breakeven_cvr"] = _r(
            _safe_div(out["cpc"], out["aov"] * target_acos) if out["aov"] else None, 6)
        return out

    ex_scope = _minus(account_wide, account)
    mc = min_clicks(cvr_baseline["cvr"] or 0, confidence)
    camp_adj = campaign_weighted_adjustment(conn, start, end)
    bias = cpc_bias(conn, start, end, campaign_adj=camp_adj)
    floor = bid_floor(conn, start, end, campaign_adj=camp_adj)
    # 止损线用**本组合**客单价，和 R01 算目标 bid 时同一口径 —— 两处若用不同的
    # 「一单值多少」，止损说「花了 6 单的钱」而降价说「目标 $0.32」，主人没法对账。
    cc = cost_ceiling(conn, start, end,
                      aov=account["aov"] or account_wide.get("aov") or 0,
                      target_acos=target_acos)

    r_targets = rank_targets(conn)
    r_state = rank_state(conn, r_targets)
    r_tos = rank_tos_pct(
        conn,
        sorted({t["campaign_id"] for t in r_targets if t.get("campaign_id")}),
        start, end,
    )

    facts = Facts(
        start=start, end=end, window_days=window_days, target_acos=target_acos,
        budget_step=budget_step,
        account=account, cvr_baseline=cvr_baseline, breakeven=be, min_clicks=mc,
        account_wide=account_wide,
        account_ex_scope=ex_scope, breakeven_wide=be_wide,
        cpc_bias=bias, floor=floor, cost_ceiling=cc,
        placements=placement_stats(conn, start, end),
        campaign_placements=campaign_placement_stats(conn, start, end),
        keywords=keyword_stats(conn, start, end),
        searchterms=searchterm_stats(conn, start, end),
        campaigns=campaign_stats(conn, start, end),
        rank_targets=r_targets,
        rank_state=r_state,
        keyword_portfolio=keyword_portfolio_map(conn, [t["keyword_id"] for t in r_targets]),
        rank_tos=r_tos,
        layer_evidence=_safe_layer_evidence(conn, start, end),
        alert_history={"R30": alert_history(conn, "R30")},
        placement_max_adjustment=placement_max_adjustment(conn, start, end),
        scope_label=scope.SCOPE_LABEL,
    )
    facts.notes.append(
        f"范围：{scope.SCOPE_LABEL}，{len(facts.campaigns)} 个活动"
    )

    # 数据质量提示
    if account["orders"] < 30:
        facts.notes.append(f"窗口内仅 {account['orders']} 单，慢层指标（CVR/CPA/ROAS）不可靠")
    if facts.floor.get("confidence") in ("low", "none"):
        facts.notes.append("bid 地板估计置信度低，任何降 bid 建议都必须带地板校验")
    if not facts.target_reachable:
        facts.notes.append(
            f"目标 ACOS {target_acos:.0%} 不可达：实际 CVR {cvr_baseline['cvr']:.2%} "
            f"< 盈亏平衡 {be.get('breakeven_cvr'):.2%}，降 bid 只能省钱不能达标"
        )
    # 卡位数据质量
    if facts.rank_targets:
        stale = [k for k, s in facts.rank_state.items() if s.get("stale")]
        if stale:
            facts.notes.append(
                f"{len(stale)} 个卡位关键词的排名观测超过 {RANK_SNAPSHOT_MAX_AGE_HOURS}h 或缺失，"
                f"本轮不出加价建议（宁可不卡，不瞎卡）"
            )
    return facts
