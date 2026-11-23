#!/usr/bin/env python3
"""工作台数据库迁移脚本（幂等）。

做三件事：
1. 建 P1 新增的 7 张表（schema.sql）
2. 给已有表补列（reason_hit.answer_type / reason_hit.decision_id /
   decision_log.executed_by / decision_log.attribution_due_at）
3. 清历史僵尸：78 条 status='expired' 已清过，本轮把 5 条旧 proposed 里的
   R30 告警保留、其余按 legacy 归档（可通过 --no-zombie 跳过）

用法：
    python3 adsdesk/api/migrate.py            # 建表 + 补列 + 清僵尸
    python3 adsdesk/api/migrate.py --dry-run  # 只报告，不动库
    python3 adsdesk/api/migrate.py --no-zombie
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 支持 ADS_DB 重定向，与 api/db.py:24 保持同一套约定。
# 理由：迁移必须在真库上验证，但没人想拿生产库试；没有这个开关，
# 唯一能验证的路径就是「直接改生产库」，那就把可回滚变成了不可回滚。
DB_PATH = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# (表, 列, 列定义) —— 已存在则跳过
ALTERS: list[tuple[str, str, str]] = [
    # ① 原因词典 v1.3 的不确定性公式要数 answer_type='skip'
    ("reason_hit", "answer_type", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("reason_hit", "decision_id", "INTEGER"),
    # ② 执行单号已在 decision_log.ticket_id；补「谁标的已改」与「归因到期日」
    ("decision_log", "executed_by", "TEXT"),
    ("decision_log", "attribution_due_at", "TEXT"),
    # ③ S1 证据闸门：引擎按住的原因 + 人工强制批准的来源
    ("decision_log", "gate_reason", "TEXT"),
    ("decision_log", "override_by", "TEXT"),
    ("decision_log", "override_reason", "TEXT"),
    ("decision_log", "overridden_at", "TEXT"),
    # ④ 把「质疑」变成「可兑现的断言」：原来 agent_objection 只有 verdict（态度），
    #    26 条质疑全是 doubt/oppose、一条都没被判过对错 —— 态度不值钱，可验证才值钱。
    #    outcome 的取值域故意复用 agent_eval 的四值（pending/correct/wrong/unverifiable），
    #    因为「质疑命中率」和「断言命中率」最终要放在同一张表里对比；
    #    两套词表会让「到底该不该继续听 agent 的」这个问题永远算不清。
    ("agent_objection", "predicts_json", "TEXT"),
    ("agent_objection", "verifiable_at", "TEXT"),
    ("agent_objection", "checked_at", "TEXT"),
    ("agent_objection", "outcome", "TEXT NOT NULL DEFAULT 'pending'"),
    ("agent_objection", "actual", "TEXT"),
    ("agent_objection", "judged_by", "TEXT"),
    # ⑤ 收敛 agent 的「提权」出口（计划书 §8.5）：某个 question_code 被判定为
    #    refuted（默认假设被数据打脸）时，把该题在 reason_hit 上的跳过率提上去，
    #    意思是「这题以后别再让我跳过了，我跳就是错」。
    #    没有这一列，refuted 只能写 policy 草案、提权那半边恒被跳过 —— 分支是半截的。
    #    用 REAL 不用 INTEGER：它是 0~1 的比例，不是计数。
    ("reason_hit", "skip_ratio", "REAL"),
]

# 补列补不出来的东西：索引必须先有列才能建，所以要排在 ALTERS 全跑完之后。
# 放在这里而不是 schema.sql 里，是因为 schema.sql 只在「表不存在」时才建表 ——
# 老库上那条 CREATE INDEX 会被整个跳过，due 查询就永远走全表扫。
POST_INDEXES: list[tuple[str, str]] = [
    # 收敛 agent 每轮只查「到期且未判」，这两个字段的顺序就是那个查询的形状
    ("ix_agent_objection_due", "ON agent_objection(verifiable_at, outcome)"),
    # rank_snapshot 原来只有「按词」「按时间」两个普通索引，没有唯一约束。
    # ingest/rank.py 的幂等靠「先查再插 + where not exists」，单进程反复跑没问题
    # （实测第二次 rows_upserted=0），但两个 rank.py 同时跑就有窗口期会插双份 ——
    # 位次快照一旦重复，R31 的「最新一次观测」会随机取到其中一条，加价判定跟着飘。
    # 唯一键取这六个字段：它们合起来才是「同一个词、同一时刻、同一个位次」的身份。
    # body 以 "UNIQUE " 开头即建成唯一索引。原来这里一律写 `create index`，
    # 于是这条唯一约束被静默降级成普通索引（2026-11-23 实测抓到）——
    # 名字叫 ux_ 却拦不住重复，比没有更糟：看名字会以为已经防住了。
    ("ux_rank_snapshot_ident",
     "UNIQUE ON rank_snapshot(keyword_id, observed_at, placement, asin, page, position)"),
]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(conn, table):
        return False
    cols = {r[1] for r in conn.execute(f"pragma table_info({table})")}
    return column in cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-zombie", action="store_true", help="不清历史 proposed")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[错误] 库不存在：{DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=WAL")

    print(f"[库] {DB_PATH}")
    print(f"[库] 迁移前表数 = {conn.execute('select count(*) from sqlite_master where type=\'table\'').fetchone()[0]}")

    # ── 1. 建表 ──────────────────────────────────────────────
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    new_tables = [
        "memorial", "agent_run", "agent_objection", "agent_eval",
        "chat_turn", "exec_order", "exec_order_item",
        # ⑦ 外部市场（P4-23）。cockpit.py:/market 就是按这三个表名探测是否接通的，
        # 改名必须两边一起改，否则面板会永远显示「表还没建」。
        "aba_weekly", "competitor_move", "market_daily",
        # ⑧ 读数快照（2026-11-20）。fact_*_daily 是 upsert 覆盖，不留同日历史读数，
        #    导致「T+1 与 T+3 两次重拉」类断言（如 #74）永远判不了 —— 它需要同一个
        #    (dataset, report_date) 的两次读数，可库里只剩最后一次。
        "fact_read_snapshot",
    ]
    before = {t for t in new_tables if table_exists(conn, t)}
    if args.dry_run:
        print(f"[建表·dry] 将执行 schema.sql；已存在 {sorted(before) or '无'}")
    else:
        conn.executescript(ddl)
        conn.commit()
        after = {t for t in new_tables if table_exists(conn, t)}
        print(f"[建表] 应有 {len(new_tables)} 张，实际存在 {len(after)} 张：{sorted(after)}")
        missing = set(new_tables) - after
        if missing:
            print(f"[错误] 缺表：{sorted(missing)}")
            return 1

    # ── 2. 补列 ──────────────────────────────────────────────
    for table, col, decl in ALTERS:
        if not table_exists(conn, table):
            print(f"[补列·跳过] {table} 不存在")
            continue
        if column_exists(conn, table, col):
            print(f"[补列·已有] {table}.{col}")
            continue
        if args.dry_run:
            print(f"[补列·dry] alter table {table} add column {col} {decl}")
            continue
        conn.execute(f"alter table {table} add column {col} {decl}")
        conn.commit()
        print(f"[补列] {table}.{col} {decl}")

    # ── 2.5 建补列之后才能建的索引 ────────────────────────────
    # 顺序不能反：索引的列必须先存在。老库上 schema.sql 里的 CREATE INDEX
    # 会被 CREATE TABLE IF NOT EXISTS 一起跳过，所以索引只能在这里补。
    for name, body in POST_INDEXES:
        exists = conn.execute(
            "select 1 from sqlite_master where type='index' and name=?", (name,)
        ).fetchone()
        if exists:
            print(f"[索引·已有] {name}")
            continue
        unique = body.upper().startswith("UNIQUE ")
        clause = body[7:].strip() if unique else body
        kind = "unique index" if unique else "index"
        if args.dry_run:
            print(f"[索引·dry] create {kind} if not exists {name} {clause}")
            continue
        conn.execute(f"create {kind} if not exists {name} {clause}")
        conn.commit()
        print(f"[索引] {name} create {kind} {clause}")

    # ── 3. 清僵尸 proposed ───────────────────────────────────
    # 5 条 proposed 全是 R30 全店告警，无 entity_id 可打钩，工作台里无法审批。
    # 处置：置 expired，reject_reason_code='no_actionable_entity'。
    rows = conn.execute(
        "select decision_id, rule_id, action_type, entity_type from decision_log "
        "where status='proposed' order by decision_id"
    ).fetchall()
    print(f"[僵尸] status='proposed' 共 {len(rows)} 条")
    for r in rows:
        print(f"  #{r['decision_id']} {r['rule_id']} {r['action_type']} entity={r['entity_type']}")

    if args.no_zombie:
        print("[僵尸·跳过] --no-zombie")
    elif args.dry_run:
        print(f"[僵尸·dry] 将把 {len(rows)} 条置 expired")
    else:
        n = 0
        # 全店级/账户级告警（entity_type in account|store）与 entity_id 为空的，
        # 工作台里没有可打钩的落点，置 expired。
        ACCOUNT_LEVEL = {"account", "store", ""}
        for r in rows:
            ent = conn.execute(
                "select entity_id from decision_log where decision_id=?",
                (r["decision_id"],),
            ).fetchone()
            entity_id = ent["entity_id"] if ent else None
            if r["entity_type"] in ACCOUNT_LEVEL or not entity_id:
                conn.execute(
                    "update decision_log set status='expired', "
                    "reject_reason_code='no_actionable_entity', "
                    "reject_reason='全店级告警，无具体对象可审批，工作台无法打钩' "
                    "where decision_id=?",
                    (r["decision_id"],),
                )
                n += 1
        conn.commit()
        print(f"[僵尸] 已置 expired {n} 条")

    # ── 4. 汇总 ──────────────────────────────────────────────
    print()
    print("[汇总] 关键表行数：")
    for t in ("memorial", "agent_run", "agent_objection", "agent_eval",
              "chat_turn", "exec_order", "exec_order_item", "decision_log"):
        if table_exists(conn, t):
            c = conn.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"  {t:20s} {c}")
    print()
    st = conn.execute(
        "select status, count(*) c from decision_log group by status order by c desc"
    ).fetchall()
    print("[汇总] decision_log 状态：", {r["status"]: r["c"] for r in st})
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
