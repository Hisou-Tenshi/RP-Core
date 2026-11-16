# SPDX-License-Identifier: AGPL-3.0-only














from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from error_text import (
    exception_chain,
    format_errors,
    format_exception,
    format_exception_inline,
)

REAL_SDK_MESSAGE = (
    "Invalid `timeout` argument; `httpx.Timeout` is from the `httpx` package, "
    "but this SDK uses `httpx2`. Use `httpx2.Timeout`"
)


def _nested_error() -> BaseException:
    """复刻事故现场：SDK 把真实原因包在通用说明里。"""
    try:
        try:
            raise TypeError(REAL_SDK_MESSAGE)
        except TypeError as inner:
            raise RuntimeError("Connection error.") from inner
    except RuntimeError as outer:
        return outer


class ErrorTextTest(unittest.TestCase):
    """异常原文渲染。"""

    def test_chain_exposes_hidden_cause(self):
        outer = _nested_error()
        inline = format_exception_inline(outer)
        self.assertIn("RuntimeError: Connection error.", inline)
        self.assertIn("caused by", inline)
        self.assertIn("TypeError", inline)
        self.assertIn("httpx2", inline, "真实原因必须出现在日志文本里")
        self.assertEqual(len(exception_chain(outer)), 2)

    def test_no_truncation_of_long_messages(self):
        long_message = "x" * 5000
        try:
            raise ValueError(long_message)
        except ValueError as exc:
            inline = format_exception_inline(exc)
            multiline = format_exception(exc)
        self.assertIn(long_message, inline)
        self.assertIn(long_message, multiline)

    def test_multiline_includes_raise_origin(self):
        outer = _nested_error()
        text = format_exception(outer)
        self.assertIn("at ", text)
        self.assertIn(".py:", text)

    def test_format_errors_keeps_every_attempt(self):
        errors = [
            ("claude-opus-5 via Codeyu Base (attempt 1)", ValueError("first-boom")),
            ("claude-opus-5 via CoderPlan (attempt 1)", RuntimeError("second-boom")),
        ]
        text = format_errors(errors)
        self.assertIn("first-boom", text)
        self.assertIn("second-boom", text)
        self.assertIn("Codeyu Base", text)

    def test_none_is_rendered_not_crashed(self):
        self.assertEqual(format_exception_inline(None), "<无异常>")
        self.assertIn("无可用错误信息", format_errors([]))


