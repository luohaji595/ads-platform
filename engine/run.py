"""
规则引擎主入口

一次运行做四件事：
  1. 给最新一天拍配置快照，和上一份比出变更流水（snapshot.py）
  2. 从积加报表算事实层（facts.py）——阈值、盈亏平衡、地板、校准系数
  3. 跑八条规则，得到建议清单（rules.py）
  4. 把建议写进 decision_log，并生成一份可批奏折到 reports/

它不做的事：
  - 不写广告后台。要写的时候走审批（ticket），不是这个脚本。
  - 不假装知道最优值。算不出来就说算不出来，标 observe。

用法：
    python3 run.py                      # 正常跑，写库 + 出奏折
    python3 run.py --dry-run            # 只出报告，不写库
    python3 run.py --window-days 21     # 换分析窗口
    python3 run.py --target-acos 0.35   # 换目标 ACOS
    python3 run.py --no-snapshot        # 跳过快照（调试用）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import datetime as _dt
from datetime import datetime, timedelta, timezone

import facts
import reason_routes
import report
import record
import rules
import scope
import snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "ingest", "data", "ads.sqlite3")
SCHEMA = os.path.join(HERE, "schema_v2.sql")
REPORTS = os.path.join(HERE, "reports")


# ------------------------------------------------------------------
# 数据库
# ------------------------------------------------------------------

def connect(path: str = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    _ensure_columns(conn)


# schema_v2.sql 只有 CREATE TABLE IF NOT EXISTS —— 表已经存在时，新加的列不会被建出来。
# 引擎每次跑都要写 gate_reason 等新列，所以这里按 pragma 幂等补一次。
# （工作台侧 adsdesk/api/migrate.py 有同一份清单，两边都补是为了「谁先跑都不会炸」。）
_DECISION_EXTRA_COLUMNS = (
    ("executed_by", "TEXT"),
    ("attribution_due_at", "TEXT"),
    ("gate_reason", "TEXT"),
    ("override_by", "TEXT"),
    ("override_reason", "TEXT"),
    ("overridden_at", "TEXT"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_log)")}
    added = []
    for name, decl in _DECISION_EXTRA_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE decision_log ADD COLUMN {name} {decl}")
            added.append(name)
    if added:
        conn.commit()


def start_run(conn, window_start, window_end, rules_enabled) -> int:
    cur = conn.execute(
        """INSERT INTO engine_run (started_at, window_start, window_end, status,
                                   rules_enabled, decisions, suppressed)
           VALUES (?,?,?,'running',?,0,0)""",
        (_now(), window_start, window_end, ",".join(rules_enabled)),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, decisions=0, suppressed=0, error=None, note=None):
    conn.execute(
        """UPDATE engine_run SET finished_at=?, status=?, decisions=?, suppressed=?,
                                 error=?, note=?
           WHERE run_id=?""",
        (_now(), status, decisions, suppressed, error, note, run_id),
    )
    conn.commit()


def save_decisions(conn, run_id: int, decisions) -> dict:
    """写进决策账本。同一个 run 里 rule+entity+action 相同的不重复写。

    返回 {"inserted": n, "ids": {occurrence_key: decision_id}}，并把 decision_id
    回填到每个 Decision 对象上 —— 工作台打钩、agent 反对都要用这个 id 定位。
    occurrence_key 用 (rule_id, entity_type, entity_id, action_type) 元组。
    """
    # 先清理再写：把历史重复收掉、过期行收掉，之后「沿用原 decision_id」沿用到的
    # 就是最早那条 —— 它记的才是「这件事从哪天开始等你」。反过来的话，收尾在最后跑，
    # 循环里看到的最新一条会被留着，而更早那条被当作重复清掉，早期等待时间就丢了。
    _expire_stale_proposals(conn, run_id)

    n = 0
    reused = 0
    ids: dict[tuple, int] = {}
    for d in decisions:
        row = d.to_row()
        row["run_id"] = run_id
        key = (row["rule_id"], row["entity_type"], row["entity_id"], row["action_type"])
        # 同一个 (规则, 对象, 动作) 在账本里只允许有一条「还在等你」的行。
        #
        # 原来只查 run_id=?，等于「每次跑引擎都重新问一遍同一件事」：11-19 一天之内
        # R32 因为改了两版文案就长出 #177 与 #182 两条一模一样的 proposed，
        # 双休日没人看的话就是三条。observing 早就是跨 run 复用的（_upsert_observing），
        # 待批反而没有 —— 这是不对称的：越是要人动手的东西，越不该越堆越多。
        #
        # 复用而不是「旧的置 expired、再插一条」的理由：主人的待办清单里不应该出现
        # 「同一条建议的前一版」。decision_id 保持第一次问你的那个，decided_at 也保持
        # 第一次问你的时间 —— 那是「这件事从哪天开始等你」的唯一凭据。
        # 但内容必须是最新算出来的（价格、门槛、证据都会变），所以就地刷新，
        # 并把刷新次数与最后一次 run 记进 context_json，让「改过几版」可查。
        exists = conn.execute(
            """SELECT decision_id FROM decision_log
               WHERE run_id=? AND rule_id=? AND entity_type=? AND entity_id=? AND action_type=?""",
            (run_id, row["rule_id"], row["entity_type"], row["entity_id"], row["action_type"]),
        ).fetchone()
        if exists:
            d.decision_id = exists[0]
            ids[key] = exists[0]
            continue
        live = conn.execute(
            """SELECT decision_id, context_json FROM decision_log
               WHERE status='proposed' AND rule_id=? AND entity_type=? AND entity_id=?
                 AND action_type=?
               ORDER BY decision_id DESC LIMIT 1""",
            (row["rule_id"], row["entity_type"], row["entity_id"], row["action_type"]),
        ).fetchone()
        if live:
            # 取**新**的 context 再补两个血缘字段，不是拿旧的改。
            # 拿旧 context 原地改，等于「刷新」只刷新了 current_value 和 rationale，
            # 新加的字段（比如 cost_ceiling.bid）永远进不了库 —— 而这条路径
            # 一旦命中就再也不会走 insert，那个字段就永久缺失。
            try:
                _ctx = json.loads(row.get("context_json") or "{}")
            except (ValueError, TypeError):
                _ctx = {}
            if not isinstance(_ctx, dict):
                _ctx = {}
            try:
                _old = json.loads(live["context_json"] or "{}")
            except (ValueError, TypeError):
                _old = {}
            _ctx["revisions"] = int((_old or {}).get("revisions") or 1) + 1
            _ctx["last_run_id"] = run_id
            # 刷新：内容换新，身份（decision_id / decided_at）不动。
            #
            # cooldown_until 必须一起刷新。原来这条 UPDATE 的 SET 子句里没有它，
            # 于是只有「第一次 INSERT」才写得进冷却期；之后每次复用都把提案行上的
            # 冷却时间原样留在库里。而 `adsdesk/api/routes/approve.py` 的 mark_executed
            # 是拿「这行有没有 cooldown_until」判断「这类动作受不受频控管」的 ——
            # 复用路径一旦把新值丢掉，那条判据就永远为假，冷却永不生效。
            conn.execute(
                """UPDATE decision_log SET
                       run_id=?, current_value=?, proposed_value=?, confidence=?,
                       context_json=?, calculation_json=?, significance_json=?,
                       rationale=?, spend_impact=?, expires_at=?, gate_reason=?,
                       cooldown_until=?
                   WHERE decision_id=?""",
                (run_id,
                 row.get("current_value"), row.get("proposed_value"), row.get("confidence"),
                 json.dumps(_ctx, ensure_ascii=False, default=str),
                 row.get("calculation_json"), row.get("significance_json"),
                 row.get("rationale"), row.get("spend_impact"), row.get("expires_at"),
                 row.get("gate_reason"), row.get("cooldown_until"), live["decision_id"]),
            )
            d.decision_id = live["decision_id"]
            ids[key] = live["decision_id"]
            reused += 1
            continue
        cols = ", ".join(row.keys())
        ph = ", ".join("?" * len(row))
        cur = conn.execute(f"INSERT INTO decision_log ({cols}) VALUES ({ph})", tuple(row.values()))
        d.decision_id = cur.lastrowid
        ids[key] = cur.lastrowid
        n += 1
    conn.commit()
    return {"inserted": n, "reused": reused, "ids": ids}


def _expire_stale_proposals(conn, run_id: int) -> int:
    """把「有效期过了、还挂着 proposed」的行收掉。

    每行都带 expires_at（_expires() 给 3 天）。过了这个点它既不该继续占着待批队列，
    也不该被当成「主人否决了」—— 主人根本没看见它。所以置 expired 而不是 rejected，
    reject_reason_code 用 expires_unanswered，读的人一眼能分清「被拒绝」与「过期没人管」。

    口径修正（2026-11-23）：原来只比 `substr(expires_at,1,10) < today`，也就是
    「今天零点前就过期」的才收。这条漏掉了一类真僵尸：今天上午过期的行 —— 它已经
    失效、又不在今天这份奏折里（今天没重新提案），却还占着 proposed 队列，主人
    打开审批台看到一条点不动的建议。decision 177（expires_at 2026-11-23T09:34:43，
    run #114 的 pack 里 front/observing/alerts 三处都没有它）就是这么活下来的。
    现在改成比完整时间戳：只要 expires_at 已经过去就收。
    """
    # 与 rules._now() 同一口径（CST aware，带 +08:00），否则新写的 expires_at
    # 与这里比较用的 now 一个带后缀一个不带，字符串直比会漏掉「刚刚过期」的行。
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    cur = conn.execute(
        """UPDATE decision_log
              SET status='expired',
                  reject_reason='有效期过了，没人表态（不是否决）',
                  reject_reason_code='expires_unanswered'
            WHERE status='proposed'
              AND expires_at IS NOT NULL
              AND expires_at <= ?""",
        (now,),
    )
    n = cur.rowcount or 0
    if n:
        print(f"[账本] {n} 条待批有效期已过，置 expired（没表态 ≠ 否决）", flush=True)
    return n + _collapse_duplicate_proposals(conn)


def _collapse_duplicate_proposals(conn) -> int:
    """同一个 (规则, 对象, 动作) 有多条 proposed 时，只留最早的那条。

    这是给「跨 run 复用」上线前长出来的历史数据收尾：修 #177/#182 之前，每跑一次
    引擎就多一条待批，同一条建议在工作台上排成两行、三行。留**最早**那条 ——
    decided_at 记的是「这件事从哪天开始等你」，那是主人唯一能在意的时间；
    它的数字会在下面被就地刷新成最新一版，所以留旧行不丢新信息。
    其余置 expired，reject_reason_code 用 superseded（与 expires_unanswered 分开：
    那是「没人看」，这是「被新版本取代」，原因不同）。

    只在有多条时动手；正常运行时每条 key 最多一条 proposed，这个函数一次都不写。
    """
    rows = conn.execute(
        """SELECT rule_id, entity_type, entity_id, action_type, COUNT(*) c, MIN(decision_id) keep
             FROM decision_log WHERE status='proposed'
            GROUP BY rule_id, entity_type, entity_id, action_type
           HAVING c > 1"""
    ).fetchall()
    n = 0
    for r in rows:
        cur = conn.execute(
            """UPDATE decision_log
                  SET status='expired',
                      reject_reason='同一条建议有更新的版本，这一行被取代',
                      reject_reason_code='superseded'
                WHERE status='proposed' AND rule_id=? AND entity_type=? AND entity_id=?
                  AND action_type=? AND decision_id<>?""",
            (r["rule_id"], r["entity_type"], r["entity_id"], r["action_type"], r["keep"]),
        )
        n += cur.rowcount or 0
    if n:
        print(f"[账本] {n} 条重复待批已合并（同一建议只留最早一条，内容用最新一版）", flush=True)
    return n



# 「先观察、不执行」的有效期（天）。
# 只查当天是错的：11-16 批的先观察，11-17 就失效了，同一对象会重新进待批。
# 观察期长度取一个归因窗口，到期后重新评估 —— 要么数据变了、要么该重新问一次。
OBSERVE_HOLD_DAYS = facts.DEFAULT_WINDOW_DAYS


def hold_recent_observe(conn, pack):
    """观察期内的「先观察、不执行」对象，不再进待批正文。

    有效期按 OBSERVE_HOLD_DAYS 算，不是「只看当天」。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=OBSERVE_HOLD_DAYS)).strftime("%Y-%m-%d")
    kept, held = [], []
    for d in pack.front:
        row = conn.execute(
            """SELECT checked_at, reject_reason, action_type, rule_id FROM decision_log
               WHERE entity_type=? AND entity_id=?
                 AND status='rejected' AND reject_reason_code='observe_no_execute'
                 AND substr(checked_at, 1, 10) >= ?
               ORDER BY checked_at DESC LIMIT 1""",
            (d.entity_type, d.entity_id, cutoff),
        ).fetchone()
        # 同一个对象被不同规则同时看上是常态：R01 想说「降 20% bid」，
        # R32 想说「你已经花掉 4 单的钱」。主人 11-16 批的「先观察」是针对
        # R01 那个问题，不代表他同意 R32 这个问题也先观察 —— 拿旧答复按住
        # 新问题，等于用「bid 先别动」回答「这笔钱还要不要花」。
        # 所以只压住**同一条规则**对同一个对象的重复提问。
        if row and row["checked_at"] and row["rule_id"] != d.rule_id:
            d.rationale = (d.rationale or "") + (
                f"（{str(row['checked_at'])[:10]} 你已就同一个对象批过 {row['rule_id']} 的"
                f"「先观察、不执行」；那是另一个问题，这条是新问题，所以仍然列出来。）"
            )
            row = None
        if row and row["checked_at"]:
            checked_day = str(row["checked_at"])[:10]
            if checked_day == today:
                d.rationale = (d.rationale or "") + "；今日已批先观察、不执行，本轮不再作为待批。"
            else:
                d.rationale = (d.rationale or "") + (
                    f"；{checked_day} 已批先观察、不执行，观察期至 "
                    f"{(datetime.strptime(checked_day, '%Y-%m-%d') + timedelta(days=OBSERVE_HOLD_DAYS)).strftime('%Y-%m-%d')}，"
                    f"期间不再作为待批。"
                )
            d.held = True   # 账本行在旧 run 里，本轮不重复落库
            held.append(d)
        else:
            kept.append(d)
    pack.front = kept
    pack.observing = list(pack.observing) + held
    if held:
        pack.notes.append(f"{len(held)} 条观察期内已批「先观察、不执行」，不重复进待批。")

    # below 里的对象不挪位置（它们本来就在「只观察」区），但标注已批观察期，
    # 免得每天重复看到同一条却不记得已经批过。
    marked = 0
    for d in pack.below:
        row = conn.execute(
            """SELECT checked_at FROM decision_log
               WHERE entity_type=? AND entity_id=?
                 AND status='rejected' AND reject_reason_code='observe_no_execute'
                 AND substr(checked_at, 1, 10) >= ?
               ORDER BY checked_at DESC LIMIT 1""",
            (d.entity_type, d.entity_id, cutoff),
        ).fetchone()
        if row and row["checked_at"]:
            checked_day = str(row["checked_at"])[:10]
            until = (datetime.strptime(checked_day, "%Y-%m-%d")
                     + timedelta(days=OBSERVE_HOLD_DAYS)).strftime("%Y-%m-%d")
            d.rationale = (d.rationale or "") + (
                f"；{checked_day} 已批先观察，观察期至 {until}，期间不重复提案。"
            )
            marked += 1
    if marked:
        pack.notes.append(f"另有 {marked} 条未达判定线的对象处于已批观察期，不重复提案。")
    pack.saved = sum(d.spend_impact for d in pack.front if d.spend_impact and d.spend_impact > 0)
    pack.spent = sum(-d.spend_impact for d in pack.front if d.spend_impact and d.spend_impact < 0)
    # questions that pointed at held items should drop
    held_ids = {(d.rule_id, d.entity_id, d.action_type) for d in held}
    new_qs = []
    for q in pack.questions:
        ref = q.get("decision_ref") or ""
        parts = ref.split(":")
        if len(parts) >= 3 and (parts[0], parts[1], parts[2]) in {(a, b, c) for a, b, c in held_ids}:
            continue
        # decision_ref format rule:entity:action; entity may contain ':' (placement)
        skip = False
        for d in held:
            if q.get("decision_ref") == f"{d.rule_id}:{d.entity_id}:{d.action_type}":
                skip = True
                break
        if not skip:
            new_qs.append(q)
    pack.questions = new_qs
    return pack


