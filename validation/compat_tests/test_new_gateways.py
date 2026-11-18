# SPDX-License-Identifier: AGPL-3.0-only
"""77 / Dragon 两家后加网关的接线：proxy 列表、路由目录、secret 规格。"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from config import providers


class _Reload(SimpleNamespace):
    pass


class NewGatewayTest(unittest.TestCase):
    def _reload(self, **env: str) -> _Reload:
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                mod = importlib.reload(providers)
                return _Reload(
                    CLAUDE_PROXIES=[dict(x) for x in mod.CLAUDE_PROXIES],
                    GPT_PROXIES=[dict(x) for x in mod.GPT_PROXIES],
                    GEMINI_PROXIES=[dict(x) for x in mod.GEMINI_PROXIES],
                    GROK_PROXIES=[dict(x) for x in mod.GROK_PROXIES],
                    routes={k: tuple(v) for k, v in mod.PROVIDER_ROUTE_CATALOG.items()},
                    specs=[dict(x) for x in mod.PROXY_ENV_SPECS],
                )
        finally:
            importlib.reload(providers)

    def test_gateways_are_wired_when_keys_are_present(self):
        p = self._reload(
            CLAUDE_77_API_KEY="k77c",
            GPT_77_API_KEY="k77g",
            CLAUDE_DRAGON_API_KEY="kdc",
            GPT_DRAGON_API_KEY="kdg",
            GEMINI_DRAGON_API_KEY="kdgem",
            GROK_DRAGON_API_KEY="kdgr",
        )
        names = {
            "claude": [x["name"] for x in p.CLAUDE_PROXIES],
            "gpt": [x["name"] for x in p.GPT_PROXIES],
            "gemini": [x["name"] for x in p.GEMINI_PROXIES],
            "grok": [x["name"] for x in p.GROK_PROXIES],
        }
        self.assertIn("77", names["claude"])
        self.assertIn("77", names["gpt"])
        self.assertIn("Dragon", names["claude"])
        self.assertIn("Dragon", names["gpt"])
        self.assertIn("Dragon", names["gemini"])
        self.assertIn("Dragon", names["grok"])
        self.assertNotIn("Dragon", names["claude"][:1])

    def test_openai_compatible_gateways_end_with_v1(self):
        p = self._reload(GPT_77_API_KEY="k", GPT_DRAGON_API_KEY="k", GROK_DRAGON_API_KEY="k")
        for proxy in p.GPT_PROXIES + p.GROK_PROXIES:
            if proxy["name"] in ("77", "Dragon"):
                self.assertTrue(proxy["base_url"].rstrip("/").endswith("/v1"), proxy)

    def test_route_catalog_lists_the_new_gateways(self):
        p = self._reload(CLAUDE_77_API_KEY="k", GPT_77_API_KEY="k")
        self.assertIn("77", p.routes["claude"])
        self.assertIn("77", p.routes["gpt"])
        self.assertIn("dragon", p.routes["claude"])
        self.assertIn("dragon", p.routes["gpt"])
        self.assertIn("dragon", p.routes["gemini"])
        self.assertIn("dragon", p.routes["grok"])

    def test_secret_specs_follow_the_existing_naming_format(self):
        specs = {item["env"]: item for item in providers.PROXY_ENV_SPECS}
        for env in (
            "CLAUDE_77_API_KEY",
            "GPT_77_API_KEY",
            "CLAUDE_DRAGON_API_KEY",
            "GPT_DRAGON_API_KEY",
            "GEMINI_DRAGON_API_KEY",
            "GROK_DRAGON_API_KEY",
        ):
            self.assertIn(env, specs)
            self.assertEqual(specs[env]["role"], "key")
        self.assertEqual(specs["CLAUDE_77_BASE_URL"]["default"], "https://api.a77ai.cn")
        self.assertEqual(
            specs["GEMINI_DRAGON_BASE_URL"]["default"], "https://newapi.dragon3api.com"
        )

    def test_no_proxy_without_a_key(self):
        p = self._reload(
            CLAUDE_77_API_KEY="",
            GPT_77_API_KEY="",
            CLAUDE_DRAGON_API_KEY="",
            GPT_DRAGON_API_KEY="",
            GEMINI_DRAGON_API_KEY="",
            GROK_DRAGON_API_KEY="",
        )
        self.assertNotIn("77", [x["name"] for x in p.CLAUDE_PROXIES])
        self.assertNotIn("Dragon", [x["name"] for x in p.GPT_PROXIES])


if __name__ == "__main__":
    unittest.main()


class DragonGeminiProtocolTest(unittest.TestCase):
    """Dragon 的 Gemini 只支持 OpenAI 协议，且模型名要用原生 id（不加 google/ 前缀）。"""

    def test_dragon_gemini_proxy_uses_openai_protocol(self):
        with mock.patch.dict(os.environ, {"GEMINI_DRAGON_API_KEY": "kg"}, clear=False):
            mod = importlib.reload(providers)
            try:
                dragon = next(x for x in mod.GEMINI_PROXIES if x["name"] == "Dragon")
            finally:
                importlib.reload(providers)
        self.assertEqual(dragon.get("protocol"), "openai")
        self.assertTrue(dragon.get("native_model_ids"))

    def test_dragon_gemini_model_id_skips_openrouter_slug(self):
        import tenshi_reply as tr

        path = {
            "name": "Dragon",
            "api_key": "k",
            "base_url": "https://newapi.dragon3api.com",
            "protocol": "openai",
            "native_model_ids": True,
        }
        self.assertEqual(tr._openrouter_model_id("gemini-4-argon", path), "gemini-4-argon")

    def test_openrouter_paths_still_use_the_slug(self):
        import tenshi_reply as tr

        path = {"name": "OpenRouter", "openrouter": True, "vendor_prefix": "google/"}
        self.assertEqual(
            tr._openrouter_model_id("gemini-4-argon", path), "google/gemini-4-argon"
        )
