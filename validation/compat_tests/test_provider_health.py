# SPDX-License-Identifier: AGPL-3.0-only


















from __future__ import annotations

import contextlib
import datetime as dt
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

import provider_health


def _write_health(path: pathlib.Path, providers: dict, *, age_hours: float = 0.0) -> None:
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at_utc": stamp.replace(microsecond=0).isoformat(),
                "generated_at_bj": (stamp + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
                "providers": providers,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class ProviderHealthGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "provider_health.json"
        self._orig_path = provider_health.HEALTH_PATH
        self._orig_enabled = provider_health.ENABLED
        provider_health.HEALTH_PATH = self.path
        provider_health.ENABLED = True
        provider_health.reset_cache()

    def tearDown(self):
        provider_health.HEALTH_PATH = self._orig_path
        provider_health.ENABLED = self._orig_enabled
        provider_health.reset_cache()
        self._tmp.cleanup()

    def _healthy(self, *, age_hours: float = 0.0) -> None:
        _write_health(
            self.path,
            {
                "claude": {
                    "enabled": True,
                    "paths": {
                        "Codeyu Base": {"ok": True, "verdict": "ok"},
                        "Official": {"ok": False, "verdict": "auth_denied"},
                    },
                },
                "gemini": {"enabled": False, "paths": {"Official": {"ok": False, "verdict": "dns_fail"}}},
                "gpt": {"enabled": True, "paths": {"OpenAI": {"ok": True, "verdict": "ok"}}},
            },
            age_hours=age_hours,
        )
        provider_health.reset_cache()


    def test_green_red_and_unknown(self):
        self._healthy()
        self.assertIs(provider_health.path_state("claude", "Codeyu Base"), True)
        self.assertIs(provider_health.path_state("claude", "Official"), False)

        self.assertIsNone(provider_health.path_state("claude", "CoderPlan"))
        self.assertIsNone(provider_health.path_state("grok", "Official"))

    def test_provider_enabled_reflects_category(self):
        self._healthy()
        self.assertTrue(provider_health.provider_enabled("claude"))
        self.assertFalse(provider_health.provider_enabled("gemini"))
        self.assertTrue(provider_health.provider_enabled("unknown-provider"))


    def test_filter_paths_drops_red(self):
        self._healthy()
        paths = [{"name": "Codeyu Base"}, {"name": "Official"}, {"name": "CoderPlan"}]
        kept = provider_health.filter_paths("claude", paths)
        self.assertEqual([p["name"] for p in kept], ["Codeyu Base", "CoderPlan"])

    def test_filter_paths_can_empty_the_list(self):
        self._healthy()
        self.assertEqual(provider_health.filter_paths("gemini", [{"name": "Official"}]), [])

    def test_key_or_blank_blanks_red_only(self):
        self._healthy()
        self.assertEqual(provider_health.key_or_blank("claude", "Official", "sk-real"), "")
        self.assertEqual(provider_health.key_or_blank("claude", "Codeyu Base", "sk-real"), "sk-real")
        self.assertEqual(provider_health.key_or_blank("claude", "没探过", "sk-real"), "sk-real")


    def test_missing_file_is_fail_open(self):
        provider_health.reset_cache()
        self.assertIsNone(provider_health.path_state("claude", "Official"))
        self.assertTrue(provider_health.provider_enabled("claude"))
        self.assertEqual(provider_health.filter_paths("claude", [{"name": "Official"}])[0]["name"], "Official")

    def test_broken_json_is_fail_open(self):
        self.path.write_text("{ not json", encoding="utf-8")
        provider_health.reset_cache()
        self.assertIsNone(provider_health.path_state("claude", "Official"))
        self.assertEqual(provider_health.key_or_blank("claude", "Official", "sk-x"), "sk-x")

    def test_stale_result_is_fail_open(self):
        self._healthy(age_hours=provider_health.MAX_AGE_HOURS + 1)
        self.assertIsNone(provider_health.path_state("claude", "Official"))
        self.assertEqual(provider_health.filter_paths("claude", [{"name": "Official"}])[0]["name"], "Official")

    def test_disabled_by_env(self):
        self._healthy()
        provider_health.ENABLED = False
        provider_health.reset_cache()
        self.assertIsNone(provider_health.path_state("claude", "Official"))
        self.assertEqual(provider_health.filter_paths("claude", [{"name": "Official"}])[0]["name"], "Official")


    def test_describe_mentions_green_and_red(self):
        self._healthy()
        text = provider_health.describe()
        self.assertIn("claude", text)
        self.assertIn("Official", text)
        self.assertIn("Codeyu Base", text)

    def test_describe_without_data(self):
        provider_health.reset_cache()
        self.assertIn("照常调用", provider_health.describe())


class ProviderHealthModelLevelTest(unittest.TestCase):
    """「通路 × 模型」级 ban（2026-10-29）：模型不可用不再报黄照跑。

    对应 2026-10-29 事故：claude-sonnet-5-5 在代理账号组里根本不存在，
    每轮回复仍先在 Base 上撞 404、再在 Prime 上拿空响应，然后才降级。
    逐模型探活给出 models.<model>.ok=false 后，运行时必须跳过该组合。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "provider_health.json"
        self._orig_path = provider_health.HEALTH_PATH
        self._orig_enabled = provider_health.ENABLED
        provider_health.HEALTH_PATH = self.path
        provider_health.ENABLED = True
        provider_health.reset_cache()

    def tearDown(self):
        provider_health.HEALTH_PATH = self._orig_path
        provider_health.ENABLED = self._orig_enabled
        provider_health.reset_cache()
        self._tmp.cleanup()

    def _with_models(self, *, age_hours: float = 0.0) -> None:
        _write_health(
            self.path,
            {
                "claude": {
                    "enabled": True,
                    "paths": {
                        "Codeyu Base": {
                            "ok": True,
                            "verdict": "ok",
                            "models": {
                                "claude-opus-5": {"ok": True, "verdict": "ok", "severity": "green"},
                                "claude-sonnet-5-5": {
                                    "ok": False,
                                    "verdict": "model_not_found",
                                    "severity": "red",
                                },
                                "claude-haiku-5-5": {"ok": None, "verdict": "unknown", "severity": "unknown"},
                            },
                        },

                        "CoderPlan": {"ok": True, "verdict": "ok"},
                    },
                },
            },
            age_hours=age_hours,
        )
        provider_health.reset_cache()

    def test_model_path_state_three_states(self):
        self._with_models()
        self.assertIs(provider_health.model_path_state("claude", "Codeyu Base", "claude-sonnet-5-5"), False)
        self.assertIs(provider_health.model_path_state("claude", "Codeyu Base", "claude-opus-5"), True)

        self.assertIsNone(provider_health.model_path_state("claude", "Codeyu Base", "claude-haiku-5-5"))

        self.assertIsNone(provider_health.model_path_state("claude", "Codeyu Base", "claude-opus-5-5"))
        self.assertIsNone(provider_health.model_path_state("claude", "CoderPlan", "claude-sonnet-5-5"))
        self.assertIsNone(provider_health.model_path_state("claude", "CoderPlan", None))

    def test_filter_paths_with_model_drops_only_banned_combo(self):
        self._with_models()
        paths = [{"name": "Codeyu Base"}, {"name": "CoderPlan"}]
        kept = provider_health.filter_paths("claude", paths, model="claude-sonnet-5-5")
        self.assertEqual([p["name"] for p in kept], ["CoderPlan"])

        kept_opus = provider_health.filter_paths("claude", [dict(p) for p in paths], model="claude-opus-5")
        self.assertEqual([p["name"] for p in kept_opus], ["Codeyu Base", "CoderPlan"])

    def test_filter_paths_without_model_keeps_banned_model_paths(self):
        self._with_models()
        paths = [{"name": "Codeyu Base"}, {"name": "CoderPlan"}]
        kept = provider_health.filter_paths("claude", paths)
        self.assertEqual([p["name"] for p in kept], ["Codeyu Base", "CoderPlan"])

    def test_model_level_stale_and_missing_are_fail_open(self):
        self._with_models(age_hours=provider_health.MAX_AGE_HOURS + 1)
        self.assertIsNone(provider_health.model_path_state("claude", "Codeyu Base", "claude-sonnet-5-5"))
        self.assertEqual(
            [p["name"] for p in provider_health.filter_paths("claude", [{"name": "Codeyu Base"}], model="claude-sonnet-5-5")],
            ["Codeyu Base"],
        )
        provider_health.reset_cache()
        self.path.unlink()
        provider_health.reset_cache()
        self.assertIsNone(provider_health.model_path_state("claude", "Codeyu Base", "claude-sonnet-5-5"))


class ProviderHealthWiringTest(unittest.TestCase):
    """确认运行时真的会按探活结果 ban —— 全红时撞上既有的「没有可用通路」抛错。"""

    @classmethod
    def setUpClass(cls):
        try:
            import tenshi_reply
        except Exception as exc:
            raise unittest.SkipTest(f"tenshi_reply 不可用：{exc}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "provider_health.json"
        self._orig_path = provider_health.HEALTH_PATH
        provider_health.HEALTH_PATH = self.path
        provider_health.reset_cache()

    def tearDown(self):
        provider_health.HEALTH_PATH = self._orig_path
        provider_health.reset_cache()
        self._tmp.cleanup()

    def test_all_red_gemini_raises_existing_error(self):
        import tenshi_reply
        import tenshi_config



        orig_or_key = tenshi_config.OPENROUTER_API_KEY
        tenshi_config.OPENROUTER_API_KEY = "dummy-for-tests"
        try:
            from llm_routing import build_gemini_paths


            paths = build_gemini_paths(
                "official-key-for-test", tenshi_config.GEMINI_PROXIES
            )
            names = [str(p.get("name")) for p in paths]

            self.assertIn("OpenRouter", names)
            self.assertIn("Official", names)

            _write_health(
                self.path,
                {
                    "gemini": {
                        "enabled": False,
                        "paths": {
                            name: {"ok": False, "verdict": "dns_fail"} for name in names
                        },
                    }
                },
            )
        finally:
            tenshi_config.OPENROUTER_API_KEY = orig_or_key
        provider_health.reset_cache()

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

        self.assertIn("No API paths configured for gemini", str(ctx.exception))

    def test_openrouter_path_is_banned_by_health(self):
        """OpenRouter 也是通路之一：探活为红时同样被外层 ban（不会偷偷再调）。"""
        import tenshi_reply
        import tenshi_config

        orig_or_key = tenshi_config.OPENROUTER_API_KEY
        tenshi_config.OPENROUTER_API_KEY = "dummy-for-tests"
        try:
            from llm_routing import build_gemini_paths

            paths = build_gemini_paths(
                tenshi_reply.GEMINI_API_KEY, tenshi_config.GEMINI_PROXIES
            )
            or_path = next(p for p in paths if p.get("name") == "OpenRouter")
            self.assertIsNotNone(or_path)
            _write_health(
                self.path,
                {"gemini": {"enabled": True, "paths": {"OpenRouter": {"ok": False, "verdict": "auth_denied"}}}},
            )
        finally:
            tenshi_config.OPENROUTER_API_KEY = orig_or_key
        provider_health.reset_cache()

        kept = provider_health.filter_paths("gemini", [or_path])
        self.assertEqual(kept, [])

    def test_red_claude_paths_are_skipped_by_name(self):
        import tenshi_reply

        _write_health(
            self.path,
            {
                "claude": {
                    "enabled": True,
                    "paths": {"Official": {"ok": False, "verdict": "auth_denied"}},
                }
            },
        )
        provider_health.reset_cache()
        self.assertFalse(tenshi_reply.provider_health.path_state("claude", "Official") is not False)


class ProbeNonCallTest(unittest.TestCase):
    """探活**不得真实调用模型**：模型层只走官方非生成端点（模型列表 / count tokens）。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        script = pathlib.Path(ROOT) / "scripts" / "probe_provider_paths.py"
        spec = importlib.util.spec_from_file_location("probe_provider_paths", script)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @staticmethod
    def _target(**overrides) -> dict:
        target = {
            "provider": "gpt",
            "name": "OpenAI",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-test",
            "protocol": "openai",
            "model": "gpt-6-luna",
        }
        target.update(overrides)
        return target


    def test_script_contains_no_generation_endpoints(self):
        source = (pathlib.Path(ROOT) / "scripts" / "probe_provider_paths.py").read_text(encoding="utf-8")
        for forbidden in ("/chat/completions", ":generateContent", "max_completion_tokens"):
            self.assertNotIn(forbidden, source, f"探活脚本不应再出现生成式端点 {forbidden}")
        self.assertFalse(hasattr(self.mod, "CALLERS"))
        self.assertFalse(hasattr(self.mod, "classify_model"), "旧的真实调用判据应已删除")
        self.assertEqual(set(self.mod.COUNT_TOKEN_CALLERS), {"anthropic", "gemini"})

    def test_cli_no_longer_has_mode_or_attempts(self):
        args = self.mod.parse_args([])
        self.assertFalse(hasattr(args, "mode"))
        self.assertFalse(hasattr(args, "attempts"))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.mod.parse_args(["--mode", "full"])


    def test_extract_model_ids_supports_openai_and_gemini_shapes(self):
        mod = self.mod
        openai_body = json.dumps({"object": "list", "data": [{"id": "gpt-6-luna"}, {"id": "openai/gpt-6-luna"}]})
        self.assertEqual(mod._extract_model_ids(openai_body), ["gpt-6-luna", "openai/gpt-6-luna"])
        gemini_body = json.dumps({"models": [{"name": "models/gemini-3.8-flash"}]})
        self.assertEqual(mod._extract_model_ids(gemini_body), ["gemini-3.8-flash"])
        self.assertIsNone(mod._extract_model_ids("not json"))
        self.assertIsNone(mod._extract_model_ids("{}"))

    def test_match_model_supports_dated_snapshot_alias(self):
        mod = self.mod
        ids = ["claude-haiku-4-5-20251001", "claude-haiku-5-5"]
        self.assertEqual(mod._match_model(["claude-haiku-4-5"], ids), "claude-haiku-4-5-20251001")

        self.assertIsNone(mod._match_model(["claude-haiku-9"], ids))
        self.assertIsNone(mod._match_model(["claude-haiku-4-5-2026"], ids))

    def test_classify_model_list(self):
        mod = self.mod
        body = json.dumps({"data": [{"id": "gpt-6-luna"}]})
        self.assertEqual(mod.classify_model_list(self._target(), {"ok": True, "status": 200, "body": body})[0], "ok")

        self.assertEqual(
            mod.classify_model_list(
                self._target(), {"ok": True, "status": 200, "body": "{…", "json": {"data": [{"id": "gpt-6-luna"}]}}
            )[0],
            "ok",
        )
        miss = json.dumps({"data": [{"id": "gpt-6-luna-mini"}]})
        self.assertEqual(
            mod.classify_model_list(self._target(), {"ok": True, "status": 200, "body": miss})[0],
            "model_not_found",
        )
        self.assertEqual(mod.classify_model_list(self._target(), {"ok": True, "status": 200, "body": "{}"})[0], "invalid_probe_response")
        self.assertEqual(mod.classify_model_list(self._target(), {"ok": False, "status": 401, "body": ""})[0], "unknown")
        self.assertEqual(mod.classify_model_list(self._target(api_key=""), None)[0], "not_configured")


    @staticmethod
    def _result(status=None, ok=False, body="", error=None) -> dict:
        return {"status": status, "ok": ok, "body": body, "error": error, "latency_ms": 1.0}

    def test_classify_count_tokens(self):
        mod = self.mod
        target = self._target()
        self.assertEqual(mod.classify_count_tokens(target, self._result(200, True, '{"input_tokens": 1}'))[0], "ok")
        self.assertEqual(mod.classify_count_tokens(target, self._result(401))[0], "auth_denied")
        self.assertEqual(
            mod.classify_count_tokens(
                target, self._result(404, body='{"type": "not_found_error", "error": {"message": "model: nope"}}')
            )[0],
            "model_not_found",
        )
        self.assertEqual(mod.classify_count_tokens(target, self._result(404, body="<html>404 Not Found</html>"))[0], "probe_endpoint_unsupported")
        self.assertEqual(mod.classify_count_tokens(target, self._result(400))[0], "probe_request_rejected")
        self.assertEqual(mod.classify_count_tokens(target, self._result(429))[0], "rate_limited")
        quota = self._result(429, body='{"error":{"code":"insufficient_quota"}}')
        self.assertEqual(mod.classify_count_tokens(target, quota)[0], "quota_exhausted")
        invalid_count = self._result(200, True, '<html>OK</html>')
        self.assertEqual(mod.classify_count_tokens(target, invalid_count)[0], "invalid_probe_response")
        self.assertEqual(mod.classify_count_tokens(target, self._result(504))[0], "timeout")
        self.assertEqual(mod.classify_count_tokens(target, self._result(502))[0], "upstream_error")
        self.assertEqual(mod.classify_count_tokens(target, self._result(None, error="timeout"))[0], "timeout")


    def test_probe_target_without_key_does_not_conclude(self):
        item = self.mod.probe_target(self._target(api_key=""), timeout=0.5)
        self.assertEqual(item["checks"]["model"]["verdict"], "not_configured")
        self.assertIsNone(item["checks"]["model"]["ok"], "没配置 key 不下结论")
        self.assertNotIn("mode", item)

    def test_unreachable_path_keeps_model_layer_fail_open(self):


        def _dns_fail(host, port):
            return {"ok": False, "addresses": [], "error": "gaierror: Name or service not known", "latency_ms": 1.0}

        with mock.patch.object(self.mod, "probe_dns", _dns_fail):
            item = self.mod.probe_target(self._target(), timeout=0.5)
        self.assertEqual(item["checks"]["network"]["verdict"], "dns_fail")
        self.assertEqual(item["checks"]["model"]["verdict"], "unknown")
        self.assertIsNone(item["checks"]["model"]["ok"], "网络不通时模型层没结论，不额外下判断")

    def test_unknown_check_does_not_ban(self):
        """列模型 / count tokens 端点不存在（404）这类「没下结论」不能把通路判红。"""
        mod = self.mod
        check = mod._check("unknown", "该通路未实现列模型端点（404）")
        self.assertIsNone(check["ok"])
        self.assertEqual(check["severity"], "unknown")

    def test_red_check_bans_and_warn_does_not(self):
        mod = self.mod
        self.assertFalse(mod._check("auth_denied", "401")["ok"])

        self.assertFalse(mod._check("model_not_found", "404")["ok"], "模型不可用应 ban，不再黄档")
        self.assertTrue(mod._check("rate_limited", "429")["ok"], "限流属黄档，不 ban")

    def test_severity_mapping(self):
        mod = self.mod
        self.assertEqual(mod.severity_of("ok"), "green")
        self.assertEqual(mod.severity_of("model_not_found"), "red")
        self.assertEqual(mod.severity_of("rate_limited"), "warn")
        self.assertEqual(mod.severity_of("dns_fail"), "red")
        self.assertEqual(mod.severity_of("not_configured"), "not_configured")


if __name__ == "__main__":
    unittest.main()
