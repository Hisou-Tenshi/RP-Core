# SPDX-License-Identifier: AGPL-3.0-only








from __future__ import annotations

import unittest
from unittest import mock

import gemini_dual


class _FakeNS:
    """模拟 genai 的 client.models 命名空间。"""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls: list[tuple[tuple, dict]] = []

    def generate_content(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result

    def embed_content(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeClient:
    def __init__(self, name: str, result=None, exc=None):
        self.name = name
        self.models = _FakeNS(result, exc)


class DualGeminiClientTest(unittest.TestCase):
    def test_proxy_first_success_does_not_touch_official(self):
        proxy = _FakeClient("proxy", result="P")
        official = _FakeClient("official", result="O")
        dual = gemini_dual.DualGeminiClient(
            [("Gemini Proxy", proxy), ("Official", official)]
        )
        self.assertEqual(dual.models.generate_content(model="m", contents="x"), "P")
        self.assertEqual(len(official.models.calls), 0)

    def test_falls_back_to_official_on_proxy_error(self):
        proxy = _FakeClient("proxy", exc=RuntimeError("proxy down"))
        official = _FakeClient("official", result="O")
        dual = gemini_dual.DualGeminiClient(
            [("Gemini Proxy", proxy), ("Official", official)]
        )
        self.assertEqual(dual.models.generate_content("x"), "O")
        self.assertEqual(len(official.models.calls), 1)

    def test_raises_last_error_when_all_paths_fail(self):
        dual = gemini_dual.DualGeminiClient(
            [
                ("Gemini Proxy", _FakeClient("p", exc=RuntimeError("e1"))),
                ("Official", _FakeClient("o", exc=ValueError("e2"))),
            ]
        )
        with self.assertRaises(ValueError) as ctx:
            dual.run(lambda c: c.models.generate_content("x"))
        self.assertEqual(str(ctx.exception), "e2")

    def test_no_paths_raises_runtime_error(self):
        dual = gemini_dual.DualGeminiClient([])
        with self.assertRaises(RuntimeError):
            dual.run(lambda c: c.models.generate_content("x"))

    def test_official_only_works(self):
        official = _FakeClient("official", result="O")
        dual = gemini_dual.DualGeminiClient([("Official", official)])
        self.assertEqual(dual.models.generate_content("x"), "O")
        self.assertEqual(dual.path_names, ["Official"])

    def test_embed_content_shape_forwarded(self):
        proxy = _FakeClient("proxy", result={"embedding": [0.1, 0.2]})
        dual = gemini_dual.DualGeminiClient([("Gemini Proxy", proxy)])
        out = dual.models.embed_content(model="gemini-embedding", contents="hi")
        self.assertEqual(out, {"embedding": [0.1, 0.2]})
        _, kwargs = proxy.models.calls[0]
        self.assertEqual(kwargs.get("contents"), "hi")
        self.assertEqual(kwargs.get("model"), "gemini-embedding")


class BuildClientTest(unittest.TestCase):
    """build_gemini_dual_client：2026-10 起按 GEMINI_PROXY_KEY / GEMINI_PROXY2_KEY
    动态建代理路（代理1 → 代理2），其后才是官方；GEMINI_PROXIES 仅在动态代理全缺时兜底。
    """

    def _build_with(
        self,
        *,
        proxy_key=None,
        proxy2_key=None,
        official_key=None,
        proxies=(),
    ):
        import tenshi_config

        with mock.patch.multiple(
            tenshi_config,
            GEMINI_PROXY_KEY=proxy_key,
            GEMINI_PROXY_BASE_URL="https://proxy.test",
            GEMINI_PROXY2_KEY=proxy2_key,
            GEMINI_PROXY2_BASE_URL="https://proxy2.test",
            GEMINI_API_KEY=official_key,
            GEMINI_PROXIES=list(proxies),
            LLM_CLIENT_READ_TIMEOUT_SEC=30,
            LLM_CLIENT_MAX_RETRIES=2,
        ), mock.patch.object(gemini_dual.genai, "Client", new=lambda **kw: ("fake-client", kw)):
            return gemini_dual.build_gemini_dual_client()

    def test_paths_order_proxy1_proxy2_official(self):
        dual = self._build_with(
            proxy_key="pk1", proxy2_key="pk2", official_key="ok"
        )
        self.assertEqual(
            dual.path_names, ["Gemini Proxy", "Gemini Proxy 2", "Official"]
        )

    def test_proxy_disabled_when_no_key(self):
        dual = self._build_with(proxy_key=None, official_key="ok")
        self.assertEqual(dual.path_names, ["Official"])

    def test_no_keys_no_paths(self):
        dual = self._build_with(proxy_key=None, official_key=None)
        self.assertEqual(dual.path_names, [])
        with self.assertRaises(RuntimeError):
            dual.run(lambda c: c.models.generate_content("x"))

    def test_proxy_base_url_injected(self):
        dual = self._build_with(proxy_key="pk", official_key="ok")

        seen: list = []

        def _grab(c):
            seen.append(c)
            raise RuntimeError("fail-every-path-for-inspection")

        with self.assertRaises(RuntimeError):
            dual.run(_grab)
        self.assertEqual(seen[0][1]["http_options"].base_url, "https://proxy.test")
        self.assertEqual(seen[1][1]["api_key"], "ok")

    def test_second_proxy_base_url_injected(self):
        dual = self._build_with(
            proxy_key="pk1", proxy2_key="pk2", official_key="ok"
        )
        seen: list = []

        def _grab(c):
            seen.append(c)
            raise RuntimeError("fail")

        with self.assertRaises(RuntimeError):
            dual.run(_grab)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[1][1]["http_options"].base_url, "https://proxy2.test")
        self.assertEqual(seen[1][1]["api_key"], "pk2")

    def test_gemini_proxies_fallback_when_no_dynamic_proxy(self):
        """GEMINI_PROXY_KEY / PROXY2_KEY 都没配时，退回配置层的 GEMINI_PROXIES。"""
        dual = self._build_with(
            proxy_key=None,
            official_key="ok",
            proxies=[
                {
                    "name": "Gemini Proxy",
                    "api_key": "legacy-key",
                    "base_url": "https://legacy.test",
                }
            ],
        )
        self.assertEqual(dual.path_names, ["Gemini Proxy", "Official"])
        seen: list = []

        def _grab(c):
            seen.append(c)
            raise RuntimeError("fail")

        with self.assertRaises(RuntimeError):
            dual.run(_grab)
        self.assertEqual(seen[0][1]["http_options"].base_url, "https://legacy.test")

    def test_openrouter_relay_is_excluded_from_genai_paths(self):
        """OpenRouter 是 OpenAI 兼容协议，不塞进 genai 双路客户端（由图片识别单独兜底）。"""
        dual = self._build_with(
            proxy_key=None,
            official_key="ok",
            proxies=[
                {
                    "name": "OpenRouter",
                    "api_key": "or-key",
                    "base_url": "https://openrouter.test/api/v1",
                    "protocol": "openai",
                    "openrouter": True,
                }
            ],
        )
        self.assertEqual(dual.path_names, ["Official"])


if __name__ == "__main__":
    unittest.main()
