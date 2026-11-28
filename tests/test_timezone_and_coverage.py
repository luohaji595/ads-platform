#!/usr/bin/env python3
"""时区口径 + 结算缓冲（2026-11-26 修的两条）。

背景
----
1. **拉哪天由谁决定**：老代码全用 `date.today()`（本机 CST）算「昨天」。
   7 号店的 `dim_market.timezone` 是 `US/Pacific`，比 CST 慢 16 小时 ——
   北京时间 11-26 上午 8 点，洛杉矶还是 11-25 下午 4 点，此时按 CST 判
   「D-1 = 11-25 应该有数据」拉到的其实是站点当天过了 2/3 的残值。
   更糟的是北京时间 11-26 15:00 跑，洛杉矶才 11-25 23:00 —— 仍在拉「当天」。
   所以「今天是哪一天」必须按店铺时区，链的完成判定和 ingest 的窗口共用一个口径。

2. **数据到哪天算能用**：`converge._coverage()` 原来直接返回
   `max(report_date)`，等于宣布「最后一天是完整的一天」。但 Amazon 报表
   T+1 出、当天读数会反复上修（生产库实测 11-23 同一天从 154 击涨到 378 击）。
   拿半截日当完整日，会把「累计到 11-24 的 orders」判成 1 单，而真实值至少 5 单。
   修法是在覆盖上界扣掉结算缓冲，而不是裁断言窗口 ——
   裁窗口等于偷偷把断言改短，判出来的 correct 是假的。

跑：python3 tests/test_timezone_and_coverage.py
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "engine", ROOT / "adsdesk" / "agents"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import converge  # noqa: E402


def _load_ingest():
    """按文件路径加载 ingest/ingest.py，而不是 `import ingest`。

    为什么要这么绕：`ROOT/ingest` 里既有 `ingest.py` 又有 `jijia_client.py`，
    把 `ROOT/ingest` 塞进 sys.path 后，顶层名字 `ingest` 会被 `ingest.py` 抢走 ——
    于是别的测试里 `from ingest import jijia_client` 就解析失败
    （实测：`test_bugfixes.JijiaTokenRetryTest` 三条全 ImportError）。
    用 importlib 按路径加载既拿到模块，又不污染 sys.path 的顶层名字。
    """
    import importlib.util

    pkg_dir = ROOT / "ingest"
    added = str(pkg_dir) not in sys.path
    if added:
        sys.path.append(str(pkg_dir))   # 放末尾：ingest.py 自己 import jijia_client 要用
    try:
        spec = importlib.util.spec_from_file_location("_ingest_mod", pkg_dir / "ingest.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added:
            sys.path.remove(str(pkg_dir))


ingest_mod = _load_ingest()


def _market_db(tz: str | None) -> sqlite3.Connection:
    """只建 dim_market 一个表的内存库，够 market_today 用。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """create table dim_market(
               market_id integer primary key, market text, country_name text,
               seller_id text, ads_state text, auth_expire_date text,
               ads_report_period text, ads_report_init_begin text,
               timezone text, currency text, updated_at text)"""
    )
    if tz is not None:
        conn.execute(
            "insert into dim_market (market_id, market, timezone, currency, updated_at)"
            " values (7,'amazon-us',?,'USD','2026-11-26T00:00:00')", (tz,))
    conn.commit()
    return conn


class MarketTodayTest(unittest.TestCase):
    """`今天` 必须按店铺时区算。"""

    def test_uses_market_timezone_not_local(self):
        conn = _market_db("US/Pacific")
        got = ingest_mod.market_today(conn, 7)
        want = datetime.now(ZoneInfo("US/Pacific")).date()
        self.assertEqual(got, want)

    def test_two_timezones_can_disagree(self):
        """同一时刻、两个时区，日期确实会差一天 —— 这就是原 bug 的物理基础。"""
        la = datetime.now(ZoneInfo("US/Pacific")).date()
        cst = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        # 只在真正跨日的时段断言不等；其余时段只要求各自等于自己的时区
        self.assertEqual(la, datetime.now(ZoneInfo("US/Pacific")).date())
        self.assertEqual(cst, datetime.now(ZoneInfo("Asia/Shanghai")).date())
        self.assertIn((la == cst), (True, False))

    def test_bad_timezone_falls_back_to_local(self):
        conn = _market_db("Bogus/Zone")
        self.assertEqual(ingest_mod.market_today(conn, 7), date.today())

    def test_missing_timezone_falls_back_to_local(self):
        conn = _market_db(None)
        self.assertEqual(ingest_mod.market_today(conn, 7), date.today())


