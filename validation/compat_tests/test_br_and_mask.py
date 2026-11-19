# SPDX-License-Identifier: AGPL-3.0-only













from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")
os.environ.pop("PERCEPTION_SCHEDULE_SYNC_URL", None)
os.environ.pop("PERCEPTION_SCHEDULE_SYNC_SECRET", None)

import tenshi_config



_TMP_DIR = tempfile.mkdtemp(prefix="tenshi_br_mask_")
_ORIG_DEBUG_LOG = tenshi_config.DEBUG_LOG_PATH
_ORIG_SUMMARY_LOG = tenshi_config.SUMMARY_UPDATE_LOG_PATH
tenshi_config.DEBUG_LOG_PATH = os.path.join(_TMP_DIR, "debug.log")
tenshi_config.SUMMARY_UPDATE_LOG_PATH = os.path.join(_TMP_DIR, "summary_update.log")

import rag_modules

_ORIG_RAG_CLASS = rag_modules.TenshiRAG


class _DummyRAG:
    """tenshi_reply import 期会 ``TenshiRAG()``；替身避免打开真实 sqlite / 读 KB。"""

    def __init__(self, *args, **kwargs):
        self.kb_data = {}
        self.chat_logs = []

    def get_embedding(self, text):
        raise AssertionError("test dummy: get_embedding 不应被调用")


TENSHI_REPLY_IMPORT_ERROR: Exception | None = None
tenshi_reply = None

try:
    try:
        rag_modules.TenshiRAG = _DummyRAG
        import tenshi_reply
    finally:
        rag_modules.TenshiRAG = _ORIG_RAG_CLASS

    import context_compression as cc
    import log_update
    from message_split import (
        br_to_space,
        newlines_to_br,
        normalize_br_markup,
        split_reply,
    )
    from reply_streaming import strip_br_markup
    import tenshi_utils
except Exception as e:
    TENSHI_REPLY_IMPORT_ERROR = e
finally:
    tenshi_config.DEBUG_LOG_PATH = _ORIG_DEBUG_LOG
    tenshi_config.SUMMARY_UPDATE_LOG_PATH = _ORIG_SUMMARY_LOG


def _ts(i: int) -> float:
    return 1_750_000_000.0 + i * 60.0


class BrMarkupNormalizeTest(unittest.TestCase):
    """`<br>` 写法归一 / 换行 → `<br>` / 查询还原，全部幂等。"""

    def setUp(self):
        if TENSHI_REPLY_IMPORT_ERROR is not None:
            self.skipTest(f"依赖 import 失败：{TENSHI_REPLY_IMPORT_ERROR}")

    def test_normalize_all_variants_to_single_form(self):
        for marker in ("<br>", "<br/>", "<br />", "</br>", "<BR>", "< br >", "<Br/>"):
            with self.subTest(marker=marker):
                self.assertEqual(normalize_br_markup(f"甲{marker}乙"), "甲<br>乙")

    def test_normalize_keeps_real_newlines(self):

        self.assertEqual(normalize_br_markup("甲\n乙<br/>丙"), "甲\n乙<br>丙")

    def test_normalize_idempotent_and_non_str_safe(self):
        text = "甲<br>乙"
        self.assertEqual(normalize_br_markup(normalize_br_markup(text)), text)
        self.assertIsNone(normalize_br_markup(None))
        self.assertEqual(normalize_br_markup(""), "")

    def test_newlines_to_br_converts_crlf_and_lf(self):
        self.assertEqual(
            newlines_to_br("第一行\r\n第二行\n第三行\r第四行"),
            "第一行<br>第二行<br>第三行<br>第四行",
        )

    def test_newlines_to_br_idempotent_and_normalizes_existing_variants(self):
        once = newlines_to_br("第一行\r\n第二行")
        self.assertEqual(once, "第一行<br>第二行")
        self.assertEqual(newlines_to_br(once), once)
        self.assertEqual(newlines_to_br("已<br/>有"), "已<br>有")

    def test_br_to_space_for_query_text(self):
        self.assertEqual(br_to_space("丙<br>丁<br/>戊"), "丙 丁 戊")

    def test_split_reply_still_splits_after_newline_ingestion(self):
        self.assertEqual(split_reply(newlines_to_br("甲\n乙")), ["甲", "乙"])


