# SPDX-License-Identifier: AGPL-3.0-only


from __future__ import annotations

import os
import json
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
    "CLAUDE_CODEYU_BASE_URL",
    "CLAUDE_CODEYU_BASE_API_KEY",
    "CLAUDE_CODEYU_PRIME_API_KEY",
    "CLAUDE_CODEYU_MAX_API_KEY",
    "CLAUDE_CODERPLAN_BASE_URL",
    "CLAUDE_CODERPLAN_API_KEY",
    "CLAUDE_CODEYU_TIERS",
    "CLAUDE_CODERPLAN",
    "CLAUDE_PROXIES",
    "GEMINI_CODEYU_BASE_URL",
    "GEMINI_CODEYU_API_KEY",
    "GEMINI_CODEYU",
    "GEMINI_CODERPLAN_BASE_URL",
    "GEMINI_CODERPLAN_API_KEY",
    "GEMINI_CODERPLAN",
    "GEMINI_OPENROUTER",
    "GEMINI_PROXIES",
    "GPT_CODEYU_BASE_URL",
    "GPT_CODEYU_API_KEY",
    "GPT_CODEYU",
    "GPT_CODERPLAN_BASE_URL",
    "GPT_CODERPLAN_API_KEY",
    "GPT_CODERPLAN",
    "GPT_PROXIES",
    "GROK_CODEYU_BASE_URL",
    "GROK_CODEYU_API_KEY",
    "GROK_CODEYU",
    "GROK_CODERPLAN_BASE_URL",
    "GROK_CODERPLAN_API_KEY",
    "GROK_CODERPLAN",
    "GROK_PROXIES",
    "DEEPSEEK_PROXIES",
    "ProviderPathPolicy",
    "PROVIDER_PATH_POLICIES",
    "PROVIDER_PROXIES",
    "PROVIDER_PATH_ORDER",
    "PROVIDER_ROUTE_CATALOG",
    "PROVIDER_ROUTE_POLICIES",
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


CLAUDE_OFFICIAL_BASE_URL = _env(
    "CLAUDE_OFFICIAL_BASE_URL", "CLAUDE_BASE_URL", default="https://api.anthropic.com"
)
GEMINI_OFFICIAL_BASE_URL = _env(
    "GEMINI_OFFICIAL_BASE_URL",
    "GEMINI_BASE_URL",
    default="https://generativelanguage.googleapis.com",
)


CODERPLAN_DEFAULT_BASE_URL = "https://api.coderplan.ai"