class SdkTimeoutTest(unittest.TestCase):
    """timeout 必须与 SDK 自己使用的 http 模块一致。"""

    def test_timeout_type_matches_sdk_http_module(self):
        import anthropic
        import openai

        import llm_compat

        for sdk in (anthropic, openai):
            module = llm_compat.sdk_http_module(sdk)
            timeout = llm_compat.build_sdk_timeout(sdk)
            self.assertIsInstance(
                timeout,
                module.Timeout,
                f"{sdk.__name__} 的 timeout 必须由它自己用的 {module.__name__} 构造",
            )

    def test_explicit_connect_read_are_respected(self):
        import anthropic

        import llm_compat

        timeout = llm_compat.build_sdk_timeout(anthropic, connect=3.0, read=42.0)
        if hasattr(timeout, "read"):
            self.assertEqual(float(timeout.connect), 3.0)
            self.assertEqual(float(timeout.read), 42.0)
        else:
            self.assertEqual(float(timeout), 42.0)

    def test_sdk_compat_is_importable_without_tenshi_config(self):
        """sdk_compat 要能被镜像构建期的自检脚本独立加载（不依赖 tenshi_config）。"""
        import re

        import sdk_compat

        with open(sdk_compat.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(
            re.search(r"(?m)^\s*(import tenshi_config|from tenshi_config)", source),
            "sdk_compat 不能 import tenshi_config（构建期镜像里没有业务代码）",
        )


class PathFirstErrorAggregationTest(unittest.TestCase):
    """多通路全灭：异常里要能看到每一次尝试的完整原文。"""

    def test_all_attempt_errors_are_reported(self):
        from llm_routing import run_path_first

        paths = [{"name": "通A"}, {"name": "通B"}]
        seen: list[tuple[str, str]] = []

        def invoke(model_id, path):
            seen.append((model_id, path["name"]))
            raise ValueError(f"{path['name']}-boom")

        with self.assertRaises(RuntimeError) as ctx:
            run_path_first(["m1"], paths, invoke, sleep_fn=lambda _s: None)

        message = str(ctx.exception)
        self.assertEqual(seen, [("m1", "通A"), ("m1", "通B")])
        self.assertIn("通A-boom", message)
        self.assertIn("通B-boom", message)
        self.assertIsInstance(ctx.exception.__cause__, ValueError, "要保留原始异常链")

    def test_claude_model_fallback_reports_all_models(self):
        from llm_routing import run_claude_model_fallback

        def invoke(model_id):
            raise RuntimeError(f"{model_id}-failed")

        with self.assertRaises(RuntimeError) as ctx:
            run_claude_model_fallback(
                ["m-a", "m-b"], invoke, max_attempts_per_model=1, sleep_fn=lambda _s: None
            )
        message = str(ctx.exception)
        self.assertIn("m-a-failed", message)
        self.assertIn("m-b-failed", message)

    def test_routing_error_includes_hidden_cause(self):
        from llm_routing import run_path_first

        def invoke(_model_id, _path):
            raise _nested_error()

        with self.assertRaises(RuntimeError) as ctx:
            run_path_first(["m1"], [{"name": "通A"}], invoke, sleep_fn=lambda _s: None)
        self.assertIn("httpx2", str(ctx.exception), "被包装的真实原因必须一路带上来")


class ClaudePreflightTextTest(unittest.TestCase):
    """Claude 代理预检已退役（2026-10）：只剩「无可用客户端」异常的口径。

    历史：每轮对话前会 ping 一遍 Claude 代理（`_ping_claude_proxy_once` /
    `_run_claude_proxy_pings`）并把代理判死原因塞进异常文本。这套预检**整体删除**后，
    判活改由「每日 provider_health + 单次调用超时」负责；`_is_claude_proxy_dead_this_round`
    仅为兼容旧调用保留，恒返回 False。

    这里锁住两件事：
      * 退役后的 `_is_claude_proxy_dead_this_round` 永远不判死；
      * 真的没有任何可用客户端时，异常文本必须同时说清
        代理 / OpenRouter / 官方直连 三段原因（排查时不用再猜）。
    """

    @classmethod
    def setUpClass(cls):
        try:
            import tenshi_reply
        except Exception as exc:
            raise unittest.SkipTest(f"tenshi_reply 不可用：{exc}")

    def test_preflight_flag_is_retired_and_always_false(self):
        import tenshi_reply

        for name in ("Codeyu Base", "Codeyu Prime", "CoderPlan", "Official", ""):
            self.assertFalse(
                tenshi_reply._is_claude_proxy_dead_this_round(name),
                f"代理预检已退役，不应再判死 {name!r}",
            )

    def test_preflight_helpers_are_gone(self):
        """旧预检入口必须彻底删除（否则会有人以为它还在生效）。"""
        import tenshi_reply

        for attr in ("_ping_claude_proxy_once", "_run_claude_proxy_pings"):
            self.assertFalse(hasattr(tenshi_reply, attr), f"{attr} 应已随预检一起删除")

    def test_no_claude_client_error_lists_three_reason_segments(self):
        import tenshi_reply

        original_key = tenshi_reply.CLAUDE_API_KEY
        original_or_key = tenshi_reply.OPENROUTER_API_KEY
        original_build = tenshi_reply.build_claude_proxy_configs_for_model
        original_path_state = tenshi_reply.provider_health.path_state
        try:

            tenshi_reply.CLAUDE_API_KEY = ""
            tenshi_reply.OPENROUTER_API_KEY = None
            tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: []
            tenshi_reply.provider_health.path_state = lambda *a, **k: None

            with self.assertRaises(tenshi_reply.NoClaudeClientError) as ctx:
                tenshi_reply.call_claude(
                    "claude-opus-5", "sys", [{"role": "user", "content": "hi"}]
                )
            message = str(ctx.exception)
        finally:
            tenshi_reply.CLAUDE_API_KEY = original_key
            tenshi_reply.OPENROUTER_API_KEY = original_or_key
            tenshi_reply.build_claude_proxy_configs_for_model = original_build
            tenshi_reply.provider_health.path_state = original_path_state

        self.assertIn("No Claude clients available", message)

        self.assertIn("代理：", message)
        self.assertIn("OpenRouter：", message)
        self.assertIn("官方直连：", message)
        self.assertIn("官方 key 未配置", message)
        self.assertIn("OPENROUTER_API_KEY 未配置", message)


if __name__ == "__main__":
    unittest.main()
