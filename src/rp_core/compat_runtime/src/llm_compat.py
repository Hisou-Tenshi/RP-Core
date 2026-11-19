# SPDX-License-Identifier: AGPL-3.0-only
















from __future__ import annotations

import threading
import time
from contextlib import suppress
from contextvars import copy_context
from types import SimpleNamespace
from typing import Any, Callable, TypeVar

from _rp_cancellation import CallCancelled, CallScope, current_scope, ensure_active, mark_text_progress, remaining_timeout

import httpx
import tenshi_config
from llm_tool_text import repair_native_tool_calls
from openai import OpenAI

T = TypeVar("T")


class LlmDeadlineExceeded(TimeoutError):
    """单次 LLM 调用超过给定上限仍未返回。

    调用方应当把它当作「这次尝试失败」处理：放弃该模型、直接切下一个通路，
    绝不能把异常文本当成模型输出。
    """


def call_with_deadline(
    fn: Callable[[], T],
    *,
    timeout_sec: float | None,
    label: str = "llm",
    first_text: bool = False,
    hard_timeout_sec: float | None = None,
) -> T:
    """在独立守护线程里执行 fn,超过 timeout_sec 未返回就抛 LlmDeadlineExceeded。

    * timeout_sec 为 None 或 <= 0 时：不设上限,直接同步执行 fn（保持原行为）。
    * 超时取消当前调用作用域；SDK 请求由自身网络超时收尾，后续请求、流式回调
      和工具执行在公共边界检查取消标记。已经开始的 I/O 无法由线程强行撤销。
    * first_text=True 时，收到非空正文后解除首字计时，仍受父级硬上限约束。
    * fn 自身抛出的异常原样向上抛(不吞、不包装),调用方按原有 except 逻辑降级。
    """
    ensure_active()
    if timeout_sec is None or float(timeout_sec) <= 0:
        if hard_timeout_sec is None:
            return fn()
        timeout_sec = hard_timeout_sec
        first_text = False

    timeout_sec = remaining_timeout(timeout_sec)
    box: dict = {}
    done = threading.Event()
    progress = threading.Event() if first_text else None
    started = time.monotonic()
    hard_deadline = (
        started + remaining_timeout(hard_timeout_sec) if hard_timeout_sec is not None else None
    )
    scope = CallScope(
        deadline=started + timeout_sec, parent=current_scope.get(), first_text=progress,
        hard_deadline=hard_deadline,
    )
    context = copy_context()

    def _runner() -> None:
        token = current_scope.set(scope)
        try:
            ensure_active()
            result = fn()
            scope.check()
            box["result"] = result
        except CallCancelled:
            box["error"] = LlmDeadlineExceeded(f"{label} 已取消")
        except BaseException as exc:
            box["error"] = exc
        finally:
            current_scope.reset(token)
            done.set()

    worker = threading.Thread(
        target=lambda: context.run(_runner), name=f"llm-deadline:{label}", daemon=True
    )
    worker.start()

    while not done.is_set():
        deadline = hard_deadline
        if progress is None or not progress.is_set():
            deadline = min(deadline, scope.deadline) if deadline is not None else scope.deadline
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            if progress is not None and progress.is_set() and deadline != hard_deadline:
                continue
            scope.cancelled.set()
            phase = "首字" if progress is not None and not progress.is_set() else "请求"
            raise LlmDeadlineExceeded(f"{label} 超过{phase}限时")
        # Keep nested workers responsive to cancellation by a parent scope.
        if scope.parent is not None:
            try:
                scope.parent.check()
            except CallCancelled:
                scope.cancelled.set()
                raise
            remaining = min(remaining, 0.25) if remaining is not None else 0.25
        if progress is not None and not progress.is_set():
            remaining = min(remaining, 0.25) if remaining is not None else 0.25
        done.wait(remaining)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def stream_unsupported(exc):
    """Only downgrade a capability rejection, never a timeout or a provider outage."""
    if isinstance(exc, NotImplementedError):
        return True
    message = str(exc).lower()
    if "stream" not in message:
        return False
    return (getattr(exc, "status_code", None) or getattr(exc, "code", None)) in (400, 405, 422, 501) and any(
        cue in message for cue in ("not support", "unsupported", "not implemented", "stream=false")
    )


