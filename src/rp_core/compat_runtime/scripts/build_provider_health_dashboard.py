# SPDX-License-Identifier: AGPL-3.0-only






























































from __future__ import annotations

import argparse
import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__import__("os").environ["RP_RUNTIME_ROOT"])
DEFAULT_JSON = ROOT / "data" / "provider_health.json"
DEFAULT_HISTORY = ROOT / "reports" / "provider_health" / "history.jsonl"
DEFAULT_HTML = ROOT / "reports" / "provider_health" / "dashboard.html"
DEFAULT_MD = ROOT / "reports" / "provider_health" / "dashboard.md"

SCHEMA_EXPECTED = 4
HISTORY_LIMIT = 60
MD_HISTORY_LIMIT = 12

SEVERITIES = ("green", "warn", "red", "unknown", "not_configured")
SEV_SET = set(SEVERITIES)





WARN_VERDICTS = {"rate_limited", "tls_cert_expiring", "key_budget_exhausted"}

SEV_EMOJI = {"green": "✅", "warn": "⚠️", "red": "❌", "unknown": "·", "not_configured": "➖", "missing": "—"}
SEV_SHORT = {"green": "绿", "warn": "黄", "red": "红", "unknown": "未判定", "not_configured": "未配置", "missing": "无数据"}
SEV_CSS = {
    "green": "sev-green",
    "warn": "sev-warn",
    "red": "sev-red",
    "unknown": "sev-grey",
    "not_configured": "sev-grey",
    "missing": "sev-missing",
}

SEV_FILL = {
    "green": "#2da44e",
    "warn": "#d4a72c",
    "red": "#cf222e",
    "unknown": "#afb8c1",
    "not_configured": "#afb8c1",
    "missing": "#eaeef2",
}
SEV_LABEL = {
    "green": "绿 · 照常调用",
    "warn": "黄 · 照常调用（不 ban）",
    "red": "红 · 运行时最外层 ban 掉",
    "unknown": "未判定 · 照常调用",
    "not_configured": "未配置 · 跳过",
    "missing": "本次无记录",
}

VERDICT_LABELS = {
    "ok": "✅ 可用",
    "not_configured": "➖ 未配置",
    "dns_fail": "❌ DNS 失败",
    "tcp_fail": "❌ 连接失败",
    "tls_fail": "❌ TLS 失败",
    "tls_cert_invalid": "⚠️ 证书不受信",
    "tls_cert_expiring": "⚠️ 证书即将到期",
    "auth_denied": "❌ key 被拒",
    "blocked": "❌ 被拦截",
    "model_not_found": "❌ 模型不可用（模型级 ban）",
    "model_method_unsupported": "❌ 模型不支持所需接口",
    "rate_limited": "⚠️ 限流",
    "quota_exhausted": "❌ 额度问题",
    "key_budget_exhausted": "⚠️ 密钥预算已用尽",
    "bad_request": "❌ 请求被拒",
    "upstream_error": "❌ 上游错误",
    "timeout": "❌ 超时",
    "http_error": "❌ 请求异常",
    "probe_endpoint_unsupported": "❓ 探活端点不支持",
    "probe_request_rejected": "❓ 探活请求被拒",
    "invalid_probe_response": "❓ 探活响应格式异常",
    "empty_model_list": "❓ 模型列表为空",
    "incomplete_model_list": "❓ 模型列表未列全",
    "unexpected_redirect": "❓ 探活端点发生重定向",
    "unknown": "❓ 未判定",
}

PROBE_METHOD_LABELS = {
    "official_non_call": "不真实调用模型（官方非生成端点：DNS/TCP/TLS → 鉴权 → 模型列表 / 详情 / count tokens / 账户状态）",
}
MODEL_METHOD_LABELS = {
    "models_list": "模型列表",
    "count_tokens": "count tokens",
    "model_detail": "模型详情",
    "countTokens": "count tokens",
    "none": "未校验",
}
DEPLOYMENT_LABELS = {
    "tenshi": "Tenshi 备用运行时（Tenshi secrets）",
    "calling": "Calling 运行时（Calling secrets）",
}







