# SPDX-License-Identifier: AGPL-3.0-only







from _rp_cancellation import ensure_active
import json
import re

import tenshi_config
from llm_compat import chat_completion, build_openai_compat_client, normalize_tool_calls as _normalize_tool_calls
from openai import OpenAI
from tenshi_utils import append_llm_trace, setup_logger
from error_text import format_exception_inline


logger = setup_logger("TenshiDeepSeekModules", tenshi_config.DEBUG_LOG_PATH)
DEEPSEEK_API_KEY = tenshi_config.DEEPSEEK_API_KEY




DEEPSEEK_THINKING_EXTRA_BODY = {"thinking": {"type": "enabled"}}
DEEPSEEK_REASONING_EFFORT = "high"
DEEPSEEK_REASONING_EFFORT_AGENT = "max"


class LLMTextResponse:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text or ""


def _client_for_path(llm_path=None) -> tuple[OpenAI, dict]:
    path = llm_path or {
        "name": "Official",
        "api_key": DEEPSEEK_API_KEY,
        "base_url": tenshi_config.DEEPSEEK_BASE_URL,
    }
    client = build_openai_compat_client(
        api_key=path.get("api_key") or DEEPSEEK_API_KEY,
        base_url=path.get("base_url") or tenshi_config.DEEPSEEK_BASE_URL,
    )
    return client, path


def _normalize_model_name_for_path(model_name: str, *, base_url: str | None) -> str:
    """
    DeepSeek provider 支持多通路（官方 / 代理）：
    - Fireworks 使用其自有的模型命名空间（accounts/fireworks/models/...）。
      （Fireworks 代理 2026-11 已停用：这层归一化作为壳保留，通路未配置时不会被触发。）
    - 业务侧仍然用简短别名（deepseek-v4-pro），这里做按通路映射，避免全局改模型链。

    另：降级链尾部会用 "<model>#tail" 这种显式别名，让同一个模型在链上出现两次
    （链首按档位、链尾兜底；见 config/tunable.dialog_model_chain）。
    "#" 之后只是链路标签，必须在这里剥掉，绝不能把带 "#" 的名字发到 API。
    """
    m = (model_name or "").strip()
    if not m:
        return m
    if "#" in m:
        m = m.split("#", 1)[0].strip()
    b = (base_url or "").lower()
    if "api.fireworks.ai" in b:
        if m == "deepseek-v4-pro":
            return getattr(tenshi_config, "FIREWORKS_DEEPSEEK_V4_PRO_MODEL", m) or m
        if m == "deepseek-v4-flash":
            return getattr(tenshi_config, "FIREWORKS_DEEPSEEK_V4_FLASH_MODEL", m) or m
    return m


def get_reasoning_content(message) -> str:
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("reasoning_content") or "").strip()
    return str(getattr(message, "reasoning_content", None) or "").strip()


def assistant_message_dict(choice_msg) -> dict:
    if isinstance(choice_msg, dict):
        out = dict(choice_msg)
        out.setdefault("role", "assistant")
        if out.get("tool_calls"):
            out["tool_calls"] = _normalize_tool_calls(out["tool_calls"])
        return out
    if hasattr(choice_msg, "model_dump"):
        try:
            out = choice_msg.model_dump(exclude_none=True)
            out.setdefault("role", "assistant")
            if out.get("tool_calls"):
                out["tool_calls"] = _normalize_tool_calls(out["tool_calls"])
            return out
        except Exception:
            pass
    d = {"role": "assistant", "content": getattr(choice_msg, "content", None) or ""}
    rc = get_reasoning_content(choice_msg)
    if rc:
        d["reasoning_content"] = rc
    raw_tc = getattr(choice_msg, "tool_calls", None)
    if raw_tc:
        d["tool_calls"] = _normalize_tool_calls(raw_tc)
    return d


def _to_openai_messages(system_prompt, messages):
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in messages or []:
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


def _thinking_kwargs(*, tools=None, json_mode: bool = False, agent_complex: bool = False) -> dict:
    kw = {
        "reasoning_effort": DEEPSEEK_REASONING_EFFORT_AGENT if agent_complex else DEEPSEEK_REASONING_EFFORT,
        "extra_body": dict(DEEPSEEK_THINKING_EXTRA_BODY),
    }
    if tools is not None:
        kw["tools"] = tools
        kw["tool_choice"] = "auto"
    if json_mode:
        kw["response_format"] = {"type": "json_object"}
    return kw


