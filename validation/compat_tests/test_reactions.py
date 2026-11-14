# SPDX-License-Identifier: AGPL-3.0-only


import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import reactions
import reply_two_stage as rts


class _CfgMixin:
    """把 reactions._cfg 指到假的配置源上（不加载 .env）。"""

    def set_cfg(self, **kw):
        orig = reactions._cfg

        def fake(name, default):
            return kw[name] if name in kw else orig(name, default)

        p = mock.patch.object(reactions, "_cfg", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)


class ParseTest(_CfgMixin, unittest.TestCase):
    def test_marker_alone(self):
        body, emoji = reactions.parse("〔反应：👍〕")
        self.assertEqual(body, "")
        self.assertEqual(emoji, "👍")

    def test_marker_with_text_same_line(self):
        body, emoji = reactions.parse("哈哈〔反应：😭〕你说得对")
        self.assertEqual(emoji, "😭")
        self.assertNotIn("反应", body)
        self.assertIn("哈哈", body)
        self.assertIn("你说得对", body)

    def test_marker_on_own_segment_keeps_other_segments(self):
        body, emoji = reactions.parse("行吧<br>〔反应：👍〕<br>那我先睡了")
        self.assertEqual(emoji, "👍")
        self.assertEqual(body, "行吧<br>那我先睡了")

    def test_lenient_brackets_and_colon(self):
        for raw in ("【表情:🔥】", "[ 回应 ： 🔥 ]", "〔反应： 🔥 〕"):
            _body, emoji = reactions.parse(f"切{raw}")
            self.assertEqual(emoji, "🔥", raw)

    def test_only_first_emoji_kept(self):
        with self.assertLogs("TenshiReply", level="WARNING"):
            _body, emoji = reactions.parse("〔反应：👍〕〔反应：🔥〕")
        self.assertEqual(emoji, "👍")

    def test_unknown_emoji_dropped_not_sent(self):
        with self.assertLogs("TenshiReply", level="WARNING"):
            body, emoji = reactions.parse("〔反应：🚀〕好")
        self.assertIsNone(emoji)
        self.assertNotIn("反应", body)
        self.assertNotIn("🚀", body)
        self.assertEqual(body, "好")

    def test_no_marker_is_byte_identical(self):
        for raw in ("", "甲<br>乙", "带真换行\n的正文", "〔贴纸：嫌弃〕"):
            body, emoji = reactions.parse(raw)
            self.assertEqual(body, raw)
            self.assertIsNone(emoji)

    def test_has_and_strip(self):
        self.assertTrue(reactions.has_marker("a〔反应：🙏〕"))
        self.assertFalse(reactions.has_marker("a b"))
        self.assertFalse(reactions.has_marker(""))
        self.assertEqual(reactions.strip_markers("a〔反应：🙏〕"), "a")
        self.assertEqual(reactions.reaction_of_text("〔反应：🙏〕"), "🙏")
        self.assertIsNone(reactions.reaction_of_text("没标记"))

    def test_normalize_emoji_vs16(self):
        self.assertEqual(reactions.normalize_emoji("👍️"), "👍")
        self.assertEqual(reactions.normalize_emoji(" 👍 "), "👍")
        self.assertIsNone(reactions.normalize_emoji("🚀"))
        self.assertIsNone(reactions.normalize_emoji(""))
        self.assertIsNone(reactions.normalize_emoji(None))

    def test_marker_helper_roundtrip(self):
        self.assertEqual(reactions.marker("👍"), "〔反应：👍〕")
        self.assertEqual(reactions.parse(reactions.marker("👍")), ("", "👍"))


