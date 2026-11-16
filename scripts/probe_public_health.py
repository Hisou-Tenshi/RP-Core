# SPDX-License-Identifier: AGPL-3.0-only
"""Probe using this Actions environment; publish only allowlisted status data.

Only the single bounded private provider-call counter file is sparse-checked out;
no conversation, prompt, model response or database is read. No model generation
endpoint is used, and raw probe bodies exist only in a temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "src/rp_core/compat_runtime"
VERDICTS = {
    "ok",
    "not_configured",
    "dns_fail",
    "tcp_fail",
    "tls_fail",
    "tls_cert_invalid",
    "auth_denied",
    "blocked",
    "model_not_found",
    "rate_limited",
    "quota_exhausted",
    "bad_request",
    "upstream_error",
    "timeout",
    "http_error",
    "unknown",
}


def number(value):
    return value if type(value) in (float, int) and 0 <= value < 1_000_000 else None


def status(item):
    verdict = item.get("verdict")
    return {
        "ok": item.get("ok") if type(item.get("ok")) is bool else None,
        "verdict": verdict if verdict in VERDICTS else "unknown",
        "severity": item.get("severity")
        if item.get("severity") in {"green", "red", "warn", "not_configured"}
        else "warn",
        "status": number(item.get("status")),
        "latency_ms": number(item.get("latency_ms")),
    }


def sanitize(raw, instance, run_id, config_bytes):
    catalog = json.loads(config_bytes)
    ids = {m["id"] for m in catalog["models"]} | {catalog["embedding"]["model"]}
    providers = {}
    for provider in (p["key"] for p in catalog["providers"]):
        source = raw.get("providers", {}).get(provider)
        if not isinstance(source, dict):
            continue
        paths = {}
        for name, item in source.get("paths", {}).items():
            # Names are public routing labels, not hostnames or arbitrary upstream text.
            if not re.fullmatch(r"[A-Za-z0-9 _()/.-]{1,80}", name):
                raise ValueError("Unexpected public path label.")
            paths[name] = {
                **status(item),
                "models_total": number(item.get("models_total")),
                "models_ok": number(item.get("models_ok")),
                "checks": {
                    key: status(value)
                    for key, value in (item.get("checks") or {}).items()
                    if key in {"network", "auth", "model"} and isinstance(value, dict)
                },
                "models": {
                    key: status(value)
                    for key, value in (item.get("models") or {}).items()
                    if key in ids and isinstance(value, dict)
                },
            }
        providers[provider] = {
            "enabled": any(p["ok"] for p in paths.values()),
            "models_total": number(source.get("models_total")),
            "models_ok": number(source.get("models_ok")),
            "paths": paths,
        }
    if not providers:
        raise ValueError("Empty probe result; do not replace the previous snapshot.")
    return {
        "schema": 4,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "deployment": "rp-core",
        "instance_id": instance,
        "credential_source": f"rp-core-actions:{instance}",
        "probe_method": "official_non_call",
        "run_id": run_id if re.fullmatch(r"[0-9]{1,20}", run_id) else None,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "providers": providers,
    }


def sanitize_call_history(path: Path | None) -> dict:
    """Publish only the last-100 provider/path counters; no prompt, model or output."""
    allowed = {
        "claude": {
            "totokens",
            "codeyu_base",
            "codeyu_prime",
            "codeyu_max",
            "coderplan",
            "openrouter",
            "official",
        },
        "gemini": {"totokens", "codeyu", "coderplan", "openrouter", "official"},
        "gpt": {"totokens", "codeyu", "coderplan", "openrouter", "official"},
        "grok": {"codeyu", "totokens", "coderplan", "openrouter", "official"},
        "deepseek": {"official", "fireworks_pro", "openrouter"},
        "glm": {"official", "openrouter"},
        "qwen": {"official"},
        "magmarouter": {"official"},
        "openrouter": {"official"},
    }
    try:
        rows = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else []
    except (OSError, ValueError):
        rows = []
    if not isinstance(rows, list):
        rows = []
    # The private runtime retains a 100-attempt window per provider. Apply the
    # same bound here for older files before producing public aggregate counts.
    per_provider = {}
    legacy = {
        "claude": {
            "proxy3": "totokens",
            "proxy1_base": "codeyu_base",
            "proxy1_prime": "codeyu_prime",
            "proxy1_max": "codeyu_max",
            "proxy2": "coderplan",
        },
        "gemini": {"proxy3": "totokens", "proxy1": "codeyu", "proxy2": "coderplan"},
        "gpt": {"proxy3": "totokens", "proxy1": "codeyu", "proxy2": "coderplan"},
        "grok": {"proxy2": "totokens", "proxy1": "coderplan"},
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if provider not in allowed:
            continue
        per_provider.setdefault(provider, []).append(row)
    rows = [row for provider in allowed for row in per_provider.get(provider, [])[-100:]]
    counts = {}
    accepted = 0
    for row in rows:
        provider, route, outcome = row.get("provider"), row.get("route"), row.get("outcome")
        route = legacy.get(provider, {}).get(route, route)
        if (
            provider not in allowed
            or route not in allowed[provider]
            or outcome not in ("success", "failure")
        ):
            continue
        stats = counts.setdefault(provider, {}).setdefault(
            route, {"calls": 0, "success": 0, "failure": 0}
        )
        stats["calls"] += 1
        stats[outcome] += 1
        accepted += 1
    return {
        "schema": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "window_per_provider": 100,
        "sample_size": accepted,
        "providers": counts,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--call-history", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.instance):
        raise SystemExit("Invalid public instance ID.")
    with tempfile.TemporaryDirectory(prefix="rp-public-probe-") as temp:
        tmp = Path(temp)
        card = tmp / "character.json"
        card.write_text(
            json.dumps({"id": "probe", "name": "Example", "instructions": "Synthetic probe only."})
        )
        env = {
            **os.environ,
            "RP_RUNTIME_ROOT": str(CODE),
            "RP_CHARACTER_PATH": str(card),
            "PYTHONPATH": str(CODE / "src"),
            "RP_EXTERNAL_PERSISTENCE": "1",
            "PROVIDER_HEALTH_ENABLED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        output = tmp / "raw.json"
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CODE / "scripts/probe_provider_paths.py"),
                    "--timeout",
                    "8",
                    "--providers",
                    "claude,gemini,deepseek,gpt,glm,magmarouter,openrouter,grok,qwen",
                    "--json",
                    str(output),
                ],
                env=env,
                cwd=CODE,
                capture_output=True,
                timeout=1200,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit("Probe timed out; raw diagnostics were not published.") from None
        if result.returncode or not output.is_file():
            raise SystemExit("Probe failed; raw diagnostics were not published.")
        report = sanitize(
            json.loads(output.read_text()),
            args.instance,
            os.getenv("GITHUB_RUN_ID", ""),
            (CODE / "model-config.json").read_bytes(),
        )
    directory = ROOT / "health" / args.instance
    directory.mkdir(parents=True, exist_ok=True)
    history_path = directory / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    sample = {key: report[key] for key in ("generated_at_utc", "config_sha256", "run_id")}
    sample["providers"] = {
        provider: {
            "paths": {
                name: {
                    key: path[key]
                    for key in ("ok", "verdict", "latency_ms", "models_total", "models_ok")
                }
                for name, path in item["paths"].items()
            }
        }
        for provider, item in report["providers"].items()
    }
    history = [*history[-119:], sample]
    calls = sanitize_call_history(args.call_history)
    for path, value in (
        (directory / "latest.json", report),
        (history_path, history),
        (directory / "calls.json", calls),
    ):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + chr(10))
    print(
        json.dumps(
            {
                "published": True,
                "instance": args.instance,
                "paths": sum(len(p["paths"]) for p in report["providers"].values()),
                "chat_messages_sent": 0,
                "provider_keys_from": "RP-Core Actions",
            }
        )
    )


if __name__ == "__main__":
    main()
