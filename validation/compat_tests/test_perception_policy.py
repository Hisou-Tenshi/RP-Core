# SPDX-License-Identifier: AGPL-3.0-only




from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from perception_policy import (
    _extract_silence_from_final_text,
    _is_real_user_message,
    _line_declares_silence,
    _normalize_for_silence_compare,
    _perception_bot_answer_is_silence_only,
    _strip_leading_log_timestamp_prefix,
)


class RealUserMessageTest(unittest.TestCase):
    """感知层合成记录不能算作「用户说了话」，否则静默时长会被重置。

    说明：全量日志/归档扫描显示线上从未出现「带合成前缀却缺 perception_event」
    的条目，因此这不是在修复一个已发生的故障，而是把不变量写成显式判据，
    避免以后字段形态变化（或人工补录）时静默失去保护。
    """

    def test_plain_user_message_is_real(self):
        self.assertTrue(_is_real_user_message({"time": 1, "user_query": "早"}))
        self.assertTrue(_is_real_user_message({"time": 1, "user_query": "  多行\n消息  "}))

    def test_perception_event_is_never_real_user(self):
        self.assertFalse(_is_real_user_message({
            "time": 1, "perception_event": True, "user_query": "早",
        }))

    def test_proactive_synthetic_query_is_not_real(self):
        self.assertFalse(_is_real_user_message({
            "time": 1, "user_query": "[系统级感知层触发 - 天子主动起念]",
        }))

    def test_silence_synthetic_query_is_not_real(self):
        self.assertFalse(_is_real_user_message({
            "time": 1, "user_query": "[感知层上报世界状态，天子选择了沉默不语]",
        }))

    def test_empty_or_missing_query_is_not_real(self):
        self.assertFalse(_is_real_user_message({"time": 1}))
        self.assertFalse(_is_real_user_message({"time": 1, "user_query": ""}))
        self.assertFalse(_is_real_user_message({"time": 1, "user_query": "   "}))
        self.assertFalse(_is_real_user_message({"time": 1, "user_query": None}))
        self.assertFalse(_is_real_user_message(None))
        self.assertFalse(_is_real_user_message("not a dict"))

    def test_marker_inside_text_is_still_real(self):
        """前缀判定只认开头：用户正文里引用过这个词，仍算真实消息。"""
        self.assertTrue(_is_real_user_message({
            "time": 1, "user_query": "刚才那条[系统级感知层触发]是你自己发的吗",
        }))


class SilencePolicyTest(unittest.TestCase):
    def test_strip_leading_log_timestamp_prefix(self):
        s = _strip_leading_log_timestamp_prefix("[2026-08-24 02:11:00 UTC+8] hello")
        self.assertEqual(s, "hello")
        self.assertEqual(_strip_leading_log_timestamp_prefix("no prefix"), "no prefix")
        self.assertEqual(_strip_leading_log_timestamp_prefix(""), "")
        self.assertEqual(_strip_leading_log_timestamp_prefix(None), None)

    def test_normalize_for_silence_compare(self):
        self.assertEqual(_normalize_for_silence_compare("`SILENCE`"), "SILENCE")
        self.assertEqual(_normalize_for_silence_compare("**SILENCE**"), "SILENCE")
        self.assertEqual(_normalize_for_silence_compare("*SILENCE*"), "SILENCE")
        self.assertEqual(_normalize_for_silence_compare("  SILENCE  "), "SILENCE")
        self.assertEqual(_normalize_for_silence_compare("keep me"), "keep me")

    def test_line_declares_silence(self):
        self.assertTrue(_line_declares_silence("SILENCE"))
        self.assertTrue(_line_declares_silence("`SILENCE`"))
        self.assertTrue(_line_declares_silence("**silence**"))
        self.assertFalse(_line_declares_silence("SILENCE, but wait"))
        self.assertFalse(_line_declares_silence("I will talk"))

    def test_bot_answer_is_silence_only(self):
        self.assertTrue(_perception_bot_answer_is_silence_only("SILENCE"))
        self.assertTrue(_perception_bot_answer_is_silence_only("`SILENCE`"))
        self.assertTrue(_perception_bot_answer_is_silence_only(""))
        self.assertTrue(_perception_bot_answer_is_silence_only("   "))
        self.assertFalse(_perception_bot_answer_is_silence_only("hello there"))
        self.assertFalse(_perception_bot_answer_is_silence_only("line1\nSILENCE"))
        self.assertFalse(_perception_bot_answer_is_silence_only(None))

    def test_extract_silence_from_final_text(self):
        self.assertEqual(_extract_silence_from_final_text("SILENCE"), (True, ""))
        self.assertEqual(_extract_silence_from_final_text("`SILENCE`"), (True, ""))
        self.assertEqual(_extract_silence_from_final_text(""), (True, ""))
        self.assertEqual(_extract_silence_from_final_text(None), (False, ""))
        self.assertEqual(_extract_silence_from_final_text("talk to me"), (False, "talk to me"))

        ok, body = _extract_silence_from_final_text("some thought\nSILENCE")
        self.assertTrue(ok)
        self.assertEqual(body, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
