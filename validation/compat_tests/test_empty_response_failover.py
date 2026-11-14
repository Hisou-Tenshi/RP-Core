# SPDX-License-Identifier: AGPL-3.0-only

















from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")


GATEWAY_PLACEHOLDER = (
    "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
    "或模型对当前输入直接结束。请重试或简化输入后再试。"
)

IMPORT_ERROR: Exception | None = None
try:
    import provider_health
    import tenshi_reply
    from llm_routing import EmptyModelResponseError, require_nonempty_model_text
    from llm_tool_schema import should_skip_provider_on_error
except Exception as e:
    IMPORT_ERROR = e


def _write_health(path: pathlib.Path, providers: dict) -> None:
    stamp = dt.datetime.now(dt.timezone.utc)
    path.write_text(
        json.dumps(
            {
                "schema": 4,
                "generated_at_utc": stamp.replace(microsecond=0).isoformat(),
                "generated_at_bj": (stamp + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
                "providers": providers,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class EmptyResponseHelperTest(unittest.TestCase):
    def test_helper_raises_with_canonical_message(self):
        with self.assertRaises(EmptyModelResponseError) as ctx:
            require_nonempty_model_text("   ", "claude:claude-sonnet-5-5 via Proxy 1 Prime")
        self.assertIn(
            "returned a completed response with no content",
            str(ctx.exception),
            "空响应异常必须带上与 DSH 侧一致的可检索措辞",
        )

    def test_helper_passes_nonempty_through(self):
        self.assertEqual(require_nonempty_model_text("  嘿  ", "x"), "  嘿  ")

    def test_empty_response_is_not_a_provider_breaker(self):
        """空响应不是 provider 级确定性错误：不许因此熔断整厂。"""
        skip_ok, _reason = should_skip_provider_on_error(
            EmptyModelResponseError("claude:x via Proxy 1 Prime")
        )
        self.assertFalse(skip_ok)


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class CallClaudeEmptyResponseFailoverTest(unittest.TestCase):
    """call_claude：Base 空响应 → 继续 Prime；全空 → 带完整失败清单抛错。"""

    def setUp(self):
        self._orig_build = tenshi_reply.build_claude_proxy_configs_for_model
        self._orig_impl = tenshi_reply._call_claude_impl
        self._orig_or_key = tenshi_reply.OPENROUTER_API_KEY
        self._orig_key = tenshi_reply.CLAUDE_API_KEY
        self._orig_client = tenshi_reply.anthropic_client
        tenshi_reply.OPENROUTER_API_KEY = None
        tenshi_reply.CLAUDE_API_KEY = None
        tenshi_reply.anthropic_client = None
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k1", "base_url": "https://p1-base.example"},
            {"name": "Proxy 1 Prime", "api_key": "k2", "base_url": "https://p1-prime.example"},
        ]

        self._orig_filter = provider_health.filter_paths
        self._orig_path_state = provider_health.path_state
        self._orig_model_state = provider_health.model_path_state
        provider_health.filter_paths = lambda category, paths, **k: list(paths)
        provider_health.path_state = lambda *a, **k: None
        provider_health.model_path_state = lambda *a, **k: None

    def tearDown(self):
        tenshi_reply.build_claude_proxy_configs_for_model = self._orig_build
        tenshi_reply._call_claude_impl = self._orig_impl
        tenshi_reply.OPENROUTER_API_KEY = self._orig_or_key
        tenshi_reply.CLAUDE_API_KEY = self._orig_key
        tenshi_reply.anthropic_client = self._orig_client
        provider_health.filter_paths = self._orig_filter
        provider_health.path_state = self._orig_path_state
        provider_health.model_path_state = self._orig_model_state

    def test_empty_response_falls_through_to_next_path(self):
        """Base 返回空 → 不 return，继续 Prime；Prime 有内容 → 用 Prime。"""
        seen_paths: list[str] = []

        def fake_impl(client, model_name, *a, **k):
            url = str(getattr(client, "base_url", ""))
            seen_paths.append(url)
            if "p1-base" in url:
                return "", "", False
            return "prime ok", "", False

        tenshi_reply._call_claude_impl = fake_impl
        out = tenshi_reply.call_claude("claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(out[0], "prime ok")
        self.assertEqual(len(seen_paths), 2, "Base 空响应后必须继续尝试 Prime")

    def test_placeholder_falls_through_to_next_path(self):
        """2026-11-12 事故：Base 返回网关中文占位（非空）→ 继续 Prime，不许 return。"""
        seen_paths: list[str] = []

        def fake_impl(client, model_name, *a, **k):
            url = str(getattr(client, "base_url", ""))
            seen_paths.append(url)
            if "p1-base" in url:
                return GATEWAY_PLACEHOLDER, "", False
            return "prime ok", "", False

        tenshi_reply._call_claude_impl = fake_impl
        out = tenshi_reply.call_claude("claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(out[0], "prime ok")
        self.assertEqual(len(seen_paths), 2, "Base 返回占位后必须继续尝试 Prime")

    def test_all_placeholder_responses_raise(self):
        def fake_impl(client, model_name, *a, **k):
            return GATEWAY_PLACEHOLDER, "", False

        tenshi_reply._call_claude_impl = fake_impl
        with self.assertRaises(EmptyModelResponseError):
            tenshi_reply.call_claude("claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}])

    def test_all_empty_responses_raise_with_full_failure_list(self):
        def fake_impl(client, model_name, *a, **k):
            return "", "", False

        tenshi_reply._call_claude_impl = fake_impl
        with self.assertRaises(EmptyModelResponseError) as ctx:
            tenshi_reply.call_claude("claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}])
        self.assertIn("no content", str(ctx.exception))

    def test_model_level_ban_skips_path_before_calling(self):
        """探活判定「该模型在该通路不可用」→ 该通路连试都不试。"""

        provider_health.filter_paths = self._orig_filter
        provider_health.path_state = self._orig_path_state
        provider_health.model_path_state = self._orig_model_state
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        health_path = pathlib.Path(tmp.name) / "provider_health.json"
        _write_health(
            health_path,
            {
                "claude": {
                    "enabled": True,
                    "paths": {
                        "Proxy 1 Base": {
                            "ok": True,
                            "verdict": "ok",
                            "models": {
                                "claude-sonnet-5-5": {"ok": False, "verdict": "model_not_found", "severity": "red"},
                            },
                        },
                        "Proxy 1 Prime": {"ok": True, "verdict": "ok"},
                    },
                },
            },
        )
        self._orig_health_path = provider_health.HEALTH_PATH
        provider_health.HEALTH_PATH = health_path
        provider_health.reset_cache()
        self.addCleanup(setattr, provider_health, "HEALTH_PATH", self._orig_health_path)
        self.addCleanup(provider_health.reset_cache)


        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k1", "base_url": "https://p1-base.example"},
            {"name": "Proxy 1 Prime", "api_key": "k2", "base_url": "https://p1-prime.example"},
        ]
        seen_urls: list[str] = []

        def fake_impl(client, model_name, *a, **k):
            url = str(getattr(client, "base_url", ""))
            seen_urls.append(url)
            return f"ok via {url}", "", False

        tenshi_reply._call_claude_impl = fake_impl
        out = tenshi_reply.call_claude("claude-sonnet-5-5", "sys", [{"role": "user", "content": "hi"}])
        self.assertNotIn("p1-base", "".join(seen_urls), "模型级 ban 的通路必须整个跳过")
        self.assertIn("p1-prime", out[0])


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class PathFirstEmptyResponseFailoverTest(unittest.TestCase):
    """非 Claude 主链（gemini/gpt/glm/deepseek/grok 槽）：空响应同样按失败降级。"""

    def setUp(self):
        self._orig_paths = tenshi_reply.build_gemini_paths
        self._orig_call = tenshi_reply.call_gemini
        self._orig_filter = provider_health.filter_paths
        self._orig_path_state = provider_health.path_state
        self._orig_model_state = provider_health.model_path_state
        provider_health.filter_paths = lambda category, paths, **k: list(paths)
        provider_health.path_state = lambda *a, **k: None
        provider_health.model_path_state = lambda *a, **k: None

    def tearDown(self):
        tenshi_reply.build_gemini_paths = self._orig_paths
        tenshi_reply.call_gemini = self._orig_call
        provider_health.filter_paths = self._orig_filter
        provider_health.path_state = self._orig_path_state
        provider_health.model_path_state = self._orig_model_state

    def _run(self, fake_call):
        tenshi_reply.build_gemini_paths = lambda *a, **k: [
            {"name": "Gemini Proxy", "api_key": "k", "base_url": "https://gp.example"},
            {"name": "Official", "api_key": "k2", "base_url": None, "is_official": True},
        ]
        tenshi_reply.call_gemini = fake_call
        return tenshi_reply._run_path_first_provider(
            provider_name="gemini",
            model_ids=["gemini-3.8-flash"],
            system_prompt="sys",
            messages_payload=[{"role": "user", "content": "hi"}],
            streamer=None,
            filtered_claude_tools=[],
            tenshi_note_write_ids=set(),
        )

    def test_empty_response_moves_to_next_path(self):
        calls: list[str] = []

        def fake_call(model_name, *a, **k):
            path = (k.get("llm_path") or {}).get("name") if isinstance(k, dict) else None
            calls.append(str(path))
            if len(calls) == 1:
                return "", "", False
            return "official ok", "", False

        out = self._run(fake_call)
        self.assertEqual(out[0], "official ok")
        self.assertEqual(calls, ["Gemini Proxy", "Official"])

    def test_all_empty_responses_raise(self):
        tenshi_reply.build_gemini_paths = lambda *a, **k: [
            {"name": "Gemini Proxy", "api_key": "k", "base_url": "https://gp.example"},
        ]
        tenshi_reply.call_gemini = lambda *a, **k: ("", "", False)


        with self.assertRaises(RuntimeError) as ctx:
            tenshi_reply._run_path_first_provider(
                provider_name="gemini",
                model_ids=["gemini-3.8-flash"],
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                streamer=None,
                filtered_claude_tools=[],
                tenshi_note_write_ids=set(),
            )
        self.assertIn("no content", str(ctx.exception), "失败清单里必须能看到空响应的原始原因")

    def test_placeholder_moves_to_next_path(self):
        """2026-11-12 事故：非 Claude 通路返回网关中文占位，同样按失败换下一条通路。"""
        calls: list[str] = []

        def fake_call(model_name, *a, **k):
            path = (k.get("llm_path") or {}).get("name") if isinstance(k, dict) else None
            calls.append(str(path))
            if len(calls) == 1:
                return GATEWAY_PLACEHOLDER, "", False
            return "official ok", "", False

        out = self._run(fake_call)
        self.assertEqual(out[0], "official ok")
        self.assertEqual(calls, ["Gemini Proxy", "Official"])

    def test_model_level_ban_skips_combo(self):
        """「通路 × 模型」探活红 → 该组合抛 RuntimeError，run_path_first 继续下一个。"""
        provider_health.filter_paths = self._orig_filter
        provider_health.path_state = self._orig_path_state
        provider_health.model_path_state = lambda provider, path_name, model_id: (
            False if (provider == "gemini" and path_name == "Gemini Proxy") else None
        )
        calls: list[str] = []

        def fake_call(model_name, *a, **k):
            path = (k.get("llm_path") or {}).get("name") if isinstance(k, dict) else None
            calls.append(str(path))
            return "ok", "", False

        self._run(fake_call)
        self.assertEqual(calls, ["Official"], "被模型级 ban 的通路不应发起调用")


if __name__ == "__main__":
    unittest.main()
