#!/usr/bin/env python3
"""配置快照的结构一致性（2026-11-26 修的两条）。

背景
----
`config_snapshot` 有三份结构定义，任何一份对不上就炸：

  1. `engine/schema_v2.sql` —— 全新库建表用的
  2. `engine/snapshot.py:ensure_snapshot_schema()` 分支 A —— 老库（无 snap_ts）整表重建
  3. 同函数分支 B —— 半迁移库（有 snap_ts，主键缺 market_id）再升一次

`take_snapshot()` 里的写入是

    ON CONFLICT(snap_ts, snap_date, entity_type, entity_id, market_id, field)

SQLite 要求这串列必须**精确匹配**某个 PRIMARY KEY 或 UNIQUE 约束。
2026-11-26 实测到两个真缺陷：

  A. `schema_v2.sql` 里还是旧结构（10 列、主键 4 列、没有 snap_ts），
     分支 A 迁移出来主键也是 5 列 —— 少了 market_id。于是**全新库第一次
     `take_snapshot()` 必崩**：
     `OperationalError: ON CONFLICT clause does not match any PRIMARY KEY`。
     崩在「刚接新店的第一天」，最难排查的时刻。

  B. 分支 A 是裸 DDL + 裸 INSERT，没有事务。sqlite3 模块只在 DML 前隐式开
     事务，DDL 自动提交 —— RENAME 成功、INSERT 失败（比如老库里有一行
     entity_id 是 NULL，新表 NOT NULL）就留下「有 _config_snapshot_legacy、
     没有 config_snapshot」的半截库，下次启动直接炸。

这两条修完后固化成下面的断言。

跑：python3 tests/test_snapshot_schema.py
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"

_spec = importlib.util.spec_from_file_location("snapshot_mod", ENGINE / "snapshot.py")
snapshot = importlib.util.module_from_spec(_spec)
sys.modules["snapshot_mod"] = snapshot
_spec.loader.exec_module(snapshot)

PK = ["snap_ts", "snap_date", "entity_type", "entity_id", "market_id", "field"]


def _conn(path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or ":memory:")
    c.row_factory = sqlite3.Row
    return c


def _full_schema(path: str) -> sqlite3.Connection:
    """按 run.py 的真实顺序建库：先 ingest/schema.sql，再 engine/schema_v2.sql。"""
    c = _conn(path)
    c.executescript((ROOT / "ingest" / "schema.sql").read_text())
    c.executescript((ENGINE / "schema_v2.sql").read_text())
    c.commit()
    return c


def _add_campaign(c: sqlite3.Connection, cid: str = "C1", **kw) -> None:
    vals = dict(
        market_id=7, report_date="2026-11-25", campaign_id=cid, campaign_name="测试活动",
        impressions=100, clicks=10, cost=5.0, ads_orders=1, ads_sales=20.0,
        daily_budget=50.0, campaign_state="enabled", ingested_at="2026-11-26T01:00:00Z",
    )
    vals.update(kw)
    cols = ",".join(vals)
    qs = ",".join("?" * len(vals))
    # 事实表主键是 (market_id, report_date, campaign_id)，数据变新是 UPDATE 不是 INSERT
    c.execute(
        f"insert into fact_campaign_daily ({cols}) values ({qs}) "
        f"on conflict(market_id, report_date, campaign_id) do update set "
        + ",".join(f"{k}=excluded.{k}" for k in vals if k not in
                   ("market_id", "report_date", "campaign_id")),
        tuple(vals.values()),
    )
    c.commit()


class FreshDatabaseTest(unittest.TestCase):
    """A. 全新库：schema_v2.sql 建出来的表必须能直接接住 take_snapshot。"""

    def setUp(self):
        self.path = "/tmp/_snap_fresh_test.sqlite3"
        Path(self.path).unlink(missing_ok=True)
        self.conn = _full_schema(self.path)

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_schema_v2_pk_matches_insert_conflict_target(self):
        """建表脚本的主键必须和 take_snapshot 的 ON CONFLICT 逐列一致。

        这一条如果挂了，症状是 `OperationalError: ON CONFLICT clause does not
        match any PRIMARY KEY` —— 全新库第一次快照就崩。
        """
        self.assertEqual(snapshot._pk_columns(self.conn, "config_snapshot"), PK)

    def test_schema_v2_has_snap_ts(self):
        """旧结构没有 snap_ts，导致 ensure_snapshot_schema 把新库当老库迁。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(config_snapshot)")}
        self.assertIn("snap_ts", cols)
        self.assertIn("legacy", cols)
        self.assertEqual(len(cols), 12)

    def test_first_take_snapshot_on_fresh_db(self):
        """全新库第一次快照必须成功（原代码在这里抛 OperationalError）。"""
        _add_campaign(self.conn)
        r = snapshot.take_snapshot(self.conn)
        self.assertEqual(r["snap_date"], "2026-11-25")
        self.assertEqual(r["snap_ts"], "2026-11-26T01:00:00Z")
        self.assertGreater(r["rows"], 0)
        self.assertEqual(r["written"], r["rows"])
        n = self.conn.execute("select count(*) c from config_snapshot").fetchone()["c"]
        self.assertEqual(n, r["rows"])

    def test_second_snapshot_is_noop(self):
        """同一批数据重复快照不新增行、不刷 captured_at。"""
        _add_campaign(self.conn)
        first = snapshot.take_snapshot(self.conn)
        second = snapshot.take_snapshot(self.conn)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["rows"], first["rows"])
        self.assertIn("跳过", second["note"])

    def test_new_ingest_makes_new_observation_point(self):
        """数据变新（ingested_at 变了）要产生第二个观测点，供 diff 用。"""
        _add_campaign(self.conn)
        snapshot.take_snapshot(self.conn)
        _add_campaign(self.conn, daily_budget=60.0, ingested_at="2026-11-26T02:00:00Z")
        r2 = snapshot.take_snapshot(self.conn)
        self.assertEqual(r2["snap_ts"], "2026-11-26T02:00:00Z")
        self.assertGreater(r2["written"], 0)
        pts = {row["snap_ts"] for row in
               self.conn.execute("select distinct snap_ts from config_snapshot")}
        self.assertEqual(pts, {"2026-11-26T01:00:00Z", "2026-11-26T02:00:00Z"})