def esc(value: object) -> str:
    """HTML 转义（属性与文本都用它）：来自网络响应的文本必须过这一层。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _md_cell(value: object) -> str:
    """Markdown 表格单元格转义：竖线 / 换行会把表格撑坏。"""
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return text.replace("|", "\\|")


def _md_code(value: object) -> str:
    text = _md_cell(value)
    if not text:
        return "—"
    if "`" in text:
        return text
    return f"`{text}`"


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _num(value: object) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _first_text(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def verdict_label(verdict: str) -> str:
    if not verdict:
        return "—"
    return VERDICT_LABELS.get(verdict, verdict)


def check_severity(checks: object) -> str:
    """单个检查项（network/auth/model）的严重度。

    注意 probe 的 `severity` 里有一个 `unknown`（没下结论，fail-open 不 ban），
    它不等于红：展示成灰色、结论写「❓ 未判定」，绝不能把好通路画成红的。
    """
    check = _dict(checks)
    explicit = _text(check.get("severity")).lower()
    if explicit in SEV_SET:
        return explicit
    verdict = _text(check.get("verdict")).lower()
    if verdict == "not_configured":
        return "not_configured"
    if check.get("ok") is None:
        return "not_configured"
    if check.get("ok") is True:
        return "warn" if verdict in WARN_VERDICTS else "green"
    return "warn" if verdict in WARN_VERDICTS else "red"


def path_severity(path: object) -> str:
    """整条通路的严重度。字段缺失时按 verdict / ok 推导，保证旧数据也能着色。"""
    item = _dict(path)
    explicit = _text(item.get("severity")).lower()
    if (explicit == "green" or not explicit and item.get("ok") is True) and (
        (_num(item.get("models_total")) or 0) > 0 and item.get("models_ok") == 0
    ):
        return "unknown"
    if explicit in SEV_SET:
        return explicit
    verdict = _text(item.get("verdict")).lower()
    if verdict == "not_configured":
        return "not_configured"
    if item.get("ok") is True:
        return "green"
    if verdict in WARN_VERDICTS:
        return "warn"
    if item.get("ok") is False or verdict:
        return "red"
    return "not_configured"


def model_severity(model: object) -> str:
    """逐模型格的严重度：没结论（unknown / not_configured）→ 灰，不误判成红。"""
    item = _dict(model)
    explicit = _text(item.get("severity")).lower()
    if explicit in SEV_SET:
        return explicit
    verdict = _text(item.get("verdict")).lower()
    if item.get("ok") is None:
        return "not_configured"
    if item.get("ok") is True:
        return "warn" if verdict in WARN_VERDICTS else "green"
    return "warn" if verdict in WARN_VERDICTS else "red"


def method_label(raw: str) -> str:
    if not raw:
        return "—"
    return MODEL_METHOD_LABELS.get(raw, raw)







def _model_rows(path: dict) -> list[dict]:
    """通路下的模型维度。schema 4 用 `models` 字典；旧数据退回单模型。"""
    models = _dict(path.get("models"))
    fallback = path_severity(path)
    rows: list[dict] = []
    for model_id, raw in models.items():
        item = _dict(raw)
        rows.append(
            {
                "id": _text(model_id) or "(未知模型)",
                "severity": model_severity(item),
                "verdict": _text(item.get("verdict")).lower() or _text(path.get("verdict")).lower(),
                "detail": _first_text(item.get("detail"), item.get("verdict_text")),
                "method": _first_text(item.get("method"), path.get("model_check_method")),
                "status": item.get("status"),
                "latency_ms": _num(item.get("latency_ms")),
            }
        )
    if rows:
        return rows
    model = _text(path.get("model"))
    if not model:
        return []
    return [
        {
            "id": model,
            "severity": fallback,
            "verdict": _text(path.get("verdict")).lower(),
            "detail": _text(path.get("verdict_text")),
            "method": _text(path.get("model_check_method")),
            "status": path.get("status"),
            "latency_ms": _num(path.get("latency_ms")),
        }
    ]


def _path_rows(provider_entry: dict) -> list[dict]:
    rows: list[dict] = []
    for name, raw in _dict(provider_entry.get("paths")).items():
        item = _dict(raw)
        checks = _dict(item.get("checks"))
        rows.append(
            {
                "name": _text(name) or "(未命名通路)",
                "ok": item.get("ok"),
                "severity": path_severity(item),
                "verdict": _text(item.get("verdict")).lower(),
                "verdict_text": _text(item.get("verdict_text")),
                "checks": {key: _dict(checks.get(key)) for key in ("network", "auth", "model", "account")},
                "model_check_method": _text(item.get("model_check_method")),
                "status": item.get("status"),
                "latency_ms": _num(item.get("latency_ms")),
                "base_url": _text(item.get("base_url")),
                "model": _text(item.get("model")),
                "key_masked": _text(item.get("key_masked")),
                "models": _model_rows(item),
                "models_total": _num(item.get("models_total")),
                "models_ok": _num(item.get("models_ok")),
            }
        )
    return rows


def _resolve_deployment_label(payload: dict) -> str:
    label = _text(payload.get("deployment_label"))
    if label:
        return label
    deployment = _text(payload.get("deployment")).lower()
    if deployment in DEPLOYMENT_LABELS:
        return DEPLOYMENT_LABELS[deployment]
    if not payload:
        return ""
    return "未标注部署"


def _resolve_probe_method(payload: dict) -> str:
    raw = _text(payload.get("probe_method"))
    if not raw:
        return ""
    return PROBE_METHOD_LABELS.get(raw, raw)


def _derive_coverage(providers: list[dict]) -> tuple[int, int]:
    """按 provider 聚合模型覆盖：优先用显式 models_ok/models_total，缺失时按
    「出现过的模型 id」去重推导（同一模型多条通路只算一次）。"""
    ok = 0
    total = 0
    for provider in providers:
        explicit_total = provider.get("models_total")
        explicit_ok = provider.get("models_ok")
        if isinstance(explicit_total, int) and isinstance(explicit_ok, int):
            total += explicit_total
            ok += explicit_ok
            continue
        seen: dict[str, bool] = {}
        for path in provider["paths"]:
            for model in path["models"]:
                good = model["severity"] in ("green", "warn")
                seen[model["id"]] = seen.get(model["id"], False) or good
        total += len(seen)
        ok += sum(1 for good in seen.values() if good)
    return ok, total


def _providers_from_flat_paths(paths: dict) -> dict:
    """history 行的兼容层：契约里除 `providers` 外还可能只有顶层 `paths`（扁平的
    「provider/通路」字典）。从它反推出 providers 结构，避免历史趋势整块变空。"""
    grouped: dict[str, dict] = {}
    for raw_key, raw_path in _dict(paths).items():
        item = _dict(raw_path)
        key = _text(raw_key)
        provider = _first_text(item.get("provider"))
        name = _first_text(item.get("name"))
        if not provider and "/" in key:
            provider = key.split("/", 1)[0]
        if not name and "/" in key:
            name = key.split("/", 1)[1]
        provider = provider or "(未知类别)"
        name = name or key or "(未命名通路)"
        entry = grouped.setdefault(
            provider,
            {"enabled": False, "probe_model": _text(item.get("model")), "paths": {}},
        )
        entry["paths"][name] = item
        if item.get("ok") is True:
            entry["enabled"] = True
    return grouped


def normalize_payload(payload: object) -> dict:
    """把 JSON（或 history 的一行）规整成渲染层好用的结构；全程容错。"""
    data = _dict(payload)
    providers: list[dict] = []
    has_model_dimension = False
    raw_providers = _dict(data.get("providers"))


    has_detailed_providers = any(
        isinstance(entry, dict) and isinstance(entry.get("paths"), dict)
        for entry in raw_providers.values()
    )
    if not has_detailed_providers:
        flat_providers = _providers_from_flat_paths(_dict(data.get("paths")))
        if flat_providers:
            raw_providers = flat_providers
    for name, raw_entry in raw_providers.items():
        entry = _dict(raw_entry)
        paths = _path_rows(entry)



        raw_paths = _dict(entry.get("paths"))
        if any(_dict(raw_path.get("models")) for raw_path in raw_paths.values()):
            has_model_dimension = True
        if _num(entry.get("models_total")) is not None or _num(entry.get("models_ok")) is not None:
            has_model_dimension = True
        counts = {sev: sum(1 for path in paths if path["severity"] == sev) for sev in SEVERITIES}
        providers.append(
            {
                "name": _text(name) or "(未命名类别)",
                "enabled": entry.get("enabled"),
                "probe_model": _text(entry.get("probe_model")),
                "models_total": _num(entry.get("models_total")),
                "models_ok": _num(entry.get("models_ok")),
                "paths": paths,
                "counts": counts,
            }
        )

    counts = {sev: sum(provider["counts"][sev] for provider in providers) for sev in SEVERITIES}


    coverage = None
    raw_coverage = _dict(data.get("coverage"))
    cov_ok = _num(raw_coverage.get("models_ok"))
    cov_total = _num(raw_coverage.get("models_total"))
    if isinstance(cov_ok, int) and isinstance(cov_total, int):
        coverage = {"ok": cov_ok, "total": cov_total}
    elif has_model_dimension or any(
        isinstance(provider["models_total"], int) for provider in providers
    ):
        derived_ok, derived_total = _derive_coverage(providers)
        if derived_total:
            coverage = {"ok": derived_ok, "total": derived_total}

    if not has_model_dimension:
        for provider in providers:
            provider["models_total"] = None
            provider["models_ok"] = None

    return {
        "has_data": bool(data),
        "has_providers": bool(providers),
        "meta": {
            "schema": data.get("schema"),
            "generated_at_bj": _text(data.get("generated_at_bj")),
            "generated_at_utc": _text(data.get("generated_at_utc")),
            "environment": _text(data.get("environment")),
            "deployment": _text(data.get("deployment")),
            "deployment_label": _resolve_deployment_label(data),
            "run_id": _text(data.get("run_id")),
            "probe_method": _resolve_probe_method(data),
        },
        "providers": providers,
        "counts": counts,
        "coverage": coverage,
    }


def overall_conclusion(data: dict) -> tuple[str, str]:
    counts = data["counts"]
    total = sum(counts.values())
    if not data["has_providers"] or total == 0:
        return "➖", "暂无探活数据"
    red, warn, grey = counts["red"], counts["warn"], counts["not_configured"]
    if red:
        return "❌", f"存在 {red} 条红通路（运行时在最外层 ban 掉；其余通路照常调用）"
    if warn:
        return "⚠️", f"{warn} 条通路有告警（照常调用）"
    if counts["unknown"]:
        return "·", f"{counts['unknown']} 条通路尚未判定"
    if grey == total:
        return "➖", "全部通路未配置"
    return "✅", "全部通路正常（照常调用）"


def provider_conclusion(provider: dict) -> tuple[str, str]:
    counts = provider["counts"]
    if counts["red"]:
        return "❌", f"{counts['red']} 条红"
    if counts["warn"]:
        return "⚠️", f"{counts['warn']} 条黄"
    if counts["unknown"]:
        return "·", f"{counts['unknown']} 条未判定"
    if counts["green"]:
        return "✅", "全部可用"
    if provider["paths"]:
        return "➖", "未配置"
    return "—", "无通路"







def load_history(path: pathlib.Path) -> tuple[list[dict], str]:
    """读 history JSONL：坏行跳过、空文件/不存在返回空列表。"""
    if not path.exists():
        return [], ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"历史文件无法读取：{exc}"
    records: list[dict] = []
    bad = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue
        records.append(
            {
                "time": _first_text(record.get("generated_at_bj"), record.get("generated_at_utc")),
                "time_utc": _text(record.get("generated_at_utc")),
                "data": normalize_payload(record),
            }
        )
    if all(record["time_utc"] for record in records):
        records.sort(key=lambda record: record["time_utc"])
    records = records[-HISTORY_LIMIT:]
    note = f"历史文件有 {bad} 行无法解析，已跳过（其余 {len(records)} 次正常运行照常渲染）" if bad else ""
    return records, note


def _record_severity(record: dict, key: tuple[str, str]) -> str | None:
    provider_name, path_name = key
    for provider in record["data"]["providers"]:
        if provider["name"] != provider_name:
            continue
        for path in provider["paths"]:
            if path["name"] == path_name:
                return path["severity"]
    return None


def history_rows(records: list[dict]) -> list[dict]:
    """历史行：按首次出现的顺序（即 provider 顺序）列出每条通路的历史条带与可用率。"""
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        for provider in record["data"]["providers"]:
            for path in provider["paths"]:
                key = (provider["name"], path["name"])
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
    rows: list[dict] = []
    for key in keys:
        cells = [_record_severity(record, key) for record in records]
        observed = [cell for cell in cells if cell in ("green", "warn", "red")]
        greens = sum(1 for cell in observed if cell == "green")
        warns = sum(1 for cell in observed if cell == "warn")
        reds = sum(1 for cell in observed if cell == "red")

        usable = greens + warns
        rows.append(
            {
                "provider": key[0],
                "name": key[1],
                "cells": cells,
                "observed": len(observed),
                "greens": greens,
                "warns": warns,
                "reds": reds,
                "usable": usable,
                "availability": (usable / len(observed) * 100.0) if observed else None,
            }
        )
    return rows


def history_provider_names(records: list[dict]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for record in records:
        for provider in record["data"]["providers"]:
            if provider["name"] not in seen:
                seen.add(provider["name"])
                names.append(provider["name"])
    return names


def _record_provider_cell(record: dict, provider_name: str) -> tuple[str, str]:
    for provider in record["data"]["providers"]:
        if provider["name"] != provider_name:
            continue
        counts = provider["counts"]
        if counts["red"]:
            return "❌", f"红 {counts['red']}"
        if counts["warn"]:
            return "⚠️", f"黄 {counts['warn']}"
        if counts["unknown"]:
            return "·", f"未判定 {counts['unknown']}"
        if counts["green"]:
            return "✅", "全绿"
        if provider["paths"]:
            return "➖", "未配置"
        return "—", "无数据"
    return "—", "本期无记录"







_CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --border: #d1d9e0;
  --card: #f6f8fa; --card-border: #d1d9e0; --code-bg: #eff1f3;
  --green: #1a7f37; --green-bg: #dafbe1; --green-border: #4ac26b;
  --warn: #9a6700; --warn-bg: #fff8c5; --warn-border: #d4a72c;
  --red: #cf222e; --red-bg: #ffebe9; --red-border: #ff8182;
  --grey: #59636e; --grey-bg: #eaeef2; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --border: #3d444d;
    --card: #151b23; --card-border: #3d444d; --code-bg: #212830;
    --green: #3fb950; --green-bg: #0f2f1d; --green-border: #238636;
    --warn: #d29922; --warn-bg: #2e2410; --warn-border: #9e6a03;
    --red: #f85149; --red-bg: #3d1418; --red-border: #da3633;
    --grey: #9198a1; --grey-bg: #212830; --accent: #4493f8;
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; padding: 0 0 48px;
  background: var(--bg); color: var(--fg);
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
        "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}
main { max-width: 1240px; margin: 0 auto; padding: 0 20px; }
h1, h2, h3 { line-height: 1.3; }
h1 { font-size: 26px; margin: 0 0 6px; }
h2 {
  font-size: 19px; margin: 40px 0 14px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
}
h3 { font-size: 15px; margin: 0; }
code, .mono {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
               "Liberation Mono", monospace;
  font-size: 12.5px;
}
code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.page-head { padding: 28px 20px 4px; }
.head-inner { max-width: 1240px; margin: 0 auto; }
.subtitle { color: var(--muted); margin: 0 0 12px; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 16px; }
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
  border: 1px solid var(--card-border); background: var(--card); color: var(--fg);
}
.pill.green { color: var(--green); background: var(--green-bg); border-color: var(--green-border); }
.pill.warn  { color: var(--warn);  background: var(--warn-bg);  border-color: var(--warn-border); }
.pill.red   { color: var(--red);   background: var(--red-bg);   border-color: var(--red-border); }
.pill.grey  { color: var(--grey);  background: var(--grey-bg); }
.meta-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 2px 24px; margin: 0; padding: 14px 16px;
  border: 1px solid var(--card-border); border-radius: 10px; background: var(--card);
}
.meta-grid > div { display: flex; gap: 8px; padding: 2px 0; min-width: 0; }
.meta-grid dt { flex: 0 0 auto; color: var(--muted); font-size: 12.5px; }
.meta-grid dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.card {
  border: 1px solid var(--card-border); border-radius: 10px;
  background: var(--card); padding: 14px 16px;
}
.card h3 { color: var(--muted); font-size: 12.5px; font-weight: 600; letter-spacing: .04em; }
.card .big { font-size: 22px; font-weight: 700; margin: 6px 0 2px; display: flex; align-items: center; gap: 8px; }
.card .big.green { color: var(--green); }
.card .big.warn  { color: var(--warn); }
.card .big.red   { color: var(--red); }
.card .big.grey  { color: var(--grey); }
.card .note { color: var(--muted); font-size: 12.5px; }
.counts { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 8px; }
.count { display: flex; align-items: baseline; gap: 5px; font-size: 20px; font-weight: 700; }
.count span.label { font-size: 12px; font-weight: 500; color: var(--muted); }
.count.green { color: var(--green); }
.count.warn  { color: var(--warn); }
.count.red   { color: var(--red); }
.count.grey  { color: var(--grey); }
.bar { height: 8px; border-radius: 999px; background: var(--grey-bg); overflow: hidden; margin-top: 10px; }
.bar > i { display: block; height: 100%; background: var(--green); border-radius: 999px; }
.bar.warn > i { background: var(--warn); }
.bar.red > i { background: var(--red); }
table { border-collapse: collapse; width: 100%; }
.table-wrap { overflow-x: auto; border: 1px solid var(--card-border); border-radius: 10px; }
table.data { font-size: 12.5px; min-width: 100%; }
table.data th, table.data td {
  padding: 6px 9px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top;
}
table.data thead th { background: var(--card); font-weight: 600; white-space: nowrap; }
table.data tbody tr:last-child td, table.data tbody tr:last-child th { border-bottom: 0; }
table.data tbody tr:hover td, table.data tbody tr:hover th { background: var(--card); }
table.data th.rowhead { white-space: nowrap; font-weight: 600; }
.provider-card {
  border: 1px solid var(--card-border); border-radius: 10px;
  margin: 14px 0; overflow: hidden; background: var(--bg);
}
.provider-head {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 10px 14px; background: var(--card); border-bottom: 1px solid var(--card-border);
}
.provider-head .name { font-size: 15px; font-weight: 700; }
.provider-head .spacer { flex: 1 1 auto; }
table.matrix { width: 100%; }
table.matrix th, table.matrix td { border-bottom: 1px solid var(--border); text-align: center; }
table.matrix thead th {
  padding: 7px 8px; background: var(--card); font-size: 11.5px; font-weight: 600;
  color: var(--muted); max-width: 190px; overflow-wrap: anywhere;
}
table.matrix thead th.path-col, table.matrix td.path-col { text-align: left; }
table.matrix td { padding: 4px 6px; }
table.matrix td.path-col { padding: 6px 10px; white-space: nowrap; font-weight: 600; }
table.matrix tbody tr:last-child th, table.matrix tbody tr:last-child td { border-bottom: 0; }
.cell {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; height: 28px; border-radius: 6px; font-size: 14px;
  border: 1px solid transparent;
}
.sev-green  { background: var(--green-bg); border-color: var(--green-border); }
.sev-warn   { background: var(--warn-bg);  border-color: var(--warn-border); }
.sev-red    { background: var(--red-bg);   border-color: var(--red-border); }
.sev-grey   { background: var(--grey-bg);  border-color: var(--card-border); color: var(--grey); }
.sev-missing { background: transparent; border-color: var(--card-border); color: var(--muted); }
.badge {
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px;
  border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap;
  border: 1px solid transparent;
}
.badge.green { color: var(--green); background: var(--green-bg); border-color: var(--green-border); }
.badge.warn  { color: var(--warn);  background: var(--warn-bg);  border-color: var(--warn-border); }
.badge.red   { color: var(--red);   background: var(--red-bg);   border-color: var(--red-border); }
.badge.grey  { color: var(--grey);  background: var(--grey-bg);  border-color: var(--card-border); }
.path-card {
  border: 1px solid var(--card-border); border-radius: 10px;
  margin: 10px 0; background: var(--bg); overflow: hidden;
}
.path-card > summary {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 9px 14px; cursor: pointer; background: var(--card); list-style: none;
  font-size: 13.5px;
}
.path-card > summary::-webkit-details-marker { display: none; }
.path-card > summary::before { content: "▸"; color: var(--muted); font-size: 11px; }
.path-card[open] > summary::before { content: "▾"; }
.path-card > summary .path-name { font-weight: 700; }
.detail-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 10px 20px; padding: 12px 16px 14px;
}
.detail-grid .field { min-width: 0; }
.detail-grid .field.wide { grid-column: 1 / -1; }
.detail-grid .k { color: var(--muted); font-size: 12px; margin-bottom: 1px; }
.detail-grid .v { overflow-wrap: anywhere; word-break: break-word; }
.detail-grid .v .detail { color: var(--muted); font-size: 12.5px; }
.checks { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 8px 0 2px; }
.check-chip { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 5px; font-size: 12.5px; line-height: 1.45; }
.check-chip .detail { overflow-wrap: anywhere; }
.check-chip .tag { color: var(--muted); }
.legend-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 10px 24px; }
.legend-item { display: flex; align-items: flex-start; gap: 10px; }
.legend-item .cell { flex: 0 0 auto; }
.legend-item p { margin: 0; }
.legend-item p .k { font-weight: 700; }
.notes { border-left: 3px solid var(--warn-border); background: var(--warn-bg); color: var(--fg); padding: 10px 14px; border-radius: 0 8px 8px 0; margin: 0 0 18px; }
.notes ul { margin: 4px 0 0; padding-left: 20px; }
.note-strip { overflow-x: auto; padding: 4px 0; }
svg.strip { display: block; }
.history-meta { color: var(--muted); font-size: 12px; margin: 4px 0 10px; }
.empty {
  border: 1px dashed var(--card-border); border-radius: 10px; padding: 26px;
  text-align: center; color: var(--muted); background: var(--card); margin: 12px 0;
}
footer {
  max-width: 1240px; margin: 40px auto 0; padding: 16px 20px 0;
  border-top: 1px solid var(--border); color: var(--muted); font-size: 12px;
}
footer ul { margin: 6px 0 0; padding-left: 20px; }
@media print { body { background: #fff; } .path-card > summary::before { content: ""; } }
"""



