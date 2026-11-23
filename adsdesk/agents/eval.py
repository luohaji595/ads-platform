#!/usr/bin/env python3
"""断言兑现回填 + 兑现率报表 —— agent_eval 的唯一入口。

为什么这个文件要单独存在（2026-11-23）：

    在这之前，「回填」这件事只藏在 `converge.py` 的 `run_once()` 里，而那一轮
    除了回填断言还要判质疑、还要往 `converge_run` 写一行账。于是「我就想看
    一下今天该判的判了没有」也必须跑完整个收敛轮，包括判质疑那半边 ——
    想单独给 eval 排一条定时任务都排不出来，因为一跑就会连质疑一起判掉。

    更实际的痛点是第二个：库里已经有 142 条 pending 断言、其中 39 条早就到期，
    但从来没有人回答过「diagnose 写下的这些断言，最后到底兑现了几成」。
    `objection_report.py` 只回答质疑那半边，断言这半边是空白 —— 而断言才是
    agent 的主输出（奏折里那些「预计 X 会降到 Y」）。没有兑现率，
    「这个 agent 靠不靠谱」就只能靠感觉。

    所以这里做两件事，而且只做这两件：
      (a) 把到期未判的 agent_eval 判掉（判定逻辑一律 import converge，不重写）；
      (b) 出按 agent / method 分组的兑现率。

口径（改之前先想清楚为什么原来这么定）：

  1. 命中率 = correct / (correct + wrong)。unverifiable **不进分母** ——
     判不了是判定器和数据的缺陷，不是 agent 说错了；把它算进分母，
     agent 的分数会被「我们自己的工具不行」拉低。
  2. pending 也不进分母，理由同上：还没判的既不是对也不是错。
     到期未判的 pending 单独列一列（积压），那才是运维该盯的数。
  3. 判定结果只写 verdict / actual / checked_at 三列。claim、expected、method
     是 agent 的原话，任何情况下都不改写 —— 改写了就查不出「它当初到底说了什么」，
     而兑现率的意义正是拿原话去对事实。

为什么不自己写一套判定逻辑：

    `converge.deterministic_verdict()` 有九个判定器（next_run / reread / binom /
    placement_share / decision_log / acos / cvr / cpc / numeric），每个都对应
    一类断言的具体查法；`converge.llm_verdict()` 负责确定性判定器读不懂的语义断言。
    这里再抄一遍必然两边漂移 —— 同一个断言在 converge 里判 correct、在 eval 里判
    wrong，那种账没人能对。所以本文件**只复用、不复制**：判定器、窗口解析、
    实体解析、覆盖闸门、revive 规则全部 from converge import。

三道闸门也照抄，一条都不放松（理由见 converge.py 里那段注释）：
    ① verifiable_at 没到 → 根本不进队列（查询条件挡住）
    ② 窗口没走完（_window_state != 'covered'）→ 保持 pending，记 deferred
    ③ 确定性判定器读不懂 → 只有窗口已 covered 才敢判 unverifiable
第③条的方向是关键：把「数据没追平」写成 unverifiable，幂等的
`update ... where verdict='pending'` 会把它永久钉死，临时状态就成了永久结论。

用法：
    python3 adsdesk/agents/eval.py                      # 回填到期 + 出报表
    python3 adsdesk/agents/eval.py --dry-run            # 只判不写，看会填成什么
    python3 adsdesk/agents/eval.py --due-only           # 报表只算已到期的
    python3 adsdesk/agents/eval.py --as-of 2026-12-31   # 把时钟推到那天
    python3 adsdesk/agents/eval.py --report-only        # 不判定，只出报表
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 两个 path 插入，顺序与 converge.py 一致（parents[1] 让 `from agents import ...`
# 能解析，parents[2]/engine 让 converge 内部的 `import facts` 能解析）。
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "engine"))

from agents import context  # noqa: E402
from agents import converge  # noqa: E402
from agents import objection_report as orr  # noqa: E402  # 复用中文宽度表格工具

CST = ZoneInfo("Asia/Shanghai")
ROOT = HERE.parents[2]
DB_PATH = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))

# 本轮最多让模型判几条。与 converge 共用同一个上限：
# 两边上限不一致的话，同一批断言在 eval 里判了 12 条、在 converge 里只判 8 条，
# 剩下的就成了「谁先跑谁判」，账对不上。
LLM_BATCH_LIMIT = converge.LLM_BATCH_LIMIT
LLM_THROTTLE_SECONDS = converge.LLM_THROTTLE_SECONDS

# ------------------------------------------------------------------ 方法归族
#
# 为什么要归族：`agent_eval.method` 是自由文本，实测 145 行里几乎没有两行一样
# （「查 decision_log where rule_id='R30' and day between ...」、「按天去重，数
# decision_log 里 rule_id='R30' 且 status='notified' ... 条数」）。按原文分组
# 等于每一行一个组，报表会变成 145 行、每行 1 条断言 —— 那不是报表，是清单。
#
# 归族规则的关键词顺序**照抄 converge._DET 的判定器顺序**（next_run → reread →
# binom → placement_share → decision_log → acos → cvr → cpc → numeric）：
# 确定性判定器就是按那个顺序抢答的，族序一致，「哪个族判得最多」才能对上
# converge 的实际行为。比如「看下一个结算窗口里 layer='campaign' 的 p_value」
# 同时含「下一个结算窗口」和「p_value」，它实际是被 _ev_next_run 抢走的，
# 所以必须归 next_run 而不是 binom。
#
# 这是关键词近似，不是重跑判定器 —— 报表只需要「大致哪类断言判得准」，
# 精确归属请看 converge 那一轮的 stdout（那里会打印每条命中的判定器名）。
FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("next_run", ("下一个结算窗口", "下一次运行", "下一轮", "下一个窗口", "next run")),
    ("reread", ("重读", "重看", "reread", "回头再看")),
    ("binom", ("二项", "binom", "双侧 p", "p 值", "p_value")),
    ("placement_share", ("位次", "placement", "detail page", "商品页", "位占比")),
    ("decision_log", ("decision_log", "rule_id", "suppressed", "闸门", "拦下")),
    ("acos", ("acos",)),
    ("cvr", ("cvr", "转化率", "转化")),
    ("cpc", ("cpc", "点击单价", "bid")),
    ("numeric", ("clicks", "orders", "cost", "花费", "点击", "订单", "销量", "占比")),
)
FAMILY_ORDER = tuple(name for name, _ in FAMILY_RULES) + ("other",)
FAMILY_LABEL = {
    "next_run": "下一次运行",
    "reread": "重读事实",
    "binom": "二项检验",
    "placement_share": "位次占比",
    "decision_log": "规则日志",
    "acos": "ACoS",
    "cvr": "转化率",
    "cpc": "CPC",
    "numeric": "数值汇总",
    "other": "其它",
}
VERDICTS = ("correct", "wrong", "unverifiable", "pending")
VERDICT_LABEL = {
    "correct": "对",
    "wrong": "错",
    "unverifiable": "判不了",
    "pending": "未判",
}


def method_family(method: str | None) -> str:
    """把自由文本的 method 归到一个族。归不进去就 other，绝不猜。"""
    text = (method or "").lower()
    for name, keys in FAMILY_RULES:
        for k in keys:
            if k.lower() in text:
                return name
    return "other"


# ------------------------------------------------------------------ 查库

def connect(db: str | Path | None = None) -> sqlite3.Connection:
    """开连接。--db > ADS_DB > 默认路径，顺序与 context.connect 一致。"""
    target = db or os.environ.get("ADS_DB") or DB_PATH
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_as_of(as_of: str | None) -> str:
    return as_of or datetime.now(CST).date().isoformat()


def due_rows(conn: sqlite3.Connection, as_of: str, limit: int | None = None) -> list[sqlite3.Row]:
    """到期未判的断言。查询条件与 converge.run_once 里那条一模一样。

    这里不写 `verdict='pending' or ...`：只有 pending 需要判，
    已经判过的行哪怕 verifiable_at 早于今天也不该重判（重判会覆盖历史结论）。
    """
    sql = (
        "select eval_id, agent_run_id, agent, claim, verifiable_at, method, expected, verdict "
        "from agent_eval where verdict='pending' and verifiable_at <= ? "
        "order by verifiable_at, eval_id"
    )
    params: list = [as_of]
    if limit:
        sql += " limit ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


# ------------------------------------------------------------------ 回填

def backfill(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    dry_run: bool = False,
    use_llm: bool = True,
    limit: int | None = None,
    revive: bool = True,
    verbose: bool = True,
) -> dict:
    """把到期未判的断言判掉。返回统计 dict，不抛异常（模型挂了也不抛）。

    统计键与 converge.run_once 对齐（n_due / n_filled / n_correct / n_wrong /
    n_unverifiable / n_skipped / n_deferred），这样两边的数能直接对比。
    """
    today = resolve_as_of(as_of)
    cov = converge._coverage(conn)

    # 先复活「因缺证据被判 unverifiable、而该证据源现在已有数据」的断言。
    # 必须在查 due 之前 —— 否则这一轮它们还看不见，等于白修了证据通路。
    # 这个操作只改 verdict/actual/checked_at 三列，符合本文件「只写三列」的约束。
    revived = converge.revive_unverifiable(conn, dry_run=dry_run) if revive else []
    if revived and verbose:
        print(f"[兑现] 复活 {len(revived)} 条此前因缺证据被判 unverifiable 的断言："
              + "、".join(f"#{r['eval_id']}（{r['fragment']}，源 {r['source_rows']} 行）"
                          for r in revived))

    due = due_rows(conn, today, limit)
    if verbose:
        print(f"[兑现] as_of={today} 数据覆盖={cov[0]}~{cov[1]} 到期未判={len(due)} 条"
              + ("  (dry-run)" if dry_run else ""))

    stats = {
        "ok": True, "as_of": today, "dry_run": dry_run,
        "n_due": len(due), "n_filled": 0, "n_correct": 0, "n_wrong": 0,
        "n_unverifiable": 0, "n_skipped": 0, "n_deferred": 0,
        "n_revived": len(revived), "n_llm": 0,
        "coverage": {"from": cov[0], "to": cov[1]},
    }
    llm_used = 0
    rows_out: list[dict] = []

    for row in due:
        win = converge._parse_window(row["method"] or "", row["verifiable_at"] or "")
        ents = converge._parse_entities(conn, f"{row['method'] or ''} {row['claim'] or ''}")

        hit = converge.deterministic_verdict(conn, row, cov)
        if hit:
            verdict, actual, who = hit
        elif converge._window_state(win, cov) != "covered":
            # 窗口还没走完 → 保持 pending。理由同 converge：把「数据没追平」
            # 写成 unverifiable，幂等 UPDATE 会把它永久钉死。
            verdict, actual, who = (
                "pending",
                f"断言窗口 {converge._win_text(win)} 未走完（数据止于 {cov[1]}），"
                f"确定性判定器无法锁定结论，本轮不问模型，等 ingest 追平后自动重判",
                "gate",
            )
        elif use_llm and llm_used < LLM_BATCH_LIMIT:
            # 串行节流：429 与请求速率强相关，连续猛发只会连续被拒。
            if llm_used:
                time.sleep(LLM_THROTTLE_SECONDS)
            try:
                verdict, actual = converge.llm_verdict(conn, row, cov, win, ents, _model())
            except converge._ModelDown as exc:
                # 模型整体不可用：剩下的都没戏，收工。一条都不写库，保持 pending。
                stats["error"] = str(exc)
                stats["ok"] = False
                if verbose:
                    print(f"  ! {exc}")
                    print(f"  ! 本轮剩余 {len(due) - len(rows_out)} 条断言保持 pending，等网关恢复后重试")
                break
            who = "llm"
            llm_used += 1
            stats["n_llm"] = llm_used
        elif use_llm:
            verdict, actual, who = (
                "pending",
                f"本轮模型额度已用尽（上限 {LLM_BATCH_LIMIT} 条），未判定，下轮再判",
                "skipped",
            )
        else:
            verdict, actual, who = (
                "pending",
                "确定性判定器无法解析该断言（--no-llm 已关闭模型兜底），未判定",
                "skipped",
            )

        if verdict == "pending":
            stats["n_skipped"] += 1
            deferred = who != "skipped"
            if deferred:
                stats["n_deferred"] += 1
            rows_out.append({"eval_id": row["eval_id"], "verdict": verdict, "who": who,
                             "due": row["verifiable_at"], "expected": row["expected"],
                             "actual": actual})
            if verbose:
                tag = "本轮不判（数据未追平，下轮自动重判）" if deferred else "跳过（保持 pending）"
                print(f"  #{row['eval_id']:<4} due={row['verifiable_at']}  [{who}]  → {tag}")
                print(f"        expected: {row['expected']}")
                print(f"        actual  : {actual}")
            continue

        stats["n_filled"] += 1
        stats["n_" + verdict] += 1
        rows_out.append({"eval_id": row["eval_id"], "verdict": verdict, "who": who,
                         "due": row["verifiable_at"], "expected": row["expected"],
                         "actual": actual})
        if verbose:
            print(f"  #{row['eval_id']:<4} due={row['verifiable_at']}  [{who}]  → {verdict}")
            print(f"        expected: {row['expected']}")
            print(f"        actual  : {actual}")

        if not dry_run:
            # WHERE 带 verdict='pending' 是幂等的全部秘密：同一天重跑、两个进程
            # 并发跑，都只有一个能把这条从 pending 改走。只写三列，原话不动。
            cur = conn.execute(
                "update agent_eval set verdict=?, actual=?, checked_at=? "
                "where eval_id=? and verdict='pending'",
                (verdict, actual, datetime.now(CST).isoformat(timespec="seconds"),
                 row["eval_id"]),
            )
            if cur.rowcount == 0:      # 别人抢先判了，把这一条的统计撤回
                stats["n_filled"] -= 1
                stats["n_" + verdict] -= 1

    stats["rows"] = rows_out
    return stats


def _model() -> str:
    """模型名。converge.llm_verdict 需要一个明确的模型串，这里用它的默认链首。

    不读 --model 是因为 backfill 的调用方（run_agents.py）会把模型名透传到
    converge.run_once，而这里只做兜底回填 —— 用同一个默认值即可，
    真要指定模型就走 converge 那一轮。
    """
    from agents import gateway
    return gateway.DEFAULT_MODEL


# ------------------------------------------------------------------ 报表

def _bucket() -> dict:
    return {"correct": 0, "wrong": 0, "unverifiable": 0, "pending": 0, "total": 0}


def collect(conn: sqlite3.Connection, *, as_of: str, due_only: bool = False) -> dict:
    """按 agent 与 (agent, 族) 两个维度数兑现情况。

    due_only 的语义：报表只算**已到期**的断言（verifiable_at <= as_of）。
    为什么不给个开关把「未到期的 pending」也算进去：那些行天天都在、
    而且本来就不该判，把它们算进 pending 会把积压数顶到 140+，
    真正的「到期了还没判」就被淹没了。定时任务只关心后者。
    """
    where, params = ("where verifiable_at <= ?", (as_of,)) if due_only else ("", ())
    rows = conn.execute(
        f"select agent, method, verdict, verifiable_at from agent_eval {where}", params
    ).fetchall()

    by_agent: dict[str, dict] = {}
    by_family: dict[tuple[str, str], dict] = {}
    total = _bucket()
    for r in rows:
        agent = str(r["agent"] or "?")
        fam = method_family(r["method"])
        verdict = str(r["verdict"] or "pending")
        if verdict not in VERDICTS:
            verdict = "pending"
        a = by_agent.setdefault(agent, _bucket())
        f = by_family.setdefault((agent, fam), _bucket())
        for b in (a, f, total):
            b[verdict] += 1
            b["total"] += 1

    return {
        "as_of": as_of,
        "due_only": due_only,
        "rows": len(rows),
        "total": total,
        "by_agent": by_agent,
        "by_family": by_family,
    }


def _rate_cell(b: dict) -> str:
    """命中率。分母 0 返回 '—'，绝不返回 0.0%（会被读成「全错」）。"""
    return orr._rate(b["correct"], b["wrong"])


def render(d: dict) -> str:
    """两张表：按 agent，按 (agent, 族)。列序固定，逐期才能横着比。"""
    scope = "仅已到期" if d["due_only"] else "全部断言"
    out = [f"断言兑现率  as_of={d['as_of']}  口径={scope}  共 {d['rows']} 条", ""]

    header = ["agent", "命中率", "对", "错", "判不了", "未判", "合计"]
    aligns = ["left", "right", "right", "right", "right", "right", "right"]

    rows: list[list[str]] = []
    for agent in sorted(d["by_agent"]):
        b = d["by_agent"][agent]
        rows.append([agent, _rate_cell(b), str(b["correct"]), str(b["wrong"]),
                     str(b["unverifiable"]), str(b["pending"]), str(b["total"])])
    t = d["total"]
    rows.append(["合计", _rate_cell(t), str(t["correct"]), str(t["wrong"]),
                 str(t["unverifiable"]), str(t["pending"]), str(t["total"])])
    out.append(orr._table(header, rows, aligns))

    fam_header = ["agent", "断言族", "命中率", "对", "错", "判不了", "未判", "合计"]
    fam_aligns = ["left", "left", "right", "right", "right", "right", "right", "right"]
    fam_rows: list[list[str]] = []
    # 族序固定（FAMILY_ORDER），不按数量排 —— 按数量排的话每期顺序都在变，
    # 「这个族上期多少」就没法一眼看出来。
    for agent in sorted(d["by_agent"]):
        for fam in FAMILY_ORDER:
            b = d["by_family"].get((agent, fam))
            if not b:
                continue
            fam_rows.append([agent, FAMILY_LABEL.get(fam, fam), _rate_cell(b),
                             str(b["correct"]), str(b["wrong"]), str(b["unverifiable"]),
                             str(b["pending"]), str(b["total"])])
    if fam_rows:
        out.append("")
        out.append(orr._table(fam_header, fam_rows, fam_aligns))

    out.append("")
    out.append("命中率 = 对 ÷（对 + 错）；判不了与未判不进分母 —— "
               "判不了是判定器/数据的问题，未判是还没轮到，两者都不是 agent 说错。")
    return "\n".join(out)


# ------------------------------------------------------------------ CLI

def jsonable(d: dict) -> dict:
    """把 collect() 的结果转成 json.dumps 能吃的形状。

    为什么需要这一步：`by_family` 的键是 `(agent, 族)` 元组 —— 元组做键在
    Python 里很自然（一次查两维），但 JSON 的键只能是字符串，直接
    `json.dumps` 会抛 `TypeError: keys must be str, int, float, bool or None, not tuple`。
    这里摊平成 `{"agent/族": {...}}`，分隔符用 `/` 而不是空格，因为 agent 名
    （diagnose / attribute）和族名（下一次运行 / ACoS）都不含斜杠，摊平后
    仍能一眼拆回两维；用空格拆就会在「下一次运行」这种中文族名上歧义。
    """
    fam = {f"{a}/{k}": v for (a, k), v in (d.get("by_family") or {}).items()}
    out = dict(d)
    out["by_family"] = fam
    return out

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="断言兑现回填 + 兑现率报表（agent_eval 的唯一入口）"
    )
    ap.add_argument("--as-of", dest="as_of", default=None, metavar="YYYY-MM-DD",
                    help="把「今天」覆盖成指定日期（所有断言的到期日都在未来，调试时用）")
    ap.add_argument("--due-only", dest="due_only", action="store_true",
                    help="报表只统计已到期的断言（未到期的 pending 不计入积压）")
    ap.add_argument("--dry-run", action="store_true", help="只判不写，看会填成什么")
    ap.add_argument("--report-only", dest="report_only", action="store_true",
                    help="不判定，只出报表")
    ap.add_argument("--no-llm", dest="no_llm", action="store_true",
                    help="关闭模型兜底，只跑确定性判定器")
    ap.add_argument("--no-revive", dest="no_revive", action="store_true",
                    help="不复活「因缺证据被判 unverifiable、证据源现在有数据」的断言")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多判多少条")
    ap.add_argument("--db", default=None, help=f"库路径，默认 {DB_PATH}")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条判定明细")
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else DB_PATH
    if not Path(db).exists():
        print(f"库不在：{db}", file=sys.stderr)
        return 2

    conn = connect(db)
    try:
        stats = None
        if not args.report_only:
            stats = backfill(
                conn,
                as_of=args.as_of,
                dry_run=args.dry_run,
                use_llm=not args.no_llm,
                limit=args.limit,
                revive=not args.no_revive,
                verbose=not args.quiet,
            )
        report = collect(conn, as_of=resolve_as_of(args.as_of), due_only=args.due_only)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"stats": stats, "report": jsonable(report)},
                         ensure_ascii=False, indent=2, default=str))
        if stats is not None and not stats.get("ok"):
            return 1
        return 0

    if stats is not None:
        if args.dry_run:
            print(f"[兑现] dry-run：未写库。将填 {stats['n_filled']} 条"
                  f"（对 {stats['n_correct']} / 错 {stats['n_wrong']} / "
                  f"判不了 {stats['n_unverifiable']}），"
                  f"暂不判 {stats['n_deferred']} 条")
        else:
            print(f"[兑现] 完成：到期 {stats['n_due']} 条，回填 {stats['n_filled']} 条"
                  f"（对 {stats['n_correct']} / 错 {stats['n_wrong']} / "
                  f"判不了 {stats['n_unverifiable']}），暂不判 {stats['n_deferred']} 条"
                  f"（数据未追平，保持 pending）；模型调用 {stats['n_llm']} 次")
        if not stats.get("ok"):
            print(f"[兑现] 状态 degraded：{stats.get('error')}")
        print()
    print(render(report))
    return 0 if (stats is None or stats.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
