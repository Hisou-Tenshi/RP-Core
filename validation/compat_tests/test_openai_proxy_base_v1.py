# SPDX-License-Identifier: AGPL-3.0-only




















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


def _url_spec_defaults() -> dict[str, str]:
    return {
        item["env"]: item["default"]
        for item in providers.PROXY_ENV_SPECS
        if item["role"] == "url"
    }


class ReloadProvidersMixin:
    """用显式环境变量重载 providers 后取一份快照，避免本机 .env / CI secrets 干扰。

    注意：`importlib.reload` 会原地改模块属性，所以必须在退出 mock 环境后再 reload
    一次（还原真实配置，别污染同进程的其它测试），并把需要的值拷进快照返回。
    """

    def _reload_with(self, **env: str):
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                mod = importlib.reload(providers)
                return SimpleNamespace(
                    GPT_PROXIES=[dict(x) for x in mod.GPT_PROXIES],
                    DEEPSEEK_PROXIES=[dict(x) for x in mod.DEEPSEEK_PROXIES],
                    GPT_PROXY_BASE_URL=mod.GPT_PROXY_BASE_URL,
                    FIREWORKS_BASE_URL=mod.FIREWORKS_BASE_URL,
                    PROVIDER_PATH_POLICIES=dict(mod.PROVIDER_PATH_POLICIES),
                )
        finally:

            importlib.reload(providers)


class EnsureOpenaiV1Test(unittest.TestCase):
    def test_root_domain_gets_v1(self):
        self.assertEqual(
            providers._ensure_openai_v1("https://api.coderplan.ai"),
            "https://api.coderplan.ai/v1",
        )

    def test_trailing_slash_is_ignored(self):
        self.assertEqual(
            providers._ensure_openai_v1("https://api.coderplan.ai/"),
            "https://api.coderplan.ai/v1",
        )

    def test_versioned_url_is_kept(self):


        for url in (
            "https://api.openai.com/v1",
            "https://openrouter.ai/api/v1",
            "https://api.coderplan.ai/v1",
            "https://codeyu.shop/v1",
        ):
            self.assertEqual(providers._ensure_openai_v1(url), url)

    def test_empty_and_none_are_kept(self):
        self.assertEqual(providers._ensure_openai_v1(""), "")
        self.assertIsNone(providers._ensure_openai_v1(None))

    def test_gpt_and_grok_proxy_configs_end_with_v1(self):

        self.assertIn("/v1", providers.GPT_PROXY_BASE_URL)
        self.assertIn("/v1", providers.GPT_PROXY2_BASE_URL)
        self.assertIn("/v1", providers.GROK_PROXY_BASE_URL)

    def test_env_specs_document_v1_defaults(self):
        specs = _url_spec_defaults()

        self.assertEqual(specs["GPT_PROXY_BASE_URL"], "https://codeyu.shop/v1")
        self.assertEqual(specs["GPT_PROXY2_BASE_URL"], "https://api.coderplan.ai/v1")
        self.assertEqual(specs["GROK_PROXY_BASE_URL"], "https://api.coderplan.ai/v1")


class GptTwoHopsTest(ReloadProvidersMixin, unittest.TestCase):
    """GPT 代理1 → 代理2 的顺序、名称与独立 URL/key（2026-11 新增代理1）。"""

    def test_two_proxies_in_order(self):
        p = self._reload_with(
            GPT_PROXY_BASE_URL="",
            GPT_PROXY_KEY="key-proxy1",
            GPT_PROXY2_BASE_URL="",
            GPT_PROXY2_KEY="key-proxy2",
        )
        self.assertEqual([x["name"] for x in p.GPT_PROXIES], ["GPT Proxy", "GPT Proxy 2"])
        self.assertEqual([x["base_url"] for x in p.GPT_PROXIES], [
            "https://codeyu.shop/v1",
            "https://api.coderplan.ai/v1",
        ])
        self.assertEqual([x["api_key"] for x in p.GPT_PROXIES], ["key-proxy1", "key-proxy2"])

    def test_proxy1_url_default_and_custom_override(self):
        p = self._reload_with(GPT_PROXY_BASE_URL="", GPT_PROXY_KEY="k")
        self.assertEqual(p.GPT_PROXY_BASE_URL, "https://codeyu.shop/v1")
        p = self._reload_with(
            GPT_PROXY_BASE_URL="https://gpt-relay.example", GPT_PROXY_KEY="k"
        )
        self.assertEqual(p.GPT_PROXY_BASE_URL, "https://gpt-relay.example/v1")

    def test_proxy2_without_key_is_not_built(self):
        p = self._reload_with(
            GPT_PROXY_BASE_URL="",
            GPT_PROXY_KEY="k1",
            GPT_PROXY2_KEY="",
        )
        self.assertEqual([x["name"] for x in p.GPT_PROXIES], ["GPT Proxy"])

    def test_proxy_order_survives_policy(self):


        self.assertEqual(
            providers.PROVIDER_PATH_POLICIES["gpt"].resolved_order(),
            ("proxies", "openrouter", "official"),
        )


class FireworksRetiredTest(ReloadProvidersMixin, unittest.TestCase):
    """DeepSeek 的 Fireworks 代理：设置与默认 URL/key 置空，方法壳保留。"""

    def test_defaults_are_blank(self):
        specs = {item["env"]: item["default"] for item in providers.PROXY_ENV_SPECS}
        self.assertEqual(specs["FIREWORKS_BASE_URL"], "")
        self.assertEqual(specs["FIREWORKS_API_KEY"], "")

    def test_no_deepseek_proxy_when_unset(self):
        p = self._reload_with(FIREWORKS_BASE_URL="", FIREWORKS_API_KEY="")
        self.assertEqual(p.FIREWORKS_BASE_URL, "")
        self.assertEqual(p.DEEPSEEK_PROXIES, [])

        self.assertEqual(
            p.PROVIDER_PATH_POLICIES["deepseek"].resolved_order(),
            ("official", "proxies", "openrouter"),
        )

    def test_shell_still_works_when_explicitly_configured(self):

        p = self._reload_with(
            FIREWORKS_BASE_URL="https://api.fireworks.ai/inference/v1",
            FIREWORKS_API_KEY="fw-key",
        )
        self.assertEqual([x["name"] for x in p.DEEPSEEK_PROXIES], ["Fireworks DeepSeek Pro"])
        self.assertEqual(
            p.DEEPSEEK_PROXIES[0]["base_url"], "https://api.fireworks.ai/inference/v1"
        )


if __name__ == "__main__":
    unittest.main()