BADGE_CSS = {
    "green": "green",
    "warn": "warn",
    "red": "red",
    "unknown": "grey",
    "not_configured": "grey",
    "missing": "grey",
    "grey": "grey",
}


def _badge(severity: str, label: str | None = None) -> str:
    css = BADGE_CSS.get(severity, "grey")
    text = label if label is not None else SEV_LABEL.get(severity, severity)
    return f'<span class="badge {css}">{esc(text)}</span>'


def _pill(severity: str, label: str) -> str:
    css = BADGE_CSS.get(severity, "grey")
    return f'<span class="pill {css}">{esc(label)}</span>'


def _count_html(counts: dict) -> str:
    parts = []
    for sev in SEVERITIES:
        css = BADGE_CSS[sev]
        parts.append(
            f'<span class="count {css}">{esc(SEV_EMOJI[sev])} {counts[sev]}'
            f'<span class="label">{esc(SEV_SHORT[sev])}</span></span>'
        )
    return '<div class="counts">' + "".join(parts) + "</div>"


def _coverage_html(data: dict) -> str:
    coverage = data["coverage"]
    if not coverage:
        return (
            '<div class="big grey">—</div>'
            '<div class="note">该数据版本没有模型维度（schema 4 才有「通路 × 模型」矩阵）</div>'
        )
    ok = coverage["ok"]
    total = coverage["total"]
    percent = (ok / total * 100.0) if total else 0.0
    css = "green" if percent >= 99.999 else ("warn" if percent >= 50 else "red")
    bar = f'<div class="bar"><i style="width:{percent:.1f}%"></i></div>'
    return (
        f'<div class="big {css}">{ok} / {total}<span class="small muted">个模型可用</span></div>'
        f'<div class="note">全模型覆盖 {percent:.1f}%（绿 + 黄；红模型运行时会被 ban）</div>{bar}'
    )


