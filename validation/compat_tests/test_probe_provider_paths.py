# SPDX-License-Identifier: AGPL-3.0-only

















from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)


def _load_probe_module():
    script = pathlib.Path(ROOT) / "scripts" / "probe_provider_paths.py"
    spec = importlib.util.spec_from_file_location("probe_provider_paths_ut", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_probe_module()

MODEL_ENTRY_FIELDS = {"ok", "verdict", "severity", "detail", "method", "status", "latency_ms"}







def _target(models: list[str] | None = None, **overrides) -> dict:
    models = list(models or ["claude-haiku-4-5", "claude-opus-5-5"])
    target = {
        "provider": "claude",
        "name": "Codeyu Base",
        "base_url": "https://proxy.example/v1",
        "api_key": "sk-ant-0123456789abcdef",
        "protocol": "anthropic",
        "model": models[0],
        "models": models,
        "path_models": {m: m for m in models},
        "model_candidates": [models[0]],
    }
    target.update(overrides)
    return target


def _http_result(status: int = 200, body: str = "", json_body=None, latency_ms: float = 1.0, error: str | None = None) -> dict:
    payload = json_body
    if payload is None and body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
    return {
        "status": status,
        "ok": 200 <= status < 300,
        "latency_ms": latency_ms,
        "body": body,
        "json": payload,
        "error": error,
    }


def _ok_model_list(models: list[str]) -> dict:
    return _http_result(200, json_body={"object": "list", "data": [{"id": m} for m in models]})


@contextlib.contextmanager
def _fake_network(*, dns_ok: bool = True):
    """把三层网络探测全部 mock 掉（不碰真实 socket）。"""

    def _dns(host, port):
        return {
            "ok": dns_ok,
            "addresses": ["1.2.3.4 (IPv4)"] if dns_ok else [],
            "error": None if dns_ok else "gaierror: Name or service not known",
            "latency_ms": 1.0,
        }

    def _tcp(host, port, timeout):
        return {"ok": True, "peer": "1.2.3.4:443", "error": None, "latency_ms": 1.0}

    def _tls(host, port, timeout):
        return {
            "ok": True,
            "error": None,
            "version": "TLSv1.3",
            "cert_cn": host,
            "issuer": "Test CA",
            "days_left": 90,
            "cert_error": None,
            "unverified_ok": None,
            "latency_ms": 1.0,
        }

    with mock.patch.object(mod, "probe_dns", _dns), mock.patch.object(
        mod, "probe_tcp", _tcp
    ), mock.patch.object(mod, "probe_tls", _tls):
        yield


@contextlib.contextmanager
def _fake_endpoints(protocol: str, auth_result: dict, count_fn=None):
    """mock 鉴权（GET /models）与（可选）count tokens 端点。"""
    auth = lambda target, timeout: auth_result
    count = count_fn or (lambda target, timeout: _http_result(404, body="<html>404 Not Found</html>"))
    with mock.patch.dict(mod.AUTH_CALLERS, {protocol: auth}):


        if protocol in mod.COUNT_TOKEN_CALLERS:
            with mock.patch.dict(mod.COUNT_TOKEN_CALLERS, {protocol: count}):
                yield
        else:
            yield


def _probe(target: dict, auth_result: dict, count_fn=None, *, all_models: bool = True, dns_ok: bool = True) -> dict:
    with _fake_network(dns_ok=dns_ok), _fake_endpoints(target["protocol"], auth_result, count_fn):
        return mod.probe_target(target, timeout=1.0, all_models=all_models)







class ProviderModelRegistryTest(unittest.TestCase):
    """模型清单单一事实来源：全部来自 src/config/models.py（经 tenshi_config 暴露）。"""

    def test_provider_model_ids_cover_every_registered_model(self):
        expected = {
            "claude": 8,
            "gemini": 4,
            "deepseek": 2,
            "gpt": 1,
            "glm": 2,
            "magmarouter": 6,
            "openrouter": 1,
            "grok": 2,
        }
        for provider, count in expected.items():
            models = mod._provider_model_ids(provider)
            self.assertEqual(len(models), count, f"{provider} 全模型数不对：{models}")
            self.assertEqual(len(models), len(set(models)), f"{provider} 模型清单有重复")
            self.assertNotIn("", models)

    def test_claude_models_include_dated_snapshot(self):
        models = mod._provider_model_ids("claude")
        self.assertIn("claude-haiku-4-5", models)
        self.assertIn("claude-haiku-4-5-20251001", models)

        cfg = _load_tenshi_config()
        head = list(cfg.CLAUDE_MAX_MODELS) + list(cfg.CLAUDE_PRO_MODELS)
        self.assertEqual(models[: len(head)], head)

    def test_path_model_id_openrouter_slug(self):
        tenant = _load_tenshi_config()
        for native in ("deepseek-v4-pro", "deepseek-flash", "grok-4.7", "glm-5.3"):
            self.assertEqual(
                mod._path_model_id(native, openrouter=True),
                tenant.openrouter_fallback_slug(native),
                f"{native} 应换成 OpenRouter slug",
            )

        self.assertIsNone(tenant.openrouter_fallback_slug("claude-haiku-4-5-20251001"))
        self.assertEqual(
            mod._path_model_id("claude-haiku-4-5-20251001", openrouter=True, vendor_prefix="anthropic/"),
            "anthropic/claude-haiku-4-5-20251001",
        )

    def test_path_model_id_fireworks_namespace(self):
        tenant = _load_tenshi_config()
        self.assertEqual(
            mod._path_model_id("deepseek-v4-pro", fireworks=True),
            tenant.FIREWORKS_DEEPSEEK_V4_PRO_MODEL,
        )
        self.assertEqual(
            mod._path_model_id("deepseek-flash", fireworks=True),
            tenant.FIREWORKS_DEEPSEEK_V4_FLASH_MODEL,
        )
        self.assertIn("fireworks", mod._path_model_id("deepseek-v4-pro", fireworks=True))

    def test_with_all_models_keeps_representative_model(self):
        target = mod._target("gemini", "Official", "https://g.example", "k", "gemini", "gemini-3.5-flash-lite")
        before = target["model"]
        mod._with_all_models(target)
        self.assertEqual(target["model"], before)
        self.assertEqual(len(target["models"]), 4)
        self.assertEqual(set(target["path_models"]), set(target["models"]))
        self.assertEqual(target["path_models"]["gemini-3.5-flash-lite"], "gemini-3.5-flash-lite")


def _load_tenshi_config():
    import tenshi_config

    return tenshi_config


class CollectTargetsTest(unittest.TestCase):
    """collect_targets 给每条通路挂上该 provider 的全模型，并按通路归一化模型名。"""

    def setUp(self):
        tenant = _load_tenshi_config()
        self.tenant = tenant
        self._patches = [
            mock.patch.object(
                mod,
                "build_claude_proxy_configs_for_model",
                lambda model_id: [
                    {"name": "Codeyu Base", "base_url": "https://p1.example/v1", "api_key": "sk-p1"},
                ],
            ),
            mock.patch.object(
                mod,
                "build_gemini_paths",
                lambda api_key, proxies: [
                    {"name": "Codeyu", "base_url": "https://g.example", "api_key": "sk-g", "protocol": "gemini"},
                    {
                        "name": "OpenRouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "sk-or",
                        "protocol": "openai",
                        "vendor_prefix": "google/",
                        "openrouter": True,
                    },
                ],
            ),
            mock.patch.object(
                mod,
                "build_deepseek_paths",
                lambda api_key, base_url, proxies: [
                    {"name": "Official", "base_url": "https://api.deepseek.com", "api_key": "sk-ds"},
                    {
                        "name": "Fireworks DeepSeek Pro",
                        "base_url": "https://api.fireworks.ai/inference/v1",
                        "api_key": "sk-fw",
                    },
                    {
                        "name": "OpenRouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "sk-or",
                        "openrouter": True,
                    },
                ],
            ),
            mock.patch.object(
                mod,
                "build_gpt_paths",
                lambda api_key, base_url, proxies: [
                    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "api_key": "sk-gpt"},
                ],
            ),
            mock.patch.object(
                mod,
                "build_grok_paths",
                lambda api_key, base_url, proxies: [
                    {"name": "Official", "base_url": "https://api.x.ai/v1", "api_key": "sk-grok"},
                ],
            ),
            mock.patch.object(
                mod,
                "build_glm_paths",
                lambda api_key, base_url: [
                    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or", "openrouter": True},
                ],
            ),
            mock.patch.object(
                mod,
                "build_magmarouter_paths",
                lambda api_key, base_url: [
                    {"name": "MagmaRouter", "base_url": "https://api.magmarouter.com/v1", "api_key": "sk-mr"},
                ],
            ),
            mock.patch.object(
                mod,
                "build_openrouter_paths",
                lambda api_key, base_url: [
                    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or"},
                ],
            ),
        ]
        for patch in self._patches:
            patch.start()
        self.targets = mod.collect_targets(
            ["claude", "gemini", "deepseek", "gpt", "glm", "magmarouter", "openrouter", "grok"]
        )

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def _by(self, provider: str, name: str) -> dict:
        for target in self.targets:
            if target["provider"] == provider and target["name"] == name:
                return target
        raise AssertionError(f"没有找到通路 {provider}/{name}")

    def test_every_target_has_all_models_and_path_models(self):
        for target in self.targets:
            self.assertTrue(target["models"], f"{target['provider']}/{target['name']} 没有挂全模型")
            self.assertEqual(set(target["path_models"]), set(target["models"]))
            self.assertEqual(len(target["models"]), len(set(target["models"])))

    def test_claude_proxy_has_8_models(self):
        target = self._by("claude", "Codeyu Base")
        self.assertEqual(len(target["models"]), 8)
        self.assertIn("claude-haiku-4-5-20251001", target["models"])
        self.assertIn("claude-fable-5-2", target["models"])

    def test_gemini_openrouter_uses_google_slugs(self):
        target = self._by("gemini", "OpenRouter")
        self.assertEqual(target["path_models"]["gemini-4-argon"], "google/gemini-4-argon")
        self.assertTrue(all(m.startswith("google/") for m in target["path_models"].values()))

    def test_deepseek_paths_normalize_models(self):
        official = self._by("deepseek", "Official")
        self.assertEqual(official["path_models"], {m: m for m in official["models"]})

        fireworks = self._by("deepseek", "Fireworks DeepSeek Pro")
        tenant = self.tenant
        self.assertEqual(fireworks["path_models"]["deepseek-v4-pro"], tenant.FIREWORKS_DEEPSEEK_V4_PRO_MODEL)
        self.assertEqual(fireworks["path_models"]["deepseek-flash"], tenant.FIREWORKS_DEEPSEEK_V4_FLASH_MODEL)

        openrouter = self._by("deepseek", "OpenRouter")
        for native, slug in openrouter["path_models"].items():
            self.assertEqual(slug, tenant.openrouter_fallback_slug(native))







