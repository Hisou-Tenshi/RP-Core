# SPDX-License-Identifier: AGPL-3.0-only















import unittest

try:
    from config import fixed
    from config import tunable as tunable_config
    from llm_routing import build_gemini_paths
except Exception as e:
    IMPORT_ERROR = e
else:
    IMPORT_ERROR = None


OPENROUTER_GEMINI_CATALOG = {
    "google/gemini-4-argon",
    "google/gemini-3.8-flash",
    "google/gemini-3.7-flash",
    "google/gemini-3.6-flash",
    "google/gemini-3.5-flash",
    "google/gemini-3.5-flash-lite",
    "google/gemini-3.1-flash-lite",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3-flash-preview",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
}


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class BuildGeminiPathsPassthroughTest(unittest.TestCase):
    """build_llm_paths 对 proxy dict 附加字段（protocol / vendor_prefix）的透传。"""

    PROXIES = [
        {"name": "Gemini Proxy", "api_key": "k1", "base_url": "https://codeyu.shop"},
        {
            "name": "OpenRouter",
            "api_key": "k2",
            "base_url": "https://openrouter.ai/api/v1",
            "protocol": "openai",
            "vendor_prefix": "google/",
        },
    ]

    def test_proxy_first_order_with_official_last(self):
        paths = build_gemini_paths("official-key", self.PROXIES)
        self.assertEqual(
            [p["name"] for p in paths], ["Gemini Proxy", "OpenRouter", "Official"]
        )
        self.assertTrue(paths[-1]["is_official"])
        self.assertFalse(paths[0]["is_official"])

    def test_extra_fields_pass_through(self):
        paths = build_gemini_paths("official-key", self.PROXIES)
        or_path = paths[1]
        self.assertEqual(or_path.get("protocol"), "openai")
        self.assertEqual(or_path.get("vendor_prefix"), "google/")

        self.assertIsNone(paths[0].get("protocol"))
        self.assertIsNone(paths[0].get("vendor_prefix"))


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class PoolModelsOpenRouterIdTest(unittest.TestCase):
    """池内全部 Gemini 生成模型的 google/ 前缀 ID 都在 OpenRouter 目录里。"""

    def test_all_pool_models_have_openrouter_id(self):
        pool = tunable_config.gemini_dialog_models() + tunable_config.GEMINI_IMAGE_MODELS
        self.assertTrue(pool)
        for model in pool:
            self.assertIn(
                f"google/{model}", OPENROUTER_GEMINI_CATALOG, f"OpenRouter 目录缺 {model}"
            )


@unittest.skipIf(IMPORT_ERROR is not None, f"依赖不可用：{IMPORT_ERROR}")
class FixedGeminiProxiesOrderTest(unittest.TestCase):
    """2026-10 口径：GEMINI_PROXIES 只放真正的 Gemini 代理，
    OpenRouter 由 build_gemini_paths 按 PROVIDER_PATH_POLICIES 插到官方之前。
    """

    def test_gemini_proxies_contain_only_gemini_relays(self):
        names = [p.get("name") for p in fixed.GEMINI_PROXIES]
        for name in names:
            self.assertTrue(
                str(name).startswith("Gemini Proxy"),
                f"GEMINI_PROXIES 只应有 Gemini 系代理，实际出现 {name!r}",
            )

        self.assertNotIn("OpenRouter", names)

        self.assertNotIn("Proxy 1", names)
        self.assertNotIn("Proxy 2", names)

    def _build_with_openrouter_key(self, key):
        import tenshi_config

        orig = (tenshi_config.OPENROUTER_API_KEY, tenshi_config.OPENROUTER_BASE_URL)
        tenshi_config.OPENROUTER_API_KEY = key
        tenshi_config.OPENROUTER_BASE_URL = "https://openrouter.example/api/v1"
        try:
            return build_gemini_paths("official-key")
        finally:
            (tenshi_config.OPENROUTER_API_KEY, tenshi_config.OPENROUTER_BASE_URL) = orig

    def test_openrouter_is_inserted_before_official(self):
        paths = self._build_with_openrouter_key("dummy-for-tests")
        names = [p["name"] for p in paths]
        self.assertEqual(names[-1], "Official", "官方必须最后")
        self.assertIn("OpenRouter", names)
        self.assertEqual(
            names.index("OpenRouter"),
            len(names) - 2,
            f"OpenRouter 必须紧邻官方之前：{names}",
        )

        proxy_names = [p.get("name") for p in fixed.GEMINI_PROXIES]
        self.assertEqual(names[: len(proxy_names)], proxy_names)

        or_path = paths[names.index("OpenRouter")]
        self.assertEqual(or_path.get("protocol"), "openai")
        self.assertEqual(or_path.get("vendor_prefix"), "google/")
        self.assertTrue(or_path.get("openrouter"))

    def test_no_openrouter_when_key_missing(self):
        names = [p["name"] for p in self._build_with_openrouter_key(None)]
        self.assertNotIn("OpenRouter", names)
        self.assertEqual(names[-1], "Official")


if __name__ == "__main__":
    unittest.main()
