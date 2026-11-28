#!/usr/bin/env python3
"""数据质量与幂等核验。

用法:
  python3 verify.py                 # 覆盖区间、每日行数、缺失日期、重复主键、金额汇总
  python3 verify.py --market 7
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict

DB = os.environ.get("ADS_DB", os.path.join(os.path.dirname(__file__), "data", "ads.sqlite3"))

# name -> (表名, 业务主键列)  —— 必须用完整业务主键，否则同一搜索词/广告位跨 campaign 会误报重复
FACTS = {
    "campaign": ("fact_campaign_daily", ("campaign_id",)),
    "keyword": ("fact_keyword_daily", ("keyword_id",)),
    "searchterm": ("fact_search_term_daily", ("campaign_id", "ad_group_id", "keyword_id", "search_term")),
    "placement": ("fact_placement_daily", ("campaign_id", "placement")),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if args.market is not None:
        where, params = " WHERE market_id=?", [args.market]

    print(f"DB: {DB}\n")

    print("== 1. 覆盖区间与行数 ==")
    for name, (table, _) in FACTS.items():
        row = conn.execute(
            f"SELECT COUNT(*) n, MIN(report_date) d0, MAX(report_date) d1, COUNT(DISTINCT report_date) days"
            f" FROM {table}{where}",
            params,
        ).fetchone()
        print(f"  {name:11s} {row['n']:>6d} 行  {row['d0']} → {row['d1']}  覆盖 {row['days']} 天")

    print("\n== 2. 每日行数（缺日=管道漏拉） ==")
    per_day: dict[str, dict[str, int]] = defaultdict(dict)
    for name, (table, _) in FACTS.items():
        for r in conn.execute(
            f"SELECT report_date, COUNT(*) n FROM {table}{where} GROUP BY report_date", params
        ):
            per_day[r["report_date"]][name] = r["n"]
    days = sorted(per_day)
    if days:
        head = "  日期         " + "".join(f"{n:>11s}" for n in FACTS)
        print(head)
        for d in days:
            cells = "".join(f"{per_day[d].get(n, 0):>11d}" for n in FACTS)
            flag = "" if all(per_day[d].get(n, 0) > 0 for n in FACTS) else "   ← 有数据集为空"
            print(f"  {d}  {cells}{flag}")

    print("\n== 3. 主键重复检查（应为 0） ==")
    for name, (table, keys) in FACTS.items():
        cols = ", ".join(("market_id", "report_date", *keys))
        dup = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {table}{where} GROUP BY {cols} HAVING COUNT(*) > 1)",
            params,
        ).fetchone()[0]
        print(f"  {name:11s} 重复组 {dup}")

    print("\n== 4. 金额与异常值 ==")
    for name, (table, _) in FACTS.items():
        r = conn.execute(
            f"SELECT SUM(cost) cost, SUM(clicks) clicks, SUM(impressions) imp, SUM(ads_orders) orders,"
            f" SUM(ads_sales) sales,"
            f" SUM(CASE WHEN clicks>0 AND cost=0 THEN 1 ELSE 0 END) zero_cost_clicks,"
            f" SUM(CASE WHEN cost>0 AND impressions=0 THEN 1 ELSE 0 END) cost_no_imp,"
            f" SUM(CASE WHEN ads_orders>0 AND ads_sales=0 THEN 1 ELSE 0 END) order_no_sales"
            f" FROM {table}{where}",
            params,
        ).fetchone()
        acos = (r["cost"] / r["sales"] * 100) if r["sales"] else None
        print(
            f"  {name:11s} cost={r['cost'] or 0:9.2f} clicks={r['clicks'] or 0:6d} imp={r['imp'] or 0:8d}"
            f" orders={r['orders'] or 0:5d} sales={r['sales'] or 0:9.2f} ACOS={'-' if acos is None else f'{acos:.1f}%'}"
            f" | 异常: 有点击零花费={r['zero_cost_clicks'] or 0} 有花费零曝光={r['cost_no_imp'] or 0}"
            f" 有单零销售额={r['order_no_sales'] or 0}"
        )

    print("\n== 4b. 同日跨表点击数对齐（对不上=某张表拉到了未结算的残缺值） ==")
    # 为什么查这个：2026-11-15 的 campaign 在 T+1 拉到时只有 23 击 / $23.50，
    # 而 placement 同期已经 100 击 —— 说明 campaign 那次拿到的是没结算完的快照，
    # 且之后永远不会再拉。引擎窗口最后一天正好落在这种日期上，
    # 1 天的低报会直接改写整窗 CVR / 判定线 / CPC 系数（11-15 差 4.5 倍）。
    # placement 与 campaign 是同一批曝光的不同切法，同一天合计点击数必须相等。
    cmp = conn.execute(
        "SELECT c.report_date d, c.ck, p.pk FROM"
        " (SELECT report_date, SUM(clicks) ck FROM fact_campaign_daily{0} GROUP BY report_date) c"
        " JOIN"
        " (SELECT report_date, SUM(clicks) pk FROM fact_placement_daily{0} GROUP BY report_date) p"
        " ON c.report_date = p.report_date ORDER BY c.report_date".format(
            " WHERE market_id=?" if args.market is not None else ""),
        params,
    ).fetchall()
    # 容差：跨表的当日合计偶有 1 击的出入（2026-11-01/11-04 各差 1 击，占当日 ~0.4%），
    # 属于两张表各自聚合时的取整差，不是残缺快照。真正的残缺是数量级差异（11-15 差 77 击）。
    # 所以只在「差 >= 3 击 且 差 > 当日 1%」时报警。
    def _off(r):
        ck, pk = r["ck"] or 0, r["pk"] or 0
        d = abs(pk - ck)
        return d >= 3 and d > max(ck, pk) * 0.01

    bad = [r for r in cmp if _off(r)]
    if not cmp:
        print("  （无数据）")
    elif not bad:
        print(f"  ✓ {len(cmp)} 天全部对齐（campaign 点击数 == placement 点击数，容差 3 击/1%）")
    else:
        print(f"  ✗ {len(bad)}/{len(cmp)} 天对不上：")
        for r in bad:
            diff = (r["pk"] or 0) - (r["ck"] or 0)
            print(f"    {r['d']}  campaign={r['ck']}  placement={r['pk']}  差 {diff:+d}")
        print("    → 该日期两张表拉取时间不同，其中一张是未结算快照。"
              "修法：`python3 ingest.py --start <该日> --end <该日>`")

    print("\n== 5. run_log 最近 8 条 ==")
    for r in conn.execute(
        "SELECT run_id, dataset, start_date, end_date, pages, rows_fetched, rows_upserted, status,"
        " IFNULL(note,'') note FROM run_log ORDER BY run_id DESC LIMIT 8"
    ):
        # pages / rows_fetched / rows_upserted 都可能为 NULL ——
        # market / rank 这类走别的采集器的数据集从来不写 pages，
        # 原来直接 `{r['pages']:<3d}` 会抛
        # `TypeError: unsupported format string passed to NoneType.__format__`，
        # 于是第 5 节永远打不出来（前四节正常，最容易误判成「跑完了」）。
        pages = r["pages"] if r["pages"] is not None else "-"
        fetched = r["rows_fetched"] if r["rows_fetched"] is not None else "-"
        written = r["rows_upserted"] if r["rows_upserted"] is not None else "-"
        print(f"  #{r['run_id']:<3d} {r['dataset']:11s} {r['start_date']}~{r['end_date']}"
              f" pages={pages!s:<3s} fetched={fetched!s:<6s} written={written!s:<6s}"
              f" {r['status']:8s} {r['note']}")

    conn.close()


if __name__ == "__main__":
    main()