# ------------------------------------------------------------------
# 证据闸门：不够线的动作也落库，但标 observing —— 引擎的诚实不打折，
# 人的决定权也不缺（工作台可显式二次确认，走 override 并留痕）。
# ------------------------------------------------------------------

def gate_reason(d) -> str:
    """把「引擎为什么按住这条」翻成人话。

    只描述引擎自己看到的证据，不评价人、不承诺「批了就赚」。
    """
    if getattr(d, "held", False):
        return "你在观察期内批过「先观察、不执行」，本轮不再作为待批"
    sig = d.significance or {}
    basis = str(sig.get("confidence_basis") or "").strip()
    if d.action_type == "observe":
        return f"证据不够：{basis}" if basis else "证据不够：规则算不出可执行的最优值，只标观察"
    if basis:
        return f"证据不够、未达判定线：{basis}"
    clicks, need = sig.get("clicks"), sig.get("min_clicks_needed")
    if clicks is not None and need:
        return f"证据不够、未达判定线：{clicks} 击 < 判定线 {need} 击"
    return "证据不够：样本未达判定线"


def _upsert_observing(conn, run_id: int, d) -> str:
    """observing 的落库规则与待批不同：跨 run 复用同一行。

    待批是「今天的新建议」，一天一行没问题；observing 是「同一批对象一直不够线」，
    每天插一行会让账本膨胀，而且人 override 掉昨天那条之后今天又冒出一条新的
    —— 那正是 hold_recent_observe 想避免的重复提案。

    按 (rule_id, entity_type, entity_id, action_type) 找最近一条：
      observing            → 复用，刷新证据快照（gate_reason/spend_impact 会随数据变）
      approved / executed  → 人已经批过了，跳过（不该再出现在观察区）
      rejected             → 人已经表过态（含「先观察」），跳过
      其它（expired 等）   → 重新提案，插新行

    返回 "inserted" | "reused" | "settled"。
    """
    row = conn.execute(
        """SELECT decision_id, status FROM decision_log
           WHERE rule_id=? AND entity_type=? AND entity_id=? AND action_type=?
           ORDER BY decision_id DESC LIMIT 1""",
        (d.rule_id, d.entity_type, d.entity_id, d.action_type),
    ).fetchone()
    if row is not None:
        d.decision_id = row["decision_id"]
        if row["status"] == "observing":
            conn.execute(
                """UPDATE decision_log
                      SET run_id=?, decided_at=?, gate_reason=?, significance_json=?,
                          calculation_json=?, rationale=?, spend_impact=?, confidence=?,
                          current_value=?, proposed_value=?, status='observing'
                    WHERE decision_id=?""",
                (
                    run_id, datetime.now().isoformat(timespec="seconds"), d.gate_reason,
                    json.dumps(d.significance or {}, ensure_ascii=False, default=str),
                    json.dumps(d.calculation or [], ensure_ascii=False, default=str),
                    d.rationale, d.spend_impact, d.confidence,
                    d.current_value, d.proposed_value,
                    row["decision_id"],
                ),
            )
            return "reused"
        if row["status"] == "approved":
            # 已强制批准、但还没点「已改」。不能从观察区剔除 ——
            # 引擎每天只跑一次，一旦剔除，跨天后这条就从页面上彻底消失，
            # 人再也找不到它去标「已改」，归因窗永远不开。
            # 这里也不刷新证据快照：它停在批准那一刻，正是复盘时要看的东西。
            return "reused"
        if row["status"] in ("executed", "rejected"):
            return "settled"

    r = d.to_row()
    r["run_id"] = run_id
    cols = ", ".join(r.keys())
    ph = ", ".join("?" * len(r))
    cur = conn.execute(f"INSERT INTO decision_log ({cols}) VALUES ({ph})", tuple(r.values()))
    d.decision_id = cur.lastrowid
    return "inserted"


