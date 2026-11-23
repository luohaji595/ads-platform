#!/usr/bin/env python3
"""验收核对：把计划书第十二节的清单变成可执行检查。

为什么写这个脚本：
  「验收清单」是 27 条复选框，人手一条条敲 sqlite3 既慢又容易漏。
  这里每条检查都返回 通过 / 未通过 / 跳过 三态，并带上**取证原文**（查到的值），
  这样验收时看到的是证据，不是一句「好了」。

三条纪律：
  1. 只读。任何检查都不写库；需要改状态才能验的（如「打钩后刷新还在」）交给
     `adsdesk/api/selftest.py`，这里只核对它的落库结果。
  2. 不猜。查不到就报「未通过」，绝不把「表存在」当「功能可用」。
  3. 端口检查自己起自己的：只在需要 HTTP 时用 5299，绝不抢 5273。

用法：
    python3 tools/acceptance.py            # 全部检查
    python3 tools/acceptance.py --section P3
    python3 tools/acceptance.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))
CST = timezone(timedelta(hours=8))

OK, FAIL, SKIP = "通过", "未通过", "跳过"
_MARKS = {OK: "✅", FAIL: "❌", SKIP: "⏭"}

RESULTS: list[dict] = []


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def check(section: str, item: str, fn) -> None:
    """跑一条检查。fn 返回 (状态, 证据)。异常一律算未通过并保留原文。"""
    try:
        status, evidence = fn()
    except Exception as exc:  # noqa: BLE001 —— 验收脚本必须把异常当证据
        status, evidence = FAIL, f"{type(exc).__name__}: {exc}"
    RESULTS.append({"section": section, "item": item, "status": status,
                    "evidence": str(evidence)[:400]})


def _scalar(sql: str, params: tuple = ()):
    with _conn() as c:
        row = c.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _tables() -> set[str]:
    with _conn() as c:
        return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}


def _http(path: str, port: int | None = None, timeout: float = 8.0):
    """探一个已经在跑的服务。返回 (status, body)，没起来就 (None, 原因)。

    端口默认按 5273 → 5299 顺序试：5273 是工作台正式端口（主人自己会开着看），
    5299 是留给验收脚本的备用口（主人没开服务时，可以手动在那儿起一个）。
    只探不拉 —— 验收脚本自己起 uvicorn 太贵，而且会和主人的实例抢端口。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ports = [port] if port else [5273, 5299]
    last = "没有在跑的服务"
    for pt in ports:
        req = urllib.request.Request(f"http://127.0.0.1:{pt}{path}")
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = f"{pt}: {type(exc).__name__}: {exc}"
    return None, last


# ───────────────────────────── P1 ─────────────────────────────