class PromptTest(_CfgMixin, unittest.TestCase):
    def test_enabled_by_default(self):
        self.assertTrue(reactions.enabled())
        self.assertIn("反应", reactions.typing_prompt_block())
        self.assertIn("👍", reactions.catalog_text())

    def test_disabled_block_is_empty(self):
        self.set_cfg(REPLY_REACTIONS_ENABLED=False)
        self.assertFalse(reactions.enabled())
        self.assertEqual(reactions.typing_prompt_block(), "")
        self.assertEqual(reactions.catalog_text(), "")

    def test_typing_system_explicit_reaction_block(self):
        sys_text = rts.build_typing_system(
            "CARD", "telegram", sticker_block="", reaction_block=reactions.typing_prompt_block()
        )
        self.assertIn("〔反应：", sys_text)
        self.assertIn("👍", sys_text)

        self.assertIn("（不回）", sys_text)
        self.assertIn("【打算】", sys_text)

    def test_typing_system_default_has_no_reaction_block(self):

        base = rts.build_typing_system("CARD", "telegram", sticker_block="")
        self.assertNotIn("〔反应：", base)
        self.assertIn("（不回）", base)

    def test_disabled_feature_leaves_prompt_identical(self):
        self.set_cfg(REPLY_REACTIONS_ENABLED=False)
        off_block = reactions.typing_prompt_block()
        self.assertEqual(off_block, "")
        with_off = rts.build_typing_system("CARD", "telegram", sticker_block="", reaction_block=off_block)
        base = rts.build_typing_system("CARD", "telegram", sticker_block="")
        self.assertEqual(with_off, base)


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body if body is not None else {"ok": True, "result": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class SendTest(_CfgMixin, unittest.TestCase):
    def setUp(self):
        self.posts = []

        def fake_post(url, json=None, **kw):
            self.posts.append((url, dict(json or {})))
            return _Resp(200, {"ok": True, "result": True})

        p = mock.patch.object(reactions.requests, "post", side_effect=fake_post)
        p.start()
        self.addCleanup(p.stop)

    def test_payload(self):
        ok = reactions.send(token="tk", chat_id=12345, message_id="77", emoji="👍")
        self.assertTrue(ok)
        url, payload = self.posts[0]
        self.assertTrue(url.endswith("/bottk/setMessageReaction"))
        self.assertEqual(payload["chat_id"], 12345)
        self.assertEqual(payload["message_id"], 77)
        self.assertEqual(payload["reaction"], [{"type": "emoji", "emoji": "👍"}])

    def test_bad_message_id_no_request(self):
        with self.assertLogs("TenshiReply", level="WARNING"):
            self.assertFalse(reactions.send(token="tk", chat_id=1, message_id=None, emoji="👍"))
        self.assertFalse(reactions.send(token="tk", chat_id=1, message_id="abc", emoji="👍"))
        self.assertFalse(reactions.send(token="tk", chat_id=1, message_id="0", emoji="👍"))
        self.assertEqual(self.posts, [])

    def test_unknown_emoji_no_request(self):
        with self.assertLogs("TenshiReply", level="WARNING"):
            self.assertFalse(reactions.send(token="tk", chat_id=1, message_id=9, emoji="🚀"))
        self.assertEqual(self.posts, [])

    def test_api_reject_is_soft(self):
        reactions.requests.post.side_effect = lambda url, json=None, **kw: _Resp(
            400, {"ok": False, "description": "Bad Request: message to react not found"}
        )
        with self.assertLogs("TenshiReply", level="WARNING"):
            self.assertFalse(reactions.send(token="tk", chat_id=1, message_id=9, emoji="👍"))

    def test_network_error_is_soft(self):
        reactions.requests.post.side_effect = RuntimeError("boom")
        with self.assertLogs("TenshiReply", level="WARNING"):
            self.assertFalse(reactions.send(token="tk", chat_id=1, message_id=9, emoji="👍"))

    def test_disabled_skips_request(self):
        self.set_cfg(REPLY_REACTIONS_ENABLED=False)
        with self.assertLogs("TenshiReply", level="INFO"):
            self.assertFalse(reactions.send(token="tk", chat_id=1, message_id=9, emoji="👍"))
        self.assertEqual(self.posts, [])


class _FakeBot:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def set_message_reaction(self, chat_id=None, message_id=None, reaction=None):
        self.calls.append((chat_id, message_id, reaction))
        if self.error:
            raise self.error


class SendAsyncTest(_CfgMixin, unittest.TestCase):
    def test_ok(self):
        bot = _FakeBot()
        ok = asyncio.run(reactions.send_async(bot, chat_id=1, message_id="77", emoji="👍"))
        self.assertTrue(ok)
        chat_id, message_id, reaction = bot.calls[0]
        self.assertEqual((chat_id, message_id), (1, 77))
        self.assertEqual(reaction[0].emoji, "👍")

    def test_error_is_soft(self):
        bot = _FakeBot(error=RuntimeError("forbidden"))
        with self.assertLogs("TenshiReply", level="WARNING"):
            ok = asyncio.run(reactions.send_async(bot, chat_id=1, message_id=9, emoji="👍"))
        self.assertFalse(ok)

    def test_bad_target_no_call(self):
        bot = _FakeBot()
        self.assertFalse(asyncio.run(reactions.send_async(bot, chat_id=1, message_id="", emoji="👍")))
        self.assertEqual(bot.calls, [])


class StreamerTest(_CfgMixin, unittest.TestCase):
    """流式外发层：标记绝不能漏成文字；只有表情时一条消息都不发。"""

    def setUp(self):
        import reply_streaming as rs

        self.rs = rs
        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "0"
        self.addCleanup(os.environ.pop, "REPLY_FOLLOWUP_INTERVAL_SEC", None)
        self.calls = []
        self.next_id = [100]

        def fake_post(url, json=None, **kw):
            method = url.rsplit("/", 1)[-1]
            self.calls.append((method, dict(json or {})))
            self.next_id[0] += 1
            return _Resp(200, {"ok": True, "result": {"message_id": self.next_id[0]}})

        p = mock.patch.object(rs.requests, "post", side_effect=fake_post)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(rs.time, "sleep", side_effect=lambda *_: None)
        p.start()
        self.addCleanup(p.stop)

    def _streamer(self):
        s = self.rs.TelegramStreamer("token", "chat")
        s.is_active = True
        return s

    def methods(self):
        return [m for m, _ in self.calls]

    def test_reaction_only_sends_no_message(self):
        s = self._streamer()
        s.finalize_with_text("〔反应：👍〕")
        self.assertEqual(self.methods(), [])
        self.assertEqual((s.full_text or "").strip(), "")

    def test_reaction_with_text_strips_marker(self):
        s = self._streamer()
        s.finalize_with_text("〔反应：👍〕<br>那我先睡了")
        texts = [j.get("text") for m, j in self.calls if m in ("sendMessage", "editMessageText")]
        self.assertEqual(texts, ["那我先睡了"])
        self.assertFalse(any("反应" in (t or "") for t in texts))

    def test_marker_never_sent_as_text_even_unknown(self):
        s = self._streamer()
        with self.assertLogs("TenshiReply", level="WARNING"):
            s.finalize_with_text("〔反应：🚀〕")
        self.assertEqual(self.methods(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
