# SPDX-License-Identifier: AGPL-3.0-only
















from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "build_provider_health_dashboard",
    ROOT / "scripts" / "build_provider_health_dashboard.py",
)
assert _SPEC and _SPEC.loader
dashboard = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = dashboard
_SPEC.loader.exec_module(dashboard)


def _check(
    ok: bool | None,
    verdict: str | None,
    detail: str = "",
    *,
    status: object = 200,
    latency_ms: float | None = 12.0,
    method: str = "models_list",
    severity: str | None = None,
) -> dict:
    item = {
        "ok": ok,
        "verdict": verdict,
        "detail": detail,
        "method": method,
        "status": status,
        "latency_ms": latency_ms,
    }
    if severity:
        item["severity"] = severity
    return item


def _path(
    name: str,
    model: str,
    severity: str,
    verdict: str,
    verdict_text: str,
    models: dict,
    checks: dict,
    *,
    base_url: str = "https://proxy.example.com/v1",
    key_masked: str = "sk-ab…cd12",
    latency_ms: float | None = 120.5,
    status: object = 200,
    method: str | None = "models_list",
) -> tuple[str, dict]:
    entry = {
        "ok": severity in ("green", "warn"),
        "severity": severity,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "checks": checks,
        "model_check_method": method,
        "status": status,
        "latency_ms": latency_ms,
        "base_url": base_url,
        "model": model,
        "key_masked": key_masked,
        "models_total": len(models),
        "models_ok": sum(1 for item in models.values() if item.get("ok")),
        "models": models,
    }
    return name, entry


def _checks(network_ok: bool = True, auth_ok: bool = True, model_ok: bool = True) -> dict:
    return {
        "network": _check(network_ok, "ok" if network_ok else "tcp_fail", "TLS ok", latency_ms=5),
        "auth": _check(
            auth_ok, "ok" if auth_ok else "auth_denied", "/models 200" if auth_ok else "401",
            status=200 if auth_ok else 401, latency_ms=30,
        ),
        "model": _check(
            model_ok, "ok" if model_ok else "model_not_found",
            "模型在列表中" if model_ok else "探测模型不在该档模型列表里",
            status=200 if model_ok else 404, latency_ms=30,
        ),
    }


def _payload() -> dict:
    """schema 4 合成数据：覆盖绿 / 黄 / 红 / 未配置四种状态与多模型矩阵。"""
    return {
        "schema": 4,
        "generated_at_utc": "2026-10-28T12:30:00+00:00",
        "generated_at_bj": "2026-10-28 20:30:00",
        "environment": "容器",
        "deployment": "tenshi",
        "deployment_label": "Tenshi 备用运行时（Tenshi secrets）",
        "run_id": "40700000001",
        "probe_method": "official_non_call",
        "coverage": {"models_ok": 4, "models_total": 6},
        "providers": {
            "claude": {
                "enabled": True,
                "probe_model": "claude-haiku-4-5",
                "models_total": 3,
                "models_ok": 2,
                "paths": dict(
                    [
                        _path(
                            "Proxy 1 Base",
                            "claude-haiku-4-5",
                            "green",
                            "ok",
                            "三层检查均通过",
                            {
                                "claude-haiku-4-5": _check(True, "ok", "在列表中"),
                                "claude-sonnet-4-6": _check(True, "ok", "count tokens 通过", method="count_tokens"),
                            },
                            _checks(),
                        ),
                        _path(
                            "Proxy 2",
                            "claude-haiku-4-5",
                            "warn",
                            "model_not_found",
                            "模型不在该通路的模型列表里 <script>alert('x')</script> & <b>粗体</b>",
                            {
                                "claude-haiku-4-5": _check(
                                    False,
                                    "model_not_found",
                                    '404 model_not_found <img src=x onerror="alert(1)">',
                                    status=404,
                                    severity="warn",
                                ),
                            },
                            _checks(model_ok=False),
                            latency_ms=753.2,
                            status=404,
                        ),
                        _path(
                            "Official",
                            "claude-haiku-4-5",
                            "red",
                            "auth_denied",
                            "key 被拒（401 invalid_api_key）",
                            {"claude-haiku-4-5": _check(False, "auth_denied", "401 invalid x-api-key", status=401)},
                            _checks(auth_ok=False),
                            base_url="https://api.anthropic.com/v1",
                            key_masked="sk-ant…9f2c",
                            latency_ms=64.9,
                            status=401,
                        ),
                    ]
                ),
            },
            "gemini": {
                "enabled": False,
                "probe_model": "gemini-3.5-flash-lite",
                "models_total": 0,
                "models_ok": 0,
                "paths": dict(
                    [
                        _path(
                            "Gemini Proxy|旧",
                            "gemini-3.5-flash-lite",
                            "not_configured",
                            "not_configured",
                            "未配置 GEMINI_PROXY_KEY / GEMINI_PROXY_BASE_URL",
                            {},
                            {},
                            key_masked="",
                            base_url="",
                            latency_ms=None,
                            status=None,
                            method=None,
                        ),
                    ]
                ),
            },
            "deepseek|坏名字": {
                "enabled": True,
                "probe_model": "deepseek-flash",
                "models_total": 3,
                "models_ok": 2,
                "paths": dict(
                    [
                        _path(
                            "Official",
                            "deepseek-flash",
                            "green",
                            "ok",
                            "三层检查均通过",
                            {
                                "deepseek-flash": _check(True, "ok", "在列表中"),
                                "deepseek-reasoner": _check(True, "warn", "429 限流", status=429, severity="warn"),
                                "deepseek-coder": _check(True, "ok", "在列表中"),
                            },
                            _checks(),
                        ),
                    ]
                ),
            },
        },
    }


