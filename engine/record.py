"""规则战绩：把归因结果变成决策时真的会用上的事实。

为什么需要这个模块：`engine/attribute.py` 到期回填之后，结果只写进
`decision_log.outcome_7d_json / outcome_14d_json` 和一条 `reason_hit`。
规则层（`engine/rules.py`）从头到尾没读过这两个字段 —— 系统会「记得」上次
改坏了，但下次提案时一点都不会因此更谨慎。反馈环在最后一米是断的。

这个模块只做三件事，且都往「少说、别吹」的方向走：

1. **给每条决策附一段战绩**（`annotate`）：这条规则过去在这个对象、这个
   规则上被批过几次、结果如何。写进 context，人和 agent 都能看见。
2. **按战绩收紧配额**（`quota_cap`）：一条规则要是反复改坏，它今天能占到
   的待批名额就少一格 —— 不是封杀，是别让它天天霸占注意力。
3. **同一个对象反复改坏就本届不进待批**（`apply`）：挪进观察区，并在
   gate_reason 里写清楚是哪几次、什么结论。人仍然可以强制批准（S1 的出口）。

三条口径，都是为了不被噪声牵着走：

- 只认**已出方向**的判定（improved / worsened / flat）。`insufficient` 和
  `no_data` 不进分母 —— 把「证据不够」算成「没变好」，会让一个规则因为
  运气差被永久降级。
- **14 天终局优先于 7 天**。同一条决策先有 7 天、后有 14 天时只算一次。
- 样本少于 `MIN_SAMPLES` 时 `hit_rate` 返回 `None`，不是返回 0 或 1。
  一次执行说明不了任何规则的好坏，返回一个数字就是在装懂。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

# 只看最近 90 天：更早的执行对应的配置、口径、活动结构大概率都变了，
# 拿三个月前的结论给今天的提案"增信"，是在用旧世界的经验指导新世界。
RECORD_WINDOW_DAYS = 90

# 少于 3 次判定就不算战绩。1 次成功 = 运气，2 次 = 还可能是运气。
MIN_SAMPLES = 3

# 同一个对象被判「改坏了」几次之后就不再自动进待批。
# 取 2 而不是 1：第一次可能是市场波动，第二次还这样就是这个对象的脾气。
OBJECT_BRAKE_WORSENED = 2

DIRECTIONS = ("improved", "worsened", "flat")
NOISE = ("insufficient", "no_data")

VERDICT_LABEL = {
    "improved": "变好",
    "worsened": "变差",
    "flat": "看不出方向",
    "insufficient": "证据不够",
    "no_data": "还没数据",
}


# ------------------------------------------------------------------ 读账本

def _payload(raw: str | None) -> dict | None:
    """读 outcome_json。pending 占位（7 天那次写的 14 天占位）不算结果。"""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("pending"):
        return None
    return d if d.get("verdict") in DIRECTIONS + NOISE else None


def _rows(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """select decision_id, rule_id, entity_type, entity_id, entity_label,
                  action_type, executed_at, attribution_due_at,
                  outcome_7d_json, outcome_14d_json, outcome_filled_at
             from decision_log
            where status = 'executed'
              and (outcome_7d_json is not null or outcome_14d_json is not null)
              and coalesce(executed_at, decided_at) >= ?
            order by decision_id""",
        (since,),
    ).fetchall()


def _resolved(row: sqlite3.Row) -> dict | None:
    """一条决策的最终结论：14 天终局优先，其次 7 天。"""
    p14 = _payload(row["outcome_14d_json"])
    if p14:
        return p14
    return _payload(row["outcome_7d_json"])


# ------------------------------------------------------------------ 统计

def _tally(items: list[dict]) -> dict:
    c = Counter(i["verdict"] for i in items)
    judged = sum(c[k] for k in DIRECTIONS)
    return {
        "judged": judged,                       # 有方向的次数
        "improved": c["improved"],
        "worsened": c["worsened"],
        "flat": c["flat"],
        "insufficient": c["insufficient"],
        "no_data": c["no_data"],
        # 样本不够时是 None，不是 0 —— 见模块头第三条口径。
        "hit_rate": (c["improved"] / judged) if judged >= MIN_SAMPLES else None,
        "worsened_rate": (c["worsened"] / judged) if judged >= MIN_SAMPLES else None,
        "enough": judged >= MIN_SAMPLES,
    }


def build(conn: sqlite3.Connection, now: datetime | None = None,
          since_days: int = RECORD_WINDOW_DAYS) -> dict:
    """算出规则战绩与对象历史，供 annotate / quota_cap / apply 用。"""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%S")

    by_rule: dict[str, list[dict]] = {}
    by_object: dict[tuple[str, str], list[dict]] = {}
    items = []
    for row in _rows(conn, since):
        p = _resolved(row)
        if not p:
            continue
        item = {
            "decision_id": row["decision_id"],
            "rule_id": row["rule_id"],
            "entity_type": row["entity_type"],
            "entity_id": str(row["entity_id"]),
            "entity_label": row["entity_label"],
            "action_type": row["action_type"],
            "verdict": p["verdict"],
            "days": p.get("window_days"),
            "basis": (p.get("basis") or "")[:120],
            "executed_at": row["executed_at"],
        }
        items.append(item)
        by_rule.setdefault(row["rule_id"], []).append(item)
        by_object.setdefault((row["entity_type"], str(row["entity_id"])), []).append(item)

    return {
        "since": since[:10],
        "since_days": since_days,
        "min_samples": MIN_SAMPLES,
        "totals": _tally(items),
        "rules": {rid: _tally(v) for rid, v in by_rule.items()},
        "objects": {k: _tally(v) for k, v in by_object.items()},
        # 明细留着 —— gate_reason 要能说出"是哪几次"，只说"过去表现不好"没人信。
        "detail": {"rules": {rid: v for rid, v in by_rule.items()},
                   "objects": {f"{k[0]}:{k[1]}": v for k, v in by_object.items()}},
    }


def rule_line(stat: dict | None, rule_id: str) -> str | None:
    """把一条规则战绩翻成人话；样本不够时返回 None（宁可不提）。"""
    if not stat or not stat.get("enough"):
        return None
    judged = stat["judged"]
    parts = []
    if stat["improved"]:
        parts.append(f"{stat['improved']} 次变好")
    if stat["worsened"]:
        parts.append(f"{stat['worsened']} 次变差")
    if stat["flat"]:
        parts.append(f"{stat['flat']} 次看不出方向")
    tail = ""
    if stat["worsened_rate"] is not None and stat["worsened_rate"] >= 0.5:
        tail = "。这条规则最近不太灵，批之前先想想是不是该反过来判"
    return f"{rule_id} 过去 {judged} 次有结论的执行：" + "、".join(parts) + tail


# ------------------------------------------------------------------ 三处接入

def annotate(decisions: list, rec: dict) -> int:
    """给每条决策的 context 挂上规则战绩与对象历史。返回挂上的条数。

    没有战绩就什么都不挂 —— context 里塞一句「暂无历史」只会让人多读一行废话。
    """
    n = 0
    rules_stat = rec.get("rules") or {}
    objects_stat = rec.get("objects") or {}
    for d in decisions:
        ctx = d.context if isinstance(d.context, dict) else {}
        line = rule_line(rules_stat.get(d.rule_id), d.rule_id)
        obj = objects_stat.get((d.entity_type, str(d.entity_id)))
        if line:
            ctx["rule_record"] = line
            n += 1
        if obj and obj.get("enough"):
            bits = [
                f"{obj[k]} 次{VERDICT_LABEL[lab]}"
                for k, lab in (("improved", "improved"), ("worsened", "worsened"), ("flat", "flat"))
                if obj[k]
            ]
            ctx["object_record"] = (
                f"这个对象过去 {obj['judged']} 次有结论的改动：" + "、".join(bits)
            )
        if ctx:
            d.context = ctx
    return n


def quota_cap(rule_id: str, base: int, rec: dict) -> int:
    """按规则战绩收紧今天能占几个待批名额。

    只在「样本够 + 变差过半」时减一格，下限是 1 —— 再差的规则也留一个位置，
    否则它连被看见的机会都没有，人也无从判断它到底还准不准。
    """
    stat = (rec.get("rules") or {}).get(rule_id)
    if not stat or not stat.get("enough"):
        return base
    if stat["worsened_rate"] is not None and stat["worsened_rate"] >= 0.5:
        return max(1, base - 1)
    return base


def apply(pack, rec: dict) -> list:
    """同一对象反复改坏的，从待批挪进观察区。返回被挪走的决策。

    这不是封杀：挪走之后它仍然出现在「只看不动」里，仍然能强制批准（S1 出口），
    只是不再自动占掉一个待批名额。gate_reason 会写清楚是哪几次、判成什么。
    """
    objects = rec.get("objects") or {}
    detail = ((rec.get("detail") or {}).get("objects") or {})
    moved = []
    keep = []
    for d in pack.front:
        stat = objects.get((d.entity_type, str(d.entity_id)))
        if not stat or stat["worsened"] < OBJECT_BRAKE_WORSENED:
            keep.append(d)
            continue
        hist = detail.get(f"{d.entity_type}:{d.entity_id}") or []
        bits = [
            f"{h['executed_at'][:10] if h.get('executed_at') else '某天'} 判为"
            f"{VERDICT_LABEL.get(h['verdict'], h['verdict'])}"
            f"（{(h.get('basis') or '').split('。')[0][:40]}）"
            for h in hist
            if h["verdict"] == "worsened"
        ][:3]
        d.gate_reason = (
            f"这个对象已被改动 {stat['judged']} 次、其中 {stat['worsened']} 次判为变差"
            f"（{'；'.join(bits)}）。本轮不自动进待批 —— 要动就显式强制批准。"
        )
        d.status = "observing"
        moved.append(d)

    if moved:
        pack.front = keep
        pack.observing = list(pack.observing or []) + moved
        pack.notes.append(
            f"另有 {len(moved)} 条被「这个对象过去改坏过」挡在待批之外，可在只看不动里强制批准。"
        )
        pack.saved = sum(d.spend_impact for d in pack.front if d.spend_impact and d.spend_impact > 0)
        pack.spent = sum(-d.spend_impact for d in pack.front if d.spend_impact and d.spend_impact < 0)
    return moved


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="看规则战绩（只读）")
    ap.add_argument("--db", default=None)
    ap.add_argument("--since-days", type=int, default=RECORD_WINDOW_DAYS)
    args = ap.parse_args(argv)

    from pathlib import Path
    db = args.db or str(Path(__file__).resolve().parent.parent / "ingest" / "data" / "ads.sqlite3")
    conn = sqlite3.connect(db)
    rec = build(conn, since_days=args.since_days)
    conn.close()

    t = rec["totals"]
    print(f"[战绩] 窗口 {rec['since']} 起 {rec['since_days']} 天："
          f"{t['judged']} 次有结论（好 {t['improved']} / 差 {t['worsened']} / 平 {t['flat']}），"
          f"另有 {t['insufficient']} 次证据不够、{t['no_data']} 次没数据")
    if not rec["rules"]:
        print("[战绩] 还没有任何规则攒够历史 —— 归因回填第一轮之后才会有。")
    for rid, stat in sorted(rec["rules"].items()):
        line = rule_line(stat, rid)
        print("  " + (line or f"{rid}: {stat['judged']} 次有结论，样本还不够（要 {MIN_SAMPLES} 次）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())