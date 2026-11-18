# SPDX-License-Identifier: AGPL-3.0-only


import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from message_split import (
    chunk_to_limit,
    describe_split,
    split_reply,
    typed_newlines_to_br,
    unescape_model_text,
    MAX_MESSAGE_CHARS,
)


class TestSplitReply(unittest.TestCase):
    def test_no_marker_single_message(self):
        """没有标记就发一条:这条是回归底线,别把普通回复切成几条。"""
        text = "喂，看这儿。比那名居天子，天界来的，记住没？"
        self.assertEqual(split_reply(text), [text])

    def test_chunk_long_false_no_marker_never_splits(self):
        """感知层口径(chunk_long=False):没有 <br> 就整条一条,超长也不按限切。"""
        long_text = "喂，进来了就好——路上折腾够呛吧？" * 300
        self.assertGreater(len(long_text), MAX_MESSAGE_CHARS)
        self.assertEqual(split_reply(long_text, chunk_long=False), [long_text])

    def test_chunk_long_false_short_matches_default(self):
        """无标记的普通长度文本,两种口径都是一条,行为一致。"""
        text = "喂，看这儿。比那名居天子，天界来的，记住没？"
        self.assertEqual(split_reply(text, chunk_long=False), [text])

    def test_split_for_send_turns_blank_lines_into_separate_messages(self):
        import stickers

        self.assertEqual(stickers.split_for_send("甲\n\n乙"), ["甲", "乙"])
        self.assertEqual(stickers.split_for_send("甲\n乙"), ["甲", "乙"])

    def test_chunk_long_false_still_splits_on_marker(self):
        """chunk_long=False 只关超长切,不关 <br> 切分。"""
        self.assertEqual(split_reply("甲<br>乙", chunk_long=False), ["甲", "乙"])

    def test_default_still_chunks_long(self):
        """默认口径(主对话)超长仍按限切,每段不超上限——回归保护。"""
        long_text = "喂，进来了就好——路上折腾够呛吧？" * 300
        segs = split_reply(long_text)
        self.assertGreater(len(segs), 1)
        for s in segs:
            self.assertLessEqual(len(s), MAX_MESSAGE_CHARS)

    def test_empty(self):
        self.assertEqual(split_reply(""), [])
        self.assertEqual(split_reply("   \n  "), [])

    def test_basic_two_and_three(self):
        self.assertEqual(
            split_reply("八点到机场？<br>那你还磨蹭什么，赶紧走"),
            ["八点到机场？", "那你还磨蹭什么，赶紧走"],
        )
        self.assertEqual(
            split_reply("行啊你<br>山茶花戴着挺乖<br>过关别弄丢了"),
            ["行啊你", "山茶花戴着挺乖", "过关别弄丢了"],
        )

    def test_marker_variants(self):
        for marker in ("<br>", "<br/>", "<br />", "</br>", "<BR>", "< br >"):
            with self.subTest(marker=marker):
                self.assertEqual(split_reply(f"甲{marker}乙"), ["甲", "乙"])

    def test_marker_never_leaks(self):
        """标记绝不能出现在任何一条正文里。"""
        segs = split_reply("甲<br>乙<br/>丙</br>丁")
        for s in segs:
            self.assertNotIn("br", s.lower())
            self.assertNotIn("<", s)

    def test_short_but_meaningful_survives(self):
        """3 个字的完整短句要独立成条(她就是会这么说话)。"""
        self.assertEqual(split_reply("行啊你<br>山茶花戴着挺乖"), ["行啊你", "山茶花戴着挺乖"])

    def test_filler_kept_as_own_message(self):
        """她用 <br> 分出的每一条都是有意发的:嗯/哦/？也原样独立成条,不并进相邻条。"""
        self.assertEqual(
            split_reply("哈？？？<br>嗯<br>你这脑子是这么长的吗"),
            ["哈？？？", "嗯", "你这脑子是这么长的吗"],
        )

    def test_filler_tail_kept(self):
        self.assertEqual(split_reply("我看看你包里塞了什么<br>哦"), ["我看看你包里塞了什么", "哦"])

    def test_filler_leading_and_repeated_kept(self):
        self.assertEqual(split_reply("？<br>哈哈哈<br>行"), ["？", "哈哈哈", "行"])

    def test_empty_segments_dropped(self):
        self.assertEqual(split_reply("第一句<br><br>第三句"), ["第一句", "第三句"])

    def test_many_markers_kept(self):
        """真实连发:8 条单字也逐条保留,不整体退回一条。"""
        parts = ["一", "二", "三", "四", "五", "六", "七", "八"]
        self.assertEqual(split_reply("<br>".join(parts)), parts)

    def test_over_max_total_merges_only_tail(self):
        """超过上限(默认 40):前 39 条原样保留,超出部分并进最后一条。"""
        parts = [f"第{i}条" for i in range(50)]
        segs = split_reply("<br>".join(parts))
        self.assertEqual(len(segs), 40)
        self.assertEqual(segs[:39], parts[:39])
        self.assertEqual(segs[39], "\n".join(parts[39:]))
        for s in segs:
            self.assertNotIn("<", s)

    def test_custom_max_total(self):
        self.assertEqual(split_reply("甲<br>乙<br>丙<br>丁", max_total=2), ["甲", "乙\n丙\n丁"])

    def test_long_segment_with_markers_chunked(self):
        """有标记时单条超过 MAX_MESSAGE_CHARS 仍按限切。"""
        segs = split_reply("哦<br>" + "句子一。" * 2000)
        self.assertEqual(segs[0], "哦")
        self.assertGreater(len(segs), 2)
        self.assertTrue(all(len(s) <= MAX_MESSAGE_CHARS for s in segs))

    def test_single_segment_no_crash(self):
        self.assertEqual(split_reply("只有一句<br>"), ["只有一句"])

    def test_long_segment_chunked(self):
        segs = split_reply("句子一。" * 2000)
        self.assertTrue(all(len(s) <= 4000 for s in segs))
        self.assertGreater(len(segs), 1)

    def test_chunk_to_limit(self):
        self.assertEqual(chunk_to_limit("短", 4000), ["短"])
        out = chunk_to_limit("。" * 5000, 4000)
        self.assertTrue(all(len(s) <= 4000 for s in out))
        self.assertEqual("".join(out).count("。"), 5000)

    def test_describe_split(self):
        n, lens = describe_split(["甲", "乙丙"])
        self.assertEqual(n, 2)
        self.assertEqual(lens, [1, 2])


