#!/usr/bin/env python3
"""外部市场数据采集（计划书 P4 第 23 项）：ABA 周榜 / 竞品动作 / 品类日行情。

为什么单独一层：积加接口只有「自家广告」，平台现在所有判断都是内向的。主人要的
分析层另一半在**外面** —— 每周 ABA 搜索趋势、竞品改价报活动改图、品类价位带漂移。
这块只能走 sorftime MCP，和 ingest.py 的积加口径完全两套，所以另起一个采集器。

三条纪律：
1. 连接层复用 tools/sorftime_probe.py 的 rpc()，不复制第二份。探针里踩过的坑
   （本机代理会掐断 SSE，所以要 ProxyHandler({})）只应该存在一处。
2. 一个 dataset 先把数据**全部取完**，再一次性写库。中途任何失败 => 整批不写、
   只落日志。半截数据比没数据更坏：面板会显示「已接入」，实际只有第一页。
3. 幂等靠 schema.sql 里的唯一索引 + on conflict do update，不做「先删再插」。
   重跑行数不变，是唯一索引在起作用，不是运气。

工具名不写死：`--list-tools` 打网关的真实清单（48 个），每个 dataset 用哪个工具、
参数怎么拼，都集中在下面 collect_* 函数里，改的时候只看一处。

用法：
  python3 market.py --list-tools
  python3 market.py --seed                        # 用自己的搜索词反查竞品，灌 watchlist
  python3 market.py --dataset aba --terms 30     # 自家搜索词的 ABA
  python3 market.py --dataset competitor --with-content
  python3 market.py --dataset market --node-id 3408401
  python3 market.py --dataset all --date 2026-11-19 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB_PATH = Path(os.environ.get("ADS_DB", HERE / "data" / "ads.sqlite3"))
FAIL_LOG = HERE / "data" / "market_fail.log"

# 复用探针的连接层。sys.path 而不是复制一份代码：SSE/代理那些坑只有一处需要维护。
sys.path.insert(0, str(ROOT / "tools"))
from sorftime_probe import DEFAULT_URL, rpc  # noqa: E402

CST = timezone(timedelta(hours=8))


# ════════════════════════════════════════════════════════════════
# 小工具
# ════════════════════════════════════════════════════════════════
def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def row_hash(*parts) -> str:
    """幂等用的内容指纹。只用于 debug/对账 —— 真正的唯一性由唯一索引保证。"""
    raw = json.dumps(list(parts), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def as_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_int(v):
    n = as_num(v)
    return None if n is None else int(round(n))


def _top_window(real_today: str) -> tuple[str, str]:
    """category_products 的日期窗口：结束 = 真实今天 − 2 天，往前回看 3 天。

    为什么必须给（2026-11-21~23 连续三天品类行情全灭的根因）：
    - 只传 {node_id, amz_site}，网关直接报错
      `An error occurred invoking 'category_products'`；
    - 传了日期但 query_date 晚于「今天 − 2」，网关不报错、返回 None（空），
      照样采不到。实测今天=2026-11-23 时 2026-11-21..2026-11-23 就是空。
    - 可用区间：跨度 3–40 天，且结束日 ≤ 今天 − 2。3 天窗最省额度又不会空。
    所以窗口统一从这里算，不把日期算术散到调用点上。
    """
    end = date.fromisoformat(real_today) - timedelta(days=2)
    start = end - timedelta(days=2)
    return start.isoformat(), end.isoformat()


def pick(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def _data(obj):
    """网关返回形状不统一：多数套一层 {"data": ...}，category_name_search 直接给数组。
    统一从这一个出口取，写库代码里就不会出现 obj["data"] 这种猜测式写法。"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return obj.get("data")
    return None


def node_rank(d: dict) -> int | None:
    """从 `sub_category` 里取「该 node 内的排名」。

    为什么不能读 `d["subcategory_rank"]`：`product_detail` 根本没有这个字段
    （它的 doc 里只有 sub_category），于是 asin 行的 subcategory_rank 长期
    全是 NULL —— 面板上「子类排名」一列永远空着，看着像源端没数据，
    实际是取错了键。

    实测两种返回形状：
      product_detail   sub_category = [["Rowers", "3407791", "4", "20261119"]]
      category_products sub_category = [["Rowers", "3407791", 1]]
    第三位就是节点内排名（Concept2 在 Rowers 里第 4 名）。值可能是字符串，
    统一走 as_int。

    注意 category_products 里另有一个 `subcategory_rank`（实测 3102、8075…），
    那是更大范围的总榜排名，和这里的「节点内第几名」不是一回事。两者都保留，
    但叫「子类排名」的那个必须是节点内的这个。
    """
    sc = d.get("sub_category")
    if not isinstance(sc, list) or not sc:
        return None
    first = sc[0]
    if not isinstance(first, list) or len(first) < 3:
        return None
    return as_int(first[2])


def node_of(d: dict) -> str | None:
    """同上的第二个位置：node_id。"""
    sc = d.get("sub_category")
    if not isinstance(sc, list) or not sc:
        return None
    first = sc[0]
    if not isinstance(first, list) or len(first) < 2:
        return None
    v = first[1]
    return None if v in (None, "") else str(v)


def as_num_pos(v):
    """数值列取正数，负数一律当「没值」。

    sorftime 对「这格没算出来」不给 null，给 -1（实测 cpc=-1.0、cpo=-1.0）。
    直接落库会变成「建议出价 -$1」，比缺数据更坏：面板上分不清是数据错误还是
    真的没有建议价，下游任何 sum/avg 都会被它拖偏。
    """
    n = as_num(v)
    return None if n is not None and n < 0 else n


