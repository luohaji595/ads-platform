"""归因 agent —— 到期之后，拿真实数据回看一次，并给出证据等级。

计划书 §8.4 / §4.4 / §9.3 指定的落点就是这里（`agents/attribute.py`）。
一次运行做五件事：

  1. 找出「已点『已改』、归因窗走完、还没回看」的决定
     （decision_log.status='executed' and outcome_filled_at is null）
  2. 算 before / after 两段真实数据，并**同时**算两组对照：
       - account：全店同期 —— 扣掉「整个账户都在变」那一层（旺季、流量、竞品）
       - control：同规则、同层级、±7 天内**没改动**的对象（§8.4 的对照组 SQL）
  3. 算 effect（cpc / cvr / orders 的变化）与判定线（判「有方向」需要多少点击）
  4. 按**写死在代码里**的三档给 evidence_level：strong / medium / weak
  5. 写回 outcome_7d_json / outcome_14d_json + outcome_filled_at，留一条
     agent_eval 断言；**只有 strong/medium 才写 policy 提案**（status='proposed'）

它不做的事
----------
* 不说「这是我改好的」。执行前后对比不是 A/B 测试，所以每条都带对照组；
  **对照组缺席时一律 weak** —— 没有对照就没有归因，这是 §4.4 的原话。
* 不在样本不够的时候给结论：后窗点击不到判定线 → weak + insufficient。
* 不写 status、不改建议、不动广告后台。只填空。
* **flat 不进策略库**：策略要能指导下一次动作，「看不出方向」给不出动作。
* **weak 不进策略库**（§9.3 的硬门）：weak 只写 outcome_*_json 与 agent_eval，
  不写 policy、不写 agent_assumption。

为什么 verdict 不用 §8.4 示例里的 `no_clear_effect`
----------------------------------------------------
`engine/record.py:45-46` 只认 DIRECTIONS=(improved, worsened, flat) 与
NOISE=(insufficient, no_data)：`_payload()` 遇到别的词直接返回 None，那条
归因对规则战绩**完全不可见**（§2.1.4 的「归因反哺提案」会静默失效）。
所以这里保留引擎词表，把 §8.4 的 evidence_level / control / effect / why
作为**新增键**附加 —— 形状只增不改，读旧字段的人不受影响。

三档判据（代码判定，不交给模型）
--------------------------------
判定线 = `facts.min_clicks_for_orders(账户后窗 CVR, 后窗订单数)`：
「在这个订单数下，要多少次点击才能说它比基线差」。

* strong = 后窗点击 ≥ 判定线 且 有对照组 且 效应方向与对照相反
* medium = 后窗点击 ≥ 判定线 或（有对照组 且 效应幅度 > 20%）
* weak   = 其余；也没有方向（flat/insufficient/no_data）一律 weak

用法
----
    python3 adsdesk/agents/attribute.py
    python3 adsdesk/agents/attribute.py --dry-run
    python3 adsdesk/agents/attribute.py --now 2026-11-27T07:00:00+08:00
    python3 adsdesk/agents/attribute.py --decision 153
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# adsdesk/ 让 `from agents import ...` 能解析；engine/ 让 `import facts` 能解析
# （facts.py 同目录还 import scope）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

import facts  # noqa: E402

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]                      # 项目根
DB = str(ROOT / "ingest" / "data" / "ads.sqlite3")

CST = ZoneInfo("Asia/Shanghai")
CST_TZ = timezone(timedelta(hours=8))       # strptime 用；与 CST 同一时区

AGENT = "attribute"
# 这个 agent 不调模型：三档判据全在代码里（§8.4「写进代码，不交给 LLM」）。
# model 写成无歧义的字符串，免得以后有人以为它是模型结论。
MODEL = "deterministic/no-llm"
PROMPT_VER = "attribute-v1"                 # 判据版本，跟代码一起改

# ------------------------------------------------------------------ 常量

# 归因窗长度（天）—— 计划书 §4.4，**全系统唯一定义**。
# approve.py 标「已改」时用它算 attribution_due_at，本 agent 则用
# attribution_due_at − executed_at 反推窗口长度：一处定义，两边不可能漂。
#
# 为什么不是一刀切 7 天：
#   预算改动当天就影响投放量；bid 要等下一次竞价生效；否词要等归因回传；
#   收割/暂停要等长尾跑完。窗口短了 = 在效应还没显现时判「无效」。
ATTRIBUTION_DAYS = {
    "lower_budget": 3,      # 降预算：当天见效，但要看 3 天才够判定线
    "reduce_budget": 3,     # 同义的旧写法，留着防止历史行匹配不到
    "raise_budget": 3,
    "reduce_bid": 7,        # 调 bid 要等竞价与归因回传
    "raise_bid": 7,
    "adjust_placement": 7,  # 位置溢价
    "add_negative": 14,     # 否词：要等搜索词重新累积
    "harvest": 14,          # 收割：长尾
    "harvest_keyword": 14,
    "pause_candidate": 14,  # 停投：判断「停了到底有没有变差」需要更长窗口
    "pause": 14,
    "floor_breach": 7,      # 计划书未列；地板价击穿是风控动作，按默认 7 天回看
    "observe": 7,           # 观察类：不改量，按默认窗口回看
}
DEFAULT_ATTRIBUTION_DAYS = 7

# 判「有变化」的带宽。**工程默认，不是统计结论** —— 单对象前后对比噪声大，
# 低于 15% 的变化不值得当信号。
RELEVANCE_BAND = 0.15

# 后窗口点击少于此数，任何方向都不谈（连 CVR 都不稳）。
MIN_AFTER_CLICKS = 20

# 终局窗口：7 天到期先回看一次，14 天再补一次。
FINAL_WINDOW_DAYS = 14        # 终局窗口：执行后第 14 天回看
GRACE_DAYS = 7                # 窗结束后再等 7 天；仍无数据就按「确实没数据」结案

# 对照组：同规则、同层级、±7 天内未改动，最多取 5 条（§8.4 的 SQL 原样）。
CONTROL_WINDOW_DAYS = 7
CONTROL_PEER_LIMIT = 5

# medium 的第二个条件：「效应幅度 > 20%」。
EFFECT_SIGNIFICANT = 0.20

# 判方向时忽略的小抖动：低于 2% 的变化不当成「哪个方向」。
SIGN_EPS = 0.02

# 强/中/弱。顺序有意义，strong 先判。
EVIDENCE_LEVELS = ("strong", "medium", "weak")

# record.py 认的方向词；本文件里凡是「有没有方向」都问这个元组。
DIRECTIONS = ("improved", "worsened", "flat")
NOISE = ("insufficient", "no_data")


def days_for(action_type: str | None) -> int:
    """动作类型 → 归因窗天数。approve.py 与文档之外的一切都该走这个函数。"""
    return ATTRIBUTION_DAYS.get((action_type or "").lower(), DEFAULT_ATTRIBUTION_DAYS)


# ---------------------------------------------------------------- 时间与账本

def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=CST_TZ)
        except ValueError:
            continue
    return None


def _d(day: str) -> date:
    return date.fromisoformat(day)


def _window(start: date, end: date) -> tuple[str, str, int]:
    """闭区间窗口。end < start 时返回 0 天。"""
    return start.isoformat(), end.isoformat(), max(0, (end - start).days + 1)


_ROWS_SQL = """select decision_id, run_id, rule_id, entity_type, entity_id, entity_label,
                      market_id, campaign_id, action_type, current_value, proposed_value,
                      executed_value, executed_at, attribution_due_at, confidence,
                      decided_at, spend_impact, outcome_7d_json, outcome_14d_json,
                      outcome_filled_at
                 from decision_log
                where {where}
                order by {order}"""


def due_rows(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """到期待回填：状态是 executed、归因窗已过、7 天结果还没落定。

    第二个分支是「补跑」：上一轮跑的时候报表还没跟上（after_covered_days
    小于 window_days），那次的 no_data 只说明「数据没到」，不说明「没效果」。
    不补跑的话，这条结论会永久冻结在库里 —— #153 就是这种情况。
    一旦覆盖补齐，payload 里的 after_covered_days 等于 window_days，
    这个分支自然不再选中它，不会无限重跑。

    now 只用于调用方过滤（见 run）；这里不重复判时间，因为 SQL 里的
    attribution_due_at <= now 与 _parse_ts 的时区处理口径必须只有一处。
    """
    return conn.execute(_ROWS_SQL.format(
        where=("status = 'executed' and outcome_filled_at is null "
               "and attribution_due_at is not null and ("
               "  outcome_7d_json is null"
               "  or coalesce(json_extract(outcome_7d_json, '$.after_covered_days'), 0)"
               "     < coalesce(json_extract(outcome_7d_json, '$.window_days'), 0)"
               ")"),
        order="attribution_due_at")).fetchall()


def second_pass_rows(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    """7 天已回填、14 天还没到终局的：到期后再补一次长窗口。

    `{"pending": true}` 占位是「还没看」，不是「已经看过没有」——把它当非空
    排除掉，14 天终局就永远不会跑（引擎那版就有这个洞）。
    """
    return conn.execute(_ROWS_SQL.format(
        where=("status = 'executed' and outcome_filled_at is null "
               "and outcome_7d_json is not null and executed_at is not null "
               "and (outcome_14d_json is null "
               "     or coalesce(json_extract(outcome_14d_json, '$.pending'), 0) = 1)"),
        order="executed_at")).fetchall()


# ---------------------------------------------------------------- 取数

_METRIC_COLS = ("sum(clicks) clicks, sum(impressions) impressions, "
                "sum(cost) cost, sum(ads_sales) sales, sum(ads_orders) orders")

# entity_type → 事实表。表里没有的对象就不该出现在决策里，出现也算没数据。
_ENTITY_TABLE = {
    "keyword": ("fact_keyword_daily", "keyword_id"),
    "search_term": ("fact_search_term_daily", "search_term"),
    "campaign": ("fact_campaign_daily", "campaign_id"),
    "placement": ("fact_placement_daily", "placement"),
}


def _ratio(num, den):
    if num is None or den in (None, 0):
        return None
    return round(num / den, 4)


def _derive(raw: dict, start: str, end: str, rows: int | None = None) -> dict:
    clicks = raw.get("clicks")
    orders = raw.get("orders")
    cost = raw.get("cost")
    sales = raw.get("sales")
    impressions = raw.get("impressions")
    out = {
        "start": start,
        "end": end,
        "days": (_d(end) - _d(start)).days + 1,
        "clicks": clicks or 0,
        "impressions": impressions or 0,
        "orders": orders or 0,
        "cost": round(cost, 2) if cost is not None else None,
        "sales": round(sales, 2) if sales is not None else None,
        "cvr": _ratio(orders, clicks),
        "cpc": _ratio(cost, clicks),
        "cpo": _ratio(cost, orders),   # 每单成本：最像「这笔钱花得值不值」
        "acos": _ratio(cost, sales),
    }
    if rows is not None:
        out["data_days"] = rows
    return out


def _entity_stats(conn: sqlite3.Connection, entity_type: str, entity_id,
                  start: str, end: str) -> dict:
    """按 entity_type 选表。找不到对象就当没数据，不猜。"""
    spec = _ENTITY_TABLE.get((entity_type or "").lower())
    if spec is None:
        return {"rows": 0, "unsupported_entity_type": entity_type}
    table, key = spec
    sql = (f"select count(distinct report_date) rows, {_METRIC_COLS} "
           f"from {table} where {key} = ? and report_date between ? and ?")
    try:
        r = conn.execute(sql, (entity_id, start, end)).fetchone()
    except sqlite3.Error as exc:              # 表/列不在（老库）
        return {"rows": 0, "error": str(exc)}
    if r is None or (r["clicks"] is None and r["cost"] is None
                     and r["impressions"] is None):
        return {"rows": 0}
    return _derive({k: r[k] for k in r.keys() if k != "rows"},
                   start, end, rows=r["rows"])


def _account_stats(conn: sqlite3.Connection, market_id, start: str, end: str) -> dict:
    """同期全店：用来扣掉「整个账户都在变」那部分。"""
    try:
        if market_id is None:
            r = conn.execute(
                f"select count(distinct report_date) rows, {_METRIC_COLS} "
                "from fact_campaign_daily where report_date between ? and ?",
                (start, end)).fetchone()
        else:
            r = conn.execute(
                f"select count(distinct report_date) rows, {_METRIC_COLS} "
                "from fact_campaign_daily where market_id = ? and report_date between ? and ?",
                (market_id, start, end)).fetchone()
    except sqlite3.Error as exc:
        return {"rows": 0, "error": str(exc)}
    if r is None or not r["rows"]:
        return {"rows": 0}
    return _derive({k: r[k] for k in r.keys() if k != "rows"},
                   start, end, rows=r["rows"])


# ---------------------------------------------------------------- 判定

def _pct_delta(before, after):
    if before in (None, 0) or after is None:
        return None
    return round((after - before) / before, 4)


def _verdict(before: dict, after: dict, acct_b: dict, acct_a: dict) -> tuple[str, str]:
    """返回 (verdict, basis)。宁可 insufficient，不把噪声说成效果。

    顺序有讲究：先看数据到没到，再看样本够不够，最后才谈方向。
    零单那一段（最常见）不给「每单成本」—— 除以 0 没有意义，改比花费，
    同样扣掉同期全店的变化。
    """
    if not after.get("clicks") and not after.get("cost"):
        return "no_data", "执行后这段窗口还没有数据 —— 报表还没到，等下一天。"
    if (after.get("days") or 0) <= 0:
        return "no_data", "执行后窗口长度为 0，没有可比的数据。"

    acct_cost = _pct_delta(acct_b.get("cost"), acct_a.get("cost"))
    acct_cpo = _pct_delta(acct_b.get("cpo"), acct_a.get("cpo"))

    # ---- 执行后零单：每单成本无意义，比花费 ----
    if not after.get("orders"):
        cost_b, cost_a = before.get("cost") or 0.0, after.get("cost") or 0.0
        if cost_b <= 0 and cost_a <= 0:
            return "insufficient", "执行前后都没有花费，没有可比的东西。"
        ent = _pct_delta(cost_b, cost_a)
        rel = round((ent if ent is not None else -1.0) - (acct_cost or 0.0), 4)
        word = "相对少花" if rel < 0 else "相对多花"
        if acct_cost is None:
            line = f"执行后零单：花 {cost_a:.2f}（前 {cost_b:.2f}）；全店同期无对照"
        else:
            line = (f"执行后零单：花 {cost_a:.2f}（前 {cost_b:.2f}"
                    f"{'' if ent is None else f'，{ent:+.0%}'}）；"
                    f"全店同期花费 {acct_cost:+.0%}，{word} {abs(rel):.0%}")
        if before.get("orders") and not after.get("orders"):
            # 零单是不是「改坏了」，取决于同期本来就该出几单。
            exp = (after.get("clicks") or 0) * (acct_a.get("cvr") or 0.0)
            line += f"；{before['orders']} 单 → 0 单"
            if acct_cpo and acct_cpo > 0:
                line += f"（全店每单成本同期 {acct_cpo:+.0%}）"
            if exp < 1.0:
                return ("insufficient",
                        line + f"。按全店同期 CVR，这 {after.get('clicks') or 0} 击"
                               f"本来也只该出 {exp:.2f} 单，零单在噪声内，判不出是谁的错。")
            return ("worsened",
                    line + f"。按全店同期 CVR 这 {after.get('clicks') or 0} 击"
                           f"该出 {exp:.2f} 单，零单超出了噪声。")
        if rel <= -RELEVANCE_BAND:
            return "improved", line + " → 同样零单，但相对少花了钱。"
        if rel >= RELEVANCE_BAND:
            return "worsened", line + " → 同样零单，还相对多花了钱。"
        return "flat", line + " → 落在带宽内。"

    if (after.get("clicks") or 0) < MIN_AFTER_CLICKS:
        return ("insufficient",
                f"执行后只有 {after.get('clicks') or 0} 次点击，少于 {MIN_AFTER_CLICKS}，"
                f"这个量级连 CVR 都不稳，不谈效果。")

    # ---- 前后都出单：比每单成本，扣掉全店 ----
    cpo_b, cpo_a = before.get("cpo"), after.get("cpo")
    if cpo_b and cpo_a:
        ent = _pct_delta(cpo_b, cpo_a)
        rel = round(ent - (acct_cpo or 0.0), 4)
        # 全店可能压根没出单（acct_cpo 为 None）——那就只报绝对值，
        # 别拿 None 去格式化，也别假装做了扣减。
        acct_txt = (f"全店同期 {acct_cpo:+.0%}" if acct_cpo is not None
                    else "全店同期无单，扣不掉全店那层")
        basis = (f"每单成本 {cpo_b:.2f} → {cpo_a:.2f}（{ent:+.0%}），"
                 f"{acct_txt}；相对变化 {rel:+.0%}，带宽 ±{RELEVANCE_BAND:.0%}")
        if rel <= -RELEVANCE_BAND:
            return "improved", basis + " → 相对全店确实变好。"
        if rel >= RELEVANCE_BAND:
            return "worsened", basis + " → 相对全店确实变差。"
        return "flat", basis + " → 落在带宽内，看不出方向。"

    # ---- 执行前零单、执行后出单 ----
    if before.get("orders") in (0, None):
        head = (f"执行前零单（{before.get('clicks') or 0} 击），"
                f"执行后出 {after['orders']} 单、花 {after.get('cost') or 0:.2f}")
        if acct_cpo is not None:
            head += f"；全店每单成本同期 {acct_cpo:+.0%}"
        return "improved", head + "。"

    return "insufficient", "前后都出了单但每单成本算不出来，样本不足。"


# ---------------------------------------------------------------- 对照组

def peers(conn: sqlite3.Connection, d: sqlite3.Row) -> list[dict]:
    """§8.4 的对照组：同规则、同层级、±7 天内**没改动**的邻居。

    只收 expired / rejected —— 这两种的共同点是「建议在，但后台没动」。
    拿它们当反事实基线：如果这些没动的对象同期也朝同一个方向变，
    那这次改动就不是功劳，是趋势。
    """
    anchor = (d["executed_at"] or "")[:10] or (d["decided_at"] or "")[:10]
    if not anchor:
        return []
    try:
        rows = conn.execute(
            """select decision_id, entity_type, entity_id, entity_label, status,
                      decided_at, action_type
                 from decision_log
                where rule_id = ? and entity_type = ? and decision_id != ?
                  and substr(decided_at, 1, 10) between date(?, ?) and date(?, ?)
                  and status in ('expired', 'rejected')
                order by decided_at desc
                limit ?""",
            (d["rule_id"], d["entity_type"], d["decision_id"],
             anchor, f"-{CONTROL_WINDOW_DAYS} days",
             anchor, f"+{CONTROL_WINDOW_DAYS} days",
             CONTROL_PEER_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _peer_window(peer: dict, n: int) -> tuple[tuple[str, str, int], tuple[str, str, int]] | None:
    """对照组的时间窗：以 decided_at 为锚，长度与本次归因窗一致。"""
    anchor = _parse_ts(peer.get("decided_at"))
    if not anchor:
        return None
    a = anchor.date()
    return (_window(a - timedelta(days=n), a - timedelta(days=1)),
            _window(a, a + timedelta(days=n - 1)))


def _sum_stats(items: list[dict]) -> dict:
    clicks = sum(i.get("clicks") or 0 for i in items)
    orders = sum(i.get("orders") or 0 for i in items)
    cost = sum(i.get("cost") or 0.0 for i in items)
    return {
        "clicks": clicks,
        "orders": orders,
        "cost": round(cost, 2),
        "cvr": _ratio(orders, clicks),
        "cpc": _ratio(cost, clicks),
        "cpo": _ratio(cost, orders),
    }


def control_block(conn: sqlite3.Connection, d: sqlite3.Row, n: int) -> dict | None:
    """对照组聚合。没有可用对照 → None（调用方据此把 evidence_level 压到 weak）。"""
    rows = peers(conn, d)
    if not rows:
        return None
    before_items, after_items, ids, usable = [], [], [], 0
    for p in rows:
        win = _peer_window(p, n)
        if not win:
            continue
        (bs, be, bdays), (as_, ae, adays) = win
        b = _entity_stats(conn, p["entity_type"], p["entity_id"], bs, be)
        a = _entity_stats(conn, p["entity_type"], p["entity_id"], as_, ae)
        if not b.get("clicks") and not a.get("clicks"):
            continue                            # 一点数据都没有，就别充数
        usable += 1
        ids.append(p["decision_id"])
        before_items.append(b)
        after_items.append(a)
    if not usable:
        return None
    cb, ca = _sum_stats(before_items), _sum_stats(after_items)
    return {
        **ca,                                    # §8.4 形状：后窗聚合在前
        "before": cb,
        "delta": {
            "cpc_delta_pct": _pct_delta(cb.get("cpc"), ca.get("cpc")),
            "cvr_delta_pct": _pct_delta(cb.get("cvr"), ca.get("cvr")),
            "cost_delta_pct": _pct_delta(cb.get("cost"), ca.get("cost")),
            "orders_delta": (ca.get("orders") or 0) - (cb.get("orders") or 0),
        },
        "peers": usable,
        "ids": ids,
        "note": (f"同规则、同层级、±{CONTROL_WINDOW_DAYS} 天内未改动的 {usable} 条对照；"
                 f"它们同期朝哪边走，就说明哪部分变化不是这次改动带来的"),
    }


# ---------------------------------------------------------------- 效应与证据

def effect_block(before: dict, after: dict) -> dict:
    """§8.4 的 effect：只报三个能直接读的数，外加成本侧两个辅助。

    后窗一条数据都没有时全部给 null —— 不能给 0。0 会被读成
    「变动了 0%」，而真相是「还没测」，这两件事在复盘里结论相反。
    """
    if not after or after.get("rows") == 0 or "clicks" not in after:
        return {"cpc_delta_pct": None, "cvr_delta_pct": None, "orders_delta": None,
                "cost_delta_pct": None, "cpo_delta_pct": None}
    return {
        "cpc_delta_pct": _pct_delta(before.get("cpc"), after.get("cpc")),
        "cvr_delta_pct": _pct_delta(before.get("cvr"), after.get("cvr")),
        "orders_delta": (after.get("orders") or 0) - (before.get("orders") or 0),
        "cost_delta_pct": _pct_delta(before.get("cost"), after.get("cost")),
        "cpo_delta_pct": _pct_delta(before.get("cpo"), after.get("cpo")),
    }


def threshold_clicks(baseline_cvr: float | None, orders: int) -> int | None:
    """判定线：这个订单数下，要多少点击才能说「它比基线差」。

    baseline 用**账户后窗 CVR**（同期市场水平），不用对象自己的 before CVR ——
    用自己过去的 CVR 当基线，会把「本来就差」算成「改坏了」。

    置信度跟着 `facts.DEFAULT_CONFIDENCE`（本仓 0.80，单侧）。别把它当 95%：
    这是「够不够下判断」的工程线，不是学术结论；升级只改 facts 一处。
    """
    if not baseline_cvr or not (0 < baseline_cvr < 1):
        return None
    return facts.min_clicks_for_orders(baseline_cvr, int(orders or 0))


def _direction(verdict: str) -> int:
    return {"improved": 1, "worsened": -1}.get(verdict, 0)


def _direction_of(delta: dict) -> int:
    """从对照组的变化率猜方向：+1 变好、-1 变差、0 看不出。

    只看两个最能代表效率的维度：CVR（分母）与 CPC（分子）。
    「CVR 上去、CPC 下来」= 效率变好，反之变差；都落在 SIGN_EPS 内算没方向。
    """
    cvr = delta.get("cvr_delta_pct")
    cpc = delta.get("cpc_delta_pct")
    score = (cvr or 0.0) - (cpc or 0.0)
    if score > SIGN_EPS:
        return 1
    if score < -SIGN_EPS:
        return -1
    return 0


def grade(verdict: str, after: dict, line: int | None,
          control: dict | None, effect: dict) -> tuple[str, str]:
    """三档判定（§8.4）。返回 (evidence_level, 为什么是这个等级)。

    这是整个 agent 的**唯一裁决点**：level 决定能不能进策略库，
    所以它必须是纯函数、可重放、不依赖任何模型输出。
    """
    clicks = after.get("clicks") or 0

    # 没有方向就没有策略：flat / insufficient / no_data 一律 weak。
    if verdict not in ("improved", "worsened"):
        if verdict == "flat":
            return "weak", f"判定为「看不出方向」，策略库只收有方向的结论。"
        return "weak", f"判定为「{verdict}」，样本不够，不构成证据。"

    reached = bool(line) and clicks >= line
    has_control = bool(control and control.get("peers"))
    opposite = (has_control
                and _direction(verdict) * _direction_of(control.get("delta") or {}) < 0)
    amp = max(abs(effect.get("cpc_delta_pct") or 0.0),
              abs(effect.get("cvr_delta_pct") or 0.0))

    if reached and opposite:
        return ("strong",
                f"后窗 {clicks} 击 ≥ 判定线 {line} 击，且 {control['peers']} 条对照"
                f"同期朝反方向走 —— 变化不能由账户趋势解释。")

    if reached:
        why = f"后窗 {clicks} 击 ≥ 判定线 {line} 击，样本够了"
        if not has_control:
            why += "，但没有可用对照（同规则同层级±7天内无未改动对象），只能算中等。"
        else:
            why += ("，但对照组同期同向变化，方向分不清是改动还是趋势，只能算中等。"
                    if not opposite else "。")
        return "medium", why

    if has_control and amp >= EFFECT_SIGNIFICANT:
        return ("medium",
                f"效应幅度 {amp:.0%} ≥ {EFFECT_SIGNIFICANT:.0%}，且有 {control['peers']} 条对照；"
                f"但后窗 {clicks} 击未到判定线 {line if line else '（算不出）'}，只能算中等。")

    if not has_control:
        return ("weak",
                f"后窗 {clicks} 击" + (f"，未到判定线 {line} 击" if line else "，判定线算不出")
                + "；且同规则同层级±7天内没有未改动对象，缺对照组 → 不进策略库。")
    return ("weak",
            f"后窗 {clicks} 击未到判定线 {line if line else '（算不出）'}，"
            f"效应幅度 {amp:.0%} < {EFFECT_SIGNIFICANT:.0%}，证据不足以进策略库。")


# ---------------------------------------------------------------- 组装

def _data_through(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("select max(report_date) d from fact_campaign_daily").fetchone()
    return r["d"] if r and r["d"] else None


def _windows(d: sqlite3.Row, long: bool) -> tuple[int, dict] | None:
    """按账本自己的 executed→due 天数推窗口，不另建一张映射表。

    这样 approve.py 改了 ATTRIBUTION_DAYS，本 agent 无需再改一行：
    due_at 是那次写库时算出来的，窗口从它反推，天然一致。
    """
    ex = _parse_ts(d["executed_at"])
    due = _parse_ts(d["attribution_due_at"])
    if not ex:
        return None
    if long:
        n = FINAL_WINDOW_DAYS
    elif due:
        n = max(1, (due.date() - ex.date()).days)
    else:
        n = days_for(d["action_type"])
    exd = ex.date()
    if long or not due:
        before = _window(exd - timedelta(days=n), exd - timedelta(days=1))
        after = _window(exd, exd + timedelta(days=n - 1))
    else:
        before = _window(exd - timedelta(days=n), exd - timedelta(days=1))
        after = _window(exd, due.date() - timedelta(days=1))
    return n, {"n": n, "before": before, "after": after}


def _predicts(d: sqlite3.Row, payload: dict, now: datetime) -> list[dict]:
    """写一条可被 converge.py 确定性判定的断言（§8.4 的 predicts）。

    为什么只写一条、而且是二项检验：
    `converge.py:_ev_binom` 能从 method 里的内联参数
    （clicks=N / orders=K / p0=X）纯算术复算，不碰库、不可能判错口径。
    断言的内容就是这次判定的核心主张 ——「这点差异算不算显著」。
    窗口内还会进新数据，所以它仍是一条会被推翻的预言，不是复述结果。
    """
    after = payload.get("after") or {}
    acct = (payload.get("account") or {}).get("after") or {}
    clicks, orders = int(after.get("clicks") or 0), int(after.get("orders") or 0)
    p0 = acct.get("cvr")
    if not p0 or clicks <= 0 or not (0 < p0 < 0.5):
        return []                              # 没有基线就别写判不了的断言
    ex = _parse_ts(d["executed_at"]) or now
    n = payload["window_days"]
    significant = payload["verdict"] in ("improved", "worsened")
    # 阈值与判定线同一个置信度：converge 的 `_ev_binom` 算双侧 p，再过
    # `p < alpha`。alpha 取 1 - facts.DEFAULT_CONFIDENCE，断言才和本次判定
    # 说同一件事 —— 否则用 p<0.05 复算，等于拿更严的尺子量同一条结论，
    # 兑现率会因为这个口径差假性归零。
    alpha = round(1.0 - facts.DEFAULT_CONFIDENCE, 2)
    spec = _ENTITY_TABLE.get((d["entity_type"] or "").lower())
    table = spec[0] if spec else "fact_keyword_daily"
    return [{
        "claim": (f"#{d['decision_id']} 后窗 CVR 与账户基线 {p0:.2%} "
                  f"{'有' if significant else '无'}显著差异（本次判定 {payload['verdict']}）"),
        "verifiable_at": (ex + timedelta(days=n)).isoformat(timespec="seconds"),
        "method": (f"decision_log + {table}；clicks={clicks} / orders={orders} / p0={p0}；"
                   f"窗口 {after.get('start')}~{after.get('end')}；双侧二项检验"),
        "expected": f"p < {alpha}" if significant else f"p ≥ {alpha}",
    }]


def build_outcome(conn: sqlite3.Connection, d: sqlite3.Row,
                  now: datetime, long: bool = False) -> dict | None:
    """一条决策的完整归因结果。纯读，不写库（dry-run 与实跑共用）。"""
    win = _windows(d, long)
    if not win:
        return None
    n, w = win
    bs, be, _bdays = w["before"]
    as_, ae, _adays = w["after"]

    before = _entity_stats(conn, d["entity_type"], d["entity_id"], bs, be)
    after = _entity_stats(conn, d["entity_type"], d["entity_id"], as_, ae)
    acct_b = _account_stats(conn, d["market_id"], bs, be)
    acct_a = _account_stats(conn, d["market_id"], as_, ae)

    verdict, basis = _verdict(before, after, acct_b, acct_a)
    effect = effect_block(before, after)
    control = control_block(conn, d, n)
    line = threshold_clicks(acct_a.get("cvr"), after.get("orders") or 0)
    level, why = grade(verdict, after, line, control, effect)

    # 数据只到哪天：写进结果，免得人拿「报表还没到」当成「没效果」
    through = _data_through(conn)
    covered = None
    if through:
        last = min(_d(through), _d(ae))
        if _d(as_) <= last:
            covered = (last - _d(as_)).days + 1

    payload = {
        # 自描述：只看这段 JSON 也能知道它说的是哪条决策（§8.4 的示例里就有）
        "decision_id": d["decision_id"],
        "rule_id": d["rule_id"],
        # —— 原有字段（record.py / cockpit.py / memorial.py 都在读，不能改名）
        "window_days": n,
        "executed_at": d["executed_at"],
        "executed_value": d["executed_value"],
        "action_type": d["action_type"],
        "entity": {"type": d["entity_type"], "id": d["entity_id"],
                   "label": d["entity_label"]},
        "before": before,
        "after": after,
        # —— §8.4 新增
        "control": control,
        "effect": effect,
        "evidence_level": level,
        "why": why,
        "threshold_clicks": line,
        "account": {"before": acct_b, "after": acct_a,
                    "note": "同期全店，用来扣掉「整个账户都在变」那部分"},
        "relative": {
            "cpo": effect.get("cpo_delta_pct"),
            "cvr": effect.get("cvr_delta_pct"),
            "cost": effect.get("cost_delta_pct"),
            "clicks": _pct_delta(before.get("clicks"), after.get("clicks")),
        },
        "verdict": verdict,
        "basis": basis,
        "data_through": through,
        "after_covered_days": covered,
        "caveat": ("执行前后对比，不是 A/B 测试：同期账户整体也在变（旺季、流量、"
                   "竞品动作），所以每条都给了全店同期与同规则对照。看的是「相对对照」"
                   "那一列，不是绝对数字。"),
        "filled_at": now.isoformat(timespec="seconds"),
    }
    payload["predicts"] = _predicts(d, payload, now)
    return payload


# ---------------------------------------------------------------- agent 账本

def _open_run(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute(
        """insert into agent_run (job, model, prompt_ver, started_at, status)
           values (?, ?, ?, ?, 'running')""",
        (AGENT, MODEL, PROMPT_VER, now.isoformat(timespec="seconds")))
    return int(cur.lastrowid)


def _finish_run(conn: sqlite3.Connection, run_id: int, status: str,
                output: dict | None, error: str | None = None) -> None:
    conn.execute(
        """update agent_run set finished_at=?, status=?, output_json=?, error=?
            where agent_run_id=?""",
        (datetime.now(CST).isoformat(timespec="seconds"), status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         error, run_id))


def _reap_stale(conn: sqlite3.Connection, minutes: int = 120) -> int:
    """上一次跑到一半崩了（进程被杀/断电）会留下 running 行，先收掉。"""
    cur = conn.execute(
        """update agent_run set status='failed', finished_at=?,
                  error=coalesce(error, 'stale: 上次运行没有收尾')
            where job=? and status='running'
              and started_at < ?""",
        (datetime.now(CST).isoformat(timespec="seconds"), AGENT,
         (datetime.now(CST) - timedelta(minutes=minutes)).isoformat(timespec="seconds")))
    return cur.rowcount or 0


def _write_evals(conn: sqlite3.Connection, run_id: int, d: sqlite3.Row,
                 payload: dict, now: datetime) -> int:
    """把 predicts 落成 agent_eval，等 converge 到期回填。"""
    n = 0
    ts = now.isoformat(timespec="seconds")
    for p in payload.get("predicts") or []:
        claim = (p.get("claim") or "").strip()
        if not claim:
            continue
        dup = conn.execute(
            "select 1 from agent_eval where agent=? and claim=? limit 1",
            (AGENT, claim[:500])).fetchone()
        if dup:
            continue                            # 补跑不重复记账，否则兑现率被灌水
        conn.execute(
            """insert into agent_eval (agent_run_id, agent, claim, verifiable_at,
                                       method, expected, verdict, created_at)
               values (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (run_id, AGENT, claim[:500], p.get("verifiable_at"),
             (p.get("method") or "")[:800], (p.get("expected") or "")[:300], ts))
        n += 1
    return n