def save_observing(conn, run_id: int, pack) -> dict:
    """把「不够线」与「只看不动」两类落成 status='observing'，并从观察区剔除已表过态的。

    held（观察期内已批先观察）只做展示、不落库 —— 它的账本行在旧 run 里。
    """
    stat = {"inserted": 0, "reused": 0, "settled": 0, "held": 0}

    def handle(d) -> bool:
        if getattr(d, "held", False):
            d.status = "observing"
            d.gate_reason = gate_reason(d)
            stat["held"] += 1
            return True
        d.status = "observing"
        d.gate_reason = gate_reason(d)
        verdict = _upsert_observing(conn, run_id, d)
        stat[verdict] += 1
        return verdict != "settled"

    pack.observing = [d for d in pack.observing if handle(d)]
    pack.below = [d for d in pack.below if handle(d)]
    # 收掉「上一轮观察、这一轮不再出现」的僵尸行。
    #
    # observing 是状态不是事件，_upsert_observing 故意跨 run 复用同一行，所以它
    # 从来不被 _expire_stale_proposals 收（那条 SQL 只认 status='proposed'）。
    # 一旦某个对象这轮不再落进 pack.observing / pack.below（它有了决策、掉出范围、
    # 或换了四元组），那行就变成：不在奏折里、approve.py 的强制批准拿不到 id
    # （人只有从 pack 里才看得到 decision_id）、v_open_decisions 也不含 observing。
    # 结果是它永不结算、也永不消失，还占着 (rule_id, entity_type, entity_id,
    # action_type) 让 _upsert_observing 一直复用同一行。2026-11-26 实测：
    # 库里 observing 有 154/155/156/173/174/207/227 七条，run #123 的 pack 里只有
    # 155/174/226/227 —— 差集 154/156/173/207 就是四条看不见的僵尸。
    #
    # 判据用「本轮出现过没有」（decision_id 白名单），不用 expires_at：observing 行的
    # expires_at 是首次写入时的值、之后从不更新（#155 的 decided_at 是 11-26、
    # expires_at 还是 11-22），拿它判会把正在用的 155/174 一起收掉。
    # 也不用 run_id：held（观察期内已批「先观察」）本轮在 pack 里出现但不落库，
    # 它的 run_id 停在旧 run 上，按 run_id 判会把这些「人已经批过的」误收成过期。
    #
    # 只在**本轮确实写了观察行**时才扫：一轮零条 observing，更可能是数据没追平
    # 或规则集体没跑到，而不是「全都解决了」。那种时候把整张表扫成 expired，
    # 正是我们一直在修的那种静默伤害。
    seen_ids = [d.decision_id for d in (list(pack.observing) + list(pack.below))
                if getattr(d, "decision_id", None)]
    swept = 0
    if stat["inserted"] + stat["reused"] > 0 and seen_ids:
        marks = ", ".join("?" * len(seen_ids))
        cur = conn.execute(
            f"""UPDATE decision_log
                   SET status='expired',
                       reject_reason='观察区里已经不再出现了（不是否决）',
                       reject_reason_code='observe_dropped'
                 WHERE status='observing'
                   AND decision_id NOT IN ({marks})""",
            tuple(seen_ids),
        )
        swept = cur.rowcount or 0
        if swept:
            print(f"[账本] {swept} 条观察行本轮已不再出现，置 expired（不是否决）", flush=True)
    conn.commit()
    if stat["settled"]:
        pack.notes.append(f"另有 {stat['settled']} 条不够线的对象已在账本里表过态，不再重复列出。")
    return stat


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------


