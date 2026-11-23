#!/usr/bin/env python3
"""四类沉淀通路的端到端验证：写侧（打钩 → 四张表）+ 读侧（覆盖 → 引擎换目标）。

为什么要有这个脚本：库里 param_override / observation_task / experiment 长期是 0 行，
policy 8 行全是平台系统约定，reason_hit 只有 1 行。光读代码分不清两种可能 ——
「通路写坏了」还是「从来没人打过钩」。这两件事的处置完全不同：前者要改代码，
后者只是没被触发。要分清，只能自己造一次真实的打钩，然后回查表。

做法（全程在库副本上，生产库只读，绝不直连写）：
  1. 用 sqlite3 的 backup API 复制生产库到 /tmp —— 不用 cp：生产库开着 WAL，
     直接拷主文件会丢掉 -wal 里还没 checkpoint 的行
  2. 从最新的 memorial 里取真实 option_meta（出题侧真的这么写进 pack 的，不手捏假数据；
     库里从来没有出过 experiment 题，那一路才退化成同形状构造，并在输出里标明）
  3. 四个 destination 各 apply_answer 一次 → 回查目标表 → 打印对照表
  4. 读侧：param_override 那条正好是 target_acos=0.8，直接再跑一次引擎，
     确认「[沉淀] 参数覆盖生效」日志和盈亏平衡 CVR 真的变了。
     这是整条闭环最关键的一环：主人打钩 → 下次运行真的换目标。这条不通，前面全白做。

为什么不写进 selftest.py：selftest 是给工作台自查用的（http 层），
这里是引擎与沉淀表的契约验证，两者失败时的排查路径完全不同，混在一起会互相掩盖。

退出码：0 = 四条全 PASS 且读侧生效；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent          # adsdesk/api
ROOT = HERE.parents[1]                          # 项目根
ENGINE = ROOT / "engine"
for _p in (str(ENGINE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reason_routes                              # noqa: E402  engine/reason_routes.py
import scope as scope_mod                         # noqa: E402  engine/scope.py

PROD_DB = ROOT / "ingest" / "data" / "ads.sqlite3"

# (destination, 期望表, 兜底原因码, 强制覆盖的字段)
# 兜底原因码只在 memorial 里找不到这个去向的真实 option_meta 时才用 —— 那是
# 「出题侧从来没问过这类题」的证据，本身也是要报告出去的事实。
CASES: tuple[tuple[str, str, str, dict], ...] = (
    ("policy", "policy", "goal_keep_acos30_control_loss", {}),
    ("param_override", "param_override", "goal_relax_acos",
     {"param_key": "target_acos", "param_value": 0.8, "ttl_days": 90}),
    ("observation_task", "observation_task", "goal_product_side_problem", {}),
    ("experiment", "experiment", "explore_run_experiment", {}),
)


# ----------------------------------------------------------------------
# 输出小工具：中文占两格宽，不按显示宽度补空格表格会歪
# ----------------------------------------------------------------------
def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _w(text))


def _line(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return "  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip()


# ----------------------------------------------------------------------
# 库副本
# ----------------------------------------------------------------------
def copy_db(src: Path, dst: Path) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def open_copy(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=5000")
    return conn


# ----------------------------------------------------------------------
# 取真实 option_meta
# ----------------------------------------------------------------------
def load_options(conn: sqlite3.Connection) -> tuple[dict[str, dict], int | None]:
    """从 memorial 里按 destination 各取第一个真实 option_meta。

    返回 ({destination: option}, 来源 run_id)。工作台的打钩就是这么进来的：
    approve.py 把 pack.questions[i].option_meta[j] 原样交给 apply_answer。
    用真实的那条，才能保证字段名不是我们自己想当然写的。
    """
    found: dict[str, dict] = {}
    rows = conn.execute(
        "select run_id, pack_json from memorial order by run_id desc"
    ).fetchall()
    for r in rows:
        try:
            pack = json.loads(r["pack_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for q in pack.get("questions") or []:
            for meta in q.get("option_meta") or []:
                if not isinstance(meta, dict):
                    continue
                dest = meta.get("destination")
                if dest and dest not in found:
                    found[dest] = dict(meta)
                    found[dest]["_run_id"] = r["run_id"]
        if all(dest in found for dest, *_ in CASES):
            break
    return found, found.get("policy", {}).get("_run_id")


def fallback_option(dest: str, code: str, object_id: str) -> dict:
    """库里没有这个去向的出题记录时，按 engine/report.py 的同形状构造一条。

    experiment 就是这种情况：Q05 只在 below 池里挑到带对照组的候选才出题，
    而真实数据里一条都没挑到过 —— 所以「experiment 0 行」不等于「写坏了」。
    """
    if dest == "experiment":
        return {
            "reason_code": code,
            "destination": "experiment",
            "scope": "object",
            "object_id": object_id,
            "metric": "roas,cpa",
            "ttl_days": 14,
            # change/control 的形状照抄 report._explore_candidate 的产出
            "change": {"campaign_id": "0", "entity_type": "keyword", "entity_id": "验-构造",
                       "field": "bid", "from": 0.40, "to": 0.36, "delta_pct": -10.0,
                       "note": "构造样本：验证 experiment 这一路能落库"},
            "control": {"campaign_id": "0", "keyword_text": "验-对照",
                        "label": "验-对照", "rationale": "同活动点击量最接近，本次不动它",
                        "clicks": 300, "orders": 2},
            "option_text": "构造：开实验验证 experiment 落库",
            "_constructed": True,
        }
    return {
        "reason_code": code,
        "destination": dest,
        "scope": "object",
        "object_id": object_id,
        "option_text": f"构造：验证 {dest} 落库",
        "_constructed": True,
    }


# ----------------------------------------------------------------------
# 回查：写进去了没有、字段对不对
# ----------------------------------------------------------------------
def check_policy(conn, receipt, object_id, code, today) -> tuple[bool, str]:
    row = conn.execute("select * from policy where policy_id=?", (receipt.get("id"),)).fetchone()
    if row is None:
        return False, "回查不到这一行"
    params = json.loads(row["params_json"] or "{}")
    ok = (
        row["object_id"] == object_id
        and row["status"] == "active"
        and (row["expires_at"] or "") >= today
        and params.get("reason_code") == code
    )
    detail = (f"id={row['policy_id']} object={row['object_id']} "
              f"type={row['policy_type']} status={row['status']} "
              f"至 {row['expires_at']} params.reason_code={params.get('reason_code')}")
    return ok, detail


def check_param_override(conn, receipt, object_id, code, today) -> tuple[bool, str]:
    row = conn.execute("select * from param_override where override_id=?",
                       (receipt.get("id"),)).fetchone()
    if row is None:
        return False, "回查不到这一行"
    ok = (
        row["object_id"] == object_id
        and row["param_key"] == "target_acos"
        and str(row["param_value"]) == "0.8"
        and (row["expires_at"] or "") >= today
    )
    detail = (f"id={row['override_id']} object={row['object_id']} "
              f"{row['param_key']}={row['param_value']} 至 {row['expires_at']} "
              f"源={row['source']}")
    return ok, detail


def check_observation_task(conn, receipt, object_id, code, today) -> tuple[bool, str]:
    row = conn.execute("select * from observation_task where task_id=?",
                       (receipt.get("id"),)).fetchone()
    if row is None:
        return False, "回查不到这一行"
    ok = (
        row["object_id"] == object_id
        and row["reason_code"] == code
        and row["result"] is None
        and (row["due_at"] or "") >= today
        and bool(row["metric"])
    )
    detail = (f"id={row['task_id']} object={row['object_id']} code={row['reason_code']} "
              f"metric={row['metric']} 到期 {row['due_at']} result={row['result']}")
    return ok, detail


def check_experiment(conn, receipt, object_id, code, today) -> tuple[bool, str]:
    row = conn.execute("select * from experiment where experiment_id=?",
                       (receipt.get("id"),)).fetchone()
    if row is None:
        return False, "回查不到这一行"
    try:
        change = json.loads(row["change_json"] or "{}")
        control = json.loads(row["control_json"] or "{}")
    except json.JSONDecodeError:
        change = control = {}
    ok = (
        row["object_id"] == object_id
        and row["reason_code"] == code
        and bool(change)
        and bool(control)
        and row["result"] is None
        and (row["ends_at"] or "") >= today
    )
    detail = (f"id={row['experiment_id']} object={row['object_id']} code={row['reason_code']} "
              f"change.{next(iter(change), '-')}/{len(change)} 键 "
              f"control={control.get('label') or control.get('keyword_text')} 至 {row['ends_at']}")
    return ok, detail


CHECKERS = {
    "policy": check_policy,
    "param_override": check_param_override,
    "observation_task": check_observation_task,
    "experiment": check_experiment,
}
TABLES = {
    "policy": ("policy", "policy_id"),
    "param_override": ("param_override", "override_id"),
    "observation_task": ("observation_task", "task_id"),
    "experiment": ("experiment", "experiment_id"),
}


def count_rows(conn, table: str) -> int:
    return conn.execute(f"select count(*) from {table}").fetchone()[0]


# ----------------------------------------------------------------------
# 读侧：param_override 生效 → 引擎真的换目标
# ----------------------------------------------------------------------
def check_read_side(db_path: Path, object_id: str) -> tuple[bool, list[str]]:
    """读侧闭环：引擎读到 target_acos=0.80，并且盈亏平衡 CVR 跟着变。

    只查表说「有一行 param_override」是不够的 —— 那可能是一行没人读的废数据。
    这里真的把引擎跑一遍（--dry-run，不写库、不出 memorial），看它自己的日志。
    """
    out: list[str] = []
    ok = True

    conn = open_copy(db_path)
    try:
        ov = reason_routes.overrides(conn, object_id)
        hit = ov.get("target_acos")
        out.append(f"reason_routes.overrides() → {{'target_acos': {hit!r}}}")
        if hit != "0.8":
            ok = False
            out.append("  FAIL：读侧拿不到 target_acos=0.8")

        import facts as facts_mod                  # 延迟导入：写侧用例不依赖事实层
        be_default = (facts_mod.build_facts(conn).breakeven or {}).get("breakeven_cvr")
        be_over = (facts_mod.build_facts(conn, target_acos=0.8).breakeven or {}).get("breakeven_cvr")
        out.append(f"盈亏平衡 CVR：目标 30% → {be_default:.4%}；目标 80% → {be_over:.4%}")
        if be_default is None or be_over is None or abs(be_over - be_default) < 1e-9:
            ok = False
            out.append("  FAIL：目标 ACOS 变了但盈亏平衡 CVR 没变，目标没进到事实层")
    finally:
        conn.close()

    import run as engine_run                       # 延迟导入：只有这一段要跑整套引擎

    tmpdir = db_path.parent
    engine_run.REPORTS = str(tmpdir / "reports")   # 别把仓库里的奏折覆盖掉
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = engine_run.main(["--db", str(db_path), "--dry-run", "--no-snapshot"])
    log = buf.getvalue()

    want = "[沉淀] 参数覆盖生效：目标 ACOS 30.00% → 80.00%"
    got = next((ln for ln in log.splitlines() if ln.startswith("[沉淀] 参数覆盖生效")), "")
    out.append(f"引擎 exit={rc}")
    out.append(f"引擎日志：{got or '（没有这一行）'}")
    fact_line = next((ln for ln in log.splitlines() if "盈亏平衡 CVR" in ln), "")
    if fact_line:
        out.append(f"引擎日志：{fact_line}")
    if rc != 0 or want not in log:
        ok = False
        out.append("  FAIL：引擎没有按覆盖值重算（或退出码非 0）")
    return ok, out


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="验证四类沉淀通路（写侧 + 读侧）")
    ap.add_argument("--db", default=str(PROD_DB), help="源库（只读，会被复制）")
    ap.add_argument("--keep", action="store_true", help="保留 /tmp 副本便于排查")
    args = ap.parse_args(argv)

    src = Path(args.db).resolve()
    if not src.exists():
        print(f"库不存在：{src}")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-sediment-"))
    db_path = tmpdir / "sediment.sqlite3"
    copy_db(src, db_path)
    print(f"源库（只读）：{src}")
    print(f"副本        ：{db_path}")
    print()

    conn = open_copy(db_path)
    today = date.today().isoformat()
    object_id = scope_mod.SCOPE_ASIN
    failures = 0
    try:
        options, src_run = load_options(conn)
        print(f"option_meta 来源：memorial run #{src_run}"
              f"，命中 {sorted(options)}")
        for dest, *_ in CASES:
            if dest not in options:
                print(f"  注意：memorial 里没有任何 destination={dest} 的 option_meta"
                      f"（出题侧从未问过这类题），这一路用同形状构造")
        print()

        widths = (20, 16, 12, 46, 6)
        print(_line(("destination", "期望表", "实际行数", "关键字段", "结果"), widths))
        print(_line(("-" * 18, "-" * 14, "-" * 10, "-" * 44, "-" * 5), widths))

        for dest, table, code_default, overrides in CASES:
            meta = options.get(dest) or fallback_option(dest, code_default, object_id)
            option = dict(meta)
            option.update(overrides)          # 任务指定的取值覆盖真实 meta 里的默认值
            code = option.get("reason_code") or code_default
            before = count_rows(conn, table)
            try:
                receipt = reason_routes.apply_answer(
                    conn, code=code, object_id=object_id, option=option,
                    free_text=None, question_id="verify", run_id=0,
                )
                conn.commit()
            except Exception as e:                      # noqa: BLE001 这条挂了也要继续验下一条
                failures += 1
                print(_line((dest, table, f"{before}", f"apply_answer 抛异常：{e}", "FAIL"),
                            widths))
                continue

            actual_table = receipt.get("table")
            after = count_rows(conn, table)
            if actual_table != table:
                failures += 1
                print(_line((dest, table, f"{before}→{after}",
                             f"回执落到 {actual_table}：{receipt}", "FAIL"), widths))
                continue

            ok, detail = CHECKERS[dest](conn, receipt, object_id, code, today)
            if not ok:
                failures += 1
            rows = f"{before}→{after}"
            print(_line((dest, table, rows, detail, "PASS" if ok else "FAIL"), widths))

        print()
        # 出题去重靠 active_codes：沉淀过的码在有效期内不再问第二遍。
        # 这条不通的话，人会每天被同一个问题追着问，沉淀就白沉了。
        active = reason_routes.active_codes(conn, object_id)
        codes = [dict(options.get(d) or fallback_option(d, c, object_id)).get("reason_code") or c
                 for d, _, c, _ in CASES]
        routed = {c: active.get(c, {}).get("table") for c in codes}
        print(f"读侧·出题去重 active_codes：{routed}")
        if any(v is None for v in routed.values()):
            failures += 1
            print("  FAIL：有沉淀码没被 active_codes 认出来，同一个问题会被重复问")
        print()

        print("读侧·参数覆盖生效（引擎重跑）：")
        read_ok, lines = check_read_side(db_path, object_id)
        for ln in lines:
            print(f"  {ln}")
        if not read_ok:
            failures += 1
        print()

        print("=" * 78)
        if failures:
            print(f"结论：{failures} 项 FAIL —— 沉淀通路有断点，见上面逐条")
        else:
            print("结论：四条 destination 全部落库、字段对得上，读侧参数覆盖已生效")
        print("=" * 78)
    finally:
        conn.close()
        if not args.keep:
            for p in sorted(tmpdir.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            tmpdir.rmdir()
        else:
            print(f"副本保留在：{db_path}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())