def _norm_date(v, fallback: str) -> str:
    """data_update_time 是 '20261117' 这种紧凑格式，统一成 YYYY-MM-DD 再入表。"""
    s = str(v or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 10 and s[4] == "-":
        return s
    return fallback


def _iso_week(day: str) -> str:
    """周标识用 ISO 周（2026-W46）。ABA 本来就是周榜，用周做主键维度比日稳。"""
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def upsert_sql(table: str, cols: list[str], key: list[str], keep: tuple[str, ...] = ("created_at",)) -> str:
    """从列名生成 upsert 语句：唯一索引列 = key，冲突时覆盖其余列。

    集中生成的好处：改表结构只改 cols 一处，不会出现「INSERT 有 35 个问号、
    VALUES 只有 34 个」这种经典事故。keep 里的列首次写入后不再被覆盖。
    """
    not_updatable = set(key) | set(keep)
    upd = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in not_updatable)
    sql = (
        f"insert into {table} ({', '.join(cols)}) "
        f"values ({', '.join(':' + c for c in cols)})"
    )
    if upd:
        sql += f" on conflict({', '.join(key)}) do update set {upd}"
    else:
        sql += f" on conflict({', '.join(key)}) do nothing"
    return sql


def blank(cols: list[str], **kw) -> dict:
    """先铺满 None 再覆盖：命名占位符要求每个 key 都存在，缺列就等于 NULL。"""
    row = {c: None for c in cols}
    row.update(kw)
    return row


# ════════════════════════════════════════════════════════════════
# 表结构（列名与 schema.sql 的 ⑦ 区块一一对应）
# ════════════════════════════════════════════════════════════════
ABA_COLS = [
    "snapshot_week", "snapshot_date", "market_id", "search_term", "search_term_cn",
    "search_frequency_rank", "rank_gain", "search_volume", "click_share",
    "conversion_share", "top3_brand_share", "cpc_bid", "peak_season",
    "source", "row_hash", "raw_json", "ingested_at", "created_at",
]
ABA_KEY = ["market_id", "snapshot_week", "search_term"]

MOVE_COLS = [
    "asin", "market_id", "observed_at", "observed_date", "move_type", "field",
    "old_value", "new_value", "delta", "label", "source",
    "row_hash", "raw_json", "created_at",
]
MOVE_KEY = ["asin", "market_id", "observed_date", "move_type", "field"]

# id 不进 insert，其余按 schema.sql 顺序抄一遍
MD_COLS = [
    "report_date", "market_id", "subject_type", "subject_id", "subject_name",
    "top100_sales_volume", "top100_sales_amount", "avg_price", "min_price", "max_price",
    "top3_brand_share", "top3_seller_share", "amazon_owned_share",
    "return_rate", "conversion_rate", "product_count", "avg_listing_days",
    "price_bands_json", "bsr_bands_json", "review_bands_json",
    "price", "list_price", "coupon", "star_rating", "review_count", "review_growth",
    "variation_count", "subcategory_rank", "title", "main_image", "search_volume",
    "source", "row_hash", "raw_json", "created_at",
]
MD_KEY = ["report_date", "market_id", "subject_type", "subject_id"]

ABA_SQL = upsert_sql("aba_weekly", ABA_COLS, ABA_KEY, keep=("created_at",))
MOVE_SQL = upsert_sql("competitor_move", MOVE_COLS, MOVE_KEY, keep=("created_at",))
MD_SQL = upsert_sql("market_daily", MD_COLS, MD_KEY, keep=("created_at",))


# ════════════════════════════════════════════════════════════════
# MCP 客户端
# ════════════════════════════════════════════════════════════════
class MarketError(RuntimeError):
    """采集失败的唯一异常类型。main() 只认它，避免把 KeyboardInterrupt 也吃成「采集失败」。"""


class Client:
    """sorftime MCP 的薄封装：只负责 JSON-RPC 信封和 tools/call 的 content 拆包。
    连接层的坑（SSE 多行 data、本机代理）在 tools/sorftime_probe.py 里已解决。"""

    def __init__(self, url: str = DEFAULT_URL, verbose: bool = False):
        self.url = url
        self.verbose = verbose
        self.calls = 0
        # 进程内缓存：aba 与 market 两个 dataset 都会调 keyword_detail，撞上同一个词时
        # 没必要付两次额度。只缓存**成功返回**的结果，失败照旧抛出 —— 把异常也缓存起来
        # 会让 `--dataset all` 里第一个 dataset 的网络抖动毒掉第二个 dataset。
        self._cache: dict[str, object] = {}

    def call(self, tool: str, args: dict | None = None):
        payload = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }
        self.calls += 1
        try:
            res = rpc(self.url, payload)
        except Exception as exc:  # 网络/证书/HTTP 层
            raise MarketError(
                f"{tool}({json.dumps(args or {}, ensure_ascii=False)}) 调用失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        texts = [c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text"]
        raw = "\n".join(texts).strip()
        # 网关的工具级报错走 isError=true + 一句英文，不是 JSON-RPC error。
        # 不先看这个标志，就会把它当成「返回的不是 JSON」，报错信息丢掉真正原因。
        if res.get("isError"):
            raise MarketError(
                f"{tool}({json.dumps(args or {}, ensure_ascii=False)}) 网关报错：{raw or '(无正文)'}"
            )
        if not raw:
            raise MarketError(f"{tool} 返回空 content（result keys={list(res.keys())}）")
        # 网关对「没有这个词的数据」不返回 JSON，直接给一句纯文本 "No data available."。
        # 这是**正常的空结果**，不是故障：长尾词没进 ABA 榜、某个 ASIN 没有券，都会这样。
        # 当成异常抛，会让整批采集因为其中一个词没数据而全灭（2026-11-19 采 30 个自家
        # 搜索词时就是这么炸的）。只精确匹配这两句，别的一律照旧抛 —— 不能拿它兜住
        # 真正的坏响应。
        if raw.strip().rstrip(".").strip().lower() in ("no data available", "no data"):
            obj = {}
        else:
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                raise MarketError(f"{tool} 返回的不是 JSON：{raw[:300]}") from exc
        # 网关会把业务错误塞进 200 响应体，必须显式挑出来，否则会写一批空行进库
        if isinstance(obj, dict) and obj.get("code") not in (None, 0, "0", 200):
            raise MarketError(
                f"{tool} 业务错误 code={obj.get('code')} "
                f"msg={obj.get('msg') or obj.get('message') or obj}"
            )
        if self.verbose:
            print(f"  · {tool}({json.dumps(args or {}, ensure_ascii=False)}) ok", file=sys.stderr)
        return obj

    def call_cached(self, tool: str, args: dict | None = None):
        """同一个 (tool, args) 在本进程内只真正调用一次。

        只给「按词逐个查」这类重复率高的只读接口用（keyword_detail）。
        缓存的是已解析的 JSON 对象，调用方只读不改 —— 所以不需要深拷贝。
        """
        key = tool + "|" + json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        obj = self.call(tool, args)
        self._cache[key] = obj
        return obj

    def credit(self):
        """剩余额度。每次跑之前打一行，额度见底时能提前从日志看出来。"""
        try:
            d = _data(self.call("common_usage_query", {})) or {}
            return d.get("credit_left")
        except MarketError:
            return None


