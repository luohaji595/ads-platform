#!/usr/bin/env python3
"""「失败必须看得见」的四条回归。

背景（2026-11-26 横向审计）：这一轮修的大多数 bug 有一个共同形状 ——
**代码其实知道出事了，但把这个信息丢在了半路上**。失败被写成和「还没发生」
一样的值，人就分不清「去修」和「等它跑」。

四条各自独立，一条一个文件边界：

  A4 工作台 /active 不看 expires_at  → 人照着引擎不认的规矩做决定
  A5 converge 失败记 degraded，chain 只认 ok → 「跑挂了」显示成「还没跑」
  A6 diagnose 的 impact/uncertainty 无边界 → 前端能显示「影响 $999999999」
  A7 attribute 不认识对象类型就借关键词表名 → 断言指错对象

跑：python3 tests/test_visible_failures.py
"""
from __future__ import annotations

import importlib.util
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    """按路径加载模块。

    不能用 `sys.path.insert`：那会让顶层名 `ingest` 被 `ingest/ingest.py` 抢走，
    `from ingest import jijia_client` 直接 ImportError。
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ A4

class PolicyExpiryTest(unittest.TestCase):
    """工作台列「当前规矩」必须和引擎的执行口径一致。"""

    def setUp(self):
        self.pol = _load("policy_under_test", "adsdesk/api/routes/policy.py")

    @staticmethod
    def _row(pid, exp):
        # _row() 要的列名与真实 select 一致；这里只造它读的那几个。
        return {"policy_id": pid, "scope": "object", "object_id": "X", "policy_type": "t",
                "params_json": "{}", "source": "s", "evidence_text": None,
                "status": "active", "created_at": "2026-11-01T00:00:00+08:00",
                "reviewed_at": "2026-11-02T00:00:00+08:00", "expires_at": exp}

    def test_expired_goes_to_its_own_bucket(self):
        rows = [self._row(1, None), self._row(2, "2020-01-01"), self._row(3, "2027-01-01")]
        items, expired = self.pol.split_by_expiry(rows, "2026-11-27")
        self.assertEqual([p["policy_id"] for p in items], [1, 3])
        self.assertEqual([p["policy_id"] for p in expired], [2])

    def test_null_expiry_never_expires(self):
        # agent 生成的策略从不带 expires_at（attribute.py 的 INSERT 列表没这一列），
        # 所以 NULL 必须是「永久有效」，不能被当成「已过期」。
        items, expired = self.pol.split_by_expiry([self._row(1, None)], "2099-01-01")
        self.assertEqual([p["policy_id"] for p in items], [1])
        self.assertEqual(expired, [])

    def test_expiry_day_itself_still_counts(self):
        # 引擎是 `expires_at >= today`，到期当天还算生效。
        # 这里若用 `<` 改写成 `<=`，会有一天两边的答案不一样。
        items, _ = self.pol.split_by_expiry([self._row(1, "2026-11-27")], "2026-11-27")
        self.assertEqual(len(items), 1)

    def test_timestamp_form_compares_by_day(self):
        # 库里既有 `2026-11-27` 也有 `2026-11-27T00:00:00+08:00`，都按日前 10 位比。
        items, expired = self.pol.split_by_expiry(
            [self._row(1, "2026-11-26T23:59:59+08:00")], "2026-11-27")
        self.assertEqual(items, [])
        self.assertEqual([p["policy_id"] for p in expired], [1])


# ------------------------------------------------------------------ A5

class ChainFailureVisibilityTest(unittest.TestCase):
    """跑过但失败，不能显示成「还没跑」。"""

    def setUp(self):
        self.chain = _load("chain_under_test", "adsdesk/chain.py")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # step_done 只用这两张表的少量列，建最小的就够。
        self.conn.executescript("""
            create table chain_run (business_day text, step text, status text, finished_at text);
            create table converge_run (
                converge_run_id integer primary key, started_at text, finished_at text,
                status text, n_due integer, n_filled integer, n_correct integer,
                n_wrong integer, n_unverifiable integer, n_deferred integer,
                n_objection_due integer, n_objection_judged integer, model text, error text);
        """)
        self.conn.execute(
            "insert into chain_run values ('2026-11-27','ingest','ok','2026-11-27T09:38:00+08:00')")
        self.conn.commit()

    def _add(self, status, error=None):
        self.conn.execute(
            "insert into converge_run (started_at, finished_at, status, n_due, n_filled, "
            "n_correct, n_wrong, n_unverifiable, n_deferred, n_objection_due, "
            "n_objection_judged, model, error) values (?,?,?,0,0,0,0,0,0,0,0,'m',?)",
            ("2026-11-27T09:50:00+08:00", "2026-11-27T09:52:00+08:00", status, error))
        self.conn.commit()

    def test_degraded_says_it_ran_and_failed(self):
        self._add("degraded", "网关 600s 硬截止")
        ok, why = self.chain.step_done(self.conn, "converge", "2026-11-27")
        self.assertFalse(ok)
        self.assertIn("degraded", why)
        self.assertIn("网关 600s 硬截止", why)
        self.assertNotIn("还没有", why)          # 关键：不能说成「还没跑」

    def test_no_row_at_all_says_not_yet(self):
        ok, why = self.chain.step_done(self.conn, "converge", "2026-11-27")
        self.assertFalse(ok)
        self.assertIn("还没有", why)

    def test_ok_run_still_passes(self):
        self._add("ok")
        ok, why = self.chain.step_done(self.conn, "converge", "2026-11-27")
        self.assertTrue(ok, why)
        self.assertIn("晚于取数", why)

    def test_failed_then_ok_uses_latest(self):
        # 先挂后成：最新一条是 ok 就该放行，不能被早上的失败卡住一整天。
        self._add("degraded", "第一次挂了")
        self._add("ok")
        ok, why = self.chain.step_done(self.conn, "converge", "2026-11-27")
        self.assertTrue(ok, why)


# ------------------------------------------------------------------ A6

class DiagnoseNumberBoundsTest(unittest.TestCase):
    """模型给的数字越界就当没给 —— 越界钳位会造出「看起来算过」的假数字。"""

    def setUp(self):
        self.diag = _load("diagnose_under_test", "adsdesk/agents/diagnose.py")

    def test_impact_over_ceiling_is_dropped(self):
        self.assertIsNone(self.diag._num("999999999", lo=0.0, hi=1e6))

    def test_uncertainty_over_one_is_dropped(self):
        self.assertIsNone(self.diag._num("42", lo=0.0, hi=1.0))

    def test_negative_impact_is_dropped(self):
        self.assertIsNone(self.diag._num("-1", lo=0.0, hi=1e6))

    def test_nan_and_inf_are_dropped(self):
        # float('nan') 能过 `try: float()` 这一关，但它不是数。
        self.assertIsNone(self.diag._num("nan"))
        self.assertIsNone(self.diag._num("inf"))
        self.assertIsNone(self.diag._num("-inf"))

    def test_normal_values_survive(self):
        self.assertEqual(self.diag._num("0.3", lo=0.0, hi=1.0), 0.3)
        self.assertEqual(self.diag._num("120.5", lo=0.0, hi=1e6), 120.5)
        self.assertEqual(self.diag._num(0, lo=0.0, hi=1.0), 0.0)

    def test_unparseable_is_still_none(self):
        for bad in ("", None, "abc", [], {}):
            self.assertIsNone(self.diag._num(bad))


# ------------------------------------------------------------------ A7

class AttributeNoWrongTableTest(unittest.TestCase):
    """不认识的 entity_type 不写断言 —— method 文本会被 converge 拿去抽实体。"""

    def setUp(self):
        self.attr = _load("attribute_under_test", "adsdesk/agents/attribute.py")

    @staticmethod
    def _payload():
        return {"after": {"clicks": 200, "orders": 4,
                          "start": "2026-11-21", "end": "2026-11-27"},
                "account": {"after": {"cvr": 0.02}},
                "window_days": 7, "verdict": "improved"}

    def _d(self, entity_type):
        return {"entity_type": entity_type, "entity_id": "X1",
                "decision_id": 999, "executed_at": "2026-11-20T10:00:00"}

    def test_unknown_entity_type_emits_no_claim(self):
        out = self.attr._predicts(self._d("unknown_thing"), self._payload(),
                                  _now())
        self.assertEqual(out, [])

    def test_known_types_still_emit_claim(self):
        for et in ("keyword", "campaign", "campaign_placement"):
            out = self.attr._predicts(self._d(et), self._payload(), _now())
            self.assertEqual(len(out), 1, et)
            # 表名必须来自 _ENTITY_TABLE，不能是写死的 fact_keyword_daily
            want = self.attr._ENTITY_TABLE[et][0]
            self.assertIn(want, out[0]["method"])

    def test_placement_does_not_borrow_keyword_table(self):
        out = self.attr._predicts(self._d("campaign_placement"), self._payload(), _now())
        self.assertIn("fact_placement_daily", out[0]["method"])
        self.assertNotIn("fact_keyword_daily", out[0]["method"])


def _now():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
