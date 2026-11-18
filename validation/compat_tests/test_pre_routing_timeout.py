# SPDX-License-Identifier: AGPL-3.0-only













from __future__ import annotations

import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")

IMPORT_ERROR: Exception | None = None
try:
    import grok_modules
    import tenshi_config
    from llm_compat import LlmDeadlineExceeded, call_with_deadline
except Exception as e:
    IMPORT_ERROR = e


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class CallWithDeadlineTest(unittest.TestCase):
    """1) 单次调用上限：到点即抛，异常不吞。"""

    def test_fast_call_returns_value(self):
        self.assertEqual(call_with_deadline(lambda: 42, timeout_sec=5.0, label="t"), 42)

    def test_slow_call_raises_deadline_exceeded(self):
        started = time.monotonic()
        with self.assertRaises(LlmDeadlineExceeded):
            call_with_deadline(lambda: time.sleep(1.5), timeout_sec=0.2, label="slow")

        self.assertLess(time.monotonic() - started, 1.0)

    def test_callee_exception_is_reraised_as_is(self):
        def _boom():
            raise ValueError("upstream 500")

        with self.assertRaises(ValueError):
            call_with_deadline(_boom, timeout_sec=5.0, label="boom")

    def test_non_positive_timeout_runs_inline(self):
        """timeout<=0 = 关闭兜底：同线程同步执行（不套 daemon 线程）。"""
        import threading

        seen: dict = {}

        def _record():
            seen["thread"] = threading.current_thread().name
            return "ok"

        self.assertEqual(call_with_deadline(_record, timeout_sec=0, label="inline"), "ok")
        self.assertEqual(seen["thread"], threading.current_thread().name)


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class PreDialogRoutingTimeoutTest(unittest.TestCase):
    """2/3/4) 前置路由：单模型超时 → 切下一个；总预算兜底。"""

    QUERY = "今天晚饭吃什么好呢？"

    GOOD_ROUTING_JSON = json.dumps(
        {
            "nsfw": False,
            "low_mood_intervention": False,
            "subtext": 2,
            "persona_pressure": 3,
            "narrative_span": 2,
            "staging_craft": 3,
            "tactical_logic": 0,
            "risk_flag": False,
            "reason": "unit-test",
        }
    )

    def setUp(self):
        self._orig_invoke = grok_modules._invoke_pre_dialog_routing_llm
        self._orig_per = tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC
        self._orig_total = tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC
        self._orig_min = tenshi_config.PRE_ROUTING_MIN_ATTEMPT_SEC

        self.calls: list[str] = []

    def tearDown(self):
        grok_modules._invoke_pre_dialog_routing_llm = self._orig_invoke
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = self._orig_per
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = self._orig_total
        tenshi_config.PRE_ROUTING_MIN_ATTEMPT_SEC = self._orig_min

    def _set_hang(self, hang_providers, *, good_provider=None, hang_sec=0.8):
        """挂起指定 provider；其余（或 good_provider）返回一份合法路由 JSON。"""

        def _fake(provider, system_prompt, user_prompt):
            self.calls.append(provider)
            if provider in hang_providers:
                time.sleep(hang_sec)
                return self.GOOD_ROUTING_JSON
            if good_provider is None or provider == good_provider:
                return self.GOOD_ROUTING_JSON
            raise AssertionError(f"不该调用 {provider}")

        grok_modules._invoke_pre_dialog_routing_llm = _fake

    def test_hung_first_provider_falls_through_to_next_model(self):
        """GPT 卡住 → 到点放弃 → 用 Claude Haiku 的结果，而不是等 GPT 回来。"""
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = 0.2
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = 30.0
        self._set_hang({"gpt"}, good_provider="claude")

        started = time.monotonic()
        result = grok_modules.run_pre_dialog_routing(self.QUERY)
        elapsed = time.monotonic() - started

        self.assertEqual(self.calls[:2], ["gpt", "claude"])
        self.assertFalse(result["nsfw"])
        self.assertEqual(result["dialog_grade"]["tier"], "low")

        self.assertLess(elapsed, 0.7)

    def test_all_providers_hung_stops_at_total_budget(self):
        """整条链都卡死：总预算用尽即停，回退安全默认（不会逐个等满）。"""
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = 0.2
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = 0.5
        self._set_hang({"gpt", "claude", "gemini", "grok", "openrouter", "deepseek"}, hang_sec=1.2)

        started = time.monotonic()
        result = grok_modules.run_pre_dialog_routing(self.QUERY)
        elapsed = time.monotonic() - started

        self.assertFalse(result["nsfw"])
        self.assertEqual(result["dialog_grade"]["tier"], "medium")
        self.assertEqual(result["dialog_grade"]["reason"], "routing fallback")
        self.assertLess(elapsed, 1.0)

        self.assertLessEqual(len(self.calls), 4)

    def test_total_budget_clamps_the_per_model_timeout(self):
        """总预算比单模型上限还小时，就用剩余预算当该模型的上限（不等满单模型上限）。

        链首拿到的是「剩余预算 0.4s」而不是单模型上限 10s；它挂起后总预算见底，
        剩余量低于 PRE_ROUTING_MIN_ATTEMPT_SEC（最小可用片），因此直接停链、不再试第二个模型。
        断言 `calls == ["gpt"]` 依赖的就是这条「不开注定失败的新请求」规则——
        早先只判 `remaining <= 0` 时，剩下的零点几毫秒可能仍为正数，
        于是概率性地多起一次 gemini（本用例曾因此 flaky）。
        """
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = 10.0
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = 0.4
        tenshi_config.PRE_ROUTING_MIN_ATTEMPT_SEC = 0.1
        self._set_hang({"gpt", "gemini", "grok", "openrouter", "deepseek"}, hang_sec=1.5)

        started = time.monotonic()
        result = grok_modules.run_pre_dialog_routing(self.QUERY)
        elapsed = time.monotonic() - started

        self.assertEqual(self.calls, ["gpt"])
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["dialog_grade"]["reason"], "routing fallback")

    def test_min_attempt_slice_disabled_restores_old_behaviour(self):
        """把最小可用片关掉（<=0）时，回到「只看剩余是否 <= 0」的老行为。"""
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = 10.0
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = 0.4
        tenshi_config.PRE_ROUTING_MIN_ATTEMPT_SEC = 0.0
        self._set_hang({"gpt", "gemini", "grok", "openrouter", "deepseek"}, hang_sec=1.5)

        result = grok_modules.run_pre_dialog_routing(self.QUERY)


        self.assertGreaterEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], "gpt")
        self.assertEqual(result["dialog_grade"]["reason"], "routing fallback")

    def test_fast_failure_within_budget_still_uses_next_provider(self):
        """链首快速报错（不是挂起）时，预算内继续用下一个模型。"""
        tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC = 10.0
        tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC = 5.0

        def _fake(provider, system_prompt, user_prompt):
            self.calls.append(provider)
            if provider == "gpt":
                raise RuntimeError("upstream 500")
            return self.GOOD_ROUTING_JSON

        grok_modules._invoke_pre_dialog_routing_llm = _fake

        result = grok_modules.run_pre_dialog_routing(self.QUERY)

        self.assertEqual(self.calls[:2], ["gpt", "claude"])
        self.assertFalse(result["nsfw"])

    def test_config_defaults_are_exposed(self):
        self.assertGreater(tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC, 0)
        self.assertGreater(tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC, 0)
        per, total = grok_modules._pre_routing_deadlines()
        self.assertEqual(per, float(tenshi_config.PRE_ROUTING_PROVIDER_TIMEOUT_SEC))
        self.assertEqual(total, float(tenshi_config.PRE_ROUTING_TOTAL_TIMEOUT_SEC))


if __name__ == "__main__":
    unittest.main(verbosity=2)