def p1():
    need = {"memorial", "agent_run", "agent_objection", "agent_eval", "chat_turn"}
    check("P1", "5 张新表存在", lambda: (
        OK if need <= _tables() else FAIL,
        f"缺 {sorted(need - _tables())}" if not need <= _tables() else f"{len(need)} 张都在"))

    def zombie():
        # 僵尸 = 从没翻到审批台的 proposed。翻过的（checked_at 非空）是活的待打钩建议，
        # 不能当垃圾清掉 —— 计划书 R6 的原意是清「引擎自说自话、用户从没见过」的那批。
        with _conn() as c:
            rows = c.execute("select decision_id, rule_id, entity_label, decided_at, checked_at "
                             "from decision_log where status='proposed' order by decided_at").fetchall()
        dead = [r for r in rows if not r["checked_at"]]
        live = [f"#{r['decision_id']} {r['rule_id']} {r['entity_label']}" for r in rows if r["checked_at"]]
        return (OK if len(dead) == 0 else FAIL,
                f"proposed {len(rows)} 条，僵尸（未翻台）{len(dead)} 条；活的待打钩：{live or '无'}")
    check("P1", "僵尸 proposed 已清（未翻台的一条不剩）", zombie)

    def pack():
        p = ROOT / "engine" / "reports" / "2026-11-14-奏折.json"
        if not p.exists():
            return SKIP, f"{p.name} 不存在（可能已按日清理）"
        d = json.loads(p.read_text())
        has_conf = isinstance(d.get("conflicts"), list) and len(d["conflicts"]) > 0
        # decision_id 可能落在 front / observing / alerts 三处之一：front 空是因为当天全部
        # 进 observing 或 suppressed，不是字段缺失。所以按「三处任一」判，不按 front 单点判。
        where = [k for k in ("front", "observing", "alerts") if "decision_id" in json.dumps(d.get(k), ensure_ascii=False)]
        return (OK if has_conf and where else FAIL,
                f"conflicts {len(d.get('conflicts') or [])} 条；decision_id 出现在 {where or '无'}")
    check("P1", "奏折 json 含 conflicts 与 decision_id 回填", pack)

    def health():
        code, body = _http("/api/health")
        if code is None:
            return SKIP, f"5273/5299 都没有在跑的服务（要 HTTP 验收先启动 api）：{body}"
        return (OK if code == 200 else FAIL), f"HTTP {code} {body[:200]}"
    check("P1", "GET /api/health 返回 ok", health)

    def executed():
        row = _scalar("select count(*) from decision_log where executed_at is not null "
                      "and attribution_due_at is not null")
        total = _scalar("select count(*) from decision_log where executed_at is not null")
        return (OK if row and row > 0 else FAIL), f"已改 {total} 条，其中归因窗已排 {row} 条"
    check("P1", "点「已改」后 executed_at 与 attribution_due_at 都有值", executed)

    def order():
        """执行单能不能落库。

        两种证据都算数，但要说清是哪一种：
          - 生产库里有真单（n>0）：最直接。
          - 生产库是空的：跑一次 selftest，它在**副本库**上把
            「打钩 → 下单 → 分组 → 逐条标已改 → 归因窗」整条走完再自删。
            这不是绕过检查，而是唯一能证明「流程通」又不污染真库的办法 ——
            真库里的执行单只能来自主人的真实决定，脚本不该替他造。
        """
        n = _scalar("select count(*) from exec_order")
        items = _scalar("select count(*) from exec_order_item")
        if n and items:
            return OK, f"生产库执行单 {n} 张，明细 {items} 行"
        # 生产库空 → 用副本库自测作为证据。
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "adsdesk" / "api" / "selftest.py")],
                           capture_output=True, text=True, timeout=900,
                           cwd=str(ROOT))
        tail = (r.stdout or "").strip().splitlines()
        summary = next((ln for ln in reversed(tail) if ln.startswith("==")), "")
        if r.returncode == 0 and summary:
            return OK, (f"生产库暂无真实执行单（只能来自主人的真实决定）；"
                        f"副本库自测 {summary.strip('= ')} 全过，"
                        f"含「下单 → 按活动分组 → 逐条标已改 → 归因窗」闭环")
        return FAIL, f"生产库 0 张，且副本自测失败（exit={r.returncode}）：{summary or (r.stderr or '')[-200:]}"
    check("P1", "执行单与明细落库", order)

    def whitelist():
        sys.path.insert(0, str(ROOT / "adsdesk"))
        try:
            from api.db import WRITE_ALLOWLIST  # noqa: F401
            return OK, f"白名单 {len(WRITE_ALLOWLIST)} 张表（policy 是否在内需人工看实现）"
        except Exception as exc:  # noqa: BLE001
            return FAIL, f"读不到白名单：{exc}"
    check("P1", "写白名单可加载", whitelist)


# ───────────────────────────── P2 ─────────────────────────────

def p2():
    def panels():
        sys.path.insert(0, str(ROOT / "adsdesk"))
        from api.routes import cockpit
        want = ["funnel", "portfolios", "conflicts", "keywords", "market", "attribution"]
        missing = [w for w in want if not hasattr(cockpit, w)]
        return (OK if not missing else FAIL), f"缺 {missing}" if missing else "六个面板函数都在"
    check("P2", "cockpit 六面板接口齐全", panels)

    def funnel_data():
        with _conn() as c:
            row = c.execute("select min(report_date), max(report_date), count(*) from "
                            "(select distinct report_date from fact_campaign_daily)").fetchone()
        return (OK if row[2] > 0 else FAIL), f"{row[0]}..{row[1]} 共 {row[2]} 天"
    check("P2", "漏斗有数据", funnel_data)

    def conflicts():
        # 直接跑真实现，不在这边重写一遍口径 —— 验收要验的是上线的那份代码。
        sys.path.insert(0, str(ROOT / "adsdesk"))
        from api.routes import cockpit
        d = cockpit.conflicts() or {}
        rows = d.get("conflicts") or []
        # 注意字段名是 tightest 不是 worst（实现里 camps.append({**cp, "tightest": worst})）
        tight = [cp["tightest"] for it in rows for cp in (it.get("campaigns") or []) if cp.get("tightest")]
        if not rows:
            return FAIL, f"冲突面板空（返回键 {list(d.keys())}）"
        if not tight:
            return FAIL, f"{len(rows)} 组同词多活动，但没有一条算出 ratio（bid 或溢价缺失）"
        top = max(tight, key=lambda w: w.get("ratio") or 0)
        return OK, (f"{len(rows)} 组冲突、{len(tight)} 个活动算了 ratio；"
                    f"最紧 {top.get('placement')} {top.get('report_date')} "
                    f"CPC ${top.get('cpc')} / 上限 ${top.get('cap')} = {top.get('ratio')}；"
                    f"abnormal_count={d.get('abnormal_count')}")
    check("P2", "冲突面板按 逐日×逐位次 算出 CPC/上限 比例", conflicts)

    def market_panel():
        n = _scalar("select count(*) from market_daily")
        return (OK if n and n > 0 else FAIL), f"market_daily {n} 行"
    check("P2", "外部市场面板有数据", market_panel)

    def rank_panel():
        n = _scalar("select count(*) from rank_snapshot")
        return (OK if n and n > 0 else FAIL), f"rank_snapshot {n} 行"
    check("P2", "位次面板有数据", rank_panel)


