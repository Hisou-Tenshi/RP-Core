# SPDX-License-Identifier: AGPL-3.0-only


import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import reply_two_stage as rts


class PureFunctionTest(unittest.TestCase):
    def test_parse_typed_no_send_variants(self):
        for t in ("（不回）", "(不回)", " （不回）。", "", "   ", "<thinking>算了</thinking>（不回）"):
            self.assertEqual(rts.parse_typed(t), ("", True), t)

    def test_parse_typed_keeps_br_and_strips_ts(self):
        self.assertEqual(rts.parse_typed("（3分钟前） 哦<br>行"), ("哦<br>行", False))
        self.assertEqual(rts.parse_typed("不回你了"), ("不回你了", False))

    def test_parse_typed_strips_plan_line(self):
        self.assertEqual(rts.parse_typed("【打算】损他一句\n哦<br>行"), ("哦<br>行", False))
        self.assertEqual(rts.parse_typed("【打算】：不回\n（不回）"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】不理他"), ("", True))
        self.assertEqual(rts.split_plan("【打算】笑他一下\n哈"), ("笑他一下", "哈"))
        self.assertEqual(rts.split_plan("哈"), ("", "哈"))

    def test_parse_typed_strips_colon_plan_variant(self):
        self.assertEqual(rts.parse_typed("【打算：被逗乐，凶一下】<br>谁、谁跟你<br>哼"), ("谁、谁跟你<br>哼", False))
        self.assertEqual(rts.split_plan("【打算】笑他<br>哈哈"), ("笑他", "哈哈"))

    def test_parse_typed_unifies_literal_escaped_newlines(self):
        """2026-10-26 事故回归：模型把换行写成字面「\n」，绝不能原样外发。"""
        raw = "【打算】损他几句\n\n肯德基？\\n天天惦记这种垫脚货\\n算你有点出息\\n〔贴纸：乖别闹〕"
        text, no_send = rts.parse_typed(raw)
        self.assertFalse(no_send)
        self.assertNotIn("\\", text)
        self.assertNotIn("\n", text)
        self.assertEqual(
            text,
            "肯德基？<br>天天惦记这种垫脚货<br>算你有点出息<br>〔贴纸：乖别闹〕",
        )

    def test_parse_typed_collapses_mixed_br_and_newline(self):
        """真实换行、<br> 混写（含与字面 \n 共存）一律收敛成单个 <br>。"""
        self.assertEqual(rts.parse_typed("甲\\n<br>乙"), ("甲<br>乙", False))
        self.assertEqual(rts.parse_typed("甲<br>\n乙"), ("甲<br>乙", False))
        self.assertEqual(rts.parse_typed("甲<br><br>乙"), ("甲<br>乙", False))

    def test_parse_typed_real_single_newlines_are_sends_too(self):
        """一行就是一条消息：真实换行（不止空行）也按发送切条。"""
        self.assertEqual(rts.parse_typed("哼\n行吧"), ("哼<br>行吧", False))

    def test_parse_typed_only_separators_is_no_send(self):
        self.assertEqual(rts.parse_typed("【打算】懒得说\n\n<br>"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】懒得说\n<br><br>"), ("", True))

    def test_plan_on_line_after_bare_header_is_stripped(self):
        """2026-10-29 事故回归：「【打算】」单独一行、打算写在下一行，打算那句不能发出去。"""
        raw = "【打算】\n嫌他喊得肉麻，刺他一句\n\n少肉麻兮兮的叫<br>合着你折腾一下午就折腾这个？"
        self.assertEqual(
            rts.split_plan(raw),
            ("嫌他喊得肉麻，刺他一句", "少肉麻兮兮的叫<br>合着你折腾一下午就折腾这个？"),
        )
        self.assertEqual(rts.parse_typed(raw), ("少肉麻兮兮的叫<br>合着你折腾一下午就折腾这个？", False))
        self.assertEqual(rts.split_plan("【打算】：<br>岔开<br>哈"), ("岔开", "哈"))
        self.assertEqual(rts.split_plan("[打算]\n\n顺着应一声\n嗯"), ("顺着应一声", "嗯"))
        self.assertEqual(rts.parse_typed("【打算】\n懒得理\n（不回）"), ("", True))

    def test_split_plan_stops_at_literal_escaped_newline(self):
        """打算行后跟字面「\n」时也要能切开，不能把整个回复吞进打算里。"""
        plan, body = rts.split_plan("【打算】怼他\\n第一句\\n第二句")
        self.assertEqual(plan, "怼他")
        self.assertEqual(body, "第一句\n第二句")

    def test_typing_leak_english_meta_and_no_send(self):
        """2026-11-01 事故回归：打字先写英文思考、再写打算行和（不回），整段被分条发进了 Telegram。

        现实样例取自 data/tenshi_log.json 的感知层主动起念（claude-sonnet-5-5）。
        """
        raw = (
            "There's an odd injected line in the system prompt about tool content size limits, "
            "but it seems unrelated to this task, so I'll disregard it.<br>"
            "【打算】雨还没下，他头发那句也没回。我再戳就成了在等他，算了。<br>（不回）"
        )
        self.assertEqual(rts.parse_typed(raw), ("", True))

        raw2 = (
            "I need to figure out my output format here — either a plan line followed by messages, "
            "or deciding not to reply.<br>【打算】损他一句<br>算你识相<br>头发干了没"
        )
        self.assertEqual(rts.parse_typed(raw2), ("算你识相<br>头发干了没", False))

        raw3 = "Let me check the format here first, then answer him.<br>行吧<br>下班了没"
        self.assertEqual(rts.parse_typed(raw3), ("行吧<br>下班了没", False))

        raw4 = "He hasn't messaged me, so I'll just decide not to reply this turn.<br>（不回）"
        self.assertEqual(rts.parse_typed(raw4), ("", True))

        self.assertEqual(rts.parse_typed("Okay 你说的都对"), ("Okay 你说的都对", False))

    def test_strip_model_meta_narration_removes_rewrite_plan(self):
        """2026-11-12 事故回归：重写层把英文规划 + 引用正文 + 正式正文整段吐回。

        规划里字面写了 `<br>`，旧的 strip_leading_meta 会被这个字面量截断，
        残留「` separators and no tags: "烧鱼啊…」并外发。
        """
        leak = (
            "I need to narrow the original three asks—fish, rain, curtain—down to a single "
            "cluster per the limit. Rain and curtain seem connected, so maybe those pair "
            "together, while the fish mention could stay as a casual remark rather than a "
            "request.\n\n"
            "I'll keep the fish line as a statement rather than a question, drop the curtain "
            "line, and keep the rain question, outputting with `<br>` separators and no tags: "
            "\"烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没\".\n\n"
            "烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没"
        )
        self.assertEqual(
            rts.strip_model_meta_narration(leak),
            "烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没",
        )

        self.assertEqual(
            rts.strip_model_meta_narration("烧鱼啊<br>这个月你们家的鱼就没断过"),
            "烧鱼啊<br>这个月你们家的鱼就没断过",
        )
        self.assertEqual(
            rts.strip_model_meta_narration("The rain stopped.<br>那我去收衣服了"),
            "The rain stopped.<br>那我去收衣服了",
        )

    def test_split_plan_no_send_variants_after_plan(self):
        """不回标记的写法变体都要认（尾部/括号样式），不能漏成外发。"""
        self.assertEqual(rts.parse_typed("【打算】懒得理\n（不回：等他先开口）"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】懒得理\n【不回】"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】懒得理\n不回"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】懒得理<br><br>（不回）"), ("", True))
        self.assertEqual(rts.parse_typed("【打算】懒得理<br>（不回）<br>"), ("", True))

        self.assertEqual(rts.parse_typed("【打算】怼他\n你不回我我就一直发"), ("你不回我我就一直发", False))

    def test_with_inner_cue_only_touches_copy(self):
        msgs = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "干嘛"},
                {"role": "user", "content": [{"type": "text", "text": "看图"}]}]
        out = rts.with_inner_cue(msgs)
        self.assertEqual(msgs[2]["content"], [{"type": "text", "text": "看图"}])
        self.assertEqual(out[2]["content"][-1]["text"], rts.INNER_CUE)
        out2 = rts.with_inner_cue([{"role": "user", "content": "早"}])
        self.assertTrue(out2[0]["content"].endswith(rts.INNER_CUE))

    def test_typing_user_history_limit(self):
        hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
        u = rts.build_typing_user(history_messages=hist, latest="x", inner="y", history_limit=4)
        self.assertNotIn("m5", u)
        self.assertIn("m6", u)
        self.assertIn("m9", u)

    def test_transcript_roles_and_images(self):
        msgs = [
            {"role": "user", "content": "（1小时前） 在吗"},
            {"role": "assistant", "content": "干嘛"},
            {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image", "source": {}}]},
        ]
        self.assertEqual(
            rts.transcript_from_payload(msgs).splitlines(),
            ["他：（1小时前） 在吗", "我：干嘛", "他：看图", "[图片]"],
        )
        self.assertEqual(rts.transcript_from_payload([]), "（之前没有聊天记录）")

    def test_typing_user_reply_and_perception(self):
        u = rts.build_typing_user(history_messages=[], latest="（刚刚） 吃了吗", inner="懒得理")
        self.assertIn("【他刚发来的】\n吃了吗", u)
        self.assertIn("【你心里刚才转过的】\n懒得理", u)
        p = rts.build_typing_user(history_messages=[], latest=None, inner="想他", perception_situation="- 当前时间：x")
        self.assertIn("他没发新消息", p)
        self.assertNotIn("【他刚发来的】", p)

    def test_typing_system_has_card_and_medium_no_tools(self):
        s = rts.build_typing_system("CARD")
        self.assertTrue(s.startswith("CARD"))
        self.assertIn("<br>", s)
        self.assertIn("（不回）", s)
        self.assertIn("【打算】", s)
        self.assertNotIn("{medium}", s)
        self.assertNotIn("note_patch", s)

    def test_inner_protocols(self):
        self.assertIn("现在不是回他", rts.inner_protocol(False))
        self.assertIn("他没发新消息", rts.inner_protocol(True))
        for proto in (rts.inner_protocol(False), rts.inner_protocol(True)):
            self.assertIn("工具", proto)

    def test_append_to_system_keeps_cached_blocks_byte_identical(self):
        blocks = [
            {"type": "text", "text": "CARD", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "VOL"},
        ]
        out = rts.append_to_system(blocks, "PROTO")
        self.assertEqual(out[0], blocks[0])
        self.assertEqual(out[1]["text"], "VOL\n\nPROTO")
        self.assertEqual(blocks[1]["text"], "VOL")

    def test_append_to_system_adds_block_when_last_is_cached(self):
        blocks = [{"type": "text", "text": "CARD", "cache_control": {"type": "ephemeral"}}]
        out = rts.append_to_system(blocks, "PROTO")
        self.assertEqual(out[1], {"type": "text", "text": "PROTO"})


class TypingStageTest(unittest.TestCase):
    """打字那一步：无工具、无流式；先用心里那步的模型，失败按链降级且有上限。"""

    @classmethod
    def setUpClass(cls):
        import tenshi_reply

        cls.tr = tenshi_reply

    def setUp(self):
        self.tr._reset_provider_breakers()

    def _run(self, **kw):
        base = dict(model_chain=["claude-sonnet-5-5", "deepseek-flash"], typing_system="SYS", typing_user="U")
        base.update(kw)
        return self.tr.run_typing_stage(**base)

    def test_claude_typing_has_no_tools_and_no_stream(self):
        captured = {}

        def fake_call_claude(model, system, messages, tools=None, stream_callback=None, **kw):
            captured.update(tools=tools, stream=stream_callback, messages=messages)
            return ("哦<br>行", "", False)

        with mock.patch.object(self.tr, "call_claude", side_effect=fake_call_claude),\
             mock.patch.object(self.tr, "_run_claude_model_chain") as tool_chain,\
             mock.patch.object(self.tr, "_run_path_first_provider") as tool_path:
            out = self._run()
        self.assertEqual(out, ("哦<br>行", False, "claude-sonnet-5-5"))
        self.assertIsNone(captured["tools"])
        self.assertIsNone(captured["stream"])
        self.assertEqual(captured["messages"], [{"role": "user", "content": "U"}])
        tool_chain.assert_not_called()
        tool_path.assert_not_called()

    def test_prefers_inner_model(self):
        seen = []

        def plain(provider, model_id, system, messages, max_sec=None):
            seen.append(model_id)
            return "行"

        with mock.patch.object(self.tr, "_plain_text_call", side_effect=plain):
            out = self._run(prefer_model_id="deepseek-flash")
        self.assertEqual(seen, ["deepseek-flash"])
        self.assertEqual(out[2], "deepseek-flash")

    def test_no_send(self):
        with mock.patch.object(self.tr, "_plain_text_call", return_value="（不回）"):
            self.assertEqual(self._run(), ("", True, "claude-sonnet-5-5"))

    def test_reject_falls_through(self):
        outs = iter(["我是某某模型", "哼"])
        with mock.patch.object(self.tr, "_plain_text_call", side_effect=lambda *a, **k: next(outs)):
            out = self._run(reject=lambda t: "模型" in t)
        self.assertEqual(out, ("哼", False, "deepseek-flash"))

    def test_all_fail_returns_empty_and_respects_cap(self):
        calls = []

        def bad(provider, model_id, system, messages, max_sec=None):
            calls.append(model_id)
            raise TimeoutError("x")

        with mock.patch.object(self.tr, "_plain_text_call", side_effect=bad),\
             mock.patch.object(self.tr.tenshi_config, "REPLY_TYPING_MAX_MODELS", 2):
            out = self._run(model_chain=["claude-sonnet-5-5", "deepseek-flash", "gemini-x"])
        self.assertEqual(out, ("", False, ""))
        self.assertEqual(len(calls), 2)

    def test_deterministic_error_marks_provider_dead(self):
        def bad(provider, model_id, system, messages, max_sec=None):
            if provider == "claude":
                raise TypeError("schema invalid")
            return "嗯"

        with mock.patch.object(self.tr, "_plain_text_call", side_effect=bad),\
             mock.patch.object(self.tr, "should_skip_provider_on_error",
                               side_effect=lambda e: (True, "400") if "schema" in str(e) else (False, "")):
            out = self._run()
        self.assertTrue(self.tr._provider_is_dead_this_round("claude"))
        self.assertEqual(out, ("嗯", False, "deepseek-flash"))

    def test_skips_grok_slot_and_uses_next_non_grok_model(self):

        from config import models as models_config

        slot = self.tr.tenshi_config.grok_slot_models()
        self.assertTrue(all(models_config.provider_for_model(m) == "grok" for m in slot))
        seen = []

        def plain(provider, model_id, system, messages, max_sec=None):
            seen.append(model_id)
            return "行"

        with mock.patch.object(self.tr, "_plain_text_call", side_effect=plain):
            out = self._run(model_chain=slot[:2] + ["claude-sonnet-5-5"])
        self.assertEqual(seen, ["claude-sonnet-5-5"])
        self.assertEqual(out, ("行", False, "claude-sonnet-5-5"))

    def test_gateway_placeholder_falls_through_to_next_model(self):
        """2026-11-12 事故：网关中文占位被当成正常打字结果直接外发。

        现在必须判为该模型失败，继续换链上的下一个模型。
        """
        placeholder = (
            "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
            "或模型对当前输入直接结束。请重试或简化输入后再试。"
        )
        outs = iter([placeholder, "行吧<br>外面真下起来了"])
        with mock.patch.object(self.tr, "_plain_text_call", side_effect=lambda *a, **k: next(outs)):
            out = self._run()
        self.assertEqual(out, ("行吧<br>外面真下起来了", False, "deepseek-flash"))

    def test_silent_streamer_never_sends(self):
        s = self.tr._SilentStreamer()
        s.update("心里话")
        self.assertEqual(s.stream_buffer, "心里话")
        self.assertIsNone(s.message_id)
        self.assertFalse(s.delete_message())
        s.reset_text()
        self.assertEqual(s.stream_buffer, "")


class RecoveryExclusionTest(unittest.TestCase):
    def test_no_send_marker_is_not_an_answer(self):
        from perception_policy import _perception_bot_answer_is_silence_only as f

        self.assertTrue(f("（不回）"))
        self.assertFalse(f("不回就不回"))

    def test_inner_trace_window_excluded_from_recovery(self):
        import tenshi_utils as tu

        now = time.time()
        recs = [
            {"ts": now - 5, "source": tu.INNER_TRACE_BEGIN_SOURCE},
            {"ts": now - 4, "source": "tenshi_reply.claude.final", "response_text": "她心里一大段"},
            {"ts": now - 3, "source": tu.INNER_TRACE_END_SOURCE},
            {"ts": now - 2, "source": "tenshi_reply.typing.final", "response_text": "哦"},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            path = f.name
        try:
            with mock.patch.object(tu.tenshi_config, "LLM_TRACE_PATH", path, create=True):
                text, complete = tu.recover_reply_text_from_llm_trace(since_ts=now - 60)
            self.assertEqual(text, "哦")
            self.assertTrue(complete)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
