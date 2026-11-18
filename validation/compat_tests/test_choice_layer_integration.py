# SPDX-License-Identifier: AGPL-3.0-only











from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")

_THIRD_PARTY = (
    "requests", "httpx", "google", "anthropic", "cryptography", "dotenv", "telegram",
    "openai", "numpy", "filelock", "pytz", "zai", "geopy", "PIL", "pillow_heif", "faiss",
    "sklearn", "tiktoken", "bs4", "markdownify", "fitz", "pypdf", "docx", "jieba",
    "rank_bm25", "timezonefinder", "lxml", "html2text", "trafilatura", "yaml", "aiohttp",
    "zhipuai", "pydantic", "dateutil",
)


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, roots):
        self.roots = set(roots)

    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in self.roots:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        m = mock.MagicMock(name=spec.name)
        m.__path__ = []
        m.__spec__ = spec
        m.__name__ = spec.name
        return m

    def exec_module(self, module):
        pass


def _import_targets():
    try:
        import perception_layer as pl
        import reply_streaming as rs
        import tenshi_reply as tr
        return tr, pl, rs
    except Exception:
        pass
    missing = []
    for root in _THIRD_PARTY:
        try:
            if importlib.util.find_spec(root) is None:
                missing.append(root)
        except Exception:
            missing.append(root)
    before = set(sys.modules)
    finder = _StubFinder(missing)
    sys.meta_path.insert(0, finder)
    try:
        import tenshi_reply as tr
        import perception_layer as pl
        import reply_streaming as rs
    finally:
        sys.meta_path.remove(finder)
        for name in set(sys.modules) - before:
            sys.modules.pop(name, None)
    return tr, pl, rs


IMPORT_ERROR: Exception | None = None
try:
    tenshi_reply, perception_layer, reply_streaming = _import_targets()
    tenshi_config = tenshi_reply.tenshi_config
    deferred_intent = tenshi_reply.deferred_intent
except Exception as e:
    IMPORT_ERROR = e

BUDGET = {
    "bite": ["碗"],
    "signature": "",
    "ask": [],
    "drop": [
        {"what": "他昨晚熬夜的事", "why": "还想骂他", "carry": True},
        {"what": "天气", "why": "无聊", "carry": False},
    ],
    "trigger": "",
}
RAW = (
    "<budget>" + json.dumps(BUDGET, ensure_ascii=False) + "</budget>\n"
    "<reply>\n87。碗呢。<br>别装死。\n</reply>"
)


REWRITE_LEAK = (
    "I need to narrow the original three asks—fish, rain, curtain—down to a single cluster "
    "per the limit. Rain and curtain seem connected, so maybe those pair together, while "
    "the fish mention could stay as a casual remark rather than a request.\n\n"
    "I'll keep the fish line as a statement rather than a question, drop the curtain line, "
    "and keep the rain question, outputting with `<br>` separators and no tags: "
    "\"烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没\".\n\n"
    "烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没"
)


GATEWAY_PLACEHOLDER = (
    "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
    "或模型对当前输入直接结束。请重试或简化输入后再试。"
)


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._patchers = []

        targets = [tenshi_config]
        tunable = getattr(deferred_intent, "tunable", None)
        if tunable is not None and tunable is not tenshi_config:
            targets.append(tunable)
        values = {
            "DEFERRED_INTENT_STORE": os.path.join(d, "deferred_intents.json"),
            "DEFERRED_PILOT_STATS": os.path.join(d, "pilot.json"),
            "TEMP_SUMMARY_STORE": os.path.join(d, "temp_summary.json"),
            "DEFERRED_INTENT_ENABLED": True,
            "DEFERRED_INTENT_MAX_CARRY": 1,
            "EVALUATOR_ENABLED": True,
            "EVALUATOR_MAX_REWRITES": 1,
            "STYLE_GUARD_ENABLED": True,
            "STYLE_GUARD_MAX_REWRITES": 1,
            "BUDGET_STAGE_ENABLED": True,
            "BUDGET_PARSE_STRICT": False,
            "HISTORY_RAW_INJECTION": False,
            "TEMP_SUMMARY_ENABLED": True,
            "TEMP_SUMMARY_REFRESH": True,
        }
        for t in targets:
            for k, v in values.items():
                p = mock.patch.object(t, k, v, create=True)
                p.start()
                self._patchers.append(p)
        p = mock.patch.object(tenshi_reply, "reply_deadline_exceeded", lambda: False)
        p.start()
        self._patchers.append(p)
        self.store = values["DEFERRED_INTENT_STORE"]
        self.stats = values["DEFERRED_PILOT_STATS"]
        self.summary_path = values["TEMP_SUMMARY_STORE"]

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self._tmp.cleanup()

    def _events(self):
        with open(self.stats, encoding="utf-8") as f:
            return json.load(f)["events"]


