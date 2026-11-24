#!/usr/bin/env python3
"""日更链：ingest → engine → diagnose → attribute → converge，一条命令按序跑完。

为什么要有这个文件（2026-11-19）：
    这台 Mac 每天 9 点才开机，而原来的五个 launchd 定在 06:10~07:15。
    launchd 的 StartCalendarInterval 在机器**关机**时错过的触发点不会补跑
    （睡眠中错过的会合并成唤醒后一次，关机不会），所以那条链在真机上
    基本等于没接线：数据停在 11-17、attribute 一次没跑过、converge 只有手工跑过。

    改成「守夜人」结构：
      一个 job，RunAtLoad + StartInterval(30min) 反复问「今天这条链做完了吗」，
      没做完就按依赖顺序补；成功过的步骤当天不再重跑。
      不管几点开机，最多半小时内自己补上；断网/超时也会自动重试。

判定一律看真实状态，不看自己的记录：
    ingest    → 四张事实表 max(report_date) 是否到 D-1（物理覆盖）
    engine    → engine_run 今天有 status='ok'
    diagnose  → memorial 今天有 agent_state='ok'
    attribute → agent_run(job='attribute') 今天有 ok
    converge  → converge_run 今天有 ok
    chain_run 只记「试过几次、错在哪、耗时多少」，不当完成凭据。

用法：
    python3 adsdesk/chain.py                 # 正常跑（只在 09:00~23:00 内动手）
    python3 adsdesk/chain.py --force         # 忽略时间窗，立刻跑
    python3 adsdesk/chain.py --status        # 只看状态，不动手
    python3 adsdesk/chain.py --only ingest engine

关于 midship（2026-11-24 补）：
    `midship` 已接进本文件（MAX_ATTEMPTS / TIMEOUTS / build_cmd / step_done 都有
    它的分支），可以用 `--only midship` 或 `--only ingest ... converge midship`
    显式排进来，但它**不在默认七步里**。原因是它的输入是事件账本 events.db，
    账本由各 agent 的 hooks 在运行时投递；整条链跑完之前当天账本还没闭合，
    先投影等于把半截事件推给 gbrain。而且 midship 失败会让整链 blocked，
    代价远大于晚一天投影。想每天都跑，就在调度器里再挂一个「链跑完后」的
    独立 job，别塞进这条顺序链。
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = os.environ.get("ADS_DB") or str(ROOT / "ingest" / "data" / "ads.sqlite3")
PY = sys.executable
LOCK_PATH = "/tmp/ads-chain.lock"

# 时间窗：开机后到睡前。窗口外不动手（半夜机器自己醒来不该花 LLM 钱）
WINDOW_START_HOUR = 9
WINDOW_END_HOUR = 23
# 每步每天最多试几次（失败才计数；ok 之后当天不再跑）
# midship 记在这里，才能用 `--only midship` 手动触发（argparse 的 choices 就是
# 这份字典）。它默认不在日更链里，原因见 main() 里 steps 那段注释。
MAX_ATTEMPTS = {"ingest": 8, "market": 3, "rank": 4, "engine": 4, "diagnose": 2,
                "attribute": 2, "converge": 2, "midship": 2}
# 每步超时（秒）：ingest 有 60s/次的接口节流，补 7 天要上百次调用
# market 是 sorftime 逐词 keyword_detail（默认 30 个词）+ 竞品快照，给 20 分钟够用
TIMEOUTS = {"ingest": 180 * 60, "market": 20 * 60, "rank": 15 * 60, "engine": 30 * 60,
            "diagnose": 30 * 60, "attribute": 20 * 60, "converge": 20 * 60,
            "midship": 10 * 60}
# R31 加价的闸是 rank_snapshot 观测不超过 6 小时（engine/facts.py 的
# RANK_SNAPSHOT_MAX_AGE_HOURS）。这里不重复那个判定 —— 数据老不老由引擎的闸
# 负责（它会正确地停手），采集步只负责「今天问过了」。两处都判会让守夜人
# 因为「源端没给新数据」而永远红着。
RANK_FRESH_HOURS = 4   # 仅用于诊断输出提示，不参与 step_done
FACT_TABLES = ("fact_campaign_daily", "fact_keyword_daily",
               "fact_search_term_daily", "fact_placement_daily")
INGEST_LOOKBACK_MAX = 30   # 最多往前补 30 天，防止一次拉爆接口


# ---------------------------------------------------------------- 基础设施

def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        create table if not exists chain_run (
            run_id       integer primary key autoincrement,
            business_day text not null,
            step         text not null,
            attempt      integer not null,
            status       text not null,        -- ok | failed | skipped
            exit_code    integer,
            started_at   text not null,
            finished_at  text,
            detail       text
        );
        create index if not exists idx_chain_day on chain_run(business_day, step);
    """)
    conn.commit()