class ResolveDayTest(unittest.TestCase):
    """相对日期必须允许显式传 today —— 不传就用本机，那是老行为。"""

    def test_yesterday_with_explicit_today(self):
        self.assertEqual(ingest_mod.resolve_day("yesterday", today=date(2026, 11, 26)),
                         "2026-11-25")

    def test_minus_n(self):
        self.assertEqual(ingest_mod.resolve_day("-3", today=date(2026, 11, 26)),
                         "2026-11-23")

    def test_absolute_passthrough(self):
        self.assertEqual(ingest_mod.resolve_day("2026-11-01", today=date(2026, 11, 26)),
                         "2026-11-01")


class ApplyDatesTest(unittest.TestCase):
    """parse_args 只解析、不落日期；apply_dates 才按店铺时区算。"""

    def test_parse_args_leaves_dates_unresolved(self):
        a = ingest_mod.parse_args(["--market", "7"])
        self.assertIsNone(a.start, "解析阶段不能按本机日期算死")
        self.assertIsNone(a.end)
        self.assertEqual(a._rel, ("yesterday", "yesterday"))

    def test_apply_dates_uses_market_today(self):
        conn = _market_db("US/Pacific")
        a = ingest_mod.parse_args(["--market", "7", "--recheck-days", "3"])
        ingest_mod.apply_dates(a, conn)
        today = datetime.now(ZoneInfo("US/Pacific")).date()
        self.assertEqual(a.end, (today - timedelta(days=1)).isoformat())
        self.assertEqual(
            a.start,
            (today - timedelta(days=3)).isoformat(),
            "--recheck-days 的窗口也要从店铺时区往前推")

    def test_explicit_window_not_overridden(self):
        conn = _market_db("US/Pacific")
        a = ingest_mod.parse_args(
            ["--market", "7", "--start", "2026-11-01", "--end", "2026-11-05"])
        ingest_mod.apply_dates(a, conn)
        self.assertEqual((a.start, a.end), ("2026-11-01", "2026-11-05"))


class CoverageBufferTest(unittest.TestCase):
    """`converge._coverage()` 的上界要扣结算缓冲。"""

    def _facts_db(self, dates: list[str]) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for t in ("fact_campaign_daily", "fact_keyword_daily", "fact_placement_daily"):
            conn.execute(f"create table {t}(report_date text)")
            for d in dates:
                conn.execute(f"insert into {t} values (?)", (d,))
        conn.commit()
        return conn

    def test_upper_bound_subtracts_settle_days(self):
        conn = self._facts_db(["2026-11-01", "2026-11-20", "2026-11-24"])
        d_min, d_max = converge._coverage(conn)
        self.assertEqual(d_min, "2026-11-01")
        self.assertEqual(d_max, "2026-11-22",
                         "11-24 是半截日，扣 2 天到 11-22")

    def test_buffer_never_goes_below_start(self):
        """数据只有一两天时，扣完不能倒挂 —— 宁可按起点算，全判 pending。"""
        conn = self._facts_db(["2026-11-25"])
        d_min, d_max = converge._coverage(conn)
        self.assertEqual(d_min, "2026-11-25")
        self.assertEqual(d_max, "2026-11-25")
        self.assertGreaterEqual(d_max, d_min)

    def test_empty_db(self):
        conn = self._facts_db([])
        self.assertEqual(converge._coverage(conn), (None, None))

    def test_settle_days_matches_engine(self):
        """两边口径必须一致，否则引擎判的和收敛器判的会互相打脸。"""
        import facts
        self.assertEqual(converge.SETTLE_EXCLUDE_DAYS, facts.DEFAULT_ATTR_EXCLUDE_DAYS)


