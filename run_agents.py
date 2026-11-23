#!/usr/bin/env python3
"""agent 总调度：一条命令按序跑 diagnose / attribute / converge / eval / converse。

为什么要有这个文件（2026-11-23）：

    `adsdesk/chain.py` 那个守夜人负责的是「数据链」—— ingest → engine →
    diagnose → attribute → converge，它关心的是「今天的数据补到哪一步了」，
    判定完成看的是 memorial / converge_run 这些真实状态。

    但日常调试和 P3 验收要的是另一件事：**手动跑一次，然后一眼看到
    「这次到底产出了什么」**。chain.py 不回答这个 —— 它的输出是一行行
    进度日志，跑完之后 agent_run 新增了哪几行、agent_eval 多了几条断言、
    agent_objection 有没有反对，全靠人再去 sqlite 里捞。

    所以这个文件只做「跑 + 记账 + 汇总」，不做调度决策：没有时间窗、
    没有重试上限、没有锁。它是手上用的工具，chain.py 是机器上用的守夜人。
    两者互不替代，也互不引用（chain.py 的 build_cmd 里没有本文件）。

为什么优先 import 而不是 subprocess：

    subprocess 只能拿到退出码和 stdout 文本，要结构化结果就得解析日志 ——
    那是拿正则去啃人写给人看的话，改一句措辞就崩。直接 import 各模块的
    `run_once()` / `run()` 能拿到它们本来就返回的 dict（diagnose 直接返回
    agent_run_id，converge 返回 n_due/n_filled 等）。拿不到的结构化字段
    （attribute 的 stat 里没有 agent_run_id）用「跑前/跑后 max(id) 差」补，
    这比解析日志可靠得多。

    subprocess 只在 import 失败时兜底 —— 比如某个模块被改坏了语法、
    或者它自己 import 了一个装不上的依赖。兜底时把代理环境变量清掉：
    本机 http_proxy 指向 127.0.0.1:7897，而模型网关是 127.0.0.1:3425，
    子进程读到 HTTP_PROXY 会把本地请求也送进代理，实测报 502。

退出码：

    全部 ok（含 skipped）→ 0；任一 job 失败 → 1。
    这是给 launchd / shell chain 用的：它们只认退出码，不读中文汇总表。
    「converse 没给 --text 所以跳过」不算失败 —— 那是有意的跳过，不是出错。

用法：
    python3 run_agents.py --job diagnose
    python3 run_agents.py --job all --verbose
    python3 run_agents.py --job all --dry-run
    python3 run_agents.py --job eval --as-of 2026-12-31
    python3 run_agents.py --job converse --text "这周高位下拉为什么掉单"
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve()
ROOT = HERE.parent
# 两个 path 插入，顺序与 converge.py / eval.py 一致：
#   ROOT/adsdesk 让 `from agents import ...` 能解析；
#   ROOT/engine 让 agents 内部 `import facts` 能解析。
sys.path.insert(0, str(ROOT / "adsdesk"))
sys.path.insert(0, str(ROOT / "engine"))

CST = ZoneInfo("Asia/Shanghai")
DB_PATH = Path(os.environ.get("ADS_DB") or (ROOT / "ingest" / "data" / "ads.sqlite3"))

# job 的固定顺序。「all」按这个顺序跑，理由是有真实依赖：
#   diagnose  写断言/反对（agent_eval / agent_objection 的产地）
#   attribute 回看归因窗，也写断言 —— 必须在 diagnose 之后，
#             否则这一轮新写的断言没人看
#   converge  判断言和反对 —— 必须在两者之后，否则它判的是上一轮的库存
#   eval      回填 converge 挂起的（数据未追平那部分）+ 出兑现率报表
#   converse  用最新奏折答一句追问 —— 放最后，它读的是别人刚写完的奏折
JOBS = ("diagnose", "attribute", "converge", "eval", "converse")
ALL_ORDER = JOBS

# 哪些 job 必须调模型。dry-run 时据此决定要不要探网关。
NEEDS_MODEL = {"diagnose", "attribute", "converge", "converse"}

# 汇总表要数的两张表
COUNT_TABLES = (("agent_eval", "eval_id"), ("agent_objection", "objection_id"))


# ------------------------------------------------------------------ 基础设施

def say(msg: str) -> None:
    print(msg, flush=True)


def resolve_db(explicit: str | None) -> Path:
    return Path(explicit) if explicit else DB_PATH


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def max_id(conn: sqlite3.Connection, table: str, col: str) -> int:
    try:
        row = conn.execute(f"select coalesce(max({col}),0) from {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def snapshot(conn: sqlite3.Connection) -> dict:
    """跑之前的账本水位。用 max(id) 而不是 count(*)：
    并发时可能有别的进程删行（diagnose 的 _reap_stale 会删 running 僵尸行），
    count 会因此变负，而 max(id) 只会往前走。"""
    snap = {"agent_run": max_id(conn, "agent_run", "agent_run_id")}
    for table, col in COUNT_TABLES:
        snap[table] = max_id(conn, table, col)
    return snap


def delta(conn: sqlite3.Connection, before: dict) -> dict:
    after = snapshot(conn)
    return {k: after[k] - before[k] for k in after}


def new_agent_run_ids(conn: sqlite3.Connection, after_id: int, job: str) -> list[int]:
    """跑完之后新增的 agent_run_id。按 job 过滤 —— 同一时刻 chain.py
    可能正在跑另一个 job，不过滤会把它的行算到自己头上。"""
    try:
        rows = conn.execute(
            "select agent_run_id from agent_run where agent_run_id > ? and job = ? "
            "order by agent_run_id",
            (after_id, job),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r[0]) for r in rows]


# ------------------------------------------------------------------ 预检

# 账本表：所有 job 都要，缺了连「跑了什么」都记不下来。
BASE_TABLES = ("agent_run", "agent_eval", "agent_objection", "memorial", "decision_log")

# 事实表：判定器全部是拿 SQL 去 facts 里捞数，缺一张就有一族断言永远判不了。
# 这里列的是真表名（曾误写成 fact_daily，库里根本没有这张表 —— 写名字要
# 照 sqlite_master 抄，不能照印象写）。
FACT_TABLES = ("fact_campaign_daily", "fact_keyword_daily",
               "fact_placement_daily", "fact_search_term_daily")

# 哪些 job 要读事实表。converse 只读奏折与聊天记录，不碰 facts，
# 所以它不该因为 facts 缺表而被拦下。
NEEDS_FACTS = {"diagnose", "attribute", "converge", "eval"}


def preflight(db: Path, jobs: list[str],
              as_of: str | None = None) -> tuple[bool, list[str], list[str]]:
    """检查依赖与库表。返回 (能跑?, 已具备清单, 缺什么清单)。

    dry-run 只跑到这里就返回 —— 不写库、不调模型。
    理由：dry-run 的价值是「上生产前先看一遍要花什么代价」，
    如果它自己也调模型，那它就不能在半夜、断网、没额度的时候跑，
    而这个工具最需要跑的时刻恰恰是那种时候。

    as_of 必须由调用方传进来（而不是这里取 now）：`--as-of 2026-12-31`
    时预检数出来的「到期未判断言」必须和真正会跑的 eval 用同一个日期，
    否则预检报 39 条、实际跑 145 条，这个数字就成了误导。
    """
    ok: list[str] = []
    bad: list[str] = []

    if db.exists():
        ok.append(f"库存在 {db}")
    else:
        bad.append(f"库不在 {db}")
        return False, ok, bad

    conn = connect(db)
    try:
        have = {r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'").fetchall()}
        need = list(BASE_TABLES)
        if any(j in NEEDS_FACTS for j in jobs):
            need += list(FACT_TABLES)
        for t in need:
            if t in have:
                ok.append(f"表 {t}")
            else:
                bad.append(f"表 {t} 缺失")

        cut = as_of or datetime.now(CST).date().isoformat()
        if "agent_eval" in have:
            n = conn.execute(
                "select count(*) from agent_eval where verdict='pending' and verifiable_at <= ?",
                (cut,)).fetchone()[0]
            ok.append(f"到期未判断言 {n} 条（as_of={cut}）")
        if "agent_objection" in have:
            n = conn.execute(
                "select count(*) from agent_objection where outcome='pending'").fetchone()[0]
            ok.append(f"待判质疑 {n} 条")
    finally:
        conn.close()

    if any(j in NEEDS_MODEL for j in jobs):
        try:
            from agents import gateway
            h = gateway.health()
            ok.append(f"模型网关可用（{h.get('count')} 个模型，默认 {gateway.DEFAULT_MODEL}）")
        except Exception as exc:
            bad.append(f"模型网关不可用：{type(exc).__name__}: {exc}")

    return (not bad), ok, bad


# ------------------------------------------------------------------ 各 job

def _import(mod: str):
    """按需 import。放在函数里是为了让 --dry-run 在某个模块坏掉时仍能预检。"""
    import importlib
    return importlib.import_module(f"agents.{mod}")


def run_diagnose(conn, *, model: str | None, as_of: str | None) -> dict:
    mod = _import("diagnose")
    # diagnose 不支持 --as-of：它永远按库里最新一个 run 出结论。
    # 传了 as_of 只提示，不假装支持 —— 假装支持会让 --as-of 变成安慰剂。
    note = "diagnose 不接受 --as-of（永远按库里最新 run），已忽略" if as_of else ""
    res = mod.run_once(conn, model=model, shadow=True, dry_run=False)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "未知错误",
                "agent_run_ids": [res["agent_run_id"]] if res.get("agent_run_id") else [],
                "note": note}
    return {
        "ok": True,
        "agent_run_ids": [res["agent_run_id"]] if res.get("agent_run_id") else [],
        "n_eval": res.get("n_eval", 0),
        "n_objection": res.get("n_obj", 0),
        "note": "；".join(filter(None, [
            f"run {res.get('run_id')}",
            f"模型 {res.get('model')}",
            "降级" if res.get("fell_back") else "",
            note,
        ])),
        "raw": {k: res.get(k) for k in ("run_id", "n_eval", "n_obj", "model", "tokens")},
    }


def run_attribute(conn, *, db: Path, as_of: str | None) -> dict:
    mod = _import("attribute")
    now = None
    if as_of:
        # attribute 的 --now 是「假装现在是这个时间」。给到当天 23:59:59，
        # 让所有当天到期的归因窗都算到期 —— 否则 --as-of 只是改个标签。
        now = datetime.fromisoformat(f"{as_of}T23:59:59").replace(tzinfo=CST)
    stat = mod.run(db=str(db), now=now, dry_run=False, verbose=False)
    return {
        "ok": True,
        "n_eval": 0,          # 新建断言数由 delta 数出来，这里不猜
        "n_objection": 0,
        "note": (f"到期 {stat.get('due', 0)} 条、14 天补看 {stat.get('second_pass', 0)} 条 → "
                 f"strong {(stat.get('levels') or {}).get('strong', 0)} / "
                 f"medium {(stat.get('levels') or {}).get('medium', 0)} / "
                 f"weak {(stat.get('levels') or {}).get('weak', 0)}"),
        "raw": {k: stat.get(k) for k in ("due", "second_pass", "filled", "levels")},
    }


def run_converge(conn, *, model: str | None, as_of: str | None, dry_run: bool) -> dict:
    mod = _import("converge")
    res = mod.run_once(conn, as_of=as_of, model=model, dry_run=dry_run,
                       use_llm=not dry_run, judge_eval=True, with_objections=True)
    return {
        "ok": bool(res.get("ok")),
        "error": res.get("error"),
        "n_eval": 0,
        "n_objection": 0,
        "note": (f"到期断言 {res.get('n_due', 0)} 条 → 回填 {res.get('n_filled', 0)}"
                 f"（对 {res.get('n_correct', 0)} / 错 {res.get('n_wrong', 0)} / "
                 f"判不了 {res.get('n_unverifiable', 0)}），暂不判 {res.get('n_deferred', 0)}；"
                 f"质疑到期 {res.get('n_objection_due', 0)} 判 {res.get('n_objection_judged', 0)}"),
        "raw": {k: res.get(k) for k in ("n_due", "n_filled", "n_correct", "n_wrong",
                                        "n_unverifiable", "n_deferred",
                                        "n_objection_due", "n_objection_judged")},
    }


def run_eval(conn, *, as_of: str | None, dry_run: bool, limit: int | None) -> dict:
    """调 eval 模块的 backfill + collect。dry-run 走「确定性判定器预览」：
    不调模型、不写库，但能看到真实会填成什么 —— 因为确定性判定器全是纯 SQL。"""
    mod = _import("eval")
    stats = mod.backfill(conn, as_of=as_of, dry_run=dry_run,
                         use_llm=not dry_run, limit=limit, revive=not dry_run,
                         verbose=False)
    rep = mod.collect(conn, as_of=mod.resolve_as_of(as_of), due_only=True)
    t = rep["total"]
    rate = "—" if (t["correct"] + t["wrong"]) == 0 else \
        f"{t['correct'] / (t['correct'] + t['wrong']) * 100:.1f}%"
    return {
        "ok": bool(stats.get("ok")),
        "error": stats.get("error"),
        "n_eval": 0,
        "n_objection": 0,
        "note": (f"{'将填' if dry_run else '回填'} {stats.get('n_filled', 0)} 条"
                 f"（对 {stats.get('n_correct', 0)} / 错 {stats.get('n_wrong', 0)} / "
                 f"判不了 {stats.get('n_unverifiable', 0)}），暂不判 {stats.get('n_deferred', 0)}；"
                 f"已到期命中率 {rate}（对 {t['correct']} / 错 {t['wrong']} / "
                 f"未判 {t['pending']}）"),
        "raw": {"stats": {k: v for k, v in stats.items() if k != "rows"},
                "report_total": t},
    }


def run_converse(*, text: str | None, model: str | None, as_of: str | None,
                 dry_run: bool) -> dict:
    if not text:
        return {"ok": True, "skipped": True, "n_eval": 0, "n_objection": 0,
                "note": "未给 --text，跳过（不算失败：converse 是可选的一问一答）"}
    mod = _import("converse")
    res = mod.run_once(text=text, model=model, as_of=as_of, dry_run=dry_run)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "n_eval": 0, "n_objection": 0,
                "note": f"失败类型 {res.get('kind')}"}
    if res.get("dry_run"):
        return {"ok": True, "n_eval": 0, "n_objection": 0,
                "note": f"dry-run：奏折 #{res.get('run_id')}、历史 {res.get('history_used')} 条、"
                        f"system {res.get('system_chars')} 字"}
    return {"ok": True, "n_eval": 0, "n_objection": 0,
            "note": f"turn {res.get('turn_id')}，回复 {len(res.get('reply') or '')} 字",
            "raw": {k: res.get(k) for k in ("turn_id", "user_turn_id", "elapsed_ms")}}


# ------------------------------------------------------------------ 兜底：subprocess

def _clean_env(db: Path) -> dict:
    """子进程环境：清掉代理，指到同一个库。

    代理必须清：gateway 的地址是 127.0.0.1:3425，而本机 http_proxy 指向
    127.0.0.1:7897。urllib 默认会读环境变量里的代理，于是本地请求被送进代理，
    实测返回 502。gateway.py 自己 build_opener 禁了代理，但那是进程内的，
    子进程里任何第三方库都可能自己再读一次环境变量。
    """
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env["ADS_DB"] = str(db)
    env["PYTHONUNBUFFERED"] = "1"
    return env


SUB_CMD = {
    "diagnose": ["adsdesk/agents/diagnose.py"],
    "attribute": ["adsdesk/agents/attribute.py"],
    "converge": ["adsdesk/agents/converge.py"],
    "eval": ["adsdesk/agents/eval.py"],
}


def run_subprocess(job: str, db: Path, extra: list[str]) -> dict:
    """import 失败时的兜底。只拿退出码，结构化数字一律由 delta 数。"""
    cmd = [sys.executable] + [str(ROOT / p) for p in SUB_CMD[job]] + extra
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=_clean_env(db),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=3600)
        out, code = proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        out, code = (exc.stdout or "") + "\n[run_agents] 超时 3600s", 124
    tail = "\n".join(out.strip().splitlines()[-6:])
    return {"ok": code == 0, "error": None if code == 0 else f"退出码 {code}",
            "n_eval": 0, "n_objection": 0,
            "note": f"subprocess 兜底（import 失败）：{tail}".replace("\n", " / ")[:400],
            "agent_run_ids": []}


# ------------------------------------------------------------------ 汇总表

def _w(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(s))
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def render_table(rows: list[dict]) -> str:
    """中文汇总表。列宽按内容自适应 —— 中文标签长短差很多，
    写死宽度必然有一行把整张表推歪，而歪掉的表没人愿意读第二遍。"""
    header = ["job", "状态", "耗时", "新增 agent_run_id", "新增断言", "新增反对", "备注"]
    aligns = ["left", "left", "right", "left", "right", "right", "left"]
    body: list[list[str]] = []
    for r in rows:
        ids = r.get("agent_run_ids") or []
        body.append([
            r["job"],
            r["status"],
            f"{r['elapsed']:.1f}s",
            "、".join(f"#{i}" for i in ids) if ids else "—",
            str(r.get("n_eval", 0)),
            str(r.get("n_objection", 0)),
            (r.get("note") or r.get("error") or "").strip() or "—",
        ])
    widths = [_w(h) for h in header]
    for r in body:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], _w(c))
    lines = ["  ".join(_pad(c, w, a) for c, w, a in zip(header, widths, aligns))]
    for r in body:
        lines.append("  ".join(_pad(c, w, a) for c, w, a in zip(r, widths, aligns)))
    return "\n".join(lines)


# ------------------------------------------------------------------ 主流程

def run_job(job: str, conn, *, db: Path, model: str | None, as_of: str | None,
            text: str | None, dry_run: bool, limit: int | None,
            verbose: bool) -> dict:
    """跑一个 job，返回统一形状的结果 dict。

    统一形状是汇总表能一行行打出来的前提：各模块返回的键各不相同
    （diagnose 有 agent_run_id，converge 有 n_filled，converse 有 turn_id），
    在各自的分支里就地翻译成 {ok, status, elapsed, n_eval, n_objection, note}。
    """
    before = snapshot(conn)
    t0 = time.time()
    res: dict
    buf = io.StringIO()

    # 非 verbose 时把模块的啰嗦输出收进缓冲：这些模块的 stdout 是给人读的
    # 逐条流水，一次 all 能刷几百行，汇总表会被冲到看不见。
    #
    # 这里必须用 redirect_stdout(buf)，**不能**写 `with buf:` ——
    # StringIO 作为上下文管理器，退出时会把自己 close 掉，之后
    # buf.getvalue() 就抛 "I/O operation on closed file"。那个异常发生在
    # 「job 已经失败、正要取尾部日志」的当口，结果是真正的失败原因被这个
    # 二次异常盖掉，只剩一句看不懂的 ValueError。（2026-11-23 实测踩到。）
    sink = (contextlib.redirect_stdout(buf) if not verbose
            else contextlib.nullcontext())

    try:
        with sink:
            if job == "diagnose":
                res = run_diagnose(conn, model=model, as_of=as_of)
            elif job == "attribute":
                res = run_attribute(conn, db=db, as_of=as_of)
            elif job == "converge":
                res = run_converge(conn, model=model, as_of=as_of, dry_run=dry_run)
            elif job == "eval":
                res = run_eval(conn, as_of=as_of, dry_run=dry_run, limit=limit)
            elif job == "converse":
                res = run_converse(text=text, model=model, as_of=as_of, dry_run=dry_run)
            else:
                raise KeyError(job)
    except Exception as exc:
        # import 失败 / 模块内部抛错 → subprocess 兜底（converse 没有 CLI 兜底路径：
        # 它要一句用户输入，参数拼起来容易错，直接报错更诚实）
        if job in SUB_CMD:
            extra = []
            if as_of:
                extra += ["--as-of", as_of]
            if model:
                extra += ["--model", model]
            if dry_run:
                extra += ["--dry-run"]
            res = run_subprocess(job, db, extra)
            res["note"] = f"{type(exc).__name__}: {exc} → {res['note']}"
        else:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "n_eval": 0, "n_objection": 0, "note": ""}

    elapsed = time.time() - t0
    d = delta(conn, before)

    # 新增 agent_run_id：diagnose 自己会返回，其它 job 用 delta + 按 job 过滤补。
    ids = list(res.get("agent_run_ids") or [])
    if not ids and d.get("agent_run", 0) > 0:
        ids = new_agent_run_ids(conn, before["agent_run"], job)

    # 断言/反对的新增数优先用模块自己报的（diagnose 的 n_eval 是它亲手 insert 的
    # 条数，比 delta 准 —— delta 会把并发进程写的行算进来）。
    n_eval = res.get("n_eval") or 0
    n_obj = res.get("n_objection") or 0
    if job in ("diagnose", "attribute") and not n_eval:
        n_eval = max(0, d.get("agent_eval", 0))
    if job in ("diagnose", "attribute") and not n_obj:
        n_obj = max(0, d.get("agent_objection", 0))

    status = "skipped" if res.get("skipped") else ("ok" if res.get("ok") else "failed")
    out = {
        "job": job, "status": status, "elapsed": elapsed,
        "agent_run_ids": ids, "n_eval": n_eval, "n_objection": n_obj,
        "note": res.get("note") or "", "error": res.get("error"),
        "raw": res.get("raw"),
    }
    if not verbose and status == "failed":
        tail = "\n".join(buf.getvalue().strip().splitlines()[-8:])
        if tail:
            out["note"] = (out["note"] + " || " + tail.replace("\n", " / "))[:600]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="agent 总调度：按序跑 diagnose / attribute / converge / eval / converse，出一张汇总表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：全部 ok（含 skipped）→ 0；任一 job 失败 → 1。",
    )
    ap.add_argument("--job", required=True, choices=JOBS + ("all",),
                    help="要跑的 job；all 按 diagnose→attribute→converge→eval→converse 顺序跑")
    ap.add_argument("--as-of", dest="as_of", default=None, metavar="YYYY-MM-DD",
                    help="把「今天」覆盖成指定日期（调试用：断言到期日都在未来）")
    ap.add_argument("--model", default=None, help="模型名，默认各模块自己的选择")
    ap.add_argument("--text", default=None,
                    help="converse 要说的一句话；不给就跳过 converse（不算失败）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检查依赖与库表、打印将要做的事，不写库、不调模型")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON（不打印表）")
    ap.add_argument("--verbose", action="store_true",
                    help="透传各模块的逐条流水（默认收进缓冲，失败时才打印尾部）")
    ap.add_argument("--db", default=None, help=f"库路径，默认 {DB_PATH}")
    ap.add_argument("--limit", type=int, default=None, help="透传给 converge / eval 的条数上限")
    args = ap.parse_args(argv)

    db = resolve_db(args.db)
    jobs = list(ALL_ORDER) if args.job == "all" else [args.job]
    as_of = args.as_of or datetime.now(CST).date().isoformat()

    # converse 没给 --text 时仍留在列表里，让它显示成 skipped ——
    # 直接摘掉的话，读者会以为 all 本来就只有四个 job。
    ok, good, bad = preflight(db, jobs, as_of)

    if args.dry_run:
        payload = {
            "as_of": as_of, "dry_run": True, "db": str(db), "jobs": jobs,
            "preflight": {"ok": ok, "have": good, "missing": bad},
            "plan": [],
        }
        for j in jobs:
            plan = {
                "diagnose": "调模型出新奏折结论 → 写 agent_run 一行 + agent_eval/agent_objection 若干",
                "attribute": "回看已到期的归因窗 → 写 agent_run 一行 + agent_eval 若干",
                "converge": "判到期断言与质疑 → 写 converge_run 一行（dry-run 下不写）",
                "eval": "回填到期断言（确定性判定器预览，不调模型）+ 出兑现率报表",
                "converse": "用最新奏折答一句追问 → 写 chat_turn 两行",
            }[j]
            if j == "converse" and not args.text:
                plan = "跳过：未给 --text"
            payload["plan"].append({"job": j, "will": plan})

        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            say(f"[预检] 库 {db}  as_of={as_of}")
            for g in good:
                say(f"  ✓ {g}")
            for b in bad:
                say(f"  ✗ {b}")
            say("")
            say("[将要做的事]（dry-run：不写库、不调模型）")
            for p in payload["plan"]:
                say(f"  {p['job']:<10} {p['will']}")
            if not ok:
                say("")
                say("[预检] 有缺失项，正常跑会失败 —— 先补上面的 ✗")
        # dry-run 的退出码看预检：预检不过就是「现在跑会失败」，
        # 那它就该和真跑失败一样返回非 0，否则 shell chain 会以为能跑。
        return 0 if ok else 1

    if not ok:
        say("[预检] 有缺失项，中止：")
        for b in bad:
            say(f"  ✗ {b}")
        return 1

    conn = connect(db)
    results: list[dict] = []
    try:
        for job in jobs:
            if not args.json:
                say(f"▶ {job} …")
            results.append(run_job(
                job, conn, db=db, model=args.model, as_of=args.as_of,
                text=args.text, dry_run=False, limit=args.limit,
                verbose=args.verbose,
            ))
    finally:
        conn.close()

    any_failed = any(r["status"] == "failed" for r in results)

    if args.json:
        print(json.dumps({
            "as_of": as_of, "dry_run": False, "db": str(db),
            "ok": not any_failed, "jobs": results,
        }, ensure_ascii=False, indent=2, default=str))
        return 1 if any_failed else 0

    say("")
    say(f"[汇总] as_of={as_of}  库 {db}  共 {len(results)} 个 job")
    say(render_table(results))
    say("")
    if any_failed:
        failed = [r["job"] for r in results if r["status"] == "failed"]
        say(f"[汇总] 失败：{'、'.join(failed)}")
        return 1
    say("[汇总] 全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
