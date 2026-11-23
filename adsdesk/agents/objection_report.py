"""质疑命中率报表 —— 主人判断「要不要继续听这个 agent 的」的唯一依据。

为什么值得单独一个文件，而不是塞进 cockpit 面板：

1. cockpit 是「今天该干什么」的实时面板，看的是当下；
   这个报表是「过去 30 天它说的话准不准」，看的是趋势。
   两种东西混在一个接口里，谁都不好读，而且面板会被迫做跨月聚合，越拖越慢。

2. 它要能被 cron/launchd 直接跑出纯文本贴进群里，不依赖前端。

3. 这是唯一一个「允许怀疑 agent 本身」的入口。别的地方都在消费 agent 的结论，
   只有这里在给它打成绩 —— 所以口径必须写死在代码里，不能由调用方随便改。

口径（三句话，改之前先想清楚为什么原来这么定）：

- 命中率 = correct / (correct + wrong)。
  **unverifiable 不进分母**：判不了是「我们的判定器不够用」，不是「它说错了」。
  把判不了算成错，等于用工具的缺陷去惩罚 agent，会让命中率随判定器覆盖率漂移，
  这个数就没法纵向比了。unverifiable 单独一行展示。

- pending 不进分母，也不叫「没判」。pending 是「还没到能判的那天」，
  它今天的占比反映的是队列健康度，不是 agent 的水平。

- 历史行（predicts_json 为 NULL 的那 26 条）永远会是 pending：
  当初没有这一列，事后补判就是在编造判据。所以报表里必须能把它们和
  「真在等数据」的行分开 —— 这就是 `(无 predicts)` 那一行的意义。

用法：
    python3 adsdesk/agents/objection_report.py            # 最近 30 天
    python3 adsdesk/agents/objection_report.py --days 90
    python3 adsdesk/agents/objection_report.py --json      # 给别的程序吃
    ADS_DB=/tmp/x.sqlite3 python3 adsdesk/agents/objection_report.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))
CST = timezone(timedelta(hours=8))

# 结局的展示顺序与中文名。顺序固定，不按数量排 ——
# 报表要能一眼看出「correct 那一格在长还是在缩」，每期顺序乱跳就没法比。
OUTCOME_LABEL = {
    "correct": "对（预测兑现）",
    "wrong": "错（预测落空）",
    "unverifiable": "判不了（判定器覆盖不到）",
    "pending": "待判",
}
OUTCOMES = ("correct", "wrong", "unverifiable", "pending")


def _w(s: str) -> int:
    """字符串的显示宽度：CJK 全角算 2 列，其余算 1。

    为什么要自己算：中文表头用 ljust 对不齐，肉眼一看就是歪的，
    而歪掉的表没人愿意读第二遍。
    """
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(s))
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def _table(header: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    """按内容自适应列宽的表格。

    为什么不用固定宽度：中文标签长短差很多（「对（预测兑现）」vs「unverifiable ·
    判不了（无判定器/无定义）」），写死宽度必然有一行把整张表推歪。
    自适应是这里唯一不会出错的做法。
    """
    aligns = aligns or ["left"] * len(header)
    widths = [_w(h) for h in header]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], _w(c))
    lines = [_row_raw(header, widths, aligns)]
    for r in rows:
        lines.append(_row_raw(r, widths, aligns))
    return "\n".join(lines)


def _row_raw(cells: list[str], widths: list[int], aligns: list[str]) -> str:
    return "  ".join(_pad(c, w, a) for c, w, a in zip(cells, widths, aligns))


def _rate(correct: int, wrong: int) -> str:
    """命中率的唯一算法。分母为 0 时返回 '—'，绝不返回 0.0% ——
    0.0% 会被读成「它全错」，而事实是「还没判过」。"""
    n = correct + wrong
    if n == 0:
        return "—"
    return f"{correct / n * 100:.1f}%"


def collect(conn: sqlite3.Connection, days: int) -> dict:
    cutoff = (datetime.now(CST) - timedelta(days=days)).date().isoformat()
    base = conn.execute(
        "select count(*) from agent_objection where substr(created_at,1,10) >= ?", (cutoff,)
    ).fetchone()[0]

    rows = conn.execute(
        """select verdict, outcome, judged_by, predicts_json is null as no_pred,
                  decision_id
             from agent_objection
            where substr(created_at,1,10) >= ?""",
        (cutoff,),
    ).fetchall()

    def bucket() -> dict:
        return {"n": 0, "correct": 0, "wrong": 0, "unverifiable": 0, "pending": 0}

    total = bucket()
    by_outcome = {k: 0 for k in OUTCOMES}
    by_judged: dict[str, dict] = {}
    by_verdict: dict[str, dict] = {}
    by_decision: dict[int, int] = {}
    legacy_pending = 0

    for r in rows:
        outcome = r["outcome"]
        if outcome not in by_outcome:
            outcome = "pending"
        by_outcome[outcome] += 1
        total["n"] += 1
        total[outcome] += 1
        if outcome == "pending" and r["no_pred"]:
            legacy_pending += 1

        key = r["judged_by"] or "(未判)"
        b = by_judged.setdefault(key, bucket())
        b["n"] += 1
        b[outcome] += 1

        v = r["verdict"] or "(空)"
        b2 = by_verdict.setdefault(v, bucket())
        b2["n"] += 1
        b2[outcome] += 1

        by_decision[r["decision_id"]] = by_decision.get(r["decision_id"], 0) + 1

    return {
        "days": days,
        "cutoff": cutoff,
        "n": base,
        "total": total,
        "by_outcome": by_outcome,
        "by_judged": by_judged,
        "by_verdict": by_verdict,
        "legacy_pending": legacy_pending,
        "repeat": sorted(by_decision.items(), key=lambda kv: (-kv[1], kv[0]))[:8],
    }


def render(d: dict) -> str:
    out: list[str] = []
    out.append("")
    out.append(f"质疑命中率报表 · 最近 {d['days']} 天（{d['cutoff']} 起）")
    out.append("=" * 78)

    t = d["total"]
    judged = t["correct"] + t["wrong"]
    out.append(f"质疑总数        {t['n']}")
    out.append(f"已判（进分母）  {judged}   命中率 {_rate(t['correct'], t['wrong'])}")
    out.append(f"待判            {t['pending']}"
               + (f"（其中 {d['legacy_pending']} 条是无 predicts 的历史行，永远判不了）"
                  if d["legacy_pending"] else ""))
    out.append(f"判不了          {t['unverifiable']}（判定器覆盖不到，不计入命中率）")

    if judged == 0:
        out.append("")
        out.append("※ 还没有任何一条被判定过，「命中率」暂时无法计算。")
        out.append("  这不是 0%，是「没有样本」—— 任何把它读成 0% 的结论都是错的。")

    # ① 按结局
    out.append("")
    out.append("── 按结局 ──")
    rows = []
    for k in OUTCOMES:
        n = d["by_outcome"][k]
        pct = f"{n / t['n'] * 100:.0f}%" if t["n"] else "—"
        rows.append([f"{k} · {OUTCOME_LABEL[k]}", str(n), pct])
    out.append(_table(["结局", "条数", "占比"], rows, ["left", "right", "right"]))

    # ② 按判定器 —— 回答「哪个判定器在扛活、哪个一判就错」
    out.append("")
    out.append("── 按判定器 judged_by ──")
    rows = []
    for key in sorted(d["by_judged"], key=lambda k: (-d["by_judged"][k]["n"], k)):
        b = d["by_judged"][key]
        rows.append([key, str(b["n"]), str(b["correct"]), str(b["wrong"]),
                     str(b["unverifiable"]), _rate(b["correct"], b["wrong"])])
    out.append(_table(["judged_by", "条数", "对", "错", "判不了", "命中率"], rows,
                      ["left", "right", "right", "right", "right", "right"]))

    # ③ 按 agent 态度 —— 回答「它说 oppose 时是不是比 doubt 更准」
    out.append("")
    out.append("── 按 agent 态度 verdict（态度 ≠ 结果，故与 outcome 分开统计）──")
    rows = []
    for key in sorted(d["by_verdict"], key=lambda k: (-d["by_verdict"][k]["n"], k)):
        b = d["by_verdict"][key]
        rows.append([key, str(b["n"]), str(b["correct"]), str(b["wrong"]),
                     str(b["pending"]), _rate(b["correct"], b["wrong"])])
    out.append(_table(["verdict", "条数", "对", "错", "待判", "命中率"], rows,
                      ["left", "right", "right", "right", "right", "right"]))

    # ④ 重复质疑 —— 「连续 7 轮打同一条决策」是这里最值钱的信号：
    #    同一个 decision 被反复质疑，要么是真 bug 一直没修，要么是 agent 在空转。
    out.append("")
    out.append("── 被反复质疑的 decision（同一决策被质疑的次数）──")
    if d["repeat"]:
        rows = [[str(did), str(n)] for did, n in d["repeat"]]
        out.append(_table(["decision_id", "次数"], rows, ["left", "right"]))
        out.append("注：次数 ≥3 且这些行 outcome 多为 wrong → agent 在空转；多为 correct → 规则真有问题。")
    else:
        out.append("（无）")

    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="按 judged_by / outcome 聚合质疑命中率")
    ap.add_argument("--days", type=int, default=30, help="回看天数，默认 30")
    ap.add_argument("--db", default=None, help="库路径，默认 ingest/data/ads.sqlite3 或 $ADS_DB")
    ap.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON 而不是表格")
    args = ap.parse_args()

    path = Path(args.db) if args.db else DB_PATH
    if not path.exists():
        print(f"[错误] 库不存在：{path}", file=sys.stderr)
        return 1
    # 只读打开：报表永远不该有写权限，这是防止「看报表把生产库改了」的唯一手段。
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # 表 / 列缺失时给一句人话，而不是甩 OperationalError 栈
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_objection)")}
        if "outcome" not in cols:
            print("[错误] agent_objection 还没有 outcome 列，先跑 "
                  "python3 adsdesk/api/migrate.py", file=sys.stderr)
            return 1
        d = collect(conn, args.days)
    finally:
        conn.close()

    if args.as_json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(render(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())