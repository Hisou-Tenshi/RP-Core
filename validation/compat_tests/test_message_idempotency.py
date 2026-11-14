# SPDX-License-Identifier: AGPL-3.0-only








from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from telegram_tenshi_logger import message_already_replied


class TestMessageAlreadyReplied(unittest.TestCase):
    def test_empty_history(self):
        self.assertFalse(message_already_replied([], "14475"))
        self.assertFalse(message_already_replied(None, "14475"))

    def test_replied_flag_true(self):
        history = [
            {
                "msg_id": "14475",
                "user_query": "（对方发来了一张图，没有配文字。）",
                "replied_flag": "True",
                "bot_answer": "绘马架啊",
            }
        ]
        self.assertTrue(message_already_replied(history, "14475"))

        self.assertTrue(message_already_replied(history, 14475))

    def test_bot_answer_without_flag(self):
        history = [{"msg_id": "14476", "bot_answer": "那就好"}]
        self.assertTrue(message_already_replied(history, "14476"))

    def test_logged_but_not_replied_prefers_retry(self):

        history = [
            {"msg_id": "14477", "user_query": "在吗", "replied_flag": "False"},
            {"msg_id": "14478", "user_query": "另一条"},
        ]
        self.assertFalse(message_already_replied(history, "14477"))

    def test_different_msg_id(self):
        history = [{"msg_id": "14475", "replied_flag": "True", "bot_answer": "x"}]
        self.assertFalse(message_already_replied(history, "14476"))

    def test_malformed_entries(self):
        history = [None, "junk", 42, {"msg_id": None}, {"msg_id": "14475"}]
        self.assertFalse(message_already_replied(history, "14475"))


if __name__ == "__main__":
    unittest.main()