def _policy_proposal(conn: sqlite3.Connection, d: sqlite3.Row,
                     payload: dict, now: datetime) -> bool:
    """§9.3 的硬门：只有 strong / medium 才写策略库，且写成 proposed。

    为什么不写 active：`agents/context.py` 与 `routes/meta.py` 只读
    status='active'。写成 proposed = 这条结论已经沉淀、但还没被主人认领；
    weak 一行都不写 —— 这就是「弱证据不进策略库」的落地点。
    """
    level = payload.get("evidence_level")
    if level not in ("strong", "medium"):
        return False
    if payload.get("verdict") not in ("improved", "worsened"):
        return False
    if not payload.get("control"):
        return False                            # 无对照不沉淀（§4.4）
    source = f"归因agent#{d['decision_id']}-{payload['window_days']}d-{now.date()}"
    dup = conn.execute(
        "select 1 from policy where object_id=? and policy_type=? and source=?",
        (str(d["entity_id"]), f"attribution_{d['action_type']}", source)).fetchone()
    if dup:
        return False
    conn.execute(
        """insert into policy (scope, object_id, policy_type, params_json, source,
                               evidence_text, status, created_at)
           values (?, ?, ?, ?, ?, ?, 'proposed', ?)""",
        (d["entity_type"] or "platform", str(d["entity_id"]),
         f"attribution_{d['action_type']}",
         json.dumps({
             "decision_id": d["decision_id"],
             "rule_id": d["rule_id"],
             "window_days": payload["window_days"],
             "verdict": payload["verdict"],
             "evidence_level": level,
             "effect": payload.get("effect"),
             "threshold_clicks": payload.get("threshold_clicks"),
             "control_ids": (payload.get("control") or {}).get("ids"),
         }, ensure_ascii=False),
         source, (payload.get("why") or "")[:500],
         now.isoformat(timespec="seconds")))
    return True


