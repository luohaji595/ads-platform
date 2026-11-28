#!/usr/bin/env python3
"""冷却期（cooldown_until）的回归测试。纯 unittest。

背景：`cooldown_until` 这个列原来只有写、没有读 ——
`engine/rules.py` 在 R01/R03/R31/R08 四处写了它，全仓零处 SELECT。
后果是「同一个词 72 小时内不重复改 bid」这条频控完全不存在：
实测 `decision_log` 里带 cooldown 的 22 条，21 条 expired、1 条 rejected，
零条被冷却拦住；同一个词 `151878211767486` 在 11-16 一天内出了 4 条
一模一样的 pause 决策（decision_id 2/26/53/80）。

修法有三个必须同时成立的点，任何一点漏掉都等于没修：
  1. 读侧只认 `status='executed'` —— 提案行自己的 cooldown_until 不参与抑制，
     否则同一天第二次跑、或次日还没审批，建议会被自己的冷却期屏蔽掉，
     从待审台（memorial.pack.front）消失，待审台直接变空。
  2. 写侧在「标已改」时按**执行时刻**重算 —— 提案可能放了三天才批，
     沿用提案时写的值等于刚改完就解除冷却。
  3. 只拦「会改动后台」的动作 —— observe / alert 不改任何东西，
     被冷却拦掉只会让人少看一条「我看到了，先不动」。

跑法：`python3 tests/test_cooldown.py`
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.path.join(ROOT, "adsdesk"), os.path.join(ROOT, "engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import facts  # noqa: E402
import rules  # noqa: E402

CST = timezone(timedelta(hours=8))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _schema_db() -> tuple[sqlite3.Connection, str]:
    """按 engine/schema_v2.sql 建一个临时库，再补上 migrate.py 的增量列。

    为什么还要跑 ALTERS：`engine/schema_v2.sql` 落后于生产库 ——
    `executed_by` / `attribution_due_at` / `gate_reason` / `override_*` 这几列
    在生产库里存在，但 schema_v2.sql 里没有，靠 `adsdesk/api/migrate.py` 的
    ALTERS 补。只跑 schema_v2.sql 建出来的库，mark_executed 一写就
    `no such column: executed_by`。这里照生产库的建库路径走一遍。
    """
    path = os.path.join(tempfile.mkdtemp(), "cooldown.sqlite3")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # 两套 schema 都要跑：engine/schema_v2.sql 建引擎侧（decision_log 等），
    # adsdesk/api/schema.sql 建工作台侧（agent_objection / exec_order 等）。
    for _sql in ("engine/schema_v2.sql", "adsdesk/api/schema.sql"):
        conn.executescript((Path(ROOT) / _sql).read_text(encoding="utf-8"))

    import api.migrate as migrate
    for table, col, decl in migrate.ALTERS:
        have = {r["name"] for r in conn.execute(f"pragma table_info({table})")}
        if col not in have:
            conn.execute(f"alter table {table} add column {col} {decl}")
    conn.commit()
    # sqlite3.Connection 不允许挂自定义属性，所以路径单独返回
    # （mark_executed 自己开连接，需要知道库在哪）。
    return conn, path


def _add_decision(conn, *, status: str, cooldown: str | None,
                  entity_type: str = "keyword", entity_id: str = "KW1",
                  action: str = "reduce_bid") -> int:
    cur = conn.execute(
        """insert into decision_log
             (decided_at, rule_id, rule_version, entity_type, entity_id, entity_label,
              market_id, action_type, confidence, context_json, calculation_json,
              significance_json, status, cooldown_until)
           values (?,?,?,?,?,?,?,?,?,'{}','[]','{}',?,?)""",
        (_iso(datetime.now(CST)), "R01", "1.0", entity_type, entity_id, "测试词",
         7, action, "high", status, cooldown),
    )
    conn.commit()
    return int(cur.lastrowid)


class ActiveCooldownsTest(unittest.TestCase):
    """读侧：只有 executed 且未到期的行才算「在冷却」。"""

    def setUp(self):
        self.conn, self.path = _schema_db()
        self.future = _iso(datetime.now(CST) + timedelta(hours=48))
        self.past = _iso(datetime.now(CST) - timedelta(hours=48))

    def tearDown(self):
        self.conn.close()

    def test_executed_and_future_counts(self):
        _add_decision(self.conn, status="executed", cooldown=self.future)
        got = facts.active_cooldowns(self.conn)
        self.assertEqual(got, {"keyword:KW1": self.future})

    def test_executed_but_past_does_not_count(self):
        _add_decision(self.conn, status="executed", cooldown=self.past)
        self.assertEqual(facts.active_cooldowns(self.conn), {})

    def test_proposed_is_ignored(self):
        """最关键的一条：提案行不能被自己的冷却期屏蔽。

        修之前如果按 status in ('proposed','approved','executed') 查，
        同一天第二次跑就会把自己刚提的建议屏蔽掉 —— 待审台变空，
        主人以为系统没建议，实际是被自己拦了。
        """
        _add_decision(self.conn, status="proposed", cooldown=self.future)
        self.assertEqual(facts.active_cooldowns(self.conn), {})

    def test_approved_is_ignored(self):
        """批了但还没改：不算冷却。归因窗从「已改」起算，冷却也该从「已改」起算。"""
        _add_decision(self.conn, status="approved", cooldown=self.future)
        self.assertEqual(facts.active_cooldowns(self.conn), {})

    def test_null_cooldown_is_ignored(self):
        _add_decision(self.conn, status="executed", cooldown=None)
        self.assertEqual(facts.active_cooldowns(self.conn), {})

    def test_in_cooldown_lookup(self):
        _add_decision(self.conn, status="executed", cooldown=self.future)
        f = facts.Facts(
            start="", end="", window_days=14, target_acos=0.35,
            account={}, cvr_baseline={}, breakeven={}, min_clicks={},
            cpc_bias={}, floor={}, placements=[], campaign_placements=[],
            keywords=[], searchterms=[], campaigns=[],
            cooldown_until=facts.active_cooldowns(self.conn),
        )
        self.assertEqual(f.in_cooldown("keyword", "KW1"), self.future)
        self.assertIsNone(f.in_cooldown("keyword", "OTHER"))
        self.assertIsNone(f.in_cooldown("campaign", "KW1"))


class ParseTsTest(unittest.TestCase):
    """库里两种时间格式混着：老行 naive（CST 本地）、新行带 +08:00、engine_run 带 Z。"""

    def test_naive_is_read_as_cst(self):
        dt = facts.parse_ts("2026-11-19T14:17:11")
        self.assertEqual(dt.utcoffset(), timedelta(hours=8))
        self.assertEqual(dt.hour, 14)

    def test_offset_preserved(self):
        dt = facts.parse_ts("2026-11-26T15:30:00+08:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=8))
        self.assertEqual(dt.hour, 15)

    def test_z_suffix(self):
        dt = facts.parse_ts("2026-11-26T07:30:00Z")
        self.assertEqual(dt.utcoffset(), timedelta(0))
        # 07:30Z == 15:30+08:00
        self.assertEqual(dt.astimezone(CST).hour, 15)

    def test_garbage_returns_none(self):
        self.assertIsNone(facts.parse_ts(""))
        self.assertIsNone(facts.parse_ts(None))
        self.assertIsNone(facts.parse_ts("not-a-time"))


class RuleCooldownGateTest(unittest.TestCase):
    """R01 遇到冷却期要让路；但只拦「会改动」的动作。"""

    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.conn = conn
        self.future = _iso(datetime.now(CST) + timedelta(hours=48))
        self.base = facts.Facts(
            start="2026-11-10", end="2026-11-23", window_days=14, target_acos=0.35,
            account={"aov": 187.99, "orders": 4, "cpa": 163.81, "clicks": 881,
                     "cost": 780.01, "sales": 756.0, "cvr": 0.0045,
                     "cpc": 0.885, "acos": 1.0317, "impressions": 150000},
            account_wide={"cvr": 0.0045, "cpc": 0.885, "aov": 187.99,
                          "acos": 1.0317, "clicks": 881, "cost": 780.01,
                          "orders": 4, "sales": 756.0},
            breakeven_wide={"breakeven_cvr": 0.0156},
            cvr_baseline={"cvr": 0.0045},
            breakeven={"breakeven_cvr": 0.0156},
            min_clicks={"reduce": 100, "raise": 200},
            cpc_bias={"bias": 1.0, "campaign_adj": {}},
            floor={"nominal": 0.80, "effective": 0.80},
            placements=[], campaign_placements=[],
            keywords=[], searchterms=[], campaigns=[],
            cost_ceiling={}, rank_targets=[],
        )
        self.kw = {
            "keyword_id": "KW1", "keyword_text": "测试词", "market_id": 7,
            "campaign_id": "C1", "ad_group_id": "A1",
            "clicks": 1000, "orders": 2, "cost": 600.0, "sales": 300.0,
            "bid": 1.0, "cvr": 0.002, "acos": 2.0, "impressions": 20000,
            "cpc": 0.60, "cpa": 300.0, "match_type": "exact",
        }

    def _run(self, cooldown_map: dict, evidence: dict):
        f = dataclasses.replace(self.base, keywords=[self.kw],
                                cooldown_until=cooldown_map)
        with mock.patch.object(rules, "_evidence", return_value=evidence):
            return rules.rule_01_reduce_bid(f)

    def test_change_action_is_suppressed(self):
        ev = {"confidence": "high", "p_worse": 0.001, "p_better": 0.9,
              "need_for_orders": None}
        free = self._run({}, ev)
        self.assertTrue(free.decisions, "无冷却时应该出建议（否则这条测试没意义）")

        blocked = self._run({"keyword:KW1": self.future}, ev)
        self.assertEqual(blocked.decisions, [], "冷却期内不该再出改动类建议")
        reasons = [s["reason"] for s in blocked.suppressed]
        self.assertIn("冷却期内", reasons)

    def test_observe_is_not_suppressed(self):
        """observe 不改后台，冷却不该拦它 —— 拦掉只是让人少看一条信息。"""
        ev = {"confidence": "low", "p_worse": 0.5, "p_better": 0.5,
              "need_for_orders": None}
        free = self._run({}, ev)
        self.assertTrue(free.decisions)
        self.assertEqual(free.decisions[0].action_type, "observe")

        still = self._run({"keyword:KW1": self.future}, ev)
        self.assertTrue(still.decisions, "observe 被冷却拦掉了")
        self.assertEqual(still.decisions[0].action_type, "observe")


class R08R09CooldownGateTest(unittest.TestCase):
    """R08（加预算）与 R09（调位次加价）也必须受冷却管。

    这个缺口比 R01 那处更隐蔽：`COOLDOWN_ACTIONS` 里一直列着 raise_budget 与
    adjust_placement，读侧 `mark_executed` 也会照着写冷却，但**写侧从来没有
    写过 cooldown_until** —— 实测（2026-11-26）生产库 6 条 raise_budget、
    10 条 adjust_placement 决策全是 NULL。也就是这两类动作的频控从未生效过一次。
    「名单里有、实现里没有」比「干脆不在名单里」更坏：读代码的人会以为它在。
    """

    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.conn = conn
        self.future = _iso(datetime.now(CST) + timedelta(hours=48))
        self.base = facts.Facts(
            start="2026-11-10", end="2026-11-23", window_days=14, target_acos=0.35,
            account={"aov": 60.0, "orders": 50, "cpa": 30.0, "clicks": 1000,
                     "cost": 1500.0, "sales": 3000.0, "cvr": 0.05,
                     "cpc": 1.5, "acos": 0.5, "impressions": 20000},
            account_wide={"cvr": 0.05, "cpc": 1.5, "aov": 60.0, "acos": 0.5,
                          "clicks": 1000, "cost": 1500.0, "orders": 50,
                          "sales": 3000.0},
            breakeven_wide={"breakeven_cvr": 0.05},
            cvr_baseline={"cvr": 0.05},
            breakeven={"breakeven_cvr": 0.05},
            min_clicks={"reduce": 100, "raise": 200},
            cpc_bias={"bias": 1.0, "campaign_adj": {}},
            floor={"nominal": 0.80, "effective": 0.80},
            placements=[], campaign_placements=[],
            keywords=[], searchterms=[], campaigns=[],
            cost_ceiling={}, rank_targets=[],
        )
        # 卡预算 + 表现好 + 点击够 → R08 会出 raise_budget
        self.camp = {
            "campaign_id": "C1", "campaign_name": "测试活动", "state": "ENABLED",
            "budget_util": 0.95, "daily_budget": 50.0, "avg_daily_cost": 47.5,
            "cost": 665.0, "clicks": 800, "orders": 60, "sales": 1800.0,
            "cpa": 11.08, "roas": 2.71, "market_id": 7,
            "portfolio_name": "测试组合", "is_auto": False,
        }
        # 占比够 + ROAS 好 → R09 会出 adjust_placement
        self.cp = {
            "campaign_id": "C1", "campaign_name": "测试活动",
            "placement": "Top of Search on-Amazon",
            "bid_adjustment": 100.0, "bid_adjustment_name": "100%",
            "campaign_roas": 2.0, "campaign_cost": 665.0,
            "cost": 400.0, "cost_share_of_campaign": 0.60,
            "clicks": 600, "orders": 40, "roas": 2.8, "cpa": 10.0, "sales": 1120.0,
        }

    def _facts(self, cooldown_map=None, weak_placement=False):
        cp = dict(self.cp)
        if weak_placement:
            # 点击太少 → R09 会降级成 observe
            cp.update(clicks=30, orders=2)
        return dataclasses.replace(
            self.base, campaigns=[self.camp], campaign_placements=[cp],
            cooldown_until=cooldown_map or {})

    def tearDown(self):
        self.conn.close()

    # ---- R08 --------------------------------------------------------------

    def test_r08_writes_cooldown_on_decision(self):
        """R08 出的 raise_budget 必须自带 cooldown_until，否则读侧无从下手。"""
        res = rules.rule_08_budget(self._facts())
        self.assertTrue(res.decisions, "无冷却时 R08 应该出建议（否则这条测试没意义）")
        d = res.decisions[0]
        self.assertEqual(d.action_type, "raise_budget")
        self.assertIsNotNone(d.cooldown_until, "raise_budget 没写 cooldown_until")
        cd = facts.parse_ts(d.cooldown_until)
        self.assertIsNotNone(cd)
        self.assertGreater(cd, datetime.now(CST) + timedelta(hours=71))

    def test_r08_is_suppressed_in_cooldown(self):
        res = rules.rule_08_budget(self._facts({"campaign:C1": self.future}))
        self.assertEqual(res.decisions, [], "冷却期内不该再出加预算建议")
        self.assertIn("冷却期内", [s["reason"] for s in res.suppressed])

    # ---- R09 --------------------------------------------------------------

    def test_r09_writes_cooldown_on_decision(self):
        res = rules.rule_09_placement(self._facts())
        self.assertTrue(res.decisions, "无冷却时 R09 应该出建议")
        d = res.decisions[0]
        self.assertEqual(d.action_type, "adjust_placement")
        self.assertIsNotNone(d.cooldown_until, "adjust_placement 没写 cooldown_until")

    def test_r09_is_suppressed_in_cooldown(self):
        """位次加价的冷却键必须是「活动::位次」，不能退化成活动级。"""
        res = rules.rule_09_placement(
            self._facts({"campaign_placement:C1::Top of Search on-Amazon": self.future}))
        self.assertEqual(res.decisions, [], "冷却期内不该再出调位次建议")
        self.assertIn("冷却期内", [s["reason"] for s in res.suppressed])

    def test_r09_cooldown_key_is_placement_scoped(self):
        """给同一个活动的**另一个**位次设冷却，不该误伤这一条。"""
        res = rules.rule_09_placement(
            self._facts({"campaign_placement:C1::Product pages": self.future}))
        self.assertTrue(res.decisions, "别的位次的冷却把这条也拦了 —— 键粒度错了")

    def test_r09_observe_is_not_suppressed(self):
        """证据不足降级成 observe 后，冷却不该拦 —— observe 不改后台。"""
        res = rules.rule_09_placement(
            self._facts({"campaign_placement:C1::Top of Search on-Amazon": self.future},
                        weak_placement=True))
        self.assertTrue(res.decisions, "observe 被冷却误拦了")
        self.assertEqual(res.decisions[0].action_type, "observe")
        self.assertIsNone(res.decisions[0].cooldown_until)
        self.assertNotIn("冷却期内", [s["reason"] for s in res.suppressed])


class ReuseKeepsCooldownTest(unittest.TestCase):
    """复用路径必须把新的 cooldown_until 刷进库。

    `engine/run.py` 的 `save_decisions` 有一条「同一条建议还在等你，就复用
    decision_id、就地刷新内容」的分支。它的 UPDATE 子句原来漏了 cooldown_until，
    于是只有第一次 INSERT 写得进冷却；之后每次复用都把旧值留在库里。
    后果比「完全不写冷却」更隐蔽：`mark_executed` 是拿「这行有没有 cooldown_until」
    判断「这类动作受不受频控管」的，复用一旦丢掉新值，那条判据就永远为假。
    """

    def setUp(self):
        self.conn, _ = _schema_db()
        self.old = "2026-11-01T00:00:00"
        self.new = "2026-12-02T00:00:00+08:00"

    def tearDown(self):
        self.conn.close()

    def _decision(self, cooldown: str | None, proposed: str = "0.80"):
        return rules.Decision(
            rule_id="R01", rule_version="1.0", entity_type="keyword",
            entity_id="KW-REUSE", entity_label="复用测试词", action_type="reduce_bid",
            current_value="1.00", proposed_value=proposed, confidence="high",
            context={}, calculation=[], significance={}, rationale="复用刷新测试",
            spend_impact=1.0, cooldown_until=cooldown, expires_at=self.new,
        )

    def test_reuse_refreshes_cooldown(self):
        import run as engine_run
        did = _add_decision(self.conn, status="proposed", cooldown=self.old,
                            entity_type="keyword", entity_id="KW-REUSE")
        # 用一个没出现过的 run_id，强制走 live（复用）分支而不是同 run 去重
        res = engine_run.save_decisions(self.conn, 99999, [self._decision(self.new)])
        self.assertEqual(res["reused"], 1, "没走到复用分支，这个测试就没意义")
        row = self.conn.execute(
            "select cooldown_until, proposed_value from decision_log where decision_id=?",
            (did,)).fetchone()
        self.assertEqual(row["cooldown_until"], self.new, "复用路径把冷却时间丢掉了")
        self.assertEqual(row["proposed_value"], "0.80")

    def test_reuse_can_clear_cooldown(self):
        """规则不再给冷却时（比如降级成 observe），复用也要能把旧值清掉。

        否则一个词只要进过一次冷却，之后所有建议都会一直带着那个时间戳。
        """
        import run as engine_run
        did = _add_decision(self.conn, status="proposed", cooldown=self.old,
                            entity_type="keyword", entity_id="KW-REUSE")
        engine_run.save_decisions(self.conn, 99999, [self._decision(None)])
        row = self.conn.execute(
            "select cooldown_until from decision_log where decision_id=?",
            (did,)).fetchone()
        self.assertIsNone(row["cooldown_until"])


class MarkExecutedRestampsTest(unittest.TestCase):
    """写侧：标「已改」时按执行时刻重算冷却，不沿用提案时写的旧值。"""

    def setUp(self):
        self.conn, self.path = _schema_db()
        import api.db as dbmod
        self.dbmod = dbmod
        self._patch = mock.patch.object(dbmod, "DB_PATH", Path(self.path))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.conn.close()

    def test_cooldown_is_recomputed_from_execution_time(self):
        from api.models import ExecutedIn
        from api.routes import approve

        # 提案时写的冷却早就过去了（模拟「三天前提的、今天才批」）
        stale = _iso(datetime.now(CST) - timedelta(hours=1))
        did = _add_decision(self.conn, status="approved", cooldown=stale)

        before = datetime.now(CST)
        approve.mark_executed(ExecutedIn(decision_id=did))

        row = self.conn.execute(
            "select status, cooldown_until from decision_log where decision_id=?",
            (did,)).fetchone()
        self.assertEqual(row["status"], "executed")
        new_cd = facts.parse_ts(row["cooldown_until"])
        self.assertIsNotNone(new_cd)
        # 应该落在「现在 + 72h」附近，而不是沿用那个已经过期的值
        self.assertGreater(new_cd, before + timedelta(hours=71))
        self.assertLess(new_cd, datetime.now(CST) + timedelta(hours=73))

    def test_action_without_cooldown_gets_none(self):
        """R03 在预算打满时提的是提示（raise_bid_blocked），不带冷却；执行后也不该凭空长出冷却。"""
        from api.models import ExecutedIn
        from api.routes import approve

        did = _add_decision(self.conn, status="approved", cooldown=None,
                            action="raise_bid_blocked")
        approve.mark_executed(ExecutedIn(decision_id=did))
        row = self.conn.execute(
            "select cooldown_until from decision_log where decision_id=?",
            (did,)).fetchone()
        self.assertIsNone(row["cooldown_until"])


class R31RankStepDownCooldownGateTest(unittest.TestCase):
    """R31 rank_step_down 的冷却判定必须在 push_mode=='bid' 与空转检查之后。"""

    def setUp(self):
        self.future = _iso(datetime.now(CST) + timedelta(hours=48))
        self.base = facts.Facts(
            start="2026-11-10", end="2026-11-23", window_days=14, target_acos=0.30,
            account={"aov": 180.0, "orders": 20, "cpa": 20.0, "clicks": 500,
                     "cost": 400.0, "sales": 3600.0, "cvr": 0.04,
                     "cpc": 0.80, "acos": 0.11, "impressions": 50000},
            account_wide={"cvr": 0.04, "cpc": 0.80, "aov": 180.0,
                          "acos": 0.11, "clicks": 500, "cost": 400.0,
                          "orders": 20, "sales": 3600.0},
            breakeven_wide={"breakeven_cvr": 0.015},
            cvr_baseline={"cvr": 0.04},
            breakeven={"breakeven_cvr": 0.015},
            min_clicks={"reduce": 100, "raise": 200},
            cpc_bias={"bias": 1.0, "campaign_adj": {}},
            floor={"nominal": 0.40, "effective": 0.40},
            placements=[], campaign_placements=[],
            keywords=[{
                "keyword_id": "KW-R31", "keyword_text": "卡位测试词", "market_id": 7,
                "campaign_id": "C1", "ad_group_id": "A1",
                "clicks": 100, "orders": 10, "cost": 50.0, "sales": 1000.0,
                "bid": 0.50, "cvr": 0.10, "acos": 0.05, "impressions": 5000,
                "cpc": 0.50, "cpa": 5.0, "match_type": "exact",
            }],
            searchterms=[], campaigns=[],
            cost_ceiling={},
            rank_state={
                "KW-R31": {
                    "observations": 6,
                    "stale": False,
                    "age_hours": 1.0,
                    "self_competing": False,
                    "pushes_used": 0,
                    "hit": True,
                    "streak": 5,
                    "batches": 6,
                    "latest": {"observed_at": "2026-11-23 12:00:00", "page": 1, "position": 2,
                               "asin": "B0TEST", "postal_code": "10001"},
                }
            },
        )

    def _target(self, **kw):
        t = dict(
            keyword_id="KW-R31", keyword_text="卡位测试词", campaign_id="C1",
            ad_group_id="A1", asin="B0TEST", goal_page=1, goal_position=4,
            push_mode="bid", step_mode="fixed", step_value=0.10,
            max_bid=2.0, max_tos_pct=900.0, acos_guard_mult=1.5,
            max_pushes=6, success_streak_required=5,
            decrease_mode="percent", decrease_value=5.0,
        )
        t.update(kw)
        return t

    def test_step_down_is_suppressed_in_cooldown(self):
        f = dataclasses.replace(
            self.base,
            rank_targets=[self._target()],
            cooldown_until={"keyword:KW-R31": self.future},
        )
        res = rules.rule_31_rank_position_push(f)
        self.assertEqual(res.decisions, [])
        self.assertEqual([s["reason"] for s in res.suppressed], ["冷却期内"])

    def test_noop_beats_cooldown(self):
        """降价幅度 < 0.01 时应报「空转」，不能被前置冷却误报成「冷却期内」。"""
        f = dataclasses.replace(
            self.base,
            rank_targets=[self._target(decrease_mode="fixed", decrease_value=0.001)],
            cooldown_until={"keyword:KW-R31": self.future},
        )
        res = rules.rule_31_rank_position_push(f)
        self.assertEqual(res.decisions, [])
        self.assertEqual([s["reason"] for s in res.suppressed], ["空转"])

    def test_tos_mode_beats_cooldown(self):
        """TOS 模式未实现降价应报「TOS 模式降价未实现」，不能被误报成「冷却期内」。"""
        f = dataclasses.replace(
            self.base,
            rank_targets=[self._target(push_mode="tos")],
            cooldown_until={"keyword:KW-R31": self.future},
        )
        res = rules.rule_31_rank_position_push(f)
        self.assertEqual(res.decisions, [])
        self.assertEqual([s["reason"] for s in res.suppressed], ["TOS 模式降价未实现"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
