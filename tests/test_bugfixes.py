#!/usr/bin/env python3
"""本次修复的回归测试。纯 unittest，直接 `python3 tests/test_bugfixes.py` 跑。

为什么不用 pytest：本机没装（`import pytest` → ModuleNotFoundError），仓库现有的
测试（midship/test_midship.py 等）也都是纯脚本风格。测试若要额外依赖，就等于
「在有网有闲的机器上能过、在守夜人这台机器上跑不起来」。

每个用例都对着一个真实踩过的坑，注释写清「修之前会怎样」，这样以后谁改坏了
能立刻知道坏在哪。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.path.join(ROOT, "adsdesk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ------------------------------------------------------------------ Fix1 积加 token

class JijiaTokenRetryTest(unittest.TestCase):
    """HTTP 200 + 业务码 401/40005 时必须清 token 重试。

    修之前：call() 只在 RuntimeError 里认 401，而那条 RuntimeError 只可能由
    「取 token 失败」抛出。于是正常调用路径上「token 过期」这段是死代码 ——
    接口一直回 401，客户端一次都不肯续。
    """

    def setUp(self):
        from ingest import jijia_client
        self.mod = jijia_client

    def _client(self):
        c = self.mod.JijiaClient.__new__(self.mod.JijiaClient)
        c._token = "stale"
        c._last_call_at = 0.0
        return c

    def test_http200_business_code_triggers_retry(self):
        c = self._client()
        calls = []

        def fake_once(endpoint, payload, app_key):
            calls.append(endpoint)
            if len(calls) == 1:
                return {"code": 40005, "msg": "token 已过期"}
            return {"code": 0, "data": {"ok": True}}

        with mock.patch.object(c, "_credentials", return_value=("ak", "sk")), \
             mock.patch.object(c, "_call_once", side_effect=fake_once), \
             mock.patch.object(c, "_throttle") as throttle:
            out = c.call("/x")

        self.assertEqual(out, {"code": 0, "data": {"ok": True}})
        self.assertEqual(len(calls), 2, "应当重试恰好一次")
        self.assertEqual(throttle.call_count, 2, "主节流 + 重试短节流各一次")

    def test_string_code_also_recognized(self):
        self.assertTrue(self.mod.JijiaClient._is_token_expired_resp({"code": "401"}))
        self.assertTrue(self.mod.JijiaClient._is_token_expired_resp({"code": 401}))
        self.assertTrue(self.mod.JijiaClient._is_token_expired_resp(
            {"msg": "access token expire"}))
        self.assertFalse(self.mod.JijiaClient._is_token_expired_resp({"code": 0}))
        self.assertFalse(self.mod.JijiaClient._is_token_expired_resp({}))

    def test_retry_uses_short_interval_not_main_throttle(self):
        """重试若复用 60s 主节流，一次过期就阻塞一分钟，批量补数会撞 600s 硬截止。"""
        c = self._client()
        self.assertLess(self.mod.RETRY_INTERVAL, self.mod.MIN_INTERVAL)

        def fake_once(endpoint, payload, app_key):
            return {"code": 40005, "msg": "token 已过期"}

        with mock.patch.object(c, "_credentials", return_value=("ak", "sk")), \
             mock.patch.object(c, "_call_once", side_effect=fake_once), \
             mock.patch.object(c, "_throttle") as throttle:
            c.call("/x")

        intervals = [kw.get("args", (None,))[0] if kw.get("args") else None
                     for kw in ({"args": cc.args} for cc in throttle.call_args_list)]
        self.assertEqual(intervals, [None, self.mod.RETRY_INTERVAL])


# ------------------------------------------------------------------ Fix2 审批状态机

class ApproveStateMachineTest(unittest.TestCase):
    """mark_executed 只认 approved；_close_exec_items 不碰已取消单。"""

    def setUp(self):
        from adsdesk.api.routes import approve
        from adsdesk.api import models
        self.approve = approve
        self.models = models

    def _mem(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            create table decision_log(
                decision_id integer primary key, run_id integer, rule_id text,
                action_type text, entity_type text, entity_id text, entity_label text,
                campaign_id text, current_value text, proposed_value text, status text,
                confidence real, spend_impact real, rationale text, ticket_id text,
                gate_reason text, override_by text, override_reason text,
                overridden_at text, executed_at text, executed_by text,
                executed_value text, attribution_due_at text, outcome_filled_at text,
                cooldown_until text, expires_at text);
            create table exec_order(
                order_id text primary key, decision_id integer, status text);
            create table exec_order_item(
                decision_id integer, order_id text, sku text, executed_at text,
                executed_by text);
        """)
        conn.execute("""insert into decision_log(decision_id, rule_id, action_type,
                        entity_type, entity_id, entity_label, proposed_value, status)
                        values(1,'R05','lower_budget','campaign','C1','C1','150',?)""",
                     ("proposed",))
        conn.commit()
        return conn

    def test_proposed_cannot_be_marked_executed(self):
        conn = self._mem()
        body = self.models.ExecutedIn(decision_id=1, executed_value="150")

        @__import__("contextlib").contextmanager
        def fake_write_conn():
            yield conn

        with mock.patch.object(self.approve, "write_conn", fake_write_conn):
            with self.assertRaises(self.approve.HTTPException) as ctx:
                self.approve.mark_executed(body)
        self.assertEqual(ctx.exception.status_code, 409)
        row = conn.execute("select status, executed_at from decision_log").fetchone()
        self.assertEqual(row["status"], "proposed", "被拒后状态不能变")
        self.assertIsNone(row["executed_at"])

    def test_close_items_skips_cancelled_order(self):
        conn = self._mem()
        conn.execute("insert into exec_order values('O1',1,'open')")
        conn.execute("insert into exec_order values('O2',1,'cancelled')")
        conn.execute("insert into exec_order_item values(1,'O1','A',null,null)")
        conn.execute("insert into exec_order_item values(1,'O2','B',null,null)")
        conn.commit()
        out = self.approve._close_exec_items(conn, 1, "2026-11-24T10:00:00", "tester")
        self.assertEqual([o["order_id"] for o in out], ["O1"])
        self.assertEqual(out[0]["status"], "done")
        rows = {r["order_id"]: r["executed_at"] for r in
                conn.execute("select order_id, executed_at from exec_order_item")}
        self.assertIsNotNone(rows["O1"])
        self.assertIsNone(rows["O2"], "已取消单的明细不该被打上执行时间")
        cancelled = conn.execute(
            "select status from exec_order where order_id='O2'").fetchone()["status"]
        self.assertEqual(cancelled, "cancelled", "已取消单的状态不该被改写")