def _reason_hit(conn: sqlite3.Connection, d: sqlite3.Row, payload: dict,
                now: datetime) -> None:
    """归因结论也进原因账本，将来复盘这个词时能看到「当时回看过，结论是什么」。"""
    try:
        text = (f"{payload['verdict']}/{payload.get('evidence_level')}："
                f"{payload.get('why') or payload.get('basis')}")[:500]
        row = conn.execute(
            """select hit_id from reason_hit
                where object_id=? and reason_code=? and decision_id=? limit 1""",
            (str(d["entity_id"]), f"outcome_{payload['window_days']}d",
             d["decision_id"])).fetchone()
        if row:
            conn.execute("update reason_hit set evidence_text=?, created_at=? where hit_id=?",
                         (text, now.isoformat(timespec="seconds"), row["hit_id"]))
            return
        conn.execute(
            """insert into reason_hit (object_id, reason_code, scope, evidence_text,
                                       created_at, answer_type, decision_id)
               values (?, ?, ?, ?, ?, 'attribution', ?)""",
            (str(d["entity_id"]), f"outcome_{payload['window_days']}d",
             d["rule_id"], text,
             now.isoformat(timespec="seconds"), d["decision_id"]))
    except sqlite3.Error:
        pass  # reason_hit 是旁证，写不进去不该挡住回填