def _history_record(index: int, claude_severity: str, deepseek_severity: str) -> dict:
    hour = 10 + index
    return {
        "schema": 4,
        "generated_at_utc": f"2026-10-28T{hour % 24:02d}:00:00+00:00",
        "generated_at_bj": f"2026-10-28 {hour % 24:02d}:00:00",
        "environment": "容器",
        "deployment": "tenshi",
        "deployment_label": "Tenshi 备用运行时（Tenshi secrets）",
        "run_id": str(40700000000 + index),
        "probe_method": "official_non_call",
        "paths": {},
        "providers": {
            "claude": {
                "enabled": claude_severity != "red",
                "paths": {
                    "Proxy 1 Base": {
                        "ok": claude_severity != "red",
                        "severity": claude_severity,
                        "verdict": "ok" if claude_severity != "red" else "auth_denied",
                    },
                    "Official": {"ok": True, "severity": "green", "verdict": "ok"},
                },
            },
            "deepseek": {
                "enabled": deepseek_severity != "red",
                "paths": {
                    "Official": {
                        "ok": deepseek_severity != "red",
                        "severity": deepseek_severity,
                        "verdict": "ok" if deepseek_severity != "red" else "timeout",
                    }
                },
            },
        },
    }


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.json_path = self.tmp / "provider_health.json"
        self.history_path = self.tmp / "history.jsonl"
        self.html_path = self.tmp / "dashboard.html"
        self.md_path = self.tmp / "dashboard.md"



    def run_cli(self) -> int:
        return dashboard.main(
            [
                "--json",
                str(self.json_path),
                "--history",
                str(self.history_path),
                "--out-html",
                str(self.html_path),
                "--out-md",
                str(self.md_path),
            ]
        )

    def render(self, payload: dict | None = None, history_lines: list[str] | None = None) -> tuple[str, str]:
        if payload is not None:
            self.json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if history_lines is not None:
            self.history_path.write_text("\n".join(history_lines) + "\n", encoding="utf-8")
        code = self.run_cli()
        self.assertEqual(code, 0, "main() 应始终返回 0（数据缺失也不失败）")
        self.assertTrue(self.html_path.exists(), "必须写出 dashboard.html")
        self.assertTrue(self.md_path.exists(), "必须写出 dashboard.md")
        html_text = self.html_path.read_text(encoding="utf-8")
        md_text = self.md_path.read_text(encoding="utf-8")
        return html_text, md_text



    def test_html_contains_all_key_sections(self) -> None:
        history = [_history_record(0, "green", "green"), _history_record(1, "warn", "green")]
        html_text, md_text = self.render(_payload(), [json.dumps(item, ensure_ascii=False) for item in history])


        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn('lang="zh-CN"', html_text)
        self.assertIn("Provider 探活面板", html_text)
        self.assertIn("Tenshi 备用运行时（Tenshi secrets）", html_text)
        self.assertIn("2026-10-28 20:30:00", html_text)
        self.assertIn("2026-10-28T12:30:00+00:00", html_text)
        self.assertIn("40700000001", html_text)
        self.assertIn("不真实调用模型", html_text)


        for section_id in ("top", "overview", "matrix", "paths", "history", "legend"):
            self.assertIn(f'id="{section_id}"', html_text)


        self.assertIn("整体结论", html_text)
        self.assertIn("全模型覆盖", html_text)
        self.assertIn("4 / 6", html_text)
        self.assertIn("存在 1 条红通路", html_text)


        self.assertIn("当前矩阵：通路 × 模型", html_text)
        self.assertIn("claude-sonnet-4-6", html_text)
        self.assertIn("deepseek-reasoner", html_text)
        self.assertIn("Proxy 1 Base", html_text)
        self.assertIn("Gemini Proxy|旧", html_text)


        self.assertIn("逐通路细节", html_text)
        self.assertIn("网络", html_text)
        self.assertIn("鉴权", html_text)
        self.assertIn("https://api.anthropic.com/v1", html_text)
        self.assertIn("sk-ant…9f2c", html_text)
        self.assertIn("753.2 ms", html_text)

        self.assertNotIn("sk-ant-full-secret", html_text)


        self.assertIn("历史趋势", html_text)
        self.assertIn("<svg", html_text)
        self.assertIn("<rect", html_text)
        self.assertIn("可用率", html_text)
        self.assertIn("100%", html_text)


        self.assertIn("图例与说明", html_text)
        self.assertIn("fail-open", html_text)


        self.assertIn("# Provider 探活面板", md_text)
        self.assertIn("## 总览", md_text)
        self.assertIn("## 通路 × 模型矩阵", md_text)
        self.assertIn("## 最近历史", md_text)
        self.assertIn("| 类别 | 通路数 |", md_text)
        self.assertIn("claude-haiku-4-5", md_text)
        self.assertIn("✅", md_text)
        self.assertIn("⚠️", md_text)
        self.assertIn("❌", md_text)

    def test_output_is_self_contained(self) -> None:
        html_text, md_text = self.render(
            _payload(), [json.dumps(_history_record(0, "green", "green"), ensure_ascii=False)]
        )

        for forbidden in ("<link", "<script", "<iframe", "<img", "@import", "url(", "fonts.googleapis"):
            self.assertNotIn(forbidden, html_text, f"HTML 里不应出现外部资源引用：{forbidden}")
        self.assertIn("<style>", html_text)
        self.assertIn("</style>", html_text)
        self.assertIn("<svg", html_text)

    def test_html_escapes_network_text(self) -> None:
        html_text, _ = self.render(_payload(), [])

        self.assertNotIn("<script>alert", html_text)
        self.assertNotIn("<img src=x", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertIn("&lt;img src=x onerror=", html_text)
        self.assertIn("&amp; &lt;b&gt;粗体&lt;/b&gt;", html_text)

        self.assertNotIn("<b>粗体</b>", html_text)

    def test_markdown_escapes_table_pipes(self) -> None:
        _, md_text = self.render(_payload(), [])

        self.assertIn("deepseek\\|坏名字", md_text)
        self.assertIn("Gemini Proxy\\|旧", md_text)
        for line in md_text.splitlines():
            if line.startswith("|"):
                self.assertTrue(line.rstrip().endswith("|"), f"markdown 表格行未闭合：{line}")

    def test_deterministic_output(self) -> None:
        history = [json.dumps(_history_record(i, "green", "warn"), ensure_ascii=False) for i in range(3)]
        self.render(_payload(), history)
        first_html = self.html_path.read_bytes()
        first_md = self.md_path.read_bytes()
        self.render(_payload(), history)
        self.assertEqual(first_html, self.html_path.read_bytes(), "同一输入两次生成的 HTML 必须一致")
        self.assertEqual(first_md, self.md_path.read_bytes(), "同一输入两次生成的 MD 必须一致")



    def test_missing_json_still_renders(self) -> None:

        html_text, md_text = self.render()
        self.assertIn("暂无数据", html_text)
        self.assertIn("暂无历史数据", html_text)
        self.assertIn("暂无数据", md_text)
        self.assertIn("未找到探活 JSON", html_text)

    def test_empty_providers_still_renders(self) -> None:
        html_text, md_text = self.render({"schema": 4, "providers": {}}, ["not json"])
        self.assertIn("暂无数据", html_text)
        self.assertIn("暂无数据", md_text)
        self.assertIn("id=\"matrix\"", html_text)

    def test_bad_history_lines_are_skipped(self) -> None:
        lines = [
            json.dumps(_history_record(0, "green", "green"), ensure_ascii=False),
            '{"broken": ',
            "",
            "not json at all",
            json.dumps(_history_record(1, "red", "green"), ensure_ascii=False),
        ]
        html_text, md_text = self.render(_payload(), lines)
        self.assertIn("历史文件有 2 行无法解析", html_text)
        self.assertIn("历史文件有 2 行无法解析", md_text)
        self.assertIn("历史趋势", html_text)
        self.assertIn("<svg", html_text)

    def test_history_with_flat_paths_and_summary_providers(self) -> None:


        record = {
            "generated_at_utc": "2026-10-28T13:00:00+00:00",
            "generated_at_bj": "2026-10-28 21:00:00",
            "environment": "容器",
            "deployment": "tenshi",
            "deployment_label": "Tenshi 备用运行时（Tenshi secrets）",
            "run_id": "40700999999",
            "probe_method": "official_non_call",
            "paths": {
                "claude/Proxy 1 Base": {"ok": True, "severity": "green", "verdict": "ok"},
                "claude/Official": {"ok": False, "severity": "red", "verdict": "auth_denied"},
                "deepseek/Official": {
                    "ok": True,
                    "severity": "warn",
                    "verdict": "model_not_found",
                    "models_ok": 2,
                    "models_total": 3,
                },
            },
            "providers": {
                "claude": {"ok": 1, "total": 2, "models_ok": 1, "models_total": 3},
                "deepseek": {"ok": 1, "total": 1, "models_ok": 2, "models_total": 3},
            },
        }
        html_text, md_text = self.render(_payload(), [json.dumps(record, ensure_ascii=False)])

        self.assertIn("<svg", html_text)
        self.assertIn("Proxy 1 Base", html_text)
        self.assertIn("deepseek", md_text)
        self.assertIn("❌", md_text)
        self.assertIn("⚠️", md_text)

    def test_legacy_schema3_data_tolerated(self) -> None:

        legacy = {
            "schema": 3,
            "generated_at_utc": "2026-10-28T11:58:14+00:00",
            "generated_at_bj": "2026-10-28 19:58:14",
            "environment": "容器",
            "run_id": "40682033724",
            "probe_method": "official_non_call",
            "providers": {
                "claude": {
                    "enabled": True,
                    "probe_model": "claude-haiku-4-5",
                    "paths": dict(
                        [
                            _path(
                                "Official",
                                "claude-haiku-4-5",
                                "green",
                                "ok",
                                "三层检查均通过",
                                {},
                                _checks(),
                            )
                        ]
                    ),
                }
            },
        }
        html_text, md_text = self.render(legacy, [])
        self.assertIn("schema 3", html_text)
        self.assertIn("该数据版本没有模型维度", html_text)
        self.assertIn("全模型覆盖：—", md_text)
        self.assertIn("claude-haiku-4-5", html_text)

    def test_unknown_conclusion_is_grey_not_red(self) -> None:


        payload = _payload()
        path = payload["providers"]["deepseek|坏名字"]["paths"]["Official"]
        path["models"]["deepseek-coder"] = {
            "ok": None,
            "verdict": "unknown",
            "severity": "unknown",
            "detail": "代理不实现列模型端点，未下结论",
            "method": "none",
            "status": None,
            "latency_ms": None,
        }
        path["checks"]["model"] = {
            "ok": None,
            "verdict": "unknown",
            "severity": "unknown",
            "verdict_text": "代理不实现列模型端点，未下结论",
            "latency_ms": None,
            "status": None,
        }
        html_text, _ = self.render(payload, [])
        self.assertIn("❓ 未判定", html_text)

        self.assertIn("代理不实现列模型端点，未下结论", html_text)
        self.assertIn("sev-grey", html_text)

    def test_checks_use_verdict_text_field(self) -> None:

        payload = _payload()
        payload["providers"]["claude"]["paths"]["Proxy 1 Base"]["checks"]["network"] = {
            "ok": True,
            "verdict": "ok",
            "severity": "green",
            "verdict_text": "TLS1.3 · 证书链完整 · DNS 12ms",
            "latency_ms": 12,
            "status": 200,
        }
        html_text, _ = self.render(payload, [])
        self.assertIn("TLS1.3 · 证书链完整 · DNS 12ms", html_text)

    def test_coverage_dict_form_from_probe(self) -> None:


        payload = _payload()
        payload["coverage"] = {"claude": 3, "gemini": 0, "deepseek|坏名字": 3}
        html_text, md_text = self.render(payload, [])
        self.assertIn("4 / 6", html_text)
        self.assertIn("全模型覆盖：**4 / 6**", md_text)

    def test_history_availability_counts_warn_as_available(self) -> None:

        records = []
        for i, severity in enumerate(("green", "warn", "red")):
            records.append(
                json.dumps(
                    {
                        "generated_at_utc": f"2026-10-28T1{i}:00:00+00:00",
                        "generated_at_bj": f"2026-10-28 1{i}:00:00",
                        "environment": "容器",
                        "paths": {"claude/Proxy 1 Base": {"ok": severity != "red", "severity": severity, "verdict": "ok"}},
                        "providers": {"claude": {"ok": 1, "total": 1}},
                    },
                    ensure_ascii=False,
                )
            )
        html_text, md_text = self.render(_payload(), records)
        self.assertIn("67%", html_text)
        self.assertIn("67%", md_text)

        self.assertIn("可用 2/3 次", html_text)

    def test_history_limit_caps_at_60(self) -> None:
        lines = [
            json.dumps(_history_record(i, "green", "green"), ensure_ascii=False) for i in range(70)
        ]
        self.history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        records, note = dashboard.load_history(self.history_path)
        self.assertEqual(note, "")
        self.assertEqual(len(records), dashboard.HISTORY_LIMIT)
        self.assertEqual(len(records), 60)


if __name__ == "__main__":
    unittest.main()