def say(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def acquire_lock():
    """同一时刻只允许一条链在跑；拿不到锁直接退出（launchd 30 分钟后再问）。"""
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("另一条链正在跑，本次退出（不排队）")
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# ---------------------------------------------------------------- 真实状态

def coverage(conn: sqlite3.Connection) -> dict[str, str | None]:
    out = {}
    for t in FACT_TABLES:
        row = conn.execute(f"select max(report_date) from {t}").fetchone()
        out[t] = row[0] if row else None
    return out


def _iso_week(day: str) -> str:
    """2026-11-19 → 2026-W47。与 ingest/market.py 写库时的周键同一口径。"""
    d = date.fromisoformat(day)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def market_datasets(conn: sqlite3.Connection, today: str) -> list[str]:
    """今天该采哪些外部市场数据集。

    竞品动作与品类行情每天都要一份新的：动作是「跟上一份快照比」出来的，
    断一天就比不出那一天发生了什么。ABA 不一样，它是周数据 —— 本周已经采到
    就不该每天再花 30 次额度把同样 30 个词重采一遍（一次 keyword_detail 一个额度）。
    """
    want = ["competitor", "market"]
    row = conn.execute("select max(snapshot_week) w from aba_weekly").fetchone()
    have = (row["w"] if row and row["w"] else "") or ""
    if have < _iso_week(today):
        want.insert(0, "aba")
    return want


def _market_covered(conn: sqlite3.Connection, today: str) -> set[str]:
    """今天已经成功采过的数据集。run_log.dataset 形如 market.aba+competitor+market。"""
    got: set[str] = set()
    for r in conn.execute(
            "select dataset from run_log where status='ok' and dataset like 'market.%' "
            "and substr(coalesce(finished_at, started_at),1,10)=?", (today,)):
        for d in str(r["dataset"]).split(".", 1)[1].split("+"):
            if d:
                got.add(d)
    return got


def freshness(conn: sqlite3.Connection, today: str) -> str:
    """今天数据变新的时刻＝最后一次 ingest 成功的时间。

    下游四步必须跑在这之后，否则就是拿旧数据出建议。
    2026-11-19 实测：engine 在凌晨 01:22 拿「止于 11-17」的数据跑了 4 次，
    白天 ingest 把数据补到 11-18 之后，那 4 次不能算数。
    """
    row = conn.execute(
        "select max(finished_at) f from chain_run where business_day=? and step='ingest' and status='ok'",
        (today,)).fetchone()
    return (row["f"] if row and row["f"] else f"{today}T00:00:00")


def step_done(conn: sqlite3.Connection, step: str, today: str) -> tuple[bool, str]:
    """今天这一步是否已经成功（且用的是最新的数据）。返回 (完成?, 依据)。"""
    if step == "ingest":
        cov = coverage(conn)
        target = (date.today() - timedelta(days=1)).isoformat()
        behind = {t: v for t, v in cov.items() if (v or "") < target}
        if behind:
            return False, "；".join(f"{t.replace('fact_', '').replace('_daily', '')} 止于 {v or '空'}"
                                   for t, v in behind.items()) + f"（目标 {target}）"
        return True, f"四张事实表均已到 {target}"
    if step == "market":
        want = market_datasets(conn, today)
        got = _market_covered(conn, today)
        missing = [d for d in want if d not in got]
        if missing:
            return False, (f"今天还差 {'、'.join(missing)}"
                           f"（已采 {'、'.join(sorted(got)) or '无'}）")
        return True, f"今天已采 {'、'.join(sorted(want))}"
    if step == "rank":
        # 判定的是「今天有没有成功轮询过」，不是「源端有没有给新观测」。
        # 这里踩过一次坑（2026-11-23 18:29）：一开始按「观测距今 ≤ 4h」判，
        # 结果 rank 步永远 failed —— 因为 sorftime 只按它自己的采样时刻出数据，
        # 我们重跑十次也拿不到一条更新的观测。数据老不老该由 R31 的
        # RANK_SNAPSHOT_MAX_AGE_HOURS 闸去管（它读到旧数据就停手，这是对的），
        # 采集步只需要证明「我今天问过了」，否则守夜人会一直红着重试。
        row = conn.execute(
            "select count(*) c from run_log where status='ok' and dataset like 'rank.%' "
            "and substr(coalesce(finished_at, started_at),1,10)=?", (today,)).fetchone()
        if not row["c"]:
            return False, "今天还没有成功的位次采集"
        obs = conn.execute(
            "select max(observed_at) m, count(*) c from rank_snapshot "
            "where is_self=1 and placement='sponsored'").fetchone()
        last = _ts(obs["m"])
        age = f"，最新观测 {obs['m']}（{(datetime.now() - last).total_seconds() / 3600.0:.1f}h 前）" if last else ""
        return True, f"今天已轮询 {row['c']} 次{age}"

    if step == "midship":
        # 判定的是「今天有没有成功投影过」，读 run_log（真实状态），不读 chain_run。
        # 踩过的坑（2026-11-24 18:00）：一开始读 chain_run，但 run_step 是
        # 「跑完 → 复验 → 才 record」，所以第一次成功也必判 failed —— 链会一直
        # 重试到 MAX_ATTEMPTS 然后报红。现在由 midship 自己在退出前写 run_log
        # （dataset='midship.project'），和 rank/market 一个路子。
        row = conn.execute(
            "select count(*) c, max(substr(coalesce(finished_at, started_at),12,8)) t "
            "from run_log where status='ok' and dataset like 'midship%' "
            "and substr(coalesce(finished_at, started_at),1,10)=?", (today,)).fetchone()
        if not row["c"]:
            return False, "今天还没有成功的事件中台投影"
        return True, f"今天已投影 {row['c']} 次（最近 {row['t'] or '—'}）"

    fresh = freshness(conn, today)
    if step == "engine":
        row = conn.execute(
            "select run_id, window_end from engine_run where status='ok' "
            "and substr(started_at,1,10)=? order by run_id desc limit 1", (today,)).fetchone()
        if not row:
            return False, "今天还没有成功的 engine_run"
        if _started_after(conn, "engine_run", "started_at", today, fresh):
            return True, f"engine_run #{row['run_id']}（窗口末 {row['window_end']}，晚于取数 {fresh[11:16]}）"
        return False, f"engine_run #{row['run_id']} 早于今天取数完成时刻 {fresh[11:16]}，数据已变新需重跑"
    if step == "diagnose":
        row = conn.execute(
            "select run_id, business_day from memorial where agent_state='ok' "
            "and substr(created_at,1,10)=? order by run_id desc limit 1", (today,)).fetchone()
        if not row:
            return False, "今天还没有 ok 的诊断"
        if _started_after(conn, "memorial", "created_at", today, fresh):
            return True, f"memorial #{row['run_id']}（窗口末 {row['business_day']}，晚于取数 {fresh[11:16]}）"
        return False, f"memorial #{row['run_id']} 早于今天取数完成时刻 {fresh[11:16]}，数据已变新需重诊断"
    if step == "attribute":
        row = conn.execute(
            "select agent_run_id from agent_run where job='attribute' and status='ok' "
            "and substr(started_at,1,10)=? order by agent_run_id desc limit 1", (today,)).fetchone()
        if not row:
            return False, "今天还没有成功的归因"
        if _started_after(conn, "agent_run", "started_at", today, fresh, "job='attribute' and "):
            return True, f"agent_run #{row['agent_run_id']}（晚于取数 {fresh[11:16]}）"
        return False, f"agent_run #{row['agent_run_id']} 早于今天取数完成时刻 {fresh[11:16]}，需重跑"
    if step == "converge":
        row = conn.execute(
            "select converge_run_id, n_due, n_filled, n_deferred from converge_run where status='ok' "
            "and substr(started_at,1,10)=? order by converge_run_id desc limit 1", (today,)).fetchone()
        if not row:
            return False, "今天还没有成功的 converge_run"
        if _started_after(conn, "converge_run", "started_at", today, fresh):
            return True, (f"converge_run #{row['converge_run_id']}"
                          f"（到期 {row['n_due']} / 回填 {row['n_filled']} / 暂不判 {row['n_deferred']}，"
                          f"晚于取数 {fresh[11:16]}）")
        return False, f"converge_run #{row['converge_run_id']} 早于今天取数完成时刻 {fresh[11:16]}，需重跑"
    raise KeyError(step)


def _ts(value: str | None) -> datetime | None:
    """把库里两种时间写法统一成「本地 naive datetime」。

    坑：engine_run/memorial 存的是 `2026-11-19 14:54:12`（空格），chain_run 存 ISO
    `2026-11-19T14:53:40+08:00`。直接比字符串会得出「刚跑完的这次比取数还早」——
    2026-11-19 实测就踩了：engine #104 明明在 14:54 跑完，被判成「早于 14:53」。
    """
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone().replace(tzinfo=None)


def _started_after(conn, table: str, col: str, today: str, fresh: str, extra: str = "") -> bool:
    """该表今天最近一次成功是否晚于数据变新的时刻。"""
    st = "status='ok' and " if table != "memorial" else "agent_state='ok' and "
    sql = (f"select max({col}) m from {table} where {extra}{st}substr({col},1,10)=?")
    m, f = _ts(conn.execute(sql, (today,)).fetchone()["m"]), _ts(fresh)
    return bool(m and f and m >= f)


def attempts_today(conn: sqlite3.Connection, step: str, today: str) -> int:
    row = conn.execute(
        "select count(*) c from chain_run where business_day=? and step=? and status='failed'",
        (today, step)).fetchone()
    return int(row["c"])


def record(conn, today, step, attempt, status, code, started, detail) -> None:
    conn.execute(
        "insert into chain_run(business_day, step, attempt, status, exit_code,"
        " started_at, finished_at, detail) values(?,?,?,?,?,?,?,?)",
        (today, step, attempt, status, code, started, now_iso(), detail))
    conn.commit()


# ---------------------------------------------------------------- 执行

def net_ready(deadline_s: int = 300) -> bool:
    """等 DNS。机器刚唤醒时网络常常还没起来（11-19 08:47 就是这么拉的：Could not resolve host）。"""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        try:
            socket.getaddrinfo("open.gerpgo.com", 443)
            return True
        except OSError:
            say(f"网络未就绪，10 秒后重试（已等 {int(time.time() - t0)}s）")
            time.sleep(10)
    return False


def build_cmd(step: str, model: str) -> list[str]:
    if step == "ingest":
        return [PY, str(ROOT / "ingest" / "ingest.py"), "--market", "7"]
    if step == "market":
        # 数据集不写死：ABA 是周数据，本周采到了就不该每天再花 30 次额度重采
        return [PY, str(ROOT / "ingest" / "market.py")]
    if step == "rank":
        return [PY, str(ROOT / "ingest" / "rank.py"), "--dataset", "all", "--market", "7"]
    if step == "engine":
        return [PY, str(ROOT / "engine" / "run.py")]
    if step == "diagnose":
        return [PY, str(ROOT / "adsdesk" / "agents" / "diagnose.py")]
    if step == "attribute":
        return [PY, str(ROOT / "adsdesk" / "agents" / "attribute.py")]
    if step == "converge":
        return [PY, str(ROOT / "adsdesk" / "agents" / "converge.py"), "--model", model]
    if step == "midship":
        # 事件中台投影：把当天的事件账本推进 gbrain。--project 是投影模式
        # （--project-only 只投影不消费，留给人工排查用）。
        return [PY, "-m", "midship", "--project"]
    raise KeyError(step)


def ingest_args(conn: sqlite3.Connection) -> tuple[list[str], str]:
    """把 ingest 的起止日算成「缺的最早一天 → 昨天」，并把最近 3 天带上（报表会回填）。"""
    target = date.today() - timedelta(days=1)
    cov = coverage(conn)
    missing = []
    for t, v in cov.items():
        if not v:
            missing.append(target - timedelta(days=INGEST_LOOKBACK_MAX))
            continue
        d = date.fromisoformat(v)
        if d < target:
            missing.append(d + timedelta(days=1))
    start = min(missing) if missing else target
    start = min(start, target - timedelta(days=3))          # 滚动重拉最近 3 天
    start = max(start, target - timedelta(days=INGEST_LOOKBACK_MAX))
    return ["--start", start.isoformat(), "--end", target.isoformat()], f"{start}~{target}"


def run_step(conn, step: str, model: str, today: str, force: bool) -> str:
    """返回 ok | failed | skipped | blocked。"""
    done, why = step_done(conn, step, today)
    if done and not force:
        say(f"· {step:9s} 跳过 —— {why}")
        record(conn, today, step, attempts_today(conn, step, today) + 1, "skipped", 0, now_iso(), why)
        return "skipped"

    n = attempts_today(conn, step, today)
    if n >= MAX_ATTEMPTS[step]:
        say(f"· {step:9s} 今天已失败 {n} 次，达到上限，不再重试 —— {why}")
        return "failed"

    cmd = build_cmd(step, model)
    note = ""
    if step == "ingest":
        extra, window = ingest_args(conn)
        cmd += extra
        note = f"窗口 {window}"
    if step == "market":
        ds = market_datasets(conn, today)
        cmd += ["--dataset", ",".join(ds)]
        note = f"数据集 {'+'.join(ds)}"

    started = now_iso()
    attempt = n + 1
    say(f"▶ {step:9s} 第 {attempt} 次启动{('（' + note + '）') if note else ''}")
    env = dict(os.environ, ADS_DB=DB, PYTHONUNBUFFERED="1")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=TIMEOUTS[step],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, code = proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or "") + f"\n[chain] 超时 {TIMEOUTS[step]}s，已中止", 124
    dt = int(time.time() - t0)

    tail = "\n".join(out.strip().splitlines()[-12:])
    if tail:
        print(tail, flush=True)

    # 退出码不可尽信：ingest 在数据集全 FAIL 时也返回 0，所以再验一次真实状态
    done2, why2 = step_done(conn, step, today)
    ok = (code == 0) and done2
    status = "ok" if ok else "failed"
    detail = f"{why2}；耗时 {dt}s" + ("" if code == 0 else f"；exit={code}")
    record(conn, today, step, attempt, status, code, started, detail)
    say(f"{'✔' if ok else '✘'} {step:9s} {status} —— {detail}")
    if not ok and len(tail.splitlines()) > 0 and code != 0:
        say(f"  最后几行：{tail.splitlines()[-1][:160]}")
    return status


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="auto/deepseek-v4.1-flash-1017")
    ap.add_argument("--only", nargs="*", default=None, choices=list(MAX_ATTEMPTS))
    ap.add_argument("--force", action="store_true", help="忽略「今天已完成」和运行时间窗")
    ap.add_argument("--status", action="store_true", help="只打印状态，不动手")
    args = ap.parse_args(argv)

    conn = connect()
    ensure_schema(conn)
    today = date.today().isoformat()
    # rank 排在 market 之后、engine 之前：engine 的 R31 卡位规则要读 rank_snapshot，
    # 排在 engine 后面等于让引擎读昨天的位次。
    #
    # midship 是 opt-in：它的输入是事件账本（events.db），而账本由各 agent 的
    # hooks 在运行时投递；整条链跑完之前，当天的账本还没闭合，先投影只会把半截
    # 事件推给 gbrain，且失败会触发整链 blocked。需要时手动 `--only midship`
    # 或在收敛之后再接一段，别塞进这条顺序链里。
    steps = args.only or ["ingest", "market", "rank", "engine", "diagnose",
                          "attribute", "converge"]

    if args.status:
        print(f"库：{DB}\n今天：{today}\n")
        for s in steps:
            done, why = step_done(conn, s, today)
            fails = attempts_today(conn, s, today)
            print(f"  {s:9s} {'已完成' if done else '未完成':4s}  {why}"
                  + (f"  今天失败 {fails} 次" if fails else ""))
        last = conn.execute(
            "select business_day,step,status,substr(started_at,12,8) t,detail from chain_run "
            "order by run_id desc limit 8").fetchall()
        if last:
            print("\n  最近 8 条尝试：")
            for r in last:
                print(f"    {r['business_day']} {r['step']:9s} {r['status']:7s} {r['t']}  {(r['detail'] or '')[:80]}")
        return 0

    hour = datetime.now().hour
    if not args.force and not (WINDOW_START_HOUR <= hour < WINDOW_END_HOUR):
        say(f"当前 {hour} 点，不在 {WINDOW_START_HOUR}:00~{WINDOW_END_HOUR}:00 运行窗内，退出")
        return 0

    lock = acquire_lock()
    if lock is None:
        return 0

    say(f"日更链开始（库 {Path(DB).name}，业务日 D-1={(date.today() - timedelta(days=1)).isoformat()}）")
    if not net_ready():
        say("网络 5 分钟仍不可用，本轮放弃（30 分钟后 launchd 会再来）")
        record(conn, today, "ingest", attempts_today(conn, today, "ingest") + 1,
               "failed", 1, now_iso(), "DNS 5 分钟未就绪")
        return 1

    results: dict[str, str] = {}
    blocked_by: str | None = None
    for s in steps:
        if blocked_by:
            results[s] = "blocked"
            record(conn, today, s, attempts_today(conn, today, s) + 1, "skipped", 0, now_iso(),
                   f"上游 {blocked_by} 未完成，本轮不跑")
            say(f"· {s:9s} 跳过 —— 上游 {blocked_by} 未完成")
            continue
        st = run_step(conn, s, args.model, today, args.force)
        results[s] = st
        if st == "failed":
            blocked_by = s

    print("", flush=True)
    say("汇总：" + "、".join(f"{k}={v}" for k, v in results.items()))
    bad = [k for k, v in results.items() if v == "failed"]
    say("全部完成 ✔" if not bad else f"有步骤失败：{'、'.join(bad)}（30 分钟后自动重试）")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))