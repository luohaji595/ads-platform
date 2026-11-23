"""对话 agent：拿库里既成的事实，回答主人对某条判断的追问。

和诊断 agent 的分工：诊断 agent 每天跑一次，写 agent_run / agent_objection / agent_eval；
这个模块**只写 chat_turn**，一行都不碰事实表和台账。它存在的理由是「解释」，
不是「再判断一次」—— 主人问「今天为什么降 lat pulldown 的价」，要的是一条能点回
decision_log 的答案，不是又一份新建议。

取数必须复用 ``context.build_input()``：如果这里另写一套，同样的口径就会有两份实现，
某天两边算出不同的 ACOS，主人就再也不能相信任何一边。所以这个文件里没有一个 SELECT
去读 fact_* —— 事实全部来自那份输入包，它说什么，这里就说什么。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import context, gateway

CST = ZoneInfo("Asia/Shanghai")
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "converse_v1.md"
PROMPT_VER = hashlib.sha1(PROMPT_PATH.read_bytes()).hexdigest()[:8]

DEFAULT_SESSION = "web"

# 只带最近 20 条（≈10 轮）。再往上，事实块会在长上下文里被稀释；而历史里那些数字
# 早就过期了 —— 这一轮的唯一真相源是事实块，历史只用来接住「你刚才说的那个」。
HISTORY_TURNS = 20

# 网关默认模型带思维链（实测 ≈6800 tokens，会计入 max_tokens），32000 够一轮完整回答。
CHAT_MAX_TOKENS = 32000
# 实测一轮 100~160 秒。600 秒是三四倍余量；再长的话前端只能以为卡死了。
CHAT_TIMEOUT = 600

# 回答里点到的表名。抓的是「我这段结论站在哪张表上」，用正则而不是让模型自报：
# 自报的那份一定是它自己觉得该说的，不是它真的用了的。
_CITE_RE = re.compile(
    r"fact_[a-z_]+"
    r"|decision_log|memorial|param_override|observation_task|experiment"
    r"|reason_hit|reason_excluded|agent_assumption|policy|chat_turn"
)
# 实测第一个锚点几乎都是 decision_id，而不是表名（「decision_id=155」「decision_id 是 null」）。
# 只记表名的话，这条回答在台账里会显示「没引用任何表」—— 明明它指的那条判断比表名更精确。
_DECISION_RE = re.compile(r"decision_id\s*[=＝:：]\s*(\d+)")
# 单独认「decision_id 是 null」：那是它在指事实块里唯一没落库的那条，
# 恰恰是最该被记住的一次诚实，不能和「糊了一个号」混为一谈。
_NULL_DECISION_RE = re.compile(r"decision_id\s*(?:[=＝:：]\s*)?(?:是\s*)?null")


def _now() -> str:
    """时间戳跟 agents/ 的惯例走（本地时区 + 显式偏移）。

    不写 UTC 的 Z：台账里那些 ``2026-11-19T06:54:12Z`` 是引擎写的，人眼看的时候要换算。
    对话是人在看的，直接给本地时间，省得每次都要在脑子里减 8 小时。
    """
    return datetime.now(CST).isoformat(timespec="seconds")


def _check_schema(conn) -> None:
    """在花钱之前先确认写得进去。

    一轮对话要烧两分钟和几万 tokens。等到回答拿到手才发现表不存在，
    钱已经花掉了 —— 所以先查一次，让失败发生在免费的那一侧。
    """
    row = conn.execute(
        "select name from sqlite_master where type='table' and name='chat_turn'"
    ).fetchone()
    if not row:
        raise RuntimeError(
            "chat_turn 表不存在。生产和副本库都该有它；空库请先跑 adsdesk/api/schema.sql"
        )


def _run_for_as_of(conn, as_of: str) -> tuple[int, str]:
    """把 ``--as-of 2026-11-10`` 换成那天该看的那份奏折。

    取的是「business_day <= as_of 的最近一份」，而不是要求精确相等：
    周末没有奏折，强行要求相等会让人每次都得先去查哪天有。
    换出来的日子和请求的不一样时必须回话说清楚，否则「用 11-10 的库答 11-12 的问题」
    会变成一个没人发现的静默错位。
    """
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise ValueError(f"--as-of 要 YYYY-MM-DD，收到 {as_of!r}") from None
    row = conn.execute(
        "select run_id, business_day from memorial where business_day <= ? "
        "order by business_day desc, run_id desc limit 1",
        (as_of,),
    ).fetchone()
    if not row:
        first = conn.execute(
            "select business_day from memorial order by business_day limit 1"
        ).fetchone()
        raise RuntimeError(
            f"{as_of} 及之前没有奏折（最早一份是 {first['business_day'] if first else '无'}）"
        )
    return row["run_id"], row["business_day"]


def read_history(conn, session_id: str, limit: int = HISTORY_TURNS) -> list[dict]:
    """读最近 limit 条，按时间正序返回。

    先倒序取再正序排：直接 ``order by turn_id limit`` 拿到的是**最早**那 20 条，
    聊到第 11 轮之后，历史就永远停在开头 —— 这个 bug 只有真的聊久了才会露头。
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        "select role, content, turn_id, created_at from chat_turn where session_id = ? "
        "order by turn_id desc limit ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def cite_anchors(reply: str) -> dict:
    """抽出这条回答到底站在什么上面：表名 + decision_id + 有没有诚实指向「没落库」那条。

    这是给**事后的人**看的，不是给模型的运行期元数据：一条一个锚点都没有的回答，
    回看时就知道该打几分折扣。所以宁可如实记着空的，也不要补一个好看的 ——
    用正则而不是让模型自报，因为自报的那份是它觉得该说的，不是它真用了的。
    """
    text = reply or ""
    tables: list[str] = []
    seen: set[str] = set()
    for m in _CITE_RE.finditer(text):
        name = m.group(0)
        if name not in seen:
            seen.add(name)
            tables.append(name)
    ids: list[str] = []
    for m in _DECISION_RE.finditer(text):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return {
        "tables": tables,
        "decision_ids": ids,
        "null_decision": bool(_NULL_DECISION_RE.search(text)),
    }


