#!/usr/bin/env python3
"""【已废弃】把卡位配置写进库。请改用 `ingest/rank.py --dataset target-seed`。

为什么废弃：这个脚本原来直接对生产库 `ingest/data/ads.sqlite3` 执行 insert，
而且 `main("--dry" in sys.argv)` 意味着**不带参数运行就是真写**——
docstring 写着「默认不执行」，代码却是「默认就执行」，两者相反。
另外它把 `market_id` 硬编码成 1，而 1 号店是 ARDXHSA1HUW6V（ads_state=InvalidAuth，
早已失效），三个词实际都属于 7 号店 A29H9NAARHJBNO。

现在保留它只为一件事：如果有人手上有旧参数要复现，能看懂当年写的是什么。
真要种卡位目标，走 `ingest/rank.py --dataset target-seed`，那条路径会：
  · 从 fact_keyword_daily 里按曝光挑词，不用手抄 keyword_id
  · market_id 从 dim_market 取，不写死
  · 按 (market_id, keyword_id) 幂等去重
  · 默认 --dry-run，要真写必须显式 --write

本脚本现在的行为：**只打印，不写库**。要写必须同时给 --write 和 --i-know，
且库路径只能来自 ADS_DB（不再硬编码）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 不再硬编码生产库绝对路径：ADS_DB 没给就拒绝运行。
DB = os.environ.get("ADS_DB") or ""
ASIN = "B0GLYW51HY"
CST = timezone(timedelta(hours=8))

# 三个词实测都属于 market_id=7（A29H9NAARHJBNO / Successfully），不是 1 号旧店。
MARKET_ID = 7

TARGETS = [
    # keyword_id, text, campaign_id, ad_group_id, max_bid, step, guard, max_pushes
    ("21937639215436", "pulldown machine", "46818486452618", "37057323685447", 1.20, 0.10, 1.5, 6),
    ("277840567753078", "lat pulldown", "237761921757002", "201416328459856", 0.80, 0.05, 1.2, 4),
]


def _has_target(conn: sqlite3.Connection, keyword_id: str) -> bool:
    try:
        return conn.execute(
            "select 1 from rank_target where market_id=? and keyword_id=? limit 1",
            (MARKET_ID, keyword_id),
        ).fetchone() is not None
    except sqlite3.Error:
        # 库是空的 / 还没建表：当「不存在」处理，dry 模式下不炸
        return False


def main(dry: bool) -> int:
    if not DB:
        print("[拒绝] 没有 ADS_DB。这个脚本不再自己猜库在哪 ——\n"
              "       要么 ADS_DB=/path/to/copy.sqlite3 python3 seed_rank_target.py，\n"
              "       要么直接用 ingest/rank.py --dataset target-seed（推荐）。")
        return 2
    conn = sqlite3.connect(DB)
    now = datetime.now(CST).isoformat(timespec="seconds")
    n = 0
    for kid, text, camp, adg, max_bid, step, guard, pushes in TARGETS:
        exists = _has_target(conn, kid)
        tag = "[已存在·跳过] " if exists else ("[dry] " if dry else "[写] ")
        print(f"{tag}卡位 {text} ({kid}) market={MARKET_ID} 目标第1页第4位 "
              f"bid上限${max_bid} 每次+${step} 闸={guard}× 上限{pushes}次")
        if exists or dry:
            continue
        row = dict(
            market_id=MARKET_ID, keyword_id=kid, keyword_text=text, campaign_id=camp,
            ad_group_id=adg, asin=ASIN,
            goal_page=1, goal_position=4,
            push_mode="bid", max_bid=max_bid, max_tos_pct=900.0,
            step_mode="fixed", step_value=step,
            success_streak_required=5, decrease_mode="percent", decrease_value=5.0,
            restore_on_end=1, acos_guard_mult=guard, max_pushes=pushes,
            status="active", created_at=now, expires_at=None,
        )
        cols = ",".join(row)
        ph = ",".join("?" * len(row))
        conn.execute(f"insert into rank_target ({cols}) values ({ph})", tuple(row.values()))
        n += 1
    if n and not dry:
        conn.commit()
        print(f"已写入 {n} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    # 三道门：--write（真写）+ --i-know（知道在改生产库）。
    # 缺任何一个都只打印。老版本是 main("--dry" in sys.argv) —— 不带参数就写库。
    _write = "--write" in sys.argv and "--i-know" in sys.argv
    if not _write:
        print("[只读] 没给 --write --i-know，本次不写库。")
        print("[建议] 直接用：python3 ingest/rank.py --dataset target-seed")
    sys.exit(main(dry=not _write))
