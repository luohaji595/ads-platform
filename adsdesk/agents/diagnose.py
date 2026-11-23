"""诊断 agent（07:05）。

影子模式（第一周）：只写 agent_run / agent_objection / agent_eval，
**不改 memorial.pack_json 的 questions**。奏折与工作台照旧显示规则引擎出的题。
一周后看 agent_eval 的兑现率再决定要不要真接管。

铁律：agent 不产生建议。它输出里一旦出现动作词，那条就被丢掉。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import context, gateway

CST = ZoneInfo("Asia/Shanghai")
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "diagnose_v1.md"
PROMPT_VER = hashlib.sha1(PROMPT_PATH.read_bytes()).hexdigest()[:8]

MAX_QUESTIONS = 5
OPTION_MAX_CHARS = 30
LAST_OPTION = "其他（请补一句依据）"
BANNED_ACTION_WORDS = (
    "raise_bid", "reduce_bid", "raise_budget", "lower_budget", "add_negative",
    "pause", "enable", "disable", "停用", "启用", "加价", "降价", "加预算", "降预算", "否词",
)
PREDICT_FIELDS = ("claim", "verifiable_at", "method", "expected")

# 单模型硬截止：300s。依据是实测 —— 成功那次（deepseek、32000 预算）用了 123 秒、
# 20734 tokens；300s 是 2.4 倍余量。两个模型各 300s，最坏 10 分钟见结果，
# 而不是像 2026-11-19 那样卡到 105 分钟才写一条 failed。
DIAGNOSE_TIMEOUT = 300
# 预算 64000 会让模型倾向长思考（思维链算在这个额度里）。实测 completion 落在
# 18085~33966（2026-11-19 那次 33966，几乎贴着 36000 的上限），48000 才留得住余量。
# 真被截断也不致命了：链路会自动换下一个模型，而不是当天直接失败。
DIAGNOSE_MAX_TOKENS = 48000


# ------------------------------------------------------------------ 校验

def validate(out: dict, inp: dict) -> tuple[dict, list[str]]:
    """把不合格的条目丢掉并记录原因。**只丢条目，不丢整份输出**。"""
    warns: list[str] = []
    window_end = (inp.get("window") or {}).get("end") or ""
    valid_ids = {d.get("decision_id") for d in (inp.get("decisions") or []) if d.get("decision_id")}

    qs_out = []
    for q in (out.get("questions") or [])[:MAX_QUESTIONS]:
        opts = q.get("options") or []
        codes = q.get("reason_codes") or []
        qid = str(q.get("id") or "")
        if len(opts) != 4:
            warns.append(f"q {qid}: options 不是 4 个（{len(opts)}），丢掉")
            continue
        if opts[-1].strip() != LAST_OPTION:
            warns.append(f"q {qid}: 最后一个选项不是「{LAST_OPTION}」，丢掉")
            continue
        bad = [o for o in opts if len(str(o)) > OPTION_MAX_CHARS or "?" in str(o) or "？" in str(o)]
        if bad:
            warns.append(f"q {qid}: 选项不合规（超 {OPTION_MAX_CHARS} 字或含问号）：{bad}，丢掉")
            continue
        if len(codes) != 4:
            warns.append(f"q {qid}: reason_codes 不是 4 个，丢掉")
            continue
        preds, pw = _clean_predicts(q.get("predicts"), window_end, f"q {qid}")
        warns += pw
        if not preds:
            warns.append(f"q {qid}: 一条合格 predicts 都没有，丢掉（问题必须能验真假）")
            continue
        qs_out.append({
            "id": qid, "object": q.get("object"), "ask": q.get("ask"),
            "options": [str(o) for o in opts],
            "reason_codes": [str(c) for c in codes],
            "impact": _num(q.get("impact")),
            "uncertainty": _num(q.get("uncertainty")),
            "predicts": preds,
        })

    obj_out = []
    for o in out.get("objections") or []:
        did = o.get("decision_id")
        try:
            did = int(did)
        except (TypeError, ValueError):
            warns.append(f"objection: decision_id 不是整数（{o.get('decision_id')}），丢掉")
            continue
        if did not in valid_ids:
            warns.append(f"objection #{did}: 不在输入 decisions 里，丢掉（不许凭空造建议）")
            continue
        verdict = str(o.get("verdict") or "").lower()
        if verdict not in ("oppose", "doubt"):
            warns.append(f"objection #{did}: verdict 只能是 oppose/doubt（给了 {verdict}），丢掉")
            continue
        why = str(o.get("why") or "").strip()
        if not why:
            warns.append(f"objection #{did}: why 是空的，丢掉")
            continue
        if any(w in why for w in BANNED_ACTION_WORDS):
            warns.append(f"objection #{did}: why 里出现动作词，丢掉（agent 不提案）")
            continue
        preds, pw = _clean_predicts(o.get("predicts"), window_end, f"objection #{did}")
        warns += pw
        obj_out.append({
            "decision_id": did, "verdict": verdict, "why": why,
            "evidence": str(o.get("evidence") or ""),
            "predicts": preds,
        })

    return {"questions": qs_out, "objections": obj_out}, warns


def _clean_predicts(preds, window_end: str, tag: str) -> tuple[list[dict], list[str]]:
    warns, keep = [], []
    for p in preds or []:
        if not isinstance(p, dict):
            continue
        if any(not str(p.get(k) or "").strip() for k in PREDICT_FIELDS):
            warns.append(f"{tag}: predicts 缺字段 {[k for k in PREDICT_FIELDS if not p.get(k)]}，丢掉这条断言")
            continue
        va = str(p["verifiable_at"])[:10]
        if window_end and va <= window_end:
            warns.append(f"{tag}: verifiable_at {va} 不在 {window_end} 之后，丢掉（不能验过去）")
            continue
        keep.append({k: str(p[k]) for k in PREDICT_FIELDS})
    return keep, warns


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 落库

STALE_RUN_MINUTES = 30


def _ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS agent_run (
        agent_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job TEXT NOT NULL, run_id INTEGER, model TEXT NOT NULL,
        prompt_ver TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running', tokens_in INTEGER, tokens_out INTEGER,
        input_digest TEXT, output_json TEXT, error TEXT);
    """)
    _ensure_objection_columns(conn)
    conn.commit()
    _reap_stale(conn)