def _dedupe_alerts(conn, alerts) -> set:
    """同一天、同一结论的账户级告警不再重复写账本。

    R30 每天算的是同一句「全店目标不可达」。结论没变就没有新信息，每天重写一遍
    只会让账本长出一串「已通知」噪声，也让读的人以为每天都有新告警。
    判据用 rationale 指纹（正文一模一样就是同一句话）。

    **注意：不把告警从 pack 里删掉。** 工作台要显示它、诊断 agent 要看见它才能反对它，
    删了就等于把「今天唯一一条带 id 的记录」也藏起来。这里只做两件事：
      1. 重复的，把已有 decision_id 回填到对象上（工作台仍能引用）
      2. 返回要跳过的对象 id 集合，交给 save_decisions 时过滤

    **为什么还要回填 alert_inputs**：R30 判断「今天和上次是不是同一张纸」靠的是
    上一行 `context_json.alert_inputs`。而历史行是这条改动之前写的，里面没有这个键 ——
    于是 `_same` 永远为 False，永远走完整版，完整版又和旧行逐字相同，被这里跳过。
    结果指纹永远写不进去，短版永远不出现。这是个死锁，不是收敛慢。
    解法：跳过时顺手把今天的指纹补进那一行（只补这一个键，不动别的）。
    """
    if not alerts:
        return set()
    today = _dt.date.today().isoformat()
    dup_ids: set = set()
    for d in alerts:
        fp = (d.rationale or "")[:2000]
        row = conn.execute(
            """select decision_id, rationale, context_json from decision_log
                where rule_id=? and entity_type=? and action_type=?
                  and substr(decided_at,1,10)=?
                order by decision_id desc limit 1""",
            (d.rule_id, d.entity_type, d.action_type, today)).fetchone()
        if row and (row["rationale"] or "")[:2000] == fp:
            d.decision_id = row["decision_id"]
            dup_ids.add(id(d))
            _backfill_alert_inputs(conn, row, d)
    return dup_ids