def call_deepseek(
    model_name,
    system_prompt,
    messages,
    llm_path=None,
    *,
    agent_complex: bool = False,
):
    client, _ = _client_for_path(llm_path)
    try:
        base_url = None
        if isinstance(llm_path, dict):
            base_url = llm_path.get("base_url")
        ensure_active()
        completion = chat_completion(
            client.chat.completions.create,
            model=_normalize_model_name_for_path(model_name, base_url=base_url),
            messages=_to_openai_messages(system_prompt, messages),
            **_thinking_kwargs(agent_complex=agent_complex),
        )
        msg = completion.choices[0].message
        return (getattr(msg, "content", None) or "").strip(), get_reasoning_content(msg)
    except Exception as e:
        logger.error(f"DeepSeek API 调用失败：{format_exception_inline(e)}")
        raise


def call_deepseek_with_tools(
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
    client, _ = _client_for_path(llm_path)
    try:
        base_url = None
        if isinstance(llm_path, dict):
            base_url = llm_path.get("base_url")
        kwargs = {
            "model": _normalize_model_name_for_path(model_name, base_url=base_url),
            "messages": _to_openai_messages(system_prompt, messages),
            **_thinking_kwargs(tools=tools or [], agent_complex=agent_complex),
        }
        def on_text(text):
            for callback in (stream_callback, on_stream_token):
                if callable(callback):
                    ensure_active()
                    callback(text)

        completion = chat_completion(client.chat.completions.create, on_text=on_text, **kwargs)
        choice_msg = completion.choices[0].message
        raw_tool_calls = getattr(choice_msg, "tool_calls", None)
        tool_calls = _normalize_tool_calls(raw_tool_calls) if raw_tool_calls else []
        return getattr(choice_msg, "content", None) or "", tool_calls, assistant_message_dict(choice_msg)
    except Exception as e:
        logger.error(f"DeepSeek 工具调用 API 失败：{format_exception_inline(e)}")
        raise


def deepseek_generate_json(
    model_name,
    prompt,
    *,
    system_prompt=None,
    llm_path=None,
    agent_complex: bool = False,
) -> str:
    client, _ = _client_for_path(llm_path)
    base_url = None
    if isinstance(llm_path, dict):
        base_url = llm_path.get("base_url")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    ensure_active()
    completion = chat_completion(
        client.chat.completions.create,
        model=_normalize_model_name_for_path(model_name, base_url=base_url),
        messages=messages,
        **_thinking_kwargs(json_mode=True, agent_complex=agent_complex),
    )
    return (completion.choices[0].message.content or "").strip()


def score_chat_importance_batch(messages_for_scoring, *, trace_path: str | None = None):
    """
    messages_for_scoring：包含以下键的 dict 列表：
      - msg_id
      - time (unix)
      - user_query
      - bot_answer
    返回：dict[msg_id] -> float importance_score
    """
    bot_label = getattr(tenshi_config, "CHAT_SCORE_BOT_LABEL", "Bot")
    model_name = tenshi_config.deepseek_preprocess_primary()

    lines = []
    for i, m in enumerate(messages_for_scoring):
        uq = (m.get("user_query") or "").strip()
        ba = (m.get("bot_answer") or "").strip()
        if len(uq) > 600:
            uq = uq[:600] + "…"
        if len(ba) > 900:
            ba = ba[:900] + "…"
        lines.append(
            f"[{i}] msg_id={m.get('msg_id')} time={m.get('time')}\nUser: {uq}\n{bot_label}: {ba}\n"
        )

    user_prompt = "请对以下消息逐条打分：\n\n" + "\n".join(lines)
    try:
        resp, _ = call_deepseek(
            model_name=model_name,
            system_prompt=tenshi_config.CHAT_IMPORTANCE_RUBRIC_V1,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.warning(f"重要度打分 DeepSeek 失败，尝试 OpenRouter 同款兜底：{format_exception_inline(e)}")
        from llm_routing import try_openrouter_fallback_path

        fb = try_openrouter_fallback_path(
            model_name,
            lambda slug, path: call_deepseek(
                slug,
                tenshi_config.CHAT_IMPORTANCE_RUBRIC_V1,
                [{"role": "user", "content": user_prompt}],
                llm_path=path,
            ),
            log=logger,
        )
        if not fb:
            raise
        resp, _ = fb

    try:
        append_llm_trace({
            "source": "log_update.chat_importance_score",
            "provider": "deepseek",
            "model": model_name,
            "system_prompt": tenshi_config.CHAT_IMPORTANCE_RUBRIC_V1,
            "user_prompt": user_prompt,
            "response_text": resp,
        }, trace_path=trace_path)
    except Exception:
        pass

    text = (resp or "").strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"(\[[\s\S]*\])", text)
        if not m:
            raise ValueError(f"解析 DeepSeek JSON 失败：{text[:200]}")
        data = json.loads(m.group(1))

    out = {}
    for item in data:
        mid = str(item.get("msg_id", "")).strip()
        if not mid:
            continue
        try:
            score = float(item.get("importance_score", 0.0))
        except Exception:
            score = 0.0
        if score < 0.0:
            score = 0.0
        if score > 2.0:
            score = 2.0
        out[mid] = score
    return out