def _ensure_openai_v1(url: str | None) -> str | None:
    """OpenAI 兼容代理的 base_url 纯字符串兜底:URL 里没有 ``/v1`` 就补上。

    coderplan 这类网关的 OpenAI 兼容端点是挂在 ``/v1`` 下的
    (``/v1/models``、``/v1/chat/completions``)。配置里若只写了根域名,
    OpenAI SDK **不会**替你补 ``/v1``,请求会落到 ``/chat/completions`` 直接 404;
    探活拼 ``{base}/models`` 同样 404,还会被 fail-open 记成「没结论」。
    这里只看字符串(不发网络请求):已含 ``/v1`` 的原样返回,否则末尾拼 ``/v1``。

    用于 GPT / Grok 的 OpenAI 兼容代理，以及 Dragon 的 Gemini（该网关只开
    OpenAI 协议）。Claude / Gemini 走原生协议的代理通路不能套这条规则:
    协议客户端自己会带 ``/v1`` / ``/v1beta``。
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
QWEN_BASE_URL = _env("QWEN_BASE_URL", default="https://maas.qianwenaiapi.com/compatible-mode/v1")


MAGMAROUTER_API_KEY = _env("MAGMAROUTER_API_KEY")
MAGMAROUTER_BASE_URL = _env("MAGMAROUTER_BASE_URL", default="https://api.magmarouter.com/v1")


OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1")


CLAUDE_CODEYU_BASE_URL = _env("CLAUDE_CODEYU_BASE_URL")
CLAUDE_CODEYU_BASE_API_KEY = _env("CLAUDE_CODEYU_BASE_API_KEY")
CLAUDE_CODEYU_PRIME_API_KEY = _env("CLAUDE_CODEYU_PRIME_API_KEY")
CLAUDE_CODEYU_MAX_API_KEY = _env("CLAUDE_CODEYU_MAX_API_KEY")


CLAUDE_CODERPLAN_BASE_URL = _env("CLAUDE_CODERPLAN_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
CLAUDE_CODERPLAN_API_KEY = _env("CLAUDE_CODERPLAN_API_KEY")

# To-Tokens is first in the fallback order; route labels use the upstream site.
CLAUDE_TOTOKENS_BASE_URL = _env("CLAUDE_TOTOKENS_BASE_URL", default="https://drc.totokens.cc")
CLAUDE_TOTOKENS_API_KEY = _env("CLAUDE_TOTOKENS_API_KEY")
CLAUDE_TOTOKENS = (
    {
        "name": "To-Tokens",
        "api_key": CLAUDE_TOTOKENS_API_KEY,
        "route_id": "totokens",
        "base_url": CLAUDE_TOTOKENS_BASE_URL,
    }
    if CLAUDE_TOTOKENS_API_KEY and CLAUDE_TOTOKENS_BASE_URL
    else None
)

CLAUDE_CODEYU_TIERS: dict[str, dict] = {}
if CLAUDE_CODEYU_BASE_URL:
    if CLAUDE_CODEYU_BASE_API_KEY:
        CLAUDE_CODEYU_TIERS["base"] = {
            "name": "Codeyu Base",
            "route_id": "codeyu_base",
            "api_key": CLAUDE_CODEYU_BASE_API_KEY,
            "base_url": CLAUDE_CODEYU_BASE_URL,
        }
    if CLAUDE_CODEYU_PRIME_API_KEY:
        CLAUDE_CODEYU_TIERS["prime"] = {
            "name": "Codeyu Prime",
            "route_id": "codeyu_prime",
            "api_key": CLAUDE_CODEYU_PRIME_API_KEY,
            "base_url": CLAUDE_CODEYU_BASE_URL,
        }
    if CLAUDE_CODEYU_MAX_API_KEY:
        CLAUDE_CODEYU_TIERS["max"] = {
            "name": "Codeyu Max",
            "route_id": "codeyu_max",
            "api_key": CLAUDE_CODEYU_MAX_API_KEY,
            "base_url": CLAUDE_CODEYU_BASE_URL,
        }

CLAUDE_CODERPLAN: dict | None = None
if CLAUDE_CODERPLAN_API_KEY and CLAUDE_CODERPLAN_BASE_URL:
    CLAUDE_CODERPLAN = {
        "name": "CoderPlan",
        "route_id": "coderplan",
        "api_key": CLAUDE_CODERPLAN_API_KEY,
        "base_url": CLAUDE_CODERPLAN_BASE_URL,
    }


CLAUDE_77_BASE_URL = _env("CLAUDE_77_BASE_URL", default="https://api.a77ai.cn")
CLAUDE_77_API_KEY = _env("CLAUDE_77_API_KEY")
CLAUDE_77: dict | None = None
if CLAUDE_77_API_KEY and CLAUDE_77_BASE_URL:
    CLAUDE_77 = {
        "name": "77",
        "route_id": "77",
        "api_key": CLAUDE_77_API_KEY,
        "base_url": CLAUDE_77_BASE_URL,
    }

CLAUDE_DRAGON_BASE_URL = _env("CLAUDE_DRAGON_BASE_URL", default="https://newapi.dragon3api.com")
CLAUDE_DRAGON_API_KEY = _env("CLAUDE_DRAGON_API_KEY")
CLAUDE_DRAGON: dict | None = None
if CLAUDE_DRAGON_API_KEY and CLAUDE_DRAGON_BASE_URL:
    CLAUDE_DRAGON = {
        "name": "Dragon",
        "route_id": "dragon",
        "api_key": CLAUDE_DRAGON_API_KEY,
        "base_url": CLAUDE_DRAGON_BASE_URL,
    }


_PROXY_CONFIG_WARNINGS: list[str] = []
if CLAUDE_CODEYU_BASE_URL and not any(
    (CLAUDE_CODEYU_BASE_API_KEY, CLAUDE_CODEYU_PRIME_API_KEY, CLAUDE_CODEYU_MAX_API_KEY)
):
    _PROXY_CONFIG_WARNINGS.append(
        "CLAUDE_CODEYU_BASE_URL 已设置但没有任何 Codeyu 档位 key：Codeyu 被静默禁用。"
    )


CLAUDE_PROXIES: list[dict] = []
if CLAUDE_TOTOKENS is not None:
    CLAUDE_PROXIES.append(dict(CLAUDE_TOTOKENS))
for _tier in ("base", "prime", "max"):
    if _tier in CLAUDE_CODEYU_TIERS:
        CLAUDE_PROXIES.append(
            {
                "name": "Codeyu",
                "route_id": f"codeyu_{_tier}",
                "api_key": CLAUDE_CODEYU_TIERS[_tier]["api_key"],
                "base_url": CLAUDE_CODEYU_TIERS[_tier]["base_url"],
            }
        )
        break
if CLAUDE_CODERPLAN is not None:
    CLAUDE_PROXIES.append(dict(CLAUDE_CODERPLAN))
if CLAUDE_77 is not None:
    CLAUDE_PROXIES.append(dict(CLAUDE_77))
if CLAUDE_DRAGON is not None:
    CLAUDE_PROXIES.append(dict(CLAUDE_DRAGON))


GEMINI_CODEYU_BASE_URL = _env("GEMINI_CODEYU_BASE_URL", default="https://codeyu.shop")
GEMINI_CODEYU_API_KEY = _env("GEMINI_CODEYU_API_KEY")
GEMINI_CODEYU: dict | None = None
if GEMINI_CODEYU_BASE_URL and GEMINI_CODEYU_API_KEY:
    GEMINI_CODEYU = {
        "name": "Codeyu",
        "route_id": "codeyu",
        "api_key": GEMINI_CODEYU_API_KEY,
        "base_url": GEMINI_CODEYU_BASE_URL,
    }


GEMINI_CODERPLAN_BASE_URL = _env("GEMINI_CODERPLAN_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
GEMINI_CODERPLAN_API_KEY = _env("GEMINI_CODERPLAN_API_KEY")
GEMINI_TOTOKENS_BASE_URL = _env("GEMINI_TOTOKENS_BASE_URL", default="https://drc.totokens.cc")
GEMINI_TOTOKENS_API_KEY = _env("GEMINI_TOTOKENS_API_KEY")
GEMINI_TOTOKENS = (
    {
        "name": "To-Tokens",
        "api_key": GEMINI_TOTOKENS_API_KEY,
        "route_id": "totokens",
        "base_url": GEMINI_TOTOKENS_BASE_URL,
    }
    if GEMINI_TOTOKENS_API_KEY and GEMINI_TOTOKENS_BASE_URL
    else None
)
GEMINI_CODERPLAN: dict | None = None
if GEMINI_CODERPLAN_API_KEY and GEMINI_CODERPLAN_BASE_URL:
    GEMINI_CODERPLAN = {
        "name": "CoderPlan",
        "route_id": "coderplan",
        "api_key": GEMINI_CODERPLAN_API_KEY,
        "base_url": GEMINI_CODERPLAN_BASE_URL,
    }


GEMINI_DRAGON_BASE_URL = _ensure_openai_v1(
    _env("GEMINI_DRAGON_BASE_URL", default="https://newapi.dragon3api.com")
)
GEMINI_DRAGON_API_KEY = _env("GEMINI_DRAGON_API_KEY")
GEMINI_DRAGON: dict | None = None
if GEMINI_DRAGON_API_KEY and GEMINI_DRAGON_BASE_URL:
    GEMINI_DRAGON = {
        "name": "Dragon",
        "route_id": "dragon",
        "api_key": GEMINI_DRAGON_API_KEY,
        "base_url": GEMINI_DRAGON_BASE_URL,
        # Dragon 的 Gemini 只开 OpenAI 兼容协议：走 chat.completions，
        # 模型名用原生 id（不加 google/ 前缀）。
        "protocol": "openai",
        "native_model_ids": True,
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
if GEMINI_TOTOKENS is not None:
    GEMINI_PROXIES.append(dict(GEMINI_TOTOKENS))
if GEMINI_CODEYU is not None:
    GEMINI_PROXIES.append(dict(GEMINI_CODEYU))
if GEMINI_CODERPLAN is not None:
    GEMINI_PROXIES.append(dict(GEMINI_CODERPLAN))
if GEMINI_DRAGON is not None:
    GEMINI_PROXIES.append(dict(GEMINI_DRAGON))

if GEMINI_CODERPLAN_BASE_URL and not GEMINI_CODERPLAN_API_KEY:
    if GEMINI_CODERPLAN_BASE_URL != CODERPLAN_DEFAULT_BASE_URL:
        _PROXY_CONFIG_WARNINGS.append(
            "GEMINI_CODERPLAN_BASE_URL 已设置但 GEMINI_CODERPLAN_API_KEY 缺失：Gemini CoderPlan 被禁用。"
        )


GPT_CODEYU_BASE_URL = _ensure_openai_v1(
    _env("GPT_CODEYU_BASE_URL", default="https://codeyu.shop/v1")
)
GPT_CODEYU_API_KEY = _env("GPT_CODEYU_API_KEY")
GPT_CODEYU: dict | None = None
if GPT_CODEYU_API_KEY and GPT_CODEYU_BASE_URL:
    GPT_CODEYU = {
        "name": "Codeyu",
        "route_id": "codeyu",
        "api_key": GPT_CODEYU_API_KEY,
        "base_url": GPT_CODEYU_BASE_URL,
    }


GPT_CODERPLAN_BASE_URL = _ensure_openai_v1(
    _env("GPT_CODERPLAN_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
)
GPT_CODERPLAN_API_KEY = _env("GPT_CODERPLAN_API_KEY")
GPT_TOTOKENS_BASE_URL = _ensure_openai_v1(
    _env("GPT_TOTOKENS_BASE_URL", default="https://drc.totokens.cc/v1")
)
GPT_TOTOKENS_API_KEY = _env("GPT_TOTOKENS_API_KEY")
GPT_TOTOKENS_PROTOCOL = _env("GPT_TOTOKENS_PROTOCOL", default="responses")
GPT_TOTOKENS = (
    {
        "name": "To-Tokens",
        "api_key": GPT_TOTOKENS_API_KEY,
        "route_id": "totokens",
        "base_url": GPT_TOTOKENS_BASE_URL,
        "protocol": GPT_TOTOKENS_PROTOCOL,
    }
    if GPT_TOTOKENS_API_KEY and GPT_TOTOKENS_BASE_URL
    else None
)
GPT_CODERPLAN: dict | None = None
if GPT_CODERPLAN_API_KEY and GPT_CODERPLAN_BASE_URL:
    GPT_CODERPLAN = {
        "name": "CoderPlan",
        "route_id": "coderplan",
        "api_key": GPT_CODERPLAN_API_KEY,
        "base_url": GPT_CODERPLAN_BASE_URL,
    }


GPT_77_BASE_URL = _ensure_openai_v1(_env("GPT_77_BASE_URL", default="https://api.a77ai.cn"))
GPT_77_API_KEY = _env("GPT_77_API_KEY")
GPT_77: dict | None = None
if GPT_77_API_KEY and GPT_77_BASE_URL:
    GPT_77 = {
        "name": "77",
        "route_id": "77",
        "api_key": GPT_77_API_KEY,
        "base_url": GPT_77_BASE_URL,
    }

GPT_DRAGON_BASE_URL = _ensure_openai_v1(
    _env("GPT_DRAGON_BASE_URL", default="https://newapi.dragon3api.com")
)
GPT_DRAGON_API_KEY = _env("GPT_DRAGON_API_KEY")
GPT_DRAGON: dict | None = None
if GPT_DRAGON_API_KEY and GPT_DRAGON_BASE_URL:
    GPT_DRAGON = {
        "name": "Dragon",
        "route_id": "dragon",
        "api_key": GPT_DRAGON_API_KEY,
        "base_url": GPT_DRAGON_BASE_URL,
    }


GPT_PROXIES: list[dict] = []
if GPT_TOTOKENS is not None:
    GPT_PROXIES.append(dict(GPT_TOTOKENS))
if GPT_CODEYU is not None:
    GPT_PROXIES.append(dict(GPT_CODEYU))
if GPT_CODERPLAN is not None:
    GPT_PROXIES.append(dict(GPT_CODERPLAN))
if GPT_77 is not None:
    GPT_PROXIES.append(dict(GPT_77))
if GPT_DRAGON is not None:
    GPT_PROXIES.append(dict(GPT_DRAGON))

if GPT_CODERPLAN_BASE_URL and not GPT_CODERPLAN_API_KEY:
    if GPT_CODERPLAN_BASE_URL != _ensure_openai_v1(CODERPLAN_DEFAULT_BASE_URL):
        _PROXY_CONFIG_WARNINGS.append(
            "GPT_CODERPLAN_BASE_URL 已设置但 GPT_CODERPLAN_API_KEY 缺失:GPT CoderPlan 被禁用。"
        )


GROK_CODEYU_BASE_URL = _ensure_openai_v1(
    _env("GROK_CODEYU_BASE_URL", default="https://codeyu.shop/v1")
)
GROK_CODEYU_API_KEY = _env("GROK_CODEYU_API_KEY")
GROK_CODEYU: dict | None = None
if GROK_CODEYU_API_KEY and GROK_CODEYU_BASE_URL:
    GROK_CODEYU = {
        "name": "Codeyu",
        "route_id": "codeyu",
        "api_key": GROK_CODEYU_API_KEY,
        "base_url": GROK_CODEYU_BASE_URL,
    }

GROK_CODERPLAN_BASE_URL = _ensure_openai_v1(
    _env("GROK_CODERPLAN_BASE_URL", default=CODERPLAN_DEFAULT_BASE_URL)
)
GROK_CODERPLAN_API_KEY = _env("GROK_CODERPLAN_API_KEY")
GROK_CODERPLAN: dict | None = None
if GROK_CODERPLAN_API_KEY and GROK_CODERPLAN_BASE_URL:
    GROK_CODERPLAN = {
        "name": "CoderPlan",
        "route_id": "coderplan",
        "api_key": GROK_CODERPLAN_API_KEY,
        "base_url": GROK_CODERPLAN_BASE_URL,
    }
GROK_DRAGON_BASE_URL = _ensure_openai_v1(
    _env("GROK_DRAGON_BASE_URL", default="https://newapi.dragon3api.com")
)
GROK_DRAGON_API_KEY = _env("GROK_DRAGON_API_KEY")
GROK_DRAGON: dict | None = None
if GROK_DRAGON_API_KEY and GROK_DRAGON_BASE_URL:
    GROK_DRAGON = {
        "name": "Dragon",
        "route_id": "dragon",
        "api_key": GROK_DRAGON_API_KEY,
        "base_url": GROK_DRAGON_BASE_URL,
    }
GROK_PROXIES: list[dict] = []
GROK_TOTOKENS_BASE_URL = _ensure_openai_v1(
    _env("GROK_TOTOKENS_BASE_URL", default="https://drc.totokens.cc/v1")
)
GROK_TOTOKENS_API_KEY = _env("GROK_TOTOKENS_API_KEY")
GROK_TOTOKENS_PROTOCOL = _env("GROK_TOTOKENS_PROTOCOL", default="responses")
GROK_TOTOKENS = (
    {
        "name": "To-Tokens",
        "api_key": GROK_TOTOKENS_API_KEY,
        "route_id": "totokens",
        "base_url": GROK_TOTOKENS_BASE_URL,
        "protocol": GROK_TOTOKENS_PROTOCOL,
    }
    if GROK_TOTOKENS_API_KEY and GROK_TOTOKENS_BASE_URL
    else None
)
if GROK_TOTOKENS is not None:
    GROK_PROXIES.append(dict(GROK_TOTOKENS))
if GROK_CODERPLAN is not None:
    GROK_PROXIES.append(dict(GROK_CODERPLAN))
if GROK_CODEYU is not None:
    GROK_PROXIES.insert(0, dict(GROK_CODEYU))
if GROK_DRAGON is not None:
    GROK_PROXIES.append(dict(GROK_DRAGON))


DEEPSEEK_PROXIES: list[dict] = []
if FIREWORKS_API_KEY and FIREWORKS_BASE_URL:
    DEEPSEEK_PROXIES.append(
        {
            "name": "Fireworks DeepSeek Pro",
            "route_id": "fireworks_pro",
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
        "To-Tokens → Codeyu 三档 → CoderPlan → OpenRouter → 官方(官方仅 sonnet 系列)",
    ),
    "gemini": ProviderPathPolicy(
        "gemini",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "Official",
        "google/",
        True,
        "To-Tokens → Codeyu → CoderPlan → OpenRouter → 官方",
    ),
    "grok": ProviderPathPolicy(
        "grok",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "Official",
        "x-ai/",
        True,
        "Codeyu → To-Tokens → CoderPlan → OpenRouter → 官方",
    ),
    "gpt": ProviderPathPolicy(
        "gpt",
        _PROXY_THEN_OR_THEN_OFFICIAL,
        "OpenAI",
        "openai/",
        True,
        "To-Tokens → Codeyu → CoderPlan→ OpenRouter → 官方",
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

PROVIDER_ROUTE_CATALOG = {
    "claude": (
        "totokens",
        "codeyu_base",
        "codeyu_prime",
        "codeyu_max",
        "coderplan",
        "77",
        "dragon",
        "openrouter",
        "official",
    ),
    "gemini": ("totokens", "codeyu", "coderplan", "dragon", "openrouter", "official"),
    "gpt": ("totokens", "codeyu", "coderplan", "77", "dragon", "openrouter", "official"),
    "grok": ("codeyu", "totokens", "coderplan", "dragon", "openrouter", "official"),
    "deepseek": ("official", "fireworks_pro", "openrouter"),
    "glm": ("official", "openrouter"),
    "qwen": ("official",),
    "magmarouter": ("official",),
    "openrouter": ("official",),
}


def _load_provider_route_policies():
    try:
        raw = json.loads(os.getenv("RP_PROVIDER_PATH_POLICIES", "{}"))
    except (TypeError, ValueError):
        raw = {}
    result = {}
    for provider, routes in PROVIDER_ROUTE_CATALOG.items():
        supplied = raw.get(provider, {}) if isinstance(raw, dict) else {}
        if not isinstance(supplied, dict):
            supplied = {}
        order, disabled = supplied.get("order", []), supplied.get("disabled", [])
        if not isinstance(order, list) or any(route not in routes for route in order):
            order = []
        if not isinstance(disabled, list) or any(route not in routes for route in disabled):
            disabled = []
        result[provider] = {"order": tuple(order), "disabled": frozenset(disabled)}
    return result


PROVIDER_ROUTE_POLICIES = _load_provider_route_policies()


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
    {
        "provider": "claude",
        "proxy": "Codeyu Base",
        "role": "url",
        "env": "CLAUDE_CODEYU_BASE_URL",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "Codeyu Base",
        "role": "key",
        "env": "CLAUDE_CODEYU_BASE_API_KEY",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "Codeyu Prime",
        "role": "key",
        "env": "CLAUDE_CODEYU_PRIME_API_KEY",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "Codeyu Max",
        "role": "key",
        "env": "CLAUDE_CODEYU_MAX_API_KEY",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "CoderPlan",
        "role": "url",
        "env": "CLAUDE_CODERPLAN_BASE_URL",
        "default": "https://api.coderplan.ai",
    },
    {
        "provider": "claude",
        "proxy": "CoderPlan",
        "role": "key",
        "env": "CLAUDE_CODERPLAN_API_KEY",
        "default": "",
    },
    {
        "provider": "gemini",
        "proxy": "Codeyu",
        "role": "url",
        "env": "GEMINI_CODEYU_BASE_URL",
        "default": "https://codeyu.shop",
    },
    {
        "provider": "gemini",
        "proxy": "Codeyu",
        "role": "key",
        "env": "GEMINI_CODEYU_API_KEY",
        "default": "",
    },
    {
        "provider": "gemini",
        "proxy": "CoderPlan",
        "role": "url",
        "env": "GEMINI_CODERPLAN_BASE_URL",
        "default": "https://api.coderplan.ai",
    },
    {
        "provider": "gemini",
        "proxy": "CoderPlan",
        "role": "key",
        "env": "GEMINI_CODERPLAN_API_KEY",
        "default": "",
    },
    {
        "provider": "gpt",
        "proxy": "Codeyu",
        "role": "url",
        "env": "GPT_CODEYU_BASE_URL",
        "default": "https://codeyu.shop/v1",
    },
    {
        "provider": "gpt",
        "proxy": "Codeyu",
        "role": "key",
        "env": "GPT_CODEYU_API_KEY",
        "default": "",
    },
    {
        "provider": "gpt",
        "proxy": "CoderPlan",
        "role": "url",
        "env": "GPT_CODERPLAN_BASE_URL",
        "default": "https://api.coderplan.ai/v1",
    },
    {
        "provider": "gpt",
        "proxy": "CoderPlan",
        "role": "key",
        "env": "GPT_CODERPLAN_API_KEY",
        "default": "",
    },
    {
        "provider": "grok",
        "proxy": "Codeyu",
        "role": "url",
        "env": "GROK_CODEYU_BASE_URL",
        "default": "https://codeyu.shop/v1",
    },
    {
        "provider": "grok",
        "proxy": "Codeyu",
        "role": "key",
        "env": "GROK_CODEYU_API_KEY",
        "default": "",
    },
    {
        "provider": "grok",
        "proxy": "CoderPlan",
        "role": "url",
        "env": "GROK_CODERPLAN_BASE_URL",
        "default": "https://api.coderplan.ai/v1",
    },
    {
        "provider": "grok",
        "proxy": "CoderPlan",
        "role": "key",
        "env": "GROK_CODERPLAN_API_KEY",
        "default": "",
    },
    {
        "provider": "deepseek",
        "proxy": "Fireworks DeepSeek Pro",
        "role": "url",
        "env": "FIREWORKS_BASE_URL",
        "default": "",
    },
    {
        "provider": "deepseek",
        "proxy": "Fireworks DeepSeek Pro",
        "role": "key",
        "env": "FIREWORKS_API_KEY",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "Official",
        "role": "url",
        "env": "CLAUDE_OFFICIAL_BASE_URL",
        "default": "https://api.anthropic.com",
    },
    {
        "provider": "gemini",
        "proxy": "Official",
        "role": "url",
        "env": "GEMINI_OFFICIAL_BASE_URL",
        "default": "https://generativelanguage.googleapis.com",
    },
    {
        "provider": "gpt",
        "proxy": "OpenAI",
        "role": "url",
        "env": "GPT_BASE_URL",
        "default": "https://api.openai.com/v1",
    },
    {
        "provider": "grok",
        "proxy": "Official",
        "role": "url",
        "env": "GROK_BASE_URL",
        "default": "https://api.x.ai/v1",
    },
    {
        "provider": "deepseek",
        "proxy": "Official",
        "role": "url",
        "env": "DEEPSEEK_BASE_URL",
        "default": "https://api.deepseek.com",
    },
    {
        "provider": "glm",
        "proxy": "Official",
        "role": "url",
        "env": "GLM_BASE_URL",
        "default": "https://open.bigmodel.cn/api/paas/v4",
    },
    {
        "provider": "qwen",
        "proxy": "Official",
        "role": "url",
        "env": "QWEN_BASE_URL",
        "default": "https://maas.qianwenaiapi.com/compatible-mode/v1",
    },
    {
        "provider": "magmarouter",
        "proxy": "MagmaRouter",
        "role": "url",
        "env": "MAGMAROUTER_BASE_URL",
        "default": "https://api.magmarouter.com/v1",
    },
    {
        "provider": "openrouter",
        "proxy": "OpenRouter",
        "role": "url",
        "env": "OPENROUTER_BASE_URL",
        "default": "https://openrouter.ai/api/v1",
    },
]

__all__ += ["_PROXY_CONFIG_WARNINGS"]
__all__ += [
    "CLAUDE_TOTOKENS_BASE_URL",
    "CLAUDE_TOTOKENS_API_KEY",
    "CLAUDE_TOTOKENS",
    "GEMINI_TOTOKENS_BASE_URL",
    "GEMINI_TOTOKENS_API_KEY",
    "GEMINI_TOTOKENS",
    "GPT_TOTOKENS_BASE_URL",
    "GPT_TOTOKENS_API_KEY",
    "GPT_TOTOKENS_PROTOCOL",
    "GPT_TOTOKENS",
    "GROK_TOTOKENS_BASE_URL",
    "GROK_TOTOKENS_API_KEY",
    "GROK_TOTOKENS_PROTOCOL",
    "GROK_TOTOKENS",
]
PROXY_ENV_SPECS += [
    {
        "provider": "claude",
        "proxy": "To-Tokens",
        "role": "url",
        "env": "CLAUDE_TOTOKENS_BASE_URL",
        "default": "https://drc.totokens.cc",
    },
    {
        "provider": "claude",
        "proxy": "To-Tokens",
        "role": "key",
        "env": "CLAUDE_TOTOKENS_API_KEY",
        "default": "",
    },
    {
        "provider": "gemini",
        "proxy": "To-Tokens",
        "role": "url",
        "env": "GEMINI_TOTOKENS_BASE_URL",
        "default": "https://drc.totokens.cc",
    },
    {
        "provider": "gemini",
        "proxy": "To-Tokens",
        "role": "key",
        "env": "GEMINI_TOTOKENS_API_KEY",
        "default": "",
    },
    {
        "provider": "gpt",
        "proxy": "To-Tokens",
        "role": "url",
        "env": "GPT_TOTOKENS_BASE_URL",
        "default": "https://drc.totokens.cc/v1",
    },
    {
        "provider": "gpt",
        "proxy": "To-Tokens",
        "role": "key",
        "env": "GPT_TOTOKENS_API_KEY",
        "default": "",
    },
    {
        "provider": "grok",
        "proxy": "To-Tokens",
        "role": "url",
        "env": "GROK_TOTOKENS_BASE_URL",
        "default": "https://drc.totokens.cc/v1",
    },
    {
        "provider": "grok",
        "proxy": "To-Tokens",
        "role": "key",
        "env": "GROK_TOTOKENS_API_KEY",
        "default": "",
    },
]


# 77 / Dragon 是后加的两家 OpenAI 兼容网关：Claude 走 anthropic 协议，
# 其余 provider 走 OpenAI 兼容协议（GPT/Grok 的 base_url 由 _ensure_openai_v1 补 /v1）。
PROXY_ENV_SPECS += [
    {
        "provider": "claude",
        "proxy": "77",
        "role": "url",
        "env": "CLAUDE_77_BASE_URL",
        "default": "https://api.a77ai.cn",
    },
    {
        "provider": "claude",
        "proxy": "77",
        "role": "key",
        "env": "CLAUDE_77_API_KEY",
        "default": "",
    },
    {
        "provider": "gpt",
        "proxy": "77",
        "role": "url",
        "env": "GPT_77_BASE_URL",
        "default": "https://api.a77ai.cn/v1",
    },
    {
        "provider": "gpt",
        "proxy": "77",
        "role": "key",
        "env": "GPT_77_API_KEY",
        "default": "",
    },
    {
        "provider": "claude",
        "proxy": "Dragon",
        "role": "url",
        "env": "CLAUDE_DRAGON_BASE_URL",
        "default": "https://newapi.dragon3api.com",
    },
    {
        "provider": "claude",
        "proxy": "Dragon",
        "role": "key",
        "env": "CLAUDE_DRAGON_API_KEY",
        "default": "",
    },
    {
        "provider": "gemini",
        "proxy": "Dragon",
        "role": "url",
        "env": "GEMINI_DRAGON_BASE_URL",
        "default": "https://newapi.dragon3api.com/v1",
    },
    {
        "provider": "gemini",
        "proxy": "Dragon",
        "role": "key",
        "env": "GEMINI_DRAGON_API_KEY",
        "default": "",
    },
    {
        "provider": "gpt",
        "proxy": "Dragon",
        "role": "url",
        "env": "GPT_DRAGON_BASE_URL",
        "default": "https://newapi.dragon3api.com/v1",
    },
    {
        "provider": "gpt",
        "proxy": "Dragon",
        "role": "key",
        "env": "GPT_DRAGON_API_KEY",
        "default": "",
    },
    {
        "provider": "grok",
        "proxy": "Dragon",
        "role": "url",
        "env": "GROK_DRAGON_BASE_URL",
        "default": "https://newapi.dragon3api.com/v1",
    },
    {
        "provider": "grok",
        "proxy": "Dragon",
        "role": "key",
        "env": "GROK_DRAGON_API_KEY",
        "default": "",
    },
]


__all__ += [
    "CLAUDE_77_BASE_URL",
    "CLAUDE_77_API_KEY",
    "CLAUDE_77",
    "CLAUDE_DRAGON_BASE_URL",
    "CLAUDE_DRAGON_API_KEY",
    "CLAUDE_DRAGON",
    "GPT_77_BASE_URL",
    "GPT_77_API_KEY",
    "GPT_77",
    "GPT_DRAGON_BASE_URL",
    "GPT_DRAGON_API_KEY",
    "GPT_DRAGON",
    "GEMINI_DRAGON_BASE_URL",
    "GEMINI_DRAGON_API_KEY",
    "GEMINI_DRAGON",
    "GROK_DRAGON_BASE_URL",
    "GROK_DRAGON_API_KEY",
    "GROK_DRAGON",
]