def _meta_html(data: dict) -> str:
    meta = data["meta"]
    rows = [
        ("部署", meta["deployment_label"] or "—"),
        ("生成时间（北京）", meta["generated_at_bj"] or "—"),
        ("生成时间（UTC）", meta["generated_at_utc"] or "—"),
        ("运行环境", meta["environment"] or "—"),
        ("run id", meta["run_id"] or "（本地运行）"),
        ("探活方式", meta["probe_method"] or "—"),
        ("数据版本", f"schema {meta['schema']}" if meta["schema"] is not None else "—"),
    ]
    items = "".join(
        f'<div><dt>{esc(key)}</dt><dd>{esc(value)}</dd></div>' for key, value in rows
    )
    return f'<dl class="meta-grid">{items}</dl>'


def _check_chip(tag: str, check: dict) -> str:
    """单个检查项（网络 / 鉴权 / 模型）的展示：结论 + 延迟 + HTTP 状态 + 说明。"""
    verdict = _text(_dict(check).get("verdict")).lower()
    detail = _first_text(_dict(check).get("detail"), _dict(check).get("verdict_text"))
    if not check or (not verdict and _dict(check).get("ok") is None):

        bits = [esc(tag), '<span class="muted">— 无结论</span>']
        if detail:
            bits.append(f'<span class="detail" title="{esc(detail)}">{esc(_truncate(detail, 90))}</span>')
        return f'<span class="check-chip">{" ".join(bits)}</span>'
    severity = check_severity(check)
    label = verdict_label(verdict)
    if label == "—":
        label = f"{SEV_EMOJI.get(severity, '—')} {SEV_LABEL.get(severity, severity)}"
    bits = [esc(tag), esc(label)]
    latency = _num(check.get("latency_ms"))
    if latency is not None:
        bits.append(f'<span class="muted">{esc(_fmt_ms(latency))}</span>')
    status = check.get("status")
    if status not in (None, ""):
        bits.append(f'<span class="muted">HTTP {esc(status)}</span>')
    detail = _first_text(check.get("detail"), check.get("verdict_text"))
    if detail:
        bits.append(f'<span class="detail" title="{esc(detail)}">{esc(_truncate(detail, 90))}</span>')
    return f'<span class="check-chip">{" ".join(bits)}</span>'