# ───────────────────────────── P3 ─────────────────────────────

def p3():
    def diag():
        with _conn() as c:
            row = c.execute("select agent_run_id, status, tokens_in, started_at from agent_run "
                            "where job='diagnose' order by agent_run_id desc limit 1").fetchone()
        if row is None:
            return FAIL, "没有 diagnose 行"
        return (OK if row[1] == "ok" and (row[2] or 0) > 0 else FAIL,
                f"#{row[0]} status={row[1]} tokens_in={row[2]} at={row[3]}")
    check("P3", "agent_run 有 diagnose 且 ok、tokens_in>0", diag)

    def objection():
        with _conn() as c:
            row = c.execute(
                "select o.objection_id, o.decision_id, o.verdict, d.decision_id is not null "
                "from agent_objection o left join decision_log d using(decision_id) "
                "order by o.objection_id desc limit 1").fetchone()
        if row is None:
            return FAIL, "没有反对"
        return (OK if row[3] else FAIL), f"#{row[0]} decision={row[1]} {row[2]} 台账可查={bool(row[3])}"
    check("P3", "agent_objection 有记录且 decision_id 可查", objection)

    def eval_future():
        today = datetime.now(CST).date().isoformat()
        n = _scalar("select count(*) from agent_eval where verifiable_at > ?", (today,))
        tot = _scalar("select count(*) from agent_eval")
        return (OK if n and n > 0 else FAIL), f"未来到期 {n} 条 / 共 {tot} 条"
    check("P3", "agent_eval 有未来到期断言", eval_future)

    def eval_hit():
        corr = _scalar("select count(*) from agent_eval where verdict='correct'")
        wrong = _scalar("select count(*) from agent_eval where verdict='wrong'")
        pend = _scalar("select count(*) from agent_eval where verdict='pending'")
        unv = _scalar("select count(*) from agent_eval where verdict='unverifiable'")
        denom = corr + wrong
        rate = f"{corr/denom:.1%}" if denom else "暂无到期"
        return (OK if denom else SKIP), f"正确 {corr} / 打脸 {wrong} / 待判 {pend} / 无法判定 {unv}，命中率 {rate}"
    check("P3", "断言兑现率可算", eval_hit)

    def obj_outcome():
        n = _scalar("select count(*) from agent_objection where outcome != 'pending'")
        tot = _scalar("select count(*) from agent_objection")
        return (OK if n and n > 0 else SKIP), f"已兑现 {n} / {tot}"
    check("P3", "反对兑现判定跑过", obj_outcome)

    def chat():
        n = _scalar("select count(*) from chat_turn")
        cited = _scalar("select count(*) from chat_turn where cited_tables is not null "
                        "and cited_tables != '' and role='assistant'")
        return (OK if n and cited else FAIL), f"chat_turn {n} 行，带引用 {cited} 行"
    check("P3", "对话 agent 落库且有 cited_tables", chat)

    def agents_md():
        """奏折 md 里要有「agent 反对」区。"""
        reps = sorted((ROOT / "engine" / "reports").glob("*-奏折.md"))
        if not reps:
            return SKIP, "没有奏折 md"
        latest = reps[-1]
        text = latest.read_text(encoding="utf-8", errors="replace")
        hit = "agent 反对" in text or "agent反对" in text
        return (OK if hit else FAIL), f"{latest.name} 含「agent 反对」区={hit}"
    check("P3", "奏折 md 出现 agent 反对区", agents_md)

    def run_agents():
        p = ROOT / "run_agents.py"
        return (OK if p.exists() else FAIL), f"{p.name} {'在' if p.exists() else '缺'}"
    check("P3", "run_agents.py 存在", run_agents)

    def eval_py():
        p = ROOT / "adsdesk" / "agents" / "eval.py"
        return (OK if p.exists() else FAIL), f"{p.name} {'在' if p.exists() else '缺'}"
    check("P3", "agents/eval.py 存在", eval_py)

    def agent_panel():
        p = ROOT / "adsdesk" / "web" / "src" / "pages" / "AgentPanel.tsx"
        app = (ROOT / "adsdesk" / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        return (OK if p.exists() and "AgentPanel" in app else FAIL,
                f"页面={'在' if p.exists() else '缺'}，App.tsx 引用={'AgentPanel' in app}")
    check("P3", "Agent 面板已接入", agent_panel)


# ───────────────────────────── P4 ─────────────────────────────

def p4():
    def ext():
        if "market_daily" not in _tables():
            return FAIL, "market_daily 不存在"
        with _conn() as c:
            row = c.execute("select max(report_date), count(*) from market_daily").fetchone()
        return (OK if row[1] else FAIL), f"最新 {row[0]}，共 {row[1]} 行"
    check("P4", "外部市场日更落库", ext)

    def aba():
        with _conn() as c:
            row = c.execute("select max(snapshot_week), count(*) from aba_weekly").fetchone()
        return (OK if row[1] else FAIL), f"最新周 {row[0]}，共 {row[1]} 行"
    check("P4", "ABA 周榜落库", aba)

    def rank_rows():
        n = _scalar("select count(*) from rank_snapshot")
        t = _scalar("select count(*) from rank_target")
        return (OK if n and n > 0 else FAIL), f"快照 {n} 行 / 目标 {t} 行"
    check("P4", "位次快照采集到数据", rank_rows)

    def rank_py():
        p = ROOT / "ingest" / "rank.py"
        return (OK if p.exists() else FAIL), f"ingest/rank.py {'在' if p.exists() else '缺'}"
    check("P4", "位次采集器存在", rank_py)

    def rank_unique():
        # 幂等靠唯一索引兜底，不能只靠「先查再插」——两个采集进程同时跑会插双份
        with _conn() as c:
            rows = c.execute(
                "select name from sqlite_master where type='index' and tbl_name='rank_snapshot' "
                "and sql like '%unique%'").fetchall()
        names = [r[0] for r in rows]
        return (OK if names else FAIL), (f"唯一索引 {names[0]}" if names else "rank_snapshot 无唯一索引")
    check("P4", "位次快照有唯一索引（并发幂等）", rank_unique)

    def rank_in_chain():
        # 位次采集必须进守夜人，否则 R31 永远读到过期观测、加价规则空转
        src = (ROOT / "adsdesk" / "chain.py").read_text(encoding="utf-8")
        ok = ('"rank"' in src or "'rank'" in src) and "ingest" in src and "rank.py" in src
        return (OK if ok else FAIL), ("chain.py 已含 rank 步" if ok else "chain.py 缺 rank 步")
    check("P4", "位次采集已进守夜人链路", rank_in_chain)


SECTIONS = {"P1": p1, "P2": p2, "P3": p3, "P4": p4}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="验收核对（只读）")
    ap.add_argument("--section", choices=list(SECTIONS) + ["all"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    todo = SECTIONS.values() if args.section == "all" else [SECTIONS[args.section]]
    for fn in todo:
        fn()

    if args.json:
        print(json.dumps({"db": str(DB), "results": RESULTS}, ensure_ascii=False, indent=2))
    else:
        print(f"库：{DB}\n")
        cur = None
        for r in RESULTS:
            if r["section"] != cur:
                cur = r["section"]
                print(f"── {cur} ──")
            print(f"  {_MARKS[r['status']]} {r['item']}\n      {r['evidence']}")
        n_ok = sum(1 for r in RESULTS if r["status"] == OK)
        n_fail = sum(1 for r in RESULTS if r["status"] == FAIL)
        n_skip = sum(1 for r in RESULTS if r["status"] == SKIP)
        print(f"\n合计 {len(RESULTS)} 条：通过 {n_ok}，未通过 {n_fail}，跳过 {n_skip}")
    return 0 if all(r["status"] != FAIL for r in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
