"""R31 卡位规则全分支测试。用真库复制 + 真 schema，只插卡位配置与排名观测。"""
import shutil, sqlite3, sys, os
from datetime import datetime, timedelta

ROOT = "/Users/baoyanan/自动化广告平台"
sys.path.insert(0, os.path.join(ROOT, "engine"))

DB_SRC = os.path.join(ROOT, "ingest/data/ads.sqlite3")
DB = "/tmp/ads_rank_test.sqlite3"
SCHEMA = os.path.join(ROOT, "engine/schema_v2.sql")

GOOD = ("21937639215436", "pulldown machine", "46818486452618", 0.5)   # acos 13.9% 好词
BAD  = ("151878211767486", "lat pulldown", "175723193023274", 1.0)     # acos 123% 差词
# 曾经有个 ZERO 常量指向真库里的 cable machine（92 击 0 单）。已删：真库行的日期是固定的，
# 而窗口跟着 fact_campaign_daily 的最新日期右移，两者一旦错开，用例就静默失效。
# 需要「零单超支」的夹具时用 add_kw_daily() 按当前窗口现造（见 case 7）。
ASIN = "B0GLYW51HY"


def fresh():
    shutil.copy(DB_SRC, DB)
    conn = sqlite3.connect(DB)
    conn.executescript(open(SCHEMA, encoding="utf-8").read())
    conn.execute("delete from rank_target")
    conn.execute("delete from rank_snapshot")
    conn.execute("delete from decision_log where rule_id='R31'")
    conn.commit()
    return conn


def ts(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def add_target(conn, kid, **kw):
    row = dict(market_id=1, keyword_id=kid, keyword_text=kw.get("text", "kw"),
               campaign_id=kw.get("campaign_id"), ad_group_id=kw.get("ad_group_id"),
               asin=kw.get("asin", ASIN), goal_page=kw.get("goal_page", 1),
               goal_position=kw.get("goal_position", 4), push_mode=kw.get("push_mode", "bid"),
               max_bid=kw.get("max_bid"), max_tos_pct=kw.get("max_tos_pct", 900.0),
               step_mode=kw.get("step_mode", "fixed"), step_value=kw.get("step_value", 0.10),
               success_streak_required=kw.get("streak", 5),
               decrease_mode=kw.get("decrease_mode"), decrease_value=kw.get("decrease_value"),
               restore_on_end=1, acos_guard_mult=kw.get("guard", 1.5),
               max_pushes=kw.get("max_pushes", 6), status="active",
               created_at=ts(1), expires_at=None)
    cols = ",".join(row)
    ph = ",".join("?" * len(row))
    conn.execute(f"insert into rank_target ({cols}) values ({ph})", tuple(row.values()))


def add_batch(conn, kid, hours_ago, rows):
    """一次抓取写多行：rows = [(page, position, asin, is_self), ...]"""
    at = ts(hours_ago)
    for page, pos, asin, is_self in rows:
        conn.execute(
            """insert into rank_snapshot (market_id, keyword_id, keyword_text, observed_at,
               placement, page, position, asin, is_self, postal_code, source)
               VALUES (1,?,?,?,'sponsored',?,?,?,?, '10001','plugin')""",
            (kid, "kw", at, page, pos, asin, 1 if is_self else 0))


def add_kw_daily(conn, kid, text, camp, *, bid, days, clicks, cost, orders=0, sales=0.0):
    """给指定日期造 fact_keyword_daily 行。

    为什么需要它：真库里的历史行会随窗口右移滑出窗口（见 case 7 注释），
    需要「窗口内一定有」的夹具时只能自己造。clicks/cost 是每天的值。
    """
    for d in days:
        cpc = round(cost / clicks, 4) if clicks else None
        conn.execute(
            """insert or replace into fact_keyword_daily
               (market_id, report_date, keyword_id, keyword_text, match_type, ad_group_id,
                campaign_id, bid, serving_status, impressions, clicks, cost, ads_sales,
                ads_orders, ctr, cpc, cvr, acos, roas, cpa, row_hash, raw_json, source, ingested_at)
               values (7,?,?,?,'2','47676504188614',?,?, 'ENABLED', ?, ?, ?, ?, ?, 0.04, ?, 0,
                       null, null, null, ?, '{}', 'test', '2026-11-23T00:00:00+08:00')""",
            (d, kid, text, camp, bid, clicks * 20, clicks, cost, sales, orders,
             cpc, f"test-{kid}-{d}"))


def add_pushes(conn, kid, n):
    for i in range(n):
        conn.execute(
            """insert into decision_log (decided_at, rule_id, entity_type, entity_id,
               action_type, confidence, context_json, calculation_json, significance_json, status)
               VALUES (?,'R31','keyword',?,'rank_push','high','{}','[]','{}','proposed')""",
            (ts(2), kid))


def run(conn):
    import importlib, facts, rules
    importlib.reload(facts); importlib.reload(rules)
    f = facts.build_facts(conn, window_days=14, target_acos=0.30, exclude_days=2, confidence=0.80)
    return f, rules.rule_31_rank_position_push(f)


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("1 固定步长加价 0.5→0.60")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0, step_value=0.10)
    add_batch(c, kid, 1, [(1, 2, "B0OTHER001", False), (2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    d = [x for x in r.decisions if x.entity_id == kid]
    assert d and d[0].action_type == "rank_push", [x.action_type for x in d]
    assert abs(d[0].proposed_value - 0.60) < 1e-9, d[0].proposed_value
    assert d[0].spend_impact < 0, d[0].spend_impact


@case("2 百分比步长 10% 0.5→0.55")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0, step_mode="percent", step_value=10.0)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    d = [x for x in r.decisions if x.entity_id == kid]
    assert d and abs(d[0].proposed_value - 0.55) < 1e-9, d and d[0].proposed_value


@case("3 无排名观测 → 拦下")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0)
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "无排名观测", sup


@case("4 观测过期 → 拦下")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0)
    add_batch(c, kid, 20, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "排名观测过期", sup


@case("5 自我竞争：自家另一 ASIN 占目标位")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0)
    # 同一批次：自家另一 ASIN 第 2 位（目标区内），自己第 8 位
    add_batch(c, kid, 1, [(1, 2, "B0SELF002", True), (2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "自我竞争", sup


@case("6 ACOS 闸：有单且超闸")
def _(c):
    kid, text, camp, bid = BAD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=3.0)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "ACOS 闸", sup


@case("7 ACOS 闸：零单超支")
def _(c):
    # 零单闸的门槛是 aov × target_acos × guard。本账号 aov=$187.99、target=30%，
    # guard 收到 0.8 时门槛 $45.12；只要窗口内零单花费超过它就该拦。
    #
    # 这里不能用真库里的历史行当夹具：ZERO 指向的 cable machine 数据只到 2026-11-08，
    # 而窗口是「库里最新一天 −2 再往前 14 天」，会随日子整体右移 —— 有一天它整段滑出
    # 窗口，本用例就静默变成「词不在窗口里」，断言 sup[0] 取到的是别的分支（2026-11-23
    # 实测：窗口 11-07~11-20 只剩 11-07/11-08 两天，$24.05 打不到 $45.12 的门槛，
    # 用例挂在一个与规则无关的原因上）。所以改成按当天窗口现造数据，夹具与窗口同生共死。
    import facts
    start, end = facts.date_window(14, 2, conn=c)
    kid, text, camp = "990000000000007", "cable machine", "40077084589855"
    add_kw_daily(c, kid, text, camp, bid=0.65,
                 days=[start, end], clicks=46, cost=29.90)   # 92 击 / $59.80 / 0 单
    add_target(c, kid, campaign_id=camp, text=text, max_bid=3.0, guard=0.8)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "ACOS 闸（零单超支）", sup


@case("8 加价次数上限")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0, max_pushes=2)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    add_pushes(c, kid, 2)
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "加价次数上限", sup


@case("9 max_bid 触顶")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=0.50, step_value=0.10)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "触顶", sup