def chat_completion(create, *, on_text=None, **options):
    """Consume Chat Completions SSE and return its usual message shape to callers."""
    ensure_active()
    stream = None
    received_delta = False
    try:
        stream = create(**{**options, "stream": True})
        # Some compatible gateways answer stream=true with an ordinary completion.
        if hasattr(stream, "choices"):
            return stream
        response = getattr(stream, "response", None)
        if response is not None and response.headers.get("content-type", "").startswith("application/json"):
            response.read()
            data = response.json()
            if data.get("error"):
                raise RuntimeError("Chat Completions gateway returned an error")
            from openai.types.chat import ChatCompletion
            return ChatCompletion.model_validate(data)
        text, reasoning, calls = [], [], {}
        finish = None
        for chunk in stream:
            ensure_active()
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            choice = choices[0]
            finish = getattr(choice, "finish_reason", None) or finish
            delta = choice.delta
            content = getattr(delta, "content", None)
            if content:
                received_delta = True
                mark_text_progress(content)
                text.append(content)
                if on_text is not None:
                    on_text(content)
            thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if isinstance(thought, str):
                reasoning.append(thought)
            for item in getattr(delta, "tool_calls", None) or ():
                received_delta = True
                tool = calls.setdefault(item.index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}
                })
                if item.id:
                    tool["id"] = item.id
                if item.function:
                    tool["function"]["name"] += item.function.name or ""
                    tool["function"]["arguments"] += item.function.arguments or ""
        if finish is None and received_delta:
            raise RuntimeError("Chat Completions stream ended without a finish reason")
        message = SimpleNamespace(
            role="assistant", content="".join(text), reasoning_content="".join(reasoning),
            tool_calls=[calls[index] for index in sorted(calls)],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])
    except Exception as exc:
        if received_delta or not stream_unsupported(exc):
            raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
    ensure_active()
    return create(**{**options, "stream": False})


def build_llm_timeout() -> httpx.Timeout:
    """依配置构造读写超时(SDK 默认 600s 静默太久;connect 单独给较短值)。"""
    return httpx.Timeout(
        connect=remaining_timeout(tenshi_config.LLM_CLIENT_CONNECT_TIMEOUT_SEC),
        read=remaining_timeout(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC),
        write=remaining_timeout(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC),
        pool=remaining_timeout(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC),
    )


def sdk_http_module(sdk_module: Any):
    """取 SDK **自己使用**的 http 客户端模块——timeout 必须用它的类来构造。

    实现与说明见 `sdk_compat`（那里不依赖 tenshi_config，因此镜像**构建期**的
    `scripts/deps_selfcheck.py` 能加载同一份实现实测，而不是验另一份代码）。
    """
    import sdk_compat

    return sdk_compat.sdk_http_module(sdk_module)


def build_sdk_timeout(sdk_module: Any, *, connect: float | None = None, read: float | None = None):
    """按 SDK 自己的 http 模块构造 timeout（默认值取自 tenshi_config）。

    2026-09-15 事故：镜像里的 anthropic SDK 1.4 起改用 httpx2，而代码传的是
    `httpx.Timeout` —— 类型不匹配会让 SDK **在本地**就拒绝构造请求，表现为瞬时的
    `APIConnectionError: Connection error.`（线上 268/268 全中，且从无 HTTP 状态码错误）。
    """
    import sdk_compat

    return sdk_compat.build_sdk_timeout(
        sdk_module,
        connect=remaining_timeout(
            connect if connect is not None else tenshi_config.LLM_CLIENT_CONNECT_TIMEOUT_SEC
        ),
        read=remaining_timeout(read if read is not None else tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC),
    )


def build_openai_compat_client(api_key: str, base_url: str) -> OpenAI:
    """构建 OpenAI 兼容客户端,统一关闭内置重试(链路层已自实现降级)并设超时。"""
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=int(getattr(tenshi_config, "LLM_CLIENT_MAX_RETRIES", 0) or 0),
        timeout=build_sdk_timeout(OpenAI),
    )
    paths = getattr(tenshi_config, "PROVIDER_PROXIES", {}) or {}
    if any(path.get("protocol") == "responses" and path.get("api_key") == api_key
           and str(path.get("base_url", "")).rstrip("/") == str(base_url).rstrip("/")
           for group in paths.values() for path in group):
        from _rp_responses import ResponsesChatClient
        return ResponsesChatClient(client)
    return client


def normalize_tool_calls(raw_tool_calls):
    """
    将 OpenAI SDK 的 tool_call 对象规范化为普通 dict，并**修复上游的畸形返回**。

    实现已统一收敛到 `llm_tool_text.repair_native_tool_calls`（工具调用抢救的唯一入口）。

    与旧行为的差别（2026-09-13 事故的第二个坑）：旧实现是
    `[t for t in out if t.get("id") and t.get("function", {}).get("name") is not None]`，
    也就是**缺 id / 缺 name 的调用会被整条静默丢弃**——上层看到的是「本轮没有工具调用」，
    于是模型明明调了工具却什么都没发生，而且和「模型选择了不调用工具」无法区分。
    现在缺 id 会补一个稳定的 `callfix_<n>`，缺 name 才会跳过（并记录原因）。
    """
    return repair_native_tool_calls(raw_tool_calls).calls