class ChainTwoClocksTest(unittest.TestCase):
    """链上两个日期各管一件事，混用会让整条链空转。

    2026-11-26 真踩过：把 `today` 从 CST 日整体换成店铺日（LA），
    `step_done` 里 6 处 `substr(<CST/UTC 时间列>,1,10)=today` 全部失配。
    错位区间是 CST 09:00~15:59（LA 还停在前一天），正好落在运行窗 09:00~23:00
    里的 7/15 时间上。后果两种：
      · 昨天 CST 上午跑的那轮被判成「今天已跑」→ 7/7 步全跳过，当天什么都没做；
      · 今天真跑过的被判成「今天没跑」→ 重跑，撞 MAX_ATTEMPTS 后整天不重试。

    所以固定成两个参数：
      today      = CST 日，问「今天这条链做过没」
      market_day = 店铺日，问「站点那边哪一天已经过完」
    """

    def _db(self):
        from datetime import datetime
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            create table dim_market(market_id integer primary key, timezone text);
            create table chain_run(business_day text, step text, attempt int, status text,
                                   exit_code int, started_at text, finished_at text, detail text);
            create table run_log(dataset text, status text, started_at text, finished_at text);
            create table engine_run(run_id integer primary key, status text, started_at text,
                                    window_end text);
            create table memorial(run_id integer primary key, agent_state text,
                                  created_at text, business_day text);
            create table agent_run(agent_run_id integer primary key, job text, status text,
                                   started_at text);
            create table converge_run(converge_run_id integer primary key, status text,
                                      started_at text, n_due int, n_filled int, n_deferred int,
                                      error text);
            create table aba_weekly(market_id int, snapshot_week text, snapshot_date text);
            create table fact_campaign_daily(report_date text);
            create table fact_keyword_daily(report_date text);
            create table fact_search_term_daily(report_date text);
            create table fact_placement_daily(report_date text);
        """)
        conn.execute("insert into dim_market values(7, 'US/Pacific')")
        # 四张事实表都到 11-25
        for t in ("fact_campaign_daily", "fact_keyword_daily",
                  "fact_search_term_daily", "fact_placement_daily"):
            conn.execute(f"insert into {t} values('2026-11-25')")
        # CST 日 11-26 这天跑过的一轮（时间列写 CST）
        conn.execute("insert into chain_run values('2026-11-26','ingest',1,'ok',0,"
                     "'2026-11-26T09:38:04+08:00','2026-11-26T09:38:04+08:00','')")
        conn.execute("insert into run_log values('market.competitor+market','ok',"
                     "'2026-11-26T09:40:00+08:00','2026-11-26T09:40:00+08:00')")
        conn.execute("insert into run_log values('rank.all','ok',"
                     "'2026-11-26T09:41:00+08:00','2026-11-26T09:41:00+08:00')")
        # engine_run / memorial 写 UTC（CST 09:38 = UTC 01:38，同日）
        conn.execute("insert into engine_run values(123,'ok','2026-11-26T01:39:47Z','2026-11-23')")
        conn.execute("insert into memorial values(123,'ok','2026-11-26T01:39:47Z','2026-11-23')")
        conn.execute("insert into agent_run values(61,'attribute','ok','2026-11-26T09:42:36+08:00')")
        conn.execute("insert into converge_run values(20,'ok','2026-11-26T09:42:37+08:00',24,10,5,null)")
        conn.commit()
        return conn

    def _chain(self):
        import importlib.util
        path = ROOT / "adsdesk" / "chain.py"
        spec = importlib.util.spec_from_file_location("chain_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cst_day_finds_todays_run(self):
        """CST 日上午：今天的轮次必须认出来，否则重跑撞上限。"""
        chain, conn = self._chain(), self._db()
        for step in ("engine", "diagnose", "attribute", "converge"):
            done, why = chain.step_done(conn, step, "2026-11-26", "2026-11-26")
            self.assertTrue(done, f"{step} 应该认出今天跑过：{why}")

    def test_la_day_would_miss_todays_run(self):
        """反证：拿店铺日当 today 会认成昨天那轮（#122），今天这轮被跳过。

        这不是假设 —— 生产库 11-26 就是这样：today=11-25 时 engine 认
        `engine_run #122`（窗口末 11-22），而今天该跑的 #123（窗口末 11-23）被当成「已做」。
        """
        chain, conn = self._chain(), self._db()
        conn.execute("insert into engine_run values(122,'ok','2026-11-25T02:04:12Z','2026-11-22')")
        conn.execute("insert into memorial values(122,'ok','2026-11-25T02:04:12Z','2026-11-22')")
        conn.commit()
        done, why = chain.step_done(conn, "engine", "2026-11-25", "2026-11-25")
        self.assertTrue(done)
        self.assertIn("#122", why, "用店铺日当 today 会认到昨天那轮")

    def test_ingest_target_follows_market_day(self):
        """ingest 的目标日是店铺日的 D-1，与 CST 日无关。"""
        chain, conn = self._chain(), self._db()
        done, why = chain.step_done(conn, "ingest", "2026-11-26", "2026-11-26")
        self.assertTrue(done)
        self.assertIn("2026-11-25", why)

        # 店铺日退一天 → 目标退一天，判定依据也跟着变
        done2, why2 = chain.step_done(conn, "ingest", "2026-11-26", "2026-11-25")
        self.assertIn("2026-11-24", why2)
        self.assertNotEqual(why, why2)

    def test_ingest_args_follows_market_day(self):
        """拉数窗口的上界 = 店铺日 D-1。"""
        chain, conn = self._chain(), self._db()
        _, win26 = chain.ingest_args(conn, "2026-11-26")
        _, win25 = chain.ingest_args(conn, "2026-11-25")
        self.assertTrue(win26.endswith("2026-11-25"), win26)
        self.assertTrue(win25.endswith("2026-11-24"), win25)

    def test_other_steps_ignore_market_day(self):
        """其余四步只认 CST 日：店铺日怎么变都不该影响判定。"""
        chain, conn = self._chain(), self._db()
        for step in ("engine", "diagnose", "attribute", "converge"):
            a = chain.step_done(conn, step, "2026-11-26", "2026-11-26")
            b = chain.step_done(conn, step, "2026-11-26", "2026-11-20")
            self.assertEqual(a, b, f"{step} 不该受 market_day 影响")

    def test_market_day_defaults_to_market_today(self):
        """不传 market_day 时自己按 dim_market 算，不能退化成 CST 日。"""
        chain, conn = self._chain(), self._db()
        got = chain.step_done(conn, "ingest", "2026-11-26")
        expect = chain.step_done(conn, "ingest", "2026-11-26",
                                 chain.market_today(conn).isoformat())
        self.assertEqual(got, expect)

    def test_utc_early_morning_cst_matches_engine_and_diagnose(self):
        """CST 00:00~07:59（如 01:30）跑完时，UTC 时间落在前一天（17:30Z）。

        --force 在凌晨跑链时，engine_run.started_at / memorial.created_at 记的是
        `"2026-11-26T17:30:00Z"`，step_done("engine", "2026-11-27") 与
        step_done("diagnose", "2026-11-27") 必须按本地 CST 日期认出它，不能误判失败。
        """
        chain, conn = self._chain(), self._db()
        conn.execute("insert into chain_run values('2026-11-27','ingest',1,'ok',0,"
                     "'2026-11-27T01:10:00+08:00','2026-11-27T01:20:00+08:00','')")
        conn.execute("insert into engine_run values(124,'ok','2026-11-26T17:30:00Z','2026-11-25')")
        conn.execute("insert into memorial values(124,'ok','2026-11-26T17:30:00Z','2026-11-25')")
        conn.commit()

        for step in ("engine", "diagnose"):
            done, why = chain.step_done(conn, step, "2026-11-27", "2026-11-26")
            self.assertTrue(done, f"{step} 应该按 CST 日期认出 2026-11-26T17:30:00Z：{why}")
            self.assertIn("#124", why)

            # 不应被误算成 CST 2026-11-26（当天只有 #123，不能认成 #124）
            done_prev, why_prev = chain.step_done(conn, step, "2026-11-26", "2026-11-26")
            self.assertTrue(done_prev)
            self.assertIn("#123", why_prev)
            self.assertNotIn("#124", why_prev)

        # 若之后又有更晚的取数完成，_started_after 仍应判为需要重跑
        conn.execute("insert into chain_run values('2026-11-27','ingest',2,'ok',0,"
                     "'2026-11-27T01:35:00+08:00','2026-11-27T01:40:00+08:00','')")
        conn.commit()
        for step in ("engine", "diagnose"):
            done_stale, why_stale = chain.step_done(conn, step, "2026-11-27", "2026-11-26")
            self.assertFalse(done_stale, why_stale)
            self.assertIn("早于今天取数完成时刻", why_stale)

        # 兼容旧格式 naive CST 时间戳
        conn.execute("insert into engine_run values(125,'ok','2026-11-27 01:45:00','2026-11-25')")
        conn.execute("insert into memorial values(125,'ok','2026-11-27 01:45:00','2026-11-25')")
        conn.commit()
        for step in ("engine", "diagnose"):
            done_naive, why_naive = chain.step_done(conn, step, "2026-11-27", "2026-11-26")
            self.assertTrue(done_naive, why_naive)
            self.assertIn("#125", why_naive)


if __name__ == "__main__":
    unittest.main(verbosity=2)