@case("10 到位连续 5 次 → 降价 0.5→0.48")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, decrease_mode="percent",
               decrease_value=5.0, streak=5)
    for h in range(1, 7):
        add_batch(c, kid, h, [(1, 2, ASIN, True)])
    c.commit(); f, r = run(c)
    d = [x for x in r.decisions if x.entity_id == kid]
    assert d and d[0].action_type == "rank_step_down", d and d[0].action_type
    assert abs(d[0].proposed_value - 0.48) < 1e-9, d[0].proposed_value


@case("11 到位但 streak 不足 → 拦下")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, decrease_mode="percent",
               decrease_value=5.0, streak=5)
    for h in range(1, 3):
        add_batch(c, kid, h, [(1, 2, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "已到位", sup


@case("12 TOS 加价 150%→160%")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, push_mode="tos", step_value=10.0, max_tos_pct=900.0)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    d = [x for x in r.decisions if x.entity_id == kid]
    assert d and d[0].action_type == "rank_push", d and d[0].action_type
    assert abs(d[0].proposed_value - 160.0) < 1e-9, d[0].proposed_value


@case("13 TOS 触顶")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, push_mode="tos",
               step_value=10.0, max_tos_pct=150.0)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit(); f, r = run(c)
    sup = [s for s in r.suppressed if s["entity"] == text]
    assert sup and sup[0]["reason"] == "触顶", sup


@case("14 R01/R03 对卡位词让路")
def _(c):
    kid, text, camp, bid = BAD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=3.0, guard=99.0)
    add_batch(c, kid, 1, [(2, 8, ASIN, True)])
    c.commit()
    import facts, rules
    f = facts.build_facts(c, window_days=14, target_acos=0.30, exclude_days=2, confidence=0.80)
    res = rules.run_all(f)
    r01 = [s for s in res.suppressed if s["rule_id"] == "R01" and s["entity"] == text]
    assert r01 and r01[0]["reason"] == "卡位接管", r01
    assert not [x for x in res.decisions if x.rule_id == "R01" and x.entity_id == kid]


@case("15 抓取深度内没看到自己 → 按未到位处理")
def _(c):
    kid, text, camp, bid = GOOD
    add_target(c, kid, campaign_id=camp, text=text, max_bid=2.0)
    add_batch(c, kid, 1, [(1, 1, "B0OTHER001", False), (1, 2, "B0OTHER002", False)])
    c.commit(); f, r = run(c)
    d = [x for x in r.decisions if x.entity_id == kid]
    assert d and d[0].action_type == "rank_push", d and d[0].action_type
    step = [s for s in d[0].calculation if s["step"] == "当前位次"][0]
    assert step["result"]["unseen"] is True, step["result"]


passed = failed = 0
for name, fn in CASES:
    c = fresh()
    try:
        fn(c); passed += 1; print(f"PASS {name}")
    except AssertionError as e:
        failed += 1; print(f"FAIL {name}  →  {e}")
    except Exception as e:
        failed += 1; print(f"ERR  {name}  →  {type(e).__name__}: {e}")
    finally:
        c.close()
print(f"\n{passed} passed / {failed} failed / {len(CASES)} total")