def _ensure_objection_columns(conn):
    """补 agent_objection 的新列 —— 写库方必须能自给自足。

    为什么在 migrate.py 之外还要在这里补一遍：migrate.py 是权威迁移入口，
    但它是一次性运维动作；而 diagnose 每天跑，写的是 9 列的 INSERT。
    真库只要漏跑一次迁移，这条 INSERT 就会以
    `table agent_objection has no column named predicts_json` 整条挂掉 ——
    诊断 agent 直接停摆，而且是「昨天还好好的」那种最难查的故障。
    写库方自己保证列存在，代价是 6 行 PRAGMA，换来的是不会被迁移流程的状态拖死。

    这里只补列、不建索引：建索引在大表上要拿写锁，那是运维的活，
    留给 migrate.py 的 POST_INDEXES 去做。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_objection)")}
    if not cols:
        return  # 表不存在（空库），交给 migrate.py 建整张表
    for name, decl in (
        ("predicts_json", "TEXT"),
        ("verifiable_at", "TEXT"),
        ("checked_at", "TEXT"),
        ("outcome", "TEXT NOT NULL DEFAULT 'pending'"),
        ("actual", "TEXT"),
        ("judged_by", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE agent_objection ADD COLUMN {name} {decl}")


def _reap_stale(conn, minutes: int = STALE_RUN_MINUTES) -> int:
    """把卡在 running 的历史记录收掉。

    为什么需要这个：进程被 kill -9、或机器休眠时进程被冻结、或 launchd 直接
    杀进程，都不会走 except 分支，`agent_run` 就永远停在 running。工作台读到
    一条 20 小时前 started 的 running，会显示成「诊断跑着呢」，而它其实早死了。

    判据只看墙钟，不看进程 —— 因为记录可能是别的机器/别的进程写的。代价是
    真跑超过 30 分钟的正常调用会被误标；但那已经是单模型硬截止（300s）的 6 倍、
    整条链路（两个模型各 300s）的 3 倍，属于该失败的情况。
    """
    cutoff = (datetime.now(CST) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    cur = conn.execute(
        """update agent_run
              set status='failed',
                  finished_at=?,
                  error=coalesce(error, ?)
            where status='running' and started_at < ?""",
        (datetime.now(CST).isoformat(timespec="seconds"),
         f"stale: 超过 {minutes} 分钟仍未结束，判定为进程已死（可能是 kill 或休眠冻结）",
         cutoff),
    )
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


def _model_chain(preferred: str | None) -> list[str]:
    """把 --model 当「首选」插到链路最前面，而不是当成唯一模型。

    2026-11-19 的坑：plist 里写死 `--model deepseek`，代码又只传这一个模型，
    结果那天它在网关排队，整个诊断就废了 —— 明明另一个模型能答。
    现在 --model 只决定先试谁，剩下的照旧兜底。
    """
    chain = list(gateway.DIAGNOSE_CHAIN)
    if preferred:
        if preferred in chain:
            chain.remove(preferred)
        chain.insert(0, preferred)
    return chain


def run_once(conn, *, run_id: int | None = None, model: str | None = None,
             shadow: bool = True, dry_run: bool = False) -> dict:
    _ensure_schema(conn)
    inp = context.build_input(conn, run_id)
    dg = context.digest(inp)
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "今天的输入：\n" + json.dumps(inp, ensure_ascii=False, indent=2)},
    ]

    started = datetime.now(CST).isoformat(timespec="seconds")
    chain = _model_chain(model)
    cur = conn.execute(
        """insert into agent_run (job, run_id, model, prompt_ver, started_at, status, input_digest)
           values ('diagnose', ?, ?, ?, ?, 'running', ?)""",
        (inp["run_id"], chain[0], PROMPT_VER, started, dg),
    )
    agent_run_id = cur.lastrowid
    conn.commit()

    try:
        r = gateway.chat_with_fallback(
            messages,
            models=chain,
            timeout=DIAGNOSE_TIMEOUT,
            max_tokens=DIAGNOSE_MAX_TOKENS,
        )
    except gateway.GatewayError as e:
        conn.execute(
            "update agent_run set status='failed', finished_at=?, error=? where agent_run_id=?",
            (datetime.now(CST).isoformat(timespec="seconds"), str(e), agent_run_id),
        )
        _set_agent_state(conn, inp["run_id"], "failed")
        conn.commit()
        # 奏折里也要说清「今天没跑成」，否则读的人只会看到一块空白，
        # 以为 agent 没意见 —— 那是两种完全不同的状态。
        _patch_report_md(conn, inp["run_id"], None, "failed", err=str(e)[:200])
        return {"ok": False, "agent_run_id": agent_run_id, "error": str(e), "warnings": []}

    clean, warns = validate(r["parsed"], inp)
    finished = datetime.now(CST).isoformat(timespec="seconds")

    if dry_run:
        conn.execute("delete from agent_run where agent_run_id=?", (agent_run_id,))
        conn.commit()
        return {"ok": True, "dry_run": True, "clean": clean, "warnings": warns,
                "tokens": (r["tokens_in"], r["tokens_out"]), "raw": r["parsed"]}

    # model 用 r["model"] 而不是预期的链首：降级时账本要记真正答话的那个，
    # 否则回头查「那天为什么结论变了」会看到没参与过的模型名。
    conn.execute(
        """update agent_run set status='ok', finished_at=?, tokens_in=?, tokens_out=?,
               output_json=?, model=? where agent_run_id=?""",
        (finished, r["tokens_in"], r["tokens_out"],
         json.dumps(clean, ensure_ascii=False), r["model"], agent_run_id),
    )

    now = datetime.now(CST).isoformat(timespec="seconds")
    n_obj = 0
    for o in clean["objections"]:
        # 把 objection 自带的 predicts 一并落库 —— 这是整个「质疑可兑现」链条的起点。
        # 以前只存了 verdict/why/evidence，等于只留下了它的态度；态度没法判真假，
        # 于是 26 条质疑全部卡在 doubt 上，谁也不知道它到底对不对。
        # 原文（predicts_json）连同「最早到期日」一起存：
        #   - 原文要留着，因为判定规则以后会改，改完要能重判；
        #   - verifiable_at 取 min，因为一条反对只要有一条预测到期，
        #     这条反对就该进入收敛 agent 的待判队列（不能等到它最晚那条到期）。
        # 空数组或缺失 verifiable_at 时一律留 NULL —— 宁可这条永远 pending，
        # 也不能编一个日期出来，那等于自己给自己造一个「判过了」的假象。
        preds = o.get("predicts") or []
        preds_json = json.dumps(preds, ensure_ascii=False) if preds else None
        due = min(
            (str(p["verifiable_at"])[:10] for p in preds if p.get("verifiable_at")),
            default=None,
        )
        conn.execute(
            """insert into agent_objection
                   (agent_run_id, decision_id, verdict, why, evidence, created_at,
                    predicts_json, verifiable_at, outcome)
               values (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (agent_run_id, o["decision_id"], o["verdict"], o["why"], o["evidence"], now,
             preds_json, due),
        )
        n_obj += 1

    n_eval = 0
    for q in clean["questions"]:
        for p in q["predicts"]:
            conn.execute(
                """insert into agent_eval (agent_run_id, agent, claim, verifiable_at, method,
                       expected, verdict, created_at)
                   values (?, 'diagnose', ?, ?, ?, ?, 'pending', ?)""",
                (agent_run_id, p["claim"], p["verifiable_at"], p["method"], p["expected"], now),
            )
            n_eval += 1
    for o in clean["objections"]:
        for p in o["predicts"]:
            conn.execute(
                """insert into agent_eval (agent_run_id, agent, claim, verifiable_at, method,
                       expected, verdict, created_at)
                   values (?, 'diagnose', ?, ?, ?, ?, 'pending', ?)""",
                (agent_run_id, p["claim"], p["verifiable_at"], p["method"], p["expected"], now),
            )
            n_eval += 1

    _set_agent_state(conn, inp["run_id"], "ok")
    conn.commit()

    # 落库成功之后才动 md —— 顺序反过来会出现「奏折说有反对、库里查不到」，
    # 那种不一致比没有这一区更坏。
    patch = _patch_report_md(conn, inp["run_id"], clean["objections"], "ok")

    return {
        "ok": True, "agent_run_id": agent_run_id, "run_id": inp["run_id"],
        "questions": clean["questions"], "objections": clean["objections"],
        "warnings": warns, "tokens": (r["tokens_in"], r["tokens_out"]),
        "n_eval": n_eval, "n_obj": n_obj, "shadow": shadow, "model": r["model"],
        "fell_back": r.get("fell_back"),
        "digest": dg,
        "report_md": patch,
    }