class TestStreamingBrInterception(unittest.TestCase):
    """标记生命周期:缓冲区保留 → 显示转换行 → 收尾切条 → 落盘前清理。"""

    def setUp(self):
        from reply_streaming import TelegramStreamer, _split_streamable, strip_br_markup


        prev = os.environ.get("REPLY_FOLLOWUP_INTERVAL_SEC")
        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "0"
        self.addCleanup(
            lambda: os.environ.pop("REPLY_FOLLOWUP_INTERVAL_SEC", None)
            if prev is None
            else os.environ.__setitem__("REPLY_FOLLOWUP_INTERVAL_SEC", prev)
        )

        self.streamer_cls = TelegramStreamer
        self.split_streamable = _split_streamable
        self.strip_br_markup = strip_br_markup

    def test_stream_buffer_keeps_marker_for_splitter(self):
        """缓冲区里的标记必须活着,否则 finalize 没东西可切。"""
        emit, held = self.split_streamable("甲<br>乙")
        self.assertEqual(held, "")
        self.assertIn("<br>", emit)

    def test_partial_br_held(self):
        """逐字流式时,`<br` 这种半截标记要攒住,不能发出去。"""
        for partial in ("<", "<b", "<br", "</b", "</br"):
            with self.subTest(partial=partial):
                emit, held = self.split_streamable("甲" + partial)
                self.assertEqual(emit, "甲")
                self.assertEqual(held, partial)

    def test_strip_br_markup(self):
        self.assertEqual(self.strip_br_markup("甲<br>乙"), "甲\n乙")
        self.assertEqual(self.strip_br_markup("甲<br/>乙"), "甲\n乙")
        self.assertEqual(self.strip_br_markup("甲<br>"), "甲\n")

    def _capture_display_posts(self, streamer):
        """拦下 requests.post,返回 (调用的 text 参数列表)。"""
        import reply_streaming as rs

        posted = []

        class FakeResp:
            status_code = 200
            text = '{"ok":true}'

            def json(self):
                return {"ok": True, "result": {"message_id": 999}}

        orig_post = rs.requests.post
        rs.requests.post = lambda url, **kw: posted.append(kw.get("json", {}).get("text")) or FakeResp()
        self.addCleanup(setattr, rs.requests, "post", orig_post)
        return posted

    def test_display_strips_marker(self):
        """流式编辑(display)把标记换成换行,用户屏幕上不该出现 <br>。"""
        s = self.streamer_cls("token", "chat")
        s.is_active = True
        s.message_id = 1
        s.full_text = "甲<br>乙"
        posted = self._capture_display_posts(s)
        s._send_edit()
        self.assertEqual(posted, ["甲\n乙"])

    def test_finalize_picks_segments(self):
        """finalize 内部用 split_reply 决定分几条(不真的发网络请求)。"""
        s = self.streamer_cls("token", "chat")
        s.is_active = True
        s.message_id = 1
        s.full_text = "甲<br>乙<br>丙"
        sent = {}
        s._send_edit = lambda max_retries=1: sent.setdefault("first", s.full_text)
        s._send_followup = lambda text, max_retries=2: sent.setdefault("rest", []).append(text) or 1
        s.finalize_with_text(None)
        self.assertEqual(sent["first"], "甲")
        self.assertEqual(sent["rest"], ["乙", "丙"])

    def test_finalize_flushes_partial_br_tail(self):
        """流式断在半截 <br 时,收尾要把攒住的尾巴接回来再切。"""
        s = self.streamer_cls("token", "chat")
        s.is_active = True
        s.message_id = 1
        s.full_text = "甲"
        s._pending = "<br>乙"
        sent = {}
        s._send_edit = lambda max_retries=1: sent.setdefault("first", s.full_text)
        s._send_followup = lambda text, max_retries=2: sent.setdefault("rest", []).append(text) or 1
        s.finalize_with_text(None)
        self.assertEqual(sent["first"], "甲")
        self.assertEqual(sent["rest"], ["乙"])

    def test_finalize_single_when_no_marker(self):
        s = self.streamer_cls("token", "chat")
        s.is_active = True
        s.message_id = 1
        s.full_text = "只有一条，没标记。"
        sent = {}
        s._send_edit = lambda max_retries=1: sent.setdefault("first", s.full_text)
        s._send_followup = lambda text, max_retries=2: sent.setdefault("rest", []).append(text)
        s.finalize_with_text(None)
        self.assertEqual(sent["first"], "只有一条，没标记。")
        self.assertNotIn("rest", sent)

    def test_finalize_sleeps_by_each_bubble_length(self):
        """第一条不等待;后续每条按自己的字数延时(动态公式,首条不 sleep)。"""
        import reply_streaming as rs


        os.environ.pop("REPLY_FOLLOWUP_INTERVAL_SEC", None)
        segs = ["甲", "哦", "这是一条长一些的第二条消息啊"]
        s = self.streamer_cls("token", "chat")
        s.is_active = True
        s.message_id = 1
        s.full_text = "<br>".join(segs)
        sleeps = []
        orig_sleep = rs.time.sleep
        rs.time.sleep = lambda sec: sleeps.append(sec)
        self.addCleanup(setattr, rs.time, "sleep", orig_sleep)
        s._send_edit = lambda max_retries=1: None
        s._send_followup = lambda text, max_retries=2: 1
        s.finalize_with_text(None)

        self.assertEqual(len(sleeps), 2)
        for got, seg in zip(sleeps, segs[1:]):
            self.assertAlmostEqual(got, 0.8 + len(seg) / 15.0)

    def test_followup_429_waits_retry_after(self):
        """_send_followup 遇到 429 读 parameters.retry_after 等待后重试。"""
        import reply_streaming as rs

        class Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        responses = [
            Resp(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}),
            Resp(200, {"ok": True, "result": {"message_id": 42}}),
        ]
        orig_post = rs.requests.post
        rs.requests.post = lambda url, **kw: responses.pop(0)
        self.addCleanup(setattr, rs.requests, "post", orig_post)
        sleeps = []
        orig_sleep = rs.time.sleep
        rs.time.sleep = lambda sec: sleeps.append(sec)
        self.addCleanup(setattr, rs.time, "sleep", orig_sleep)

        s = self.streamer_cls("token", "chat")
        self.assertEqual(s._send_followup("哦", max_retries=1), 42)
        self.assertEqual(sleeps, [3.0])