class ProbeModelsTest(unittest.TestCase):
    MODELS = ["claude-haiku-4-5", "claude-opus-5-5"]

    def test_all_models_ok_via_models_list_without_count_tokens(self):
        def _count(target, timeout):
            raise AssertionError("模型都在列表里，不该再发 count tokens")

        item = _probe(_target(self.MODELS), _ok_model_list(self.MODELS), _count)
        self.assertEqual(set(item["models"]), set(self.MODELS))
        for model_id in self.MODELS:
            entry = item["models"][model_id]
            self.assertEqual(set(entry), MODEL_ENTRY_FIELDS)
            self.assertIs(entry["ok"], True)
            self.assertEqual(entry["verdict"], "ok")
            self.assertEqual(entry["method"], "models_list")
        self.assertEqual(item["models_total"], 2)
        self.assertEqual(item["models_ok"], 2)
        self.assertEqual(item["checks"]["model"]["verdict"], "ok")
        self.assertEqual(item["model_check_method"], "models_list")
        self.assertIs(item["ok"], True)

    def test_list_miss_falls_back_to_per_model_count_tokens(self):
        calls: list[str] = []

        def _count(target, timeout):
            calls.append(target["model"])
            return _http_result(200, json_body={"input_tokens": 1})

        item = _probe(_target(self.MODELS), _ok_model_list(["claude-haiku-4-5"]), _count)
        self.assertEqual(calls, ["claude-opus-5-5"], "只应对列表里没有的模型发 count tokens")
        self.assertEqual(item["models"]["claude-opus-5-5"]["method"], "count_tokens")
        self.assertIs(item["models"]["claude-opus-5-5"]["ok"], True)
        self.assertEqual(item["models"]["claude-haiku-4-5"]["method"], "models_list")
        self.assertEqual(item["model_check_method"], "models_list", "有 ok 的模型时取产出结论的 method")
        self.assertIs(item["ok"], True)

    def test_all_models_missing_makes_path_red(self):
        """2026-10-29 起：全部模型 model_not_found → 整条通路红（没有任何可用模型）。"""
        def _count(target, timeout):
            return _http_result(
                404,
                body=json.dumps({"type": "not_found_error", "error": {"message": "model: nope not found"}}),
            )

        item = _probe(_target(self.MODELS), _ok_model_list([]), _count)
        for model_id in self.MODELS:
            entry = item["models"][model_id]
            self.assertEqual(entry["verdict"], "model_not_found")
            self.assertIs(entry["ok"], False, "模型不可用按红处理（模型级 ban），不再黄档")
            self.assertEqual(entry["severity"], "red")
        self.assertEqual(item["checks"]["model"]["verdict"], "model_not_found")
        self.assertIs(item["checks"]["model"]["ok"], False)
        self.assertEqual(item["severity"], "red")
        self.assertIs(item["ok"], False, "一个可用模型都没有 → 通路红（外层 ban）")
        self.assertEqual(item["models_ok"], 0)

    def test_partial_model_missing_keeps_path_green_but_bans_models(self):
        """部分模型不可用：通路仍绿（还有模型可用），但被 ban 的模型逐个记红。"""
        def _count(target, timeout):
            return _http_result(
                404,
                body=json.dumps({"type": "not_found_error", "error": {"message": "model: nope not found"}}),
            )

        item = _probe(_target(self.MODELS), _ok_model_list(["claude-haiku-4-5"]), _count)
        self.assertIs(item["models"]["claude-haiku-4-5"]["ok"], True)
        self.assertIs(item["models"]["claude-opus-5-5"]["ok"], False, "model_not_found 按模型级红处理")
        self.assertEqual(item["models"]["claude-opus-5-5"]["severity"], "red")
        self.assertIs(item["ok"], True, "任一模型 ok → 通路绿（gate 仍是通路级）")
        self.assertEqual(item["severity"], "green")
        self.assertEqual(item["models_ok"], 1)
        self.assertIn("1 个模型在该通路 ban", item["checks"]["model"]["verdict_text"])

    def test_rate_limited_models_still_warn_not_ban(self):
        """429 仍属黄档：限流是瞬时的，不能因此 ban 通路或模型。"""
        def _count(target, timeout):
            return _http_result(429, body=json.dumps({"error": {"message": "rate limit"}}))

        item = _probe(_target(self.MODELS), _ok_model_list([]), _count)
        for model_id in self.MODELS:
            entry = item["models"][model_id]
            self.assertEqual(entry["verdict"], "rate_limited")
            self.assertIs(entry["ok"], True, "限流是瞬时错误，不 ban")
            self.assertEqual(entry["severity"], "warn")
        self.assertEqual(item["severity"], "warn")
        self.assertIs(item["ok"], True, "warn 通路照常调用")
        self.assertEqual(item["models_ok"], 0, "warn（不 ban）不算真正可用，不能算进模型覆盖")

    def test_rate_limited_models_are_not_counted_as_available(self):
        """models_ok 必须是「真正 ✅」口径：warn 的 ok=True 只是 gate 不 ban。"""

        def _count(target, timeout):
            return _http_result(429, body=json.dumps({"error": {"message": "rate limit"}}))

        item = _probe(_target(self.MODELS), _ok_model_list([]), _count)
        self.assertTrue(all(entry["severity"] == "warn" for entry in item["models"].values()))
        self.assertEqual(item["models_total"], 2)
        self.assertEqual(item["models_ok"], 0, "只有 2 个模型登记在这条通路上时不能显示 7/7")
        self.assertIs(item["ok"], True, "通路级 gate 语义不变：warn 仍照常调用")

    def test_all_unknown_is_fail_open(self):
        def _count(target, timeout):
            return _http_result(404, body="<html>404 Not Found</html>")

        item = _probe(_target(self.MODELS), _http_result(200, body="{}"), _count)
        for model_id in self.MODELS:
            entry = item["models"][model_id]
            self.assertEqual(entry["verdict"], "unknown")
            self.assertIsNone(entry["ok"], "unknown 不参与 ban")
        self.assertEqual(item["checks"]["model"]["verdict"], "unknown")
        self.assertIsNone(item["checks"]["model"]["ok"])

    def test_any_ok_dominates_red_models(self):
        def _count(target, timeout):
            return _http_result(401, body=json.dumps({"error": {"message": "invalid api key"}}))

        item = _probe(_target(self.MODELS), _ok_model_list(["claude-haiku-4-5"]), _count)
        self.assertIs(item["models"]["claude-haiku-4-5"]["ok"], True)
        self.assertIs(item["models"]["claude-opus-5-5"]["ok"], False)
        self.assertIs(item["ok"], True, "聚合规则：任一模型 ok → 通路模型层 ok")
        self.assertEqual(item["models_ok"], 1)

    def test_all_red_models_make_path_red(self):
        def _count(target, timeout):
            return _http_result(401, body=json.dumps({"error": {"message": "invalid api key"}}))

        item = _probe(_target(self.MODELS), _ok_model_list([]), _count)
        self.assertEqual(item["checks"]["model"]["verdict"], "auth_denied")
        self.assertIs(item["checks"]["model"]["ok"], False)
        self.assertEqual(item["severity"], "red")
        self.assertIs(item["ok"], False)

    def test_no_key_marks_every_model_not_configured(self):
        item = _probe(_target(self.MODELS, api_key=""), _http_result(200, body="{}"))
        for model_id in self.MODELS:
            self.assertEqual(item["models"][model_id]["verdict"], "not_configured")
            self.assertIsNone(item["models"][model_id]["ok"])
        self.assertEqual(item["checks"]["model"]["verdict"], "not_configured")
        self.assertIsNone(item["checks"]["model"]["ok"])

    def test_unreachable_network_marks_models_unknown(self):
        item = _probe(_target(self.MODELS), _http_result(200, body="{}"), dns_ok=False)
        for model_id in self.MODELS:
            self.assertEqual(item["models"][model_id]["verdict"], "unknown")
            self.assertIsNone(item["models"][model_id]["ok"])
        self.assertIsNone(item["checks"]["model"]["ok"])
        self.assertEqual(item["checks"]["network"]["verdict"], "dns_fail")

    def test_no_all_models_probes_only_representative(self):
        def _count(target, timeout):
            raise AssertionError("代表模型在列表里，不该发 count tokens")

        item = _probe(_target(self.MODELS), _ok_model_list(self.MODELS), _count, all_models=False)
        self.assertEqual(list(item["models"]), ["claude-haiku-4-5"])
        self.assertEqual(item["models_total"], 1)

    def test_key_masked_keeps_prefix_only(self):
        item = _probe(_target(self.MODELS, api_key="sk-ant-0123456789abcdef"), _ok_model_list(self.MODELS))
        self.assertEqual(item["key_masked"], "sk-a…")
        self.assertNotIn("cdef", item["key_masked"], "公共仓库里不能暴露 key 末 4 位")

        short = _probe(_target(self.MODELS, api_key="sk-short"), _ok_model_list(self.MODELS))
        self.assertEqual(short["key_masked"], "***")
        empty = _probe(_target(self.MODELS, api_key=""), _ok_model_list(self.MODELS))
        self.assertEqual(empty["key_masked"], "")

    def test_openai_protocol_only_uses_model_list(self):
        target = _target(
            ["gpt-6-luna"],
            provider="gpt",
            name="OpenAI",
            protocol="openai",
            base_url="https://api.openai.com/v1",
        )

        def _count(target, timeout):
            raise AssertionError("openai 兼容协议不该调 count tokens")



        item = _probe(target, _ok_model_list(["gpt-6-luna-mini"]), _count)
        entry = item["models"]["gpt-6-luna"]
        self.assertEqual(entry["verdict"], "model_not_found")
        self.assertIs(entry["ok"], False)
        self.assertEqual(entry["method"], "models_list")