def _report_write_guard(conn, md_path: str) -> str | None:
    """该不该往这个 md 里写。返回 None = 可以写，否则返回不写的理由。

    两条规则，任一不满足就不写：
      ① 正在用的库必须是生产库（ROOT/ingest/data/ads.sqlite3）。
         副本库里的 memorial 行说的是「生产那份奏折」，副本无权改它。
      ② md 路径必须落在项目目录内。
         防的是 report_path 被写成任意路径时，脚本顺着它去改项目外的文件。
    """
    prod_db = (Path(__file__).resolve().parents[2] / "ingest" / "data" / "ads.sqlite3")
    prod_root = Path(__file__).resolve().parents[2]
    try:
        cur = conn.execute("pragma database_list").fetchone()
        db_file = cur[2] if cur and len(cur) > 2 else None
    except Exception:  # noqa: BLE001
        db_file = None
    if not db_file:
        return "拿不到当前库路径，不敢改奏折"
    try:
        if Path(db_file).resolve() != prod_db.resolve():
            return f"当前库是 {db_file}，不是生产库，不改生产奏折"
        if not Path(md_path).resolve().is_relative_to(prod_root):
            return f"奏折路径 {md_path} 不在项目目录内"
    except Exception as e:  # noqa: BLE001
        return f"路径检查失败：{e}"
    return None