def list_tools(url: str = DEFAULT_URL) -> list[dict]:
    res = rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    return res.get("tools") or []


# ════════════════════════════════════════════════════════════════
# dataset: aba —— ABA 搜索词周榜
# ════════════════════════════════════════════════════════════════
ABA_RANK_GAIN_SQL = """
update aba_weekly set rank_gain = coalesce((
    select prev.search_frequency_rank - aba_weekly.search_frequency_rank
    from aba_weekly prev
    where prev.market_id = aba_weekly.market_id
      and prev.search_term = aba_weekly.search_term
      and prev.snapshot_week < aba_weekly.snapshot_week
    order by prev.snapshot_week desc
    limit 1
), aba_weekly.rank_gain)
where aba_weekly.market_id = ? and aba_weekly.snapshot_week = ?
"""


def backfill_aba_rank_gain(conn, ctx, plans) -> str:
    """rank_gain 必须是「和上一周比」，靠单次采集是算不出来的：有的词这周才进榜，
    有的上周漏采了。所以写完统一用 SQL 回填，窗口天然对齐，重跑也不会算错。

    只认 aba_weekly 那一个 plan。`plans` 里还装着 market_daily / competitor_move 的行，
    它们的字典没有 snapshot_week 键 —— 不筛表名就会 KeyError，而且是在 begin 之后、
    commit 之前抛，整批采集全被回滚（2026-11-19 首次灌生产库就是这么失败的）。
    """
    weeks = {
        (r["market_id"], r["snapshot_week"])
        for table, _sql, rows in plans if table == "aba_weekly"
        for r in rows
        if r.get("snapshot_week")
    }
    # coalesce 不可省：库里只有一周时子查询返回空，裸赋值会把 rank_gain 写成 NULL，
    # 把源端 keyword_detail 直接给的 rank_change_of_weekly 一起抹掉（那才是首周唯一
    # 能拿到的环比）。有第二周之后，自算值优先 —— 自己库里的两周口径一致，比源端
    # 「上周」具体指哪一周更可控。
    for market, week in weeks:
        conn.execute(ABA_RANK_GAIN_SQL, (market, week))
    return f"rank_gain 回填 {len(weeks)} 个 (site, week)"