class LegacyMigrationTest(unittest.TestCase):
    """B. 老库迁移：最老结构（无 snap_ts）和半迁移结构（有 snap_ts 缺 market_id）。"""

    def test_oldest_structure_migrates_to_six_column_pk(self):
        c = _conn()
        c.executescript("""
            create table config_snapshot (
                snap_date TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                market_id INTEGER, campaign_id TEXT, ad_group_id TEXT, entity_label TEXT,
                field TEXT NOT NULL, value TEXT, captured_at TEXT NOT NULL,
                PRIMARY KEY (snap_date, entity_type, entity_id, field));
            create index ix_snap_entity on config_snapshot(entity_type, entity_id, snap_date);
        """)
        for i in range(5):
            c.execute("insert into config_snapshot values(?,?,?,?,?,?,?,?,?,?)",
                      ("2026-11-20", "campaign", f"C{i}", 7, f"C{i}", None, f"活动{i}",
                       "daily_budget", "50.0", "2026-11-20T10:00:00+08:00"))
        c.commit()

        self.assertTrue(snapshot.ensure_snapshot_schema(c))
        self.assertEqual(snapshot._pk_columns(c, "config_snapshot"), PK)
        self.assertEqual(c.execute("select count(*) c from config_snapshot").fetchone()["c"], 5)
        row = c.execute("select * from config_snapshot limit 1").fetchone()
        self.assertEqual(row["legacy"], 1)
        self.assertEqual(row["snap_ts"], "2026-11-20T00:00:00Z")
        leftover = [r["name"] for r in c.execute(
            "select name from sqlite_master where name like '%legacy%' or name like '%_v1'")]
        self.assertEqual(leftover, [])
        self.assertFalse(snapshot.ensure_snapshot_schema(c), "二次调用应幂等返回 False")
        c.close()

    def test_half_migrated_structure_adds_market_id_to_pk(self):
        """有 snap_ts 但主键 5 列（缺 market_id）—— 上一轮修过的那条分支。"""
        c = _conn()
        c.executescript("""
            create table config_snapshot (
                snap_ts TEXT NOT NULL, snap_date TEXT NOT NULL,
                legacy INTEGER NOT NULL DEFAULT 0, entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL, market_id INTEGER, campaign_id TEXT,
                ad_group_id TEXT, entity_label TEXT, field TEXT NOT NULL,
                value TEXT, captured_at TEXT NOT NULL,
                PRIMARY KEY (snap_ts, snap_date, entity_type, entity_id, field));
        """)
        c.execute("insert into config_snapshot values(?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("2026-11-25T01:00:00Z", "2026-11-25", 0, "campaign", "C1", 7,
                   "C1", None, "活动1", "daily_budget", "50", "2026-11-25T09:00:00+08:00"))
        c.commit()

        self.assertTrue(snapshot.ensure_snapshot_schema(c))
        self.assertEqual(snapshot._pk_columns(c, "config_snapshot"), PK)
        self.assertEqual(c.execute("select count(*) c from config_snapshot").fetchone()["c"], 1)
        self.assertFalse(snapshot.ensure_snapshot_schema(c))
        c.close()

    def test_failed_migration_leaves_database_intact(self):
        """迁移中途失败必须整体回滚 —— 不能留「有 legacy、没 config_snapshot」的半截库。

        构造：老表 entity_id 可空，塞一行 NULL。新表 entity_id NOT NULL，
        于是 INSERT 必然失败，正好检验事务。
        """
        c = _conn()
        c.executescript("""
            create table config_snapshot (
                snap_date TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
                market_id INTEGER, campaign_id TEXT, ad_group_id TEXT, entity_label TEXT,
                field TEXT NOT NULL, value TEXT, captured_at TEXT NOT NULL);
        """)
        c.execute("insert into config_snapshot values('2026-11-20','campaign','C1',7,'C1',NULL,'x','daily_budget','50','2026-11-20T10:00:00+08:00')")
        c.execute("insert into config_snapshot values('2026-11-20','campaign',NULL,7,'C2',NULL,'y','daily_budget','60','2026-11-20T10:00:00+08:00')")
        c.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            snapshot.ensure_snapshot_schema(c)

        tables = [r["name"] for r in c.execute("select name from sqlite_master where type='table'")]
        self.assertIn("config_snapshot", tables, "原表必须还在")
        self.assertNotIn("_config_snapshot_legacy", tables, "不能留半截重命名表")
        self.assertEqual(c.execute("select count(*) c from config_snapshot").fetchone()["c"], 2)
        self.assertEqual([r["name"] for r in c.execute("PRAGMA table_info(config_snapshot)")][:3],
                         ["snap_date", "entity_type", "entity_id"], "原结构不能被改掉")
        c.close()

    def test_failed_branch_a_migration_leaves_database_intact(self):
        """分支 A（半迁移库补 market_id 主键）失败也必须整体回滚，不能留下 _config_snapshot_v1。"""
        c = _conn()
        c.executescript("""
            create table config_snapshot (
                snap_ts TEXT NOT NULL, snap_date TEXT NOT NULL,
                legacy INTEGER NOT NULL DEFAULT 0, entity_type TEXT NOT NULL,
                entity_id TEXT, market_id INTEGER, campaign_id TEXT,
                ad_group_id TEXT, entity_label TEXT, field TEXT NOT NULL,
                value TEXT, captured_at TEXT NOT NULL,
                PRIMARY KEY (snap_ts, snap_date, entity_type, entity_id, field));
        """)
        c.execute("insert into config_snapshot values('2026-11-25T01:00:00Z','2026-11-25',0,'campaign','C1',7,'C1',NULL,'x','daily_budget','50','2026-11-25T09:00:00+08:00')")
        c.execute("insert into config_snapshot values('2026-11-25T01:00:00Z','2026-11-25',0,'campaign',NULL,7,'C2',NULL,'y','daily_budget','60','2026-11-25T09:00:00+08:00')")
        c.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            snapshot.ensure_snapshot_schema(c)

        tables = [r["name"] for r in c.execute("select name from sqlite_master where type='table'")]
        self.assertIn("config_snapshot", tables, "原表必须还在")
        self.assertNotIn("_config_snapshot_v1", tables, "不能留半截重命名表 _config_snapshot_v1")
        self.assertEqual(c.execute("select count(*) c from config_snapshot").fetchone()["c"], 2)
        self.assertEqual(
            snapshot._pk_columns(c, "config_snapshot"),
            ["snap_ts", "snap_date", "entity_type", "entity_id", "field"],
            "原 5 列主键不能被改掉",
        )
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