def _fmt_ms(value: object) -> str:
    number = _num(value)
    if number is None:
        return "—"
    if isinstance(number, float) and not number.is_integer():
        return f"{number:.1f} ms"
    return f"{int(number)} ms"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _cell_html(severity: str | None, title: str) -> str:
    sev = severity if severity in SEV_EMOJI else "missing"
    emoji = SEV_EMOJI[sev]
    return f'<span class="cell {SEV_CSS[sev]}" title="{esc(title)}">{esc(emoji)}</span>'


def _path_title(path: dict, model: dict | None = None) -> str:
    parts = []
    if model is not None:
        parts.append(f"模型 {model['id']}")
        parts.append(verdict_label(model["verdict"]) or SEV_LABEL[model["severity"]])
        if model.get("detail"):
            parts.append(_text(model["detail"]))
        if model.get("status") not in (None, ""):
            parts.append(f"HTTP {model['status']}")
        if _num(model.get("latency_ms")) is not None:
            parts.append(_fmt_ms(model["latency_ms"]))
        if model.get("method"):
            parts.append(f"校验方式 {method_label(_text(model['method']))}")
    else:
        parts.append(verdict_label(path["verdict"]) or SEV_LABEL[path["severity"]])
        if path["verdict_text"]:
            parts.append(path["verdict_text"])
        if path["status"] not in (None, ""):
            parts.append(f"HTTP {path['status']}")
        if path["latency_ms"] is not None:
            parts.append(_fmt_ms(path["latency_ms"]))
    return "\n".join(part for part in parts if part)


def _model_cell(path: dict, model_id: str, provider_name: str) -> str:
    model = next((item for item in path["models"] if item["id"] == model_id), None)
    if model is None:
        return _cell_html(None, f"{provider_name}/{path['name']} · 模型 {model_id}\n本次无该模型记录")
    title = f"{provider_name}/{path['name']} · {_path_title(path, model)}"
    return _cell_html(model["severity"], title)


def _provider_section_html(provider: dict) -> str:
    name = provider["name"]
    counts = provider["counts"]
    model_cols: dict[str, None] = {}
    for path in provider["paths"]:
        for model in path["models"]:
            model_cols.setdefault(model["id"], None)
    columns = list(model_cols.keys())

    head_bits = [
        f'<span class="name"><code>{esc(name)}</code></span>',
        _badge("green", f"绿 {counts['green']}") if counts["green"] else "",
        _badge("warn", f"黄 {counts['warn']}") if counts["warn"] else "",
        _badge("red", f"红 {counts['red']}") if counts["red"] else "",
        _badge("grey", f"未判定 {counts['unknown']}") if counts["unknown"] else "",
        _badge("grey", f"未配置 {counts['not_configured']}") if counts["not_configured"] else "",
        '<span class="spacer"></span>',
    ]
    if provider["models_total"] is not None and provider["models_ok"] is not None:
        head_bits.append(
            f'<span class="small muted">模型覆盖 {esc(provider["models_ok"])}/{esc(provider["models_total"])}</span>'
        )
    if provider["probe_model"]:
        head_bits.append(f'<span class="small muted">探测模型 <code>{esc(provider["probe_model"])}</code></span>')
    head = '<div class="provider-head">' + "".join(bit for bit in head_bits if bit) + "</div>"

    if not provider["paths"]:
        return (
            f'<section class="provider-card"><div>{head}'
            '<div class="empty">该类别本次没有通路数据。</div></div></section>'
        )

    header_cells = ['<th class="path-col">通路</th>']
    header_cells.extend(f'<th title="{esc(model_id)}">{esc(model_id)}</th>' for model_id in columns)
    header_cells.append("<th>结论</th><th>延迟</th>")
    body = []
    for path in provider["paths"]:
        cells = [
            f'<td class="path-col"><code>{esc(path["name"])}</code></td>',
        ]
        for model_id in columns:
            cells.append(f"<td>{_model_cell(path, model_id, name)}</td>")
        cells.append(
            f'<td><span title="{esc(_path_title(path))}">'
            f'{esc(verdict_label(path["verdict"]) or SEV_LABEL[path["severity"]])}</span></td>'
        )
        cells.append(f'<td class="mono">{esc(_fmt_ms(path["latency_ms"]))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    table = (
        '<div class="table-wrap"><table class="matrix"><thead><tr>'
        + "".join(header_cells)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )
    return f'<section class="provider-card">{head}{table}</section>'


def _matrix_html(data: dict) -> str:
    if not data["providers"]:
        return '<div class="empty">暂无数据：没有读到任何 provider（JSON 缺失 / 空 providers / 旧数据格式）。</div>'
    return "".join(_provider_section_html(provider) for provider in data["providers"])


