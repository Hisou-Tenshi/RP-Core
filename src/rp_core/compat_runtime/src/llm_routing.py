# SPDX-License-Identifier: AGPL-3.0-only


from __future__ import annotations

import time
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


class NoClaudeClientError(RuntimeError):
    """没有任何可用的 Claude 客户端（代理全灭 且 官方直连不可用/未配置）。

    这是 *配置/可用性* 失败，不是模型输出。调用方必须把它当作失败处理并继续降级，
    绝不能把异常文本当作回复内容返回给用户。

    背景（2026-09-10 事故）：`call_claude` 曾在无 client 时 `return
    "Error: No API keys configured."`，该字符串被主降级链判定为「生成成功」，
    直接发给了用户，并且消息被标记为已回复、不再重试。
    """


class EmptyModelResponseError(RuntimeError):
    """模型返回了「HTTP 成功但内容为空」的响应。

    这是 2026-10-29 事故的直接教训：claude-sonnet-5-5 在 Codeyu Prime 上拿到
    200 + 空流（无 text / 无 thinking / 无 tool_use），旧代码把它当「成功」直接
    return，导致同一次调用里 Prime 之后的 Max → CoderPlan → OpenRouter → Official
    全部被短路，主链直接跳去下一个模型的容灾组。

    语义：这**不是** provider 级确定性错误（换个模型/通路可能完全正常），
    所以 `should_skip_provider_on_error` 不会因它熔断整厂；它只是
    「这一次尝试失败」，由调用方的既有降级循环继续走下一个候选。
    """

    def __init__(self, label: str):
        super().__init__(f"{label} returned a completed response with no content")
        self.label = label


def require_nonempty_model_text(text, label: str) -> str:
    """空内容校验的唯一入口：text 为空（strip 后）就抛 EmptyModelResponseError。

    所有「一次调用 = 一次尝试」的降级层都应在返回前过这一道：
    空响应必须被当成该次尝试失败，让降级循环继续走下一个通路/模型，
    而不是被当成成功短路掉后面整条链（2026-10-29 事故）。
    """
    if not (text or "").strip():
        raise EmptyModelResponseError(label)
    return text


def is_no_claude_client_error(exc: BaseException) -> bool:
    """判断异常是否为「无可用 Claude 客户端」。"""
    return isinstance(exc, NoClaudeClientError)


def is_claude_thinking_model(model_id: str) -> bool:
    """模型名含 thinking 时仅走代理（官方无单分 thinking）。"""
    return "thinking" in (model_id or "").lower()


def is_claude_fable_model(model_id: str) -> bool:
    """Fable 系列：Codeyu 走 Prime → Max（不用 Base）。"""
    return "fable" in (model_id or "").lower()


def should_skip_claude_client(model_id: str, client_name: str) -> bool:
    if client_name == "Official" and is_claude_thinking_model(model_id):
        return True
    return False


def build_claude_proxy_configs_for_model(model_id: str) -> list[dict[str, Any]]:
    """
    To-Tokens 固定优先，再按模型选择 Codeyu 档位顺序，最后接 CoderPlan：
    - Fable（5.1）：Prime → Max → CoderPlan
    - Opus / Sonnet 及其他：Base → Prime → Max → CoderPlan
    """
    import tenshi_config

    tiers = getattr(tenshi_config, "CLAUDE_CODEYU_TIERS", {}) or {}
    coderplan = getattr(tenshi_config, "CLAUDE_CODERPLAN", None)
    order = ("prime", "max") if is_claude_fable_model(model_id) else ("base", "prime", "max")

    out: list[dict[str, Any]] = []
    totokens = getattr(tenshi_config, "CLAUDE_TOTOKENS", None)
    if totokens and totokens.get("api_key") and totokens.get("base_url"):
        out.append(totokens)
    for tier in order:
        conf = tiers.get(tier)
        if conf and conf.get("api_key") and conf.get("base_url"):
            out.append(conf)
    if coderplan and coderplan.get("api_key") and coderplan.get("base_url"):
        out.append(coderplan)
    return out