def build_messages(fact: dict, history: list[dict], text: str) -> list[dict]:
    """拼成网关要的 messages。

    事实块放进 system，而不是伪造一条 assistant 回复：一旦往历史里塞假话，
    下一轮模型会把它当成自己说过的话继续引用，错误会自己强化自己。
    历史消息保持纯净（就是真的说过的话），事实每轮重新注入。
    """
    system = (
        PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\n## 本轮事实（context.build_input 的原样输出；键名已被代码依赖，别改写）\n"
        + json.dumps(fact, ensure_ascii=False, indent=2, default=str)
    )
    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in history:
        role = h.get("role")
        content = h.get("content") or ""
        if role in ("user", "assistant") and content.strip():
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": text})
    return msgs


def run_once(
    *,
    text: str,
    session_id: str = DEFAULT_SESSION,
    model: str | None = None,
    as_of: str | None = None,
    use_history: bool = True,
    history_turns: int = HISTORY_TURNS,
    dry_run: bool = False,
) -> dict:
    """跑一轮对话。

    失败时**一条都不写**：chat_turn 记的是「人和模型说过什么」，半轮记录会让人
    以为模型答过、只是内容丢了。宁可这次什么都没发生，也不要留一条读不通的历史。

    整个 LLM 调用期间不持有任何 sqlite 连接 —— 一轮几十秒，攥着写锁会把
    approve.py 和引擎一起堵死（context.connect 的 docstring 里有实测账）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "kind": "input", "error": "消息是空的",
                "written": False, "elapsed_ms": 0}

    conn = context.connect()
    try:
        _check_schema(conn)
        business_day: str | None
        if as_of:
            run_id, business_day = _run_for_as_of(conn, as_of)
        else:
            run_id, business_day, _pack = context.latest_pack(conn)
        # 显式传 run_id：build_input 自己会再 latest_pack 一次，上面已经拿到了，
        # 顺手传进去既省一次 JSON 解析，也保证两者说的是同一份奏折。
        fact = context.build_input(conn, run_id)
        history = read_history(conn, session_id, history_turns) if use_history else []
    except (RuntimeError, ValueError) as e:
        # kind 是给 HTTP 层分诊用的：同样是「没成」，输入写错该 400，网关挂了该 502。
        # 靠匹配错误文本判断状态码，改一句中文就会悄悄变成 500。
        return {"ok": False, "kind": "input", "error": str(e),
                "written": False, "elapsed_ms": 0}
    finally:
        conn.close()

    msgs = build_messages(fact, history, text)
    chosen = model or gateway.DEFAULT_MODEL

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "written": False,
            "run_id": run_id,
            "business_day": business_day,
            "history_used": len(history),
            "messages": len(msgs),
            "system_chars": len(msgs[0]["content"]),
            # 指纹要覆盖真正发出去的东西（含历史折叠进 system 的事实块），
            # 只算 fact 的话「历史变了、指纹没变」会让人以为输入一模一样。
            "input_digest": context.digest({"messages": msgs}),
            "model": chosen,
        }

    t0 = time.monotonic()
    try:
        # 走链路而不是单模型：2026-11-23 实测 duanyan/* 按 max_tokens 掐额度
        # （mt=8 能过、4000 就 429），auto/* 是 CodeArts 积分耗尽。只试一个模型时
        # 一次额度波动就等于「对话 agent 答不出来」，而换条路明明能答。
        r = gateway.chat_chain(
            msgs,
            model=chosen,
            max_tokens=CHAT_MAX_TOKENS,
            timeout=CHAT_TIMEOUT,
            json_mode=False,  # 对话要人话，json_mode 会逼模型交 JSON 再 json.loads 失败
        )
    except gateway.GatewayError as e:
        # 网关挂了就把话原样带出去，不包装成「稍后重试」—— 429 和超时该由人决定怎么办。
        return {
            "ok": False,
            "kind": "gateway",
            "error": str(e),
            "written": False,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "model": chosen,
        }
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    reply = (r.get("content") or "").strip()
    if not reply:
        return {
            "ok": False,
            "kind": "model",
            "error": f"模型返回空内容（finish_reason={r.get('finish_reason')}）",
            "written": False,
            "elapsed_ms": elapsed_ms,
            "model": r.get("model") or chosen,
        }

    anchors = cite_anchors(reply)
    # chat_turn 没有「基于哪次奏折」的列，而这句话事后一定会被追问。cited_tables 本来就是
    # 个自由 JSON 文本列，把溯源和引用一起塞进去，比改表结构便宜；读取侧 list/dict 都认。
    stamp = json.dumps(
        {
            "run_id": run_id,
            "prompt_ver": PROMPT_VER,
            "cited_tables": anchors["tables"],
            "cited_decision_ids": anchors["decision_ids"],
            "null_decision": anchors["null_decision"],
        },
        ensure_ascii=False,
    )

    conn = context.connect()
    try:
        # autocommit 连接（isolation_level=None），user 和 assistant 必须显式开事务，
        # 否则第一条 insert 就已经落盘，第二条失败会留下半轮。
        conn.execute("begin")
        cur = conn.execute(
            "insert into chat_turn (session_id, role, content, cited_tables, agent_run_id, created_at) "
            "values (?, 'user', ?, null, null, ?)",
            (session_id, text, _now()),
        )
        user_turn_id = cur.lastrowid
        cur = conn.execute(
            "insert into chat_turn (session_id, role, content, cited_tables, agent_run_id, created_at) "
            "values (?, 'assistant', ?, ?, null, ?)",
            (session_id, reply, stamp, _now()),
        )
        turn_id = cur.lastrowid
        conn.execute("commit")
    except Exception as e:  # noqa: BLE001 — 写不进去也要把回答交出去，别让人白等两分钟
        try:
            conn.execute("rollback")
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "kind": "write",
            "error": f"回答已拿到，但写 chat_turn 失败：{e}",
            "reply": reply,
            "written": False,
            "elapsed_ms": elapsed_ms,
            "model": r.get("model") or chosen,
        }
    finally:
        conn.close()

    return {
        "ok": True,
        # model 记真正答话的那个：网关降级过的话，回头查「那天为什么说法变了」
        # 必须能看到实际参与的模型名（与 diagnose.py 同一套理由）。
        "turn_id": turn_id,
        "user_turn_id": user_turn_id,
        "reply": reply,
        "model": r.get("model") or chosen,
        "tokens": {"in": r.get("tokens_in"), "out": r.get("tokens_out")},
        "elapsed_ms": elapsed_ms,
        "cited_tables": anchors["tables"],
        "cited_decision_ids": anchors["decision_ids"],
        "run_id": run_id,
        "business_day": business_day,
        "history_used": len(history),
        "written": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="对话 agent：基于库里的事实回答一句追问（只写 chat_turn）"
    )
    ap.add_argument("text", help="这一轮要说的话")
    ap.add_argument("--as-of", metavar="YYYY-MM-DD",
                    help="用这一天的奏折来答（默认最新；没有奏折的日子会往前取最近一份）")
    ap.add_argument("--model", help=f"指定模型，默认 {gateway.DEFAULT_MODEL}")
    ap.add_argument("--session", default=DEFAULT_SESSION, help="会话 id，历史按它分")
    ap.add_argument("--turns", type=int, default=HISTORY_TURNS, help="带多少条历史")
    ap.add_argument("--dry-run", action="store_true", help="只拼上下文，不调模型也不写库")
    ap.add_argument("--rebuild-context", action="store_true",
                    help="不带历史，只用当前事实重开一段干净上下文（仍然会调模型并写库）")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON 结果")
    args = ap.parse_args()

    res = run_once(
        text=args.text,
        session_id=args.session,
        model=args.model,
        as_of=args.as_of,
        use_history=not args.rebuild_context,
        history_turns=args.turns,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if res.get("dry_run"):
        print(f"[对话] dry-run：奏折 #{res['run_id']}（{res['business_day']}）"
              f" 历史 {res['history_used']} 条 / 共 {res['messages']} 条消息 / "
              f"system {res['system_chars']} 字 / model {res['model']}")
        print(f"[对话] 输入指纹 {res['input_digest']}（没调模型，没写库）")
        return 0

    if not res.get("ok"):
        print(f"[对话] 失败：{res['error']}")
        if res.get("reply"):
            print(f"[对话] 回答内容（未入库）：\n{res['reply']}")
        print(f"[对话] 耗时 {res.get('elapsed_ms')} ms")
        return 1

    print(res["reply"])
    tok = res["tokens"]
    print(f"\n--- 奏折 #{res['run_id']}（{res['business_day']}）· model {res['model']} · "
          f"{tok['in']}+{tok['out']} tokens · {res['elapsed_ms'] / 1000:.1f}s · "
          f"turn #{res['turn_id']}")
    anchors = res["cited_tables"] + [f"decision_id={i}" for i in res["cited_decision_ids"]]
    print(f"    锚点：{'、'.join(anchors) or '（无 —— 这条回答没指到任何表或 decision_id，要打折听）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())