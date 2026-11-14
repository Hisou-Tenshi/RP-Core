# SPDX-License-Identifier: AGPL-3.0-only


































from __future__ import annotations

import threading

import tenshi_config
from error_text import format_exception_inline
from llm_compat import normalize_tool_calls as _normalize_tool_calls
from tenshi_utils import append_llm_trace, setup_logger

logger = setup_logger("TenshiGlmModules", tenshi_config.DEBUG_LOG_PATH)

GLM_API_KEY = tenshi_config.GLM_API_KEY



GLM_THINKING = {"type": "enabled"}

GLM_REASONING_EFFORT = "high"

GLM_REASONING_EFFORT_AGENT = "max"



_CLIENTS: dict[tuple[str, str], object] = {}
_CLIENTS_LOCK = threading.Lock()


def _zhipu_client_class():
    """惰性导入 `zai.ZhipuAiClient`；未安装时抛带安装指引的 ImportError。"""
    try:
        from zai import ZhipuAiClient
    except Exception as exc:
        raise ImportError(
            "GLM provider 需要官方 zai-sdk（pip install zai-sdk）；"
            f"当前环境导入失败：{type(exc).__name__}: {exc}"
        ) from exc
    return ZhipuAiClient


def build_glm_client(api_key: str, base_url: str | None = None):
    """构造（或复用）zai 客户端：关闭内置重试、按 SDK 自己的 http 模块设超时。"""
    import sdk_compat

    import zai

    client_class = _zhipu_client_class()
    resolved_base = (base_url or tenshi_config.GLM_BASE_URL or "").rstrip("/") or None
    cache_key = (str(api_key or ""), str(resolved_base or ""))
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(cache_key)
        if cached is not None:
            return cached
        client = client_class(
            api_key=api_key,
            base_url=resolved_base,
            max_retries=int(getattr(tenshi_config, "LLM_CLIENT_MAX_RETRIES", 0) or 0),
            timeout=sdk_compat.build_sdk_timeout(
                zai,
                connect=float(tenshi_config.LLM_CLIENT_CONNECT_TIMEOUT_SEC),
                read=float(tenshi_config.LLM_CLIENT_READ_TIMEOUT_SEC),
            ),
        )
        _CLIENTS[cache_key] = client
        return client


def reset_client_cache() -> None:
    """测试 / 轮次重置用：丢掉缓存客户端。"""
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


def _client_for_path(llm_path=None):
    """按通路取客户端；未给通路时用官方 key + 官方 base_url。"""
    if isinstance(llm_path, dict):
        api_key = llm_path.get("api_key") or GLM_API_KEY
        base_url = llm_path.get("base_url") or tenshi_config.GLM_BASE_URL
        return build_glm_client(api_key, base_url), llm_path
    path = {
        "name": "Official",
        "api_key": GLM_API_KEY,
        "base_url": tenshi_config.GLM_BASE_URL,
    }
    return build_glm_client(GLM_API_KEY, tenshi_config.GLM_BASE_URL), path


def _normalize_model_name_for_path(model_name: str, *, base_url: str | None = None) -> str:
    """剥掉链路别名后缀（例如 `glm-5.3#tail`）——`#` 之后只是链路标签，不能发给 API。

    与 `deepseek_modules._normalize_model_name_for_path` 同口径（见 config/tunable
    的 `_append_deepseek_tail` 说明）。`base_url` 目前不改变模型名（GLM 只有官方
    一条通路），保留该参数是为了与其它 provider 的调用形状一致。
    """
    m = (model_name or "").strip()
    if "#" in m:
        m = m.split("#", 1)[0].strip()
    return m


def get_reasoning_content(message) -> str:
    """取思考正文（zai 返回对象的 `reasoning_content`）。"""
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("reasoning_content") or "").strip()
    return str(getattr(message, "reasoning_content", None) or "").strip()


def _to_glm_messages(system_prompt, messages):
    """把内部 messages 转成 zai `chat.completions.create(messages=...)` 接受的形状。

    与 `deepseek_modules._to_openai_messages` 同口径（OpenAI 风格 role/content +
    assistant.tool_calls + role=tool），因为 GLM 的对话协议本就是 OpenAI 兼容的。
    """
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        if isinstance(content, list):

            text_content = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content += block.get("text", "")
            content = text_content
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id"),
                    "content": content,
                }
            )
            continue
        if role == "assistant":
            out.append(assistant_message_dict(msg))
            continue
        out.append({"role": role, "content": content})
    return out


def assistant_message_dict(choice_msg) -> dict:
    """把 zai 的 assistant 消息规范化为普通 dict（可直接回灌进下一轮 messages）。

    `tool_calls` 走 `llm_compat.normalize_tool_calls` 统一收口：缺 id 会补一个稳定 id、
    缺 name 才丢弃（2026-09-13 事故的教训，见 `llm_tool_text.repair_native_tool_calls`）。
    """
    if isinstance(choice_msg, dict):
        out = dict(choice_msg)
        out.setdefault("role", "assistant")
        if out.get("tool_calls"):
            out["tool_calls"] = _normalize_tool_calls(out["tool_calls"])
        return out

    d = {"role": "assistant", "content": getattr(choice_msg, "content", None) or ""}
    rc = get_reasoning_content(choice_msg)
    if rc:
        d["reasoning_content"] = rc
    raw_tc = getattr(choice_msg, "tool_calls", None)
    if raw_tc:
        d["tool_calls"] = _normalize_tool_calls(raw_tc)
    return d