def _patch_report_md(conn, run_id: int | None, objs: list[dict] | None,
                     agent_state: str | None, err: str | None = None) -> str:
    """把「agent 反对」区回填进奏折 md。返回 done|missing|failed:…。

    为什么由 diagnose 回填而不是 engine 直接写：链路顺序是 engine 先出奏折、
    diagnose 之后才挑刺。engine 跑的那一刻反对还不存在，它只能在 md 里留一个
    带标记的空区块（见 engine/report.py 的 agent_block），diagnose 拿到结果后
    原位替换。这样做的好处是：diagnose 挂掉时奏折仍然是完整的，只是那一区写
    「今天没跑成」—— 符合计划书 10.1「agent 是附加价值，不是单点故障」。
    """
    if not run_id:
        return "missing"
    try:
        row = conn.execute(
            "select report_path from memorial where run_id=?", (run_id,)
        ).fetchone()
    except Exception as e:
        return f"failed:{e}"
    if not row or not row[0]:
        return "missing"

    # 两道闸，防止「在副本库上跑，却改了生产奏折」。
    # 真发生过（2026-11-23 17:49）：subagent 把库复制到 /tmp 上跑 diagnose，
    # memorial.report_path 里存的是生产 md 的绝对路径，于是 /tmp 库里的那条
    # 「429 没跑成」被写进了生产奏折 —— 读的人会以为今天真的没跑成。
    # 库是副本，md 却是原本，这种跨库写入必须挡掉。
    guard = _report_write_guard(conn, row[0])
    if guard:
        return f"skipped:{guard}"

    # engine/report.py 在 engine/ 目录下，不在 adsdesk 的 sys.path 里。
    # 不 import 它整份模块（会拉进 facts/rules 一串依赖），只取那两个纯字符串函数。
    try:
        import importlib.util
        name = "_engine_report"
        spec = importlib.util.spec_from_file_location(
            name, str(Path(__file__).resolve().parents[2] / "engine" / "report.py"))
        mod = importlib.util.module_from_spec(spec)
        # 必须先登记进 sys.modules 再 exec_module：report.py 里有 @dataclass，
        # dataclass 要回头按 __module__ 名去 sys.modules 里取模块字典，
        # 没登记就会以 "NoneType object has no attribute '__dict__'" 炸掉。
        sys.modules[name] = mod
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))
        spec.loader.exec_module(mod)
        ok = mod.rewrite_agent_block(row[0], objs, run_id,
                                     agent_state=agent_state, err=err)
        return "done" if ok else "missing"
    except Exception as e:
        return f"failed:{e}"


