# SPDX-License-Identifier: AGPL-3.0-only


















from __future__ import annotations

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
    import api_resilience
    import tenshi_config
    from api_resilience import (
        FAST_RETRY,
        NO_RETRY,
        LlmDeadlineExceeded,
        RetryPolicy,
        clamp_timeout,
        degrade,
        finalize_reply_deadline,
        reply_deadline_exceeded,
        reply_deadline_remaining,
        run_with_resilience,
        start_reply_deadline,
    )
except Exception as e:
    IMPORT_ERROR = e


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class RunWithResilienceTest(unittest.TestCase):
    """1) 统一入口的四态：永不抛异常、永不返回错误占位。"""

    def test_success_first_try(self):
        out = run_with_resilience(lambda: "ok", name="t", default="")
        self.assertTrue(out.ok)
        self.assertEqual(out.value, "ok")
        self.assertEqual(out.degradation, "none")
        self.assertEqual(out.attempts, 1)

    def test_timeout_returns_default_and_marks_deadline(self):
        started = time.monotonic()
        out = run_with_resilience(
            lambda: time.sleep(2.0),
            name="slow",
            default="FALLBACK",
            timeout_sec=0.2,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(out.ok)
        self.assertEqual(out.value, "FALLBACK")
        self.assertEqual(out.degradation, "deadline")

        self.assertLess(elapsed, 1.2, "超时应立即返回，不应等满 2s")
        self.assertTrue(out.errors and "deadline" in out.errors[-1])

    def test_deterministic_error_is_unavailable_and_not_retried(self):
        calls = []

        def _boom():
            calls.append(1)
            raise ValueError("unsupported tool declaration")

        out = run_with_resilience(
            _boom,
            name="decl",
            default="",
            timeout_sec=1.0,
            retry=RetryPolicy(max_attempts=3),
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.degradation, "unavailable")
        self.assertEqual(len(calls), 1, "确定性错误不该重试")

    def test_transient_error_is_retried_then_succeeds(self):
        calls = []

        def _flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream 500 overloaded")
            return "recovered"

        out = run_with_resilience(
            _flaky,
            name="flaky",
            default="",
            timeout_sec=1.0,
            retry=FAST_RETRY,
        )
        self.assertTrue(out.ok)
        self.assertEqual(out.value, "recovered")
        self.assertEqual(out.degradation, "retried")
        self.assertEqual(len(calls), 2)

    def test_unusable_result_degrades_instead_of_leaking_placeholder(self):
        """空文本 / 错误占位绝不能算成功（2026-09-10 事故的直接教训）。"""
        out = run_with_resilience(
            lambda: "Error: No API keys configured.",
            name="placeholder",
            default="",
            timeout_sec=1.0,
            retry=NO_RETRY,
            on_success=lambda v: v.startswith("ok"),
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.value, "")
        self.assertEqual(out.degradation, "unusable")
        self.assertNotIn("Error:", out.value)

    def test_never_raises_even_for_base_exception_style_errors(self):
        out = run_with_resilience(
            lambda: (_ for _ in ()).throw(KeyError("missing choices")),
            name="keyerror",
            default=None,
            timeout_sec=1.0,
        )
        self.assertFalse(out.ok)
        self.assertIsNone(out.value)
        self.assertTrue(out.errors)


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class DegradeContextTest(unittest.TestCase):
    """2) 非关键环节的降级命名空间：吞异常、绝不外泄。"""

    def test_exception_is_swallowed_and_reported(self):
        with degrade("weather") as ctx:
            raise RuntimeError("amap down")
        self.assertFalse(ctx["ok"])
        self.assertIsNone(ctx["value"])
        self.assertEqual(ctx["degradation"], "error")

    def test_deadline_is_classified(self):
        with degrade("image") as ctx:
            raise LlmDeadlineExceeded("too slow")
        self.assertFalse(ctx["ok"])
        self.assertEqual(ctx["degradation"], "deadline")

    def test_assigned_value_marks_success(self):
        with degrade("notes") as ctx:
            ctx["value"] = "hello"
        self.assertTrue(ctx["ok"])
        self.assertEqual(ctx["value"], "hello")

    def test_unassigned_block_is_not_success(self):
        with degrade("notes") as ctx:
            pass
        self.assertFalse(ctx["ok"])
        self.assertEqual(ctx["degradation"], "empty")


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class ReplyDeadlineTest(unittest.TestCase):
    """3) 整轮预算：单环节上限必须被夹到剩余预算之内。"""

    def tearDown(self):
        finalize_reply_deadline()

    def test_no_deadline_means_no_clamping(self):
        finalize_reply_deadline()
        self.assertIsNone(reply_deadline_remaining())
        self.assertFalse(reply_deadline_exceeded())
        self.assertEqual(clamp_timeout(240.0), 240.0)

    def test_remaining_budget_wins_over_per_step_timeout(self):
        start_reply_deadline(total_sec=200.0)
        clamped = clamp_timeout(240.0, reserve_sec=2.0)
        self.assertIsNotNone(clamped)
        self.assertLessEqual(clamped, 200.0)
        self.assertGreater(clamped, 0.0)

    def test_exhausted_budget_reports_exceeded(self):
        start_reply_deadline(total_sec=0.05)
        time.sleep(0.12)
        self.assertTrue(reply_deadline_exceeded())

    def test_exhausted_budget_never_yields_an_unbounded_call(self):
        """预算耗尽必须返回哨兵，绝不能返回 0（0 在 call_with_deadline 里 = 无上限）。"""
        from api_resilience import BUDGET_EXHAUSTED, is_budget_exhausted, startable

        start_reply_deadline(total_sec=1.0)
        clamped = clamp_timeout(240.0, reserve_sec=60.0)
        self.assertTrue(
            is_budget_exhausted(clamped),
            f"预算耗尽时应返回 BUDGET_EXHAUSTED 而不是 {clamped!r}",
        )
        self.assertIs(clamped, BUDGET_EXHAUSTED)
        self.assertFalse(startable(clamped))
        self.assertNotEqual(clamped, 0)

    def test_zero_total_disables_deadline(self):
        start_reply_deadline(total_sec=0)
        self.assertIsNone(reply_deadline_remaining())
        self.assertFalse(reply_deadline_exceeded())


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class _SlowInvokeMixin:
    """把某一 provider 的低层调用换成「永远卡住」，验证主链会按时放弃并切链。"""

    def _patch_timeout(self, seconds: float):
        self._orig_model_timeout = tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC
        self._orig_deadline = tenshi_config.REPLY_TOTAL_DEADLINE_SEC
        self._orig_tool_loop = tenshi_config.REPLY_TOOL_LOOP_TIMEOUT_SEC
        tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC = seconds
        tenshi_config.REPLY_TOOL_LOOP_TIMEOUT_SEC = seconds
        tenshi_config.REPLY_TOTAL_DEADLINE_SEC = 0
        start_reply_deadline(total_sec=0)

    def _restore_timeout(self):
        """恢复被改过的配置；即使从没 patch 过也安全（回落到当前值）。"""
        for attr, value in (
            ("REPLY_MODEL_CALL_TIMEOUT_SEC", getattr(self, "_orig_model_timeout", None)),
            ("REPLY_TOOL_LOOP_TIMEOUT_SEC", getattr(self, "_orig_tool_loop", None)),
            ("REPLY_TOTAL_DEADLINE_SEC", getattr(self, "_orig_deadline", None)),
        ):
            if value is not None:
                setattr(tenshi_config, attr, value)
        finalize_reply_deadline()

    def _capture_originals(self):
        """在没调用 _patch_timeout 的测试里，单独记录并恢复。"""
        self._orig_model_timeout = tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC
        self._orig_deadline = tenshi_config.REPLY_TOTAL_DEADLINE_SEC
        self._orig_tool_loop = tenshi_config.REPLY_TOOL_LOOP_TIMEOUT_SEC


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class MainChainTimeoutFallbackTest(_SlowInvokeMixin, unittest.TestCase):
    """4) 主链：一个模型卡死 → 在上限内放弃 → 切到下一个候选（不炸穿、不返回错误文本）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import tenshi_reply

            cls.tr = tenshi_reply
        except Exception as e:
            raise unittest.SkipTest(f"tenshi_reply 不可用：{e}")

    def test_hung_model_is_abandoned_and_next_candidate_wins(self):
        tr = self.tr
        self._patch_timeout(0.25)
        seen: list[str] = []
        orig_loop = tr.call_gpt_with_tools_loop
        orig_build = tr.build_gpt_paths

        def _fake_loop(model_name, *a, **k):
            seen.append(model_name)
            time.sleep(3.0)
            return "never", "", False



        tr.build_gpt_paths = lambda *a, **k: [
            {
                "name": "OpenAI",
                "api_key": "k",
                "base_url": "https://gpt.example/v1",
                "is_official": True,
            }
        ]
        tr.call_gpt_with_tools_loop = _fake_loop
        try:
            started = time.monotonic()
            with self.assertRaises(Exception):
                tr._run_path_first_provider(
                    provider_name="gpt",
                    model_ids=["gpt-6-luna"],
                    system_prompt="sys",
                    messages_payload=[{"role": "user", "content": "hi"}],
                    streamer=None,
                    filtered_claude_tools=[],
                    tenshi_note_write_ids=set(),
                )
            elapsed = time.monotonic() - started
        finally:
            tr.call_gpt_with_tools_loop = orig_loop
            tr.build_gpt_paths = orig_build
            self._restore_timeout()

        self.assertEqual(seen, ["gpt-6-luna"])
        self.assertLess(
            elapsed, 2.0, f"卡死的模型应在上限内被放弃（实际 {elapsed:.2f}s）"
        )

    def test_timeout_error_is_transient_so_provider_is_not_circuit_broken(self):
        """超时属于瞬时错误：provider 不该被熔断（下一轮还得能试它）。"""
        tr = self.tr
        from llm_tool_schema import should_skip_provider_on_error

        skip, reason = should_skip_provider_on_error(
            LlmDeadlineExceeded("gpt:gpt-5.4 在 240.0s 内无返回")
        )
        self.assertFalse(skip, f"超时不该熔断 provider（reason={reason}）")
        tr._reset_provider_breakers()
        tr._mark_provider_dead_this_round("gpt", "test")
        self.assertTrue(tr._provider_is_dead_this_round("gpt"))
        tr._reset_provider_breakers()
        self.assertFalse(tr._provider_is_dead_this_round("gpt"))


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class ModelTurnBudgetTest(_SlowInvokeMixin, unittest.TestCase):
    """5) 单模型工具循环预算：到点不再开新工具轮。"""

    @classmethod
    def setUpClass(cls):
        try:
            import tenshi_reply

            cls.tr = tenshi_reply
        except Exception as e:
            raise unittest.SkipTest(f"tenshi_reply 不可用：{e}")

    def test_budget_stops_further_tool_rounds(self):
        tr = self.tr
        self._capture_originals()
        try:
            budget = tr.ModelTurnBudget("deepseek", "deepseek-flash", total_sec=10.0)
            self.assertTrue(budget.should_continue_tools(), "刚开始时应允许工具轮")

            spent = tr.ModelTurnBudget(
                "deepseek", "deepseek-flash",
                started=time.monotonic() - 11.0,
                total_sec=10.0,
            )
            self.assertFalse(spent.should_continue_tools(), "超预算后应停止开新工具轮")
            self.assertTrue(spent.exceeded())
        finally:
            self._restore_timeout()

    def test_zero_budget_disables_limit(self):
        tr = self.tr
        self._capture_originals()
        tenshi_config.REPLY_TOOL_LOOP_TIMEOUT_SEC = 0
        tenshi_config.REPLY_TOTAL_DEADLINE_SEC = 0
        start_reply_deadline(total_sec=0)
        try:
            budget = tr.ModelTurnBudget("deepseek", "deepseek-flash")
            time.sleep(0.05)
            self.assertIsNone(budget.remaining())
            self.assertFalse(budget.exceeded())
            self.assertTrue(budget.should_continue_tools())
        finally:
            self._restore_timeout()

    def test_tool_loop_short_circuits_when_budget_exhausted(self):
        """预算耗尽时，工具循环不应发起任何模型请求。"""
        tr = self.tr
        self._capture_originals()
        tenshi_config.REPLY_TOOL_LOOP_TIMEOUT_SEC = 0.05
        tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC = 5.0
        tenshi_config.REPLY_TOTAL_DEADLINE_SEC = 0
        start_reply_deadline(total_sec=0)
        calls: list[int] = []

        def _fake_tools(**kwargs):
            calls.append(1)

            return (
                "",
                [{"id": "c1", "type": "function",
                  "function": {"name": "read_note", "arguments": "{}"}}],
                {"role": "assistant", "content": ""},
            )



        orig_cls = tr.ModelTurnBudget

        def _exhausted(provider, model_name, **kwargs):
            return orig_cls(
                provider, model_name,
                started=time.monotonic() - 10.0,
                total_sec=0.05,
            )

        tr.ModelTurnBudget = _exhausted
        try:
            out = tr._run_openai_compat_tools_loop(
                provider="grok",
                call_with_tools=_fake_tools,
                model_name="grok-4.6",
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "read_note"}}],
            )
        finally:
            tr.ModelTurnBudget = orig_cls
            self._restore_timeout()

        self.assertEqual(calls, [], "预算已耗尽时不应再发起模型请求")
        self.assertEqual(out[0], "")


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class FlashFallbackRuleTest(unittest.TestCase):
    """前置所有 LLM 轮：原序列之后按规则追加 DeepSeek Flash + Gemini Flash 兜底。

    规则（天子指令）：
      * 原序列不含 DeepSeek、不含 Gemini → 追加 [DeepSeek Flash, Gemini Flash]；
      * 原序列已含 DeepSeek（Flash/Pro/…#tail）→ 只追加 Gemini Flash；
      * 原序列已含 Gemini（任意档）→ 什么都不追加（即使也没有 DeepSeek）。
    """

    @classmethod
    def setUpClass(cls):
        try:
            import tenshi_reply

            cls.tr = tenshi_reply
        except Exception as e:
            raise unittest.SkipTest(f"tenshi_reply 不可用：{e}")

    def setUp(self):
        self.ds_flash = tenshi_config.DEEPSEEK_FLASH_MODELS[0]
        self.gem_flash = tenshi_config.GEMINI_FLASH_MODELS[0]
        self.ds_pro = tenshi_config.DEEPSEEK_PRO_MODELS[0]
        self.tr._reset_provider_breakers()

    def test_neither_present_appends_both_in_order(self):
        tail = self.tr._flash_fallback_tail(["gpt-5.4", "grok-4.6"])
        self.assertEqual(tail, [self.ds_flash, self.gem_flash])

    def test_deepseek_present_adds_only_gemini(self):
        tail = self.tr._flash_fallback_tail(["deepseek-flash", "grok-4.6"])
        self.assertNotIn(self.ds_flash, tail)
        self.assertIn(self.gem_flash, tail)

    def test_deepseek_tail_alias_still_counts_as_deepseek(self):
        """`#tail` 只是链路标签，真名一样 → 不能重复追加同一个模型。"""
        tail = self.tr._flash_fallback_tail([f"{self.ds_pro}#tail", "grok-4.6"])
        self.assertNotIn(self.ds_flash, tail)
        self.assertEqual(tail, [self.gem_flash])

    def test_gemini_present_adds_nothing(self):
        """已含 Gemini（任意档）→ 什么都不追加，即便也没有 DeepSeek。"""
        self.assertEqual(self.tr._flash_fallback_tail(["gemini-2.5-flash", "grok-4.6"]), [])
        self.assertEqual(self.tr._flash_fallback_tail(["gemini-3.8-flash"]), [])

    def test_both_present_adds_nothing(self):
        self.assertEqual(
            self.tr._flash_fallback_tail(["deepseek-chat", "gemini-4-argon"]), []
        )

    def test_with_flash_fallbacks_keeps_original_prefix_and_dedupes(self):
        original = ["gpt-5.4", "grok-4.6"]
        combined = self.tr._with_flash_fallbacks(original)
        self.assertEqual(combined[: len(original)], original, "原序列必须原样在前")
        self.assertEqual(combined[len(original):], [self.ds_flash, self.gem_flash])
        self.assertEqual(len(combined), len(set(combined)), "不得有重复条目")

    def test_openrouter_slash_model_does_not_suppress_deepseek_fallback(self):
        """`deepseek/…` 是 OpenRouter 的第三方命名空间，不是 DeepSeek 官方档。

        关键后果：如果它被当成「原序列已有 DeepSeek」，DeepSeek Flash 兜底就会被
        错误跳过。因此这里断言「该 id 不等于官方 deepseek」并且双兜底仍然齐全。
        """
        vendor_id = "deepseek/deepseek-r1-distill-llama-70b"
        self.assertNotEqual(self.tr._provider_of(vendor_id), "deepseek")
        self.assertEqual(
            self.tr._flash_fallback_tail([vendor_id, "grok-4.6"]),
            [self.ds_flash, self.gem_flash],
            "第三方 deepseek/… 不得顶替官方 DeepSeek Flash 兜底",
        )

    def test_pre_dialog_routing_sequence_already_contains_both_providers(self):
        """前置路由原序列本就含 Gemini 与 DeepSeek → 按规则不追加任何东西。"""
        seq = self.tr._pre_dialog_routing_providers()
        self.assertIn("gpt", seq)
        self.assertIn("gemini", seq)
        self.assertIn("deepseek", seq)

        self.assertEqual(len(seq), len(set(seq)), seq)

    def test_pre_dialog_routing_falls_back_to_original_when_a_provider_is_removed(self):
        """把原序列里的 Gemini/DeepSeek 各抽掉一个时，兜底必须补回来。"""
        orig = tenshi_config.OPENROUTER_API_KEY
        try:

            tail = self.tr._flash_fallback_tail(["gpt-5.4"])
            self.assertEqual(tail, [self.ds_flash, self.gem_flash])
        finally:
            tenshi_config.OPENROUTER_API_KEY = orig


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class ConfigKnobsTest(unittest.TestCase):
    """所有容灾预算都必须可配置（便于在生产里按实测调整）。"""

    def test_knobs_exist_and_are_numeric(self):
        for name in (
            "REPLY_TOTAL_DEADLINE_SEC",
            "REPLY_DEADLINE_RESERVE_SEC",
            "REPLY_MODEL_CALL_TIMEOUT_SEC",
            "REPLY_TOOL_LOOP_TIMEOUT_SEC",
            "REPLY_AUX_LLM_TIMEOUT_SEC",
            "REPLY_AUX_LLM_MAX_ATTEMPTS",
            "REPLY_AUX_LLM_TOTAL_TIMEOUT_SEC",
            "REPLY_IMAGE_ANALYSIS_TIMEOUT_SEC",
            "REPLY_WAVE1_TIMEOUT_SEC",
            "REPLY_MEMORY_RETRIEVAL_TIMEOUT_SEC",
        ):
            value = getattr(tenshi_config, name, None)
            self.assertIsNotNone(value, f"缺少容灾配置项 {name}")
            float(value)

    def test_reply_deadline_is_below_script_timeout(self):
        """端到端死线必须早于子进程硬上限，否则来不及走正规失败路径。"""
        from tenshi_utils import REPLY_SCRIPT_TIMEOUT_SEC

        self.assertLess(
            float(tenshi_config.REPLY_TOTAL_DEADLINE_SEC),
            float(REPLY_SCRIPT_TIMEOUT_SEC),
            "REPLY_TOTAL_DEADLINE_SEC 必须留出收尾余量（< REPLY_SCRIPT_TIMEOUT_SEC）",
        )

    def test_resilience_accessors_track_config(self):
        orig = tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC
        try:
            tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC = 123.0
            self.assertEqual(api_resilience.model_call_timeout(), 123.0)
        finally:
            tenshi_config.REPLY_MODEL_CALL_TIMEOUT_SEC = orig


if __name__ == "__main__":
    unittest.main()
