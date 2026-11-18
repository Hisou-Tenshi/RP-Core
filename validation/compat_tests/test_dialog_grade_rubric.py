# SPDX-License-Identifier: AGPL-3.0-only
"""Five-dimension dialogue grading: weighted score, peak boost and the risk floor."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import grok_modules
except Exception as exc:  # pragma: no cover - harness reports the dependency
    grok_modules = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


def _parsed(**dims: int) -> dict:
    return {
        "subtext": dims.get("subtext", 0),
        "persona_pressure": dims.get("persona_pressure", 0),
        "narrative_span": dims.get("narrative_span", 0),
        "staging_craft": dims.get("staging_craft", 0),
        "tactical_logic": dims.get("tactical_logic", 0),
        "risk_flag": dims.get("risk_flag", False),
        "risk_kind": dims.get("risk_kind", ""),
        "reason": "unit-test",
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class DialogGradeRubricTest(unittest.TestCase):
    def grade(self, **dims):
        return grok_modules._build_dialog_grade_from_parsed(_parsed(**dims), "……是吗。", "")

    def test_weighted_score_and_peak_boost(self):
        self.assertEqual(self.grade(subtext=2, persona_pressure=3, narrative_span=2, staging_craft=3)["score"], 23)
        self.assertEqual(self.grade(subtext=1, persona_pressure=1, narrative_span=1, staging_craft=1)["score"], 9)

    def test_five_anchor_tiers(self):
        self.assertEqual(self.grade(subtext=1, persona_pressure=1, narrative_span=1, staging_craft=1)["tier"], "minimal")
        self.assertEqual(self.grade(subtext=2, persona_pressure=3, narrative_span=2, staging_craft=3)["tier"], "low")
        self.assertEqual(self.grade(subtext=5, persona_pressure=6, narrative_span=5, staging_craft=5, tactical_logic=2)["tier"], "medium")
        self.assertEqual(self.grade(subtext=9, persona_pressure=8, narrative_span=8, staging_craft=7, tactical_logic=4)["tier"], "high")
        self.assertEqual(self.grade(subtext=10, persona_pressure=10, narrative_span=10, staging_craft=9, tactical_logic=8)["tier"], "max")

    def test_short_high_load_beats_long_low_load(self):
        long_low = self.grade(subtext=2, persona_pressure=3, narrative_span=2, staging_craft=3)
        short_high = self.grade(subtext=9, persona_pressure=8, narrative_span=8, staging_craft=7, tactical_logic=4)
        self.assertGreater(short_high["score"], long_low["score"])

    def test_risk_flag_floors_the_tier_at_high(self):
        grade = self.grade(risk_flag=True, risk_kind="real_distress")
        self.assertTrue(grade["risk_flag"])
        self.assertEqual(grade["risk_kind"], "real_distress")
        self.assertEqual(grade["tier"], "high")
        self.assertIn("risk_flag:floor_high", grade["reason"])

    def test_risk_flag_keeps_max(self):
        grade = self.grade(
            subtext=10,
            persona_pressure=10,
            narrative_span=10,
            staging_craft=9,
            tactical_logic=8,
            risk_flag=True,
        )
        self.assertEqual(grade["tier"], "max")

    def test_unknown_risk_kind_is_dropped(self):
        grade = self.grade(risk_flag=True, risk_kind="something_else")
        self.assertEqual(grade["risk_kind"], "")


if __name__ == "__main__":
    unittest.main()