class BudgetParseTest(_Base):
    """① 预算段被解析，且不进入正文。"""

    def test_budget_parsed_and_not_in_body(self):
        budget, body = tenshi_reply.parse_choice_reply(RAW)
        self.assertEqual(budget["bite"], ["碗"])
        self.assertEqual(body, "87。碗呢。<br>别装死。")
        self.assertNotIn("budget", body)
        self.assertNotIn("<reply>", body)

    def test_bad_json_passes_through_without_tags(self):
        with self.assertLogs("TenshiReply", level="WARNING"):
            budget, body = tenshi_reply.parse_choice_reply("<budget>{坏</budget><reply>正文。</reply>")
        self.assertIsNone(budget)
        self.assertEqual(body, "正文。")

    def test_preamble_appended_literally(self):
        out = tenshi_reply.append_budget_preamble("CORE")
        self.assertTrue(out.startswith("CORE"))
        self.assertIn('<budget>{"bite"', out)
        self.assertEqual(tenshi_reply.append_budget_preamble(out), out)


class StreamingFilterTest(_Base):
    """② 流式过滤：budget 段在流式期间被完整吞掉（含未闭合时的 hold）。"""

    def test_stream_buffer_never_contains_budget(self):
        s = reply_streaming.TelegramStreamer(None, None)
        s.is_active = True
        chunks = ["<bu", "dget>{\"bite\":[\"碗\"],", "\"ask\":[]}</bud", "get>\n<re", "ply>\n87。", "碗呢。</reply>"]
        for i, c in enumerate(chunks):
            s.update(c)
            if i < 3:
                self.assertEqual(s.stream_buffer.strip(), "", f"chunk {i} 时 budget 泄漏：{s.stream_buffer!r}")
        s.finalize_with_text(None)
        self.assertNotIn("budget", s.full_text)
        self.assertNotIn("bite", s.stream_buffer)
        self.assertNotIn("reply", s.stream_buffer)
        self.assertIn("87。碗呢。", s.stream_buffer)

    def test_split_streamable_holds_unclosed_budget(self):
        emit, held = reply_streaming._split_streamable('<budget>{"bite":["x"]')
        self.assertEqual(emit, "")
        self.assertTrue(held.startswith("<budget>"))


class EvaluatorRewriteTest(_Base):
    """③ 评估器判超预算 → 触发一次重写 → 仍超 → 放行最后一版并记日志/试点统计。"""

    def test_over_budget_rewrite_once_then_release(self):
        verdict = {"info_tickets": 3, "signature_tickets": 0, "ask_clusters": 2,
                   "style_violations": [], "over_budget": True, "notes": "多问"}
        eval_calls, rewrite_calls = [], []

        def evaluator(prompt):
            eval_calls.append(prompt)
            return "```json\n" + json.dumps(verdict) + "\n```"

        def rewriter(prompt):
            rewrite_calls.append(prompt)
            return "<reply>改过一版。</reply>"

        with self.assertLogs("TenshiReply", level="WARNING") as cm:
            g = tenshi_reply.apply_choice_guard(
                body="原文。", budget=BUDGET, user_query="吃了吗",
                call_evaluator=evaluator, call_rewrite=rewriter,
            )
        self.assertEqual(len(rewrite_calls), 1)
        self.assertIn("账超了", rewrite_calls[0])
        self.assertIn("原文。", rewrite_calls[0])
        self.assertEqual(len(eval_calls), 2)
        self.assertIn("吃了吗", eval_calls[0])
        self.assertTrue(g["rewritten"])
        self.assertTrue(g["still_over"])
        self.assertEqual(g["text"], "改过一版。")
        self.assertTrue(any("仍超标" in line for line in cm.output))

        tenshi_reply.record_choice_ledger(BUDGET, "m1", g)
        rounds = [e for e in self._events() if e["event"] == "ledger_round"]
        self.assertEqual(len(rounds), 1)
        self.assertTrue(rounds[0]["payload"]["still_over"])
        self.assertEqual(rounds[0]["payload"]["rewrites"], 1)

    def test_evaluator_failure_falls_back_to_heuristics(self):
        def boom(prompt):
            raise RuntimeError("down")

        g = tenshi_reply.apply_choice_guard(
            body="好。", budget=None, user_query="q",
            call_evaluator=boom, call_rewrite=lambda p: self.fail("不该重写"),
        )
        self.assertFalse(g["rewritten"])
        self.assertIsNone(g["verdict"])
        self.assertEqual(g["text"], "好。")


