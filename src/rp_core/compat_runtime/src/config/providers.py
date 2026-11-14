# SPDX-License-Identifier: AGPL-3.0-only
























from __future__ import annotations

import os
from dataclasses import dataclass

from config.env_boot import PROJECT_ROOT

__all__ = [

    "GEMINI_API_KEY",
    "CLAUDE_API_KEY",
    "GROK_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GPT_API_KEY",
    "GLM_API_KEY",
    "QWEN_API_KEY",
    "OPENROUTER_API_KEY",
    "MAGMAROUTER_API_KEY",
    "GROK_BASE_URL",
    "MAGMAROUTER_BASE_URL",
    "OPENROUTER_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "QWEN_BASE_URL",
    "GPT_BASE_URL",
    "GLM_BASE_URL",
    "FIREWORKS_BASE_URL",
    "FIREWORKS_DEEPSEEK_V4_PRO_MODEL",
    "FIREWORKS_DEEPSEEK_V4_FLASH_MODEL",
    "CLAUDE_OFFICIAL_BASE_URL",
    "GEMINI_OFFICIAL_BASE_URL",

    "CLAUDE_PROXY_BASE_URL",
    "CLAUDE_PROXY_KEY_BASE",
    "CLAUDE_PROXY_KEY_PRIME",
    "CLAUDE_PROXY_KEY_MAX",
    "CLAUDE_PROXY_BASE_URL_2",
    "CLAUDE_PROXY_KEY_2",
    "CLAUDE_PROXY1_TIERS",
    "CLAUDE_PROXY2",
    "CLAUDE_PROXIES",

    "GEMINI_PROXY_BASE_URL",
    "GEMINI_PROXY_KEY",
    "GEMINI_PROXY",
    "GEMINI_PROXY2_BASE_URL",
    "GEMINI_PROXY2_KEY",
    "GEMINI_PROXY2",
    "GEMINI_OPENROUTER",
    "GEMINI_PROXIES",
    "GPT_PROXY_BASE_URL",
    "GPT_PROXY_KEY",
    "GPT_PROXY",
    "GPT_PROXY2_BASE_URL",
    "GPT_PROXY2_KEY",
    "GPT_PROXY2",
    "GPT_PROXIES",
    "GROK_PROXY_BASE_URL",
    "GROK_PROXY_KEY",
    "GROK_PROXY",
    "GROK_PROXIES",
    "DEEPSEEK_PROXIES",

    "ProviderPathPolicy",
    "PROVIDER_PATH_POLICIES",
    "PROVIDER_PROXIES",
    "PROVIDER_PATH_ORDER",
    "OPENROUTER_DISABLED_PROVIDERS",
    "PROXY_ENV_SPECS",
    "proxy_config_warnings",
]


def _env(*names: str, default: str | None = None) -> str | None:
    """按顺序取第一个非空环境变量(自动 strip);全部为空时返回 default。"""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default







CLAUDE_OFFICIAL_BASE_URL = _env("CLAUDE_OFFICIAL_BASE_URL", "CLAUDE_BASE_URL", default="https://api.anthropic.com")
GEMINI_OFFICIAL_BASE_URL = _env(
    "GEMINI_OFFICIAL_BASE_URL", "GEMINI_BASE_URL", default="https://generativelanguage.googleapis.com"
)



CODERPLAN_DEFAULT_BASE_URL = _env("CODERPLAN_PROXY_BASE_URL", default="https://api.coderplan.ai")


