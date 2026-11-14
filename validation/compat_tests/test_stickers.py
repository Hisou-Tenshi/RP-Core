# SPDX-License-Identifier: AGPL-3.0-only


import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import reply_two_stage as rts
import stickers

FAKE_LIB = {
    "version": 1,
    "categories": ["开心得意", "嫌弃无语"],
    "stickers": [
        {"name": "嫌弃", "category": "嫌弃无语", "desc": "嫌弃地斜眼看你", "text": "",
         "file_id": "FID_XQ", "file_unique_id": "UID_XQ", "set": "s", "index": 1},
        {"name": "得意", "category": "开心得意", "desc": "叉腰得意", "text": "",
         "file_id": "FID_DY", "file_unique_id": "UID_DY", "set": "s", "index": 2},
        {"name": "无语", "category": "嫌弃无语", "desc": "", "text": "……",
         "file_id": "FID_WY", "file_unique_id": "UID_WY", "set": "s", "index": 3},
    ],
}


class _LibMixin:
    """把贴纸库指到临时假库；开关/上限按需打补丁。"""

    def use_lib(self, data=FAKE_LIB):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "stickers.json")
        if data is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        p = mock.patch.object(stickers, "LIBRARY_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(stickers.reload)
        stickers.reload()

    def set_cfg(self, **kw):
        orig = stickers._cfg

        def fake(name, default):
            return kw[name] if name in kw else orig(name, default)

        p = mock.patch.object(stickers, "_cfg", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        stickers.reload()


class ParseTest(_LibMixin, unittest.TestCase):
    def setUp(self):
        self.use_lib()

    def test_lenient_syntax(self):
        for t in ("〔贴纸：嫌弃〕", "【贴纸:嫌弃】", "[ 贴纸 ： 嫌弃 ]", "〔贴纸: 嫌弃 〕"):
            with self.subTest(t=t):
                self.assertEqual(stickers.normalize_reply(t), "〔贴纸：嫌弃〕")

    def test_inline_split_into_three(self):
        self.assertEqual(
            stickers.segment_reply("切【贴纸：嫌弃】你自己玩吧"),
            [("text", "切"), ("sticker", "嫌弃"), ("text", "你自己玩吧")],
        )
        self.assertEqual(stickers.normalize_reply("切〔贴纸：嫌弃〕你自己玩吧"), "切<br>〔贴纸：嫌弃〕<br>你自己玩吧")

    def test_unknown_name_removed_with_warning(self):
        with self.assertLogs("TenshiReply", level="WARNING") as cm:
            out = stickers.normalize_reply("哼<br>〔贴纸：不存在的〕<br>行吧")
        self.assertEqual(out, "哼<br>行吧")
        self.assertTrue(any("未知贴纸" in m for m in cm.output))

    def test_cap_per_turn(self):
        self.set_cfg(REPLY_STICKERS_MAX_PER_TURN=2)
        out = stickers.normalize_reply("〔贴纸：嫌弃〕<br>〔贴纸：得意〕<br>〔贴纸：无语〕<br>哼")
        self.assertEqual(out, "〔贴纸：嫌弃〕<br>〔贴纸：得意〕<br>哼")

    def test_no_marker_untouched(self):
        for t in ("哦<br>行", "【打算】不是贴纸", "[1] 列表", "a\n\nb"):
            self.assertEqual(stickers.normalize_reply(t), t)

    def test_parse_typed_sticker_only_is_reply(self):
        self.assertEqual(rts.parse_typed_with_stickers("【打算】懒得说话\n【贴纸:无语】"), ("〔贴纸：无语〕", False))

    def test_parse_typed_all_unknown_is_no_send(self):
        self.assertEqual(rts.parse_typed_with_stickers("【打算】x\n〔贴纸：没有这张〕"), ("", True))

    def test_parse_typed_plain_same_as_before(self):
        for t in ("【打算】损他\n哦<br>行", "（不回）", "（3分钟前） 哦<br>行"):
            self.assertEqual(rts.parse_typed_with_stickers(t), rts.parse_typed(t))

    def test_split_for_send_isolates_after_cap_merge(self):

        text = "<br>".join(["a", "b", "〔贴纸：嫌弃〕", "c"])
        with mock.patch("message_split._cap_total", side_effect=lambda segs, n: segs[:1] + ["\n".join(segs[1:])]):
            segs = stickers.split_for_send(text)
        self.assertEqual(segs, ["a", "b", "〔贴纸：嫌弃〕", "c"])

    def test_lookup(self):
        self.assertEqual(stickers.by_file_unique_id("UID_DY")["name"], "得意")
        self.assertEqual(stickers.by_file_id("FID_XQ")["name"], "嫌弃")
        self.assertIsNone(stickers.by_name("nope"))

    def test_alias_from_desc_quote_resolves_to_name(self):
        lib = json.loads(json.dumps(FAKE_LIB))
        lib["stickers"].append({"name": "是这样吗", "category": "嫌弃无语", "desc": "「是——这样吗——w」拖长音阴阳怪气",
                                "text": "", "file_id": "FID_SZ", "file_unique_id": "UID_SZ", "set": "s", "index": 4})

        lib["stickers"].append({"name": "看看", "category": "嫌弃无语", "desc": "「看看」凑过来", "text": "",
                                "file_id": "FID_K1", "file_unique_id": "UID_K1", "set": "s", "index": 5})
        lib["stickers"].append({"name": "盯", "category": "嫌弃无语", "desc": "「看看」疯狂探头", "text": "",
                                "file_id": "FID_K2", "file_unique_id": "UID_K2", "set": "s", "index": 6})
        self.use_lib(data=lib)
        self.assertEqual(stickers.normalize_reply("哼<br>〔贴纸：是——这样吗——w〕"), "哼<br>〔贴纸：是这样吗〕")
        self.assertEqual(stickers.sticker_name_of_segment("〔贴纸：是这样吗w〕"), "是这样吗")
        self.assertEqual(stickers.by_name("看看")["name"], "看看")
        self.assertIsNone(stickers.by_name("疯狂探头"))

    def test_missing_library_disables(self):
        self.use_lib(data=None)
        self.assertFalse(stickers.available())
        self.assertEqual(stickers.typing_prompt_block(), "")

        self.assertEqual(stickers.normalize_reply("哼<br>〔贴纸：嫌弃〕"), "哼")


class PromptTest(_LibMixin, unittest.TestCase):
    def _expected_plain(self, card):
        proto = rts.TYPING_PROTOCOL.replace("{medium}", rts.MEDIUM_TELEGRAM)
        return f"{card.strip()}\n\n{proto}"

    def test_disabled_prompt_byte_identical(self):
        self.use_lib()
        self.set_cfg(REPLY_STICKERS_ENABLED=False)
        self.assertEqual(rts.build_typing_system("CARD"), self._expected_plain("CARD"))

    def test_missing_library_prompt_byte_identical(self):
        self.use_lib(data=None)
        self.assertEqual(rts.build_typing_system("CARD"), self._expected_plain("CARD"))

    def test_enabled_prompt_has_catalog_after_medium(self):
        self.use_lib()
        s = rts.build_typing_system("CARD")
        self.assertIn(rts.MEDIUM_TELEGRAM, s)
        self.assertGreater(s.index("〔贴纸：短名〕"), s.index(rts.MEDIUM_TELEGRAM))
        self.assertLess(s.index("嫌弃：嫌弃地斜眼看你"), s.index("输出分两部分"))

        self.assertLess(s.index("【开心得意】"), s.index("【嫌弃无语】"))
        self.assertIn("\n无语\n", s + "\n")
        self.assertNotIn("适当使用", s)


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 1}}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class StreamerTest(_LibMixin, unittest.TestCase):
    def setUp(self):
        import reply_streaming as rs

        self.rs = rs
        self.use_lib()
        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "0"
        self.addCleanup(os.environ.pop, "REPLY_FOLLOWUP_INTERVAL_SEC", None)
        self.calls = []
        self.sleeps = []
        self.next_id = [100]
        self.fail_sticker = False

        def fake_post(url, json=None, **kw):
            method = url.rsplit("/", 1)[-1]
            self.calls.append((method, dict(json or {})))
            if method == "sendSticker" and self.fail_sticker:
                return _Resp(400, {"ok": False, "description": "Bad Request: wrong file"})
            if method in ("sendMessage", "sendSticker"):
                self.next_id[0] += 1
                return _Resp(200, {"ok": True, "result": {"message_id": self.next_id[0]}})
            return _Resp(200, {"ok": True, "result": True})

        for target, fn in ((rs.requests, "post"), (rs.time, "sleep")):
            p = mock.patch.object(target, fn, side_effect=fake_post if fn == "post" else self.sleeps.append)
            p.start()
            self.addCleanup(p.stop)

    def _streamer(self):
        s = self.rs.TelegramStreamer("token", "chat")
        s.is_active = True
        return s

    def methods(self):
        return [m for m, _ in self.calls]

    def test_text_then_sticker(self):
        s = self._streamer()
        s.finalize_with_text("切<br>〔贴纸：嫌弃〕")
        self.assertEqual(self.methods(), ["sendMessage", "sendChatAction", "sendSticker"])
        self.assertEqual(self.calls[0][1]["text"], "切")
        self.assertEqual(self.calls[1][1]["action"], "choose_sticker")
        self.assertEqual(self.calls[2][1]["sticker"], "FID_XQ")
        self.assertIn(1.0, self.sleeps)
        self.assertEqual(s.message_id, 101)
        self.assertFalse(any("贴纸" in (j.get("text") or "") for _, j in self.calls))

    def test_first_segment_sticker_no_edit(self):
        s = self._streamer()
        s.finalize_with_text("〔贴纸：得意〕<br>就这？")
        self.assertEqual(self.methods(), ["sendChatAction", "sendSticker", "sendMessage"])
        self.assertNotIn("editMessageText", self.methods())
        self.assertEqual(s.message_id, 101)
        self.assertEqual(self.calls[2][1]["text"], "就这？")
        self.assertNotIn(1.0, self.sleeps)

    def test_first_sticker_deletes_existing_stream_message(self):
        s = self._streamer()
        s.message_id = 55
        s.finalize_with_text("〔贴纸：得意〕")
        self.assertEqual(self.methods()[0], "deleteMessage")
        self.assertNotIn("editMessageText", self.methods())
        self.assertIn("sendSticker", self.methods())
        self.assertEqual(s.message_id, 101)

    def test_sticker_failure_skipped_not_text(self):
        self.fail_sticker = True
        s = self._streamer()
        with self.assertLogs("TenshiReply", level="WARNING"):
            s.finalize_with_text("哼<br>〔贴纸：嫌弃〕<br>算了")
        texts = [j.get("text") for m, j in self.calls if m == "sendMessage"]
        self.assertEqual(texts, ["哼", "算了"])
        self.assertEqual(self.methods().count("sendSticker"), 2)

    def test_sticker_429_waits_then_sends(self):
        seq = [_Resp(429, {"ok": False, "parameters": {"retry_after": 3}})]
        orig = self.rs.requests.post.side_effect

        def post(url, json=None, **kw):
            if url.endswith("sendSticker") and seq:
                self.calls.append(("sendSticker", dict(json)))
                return seq.pop()
            return orig(url, json=json, **kw)

        self.rs.requests.post.side_effect = post
        s = self._streamer()
        s.finalize_with_text("〔贴纸：嫌弃〕")
        self.assertEqual(self.methods().count("sendSticker"), 2)
        self.assertIn(3.0, self.sleeps)
        self.assertEqual(s.message_id, 101)

    def test_only_unknown_marker_sends_nothing(self):
        s = self._streamer()
        s.finalize_with_text("〔贴纸：没有〕")
        self.assertNotIn("sendMessage", self.methods())
        self.assertNotIn("sendSticker", self.methods())

    def test_no_marker_path_unchanged(self):
        s = self._streamer()
        s.finalize_with_text("甲<br>乙")
        self.assertEqual([j.get("text") for _, j in self.calls], ["甲", "乙"])