class AggregateModelsTest(unittest.TestCase):
    """通路级模型层聚合规则（与 task 约定逐条对齐）。"""

    @staticmethod
    def _entry(verdict: str, method: str = "models_list") -> dict:
        return mod._model_entry(verdict, verdict, method=method)

    def test_aggregate_rules(self):
        ok = self._entry("ok")
        missing = self._entry("model_not_found", "count_tokens")
        limited = self._entry("rate_limited")
        red = self._entry("auth_denied")
        unknown = self._entry("unknown", "none")
        not_configured = self._entry("not_configured", "none")

        self.assertEqual(mod._aggregate_models({"a": ok, "b": red})[0], "ok")

        self.assertEqual(mod._aggregate_models({"a": missing})[0], "model_not_found")
        self.assertEqual(mod._aggregate_models({"a": missing, "b": red})[0], "model_not_found")

        self.assertEqual(mod._aggregate_models({"a": limited})[0], "rate_limited")
        self.assertEqual(mod._aggregate_models({"a": unknown})[0], "unknown")
        self.assertEqual(mod._aggregate_models({"a": red})[0], "auth_denied")
        self.assertEqual(mod._aggregate_models({"a": not_configured})[0], "not_configured")
        self.assertEqual(mod._aggregate_models({})[0], "unknown")

    def test_aggregate_model_method(self):
        ok_list = self._entry("ok")
        ok_count = self._entry("ok", "count_tokens")
        unknown = self._entry("unknown", "none")
        self.assertEqual(mod._aggregate_model_method({"a": ok_list}), "models_list")
        self.assertEqual(mod._aggregate_model_method({"a": ok_count}), "count_tokens")
        self.assertEqual(mod._aggregate_model_method({"a": unknown}), "none")
        self.assertEqual(mod._aggregate_model_method({}), "none")