def _backfill_alert_inputs(conn, row, d) -> None:
    """把今天的输入指纹补进那条已存在的告警行（只在缺失时补）。"""
    want = (d.context or {}).get("alert_inputs")
    if not want:
        return
    try:
        ctx = json.loads(row["context_json"] or "{}")
    except (ValueError, TypeError):
        return
    if ctx.get("alert_inputs") == want:
        return
    ctx["alert_inputs"] = want
    conn.execute("update decision_log set context_json=? where decision_id=?",
                 (json.dumps(ctx, ensure_ascii=False), row["decision_id"]))


def save_memorial(conn, run_id: int, business_day: str, payload: dict,
                  report_path: str | None = None) -> None:
    """把今天的 pack_json 存进 memorial，供工作台与 agent 读。

    为什么不塞进 engine_run：engine_run 是运行记录（状态、耗时、错误），
    塞 30KB JSON 会让「查上次跑成功没」这种查询变慢，也让表语义混乱。
    memorial 一 run 一行，business_day 便于按日检索与幂等重跑。
    """
    conn.execute(
        """INSERT INTO memorial (run_id, business_day, pack_json, report_path,
                                 agent_state, created_at)
           VALUES (?,?,?,?, 'pending', ?)
           ON CONFLICT(run_id) DO UPDATE SET
               business_day=excluded.business_day,
               pack_json=excluded.pack_json,
               report_path=excluded.report_path""",
        (run_id, business_day, json.dumps(payload, ensure_ascii=False, default=str),
         report_path, _now()),
    )
    conn.commit()