def _thinking_kwargs(*, tools=None, agent_complex: bool = False) -> dict:
    """公共思考参数：thinking 恒开启，强度按场景取（可用环境变量覆盖）。"""
    kw = {
        "thinking": dict(GLM_THINKING),
        "reasoning_effort": (
            getattr(tenshi_config, "GLM_REASONING_EFFORT_AGENT", GLM_REASONING_EFFORT_AGENT)
            if agent_complex
            else getattr(tenshi_config, "GLM_REASONING_EFFORT", GLM_REASONING_EFFORT)
        ),
    }
    if tools is not None:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"
    return kw


def call_glm(
    model_name,
    system_prompt,
    messages,
    llm_path=None,
    *,
    agent_complex: bool = False,
):
    """纯文本调用（不带 tools）。

    返回 `(content_text, reasoning_text)`；异常照原样抛出，由上层降级链切换下一个候选。
    """
    client, _ = _client_for_path(llm_path)
    try:
        completion = client.chat.completions.create(
            model=_normalize_model_name_for_path(model_name),
            messages=_to_glm_messages(system_prompt, messages),
            stream=False,
            **_thinking_kwargs(agent_complex=agent_complex),
        )
        msg = completion.choices[0].message
        return (getattr(msg, "content", None) or "").strip(), get_reasoning_content(msg)
    except Exception as e:
        logger.error(f"GLM API 调用失败：{format_exception_inline(e)}")
        raise


def _accumulate_stream_tool_calls(tool_calls_by_index: dict, raw_tool_calls) -> None:
    """把流式 chunk 里的 tool_calls 增量按 index 拼成完整调用。"""
    if not raw_tool_calls:
        return
    for tc_delta in raw_tool_calls:
        idx = getattr(tc_delta, "index", 0)
        if idx is None:
            idx = 0
        if idx not in tool_calls_by_index:
            tool_calls_by_index[idx] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        acc = tool_calls_by_index[idx]
        tc_id = getattr(tc_delta, "id", None)
        if tc_id:
            acc["id"] = tc_id
        fn = getattr(tc_delta, "function", None)
        if fn:
            name = getattr(fn, "name", None)
            if name:
                acc["function"]["name"] = name
            args_part = getattr(fn, "arguments", None)
            if args_part:
                acc["function"]["arguments"] += args_part


def call_glm_with_tools(
    model_name,
    system_prompt,
    messages,
    tools,
    llm_path=None,
    stream_callback=None,
    on_stream_token=None,
    *,
    agent_complex: bool = False,
):
    """带 tools 的调用（主对话工具循环用）。

    返回 `(content_text, tool_calls, assistant_msg_dict)`：
      * `assistant_msg_dict` 是**规范化后的普通 dict**，可直接 append 回 messages 再回灌；
      * 传了 `stream_callback` / `on_stream_token` 时走流式，逐块回调增量文本。
        zai 的 chunk 形状为 `choices[0].delta.content` / `.reasoning_content` /
        `.tool_calls`，且收尾可能发空 `choices`（usage-only），必须容忍。
    """
    client, _ = _client_for_path(llm_path)
    use_stream = callable(stream_callback) or callable(on_stream_token)
    try:
        kwargs = {
            "model": _normalize_model_name_for_path(model_name),
            "messages": _to_glm_messages(system_prompt, messages),
            "stream": use_stream,
            **_thinking_kwargs(tools=tools or [], agent_complex=agent_complex),
        }
        if not use_stream:
            completion = client.chat.completions.create(**kwargs)
            choice_msg = completion.choices[0].message
            content = getattr(choice_msg, "content", None) or ""
            raw_tool_calls = getattr(choice_msg, "tool_calls", None)
            tool_calls = _normalize_tool_calls(raw_tool_calls) if raw_tool_calls else []
            return content, tool_calls, assistant_message_dict(choice_msg)

        stream = client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_by_index: dict = {}

        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                if callable(stream_callback):
                    stream_callback(text)
                if callable(on_stream_token):
                    on_stream_token(text)
            _accumulate_stream_tool_calls(tool_calls_by_index, getattr(delta, "tool_calls", None))

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts).strip()
        tool_calls = _normalize_tool_calls(
            [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
        )
        assistant_msg = {"role": "assistant", "content": content}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        return content, tool_calls, assistant_msg
    except Exception as e:
        logger.error(f"GLM 工具调用 API 失败：{format_exception_inline(e)}")
        raise


def glm_generate_json(
    model_name,
    prompt,
    *,
    system_prompt=None,
    llm_path=None,
    agent_complex: bool = False,
) -> str:
    """结构化输出（`response_format={"type": "json_object"}`）单轮调用。"""
    client, _ = _client_for_path(llm_path)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    completion = client.chat.completions.create(
        model=_normalize_model_name_for_path(model_name),
        messages=messages,
        stream=False,
        response_format={"type": "json_object"},
        **_thinking_kwargs(agent_complex=agent_complex),
    )
    return (completion.choices[0].message.content or "").strip()


def trace_glm(source: str, model_name: str, **fields) -> None:
    """把一次 GLM 调用写进 llm_trace（失败不影响主流程）。"""
    append_llm_trace({"source": source, "provider": "glm", "model": model_name, **fields})
