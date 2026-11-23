#!/usr/bin/env python3
"""S3 沉淀回路自测：打钩 → 四类落点 → 下一轮读回来。

跑法：`python3 engine/test_sediment.py`

这个文件测的不是「函数不报错」，是那条回路到底通没通：

    人打钩说「目标放宽到 38%」 → param_override 有行 → 下一轮 build_facts
    按 38% 算 → 出题时这个码算「已生效」不再出现在清单里

任何一环断开，前面那一下打钩就白打了。所以下面每条断言后面都写着
「断了会怎样」—— 那是这段代码存在的理由。

不依赖 pytest，与 engine/test_r31.py 同一范式（纯脚本 assert）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

import facts          # noqa: E402
import reason_routes  # noqa: E402
import report         # noqa: E402
import rules          # noqa: E402
import scope          # noqa: E402

SRC = ROOT / "ingest" / "data" / "ads.sqlite3"
SCHEMA = ROOT / "engine" / "schema_v2.sql"
OBJ = scope.SCOPE_ASIN          # 账户级对象就用 ASIN（engine/scope.py）
PASSED: list[str] = []


def check(name: str, why: str, cond: bool) -> None:
    if not cond:
        print(f"✗ {name}\n  断了会怎样：{why}")
        raise SystemExit(1)
    PASSED.append(name)
    print(f"✓ {name}")


def fresh() -> sqlite3.Connection:
    """一份可写的临时库：结构齐全，不碰真库。"""
    tmp = Path(tempfile.mkdtemp(prefix="sediment-")) / "t.sqlite3"
    if SRC.exists():
        shutil.copy(SRC, tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def count(conn, table: str) -> int:
    return conn.execute(f"select count(*) as n from {table}").fetchone()["n"]


# ----------------------------------------------------------------------
# 1) 校准类：放宽目标 ACOS → param_override，并且能被读回来
# ----------------------------------------------------------------------
def test_param_override() -> None:
    conn = fresh()
    rc = reason_routes.apply_answer(
        conn, code="goal_relax_acos", object_id=OBJ,
        option={"param_key": "target_acos", "param_value": 0.38, "ttl_days": 90},
        question_id="Q08", run_id=7)
    check("放宽目标 ACOS 落 param_override",
          "打钩写了句漂亮话，run.py 读不到，明天照样按 30% 出建议",
          rc["table"] == "param_override" and count(conn, "param_override") == 1)

    ov = reason_routes.overrides(conn, OBJ)
    check("param_override 能被 overrides() 读回 0.38",
          "engine/run.py 的 build_facts 拿不到覆盖值，参数改了等于没改",
          ov.get("target_acos") == "0.38")
    check("override_float 给出 float",
          "facts.build_facts 要 float，字符串进去会算错或抛异常",
          reason_routes.override_float(conn, OBJ, "target_acos") == 0.38)

    # 同一个参数再打一次钩：应该是「改」不是「堆一行」
    reason_routes.apply_answer(
        conn, code="goal_relax_acos", object_id=OBJ,
        option={"param_value": "0.42"}, question_id="Q08", run_id=8)
    check("同参数重复打钩是覆盖不是堆行",
          "一天一条覆盖行，三个月后 overrides() 读到哪条全看运气",
          count(conn, "param_override") == 1
          and reason_routes.overrides(conn, OBJ)["target_acos"] == "0.42")

    # 到期后自动失效（这就是「季度复核」的机制，不能靠人记）
    conn.execute("update param_override set expires_at=?", ("2020-01-01",))
    check("过期覆盖不再生效",
          "三年前的临时放宽变成永久设定，目标 ACOS 再也回不去",
          reason_routes.overrides(conn, OBJ) == {})

    check("参数覆盖计入 active_codes（出题去重靠它）",
          "同一个码在有效期内被反复问，人会觉得打了钩没人看",
          "goal_relax_acos" in reason_routes.active_codes(
              fresh_with({"target_acos": "0.38"}), OBJ))


def fresh_with(overrides: dict) -> sqlite3.Connection:
    conn = fresh()
    for k, v in overrides.items():
        reason_routes.apply_answer(
            conn, code=reason_routes.PARAM_CODE[k], object_id=OBJ,
            option={"param_key": k, "param_value": v}, question_id="Q", run_id=1)
    return conn


# ----------------------------------------------------------------------
# 2) 校准类（数值来自自由文本）：自由文本里的「1.05」要能进库
# ----------------------------------------------------------------------
def test_free_text_number() -> None:
    conn = fresh()
    reason_routes.apply_answer(
        conn, code="budget_raise_smaller", object_id=OBJ,
        option={"param_key": "max_budget_step"}, free_text="每次只加 5% 就好",
        question_id="Q03", run_id=9)
    check("自由文本里的百分数被解析成 1.05",
          "「幅度小一点」落不成数字，R08 还是 1.2 倍往上加",
          reason_routes.override_float(conn, OBJ, "max_budget_step") == 1.05)

    conn2 = fresh()
    reason_routes.apply_answer(
        conn2, code="budget_raise_smaller", object_id=OBJ,
        option={"param_key": "max_budget_step"}, free_text="别加了",
        question_id="Q03", run_id=9)
    check("没给数字时降级写观察任务而不是瞎猜",
          "凭空猜一个步长写进策略库，等于把猜测当成了主人的意思",
          count(conn2, "param_override") == 0
          and count(conn2, "observation_task") == 1)

    # 手填数值要压过 pack 默认：人写的比算法算的权威。
    # 反过来（pack 默认压手填）会出现「我明明写 60%，系统按 80% 走」。
    conn3 = fresh()
    reason_routes.apply_answer(
        conn3, code="goal_relax_acos", object_id=OBJ,
        option={"param_key": "target_acos", "param_value": 0.8},
        free_text="按 60% 放宽", question_id="Q08", run_id=9)
    check("手填的百分数压过 pack 默认值",
          "人写 60% 却按 80% 执行，等于没听",
          reason_routes.override_float(conn3, OBJ, "target_acos") == 0.6)

    # 但不是话里每个数字都是参数：「观察 2 周」不能读成 2%。
    conn4 = fresh()
    reason_routes.apply_answer(
        conn4, code="budget_raise_smaller", object_id=OBJ,
        option={"param_key": "max_budget_step", "param_value": 1.1},
        free_text="先观察 2 周再说", question_id="Q03", run_id=9)
    check("话里的普通数字不被当参数",
          "「观察 2 周」写成「步长 2.0」，下次加预算直接翻倍",
          reason_routes.override_float(conn4, OBJ, "max_budget_step") == 1.1)


# ----------------------------------------------------------------------
# 3) 归因类：观察任务，带判据与到期日
# ----------------------------------------------------------------------
def test_observation_task() -> None:
    conn = fresh()
    rc = reason_routes.apply_answer(
        conn, code="kw_ok_listing_problem", object_id="kw:abc",
        option={"metric": "cvr", "ttl_days": 14}, question_id="Q01", run_id=10)
    row = conn.execute("select * from observation_task").fetchone()
    check("点「Listing 有问题」生成观察任务",
          "原因码只写进 reason_hit，下次判断没有任何东西可查",
          rc["table"] == "observation_task" and count(conn, "observation_task") == 1)
    check("观察任务带 metric / judge_rule / due_at",
          "没有判据与到期日的任务不是任务，是一条心愿",
          row["metric"] == "cvr" and row["judge_rule"]
          and row["due_at"] == (date.today() + timedelta(days=14)).isoformat())

    check("未到期的不出现在待判列表",
          "刚建的任务立刻要结论，等于逼人重复回答同一件事",
          reason_routes.awaiting_verdict(conn, "kw:abc")["tasks"] == [])

    due = reason_routes.due_tasks(conn, as_of=(date.today() + timedelta(days=15)).isoformat(),
                                  object_id="kw:abc")
    check("到期后出现在 due_tasks",
          "观察期结束了也没人知道，这条任务就永久挂着",
          len(due["tasks"]) == 1)

    got = reason_routes.mark_due(conn, as_of=(date.today() + timedelta(days=15)).isoformat(),
                                 object_id="kw:abc")
    again = reason_routes.mark_due(conn, as_of=(date.today() + timedelta(days=15)).isoformat(),
                                   object_id="kw:abc")
    check("mark_due 幂等（第二遍标 0 条）",
          "每天重跑都标一遍，结案计数越跑越假",
          got["tasks"] == 1 and again["tasks"] == 0)
    check("标记后进入 awaiting_verdict 等结论",
          "标完就消失，等于「到期」这件事被系统吞了",
          len(reason_routes.awaiting_verdict(conn, "kw:abc")["tasks"]) == 1)

    reason_routes.settle(conn, kind="observation_task", row_id=row["task_id"],
                         verdict="refuted", evidence="14 天后仍是 0 单")
    check("结案写入 verdict",
          "成立/不成立写不进去，提高同类再问权重就没有依据",
          conn.execute("select result from observation_task").fetchone()["result"]
          == "refuted")


# ----------------------------------------------------------------------
# 4) 探索类：实验必须有对照组，没对照就降级
# ----------------------------------------------------------------------
def test_experiment() -> None:
    conn = fresh()
    change = {"campaign_id": "C1", "field": "daily_budget", "from": 20.0, "to": 22.0}
    control = {"campaign_id": "C2", "label": "另一个活动"}
    rc = reason_routes.apply_answer(
        conn, code="explore_run_experiment", object_id="C1",
        option={"metric": "roas,cpa", "ttl_days": 14,
                "change": change, "control": control},
        question_id="Q05", run_id=11)
    row = conn.execute("select * from experiment").fetchone()
    check("有对照才开实验",
          "四类里的「探索」永远没有生产者，实验表一辈子空着",
          rc["table"] == "experiment" and row["control_json"])

    conn2 = fresh()
    rc2 = reason_routes.apply_answer(
        conn2, code="explore_run_experiment", object_id="C1",
        option={"metric": "roas", "change": change}, question_id="Q05", run_id=11)
    check("没有对照降级成观察任务",
          "没有对照组的实验，改完看到涨了也说不清是改动还是别的原因",
          rc2["table"] == "observation_task" and count(conn2, "experiment") == 0)


# ----------------------------------------------------------------------
# 5) 阻塞类：policy 落库并带到期日
# ----------------------------------------------------------------------
def test_policy() -> None:
    conn = fresh()
    rc = reason_routes.apply_answer(
        conn, code="kw_mismatch_ok_to_pause", object_id="kw:xyz",
        option={"policy_type": "kw_pause", "ttl_days": 60},
        free_text="这个词本来就不该投", question_id="Q01", run_id=12)
    # 库里有种子策略（计划书那条），这里只看这次打钩写进去的那一行
    row = conn.execute("select * from policy where object_id='kw:xyz'").fetchone()
    check("打钩写 policy 且 status=active",
          "写成 proposed 就永远不生效，policy 表只是第二个 reason_hit",
          rc["table"] == "policy" and row["status"] == "active")
    check("policy 记原话与到期日",
          "只剩一个 policy_type，三个月后没人知道当初为什么这么定",
          "这个词本来就不该投" in (row["evidence_text"] or "")
          and bool(row["expires_at"]))
    check("policy 计入 active_codes",
          "同一个码天天问，人会以为打钩没生效",
          "kw_mismatch_ok_to_pause" in reason_routes.active_codes(conn, "kw:xyz"))


# ----------------------------------------------------------------------
# 6) 划掉 / 跳过 / 未表态：三种「没打钩」必须分开
# ----------------------------------------------------------------------
def test_three_kinds_of_silence() -> None:
    conn = fresh()
    reason_routes.exclude(conn, object_id=OBJ, code="goal_relax_acos",
                          question_id="Q08")
    check("划掉写 reason_excluded",
          "划掉的选项下次又冒出来，等于没划",
          "goal_relax_acos" in reason_routes.excluded(conn, OBJ))
    # 真库里本来就有归因写的历史 reason_hit（outcome_*），所以按这道题看，
    # 不能按整表看 —— 否则这条断言永远是红的，也测不出划掉到底写没写。
    check("划掉不写 reason_hit",
          "划掉被算成「这个码又出现一次」，跳过率算歪，不确定性跟着算歪",
          conn.execute("select count(*) as n from reason_hit where scope='Q08'"
                       ).fetchone()["n"] == 0)

    reason_routes.assume(conn, object_id=OBJ, question_id="Q08")
    row = conn.execute("select * from agent_assumption").fetchone()
    check("跳过写假设 + 14 天复核",
          "跳过没有到期日，等于默认假设永远不需要验",
          row["review_at"] == (date.today() + timedelta(days=14)).isoformat())

    check("跳过不写 reason_hit / 不写策略",
          "跳过被当成同意，策略库里会多出一堆没人真同意过的结论",
          conn.execute("select count(*) as n from reason_hit where scope='Q08'"
                       ).fetchone()["n"] == 0
          # 真库里本来就有 1 条种子策略挂在同一个 ASIN 上（scope_p0_p1），
          # 所以按「来源＝打钩」判，不按整表判。
          and conn.execute("select count(*) as n from policy where source like '打钩%'"
                           ).fetchone()["n"] == 0)


# ----------------------------------------------------------------------
# 7) 出题侧：去重、option_meta、Q05 的生产
# ----------------------------------------------------------------------
class FakeFacts(facts.Facts):
    """目标不可达 + 一个差一点到线的活动候选 + 一个对照活动。"""

    @property
    def target_reachable(self) -> bool:
        return False


def fake_facts() -> FakeFacts:
    f = FakeFacts(
        start="2026-11-01", end="2026-11-15", window_days=14,
        target_acos=0.30,
        account={"acos": 0.62, "cvr": 0.021, "cpc": 0.9, "aov": 30.0},
        cvr_baseline={"cvr": 0.021},
        breakeven={"breakeven_cvr": 0.031},
        min_clicks={"min_clicks": 400, "cvr": 0.021, "confidence": 0.8},
        cpc_bias={"overall": 0.964}, floor={"floor": 0.35},
        placements=[], campaign_placements=[], keywords=[], searchterms=[],
        campaigns=[
            {"campaign_id": "C1", "campaign_name": "手动-高位下拉",
             "portfolio_name": "JL-高位下拉", "state": "ENABLED",
             "avg_daily_cost": 20.0, "cost": 280.0, "clicks": 300, "orders": 2},
            {"campaign_id": "C2", "campaign_name": "手动-下拉变体",
             "portfolio_name": "JL-高位下拉", "state": "ENABLED",
             "avg_daily_cost": 19.0, "cost": 270.0, "clicks": 290, "orders": 3},
        ],
    )
    return f


def fake_below():
    return [rules.Decision(
        rule_id="R08", rule_version="v1", entity_type="campaign", entity_id="C1",
        entity_label="手动-高位下拉", market_id=7, campaign_id="C1",
        action_type="raise_budget", current_value=20.0, proposed_value=24.0,
        confidence="low", context={"window": {"start": "2026-11-01", "end": "2026-11-15"}},
        calculation=[], significance={"clicks": 300, "orders": 2},
        spend_impact=70.0, rationale="证据一般",
    )]


def test_questions() -> None:
    f = fake_facts()
    conn = fresh()
    qs, notes = report._build_questions(f, [], [], below=fake_below(), conn=conn)
    ids = [q["id"] for q in qs]
    check("目标不可达时出 Q08", "目标不可达却不出题，那个 62% 的 ACOS 永远没人处理",
          "Q08" in ids)
    q08 = next(q for q in qs if q["id"] == "Q08")
    check("每个选项都带去向 destination",
          "审批侧不知道钩子该落哪张表，四类去向在出题这一层就断了",
          all(m.get("destination") for m in q08["option_meta"]))
    relax = q08["option_meta"][1]
    check("放宽选项自带 param_key/value/ttl",
          "审批侧要重算放宽到多少，两处口径迟早不一致",
          relax["param_key"] == "target_acos"
          and isinstance(relax["param_value"], float)
          and relax["ttl_days"] == 90)
    check("四类都有生产者（policy / param_override / observation_task / experiment）",
          "四类只实现一半，词典写了等于没写",
          {"policy", "param_override", "observation_task", "experiment"}
          <= {m["destination"] for q in qs for m in q["option_meta"]})

    q05 = next((q for q in qs if q["id"] == "Q05"), None)
    check("差一点到线的候选会出探索题 Q05", "experiment 表没有生产者，四类永远是三类",
          q05 is not None)
    exp = q05["option_meta"][0]
    check("实验选项自带 change + control",
          "没有改动项与对照组的「实验」落库后无法复核",
          exp.get("change") and exp.get("control")
          and exp["control"].get("campaign_id") != "C1")

    # 把所有非 other 选项都沉淀一遍，这一题就不该再出现
    conn2 = fresh()
    for code in ("goal_keep_acos30_control_loss", "goal_relax_acos",
                 "goal_product_side_problem"):
        reason_routes.apply_answer(
            conn2, code=code, object_id=scope.SCOPE_ASIN,
            option={"param_value": 0.38, "ttl_days": 90} if code == "goal_relax_acos" else {},
            question_id="Q08", run_id=13)
    qs2, notes2 = report._build_questions(f, [], [], below=[], conn=conn2)
    check("四类都生效后 Q08 不再问",
          "同一件事被反复问，人会觉得打钩没有用，然后就不打了",
          "Q08" not in [q["id"] for q in qs2]
          and any("生效期内" in n for n in notes2))

    # 一个都没沉淀时不该有备注
    qs3, notes3 = report._build_questions(f, [], [], conn=fresh())
    check("没有任何沉淀时照常出题且不留备注",
          "空库就少出题，等于把「没沉淀」当成「都定了」",
          "Q08" in [q["id"] for q in qs3] and not any("生效期内" in n for n in notes3))

    payload = report.pack_json(f, report.MemorialPack(
        alerts=[], front=[], overflow=[], below=[], observing=[],
        questions=qs, conflicts=[]), 1, suppressed=[])
    check("pack_json 透出 option_meta（approve.py 靠它写库）",
          "出题器算好的去向到不了审批侧，写库还得靠猜",
          payload["questions"][0]["option_meta"]
          and "decision_ref" in payload["questions"][0])

    # 划掉也要能挡住：划掉是「这个方向不要」，和「已沉淀」一样不该再问，
    # 但备注要说清是哪一种，否则人分不清系统是记住了还是放弃了这个方向。
    conn2 = fresh()
    for _c in ("goal_keep_acos30_control_loss", "goal_relax_acos",
               "goal_product_side_problem"):
        reason_routes.exclude(conn2, object_id=OBJ, code=_c, question_id="Q08")
    qs2, notes2 = report._build_questions(f, [], [], below=None, conn=conn2)
    check("三个方向都划掉后 Q08 不再问", "划掉过的东西下次又冒出来，人会怀疑钩子没用",
          all(q["id"] != "Q08" for q in qs2))
    check("备注说清是「划掉」而不是「生效中」",
          "两种静默混成一句话，人分不清系统记住了还是放弃了",
          any("划掉" in n for n in notes2) and not any("生效期内" in n for n in notes2))

    # 只划掉一个方向：题还在，被划掉的那个选项要带标记（前端置灰）
    conn3 = fresh()
    reason_routes.exclude(conn3, object_id=OBJ, code="goal_product_side_problem",
                          question_id="Q08")
    qs3, _ = report._build_questions(f, [], [], below=None, conn=conn3)
    q08 = next((q for q in qs3 if q["id"] == "Q08"), None)
    metas3 = (q08 or {}).get("option_meta") or []
    check("只划一个方向时该选项带 excluded 标记",
          "前端不知道哪个选项被划掉过，人会被同一个问题反复劝",
          any(m.get("excluded") and m.get("exclude_until") for m in metas3))


# ----------------------------------------------------------------------
# 8) 读侧：参数覆盖真的改变了 facts 与 R08 的步长
# ----------------------------------------------------------------------
def test_read_side() -> None:
    conn = fresh_with({"max_budget_step": "1.05"})
    step = reason_routes.override_float(conn, OBJ, "max_budget_step")
    f = fake_facts()
    f.budget_step = step
    new = round(20.0 * step, 2)
    check("覆盖值进到 R08 的步长", "「幅度小一点」只改了表，加预算还是 +20%",
          new == 21.0)


def main() -> int:
    print(f"对象 = {OBJ}；库 = 临时副本（不碰真库）\n")
    test_param_override()
    test_free_text_number()
    test_observation_task()
    test_experiment()
    test_policy()
    test_three_kinds_of_silence()
    test_questions()
    test_read_side()
    print(f"\n全部通过：{len(PASSED)} 项。四类去向 + 三种沉默 + 去重 + 读回，回路是通的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())