def _context(**overrides) -> dict:
    context = {
        "generated_at_utc": "2026-10-28T12:00:00+00:00",
        "generated_at_bj": "2026-10-28 20:00:00",
        "environment": "非容器",
        "deployment": "tenshi",
        "deployment_label": "Tenshi 备用运行时（Tenshi secrets）",
        "run_id": "123456789",
        "script": "scripts/probe_provider_paths.py",
        "probe_method": "official_non_call",
    }
    context.update(overrides)
    return context


class OutputsSchemaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.item = _probe(
            _target(["claude-haiku-4-5", "claude-opus-5-5"]),
            _ok_model_list(["claude-haiku-4-5"]),
            lambda target, timeout: _http_result(200, json_body={"input_tokens": 1}),
        )
        self.context = _context()

    def tearDown(self):
        self._tmp.cleanup()

    def test_schema4_fields(self):
        payload = mod.write_outputs(self.tmp / "provider_health.json", None, self.context, [self.item])

        self.assertEqual(payload["schema"], 4)
        for key in ("deployment", "deployment_label", "coverage", "generated_at_utc", "generated_at_bj", "providers"):
            self.assertIn(key, payload)
        self.assertEqual(payload["deployment"], "tenshi")
        self.assertEqual(payload["deployment_label"], "Tenshi 备用运行时（Tenshi secrets）")

        self.assertEqual(payload["coverage"], {"claude": 8})

        provider = payload["providers"]["claude"]
        self.assertEqual(provider["models_total"], 8)
        self.assertEqual(provider["models_ok"], 2)

        path = provider["paths"]["Codeyu Base"]
        for key in ("models_total", "models_ok", "models", "model_check_method", "key_masked"):
            self.assertIn(key, path)
        self.assertEqual(path["models_total"], 2)
        self.assertEqual(path["models_ok"], 2)
        self.assertEqual(set(path["models"]), {"claude-haiku-4-5", "claude-opus-5-5"})
        for entry in path["models"].values():
            self.assertEqual(set(entry), MODEL_ENTRY_FIELDS)
        self.assertEqual(path["key_masked"], "sk-a…")


        on_disk = json.loads((self.tmp / "provider_health.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], 4)
        self.assertEqual(on_disk["providers"]["claude"]["paths"]["Codeyu Base"]["models_ok"], 2)

    def test_markdown_has_all_model_matrix(self):
        report = self.tmp / "latest.md"
        mod.write_outputs(self.tmp / "provider_health.json", report, self.context, [self.item])
        text = report.read_text(encoding="utf-8")
        self.assertIn("## 全模型矩阵", text)
        self.assertIn("### `claude`（模型 2/2 可用）", text)
        self.assertIn("| 通路 | `claude-haiku-4-5` | `claude-opus-5-5` |", text)

        self.assertIn("## 类别结论", text)
        self.assertIn("## 逐通路", text)
        self.assertIn("Tenshi 备用运行时", text)

    def test_history_entry_fields_and_trim(self):
        payload = mod.write_outputs(self.tmp / "provider_health.json", None, self.context, [self.item])
        entry = mod.build_history_entry(self.context, payload)
        self.assertEqual(
            set(entry),
            {
                "generated_at_utc",
                "generated_at_bj",
                "environment",
                "deployment",
                "deployment_label",
                "run_id",
                "probe_method",
                "paths",
                "providers",
            },
        )
        path_entry = entry["paths"]["claude/Codeyu Base"]
        self.assertEqual(
            set(path_entry), {"severity", "ok", "verdict", "latency_ms", "models_ok", "models_total"}
        )
        self.assertEqual(entry["providers"]["claude"], {"ok": 1, "total": 1, "models_ok": 2, "models_total": 8})

        history = self.tmp / "history.jsonl"
        history.write_text("\n".join(json.dumps({"run": i}) for i in range(400)) + "\n", encoding="utf-8")
        kept = mod.append_history(history, entry)
        self.assertEqual(kept, 400)
        lines = history.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 400)
        self.assertEqual(json.loads(lines[-1])["deployment"], "tenshi")
        self.assertEqual(json.loads(lines[0]), {"run": 1}, "超出 400 行的最旧记录应被裁掉")

        kept = mod.append_history(history, entry)
        self.assertEqual(kept, 400)
        self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 400)
        self.assertFalse((self.tmp / "history.jsonl.tmp").exists(), "原子写不应留下 tmp 文件")

    def test_warn_only_models_not_counted_in_coverage(self):
        """provider / path / markdown / history 的模型覆盖都按「真正可用」统计。"""

        def _count(target, timeout):
            return _http_result(429, body=json.dumps({"error": {"message": "rate limit"}}))

        item = _probe(_target(["claude-haiku-4-5", "claude-opus-5-5"]), _ok_model_list([]), _count)
        json_path = self.tmp / "warn.json"
        report_path = self.tmp / "warn.md"
        payload = mod.write_outputs(json_path, report_path, self.context, [item])

        provider = payload["providers"]["claude"]
        self.assertEqual(provider["models_total"], 8)
        self.assertEqual(provider["models_ok"], 0, "warn 不计入 provider 模型覆盖")
        path = provider["paths"]["Codeyu Base"]
        self.assertEqual(path["models_total"], 2)
        self.assertEqual(path["models_ok"], 0, "warn 不计入路径模型覆盖")
        self.assertIs(path["ok"], True, "warn 通路仍照常调用（gate 语义不变）")
        self.assertEqual(path["severity"], "warn")

        text = report_path.read_text(encoding="utf-8")
        self.assertIn("（模型 0/2 可用）", text)
        self.assertIn("| `claude` | 1/1 | 0/2 |", text)

        history = mod.build_history_entry(self.context, payload)
        self.assertEqual(history["providers"]["claude"]["models_ok"], 0)
        self.assertEqual(history["paths"]["claude/Codeyu Base"]["models_ok"], 0)
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["providers"]["claude"]["models_ok"], 0)

    def test_model_level_red_models_counted_as_unavailable(self):
        """2026-10-29 起 model_not_found 是红：通路红、覆盖 0，报告直接说 ban。"""

        def _count(target, timeout):
            return _http_result(
                404,
                body=json.dumps({"type": "not_found_error", "error": {"message": "model: nope not found"}}),
            )

        item = _probe(_target(["claude-haiku-4-5", "claude-opus-5-5"]), _ok_model_list([]), _count)
        json_path = self.tmp / "ban.json"
        report_path = self.tmp / "ban.md"
        payload = mod.write_outputs(json_path, report_path, self.context, [item])

        path = payload["providers"]["claude"]["paths"]["Codeyu Base"]
        self.assertIs(path["ok"], False, "全部模型 model_not_found → 通路红（外层 ban）")
        self.assertEqual(path["severity"], "red")
        self.assertEqual(path["models_ok"], 0)
        self.assertEqual(payload["summary"]["red"], ["claude/Codeyu Base"])
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("❌ 红（外层 ban 掉）", text)
        self.assertIn("模型级 ban", text)

    def test_history_creates_parent_and_tolerates_missing_file(self):
        history = self.tmp / "reports" / "provider_health" / "history.jsonl"
        payload = mod.write_outputs(self.tmp / "provider_health.json", None, self.context, [self.item])
        self.assertEqual(mod.append_history(history, mod.build_history_entry(self.context, payload)), 1)
        self.assertTrue(history.exists())
        self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 1)

    def test_history_drops_corrupt_lines(self):
        """历史文件被写进冲突标记 / 半截内容时，下一次 append 要能自动清洗。"""
        history = self.tmp / "history.jsonl"
        history.write_text(
            "<<<<<<< HEAD\n"
            '{"run": 1}\n'
            "=======\n"
            '{"run": 2}\n'
            ">>>>>>> other\n"
            "not-json\n",
            encoding="utf-8",
        )
        payload = mod.write_outputs(self.tmp / "ph.json", None, self.context, [self.item])
        kept = mod.append_history(history, mod.build_history_entry(self.context, payload))
        self.assertEqual(kept, 3, "两条合法旧记录 + 本次新增；冲突标记与半截行应被丢弃")
        lines = history.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertNotIn("<<<<<<<", line)
            self.assertNotIn(">>>>>>>", line)
            json.loads(line)