_FOLLOWUP_ENV_KEYS = (
    "REPLY_FOLLOWUP_BASE_SEC",
    "REPLY_FOLLOWUP_CHARS_PER_SEC",
    "REPLY_FOLLOWUP_MAX_SEC",
    "REPLY_FOLLOWUP_INTERVAL_SEC",
)


class TestFollowupDelay(unittest.TestCase):
    """分条延时:按每条气泡自己的字数动态计算(第一条不等待)。"""

    def setUp(self):
        import reply_streaming as rs

        self.rs = rs
        self._saved_env = {k: os.environ.get(k) for k in _FOLLOWUP_ENV_KEYS}
        for key in _FOLLOWUP_ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_empty_segment_returns_zero(self):
        self.assertEqual(self.rs._followup_delay_sec(""), 0.0)
        self.assertEqual(self.rs._followup_delay_sec("   \n\t "), 0.0)

    def test_shorter_bubble_delays_less_than_longer(self):
        short_text = "哦"
        long_text = "这是一条明显更长的回复，字数要多得多，得让她多敲一会儿。"
        short = self.rs._followup_delay_sec(short_text)
        long = self.rs._followup_delay_sec(long_text)
        self.assertGreater(long, short)
        self.assertAlmostEqual(short, 0.8 + len(short_text) / 15.0)
        self.assertAlmostEqual(long, 0.8 + len(long_text) / 15.0)

    def test_long_bubble_capped_by_max(self):
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 1000), 6.0)

    def test_env_tunables(self):
        os.environ["REPLY_FOLLOWUP_BASE_SEC"] = "0.5"
        os.environ["REPLY_FOLLOWUP_CHARS_PER_SEC"] = "10"
        os.environ["REPLY_FOLLOWUP_MAX_SEC"] = "2.0"
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 5), 1.0)
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 100), 2.0)

        os.environ["REPLY_FOLLOWUP_CHARS_PER_SEC"] = "0"
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 15), 1.5)

        os.environ["REPLY_FOLLOWUP_BASE_SEC"] = "bad"
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 15), 1.8)

    def test_legacy_fixed_interval_overrides_formula(self):
        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "0.25"
        self.assertAlmostEqual(self.rs._followup_delay_sec("无论多长都是固定间隔"), 0.25)

        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "0"
        self.assertEqual(self.rs._followup_delay_sec("无论多长都是零间隔"), 0.0)

        os.environ["REPLY_FOLLOWUP_INTERVAL_SEC"] = "abc"
        self.assertAlmostEqual(self.rs._followup_delay_sec("字" * 15), 1.8)


