# SPDX-License-Identifier: AGPL-3.0-only
"""Keep up to 100 sanitized recent attempts per provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

_LOCK = threading.Lock()
_PROVIDERS = {
    "claude",
    "gemini",
    "gpt",
    "grok",
    "deepseek",
    "glm",
    "qwen",
    "magmarouter",
    "openrouter",
}
_ROUTES_BY_PROVIDER = {
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
_LEGACY_ROUTE_IDS = {
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


def route_id(provider: str, path: dict) -> str:
    if provider == "openrouter":
        return "official"
    explicit = path.get("route_id")
    allowed_routes = _ROUTES_BY_PROVIDER.get(provider, set())
    explicit = _LEGACY_ROUTE_IDS.get(provider, {}).get(explicit, explicit)
    if explicit in allowed_routes:
        return explicit
    name = str(path.get("name") or "").strip().lower()
    if (
        path.get("openrouter")
        or name == "openrouter"
        or "openrouter.ai" in str(path.get("base_url") or "")
    ):
        return "openrouter"
    if path.get("is_official") or name in ("official", "openai"):
        return "official"
    aliases = {
        "claude": {
            "to-tokens": "totokens",
            "totokens": "totokens",
            "codeyu base": "codeyu_base",
            "codeyu prime": "codeyu_prime",
            "codeyu max": "codeyu_max",
            "codeyu": "codeyu_base",
            "coderplan": "coderplan",
            "proxy 3": "totokens",
            "proxy 1 base": "codeyu_base",
            "proxy 1 prime": "codeyu_prime",
            "proxy 1 max": "codeyu_max",
            "proxy 2": "coderplan",
        },
        "gemini": {
            "to-tokens": "totokens",
            "totokens": "totokens",
            "codeyu": "codeyu",
            "coderplan": "coderplan",
            "gemini proxy 3": "totokens",
            "gemini proxy": "codeyu",
            "gemini proxy 2": "coderplan",
        },
        "gpt": {
            "to-tokens": "totokens",
            "totokens": "totokens",
            "codeyu": "codeyu",
            "coderplan": "coderplan",
            "gpt proxy 3": "totokens",
            "gpt proxy": "codeyu",
            "gpt proxy 2": "coderplan",
        },
        "grok": {
            "codeyu": "codeyu",
            "to-tokens": "totokens",
            "totokens": "totokens",
            "coderplan": "coderplan",
            "grok proxy 2": "totokens",
            "grok proxy": "coderplan",
        },
        "deepseek": {"fireworks deepseek pro": "fireworks_pro"},
    }
    alias = aliases.get(provider, {}).get(name, "")
    return alias if alias in allowed_routes else ""


def _history_path() -> Path:
    data_root = Path(os.environ.get("RP_DATA_ROOT") or os.environ.get("RP_RUNTIME_ROOT") or ".")
    return data_root / "data" / "provider_call_history.json"


def record(provider: str, path: dict, *, success: bool) -> None:
    """Persist route metadata only; never store prompt, model output, host, or key."""
    provider = str(provider or "").lower()
    route = route_id(provider, path or {})
    if provider not in _PROVIDERS or route not in _ROUTES_BY_PROVIDER.get(provider, set()):
        return
    target = _history_path()
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                existing = []
            except (OSError, ValueError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing.append(
                {
                    "ts": time.time(),
                    "provider": provider,
                    "route": route,
                    "outcome": "success" if success else "failure",
                }
            )
            counts: dict[str, int] = {}
            retained = []
            for row in reversed(existing):
                row_provider = (
                    str(row.get("provider") or "").lower() if isinstance(row, dict) else ""
                )
                if row_provider not in _PROVIDERS:
                    continue
                if counts.get(row_provider, 0) >= 100:
                    continue
                counts[row_provider] = counts.get(row_provider, 0) + 1
                retained.append(row)
            existing = list(reversed(retained))
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(
                json.dumps(existing, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
    except OSError:
        return