class NsfwRewriteTest(_LibMixin, unittest.TestCase):
    def setUp(self):
        self.use_lib()

    def test_rewrite_drops_marker_restored(self):
        body, slots = stickers.extract_for_rewrite("切<br>〔贴纸：嫌弃〕<br>滚啦")
        self.assertEqual(body, "切<br>滚啦")
        self.assertNotIn("贴纸", body)

        self.assertEqual(stickers.restore_after_rewrite("哼<br>走开", slots), "哼<br>〔贴纸：嫌弃〕<br>走开")

    def test_rewrite_invented_marker_removed_and_merge_mismatch(self):
        _, slots = stickers.extract_for_rewrite("〔贴纸：得意〕<br>a<br>b")
        self.assertEqual(stickers.restore_after_rewrite("x〔贴纸：乱编〕", slots), "〔贴纸：得意〕<br>x")
        self.assertEqual(stickers.restore_after_rewrite("", slots), "〔贴纸：得意〕")

    def test_no_marker_passthrough(self):
        self.assertEqual(stickers.extract_for_rewrite("哦<br>行"), ("哦<br>行", []))
        self.assertEqual(stickers.restore_after_rewrite("改写后", []), "改写后")


class IncomingTest(_LibMixin, unittest.TestCase):
    def setUp(self):
        self.use_lib()
        import telegram_tenshi_logger as ttl

        self.ttl = ttl

    def _bot(self, uid):
        bot = mock.MagicMock()
        f = mock.MagicMock()
        f.file_unique_id = uid
        bot.get_file = mock.AsyncMock(return_value=f)
        bot.send_chat_action = mock.AsyncMock()
        return bot

    def test_hit_by_unique_id(self):
        bot = self._bot("UID_XQ")
        hit = asyncio.run(self.ttl._match_known_sticker(bot, "OTHER_FID"))
        self.assertEqual(hit["name"], "嫌弃")
        self.assertEqual(stickers.incoming_marker(hit), "〔贴纸：嫌弃〕")

    def test_hit_by_file_id_skips_getfile(self):
        bot = self._bot("x")
        hit = asyncio.run(self.ttl._match_known_sticker(bot, "FID_DY"))
        self.assertEqual(hit["name"], "得意")
        bot.get_file.assert_not_called()

    def test_miss(self):
        self.assertIsNone(asyncio.run(self.ttl._match_known_sticker(self._bot("UNKNOWN"), "F")))

    def test_disabled_no_match(self):
        self.set_cfg(REPLY_STICKERS_ENABLED=False)
        self.assertIsNone(asyncio.run(self.ttl._match_known_sticker(self._bot("UID_XQ"), "FID_XQ")))

    def test_annotate_for_context(self):
        self.assertEqual(
            stickers.annotate_user_markers("〔贴纸：嫌弃〕"),
            "〔贴纸：嫌弃〕（这张贴纸：嫌弃地斜眼看你）",
        )
        self.assertEqual(stickers.annotate_user_markers("〔贴纸：无语〕"), "〔贴纸：无语〕")
        self.assertEqual(stickers.annotate_user_markers("普通话"), "普通话")

    def test_transcript_shows_marker(self):
        t = rts.transcript_from_payload([
            {"role": "user", "content": "（刚刚） 〔贴纸：嫌弃〕（这张贴纸：嫌弃地斜眼看你）"},
            {"role": "assistant", "content": "切<br>〔贴纸：得意〕"},
        ])
        self.assertIn("他：（刚刚） 〔贴纸：嫌弃〕", t)
        self.assertIn("我：切<br>〔贴纸：得意〕", t)

    def test_fallback_bubbles_split_sticker(self):
        self.assertEqual(self.ttl.split_reply_into_bubbles("切〔贴纸：嫌弃〕"), ["切", "〔贴纸：嫌弃〕"])
        self.assertEqual(self.ttl.split_reply_into_bubbles("〔贴纸：没有〕"), [])

    def test_process_single_message_sticker_hit_logs_marker(self):
        """端到端：命中库的贴纸不下载，user_query 记成标记。"""
        ttl = self.ttl
        d = tempfile.mkdtemp()
        log_file = os.path.join(d, "log.json")
        bot = self._bot("UID_XQ")
        with mock.patch.object(ttl, "Bot", return_value=bot),\
             mock.patch.object(ttl, "LOG_FILE", log_file),\
             mock.patch.object(ttl, "LOCK_FILE", log_file + ".lock"),\
             mock.patch.object(ttl.tenshi_config, "ADMIN_ID", 1),\
             mock.patch.object(ttl, "download_file", new=mock.AsyncMock(return_value=None)) as dl,\
             mock.patch.dict(os.environ, {"MSG_FILE_IDS": ""}),\
             mock.patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("stop")):
            try:
                asyncio.run(ttl.process_single_message("", 1, 1, 7, file_id="FID_X", file_type="sticker",
                                                       file_name="sticker.webp", timestamp=1700000000))
            except Exception:
                pass
            dl.assert_not_called()
        with open(log_file, encoding="utf-8") as f:
            logs = json.load(f)
        self.assertEqual(logs[0]["chat_history"][0]["user_query"], "〔贴纸：嫌弃〕")


if __name__ == "__main__":
    unittest.main()
