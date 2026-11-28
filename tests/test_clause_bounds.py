#!/usr/bin/env python3
"""断言阈值的物理边界（converge._parse_clauses）。

背景（2026-11-26 核实）：`_parse_clauses` 一直是「正则抓到什么就信什么」，
裸 `float()` 之后直接拿去比。三种写法会静默污染兑现率：

  `cost ≥ 999999999999 且 orders ≤ 0`  → 恒判 wrong（永远花不到一万亿）
  `clicks ∈ [0, 0]`                    → 恒判 correct（任何点击都「在区间内」）
  `cvr < 99999%`                       → 折成 9.9999，比任何真实转化率都大

兑现率是人判断「这个 agent 靠不靠谱」的唯一依据，污染它等于让人拿假数据
做去留决定。这一组测试钉死：越界一律返回空列表让模型接手，绝不钳位 ——
钳到边界会造出一个「看起来能判」的假断言，比不判更糟。

跑：python3 tests/test_clause_bounds.py
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_converge():
    """按路径加载 converge.py。

    不能用 `sys.path.insert(0, ROOT/"adsdesk"/"agents")` —— 那会让顶层名
    `ingest` 被 `ingest/ingest.py` 抢走，`from ingest import jijia_client` 直接炸。
    """
    spec = importlib.util.spec_from_file_location(
        "converge_under_test", ROOT / "adsdesk" / "agents" / "converge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = _load_converge()


class OutOfBoundsTest(unittest.TestCase):
    """越界的断言不能变成「能判的断言」。"""

    def test_huge_value_is_rejected(self):
        # 一万亿的花费在任何真实账户里都不可能达到 → 恒 wrong
        self.assertEqual(cv._parse_clauses("cost ≥ 999999999999 且 orders ≤ 0"), [])

    def test_just_over_upper_bound_is_rejected(self):
        self.assertEqual(cv._parse_clauses("cost ≥ 100000000 且 orders ≤ 0"), [])

    def test_degenerate_interval_is_rejected(self):
        # [0, 0] 恒真：任何点击都满足 `>= 0 且 <= 0`？不 —— 只有 0 击满足。
        # 但「0 击」在关键词层几乎不会出现，于是它近似恒假；真正的问题是
        # [5, 5] 这种「精确等于」写法，和 lo >= hi 一样是解析/打字错误的信号。
        self.assertEqual(cv._parse_clauses("clicks ∈ [0, 0]"), [])
        self.assertEqual(cv._parse_clauses("clicks ∈ [5, 5]"), [])

    def test_inverted_interval_is_rejected(self):
        self.assertEqual(cv._parse_clauses("clicks ∈ [300, 100]"), [])

    def test_negative_threshold_is_rejected(self):
        self.assertEqual(cv._parse_clauses("orders ≤ -5"), [])

    def test_percent_double_conversion_is_rejected(self):
        # `%` 已折一次，又按 cvr 折第二次 → 999.99 → 9.9999，落进上界内。
        # 这个双重折算真踩过，所以单独钉一条。
        self.assertEqual(cv._parse_clauses("cvr < 99999%"), [])


class NormalStillParsesTest(unittest.TestCase):
    """加了边界之后，正常断言一条都不能少。"""

    def test_simple_conjunction(self):
        self.assertEqual(
            cv._parse_clauses("cost ≥ 90 且 orders ≤ 2"),
            [("cost", "ge", 90.0), ("orders", "le", 2.0)],
        )

    def test_interval(self):
        self.assertEqual(
            cv._parse_clauses("clicks ∈ [180, 330] 且 orders ≤ 1"),
            [("clicks", "ge", 180.0), ("clicks", "le", 330.0), ("orders", "le", 1.0)],
        )

    def test_cvr_percent(self):
        self.assertEqual(cv._parse_clauses("cvr < 1%"), [("cvr", "lt", 0.01)])
        self.assertEqual(cv._parse_clauses("cvr ≥ 2.5%"), [("cvr", "ge", 0.025)])

    def test_cvr_bare_number_means_percent(self):
        # `cvr < 1` 是 1% 不是 100%。按 1% 判比按 100% 判更严，
        # 不会把「转化率 0.5%」错判成满足 `< 1.0`。
        self.assertEqual(cv._parse_clauses("cvr < 1"), [("cvr", "lt", 0.01)])
        self.assertEqual(cv._parse_clauses("cvr < 0.010"), [("cvr", "lt", 0.01)])

    def test_cvr_full_percent_is_one(self):
        self.assertEqual(cv._parse_clauses("cvr < 100%"), [("cvr", "lt", 1.0)])

    def test_acos(self):
        self.assertEqual(cv._parse_clauses("acos ≥ 30%"), [("acos", "ge", 0.3)])
        self.assertEqual(cv._parse_clauses("acos ≥ 0.3"), [("acos", "ge", 0.3)])


class ArithmeticStillBlockedTest(unittest.TestCase):
    """`_ARITH` 的拦截不能被这一轮改动碰掉（它比边界更早、更重要）。"""

    def test_ratio_is_not_split(self):
        # `orders / clicks < 0.010` 曾被读成 `clicks < 0.01`，把「转化率低于 1%」
        # 判成「点击数低于 0.01 击」，任何有数据的实体都判 wrong。
        self.assertEqual(cv._parse_clauses("orders / clicks < 0.010"), [])

    def test_layer_difference_is_not_split(self):
        self.assertEqual(cv._parse_clauses("placement.clicks − campaign.clicks ∈ [40, 120]"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