def _ensure_openai_v1(url: str | None) -> str | None:
    """OpenAI 兼容代理的 base_url 纯字符串兜底:URL 里没有 ``/v1`` 就补上。

    coderplan 这类网关的 OpenAI 兼容端点是挂在 ``/v1`` 下的
    (``/v1/models``、``/v1/chat/completions``)。配置里若只写了根域名,
    OpenAI SDK **不会**替你补 ``/v1``,请求会落到 ``/chat/completions`` 直接 404;
    探活拼 ``{base}/models`` 同样 404,还会被 fail-open 记成「没结论」。
    这里只看字符串(不发网络请求):已含 ``/v1`` 的原样返回,否则末尾拼 ``/v1``。

    只用于 GPT 代理1 / GPT 代理2 / Grok 这几条 OpenAI 兼容代理。Claude 代理2 / Gemini 代理2
    虽然同为 coderplan 域名,但协议客户端自己会带 ``/v1`` / ``/v1beta``,不能套这条规则。
    """
    if not url:
        return url
    base = url.rstrip("/")
    return base if "/v1" in base else base + "/v1"







GEMINI_API_KEY = _env("GEMINI_API_KEY")


CLAUDE_API_KEY = _env("CLAUDE_API_KEY")


GROK_API_KEY = _env("GROK_API_KEY")
GROK_BASE_URL = _env("GROK_BASE_URL", default="https://api.x.ai/v1")


DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com")





FIREWORKS_API_KEY = _env("FIREWORKS_API_KEY")
FIREWORKS_BASE_URL = _env("FIREWORKS_BASE_URL", default="")
FIREWORKS_DEEPSEEK_V4_PRO_MODEL = _env(
    "FIREWORKS_DEEPSEEK_V4_PRO_MODEL", default="accounts/fireworks/models/deepseek-v4-pro"
)
FIREWORKS_DEEPSEEK_V4_FLASH_MODEL = _env(
    "FIREWORKS_DEEPSEEK_V4_FLASH_MODEL", default="accounts/fireworks/models/deepseek-v4-flash"
)


GPT_API_KEY = _env("GPT_API_KEY")
GPT_BASE_URL = _env("GPT_BASE_URL", default="https://api.openai.com/v1")


GLM_API_KEY = _env("GLM_API_KEY")
GLM_BASE_URL = _env("GLM_BASE_URL", default="https://open.bigmodel.cn/api/paas/v4")


QWEN_API_KEY = _env("QWEN_API_KEY", "QWEN_KEY")
QWEN_BASE_URL = _env(
    "QWEN_BASE_URL", default="https://maas.qianwenaiapi.com/compatible-mode/v1"
)


MAGMAROUTER_API_KEY = _env("MAGMAROUTER_API_KEY")
MAGMAROUTER_BASE_URL = _env("MAGMAROUTER_BASE_URL", default="https://api.magmarouter.com/v1")


OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")






CLAUDE_PROXY_BASE_URL = _env("CLAUDE_PROXY_BASE_URL")
CLAUDE_PROXY_KEY_BASE = _env("CLAUDE_PROXY_KEY_BASE", "CLAUDE_PROXY_KEY")
CLAUDE_PROXY_KEY_PRIME = _env("CLAUDE_PROXY_KEY_PRIME")
CLAUDE_PROXY_KEY_MAX = _env("CLAUDE_PROXY_KEY_MAX")


CLAUDE_PROXY_BASE_URL_2 = _env(
    "CLAUDE_PROXY_BASE_URL_2", default=CODERPLAN_DEFAULT_BASE_URL
)
CLAUDE_PROXY_KEY_2 = _env("CLAUDE_PROXY_KEY_2")

CLAUDE_PROXY1_TIERS: dict[str, dict] = {}
if CLAUDE_PROXY_BASE_URL:
    if CLAUDE_PROXY_KEY_BASE:
        CLAUDE_PROXY1_TIERS["base"] = {
            "name": "Proxy 1 Base",
            "api_key": CLAUDE_PROXY_KEY_BASE,
            "base_url": CLAUDE_PROXY_BASE_URL,
        }
    if CLAUDE_PROXY_KEY_PRIME:
        CLAUDE_PROXY1_TIERS["prime"] = {
            "name": "Proxy 1 Prime",
            "api_key": CLAUDE_PROXY_KEY_PRIME,
            "base_url": CLAUDE_PROXY_BASE_URL,
        }
    if CLAUDE_PROXY_KEY_MAX:
        CLAUDE_PROXY1_TIERS["max"] = {
            "name": "Proxy 1 Max",
            "api_key": CLAUDE_PROXY_KEY_MAX,
            "base_url": CLAUDE_PROXY_BASE_URL,
        }

