# SPDX-License-Identifier: AGPL-3.0-only


















from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from llm_tool_text import (
    has_tool_call_markup,
    ingest_model_tool_output,
    sanitize_model_text,
    strip_tool_call_markup,
)
from reply_streaming import TelegramStreamer, _split_streamable


LEAKED = (
    "哼，“真不愧是天子”，这种话不劳你认证。\n\n"
    "审查收尾：五问结清，容灾过关。\n\n"
    "<toolcalls>\n"
    '<invoke name="notepatch">\n'
    '<parameter name="operations">{"op": "add", "text": "09-15 13:18 容灾账结清"}</parameter>\n'
    '<parameter name="readn">3</parameter>\n'
    "</invoke>\n"
    '<invoke name="scheduleperception">\n'
    '<parameter name="triggerat">2026-09-15 13:33</parameter>\n'
    '<parameter name="note">放榜到点：他没动静就点名。</parameter>\n'
    "</invoke>\n"
    "</toolcalls>……对了，水杯先倒满，放桌上。"
)
ALLOWED = {"note_patch", "schedule_perception"}


class MarkupStrippingTest(unittest.TestCase):
    def test_underscore_less_wrapper_is_recognized(self):
        self.assertTrue(has_tool_call_markup(LEAKED))
        for variant in ("<toolcalls>", "</toolcalls>", "<functioncalls>", "<toolcall>"):
            self.assertTrue(has_tool_call_markup(f"正文\n{variant}\n正文"), variant)

    def test_no_markup_residue_after_strip(self):
        stripped = strip_tool_call_markup(LEAKED)
        self.assertFalse(has_tool_call_markup(stripped), stripped)
        for residue in ("toolcalls", "invoke", "parameter", "notepatch", "scheduleperception"):
            self.assertNotIn(residue, stripped, f"残留了 {residue}")
        self.assertIn("容灾过关", stripped)
        self.assertIn("水杯先倒满", stripped, "markup 后面的正常正文必须保留")

    def test_ingest_still_recovers_the_calls(self):
        out = ingest_model_tool_output(
            provider="deepseek", content=LEAKED, native_tool_calls=None, allowed_names=ALLOWED
        )
        names = [(c.get("function") or {}).get("name") for c in out.tool_calls]
        self.assertEqual(len(names), 2, names)
        self.assertFalse(has_tool_call_markup(out.text), out.text)
        self.assertEqual(out.formats, ["xml:invoke"])

    def test_sanitize_model_text_is_the_last_line_of_defense(self):
        cleaned = sanitize_model_text(LEAKED)
        self.assertFalse(has_tool_call_markup(cleaned))
        self.assertIn("水杯先倒满", cleaned)


class StreamInterceptionTest(unittest.TestCase):
    """流式外发前必须拦住 markup（用户永远不该看到调用标记）。"""

    def test_split_streamable_holds_unclosed_call_tag(self):
        head, held = _split_streamable("正文A\n<toolcalls>\n<invoke name=\"notepatch\">")
        self.assertEqual(head, "正文A\n")
        self.assertTrue(held.startswith("<toolcalls>"))

    def test_split_streamable_strips_complete_block(self):
        emit, held = _split_streamable(LEAKED)
        self.assertEqual(held, "")
        self.assertFalse(has_tool_call_markup(emit))
        self.assertIn("容灾过关", emit)
        self.assertIn("水杯先倒满", emit)

    def test_split_streamable_holds_partial_tag(self):
        emit, held = _split_streamable("正文B\n<tool")
        self.assertEqual(emit, "正文B\n")
        self.assertEqual(held, "<tool")

    def test_split_streamable_passes_plain_text(self):

        emit, held = _split_streamable("普通回复，没有任何标记 <不是标签吗")
        self.assertEqual(emit, "普通回复，没有任何标记 <不是标签吗")
        self.assertEqual(held, "")

    def test_split_streamable_passes_comparison_operators(self):
        for text in ("a < b 是成立的", "值 < 3 就报警", "分数 > 90"):
            emit, held = _split_streamable(text)
            self.assertEqual(emit, text, text)
            self.assertEqual(held, "", text)

    def test_split_streamable_holds_even_a_lone_angle_bracket(self):

        emit, held = _split_streamable("正文C<")
        self.assertEqual(emit, "正文C")
        self.assertEqual(held, "<")

    def test_split_streamable_flushes_when_hold_too_long(self):
        huge = "<toolcalls>\n" + ("x" * 5000)
        emit, held = _split_streamable(huge)
        self.assertEqual(held, "", "攒太久要放行，不能吞掉正常回复")
        self.assertFalse(has_tool_call_markup(emit))

    def test_streamer_never_emits_markup(self):
        streamer = TelegramStreamer(token=None, chat_id=None)
        streamer.is_active = True

        for char in LEAKED:
            streamer.update(char)

        self.assertFalse(has_tool_call_markup(streamer.full_text), streamer.full_text)
        self.assertFalse(has_tool_call_markup(streamer.stream_buffer), streamer.stream_buffer)
        self.assertIn("容灾过关", streamer.full_text)
        self.assertIn("水杯先倒满", streamer.full_text, "markup 之后的正文要继续流出去")

        self.assertIn("toolcalls", streamer.raw_stream_buffer)

    def test_streamer_keeps_normal_stream_intact(self):
        streamer = TelegramStreamer(token=None, chat_id=None)
        streamer.is_active = True
        text = "第一段。\n\n第二段，带个 < 符号和 code `x`。"
        for char in text:
            streamer.update(char)
        self.assertEqual(streamer.full_text, text)


if __name__ == "__main__":
    unittest.main()