class BotAnswerPersistenceTest(unittest.TestCase):
    """落盘口径：保留 `<br>`（统一写法），外发出口仍然不泄漏标记。"""

    def setUp(self):
        if TENSHI_REPLY_IMPORT_ERROR is not None:
            self.skipTest(f"tenshi_reply import 失败：{TENSHI_REPLY_IMPORT_ERROR}")

    def test_persistence_keeps_br(self):
        text = tenshi_reply.normalize_bot_answer_for_persistence("甲<br/>乙<br>丙")
        self.assertEqual(text, "甲<br>乙<br>丙")
        self.assertIn("<br>", text)

    def test_persistence_does_not_turn_newlines_into_markup(self):

        self.assertEqual(
            tenshi_reply.normalize_bot_answer_for_persistence("甲<br/>乙\n丙"),
            "甲<br>乙\n丙",
        )

    def test_persistence_unescapes_literal_escape_residue(self):
        """2026-10-26 事故回归：字面「\n」/双反斜杠绝不原样落盘（会回灌教坏模型）。

        落盘口径只还原成真实换行（与 test_persistence_does_not_turn_newlines_into_markup
        一致）；两步路径的字面 \n 在 parse_typed 已统一成 <br>，走不到这里。
        """
        out = tenshi_reply.normalize_bot_answer_for_persistence(
            "肯德基？\\n天天惦记\\n不过算你有点出息"
        )
        self.assertNotIn("\\", out)
        self.assertEqual(out, "肯德基？\n天天惦记\n不过算你有点出息")

    def test_persisted_text_still_renders_and_splits_without_marker(self):
        persisted = tenshi_reply.normalize_bot_answer_for_persistence("甲<br/>乙<br>丙")

        self.assertEqual(strip_br_markup(persisted), "甲\n乙\n丙")

        self.assertEqual(split_reply(persisted), ["甲", "乙", "丙"])
        for seg in split_reply(persisted):
            self.assertNotIn("<", seg)


class PayloadBrAndMaskTest(unittest.TestCase):
    """模型上下文的口径：user_query 换行 → `<br>`；context_masked 不进上下文。"""

    def setUp(self):
        if TENSHI_REPLY_IMPORT_ERROR is not None:
            self.skipTest(f"tenshi_reply import 失败：{TENSHI_REPLY_IMPORT_ERROR}")

    def _payload(self, history):
        return tenshi_reply.build_messages_payload_from_chat_history(history)

    def test_history_user_query_newlines_become_br(self):
        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "第一行\n第二行", "bot_answer": "嗯"},
        ]
        payload = self._payload(history)
        user_msgs = [m for m in payload if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertTrue(user_msgs[0]["content"].endswith("第一行<br>第二行"))
        self.assertNotIn("\n", user_msgs[0]["content"])

    def test_history_user_query_already_br_is_unchanged(self):
        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "一<br>二", "bot_answer": "嗯"},
        ]
        payload = self._payload(history)
        user_msgs = [m for m in payload if m["role"] == "user"]
        self.assertTrue(user_msgs[0]["content"].endswith("一<br>二"))

    def test_history_bot_answer_keeps_br(self):
        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "在吗", "bot_answer": "甲<br/>乙"},
        ]
        payload = self._payload(history)
        assistant_msgs = [m for m in payload if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertIn("甲<br>乙", assistant_msgs[0]["content"])

    def test_history_bot_answer_escape_residue_self_heals(self):
        """历史里残留的字面「\n」回灌前必须还原，不能教模型接着写「\n」。"""
        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "在吗",
             "bot_answer": "肯德基？\\n天天惦记\\n算你有点出息"},
        ]
        payload = self._payload(history)
        assistant_msgs = [m for m in payload if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        content = assistant_msgs[0]["content"]
        self.assertNotIn("\\n", content)
        self.assertIn("肯德基？\n天天惦记\n算你有点出息", content)

    def test_context_masked_turn_is_skipped(self):
        history = [
            {"msg_id": "m3", "time": _ts(3), "user_query": "这条该被屏蔽",
             "bot_answer": "屏蔽的回复", "context_masked": True},
            {"msg_id": "m2", "time": _ts(2), "user_query": "第二行\n三行", "bot_answer": "好的"},
            {"msg_id": "m1", "time": _ts(1), "user_query": "第一行\n第二行", "bot_answer": "嗯"},
        ]
        payload = self._payload(history)
        joined = "\n".join(m["content"] for m in payload)
        self.assertNotIn("这条该被屏蔽", joined)
        self.assertNotIn("屏蔽的回复", joined)
        self.assertIn("第一行<br>第二行", joined)
        self.assertIn("第二行<br>三行", joined)
        self.assertEqual(len(payload), 4)

    def test_context_masked_false_is_still_visible(self):
        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "还能看到", "bot_answer": "嗯",
             "context_masked": False},
        ]
        payload = self._payload(history)
        self.assertTrue(any("还能看到" in m["content"] for m in payload))

    def test_context_masked_does_not_affect_unreplied_selection(self):

        history = [
            {"msg_id": "m1", "time": _ts(1), "user_query": "在吗", "context_masked": True},
        ]
        run = tenshi_utils.select_unreplied_run(history)
        self.assertEqual([m["msg_id"] for m in run], ["m1"])


