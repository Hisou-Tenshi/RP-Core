# SPDX-License-Identifier: AGPL-3.0-only














import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import telegram_tenshi_logger as ttl
import tenshi_config
import tenshi_utils

ADMIN_ID = 4242
USER_ID = 4242
CHAT_ID = 4242
MSG_ID = 9001


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, parse_mode=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return mock.Mock(message_id=1)

    async def send_chat_action(self, *args, **kwargs):
        return True

    async def delete_message(self, *args, **kwargs):
        return True


class _FakeProc:
    def __init__(self, returncode, on_wait=None):
        self.returncode = returncode
        self.stdout = None
        self.stderr = None
        self._on_wait = on_wait

    async def wait(self):
        if self._on_wait:
            self._on_wait()
        return self.returncode

    def kill(self):
        return None


def _mark_entry(log_path, *, no_send, answered=False):
    with open(log_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for conv in data:
        for entry in conv.get("chat_history", []):
            if str(entry.get("msg_id")) == str(MSG_ID):
                entry["replied_flag"] = True
                entry["bot_answer"] = "" if not answered else "在的。"
                if no_send:
                    entry["no_send"] = True
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class NoReplyNoticeTests(unittest.TestCase):
    def _run(self, *, returncode=0, child_marks_no_send=False, child_answers=False, notice_enabled=True):
        d = tempfile.mkdtemp()
        log_file = os.path.join(d, "tenshi_log.json")
        lock_file = log_file + ".lock"
        bot = _FakeBot()

        def on_wait():
            if child_marks_no_send or child_answers:
                _mark_entry(log_file, no_send=child_marks_no_send, answered=child_answers)

        async def fake_spawn(*args, **kwargs):
            return _FakeProc(returncode, on_wait=on_wait)

        notify_failure = mock.AsyncMock()
        recover = mock.Mock(return_value=None)

        with mock.patch.object(ttl, "Bot", return_value=bot),\
             mock.patch.object(ttl, "LOG_FILE", log_file),\
             mock.patch.object(ttl, "LOCK_FILE", lock_file),\
             mock.patch.object(ttl.tenshi_config, "ADMIN_ID", ADMIN_ID),\
             mock.patch.object(ttl.tenshi_config, "REPLY_NO_REPLY_NOTICE_ENABLED", notice_enabled),\
             mock.patch.object(ttl, "_typing_indicator_loop", new=mock.AsyncMock(return_value=None)),\
             mock.patch.object(ttl, "_stream_subprocess_output", new=mock.AsyncMock(return_value=None)),\
             mock.patch.object(ttl, "_notify_reply_failure", new=notify_failure),\
             mock.patch.object(ttl, "_recover_and_persist_partial_reply", new=recover),\
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
            asyncio.run(
                ttl.process_single_message(
                    "在吗", CHAT_ID, USER_ID, MSG_ID, timestamp=1700000000
                )
            )

        with open(log_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return bot, notify_failure, recover, saved

    def test_intentional_no_send_sends_notice_and_no_error(self):
        bot, notify_failure, recover, saved = self._run(child_marks_no_send=True)
        texts = [m["text"] for m in bot.sent]
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("不回", texts[0])
        self.assertNotIn("系统报错", texts[0])
        self.assertIsNone(bot.sent[0]["parse_mode"])
        notify_failure.assert_not_awaited()
        recover.assert_not_called()
        entry = saved[0]["chat_history"][0]
        self.assertIs(entry.get("no_send"), True)
        self.assertEqual(entry.get("bot_answer"), "")

    def test_crash_after_no_send_persisted_still_counts_as_no_send(self):
        bot, notify_failure, recover, _ = self._run(returncode=1, child_marks_no_send=True)
        texts = [m["text"] for m in bot.sent]
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("不回", texts[0])
        self.assertNotIn("系统报错", texts[0])
        notify_failure.assert_not_awaited()
        recover.assert_not_called()

    def test_missing_answer_without_marker_still_reports_failure(self):
        bot, notify_failure, recover, _ = self._run(child_marks_no_send=False)


        self.assertEqual(bot.sent, [])
        notify_failure.assert_awaited()
        self.assertEqual(notify_failure.await_args.kwargs.get("reason"), "missing_answer")
        recover.assert_called()

    def test_notice_switch_off_sends_nothing(self):
        bot, notify_failure, _, _ = self._run(child_marks_no_send=True, notice_enabled=False)
        self.assertEqual(bot.sent, [])
        notify_failure.assert_not_awaited()

    def test_is_intentional_no_send_classification(self):
        self.assertTrue(ttl.is_intentional_no_send({"no_send": True}))
        self.assertTrue(ttl.is_intentional_no_send({"no_send": "true"}))
        self.assertTrue(ttl.is_intentional_no_send({"no_send": 1}))
        self.assertFalse(ttl.is_intentional_no_send({"no_send": False}))
        self.assertFalse(ttl.is_intentional_no_send({"no_send": ""}))
        self.assertFalse(ttl.is_intentional_no_send({"bot_answer": ""}))
        self.assertFalse(ttl.is_intentional_no_send({}))
        self.assertFalse(ttl.is_intentional_no_send(None))

    def test_notice_text_uses_config_and_falls_back(self):
        default_text = tenshi_utils.format_no_reply_notice_telegram_message()
        self.assertTrue(default_text.strip())
        with mock.patch.object(tenshi_config, "REPLY_NO_REPLY_NOTICE_TEXT", "  自定义提醒  "):
            self.assertEqual(
                tenshi_utils.format_no_reply_notice_telegram_message(), "自定义提醒"
            )
        with mock.patch.object(tenshi_config, "REPLY_NO_REPLY_NOTICE_TEXT", ""):
            self.assertTrue(
                tenshi_utils.format_no_reply_notice_telegram_message().strip()
            )


if __name__ == "__main__":
    unittest.main()
