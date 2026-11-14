# SPDX-License-Identifier: AGPL-3.0-only


import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from reply_budget import (
    count_clusters,
    detect_style_violations,
    evaluate_limits,
    parse_dual_segment,
    parse_evaluator_verdict,
)


class ReplyBudgetTest(unittest.TestCase):
    def test_parse_normal_and_case_whitespace(self):
        budget, reply = parse_dual_segment(
            '< BUDGET >{"bite":["饭"],"signature":"","ask":[],"drop":[],"trigger":"无"}</ BUDGET >'
            '\n< RePlY >喂，吃饭。< / RePlY >'
        )
        self.assertEqual(budget["bite"], ["饭"])
        self.assertEqual(reply, "喂，吃饭。")

    def test_parse_bad_json_soft_fails(self):
        budget, reply = parse_dual_segment("<budget>{坏}</budget><reply>正文</reply>")
        self.assertIsNone(budget)
        self.assertEqual(reply, "正文")

    def test_parse_missing_reply_uses_non_budget_text(self):
        budget, reply = parse_dual_segment('<budget>{"bite":[]}</budget>正文在这里')
        self.assertEqual(budget, {"bite": []})
        self.assertEqual(reply, "正文在这里")

    def test_parse_missing_budget_returns_clean_body(self):
        budget, reply = parse_dual_segment("<reply>只有正文</reply>")
        self.assertIsNone(budget)
        self.assertEqual(reply, "只有正文")

    def test_count_clusters_merges_same_sentence_questions(self):
        counts = count_clusters("主食几碗？汤几碗？肚子？。给我报上来。下一句呢？")

        self.assertEqual(counts["question_clusters"], 2)
        self.assertEqual(counts["imperative_clusters"], 1)
        self.assertEqual(counts["ask_cluster_total"], 2)
        self.assertEqual(counts["char_len"], len("主食几碗？汤几碗？肚子？。给我报上来。下一句呢？"))

    def test_style_violations_three_types_and_clean(self):
        self.assertEqual(detect_style_violations("**重点**"), ["bold"])
        self.assertEqual(detect_style_violations("*抬眼*"), ["stage_direction"])
        self.assertEqual(detect_style_violations("红瞳微微一眯"), ["hongtong"])
        self.assertEqual(detect_style_violations("普通正文，没有违规。"), [])
        self.assertEqual(detect_style_violations("**粗体** *动作* 红瞳危险地盯着"), ["bold", "stage_direction", "hongtong"])

    def test_unclosed_budget_never_leaks(self):
        """未闭合的 <budget> 段必须整段丢弃：宁可无预算，也不能把账本外发。"""
        budget, body = parse_dual_segment('<budget>{"bite":["a"]}\n<reply>\n你好呀\n</reply>')
        self.assertIsNone(budget)
        self.assertEqual(body, "你好呀")
        budget, body = parse_dual_segment('<budget>{"bite":["a"]}\n正文混进来了')
        self.assertIsNone(budget)
        self.assertEqual(body, "")
        self.assertNotIn("budget", body.lower())

    def test_stray_tag_in_reply_is_stripped(self):
        """模型多吐的闭合标签（正文以 </budget> 开头）必须剥掉，不能露给用户。"""
        budget, body = parse_dual_segment('<budget>{"bite":["a"]}</budget>\n<reply>\n</budget>\n\n嗯——？</reply>')
        self.assertEqual(body, "嗯——？")
        self.assertNotIn("budget", body.lower())

    def test_limits_and_verdict(self):
        self.assertEqual(
            evaluate_limits({"info_ticket_count": 3, "signature_count": 2, "ask_cluster_total": 2}, 2, 1, 1),
            [
                "information tickets exceed limit: 3>2",
                "signatures exceed limit: 2>1",
                "solicitation clusters exceed limit: 2>1",
            ],
        )
        self.assertEqual(parse_evaluator_verdict("前言\n```json\n{\"ok\": true}\n```\n后文"), {"ok": True})
        self.assertIsNone(parse_evaluator_verdict("不是 JSON"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
