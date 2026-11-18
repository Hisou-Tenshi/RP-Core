# SPDX-License-Identifier: AGPL-3.0-only
"""The pre-routing rubric must carry the agreed few-shot anchors verbatim."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import config.fixed as fixed
except Exception as exc:  # pragma: no cover - harness reports the dependency
    fixed = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class DialogGradeAnchorTest(unittest.TestCase):
    def setUp(self):
        self.prompt = fixed.GROK_PRE_DIALOG_ROUTING_PROMPT

    def test_fixed_scene_is_present(self):
        self.assertIn("比那名居天子", self.prompt)
        self.assertIn("依神紫苑", self.prompt)
        self.assertIn("分食一块烤红薯", self.prompt)
        self.assertIn("核心评估原则", self.prompt)

    def test_all_five_anchors_are_present(self):
        for idx in range(1, 6):
            self.assertIn(f"锚点示例 {idx}", self.prompt)
        self.assertIn("10、24、51、78、96", self.prompt)

    def test_long_input_short_input_contrast_is_spelled_out(self):
        self.assertIn("长输入但低认知负载", self.prompt)
        self.assertIn("极短输入但高认知负载", self.prompt)
        self.assertIn("字数极少但潜台词极重", self.prompt)

    def test_risk_floor_is_documented(self):
        self.assertIn("risk_flag", self.prompt)
        self.assertIn("real_distress", self.prompt)
        self.assertIn("sensitive_plot", self.prompt)


if __name__ == "__main__":
    unittest.main()
