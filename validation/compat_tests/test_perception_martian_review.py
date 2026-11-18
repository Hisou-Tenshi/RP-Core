# SPDX-License-Identifier: AGPL-3.0-only
"""感知层终审：火星文命中时按修正稿改写，没有修正稿就拒发。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import perception_layer as pl
import reply_two_stage


class MartianReviewFixTest(unittest.TestCase):
    def test_clean_verdict_keeps_body(self):
        body = "嗯，知道了。"
        self.assertEqual(
            pl._apply_martian_review_fix({"martian": False, "martian_fix": ""}, body), body
        )
        self.assertEqual(pl._apply_martian_review_fix(None, body), body)
        self.assertEqual(pl._apply_martian_review_fix({}, body), body)

    def test_martian_verdict_returns_cleaned_fix(self):
        review = {"martian": True, "martian_fix": "算你厉害，我看见了。<br>去睡吧。"}
        self.assertEqual(
            pl._apply_martian_review_fix(review, "扌算你厉害，睝见了。"),
            "算你厉害，我看见了。<br>去睡吧。",
        )

    def test_fix_with_choice_layer_markup_is_stripped(self):
        review = {
            "martian": True,
            "martian_fix": "<budget>{}</budget>算你厉害。",
        }
        self.assertEqual(
            pl._apply_martian_review_fix(review, "扌算你厉害。"), "算你厉害。"
        )

    def test_martian_without_fix_is_suppressed(self):
        with self.assertRaises(reply_two_stage.UnsafeResponseError) as caught:
            pl._apply_martian_review_fix({"martian": True, "martian_fix": "  "}, "扌算。")
        self.assertEqual(caught.exception.reason, "martian_unfixed")

    def test_unsafe_fix_is_refused(self):
        review = {"martian": True, "martian_fix": "the user's message 算你厉害。"}
        with self.assertRaises(reply_two_stage.UnsafeResponseError) as caught:
            pl._apply_martian_review_fix(review, "扌算你厉害。")
        self.assertEqual(caught.exception.reason, "internal_reasoning")


if __name__ == "__main__":
    unittest.main()