# ------------------------------------------------------------------ Fix3 只读连接

class ReadonlyConnectionTest(unittest.TestCase):
    """只读句柄上不许执行 journal_mode=WAL（那是写操作）。"""

    def test_readonly_conn_refuses_write(self):
        from adsdesk.api import db as dbmod
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.sqlite3")
        sqlite3.connect(path).close()
        with mock.patch.object(dbmod, "DB_PATH", __import__("pathlib").Path(path)):
            conn = dbmod.connect(readonly=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("create table x(a)")
            finally:
                conn.close()
            rw = dbmod.connect(readonly=False)
            try:
                mode = rw.execute("pragma journal_mode").fetchone()[0]
            finally:
                rw.close()
        self.assertEqual(mode.lower(), "wal")


# ------------------------------------------------------------------ Fix4 归因窗

class AttributionWindowTest(unittest.TestCase):
    """after 从执行次日起算；结算要过回传缓冲。"""

    def setUp(self):
        from adsdesk.agents import attribute
        self.attr = attribute

    def _row(self, executed_at, due_at, action_type="lower_budget"):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""create table t(executed_at text, attribution_due_at text,
                                        action_type text)""")
        conn.execute("insert into t values(?,?,?)", (executed_at, due_at, action_type))
        return conn.execute("select * from t").fetchone()

    def test_after_window_excludes_execution_day(self):
        row = self._row("2026-11-10T14:30:00+08:00", "2026-11-13T14:30:00+08:00")
        n, w = self.attr._windows(row, long=False)
        self.assertEqual(n, 3)
        self.assertEqual(w["after"][0], "2026-11-11")
        self.assertEqual(w["after"][1], "2026-11-13")
        self.assertEqual(w["after"][2], 3, "after 仍应是 n 天，不能因为剥离执行日缩水")
        self.assertEqual(w["before"][0], "2026-11-07")
        self.assertEqual(w["before"][1], "2026-11-09")

    def test_peer_window_excludes_anchor_day(self):
        peer = {"decided_at": "2026-11-10T09:00:00+08:00"}
        before, after = self.attr._peer_window(peer, 3)
        self.assertEqual(after[0], "2026-11-11")
        self.assertEqual(after[1], "2026-11-13")
        self.assertEqual(before[1], "2026-11-09")

    def test_budget_window_at_least_five_days(self):
        for action in ("lower_budget", "reduce_budget", "raise_budget"):
            self.assertGreaterEqual(self.attr.days_for(action), 5, action)

    def test_settle_lag_exists(self):
        self.assertEqual(self.attr.ATTR_SETTLE_LAG_DAYS, 3)

    def test_window_not_matured_before_lag(self):
        ex = datetime(2026, 11, 10, 14, 30)
        last = date(2026, 11, 13)          # 3 天窗 → after 11-11~11-13
        settle = last + timedelta(days=3)  # 11-16
        # 窗刚满、报表也到了，但缓冲没过 → 不算
        self.assertFalse(self.attr._window_matured(ex, 3, settle, datetime(2026, 11, 13, 20, 0)))
        # 缓冲过了、报表覆盖过缓冲日 → 可以
        self.assertTrue(self.attr._window_matured(ex, 3, settle, datetime(2026, 11, 16, 9, 0)))
        # 缓冲过了，但报表只到窗末日 → 不能算（窗末那天的订单还没回传）
        self.assertFalse(self.attr._window_matured(
            ex, 3, last, datetime(2026, 11, 16, 9, 0)))
        # 宽限期过了、报表覆盖到窗末 → 按「确实没数据」结案，别把队列堵死
        self.assertTrue(self.attr._window_matured(
            ex, 3, last, datetime(2026, 11, 21, 9, 0)))
        # 报表一天都没有 → 一律不算
        self.assertFalse(self.attr._window_matured(ex, 3, None, datetime(2026, 12, 1, 9, 0)))


# ------------------------------------------------------------------ Fix5 ctr

class FactsCtrTest(unittest.TestCase):
    def test_ctr_is_none_when_no_impressions(self):
        from engine.facts import _r, _safe_div
        self.assertIsNone(_r(_safe_div(0, 0), 6))
        self.assertEqual(_r(_safe_div(5, 100), 6), 0.05)


# ------------------------------------------------------------------ Fix6 日更链

class ChainMidshipTest(unittest.TestCase):
    def test_midship_is_wired_but_opt_in(self):
        from adsdesk import chain
        self.assertIn("midship", chain.MAX_ATTEMPTS)
        self.assertIn("midship", chain.TIMEOUTS)
        self.assertEqual(chain.build_cmd("midship", "auto/x")[-2:], ["midship", "--project"])
        self.assertNotIn("midship",
                         ["ingest", "market", "rank", "engine", "diagnose",
                          "attribute", "converge"],
                         "默认链里不跑 midship，见 chain.main 的注释")

    def test_step_done_reads_run_log_not_chain_run(self):
        """回归：step_done 曾读 chain_run，而 record() 在复验之后才写，
        导致第一次成功也必判 failed（2026-11-24 18:00 实测踩到）。"""
        import sqlite3
        from adsdesk import chain

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        chain.ensure_schema(conn)
        conn.executescript("""
            create table run_log (run_id integer primary key autoincrement,
                dataset text not null, market_id integer, start_date text, end_date text,
                pages integer default 0, rows_fetched integer default 0,
                rows_upserted integer default 0, status text not null, error text,
                started_at text not null, finished_at text, note text);
        """)
        today = "2026-11-24"

        # 只有 chain_run 的 ok 行时不能算完成（这正是原来的 bug）
        chain.record(conn, today, "midship", 1, "ok", 0,
                     f"{today}T18:00:00+08:00", "假的：chain_run 说 ok")
        done, why = chain.step_done(conn, "midship", today)
        self.assertFalse(done, f"chain_run 不该被当成完成凭据，却返回了 {why}")

        # run_log 里有了 midship 的 ok 行才算
        conn.execute(
            "insert into run_log(dataset,start_date,end_date,status,started_at,finished_at,note)"
            " values('midship.project',?,?,'ok',?,?,?)",
            (today, today, f"{today}T18:04:20+08:00", f"{today}T18:04:20+08:00", "消费 0 条"))
        conn.commit()
        done, why = chain.step_done(conn, "midship", today)
        self.assertTrue(done, why)
        self.assertIn("18:04:20", why)


# ------------------------------------------------------------------ Fix7 qkey 剥离

class QuestionQkeyTest(unittest.TestCase):
    """前端用 qkey（如 Q01#2）定位同号题，落库必须用裸题号（Q01）。

    修之前：approve.answer_question 用 qkey 定位出 q 后，仍把带 #N 的 body.question_id
    原样传给 reason_routes.assume / apply_answer / exclude 与 reason_hit.scope，
    导致 agent_assumption.question_code='Q01#2'，收敛器 _REFUTE_ON 查不到并判 unverifiable。
    """

    def setUp(self):
        from adsdesk.api.routes import approve
        from adsdesk.api import models
        self.approve = approve
        self.models = models

    def _mem(self):
        import json
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            create table memorial(run_id integer primary key, pack_json text);
            create table policy(
                policy_id integer primary key autoincrement,
                scope text, object_id text, policy_type text, params_json text,
                source text, evidence_text text, status text, created_at text,
                expires_at text, reviewed_at text);
            create table reason_hit(
                hit_id integer primary key autoincrement,
                object_id text, reason_code text, scope text, evidence_text text,
                created_at text, answer_type text, decision_id integer);
            create table reason_excluded(
                excluded_id integer primary key autoincrement,
                object_id text, reason_code text, created_at text, expires_at text);
            create table agent_assumption(
                assumption_id integer primary key autoincrement,
                object_id text, question_code text, default_action text,
                created_at text, review_at text, result text);
        """)
        pack = {
            "questions": [
                {
                    "id": "Q01",
                    "qkey": "Q01#1",
                    "object": "kw-1",
                    "options": ["允许暂停", "其他"],
                    "reason_codes": ["kw_mismatch_ok_to_pause", "kw_other"],
                    "option_meta": [
                        {"reason_code": "kw_mismatch_ok_to_pause", "policy_type": "allow_pause_no_orders"},
                        {"reason_code": "kw_other"},
                    ],
                },
                {
                    "id": "Q01",
                    "qkey": "Q01#2",
                    "object": "kw-2",
                    "options": ["允许暂停", "其他"],
                    "reason_codes": ["kw_mismatch_ok_to_pause", "kw_other"],
                    "option_meta": [
                        {"reason_code": "kw_mismatch_ok_to_pause", "policy_type": "allow_pause_no_orders"},
                        {"reason_code": "kw_other"},
                    ],
                },
            ]
        }
        conn.execute("insert into memorial values(10, ?)", (json.dumps(pack),))
        conn.commit()
        return conn

    def test_skip_with_qkey_writes_bare_question_code(self):
        conn = self._mem()
        body = self.models.QuestionAnswerIn(
            question_id="Q01#2", run_id=10, selected=[], skipped=True,
        )

        @__import__("contextlib").contextmanager
        def fake_write_conn():
            yield conn

        with mock.patch.object(self.approve, "write_conn", fake_write_conn):
            res = self.approve.answer_question(body)

        self.assertEqual(res.detail["question_id"], "Q01#2", "HTTP 回执保持原 qkey 契约")
        row = conn.execute("select object_id, question_code from agent_assumption").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["object_id"], "kw-2")
        self.assertEqual(row["question_code"], "Q01", "落库必须剥离 #2，保留裸题号")

    def test_answer_and_exclude_with_qkey_write_bare_question_id(self):
        import json
        conn = self._mem()
        body = self.models.QuestionAnswerIn(
            question_id="Q01#2", run_id=10, selected=[0], excluded=[1],
            free_text="词不匹配", skipped=False,
        )

        @__import__("contextlib").contextmanager
        def fake_write_conn():
            yield conn

        with mock.patch.object(self.approve, "write_conn", fake_write_conn):
            res = self.approve.answer_question(body)

        self.assertEqual(res.detail["question_id"], "Q01#2")
        hit = conn.execute("select object_id, scope from reason_hit").fetchone()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object_id"], "kw-2")
        self.assertEqual(hit["scope"], "Q01")

        pol = conn.execute("select object_id, source, params_json from policy").fetchone()
        self.assertIsNotNone(pol)
        self.assertEqual(pol["object_id"], "kw-2")
        self.assertIn("#Q01@run10", pol["source"])
        self.assertEqual(json.loads(pol["params_json"])["question_id"], "Q01")

        exc_sed = [s for s in res.detail["sediment"] if s.get("table") == "reason_excluded"]
        self.assertEqual(len(exc_sed), 1)
        self.assertEqual(exc_sed[0]["question_id"], "Q01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
