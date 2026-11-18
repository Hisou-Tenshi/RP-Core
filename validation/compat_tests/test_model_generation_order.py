# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import models as m


class GenerationFirstModelOrderTest(unittest.TestCase):
    """模型列表先按代际排，显式 order 不能把新一代模型压到旧一代后面。"""

    def test_lite_primary_is_newest_generation(self):
        lite = m.provider_models("claude", "lite")
        self.assertEqual(lite[0], "claude-haiku-5-5")
        self.assertLess(
            lite.index("claude-haiku-5-5"),
            lite.index("claude-haiku-4-5"),
            f"Haiku 5.5 应排在 4.5 之前：{lite}",
        )
        self.assertEqual(m.claude_lite_primary(), "claude-haiku-5-5")

    def test_claude_lite_has_no_explicit_order(self):
        """Claude lite 不写 order，让代际决定主模型；其它厂商的 order 保持不变。"""
        lite = [s for s in m.MODELS if s.provider == "claude" and s.tier == "lite"]
        self.assertTrue(lite)
        for spec in lite:
            self.assertIsNone(spec.order, f"{spec.id} 不应再带 order")


if __name__ == "__main__":
    unittest.main()