def _set_agent_state(conn, run_id: int, state: str):
    conn.execute("update memorial set agent_state=? where run_id=?", (state, run_id))


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None,
                    help="把完整 raw 写到这个文件。dry-run 只打印前 800 字，"
                         "要核「它到底怎么答的」就得看全量")
    ap.add_argument("--no-shadow", action="store_true",
                    help="保留位：影子模式关闭需要另行实现接管逻辑，目前不生效")
    args = ap.parse_args()

    conn = context.connect()
    res = run_once(conn, run_id=args.run_id, model=args.model,
                   shadow=not args.no_shadow, dry_run=args.dry_run)
    conn.close()

    if not res.get("ok"):
        print(f"[诊断] 失败：{res['error']}")
        return 1
    if res.get("dry_run"):
        raw_txt = json.dumps(res["raw"], ensure_ascii=False)
        print(f"[诊断] dry-run  raw={raw_txt[:800]}")
        print(f"[诊断] tokens={res['tokens']}")
        if args.out:
            Path(args.out).write_text(raw_txt, encoding="utf-8")
            print(f"[诊断] 完整 raw 已写入 {args.out}（{len(raw_txt)} 字）")
    else:
        print(f"[诊断] run #{res['run_id']} agent_run #{res['agent_run_id']} "
              f"model={res['model']} tokens={res['tokens']}")
        for f in (res.get("fell_back") or []):
            # 降级不是错误，但必须留痕 —— 否则下次看到结论变差，无从知道换了模型。
            print(f"[诊断] 降级：前面那个没答上来 —— {f}")
        print(f"[诊断] 问题 {len(res['questions'])} 条 / 反对 {res['n_obj']} 条 / "
              f"断言 {res['n_eval']} 条（影子模式，不改奏折）")
    for w in res["warnings"]:
        print(f"  ! {w}")
    for q in res.get("questions") or []:
        print(f"  Q {q['id']} · {q['object']} · impact={q['impact']} unc={q['uncertainty']}")
        print(f"    {q['ask']}")
        for o, c in zip(q["options"], q["reason_codes"]):
            print(f"      [ ] {o}  -> {c}")
    for o in res.get("objections") or []:
        print(f"  O #{o['decision_id']} {o['verdict']}: {o['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