def _collect_aba(c: Client, ctx, conn) -> dict:
    """ABA 周榜 —— 只采**自己投过的搜索词**，不采全站热搜榜。

    2026-11-19 之前的实现调 `keyword_list`（全站搜索热榜），采回来的是
    needoh / toilet paper / ps5 / ipad / elf —— 一个健身器材卖家拿这张表
    和自家广告对不上任何一行。表格填得满，和决策无关，等于没采。

    改成逐个 `keyword_detail`：它给的正是 ABA 需要的字段 ——
    `weekly_search_rank`（本周名次）、`rank_change_of_weekly`（周环比）、
    `update`（这周的 ABA 更新日）、`monthly_search_volume`（近 30 天搜索量）、
    `share_click_rate` / `share_conversion_rate`（Top3 商品点击/转化占比）、
    `cpc`（精确匹配建议价）、`peak_season`。
    代价是**每个词一次调用**，所以用 --terms 限量（默认 30），按窗口内
    曝光/点击降序取 —— 先采真花过钱的词，没花钱的词晚点再说。

    三条不能省的推理：

    1. `rank_change_of_weekly` 的符号与我们这列**相反**。源端给「本周 - 上周」，
       名次变小是变好，所以它给负数（实测 lat pulldown 本周 312031、环比
       -36255，意味着上周 348286 —— 名次是往前走的）。schema 里 rank_gain 是
       「上周 - 本周，正数 = 名次上升」。必须取反，否则周变动整列符号全反，
       面板上看着像全线滑坡。
    2. `search_frequency_rank` 是 NOT NULL。长尾词没进 ABA 榜时该字段为 null，
       这时**跳过并计数**，不写 0 —— 0 会被读成「排名第一」，比缺数据坏得多。
       这类词的真实含义是「这个词还不在 ABA 榜上」，要如实报出来。
    3. `update` 是 ABA 官方更新日（yyyyMMdd），比 --date 更能决定「这行属于哪一周」：
       ABA 名次是按周发布的，用采集日当周锚会在跨周那天把两周的行算成同一周。
       只有在源端没给 update 时才退回 ctx.date。

    拿不到的东西也照实说：`top3_brand` 只有品牌名、没有份额，所以
    top3_brand_share 一律 NULL（原先注释的「keyword_detail 只给品牌名」仍然成立）。
    """
    site = ctx.site.upper()
    market = ctx.site.lower()

    terms: list[str] = list(ctx.keywords)
    if not terms:
        terms = _own_terms(conn, ctx.terms)
    if not terms:
        raise MarketError(
            "没有可采的搜索词：库里 fact_search_term_daily 为空，也没给 --keyword。"
            "ABA 只能按自己的词采，先跑一次 ingest 把搜索词落下来。"
        )

    ts = now_iso()
    out: list[dict] = []
    no_rank: list[str] = []
    empty: list[str] = []
    no_brand: list[str] = []
    weeks: set[str] = set()

    for term in terms:
        d = _data(c.call_cached("keyword_detail",
                                {"keyword": term, "keyword_support_site": site}))
        if not isinstance(d, dict) or not d:
            empty.append(term)
            continue
        rank = as_int(d.get("weekly_search_rank"))
        if rank is None:
            # 没进 ABA 榜：宁可不写，也不写个 0 冒充满分
            no_rank.append(term)
            continue
        snap_date = _norm_date(d.get("update"), ctx.date or ctx.today)
        week = _iso_week(snap_date)
        weeks.add(week)

        # 源端「本周 - 上周」→ 我们「上周 - 本周」，见 docstring 第 1 条
        delta = as_int(d.get("rank_change_of_weekly"))
        gain = None if delta is None else -delta

        # cpc 用 as_num_pos：没算出建议价时源端给 -1，落库要变 NULL 而不是负数
        rng = d.get("cpc_range")
        cpc = as_num_pos(d.get("cpc"))
        if cpc is None and isinstance(rng, str) and "-" in rng:
            cpc = as_num_pos(rng.split("-", 1)[0])

        # Top3 品牌销量占比：keyword_detail 只给品牌名（top3_brand = ["Mikolo", ...]），
        # 没有占比，所以这一列长期是 NULL。但 keyword_search_results 每一项都带
        # brand + monthly_sales_volume_share_in_top100（形如 "48.35%"），而一页 20 项
        # 的份额合计实测恰好 100（lat pulldown 三页：99.98 / 100.01 / 100.01；
        # rowing machine 首页 100.01），说明它就是「该词前 100 名的销量占比」，
        # 按品牌加总取前三就是 Top3 品牌销量占比，口径与
        # category_report.top3_brand_sales_volume_share 一致。
        # 代价：每词多 1 额度（实测 credit_left 29422 → 29421）。
        brand_share = None
        try:
            lst = _data(c.call_cached("keyword_search_results", {
                "keyword": term, "keyword_support_site": site,
                "page": 1, "position_type": 1}))
            agg: dict[str, float] = {}
            for it in (lst if isinstance(lst, list) else []):
                if not isinstance(it, dict):
                    continue
                b = str(it.get("brand") or "").strip() or "（无品牌）"
                v = as_num(str(it.get("monthly_sales_volume_share_in_top100") or "").rstrip("%"))
                if v:
                    agg[b] = agg.get(b, 0.0) + v
            if agg:
                top = sorted(agg.items(), key=lambda kv: -kv[1])[:3]
                brand_share = round(sum(v for _, v in top), 2)
        except Exception as exc:
            no_brand.append(f"{term}（{exc}）")

        out.append(blank(
            ABA_COLS,
            snapshot_week=week, snapshot_date=snap_date, market_id=market,
            search_term=term, search_term_cn=d.get("keyword_cn_name"),
            search_frequency_rank=rank, rank_gain=gain,
            search_volume=as_int(d.get("monthly_search_volume")),
            click_share=as_num(d.get("share_click_rate")),
            conversion_share=as_num(d.get("share_conversion_rate")),
            top3_brand_share=brand_share,
            cpc_bid=cpc, peak_season=d.get("peak_season"),
            source="sorftime.keyword_detail",
            row_hash=row_hash(market, week, term, rank, snap_date),
            raw_json=json.dumps(d, ensure_ascii=False), ingested_at=ts, created_at=ts,
        ))

    bits = [f"keyword_detail site={site} 词 {len(terms)} 个 → 写入 {len(out)} 行"]
    if no_brand:
        bits.append(f"⚠ {len(no_brand)} 个词没算出 Top3 品牌占比：" + "、".join(no_brand[:3]))
    if weeks:
        bits.append(f"周={','.join(sorted(weeks))}")
    if no_rank:
        bits.append(f"⚠ {len(no_rank)} 个词没有 ABA 名次（没进榜，未写入）："
                    + "、".join(no_rank[:5]) + ("…" if len(no_rank) > 5 else ""))
    if empty:
        bits.append(f"⚠ {len(empty)} 个词没有数据（网关回 No data available）："
                    + "、".join(empty[:5]) + ("…" if len(empty) > 5 else ""))
    note = "；".join(bits)

    return {
        "plans": [("aba_weekly", ABA_SQL, out)],
        "fetched": len(terms), "pages": 1,
        "note": note,
        "after": backfill_aba_rank_gain,
    }


# ════════════════════════════════════════════════════════════════
# dataset: competitor —— 竞品动作（改价/报活动/改图/改标题/新增变体）
# ════════════════════════════════════════════════════════════════
# (market_daily 列, move_type, 中文名, 是否数值列)
CMP_FIELDS = (
    ("price", "price", "售价", True),
    ("list_price", "price", "原价", True),
    ("coupon", "promotion", "优惠券", True),
    ("star_rating", "rating", "星级", True),
    ("review_count", "rating", "评论数", True),
    ("variation_count", "variants", "变体数", True),
    ("subcategory_rank", "rank", "子类排名", True),
    ("title", "title", "标题", False),
    ("main_image", "image", "主图", False),
)


