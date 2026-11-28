#!/usr/bin/env python3
"""积加广告数据 → SQLite 日快照（模块1 读侧落地 + 模块2 幂等入库验证）。

用法：
  python3 ingest.py --market 7 --start 2026-11-01 --end 2026-11-13
  python3 ingest.py --market 7 --start 2026-11-13 --end 2026-11-13 --datasets campaign,keyword
  python3 ingest.py --markets-only          # 只刷新 dim_market
  python3 ingest.py --report                # 只打印库内现状

设计：
  * 每个数据集一次区间查询 + 分页，逐行 upsert（业务主键冲突则更新）。
  * raw_json 保留原始行；row_hash 保留积加行指纹。
  * run_log 记录每次运行，失败也留痕。
  * 调用间隔由 JIJIA_MIN_INTERVAL 控制（默认 60s，与积加限流约束一致）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jijia_client import JijiaClient

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ADS_DB", HERE / "data" / "ads.sqlite3"))
PAGE_SIZE = int(os.environ.get("ADS_PAGE_SIZE", "500"))   # 实测：campaign 500 可用 / 1000 报 40004；placement 999 报 50099
MAX_PAGES = int(os.environ.get("ADS_MAX_PAGES", "80"))


def pick(row: dict, *names, default=None):
    for name in names:
        if name in row and row[name] not in ("", None):
            return row[name]
    return default


def as_num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value):
    num = as_num(value)
    return None if num is None else int(num)


# ---------------- 数据集定义 ----------------
# 每个数据集：endpoint / 事实表 / 主键构造 / 列映射
def campaign_row(row: dict) -> tuple:
    return (
        as_num(pick(row, "marketId")),
        pick(row, "createDate", "reportDate", "date"),
        pick(row, "campaignId", "campaign_id", default=""),
        pick(row, "campaignName"),
        pick(row, "portfolioId"),
        pick(row, "portfolioName"),
        pick(row, "targetingType"),
        pick(row, "state", "campaignState"),
        pick(row, "servingStatus"),
        as_num(pick(row, "dailyBudget")),
        as_int(pick(row, "impressions")),
        as_int(pick(row, "clicks")),
        as_num(pick(row, "cost")),
        as_num(pick(row, "adsSales")),
        as_int(pick(row, "adsOrders")),
        as_num(pick(row, "otherProductSales")),
        as_num(pick(row, "adsProductSales")),
        as_num(pick(row, "ctr")),
        as_num(pick(row, "cpc")),
        as_num(pick(row, "cvr")),
        as_num(pick(row, "acos")),
        as_num(pick(row, "roas")),
        as_num(pick(row, "cpa")),
        pick(row, "hash"),
        json.dumps(row, ensure_ascii=False),
    )


def keyword_row(row: dict) -> tuple:
    return (
        as_num(pick(row, "marketId")),
        pick(row, "createDate", "reportDate", "date"),
        pick(row, "keywordId", "keyword_id", default=""),
        pick(row, "keywordText", "keyword"),
        pick(row, "matchType"),
        pick(row, "groupId", "adGroupId", "adgroupId"),
        pick(row, "groupName", "adGroupName"),
        pick(row, "campaignId"),
        pick(row, "campaignName"),
        as_num(pick(row, "bid", "keywordBid")),
        pick(row, "servingStatus"),
        as_int(pick(row, "impressions")),
        as_int(pick(row, "clicks")),
        as_num(pick(row, "cost")),
        as_num(pick(row, "adsSales")),
        as_int(pick(row, "adsOrders")),
        as_num(pick(row, "ctr")),
        as_num(pick(row, "cpc")),
        as_num(pick(row, "cvr")),
        as_num(pick(row, "acos")),
        as_num(pick(row, "roas")),
        as_num(pick(row, "cpa")),
        pick(row, "hash"),
        json.dumps(row, ensure_ascii=False),
    )


def search_term_row(row: dict) -> tuple:
    return (
        as_num(pick(row, "marketId")),
        pick(row, "createDate", "reportDate", "date"),
        pick(row, "campaignId", default=""),
        pick(row, "groupId", "adGroupId", default=""),
        pick(row, "keywordId", "keyword_id", default=""),
        pick(row, "keywordText", "keyword"),
        pick(row, "matchType"),
        pick(row, "query", "searchTerm", default=""),
        as_int(pick(row, "impressions")),
        as_int(pick(row, "clicks")),
        as_num(pick(row, "cost")),
        as_num(pick(row, "adsSales")),
        as_int(pick(row, "adsOrders")),
        as_num(pick(row, "ctr")),
        as_num(pick(row, "cpc")),
        as_num(pick(row, "cvr")),
        as_num(pick(row, "acos")),
        as_num(pick(row, "roas")),
        as_num(pick(row, "cpa")),
        pick(row, "hash"),
        json.dumps(row, ensure_ascii=False),
    )


def placement_row(row: dict) -> tuple:
    return (
        as_num(pick(row, "marketId")),
        pick(row, "createDate", "reportDate", "date"),
        pick(row, "campaignId", default=""),
        pick(row, "placement", default=""),
        as_num(pick(row, "bidAdjustment")),
        pick(row, "bidAdjustmentName"),
        as_int(pick(row, "bidStrategy")),
        pick(row, "bidStrategyName"),
        as_int(pick(row, "impressions")),
        as_int(pick(row, "clicks")),
        as_num(pick(row, "cost")),
        as_num(pick(row, "adsSales")),
        as_int(pick(row, "adsOrders")),
        as_num(pick(row, "ctr")),
        as_num(pick(row, "cpc")),
        as_num(pick(row, "cvr")),
        as_num(pick(row, "acos")),
        as_num(pick(row, "roas")),
        as_num(pick(row, "cpa")),
        pick(row, "hash"),
        json.dumps(row, ensure_ascii=False),
    )


DATASETS = {
    "campaign": {
        "endpoint": "/operation/ads/adsSpCampaigns/query",
        "table": "fact_campaign_daily",
        "columns": [
            "market_id", "report_date", "campaign_id", "campaign_name", "portfolio_id",
            "portfolio_name", "targeting_type", "campaign_state", "serving_status",
            "daily_budget", "impressions", "clicks", "cost", "ads_sales", "ads_orders",
            "other_product_sales", "ads_product_sales", "ctr", "cpc", "cvr", "acos",
            "roas", "cpa", "row_hash", "raw_json",
        ],
        "row": campaign_row,
        "paging": "page",
    },
    "keyword": {
        "endpoint": "/operation/ads/spKeywordsPage/query",
        "table": "fact_keyword_daily",
        "columns": [
            "market_id", "report_date", "keyword_id", "keyword_text", "match_type",
            "ad_group_id", "ad_group_name", "campaign_id", "campaign_name", "bid",
            "serving_status", "impressions", "clicks", "cost", "ads_sales", "ads_orders",
            "ctr", "cpc", "cvr", "acos", "roas", "cpa", "row_hash", "raw_json",
        ],
        "row": keyword_row,
        "paging": "page",
    },
    "searchterm": {
        "endpoint": "/operation/ads/spSearchKeywordsReport/page",
        "table": "fact_search_term_daily",
        "columns": [
            "market_id", "report_date", "campaign_id", "ad_group_id", "keyword_id",
            "keyword_text", "match_type", "search_term", "impressions", "clicks", "cost",
            "ads_sales", "ads_orders", "ctr", "cpc", "cvr", "acos", "roas", "cpa",
            "row_hash", "raw_json",
        ],
        "row": search_term_row,
        "paging": "page",
    },
    "placement": {
        "endpoint": "/operation/ads/adsSpPlacement/page",
        "table": "fact_placement_daily",
        "columns": [
            "market_id", "report_date", "campaign_id", "placement", "bid_adjustment",
            "bid_adjustment_name", "bid_strategy", "bid_strategy_name", "impressions",
            "clicks", "cost", "ads_sales", "ads_orders", "ctr", "cpc", "cvr", "acos",
            "roas", "cpa", "row_hash", "raw_json",
        ],
        "row": placement_row,
        "paging": "page",
    },
}


# ---------------- 数据库 ----------------
def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(run_log)")}
    if "note" not in cols:
        conn.execute("ALTER TABLE run_log ADD COLUMN note TEXT")
        conn.commit()
    return conn


def refresh_markets(conn: sqlite3.Connection, client: JijiaClient) -> int:
    resp = client.call("/middle/base/market/page", {"page": 1, "pagesize": 50})
    rows = (resp.get("data") or {}).get("rows") or []
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for row in rows:
        for market in row.get("marketListVos") or []:
            attrs = {a.get("name"): a.get("val") for a in (market.get("attributeList") or [])}
            conn.execute(
                """INSERT INTO dim_market (market_id, market, country_name, seller_id, ads_state,
                       auth_expire_date, ads_report_period, ads_report_init_begin, timezone, currency, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                       market=excluded.market, country_name=excluded.country_name, seller_id=excluded.seller_id,
                       ads_state=excluded.ads_state, auth_expire_date=excluded.auth_expire_date,
                       ads_report_period=excluded.ads_report_period,
                       ads_report_init_begin=excluded.ads_report_init_begin,
                       timezone=excluded.timezone, currency=excluded.currency, updated_at=excluded.updated_at""",
                (
                    market.get("marketId"), market.get("market"), market.get("countryName"),
                    market.get("sellerId"), market.get("adsState"), market.get("authExpireDate"),
                    attrs.get("AdsReportPeriod"), attrs.get("AdsReportInitDateBegin"),
                    attrs.get("Timezone"), attrs.get("DefaultCurrency"), now,
                ),
            )
            count += 1
    conn.commit()
    return count


# ------------------------------------------------------------------ 读数快照
#
# fact_*_daily 是 upsert 覆盖：同一 report_date 被重拉一次，上一次的读数就没了。
# 而 agent 的断言里有一整类是关于「重拉会不会把数补上来」的 —— 实测 #74
# 「11-16 的点击数在 T+3 重拉后 ≥ T+1 首次读数的 1.2 倍」，它需要同一个
# (dataset, report_date) 的两次读数，可库里只剩最后一次。那条断言因此被判
# unverifiable，而 unverifiable 一旦落库就被幂等 UPDATE 钉死 —— 永久结论，
# 明明只是我们没留历史。
#
# 只在读数**发生变化**时追加：日更链每半小时跑一次，全量追加会让表迅速膨胀，
# 而其中 99% 是「什么都没变」。只记变化既省空间，又刚好是断言要的东西。
#
# 已知代价：读数没变化时不会有新行，所以「重拉过但数没变」只能从 run_log 看出来。
# 判 #74 这类断言要把两者合起来读：本表给「变没变」，run_log 给「有没有重拉」。

_SNAP_METRICS = ("clicks", "impressions", "cost", "ads_orders", "ads_sales")


def snapshot_readings(conn: sqlite3.Connection, dataset: str, market_id: int,
                      start: str, end: str, run_id: int | None = None) -> int:
    """把本次窗口内「与上次读数不同」的日期追加进 fact_read_snapshot。

    必须在 upsert_rows 之后调用 —— 那时 fact 表已经是新读数，而本表里
    还留着旧读数，两边一比就知道哪些天变了。

    返回追加的行数（0 表示这一轮什么都没变，是正常且常见的结果）。
    """
    spec = DATASETS[dataset]
    table = spec["table"]
    key_col = "campaign_id" if dataset in ("campaign", "placement") else (
        "keyword_id" if dataset == "keyword" else "campaign_id")
    # 一个 (report_date) 下可能有多行（多个活动/关键词），快照按天汇总，
    # 因为断言问的是「这一天的点击数」，不是「这一天的每个活动各多少」。
    agg = conn.execute(
        f"select report_date, "
        f"  coalesce(sum(clicks),0) clicks, coalesce(sum(impressions),0) impressions, "
        f"  coalesce(sum(cost),0) cost, coalesce(sum(ads_orders),0) ads_orders, "
        f"  coalesce(sum(ads_sales),0) ads_sales "
        f"from {table} where market_id=? and report_date between ? and ? "
        f"group by report_date order by report_date",
        (market_id, start, end),
    ).fetchall()
    if not agg:
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for r in agg:
        day = r["report_date"]
        prev = conn.execute(
            "select clicks, impressions, cost, ads_orders, ads_sales, observed_at "
            "from fact_read_snapshot where dataset=? and market_id=? and report_date=? "
            "order by observed_at desc, snapshot_id desc limit 1",
            (dataset, market_id, day),
        ).fetchone()
        cur = {m: r[m] for m in _SNAP_METRICS}
        if prev is not None:
            same = all(
                (float(prev[m] or 0) == float(cur[m] or 0))
                for m in _SNAP_METRICS
            )
            if same:
                continue  # 读数没变，不写 —— 这是绝大多数情况
            diffs = [
                f"{m} {float(prev[m] or 0):g}→{float(cur[m] or 0):g}"
                for m in _SNAP_METRICS
                if float(prev[m] or 0) != float(cur[m] or 0)
            ]
            note = "读数变化：" + "、".join(diffs)
        else:
            note = "首次读数"
        conn.execute(
            "insert into fact_read_snapshot (dataset, market_id, report_date, clicks, "
            "impressions, cost, ads_orders, ads_sales, observed_at, run_id, note) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            (dataset, market_id, day, cur["clicks"], cur["impressions"], cur["cost"],
             cur["ads_orders"], cur["ads_sales"], now, run_id, note),
        )
        written += 1
    conn.commit()
    return written


def upsert_rows(conn: sqlite3.Connection, spec: dict, rows: list[dict], source: str = "jijia") -> int:
    columns = spec["columns"]
    placeholders = ",".join("?" * (len(columns) + 2))
    updates = ",".join(f"{c}=excluded.{c}" for c in columns[3:] if c not in ("report_date", "campaign_id", "keyword_id"))
    sql = (
        f"INSERT INTO {spec['table']} ({','.join(columns)}, source, ingested_at) VALUES ({placeholders}) "
        f"ON CONFLICT DO UPDATE SET {updates}, source=excluded.source, ingested_at=excluded.ingested_at"
    )
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for row in rows:
        values = spec["row"](row)
        if values[0] is None or values[1] in (None, ""):
            continue  # 缺 marketId 或日期，跳过脏行
        conn.execute(sql, (*values, source, now))
        written += 1
    conn.commit()
    return written


def run_dataset(conn: sqlite3.Connection, client: JijiaClient, name: str,
                market_id: int, start: str, end: str) -> dict:
    spec = DATASETS[name]
    cur = conn.execute(
        "INSERT INTO run_log (dataset, market_id, start_date, end_date, status, started_at) VALUES (?,?,?,?,'running',?)",
        (name, market_id, start, end, datetime.now().isoformat(timespec="seconds")),
    )
    run_id = cur.lastrowid
    conn.commit()

    fetched = written = pages = 0
    error = None
    notes: list[str] = []
    try:
        # 实测：四个广告接口全部忽略 page 参数（page/pageNo/pageIndex/offset 均返回同一批），
        # 且 count 有硬上限（campaign count=500 可用、1000 报 40004；placement 999 报 50099）。
        # → 只能"一天一查"，靠日期切分拿到全量；单日行数远低于上限。
        for day in day_range(start, end):
            # ⚠️ endDate 是排他上界：实测 (D, D) 在最新一天返回 0 行，而 (D, D+1) 正好返回 D 当天。
            # 四个接口（campaign/keyword/searchterm/placement）行为一致，统一用 [D, D+1)。
            payload = {
                "marketId": market_id,
                "count": PAGE_SIZE,
                "page": 1,
                "startDate": day,
                "endDate": next_day(day),
            }
            resp = client.call(spec["endpoint"], payload)
            if resp.get("code") != 200:
                raise RuntimeError(f"{day} code={resp.get('code')} msg={resp.get('msg')}")
            data = resp.get("data")
            rows = data if isinstance(data, list) else (data or {}).get("rows") or (data or {}).get("list") or []
            pages += 1
            fetched += len(rows)
            if len(rows) >= PAGE_SIZE:
                notes.append(f"{day} 返回 {len(rows)} 行已达 count 上限，可能截断")
            if rows:
                written += upsert_rows(conn, spec, rows)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]

    note = "；".join(dict.fromkeys(notes)) or None
    conn.execute(
        "UPDATE run_log SET pages=?, rows_fetched=?, rows_upserted=?, status=?, error=?, note=?, finished_at=? WHERE run_id=?",
        (pages, fetched, written, "failed" if error else "ok", error, note,
         datetime.now().isoformat(timespec="seconds"), run_id),
    )
    conn.commit()
    # 读数快照必须在 upsert 之后、本函数返回之前拍 —— 这时 fact 表已是新读数，
    # 而 fact_read_snapshot 里还留着旧读数，两边一比就知道哪些天变了。
    # 失败时也拍：半途失败留下的部分数据同样是「一次读数」，而断言要的正是
    # 「第一次读到多少、后来读到多少」，跟这次跑成没跑成无关。
    snaps = 0
    try:
        snaps = snapshot_readings(conn, name, market_id, start, end, run_id)
    except sqlite3.OperationalError as exc:
        # 老库上这张表可能还没建（migrate 没跑）。不能因此让整轮取数算失败 ——
        # 取数成功是主产物，快照是副产品。
        note = (note + "；" if note else "") + f"读数快照跳过：{exc}"
    return {"dataset": name, "pages": pages, "fetched": fetched, "written": written,
            "error": error, "note": note, "snapshots": snaps}


def report(conn: sqlite3.Connection) -> None:
    print(f"DB: {DB_PATH}")
    for table in ("dim_market", "fact_campaign_daily", "fact_keyword_daily",
                  "fact_search_term_daily", "fact_placement_daily"):
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:26s} {total:>7d} 行")
    print("\n最近运行：")
    for row in conn.execute(
        "SELECT dataset, market_id, start_date, end_date, pages, rows_fetched, rows_upserted, status, error "
        "FROM run_log ORDER BY run_id DESC LIMIT 10"
    ):
        print("  ", dict(row))
    print("\n事实表覆盖区间：")
    for table in ("fact_campaign_daily", "fact_keyword_daily", "fact_search_term_daily", "fact_placement_daily"):
        row = conn.execute(f"SELECT MIN(report_date), MAX(report_date) FROM {table}").fetchone()
        print(f"  {table:26s} {row[0]} → {row[1]}")

def next_day(day: str) -> str:
    """D+1（endDate 是排他上界，必须传 D+1 才拿到 D 当天数据）。"""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def day_range(start: str, end: str) -> list[str]:
    """把区间展开成逐日列表（含首尾）。"""
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        raise ValueError(f"结束日期早于开始日期: {start} → {end}")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def resolve_day(token: str, today: date | None = None) -> str:
    """支持 yesterday / today / -N 这类相对日期，其余按 YYYY-MM-DD 原样返回。

    `today` 必须由调用方按**店铺时区**算好再传（见 market_today）。
    早先这里默认 `date.today()`，拿的是本机 CST 日期 —— 而 7 号店的
    `dim_market.timezone` 是 `US/Pacific`，比 CST 慢 16 小时。
    后果：北京时间 11-26 08:00 跑，CST 的「昨天」是 11-25，
    而洛杉矶此刻还是 11-25 16:00，广告后台的 11-25 才过了 2/3，
    `ads_report_period` 是 5（按周结算），当天数据根本没长完。
    更糟的是 11-26 15:00 跑时，洛杉矶才 11-25 23:00 —— 依然在拉「当天」。
    这不是精度问题：拉到的是一天的残值，而引擎/收敛器会把它当完整日。
    """
    token = (token or "").strip().lower()
    if today is None:
        today = date.today()
    if token in ("yesterday", "y"):
        return (today - timedelta(days=1)).isoformat()
    if token in ("today", "t"):
        return today.isoformat()
    if token.startswith("-") and token[1:].isdigit():
        return (today - timedelta(days=int(token[1:]))).isoformat()
    return token


def market_today(conn: sqlite3.Connection, market_id: int) -> date:
    """这个店铺「今天」是哪一天 —— 按 dim_market.timezone，不是按本机。

    为什么必须按店铺：Amazon 的报表日是站点本地日。US/Pacific 与 CST 差 16 小时，
    北京时间上午 8 点时洛杉矶还是前一天下午 4 点 —— 此时用 CST 的「昨天」
    去拉，拿到的是洛杉矶**今天**的半天数据。

    取不到时区时退回本机日期并打一行警告，而不是静默用错的值：
    这个函数的返回值直接决定「拉哪几天」，静默错了要等几天后才在数据上显形。
    """
    row = None
    try:
        row = conn.execute(
            "select timezone from dim_market where market_id=?", (market_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    tz = ((row["timezone"] if row else "") or "").strip()
    if tz:
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except Exception as e:
            print(f"  [时区] dim_market 里的 {tz!r} 用不了（{e}），退回本机日期", flush=True)
    else:
        print(f"  [时区] market {market_id} 没有 timezone，退回本机日期 "
              f"{date.today()}（拉到的可能是站点本地半截日）", flush=True)
    return date.today()



def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--market", type=int, default=7)
    p.add_argument("--start", default="yesterday", help="YYYY-MM-DD / yesterday / -N")
    p.add_argument("--end", default="yesterday", help="YYYY-MM-DD / yesterday / -N")
    # 滚动补拉：Amazon 报表 T+1 只是"能查到"，不是"已结算"。
    # 实测 2026-11-15 在 T+1（11-16 06:10）拉到 23 击 / $23.50，
    # 到 T+3（11-18 09:35）同一接口同一天变成 103 击 / $97.21 —— 4.5 倍。
    # 只拉 yesterday 会让最近两天永远停在残缺值上，而引擎窗口的最后一天正好是它。
    # 所以默认改成「最近 N 天全部重拉一遍」（幂等 upsert，重拉无副作用）。
    p.add_argument("--recheck-days", type=int, default=3,
                   help="每次运行额外重拉最近 N 天（0=只拉 yesterday）。"
                        "报表会持续回填，T+1 的数不可信。")
    p.add_argument("--datasets", default="campaign,keyword,searchterm,placement")
    p.add_argument("--markets-only", action="store_true")
    p.add_argument("--report", action="store_true")
    args = p.parse_args(argv)
    # 相对日期必须按**店铺时区**算，不能按本机。
    # 这里先解析成 None 占位，等 main() 连上库、读到 dim_market.timezone 之后再算 ——
    # parse_args 拿不到 conn，硬在这里用 date.today() 就是原来那个 bug。
    args._rel = (args.start, args.end)
    args._argv = list(argv)
    args.start = None
    args.end = None
    return args


def apply_dates(args, conn: sqlite3.Connection) -> None:
    """把相对日期（yesterday / -N）落到具体日期上，基准是店铺时区的今天。"""
    today = market_today(conn, args.market)
    raw_start, raw_end = args._rel
    args.start = resolve_day(raw_start, today=today)
    args.end = resolve_day(raw_end, today=today)
    # 显式给了区间就不覆盖（手工补拉时以手工为准）
    if args.recheck_days and "--start" not in args._argv and "--end" not in args._argv:
        args.start = resolve_day(f"-{args.recheck_days}", today=today)



def _midship_ingest_done(conn, args, results) -> None:
    """拉数已完成后写事件并交给中台消费。失败不回滚数据。"""
    try:
        root = HERE.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from midship.hooks import safe_ingest_done
        days = day_range(args.start, args.end)
        out = safe_ingest_done(conn, days=days, results=results, market_id=args.market)
        writes = (out or {}).get("writes") or []
        consumed = (out or {}).get("consumed") or []
        inserted = sum(1 for w in writes if w.get("inserted"))
        print(f"  [中台] 写入 {inserted}/{len(writes)} 条 ingest.done，消费 {len(consumed)} 条", flush=True)
    except Exception as e:
        print(f"  [中台] 收尾失败（拉数已完成）：{e}", flush=True)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    conn = connect()
    if args.report:
        report(conn)
        return 0

    # 先刷 dim_market 再算日期：时区是 refresh_markets 写进来的，
    # 顺序反了就会拿上一次的时区（或空）算「昨天」。
    client = JijiaClient()
    if not args.markets_only:
        names = [n.strip() for n in args.datasets.split(",") if n.strip()]
        unknown = [n for n in names if n not in DATASETS]
        if unknown:
            print(f"未知数据集: {unknown}", file=sys.stderr)
            return 2
        print(f"刷新 dim_market ...", flush=True)
        print(f"  markets: {refresh_markets(conn, client)} 个", flush=True)
        apply_dates(args, conn)
        print(f"  日期窗口: {args.start} ~ {args.end}（按店铺时区）", flush=True)
        results = []
        for name in names:
            result = run_dataset(conn, client, name, args.market, args.start, args.end)
            results.append(result)
            flag = "FAIL" if result["error"] else "ok"
            print(f"  [{flag}] {result['dataset']:10s} pages={result['pages']} "
                  f"fetched={result['fetched']} written={result['written']}"
                  + (f" err={result['error']}" if result["error"] else "")
                  + (f" note={result['note']}" if result.get("note") else ""), flush=True)
    else:
        results = []
        # 同样先刷 market 再算日期：时区是 refresh_markets 写进 dim_market 的
        print(f"markets: {refresh_markets(conn, client)} 个")
        apply_dates(args, conn)

    report(conn)
    if results:
        _midship_ingest_done(conn, args, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