def _midship_engine_done(conn, run_id, f, pack, report_path) -> None:
    """引擎跑完后写事件并交给中台消费。失败不回滚建议。"""
    try:
        root = os.path.dirname(HERE)
        if root not in sys.path:
            sys.path.insert(0, root)
        from midship.hooks import safe_engine_done
        out = safe_engine_done(
            conn,
            run_id=run_id,
            window_start=f.start,
            window_end=f.end,
            front=len(pack.front),
            questions=len(pack.questions),
            report_path=report_path,
        )
        rec = (out or {}).get("write") or {}
        consumed = (out or {}).get("consumed") or []
        flag = "新写入" if rec.get("inserted") else "已存在"
        print(f"[中台] engine.done {flag} #{rec.get('event_id')}，消费 {len(consumed)} 条", flush=True)
    except Exception as e:
        print(f"[中台] 收尾失败（引擎已跑完）：{e}", flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="广告规则引擎")
    p.add_argument("--db", default=DB)
    p.add_argument("--window-days", type=int, default=facts.DEFAULT_WINDOW_DAYS)
    p.add_argument("--target-acos", type=float, default=facts.DEFAULT_TARGET_ACOS)
    p.add_argument("--exclude-days", type=int, default=facts.DEFAULT_ATTR_EXCLUDE_DAYS)
    p.add_argument("--confidence", type=float, default=facts.DEFAULT_CONFIDENCE)
    p.add_argument("--dry-run", action="store_true", help="只出报告，不写库")
    p.add_argument("--no-snapshot", action="store_true", help="跳过配置快照")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    def say(*a):
        if not args.quiet:
            print(*a)

    conn = connect(args.db)
    if not args.dry_run:
        ensure_schema(conn)

    # 1) 快照 + diff
    snap_info = diff_info = None
    if not args.no_snapshot:
        try:
            snap_info = snapshot.take_snapshot(conn)
            diff_info = snapshot.diff_snapshots(conn)
            # 报观测时刻而不是报表日期：同一天可能有多个观测点，
            # 只报日期看不出「这是当天第几次观测」，也看不出两次比较的先后
            say(f"[快照] 报表日 {snap_info['snap_date']} 观测于 {snap_info.get('snap_ts')} "
                f"共 {snap_info['rows']} 行"
                f"{'（已存在，未重写）' if snap_info.get('written') == 0 else ''}；"
                f"变更 {diff_info.get('changes', 0)} 处 "
                f"({diff_info.get('from_snap')} → {diff_info.get('to_snap')}，"
                f"{'可信' if diff_info.get('reliable') else '同一时刻抓取，仅记录'})")
        except Exception as e:  # 快照失败不该拖垮规则
            say(f"[快照] 跳过：{e}")

    # 2) 事实层
    # 先把打钩沉淀下来的参数覆盖读出来：主人说过「目标放宽到 X」「加预算幅度小一点」，
    # 若不在这一层生效，那些钩子就只是躺在表里的一条记录，回路仍然是断的。
    #
    # 读侧也要卡区间（不是只有写侧卡）：库里可能已经有历史脏值 ——
    # 老版本把「2 周后放到 40%」写成过 0.02，把「放宽到 500%」写成过 5.0。
    # 写侧修好只保证以后不写坏，已经坏掉的必须靠这里兜住。
    _ov = reason_routes.overrides(conn, scope.SCOPE_ASIN)
    _target = args.target_acos
    _raw = _ov.get("target_acos")
    if _raw not in (None, ""):
        try:
            _v = reason_routes._clamp("target_acos", float(_raw))
        except (TypeError, ValueError):
            _v = None
            say(f"[沉淀] target_acos 覆盖值不是数字（{_raw!r}），按命令行默认值算")
        if _v is None:
            say(f"[沉淀] target_acos 覆盖值 {_raw!r} 超出可接受区间 "
                f"{reason_routes.PARAM_BOUNDS['target_acos']}，忽略（按命令行默认值算）")
        else:
            _target = _v
    _step = facts.DEFAULT_BUDGET_STEP
    _raw = _ov.get("max_budget_step")
    if _raw not in (None, ""):
        try:
            _v = reason_routes._clamp("max_budget_step", float(_raw))
        except (TypeError, ValueError):
            _v = None
            say(f"[沉淀] max_budget_step 覆盖值不是数字（{_raw!r}），按默认 ×1.2 算")
        if _v is None:
            say(f"[沉淀] max_budget_step 覆盖值 {_raw!r} 超出可接受区间 "
                f"{reason_routes.PARAM_BOUNDS['max_budget_step']}，忽略（按默认 ×1.2 算）")
        else:
            _step = _v
    if _target != args.target_acos or _step != facts.DEFAULT_BUDGET_STEP:
        say(f"[沉淀] 参数覆盖生效：目标 ACOS {report._pct(args.target_acos)} → "
            f"{report._pct(_target)}；加预算步长 ×{_step}")

    f = facts.build_facts(conn, window_days=args.window_days,
                          target_acos=_target,
                          exclude_days=args.exclude_days,
                          confidence=args.confidence,
                          budget_step=_step)
    a = f.account
    say(f"[事实] {f.start}~{f.end} | 花 {report._money(a['cost'])} / {a['clicks']} 击 / "
        f"{a['orders']} 单 / ACOS {report._pct(a['acos'])} / CVR {report._pct(a['cvr'])}")
    say(f"[事实] 盈亏平衡 CVR {report._pct(f.breakeven.get('breakeven_cvr'))} → "
        f"目标{'可达' if f.target_reachable else '不可达'}；"
        f"判定线 {report._line_short(f)}；"
        f"CPC 系数 {f.cpc_bias.get('overall'):.3f}；地板 {report._money(f.floor.get('floor'))}")

    # 2.5) 规则战绩：把归因结果读回来。这一步不做，attribute.py 写的结论
    # 就永远只有人翻驾驶舱时才看得到，引擎自己提案时不会因此谨慎一分。
    f.record = record.build(conn)
    _rt = f.record.get("totals") or {}
    if _rt.get("judged"):
        say(f"[战绩] 近 {f.record['since_days']} 天 {_rt['judged']} 次有结论的执行："
            f"好 {_rt['improved']} / 差 {_rt['worsened']} / 平 {_rt['flat']}")

    # 3) 规则
    res = rules.run_all(f)
    _n_rec = record.annotate(res.decisions, f.record)
    say(f"[规则] {len(res.decisions)} 条建议，{len(res.suppressed)} 条被保护拦下"
        + (f"，{_n_rec} 条附了历史战绩" if _n_rec else ""))

    # 4) 奏折压缩：不够线的不占名额；只把正文建议写入待批台账
    # 到期的观察任务/实验先结案标记：不结的话「观察期」会悄悄变成「永远忘了」。
    # 引擎不替人下结论，只把到期项标成 result='due' 并写进奏折备注。
    # 放在 compress 之前，是为了让奏折生成时看到的库状态已经包含「今天结案了哪几条」；
    # 出题去重本身另有日期过滤（active_codes 只认 due_at >= today），不靠这一步。
    # dry-run 不写库：mark_due 会改 result/resolved_at，试跑不能留下痕迹。
    _due = ({"tasks": 0, "experiments": 0} if args.dry_run
            else reason_routes.mark_due(conn, object_id=scope.SCOPE_ASIN))
    # conn 传进去是为了「有效期内不再问」：已经沉淀过的原因码不重复出题。
    pack = report.compress(f, res, conn=conn)
    if _due["tasks"] or _due["experiments"]:
        pack.notes.append(
            f"到期观察任务 {_due['tasks']} 条、实验 {_due['experiments']} 条已结案待判："
            "按判据给结论 —— 成立升级为策略（带已验证标记），不成立就反向修正并提高同类再问权重。")
    pack = hold_recent_observe(conn, pack)
    # 同一对象反复改坏的，本届不进待批（挪进观察区，仍可强制批准）。
    _braked = record.apply(pack, f.record)
    if _braked:
        say(f"[战绩] {len(_braked)} 条被「这个对象过去改坏过」挡在待批之外")
    say(f"[奏折] 正文 {len(pack.front)} 条，溢出 {len(pack.overflow)}，不够线 {len(pack.below)}，"
        f"问题 {len(pack.questions)}，结构冲突 {len(pack.conflicts)}")

    # 先把「引擎为什么按住这条」写进对象本身 —— dry-run 的奏折也要能读到理由，
    # 否则「证据不够」四个字在报告里永远是一句没有下文的话。
    for _d in list(pack.observing) + list(pack.below):
        _d.status = "observing"
        _d.gate_reason = gate_reason(_d)

    run_id = None
    if not args.dry_run:
        run_id = start_run(conn, f.start, f.end, rules.RULE_ORDER)
        # 状态行（R30 输入未变）不写账本 —— 先挑出来，别被下面的 notified 覆盖。
        #
        # 诊断 agent 第 145 号反对（第六轮打 R30）：「没人动目标 ACOS，它就每天重算
        # 同一结论、永远等不到自愈，今天不携带任何新信息。」——它连问六轮之后，
        # 定性终于清楚了：这条不是事件，是状态。事件才进账本，状态进 pack 就够。
        # 记进去的后果是 agent 自己都看出来了：R30 天天新增、而 front 恒为 0，
        # 读的人会以为系统在「持续报警」，其实只是同一张纸被重新打印了一遍。
        states = [d for d in pack.alerts if d.status == "state"]
        # 账户级告警（R30）没有可打钩的对象，工作台里无法审批。
        # 若按 proposed 写入，每跑一次就多一条僵尸（11-17 已清过 5 条 #85–#89）。
        # 改成 notified：仍然留痕在账本里，但不进待批队列。
        for d in pack.alerts:
            if d.status != "state":
                d.status = "notified"
        # 同一天重复跑时，R30 每天都会再写一条一模一样的内容（run 36–41 就是同一天六条）。
        # 它没有可审批对象、不占配额，重复写只会让账本里长出一串「已通知」噪声，
        # 也让读的人以为「每天都有新告警」。判据用 conclusion 指纹：
        # 全店 breakeven / 实际 CVR / gap 都没变，就是同一句话，不再写第二条。
        dup_alerts = _dedupe_alerts(conn, pack.alerts)
        fresh_alerts = [d for d in pack.alerts if id(d) not in dup_alerts]
        fresh_alerts = [d for d in fresh_alerts if d.status != "state"]
        saved = save_decisions(conn, run_id, fresh_alerts + pack.front)
        n = saved["inserted"]
        # 不够线的动作也要有账本行 —— 否则工作台里没有 decision_id 可引用，
        # 人就永远只能在「等证据」和「凭感觉」之间二选一。
        # 它们落成 status='observing'：不进待批队列、不进 v_open_decisions，
        # 只在「只观察」区可见，且必须显式二次确认（override）才能变 approved。
        obs_stat = save_observing(conn, run_id, pack)
        n_observing = obs_stat["inserted"] + obs_stat["reused"]
        finish_run(conn, run_id, "ok", decisions=n, suppressed=len(res.suppressed),
                   note=f"window={f.window_days}d target_acos={f.target_acos} memorial={len(pack.front)}")
        # 别把告警算进「待批」——R30 是 notified，工作台里点不了，也改不了任何东西。
        # 原来这句写「写入 N 条待批（正文+告警）」，正文 0 条时也会显示「写入 1 条待批」，
        # 读的人会以为有一条在等他打钩。诊断 agent #130 正是据此说「告警把正文挤成 0 条」。
        n_front = len(pack.front)
        n_alert = len(pack.alerts) - len(states)
        if saved.get("reused"):
            say(f"[账本] {saved['reused']} 条是同一件事的刷新（沿用原 decision_id，没有新增）")
        say(f"[落库] run #{run_id}：待批 {n_front} 条"
            + (f"，另有 {n_alert} 条账户级告警（只留痕，不可审批）" if n_alert else "")
            + (f"，{len(states)} 条状态行未变（不进账本）" if states else "")
            + (f"，{n_observing} 条不够线已记账为 observing（可显式强制批准）" if n_observing else "")
            + (f"，{obs_stat['settled']} 条你已表过态、不再重复列出" if obs_stat["settled"] else ""))

    # 5) 奏折
    #
    # --dry-run 不许覆盖真奏折。文件名只由 f.end 决定，而 f.end 是「业务窗口末」——
    # 同一个业务日重跑就落到同一个路径。2026-11-26 实测踩到：用 --dry-run 复算
    # 11-23 那轮，把 2026-11-23-奏折.md/.json 原地覆盖了（原文件是 01:39 真跑写的），
    # 而这两个文件正是给人看的交付物、也是 memorial 里 report_path 指向的东西。
    # 更麻烦的是 dry-run 下 run_id 是 None，覆盖后连「这是哪一轮」都看不出来。
    # 所以 dry-run 一律加 .dryrun 后缀：能看、能 diff，但碰不到真文件。
    os.makedirs(REPORTS, exist_ok=True)
    suffix = ".dryrun" if args.dry_run else ""
    md = report.render_report(f, res, run_id, snap_info, diff_info, pack=pack)
    path = os.path.join(REPORTS, f"{f.end}-奏折{suffix}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    payload = report.pack_json(f, pack, run_id, suppressed=res.suppressed)
    pack_path = os.path.join(REPORTS, f"{f.end}-奏折{suffix}.json")
    with open(pack_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    say(f"[报告] {path}" + ("（dry-run，未覆盖真奏折）" if args.dry_run else ""))

    if not args.dry_run:
        # 落 memorial：工作台与 agent 都读这张表，不读文件
        save_memorial(conn, run_id, f.end, payload, path)
        say(f"[奏折] memorial 已写入 run #{run_id}")
        _midship_engine_done(conn, run_id, f, pack, path)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