def dedupe_model_ids(models: Iterable[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in models or []:
        if isinstance(m, str) and m.strip() and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def claude_model_chain(
    preferred: str | None = None,
    *,
    start_index: int = 0,
    base_list: Iterable[str] | None = None,
) -> list[str]:
    """Claude 降级列表：preferred 置顶，再按 base_list（默认 claude_dialog_models）去重。"""
    import tenshi_config

    base = list(
        base_list
        if base_list is not None
        else tenshi_config.claude_dialog_models()
    )
    if preferred and preferred.strip():
        pref = preferred.strip()
        base = [pref] + [m for m in base if m != pref]
    if start_index > 0:
        base = base[start_index:] if start_index < len(base) else ([base[-1]] if base else [])
    return dedupe_model_ids(base)


def claude_summary_model_chain() -> list[str]:
    """log_update 总结用 Claude 链(实现集中在 config/models.py)。"""
    import tenshi_config

    return tenshi_config.claude_summary_model_chain()


def try_openrouter_fallback_path(
    model_id: str,
    invoke: Callable[[str, dict[str, Any]], T],
    *,
    log: Any = None,
) -> T | None:
    """原生模型失败后,用集中映射的 OpenRouter 同款模型再试一次。

    这是**所有非 path-first 调用点**的统一兜底入口(log_update / context_compression /
    bond_reflect / 辅助链 / 最后手段 / 前置分流等),保证「除 embedding 外都有 OR 兜底」。

    invoke(openrouter_slug, path) 执行真正的调用。
    返回 None 表示没有可用 OR 通路(未配置 key / 无同款 slug / 探活红 / 全部失败),
    调用方据此继续原有降级逻辑。
    """
    import provider_health

    import tenshi_config

    slug = tenshi_config.openrouter_fallback_slug(model_id)
    if not slug:
        return None
    api_key = getattr(tenshi_config, "OPENROUTER_API_KEY", None)
    if not api_key:
        return None
    paths = provider_health.filter_paths(
        "openrouter",
        build_openrouter_paths(api_key, tenshi_config.OPENROUTER_BASE_URL),
    )
    if not paths:
        if log is not None:
            log.warning(f"[openrouter-fallback] {model_id} 没有可用 OpenRouter 通路（探活红/未配置）。")
        return None
    last_err: BaseException | None = None
    for path in paths:
        try:
            return invoke(slug, path)
        except Exception as e:
            last_err = e
            if log is not None:
                from error_text import format_exception_inline

                log.warning(
                    f"[openrouter-fallback] {model_id} -> {slug} via "
                    f"{path.get('name') or '?'} 失败：{format_exception_inline(e)}"
                )
    if last_err is not None and log is not None:
        log.warning(f"[openrouter-fallback] {model_id} 的 OpenRouter 兜底全部失败。")
    return None


def run_claude_model_fallback(
    model_ids: list[str],
    invoke: Callable[[str], T],
    *,
    max_attempts_per_model: int = 1,
    base_sleep: float = 1.0,
    sleep_fn: Callable[[float], None] | None = None,
    log: Any = None,
) -> T:
    """按模型降级；单模型内可重试。404/not_found 立即换下一模型。

    默认 max_attempts_per_model=1：单次尝试、失败即切下一个模型（回复链策略）。
    log_update 等后台任务可显式传更大的值保留重试。

    失败时抛出的异常**带上每一次尝试的完整原文**（含 cause 链，见 error_text），
    而不是只留最后一句说明。
    """
    from error_text import format_errors, format_exception_inline

    sleep = sleep_fn or time.sleep
    last_err: Exception | None = None
    failures: list[tuple[str, BaseException]] = []
    for model_id in model_ids:
        for attempt in range(1, max_attempts_per_model + 1):
            try:
                return invoke(model_id)
            except Exception as e:
                last_err = e
                err_text = str(e).lower()
                failures.append((f"{model_id} (attempt {attempt}/{max_attempts_per_model})", e))
                if log is not None:
                    log.warning(
                        f"Claude ({model_id}) attempt {attempt}/{max_attempts_per_model} failed: "
                        f"{format_exception_inline(e)}"
                    )
                if ("not_found_error" in err_text or "404" in err_text) and attempt == 1:
                    break
                if attempt < max_attempts_per_model:
                    sleep(base_sleep * (2 ** (attempt - 1)))
    raise RuntimeError(
        f"claude failed across all models: {format_errors(failures)}"
    ) from last_err


def build_llm_paths(
    official_key: str | None,
    official_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None,
    *,
    official_name: str = "Official",
    prefer_proxy_first: bool = False,
) -> list[dict[str, Any]]:
    """
    非 Claude：默认官方通路优先，其后与 Claude 相同的代理列表。
    prefer_proxy_first=True 时：代理通路优先，其后再尝试官方通路。
    返回元素: {name, api_key, base_url, is_official}
    """
    paths: list[dict[str, Any]] = []
    official_path = (
        {
            "name": official_name,
            "api_key": official_key,
            "base_url": official_base_url or None,
            "is_official": True,
        }
        if official_key
        else None
    )

    proxy_paths: list[dict[str, Any]] = []
    for proxy in proxy_configs or []:
        api_key = proxy.get("api_key")
        base_url = proxy.get("base_url")
        if not api_key or not base_url:
            continue
        proxy_paths.append(
            {
                "name": proxy.get("name") or "Proxy",
                "api_key": proxy.get("api_key"),
                "base_url": proxy.get("base_url"),
                "is_official": False,


                **{
                    k: v
                    for k, v in proxy.items()
                    if k not in ("name", "api_key", "base_url", "is_official")
                },
            }
        )

    if prefer_proxy_first:
        paths.extend(proxy_paths)
        if official_path is not None:
            paths.append(official_path)
    else:
        if official_path is not None:
            paths.append(official_path)
        paths.extend(proxy_paths)
    return paths


def _proxy_entries(proxy_configs: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把配置层代理 dict 转成统一 path dict(过滤缺 key/base_url 的半配项)。"""
    paths: list[dict[str, Any]] = []
    for proxy in proxy_configs or []:
        api_key = proxy.get("api_key")
        base_url = proxy.get("base_url")
        if not api_key or not base_url:
            continue
        entry = {
            "name": proxy.get("name") or "Proxy",
            "api_key": api_key,
            "base_url": base_url,
            "is_official": False,
        }

        entry.update(
            {k: v for k, v in proxy.items() if k not in ("name", "api_key", "base_url", "is_official")}
        )
        paths.append(entry)
    return paths



def _provider_route_id(provider: str, path: dict[str, Any]) -> str:
    from provider_call_stats import route_id

    return route_id(provider, path)


def apply_provider_route_policy(provider: str, paths: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the console's per-route ordering and disable switches to configured paths."""
    import tenshi_config

    candidates = list(paths or [])
    settings = (getattr(tenshi_config, "PROVIDER_ROUTE_POLICIES", {}) or {}).get(provider, {})
    disabled = set(settings.get("disabled", ()))
    candidates = [path for path in candidates if _provider_route_id(provider, path) not in disabled]
    order = list(settings.get("order", ()))
    if not order:
        return candidates
    rank = {route: index for index, route in enumerate(order)}
    original = {id(path): index for index, path in enumerate(candidates)}

    def route_rank(path):
        route = _provider_route_id(provider, path)
        if route:
            return rank.get(route, len(rank)), 0, original[id(path)]
        # Preserve legacy/unclassified proxy paths ahead of OpenRouter and
        # official routes while still applying configured proxy ordering.
        before_fallback = min(
            (rank[route] for route in ("openrouter", "official") if route in rank),
            default=len(rank),
        )
        return before_fallback, -1, original[id(path)]

    return sorted(candidates, key=route_rank)


def _official_path(
    provider: str,
    official_key: str | None,
    official_base_url: str | None,
    official_name: str | None,
) -> dict[str, Any] | None:
    if not official_key:
        return None
    return {
        "name": official_name or provider,
        "api_key": official_key,
        "base_url": official_base_url or None,
        "is_official": True,
    }


def _openrouter_path(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any] | None:
    """按 provider 的通路策略构造 OpenRouter 兜底 path(不支持/未配置时返回 None)。"""
    import tenshi_config

    policy = getattr(tenshi_config, "PROVIDER_PATH_POLICIES", {}).get(provider)
    if policy is None or not policy.openrouter_allowed:
        return None
    key = api_key if api_key is not None else tenshi_config.OPENROUTER_API_KEY
    url = base_url or tenshi_config.OPENROUTER_BASE_URL
    if not key or not url:
        return None
    return {
        "name": "OpenRouter",
        "api_key": key,
        "base_url": url,
        "is_official": False,
        "protocol": "openai",
        "vendor_prefix": policy.openrouter_vendor_prefix or "",
        "openrouter": True,
    }


def build_provider_paths(
    provider: str,
    *,
    official_key: str | None,
    official_base_url: str | None = None,
    official_name: str | None = None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
    openrouter_key: str | None = None,
    openrouter_base_url: str | None = None,
) -> list[dict[str, Any]]:
    """按 providers.PROVIDER_PATH_POLICIES 拼装某 provider 的通路顺序。

    这是**唯一**的通路拼装入口:代理 / OpenRouter / 官方谁前谁后全部由配置对象决定,
    业务代码不再各自拼列表。返回元素: {name, api_key, base_url, is_official, ...}。
    """
    import tenshi_config

    policy = getattr(tenshi_config, "PROVIDER_PATH_POLICIES", {}).get(provider)
    if policy is None:

        return build_llm_paths(official_key, official_base_url, proxy_configs)

    paths: list[dict[str, Any]] = []
    for token in policy.resolved_order():
        if token == "proxies":
            paths.extend(_proxy_entries(proxy_configs))
        elif token == "openrouter":

            if any(str(p.get("name") or "") == "OpenRouter" or p.get("openrouter") for p in paths):
                continue
            or_path = _openrouter_path(
                provider, api_key=openrouter_key, base_url=openrouter_base_url
            )
            if or_path is not None:
                paths.append(or_path)
        elif token == "official":
            off = _official_path(
                provider, official_key, official_base_url, official_name or policy.official_name
            )
            if off is not None:
                paths.append(off)


    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for path in paths:
        key = (str(path.get("name") or ""), str(path.get("base_url") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return apply_provider_route_policy(provider, unique)


def build_gemini_paths(
    gemini_api_key: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Gemini 通路:To-Tokens → Codeyu → CoderPlan → OpenRouter → 官方。"""
    import tenshi_config

    return build_provider_paths(
        "gemini",
        official_key=gemini_api_key,
        official_base_url=getattr(tenshi_config, "GEMINI_OFFICIAL_BASE_URL", None),
        proxy_configs=tenshi_config.GEMINI_PROXIES if proxy_configs is None else proxy_configs,
    )


def build_gpt_paths(
    gpt_api_key: str | None,
    gpt_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """GPT 通路:GPT 代理 → OpenRouter → 官方。"""
    import tenshi_config

    return build_provider_paths(
        "gpt",
        official_key=gpt_api_key,
        official_base_url=gpt_base_url,
        official_name="OpenAI",
        proxy_configs=tenshi_config.GPT_PROXIES if proxy_configs is None else proxy_configs,
    )


def build_grok_paths(
    grok_api_key: str | None,
    grok_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Grok 通路:Grok 代理 → OpenRouter → 官方。"""
    import tenshi_config

    return build_provider_paths(
        "grok",
        official_key=grok_api_key,
        official_base_url=grok_base_url,
        proxy_configs=tenshi_config.GROK_PROXIES if proxy_configs is None else proxy_configs,
    )


def build_openrouter_paths(
    openrouter_api_key: str | None,
    openrouter_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return build_provider_paths(
        "openrouter",
        official_key=openrouter_api_key,
        official_base_url=openrouter_base_url,
        official_name="OpenRouter",
        proxy_configs=proxy_configs,
    )


def build_magmarouter_paths(
    magmarouter_api_key: str | None,
    magmarouter_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """MagmaRouter(OpenAI 兼容网关)通路:官方,不挂 OpenRouter(无对应 slug)。"""
    return build_provider_paths(
        "magmarouter",
        official_key=magmarouter_api_key,
        official_base_url=magmarouter_base_url,
        official_name="MagmaRouter",
        proxy_configs=proxy_configs,
    )


def build_glm_paths(
    glm_api_key: str | None,
    glm_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """GLM(智谱 / Z.ai)通路:官方 → Dragon → OpenRouter。

    注意:base_url 是 **zai-sdk 的 base_url**,由 glm_modules.build_glm_client 交给
    官方 SDK;OpenRouter 通路(protocol=openai)由调用侧改走 call_openrouter。
    """
    import tenshi_config

    return build_provider_paths(
        "glm",
        official_key=glm_api_key,
        official_base_url=glm_base_url,
        official_name="Official",
        proxy_configs=tenshi_config.GLM_PROXIES if proxy_configs is None else proxy_configs,
    )


def build_grok_slot_tiers(
    magmarouter_models: list[str],
    magmarouter_key: str | None,
    magmarouter_base_url: str | None,
    openrouter_model: str,
    openrouter_key: str | None,
    openrouter_base_url: str | None,
    grok_model_ids: list[str],
    grok_key: str | None,
    grok_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """grok 槽（NSFW 主链 + 链末容灾）的「档」列表，依次备选：

    MagmaRouter（tunable.MAGMAROUTER_MODELS 依次备选）
      → OpenRouter（原主路，降级为下一级备选）
      → Grok（官方 + 代理）。
    """
    tiers = [
        {
            "provider": "magmarouter",
            "label": "MagmaRouter 主路",
            "models": [m for m in (magmarouter_models or []) if m],
            "paths": build_magmarouter_paths(magmarouter_key, magmarouter_base_url, None),
        },
        {
            "provider": "openrouter",
            "label": "OpenRouter 备选",
            "models": [openrouter_model] if openrouter_model else [],
            "paths": build_openrouter_paths(openrouter_key, openrouter_base_url, None),
        },
        {
            "provider": "grok",
            "label": "Grok 容灾",
            "models": [m for m in (grok_model_ids or []) if m],
            "paths": build_grok_paths(grok_key, grok_base_url, proxy_configs),
        },
    ]
    import tenshi_config

    route_policies = getattr(tenshi_config, "PROVIDER_ROUTE_POLICIES", {}) or {}
    magma_settings = (getattr(tenshi_config, "PROVIDER_ROUTE_POLICIES", {}) or {}).get("magmarouter", {})
    openrouter_settings = route_policies.get("openrouter", {})
    magma_disabled = "official" in set(magma_settings.get("disabled", ()))
    openrouter_disabled = "official" in set(openrouter_settings.get("disabled", ()))
    tiers = [
        tier for tier in tiers
        if not (tier["provider"] == "magmarouter" and magma_disabled)
        and not (tier["provider"] == "openrouter" and openrouter_disabled)
    ]
    return tiers



def run_slot_path_first(
    tiers: Iterable[dict[str, Any]],
    invoke: Callable[[str, dict[str, Any]], T],
    *,
    max_attempts_per_model: int = 1,
    sleep_fn: Callable[[float], None] | None = None,
    log: Any = None,
) -> T:
    """按「档」依次尝试；每档 = 一个 provider 的若干通路 + 该档模型列表。

    tiers 元素形如：{"provider": "magmarouter", "label": "MagmaRouter 主路",
                      "models": [...], "paths": [...]}。
    每档先按 provider 的每日探活结果丢掉红通路（`provider_health.filter_paths`），
    再在该档内按「通路 × 模型」降级；整档失败后进入下一档。全部失败时抛 RuntimeError。
    """
    from error_text import format_errors, format_exception_inline

    import provider_health

    last_err: Exception | None = None
    failures: list[tuple[str, BaseException]] = []
    for tier in tiers or []:
        provider = str(tier.get("provider") or "")
        label = str(tier.get("label") or provider or "?")
        models = [m for m in (tier.get("models") or []) if m]
        paths = provider_health.filter_paths(provider, list(tier.get("paths") or []))
        if not paths or not models:
            if log is not None and not paths:
                log.warning(f"[slot] {label}：没有可用通路（探活全红 / 未配置），跳过。")
            continue
        try:
            return run_path_first(
                models,
                paths,
                invoke,
                max_attempts_per_model=max_attempts_per_model,
                sleep_fn=sleep_fn,
                log=log,
            )
        except Exception as e:
            last_err = e
            failures.append((label, e))
            if log is not None:
                log.warning(f"[slot] {label} 全部失败，切下一档：{format_exception_inline(e)}")

    raise RuntimeError(f"grok slot failed: {format_errors(failures)}") from last_err


def build_deepseek_paths(
    deepseek_api_key: str | None,
    deepseek_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """DeepSeek 通路:官方 → Dragon → Fireworks 代理 → OpenRouter。"""
    import tenshi_config

    return build_provider_paths(
        "deepseek",
        official_key=deepseek_api_key,
        official_base_url=deepseek_base_url,
        proxy_configs=tenshi_config.DEEPSEEK_PROXIES if proxy_configs is None else proxy_configs,
    )


def build_kimi_paths(
    kimi_api_key: str | None,
    kimi_base_url: str | None,
    proxy_configs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Kimi 通路:Dragon → Kimi 官方（两家都是 OpenAI 兼容协议）。"""
    import tenshi_config

    paths = build_provider_paths(
        "kimi",
        official_key=kimi_api_key,
        official_base_url=kimi_base_url,
        official_name="Official",
        proxy_configs=tenshi_config.KIMI_PROXIES if proxy_configs is None else proxy_configs,
    )
    for path in paths:
        if path.get("is_official"):
            path["protocol"] = "openai"
            path["native_model_ids"] = True
    return paths


def run_path_first(
    model_ids: list[str],
    paths: list[dict[str, Any]],
    invoke: Callable[[str, dict[str, Any]], T],
    *,
    max_attempts_per_model: int = 1,
    sleep_fn: Callable[[float], None] | None = None,
    log: Any = None,
) -> T:
    """
    按通路循环：同一通路内依次尝试各模型；该通路全部失败后换下一通路。
    默认 max_attempts_per_model=1：单次尝试、失败即切（回复链策略）。
    传入 log 时逐通路打印尝试/成功/失败，便于定位"哪个通路挂了、是否真的切到下一个通路"。

    失败时抛出的异常带上**每一个通路/模型的完整原文**（含 cause 链），
    不再只留最后一句——多通路全灭时，只留最后一条会把真正的原因盖掉。
    """
    import time

    from error_text import format_errors, format_exception_inline

    sleep = sleep_fn or time.sleep
    last_err: Exception | None = None
    failures: list[tuple[str, BaseException]] = []
    for path in paths:
        path_name = path.get("name") or path.get("base_url") or "?"
        for model_id in model_ids:
            for attempt in range(1, max_attempts_per_model + 1):
                if log is not None:
                    log.info(
                        f"[path-first] 尝试 {model_id} via {path_name} "
                        f"(attempt {attempt}/{max_attempts_per_model})"
                    )
                try:
                    result = invoke(model_id, path)
                    if log is not None:
                        log.info(f"[path-first] {model_id} via {path_name} 成功")
                    return result
                except Exception as e:
                    last_err = e
                    err_text = str(e).lower()
                    skip_404 = ("not_found_error" in err_text or "404" in err_text) and attempt == 1
                    failures.append((f"{model_id} via {path_name} (attempt {attempt})", e))
                    if log is not None:
                        action = (
                            "404/not_found，直接换通路"
                            if skip_404
                            else ("重试" if attempt < max_attempts_per_model else "换通路/换模型")
                        )
                        log.warning(
                            f"[path-first] {model_id} via {path_name} 失败 "
                            f"(attempt {attempt}/{max_attempts_per_model}): "
                            f"{format_exception_inline(e)} → {action}"
                        )
                    if skip_404:
                        break
                    if attempt < max_attempts_per_model:
                        sleep(1.0 * (2 ** (attempt - 1)))
    raise RuntimeError(f"All paths/models failed: {format_errors(failures)}") from last_err
