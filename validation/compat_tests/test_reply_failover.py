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

IMPORT_ERROR: Exception | None = None
try:
    import tenshi_config
    import tenshi_reply
    from llm_routing import NoClaudeClientError
except Exception as e:
    IMPORT_ERROR = e


class _Sentinel:
    """truthy 的假客户端：用来断言「走的是官方客户端」而不真的构造 SDK 客户端。"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<Sentinel {self.name}>"


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class CallClaudeNoClientTest(unittest.TestCase):
    """无 client 必须抛错，绝不能把错误字符串当回复返回。"""

    def setUp(self):
        self._orig = tenshi_reply.build_claude_proxy_configs_for_model



        self._orig_or_key = tenshi_reply.OPENROUTER_API_KEY
        tenshi_reply.OPENROUTER_API_KEY = None
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: []
        self._orig_key = tenshi_reply.CLAUDE_API_KEY
        self._orig_client = tenshi_reply.anthropic_client
        tenshi_reply.CLAUDE_API_KEY = None
        tenshi_reply.anthropic_client = None

    def tearDown(self):
        tenshi_reply.build_claude_proxy_configs_for_model = self._orig
        tenshi_reply.CLAUDE_API_KEY = self._orig_key
        tenshi_reply.anthropic_client = self._orig_client
        tenshi_reply.OPENROUTER_API_KEY = self._orig_or_key

    def test_raises_instead_of_returning_error_string(self):
        with self.assertRaises(NoClaudeClientError):
            tenshi_reply.call_claude("claude-opus-5", "sys", [{"role": "user", "content": "hi"}])

    def test_error_string_would_have_been_a_placeholder(self):
        """对照：旧行为返回的那句话必须被判定为错误占位。"""
        from llm_tool_schema import is_error_placeholder_response

        self.assertTrue(is_error_placeholder_response("Error: No API keys configured."))


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class OfficialSurvivesProxyPreflightTest(unittest.TestCase):
    """代理不可用不得牵连官方直连（2026-09-09 事故的直接教训）。

    2026-10 起每轮对话前的代理预检已整体退役，判活改由每日 provider_health +
    调用超时兜底；但「代理全灭时官方仍然要被尝试」的语义必须保留。这里把
    OpenRouter 显式关掉，避免测试真的打到 OR（那样就测不到官方兜底了）。
    """

    def setUp(self):
        self._orig_build = tenshi_reply.build_claude_proxy_configs_for_model
        self._orig_dead = tenshi_reply._is_claude_proxy_dead_this_round
        self._orig_key = tenshi_reply.CLAUDE_API_KEY
        self._orig_client = tenshi_reply.anthropic_client


        self._orig_or_key = tenshi_reply.OPENROUTER_API_KEY
        tenshi_reply.OPENROUTER_API_KEY = None


        import provider_health as _ph

        self._ph = _ph
        self._orig_filter_paths = _ph.filter_paths
        self._orig_path_state = _ph.path_state
        self._orig_model_state = getattr(_ph, "model_path_state", None)
        _ph.filter_paths = lambda category, paths, **k: list(paths)
        _ph.path_state = lambda *a, **k: None
        _ph.model_path_state = lambda *a, **k: None

    def tearDown(self):
        tenshi_reply.build_claude_proxy_configs_for_model = self._orig_build
        tenshi_reply._is_claude_proxy_dead_this_round = self._orig_dead
        tenshi_reply.CLAUDE_API_KEY = self._orig_key
        tenshi_reply.anthropic_client = self._orig_client
        tenshi_reply.OPENROUTER_API_KEY = self._orig_or_key
        self._ph.filter_paths = self._orig_filter_paths
        self._ph.path_state = self._orig_path_state
        if self._orig_model_state is not None:
            self._ph.model_path_state = self._orig_model_state

    def test_official_is_tried_when_all_proxies_are_dead(self):

        tenshi_reply._is_claude_proxy_dead_this_round = lambda name: name == "Proxy 1 Base"
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k", "base_url": "https://p1.example"},
            {"name": "Proxy 2", "api_key": "k2", "base_url": "https://p2.example"},
        ]
        tenshi_reply.CLAUDE_API_KEY = "official-key"

        sentinel = _Sentinel("official-client")
        tenshi_reply.anthropic_client = sentinel

        captured = {}

        def fake_impl(client, model_name, *a, **k):


            captured["clients"] = client
            raise RuntimeError("recorded")

        orig_impl = tenshi_reply._call_claude_impl
        tenshi_reply._call_claude_impl = fake_impl
        try:
            with self.assertRaises(RuntimeError):
                tenshi_reply.call_claude(
                    "claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}]
                )
        finally:
            tenshi_reply._call_claude_impl = orig_impl

        self.assertIs(
            captured.get("clients"),
            sentinel,
            "代理全灭时，必须仍然尝试官方直连（而不是抛 NoClaudeClientError）",
        )

    def test_proxy_still_skipped_but_official_reached(self):
        """代理该死还是死；只是官方不再被牵连。"""
        order = []
        tenshi_reply._is_claude_proxy_dead_this_round = lambda name: True
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k", "base_url": "https://p1.example"},
        ]
        tenshi_reply.CLAUDE_API_KEY = "official-key"
        sentinel = _Sentinel("official-client")
        tenshi_reply.anthropic_client = sentinel

        orig_impl = tenshi_reply._call_claude_impl
        tenshi_reply._call_claude_impl = lambda client, model_name, *a, **k: (
            order.append(client) or ("ok", "", False)
        )
        try:
            tenshi_reply.call_claude(
                "claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}]
            )
        finally:
            tenshi_reply._call_claude_impl = orig_impl

        self.assertEqual(order, [sentinel], "只应尝试官方，代理仍被跳过")

    def test_non_sonnet_still_proxy_only(self):
        """Opus/Fable 依旧只用代理（官方直连的成本约束不变）。"""
        tenshi_reply._is_claude_proxy_dead_this_round = lambda name: True
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k", "base_url": "https://p1.example"},
        ]
        tenshi_reply.CLAUDE_API_KEY = "official-key"
        tenshi_reply.anthropic_client = object()

        with self.assertRaises(NoClaudeClientError):
            tenshi_reply.call_claude(
                "claude-opus-5", "sys", [{"role": "user", "content": "hi"}]
            )

    def test_official_is_last_in_order_when_proxies_alive(self):
        """代理健康时，官方仍然排在最后（成本顺序不变）。"""
        seen = []
        tenshi_reply._is_claude_proxy_dead_this_round = lambda name: False
        tenshi_reply.build_claude_proxy_configs_for_model = lambda model_id: [
            {"name": "Proxy 1 Base", "api_key": "k", "base_url": "https://p1.example"},
        ]
        tenshi_reply.CLAUDE_API_KEY = "official-key"
        tenshi_reply.anthropic_client = object()

        class _Boom:
            """代理侧抛错，验证会继续走到官方。"""

        def fake_impl(client, model_name, *a, **k):
            seen.append(type(client).__name__)
            if len(seen) == 1:
                raise RuntimeError("proxy failed")
            return "official ok", "", False

        orig_impl = tenshi_reply._call_claude_impl
        tenshi_reply._call_claude_impl = fake_impl
        try:
            out = tenshi_reply.call_claude(
                "claude-sonnet-5", "sys", [{"role": "user", "content": "hi"}]
            )
        finally:
            tenshi_reply._call_claude_impl = orig_impl

        self.assertEqual(out[0], "official ok")
        self.assertEqual(len(seen), 2, "先试代理、失败后落到官方")


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class LowTierHasNonClaudeResortTest(unittest.TestCase):
    """2) low 档（casual 对话最常见的档位）必须有 Claude/Gemini 之外的兜底。"""

    def test_low_tier_contains_deepseek_tail(self):
        chain = tenshi_reply._build_dialog_model_chain("low")
        ds = [m for m in chain if m.startswith("deepseek-")]
        self.assertTrue(
            ds,
            f"low 档降级链缺少 DeepSeek 兜底，Gemini 一挂就无路可走：{chain}",
        )

    def test_tail_entry_is_last_and_provider_detected(self):
        chain = tenshi_reply._build_dialog_model_chain("low")
        self.assertEqual(tenshi_reply._provider_from_model_id(chain[-1]), "deepseek")

    def test_all_tiers_keep_original_head(self):
        """链尾追加兜底不得改变各档位的头部语义。"""
        full = tenshi_config.dialog_model_chain()
        self.assertEqual(tenshi_reply._build_dialog_model_chain("max"), full)
        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("max")[0],
            tenshi_config.CLAUDE_PRO_MODELS[0],
        )

        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("max")[1],
            tenshi_config.GEMINI_PRO_MODELS[0],
        )

        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("medium"),
            tenshi_config.dialog_chain_from_tier("medium"),
        )
        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("medium")[0],
            tenshi_config.CLAUDE_FLASH_MODELS[0],
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class DialogTierStructureTest(unittest.TestCase):
    """5) 四档各自一个列表名，链由 DIALOG_TIER_* 拼接（改档位不用再数下标）。"""

    def test_tier_order_and_lists(self):
        self.assertEqual(tenshi_config.DIALOG_TIER_ORDER, ("max", "high", "medium", "low"))
        self.assertEqual(
            tenshi_config.dialog_tier_models("max"), [tenshi_config.CLAUDE_PRO_MODELS[0]]
        )

        self.assertEqual(
            tenshi_config.dialog_tier_models("high"), [tenshi_config.GEMINI_PRO_MODELS[0]]
        )
        self.assertEqual(tenshi_config.dialog_tier_models("high"), ["gemini-4-argon"])

        self.assertEqual(
            tenshi_config.dialog_tier_models("medium"),
            [tenshi_config.CLAUDE_FLASH_MODELS[0]],
        )
        self.assertEqual(tenshi_config.dialog_tier_models("low"), [])
        self.assertEqual(tenshi_config.DIALOG_TIER_LOW_MODELS, [])

    def test_unknown_tier_falls_back_to_medium(self):
        self.assertEqual(tenshi_config.normalize_dialog_tier("nonsense"), "medium")
        self.assertEqual(tenshi_config.normalize_dialog_tier(None), "medium")
        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("nonsense"),
            tenshi_config.dialog_chain_from_tier("medium"),
        )

    def test_chain_is_tier_heads_then_fallback_then_deepseek_tail(self):
        flat = tenshi_config.dialog_model_chain()
        heads = tenshi_config.dedupe_models(
            *[tenshi_config.DIALOG_TIER_MODELS[t] for t in tenshi_config.DIALOG_TIER_ORDER]
        )
        self.assertEqual(flat[: len(heads)], heads)
        for m in tenshi_config.DIALOG_FALLBACK_MODELS:
            self.assertIn(m, flat)
        self.assertTrue(flat[-1].startswith("deepseek-"), flat[-1])
        self.assertEqual(flat, tenshi_config.dialog_chain_from_tier("max"))

    def test_higher_tier_models_do_not_leak_into_lower_tier_fallback(self):
        """顶配 Claude 只出现在 max 档：低档兜底不该先撞上最贵的模型。"""
        self.assertNotIn(
            tenshi_config.CLAUDE_PRO_MODELS[0], tenshi_config.DIALOG_FALLBACK_MODELS
        )
        for tier in ("high", "medium", "low"):
            self.assertNotIn(
                tenshi_config.CLAUDE_PRO_MODELS[0],
                tenshi_config.dialog_chain_from_tier(tier),
                tier,
            )

    def test_low_chain_is_pure_fallback_when_nothing_is_picked(self):
        """low 档不做静态兜底：没有随机链首时，low 链就是容灾组本身（+ 链尾 DeepSeek）。"""
        chain = tenshi_config.dialog_chain_from_tier("low")
        self.assertEqual(chain[: len(tenshi_config.DIALOG_FALLBACK_MODELS)], tenshi_config.DIALOG_FALLBACK_MODELS)
        self.assertEqual(len(chain), len(tenshi_config.DIALOG_FALLBACK_MODELS) + 1)
        self.assertTrue(chain[-1].startswith("deepseek-"), chain[-1])

    def test_no_tier_owns_gemini_flash_anymore(self):
        """low 空出来后 gemini-3.8-flash 不再被任何档占用，回到容灾组的 Gemini Flash 段。"""
        self.assertNotIn(
            tenshi_config.GEMINI_FLASH_MODELS[0], tenshi_config.DIALOG_TIER_HEAD_MODELS
        )
        self.assertIn(
            tenshi_config.GEMINI_FLASH_MODELS[0], tenshi_config.DIALOG_FALLBACK_MODELS
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class FallbackLiteDemotionTest(unittest.TestCase):
    """Lite 不进容灾组第一轮：给旧高级模型让位（2026-10 调整，2026-11 补充）。"""

    def test_first_round_has_no_lite(self):
        from config import models as models_config

        fallback = tenshi_config.DIALOG_FALLBACK_MODELS
        self.assertGreaterEqual(len(fallback), 14, f"容灾组异常：{fallback}")


        for model_id in fallback[:6]:
            spec = models_config.MODEL_REGISTRY.get(model_id)
            self.assertIsNotNone(spec, model_id)
            self.assertNotEqual(spec.tier, "lite", f"Lite 不应进第一轮：{model_id}")

        self.assertEqual(
            models_config.MODEL_REGISTRY[fallback[6]].tier, "lite",
            f"第 7 名应是第二轮的第一个 Lite：{fallback[6]}",
        )

    def test_provider_with_only_lite_does_not_take_first_round_slot(self):
        """GPT 只有 gpt-6-luna（Lite）：第一轮不占位，不能为凑满 7 个硬塞进去。"""
        fallback = tenshi_config.DIALOG_FALLBACK_MODELS
        self.assertNotIn("gpt-6-luna", fallback[:6])
        self.assertIn("gpt-6-luna", fallback)
        self.assertGreater(
            fallback.index("gpt-6-luna"), 6, "GPT 的 Lite 不该挤进第一轮"
        )

    def test_claude_contribution_prefers_old_pro_over_lite(self):
        """Claude 剔除档位链首后，第一个兜底应是 opus-5（旧 Pro），而不是 haiku-5-5（Lite）。"""
        fallback = tenshi_config.DIALOG_FALLBACK_MODELS
        self.assertEqual(fallback[0], "claude-opus-5")
        self.assertIn("claude-haiku-5-5", fallback)
        self.assertEqual(fallback.index("claude-haiku-5-5"), 6, "Lite 从第二轮(第 7 名)开始")


class LowTierFlashRandomPoolTest(unittest.TestCase):
    """4) 最低档链首 = Claude / Gemini 两家 Flash[0] 时间 seed 随机二选一；后续降级链一条都不能少。"""

    def test_pool_is_two_vendor_flash_primaries(self):


        self.assertEqual(
            tenshi_config.low_tier_flash_models(),
            [
                tenshi_config.CLAUDE_FLASH_MODELS[0],
                tenshi_config.GEMINI_FLASH_MODELS[0],
            ],
        )
        self.assertEqual(len(tenshi_config.low_tier_flash_models()), 2)

        self.assertNotIn(
            tenshi_config.CLAUDE_PRO_MODELS[0], tenshi_config.low_tier_flash_models()
        )
        self.assertNotIn(
            tenshi_config.GEMINI_PRO_MODELS[0], tenshi_config.low_tier_flash_models()
        )

    def test_random_pick_covers_pool_with_fixed_seeds(self):
        seen = {tenshi_config.low_tier_random_primary(seed=i) for i in range(500)}
        self.assertEqual(seen, set(tenshi_config.low_tier_flash_models()))

    def test_non_low_tiers_are_not_randomized(self):
        for tier in ("max", "high", "medium"):
            self.assertEqual(
                tenshi_reply._build_dialog_model_chain(tier),
                tenshi_config.dialog_chain_from_tier(tier),
                tier,
            )

    def test_each_pool_member_can_be_injected_without_losing_fallbacks(self):
        base = tenshi_config.dialog_chain_from_tier("low")
        original = tenshi_config.low_tier_random_primary
        try:
            for model_id in tenshi_config.low_tier_flash_models():
                tenshi_config.low_tier_random_primary = lambda m=model_id: m
                chain = tenshi_reply._build_dialog_model_chain("low")
                self.assertEqual(chain[0], model_id)
                for kept in base:
                    self.assertIn(kept, chain, f"{model_id} 注入后丢掉了兜底 {kept}")
                self.assertEqual(tenshi_reply._provider_from_model_id(chain[-1]), "deepseek")
        finally:
            tenshi_config.low_tier_random_primary = original

    def test_low_tier_runner_up_is_always_first_of_fallback(self):
        """low 无静态档位：抽到谁，第二个都是容灾组的第一个。"""
        first_fallback = tenshi_config.DIALOG_FALLBACK_MODELS[0]
        original = tenshi_config.low_tier_random_primary
        try:
            for model_id in tenshi_config.low_tier_flash_models():
                tenshi_config.low_tier_random_primary = lambda m=model_id: m
                chain = tenshi_reply._build_dialog_model_chain("low")
                self.assertEqual(chain[0], model_id)
                self.assertEqual(chain[1], first_fallback, chain[:3])
        finally:
            tenshi_config.low_tier_random_primary = original

    def test_low_tier_head_is_always_from_pool(self):
        pool = set(tenshi_config.low_tier_flash_models())
        for _ in range(50):
            chain = tenshi_reply._build_dialog_model_chain("low")
            self.assertTrue(chain)
            self.assertIn(chain[0], pool, chain)

    def test_gpt_candidate_routes_to_gpt_provider(self):
        """抽到 GPT-Flash 时必须走 gpt 通路，而不是被 _provider_from_model_id 误判成 claude。"""
        self.assertEqual(tenshi_reply._provider_from_model_id("gpt-5.4"), "gpt")

        orig_paths = tenshi_reply.build_gpt_paths
        orig_loop = tenshi_reply.call_gpt_with_tools_loop
        seen: list[str] = []
        try:
            tenshi_reply.build_gpt_paths = lambda *a, **k: [
                {"name": "OpenAI", "api_key": "k", "base_url": "https://gpt.example"}
            ]

            def _fake_loop(model_name, *a, **k):
                seen.append(model_name)
                return "ok", "", False

            tenshi_reply.call_gpt_with_tools_loop = _fake_loop
            result = tenshi_reply._run_path_first_provider(
                provider_name="gpt",
                model_ids=["gpt-5.4"],
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                streamer=None,
                filtered_claude_tools=[],
                tenshi_note_write_ids=set(),
            )
        finally:
            tenshi_reply.build_gpt_paths = orig_paths
            tenshi_reply.call_gpt_with_tools_loop = orig_loop

        self.assertEqual(seen, ["gpt-5.4"])
        self.assertEqual(result[0], "ok")

    def test_glm_candidate_routes_to_glm_provider(self):
        """抽到 GLM 时必须走 glm 通路，而不是被 _provider_from_model_id 误判成 claude。"""
        for model_id in tenshi_config.glm_models():
            self.assertEqual(tenshi_reply._provider_from_model_id(model_id), "glm", model_id)

        self.assertEqual(tenshi_reply._provider_from_model_id("glm-5.3#tail"), "glm")

        orig_paths = tenshi_reply.build_glm_paths
        orig_loop = tenshi_reply.call_glm_with_tools_loop
        seen: list[str] = []
        try:
            tenshi_reply.build_glm_paths = lambda *a, **k: [
                {
                    "name": "Official",
                    "api_key": "k",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                }
            ]

            def _fake_loop(model_name, *a, **k):
                seen.append(model_name)
                return "ok", "", False

            tenshi_reply.call_glm_with_tools_loop = _fake_loop
            result = tenshi_reply._run_path_first_provider(
                provider_name="glm",
                model_ids=["glm-5.3"],
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                streamer=None,
                filtered_claude_tools=[],
                tenshi_note_write_ids=set(),
            )
        finally:
            tenshi_reply.build_glm_paths = orig_paths
            tenshi_reply.call_glm_with_tools_loop = orig_loop

        self.assertEqual(seen, ["glm-5.3"])
        self.assertEqual(result[0], "ok")


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class GlmProviderTest(unittest.TestCase):
    """6) GLM（智谱 / Z.ai）接入口径：high 档、low 池、容灾组，且走官方 SDK。"""

    def test_glm_is_fallback_group_not_tier_head(self):
        """2026-10-18 换档后：GLM 不再占 high 档头，glm-5.3 退回容灾组。"""
        self.assertEqual(
            tenshi_config.DIALOG_TIER_HIGH_MODELS, [tenshi_config.GEMINI_PRO_MODELS[0]]
        )
        self.assertEqual(
            tenshi_reply._build_dialog_model_chain("high")[0],
            tenshi_config.GEMINI_PRO_MODELS[0],
        )
        self.assertIn("glm-5.3", tenshi_config.DIALOG_FALLBACK_MODELS)

    def test_fallback_group_carries_the_glm_resort(self):
        """glm-5.3 与 glm-5.3-flash 都必须留在容灾组里。

        否则一旦高档模型全部失败，GLM 家在链上就彻底没有候选了。
        """
        for model_id in ("glm-5.3", "glm-5.3-flash"):
            self.assertIn(model_id, tenshi_config.DIALOG_FALLBACK_MODELS, model_id)

        full = tenshi_config.dialog_model_chain()
        for model_id in ("glm-5.3", "glm-5.3-flash"):
            self.assertIn(model_id, full, model_id)

    def test_tier_heads_are_excluded_from_fallback(self):
        """档位头的模型不进容灾组（否则低档链会先撞上最贵的模型）。"""
        for model_id in tenshi_config.DIALOG_TIER_HEAD_MODELS:
            self.assertNotIn(model_id, tenshi_config.DIALOG_FALLBACK_MODELS, model_id)

        self.assertIn("glm-5.3-flash", tenshi_config.dialog_chain_from_tier("low"))

    def test_glm_client_is_the_official_zai_client(self):
        """GLM 必须走官方 zai-sdk：构造出来的客户端是 `zai` 的 ZhipuAiClient，
        不是 openai.OpenAI（本项目其它 provider 的 OpenAI 兼容层）。

        顺带锁住两条与其它 provider 一致的客户端策略：关闭 SDK 内置重试、
        并按 SDK 自己的 http 模块构造（而非裸秒数）timeout。
        """
        import glm_modules
        import openai
        import zai
        from sdk_compat import sdk_http_module

        client = glm_modules.build_glm_client(
            "probe-key", "https://example.invalid/api/paas/v4"
        )
        try:
            self.assertIsInstance(client, glm_modules._zhipu_client_class())
            self.assertNotIsInstance(client, openai.OpenAI)
            self.assertEqual(
                client.max_retries,
                int(getattr(tenshi_config, "LLM_CLIENT_MAX_RETRIES", 0) or 0),
            )








            self.assertEqual(
                sdk_http_module(zai),
                zai._client.httpx,
                "zai-sdk 的 http 模块应从 zai._client 解析（0.2.3 没有 _base_client）",
            )
            self.assertIsInstance(client.timeout, sdk_http_module(zai).Timeout)
            self.assertEqual(
                client.timeout.connect,
                float(tenshi_config.LLM_CLIENT_CONNECT_TIMEOUT_SEC),
            )
            self.assertEqual(
                client.timeout.read, float(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC)
            )


            import httpx2

            self.assertIsNot(sdk_http_module(zai), httpx2)
        finally:
            glm_modules.reset_client_cache()

    def test_glm_module_does_not_use_the_openai_compat_client_builder(self):
        """源码层面：glm_modules 不得调用 OpenAI 兼容客户端构造器。"""
        import ast
        import pathlib

        src = (
            pathlib.Path(tenshi_config.PROJECT_ROOT, "src", "glm_modules.py").read_text(
                encoding="utf-8"
            )
        )
        calls: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name:
                    calls.add(name)
        self.assertNotIn("build_openai_compat_client", calls)
        self.assertNotIn("OpenAI", calls)

        import glm_modules

        self.assertEqual(glm_modules.GLM_THINKING, {"type": "enabled"})

    def test_glm_paths_are_official_then_openrouter(self):
        """GLM 通路 = 官方 → OpenRouter（2026-10 口径），不再只有官方一条。

        本用例显式控制 OpenRouter key，避免受本机 .env 影响。
        """
        orig_or_key = tenshi_config.OPENROUTER_API_KEY
        try:
            tenshi_config.OPENROUTER_API_KEY = "dummy-for-tests"
            paths = tenshi_reply.build_glm_paths("k", "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual([p["name"] for p in paths], ["Official", "OpenRouter"])
            self.assertTrue(paths[0]["is_official"])
            self.assertEqual(paths[1].get("vendor_prefix"), "z-ai/")


            tenshi_config.OPENROUTER_API_KEY = None
            paths = tenshi_reply.build_glm_paths("k", "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual([p["name"] for p in paths], ["Official"])
            self.assertTrue(paths[0]["is_official"])


            self.assertEqual(tenshi_reply.build_glm_paths(None, "https://x"), [])
        finally:
            tenshi_config.OPENROUTER_API_KEY = orig_or_key

    def test_glm_tail_alias_is_stripped_before_sending(self):
        """`#tail` 只是链路标签，绝不能发到 API。"""
        import glm_modules

        self.assertEqual(
            glm_modules._normalize_model_name_for_path("glm-5.3#tail"), "glm-5.3"
        )
        self.assertEqual(
            glm_modules._normalize_model_name_for_path(" glm-5.3-flash#tail "),
            "glm-5.3-flash",
        )

    def test_thinking_kwargs_carry_reasoning_effort(self):
        import glm_modules

        aux = glm_modules._thinking_kwargs()
        self.assertEqual(aux["thinking"], {"type": "enabled"})
        self.assertEqual(aux["reasoning_effort"], tenshi_config.GLM_REASONING_EFFORT)
        self.assertNotIn("tools", aux)

        agent = glm_modules._thinking_kwargs(tools=[], agent_complex=True)
        self.assertEqual(
            agent["reasoning_effort"], tenshi_config.GLM_REASONING_EFFORT_AGENT
        )
        self.assertEqual(agent["tool_choice"], "auto")


class DeepseekAliasTest(unittest.TestCase):
    """3) "#tail" 只是链路标签，绝不能发到 API。"""

    def test_alias_is_stripped(self):
        try:
            from deepseek_modules import _normalize_model_name_for_path
        except Exception as e:
            self.skipTest(f"deepseek_modules 不可用：{e}")

        self.assertEqual(
            _normalize_model_name_for_path("deepseek-v4-pro#tail", base_url=None),
            "deepseek-v4-pro",
        )
        self.assertEqual(
            _normalize_model_name_for_path("deepseek-v4-pro", base_url=None),
            "deepseek-v4-pro",
        )
        self.assertEqual(
            _normalize_model_name_for_path(" deepseek-v4-pro#tail ", base_url=None),
            "deepseek-v4-pro",
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"tenshi_reply 不可用：{IMPORT_ERROR}")
class LastResortFallbackTest(unittest.TestCase):
    """额外保险：最后手段兜底绝不返回错误占位；无通路时返回 None 而不是抛错。"""

    def test_returns_none_when_no_paths(self):
        orig_paths = tenshi_reply.build_deepseek_paths
        orig_key = tenshi_reply.DEEPSEEK_API_KEY
        tenshi_reply.build_deepseek_paths = lambda *a, **k: []
        tenshi_reply.DEEPSEEK_API_KEY = None
        try:
            out = tenshi_reply._try_last_resort_deepseek(
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                streamer=None,
            )
            self.assertIsNone(out)
        finally:
            tenshi_reply.build_deepseek_paths = orig_paths
            tenshi_reply.DEEPSEEK_API_KEY = orig_key

    def test_never_returns_error_placeholder(self):
        orig_call = tenshi_reply.call_deepseek
        orig_paths = tenshi_reply.build_deepseek_paths
        tenshi_reply.build_deepseek_paths = lambda *a, **k: [
            {"name": "Official", "api_key": "x", "base_url": None, "is_official": True}
        ]
        tenshi_reply.call_deepseek = lambda *a, **k: ("Error: boom", "")
        try:
            out = tenshi_reply._try_last_resort_deepseek(
                system_prompt="sys",
                messages_payload=[{"role": "user", "content": "hi"}],
                streamer=None,
            )
            self.assertIsNone(out)
        finally:
            tenshi_reply.call_deepseek = orig_call
            tenshi_reply.build_deepseek_paths = orig_paths


if __name__ == "__main__":
    unittest.main()
