#!/usr/bin/env python3
"""位次快照采集（卡位 R31 的数据供给层）。

为什么单独一层：`engine/facts.py:1529` 的 docstring 写得很直白 ——
「引擎不抓排名，只消费 rank_snapshot」。引擎侧 `rank_state()` 已经把「批次、
自己的位次、连续到位、自我竞争、已加价次数」全算好了，唯独没有人往
`rank_snapshot` 里写行。R31 因此永远走「无排名观测」这条抑制分支，
卡位功能等于没装。这个脚本就是那只手。

三条纪律（与 ingest/market.py 同源，理由也一样）：
1. 连接层复用 tools/sorftime_probe.py 的 rpc()，不复制第二份。探针里踩过的坑
   （本机代理会掐断 SSE，所以要 ProxyHandler({})）只应该存在一处。
2. 一个 dataset 先把数据**全部取完**，再一次性写库。中途任何失败 => 整批不写、
   只落日志。半截数据比没数据更坏：engine 会拿半个批次去判「自己在第几位」。
3. 幂等：rank_snapshot 没有唯一索引（表是别的途径建的，别去改 .sql），
   所以不靠 on conflict，而是写库时用 `insert ... where not exists(...)`，
   由 (keyword_id, observed_at, placement, asin) 四元组去重。重跑行数不变。

## 位次从哪来（2026-11-23 实测结论）

sorftime 三个候选工具，只有一个能定位「我自己的位次」：

| 工具 | 能不能用 | 原因 |
|---|---|---|
| `product_ranking_trend_by_keyword` | **主数据源** | 直接按 ASIN+关键词返回历次曝光位次，含 Organic / SP Ad / Video Ad / brand ad，带时间戳，20 行/页 |
| `product_traffic_terms` | 辅助 | 一次给 20 个词的最新 organic/ad 位次，但没有历史序列，只用来发现「这个词我也在投」 |
| `keyword_search_results` | **不能用** | 只回前 20 名有机位的商品对象，**不含位次字段**；实测自己在 `lat pulldown` 是 Page1 Pos20/50 却不在这个列表的前 20 里 —— 它给的是销量排序不是位次顺序。拿它定位会把自己的名次算错 |

## placement 只写两种，且 SP Ad 独占 sponsored

源端同一时刻会给多条广告位记录（实测 2026-11-23 05:15 的 `lat pulldown machines`
同时有 SP Ad Pos 7/69、Video Ad Pos 11/69、Video Ad Pos 69/69、Video Ad Pos 60/60）。
schema 的 placement 只有 organic|sponsored 两值，而 engine 只读 `placement='sponsored'`
并按 `position asc` 取批次首行。如果 Video Ad 也写成 sponsored，engine 拿到的是
Video Ad 的 7/69，而不是关键词竞价真正卡的 SP 位 —— 位次判定会整体错位。
所以：Organic → organic；SP Ad → sponsored；Video Ad / brand ad → **跳过并计数**。

## observed_at 用源端观测时间，不是采集时刻

`engine/facts.py:1419 RANK_SNAPSHOT_MAX_AGE_HOURS = 6`，`_parse_ts()` 用
`datetime.fromisoformat` 解析、`datetime.now()`（本地 naive）比差。如果把采集时刻
当 observed_at，age 永远是 0，一个三天前的位次会被当成「刚刚观测」直接过闸 ——
过期闸形同虚设。所以 observed_at 取源端 `time` 字段（sorftime 服务器时间实测就是
CST，与本机一致：2026-11-23 17:05:42 == 本机 17:05:42），格式化成
`YYYY-MM-DDTHH:MM:00` 的本地 naive ISO。源端没给时间才退回采集时刻。

用法：
  python3 rank.py --dataset target-seed --dry-run   # 看要种哪些卡位目标
  python3 rank.py --dataset snapshot --dry-run      # 看能采到哪些位次
  python3 rank.py --dataset all --verbose
  python3 rank.py --dataset snapshot --limit 5 --pages 2 --as-of 2026-11-20

退出码：0 成功 / 2 采集失败 / 3 写库失败（对齐 market.py）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB_PATH = Path(os.environ.get("ADS_DB", HERE / "data" / "ads.sqlite3"))
FAIL_LOG = HERE / "data" / "rank_fail.log"

# 复用探针的连接层。sys.path 而不是复制一份代码：SSE/代理那些坑只有一处需要维护。
sys.path.insert(0, str(ROOT / "tools"))
from sorftime_probe import DEFAULT_URL, rpc  # noqa: E402

CST = timezone(timedelta(hours=8))

# 自己的 ASIN。认定链条（三重独立证据，报告里会贴）：
#   policy 表 policy_id=5 的 object_id → product_detail 的 seller_id
#   → dim_market.market_id=7 的 seller_id，三者一致。
DEFAULT_ASIN = "B0GLYW51HY"
DEFAULT_MARKET = 7

# 源端位次文本："Page 1" / "Pos 37/69"。只取分子，分母是当页曝光位总数。
PAGE_RE = re.compile(r"page\s*(\d+)", re.I)
POS_RE = re.compile(r"pos\s*(\d+)", re.I)


# ════════════════════════════════════════════════════════════════
# 小工具
# ════════════════════════════════════════════════════════════════
def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _page_of(text) -> int | None:
    m = PAGE_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def _pos_of(text) -> int | None:
    m = POS_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def _obs_ts(src_time: str | None, as_of: str | None) -> str:
    """源端时间 → 本地 naive ISO（engine 的 _parse_ts 要能直接 fromisoformat）。

    源端给 "2026-11-23 05:15"（没有秒）。--as-of 只换日期部分，时分保留 ——
    补历史时批次仍按时刻可分，不会把不同批次压成同一时刻导致去重互相吞掉。
    """
    raw = str(src_time or "").strip()
    day, hhmm = None, "00:00"
    if raw:
        parts = raw.replace("T", " ").split()
        if parts:
            day = parts[0]
        if len(parts) > 1:
            hm = parts[1].split(":")
            if len(hm) >= 2:
                hhmm = f"{int(hm[0]):02d}:{int(hm[1]):02d}"
    if as_of:
        day = as_of
    if not day:
        return datetime.now(CST).replace(tzinfo=None).isoformat(timespec="seconds")
    return f"{day}T{hhmm}:00"


def placement_of(row: dict) -> str | None:
    """源端一行 → schema 的 placement 值；返回 None 表示这类广告位不写库。

    见模块 docstring：Video Ad / brand ad 绝不能混进 sponsored，否则 engine
    按 position asc 取批次首行时会拿到视频位，把「关键词竞价卡位」判错。
    """
    ptype = str(row.get("position_type") or "").strip().lower()
    pname = str(row.get("position_name") or "").strip().lower()
    if ptype.startswith("organic") or pname == "organic":
        return "organic"
    if pname in ("sp ad", "sponsored products", "sponsored product", "sponsored"):
        return "sponsored"
    if ptype.startswith("ad") and not pname:
        # 只有 "Ad" 没有具体位名时，无法区分 SP/视频/品牌。宁可跳过也不猜：
        # 猜错会把视频位的名次当成竞价位，engine 会据此加价。
        return None
    return None


# ════════════════════════════════════════════════════════════════
# 表结构
# ════════════════════════════════════════════════════════════════
SNAP_COLS = [
    "market_id", "keyword_id", "keyword_text", "observed_at", "placement",
    "page", "position", "asin", "is_self", "postal_code", "source", "raw_json",
]

# 没有唯一索引，所以不用 upsert：写库时带上 where not exists，原子去重。
#
# 判重键为什么是六元组而不是四元组（2026-11-23 实测踩过）：
# 源端同一观测时刻会给**同一 placement 的多行不同位次**，不是重复数据。
#   · `lat pulldown machines` 2026-11-22T05:01 sponsored 同时有 p3/26 和 p3/34
#   · `lat pulldown machines` 2026-11-21T05:03 sponsored 同时有 p2/19 和 p3/23
#   · `wall mounted cable machine` 2026-10-29T12:27 organic 同时有 p1/52 和 p2/4
# 按 (keyword_id, observed_at, placement, asin) 判重会把这 3 行当成重复丢掉，
# 47 行只剩 44 行 —— 静默丢数据，而且丢的正好是「同一次抓取里自己占了两个坑位」
# 这种最有信息量的观测。加上 page/position 才是真正的同一行。
SNAP_KEY = ("keyword_id", "observed_at", "placement", "asin", "page", "position")

SNAP_SQL = (
    f"insert into rank_snapshot ({', '.join(SNAP_COLS)}) "
    f"select {', '.join(':' + c for c in SNAP_COLS)} "
    "where not exists (select 1 from rank_snapshot "
    "where keyword_id = :keyword_id and observed_at = :observed_at "
    "and placement = :placement and coalesce(asin, '') = coalesce(:asin, '') "
    "and coalesce(page, -1) = coalesce(:page, -1) "
    "and coalesce(position, -1) = coalesce(:position, -1))"
)

TARGET_COLS = [
    "market_id", "keyword_id", "keyword_text", "campaign_id", "ad_group_id", "asin",
    "goal_page", "goal_position", "push_mode", "max_bid", "max_tos_pct",
    "step_mode", "step_value", "success_streak_required", "decrease_mode",
    "decrease_value", "restore_on_end", "acos_guard_mult", "max_pushes",
    "status", "created_at", "expires_at",
]

TARGET_SQL = (
    f"insert into rank_target ({', '.join(TARGET_COLS)}) "
    f"select {', '.join(':' + c for c in TARGET_COLS)} "
    "where not exists (select 1 from rank_target "
    "where market_id = :market_id and keyword_id = :keyword_id)"
)


def blank(cols: list[str], **kw) -> dict:
    row = {c: None for c in cols}
    row.update(kw)
    return row


# ════════════════════════════════════════════════════════════════
# MCP 客户端
# ════════════════════════════════════════════════════════════════
class RankError(RuntimeError):
    """采集失败的唯一异常类型。main() 只认它，避免把 KeyboardInterrupt 吃成「采集失败」。"""


class Client:
    """sorftime MCP 的薄封装：只负责 JSON-RPC 信封和 tools/call 的 content 拆包。

    为什么不直接 import market.Client：它把「返回的不是 JSON」一律当故障抛错，
    而位次接口的空结果是 `No relevant data.`（不是 `No data available.`）——
    复用它会因为一个长尾词没位次就把整批采集打死。空结果的判定必须按工具分。
    连接层的坑（SSE 多行 data、本机代理）仍然只在 sorftime_probe.rpc() 里解决。
    """

    # 各工具「正常空结果」的纯文本。sorftime 对没数据不给 JSON，给一句英文。
    EMPTY_TEXT = {
        "product_ranking_trend_by_keyword": ("no relevant data", "no data available", "no data"),
        "product_traffic_terms": ("no relevant data", "no data available", "no data"),
        "keyword_search_results": ("no relevant data", "no data available", "no data"),
        "common_usage_query": ("no data available", "no data"),
    }

    def __init__(self, url: str = DEFAULT_URL, verbose: bool = False, pause: float = 1.5):
        self.url = url
        self.verbose = verbose
        self.pause = pause      # 每次调用之间的间隔。sorftime 没有积加那种 1 分钟熔断，
        self.calls = 0          # 但也没必要狂刷；1.5s 足够把「批量」和「打点」区分开。

    def call(self, tool: str, args: dict | None = None):
        payload = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }
        if self.calls and self.pause:
            time.sleep(self.pause)
        self.calls += 1
        try:
            res = rpc(self.url, payload)
        except Exception as exc:  # 网络/证书/HTTP 层
            raise RankError(
                f"{tool}({json.dumps(args or {}, ensure_ascii=False)}) 调用失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        texts = [c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text"]
        raw = "\n".join(texts).strip()
        if res.get("isError"):
            raise RankError(
                f"{tool}({json.dumps(args or {}, ensure_ascii=False)}) 网关报错：{raw or '(无正文)'}"
            )
        if not raw:
            raise RankError(f"{tool} 返回空 content（result keys={list(res.keys())}）")
        empties = self.EMPTY_TEXT.get(tool, ("no data available", "no data"))
        if raw.strip().rstrip(".").strip().lower() in empties:
            obj = {}
        else:
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                raise RankError(f"{tool} 返回的不是 JSON：{raw[:300]}") from exc
        if isinstance(obj, dict) and obj.get("code") not in (None, 0, "0", 200):
            raise RankError(
                f"{tool} 业务错误 code={obj.get('code')} "
                f"msg={obj.get('msg') or obj.get('message') or obj}"
            )
        if self.verbose:
            n = len(obj.get("data") or []) if isinstance(obj, dict) else 0
            print(f"  · {tool}({json.dumps(args or {}, ensure_ascii=False)}) ok rows={n}",
                  file=sys.stderr)
        return obj

    def data(self, tool: str, args: dict | None = None) -> list[dict]:
        """只取 data 数组。空结果是 []，不是异常。"""
        obj = self.call(tool, args)
        if not isinstance(obj, dict):
            return []
        d = obj.get("data")
        return d if isinstance(d, list) else []

    def credit(self):
        try:
            obj = self.call("common_usage_query", {})
            d = (obj or {}).get("data") if isinstance(obj, dict) else None
            return (d or {}).get("credit_left")
        except RankError:
            return None


# ════════════════════════════════════════════════════════════════
# 词源：谁有资格当卡位目标
# ════════════════════════════════════════════════════════════════
def _targets(conn, market: int) -> list[dict]:
    """已在册的卡位目标（status 不限 —— paused 的词也值得继续观测位次）。"""
    try:
        rows = conn.execute(
            "select target_id, keyword_id, keyword_text, asin, goal_page, goal_position, status "
            "from rank_target where market_id = ? order by target_id", (market,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _kw_from_keyword_daily(conn, market: int, limit: int) -> list[dict]:
    """自己真正在投的关键词，按窗口曝光降序。

    为什么优先这张表而不是 fact_search_term_daily：rank_target 需要
    campaign_id / ad_group_id / 当前 bid 才能让 R31 算出「加多少」，
    而这三样只在 fact_keyword_daily 里有。search_term 是「客户搜进来的词」，
    可能压根没被当关键词投放，拿它种卡位目标会让 R31 走「关键词不在本窗口数据里」。
    """
    try:
        rows = conn.execute("""
            select keyword_id, keyword_text, sum(impressions) imp,
                   max(campaign_id) campaign_id, max(ad_group_id) ad_group_id,
                   max(report_date) last_date
            from fact_keyword_daily
            where market_id = ? and keyword_id is not null and keyword_text is not null
            group by keyword_id
            having imp > 0
            order by imp desc
            limit ?
        """, (market, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = dict(r)
        # 当前 bid 取窗口内最后一个有数据日期的值（配置会变，要当前生效的那个）。
        try:
            b = conn.execute(
                "select bid from fact_keyword_daily where market_id = ? and keyword_id = ? "
                "and report_date = ? and bid is not null limit 1",
                (market, d["keyword_id"], d["last_date"])).fetchone()
            d["bid"] = float(b["bid"]) if b and b["bid"] is not None else None
        except sqlite3.OperationalError:
            d["bid"] = None
        out.append(d)
    return out


def _kw_from_search_terms(conn, market: int, limit: int) -> list[dict]:
    """兜底词源：自己搜索词报表里曝光最高的词。

    只在 fact_keyword_daily 拿不出足够词时用（比如刚换账户、关键词报表还没回填）。
    这些词没有 campaign_id/bid，seed 出来的 target 需要人工补配置 —— 但位次照样能采。
    """
    try:
        rows = conn.execute("""
            select search_term keyword_text, sum(impressions) imp,
                   max(keyword_id) keyword_id
            from fact_search_term_daily
            where market_id = ? and search_term is not null and search_term <> ''
            group by search_term
            having imp > 0
            order by imp desc
            limit ?
        """, (market, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def resolve_keyword_id(conn, market: int, keyword_text: str) -> str:
    """keyword_text → keyword_id。命中不了就用词本身当 id。

    keyword_id 是 NOT NULL，所谓「留空」在物理上做不到。用词本身当 id 有两个好处：
    主键稳定（重跑不会造新行）、面板和 engine 的 join 虽然落空但位次本身可读。
    真实情况是：卡位词都来自自己投过的词，fact_keyword_daily 一定有对应 id，
    这条兜底只在报表回填延迟时走一次。
    """
    try:
        r = conn.execute(
            "select keyword_id from fact_keyword_daily where market_id = ? and keyword_text = ? "
            "and keyword_id is not null order by impressions desc limit 1",
            (market, keyword_text)).fetchone()
        if r and r["keyword_id"]:
            return str(r["keyword_id"])
        r = conn.execute(
            "select keyword_id from fact_keyword_daily where market_id = ? "
            "and lower(trim(keyword_text)) = lower(trim(?)) and keyword_id is not null limit 1",
            (market, keyword_text)).fetchone()
        if r and r["keyword_id"]:
            return str(r["keyword_id"])
    except sqlite3.OperationalError:
        pass
    return keyword_text.strip()


# ════════════════════════════════════════════════════════════════
# dataset: target-seed —— 从高曝光关键词种出卡位目标草稿
# ════════════════════════════════════════════════════════════════
def _collect_target_seed(c: Client, ctx, conn) -> dict:
    """给自己曝光最高的词各插一条 rank_target，让 R31 有活可干。

    默认值沿用 schema 的卡位语义：goal_page=1 / goal_position=4（= 搜索结果顶部，
    已排除品牌位）、push_mode='bid'、step_value=0.10、max_pushes=6。

    唯一一处**必须补**的默认值：`max_bid`。R31 的「触顶」闸靠 max_bid，
    留 NULL 就等于没有上限 —— 一个只会加价的规则，没有顶就是烧钱机器。
    取当前 bid × 1.5 并向上取到分，写进报告让人知道这个数是推出来的、不是源端的。

    幂等：按 (market_id, keyword_id) 查重，已存在就跳过（不覆盖 —— 人工改过的
    goal_position / max_bid 不该被重跑抹掉）。写库语句里再带一次 not exists 兜底。
    """
    market = ctx.market
    existing = {str(t["keyword_id"]) for t in _targets(conn, market)}

    picked = _kw_from_keyword_daily(conn, market, ctx.limit)
    source = "fact_keyword_daily"
    if len(picked) < ctx.limit:
        have = {p["keyword_text"] for p in picked}
        for extra in _kw_from_search_terms(conn, market, ctx.limit):
            if extra["keyword_text"] in have:
                continue
            extra.setdefault("campaign_id", None)
            extra.setdefault("ad_group_id", None)
            extra.setdefault("bid", None)
            picked.append(extra)
            source = "fact_keyword_daily+fact_search_term_daily"
            if len(picked) >= ctx.limit:
                break

    if not picked:
        raise RankError(
            "没有可种的词：fact_keyword_daily 与 fact_search_term_daily 都没有 market_id="
            f"{market} 的曝光数据。先跑 ingest 把关键词报表落下来。"
        )

    rows, skipped, notes = [], 0, []
    for p in picked[: ctx.limit]:
        text = (p.get("keyword_text") or "").strip()
        if not text:
            continue
        kid = str(p.get("keyword_id") or "").strip() or resolve_keyword_id(conn, market, text)
        if kid in existing:
            skipped += 1
            notes.append(f"{text}: 已在册，跳过")
            continue
        bid = p.get("bid")
        max_bid = round(float(bid) * 1.5, 2) if bid else None
        rows.append(blank(
            TARGET_COLS,
            market_id=market,
            keyword_id=kid,
            keyword_text=text,
            campaign_id=p.get("campaign_id"),
            ad_group_id=p.get("ad_group_id"),
            asin=ctx.asin,
            goal_page=1,
            goal_position=4,
            push_mode="bid",
            max_bid=max_bid,
            max_tos_pct=900.0,
            step_mode="fixed",
            step_value=0.10,
            success_streak_required=5,
            decrease_mode=None,
            decrease_value=None,
            restore_on_end=1,
            acos_guard_mult=1.5,
            max_pushes=6,
            status="active",
            created_at=now_iso(),
            expires_at=None,
        ))
        existing.add(kid)
        notes.append(f"{text}: 种入 (bid={bid}, max_bid={max_bid})")

    return {
        "plans": [("rank_target", TARGET_SQL, rows)],
        "after": None,
        "fetched": len(rows),
        "pages": 0,
        "note": (f"词源={source} 候选={len(picked)} 新增={len(rows)} 已存在={skipped}"
                 + ("；" + " | ".join(notes) if ctx.verbose and notes else "")),
    }


# ════════════════════════════════════════════════════════════════
# dataset: snapshot —— 位次快照
# ════════════════════════════════════════════════════════════════
def _collect_snapshot(c: Client, ctx, conn) -> dict:
    """逐个关键词问「我这个 ASIN 在这一词下的历次曝光位次」，落 rank_snapshot。

    为什么用 product_ranking_trend_by_keyword 而不是 keyword_search_results：
    后者只回前 20 名有机位的商品对象，**没有位次字段**。实测自己在
    `lat pulldown` 是 Page1 Pos20/50，却根本不在那个列表的前 20 里 ——
    它按销量排，不按位次排。拿它定位等于用错尺子。见模块 docstring 的表。

    每次调用返回 20 行（按时间倒序，约 5 个观测时刻 × organic/SP Ad/视频位）。
    --pages 控制翻几页：1 页够 engine 判「最新批次 + 连续到位」，
    想给面板画出更长的走势再往上加。

    查不到就跳过并记 note，绝不写 position=0：0 会被 in_goal() 判成「第 0 位」
    也就是「已经到位」，直接触发降价 —— 缺数据被当成好消息是最坏的失败方式。
    """
    market = ctx.market
    site = ctx.site.upper()
    asin = ctx.asin

    words: list[dict] = []
    seen_kid: set[str] = set()
    for t in _targets(conn, market):
        kid = str(t["keyword_id"])
        if kid in seen_kid:
            continue
        seen_kid.add(kid)
        words.append({"keyword_id": kid, "keyword_text": t["keyword_text"] or kid,
                      "from": "rank_target"})
    if not words:
        # rank_target 空时回退：先自己投过的词，再搜索词报表。
        for p in _kw_from_keyword_daily(conn, market, ctx.limit):
            kid = str(p["keyword_id"])
            if kid in seen_kid:
                continue
            seen_kid.add(kid)
            words.append({"keyword_id": kid, "keyword_text": p["keyword_text"],
                          "from": "fact_keyword_daily"})
        if len(words) < ctx.limit:
            have = {w["keyword_text"] for w in words}
            for p in _kw_from_search_terms(conn, market, ctx.limit):
                if p["keyword_text"] in have:
                    continue
                kid = resolve_keyword_id(conn, market, p["keyword_text"])
                if kid in seen_kid:
                    continue
                seen_kid.add(kid)
                words.append({"keyword_id": kid, "keyword_text": p["keyword_text"],
                              "from": "fact_search_term_daily"})
                if len(words) >= ctx.limit:
                    break
    words = words[: ctx.limit]

    if not words:
        raise RankError(
            f"没有可采的词：rank_target / fact_keyword_daily / fact_search_term_daily "
            f"都没有 market_id={market} 的数据。先跑 --dataset target-seed 或 ingest。"
        )

    rows: list[dict] = []
    hit, miss, dup_all, dup, dropped, inbatch = [], [], [], 0, 0, 0
    # 批内也要去重：用和写库 SQL 同一口径的 SNAP_KEY 六元组。
    # 注意「同一时刻同 placement 不同 page/position」不是重复 —— 那是自己
    # 在同一次抓取里占了两个坑位，必须都留下（见 SNAP_SQL 上方注释）。
    pending: set[tuple] = set()
    calls_before = c.calls
    for w in words:
        text = w["keyword_text"]
        try:
            data = []
            for page in range(1, ctx.pages + 1):
                got = c.data("product_ranking_trend_by_keyword",
                             {"asin": asin, "keyword": text, "amz_site": site, "page": page})
                if not got:
                    break
                data.extend(got)
                if len(got) < 20:
                    break
        except RankError as exc:
            # 单个词失败不该打死整批：记 note，继续下一个词。
            # （整批失败只发生在连接层，那种情况第一次调用就会抛出去。）
            miss.append(f"{text}: 调用失败 {exc}")
            continue

        kept = 0
        for r in data:
            pl = placement_of(r)
            if pl is None:
                dropped += 1          # 视频位/品牌位，见模块 docstring
                continue
            page_n, pos_n = _page_of(r.get("page")), _pos_of(r.get("position"))
            if page_n is None or pos_n is None:
                dropped += 1          # 位次文本解析不出来，不猜
                continue
            obs = _obs_ts(r.get("time"), ctx.as_of)
            row = blank(
                SNAP_COLS,
                market_id=market,
                keyword_id=w["keyword_id"],
                keyword_text=text,
                observed_at=obs,
                placement=pl,
                page=page_n,
                position=pos_n,
                asin=asin,
                is_self=1,
                postal_code=None,
                source="api",
                raw_json=json.dumps(r, ensure_ascii=False),
            )
            if _snap_exists(conn, row):
                dup += 1
                continue
            key = tuple(row[c] if c != "asin" else (row["asin"] or "") for c in SNAP_KEY)
            if key in pending:
                inbatch += 1
                continue
            pending.add(key)
            rows.append(row)
            kept += 1

        if kept:
            hit.append(f"{text}: +{kept}")
        elif not data:
            miss.append(f"{text}: 源端无位次（No relevant data）")
        else:
            # 源端有行、但全部已入库 —— 这不是「缺数据」，是「这次没新东西」。
            # 两者混在一起会让运维以为词采不到，所以分开计。
            dup_all.append(f"{text}: {len(data)} 行均已在库")

    note = (f"词={len(words)} 新增行={len(rows)} 已存在={dup} 批内重复={inbatch} "
            f"跳过非SP位={dropped} 源端无位次={len(miss)} 无新数据={len(dup_all)}")
    if ctx.verbose:
        if hit:
            note += "；命中 " + ", ".join(hit[:12])
        if dup_all:
            note += "；无新 " + ", ".join(x.split(":")[0] for x in dup_all[:12])
        if miss:
            note += "；缺 " + "; ".join(miss[:12])
    return {
        "plans": [("rank_snapshot", SNAP_SQL, rows)],
        "after": None,
        "fetched": len(rows) + dup + inbatch,
        "pages": c.calls - calls_before,
        "note": note,
    }


def _snap_exists(conn, row: dict) -> bool:
    """(keyword_id, observed_at, placement, asin, page, position) 六元组判重。

    表上没有唯一索引（是别的途径建的），所以判重只能在这里做；写库语句里
    还带了一层 `where not exists` 做原子兜底，两边同一口径（都走 SNAP_KEY）。

    为什么带 page/position：源端同一时刻会有同 placement 的多行不同位次
    （实测 3 组，见 SNAP_SQL 上方注释）。只按前四项判重会静默丢掉「同一次
    抓取里自己占了两个坑位」的观测。
    """
    try:
        r = conn.execute(
            "select 1 from rank_snapshot where keyword_id = ? and observed_at = ? "
            "and placement = ? and coalesce(asin, '') = coalesce(?, '') "
            "and coalesce(page, -1) = coalesce(?, -1) "
            "and coalesce(position, -1) = coalesce(?, -1) limit 1",
            (row["keyword_id"], row["observed_at"], row["placement"], row["asin"],
             row["page"], row["position"])).fetchone()
        return r is not None
    except sqlite3.OperationalError:
        return False


# ════════════════════════════════════════════════════════════════
# 注册表（顺序 = --dataset all 的执行顺序：先种目标，再采位次）
# ════════════════════════════════════════════════════════════════
DATASETS = {
    "target-seed": {
        "tools": [],
        "tables": ["rank_target"],
        "desc": "从自己高曝光关键词种卡位目标草稿（goal 1/4，max_bid=bid×1.5）",
        "collect": _collect_target_seed,
    },
    "snapshot": {
        "tools": ["product_ranking_trend_by_keyword"],
        "tables": ["rank_snapshot"],
        "desc": "逐词采自己的曝光位次（organic / sponsored，含历史序列）",
        "collect": _collect_snapshot,
    },
}


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════
def _fail(ctx_desc: str, exc: BaseException) -> None:
    """失败一律落在 data/rank_fail.log，一行 JSON 便于 grep 和后续告警。"""
    FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "at": now_iso(), "dataset": ctx_desc, "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(limit=6),
    }
    with FAIL_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[失败] {ctx_desc}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"[失败] 详情已写入 {FAIL_LOG}", file=sys.stderr)


def _log_run(conn, ctx, datasets: list[str], fetched: int, upserted: int, status: str,
             err: str | None, started: str, note: str) -> None:
    """登记到 run_log，和 ingest.py / market.py 的采集合账放在同一张表里。

    market_id 用真账户 id（和 market.py 不同：那边是外部站点行情，只能写 NULL；
    这边采的是自家账户在某个站点的位次，market_id=7 是确切归属，应该落上）。
    """
    conn.execute(
        "insert into run_log (dataset, market_id, start_date, end_date, pages, rows_fetched, "
        "rows_upserted, status, error, started_at, finished_at, note) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"rank.{'+'.join(datasets)}", ctx.market, ctx.today, ctx.today,
         None, fetched, upserted, status, err, started, now_iso(), note),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="位次快照采集（sorftime MCP）")
    ap.add_argument("--dataset", default="all",
                    help="snapshot / target-seed，可用逗号组合，或 all（= 先 target-seed 再 snapshot）")
    ap.add_argument("--market", type=int, default=DEFAULT_MARKET, help="market_id，默认 7（amazon-us）")
    ap.add_argument("--site", default="US", help="sorftime 站点代码 US/GB/DE/…")
    ap.add_argument("--asin", default=DEFAULT_ASIN, help=f"卡位对象 ASIN，默认 {DEFAULT_ASIN}")
    ap.add_argument("--limit", type=int, default=12, help="最多采/种多少个关键词，默认 12")
    ap.add_argument("--pages", type=int, default=1,
                    help="每个词翻几页位次趋势（20 行/页，约 5 个观测时刻），默认 1")
    ap.add_argument("--as-of", default=None,
                    help="把观测时间改标成这一天 YYYY-MM-DD（补历史用，时分保留源端值）")
    ap.add_argument("--dry-run", action="store_true", help="只取数不写库，打印将要写入的行数")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    today = datetime.now(CST).date().isoformat()
    ctx = argparse.Namespace(
        market=args.market, site=args.site, asin=args.asin.strip(),
        limit=max(1, args.limit), pages=max(1, args.pages),
        as_of=args.as_of, today=args.as_of or today, real_today=today,
        dry_run=args.dry_run, verbose=args.verbose,
    )

    wanted = [d.strip() for d in args.dataset.split(",") if d.strip()]
    if "all" in wanted:
        wanted = list(DATASETS)
    unknown = [d for d in wanted if d not in DATASETS]
    if unknown:
        print(f"[错误] 未知 dataset {unknown}，可选 {list(DATASETS)}", file=sys.stderr)
        return 2

    if not DB_PATH.exists():
        _fail("open-db", RankError(f"库不存在：{DB_PATH}"))
        return 2

    client = Client(verbose=args.verbose)
    started = now_iso()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("pragma busy_timeout = 5000")
        credit = client.credit()
        print(f"[开始] dataset={wanted} market={ctx.market} site={ctx.site} asin={ctx.asin} "
              f"limit={ctx.limit} pages={ctx.pages} dry_run={ctx.dry_run} 额度={credit}")

        # ── 第一步：全部取完。任何失败都在写库之前抛出 ──
        plans, afters, fetched, notes, pages = [], [], 0, [], 0
        for name in wanted:
            spec = DATASETS[name]
            print(f"\n[{name}] {spec['desc']}")
            try:
                got = spec["collect"](client, ctx, conn)
            except RankError as exc:
                _fail(f"dataset={name}", exc)
                return 2
            except Exception as exc:  # 解析/字段名意外，同样按失败处理
                _fail(f"dataset={name}", exc)
                return 2
            plans.extend(got["plans"])
            if got.get("after"):
                afters.append(got["after"])
            fetched += got["fetched"]
            pages += got["pages"]
            notes.append(f"{name}: {got['note']}")
            for table, _sql, rows in got["plans"]:
                print(f"  {table}: {len(rows)} 行待写")

        total = sum(len(rows) for _, _, rows in plans)
        if ctx.dry_run:
            for table, sql, rows in plans:
                print(f"\n[ddl] {table} insert =\n{sql}")
                if rows:
                    print(f"[sample] {json.dumps(rows[0], ensure_ascii=False, default=str)[:400]}")
            print(f"\n[dry-run] 不写库。共同步 {total} 行，API 调用 {client.calls} 次")
            print(f"[备注] {' | '.join(notes)}")
            return 0

        # ── 第二步：一个事务写完全部 ──
        upserted = 0
        try:
            conn.execute("begin")
            for table, sql, rows in plans:
                for row in rows:
                    conn.execute(sql, row)
                upserted += len(rows)
            extra = [fn(conn, ctx, plans) for fn in afters if fn is not None]
            note = " | ".join(notes + extra)
            _log_run(conn, ctx, wanted, fetched, upserted, "ok", None, started, note)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _fail("write", exc)
            return 3

        print(f"\n[完成] rows_fetched={fetched} rows_upserted={upserted} "
              f"pages={pages} calls={client.calls} 额度={client.credit()}")
        print(f"[备注] {' | '.join(notes)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
