#!/usr/bin/env python3
"""从历史备份库里回收 `fact_read_snapshot` 的读数。

为什么要这个脚本
----------------
`fact_read_snapshot` 是 2026-11-20 才建的，只从建成那一刻开始记录。可
`agent_eval` 里有断言问的是**建表之前**的两次读数，最典型的是 `#74`：

    11-16 的点击数在 T+3 重拉后 ≥ T+1 首次读数的 1.2 倍
    expected: T+3 clicks ≥ 1.2 × T+1 clicks

这两次读数都发生在建表之前（T+1=11-17 拉的那次、T+3=11-19 拉的那次），
按"表建晚了所以判不了"处理是错的 —— `fact_*_daily` 是 upsert 覆盖，
但 `ingest/data/` 下留着一串 `.bak-*` 备份库，每个都冻结了它那一刻的
读数。备份库的 mtime 就是 `observed_at`，表里的值就是那次读数。

本脚本把这些还原成 `fact_read_snapshot` 的行，让"历史读数"这件事重新
成立。它是一次性的历史回收，不是常规路径 —— 常规路径在
`ingest.ingest.snapshot_readings`，每次 ingest 之后自动跑。

设计取舍
--------
* **只认备份库，不猜**。没有备份的日期就是没有读数，不插空行、不用
  `fact_*_daily` 的现值冒充（那是"最后一次读数"，冒充会把 T+1 的值
  写成 T+3 的值，`#74` 的比值永远是 1.0）。
* **同一时刻多份备份只取一份**。备份常常成对出现（同一分钟内做了两次
  备份），读数相同，去重按 `(dataset, report_date, 读数)`。
* **`note` 写明来源**：`从备份库恢复：ads.sqlite3.bak-xxx（2026-11-17 17:05）`。
  人要能一眼看出这行不是引擎拍的，是事后恢复的。
* **幂等**：同一 `(dataset, market_id, report_date, observed_at, clicks)` 已
  存在就跳过。重复跑不会长行。
* **默认 dry-run**，`--apply` 才真写。这脚本改的是判定依据，不该一键生效。

用法
----
    python3 ingest/backfill_readings.py                 # 只看会写什么
    python3 ingest/backfill_readings.py --apply         # 真写
    python3 ingest/backfill_readings.py --apply --since 2026-11-14
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ADS_DB", HERE / "data" / "ads.sqlite3"))

# 数据集 → fact 表。与 `ingest.ingest.DATASETS` 保持一致；这里不 import 它，
# 因为那个模块 import 时会拉起 jijia_client（要凭证、要网络），回填不该有
# 那层依赖。
TABLES = {
    "campaign": "fact_campaign_daily",
    "keyword": "fact_keyword_daily",
    "searchterm": "fact_search_term_daily",
    "placement": "fact_placement_daily",
}

METRICS = ("clicks", "impressions", "cost", "ads_orders", "ads_sales")


def daily_agg(conn: sqlite3.Connection, table: str, market_id: int,
              since: str | None) -> dict[str, dict]:
    """把一张 fact 表按 report_date 汇总成 {day: {metric: value}}。

    按天汇总而不是按活动 —— 断言问的是「这一天的点击数」。`#74` 的
    「11-16 的点击数」是账户那天一共点了多少下，不是某个活动点了多少下。
    """
    where = "where market_id=?"
    args: list = [market_id]
    if since:
        where += " and report_date >= ?"
        args.append(since)
    rows = conn.execute(
        f"select report_date, "
        f"  coalesce(sum(clicks),0) clicks, coalesce(sum(impressions),0) impressions, "
        f"  coalesce(sum(cost),0) cost, coalesce(sum(ads_orders),0) ads_orders, "
        f"  coalesce(sum(ads_sales),0) ads_sales "
        f"from {table} {where} group by report_date order by report_date",
        args,
    ).fetchall()
    return {r["report_date"]: {m: r[m] for m in METRICS} for r in rows}


def collect(backups: list[Path], market_id: int, since: str | None) -> list[dict]:
    """扫全部备份库，抽出每一次可识别的读数。

    返回按 (dataset, day, observed_at) 排好序的读数列表，已按
    (dataset, day, observed_at) 去重 —— 同一时刻的多份备份读数相同，
    只留一份。
    """
    seen: dict[tuple, dict] = {}
    for path in backups:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        # observed_at 取备份文件的 mtime：那是这一份数据被冻结的时刻，
        # 也就是「这一刻我们读到的值」。不取事实表里的 ingested_at ——
        # 那是 upsert 时间，同一行被覆盖多次只剩最后一次。
        observed_at = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            print(f"! 打不开 {path.name}：{exc}")
            continue
        try:
            for dataset, table in TABLES.items():
                try:
                    agg = daily_agg(conn, table, market_id, since)
                except sqlite3.Error as exc:
                    # 老备份可能没有这张表（例如 placement 是后来加的）。
                    # 跳过并记一行，不要因此中断整个回填。
                    print(f"  跳过 {path.name} / {dataset}：{exc}")
                    continue
                for day, metrics in agg.items():
                    key = (dataset, day, observed_at)
                    if key in seen:
                        continue
                    seen[key] = {
                        "dataset": dataset,
                        "market_id": market_id,
                        "report_date": day,
                        "observed_at": observed_at,
                        "source": path.name,
                        **metrics,
                    }
        finally:
            conn.close()
    return sorted(
        seen.values(),
        key=lambda r: (r["dataset"], r["report_date"], r["observed_at"]),
    )


def diff_note(prev: dict | None, cur: dict) -> str | None:
    """与同一 (dataset, day) 上一读数比。返回 None 表示读数没变、不该写。"""
    if prev is None:
        return "首次读数（从备份库恢复）"
    diffs = [
        f"{m} {float(prev.get(m) or 0):g}→{float(cur.get(m) or 0):g}"
        for m in METRICS
        if float(prev.get(m) or 0) != float(cur.get(m) or 0)
    ]
    if not diffs:
        return None
    return "读数变化：" + "、".join(diffs)


def apply(conn: sqlite3.Connection, readings: list[dict], *, dry_run: bool) -> int:
    """把读数写进 fact_read_snapshot。返回新写入行数。

    与 `ingest.ingest.snapshot_readings` 同一条规矩：**只在读数变化时追加**。
    一次真实的"重拉"会留下两个备份，读数一样 —— 那不是两次不同的读数，
    是同一次读数被观测了两遍。全写进去会让 `#74` 那类「前一次 vs 后一次」
    的断言拿到一串相同的值，比值永远 1.0。

    所以每个 (dataset, day) 只在值真正变化时留一行，`observed_at` 取该值
    第一次被看到的时刻。
    """
    written = 0
    last: dict[tuple, dict] = {}
    for r in readings:
        key = (r["dataset"], r["report_date"])
        note = diff_note(last.get(key), r)
        if note is None:
            last[key] = r
            continue
        exists = conn.execute(
            "select 1 from fact_read_snapshot where dataset=? and market_id=? "
            "and report_date=? and observed_at=? and clicks=? limit 1",
            (r["dataset"], r["market_id"], r["report_date"], r["observed_at"],
             r["clicks"]),
        ).fetchone()
        if exists:
            last[key] = r
            continue
        if not dry_run:
            conn.execute(
                "insert into fact_read_snapshot (dataset, market_id, report_date, clicks, "
                "impressions, cost, ads_orders, ads_sales, observed_at, run_id, note) "
                "values (?,?,?,?,?,?,?,?,?,?,?)",
                (r["dataset"], r["market_id"], r["report_date"], r["clicks"],
                 r["impressions"], r["cost"], r["ads_orders"], r["ads_sales"],
                 r["observed_at"], None,
                 f"从备份库恢复：{r['source']}（{r['observed_at'][:16].replace('T', ' ')}）"
                 f"；{note}"),
            )
        print(
            f"  {r['dataset']:11s} {r['report_date']}  {r['observed_at'][:16]}  "
            f"clicks={r['clicks']:>5}  {note}  ← {r['source']}"
        )
        written += 1
        last[key] = r
    if not dry_run:
        conn.commit()
    return written


def clear_placeholder(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """删掉「回填时把现值当首次读数」写进去的假读数。

    建表当天我用 `snapshot_readings` 回填过一遍 76 行，但那批行拿的是
    `fact_*_daily` 的**当前值**，全标成「首次读数」。对建表之前就被拉过
    多次的日期来说，那既不是首次也不是任何一次真实读数的时刻 —— 它是
    "最后一次读数"，`observed_at` 是建表那一刻。留着它会让 `#74` 的
    T+1 读数变成 T+3 的值，比值永远 1.0，把一条本该判 correct 的断言
    判成 wrong。所以：凡 run_id is null 且 note 以「首次读数」开头的，
    都是那批假行，清掉。
    """
    rows = conn.execute(
        "select snapshot_id, dataset, report_date, clicks, observed_at from fact_read_snapshot "
        "where run_id is null and note like '首次读数%' order by dataset, report_date"
    ).fetchall()
    if not rows:
        return 0
    print(f"\n清掉 {len(rows)} 行「把当前值当首次读数」的回填行：")
    for r in rows[:5]:
        print(f"  #{r['snapshot_id']} {r['dataset']:11s} {r['report_date']} "
              f"clicks={r['clicks']} observed_at={r['observed_at']}")
    if len(rows) > 5:
        print(f"  …… 另有 {len(rows) - 5} 行")
    if not dry_run:
        conn.executemany(
            "delete from fact_read_snapshot where snapshot_id=?",
            [(r["snapshot_id"],) for r in rows],
        )
        conn.commit()
    return len(rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="从历史备份库回收 fact_read_snapshot 读数")
    ap.add_argument("--db", default=str(DB_PATH), help="目标库（默认为 ingest/data/ads.sqlite3）")
    ap.add_argument("--market", type=int, default=7, help="积加 marketId（本账号 amazon-us 是 7）")
    ap.add_argument("--since", default=None, help="只回填这个日期之后（YYYY-MM-DD）")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只打印）")
    ap.add_argument("--keep-placeholder", action="store_true",
                    help="保留建表当天那批「首次读数」占位行（默认清掉）")
    args = ap.parse_args(argv)
    dry_run = not args.apply

    db = Path(args.db)
    if not db.exists():
        print(f"! 库不存在：{db}")
        return 2
    # `src.backup()` 会把 -shm/-wal 一起拷过来（那是 SQLite 的 WAL 副产物），
    # 它们不是数据库：-shm 是共享内存索引（打不开），-wal 是预写日志
    # （能打开但没有表）。一起扫会把 21 个文件里 12 个当备份，全刷一遍
    # 「跳过 X：file is not a database」，把真正的输出埋掉。
    backups = sorted(
        p for p in db.parent.glob(db.name + ".bak-*")
        if p.is_file() and not p.name.endswith(("-shm", "-wal"))
    )
    if not backups:
        print(f"! {db.parent} 下没有 {db.name}.bak-* 备份，无处可回填")
        return 2
    print(f"扫 {len(backups)} 个备份库（market_id={args.market}）：")
    for p in backups:
        print(f"  {p.name}  {datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")

    readings = collect(backups, args.market, args.since)
    if not readings:
        print("\n没有可回收的读数。")
        return 1
    days = sorted({r["report_date"] for r in readings})
    print(f"\n回收候选 {len(readings)} 条读数，覆盖 {len(days)} 天"
          f"（{days[0]} ~ {days[-1]}）：")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        if not args.keep_placeholder:
            cleared = clear_placeholder(conn, dry_run=dry_run)
            if cleared:
                print()
        written = apply(conn, readings, dry_run=dry_run)
        total = conn.execute("select count(*) from fact_read_snapshot").fetchone()[0]
    finally:
        conn.close()

    verb = "将写入" if dry_run else "已写入"
    print(f"\n{verb} {written} 行；表内现有 {total} 行。")
    if dry_run:
        print("这是预演。确认无误后加 --apply 真写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))