class _FakeResp:
    def __init__(self, body=None, status_code=200, text=None):
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 1}}
        self.status_code = status_code
        self.text = text if text is not None else str(self._body)

    def json(self):
        return self._body


class _FakeTelegram:
    """拦下 requests.post,按 URL 末段记录调用的 Telegram 方法。

    ``send_message_failures`` 可注入「前 N 次 sendMessage 都失败」,用来验证
    首条懒创建失败后的兜底路径。
    """

    def __init__(self):
        self.calls = []
        self._next_message_id = 1000
        self.send_message_failures = 0

    def install(self, testcase):
        import reply_streaming as rs

        self._orig_post = rs.requests.post
        rs.requests.post = self.post
        testcase.addCleanup(setattr, rs.requests, "post", self._orig_post)

    def methods(self):
        return [method for method, _payload in self.calls]

    def sent_texts(self):
        return [payload.get("text") for method, payload in self.calls if method == "sendMessage"]

    def post(self, url, **kwargs):
        method = url.rsplit("/", 1)[-1]
        payload = kwargs.get("json") or {}
        self.calls.append((method, payload))
        if method == "sendMessage":
            if self.send_message_failures > 0:
                self.send_message_failures -= 1
                return _FakeResp(
                    {"ok": False, "description": "Internal Server Error"},
                    status_code=500,
                    text='{"ok":false,"description":"Internal Server Error"}',
                )
            message_id = self._next_message_id
            self._next_message_id += 1
            return _FakeResp({"ok": True, "result": {"message_id": message_id}})
        return _FakeResp({"ok": True, "result": True})