def _diff(prev: sqlite3.Row | None, cur: dict) -> list[dict]:
    """和上一份快照逐字段对比。首日没有前值就一条都不产出 ——
    「从无到有」不是动作，编出来的 move 面板上全是噪音。"""
    if prev is None:
        return []
    out = []
    for col, mtype, _cn, numeric in CMP_FIELDS:
        old, new = prev[col], cur[col]
        if new is None or old == new:
            continue  # 源端这格没给（有的 ASIN 没券）就当没变化，不猜
        if old is None and not numeric:
            # 标题/主图这类文本列：旧值为空只可能意味着「上一份快照根本没采这格」
            # （比如昨天没开 --with-content），绝不是「原来是空图」。拿它当对比基准
            # 会刷出一整批假「改主图」。数值列不一样：空券 → 有券是真实的报活动。
            continue
        delta = None
        if numeric:
            a, b = as_num(old), as_num(new)
            if a is not None and b is not None:
                delta = round(b - a, 4)
        out.append({"move_type": mtype, "field": col,
                    "old_value": None if old is None else str(old),
                    "new_value": str(new), "delta": delta})
    return out


def _prev_snapshot(conn, asin: str, market: str, day: str):
    return conn.execute(
        "select * from market_daily where subject_type = 'asin' and subject_id = ? "
        "and market_id = ? and report_date < ? order by report_date desc limit 1",
        (asin, market, day),
    ).fetchone()


def _collect_competitor(c: Client, ctx, conn) -> dict:
    site = ctx.site.upper()
    market = ctx.site.lower()
    asins = list(ctx.asins)
    if not asins:
        asins = [r[0] for r in conn.execute(
            "select asin from competitor_watchlist where active = 1 order by created_at, asin")]
    if not asins:
        raise MarketError(
            "competitor_watchlist 是空的，也没给 --asins；先跑 --seed 用自己的搜索词反查竞品"
        )

    ts = now_iso()
    snaps, moves, fetched = [], [], 0
    for asin in asins:
        d = _data(c.call("product_detail", {"asin": asin, "amz_site": site}))
        if not isinstance(d, dict) or not d:
            continue
        fetched += 1
        brand = str(d.get("brand") or "").strip()
        title = str(d.get("title") or "").strip()
        row = blank(
            MD_COLS,
            report_date=ctx.today, market_id=market, subject_type="asin", subject_id=asin,
            subject_name=(f"{brand} · {title[:40]}".strip(" ·") or asin),
            price=as_num(d.get("price")), list_price=as_num(d.get("list_price")),
            coupon=as_num(d.get("coupon")), star_rating=as_num(d.get("star_rating")),
            review_count=as_int(d.get("review_count")),
            variation_count=as_int(d.get("variation_count")),
            # product_detail 没有 subcategory_rank 字段，排名在 sub_category[0][2]
            subcategory_rank=node_rank(d),
            title=title or None,
            source="sorftime.product_detail",
            raw_json=json.dumps(d, ensure_ascii=False), created_at=ts,
        )
        if ctx.with_content:
            # 主图只有 product_content 给（product_detail 没有），所以改图检测要多一次调用
            pc = _data(c.call("product_content", {"asin": asin, "amz_site": site})) or {}
            imgs = pc.get("main_image") if isinstance(pc, dict) else None
            if isinstance(imgs, list) and imgs:
                row["main_image"] = str(imgs[0])
            row["raw_json"] = json.dumps({"detail": d, "content": pc}, ensure_ascii=False)

        prev = _prev_snapshot(conn, asin, market, ctx.today)
        if prev is not None and row["review_count"] is not None and prev["review_count"] is not None:
            days = (date.fromisoformat(ctx.today) - date.fromisoformat(prev["report_date"])).days or 1
            row["review_growth"] = round((row["review_count"] - prev["review_count"]) / days, 2)

        row["row_hash"] = row_hash(
            asin, ctx.today, row["price"], row["star_rating"], row["review_count"],
            row["variation_count"], row["subcategory_rank"], row["title"], row["main_image"])
        snaps.append(row)

        for mv in _diff(prev, row):
            moves.append(blank(
                MOVE_COLS,
                asin=asin, market_id=market, observed_at=ts, observed_date=ctx.today,
                move_type=mv["move_type"], field=mv["field"],
                old_value=mv["old_value"], new_value=mv["new_value"], delta=mv["delta"],
                label=row["subject_name"], source="sorftime.product_detail",
                row_hash=row_hash(asin, ctx.today, mv["move_type"], mv["field"],
                                  mv["old_value"], mv["new_value"]),
                raw_json=json.dumps({"asin": asin, **mv}, ensure_ascii=False),
                created_at=ts,
            ))
    return {
        "plans": [("market_daily", MD_SQL, snaps), ("competitor_move", MOVE_SQL, moves)],
        "fetched": fetched, "pages": 1,
        "note": f"product_detail x{len(asins)} asin, 动作 {len(moves)} 条"
                + ("（含 product_content 主图）" if ctx.with_content else ""),
        "after": None,
    }


# ════════════════════════════════════════════════════════════════
# dataset: market —— 品类/关键词日行情（价位带、排名带、评论分布）
# ════════════════════════════════════════════════════════════════
# 固定边界而不是分位数：分位数每天都会动，看不出「高价带在变厚」这种趋势
PRICE_BANDS = ((0, 25), (25, 50), (50, 100), (100, 200), (200, None))
RANK_BANDS = ((1, 6), (6, 21), (21, 51), (51, 101), (101, None))
REVIEW_BANDS = ((0, 51), (51, 201), (201, 1001), (1001, None))


