"""驾驶舱六个面板。全部只读。

每个面板回一份「能被手写 SVG 直接画」的数据 —— 不给前端图表库留活，
也就不会出现「图好看但数字对不上」。

六个面板（用户 m00524 全选）：
  1. funnel        账户漏斗趋势（花费/点击/订单/ACOS/CVR 按日）
  2. portfolios    投放组合对比（各组合 CVR 与置信区间）
  3. conflicts     结构冲突（同词多活动 + 位次溢价上限占比）
  4. keywords      关键词词族排行
  5. market        外部市场变动（sorftime ABA 周榜 + 竞品动作 + 日行情）
  6. attribution   归因窗追踪（已改但还没到期的）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from api.db import read_conn

router = APIRouter()

CST = timezone(timedelta(hours=8))


def _days(n: int = 30) -> tuple[str, str]:
    today = datetime.now(CST).date()
    end = today - timedelta(days=2)      # 归因排除最后 2 天
    start = end - timedelta(days=n - 1)
    return start.isoformat(), end.isoformat()


@router.get("/funnel")
def funnel(days: int = 30, portfolio: str | None = None):
    start, end = _days(days)
    where = "report_date between ? and ?"
    params: list = [start, end]
    if portfolio:
        where += " and portfolio_name = ?"
        params.append(portfolio)
    with read_conn() as conn:
        rows = conn.execute(
            f"""select report_date,
                       sum(cost) cost, sum(clicks) clicks, sum(impressions) impressions,
                       sum(ads_orders) orders, sum(ads_sales) sales
                from fact_campaign_daily where {where}
                group by report_date order by report_date""", params
        ).fetchall()
    series = []
    for r in rows:
        cost = float(r["cost"] or 0)
        clicks = int(r["clicks"] or 0)
        orders = int(r["orders"] or 0)
        sales = float(r["sales"] or 0)
        series.append({
            "date": r["report_date"],
            "cost": round(cost, 2),
            "clicks": clicks,
            "impressions": int(r["impressions"] or 0),
            "orders": orders,
            "sales": round(sales, 2),
            "cpc": round(cost / clicks, 4) if clicks else None,
            "cvr": round(orders / clicks, 6) if clicks else None,
            "acos": round(cost / sales, 4) if sales else None,
        })
    tot_cost = sum(s["cost"] for s in series)
    tot_clicks = sum(s["clicks"] for s in series)
    tot_orders = sum(s["orders"] for s in series)
    tot_sales = sum(s["sales"] for s in series)
    return {
        "window": {"start": start, "end": end, "days": days},
        "series": series,
        "totals": {
            "cost": round(tot_cost, 2),
            "clicks": tot_clicks,
            "orders": tot_orders,
            "sales": round(tot_sales, 2),
            "cpc": round(tot_cost / tot_clicks, 4) if tot_clicks else None,
            "cvr": round(tot_orders / tot_clicks, 6) if tot_clicks else None,
            "acos": round(tot_cost / tot_sales, 4) if tot_sales else None,
            "aov": round(tot_sales / tot_orders, 2) if tot_orders else None,
        },
    }


@router.get("/portfolios")
def portfolios(days: int = 30):
    """组合层对比。带 Wilson 区间 —— 让人一眼看出「差是真的差，还是样本不够」。"""
    start, end = _days(days)
    with read_conn() as conn:
        rows = conn.execute(
            """select coalesce(portfolio_name,'(无)') name,
                      sum(clicks) clicks, sum(ads_orders) orders,
                      sum(cost) cost, sum(ads_sales) sales
               from fact_campaign_daily where report_date between ? and ?
               group by coalesce(portfolio_name,'(无)')
               order by sum(cost) desc""", (start, end)
        ).fetchall()
    import math

    def wilson(k: int, n: int, z: float = 1.96):
        if n == 0:
            return (0.0, 0.0)
        p = k / n
        d = 1 + z * z / n
        c = p + z * z / (2 * n)
        m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (round((c - m) / d, 6), round((c + m) / d, 6))

    out = []
    for r in rows:
        clicks, orders = int(r["clicks"] or 0), int(r["orders"] or 0)
        cost, sales = float(r["cost"] or 0), float(r["sales"] or 0)
        lo, hi = wilson(orders, clicks)
        out.append({
            "name": r["name"],
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "cvr": round(orders / clicks, 6) if clicks else None,
            "cvr_lo": lo,
            "cvr_hi": hi,
            "acos": round(cost / sales, 4) if sales else None,
        })
    return {"window": {"start": start, "end": end}, "portfolios": out}


@router.get("/conflicts")
def conflicts():
    """结构冲突：同词多活动 + 每个活动「最紧那档位次」占该档溢价上限的比例。

    ⚠ 口径（2026-11-17 勘误后定的，三条都是踩过的坑）：
    1. 比的是「该位次的 CPC ÷ 该位次的上限」，两个数必须同一位次。拿活动全位次
       均价去比单个位次的上限，等于拿平均价对顶价 —— lat pulldown 主活动全位次
       均价 $0.83 看着离上限很远，它 TOS 那档实际就是 $2.0000。
    2. 上限里的 bid 必须用**当天**的 bid，不能用今天的快照。bid 0.40 那个活动
       窗口内改过价，用现价算会得出「CPC 是上限 2.9 倍」这种不可能的结论。
    3. 溢价档位要把零点击的日子也算进来 —— 11-14 TOS 把 100% 改成 0%、当天才买到
       量，只看有点击的日子会以为一直是 0%。
    4. 所以这里不出「超上限」结论。比例 >1 一律标 mismatch = 「该位次当日配置与
       实际扣费对不上，去核那一天」——可能是当天改过配置、也可能是报表口径如此。
       把「贴到 100%」这种能被数据支撑的话说清楚，把证不了的机制主张删掉。
    """
    with read_conn() as conn:
        row = conn.execute(
            "select run_id, business_day, pack_json from memorial order by run_id desc limit 1"
        ).fetchone()
        if not row:
            raise HTTPException(404, "还没有 memorial")
        pack = json.loads(row["pack_json"])
        w_start, w_end = pack.get("window_start"), pack.get("window_end")
        out = []
        for c in pack.get("conflicts") or []:
            camps = []
            for cp in c.get("campaigns") or []:
                cid = cp.get("campaign_id")
                worst = None
                meta = {"adj_values": [], "strategies": [], "bid_values": []}
                if cid and w_start and w_end:
                    # 当天在用的 bid：一个活动可能挂多个关键词，取当天点击最多的那个词
                    bid_by_day: dict[str, tuple[float, int]] = {}
                    for r in conn.execute(
                        """select report_date, bid, clicks from fact_keyword_daily
                           where campaign_id=? and report_date between ? and ?""",
                        (cid, w_start, w_end),
                    ):
                        d0 = r["report_date"]
                        ck0 = int(r["clicks"] or 0)
                        prev = bid_by_day.get(d0)
                        if prev is None or ck0 > prev[1]:
                            bid_by_day[d0] = (r["bid"], ck0)

                    pl_rows = conn.execute(
                        """select report_date, placement, bid_adjustment, bid_strategy_name,
                                  sum(clicks) clicks, sum(cost) cost
                           from fact_placement_daily
                           where campaign_id=? and report_date between ? and ?
                           group by report_date, placement, bid_adjustment, bid_strategy_name""",
                        (cid, w_start, w_end),
                    ).fetchall()

                    seen: dict[str, dict] = {}
                    for r in pl_rows:
                        pl = r["placement"]
                        e = seen.setdefault(pl, {"adjs": [], "strategies": []})
                        adj = r["bid_adjustment"]
                        if adj is not None and round(float(adj), 1) not in e["adjs"]:
                            e["adjs"].append(round(float(adj), 1))
                        st = r["bid_strategy_name"]
                        if st and st not in e["strategies"]:
                            e["strategies"].append(st)
                        ck = int(r["clicks"] or 0)
                        if ck <= 0 or adj is None:
                            continue
                        bid_day = bid_by_day.get(r["report_date"], (None, 0))[0]
                        if not bid_day:
                            continue
                        cap = float(bid_day) * (1 + float(adj) / 100.0)
                        if cap <= 0:
                            continue
                        cpc = float(r["cost"] or 0) / ck
                        pct = cpc / cap
                        if worst is None or pct > worst["ratio"]:
                            worst = {
                                "placement": pl,
                                "report_date": r["report_date"],
                                "clicks": ck,
                                "cost": round(float(r["cost"] or 0), 2),
                                "cpc": round(cpc, 4),
                                "bid": float(bid_day),
                                "bid_adjustment": round(float(adj), 1),
                                "cap": round(cap, 4),
                                "ratio": round(pct, 4),
                                "strategy": st,
                            }

                    meta["adj_values"] = sorted({a for e in seen.values() for a in e["adjs"]})
                    meta["strategies"] = sorted({a for e in seen.values() for a in e["strategies"]})
                    meta["bid_values"] = sorted({round(float(b), 2) for b, _ in bid_by_day.values()})

                ratio = worst["ratio"] if worst else None
                changed = len(meta["bid_values"]) > 1 or len(meta["adj_values"]) > 1
                # 四种判定，刻意没有「超上限」这一档 —— 详见 docstring 第 4 条：
                # 报告里的溢价档位和当天实际配置并非总自洽，比例 >1 更可能是
                # 报表口径问题，不是真的买超了平台上限。说「对不上，去核那一天」
                # 比说「超上限了」有用，也不会骗人。
                flag = (
                    "unknown" if ratio is None
                    else "mismatch" if ratio > 1.0
                    else "near" if ratio >= 0.95
                    else "ok"
                )
                camps.append({
                    **cp,
                    "cpc_all_placements": cp.get("cpc"),   # 只说明规模，不作判定依据
                    "tightest": worst,
                    "bid_values": meta["bid_values"],
                    "adj_values": meta["adj_values"],
                    "strategies": meta["strategies"],
                    "config_changed": changed,
                    "flag": flag,
                })
            out.append({"keyword_text": c.get("keyword_text"), "campaigns": camps})
    abnormal = sum(1 for c in out for cp in c["campaigns"] if cp["flag"] != "ok")
    return {
        "run_id": row["run_id"],
        "business_day": row["business_day"],
        "window": {"start": w_start, "end": w_end},
        "conflict_count": len(out),
        "abnormal_count": abnormal,
        "conflicts": out,
    }


@router.get("/keywords")
def keywords(days: int = 30, limit: int = 30, by: str = "cost"):
    start, end = _days(days)
    order = {"cost": "sum(cost) desc", "orders": "sum(ads_orders) desc",
             "clicks": "sum(clicks) desc"}.get(by, "sum(cost) desc")
    with read_conn() as conn:
        rows = conn.execute(
            f"""select keyword_text, max(match_type) match_type,
                       count(distinct campaign_id) campaigns,
                       sum(clicks) clicks, sum(ads_orders) orders,
                       sum(cost) cost, sum(ads_sales) sales
                from fact_keyword_daily where report_date between ? and ?
                group by keyword_text order by {order} limit ?""",
            (start, end, max(1, min(limit, 200)))
        ).fetchall()
    out = []
    for r in rows:
        clicks, orders = int(r["clicks"] or 0), int(r["orders"] or 0)
        cost, sales = float(r["cost"] or 0), float(r["sales"] or 0)
        out.append({
            "keyword_text": r["keyword_text"],
            "match_type": r["match_type"],
            "campaign_count": int(r["campaigns"] or 0),
            "clicks": clicks,
            "orders": orders,
            "cost": round(cost, 2),
            "sales": round(sales, 2),
            "cpc": round(cost / clicks, 4) if clicks else None,
            "cvr": round(orders / clicks, 6) if clicks else None,
            "acos": round(cost / sales, 4) if sales else None,
        })
    return {"window": {"start": start, "end": end}, "keywords": out}


@router.get("/market")
def market(limit: int = 20):
    """外部市场变动。数据来自 `ingest/market.py` 采集的 sorftime 行情。

    两种状态必须分清，别混成一句话骗自己：
      - 表都没建        → not_connected（note 说表还没建）
      - 表建了但没数据  → not_connected（note 说还没采到数据，跑哪条命令能采）
      - 有数据          → connected + watched（竞品动作 + ABA 名次变动，按时间倒序）

    `watched` 的每一项都是一条「外部发生了什么」，前端照 ASIN/词 + 动作 + 变化 + 时间画表。
    """
    limit = max(1, min(limit, 100))
    with read_conn() as conn:
        tables = {r["name"] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
        missing = [t for t in ("aba_weekly", "competitor_move", "market_daily")
                   if t not in tables]
        if len(missing) == 3:
            # 一张都没有：这是「表还没建」，跑 migrate.py 就能补
            return {
                "status": "not_connected",
                "note": "外部市场数据表还没建。跑 python3 adsdesk/api/migrate.py 建表，"
                        "再用 ingest/market.py 采集。这一格暂时没有内容。",
                "watched": [],
            }
        counts = _market_counts(conn)
        watched = _market_moves(conn, limit)
        aba_top = _aba_top(conn, 10)
        latest = conn.execute(
            "select max(snapshot_week) from aba_weekly").fetchone()[0]
        # 表里一共多少个词。只印前 10 行却写「N 个词」会让人以为全部都在这里，
        # 所以标题里两个数都要给：「前 10 / 共 28」。
        aba_total = conn.execute(
            "select count(*) from aba_weekly where snapshot_week = ?", (latest,)).fetchone()[0]
        marks = _market_daily_latest(conn, 12)

    if not any(counts.values()):
        # 表在、三张表全空：说「还没采到」，不要说「还没建」—— 否则运维会去重复建表
        return {
            "status": "not_connected",
            "note": "表建好了，还没采到数据。跑 python3 ingest/market.py --dataset all "
                    "（先 --dry-run 看要写多少行）就能灌进来。",
            "counts": counts,
            "watched": [],
            "aba": [],
            "markets": [],
        }
    parts = []
    if counts["moves"]:
        parts.append(f"竞品动作 {counts['moves']} 条")
    if counts["aba"]:
        # 说「自家搜索词」而不是「周榜」：这张表装的是我们自己投过的词在 ABA 里的名次，
        # 不是全站热榜。用词不改，运维会以为漏采了全站榜单。
        parts.append(f"自家搜索词 ABA {counts['aba']} 个（最新 {latest}）")
    if counts["daily"]:
        parts.append(f"日行情 {counts['daily']} 条")
    if not counts["moves"]:
        # 动作是「和上一次快照比出来的」，第一次采集必然 0 条 —— 这不是「没采到」。
        # 行情已经在库里了，如实说清「还没攒出对照」，别让人以为采集失败又跑一遍。
        parts.append("还没有可比的上一份快照，暂未检出动作")
    return {
        "status": "connected",
        "note": "；".join(parts) if parts else None,
        "counts": counts,
        "latest_week": latest,
        "watched": watched,
        "aba": aba_top,
        "aba_total": aba_total,
        "markets": marks,
    }


# 动作/字段的中文名。前端直接显示，别再让它自己维护一份映射。
_MOVE_LABEL = {
    "price": "改价", "promotion": "报活动", "rating": "评价变化",
    "rank": "排名变化", "variants": "变体变化", "title": "改标题", "image": "改主图",
}
_FIELD_LABEL = {
    "price": "售价", "list_price": "原价", "coupon": "优惠券", "star_rating": "星级",
    "review_count": "评论数", "variation_count": "变体数", "subcategory_rank": "子类排名",
    "title": "标题", "main_image": "主图", "rank_gain": "周名次",
}


def _market_counts(conn) -> dict:
    def one(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except Exception:
            return 0
    return {
        "aba": one("select count(*) from aba_weekly"),
        "moves": one("select count(*) from competitor_move"),
        "daily": one("select count(*) from market_daily"),
        "watchlist": one("select count(*) from competitor_watchlist where active=1"),
    }


def _market_moves(conn, limit: int) -> list[dict]:
    """竞品动作 + ABA 名次变动合成一条时间线。

    两边形状不同（一个是 old/new 字符串，一个是名次差），这里统一成
    {kind, subject, label, move_type, old_value, new_value, delta, observed_at}
    让前端只画一张表，也就不用在前端再判断数据来源。
    """
    out: list[dict] = []
    try:
        for r in conn.execute(
            """select asin, label, market_id, observed_at, observed_date, move_type,
                      field, old_value, new_value, delta, source
               from competitor_move order by observed_date desc, observed_at desc limit ?""",
                (limit,)):
            out.append({
                "kind": "competitor", "subject": r["asin"], "label": r["label"],
                "move_type": r["move_type"],
                "move_label": _MOVE_LABEL.get(r["move_type"], r["move_type"]),
                "field": r["field"], "field_label": _FIELD_LABEL.get(r["field"], r["field"]),
                "old_value": r["old_value"], "new_value": r["new_value"],
                "delta": r["delta"], "observed_at": r["observed_at"],
                "observed_date": r["observed_date"], "source": r["source"],
            })
    except Exception:
        pass
    try:
        week = conn.execute("select max(snapshot_week) from aba_weekly").fetchone()[0]
        if week:
            # 只取「名次真的动过」的词：rank_gain 为 0 或没上周可比的不算动作。
            # 但**光看绝对值会全是噪音**：名次 150 万的长尾词（月搜索量 95）一周跳
            # 42 万名，绝对值比 dumbbell set 跳 142 名大三个数量级，排在时间线最上面
            # 让人以为「外面出大事了」。名次是长尾分布，同一绝对变动在深处的意义远小于
            # 在头部。所以按**相对变动**排序：|gain| / 当前名次，越大说明这个词在自己
            # 的量级里动得越猛。再兜一个下限，月搜索量 < 500 的词不进时间线 ——
            # 那些词每周排名本来就是随机抖动，谈不上「外面发生了什么」。
            for r in conn.execute(
                """select search_term, search_term_cn, snapshot_week, snapshot_date,
                          search_frequency_rank, rank_gain, search_volume, market_id, source
                   from aba_weekly
                   where snapshot_week = ? and rank_gain is not null and rank_gain <> 0
                     and coalesce(search_volume, 0) >= 500
                   order by abs(cast(rank_gain as real)) / max(search_frequency_rank, 1) desc
                   limit ?""",
                    (week, limit)):
                gain = r["rank_gain"]
                cur = r["search_frequency_rank"]
                out.append({
                    "kind": "aba", "subject": r["search_term"],
                    "label": r["search_term_cn"] or r["search_term"],
                    "move_type": "rank", "move_label": "周名次",
                    "field": "rank_gain", "field_label": "名次变动",
                    # rank_gain = 上周名次 - 本周名次，正数是名次上升；反推上周名次给前端显示
                    "old_value": None if (cur is None or gain is None) else str(cur + gain),
                    "new_value": None if cur is None else str(cur),
                    "delta": gain, "observed_at": r["snapshot_date"],
                    "observed_date": r["snapshot_date"], "source": r["source"],
                    # 相对变动要一起给前端：光看 -423700 会以为天塌了，看到「占当前
                    # 名次的 28%」才知道那是个 150 万名的长尾词。百分比自己算一份，
                    # 前后端各算一遍容易一处改了一处忘了。
                    "rel_pct": None if (cur or 0) <= 0 or gain is None
                               else round(abs(gain) / cur * 100, 1),
                    "note": f"{r['snapshot_week']} 搜索量 {r['search_volume']}",
                })
    except Exception:
        pass
    out.sort(key=lambda x: (x.get("observed_at") or ""), reverse=True)
    return out[:limit]


def _aba_top(conn, limit: int) -> list[dict]:
    """最新一周的 ABA 榜单前 N —— 面板上给一眼「外面在搜什么」。"""
    try:
        week = conn.execute("select max(snapshot_week) from aba_weekly").fetchone()[0]
        if not week:
            return []
        return [{
            "week": r["snapshot_week"], "term": r["search_term"],
            "term_cn": r["search_term_cn"], "rank": r["search_frequency_rank"],
            "rank_gain": r["rank_gain"], "search_volume": r["search_volume"],
            "click_share": r["click_share"], "cpc_bid": r["cpc_bid"],
            "top3_brand_share": r["top3_brand_share"],
        } for r in conn.execute(
            """select snapshot_week, search_term, search_term_cn, search_frequency_rank,
                      rank_gain, search_volume, click_share, cpc_bid, top3_brand_share
               from aba_weekly where snapshot_week = ?
               order by search_frequency_rank limit ?""", (week, limit))]
    except Exception:
        return []


def _market_daily_latest(conn, limit: int) -> list[dict]:
    """每个 subject 取最近一天：品类价位带 / 竞品快照都在这张表里。"""
    try:
        return [{
            "subject_type": r["subject_type"], "subject_id": r["subject_id"],
            "subject_name": r["subject_name"], "report_date": r["report_date"],
            "avg_price": r["avg_price"], "top100_sales_volume": r["top100_sales_volume"],
            "top3_brand_share": r["top3_brand_share"], "product_count": r["product_count"],
            "price": r["price"],
            # 竞品在它自己那个子类里的排名（product_detail 的 sub_category[0][2]）。
            # 读的是节点内名次，不是 category_products 那个更大范围的总榜排名。
            "subcategory_rank": r["subcategory_rank"],
        } for r in conn.execute(
            """select subject_type, subject_id, subject_name, report_date,
                      avg_price, top100_sales_volume, top3_brand_share, product_count, price,
                      subcategory_rank
               from market_daily d
               where report_date = (select max(report_date) from market_daily x
                                    where x.subject_type = d.subject_type
                                      and x.subject_id = d.subject_id)
               order by subject_type, subject_id limit ?""", (limit,))]
    except Exception:
        return []


def _competitor_watchlist(conn) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(
            "select asin, label, active from competitor_watchlist where active=1")]
    except Exception:
        return []


@router.get("/attribution")
def attribution():
    """归因窗追踪：已标「已改」但还没到期的，以及到期该回填的。"""
    with read_conn() as conn:
        open_rows = conn.execute(
            """select decision_id, run_id, rule_id, entity_type, entity_id, entity_label,
                      action_type, current_value, proposed_value, executed_value,
                      executed_at, executed_by, attribution_due_at, confidence,
                      spend_impact, outcome_7d_json, outcome_14d_json
               from decision_log
               where status='executed' and outcome_filled_at is null
               order by attribution_due_at"""
        ).fetchall()
        done_rows = conn.execute(
            """select decision_id, rule_id, entity_type, entity_id, entity_label,
                      action_type, executed_at, attribution_due_at, outcome_filled_at,
                      outcome_7d_json, outcome_14d_json
               from decision_log where outcome_filled_at is not null
               order by outcome_filled_at desc limit 20"""
        ).fetchall()
    now = datetime.now(CST).isoformat(timespec="seconds")
    open_out = []
    for r in open_rows:
        d = dict(r)
        seen7 = _load_json(d.pop("outcome_7d_json", None))
        seen14 = _load_json(d.pop("outcome_14d_json", None))
        due = d.get("attribution_due_at")
        d["overdue"] = bool(due and due <= now)
        d["days_left"] = _days_left(due)
        # 一条执行会看两次：到期看 7 天，再往后看 14 天终局。
        # 页面要能分清「还没看过」和「看过一次、等终局」，否则到期那条会
        # 一直挂红却没人知道已经填过一次了。
        d["seen_7d"] = bool(seen7 and not seen7.get("pending"))
        d["seen_14d"] = bool(seen14 and not seen14.get("pending"))
        d["next_window"] = 14 if d["seen_7d"] else 7
        d["outcome_7d"] = seen7
        open_out.append(d)
    due_now = sum(1 for d in open_out if d["overdue"])
    filled = []
    for r in done_rows:
        d = dict(r)
        d["outcome_7d"] = _load_json(d.pop("outcome_7d_json", None))
        d["outcome_14d"] = _load_json(d.pop("outcome_14d_json", None))
        filled.append(d)
    return {
        "now": now,
        "open": open_out,
        "open_count": len(open_out),
        "due_count": due_now,
        "filled": filled,
        "filled_count": len(filled),
    }


def _load_json(raw):
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _days_left(due: str | None) -> int | None:
    if not due:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z",):
        try:
            dt = datetime.strptime(due, fmt)
            return (dt.date() - datetime.now(CST).date()).days
        except ValueError:
            continue
    try:
        return (datetime.fromisoformat(due).date() - datetime.now(CST).date()).days
    except ValueError:
        return None


@router.get("/summary")
def summary():
    """首屏六格的一张总表，省得前端并发打 6 个接口。"""
    out = {}
    for name, fn in (("funnel", funnel), ("portfolios", portfolios),
                     ("conflicts", conflicts), ("keywords", keywords),
                     ("market", market), ("attribution", attribution)):
        try:
            out[name] = fn()
        except HTTPException as e:
            out[name] = {"error": e.detail, "status": "empty"}
        except Exception as e:  # 单格失败不拖垮整页
            out[name] = {"error": f"{type(e).__name__}: {e}", "status": "error"}
    return out

# ════════════════════════════════════════════════════════════════
# 位次面板（第七格，独立路由，不进 /summary）
#
# 数据来自 ingest/rank.py 采集的 rank_snapshot + 人工/脚本种下的 rank_target。
# 为什么单独一格而不是塞进 /summary：卡位（R31）是 P1 的主动动作，它要看的
# 「我现在第几位」和其余六格的「花了多少 / 结构对不对」不是同一类判断，
# 而且它天然依赖外部抓取，随时可能是空的 —— 混进首屏总表会让整页的「有数据」
# 变成「部分没数据」。分开之后，这一格空就只是这一格空。
# ════════════════════════════════════════════════════════════════

# 面板走势给几个点。5 个足够看出「在往上走还是在掉」，再多也读不过来。
_RANK_SERIES_POINTS = 5


def _rank_targets(conn) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(
            """select target_id, keyword_id, keyword_text, asin, goal_page, goal_position,
                      push_mode, max_bid, max_pushes, status, created_at
               from rank_target
               where status in ('active', 'paused')
               order by target_id""")]
    except Exception:
        return []


def _rank_latest(conn, keyword_id: str) -> dict | None:
    """这个词最新一次观测里「该看哪一行」。

    同一批次里通常同时有 organic 和 sponsored 两行（实测 lat pulldown machines
    在 2026-11-23 05:15 有 organic 37 和 sponsored 7）。面板只展示一个「当前位次」，
    所以必须先定口径：**优先 sponsored**。

    理由是 engine/facts.py 的 rank_state() 只读 placement='sponsored' —— 卡位加价
    打的是关键词竞价位，自然位再高也不受 bid 影响。面板如果显示 organic，
    就会出现「面板说第 37 位、引擎说第 7 位」这种两套数字打架的情况。
    自然位仍然保留在 series 之外的地方（数据库里都在），只是不作为「当前位次」。
    """
    try:
        # 先找「最新的广告位观测」，不管它是不是最新那一批。
        # 这一点踩过：wall mounted cable machine 的最新批次是 2026-11-21 的 organic，
        # 而它的 sponsored 只有 2026-11-06 一条。如果先取最新批次再在批次内找
        # sponsored，就会退回 organic 并把自然位当成「当前位次」——
        # 而 engine 的 rank_state() 只读 sponsored，于是面板说 48、引擎说 56，两套数字打架。
        # 广告位旧就旧，把 observed_at 一起亮出来，让「过期」看得见，而不是换成别的轨道。
        r = conn.execute(
            """select observed_at, placement, page, position, asin, source
               from rank_snapshot
               where keyword_id = ? and placement = 'sponsored'
               order by observed_at desc, position asc
               limit 1""", (keyword_id,)).fetchone()
        if r:
            return dict(r)
        # 这个词压根没有广告位观测（纯自然位词）：退回 organic，placement 字段
        # 会如实写 organic，前端据此标注，不冒充广告位。
        r = conn.execute(
            """select observed_at, placement, page, position, asin, source
               from rank_snapshot
               where keyword_id = ? and placement = 'organic'
               order by observed_at desc, position asc
               limit 1""", (keyword_id,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _rank_series(conn, keyword_id: str, placement: str, n: int) -> list[dict]:
    """该词最近 n 个观测点（同一条轨道：跟随 latest 的 placement）。

    为什么不把 organic 和 sponsored 混成一条线：两条轨道的数字没有可比性
    （实测同一个词同一时刻 organic 37 / sponsored 7），混着画出来的「走向」
    是噪声而不是趋势。

    为什么同一 observed_at 只取一行：源端一次抓取里，同一个词可能同时占多个
    坑位（实测 `lat pulldown machines` 2026-11-22T05:01 sponsored 有 p3/26 和
    p3/34 两条）。取批次里 position 最小的那条 —— 与 engine `rank_state()` 的
    `order by observed_at desc, position asc` 同口径，否则面板和引擎会拿同一
    批次里不同的行算出不同的「当前位次」，两套数字打架。
    """
    try:
        rows = conn.execute(
            """select observed_at, page, position
               from rank_snapshot
               where keyword_id = ? and placement = ? and position is not null
               order by observed_at desc, position asc""", (keyword_id, placement)).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    seen_ts: set[str] = set()
    for r in rows:                      # 已按 observed_at desc 排好，逐批取首行
        ts = str(r["observed_at"])
        if ts in seen_ts:
            continue
        seen_ts.add(ts)
        out.append({"observed_at": ts, "page": r["page"], "position": r["position"]})
        if len(out) >= n:
            break
    return list(reversed(out))


def _rank_pushes(conn) -> dict[str, int]:
    """每个词已经推过几次。口径必须和 engine 完全一致，否则面板和引擎会对不上：
    rule_id='R31' + action_type='rank_push' + status in (proposed/approved/executed)。
    entity_id 存的是 keyword_id。没有行 = 0（当前就是这个状态：R31 还没产出过动作）。
    """
    try:
        return {str(r["entity_id"]): int(r["n"] or 0) for r in conn.execute(
            """select entity_id, count(*) n from decision_log
               where rule_id = 'R31' and action_type = 'rank_push'
                 and status in ('proposed','approved','executed')
               group by entity_id""")}
    except Exception:
        return {}


@router.get("/rank")
def rank():
    """位次面板：每个卡位词的当前位次 / 离目标差几位 / 已推次数 / 最近走向。

    三态（和 /market 同一个规矩，别把「没建表」和「没采到数」说成一句话）：
      - 两张表都没建  → not_connected，note 指向 migrate
      - 表在但没目标  → empty，note 指向 --dataset target-seed
      - 有目标没快照  → 仍然返回 targets（latest_* 全 None），note 指向 --dataset snapshot
      - 有数据        → targets + series + summary
    """
    with read_conn() as conn:
        tables = {r["name"] for r in conn.execute(
            "select name from sqlite_master where type='table'")}
        missing = [t for t in ("rank_snapshot", "rank_target") if t not in tables]
        if missing:
            return {
                "status": "not_connected",
                "note": f"位次表还没建（缺 {', '.join(missing)}）。"
                        "跑 python3 adsdesk/api/migrate.py 建表，"
                        "再用 python3 ingest/rank.py --dataset all 采集。",
                "targets": [], "series": {},
                "summary": {"n_targets": 0, "n_have_data": 0, "n_on_goal": 0},
            }

        tgt_rows = _rank_targets(conn)
        n_snap = conn.execute("select count(*) from rank_snapshot").fetchone()[0]
        pushes = _rank_pushes(conn)

        if not tgt_rows:
            # 快照可能有、目标没有：这种状态下位次能看，但没有「该到第几位」的标尺。
            # 空表提示按用户要求给到 snapshot 那条命令；同时把 target-seed 也写上，
            # 因为只有快照没目标时 R31 仍然不会动。
            note = ("位次快照还没采集，先跑 `python3 ingest/rank.py --dataset snapshot`。"
                    if not n_snap else
                    f"已有 {n_snap} 条位次快照，但还没有卡位目标 —— 跑 "
                    "`python3 ingest/rank.py --dataset target-seed` 种一批，"
                    "R31 才有「该到第几位」的判据。")
            return {
                "status": "empty",
                "note": note,
                "targets": [], "series": {},
                "summary": {"n_targets": 0, "n_have_data": 0, "n_on_goal": 0},
            }

        targets, series = [], {}
        n_have = n_on_goal = 0
        for t in tgt_rows:
            kid = str(t["keyword_id"])
            label = t["keyword_text"] or kid
            goal_page = int(t["goal_page"] or 1)
            goal_pos = int(t["goal_position"] or 4)
            latest = _rank_latest(conn, kid)

            if latest and latest.get("position") is not None:
                n_have += 1
                page, pos = latest.get("page"), latest.get("position")
                # 跨页不可比：第 2 页第 13 位离「第 1 页第 4 位」差多少，
                # 取决于每页多少个坑位，不能拿 position 相减冒充。给 None，前端画 —。
                gap = (pos - goal_pos) if (page or 99) == goal_page else None
                on_goal = ((page or 99) <= goal_page and (pos or 999) <= goal_pos)
                if on_goal:
                    n_on_goal += 1
                series[label] = _rank_series(conn, kid, latest["placement"],
                                             _RANK_SERIES_POINTS)
            else:
                page = pos = None
                gap = None
                on_goal = False

            targets.append({
                "keyword_id": kid,
                "keyword_text": label,
                "goal_page": goal_page,
                "goal_position": goal_pos,
                "latest_position": pos,
                "latest_page": page,
                "latest_placement": (latest or {}).get("placement"),
                "latest_asin": (latest or {}).get("asin"),
                "observed_at": (latest or {}).get("observed_at"),
                "gap_vs_goal": gap,
                "on_goal": on_goal,
                "pushes_used": pushes.get(kid, 0),
                "max_pushes": int(t["max_pushes"] or 6),
                "status": t["status"],
            })

        if not n_snap:
            note = ("位次快照还没采集，先跑 `python3 ingest/rank.py --dataset snapshot`。"
                    f"（已有 {len(targets)} 个卡位目标在等位次数据）")
        elif not n_have:
            note = (f"有 {n_snap} 条快照，但没有一条对得上这 {len(targets)} 个卡位目标 —— "
                    "多半是 keyword_id 口径不一致（快照按搜索词、目标按投放词）。"
                    "跑 `python3 ingest/rank.py --dataset snapshot` 会按 rank_target 的词重采。")
        else:
            note = (f"{n_have}/{len(targets)} 个词有位次数据，{n_on_goal} 个已在目标位。"
                    "当前位次取最新一次观测的广告位（sponsored）；自然位仅入库不参与判定。")

    return {
        "status": "connected" if n_have else "empty",
        "targets": targets,
        "series": series,
        "summary": {"n_targets": len(targets), "n_have_data": n_have,
                    "n_on_goal": n_on_goal},
        "note": note,
    }