class CliTest(unittest.TestCase):
    def test_defaults(self):
        args = mod.parse_args([])
        self.assertIsNone(args.history, "默认不写历史")
        self.assertEqual(args.deployment, "local")
        self.assertFalse(args.no_all_models, "默认全模型")
        self.assertIsNone(args.deployment_label)

    def test_new_flags(self):
        args = mod.parse_args(
            [
                "--history",
                "/tmp/history.jsonl",
                "--deployment",
                "tenshi",
                "--deployment-label",
                "Tenshi 备用",
                "--no-all-models",
            ]
        )
        self.assertEqual(args.history, "/tmp/history.jsonl")
        self.assertEqual(args.deployment, "tenshi")
        self.assertEqual(args.deployment_label, "Tenshi 备用")
        self.assertTrue(args.no_all_models)

    def test_invalid_deployment_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.parse_args(["--deployment", "nope"])

    def test_default_deployment_labels(self):
        self.assertIn("Calling", mod.deployment_label_for("calling"))
        self.assertIn("Tenshi", mod.deployment_label_for("tenshi"))
        self.assertIn("本地", mod.deployment_label_for("local"))

    def test_main_end_to_end_writes_schema4_and_history(self):
        target = _target(["claude-haiku-4-5"])
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            json_path = tmpdir / "provider_health.json"
            report_path = tmpdir / "latest.md"
            history_path = tmpdir / "history.jsonl"
            with _fake_network(), _fake_endpoints("anthropic", _ok_model_list(["claude-haiku-4-5"])), mock.patch.object(
                mod, "collect_targets", lambda providers: [target]
            ), contextlib.redirect_stdout(io.StringIO()) as out:
                rc = mod.main(
                    [
                        "--providers",
                        "claude",
                        "--json",
                        str(json_path),
                        "--report",
                        str(report_path),
                        "--history",
                        str(history_path),
                        "--deployment",
                        "tenshi",
                        "--deployment-label",
                        "Tenshi 备用运行时（Tenshi secrets）",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 4)
            self.assertEqual(payload["deployment"], "tenshi")
            self.assertEqual(payload["providers"]["claude"]["paths"]["Codeyu Base"]["models_ok"], 1)
            history_lines = history_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(history_lines), 1)
            self.assertEqual(json.loads(history_lines[0])["deployment"], "tenshi")
            self.assertIn("[探活]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