class StyleRewriteTest(_Base):
    """④ 文体命中 → 重写。"""

    def test_bold_triggers_rewrite(self):
        calls = []

        def rewriter(prompt):
            calls.append(prompt)
            return "哼，干净了。"

        g = tenshi_reply.apply_choice_guard(
            body="**哼**，*瞥了一眼*。", budget=None, user_query="q",
            call_evaluator=lambda p: "not json", call_rewrite=rewriter,
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("加粗", calls[0])
        self.assertTrue(g["rewritten"])
        self.assertFalse(g["still_over"])
        self.assertEqual(g["text"], "哼，干净了。")

    def test_perception_style_guard_rewrites_once(self):
        calls = []
        out = perception_layer._style_guard_perception_text(
            "红瞳微眯。你又熬夜了？",
            call_rewrite=lambda p: (calls.append(p), "你又熬夜了？")[1],
        )
        self.assertEqual(out, "你又熬夜了？")
        self.assertEqual(len(calls), 1)

    def test_rewrite_strips_model_english_plan(self):
        """2026-11-12 事故回归：重写结果里的英文规划不能外发，只留正文。"""
        out = tenshi_reply.rewrite_choice_text(
            call_rewrite=lambda p: REWRITE_LEAK,
            violations_text="- 旁白",
            original_text="烧鱼啊",
        )
        self.assertEqual(
            out, "烧鱼啊<br>这个月你们家鱼就没断过<br>雨呢，说好十一点多下的，落了没"
        )

    def test_rewrite_placeholder_is_rejected(self):
        """2026-11-12 事故回归：重写通路返回网关占位时，放行上一版而不是把占位发出去。"""
        g = tenshi_reply.apply_choice_guard(
            body="**哼**。", budget=None, user_query="q",
            call_evaluator=lambda p: "not json",
            call_rewrite=lambda p: GATEWAY_PLACEHOLDER,
        )
        self.assertEqual(g["text"], "**哼**。")
        self.assertFalse(g["rewritten"])

    def test_rewrite_failure_keeps_original(self):
        g = tenshi_reply.apply_choice_guard(
            body="**哼**。", budget=None, user_query="q",
            call_evaluator=lambda p: None, call_rewrite=lambda p: "",
        )
        self.assertEqual(g["text"], "**哼**。")
        self.assertFalse(g["rewritten"])


class DeferredLedgerTimingTest(_Base):
    """⑤ 主回复轮 add_intents；感知轮 expire_and_collect → mark_expired_all 两步时序。"""

    def _read_store(self):
        with open(self.store, encoding="utf-8") as f:
            return json.load(f)

    def test_add_then_perception_round_expires(self):
        added = tenshi_reply.record_choice_ledger(BUDGET, "m42", {})
        self.assertEqual(added, 1)
        store = self._read_store()
        self.assertEqual([i["what"] for i in store], ["他昨晚熬夜的事"])
        self.assertEqual(store[0]["status"], "pending")

        rnd = perception_layer._DeferredRound()
        pending = rnd.begin("perception-1")
        self.assertEqual(pending["what"], "他昨晚熬夜的事")
        self.assertEqual(self._read_store()[0]["status"], "pending")
        rnd.finish()
        self.assertEqual(self._read_store()[0]["status"], "expired")
        self.assertIsNone(perception_layer._DeferredRound().begin("perception-2"))

    def test_fired_round_marks_fired_before_expire(self):
        tenshi_reply.record_choice_ledger(BUDGET, "m43", {})
        rnd = perception_layer._DeferredRound()
        rnd.begin("perception-3")
        rnd.fired = True
        rnd.finish()
        self.assertEqual(self._read_store()[0]["status"], "fired")

    def test_run_perception_calls_collect_then_expire(self):

        tenshi_reply.record_choice_ledger(BUDGET, "m44", {})
        manager = mock.MagicMock()
        manager.expire_and_collect.return_value = []

        async def fake_impl(**kwargs):
            kwargs["round_ctx"].begin("perception-9")
            return 0

        with mock.patch.object(perception_layer, "deferred_intent", manager),\
                mock.patch.object(perception_layer, "_run_perception_impl", fake_impl):
            self.assertEqual(asyncio.run(perception_layer.run_perception(force=True)), 0)
        names = [c[0] for c in manager.method_calls]
        self.assertEqual(names, ["expire_and_collect", "mark_expired_all"])

    def test_skipped_round_does_not_touch_ledger(self):
        manager = mock.MagicMock()

        async def fake_impl(**kwargs):
            return 2

        with mock.patch.object(perception_layer, "deferred_intent", manager),\
                mock.patch.object(perception_layer, "_run_perception_impl", fake_impl):
            asyncio.run(perception_layer.run_perception())
        self.assertEqual(manager.method_calls, [])


class StrongHistoryModeTest(_Base):
    """⑥ 强模式：不注入原始历史（含感知条目），注入临时摘要段。"""

    HISTORY = [
        {"role": "user", "content": "（昨天） 早"},
        {"role": "assistant", "content": "（昨天） [主动起念] 起床了没"},
    ]

    def test_strong_mode_drops_raw_history_and_injects_summary(self):
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump({"summary": "他答应今天早睡。", "covers_until_msg_id": "m1"}, f, ensure_ascii=False)
        self.assertEqual(tenshi_reply.select_history_for_main_context(self.HISTORY), [])
        block = tenshi_reply.build_temp_summary_block()
        self.assertIn("临时摘要（原始历史本轮未注入）", block)
        self.assertIn("他答应今天早睡。", block)

    def test_missing_summary_file_injects_empty_section(self):
        block = tenshi_reply.build_temp_summary_block()
        self.assertIn("临时摘要（原始历史本轮未注入）", block)
        self.assertIn("（暂无）", block)

    def test_raw_injection_flag_keeps_history(self):
        with mock.patch.object(tenshi_config, "HISTORY_RAW_INJECTION", True, create=True):
            self.assertEqual(tenshi_reply.select_history_for_main_context(self.HISTORY), self.HISTORY)

    def test_refresh_summary_only_new_turns_and_failure_keeps_old(self):
        old = {"summary": "旧。", "covers_until_msg_id": "m1"}
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False)
        history = [
            {"msg_id": "m3", "user_query": "睡了", "bot_answer": "早该睡。"},
            {"msg_id": "m2", "user_query": "还在写", "bot_answer": "<br>快去睡。"},
            {"msg_id": "m1", "user_query": "早", "bot_answer": "早。"},
        ]
        seen = []
        ok = tenshi_reply.refresh_temp_summary(history, call_model=lambda p: (seen.append(p), "新摘要" * 500)[1])
        self.assertTrue(ok)
        self.assertIn("还在写", seen[0])
        self.assertNotIn("用户：早\n", seen[0])
        with open(self.summary_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["covers_until_msg_id"], "m3")
        self.assertLessEqual(len(data["summary"]), int(tenshi_config.TEMP_SUMMARY_MAX_CHARS))

        def boom(p):
            raise RuntimeError("down")

        history.insert(0, {"msg_id": "m4", "user_query": "嗯", "bot_answer": "嗯。"})
        self.assertFalse(tenshi_reply.refresh_temp_summary(history, call_model=boom))
        with open(self.summary_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)


if __name__ == "__main__":
    unittest.main()