def _bands(values: list, bands) -> list[dict]:
    nums = [v for v in values if v is not None]
    total = len(nums)
    out = []
    for lo, hi in bands:
        n = sum(1 for v in nums if v >= lo and (hi is None or v < hi))
        out.append({
            "band": f"{lo:g}+" if hi is None else f"{lo:g}-{hi:g}",
            "count": n,
            "share": round(n * 100.0 / total, 2) if total else 0.0,
        })
    return out


def _own_terms(conn, limit: int = 3) -> list[str]:
    """自己真投过的搜索词。比手填类目名靠谱：这些词是我们花钱买到过的流量。"""
    return [r[0] for r in conn.execute(
        "select search_term from fact_search_term_daily "
        "where search_term is not null and trim(search_term) <> '' "
        "group by search_term order by sum(impressions) desc, sum(clicks) desc limit ?",
        (limit,))]


def _share_word(term: str, category_name: str) -> bool:
    """搜索词和类目名有没有共同的实词（≥4 字母）。

    只做「同词干」这种粗判：adjustable dumbbells ↔ Dumbbells 命中；
    rowing machines for home foldable ↔ Portable Washers 不命中。
    不做语义判断 —— 判不准就跳过类目行情，不拿错类目污染行情表。
    """
    a = {w.strip("s") for w in (term or "").lower().replace("-", " ").split() if len(w) >= 4}
    b = {w.strip("s") for w in (category_name or "").lower().replace("-", " ").split() if len(w) >= 4}
    return bool(a & b)


