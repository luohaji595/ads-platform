#!/usr/bin/env python3
"""手填参数的解析与钳位（reason_routes）。

背景（2026-11-26 核实）：老版本 `_explicit_ratio` 的判据是「句子里出现过 %，
就把句子里第一个数字当比例」。实测 `'2 周后放到 40%'` 被读成 0.02 ——
主人想放宽到 40%，系统把目标写成了 2%，第二天所有词都成了「亏钱词」。
同一套逻辑还把 `'放宽到 500%'` 原样收成 5.0（% 分支零上限）。

这一组测试钉死三件事：
  1. 数字必须绑单位才算比例（`40%` / `40 个点` / `0.45`），裸数字不采信
  2. 一句话里多个不同比例 → 歧义，返回 None，绝不「取第一个」
  3. 超出参数区间的值 → 写侧不收、读侧忽略（两层都要，库里已有历史脏值）

跑：python3 tests/test_param_parse.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "engine"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import reason_routes as rr  # noqa: E402


class ExplicitRatioTest(unittest.TestCase):
    """人写的那句话 → 比例。核心是「别把不相干的数字当参数」。"""

    def test_percent_next_to_number(self):
        self.assertEqual(rr._explicit_ratio("放到 40%"), 0.40)
        self.assertEqual(rr._explicit_ratio("40%"), 0.40)
        self.assertEqual(rr._explicit_ratio("放宽到 45％"), 0.45)  # 全角

    def test_week_number_before_percent_is_not_the_value(self):
        """原 bug 的原始用例：2 周 + 40% 同时出现，必须取 40%。"""
        self.assertEqual(rr._explicit_ratio("2 周后放到 40%"), 0.40)
        self.assertEqual(rr._explicit_ratio("放到 35%，2 周后再说"), 0.35)

    def test_no_unit_no_value(self):
        """没有单位的口语数字不是参数：「2 周后再说」不能变成 2%。"""
        self.assertIsNone(rr._explicit_ratio("先查主图，2 周后再说"))
        self.assertIsNone(rr._explicit_ratio("下个月再看"))
        self.assertIsNone(rr._explicit_ratio(""))
        self.assertIsNone(rr._explicit_ratio(None))

    def test_points(self):
        self.assertEqual(rr._explicit_ratio("40 个点"), 0.40)
        self.assertEqual(rr._explicit_ratio("12个点"), 0.12)
        self.assertEqual(rr._explicit_ratio("5 百分点"), 0.05)
        self.assertEqual(rr._explicit_ratio("5百分点"), 0.05)
        self.assertEqual(rr._explicit_ratio("加 5 个百分点"), 0.05)

    def test_bare_dian_time_or_item_is_not_ratio(self):
        """「12 点再看」「15 点」是时间/条目，没有「个/百分」前缀不能当百分点。"""
        self.assertIsNone(rr._explicit_ratio("12 点再看"))
        self.assertIsNone(rr._explicit_ratio("15 点"))

    def test_negative_decimal_is_parsed_with_sign_and_rejected_by_bounds(self):
        self.assertEqual(rr._explicit_ratio("-0.4"), -0.4)
        self.assertIsNone(rr._clamp("target_acos", -0.4))
        v = rr._value_for(
            type("R", (), {"param_key": "target_acos"})(),
            {"param_value": 0.35}, "-0.4", "target_acos",
        )
        self.assertEqual(v, "0.35")

    def test_decimal_alone(self):
        self.assertEqual(rr._explicit_ratio("0.45"), 0.45)

    def test_two_different_percents_is_ambiguous(self):
        """两个不同的带单位比例 → 不知道人要哪个，返回 None（退回默认值）。"""
        self.assertIsNone(rr._explicit_ratio("放到 30% 或 40%"))

    def test_same_percent_twice_is_fine(self):
        self.assertEqual(rr._explicit_ratio("40% 到 40% 都行"), 0.40)


class ClampTest(unittest.TestCase):
    """参数区间：写侧不收、读侧忽略。"""

    def test_target_acos_in_range(self):
        self.assertEqual(rr._clamp("target_acos", 0.40), 0.40)
        self.assertEqual(rr._clamp("target_acos", 0.10), 0.10)
        self.assertEqual(rr._clamp("target_acos", 0.80), 0.80)

    def test_target_acos_out_of_range(self):
        # 0.02 就是老 bug 写进去的那个值
        self.assertIsNone(rr._clamp("target_acos", 0.02))
        self.assertIsNone(rr._clamp("target_acos", 2.0))   # 「放宽到 200%」
        self.assertIsNone(rr._clamp("target_acos", 5.0))   # 「放宽到 500%」

    def test_budget_step_bounds(self):
        self.assertEqual(rr._clamp("max_budget_step", 1.05), 1.05)
        self.assertEqual(rr._clamp("max_budget_step", 1.20), 1.20)
        self.assertIsNone(rr._clamp("max_budget_step", 3.0))   # 「加 200%」
        self.assertIsNone(rr._clamp("max_budget_step", 0.05))  # 幅度被当乘数

    def test_unknown_key_passes_through(self):
        """没登记的键不做判断，保持原值（不要凭空加限制）。"""
        self.assertEqual(rr._clamp("something_else", 99.0), 99.0)
        self.assertEqual(rr._clamp(None, 99.0), 99.0)


class ValueForTest(unittest.TestCase):
    """_value_for = 手填 > pack 默认，且都要过区间。"""

    def setUp(self):
        class _R:
            param_key = "target_acos"
            table = "param_override"
            scope = "account"
            ttl_days = 90
        self.route = _R()

    def test_free_text_wins(self):
        v = rr._value_for(self.route, {"param_value": 0.35}, "放到 40%", "target_acos")
        self.assertEqual(v, "0.4")

    def test_out_of_range_falls_back_to_option(self):
        """「放宽到 500%」不收，退回 pack 里的合理默认值。"""
        v = rr._value_for(self.route, {"param_value": 0.35}, "放宽到 500%", "target_acos")
        self.assertEqual(v, "0.35")

    def test_out_of_range_without_option_is_none(self):
        self.assertIsNone(rr._value_for(self.route, None, "放宽到 500%", "target_acos"))

    def test_budget_step_additive(self):
        """人写幅度「只加 5%」→ 乘数 1.05，不是 0.05。"""
        v = rr._value_for(self.route, None, "只加 5%", "max_budget_step")
        self.assertEqual(v, "1.05")


class MoneyIsNotRatioTest(unittest.TestCase):
    """钱不是比例 —— 2026-11-26 实测出来的会花错钱的那一类。

    现场：主人写「CPA 到 $1.5 再说」（意思是别再往上加），
    老代码的 `_DEC` 抓到 `1.5` → max_budget_step=1.5，
    而 PARAM_BOUNDS 的上界恰好也是 1.5 —— clamp 放行，
    第二天 R08 真按 1.5 倍加了预算。

    判据：带 % 或「个点」的数已经绑了单位，无论句子里说什么都是比例；
    裸小数要看上下文 —— 句子里有钱的痕迹（$ ¥ € £、美元/美金/刀/块、CPA/CPC/出价…）
    就不当比例，退回 pack 的默认值。
    """

    MONEY = [
        "$1.5", "¥1.5", "€1.5", "£1.5",
        "CPA 到 $1.5 再说", "CPA 已经 $1.5 了，先别加",
        "客单价 1.5 美元", "先不加，CPA 都 1.5 了",
        "花了 1.5 刀", "CPC 1.5", "出价 1.5", "单均 1.5", "成本 1.5 块",
    ]

    RATIO = [
        ("0.45", 0.45), ("0.4", 0.4), ("1.2 倍", 1.2), ("目标 1.5 倍", 1.5),
        ("放到 40%", 0.4), ("2 周后放到 40%", 0.4), ("放宽到 45%", 0.45),
        ("加 5 个百分点", 0.05), ("只加 5%", 0.05),
    ]

    def test_money_sentences_are_not_ratios(self):
        for t in self.MONEY:
            with self.subTest(text=t):
                self.assertIsNone(rr._explicit_ratio(t),
                                  f"{t!r} 在说钱，不该被读成比例")

    def test_ratio_sentences_still_parse(self):
        """收紧不能收过头 —— 这些是真正在说比例的句子。"""
        for t, want in self.RATIO:
            with self.subTest(text=t):
                self.assertAlmostEqual(rr._explicit_ratio(t), want, places=6)

    def test_money_falls_back_to_option(self):
        """退回 pack 默认值，不是退回 None —— 上层要拿到一个能用的数。"""
        opt = {"param_value": 1.2}
        route = {"param_key": "max_budget_step"}
        for t in self.MONEY:
            with self.subTest(text=t):
                self.assertEqual(
                    rr._value_for(route, opt, t, "max_budget_step"), "1.2",
                    f"{t!r} 应该退回 pack 的 1.2，而不是拿钱当倍数")

    def test_percent_ignores_money_context(self):
        """「CPA 涨到 40% 也没关系」里的 40% 仍是比例 —— 带了单位就不看上下文。"""
        self.assertAlmostEqual(rr._explicit_ratio("CPA 占比放到 40%"), 0.4, places=6)
        self.assertAlmostEqual(rr._explicit_ratio("$1.5 但占比 45%"), 0.45, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
