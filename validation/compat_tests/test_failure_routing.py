# SPDX-License-Identifier: AGPL-3.0-only











from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")

os.environ.pop("PERCEPTION_SCHEDULE_SYNC_URL", None)
os.environ.pop("PERCEPTION_SCHEDULE_SYNC_SECRET", None)

IMPORT_ERROR: Exception | None = None
try:
    import grok_modules
    import perception_layer
    import perception_schedule
    import tenshi_config
    import tenshi_reply
    from llm_routing import (
        build_deepseek_paths,
        build_gemini_paths,
        build_gpt_paths,
        build_grok_paths,
    )
except Exception as e:
    IMPORT_ERROR = e


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class PathOrderTest(unittest.TestCase):
    """1) 2026-10 通路口径（由 providers.PROVIDER_PATH_POLICIES 决定）：
    Gemini/Grok/GPT/Claude 代理 → OpenRouter → 官方；
    DeepSeek/GLM 官方 → 代理 → OpenRouter。

    本类刻意把 OpenRouter 关掉（patch tenshi_config.OPENROUTER_API_KEY=None），
    只验证「代理 vs 官方」的相对顺序，结果不应受本机 .env 是否配了 OR key 影响。
    """

    PROXIES = [{"name": "Relay", "api_key": "r", "base_url": "https://relay.example"}]

    def setUp(self):
        self._orig_or_key = tenshi_config.OPENROUTER_API_KEY
        tenshi_config.OPENROUTER_API_KEY = None

    def tearDown(self):
        tenshi_config.OPENROUTER_API_KEY = self._orig_or_key

    def test_deepseek_official_first_then_proxies(self):
        paths = build_deepseek_paths("k", "https://api.deepseek.com", self.PROXIES)
        self.assertEqual([p["name"] for p in paths], ["Official", "Relay"])
        self.assertTrue(paths[0]["is_official"])

    def test_gemini_proxies_first_then_official(self):
        paths = build_gemini_paths("k", self.PROXIES)
        self.assertEqual([p["name"] for p in paths], ["Relay", "Official"])
        self.assertTrue(paths[-1]["is_official"])

    def test_grok_proxies_first_then_official(self):
        paths = build_grok_paths("k", "https://api.x.ai/v1", self.PROXIES)
        self.assertEqual([p["name"] for p in paths], ["Relay", "Official"])
        self.assertTrue(paths[-1]["is_official"])

    def test_gpt_proxies_first_then_official(self):

        self.assertEqual(
            [p["name"] for p in build_gpt_paths("k", "https://api.openai.com/v1", [])],
            ["OpenAI"],
        )
        paths = build_gpt_paths("k", "https://api.openai.com/v1", self.PROXIES)
        self.assertEqual([p["name"] for p in paths], ["Relay", "OpenAI"])
        self.assertTrue(paths[-1]["is_official"])

    def test_gpt_two_proxies_keep_list_order(self):
        """2026-11：GPT 有代理1/代理2 两条，build_gpt_paths 必须按传入列表顺序排（代理1 → 代理2）。"""
        proxies = [
            {"name": "GPT Proxy", "api_key": "k1", "base_url": "https://codeyu.shop/v1"},
            {"name": "GPT Proxy 2", "api_key": "k2", "base_url": "https://api.coderplan.ai/v1"},
        ]
        paths = build_gpt_paths("k", "https://api.openai.com/v1", proxies)
        self.assertEqual(
            [p["name"] for p in paths], ["GPT Proxy", "GPT Proxy 2", "OpenAI"]
        )
        self.assertTrue(paths[-1]["is_official"])

    def test_gpt_provider_does_not_reuse_claude_proxies(self):
        """Claude 代理是 Anthropic 格式，不能拿来当 GPT 通路（GPT 有自己独立的代理变量）。"""
        import inspect

        src = inspect.getsource(tenshi_reply._run_path_first_provider)
        gpt_branch = src.split('elif provider_name == "gpt":', 1)[1].split("elif provider_name", 1)[0]
        self.assertIn(
            "build_gpt_paths(GPT_API_KEY, tenshi_config.GPT_BASE_URL, tenshi_config.GPT_PROXIES)",
            gpt_branch,
        )
        self.assertNotIn("CLAUDE_PROXIES", gpt_branch)


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class PathOrderWithOpenRouterTest(unittest.TestCase):
    """1b) 配了 OpenRouter key 时，OR 必须插在策略规定的位置（且复用同一份模型 slug）。"""

    PROXIES = [{"name": "Relay", "api_key": "r", "base_url": "https://relay.example"}]

    def setUp(self):
        self._orig_or = (
            tenshi_config.OPENROUTER_API_KEY,
            tenshi_config.OPENROUTER_BASE_URL,
        )
        tenshi_config.OPENROUTER_API_KEY = "dummy-for-tests"
        tenshi_config.OPENROUTER_BASE_URL = "https://openrouter.example/api/v1"

    def tearDown(self):
        (
            tenshi_config.OPENROUTER_API_KEY,
            tenshi_config.OPENROUTER_BASE_URL,
        ) = self._orig_or

    def test_proxy_then_openrouter_then_official(self):
        for provider, build, official_name in (
            ("gemini", lambda: build_gemini_paths("k", self.PROXIES), "Official"),
            ("grok", lambda: build_grok_paths("k", "https://api.x.ai/v1", self.PROXIES), "Official"),
            ("gpt", lambda: build_gpt_paths("k", "https://api.openai.com/v1", self.PROXIES), "OpenAI"),
        ):
            paths = build()
            self.assertEqual(
                [p["name"] for p in paths],
                ["Relay", "OpenRouter", official_name],
                provider,
            )
            self.assertTrue(paths[-1]["is_official"], provider)
            or_path = paths[1]
            self.assertTrue(or_path.get("openrouter"), provider)
            self.assertEqual(or_path.get("protocol"), "openai", provider)

    def test_deepseek_official_then_proxy_then_openrouter(self):
        paths = build_deepseek_paths("k", "https://api.deepseek.com", self.PROXIES)
        self.assertEqual([p["name"] for p in paths], ["Official", "Relay", "OpenRouter"])
        self.assertTrue(paths[0]["is_official"])
        self.assertEqual(paths[-1].get("vendor_prefix"), "deepseek/")

    def test_openrouter_uses_provider_vendor_prefix(self):
        for build, prefix in (
            (lambda: build_gemini_paths("k", self.PROXIES), "google/"),
            (lambda: build_grok_paths("k", "https://api.x.ai/v1", self.PROXIES), "x-ai/"),
            (lambda: build_gpt_paths("k", "https://api.openai.com/v1", self.PROXIES), "openai/"),
        ):
            or_path = next(p for p in build() if p["name"] == "OpenRouter")
            self.assertEqual(or_path.get("vendor_prefix"), prefix)


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class OpenRouterTextStreamTest(unittest.TestCase):
    """2a) OpenRouter 纯文本调用：可流式、可传完整上下文、不带 tools。"""

    @staticmethod
    def _chunk(text: str):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
        )

    def test_streams_deltas_and_joins_text(self):
        captured: dict = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return iter([self._c("你"), self._c("好"), self._c("世界"), SimpleNamespace(choices=[])])

            @staticmethod
            def _c(text):
                return SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        orig = grok_modules._build_openai_client
        seen: list[str] = []
        try:
            grok_modules._build_openai_client = lambda *a, **k: fake_client
            text, thinking = grok_modules.call_openrouter(
                "some/model",
                "sys",
                [{"role": "user", "content": "hi"}],
                stream_callback=seen.append,
            )
        finally:
            grok_modules._build_openai_client = orig

        self.assertEqual(text, "你好世界")
        self.assertEqual(thinking, "")
        self.assertEqual(seen, ["你", "好", "世界"])
        self.assertTrue(captured.get("stream"))
        self.assertNotIn("tools", captured)
        self.assertEqual(captured["messages"][0], {"role": "system", "content": "sys"})


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class NsfwSlotPriorityTest(unittest.TestCase):
    """2b) NSFW 与主回复走同一条两步流程；模型差异只剩 grok 槽优先，槽内 MagmaRouter 置顶 mimo。"""

    def test_magmarouter_order_puts_mimo_first(self):
        models = tenshi_config.magmarouter_models()
        self.assertEqual(models[0], "mimo-v2.6-flash-xploded")
        self.assertEqual(tenshi_config.magmarouter_primary(), models[0])

    def test_nsfw_slot_and_rewrite_use_slot_head(self):
        self.assertEqual(tenshi_config.grok_slot_models()[0], "mimo-v2.6-flash-xploded")
        self.assertIn(
            tenshi_config.grok_slot_rewrite_model(), tenshi_config.grok_slot_models()
        )
        self.assertEqual(tenshi_config.TASK_MODEL_PRIMARY["nsfw"], "mimo-v2.6-flash-xploded")
        self.assertEqual(tenshi_config.TASK_MODEL_PRIMARY["nsfw_rewrite"], "mimo-v2.6-flash-xploded")

    def test_nsfw_model_groups_grok_slot_then_dialog_chain(self):

        groups = tenshi_reply._build_reply_model_groups(True, "medium")
        self.assertEqual(groups[0], ("grok", tenshi_config.grok_slot_models()))
        slot = set(tenshi_config.grok_slot_models())
        expected_tail = [
            m for m in tenshi_reply._build_dialog_model_chain("medium") if m not in slot
        ]
        tail_flat = [m for _p, ids in groups[1:] for m in ids]
        self.assertEqual(tail_flat, expected_tail)
        self.assertTrue(
            [m for m in tail_flat if tenshi_config.provider_for_model(m) != "grok"],
            "打字阶段会跳过 grok 槽，NSFW 链上必须有非 grok 候选",
        )

    def test_two_step_is_not_downgraded_for_nsfw(self):


        expected = bool(getattr(tenshi_config, "REPLY_TWO_STAGE_ENABLED", True))
        self.assertEqual(tenshi_reply._two_step_enabled(True), expected)
        self.assertEqual(tenshi_reply._two_step_enabled(False), expected)

    def test_plain_model_groups_match_dialog_chain(self):

        groups = tenshi_reply._build_reply_model_groups(False, "medium")
        chain = tenshi_reply._build_dialog_model_chain("medium")
        self.assertEqual([m for _p, ids in groups for m in ids], chain)
        self.assertEqual(len(groups), len(chain))
        self.assertTrue(all(len(ids) == 1 for _p, ids in groups))


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class FailedReplyRetryTest(unittest.TestCase):
    """3) 全链失败 → 登记近程重试（[reply-retry n/N] 标记 + 有上限地连续重试）。"""

    def setUp(self):
        self._orig_impl = tenshi_reply._schedule_perception_impl
        self._orig_pending = tenshi_reply.has_pending_reply_retry
        self._orig_enabled = tenshi_config.FAILED_REPLY_RETRY_ENABLED
        self._orig_max = tenshi_config.FAILED_REPLY_RETRY_MAX_ATTEMPTS
        self._orig_note_env = os.environ.pop("PERCEPTION_SCHEDULE_NOTE", None)

    def tearDown(self):
        tenshi_reply._schedule_perception_impl = self._orig_impl
        tenshi_reply.has_pending_reply_retry = self._orig_pending
        tenshi_config.FAILED_REPLY_RETRY_ENABLED = self._orig_enabled
        tenshi_config.FAILED_REPLY_RETRY_MAX_ATTEMPTS = self._orig_max
        if self._orig_note_env is None:
            os.environ.pop("PERCEPTION_SCHEDULE_NOTE", None)
        else:
            os.environ["PERCEPTION_SCHEDULE_NOTE"] = self._orig_note_env

    def _stub(self, pending=False):
        calls: dict = {}
        tenshi_reply.has_pending_reply_retry = lambda: pending
        tenshi_reply._schedule_perception_impl = lambda **kw: calls.update(kw) or "[TOOL RESULT] ok"
        return calls

    def test_schedules_marked_near_term_retry(self):
        calls = self._stub()

        out = tenshi_reply._schedule_failed_reply_retry(reason="全链 23 个候选全灭")

        self.assertTrue(calls["note"].startswith(perception_schedule.REPLY_RETRY_NOTE_PREFIX))
        self.assertIn("全链 23 个候选全灭", calls["note"])
        self.assertEqual(
            perception_schedule.parse_reply_retry_note(calls["note"]),
            (1, tenshi_config.FAILED_REPLY_RETRY_MAX_ATTEMPTS),
        )
        self.assertEqual(
            calls["interval_minutes"], tenshi_config.FAILED_REPLY_RETRY_INTERVAL_MINUTES
        )
        self.assertIn("ok", out)

    def test_retry_run_schedules_next_attempt(self):
        os.environ["PERCEPTION_SCHEDULE_NOTE"] = "[reply-retry 1/3] 补跑回复"
        calls = self._stub(pending=True)
        tenshi_reply._schedule_failed_reply_retry(reason="again")
        self.assertEqual(perception_schedule.parse_reply_retry_note(calls["note"]), (2, 3))

    def test_stops_after_max_attempts(self):
        tenshi_config.FAILED_REPLY_RETRY_MAX_ATTEMPTS = 3
        os.environ["PERCEPTION_SCHEDULE_NOTE"] = "[reply-retry 3/3] 补跑回复"
        called: list = []
        tenshi_reply.has_pending_reply_retry = lambda: False
        tenshi_reply._schedule_perception_impl = lambda **kw: called.append(kw)
        self.assertIsNone(tenshi_reply._schedule_failed_reply_retry())
        self.assertEqual(called, [])

    def test_skips_when_one_is_already_queued(self):
        called = self._stub(pending=True)
        self.assertIsNone(tenshi_reply._schedule_failed_reply_retry())
        self.assertEqual(called, {})

    def test_disabled_by_config(self):
        tenshi_config.FAILED_REPLY_RETRY_ENABLED = False
        called = self._stub()
        self.assertIsNone(tenshi_reply._schedule_failed_reply_retry())
        self.assertEqual(called, {})

    def test_scheduling_failure_is_swallowed(self):
        tenshi_reply.has_pending_reply_retry = lambda: False

        def boom(**kw):
            raise RuntimeError("KV down")

        tenshi_reply._schedule_perception_impl = boom
        self.assertIsNone(tenshi_reply._schedule_failed_reply_retry())


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class ReplyRetryQueueTest(unittest.TestCase):
    """3b) 队列层面：标记识别 + 去重判定 + 触发后消费。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = tenshi_config.PROJECT_ROOT
        tenshi_config.PROJECT_ROOT = self._tmp.name

    def tearDown(self):
        tenshi_config.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def test_marker_helpers_and_consume(self):
        self.assertTrue(perception_schedule.is_reply_retry_note("[reply-retry] x"))
        self.assertTrue(perception_schedule.is_reply_retry_note("[reply-retry 2/3] x"))
        self.assertFalse(perception_schedule.is_reply_retry_note("普通定时起念"))
        self.assertFalse(perception_schedule.is_reply_retry_note("[reply-retry-x]"))
        self.assertEqual(perception_schedule.parse_reply_retry_note("[reply-retry] x"), (1, 1))
        self.assertEqual(perception_schedule.parse_reply_retry_note("[reply-retry 2/3] x"), (2, 3))
        self.assertIsNone(perception_schedule.parse_reply_retry_note("普通起念"))
        self.assertEqual(
            perception_schedule.build_reply_retry_note("boom", 1, 1).startswith("[reply-retry]"),
            True,
        )
        self.assertTrue(
            perception_schedule.is_reply_retry_note(
                perception_schedule.build_reply_retry_note("boom", 2, 3)
            )
        )
        self.assertEqual(
            perception_schedule.parse_reply_retry_note(
                perception_schedule.build_reply_retry_note("boom", 2, 3)
            ),
            (2, 3),
        )
        self.assertFalse(perception_schedule.has_pending_reply_retry())

        perception_schedule.schedule_perception(note="[reply-retry] 补跑回复", interval_minutes=2)
        self.assertTrue(perception_schedule.has_pending_reply_retry())

        entries = perception_schedule.list_pending_schedule_entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(perception_schedule.is_reply_retry_note(entries[0]["note"]))
        self.assertEqual(perception_schedule.load_pending_schedule_entry()["id"], entries[0]["id"])

        perception_schedule.complete_scheduled_trigger(entries[0]["trigger_at_ms"])
        self.assertFalse(perception_schedule.has_pending_reply_retry())
        self.assertEqual(perception_schedule.list_pending_schedule_entries(), [])

    def test_normal_entry_is_not_a_retry(self):
        perception_schedule.schedule_perception(note="晚上提醒他喝水", interval_minutes=5)
        self.assertFalse(perception_schedule.has_pending_reply_retry())


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class PerceptionReplyRetryTriggerTest(unittest.TestCase):
    """4) 感知层只认 [reply-retry]：补跑回复引擎，且不抢普通定时起念。"""

    def setUp(self):
        self._orig = perception_layer.load_pending_schedule_entry
        self._orig_called = perception_layer._called_from_perception_workflow

    def tearDown(self):
        perception_layer.load_pending_schedule_entry = self._orig
        perception_layer._called_from_perception_workflow = self._orig_called

    def test_payload_note_marker_is_detected(self):
        perception_layer.load_pending_schedule_entry = lambda: None
        entry = perception_layer._resolve_reply_retry_trigger("[reply-retry] 补跑")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["trigger_at_ms"], 0)

    def test_queue_head_marker_used_when_payload_has_no_note(self):
        perception_layer.load_pending_schedule_entry = lambda: {
            "id": "sched_x",
            "note": "[reply-retry] 补跑",
            "trigger_at_ms": 123,
        }
        entry = perception_layer._resolve_reply_retry_trigger("")
        self.assertEqual(entry["id"], "sched_x")
        self.assertEqual(entry["trigger_at_ms"], 123)

    def test_ordinary_note_never_hijacked_by_queued_retry(self):
        perception_layer.load_pending_schedule_entry = lambda: {
            "id": "sched_retry",
            "note": "[reply-retry] 补跑",
            "trigger_at_ms": 123,
        }
        self.assertIsNone(perception_layer._resolve_reply_retry_trigger("晚上提醒他喝水"))

    def test_no_marker_no_trigger(self):
        perception_layer.load_pending_schedule_entry = lambda: None
        self.assertIsNone(perception_layer._resolve_reply_retry_trigger(""))
        self.assertIsNone(perception_layer._resolve_reply_retry_trigger("普通起念"))

    def test_run_perception_consumes_retry_without_perception_message(self):
        """带标记的定时触发 → 先消费条目，再补跑回复引擎；不走定时起念流程。"""
        import asyncio

        orig = (
            perception_layer.get_last_interaction,
            perception_layer._maybe_reply_if_unreplied_before_probability,
            perception_layer.complete_scheduled_trigger,
            perception_layer.load_pending_schedule_entry,
        )
        order: list = []
        try:
            perception_layer.get_last_interaction = lambda: (None, 0, 0)
            perception_layer._maybe_reply_if_unreplied_before_probability = (
                lambda: order.append("reply_engine") or True
            )
            perception_layer.complete_scheduled_trigger = (
                lambda ms=None: order.append(("consumed", ms))
            )
            perception_layer.load_pending_schedule_entry = lambda: None

            rc = asyncio.run(
                perception_layer.run_perception(
                    scheduled_force=True,
                    schedule_note="[reply-retry 2/3] 补跑",
                    schedule_trigger_at_ms=12345,
                )
            )
        finally:
            (
                perception_layer.get_last_interaction,
                perception_layer._maybe_reply_if_unreplied_before_probability,
                perception_layer.complete_scheduled_trigger,
                perception_layer.load_pending_schedule_entry,
            ) = orig

        self.assertEqual(rc, 0)
        self.assertEqual(
            order,
            [("consumed", 12345), "reply_engine"],
            "必须先消费条目再补跑，这样补跑再失败还能登记下一次",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