def _collect_market(c: Client, ctx, conn) -> dict:
    site = ctx.site.upper()
    market = ctx.site.lower()
    ts = now_iso()
    rows, notes, fetched = [], [], 0

    nodes = list(ctx.node_ids)
    if not nodes:
        # 没指定类目就用自己投过的高曝光搜索词去换 node_id。
        # category_name_search 给的是「相近类目」列表，实测第一个常常完全不搭
        # （搜索词 "rowing machines for home foldable" → 第一命中 "Portable Washers"），
        # 直接取第一个会往 market_daily 里写一条错类目的行情，比不写更糟。
        # 所以要求类目名和搜索词至少共享一个实词（≥4 字母），否则换下一个候选；
        # 全都不搭就跳过类目行情并在 note 里说明 —— 宁可少一格，不留假数据。
        for term in _own_terms(conn, 3):
            res = _data(c.call("category_name_search", {"category_name": term, "amz_site": site}))
            picked = None
            for cand in (res if isinstance(res, list) else []):
                if not isinstance(cand, dict) or not cand.get("node_id"):
                    continue
                if _share_word(term, str(cand.get("category_name") or "")):
                    picked = cand
                    break
            if picked:
                nodes.append(str(picked["node_id"]))
                notes.append(f"node {picked['node_id']} ← 搜索词「{term}」({picked.get('category_name')})")
                break
            notes.append(f"搜索词「{term}」的反查类目都不搭（{len(res) if isinstance(res, list) else 0} 个候选），已跳过")
    if not nodes and not ctx.keywords:
        raise MarketError("没有可用 node_id：给 --node-id，或库里先有搜索词可反查")

    # category_products 必须带日期窗口，见 _top_window 的注释；这里只算一次，全节点共用
    q_start, q_end = _top_window(ctx.real_today)
    kept_nodes = 0

    for node in nodes:
        # category_report 不需要日期，别给它加参数
        rep = _data(c.call("category_report", {"node_id": node, "amz_site": site})) or {}
        prods = _data(c.call("category_products", {
            "node_id": node, "amz_site": site,
            "query_start": q_start, "query_date": q_end,
        }))
        fetched += 1
        # 空结果不整批崩、也不写半截：记一笔 note 跳过该节点，继续下一个。
        # _data() 对空窗口会返回 None，别直接拿去迭代。
        if not isinstance(prods, list) or not prods:
            notes.append(f"category {node}: category_products 在窗口 {q_start}..{q_end} 返回空，跳过该节点")
            continue
        fetched += 1
        if not isinstance(rep, dict):
            rep = {}
        prices = [as_num(p.get("price")) for p in prods if isinstance(p, dict)]
        # 分档要用「节点内第几名」。category_products 里的 subcategory_rank
        # 实测是 3102 / 8075 / 11090 这种更大范围的总榜排名，直接分档会让
        # 1-6 / 6-21 / 21-51 / 51-101 四档全空、100% 落进「101+」——
        # 那不是「这个类目的商品都很靠后」，是分母选错了。
        ranks = [node_rank(p) for p in prods if isinstance(p, dict)]
        reviews = [as_int(p.get("rating_count")) for p in prods if isinstance(p, dict)]
        # category_products_price 语义（来自 doc）：avg卖价, min, max, avg利润, max利润, min利润
        cp = rep.get("category_products_price") or []
        if not (isinstance(cp, list) and len(cp) >= 3):
            cp = []
        avg_reviews = round(sum(v for v in reviews if v is not None)
                            / max(1, sum(1 for v in reviews if v is not None)), 2) if reviews else None
        # category_report 不返回类目名，只有 node_id。拿 top100 商品的 sub_category
        # （实测形如 [['Dumbbells', '3408401', 1]]，嵌套一层）把名字补上，
        # 否则面板上只剩一串数字没人看得懂。取不到就留空，不猜。
        node_name = None
        for p in prods:
            if not isinstance(p, dict) or node_of(p) != str(node):
                continue
            sc = p.get("sub_category")
            first = sc[0] if isinstance(sc, list) and sc and isinstance(sc[0], list) else None
            if first and first[0]:
                node_name = str(first[0])
                break
        row = blank(
            MD_COLS,
            report_date=ctx.today, market_id=market, subject_type="category",
            subject_id=node, subject_name=node_name,
            top100_sales_volume=as_int(rep.get("top100_monthly_sales_volume")),
            top100_sales_amount=as_num(rep.get("top100_monthly_sales_amount")),
            avg_price=as_num(cp[0]) if cp else None,
            min_price=as_num(cp[1]) if cp else None,
            max_price=as_num(cp[2]) if cp else None,
            top3_brand_share=as_num(rep.get("top3_brand_sales_volume_share")),
            top3_seller_share=as_num(rep.get("top3_seller_sales_volume_share")),
            amazon_owned_share=as_num(rep.get("amazon_owned_sales_volume_share")),
            return_rate=as_num(rep.get("return_rate")),
            conversion_rate=as_num(rep.get("conversion_rate")),
            product_count=as_int(rep.get("category_product_number")),
            avg_listing_days=as_int(rep.get("product_age_avg_days") or rep.get("avg_listing_days")),
            price_bands_json=json.dumps(_bands(prices, PRICE_BANDS), ensure_ascii=False),
            bsr_bands_json=json.dumps(_bands(ranks, RANK_BANDS), ensure_ascii=False),
            review_bands_json=json.dumps(_bands(reviews, REVIEW_BANDS), ensure_ascii=False),
            review_count=None if avg_reviews is None else int(round(avg_reviews)),
            source="sorftime.category_report+category_products",
            raw_json=json.dumps({"report": rep, "top_products": prods}, ensure_ascii=False),
            created_at=ts,
        )
        prev = conn.execute(
            "select * from market_daily where subject_type = 'category' and subject_id = ? "
            "and market_id = ? and report_date < ? order by report_date desc limit 1",
            (node, market, ctx.today)).fetchone()
        if prev is not None and row["review_count"] is not None and prev["review_count"] is not None:
            days = (date.fromisoformat(ctx.today) - date.fromisoformat(prev["report_date"])).days or 1
            row["review_growth"] = round((row["review_count"] - prev["review_count"]) / days, 2)
        row["row_hash"] = row_hash(node, ctx.today, row["avg_price"], row["top100_sales_volume"],
                                   row["review_count"], row["product_count"])
        rows.append(row)
        kept_nodes += 1
        notes.append(f"category {node}: 窗口 {q_start}..{q_end} "
                     f"top100 月销 {row['top100_sales_volume']} "
                     f"均价 {row['avg_price']} 商品 {row['product_count']}")

    # 所有节点都空 = 这条链路整体没通。按「中途失败整批不写」的纪律抛错，
    # 不要静默写几条关键词行充数，让 run_log 显示 ok。
    if nodes and kept_nodes == 0:
        raise MarketError(
            f"category_products 在窗口 {q_start}..{q_end} 对所有节点都返回空（{nodes}），整批不写"
        )

    for kw in ctx.keywords:
        d = _data(c.call_cached("keyword_detail", {"keyword": kw, "keyword_support_site": site})) or {}
        if not isinstance(d, dict) or not d:
            continue
        fetched += 1
        fr = d.get("search_result_first_page_stats") or {}
        row = blank(
            MD_COLS,
            report_date=ctx.today, market_id=market, subject_type="keyword", subject_id=kw,
            subject_name=d.get("keyword_cn_name"),
            search_volume=as_int(d.get("monthly_search_volume")),
            # 首页坑位/竞争度：广告位占比是「这个词还能不能挤进去」的直接指标
            product_count=as_int(fr.get("product_count")) if isinstance(fr, dict) else None,
            top3_brand_share=None,  # keyword_detail 只给品牌名，不给份额
            price=as_num(d.get("cpc")),      # 关键词行 price 列借用来存 CPC，见 note
            conversion_rate=as_num(d.get("search_conversion_rate")),
            source="sorftime.keyword_detail",
            raw_json=json.dumps(d, ensure_ascii=False), created_at=ts,
        )
        row["row_hash"] = row_hash(kw, ctx.today, row["search_volume"], row["price"])
        rows.append(row)
        notes.append(f"keyword 「{kw}」月搜索 {row['search_volume']} cpc {row['price']}")

    return {
        "plans": [("market_daily", MD_SQL, rows)],
        "fetched": fetched, "pages": 1,
        "note": "; ".join(notes) or "无数据",
        "after": None,
    }


# ════════════════════════════════════════════════════════════════
# watchlist 种子
# ════════════════════════════════════════════════════════════════
def seed_watchlist(conn, c: Client, ctx) -> int:
    """用自己投过的搜索词去 product_search 反查竞品，灌进 competitor_watchlist。

    为什么不手填 ASIN：库里没有任何「自己家 ASIN」的清单，无法排除自家链接；
    但 watchlist 只是「要盯的对象」，把自家也算进去无害（自己的改价本来就该看）。
    """
    terms = _own_terms(conn, 2)
    if not terms:
        raise MarketError("fact_search_term_daily 里没有搜索词，--seed 无从下手")
    added = 0
    for term in terms:
        rows = _data(c.call("product_search",
                            {"keyword": term, "amz_site": ctx.site.upper(), "page": 1})) or []
        for it in rows[:5]:
            asin = str(it.get("asin") or "").strip()
            if not asin:
                continue
            label = f"{it.get('brand') or ''} · {str(it.get('title') or '')[:40]}".strip(" ·")
            cur = conn.execute(
                "insert into competitor_watchlist (asin, label, watch_dims, thresholds_json, active, created_at) "
                "values (?,?,?,?,1,?) on conflict(asin) do nothing",
                (asin, label or asin, "price,promotion,rating,rank,variants,title,image", None, now_iso()))
            added += cur.rowcount
    conn.commit()
    print(f"[seed] 搜索词 {terms} → 新增 {added} 个竞品 ASIN（已存在的跳过）")
    return added