class ContextMaskedCompressionTest(unittest.TestCase):
    """压缩器：有效轮次/待压缩窗口都跳过 context_masked，且不会反复重压。"""

    def setUp(self):
        if TENSHI_REPLY_IMPORT_ERROR is not None:
            self.skipTest(f"依赖 import 失败：{TENSHI_REPLY_IMPORT_ERROR}")

    def _mk_turns(self, n: int) -> list[dict]:
        return [
            {"time": _ts(-i), "user_query": f"u{i}", "bot_answer": f"b{i}"}
            for i in range(n)
        ]

    def _pkg(self, slice_nf: list[dict]) -> dict:
        oldest, newest = cc._slice_time_span(slice_nf)
        return {
            "summary": "x",
            "created_at": 1.0,
            "start_time": oldest,
            "end_time": newest,
            "start_time_label": cc.format_mmdd_hhmm(oldest),
            "end_time_label": cc.format_mmdd_hhmm(newest),
        }

    def test_effective_turns_skip_masked(self):
        turns = self._mk_turns(5)
        turns[1]["context_masked"] = True
        eff = cc._effective_turns_newest_first(turns)
        self.assertEqual([t["user_query"] for t in eff], ["u0", "u2", "u3", "u4"])

    def test_masked_turn_never_enters_due_slice(self):
        turns = self._mk_turns(30)
        anchor = self._pkg(turns[15:25])

        turns[3]["context_masked"] = True
        eff = cc._effective_turns_newest_first(turns)
        due = cc._select_due_slice(eff, [anchor])
        self.assertIsNotNone(due)
        self.assertEqual(len(due), 10)
        self.assertNotIn("u3", [t["user_query"] for t in due])

    def test_compression_progresses_and_does_not_repeat(self):
        turns = self._mk_turns(30)
        anchor = self._pkg(turns[15:25])
        turns[3]["context_masked"] = True
        eff = cc._effective_turns_newest_first(turns)

        due = cc._select_due_slice(eff, [anchor])
        self.assertIsNotNone(due)
        new_pkg = self._pkg(due)


        self.assertIsNone(cc._select_due_slice(eff, [anchor, new_pkg]))

    def test_masked_only_tail_does_not_stall_bootstrap(self):
        turns = self._mk_turns(25)
        for i in (0, 1, 2):
            turns[i]["context_masked"] = True
        eff = cc._effective_turns_newest_first(turns)
        self.assertEqual(len(eff), 22)
        due = cc._bootstrap_due_slices(eff, [])
        self.assertEqual(due, [(5, 15)])
        sl = eff[due[0][0]:due[0][1]]
        self.assertFalse(any(t.get("context_masked") for t in sl))


class ContextMaskedSummaryTest(unittest.TestCase):
    """长期记忆总结：context_masked 记录不进总结材料。"""

    def setUp(self):
        if TENSHI_REPLY_IMPORT_ERROR is not None:
            self.skipTest(f"依赖 import 失败：{TENSHI_REPLY_IMPORT_ERROR}")

    def test_is_context_masked_message(self):
        self.assertTrue(log_update.is_context_masked_message({"context_masked": True}))
        self.assertFalse(log_update.is_context_masked_message({"context_masked": False}))
        self.assertFalse(log_update.is_context_masked_message({}))
        self.assertFalse(log_update.is_context_masked_message("not-a-dict"))

    def test_summarize_returns_early_when_all_masked(self):

        msgs = [
            {"msg_id": "m1", "time": _ts(0), "user_query": "秘密", "bot_answer": "知道了",
             "context_masked": True},
        ]
        self.assertEqual(log_update.global_segment_and_summarize(list(msgs)), (None, None, "completed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