CLAUDE_PROXY2: dict | None = None
if CLAUDE_PROXY_KEY_2 and CLAUDE_PROXY_BASE_URL_2:
    CLAUDE_PROXY2 = {
        "name": "Proxy 2",
        "api_key": CLAUDE_PROXY_KEY_2,
        "base_url": CLAUDE_PROXY_BASE_URL_2,
    }


_PROXY_CONFIG_WARNINGS: list[str] = []
if CLAUDE_PROXY_BASE_URL and not any(
    (CLAUDE_PROXY_KEY_BASE, CLAUDE_PROXY_KEY_PRIME, CLAUDE_PROXY_KEY_MAX)
):
    _PROXY_CONFIG_WARNINGS.append(
        "CLAUDE_PROXY_BASE_URL 已设置但没有任何 Proxy 1 档位 key：Proxy 1 被静默禁用。"
    )


CLAUDE_PROXIES: list[dict] = []
for _tier in ("base", "prime", "max"):
    if _tier in CLAUDE_PROXY1_TIERS:
        CLAUDE_PROXIES.append(
            {
                "name": "Proxy 1",
                "api_key": CLAUDE_PROXY1_TIERS[_tier]["api_key"],
                "base_url": CLAUDE_PROXY1_TIERS[_tier]["base_url"],
            }
        )
        break
if CLAUDE_PROXY2 is not None:
    CLAUDE_PROXIES.append(dict(CLAUDE_PROXY2))








GEMINI_PROXY_BASE_URL = _env("GEMINI_PROXY_BASE_URL", default="https://codeyu.shop")
GEMINI_PROXY_KEY = _env("GEMINI_PROXY_KEY") or GEMINI_API_KEY
GEMINI_PROXY: dict | None = None
if GEMINI_PROXY_BASE_URL and GEMINI_PROXY_KEY:
    GEMINI_PROXY = {
        "name": "Gemini Proxy",
        "api_key": GEMINI_PROXY_KEY,
        "base_url": GEMINI_PROXY_BASE_URL,
    }



GEMINI_PROXY2_BASE_URL = _env(
    "GEMINI_PROXY2_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL
)
GEMINI_PROXY2_KEY = _env("GEMINI_PROXY2_KEY")
GEMINI_PROXY2: dict | None = None
if GEMINI_PROXY2_KEY and GEMINI_PROXY2_BASE_URL:
    GEMINI_PROXY2 = {
        "name": "Gemini Proxy 2",
        "api_key": GEMINI_PROXY2_KEY,
        "base_url": GEMINI_PROXY2_BASE_URL,
    }


GEMINI_OPENROUTER: dict | None = None
if OPENROUTER_API_KEY and OPENROUTER_BASE_URL:
    GEMINI_OPENROUTER = {
        "name": "OpenRouter",
        "api_key": OPENROUTER_API_KEY,
        "base_url": OPENROUTER_BASE_URL,
        "protocol": "openai",
        "vendor_prefix": "google/",
        "openrouter": True,
    }


GEMINI_PROXIES: list[dict] = []
if GEMINI_PROXY is not None:
    GEMINI_PROXIES.append(dict(GEMINI_PROXY))
if GEMINI_PROXY2 is not None:
    GEMINI_PROXIES.append(dict(GEMINI_PROXY2))

if GEMINI_PROXY2_BASE_URL and not GEMINI_PROXY2_KEY:

    if GEMINI_PROXY2_BASE_URL != CODERPLAN_DEFAULT_BASE_URL:
        _PROXY_CONFIG_WARNINGS.append(
            "GEMINI_PROXY2_BASE_URL 已设置但 GEMINI_PROXY2_KEY 缺失：Gemini 第二代理被禁用。"
        )











