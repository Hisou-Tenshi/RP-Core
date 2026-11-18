# SPDX-License-Identifier: AGPL-3.0-only






















from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


os.environ.setdefault("OPENROUTER_API_KEY", "dummy-for-tests")
os.environ.setdefault("GEMINI_API_KEY", "dummy-for-tests")
os.environ.setdefault("DEEPSEEK_API_KEY", "dummy-for-tests")


try:
    from config import tunable as tunable_config
except Exception as e:
    tunable_config = None
    CONFIG_IMPORT_ERROR = e
else:
    CONFIG_IMPORT_ERROR = None


try:
    import tenshi_reply
    import tenshi_config
    from llm_routing import (
        build_deepseek_paths,
        build_gemini_paths,
        build_glm_paths,
        build_gpt_paths,
        build_grok_paths,
    )
except Exception as e:
    tenshi_reply = None
    tenshi_config = None
    REPLY_IMPORT_ERROR = e
else:
    REPLY_IMPORT_ERROR = None


class _Sentinel:
    """truthy 的假客户端：用来断言「走的是官方客户端」而不真的构造 SDK 客户端。"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<Sentinel {self.name}>"


@unittest.skipIf(CONFIG_IMPORT_ERROR is not None, f"依赖不可用：{CONFIG_IMPORT_ERROR}")
class OpenRouterFallbackMapTest(unittest.TestCase):
    """集中映射：原生模型 id → OpenRouter slug。"""

    def test_slug_lookup(self):
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("claude-sonnet-5-5"),
            "anthropic/claude-sonnet-5.5",
        )
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("deepseek-flash"),
            "deepseek/deepseek-v4.1-flash-1017",
        )
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("deepseek-v4-pro"),
            "deepseek/deepseek-v4-pro",
        )
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("gemini-3.8-flash"),
            "google/gemini-3.8-flash",
        )
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("gpt-6-luna"),
            "openai/gpt-6-luna",
        )

    def test_tail_alias_is_stripped(self):
        self.assertEqual(
            tunable_config.openrouter_fallback_slug("deepseek-flash#tail"),
            "deepseek/deepseek-v4.1-flash-1017",
        )

    def test_no_slug_for_magmarouter_and_openrouter_models(self):
        self.assertIsNone(tunable_config.openrouter_fallback_slug("glm-5.3-flash-xploded"))
        self.assertIsNone(tunable_config.openrouter_fallback_slug("magma-1-xploded"))
        self.assertIsNone(
            tunable_config.openrouter_fallback_slug(
                "cognitivecomputations/dolphin-mistral-24b-venice-edition"
            )
        )

    def test_dialog_chain_models_all_have_slug_except_excluded(self):
        """主对话全链里，除 MagmaRouter / OpenRouter 自带的模型外，每个模型都有 slug。"""
        magmarouter = set(tunable_config.magmarouter_models())
        for model in tunable_config.dialog_model_chain():
            base = (model or "").split("#", 1)[0].strip()
            if not base or base in magmarouter or "/" in base:
                continue
            self.assertIsNotNone(
                tunable_config.openrouter_fallback_slug(base),
                f"主链模型 {model} 缺少 OpenRouter 兜底 slug",
            )

    def test_env_json_override(self):
        with mock.patch.dict(
            os.environ, {"OPENROUTER_FALLBACK_MAP": '{"claude-sonnet-5-5": "override/claude"}'}
        ):
            resolved = tunable_config._resolve_openrouter_fallback_map()
        self.assertEqual(resolved["claude-sonnet-5-5"], "override/claude")

        self.assertEqual(resolved["deepseek-flash"], "deepseek/deepseek-v4.1-flash-1017")


@unittest.skipIf(REPLY_IMPORT_ERROR is not None, f"依赖不可用：{REPLY_IMPORT_ERROR}")
class OpenRouterPathInsertionTest(unittest.TestCase):
    """新架构核心：OR 由 build_*_paths 按 PROVIDER_PATH_POLICIES 插进通路。"""

    PROXY = {"name": "Relay", "api_key": "r", "base_url": "https://relay.example"}

    def setUp(self):
        self._orig = (
            tenshi_config.OPENROUTER_API_KEY,
            tenshi_config.OPENROUTER_BASE_URL,
        )
        tenshi_config.OPENROUTER_API_KEY = "dummy-for-tests"
        tenshi_config.OPENROUTER_BASE_URL = "https://openrouter.example/api/v1"

    def tearDown(self):
        (
            tenshi_config.OPENROUTER_API_KEY,
            tenshi_config.OPENROUTER_BASE_URL,
        ) = self._orig

    def test_proxy_first_providers_put_openrouter_between_proxy_and_official(self):
        builders = {
            # 2026-11 口径：Gemini 是 proxies → official → OpenRouter；GPT/Grok 仍在官方之前插 OR。
            "gemini": (lambda: build_gemini_paths("k", [self.PROXY]), ["Relay", "Official", "OpenRouter"]),
            "grok": (lambda: build_grok_paths("k", "https://api.x.ai/v1", [self.PROXY]), ["Relay", "OpenRouter", "Official"]),
            "gpt": (lambda: build_gpt_paths("k", "https://api.openai.com/v1", [self.PROXY]), ["Relay", "OpenRouter", "OpenAI"]),
        }
        for provider, (build, expected) in builders.items():
            names = [p["name"] for p in build()]
            self.assertEqual(names, expected, provider)

    def test_official_first_providers_put_openrouter_last(self):
        gemini = [p["name"] for p in build_gemini_paths("k", [self.PROXY])]
        self.assertEqual(gemini, ["Relay", "Official", "OpenRouter"])
        deepseek = [p["name"] for p in build_deepseek_paths("k", "https://api.deepseek.com", [self.PROXY])]
        self.assertEqual(deepseek, ["Official", "Relay", "OpenRouter"])
        glm = [p["name"] for p in build_glm_paths("k", "https://open.bigmodel.cn/api/paas/v4")]
        self.assertEqual(glm, ["Official", "OpenRouter"])

    def test_openrouter_path_is_openai_compatible_with_vendor_prefix(self):
        cases = {
            "gemini": (build_gemini_paths("k", [self.PROXY]), "google/"),
            "grok": (build_grok_paths("k", "https://api.x.ai/v1", [self.PROXY]), "x-ai/"),
            "gpt": (build_gpt_paths("k", "https://api.openai.com/v1", [self.PROXY]), "openai/"),
            "deepseek": (build_deepseek_paths("k", "https://api.deepseek.com", [self.PROXY]), "deepseek/"),
            "glm": (build_glm_paths("k", "https://open.bigmodel.cn/api/paas/v4"), "z-ai/"),
        }
        for provider, (paths, prefix) in cases.items():
            or_path = next(p for p in paths if p["name"] == "OpenRouter")
            self.assertTrue(or_path.get("openrouter"), provider)
            self.assertEqual(or_path.get("protocol"), "openai", provider)
            self.assertEqual(or_path.get("vendor_prefix"), prefix, provider)
            self.assertEqual(or_path.get("api_key"), "dummy-for-tests", provider)
            self.assertEqual(or_path.get("base_url"), "https://openrouter.example/api/v1", provider)
            self.assertFalse(or_path.get("is_official"), provider)

    def test_no_openrouter_path_without_key(self):
        tenshi_config.OPENROUTER_API_KEY = None
        self.assertEqual(
            [p["name"] for p in build_gemini_paths("k", [self.PROXY])],
            ["Relay", "Official"],
        )
        self.assertEqual(
            [p["name"] for p in build_deepseek_paths("k", "https://api.deepseek.com", [self.PROXY])],
            ["Official", "Relay"],
        )
        self.assertEqual(
            [p["name"] for p in build_glm_paths("k", "https://open.bigmodel.cn/api/paas/v4")],
            ["Official"],
        )

    def test_manual_openrouter_proxy_is_not_duplicated(self):
        """旧配置把 OR 塞进 proxy_configs 时，build_*_paths 不能再多插一条。"""
        manual_or = {
            "name": "OpenRouter",
            "api_key": "old-key",
            "base_url": "https://old-openrouter.example/api/v1",
            "protocol": "openai",
            "vendor_prefix": "google/",
        }
        paths = build_gemini_paths("k", [self.PROXY, manual_or])
        self.assertEqual([p["name"] for p in paths], ["Relay", "OpenRouter", "Official"])
        self.assertEqual(paths[1]["api_key"], "old-key")


@unittest.skipIf(REPLY_IMPORT_ERROR is not None, f"依赖不可用：{REPLY_IMPORT_ERROR}")
class TryOpenRouterFallbackRetiredTest(unittest.TestCase):
    """`_try_openrouter_fallback` 保留为兼容 shim，但对已注册 provider 恒返回 None。

    这些 provider 的 OR 已经由 path-first 真正试过；再兜一次等于同一轮多打一次 OR。
    """


    MODEL_IDS = [
        "claude-sonnet-5-5",
        "gemini-3.8-flash",
        "grok-4.6",
        "gpt-6-luna",
        "deepseek-flash",
        "glm-5.3",
    ]

    def setUp(self):
        self._orig_or_key = tenshi_reply.OPENROUTER_API_KEY
        self._orig_call = tenshi_reply.call_openrouter_with_tools_loop
        tenshi_reply.OPENROUTER_API_KEY = "dummy-for-tests"
        tenshi_reply._reset_provider_breakers()
        self._or_call = mock.Mock(side_effect=AssertionError("不应重复调用 OpenRouter"))
        tenshi_reply.call_openrouter_with_tools_loop = self._or_call

    def tearDown(self):
        tenshi_reply.OPENROUTER_API_KEY = self._orig_or_key
        tenshi_reply.call_openrouter_with_tools_loop = self._orig_call
        tenshi_reply._reset_provider_breakers()

    def test_registered_providers_are_short_circuited(self):
        for model_id in self.MODEL_IDS:

            self.assertIsNotNone(
                tenshi_config.openrouter_fallback_slug(model_id), model_id
            )
            self.assertIsNone(
                tenshi_reply._try_openrouter_fallback(
                    model_id=model_id,
                    system_prompt="sys",
                    messages_payload=[{"role": "user", "content": "hi"}],
                    tenshi_note_write_ids=set(),
                ),
                model_id,
            )
        self._or_call.assert_not_called()

    def test_no_slug_is_still_skipped(self):

        self.assertIsNone(
            tenshi_reply._try_openrouter_fallback(
                model_id="glm-5.3-flash-xploded",
                system_prompt="sys",
                messages_payload=[],
                tenshi_note_write_ids=set(),
            )
        )
        self._or_call.assert_not_called()


@unittest.skipIf(REPLY_IMPORT_ERROR is not None, f"依赖不可用：{REPLY_IMPORT_ERROR}")
class CallClaudeAttemptOrderTest(unittest.TestCase):
    """call_claude 内部尝试顺序：代理 → OpenRouter → 官方（官方永远最后）。"""

    def setUp(self):
        self._orig = (
            tenshi_reply.CLAUDE_API_KEY,
            tenshi_reply.anthropic_client,
            tenshi_reply.OPENROUTER_API_KEY,
            tenshi_reply.build_claude_proxy_configs_for_model,
            tenshi_reply._call_claude_impl,
            tenshi_reply.call_openrouter,
            tenshi_reply.anthropic.Anthropic,
            tenshi_reply.provider_health.path_state,
            tenshi_reply.provider_health.filter_paths,
        )
        self._official = _Sentinel("official-client")
        self._proxy = _Sentinel("proxy-client")
        tenshi_reply.CLAUDE_API_KEY = "official-key"
        tenshi_reply.anthropic_client = self._official
        tenshi_reply.OPENROUTER_API_KEY = "dummy-for-tests"
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Codeyu Base", "api_key": "k", "base_url": "https://p1.example"}
        ]

        tenshi_reply.anthropic.Anthropic = lambda *a, **k: self._proxy

        tenshi_reply.provider_health.path_state = lambda *a, **k: None
        tenshi_reply.provider_health.filter_paths = lambda provider, paths, **kw: list(paths or [])
        tenshi_reply._reset_provider_breakers()

    def tearDown(self):
        (
            tenshi_reply.CLAUDE_API_KEY,
            tenshi_reply.anthropic_client,
            tenshi_reply.OPENROUTER_API_KEY,
            tenshi_reply.build_claude_proxy_configs_for_model,
            tenshi_reply._call_claude_impl,
            tenshi_reply.call_openrouter,
            tenshi_reply.anthropic.Anthropic,
            tenshi_reply.provider_health.path_state,
            tenshi_reply.provider_health.filter_paths,
        ) = self._orig
        tenshi_reply._reset_provider_breakers()

    def _record_attempts(self):
        order: list[str] = []

        def fake_impl(client, model_name, *a, **k):
            order.append("Official" if client is self._official else "Proxy")
            raise RuntimeError(f"{order[-1]} failed")

        def fake_openrouter(*a, **k):
            order.append("OpenRouter")
            raise RuntimeError("OpenRouter failed")

        tenshi_reply._call_claude_impl = fake_impl
        tenshi_reply.call_openrouter = fake_openrouter
        return order

    def _call(self):
        with self.assertRaises(RuntimeError):
            tenshi_reply.call_claude(
                "claude-sonnet-5-5", "sys", [{"role": "user", "content": "hi"}]
            )

    def test_proxy_then_openrouter_then_official(self):
        order = self._record_attempts()
        self._call()
        self.assertEqual(order, ["Proxy", "OpenRouter", "Official"], "官方必须排最后")

    def test_proxy1_then_proxy2_then_openrouter_then_official(self):
        """CoderPlan 已恢复使用：排在 OpenRouter 之前。"""
        p1 = _Sentinel("p1")
        p2 = _Sentinel("p2")
        order: list[str] = []
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Codeyu Base", "api_key": "k1", "base_url": "https://p1.example"},
            {"name": "CoderPlan", "api_key": "k2", "base_url": "https://p2.example"},
        ]
        tenshi_reply.anthropic.Anthropic = (
            lambda *a, **k: p1 if k.get("api_key") == "k1" else p2
        )

        def fake_impl(client, model_name, *a, **k):
            order.append({p1: "Codeyu", p2: "CoderPlan", self._official: "Official"}[client])
            raise RuntimeError("fail-for-order")

        def fake_openrouter(*a, **k):
            order.append("OpenRouter")
            raise RuntimeError("OpenRouter failed")

        tenshi_reply._call_claude_impl = fake_impl
        tenshi_reply.call_openrouter = fake_openrouter
        self._call()
        self.assertEqual(
            order, ["Codeyu", "CoderPlan", "OpenRouter", "Official"], "官方必须最后"
        )

    def test_without_openrouter_key_proxy_then_official(self):
        tenshi_reply.OPENROUTER_API_KEY = None
        order = self._record_attempts()
        self._call()
        self.assertEqual(order, ["Proxy", "Official"])

    def test_openrouter_is_skipped_when_dead_this_round(self):
        tenshi_reply._mark_provider_dead_this_round("openrouter", "test")
        order = self._record_attempts()
        self._call()
        self.assertEqual(order, ["Proxy", "Official"])


@unittest.skipIf(REPLY_IMPORT_ERROR is not None, f"依赖不可用：{REPLY_IMPORT_ERROR}")
class DialogChainOpenRouterPositionTest(unittest.TestCase):
    """invoke_dialog_model_chain：OR 已在 provider 的 path 内，链上不再额外插一次。

    2026-10 前：每个模型失败 → 链外再调一次 `_try_openrouter_fallback`。
    现在：`_run_path_first_provider` 走的就是含 OR 的 path 列表，链只负责换模型。
    """

    def setUp(self):
        self._orig = (
            tenshi_reply._build_dialog_model_chain,
            tenshi_reply._run_path_first_provider,
            tenshi_reply._run_claude_model_chain,
            tenshi_reply.call_openrouter_with_tools_loop,
        )
        tenshi_reply._reset_provider_breakers()

    def tearDown(self):
        (
            tenshi_reply._build_dialog_model_chain,
            tenshi_reply._run_path_first_provider,
            tenshi_reply._run_claude_model_chain,
            tenshi_reply.call_openrouter_with_tools_loop,
        ) = self._orig
        tenshi_reply._reset_provider_breakers()

    def test_single_provider_is_run_once(self):
        tenshi_reply._build_dialog_model_chain = lambda tier: ["deepseek-flash"]
        calls: list[tuple[str, list]] = []
        or_call = mock.Mock(side_effect=AssertionError("链上不应再额外调 OpenRouter"))
        tenshi_reply.call_openrouter_with_tools_loop = or_call

        def fake_native(provider_name, model_ids, **kw):
            calls.append((provider_name, list(model_ids)))
            return ("native-reply", "", False)

        tenshi_reply._run_path_first_provider = fake_native
        out = tenshi_reply.invoke_dialog_model_chain(
            grade_tier="medium",
            system_prompt="sys",
            messages_payload=[{"role": "user", "content": "hi"}],
        )
        self.assertEqual(out, ("native-reply", "", False))
        self.assertEqual(calls, [("deepseek", ["deepseek-flash"])])
        or_call.assert_not_called()

    def test_failed_provider_falls_through_to_next_model_not_extra_openrouter(self):
        tenshi_reply._build_dialog_model_chain = lambda tier: ["deepseek-flash", "gpt-6-luna"]
        order: list[str] = []
        or_call = mock.Mock(side_effect=AssertionError("链上不应再额外调 OpenRouter"))
        tenshi_reply.call_openrouter_with_tools_loop = or_call

        def fake_native(provider_name, model_ids, **kw):
            order.append(f"native:{provider_name}:{model_ids[0]}")
            if provider_name == "deepseek":
                raise RuntimeError("deepseek native down")
            return ("gpt-native-reply", "", False)

        tenshi_reply._run_path_first_provider = fake_native
        out = tenshi_reply.invoke_dialog_model_chain(
            grade_tier="medium",
            system_prompt="sys",
            messages_payload=[{"role": "user", "content": "hi"}],
        )
        self.assertEqual(out, ("gpt-native-reply", "", False))


        self.assertEqual(
            order,
            ["native:deepseek:deepseek-flash", "native:gpt:gpt-6-luna"],
        )
        or_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
