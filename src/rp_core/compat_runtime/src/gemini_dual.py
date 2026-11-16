# SPDX-License-Identifier: AGPL-3.0-only

















from __future__ import annotations

import logging
from _rp_cancellation import ensure_active
from typing import Any, Callable, TypeVar

from google import genai
from google.genai import types

import tenshi_config

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _DualNamespace:
    """``dual.models.generate_content(...)`` 形态的中转层。

    保持调用方写法不变：属性链第一级由 DualGeminiClient.__getattr__ 交给本类，
    第二级（方法名）在这里包成「逐路尝试」的调用。
    """

    def __init__(self, dual: "DualGeminiClient", attr: str) -> None:
        object.__setattr__(self, "_dual", dual)
        object.__setattr__(self, "_attr", attr)

    def __getattr__(self, method: str) -> Callable[..., Any]:
        attr = self._attr
        dual = self._dual

        def _call(*args: Any, **kwargs: Any) -> Any:
            return dual.run(
                lambda client: getattr(getattr(client, attr), method)(*args, **kwargs)
            )

        return _call


class DualGeminiClient:
    """持有按优先级排列的若干 genai.Client；属性调用自动「先代理后官方」。

    用法与原生 client 完全同形：``dual.models.generate_content(...)``；
    任一路抛异常即切下一路，全灭时重抛最后一次的异常（调用方既有降级逻辑原样接住）。
    """

    def __init__(self, clients: list[tuple[str, Any]]) -> None:

        object.__setattr__(self, "_clients", clients)

    @property
    def path_names(self) -> list[str]:
        return [name for name, _ in self._clients]

    def run(self, invoke: Callable[[Any], T]) -> T:
        last_err: BaseException | None = None
        for name, client in self._clients:
            try:
                ensure_active()
                return invoke(client)
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[gemini-dual] {name} 调用失败，切下一路："
                    f"{type(e).__name__}: {str(e)[:200]}"
                )
        raise last_err or RuntimeError("[gemini-dual] 没有任何可用的 Gemini 通路")

    def __getattr__(self, name: str) -> _DualNamespace:
        return _DualNamespace(self, name)


def build_gemini_dual_client() -> DualGeminiClient:
    """构造「代理优先、官方兜底」的多通路客户端。

    * To-Tokens：key 取 ``GEMINI_TOTOKENS_API_KEY``，优先尝试。
    * Codeyu：key 取 ``GEMINI_CODEYU_API_KEY``（未配则不建），
      base_url 取 ``GEMINI_CODEYU_BASE_URL``（默认 codeyu.shop）。
    * CoderPlan：key 取 ``GEMINI_CODERPLAN_API_KEY``（**不**回退官方 key，未配则不建），
      base_url 取 ``GEMINI_CODERPLAN_BASE_URL``（默认 api.coderplan.ai）。
    * 官方路：key 取 ``GEMINI_API_KEY``；key 缺失时官方路不建。
    * 所有通路都缺失：返回空路客户端（调用时抛 RuntimeError，与旧版单 client
      在无 key 时调用必炸的行为等价，不会让模块 import 失败）。
    """
    http_kwargs: dict[str, Any] = dict(
        timeout=int(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC * 1000),
        retryOptions=types.HttpRetryOptions(attempts=tenshi_config.LLM_CLIENT_MAX_RETRIES),
    )
    clients: list[tuple[str, Any]] = []




    proxies: list[dict] = []
    _totokens_key = getattr(tenshi_config, "GEMINI_TOTOKENS_API_KEY", None)
    _totokens_url = getattr(tenshi_config, "GEMINI_TOTOKENS_BASE_URL", None)
    if _totokens_key and _totokens_url:
        proxies.append({"name": "To-Tokens", "api_key": _totokens_key, "base_url": _totokens_url})
    _codeyu_key = getattr(tenshi_config, "GEMINI_CODEYU_API_KEY", None)
    _codeyu_url = getattr(tenshi_config, "GEMINI_CODEYU_BASE_URL", None)
    if _codeyu_key and _codeyu_url:
        proxies.append({"name": "Codeyu", "api_key": _codeyu_key, "base_url": _codeyu_url})
    _coderplan_key = getattr(tenshi_config, "GEMINI_CODERPLAN_API_KEY", None)
    _coderplan_url = getattr(tenshi_config, "GEMINI_CODERPLAN_BASE_URL", None)
    if _coderplan_key and _coderplan_url:
        proxies.append({"name": "CoderPlan", "api_key": _coderplan_key, "base_url": _coderplan_url})
    if not proxies:
        proxies = [
            dict(p)
            for p in (getattr(tenshi_config, "GEMINI_PROXIES", []) or [])
            if p.get("api_key") and p.get("base_url")
        ]

    for proxy in proxies:
        proxy_key = proxy.get("api_key")
        proxy_base = proxy.get("base_url")
        if not proxy_key or not proxy_base:
            continue


        if proxy.get("protocol") == "openai" or proxy.get("openrouter"):
            continue
        clients.append(
            (
                str(proxy.get("name") or "Codeyu"),
                genai.Client(
                    api_key=proxy_key,
                    http_options=types.HttpOptions(base_url=proxy_base, **http_kwargs),
                ),
            )
        )

    official_key = tenshi_config.GEMINI_API_KEY
    if official_key:
        clients.append(
            (
                "Official",
                genai.Client(
                    api_key=official_key,
                    http_options=types.HttpOptions(
                        base_url=getattr(tenshi_config, "GEMINI_OFFICIAL_BASE_URL", None),
                        **http_kwargs,
                    ),
                ),
            )
        )

    logger.info(f"[gemini-dual] 双路客户端就绪：{' → '.join(n for n, _ in clients) or '（无通路）'}")
    return DualGeminiClient(clients)