def fill_one(conn: sqlite3.Connection, d: sqlite3.Row, now: datetime,
             long: bool = False, run_id: int | None = None) -> dict:
    payload = build_outcome(conn, d, now, long=long)
    if payload is None:
        return {"decision_id": d["decision_id"], "skipped": "executed_at 缺失或不可解析"}

    # 列名跟着**窗口长度**走，不跟着「第几次回看」走：一个 add_negative 天生
    # 就是 14 天窗，它第一次（也是唯一一次）回看就该落进 outcome_14d_json，
    # 否则 7 天列里塞着 14 天的数据，人和后续脚本都会被误导。
    final = long or payload["window_days"] >= FINAL_WINDOW_DAYS
    col = "outcome_14d_json" if final else "outcome_7d_json"
    sets = [f"{col} = ?"]
    params: list = [json.dumps(payload, ensure_ascii=False)]

    # 什么时候算「终局」：窗口满 14 天，且报表真的覆盖到窗末。
    # 覆盖不到的（投放停了、关键词零曝光）不能永远挂着：窗结束后再过
    # GRACE_DAYS 天仍没数据，就按「确实没数据」结案，caveat 里写明。
    final_window = long or payload["window_days"] >= FINAL_WINDOW_DAYS
    covered = payload.get("after_covered_days") or 0
    win_end = _d(payload["after"]["end"]) if payload.get("after") else None
    overdue = win_end is not None and (now.date() - win_end).days >= GRACE_DAYS
    mark_done = final_window and (covered >= payload["window_days"] or overdue)
    if mark_done:
        sets.append("outcome_filled_at = ?")
        params.append(now.isoformat(timespec="seconds"))

    # 7 天那次顺带把 14 天的占位写上，页面上能看出「还要再看一次」
    if not long and payload["window_days"] < FINAL_WINDOW_DAYS and not d["outcome_14d_json"]:
        sets.append("outcome_14d_json = ?")
        params.append(json.dumps({
            "pending": True,
            "window_days": FINAL_WINDOW_DAYS,
            "note": f"14 天终局要等到执行后第 {FINAL_WINDOW_DAYS} 天再看一次。",
        }, ensure_ascii=False))

    params.append(d["decision_id"])
    conn.execute(f"update decision_log set {', '.join(sets)} where decision_id = ?", params)
    _reason_hit(conn, d, payload, now)
    evals = _write_evals(conn, run_id, d, payload, now) if run_id else 0
    proposed = _policy_proposal(conn, d, payload, now)
    return {
        "decision_id": d["decision_id"],
        "verdict": payload["verdict"],
        "evidence_level": payload["evidence_level"],
        "window_days": payload["window_days"],
        "why": payload["why"],
        "evals": evals,
        "policy_proposed": proposed,
        "done": mark_done,
    }


