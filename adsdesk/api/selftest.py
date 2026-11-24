"""P1 端到端自测：打钩 → 生成执行单 → 标已改 → 归因窗 → 答题，全链路走一遍。

真实顺序是「打钩 → 生成执行单 → 改后台 → 标已改」，脚本按这个顺序走。
只碰 test 专用的行，跑完自己删干净。真实数据一行不改。
用法：python3 adsdesk/api/selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1]))

from api.db import DB_PATH, connect  # noqa: E402
from agents.attribute import days_for  # noqa: E402

BASE = "http://127.0.0.1:5273/api"
MARK = "SELFTEST_P1"


def call_on(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def call(path: str, body: dict | None = None) -> tuple[int, dict]:
    return call_on(BASE, path, body)


def _free_port() -> int:
    """让内核挑一个空端口，避免写死端口和已跑的服务撞车。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def det(r: dict) -> dict:
    """HTTPException 的 detail 是字符串，成功响应的是 dict。统一成 dict。"""
    d = r.get("detail")
    return d if isinstance(d, dict) else {}


def msg(r: dict) -> str:
    d = r.get("detail")
    return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)[:160]


def main() -> int:
    conn = connect()
    ok = 0
    bad = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, bad
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            bad += 1
            print(f"  ✗ {name} {detail}")

    def exec_chain() -> None:
        """执行单整条链，跑在临时库副本 + 独立端口上。

        为什么不放在主流程里顺手做：这一段要真的把一张单子从 open 走到 done，
        会落下 executed 决策。打在生产库上就是每跑一次自测多 3 条 executed，
        下一轮 attribute 会拿它们去回看，自测污染归因结论（11-19 那次实测就是
        在生产库上留下一条带单号的孤儿决策 id=90）。
        副本必须用 sqlite backup API 从只读连接拷：生产库开着 WAL，直接 cp 主文件
        会丢 WAL 里还没 checkpoint 的行。
        """
        print("\n11) 执行单闭环 + 归因窗（临时库副本，独立端口）")
        # 计划书 §4.4 是归因窗的设计依据。早期 approve.py 里的手写值偏短
        # （bid 类只给 4 天、add_negative 只给 10 天、pause 只给 7 天），
        # 效应还没显现就判「无效」，attribute 会永久产出 weak。
        # 这里逐项钉死，防止哪天常量又被调短而没人发现。
        plan_days = {
            # 预算三类由计划书的 3 天改为 5 天：计划书写 3 天时的前提是「窗一到
            # 就结算」，而窗末最后 1~2 天的订单还没回传，3 天里能用的其实只有
            # 1~2 天。现在 attribute 改成「窗满 + 3 天回传缓冲」才结算，3 天窗
            # 的样本量仍然太薄（判一次预算动作至少要看 5 天量级变化），故上调。
            # 计划书 §4.4 表格已同步注明。
            "raise_budget": 5, "reduce_budget": 5, "lower_budget": 5,
            "reduce_bid": 7, "raise_bid": 7,
            "add_negative": 14, "adjust_placement": 7,
            "harvest_keyword": 14, "harvest": 14, "pause_candidate": 14,
            "floor_breach": 7,  # 计划书未覆盖，沿用 observe 类 7 天
            "observe": 7,
        }
        drift = {k: (days_for(k), v) for k, v in plan_days.items() if days_for(k) != v}
        check("归因窗与计划书 §4.4 一致", not drift, str(drift))
        check("未知 action_type 落到默认 7 天", days_for("no_such_action") == 7,
              str(days_for("no_such_action")))

        tmpdir = Path(tempfile.mkdtemp(prefix="adsdesk-selftest-"))
        tmpdb = tmpdir / "ads.sqlite3"
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(tmpdb)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        copy = sqlite3.connect(tmpdb)
        copy.row_factory = sqlite3.Row
        port = _free_port()
        base = f"http://127.0.0.1:{port}/api"
        log = open(tmpdir / "uvicorn.log", "w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(HERE), env={**os.environ, "ADS_DB": str(tmpdb)},
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            up = False
            for _ in range(160):
                time.sleep(0.25)
                try:
                    st, h = call_on(base, "/health")
                    # 光端口通不够：必须确认它读的就是副本，否则后面全打在生产库上
                    if st == 200 and Path(h.get("db") or "") == tmpdb:
                        up = True
                        break
                except Exception:  # noqa: BLE001 - 起服务期间连不上是常态
                    pass
            check("副本实例起来了且指向副本库", up, f"port={port}")
            if not up:
                # 起不来时把服务的 stderr 打出来，否则只看到「port=xxx」没法查
                log.flush()
                print("  —— uvicorn 日志尾 ——")
                print((tmpdir / "uvicorn.log").read_text()[-800:])
                return

            # 夹具直接写副本：两条同活动 + 一条无活动归属。
            # 用三种不同的 action_type，是为了让归因窗分别落到 7/14/14 天。
            fixtures = [
                ("selftest 执行单词 A", "C-SELFTEST-1", "selftest-kw-a",
                 "reduce_bid", "$1.00", "$0.80"),
                ("selftest 执行单词 B", "C-SELFTEST-1", "selftest-kw-b",
                 "pause_candidate", "$2.00", "pause"),
                ("selftest 执行单词 C", "", "selftest-kw-c",
                 "add_negative", None, "negative exact"),
            ]
            dids: list[int] = []
            for label, camp, eid, action, cur, prop in fixtures:
                copy.execute(
                    """insert into decision_log
                       (run_id, rule_id, rule_version, entity_type, entity_id,
                        entity_label, campaign_id, action_type, confidence,
                        current_value, proposed_value, spend_impact, status,
                        rationale, decided_at, context_json, calculation_json,
                        significance_json)
                       values (11,'R01','1.0','keyword',?,?,?,?, 'high',?,?, -5.0,
                               'approved',?,'2026-11-17T00:00:00Z','{}','[]','{}')""",
                    (eid, label, camp, action, cur, prop, MARK),
                )
                dids.append(int(copy.execute(
                    "select last_insert_rowid() i").fetchone()["i"]))
            copy.commit()

            st, r = call_on(base, "/approve/exec-order",
                            {"decision_ids": dids, "run_id": 11, "note": MARK})
            dd = det(r)
            check("下单 200", st == 200, msg(r))
            order_id = dd.get("order_id")
            ticket = dd.get("ticket_id")
            check("单号形如 T<日期>-NNN",
                  isinstance(ticket, str) and ticket.startswith("T")
                  and len(ticket.rsplit("-", 1)[-1]) == 3, str(ticket))
            check("回执带 3 条 item", len(dd.get("items") or []) == 3,
                  str(len(dd.get("items") or [])))
            check("副本里落了 1 张单", copy.execute(
                "select count(*) c from exec_order where order_id=?",
                (order_id,)).fetchone()["c"] == 1, str(order_id))
            check("副本里落了 3 条条目", copy.execute(
                "select count(*) c from exec_order_item where order_id=?",
                (order_id,)).fetchone()["c"] == 3, str(order_id))

            # ticket 表是「一张单的摘要」，exec_order 是「这张单本身」。
            # exec_order.ticket_id 的注释一直写着「关联 ticket.ticket_id」，
            # 但从来没有人往里写过 —— 于是「按单号查摘要」永远查空。
            tk = copy.execute("select * from ticket where ticket_id=?", (ticket,)).fetchone()
            check("ticket 表落了这张单的摘要", tk is not None, str(ticket))
            check("摘要里的条数与单子一致",
                  (tk["decision_count"] if tk else None) == 3,
                  str(dict(tk) if tk else None))
            check("摘要里记了总影响金额",
                  tk is not None and tk["spend_impact"] is not None,
                  str(dict(tk) if tk else None))

            # 前端 `order.order?.ticket_id` 读的就是回执里的 order 键；
            # 原来只回 detail 里的平铺字段，那一行永远显示 undefined。
            check("下单回执带 order 键", isinstance(dd.get("order"), dict),
                  str(list(dd.keys())))
            check("回执 order.ticket_id 与单号一致",
                  (dd.get("order") or {}).get("ticket_id") == ticket, str(ticket))

            st, r = call_on(base, f"/approve/exec-order/{order_id}")
            ddd = det(r)
            order = ddd.get("order") or {}
            groups = ddd.get("groups") or []
            check("单子初始 open", order.get("status") == "open",
                  str(order.get("status")))
            sizes = {g["campaign_name"]: g["count"] for g in groups}
            check("按活动分成 2 组（同活动合组、无活动单独一组）",
                  len(groups) == 2, str(sizes))
            check("同活动 2 条并到一组", sizes.get("C-SELFTEST-1") == 2, str(sizes))
            # 无活动归属的条目原来存空串，会和「活动名恰好为空」的行混进同一组，
            # 分组时看不出这是两码事 —— 现在写成人话占位符，自成一组的语义才立得住。
            check("无活动归属 1 条自成一组", sizes.get("（无活动归属）") == 1, str(sizes))

            seen: list[str] = []
            codes: list[int] = []
            for did in dids:
                st, r = call_on(base, "/approve/executed",
                                {"decision_id": did, "executed_by": "selftest"})
                codes.append(st)
                for o in (det(r).get("exec_orders") or []):
                    seen.append(o.get("status"))
            check("逐条标已改全部 200", all(c == 200 for c in codes), str(codes))
            # 这一条是本次修的断点：改之前 item.executed_at 永远是 NULL，
            # 单子永远 open，前端「已改」按钮点了不消失。
            check("单子状态随改完条数推进 partial→partial→done",
                  seen == ["partial", "partial", "done"], str(seen))

            st, r = call_on(base, f"/approve/exec-order/{order_id}")
            ddd = det(r)
            items = ddd.get("items") or []
            order = ddd.get("order") or {}
            check("单子最终 done", order.get("status") == "done",
                  str(order.get("status")))
            check("3 条 item 都写了 executed_at",
                  len(items) == 3 and all(it.get("executed_at") for it in items),
                  str([it.get("executed_at") for it in items]))
            check("分组 executed 计数同步",
                  all(g["executed"] == g["count"]
                      for g in (ddd.get("groups") or [])),
                  str(ddd.get("groups")))

            for did, action in zip(dids, ["reduce_bid", "pause_candidate",
                                          "add_negative"]):
                row = copy.execute(
                    """select executed_at, attribution_due_at from decision_log
                       where decision_id=?""", (did,)).fetchone()
                try:
                    delta: object = round(
                        (datetime.fromisoformat(row["attribution_due_at"])
                         - datetime.fromisoformat(row["executed_at"])
                         ).total_seconds() / 86400, 3)
                except Exception as e:  # noqa: BLE001
                    delta = f"解析失败: {e}"
                want = float(days_for(action))
                check(f"{action} 归因窗 = {int(want)} 天（计划书 §4.4）",
                      delta == want, f"{delta} vs {want}")

            st, r = call_on(base, "/approve/executed", {"decision_id": dids[0]})
            check("重复标已改 409", st == 409, msg(r))
            st, r = call_on(base, "/approve/exec-order",
                            {"decision_ids": [dids[0]]})
            check("已执行的不能再进单 409", st == 409, msg(r))

            # 单号唯一性：自带一个已存在的号必须被挡，留空则必须发新号。
            # 这两条锁的是「两张单同号 → 按单号再也分不清决策在哪张单上」。
            copy.execute(
                """insert into decision_log
                   (run_id, rule_id, rule_version, entity_type, entity_id,
                    entity_label, campaign_id, action_type, confidence,
                    current_value, proposed_value, spend_impact, status,
                    rationale, decided_at, context_json, calculation_json,
                    significance_json)
                   values (11,'R01','1.0','keyword','selftest-kw-d','selftest 执行单词 D',
                           'C-SELFTEST-2','reduce_bid','high','$1.00','$0.90',-5.0,
                           'approved',?,'2026-11-17T00:00:00Z','{}','[]','{}')""",
                (MARK,),
            )
            extra = int(copy.execute(
                "select last_insert_rowid() i").fetchone()["i"])
            copy.commit()
            st, r = call_on(base, "/approve/exec-order",
                            {"decision_ids": [extra], "ticket_id": ticket})
            check("自带单号撞号 409", st == 409, msg(r))
            st, r = call_on(base, "/approve/exec-order",
                            {"decision_ids": [extra]})
            check("留空时自动发新号（不与已有单同号）",
                  st == 200 and det(r).get("ticket_id") != ticket, msg(r))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            log.close()
            copy.close()
            # 副本整个删掉：不留 .sqlite3-wal/-shm，也不让 /tmp 攒垃圾
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 造一条可审批的测试建议 ----------
    conn.execute(
        """insert into decision_log
           (run_id, rule_id, rule_version, entity_type, entity_id, entity_label,
            campaign_id, action_type, confidence, current_value, proposed_value,
            spend_impact, status, rationale, decided_at,
            context_json, calculation_json, significance_json)
           values (10,'R01','1.0','keyword','selftest-kw','selftest 词',
                   '175723193023274','reduce_bid','high','$1.00','$0.80',
                   -12.5,'proposed',?,?,'{}','[]','{}')""",
        (MARK, "2026-11-17T00:00:00Z"),
    )
    conn.commit()
    did = conn.execute(
        "select decision_id from decision_log where rationale=? order by decision_id desc limit 1",
        (MARK,),
    ).fetchone()["decision_id"]
    print(f"[测试行] decision_id={did}")

    try:
        print("\n1) 审批：批这条")
        st, r = call("/approve/decision", {"decision_id": did, "status": "approved"})
        check("200", st == 200, msg(r))
        row = conn.execute(
            "select status, checked_at, checked_by from decision_log where decision_id=?", (did,)
        ).fetchone()
        check("落库 approved", row["status"] == "approved", str(dict(row)))
        check("写了 checked_at", bool(row["checked_at"]))
        check("写了 checked_by", bool(row["checked_by"]))

        print("\n2) 重复审批同一状态：应 409")
        st, r = call("/approve/decision", {"decision_id": did, "status": "approved"})
        check("409", st == 409, msg(r))

        print("\n3) 生成执行单")
        st, r = call("/approve/exec-order", {
            "decision_ids": [did], "run_id": 10, "note": MARK,
        })
        dd = det(r)
        check("200", st == 200, msg(r))
        ticket = dd.get("ticket_id")
        check("有 ticket_id", bool(ticket), msg(r))
        order_id = dd.get("order_id")
        if order_id:
            st, r2 = call(f"/approve/exec-order/{order_id}")
            items = det(r2).get("items") or []
            check("执行单含 1 条", len(items) == 1, str(len(items)))
            if items:
                check("执行单带当前值→目标值",
                      items[0].get("current_value") == "$1.00"
                      and items[0].get("target_value") == "$0.80",
                      str(items[0]))
        tk = conn.execute(
            "select ticket_id from decision_log where decision_id=?", (did,)
        ).fetchone()["ticket_id"]
        check("decision_log.ticket_id 回写", tk == ticket, str(tk))

        print("\n4) 同一个对象再下一单：应 409")
        st, r = call("/approve/exec-order", {"decision_ids": [did], "run_id": 10})
        check("409", st == 409, msg(r))

        print("\n5) 标「已改」→ 归因窗开始")
        # 不传 attribution_days：这里要测的就是「默认走常量表」这条路径。
        # 原来手写 4 天，等于在守护一个和计划书 §4.4 不符的数字（bid 类应 7 天）。
        st, r = call(
            "/approve/executed",
            {"decision_id": did, "executed_value": "$0.80"},
        )
        check("200", st == 200, msg(r))
        row = conn.execute(
            """select status, executed_at, executed_value, attribution_due_at
               from decision_log where decision_id=?""", (did,)
        ).fetchone()
        check("status=executed", row["status"] == "executed", str(dict(row)))
        check("executed_value 写入", row["executed_value"] == "$0.80")
        due = row["attribution_due_at"] or ""
        # 断言「相对 executed_at 正好 days_for(reduce_bid) 天」，不写死绝对日期 ——
        # 写死日期的话脚本只能用一天，第二天就跑不过（11-17 写的 11-21，11-18 就红了）。
        try:
            _exec = datetime.fromisoformat(row["executed_at"])
            _due = datetime.fromisoformat(due)
            _delta = round((_due - _exec).total_seconds() / 86400, 3)
        except Exception as e:  # noqa: BLE001
            _delta = f"解析失败: {e}"
        _want = float(days_for("reduce_bid"))
        check(f"到期日 = executed_at + {int(_want)} 天（计划书 §4.4）",
              _delta == _want, f"{due} (差 {_delta} 天，应 {_want})")

        print("\n6) 归因面板能看到它")
        st, r = call("/cockpit/attribution")
        found = any(x.get("decision_id") == did for x in (r.get("open") or []))
        check("出现在 open 列表", found)

        print("\n7) 已执行的不能再审批：应 409")
        st, r = call("/approve/decision", {
            "decision_id": did, "status": "rejected", "reject_reason_code": "owner_reject",
        })
        check("409", st == 409, msg(r))

        print("\n8) 答题：Q08 选第 0 项（→ 沉淀到 policy）")
        st, r = call("/approve/question", {
            "question_id": "Q08", "run_id": 10, "object_id": "账户目标",
            "selected": [0], "free_text": MARK, "skipped": False,
        })
        check("200", st == 200, msg(r))
        hit = conn.execute(
            "select answer_type, reason_code from reason_hit where evidence_text like ? order by hit_id desc limit 1",
            (f"%{MARK}%",),
        ).fetchone()
        check("reason_hit 记了 answer",
              hit is not None and hit["answer_type"] == "answer", str(hit))

        # 这一步是 S3 的核心：打钩不只是留痕，还要落进 policy 表，
        # 否则「我明明说过先维持 30%」在下一轮判断里根本不存在。
        pol = conn.execute(
            "select policy_type, status, expires_at from policy where object_id='账户目标' "
            "and source like '打钩%' order by policy_id desc limit 1"
        ).fetchone()
        check("打钩沉淀进 policy（不只是 reason_hit）",
              pol is not None and pol["status"] == "active", str(pol))
        sed = det(r).get("sediment") or []
        check("回执里带沉淀去向（前端要显示「已落到哪张表」）",
              bool(sed) and sed[0].get("table") == "policy", str(sed)[:160])

        print("\n9) 划掉 = 这个方向别再问（→ reason_excluded），不写 reason_hit")
        st, r = call("/approve/question", {
            "question_id": "Q08", "run_id": 10, "object_id": "账户目标-cross",
            "selected": [], "excluded": [1], "free_text": MARK, "skipped": False,
        })
        check("200", st == 200, msg(r))
        ex = conn.execute(
            "select expires_at from reason_excluded where object_id='账户目标-cross'"
        ).fetchone()
        check("划掉写 reason_excluded（90 天静默）", ex is not None, str(ex))
        crossed_hit = conn.execute(
            "select count(*) c from reason_hit where scope='Q08' "
            "and object_id='账户目标-cross'"
        ).fetchone()["c"]
        check("划掉不写 reason_hit（否则被算成「这个码又出现一次」）",
              crossed_hit == 0, str(crossed_hit))

        print("\n9b) 跳过 = 授权默认假设，写 agent_assumption")
        st, r = call("/approve/question", {
            "question_id": "Q08", "run_id": 10, "object_id": "账户目标-skip",
            "selected": [], "free_text": MARK, "skipped": True,
        })
        check("200", st == 200, msg(r))
        asm = conn.execute(
            "select default_action, review_at from agent_assumption where object_id=?",
            ("账户目标-skip",),
        ).fetchone()
        check("写了默认假设", asm is not None, str(asm))
        if asm:
            # 沉淀表的日期一律 date-only（YYYY-MM-DD），与「今天」直接比字符串，
            # 所以这里断言「复核日 == 今天 + 14 天」，不再算带时区的小数差。
            _want = (datetime.now(timezone(timedelta(hours=8))).date()
                     + timedelta(days=14)).isoformat()
            check("观察期 = 今天 + 14 天", asm["review_at"] == _want,
                  f"{asm['review_at']} 应等于 {_want}")

        print("\n10) 没有对应问题的题必须 404")
        st, r = call("/approve/question", {
            "question_id": "Q99", "run_id": 10, "object_id": "x", "selected": [0],
        })
        check("404", st == 404, msg(r))

        # 最后一段跑在副本上，所以它不会碰 conn（conn 指生产库）。
        exec_chain()

    finally:
        conn.execute("delete from exec_order_item where decision_id=?", (did,))
        conn.execute("delete from exec_order_item where order_id in "
                     "(select order_id from exec_order where note=?)", (MARK,))
        # ticket 必须在删 exec_order 之前清：它靠 exec_order.ticket_id 定位，
        # 顺序反了就只能按 ticket_id 猜，而自测每次发的号都不一样。
        conn.execute("delete from ticket where ticket_id in "
                     "(select ticket_id from exec_order where note=?)", (MARK,))
        conn.execute("delete from exec_order where note=?", (MARK,))
        conn.execute("delete from reason_hit where evidence_text like ?", (f"%{MARK}%",))
        conn.execute("delete from agent_assumption where object_id = ?", ("账户目标-skip",))
        # S3 之后打钩会写沉淀表；自测的行也必须删掉，否则真库里会留下
        # 一条「账户目标」的 active policy，下一轮真实判断会照它走。
        for _t, _col in (("policy", "policy_type"), ("param_override", "param_key"),
                         ("observation_task", "reason_code"), ("experiment", "reason_code"),
                         ("reason_excluded", "reason_code")):
            conn.execute(f"delete from {_t} where object_id like '账户目标%'")
        conn.execute("delete from decision_log where rationale=?", (MARK,))
        conn.commit()
        left = conn.execute(
            "select count(*) c from decision_log where rationale=?", (MARK,)
        ).fetchone()["c"]
        residue = {
            "decision_log": left,
            "exec_order": conn.execute(
                "select count(*) c from exec_order where note=?", (MARK,)
            ).fetchone()["c"],
            "exec_order_item": conn.execute(
                "select count(*) c from exec_order_item where decision_id not in "
                "(select decision_id from decision_log)"
            ).fetchone()["c"],
            # ticket 曾经不在清理和体检里，于是每跑一次自测就往真库留一张
            # 「selftest 词 $1.00→$0.80」的假单 —— 体检项漏了，污染就一直看不见。
            "ticket": conn.execute(
                "select count(*) c from ticket where summary like '%selftest%'"
            ).fetchone()["c"],
            "reason_hit": conn.execute(
                "select count(*) c from reason_hit where evidence_text like ?", (f"%{MARK}%",)
            ).fetchone()["c"],
            "agent_assumption": conn.execute(
                "select count(*) c from agent_assumption where object_id like '%selftest%'"
            ).fetchone()["c"],
            "sediment": conn.execute(
                "select (select count(*) from policy where object_id like '账户目标%')"
                " + (select count(*) from param_override where object_id like '账户目标%')"
                " + (select count(*) from observation_task where object_id like '账户目标%')"
                " + (select count(*) from experiment where object_id like '账户目标%')"
                " + (select count(*) from reason_excluded where object_id like '账户目标%') c"
            ).fetchone()["c"],
        }
        conn.commit()
        print(f"\n[清理] {residue}")
        conn.close()

    print(f"\n== {ok} 通过 / {bad} 失败 ==")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())