class TestNoPlaceholder(unittest.TestCase):
    """发送前不再有「Thinking...」占位消息:首条真实文本到达时才懒创建。"""

    def test_start_sends_only_typing_and_no_message(self):
        from reply_streaming import TelegramStreamer

        fake = _FakeTelegram()
        fake.install(self)
        s = TelegramStreamer("token", "chat")
        self.addCleanup(s.stop_typing)
        s.start()
        self.assertTrue(s.is_active)
        self.assertIsNone(s.message_id)
        self.assertEqual(s.last_sent_text, "")
        self.assertIn("sendChatAction", fake.methods())
        self.assertNotIn("sendMessage", fake.methods())

    def test_send_edit_lazily_creates_message(self):
        from reply_streaming import TelegramStreamer

        fake = _FakeTelegram()
        fake.install(self)
        s = TelegramStreamer("token", "chat")
        s.is_active = True
        s.full_text = "第一条真实文本"
        s._send_edit()
        self.assertEqual(fake.methods(), ["sendMessage"])
        self.assertEqual(s.last_sent_text, "第一条真实文本")
        self.assertIsNotNone(s.message_id)
        first_message_id = s.message_id

        s.full_text = "第一条真实文本，改"
        s._send_edit()
        self.assertEqual(fake.methods(), ["sendMessage", "editMessageText"])
        self.assertEqual(s.message_id, first_message_id)

    def test_never_sends_empty_text(self):
        from reply_streaming import TelegramStreamer

        fake = _FakeTelegram()
        fake.install(self)
        s = TelegramStreamer("token", "chat")
        s.is_active = True
        s.full_text = "   \n "
        s._send_edit()
        self.assertEqual(fake.calls, [])
        self.assertIsNone(s.message_id)

    def test_reset_text_deletes_message_then_recreates(self):
        from reply_streaming import TelegramStreamer

        fake = _FakeTelegram()
        fake.install(self)
        s = TelegramStreamer("token", "chat")
        s.is_active = True
        s.message_id = 777
        s.full_text = "错误占位内容"
        s.last_sent_text = "错误占位内容"
        s.reset_text()
        self.assertEqual(fake.methods(), ["deleteMessage"])
        self.assertEqual(fake.calls[0][1]["message_id"], 777)
        self.assertIsNone(s.message_id)
        self.assertTrue(s.is_active, "reset 后仍是活跃流,后续可重新建消息")
        self.assertEqual(s.full_text, "")
        self.assertEqual(s.last_sent_text, "")

        s.full_text = "正确的回复"
        s._send_edit()
        self.assertEqual(fake.methods(), ["deleteMessage", "sendMessage"])
        self.assertIsNotNone(s.message_id)


class TestFirstSegmentFallback(unittest.TestCase):
    """首条懒创建失败时:用 _send_followup 兜底把第 1 条发出去,不能只落库。"""

    def setUp(self):
        import reply_streaming as rs

        self.rs = rs
        self._saved_env = {k: os.environ.get(k) for k in _FOLLOWUP_ENV_KEYS}
        for key in _FOLLOWUP_ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _make_streamer(self):
        from reply_streaming import TelegramStreamer

        fake = _FakeTelegram()
        fake.install(self)
        sleeps = []
        orig_sleep = self.rs.time.sleep
        self.rs.time.sleep = lambda sec: sleeps.append(sec)
        self.addCleanup(setattr, self.rs.time, "sleep", orig_sleep)
        s = TelegramStreamer("token", "chat")
        s.is_active = True
        return fake, s, sleeps

    def test_multi_segment_falls_back_to_followup_without_delay(self):
        fake, s, sleeps = self._make_streamer()
        fake.send_message_failures = 3
        segs = ["甲", "哦", "这是一条长一些的第二条消息"]
        s.full_text = "<br>".join(segs)
        s.finalize_with_text(None)


        self.assertEqual(fake.sent_texts()[-3:], segs)
        self.assertNotIn("<br>", fake.sent_texts()[-3])
        self.assertIsNotNone(s.message_id, "兜底成功后要回填 message_id 供落库")
        self.assertEqual(s.last_sent_text, segs[0])

        self.assertEqual(sleeps[:2], [1.0, 1.0])
        delays = sleeps[2:]
        self.assertEqual(len(delays), 2, "第 1 条兜底不 sleep,仅第 2/3 条按字数延时")
        for got, seg in zip(delays, segs[1:]):
            self.assertAlmostEqual(got, 0.8 + len(seg) / 15.0)

    def test_single_segment_falls_back_to_followup_without_delay(self):
        fake, s, sleeps = self._make_streamer()
        fake.send_message_failures = 3
        text = "只有一条，没标记。"
        s.full_text = text
        s.finalize_with_text(None)

        self.assertEqual(fake.sent_texts()[-1], text)
        self.assertIsNotNone(s.message_id)
        self.assertEqual(s.last_sent_text, text)

        self.assertEqual(sleeps, [1.0, 1.0])