# ---------------------------------------------------------------- 入口

def say(msg: str) -> None:
    print(msg, flush=True)


def run(db: str = DB, now: datetime | None = None, dry_run: bool = False,
        only: int | None = None, verbose: bool = True) -> dict:
    now = now or datetime.now(CST)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        if not dry_run:
            _reap_stale(conn)

        todo = [r for r in due_rows(conn, now)
                if (only is None or r["decision_id"] == only)
                and _parse_ts(r["attribution_due_at"])
                and _parse_ts(r["attribution_due_at"]) <= now]

        # 14 天终局这两道门缺一不可：窗到期（时间），且报表覆盖到窗末日（数据）。
        # 只判前者的话，报表迟到那天会算出一片空白，然后被当成终局永久写进库。
        _through_raw = _data_through(conn)
        through = _d(_through_raw) if _through_raw else None

        def _long_ready(r: sqlite3.Row) -> bool:
            ex = _parse_ts(r["executed_at"])
            if not ex:
                return False
            last_day = ex.date() + timedelta(days=FINAL_WINDOW_DAYS - 1)
            if last_day > now.date():
                return False
            if through is None:
                return False
            return through >= last_day

        todo_long = [r for r in second_pass_rows(conn, now)
                     if (only is None or r["decision_id"] == only)
                     and _long_ready(r)]

        if dry_run:
            results = [{"decision_id": r["decision_id"], "dry_run": True,
                        "outcome": build_outcome(conn, r, now, long=False)} for r in todo]
            results += [{"decision_id": r["decision_id"], "dry_run": True,
                         "outcome_14d": build_outcome(conn, r, now, long=True)}
                        for r in todo_long]
            stat = {"due": len(todo), "second_pass": len(todo_long),
                    "results": results, "now": now.isoformat(timespec="seconds"),
                    "dry_run": True}
            if verbose:
                say(f"[归因] dry-run：到期 {len(todo)} 条，14 天补看 {len(todo_long)} 条，未写库")
            return stat

        run_id = _open_run(conn, now)
        conn.commit()
        results = []
        try:
            for r in todo:
                one = fill_one(conn, r, now, long=False, run_id=run_id)
                results.append(one)
                if verbose:
                    say(f"[归因] #{one['decision_id']} {one.get('verdict')}"
                        f"／{one.get('evidence_level')}（{one.get('window_days')} 天）："
                        f"{one.get('why')}")
            for r in todo_long:
                one = fill_one(conn, r, now, long=True, run_id=run_id)
                results.append(one)
                if verbose:
                    say(f"[归因] #{one['decision_id']} 14 天终局 {one.get('verdict')}"
                        f"／{one.get('evidence_level')}：{one.get('why')}")
            conn.commit()
        except Exception as exc:                 # 任何一条炸了，整批回滚并记账
            conn.rollback()
            _finish_run(conn, run_id, "failed", None, f"{type(exc).__name__}: {exc}")
            conn.commit()
            raise

        levels = {k: 0 for k in EVIDENCE_LEVELS}
        for one in results:
            lv = one.get("evidence_level")
            if lv in levels:
                levels[lv] += 1
        stat = {
            "due": len(todo),
            "second_pass": len(todo_long),
            "filled": len(results),
            "levels": levels,
            "policy_proposed": sum(1 for one in results if one.get("policy_proposed")),
            "results": results,
            "now": now.isoformat(timespec="seconds"),
            "dry_run": False,
        }
        _finish_run(conn, run_id, "ok", stat)
        conn.commit()
        if verbose:
            say(f"[归因] 到期 {len(todo)} 条、14 天补看 {len(todo_long)} 条 → "
                f"strong {levels['strong']} / medium {levels['medium']} / weak {levels['weak']}，"
                f"策略库新增 {stat['policy_proposed']} 条（proposed）")
            if not todo and not todo_long:
                say("[归因] 没有到期的 —— 归因窗还没走完，这是正常的。")
        return stat
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="归因 agent：到期后拿真实数据回看一遍，并按三档证据分级")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--now", default=None, help="假装现在是这个时间（ISO 带时区）")
    ap.add_argument("--decision", type=int, default=None, help="只处理这一条")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    now = None
    if args.now:
        now = _parse_ts(args.now)
        if now is None:
            say(f"时间读不懂：{args.now}")
            return 2
    if not os.path.exists(args.db):
        say(f"库不在：{args.db}")
        return 2

    run(db=args.db, now=now, dry_run=args.dry_run, only=args.decision,
        verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())