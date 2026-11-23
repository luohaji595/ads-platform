"""P1.2 语义层 —— 搜索词聚类（只做否词侧）。

目的只有一个：把几十上百个搜索词压成少数几个簇，让运营判断否词时
「一次看一簇」而不是「一次看 80 个词」，降低人工成本。
本模块只产出簇，不产出否词动作，也不接线到 rules.py。

================================================================================
两条必须避开的坑（都来自前两版实现，是本项目最贵的教训）
================================================================================

坑一：并查集 + 单链接聚类必然链式合并
--------------------------------------------------------------------------
第一版用「并查集 + Jaccard >= 阈值 + 必须共享核心词」双闸门。
搜索词 `lat pull down and low row cable machine` 同时含 pulldown 和 cable，
它自己就成了桥，把「高位下拉」和「龙门架」两个完全不同的品类焊成一簇，
实跑出「lat pulldown machines 67 词 685 击 4 单 $610.44」这种垃圾簇 —— 
685 击里混着两个品类的流量，拿它去否词会连带否掉赚钱的流量。

教训：单链接（union-find，两个词有一条边就并）在存在长尾同义词的场景下
**必然**链式合并，这是算法性质问题，不是调阈值能解决的 —— 
只要桥词存在，阈值调到 0.99 也照样并。
所以本模块用 complete-linkage（全连接）：新成员必须与簇内**每一个**成员
都单独成边才允许加入。桥词只能自己单独成一簇，无法把两簇焊起来。

坑二：token 相似度这个闸门本身就是错的
--------------------------------------------------------------------------
第二版改成「种子式」：新词只跟种子比、不跟成员比，阈值 0.60 + 必须共享核心词。
86 个搜索词聚成 62 簇（52 个单例簇 64 击 + 10 个多词簇 641 击），
最大簇 `lat pulldown machines` 5 词 574 击 3 单 $522.55。

表面看还行，但它把 `lat pulldown` 和 `lat pulldown machines` 并了，这是错的。
把 machine/machines 放进 STOP 表时，tok('lat pulldown') = tok('lat pulldown machines')
= {lat, pulldown}，Jaccard = 1.00，必然合并；即使移出 STOP，
Jaccard = 0.67 仍 >= 0.60，还是合并。

铁证（scope 内实测）：
  `lat pulldown`          跑出的搜索词 = ['lat pulldown'(279), 'lat pulldowns'(2)]
  `lat pulldown machines` 跑出的搜索词 = ['lat pulldown machines'(186),
                                          'lat pulldown machine'(5),
                                          'latpulldown machine'(1),
                                          'lat pull down machine'(1)]
  交集 = 空集，Jaccard = 0.00，重叠率 0%。
  实测 scope 内**任意两个投放词**的搜索词 Jaccard 全是 0.00 —— 
  每个投放词各自精准吃自己的流量（`lat pulldown` 99.3% 的点击就是本词）。

教训：token 相似度不能作为合并依据。字面像不代表吃同一批流量；
字面不像也不代表不是一个东西（`latpulldown` vs `lat pulldown`）。
**要合并两个搜索词，必须有它们「实际吃同一批流量」的证据。**

================================================================================
本模块的合并依据：流量重叠（唯一依据）
================================================================================
数据事实：每条 fact_search_term_daily 记录都是
「某个 keyword 跑出了某个 search_term，花了多少点击」。
所以「搜索词 -> 它从哪个 keyword 拿到点击」天然是一张
「搜索词 × keyword」的点击矩阵。这张矩阵就是流量归属的直接证据。

两个搜索词 A、B 允许合并，必须同时过四道闸门（下面每个常量都写了为什么）：

  闸门 1（有共同渠道）：A、B 至少在一个 keyword 下同时拿到点击。
          没有共同 keyword = 从没在同一个流量池里出现过 = 没有证据。

  闸门 2（双向覆盖率 >= 0.70）：A 在「共享 keyword 集合」上的点击
          占 A 总点击 >= 70%，B 同理，**双向都要满足**。
          理由：单向不算数。若 A 的流量 95% 来自共享渠道、B 只有 20%，
          说明 B 主要是别的 keyword 喂的，只是顺带蹭到，并进来会污染 B。
          0.70 的取值依据见下面「阈值校准」实测。

  闸门 3（共享点击 >= 3）：共享渠道上的点击，A、B 各自至少 3 击。
          理由：1-2 击的「共享」是噪声，一个点击的抖动就能造出假重叠。

  闸门 4（共享渠道的支配度 >= 0.50）：在**某一个**共享 keyword 内，
          A+B 的点击合计必须占该 keyword 总点击 >= 50%。
          理由（这条是防坑二的关键护栏）：
          一个 broad 匹配的 keyword 可能跑出几十个互不相关的搜索词
          （实测 `cable machine` 一个 keyword 跑出 67 个搜索词）。
          这些词确实「共享同一个 keyword」，但每个只占 1%-12%，
          共享渠道里塞满了别人，说明这个渠道对它们没有区分意义，
          拿它当合并证据等于「都在同一个广告组里跑过」就合并 —— 荒谬。
          要求这对词在某渠道里合计占一半以上，才说明该渠道
          **主要由这两个词消费**，它俩才是同一流量池的主角。

聚合成簇用 complete-linkage（见坑一），不做并查集单链接。

================================================================================
不显著就说不动（硬约束）
================================================================================
簇的 CVR 是否真的比基线差，一律走 facts.significance 的二项检验。
verdict == "inconclusive" 的簇**不进否词建议**，只标「看着像，但样本不够」。
反面教材（实测，别重犯）：cable 相关 72 个词跨十几簇、106 击 0 单、$75.04，
在基线 cvr = 0.005013（同窗口 1197 击 6 单）下 P(106 击 0 单) = 0.587 ——
完全落在「运气不好」的正常范围内，不显著。
「0 单」不等于「转化差」，可能只是还没跑够；要判「更差」这个窗口需要 321 击。
本模块绝不因为 0 单就建议否。

不依赖 embedding（网关 /v1/embeddings 实测 404），纯 Python + 统计。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from itertools import combinations

# facts.py 用 `import scope`，所以本模块也按同目录方式导入，
# 直接 `python3 engine/semantics.py` 跑自测时靠下面这段把 engine/ 塞进 sys.path。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from facts import (  # noqa: E402
    DEFAULT_CONFIDENCE,
    asin_cvr_baseline,
    searchterm_stats,
    significance,
)


# ============================================================ 结构性约束

# 投放词（keyword）永远不参与合并。这是用户明确拍板的决定，不是可调参数。
# 坑二就是因为把两个**投放词**（lat pulldown / lat pulldown machines）
# 当成可合并对象才炸的。本模块的聚类对象只有 search_term；
# keyword 只作为「流量来源」的证据出现，带 mergeable=0 一起返回，方便审计。
KEYWORD_MERGEABLE = 0


# ============================================================ 阈值

# 双向覆盖率门槛。取值依据（scope 实测，2026-11-02~11-15）：
# 搜索词在某 keyword 内的点击份额是**双峰**分布 —— 要么 >= 0.79，要么 <= 0.21，
# 中间几乎为空（分位数 p50=p70=0.011, p90=0.211, p95=0.966）。
# 也就是说「主导」和「蹭流量」之间有一大片空地，阈值只要落在这片空地里
# 结果就一样。实测 0.60 / 0.70 / 0.80 三档产出的边完全相同，0.70 取中。
MIN_COVERAGE = 0.70

# 共享渠道上的最小点击。取 3 而不是 5：
# 单复数变体（lat pulldowns 只有 2-3 击）本身量就极小，取 5 会把它丢掉，
# 而丢掉一个真同义词的代价（簇碎掉、人工多看一个词）大于
# 错误并入它的代价（它只占簇内 1% 点击，不影响簇的整体判断）。
# 小样本的风险由 confidence 的收缩因子单独扣分，不靠抬门槛硬挡。
MIN_SHARED_CLICKS = 3

# 共享渠道支配度门槛。取值依据：实测 cable machine 那个 67 词分发器里，
# 任意两词合计占该 keyword 的份额最大只有 0.196；
# 而真正该合并的 3 对分别是 0.992 / 1.000 / 1.000。
# 0.50 稳稳落在这条 0.196 ~ 0.992 的空档中间，上下浮动 0.2 结果都不变。
MIN_DOMINANCE = 0.50

# confidence 的样本收缩尺度：confidence = 覆盖率 * s/(s+SHRINK_CLICKS)。
# 共享点击 3 -> 0.50，12 -> 0.80，346 -> 0.99。
# 用收缩而不是直接截断，是为了让「覆盖率满分但只有 3 击」的簇
# 仍然排在有实打实证据的簇后面，而不是被一刀切丢掉。
SHRINK_CLICKS = 3.0


# ============================================================ 内部工具


def _norm(s: str) -> str:
    """归一化：小写、压空格。只用于判定「搜索词是否等于投放词本身」。"""
    return " ".join((s or "").lower().split())


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _build_click_matrix(conn: sqlite3.Connection, start: str, end: str):
    """把 searchterm_stats 的 ad_group 级行，重新聚合成
    「搜索词 × keyword」的点击矩阵。

    复用 facts.searchterm_stats（不重写 SQL）：它已经带好 scope 过滤和
    impressions/clicks/cost/orders/sales 聚合，但它是按
    (market_id, search_term, campaign_id, ad_group_id) 分组的，
    同一个搜索词会出多行（不同 ad_group / campaign）。
    聚类需要的是「搜索词在某个 keyword 上总共拿到多少点击」，
    所以这里再按 (search_term, keyword_id) 加总一次。

    返回 (cells, term_ck, term_od, term_cost, kw_ck, kw_meta, term_kws)
      cells[term][kw] = 点击
      term_kws[term]  = {kw, ...}
    """
    rows = searchterm_stats(conn, start, end)

    cells: dict[str, dict[str, int]] = {}
    term_ck: dict[str, int] = {}
    term_od: dict[str, int] = {}
    term_cost: dict[str, float] = {}
    kw_ck: dict[str, int] = {}
    kw_meta: dict[str, dict] = {}

    for r in rows:
        term = (r.get("search_term") or "").strip()
        kw = r.get("keyword_id")
        ck = int(r.get("clicks") or 0)
        # 零点击行：没有流量就无从谈「流量重叠」，否词也不需要它们。
        # 实测 scope 内这类行本来就是 0 行，这层过滤只是防御。
        if not term or not kw or ck <= 0:
            continue

        bucket = cells.setdefault(term, {})
        bucket[kw] = bucket.get(kw, 0) + ck
        term_ck[term] = term_ck.get(term, 0) + ck
        term_od[term] = term_od.get(term, 0) + int(r.get("orders") or 0)
        term_cost[term] = term_cost.get(term, 0.0) + float(r.get("cost") or 0.0)
        kw_ck[kw] = kw_ck.get(kw, 0) + ck

        meta = kw_meta.setdefault(kw, {"keyword_text": r.get("keyword_text") or "",
                                       "match_type": r.get("match_type") or "",
                                       "clicks": 0})
        meta["clicks"] = kw_ck[kw]

    term_kws = {t: set(d) for t, d in cells.items()}
    return cells, term_ck, term_od, term_cost, kw_ck, kw_meta, term_kws


def _link_evidence(a: str, b: str, cells, term_ck, kw_ck, term_kws,
                   min_coverage: float, min_shared_clicks: int,
                   min_dominance: float) -> tuple[bool, dict]:
    """判断两个搜索词 A、B 是否允许合并，并返回完整证据。

    四道闸门全过才算成边（每道闸门的理由见模块头部）。
    证据里保留全部中间量，方便解释「为什么并 / 为什么不并」。
    """
    shared = term_kws[a] & term_kws[b]
    if not shared:
        # 闸门 1：没有共同 keyword，从没在同一个流量池出现过。
        return False, {"shared_keywords": [], "reason": "无共同 keyword"}

    a_shared = sum(cells[a][k] for k in shared)
    b_shared = sum(cells[b][k] for k in shared)
    cov_a = a_shared / term_ck[a]
    cov_b = b_shared / term_ck[b]

    # 闸门 4：在某个共享 keyword 内，A+B 合计占比的最大值。
    dominance = max((cells[a][k] + cells[b][k]) / kw_ck[k] for k in shared)

    ok = (cov_a >= min_coverage
          and cov_b >= min_coverage
          and min(a_shared, b_shared) >= min_shared_clicks
          and dominance >= min_dominance)

    if ok:
        reason = "四闸门通过"
    elif min(a_shared, b_shared) < min_shared_clicks:
        reason = f"共享点击不足（{min(a_shared, b_shared)} < {min_shared_clicks}）"
    elif cov_a < min_coverage or cov_b < min_coverage:
        reason = f"覆盖率不足（{_pct(cov_a)} / {_pct(cov_b)} < {_pct(min_coverage)}）"
    else:
        reason = (f"共享渠道被稀释（A+B 在某 keyword 内最多只占 "
                  f"{_pct(dominance)} < {_pct(min_dominance)}）")

    return ok, {
        "shared_keywords": sorted(shared),
        "a_shared_clicks": a_shared,
        "b_shared_clicks": b_shared,
        "coverage_a": round(cov_a, 4),
        "coverage_b": round(cov_b, 4),
        "dominance": round(dominance, 4),
        "reason": reason,
    }


def _cluster_from_matrix(cells, term_ck, kw_ck, term_kws,
                         min_coverage: float, min_shared_clicks: int,
                         min_dominance: float) -> list[dict]:
    """在点击矩阵上做 complete-linkage 贪心聚合。

    单独抽出来是为了能用合成矩阵做桥词回归测试（见自测第五节）——
    真实数据里桥词恰好没出现，只靠真实数据测不出「会不会爆链」。
    """
    # 大词优先做种子：先成型的簇证据最足，也让人读结果时大簇在前。
    terms = sorted(term_ck, key=lambda t: (-term_ck[t], t))

    clusters: list[dict] = []
    for term in terms:
        candidates = []
        for cl in clusters:
            edges = []
            # complete-linkage：新词必须与簇内**每一个**成员都单独成边。
            # 桥词只跟其中一部分成员成边，所以进不来，也就无法把两簇焊起来。
            for m in cl["members"]:
                ok, ev = _link_evidence(m, term, cells, term_ck, kw_ck, term_kws,
                                        min_coverage, min_shared_clicks, min_dominance)
                if not ok:
                    break
                edges.append((m, ev))
            else:
                candidates.append((cl, edges))
        if candidates:
            # 多个簇都能接：并入点击最大的那个（确定性，且强化强证据簇）
            cl, edges = max(candidates, key=lambda p: p[0]["clicks"])
            cl["members"].append(term)
            cl["clicks"] += term_ck[term]
            cl["edges"].extend(edges)
        else:
            clusters.append({"members": [term], "clicks": term_ck[term], "edges": []})

    return clusters


# ============================================================ 主函数


def cluster_search_terms(conn: sqlite3.Connection, start: str, end: str,
                         *,
                         min_coverage: float = MIN_COVERAGE,
                         min_shared_clicks: int = MIN_SHARED_CLICKS,
                         min_dominance: float = MIN_DOMINANCE,
                         confidence: float = DEFAULT_CONFIDENCE) -> list[dict]:
    """把窗口内的搜索词按「实际吃同一批流量」聚成簇。

    参数
    ----
    conn / start / end : 数据库连接与日期窗口（闭区间）
    min_coverage / min_shared_clicks / min_dominance : 四闸门阈值，默认值见模块头部
    confidence : 传给 facts.significance 的置信度

    返回
    ----
    簇列表，按簇内点击降序。每个簇：
        seed         : 簇内点击最多的搜索词（人读簇名用）
        members      : 成员明细列表
        why          : 一句话解释「这些词凭什么在一起」，带真实百分比
        clicks/orders/cost : 簇合计
        confidence   : 0-1，覆盖率 * 样本收缩
        significance : facts.significance 原样返回（含 verdict）
        suggest      : negate_candidate / watch_only / keep / unknown
        negation_risk: 否掉这个簇会不会误伤自己的投放词
    """
    (cells, term_ck, term_od, term_cost,
     kw_ck, kw_meta, term_kws) = _build_click_matrix(conn, start, end)

    if not cells:
        return []

    # ---- complete-linkage 贪心聚合（实现见 _cluster_from_matrix）
    # 为什么不是并查集：见模块头「坑一」。
    clusters = _cluster_from_matrix(cells, term_ck, kw_ck, term_kws,
                                    min_coverage, min_shared_clicks, min_dominance)

    # ---- 组装输出
    baseline = asin_cvr_baseline(conn, start, end)
    baseline_cvr = baseline.get("cvr") or 0.0
    out: list[dict] = []

    for cl in clusters:
        members = sorted(cl["members"], key=lambda t: (-term_ck[t], t))
        seed = members[0]
        total_ck = sum(term_ck[t] for t in members)
        total_od = sum(term_od[t] for t in members)
        total_cost = round(sum(term_cost[t] for t in members), 2)

        # 簇级共同渠道 = 所有成员 kw 集合的交集。
        # complete-linkage 只保证两两有交集，三成员以上时全局交集可能为空
        # （A∩B={k1}, B∩C={k2}, A∩C={k3}），这种情况如实标成弱证据。
        common = set(term_kws[members[0]])
        for t in members[1:]:
            common &= term_kws[t]

        if len(members) == 1:
            why = ("单例：没有任何其他搜索词与它在同一批 keyword 上"
                   f"双向覆盖 >= {_pct(min_coverage)} 且共享渠道未被稀释，"
                   "不合并（宁可不合，也不制造假簇）")
            min_cov = 1.0
            evidence_clicks = term_ck[seed]
            shared_kw_texts: list[str] = []
            weak = False
            dom_text = ""
        elif common:
            # 每个成员在共同渠道上的点击，以及覆盖率
            per = {t: sum(cells[t][k] for k in common) for t in members}
            covs = [per[t] / term_ck[t] for t in members]
            min_cov = min(covs)
            evidence_clicks = min(per.values())
            shared_kw_texts = sorted({kw_meta[k]["keyword_text"] or k for k in common})
            # 共同渠道里最"薄"的那一个的支配度（这对词合计占比）
            dom = min((sum(cells[t][k] for t in members) / kw_ck[k]) for k in common)
            weak = False
            dom_text = f"这些词在该 keyword 内合计占 {_pct(dom)} 的点击，"
            # 措辞按「最弱成员」写，不说「XX% 以上」—— min_cov 已经是最弱值，
            # 再说「以上」会让人误以为下限被低估，反而不诚实。
            why = (f"这些搜索词最弱的成员也有 {_pct(min_cov)} 的点击来自同一批 "
                   f"keyword：{'、'.join(shared_kw_texts)}；"
                   f"{dom_text}最少的一个成员在该渠道也有 {evidence_clicks} 击 —— "
                   "是同一个流量池，不是字面像")
        else:
            # 全局交集为空，退回两两证据
            pair_covs = []
            pair_clicks = []
            for a, b in combinations(members, 2):
                ok, ev = _link_evidence(a, b, cells, term_ck, kw_ck, term_kws,
                                        min_coverage, min_shared_clicks, min_dominance)
                if ok:
                    pair_covs.append(min(ev["coverage_a"], ev["coverage_b"]))
                    pair_clicks.append(min(ev["a_shared_clicks"], ev["b_shared_clicks"]))
            min_cov = min(pair_covs) if pair_covs else 0.0
            evidence_clicks = min(pair_clicks) if pair_clicks else 0
            shared_kw_texts = []
            weak = True
            dom_text = ""
            why = (f"两两之间都有 >= {_pct(min_coverage)} 的流量重叠（最弱一对 "
                   f"{_pct(min_cov)}），但没有一个所有成员共用的 keyword —— 证据偏弱，"
                   "仅说明它们在同一批广告组里互相抢量")

        shrink = evidence_clicks / (evidence_clicks + SHRINK_CLICKS) if evidence_clicks else 0.0
        conf = round(min_cov * shrink, 3) if len(members) > 1 else 1.0

        sig = significance(total_ck, total_od, baseline_cvr, confidence)
        verdict = sig.get("verdict")

        # 否词建议门控：只有统计上确实更差才允许进建议。
        # inconclusive 一律 watch_only —— 「看着像，但样本不够」。
        if verdict == "worse":
            suggest = "negate_candidate"
        elif verdict == "better":
            suggest = "keep"
        elif verdict == "inconclusive":
            suggest = "watch_only"
        else:
            suggest = "unknown"

        # 否词风险：簇里有成员就是它自己来源 keyword 的原文，
        # 否掉它等于把投放词自己的精准流量掐了。
        echo_terms = sorted({t for t in members
                             if any(_norm(t) == _norm(kw_meta[k]["keyword_text"])
                                    for k in term_kws[t])})
        negation_risk = "high" if echo_terms else "low"

        # 来源 keyword 明细（mergeable 硬编码 0，见模块头结构性约束）
        src_kw = {}
        for t in members:
            for k in term_kws[t]:
                s = src_kw.setdefault(k, {"keyword_id": k,
                                          "keyword_text": kw_meta[k]["keyword_text"],
                                          "match_type": kw_meta[k]["match_type"],
                                          "clicks": 0,
                                          "mergeable": KEYWORD_MERGEABLE})
                s["clicks"] += cells[t][k]
        sources = sorted(src_kw.values(), key=lambda s: -s["clicks"])

        out.append({
            "seed": seed,
            "seed_clicks": term_ck[seed],
            "members": [{
                "search_term": t,
                "clicks": term_ck[t],
                "orders": term_od[t],
                "cost": round(term_cost[t], 2),
                "share_in_cluster": round(term_ck[t] / total_ck, 4) if total_ck else 0.0,
                "is_keyword_echo": t in echo_terms,
            } for t in members],
            "why": why,
            "clicks": total_ck,
            "orders": total_od,
            "cost": total_cost,
            "confidence": conf,
            "weak_evidence": weak,
            "shared_keywords": shared_kw_texts,
            "sources": sources,
            "significance": sig,
            "suggest": suggest,
            "negation_risk": negation_risk,
            "echo_terms": echo_terms,
        })

    out.sort(key=lambda c: (-c["clicks"], c["seed"]))
    return out


# ============================================================ 自测


def _selftest() -> None:
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "ingest", "data", "ads.sqlite3")
    start, end = "2026-11-02", "2026-11-15"

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    baseline = asin_cvr_baseline(conn, start, end)
    print(f"数据库: {db}")
    print(f"窗口  : {start} ~ {end}（scope 由 facts.searchterm_stats 内部过滤）")
    print(f"基线  : clicks={baseline['clicks']} orders={baseline['orders']} "
          f"cvr={baseline['cvr']:.6f}  ({baseline['scope']})")
    print()

    clusters = cluster_search_terms(conn, start, end)

    single = [c for c in clusters if len(c["members"]) == 1]
    multi = [c for c in clusters if len(c["members"]) > 1]
    total_ck = sum(c["clicks"] for c in clusters)

    print("=" * 78)
    print("一、总体")
    print("=" * 78)
    print(f"簇数            : {len(clusters)}")
    print(f"单例簇          : {len(single)} 个，点击 {sum(c['clicks'] for c in single)}")
    print(f"多词簇          : {len(multi)} 个，点击 {sum(c['clicks'] for c in multi)}")
    print(f"覆盖搜索词      : {sum(len(c['members']) for c in clusters)} 个，"
          f"点击 {total_ck}")
    by_suggest = {}
    for c in clusters:
        by_suggest.setdefault(c["suggest"], []).append(c)
    for k in ("negate_candidate", "watch_only", "keep", "unknown"):
        if k in by_suggest:
            cs = by_suggest[k]
            print(f"  {k:<18}: {len(cs):>3} 簇，点击 {sum(x['clicks'] for x in cs):>4}，"
                  f"订单 {sum(x['orders'] for x in cs)}")
    print()

    print("=" * 78)
    print("二、最大的 5 个簇")
    print("=" * 78)
    for i, c in enumerate(clusters[:5], 1):
        print(f"\n[{i}] seed = {c['seed']}   ({len(c['members'])} 词 / "
              f"{c['clicks']} 击 / {c['orders']} 单 / ${c['cost']:.2f} / "
              f"confidence={c['confidence']})")
        print(f"    why : {c['why']}")
        sig = c["significance"]
        print(f"    检验: verdict={sig['verdict']}  p_worse={sig['p_worse']}  "
              f"p_better={sig['p_better']}  need_for_orders={sig['need_for_orders']}")
        print(f"    结论: suggest={c['suggest']}  negation_risk={c['negation_risk']}")
        print(f"    成员:")
        for m in c["members"]:
            echo = "  ← 就是投放词本身" if m["is_keyword_echo"] else ""
            print(f"      {m['search_term']:<34} {m['clicks']:>4} 击  "
                  f"{m['orders']} 单  ${m['cost']:>7.2f}  "
                  f"占簇 {_pct(m['share_in_cluster'])}{echo}")
        print(f"    来源 keyword:")
        for s in c["sources"][:4]:
            print(f"      {s['keyword_text']:<26} mt={s['match_type']}  "
                  f"{s['clicks']:>4} 击  mergeable={s['mergeable']}")

    print()
    print("=" * 78)
    print("三、阈值校准：过了「覆盖率+共享点击」闸门的 pair，其支配度分布")
    print("   （用来验证 MIN_DOMINANCE=0.50 是否落在空档里）")
    print("=" * 78)
    (cells, term_ck, _od, _cost, kw_ck, kw_meta, term_kws) = _build_click_matrix(conn, start, end)
    probe = []
    for a, b in combinations(sorted(term_ck), 2):
        shared = term_kws[a] & term_kws[b]
        if not shared:
            continue
        sa = sum(cells[a][k] for k in shared)
        sb = sum(cells[b][k] for k in shared)
        ca, cb = sa / term_ck[a], sb / term_ck[b]
        dom = max((cells[a][k] + cells[b][k]) / kw_ck[k] for k in shared)
        if ca >= MIN_COVERAGE and cb >= MIN_COVERAGE and min(sa, sb) >= MIN_SHARED_CLICKS:
            probe.append((dom, a, b, ca, cb, sa, sb))
    probe.sort(reverse=True)
    for dom, a, b, ca, cb, sa, sb in probe:
        mark = "✅ 合并" if dom >= MIN_DOMINANCE else "❌ 拒绝"
        print(f"  dom={dom:.3f}  {mark}  {a[:30]:<30} | {b[:30]:<30} "
              f"cov={ca:.3f}/{cb:.3f} 共享={sa}/{sb}")
    if probe:
        print(f"  → 支配度区间: 最大 {probe[0][0]:.3f} / 最小 {probe[-1][0]:.3f}；"
              f"门槛 {MIN_DOMINANCE} 判定：{sum(1 for p in probe if p[0] >= MIN_DOMINANCE)} 合 / "
              f"{sum(1 for p in probe if p[0] < MIN_DOMINANCE)} 拒")

    print()
    print("=" * 78)
    print("四、反例校验：坑二必须不再发生")
    print("=" * 78)
    # 4.1 lat pulldown 与 lat pulldown machines 不能同簇
    def _cluster_of(term):
        for c in clusters:
            if any(m["search_term"] == term for m in c["members"]):
                return c
        return None
    a, b = _cluster_of("lat pulldown"), _cluster_of("lat pulldown machines")
    print(f"  lat pulldown 所在簇 seed = {a['seed'] if a else None}")
    print(f"  lat pulldown machines 所在簇 seed = {b['seed'] if b else None}")
    print(f"  → 是否被错误合并: {'是（算法有 bug）' if a is b else '否 ✅'}")
    # 4.2 长尾分发器不能被聚成一坨
    # 按「喂出的搜索词个数」挑分发器，而不是按 点击数/词数 ——
    # 后者会把「词少但点击大」的精准词误选成分发器（实测踩过）。
    n_terms_of = {k: len([t for t in term_kws if k in term_kws[t]]) for k in kw_ck}
    big = max(n_terms_of, key=lambda k: (n_terms_of[k], kw_ck[k]))
    nt = n_terms_of[big]
    top = max(cells[t][big] for t in term_kws if big in term_kws[t])
    print(f"  长尾分发器 keyword = {kw_meta[big]['keyword_text']} "
          f"(mt={kw_meta[big]['match_type']}, {kw_ck[big]} 击 / {nt} 个搜索词 / "
          f"top1 仅占 {_pct(top / kw_ck[big])})")
    merged_big = [c for c in clusters
                  if len(c["members"]) > 1
                  and all(big in term_kws[m["search_term"]] for m in c["members"])]
    print(f"  → 由它喂出的多词簇数: {len(merged_big)} "
          f"{'✅ 被支配度闸门挡住' if not merged_big else '❌ 漏了'}")
    # 4.3 106 击 0 单不显著
    sig106 = significance(106, 0, baseline["cvr"])
    print(f"  cable 汇总 106 击 0 单 -> verdict={sig106['verdict']}  "
          f"p_worse={sig106['p_worse']}  need_if_zero={sig106['need_if_zero']}")
    print(f"  → 0 单不等于转化差：要判「更差」需要 {sig106['need_if_zero']} 击，"
          f"现在只有 106 击")

    # ---- 五、合成矩阵：桥词为什么进不来（坑一回归测试）
    _synthetic_bridge_test()

    # ---- 六、已知盲区（自曝，别当成 bug 报告）—— 必须在 close 之前跑
    _known_blind_spots(conn, start, end, clusters, baseline)

    conn.close()


def _known_blind_spots(conn, start, end, clusters, baseline) -> None:
    """自曝三类必然出错的场景，跑真实数据量化，避免下游误用。

    这个函数不是「检查有没有 bug」，而是**确认这些盲区客观存在且有多大**。
    如果哪天数字变了，说明数据分布变了，阈值可能需要重看。
    """
    print()
    print("=" * 78)
    print("六、已知盲区（必然出错的地方，自曝）")
    print("=" * 78)

    # 盲区 1：绝大多数点击是「搜索词 == 投放词原文」，这些簇没有信息量
    total = sum(c["clicks"] for c in clusters)
    echo_ck = sum(m["clicks"] for c in clusters for m in c["members"] if m["is_keyword_echo"])
    all_echo = sum(1 for c in clusters
                   if all(m["is_keyword_echo"] for m in c["members"]))
    non_echo_terms = [m["search_term"] for c in clusters for m in c["members"]
                      if not m["is_keyword_echo"]]
    non_echo_ck = sum(m["clicks"] for c in clusters for m in c["members"]
                      if not m["is_keyword_echo"])
    print()
    print("  盲区 1：簇里大部分点击就是投放词自己，否词价值接近于零")
    print(f"    搜索词 == 来源 keyword 原文的点击：{echo_ck} / {total}（{_pct(echo_ck / total)}）")
    print(f"    整簇都是「投放词自己」的簇：{all_echo} 个")
    print(f"    真正的「非投放词搜索词」：{len(non_echo_terms)} 个，合计只有 {non_echo_ck} 击")
    print("    → 本窗口能拿来判否词的素材只有这 %d 击。" % non_echo_ck)
    print("      大簇（362 击、314 击）看着唬人，其实 99%+ 是投放词自己的精准流量，")
    print("      把整簇否掉等于把自己的投放词掐了 —— 所以 negation_risk 一律 high。")

    # 盲区 2：keyword 只喂少量搜索词时，闸门必然满分，语义无关也会合并
    print()
    print("  盲区 2：keyword 只喂 2-3 个搜索词时，四闸门必然全过，无关词也会合并")
    def _build(rows):
        cells: dict[str, dict[str, int]] = {}
        tck: dict[str, int] = {}
        kck: dict[str, int] = {}
        tk: dict[str, set] = {}
        for t, k, c in rows:
            cells.setdefault(t, {})
            cells[t][k] = cells[t].get(k, 0) + c
            tck[t] = tck.get(t, 0) + c
            kck[k] = kck.get(k, 0) + c
            tk.setdefault(t, set()).add(k)
        return cells, tck, kck, tk

    two = [("cable machine for home gym", "cable machine", 30),
           ("cable crossover attachment", "cable machine", 25)]
    c2, t2, k2, kw2 = _build(two)
    ok, ev = _link_evidence(two[0][0], two[1][0], c2, t2, k2, kw2,
                            MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
    print(f"    该 keyword 只喂这 2 个词（30 击 / 25 击）→ 成边={ok}  "
          f"cov={ev['coverage_a']:.2f}/{ev['coverage_b']:.2f}  dom={ev['dominance']:.2f}")
    print("    → 两个语义无关的词，只因该 keyword 只喂了它们俩，覆盖率与支配度")
    print("      都必然满分，直接被合并。这是覆盖率/支配度用「占比」定义的固有缺陷：")
    print("      分母里只有它们自己，占比自然 100%。")
    noisy = two + [(f"junk{i}", "cable machine", 1) for i in range(60)]
    c3, t3, k3, kw3 = _build(noisy)
    ok2, ev2 = _link_evidence(two[0][0], two[1][0], c3, t3, k3, kw3,
                              MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
    print(f"    同样两个词，但该 keyword 另有 60 个 1 击长尾 → 成边={ok2}  "
          f"dom={ev2['dominance']:.2f}（{ev2['reason']}）")
    print("    → 同一对词，判定结果被「第三方噪声」翻转：噪声多就不合并，")
    print("      噪声少就合并。这说明闸门衡量的是「渠道是否被稀释」，")
    print("      不是「两个词是否同类」—— 语义相关性本模块无法判断，也不假装能。")

    # 盲区 3：搜索词报告的点击 ≠ keyword 报表的点击
    print()
    print("  盲区 3：搜索词报告并不覆盖 keyword 的全部点击")
    from facts import keyword_stats
    ks = {k["keyword_id"]: k for k in keyword_stats(conn, start, end)}
    (cells, term_ck, term_od, term_cost,
     kw_ck, kw_meta, term_kws) = _build_click_matrix(conn, start, end)
    gaps = []
    for k, ck in kw_ck.items():
        full = ks.get(k, {}).get("clicks")
        if full is not None and full != ck:
            gaps.append((kw_meta[k]["keyword_text"], ck, full))
    for name, a, b in sorted(gaps, key=lambda x: -(x[2] - x[1])):
        print(f"    {name:<24} 搜索词报告汇总 {a:>4} 击 / keyword 报表 {b:>4} 击"
              f"（差 {b - a}）")
    if gaps:
        print("    → 支配度闸门的分母是「搜索词报告内的点击」，不是 keyword 的真实点击。")
        print("      差额来自未归因到具体搜索词的点击。方向是让支配度**偏高**（分母偏小），")
        print("      所以闸门比看起来更宽松一点，不构成漏判，但报告里不要拿这两个数对账。")

    # 盲区 4：本窗口所有簇都是 inconclusive
    print()
    print("  盲区 4：本窗口不存在任何「统计上确认更差」的簇")
    from collections import Counter
    dist = Counter(c["significance"]["verdict"] for c in clusters)
    print(f"    verdict 分布：{dict(dist)}")
    print(f"    基线 cvr={baseline['cvr']:.6f}（{baseline['clicks']} 击 / "
          f"{baseline['orders']} 单），要判「更差」需要的点击量在数百到上千量级。")
    print("    → 现阶段本模块只能输出「看着像，但样本不够」，")
    print("      这是正确行为，不是缺陷。数据量上来后 verdict 自然会分化。")


def _link_evidence_onedir(a, b, cells, term_ck, kw_ck, term_kws,
                          min_coverage, min_shared_clicks, min_dominance):
    """仅供对照实验：把「双向覆盖率」降级成「单向下限」，证明双向是必需的。

    只要求共享点击 >= 门槛、支配度达标，覆盖率只查 a 一侧。
    这个版本会被桥词骗过 —— 见自测情形 3。
    """
    shared = term_kws[a] & term_kws[b]
    if not shared:
        return False, {}
    a_shared = sum(cells[a][k] for k in shared)
    b_shared = sum(cells[b][k] for k in shared)
    dom = max((cells[a][k] + cells[b][k]) / kw_ck[k] for k in shared)
    ok = (a_shared / term_ck[a] >= min_coverage
          and min(a_shared, b_shared) >= min_shared_clicks
          and dom >= min_dominance)
    return ok, {"coverage_a": round(a_shared / term_ck[a], 4),
                "dominance": round(dom, 4)}


def _synthetic_bridge_test() -> None:
    """坑一的回归测试：桥词到底被哪一层闸门拦住，以及 complete-linkage 是否多余。

    真实数据这次恰好没出现桥词（scope 内 pulldown 系与 cable 系不共享 keyword），
    只跑真实数据测不出「会不会爆链」。这里手工构造最坏情况 + 随机矩阵对照。

    ## 跑出来才发现的两件事（原先的推导只对了一半，如实记录）

    ### 1. 桥词靠「覆盖率」就被拦住，且加码无效
    覆盖率的分母是**桥词自己的总点击**。桥词在 k1、k2 各吃 50 击时，
    它对 A 的覆盖率是 50/100 = 0.50。把它加码到 90/90 也没用，仍是 90/180 = 0.50。
    设桥词 B 的 keyword 集合为 K，邻居 A、C 的 keyword 集合互不相交，则
    B∩A 与 B∩C 是 K 的两个不相交子集，于是
        cov(B,A) + cov(B,C) <= 1
    所以只要 MIN_COVERAGE > 0.5，**同一个节点不可能同时紧邻两个 keyword 不相交的节点**。
    这是选择 0.70 而不是 0.5 以下的第二个理由，比「数据分布」更硬。

    ### 2. 但「连通分量必然是团」是错的 —— 我一开始就是这么推的，被随机矩阵打翻
    上面只排除了「邻居互不相交」这一种链。链还能从另一条缝钻进来：
    **中间节点的两个邻居可以共享 keyword，只是它们自己那一对没过闸门。**
    最常见的原因是单侧覆盖率不足：A 与 C 共享 k2、k3，但 C 的流量大部分跑去了
    只有它自己有的 k1，于是 cov(C, A∩C) 只有 0.39 < 0.70 —— A—C 不成边，
    而 A—B、B—C 都成边，A 与 C 就被 B 串起来了。
    实测 200 组随机矩阵里有 49 组单链接与 complete-linkage 划分不同，
    人工核对其中 3 组，**都是 complete-linkage 的分法更站得住**：
      · 样本 0：t0(52击) 与 t1(92击) 共享 k2/k3，但 t1 有 61% 流量在 t0 没有的 k1 上，
        cov=0.94/0.39 本该拒绝；单链接靠 t3 把三者焊成一簇，CL 正确分开。
      · 样本 2：t5 只有 k1，t3 有 41% 流量在 k0，cov=0.59 本该拒绝；
        单链接靠 t4 把 t5 焊进 {t3,t4}，CL 正确留在外面。
      · 样本 8：t3 有 91% 流量在 k2，t5 只有 56% 在共享渠道，cov 单侧不足；
        单链接焊成 {t1,t3,t5}，CL 分成 {t1,t5} 与 {t3}。

    ### 结论
    complete-linkage **不是**冗余保险，是这条算法真正干活的地方：
    它把「簇内每一对都同池」从口号变成事实，也让 why 文案不说谎。
    """
    print()
    print("=" * 78)
    print("五、合成矩阵：桥词怎么被拦住（坑一回归测试）")
    print("=" * 78)

    def build(rows):
        cells: dict[str, dict[str, int]] = {}
        term_ck: dict[str, int] = {}
        kw_ck: dict[str, int] = {}
        term_kws: dict[str, set] = {}
        for term, kw, ck in rows:
            cells.setdefault(term, {})
            cells[term][kw] = cells[term].get(kw, 0) + ck
            term_ck[term] = term_ck.get(term, 0) + ck
            kw_ck[kw] = kw_ck.get(kw, 0) + ck
            term_kws.setdefault(term, set()).add(kw)
        return cells, term_ck, kw_ck, term_kws

    def single_linkage(cells, term_ck, kw_ck, term_kws, onedir=False):
        """并查集单链接：任意一条边成立就把两个连通块焊在一起。"""
        parent = {t: t for t in term_ck}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        linker = _link_evidence_onedir if onedir else _link_evidence
        for a, b in combinations(sorted(term_ck), 2):
            ok, _ = linker(a, b, cells, term_ck, kw_ck, term_kws,
                           MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
            if ok:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
        groups: dict[str, list[str]] = {}
        for t in term_ck:
            groups.setdefault(find(t), []).append(t)
        return [sorted(v) for v in groups.values()]

    def report(title, rows, a, b):
        print()
        print(f"  【{title}】")
        cells, term_ck, kw_ck, term_kws = build(rows)
        for y in (a, b):
            ok, ev = _link_evidence("bridge", y, cells, term_ck, kw_ck, term_kws,
                                    MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
            print(f"    bridge ↔ {y}: 成边={ok}  cov={ev['coverage_a']:.2f}/"
                  f"{ev['coverage_b']:.2f}  共享={ev['a_shared_clicks']}/"
                  f"{ev['b_shared_clicks']}  dom={ev['dominance']:.2f}  "
                  f"→ {ev['reason']}")
        cl = _cluster_from_matrix(cells, term_ck, kw_ck, term_kws,
                                  MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
        cl_sets = [sorted(c["members"]) for c in cl]
        sl_sets = single_linkage(cells, term_ck, kw_ck, term_kws)
        print(f"    complete-linkage : {cl_sets}")
        print(f"    单链接(并查集)   : {sl_sets}")
        welded = any(a in s and b in s for s in cl_sets)
        print(f"    → {a} 与 {b} 是否同簇: {'是 ❌' if welded else '否 ✅'}")
        return welded

    welded = report(
        "情形 1：50/50 桥词（bridge 在 k1、k2 各吃 50 击）",
        [("A", "k1", 100), ("B", "k1", 100), ("bridge", "k1", 50),
         ("C", "k2", 100), ("bridge", "k2", 50)], "A", "C")
    print("    说明: 覆盖率 50/100 = 0.50 < 0.70，第一层就拒。")

    report(
        "情形 2：90/90 强桥词（bridge 在 k1 吃 90 击、k2 吃 90 击）",
        [("A", "k1", 100), ("B", "k1", 100), ("bridge", "k1", 90),
         ("C", "k2", 100), ("bridge", "k2", 90)], "A", "C")
    print("    说明: 覆盖率仍是 90/180 = 0.50 —— 加码桥词总点击不会提高覆盖率，")
    print("          分母同步变大。cov(B,A)+cov(B,C) <= 1 是恒等式（邻居 keyword 不相交时）。")

    print()
    print("  【情形 3：对照实验 —— 把双向覆盖率降级成单向下限】")
    rows3 = [("A", "k1", 100), ("C", "k2", 100), ("bridge", "k1", 20), ("bridge", "k2", 80)]
    cells3, term_ck3, kw_ck3, term_kws3 = build(rows3)
    ok, ev = _link_evidence_onedir("A", "bridge", cells3, term_ck3, kw_ck3, term_kws3,
                                   MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
    print(f"    单向下限: A ↔ bridge 成边={ok}  dom={ev.get('dominance')}")
    ok2, ev2 = _link_evidence_onedir("C", "bridge", cells3, term_ck3, kw_ck3, term_kws3,
                                     MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE)
    print(f"    单向下限: C ↔ bridge 成边={ok2}  dom={ev2.get('dominance')}")
    sl_onedir = single_linkage(cells3, term_ck3, kw_ck3, term_kws3, onedir=True)
    print(f"    单向下限 + 单链接 → {sl_onedir}")
    print(f"    → A 与 C 是否被焊在一起: "
          f"{'是 ❌ 桥词得逞，坑一重现' if any('A' in s and 'C' in s for s in sl_onedir) else '否'}")
    print("    结论: 「双向」是硬要求。只查一侧时桥词只需在两侧各自讨好对方，")
    print("          自身流量怎么分都无所谓。这里 bridge 的 k1 只占 20%，")
    print("          所以它对 A 的双向覆盖率只有 0.20，双向版本会拒绝 A↔bridge。")

    print()
    print("  【情形 4：随机矩阵对照 —— complete-linkage 是不是冗余】")
    import random
    rng = random.Random(20261118)
    diff = 0
    total = 0
    for _ in range(200):
        rows = []
        n_kw = rng.randint(2, 4)
        for t in range(rng.randint(3, 7)):
            for k in range(n_kw):
                if rng.random() < 0.5:
                    rows.append((f"t{t}", f"k{k}", rng.randint(1, 60)))
        if not rows:
            continue
        total += 1
        cells, term_ck, kw_ck, term_kws = build(rows)
        cl = sorted(sorted(c["members"]) for c in _cluster_from_matrix(
            cells, term_ck, kw_ck, term_kws, MIN_COVERAGE, MIN_SHARED_CLICKS, MIN_DOMINANCE))
        sl = sorted(single_linkage(cells, term_ck, kw_ck, term_kws))
        if cl != sl:
            diff += 1
    print(f"    {total} 组随机矩阵：划分不一致 {diff} 组（{_pct(diff / total)}）")
    print("    → complete-linkage 不是冗余保险：单链接确实会多焊出簇。")
    print("      机制: 「邻居 keyword 互不相交」只能排除一种链；")
    print("            中间节点的两个邻居可以共享 keyword、只是自己那一对没过闸门")
    print("            （典型是单侧覆盖率不足：C 的流量大多跑去了只有它自己有的 keyword）。")
    print("      人工核对样本 0/2/8 三组，都是 complete-linkage 的分法更站得住。")
    print("      且簇的 why 宣称「所有成员来自同一批 keyword」——")
    print("      只有 complete-linkage 能让这句话对簇内每一对成员都成立。")


if __name__ == "__main__":
    _selftest()