class UnescapeModelTextTest(unittest.TestCase):
    """模型偶发把换行写成字面「\n」（JSON 转义残留）：统一还原成真实字符。

    回归背景（2026-10-26 事故）：打字那一步输出里出现字面「\n」，原样发进
    Telegram，还和 <br> 同条共存。转义残留一律在发送/落盘/回灌前还原。
    """

    def test_literal_escaped_newlines_become_real(self):
        self.assertEqual(unescape_model_text("甲\\n乙"), "甲\n乙")
        self.assertEqual(unescape_model_text("甲\\r\\n乙"), "甲\n乙")
        self.assertEqual(unescape_model_text("甲\\r乙"), "甲\n乙")

    def test_double_escaped_newlines_become_real(self):

        self.assertEqual(unescape_model_text("甲\\\\n乙"), "甲\n乙")
        self.assertEqual(unescape_model_text("甲\\\\r\\\\n乙"), "甲\n乙")
        self.assertEqual(unescape_model_text("甲\\\\\\\\n乙"), "甲\n乙")

    def test_double_backslash_collapses_to_single(self):
        self.assertEqual(unescape_model_text("甲\\\\乙"), "甲\\乙")

    def test_single_backslash_and_normal_text_untouched(self):
        self.assertEqual(unescape_model_text("甲\\乙"), "甲\\乙")
        self.assertEqual(unescape_model_text("甲<br>乙"), "甲<br>乙")
        self.assertEqual(unescape_model_text("甲\n乙"), "甲\n乙")
        self.assertIsNone(unescape_model_text(None))
        self.assertEqual(unescape_model_text(""), "")

    def test_idempotent(self):
        once = unescape_model_text("甲\\n乙\\\\丙")
        self.assertEqual(unescape_model_text(once), once)


class TypedNewlinesToBrTest(unittest.TestCase):
    """打字输出口径：任何换行表示都收敛成单个 <br>（一行 = 按一次发送）。"""

    def test_real_and_escaped_newlines_unify(self):
        self.assertEqual(typed_newlines_to_br("甲\\n乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲\n乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲\r\n乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲\n\n乙"), "甲<br>乙")

    def test_mixed_br_and_newline_coexistence_collapses(self):

        self.assertEqual(typed_newlines_to_br("甲\\n<br>乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲<br>\n乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲<br><br>乙"), "甲<br>乙")

    def test_marker_variants_and_spaces(self):
        self.assertEqual(typed_newlines_to_br("甲<br/>乙"), "甲<br>乙")
        self.assertEqual(typed_newlines_to_br("甲 <br> 乙"), "甲<br>乙")

    def test_plain_text_untouched_and_empty_safe(self):
        self.assertEqual(typed_newlines_to_br("哦<br>行"), "哦<br>行")
        self.assertEqual(typed_newlines_to_br("就是一句话"), "就是一句话")
        self.assertEqual(typed_newlines_to_br("   "), "")
        self.assertEqual(typed_newlines_to_br(""), "")
        self.assertIsNone(typed_newlines_to_br(None))


if __name__ == "__main__":
    unittest.main()