# ════════════════════════════════════════════════════════════════
# 注册表
# ════════════════════════════════════════════════════════════════
DATASETS = {
    "aba": {
        "tools": ["keyword_detail"],
        "tables": ["aba_weekly"],
        "desc": "自家搜索词的 ABA 周榜（逐词 keyword_detail，--terms 限量）",
        "collect": _collect_aba,
    },
    "competitor": {
        "tools": ["product_detail", "product_content"],
        "tables": ["market_daily", "competitor_move"],
        "desc": "竞品日快照 + 动作（改价/报活动/改图/改标题/新增变体）",
        "collect": _collect_competitor,
    },
    "market": {
        "tools": ["category_name_search", "category_report", "category_products", "keyword_detail"],
        "tables": ["market_daily"],
        "desc": "品类日行情（价位带/排名带/评论分布）+ 关键词行情",
        "collect": _collect_market,
    },
}


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════
def _fail(ctx_desc: str, exc: BaseException) -> None:
    """失败一律落在 data/market_fail.log，一行 JSON 便于 grep 和后续告警。"""
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
    """登记到 run_log，和 ingest.py 的采集合账放在同一张表里。

    market_id 留 NULL：run_log.market_id 是积加的整数账户 id，而外部市场用站点
    代码（us/gb…）。硬塞字符串会污染那一列的类型假设，站点写进 note 更干净。
    """
    conn.execute(
        "insert into run_log (dataset, market_id, start_date, end_date, pages, rows_fetched, "
        "rows_upserted, status, error, started_at, finished_at, note) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"market.{'+'.join(datasets)}", None, ctx.date or ctx.today, ctx.date or ctx.today,
         None, fetched, upserted, status, err, started, now_iso(), note),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="外部市场数据采集（sorftime MCP）")
    ap.add_argument("--list-tools", action="store_true", help="列出 sorftime 提供的工具及必填参数")
    ap.add_argument("--dataset", default="all",
                    help="aba / competitor / market，可用逗号组合，或 all")
    ap.add_argument("--date", default=None, help="采集日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--site", default="US", help="站点代码 US/GB/DE/…（写库时落成小写 market_id）")
    ap.add_argument("--pages", type=int, default=3, help="品类型目翻页数（aba 不再翻页）")
    ap.add_argument("--terms", type=int, default=30,
                    help="ABA 采多少个自家搜索词（每个词一次调用，默认 30）")
    ap.add_argument("--node-id", action="append", default=[], help="类目 node_id，可重复")
    ap.add_argument("--asin", action="append", default=[], help="指定竞品 ASIN，可重复（优先于 watchlist）")
    ap.add_argument("--keyword", action="append", default=[], help="指定关键词取 keyword_detail，可重复")
    ap.add_argument("--with-content", action="store_true", help="竞品额外取 product_content（主图/卖点）")
    ap.add_argument("--seed", action="store_true", help="用自家搜索词反查竞品，灌 competitor_watchlist")
    ap.add_argument("--dry-run", action="store_true", help="只取数不写库，打印将要写入的行数")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.list_tools:
        try:
            tools = list_tools()
        except Exception as exc:
            _fail("tools/list", exc)
            return 2
        print(f"[ok] tools/list count={len(tools)}")
        for t in tools:
            schema = t.get("inputSchema") or {}
            req = schema.get("required") or []
            props = list((schema.get("properties") or {}).keys())
            print(f"  - {t.get('name'):34s} required={req} props={len(props)}")
            desc = (t.get("description") or "").strip().replace("\n", " ")
            if desc:
                print(f"      {desc[:150]}")
        return 0

    today = datetime.now(CST).date().isoformat()
    ctx = argparse.Namespace(
        # today = 这一批数据归属的日期（--date 给了就用它），real_today = 真正的今天。
        # 两个必须分开：判「这是不是历史周」只能拿真今天比，否则 --date 传什么都没有历史感。
        site=args.site, date=args.date, today=args.date or today, real_today=today,
        pages=max(1, args.pages), terms=max(1, args.terms),
        node_ids=args.node_id, asins=args.asin,
        keywords=args.keyword, with_content=args.with_content,
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
        _fail("open-db", MarketError(f"库不存在：{DB_PATH}"))
        return 2

    client = Client(verbose=args.verbose)
    started = now_iso()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("pragma busy_timeout = 5000")
        if args.seed:
            try:
                seed_watchlist(conn, client, ctx)
            except Exception as exc:
                _fail("seed", exc)
                return 2

        credit = client.credit()
        print(f"[开始] dataset={wanted} site={ctx.site} date={ctx.today} "
              f"dry_run={ctx.dry_run} 额度={credit}")

        # ── 第一步：全部取完。任何失败都在写库之前抛出 ──
        plans, afters, fetched, notes, pages = [], [], 0, [], 0
        for name in wanted:
            spec = DATASETS[name]
            print(f"\n[{name}] {spec['desc']}")
            try:
                got = spec["collect"](client, ctx, conn)
            except MarketError as exc:
                _fail(f"dataset={name}", exc)
                return 2
            except Exception as exc:  # 解析/字段名意外，同样按失败处理
                _fail(f"dataset={name}", exc)
                return 2
            for plan in got["plans"]:
                plans.append(plan)
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
                print(f"\n[ddl] {table} upsert =\n{sql}")
                if rows:
                    print(f"[sample] {json.dumps(rows[0], ensure_ascii=False, default=str)[:400]}")
            print(f"\n[dry-run] 不写库。共同步 {total} 行，API 调用 {client.calls} 次")
            return 0

        # ── 第二步：一个事务写完全部，回填 hook 也在事务里 ──
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