def _path_detail_html(provider: dict, path: dict) -> str:
    checks = path["checks"]
    open_attr = "" if path["severity"] == "green" else " open"
    verdict_text_label = verdict_label(path["verdict"])
    if verdict_text_label == "—":
        verdict_text_label = f"{SEV_EMOJI[path['severity']]} {SEV_SHORT[path['severity']]}"
    verdict_cell = _badge(path["severity"], verdict_text_label)
    fields = [
        ("通路结论", f'{verdict_cell} <span class="detail">{esc(path["verdict_text"])}</span>'),
        ("网络", _check_chip("网络", checks["network"])),
        ("鉴权", _check_chip("鉴权", checks["auth"])),
        ("模型", _check_chip("模型", checks["model"])),
        *([("账户", _check_chip("账户", checks["account"]))] if checks["account"] else []),
        ("base_url", f'<code>{esc(path["base_url"] or "（未配置）")}</code>'),
        ("key（掩码）", f'<code>{esc(path["key_masked"] or "（未配置）")}</code>'),
        ("探测模型", f'<code>{esc(path["model"] or provider["probe_model"] or "—")}</code>'),
        ("延迟", f'<span class="mono">{esc(_fmt_ms(path["latency_ms"]))}</span>'),
        (
            "HTTP 状态",
            f'<span class="mono">{esc(path["status"])}</span>' if path["status"] not in (None, "") else "—",
        ),
        ("校验方式", esc(method_label(path["model_check_method"]))),
    ]
    rendered = []
    for key, value in fields:
        wide = " wide" if key == "通路结论" else ""
        rendered.append(
            f'<div class="field{wide}"><div class="k">{esc(key)}</div><div class="v">{value}</div></div>'
        )
    return (
        f'<details class="path-card"{open_attr}>'
        f'<summary><span class="path-name">{esc(provider["name"])} / {esc(path["name"])}</span>'
        f'{_badge(path["severity"], SEV_EMOJI[path["severity"]] + " " + SEV_SHORT[path["severity"]])}'
        f'<span class="small muted">{esc(_fmt_ms(path["latency_ms"]))}</span>'
        f'<span class="small muted">{esc(verdict_label(path["verdict"]))}</span>'
        "</summary>"
        f'<div class="detail-grid">{"".join(rendered)}</div>'
        "</details>"
    )


def _paths_html(data: dict) -> str:
    if not data["providers"]:
        return '<div class="empty">暂无数据：没有可展示的通路明细。</div>'
    blocks = []
    for provider in data["providers"]:
        if not provider["paths"]:
            continue
        blocks.append(
            f'<h3 style="margin:18px 0 4px"><code>{esc(provider["name"])}</code></h3>'
            + "".join(_path_detail_html(provider, path) for path in provider["paths"])
        )
    if not blocks:
        return '<div class="empty">暂无数据：所有类别都没有通路明细。</div>'
    return "".join(blocks)