GPT_PROXY_BASE_URL = _ensure_openai_v1(
    _env("GPT_PROXY_BASE_URL", default="https://codeyu.shop/v1")
)
GPT_PROXY_KEY = _env("GPT_PROXY_KEY")
GPT_PROXY: dict | None = None
if GPT_PROXY_KEY and GPT_PROXY_BASE_URL:
    GPT_PROXY = {
        "name": "GPT Proxy",
        "api_key": GPT_PROXY_KEY,
        "base_url": GPT_PROXY_BASE_URL,
    }



GPT_PROXY2_BASE_URL = _ensure_openai_v1(
    _env("GPT_PROXY2_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
)
GPT_PROXY2_KEY = _env("GPT_PROXY2_KEY")
GPT_PROXY2: dict | None = None
if GPT_PROXY2_KEY and GPT_PROXY2_BASE_URL:
    GPT_PROXY2 = {
        "name": "GPT Proxy 2",
        "api_key": GPT_PROXY2_KEY,
        "base_url": GPT_PROXY2_BASE_URL,
    }


GPT_PROXIES: list[dict] = []
if GPT_PROXY is not None:
    GPT_PROXIES.append(dict(GPT_PROXY))
if GPT_PROXY2 is not None:
    GPT_PROXIES.append(dict(GPT_PROXY2))

if GPT_PROXY2_BASE_URL and not GPT_PROXY2_KEY:

    if GPT_PROXY2_BASE_URL != _ensure_openai_v1(CODERPLAN_DEFAULT_BASE_URL):
        _PROXY_CONFIG_WARNINGS.append(
            "GPT_PROXY2_BASE_URL 已设置但 GPT_PROXY2_KEY 缺失:GPT 第二代理被禁用。"
        )


GROK_PROXY_BASE_URL = _ensure_openai_v1(
    _env("GROK_PROXY_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
)
GROK_PROXY_KEY = _env("GROK_PROXY_KEY")
GROK_PROXY: dict | None = None
if GROK_PROXY_KEY and GROK_PROXY_BASE_URL:
    GROK_PROXY = {
        "name": "Grok Proxy",
        "api_key": GROK_PROXY_KEY,
        "base_url": GROK_PROXY_BASE_URL,
    }
GROK_PROXIES: list[dict] = [dict(GROK_PROXY)] if GROK_PROXY is not None else []








DEEPSEEK_PROXIES: list[dict] = []
if FIREWORKS_API_KEY and FIREWORKS_BASE_URL:
    DEEPSEEK_PROXIES.append(
        {
            "name": "Fireworks DeepSeek Pro",
            "api_key": FIREWORKS_API_KEY,
            "base_url": FIREWORKS_BASE_URL,
        }
    )

















@dataclass(frozen=True)
class ProviderPathPolicy:
    """一个 provider 的通路顺序与元信息。"""

    provider: str
    order: tuple[str, ...] = ("official",)
    official_name: str = "Official"

    openrouter_vendor_prefix: str | None = None

    openrouter_allowed: bool = True

    note: str = ""

    def resolved_order(self) -> tuple[str, ...]:
        """过滤掉不允许的 token(openrouter_allowed=False 时去掉 openrouter)。"""
        tokens = [t for t in self.order if t in ("proxies", "openrouter", "official")]
        if not self.openrouter_allowed:
            tokens = [t for t in tokens if t != "openrouter"]
        return tuple(tokens)


_PROXY_THEN_OR_THEN_OFFICIAL = ("proxies", "openrouter", "official")
_OFFICIAL_THEN_PROXY_THEN_OR = ("official", "proxies", "openrouter")
_OFFICIAL_ONLY = ("official",)

PROVIDER_PATH_POLICIES: dict[str, ProviderPathPolicy] = {
    "claude": ProviderPathPolicy(
        "claude",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "Official",
        "anthropic/",
        True,
        "代理1 三档 → 代理2 → OpenRouter → 官方(官方仅 sonnet 系列,见 tenshi_reply.call_claude)",
    ),
    "gemini": ProviderPathPolicy(
        "gemini",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "Official",
        "google/",
        True,
        "Gemini 代理1 → Gemini 代理2 → OpenRouter → 官方",
    ),
    "grok": ProviderPathPolicy(
        "grok",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "Official",
        "x-ai/",
        True,
        "Grok 代理 → OpenRouter → 官方",
    ),
    "gpt": ProviderPathPolicy(
        "gpt",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "OpenAI",
        "openai/",
        True,
        "GPT 代理1(codeyu.shop/v1)→ 代理2(coderplan/v1)→ OpenRouter → 官方",
    ),
    "deepseek": ProviderPathPolicy(
        "deepseek",
        _OFFICIAL_THEN_PROXY_THEN_OR,
        "Official",
        "deepseek/",
        True,
        "官方 → OpenRouter(Fireworks 代理 2026-11 已停用:代理段为空,壳保留)",
    ),
    "glm": ProviderPathPolicy(
        "glm",
        _OFFICIAL_THEN_PROXY_THEN_OR,
        "Official",
        "z-ai/",
        True,
        "官方 → OpenRouter",
    ),
    "qwen": ProviderPathPolicy(
        "qwen",
        _OFFICIAL_ONLY,
        "Official",
        None,
        False,
        "embedding 专用;不挂 OpenRouter(OpenRouter 无对应 embedding 模型)",
    ),
    "magmarouter": ProviderPathPolicy(
        "magmarouter",
        _OFFICIAL_ONLY,
        "MagmaRouter",
        None,
        False,
        "聚合网关;其 xploded 模型在 OpenRouter 无对应 slug,不兜底",
    ),
    "openrouter": ProviderPathPolicy(
        "openrouter",
        _OFFICIAL_ONLY,
        "OpenRouter",
        None,
        False,
        "自身即兜底通路,不再套娃",
    ),
}


PROVIDER_PATH_ORDER: dict[str, tuple[str, ...]] = {
    name: policy.resolved_order() for name, policy in PROVIDER_PATH_POLICIES.items()
}


OPENROUTER_DISABLED_PROVIDERS: set[str] = {
    name for name, policy in PROVIDER_PATH_POLICIES.items() if not policy.openrouter_allowed
}


PROVIDER_PROXIES: dict[str, list[dict]] = {
    "claude": CLAUDE_PROXIES,
    "gemini": GEMINI_PROXIES,
    "gpt": GPT_PROXIES,
    "grok": GROK_PROXIES,
    "deepseek": DEEPSEEK_PROXIES,
    "glm": [],
    "qwen": [],
    "magmarouter": [],
    "openrouter": [],
}


def proxy_config_warnings() -> list[str]:
    """返回代理半配告警(调用方负责打印;fixed.py 启动时会打一遍)。"""
    return list(_PROXY_CONFIG_WARNINGS)



if _PROXY_CONFIG_WARNINGS:
    import logging as _logging
    import sys as _sys

    for _w in _PROXY_CONFIG_WARNINGS:
        _logging.getLogger("TenshiConfig").warning(f"[proxy] {_w}")
        print(f"[proxy-config] WARNING: {_w}", file=_sys.stderr, flush=True)









PROXY_ENV_SPECS: list[dict] = [

    {"provider": "claude", "proxy": "Proxy 1 Base", "role": "url", "env": "CLAUDE_PROXY_BASE_URL", "default": ""},
    {"provider": "claude", "proxy": "Proxy 1 Base", "role": "key", "env": "CLAUDE_PROXY_KEY_BASE", "default": "", "fallback_env": "CLAUDE_PROXY_KEY"},
    {"provider": "claude", "proxy": "Proxy 1 Prime", "role": "key", "env": "CLAUDE_PROXY_KEY_PRIME", "default": ""},
    {"provider": "claude", "proxy": "Proxy 1 Max", "role": "key", "env": "CLAUDE_PROXY_KEY_MAX", "default": ""},

    {"provider": "claude", "proxy": "Proxy 2", "role": "url", "env": "CLAUDE_PROXY_BASE_URL_2", "default": "https://api.coderplan.ai"},
    {"provider": "claude", "proxy": "Proxy 2", "role": "key", "env": "CLAUDE_PROXY_KEY_2", "default": ""},

    {"provider": "gemini", "proxy": "Gemini Proxy", "role": "url", "env": "GEMINI_PROXY_BASE_URL", "default": "https://codeyu.shop"},
    {"provider": "gemini", "proxy": "Gemini Proxy", "role": "key", "env": "GEMINI_PROXY_KEY", "default": "(fallback: GEMINI_API_KEY)"},
    {"provider": "gemini", "proxy": "Gemini Proxy 2", "role": "url", "env": "GEMINI_PROXY2_BASE_URL", "default": "https://api.coderplan.ai"},
    {"provider": "gemini", "proxy": "Gemini Proxy 2", "role": "key", "env": "GEMINI_PROXY2_KEY", "default": ""},

    {"provider": "gpt", "proxy": "GPT Proxy", "role": "url", "env": "GPT_PROXY_BASE_URL", "default": "https://codeyu.shop/v1"},
    {"provider": "gpt", "proxy": "GPT Proxy", "role": "key", "env": "GPT_PROXY_KEY", "default": ""},
    {"provider": "gpt", "proxy": "GPT Proxy 2", "role": "url", "env": "GPT_PROXY2_BASE_URL", "default": "https://api.coderplan.ai/v1"},
    {"provider": "gpt", "proxy": "GPT Proxy 2", "role": "key", "env": "GPT_PROXY2_KEY", "default": ""},

    {"provider": "grok", "proxy": "Grok Proxy", "role": "url", "env": "GROK_PROXY_BASE_URL", "default": "https://api.coderplan.ai/v1"},
    {"provider": "grok", "proxy": "Grok Proxy", "role": "key", "env": "GROK_PROXY_KEY", "default": ""},

    {"provider": "deepseek", "proxy": "Fireworks DeepSeek Pro", "role": "url", "env": "FIREWORKS_BASE_URL", "default": ""},
    {"provider": "deepseek", "proxy": "Fireworks DeepSeek Pro", "role": "key", "env": "FIREWORKS_API_KEY", "default": ""},

    {"provider": "claude", "proxy": "Official", "role": "url", "env": "CLAUDE_OFFICIAL_BASE_URL", "default": "https://api.anthropic.com"},
    {"provider": "gemini", "proxy": "Official", "role": "url", "env": "GEMINI_OFFICIAL_BASE_URL", "default": "https://generativelanguage.googleapis.com"},
    {"provider": "gpt", "proxy": "OpenAI", "role": "url", "env": "GPT_BASE_URL", "default": "https://api.openai.com/v1"},
    {"provider": "grok", "proxy": "Official", "role": "url", "env": "GROK_BASE_URL", "default": "https://api.x.ai/v1"},
    {"provider": "deepseek", "proxy": "Official", "role": "url", "env": "DEEPSEEK_BASE_URL", "default": "https://api.deepseek.com"},
    {"provider": "glm", "proxy": "Official", "role": "url", "env": "GLM_BASE_URL", "default": "https://open.bigmodel.cn/api/paas/v4"},
    {"provider": "qwen", "proxy": "Official", "role": "url", "env": "QWEN_BASE_URL", "default": "https://maas.qianwenaiapi.com/compatible-mode/v1"},
    {"provider": "magmarouter", "proxy": "MagmaRouter", "role": "url", "env": "MAGMAROUTER_BASE_URL", "default": "https://api.magmarouter.com/v1"},
    {"provider": "openrouter", "proxy": "OpenRouter", "role": "url", "env": "OPENROUTER_BASE_URL", "default": "https://openrouter.ai/api/v1"},
]

__all__ += ["_PROXY_CONFIG_WARNINGS"]