def _svg_strip(cells: list[str | None], times: list[str]) -> str:
    """内联 SVG 热力条带：每个小方块 = 一次运行，红黄绿灰；title 里带时间与含义。"""
    cell_w, gap, height = 13, 2, 18
    width = max(1, len(cells)) * (cell_w + gap) - gap
    parts = [
        f'<svg class="strip" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="历史趋势条带">'
    ]
    for index, cell in enumerate(cells):
        cel = cell if cell in SEV_FILL else "missing"
        x = index * (cell_w + gap)
        time_label = times[index] if index < len(times) else ""
        title = " · ".join(part for part in (time_label, SEV_LABEL[cel]) if part)
        parts.append(
            f'<rect x="{x}" y="1" width="{cell_w}" height="{height - 2}" rx="2" fill="{SEV_FILL[cel]}">'
            f'<title>{esc(title)}</title></rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _history_html(records: list[dict]) -> str:
    if not records:
        return (
            '<div class="empty">暂无历史数据：<code>reports/provider_health/history.jsonl</code>'
            " 不存在或为空（第一次运行探活后才会出现趋势）。</div>"
        )
    rows = history_rows(records)
    times = [record["time"] or "—" for record in records]
    body = []
    for row in rows:
        availability = row["availability"]
        if availability is None:
            avail_html = '<span class="muted">—</span>'
            bar = ""
        else:
            css = "green" if availability >= 99.999 else ("warn" if availability >= 50 else "red")
            breakdown = f'绿 {row["greens"]} · 黄 {row["warns"]} · 红 {row["reds"]}'
            avail_html = (
                f'<span class="mono" title="{esc(breakdown)}">{availability:.0f}%</span>'
                f' <span class="small muted">(可用 {row["usable"]}/{row["observed"]} 次)</span>'
            )
            bar = (
                f'<div class="bar {css}" style="max-width:150px;margin-top:4px">'
                f'<i style="width:{availability:.1f}%"></i></div>'
            )
        body.append(
            "<tr>"
            f'<th class="rowhead"><code>{esc(row["provider"])}</code> / {esc(row["name"])}</th>'
            f'<td><div class="note-strip">{_svg_strip(row["cells"], times)}</div></td>'
            f"<td>{avail_html}{bar}</td>"
            "</tr>"
        )
    caption = (
        f'<p class="history-meta">共 {len(records)} 次记录（最多保留最近 {HISTORY_LIMIT} 次）'
        f" · 左侧 {esc(times[0])} · 右侧 {esc(times[-1])}"
        " · 灰色 = 该次运行没有这条通路的记录</p>"
    )
    table = (
        '<div class="table-wrap"><table class="data"><thead><tr>'
        "<th>通路</th><th>历史趋势（左旧 → 右新）</th><th>可用率（绿+黄 / 有结论的次数）</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )
    return caption + table


def _overview_table_html(data: dict) -> str:
    if not data["providers"]:
        return ""
    rows = []
    for provider in data["providers"]:
        counts = provider["counts"]
        emoji, text = provider_conclusion(provider)
        coverage = (
            f'{provider["models_ok"]}/{provider["models_total"]}'
            if provider["models_total"] is not None and provider["models_ok"] is not None
            else "—"
        )
        rows.append(
            "<tr>"
            f'<th class="rowhead"><code>{esc(provider["name"])}</code></th>'
            f'<td>{len(provider["paths"])}</td>'
            f'<td>{_count_html(counts)}</td>'
            f'<td class="mono">{esc(coverage)}</td>'
            f"<td>{esc(emoji)} {esc(text)}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="data"><thead><tr>'
        "<th>类别</th><th>通路数</th><th>通路状态（绿 / 黄 / 红 / 未判定 / 未配置）</th>"
        "<th>模型覆盖</th><th>结论</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _legend_html() -> str:
    items = [
        ("green", "绿 · 可用", "三层检查通过（网络 / 鉴权 / 模型），运行时照常调用。"),
        ("warn", "黄 · 可用但不理想", "端点活着，只是这次探测本身不理想（瞬时限流）。不 ban，照常调用。"),
        ("red", "红 · 不可用", "通路级：整条通路被 ban（丢通路 / 传空 key）。模型矩阵格：该「通路 × 模型」组合被 ban（模型级，2026-10-29 起）。自动走下一档，不影响其它通路。"),
        ("unknown", "灰 · 未判定", "非生成检查未得到模型可用性的结论，运行时照常调用。"),
        ("not_configured", "灰 · 未配置", "该通路没有配 key / URL，跳过，不参与判定。"),
    ]
    blocks = []
    for severity, title, text in items:
        blocks.append(
            '<div class="legend-item">'
            f'<span class="cell {SEV_CSS[severity]}">{SEV_EMOJI[severity]}</span>'
            f'<p><span class="k">{esc(title)}</span><br>{esc(text)}</p>'
            "</div>"
        )
    return '<div class="legend-grid">' + "".join(blocks) + "</div>"


def _notes_html(notes: list[str]) -> str:
    if not notes:
        return ""
    items = "".join(f"<li>{esc(note)}</li>" for note in notes)
    return f'<div class="notes"><b>数据提示</b><ul>{items}</ul></div>'


def render_html(data: dict, records: list[dict], notes: list[str]) -> str:
    meta = data["meta"]
    overall_emoji, overall_text = overall_conclusion(data)
    overall_sev = {"✅": "green", "⚠️": "warn", "❌": "red", "➖": "grey"}.get(overall_emoji, "grey")
    counts = data["counts"]
    total_paths = sum(counts.values())
    enabled = sum(1 for provider in data["providers"] if provider["counts"]["green"] or provider["counts"]["warn"])
    deployment = meta["deployment_label"] or "未标注部署"

    head_badges = "".join(
        [
            _pill("grey", deployment),
            _pill(overall_sev, f"{overall_emoji} {overall_text}"),
            _pill("grey", f"schema {meta['schema']}" if meta["schema"] is not None else "schema ?"),
        ]
    )
    title = f"Provider 探活面板 · {deployment}"
    history_heading = (
        f"历史趋势（最近 {len(records)} 次，最多保留 {HISTORY_LIMIT} 次）"
        if records
        else "历史趋势"
    )

    cards = [
        (
            "整体结论",
            f'<div class="big {overall_sev}">{esc(overall_emoji)} {esc(overall_text)}</div>'
            f'<div class="note">共 {total_paths} 条通路 · {len(data["providers"])} 个类别 · '
            f"{enabled} 个类别有可用通路</div>",
        ),
        ("通路状态", _count_html(counts) + f'<div class="note">合计 {total_paths} 条通路</div>'),
        ("全模型覆盖", _coverage_html(data)),
        (
            "探活方式",
            f'<div class="big grey" style="font-size:15px">不真实调用模型</div>'
            f'<div class="note">{esc(meta["probe_method"] or "（数据未标注探活方式）")}</div>',
        ),
    ]
    cards_html = "".join(
        f'<div class="card"><h3>{esc(card_title)}</h3>{card_body}</div>' for card_title, card_body in cards
    )

    footer_items = [
        "面板由 <code>scripts/build_provider_health_dashboard.py</code> 生成，纯静态自包含（无 CDN / 无外链 / 无 JS），可离线打开。",
        "数据来自 <code>scripts/probe_provider_paths.py</code>：全程只走官方非生成端点（DNS/TCP/TLS → 鉴权 → 模型列表 / 详情 / count tokens / 账户状态），不发真实生成请求、不产生生成费用。",
        "fail-open：模型层拿不到结论时按「没结论」处理，不 ban；黄通路照常调用；红通路只在最外层 ban，其它通路不受影响。",
        "历史来自 <code>reports/provider_health/history.jsonl</code>，每次探活追加一行；坏行会被跳过。",
        "同一份输入的渲染结果稳定（不使用当前时间），可直接 diff。",
    ]
    footer_list = "".join(f"<li>{item}</li>" for item in footer_items)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="scripts/build_provider_health_dashboard.py">
<title>{esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="page-head" id="top">
  <div class="head-inner">
    <h1>Provider 探活面板</h1>
    <p class="subtitle">全 provider · 全通路 · 不真实调用模型 · 绿照常 / 红在最外层 ban</p>
    <div class="badges">{head_badges}</div>
    {_meta_html(data)}
  </div>
</header>
<main>
  {_notes_html(notes)}
  <section id="overview">
    <h2>总览</h2>
    <div class="cards">{cards_html}</div>
    <div style="margin-top:14px">{_overview_table_html(data)}</div>
  </section>
  <section id="matrix">
    <h2>当前矩阵：通路 × 模型</h2>
    <p class="small muted" style="margin-top:-6px">
      每个格子是该通路上一个模型的最新结论（鼠标悬停看 verdict / 延迟 / HTTP 状态）；
      每行最后一列是整条通路的结论。格子来自 <code>paths.&lt;通路&gt;.models</code>。
    </p>
    {_matrix_html(data)}
  </section>
  <section id="paths">
    <h2>逐通路细节：网络 / 鉴权 / 模型三层结论</h2>
    <p class="small muted" style="margin-top:-6px">
      绿色通路默认折叠；黄 / 红 / 未配置自动展开。key 只显示掩码。
    </p>
    {_paths_html(data)}
  </section>
  <section id="history">
    <h2>{history_heading}</h2>
    {_history_html(records)}
  </section>
  <section id="legend">
    <h2>图例与说明</h2>
    {_legend_html()}
  </section>
</main>
<footer>
  <b>脚注</b>
  <ul>{footer_list}</ul>
</footer>
</body>
</html>
"""







def _md_sev(severity: str) -> str:
    return SEV_EMOJI.get(severity, "—")


def build_markdown(data: dict, records: list[dict], notes: list[str]) -> str:
    meta = data["meta"]
    overall_emoji, overall_text = overall_conclusion(data)
    counts = data["counts"]
    total_paths = sum(counts.values())
    deployment = meta["deployment_label"] or "未标注部署"

    lines: list[str] = ["# Provider 探活面板", ""]
    lines.append(f"> {overall_emoji} **{overall_text}** · 共 {total_paths} 条通路 · {len(data['providers'])} 个类别")
    lines.append("")
    lines.append(f"- 部署：**{_md_cell(deployment)}**")
    lines.append(
        f"- 生成时间：`{_md_cell(meta['generated_at_bj']) or '—'}`（北京时间 UTC+8） / "
        f"`{_md_cell(meta['generated_at_utc']) or '—'}`（UTC）"
    )
    lines.append(
        f"- 环境：{_md_cell(meta['environment']) or '—'} · run `{_md_cell(meta['run_id']) or '本地'}` · "
        f"schema {_md_cell(meta['schema']) if meta['schema'] is not None else '—'}"
    )
    lines.append(f"- 探活方式：{_md_cell(meta['probe_method']) or '—'}")
    if data["coverage"]:
        coverage = data["coverage"]
        percent = (coverage["ok"] / coverage["total"] * 100.0) if coverage["total"] else 0.0
        lines.append(f"- 全模型覆盖：**{coverage['ok']} / {coverage['total']}**（{percent:.1f}%）")
    else:
        lines.append("- 全模型覆盖：—（该数据版本没有模型维度）")
    lines.append("")

    if notes:
        for note in notes:
            lines.append(f"> ⚠️ {_md_cell(note)}")
        lines.append("")


    lines.append("## 总览")
    lines.append("")
    if not data["providers"]:
        lines.append("_暂无数据：没有读到任何 provider（JSON 缺失 / 空 providers / 旧数据格式）。_")
        lines.append("")
    else:
        lines.append("| 类别 | 通路数 | 绿 | 黄 | 红 | 未判定 | 未配置 | 模型覆盖 | 结论 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for provider in data["providers"]:
            provider_counts = provider["counts"]
            coverage_cell = (
                f"{provider['models_ok']}/{provider['models_total']}"
                if provider["models_total"] is not None and provider["models_ok"] is not None
                else "—"
            )
            emoji, text = provider_conclusion(provider)
            lines.append(
                f"| {_md_code(provider['name'])} | {len(provider['paths'])} | "
                f"{provider_counts['green']} | {provider_counts['warn']} | "
                f"{provider_counts['red']} | {provider_counts['unknown']} | {provider_counts['not_configured']} | "
                f"{coverage_cell} | {emoji} {_md_cell(text)} |"
            )
        coverage_total_cell = "—"
        if data["coverage"]:
            coverage_total_cell = f"{data['coverage']['ok']}/{data['coverage']['total']}"
        lines.append(
            f"| **合计** | {total_paths} | {counts['green']} | {counts['warn']} | "
            f"{counts['red']} | {counts['unknown']} | {counts['not_configured']} | "
            f"{coverage_total_cell} | {overall_emoji} |"
        )
        lines.append("")


    lines.append("## 通路 × 模型矩阵")
    lines.append("")
    if not data["providers"]:
        lines.append("_暂无数据。_")
        lines.append("")
    else:
        for provider in data["providers"]:
            counts_p = provider["counts"]
            emoji, text = provider_conclusion(provider)
            lines.append(
                f"### {_md_code(provider['name'])} · {emoji} {_md_cell(text)}"
                f"（绿 {counts_p['green']} / 黄 {counts_p['warn']} / 红 {counts_p['red']}"
                f" / 未判定 {counts_p['unknown']} / 未配置 {counts_p['not_configured']}）"
            )
            lines.append("")
            if not provider["paths"]:
                lines.append("_该类别本次没有通路数据。_")
                lines.append("")
                continue
            model_columns: dict[str, None] = {}
            for path in provider["paths"]:
                for model in path["models"]:
                    model_columns.setdefault(model["id"], None)
            columns = list(model_columns.keys())
            header = ["通路", *columns, "结论", "延迟"]
            lines.append("| " + " | ".join(_md_cell(col) for col in header) + " |")
            lines.append("|" + "|".join(["---"] * len(header)) + "|")
            for path in provider["paths"]:
                cells = [_md_code(path["name"])]
                for model_id in columns:
                    model = next((item for item in path["models"] if item["id"] == model_id), None)
                    cells.append(_md_sev(model["severity"]) if model else "—")
                cells.append(_md_cell(verdict_label(path["verdict"]) or SEV_LABEL[path["severity"]]))
                cells.append(_fmt_ms(path["latency_ms"]))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")


    lines.append(f"## 最近历史（最近 {MD_HISTORY_LIMIT} 次）")
    lines.append("")
    if not records:
        lines.append(
            "_暂无历史数据：`reports/provider_health/history.jsonl` 不存在或为空"
            "（第一次运行探活后才会出现趋势）。_"
        )
        lines.append("")
    else:
        recent = records[-MD_HISTORY_LIMIT:]
        provider_names = history_provider_names(records)
        header = ["时间（北京）", *provider_names]
        lines.append("| " + " | ".join(_md_cell(col) for col in header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for record in recent:
            cells = [_md_cell(record["time"] or "—")]
            for provider_name in provider_names:
                emoji, _ = _record_provider_cell(record, provider_name)
                cells.append(emoji)
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        rows = history_rows(records)
        if rows:
            lines.append("**通路历史可用率**（可用 = 绿 + 黄；红才算不可用，分母是有结论的次数）：")
            lines.append("")
            lines.append("| 通路 | 可用率 | 可用/观测 |")
            lines.append("|---|---|---|")
            for row in rows:
                availability = "—" if row["availability"] is None else f"{row['availability']:.0f}%"
                lines.append(
                    f"| {_md_code(row['provider'] + ' / ' + row['name'])} | {availability} | "
                    f"{row['usable']}/{row['observed']} |"
                )
            lines.append("")

    lines.append("## 图例与说明")
    lines.append("")
    lines.append("- ✅ **绿**：三层检查通过（网络 / 鉴权 / 模型），运行时照常调用。")
    lines.append(
        "- ⚠️ **黄**：端点通/活，只是这次探测请求本身不理想（探测模型不在该档模型列表里、429 限流等）。**不 ban**，照常调用。"
    )
    lines.append("- ❌ **红**：运行时在最外层 ban 掉这条通路，自动走下一档；不影响其它通路。")
    lines.append("- ➖ **未配置**：没配 key / URL，跳过，不参与判定。")
    lines.append(
        "- fail-open：模型层拿不到结论时按「没结论」处理（不 ban）；探活不真实调用模型，只走官方非生成端点，不产生生成费用。"
    )
    lines.append(
        "- 本文件由 `scripts/build_provider_health_dashboard.py` 从 "
        "`data/provider_health.json` + `reports/provider_health/history.jsonl` 生成，纯静态、同输入同输出。"
    )
    lines.append("")
    return "\n".join(lines)







def _read_json(path: pathlib.Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, f"未找到探活 JSON：{path}（页面按「暂无数据」渲染；先跑一次 scripts/probe_provider_paths.py）"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, f"探活 JSON 无法读取：{path}（{exc}）"
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"探活 JSON 解析失败：{path}（{exc}）；页面按「暂无数据」渲染"
    if not isinstance(payload, dict):
        return {}, f"探活 JSON 顶层不是对象：{path}；页面按「暂无数据」渲染"
    return payload, ""


def _write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provider 探活面板生成器（自包含 HTML + Markdown）")
    parser.add_argument("--json", default=str(DEFAULT_JSON), help="探活结果 JSON（schema 4）")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY), help="历史 JSONL（可选，缺失也能生成）")
    parser.add_argument("--out-html", default=str(DEFAULT_HTML), help="自包含 HTML 输出路径")
    parser.add_argument("--out-md", default=str(DEFAULT_MD), help="Markdown 输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    notes: list[str] = []
    payload, json_note = _read_json(pathlib.Path(args.json))
    if json_note:
        notes.append(json_note)
    data = normalize_payload(payload)

    records, history_note = load_history(pathlib.Path(args.history))
    if history_note:
        notes.append(history_note)

    html_text = render_html(data, records, notes)
    md_text = build_markdown(data, records, notes)
    _write_text(pathlib.Path(args.out_html), html_text)
    _write_text(pathlib.Path(args.out_md), md_text)

    counts = data["counts"]
    total_paths = sum(counts.values())
    print("[dashboard] 部署：" + (data["meta"]["deployment_label"] or "未标注部署"))
    print(
        f"[dashboard] 通路 {total_paths} 条：绿 {counts['green']} / 黄 {counts['warn']} / "
        f"红 {counts['red']} / 未配置 {counts['not_configured']}"
    )
    if data["coverage"]:
        print(f"[dashboard] 全模型覆盖：{data['coverage']['ok']}/{data['coverage']['total']}")
    print(f"[dashboard] 历史：{len(records)} 次记录")
    for note in notes:
        print(f"[dashboard] 提示：{note}")
    print(f"[dashboard] 已写入 {args.out_html}")
    print(f"[dashboard] 已写入 {args.out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
