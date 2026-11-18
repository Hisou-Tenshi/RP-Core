# SPDX-License-Identifier: AGPL-3.0-only

from _rp_cancellation import ensure_active
import os
import logging
import json
import re
import time

import tenshi_config
from tenshi_utils import setup_logger
from openai import OpenAI

from llm_compat import (
    LlmDeadlineExceeded,
    build_llm_timeout,
    build_openai_compat_client,
    call_with_deadline,
    normalize_tool_calls as _normalize_tool_calls,
)
from error_text import format_exception_inline

logger = setup_logger("GrokModules", tenshi_config.DEBUG_LOG_PATH)
GROK_API_KEY = tenshi_config.GROK_API_KEY
MAGMAROUTER_API_KEY = tenshi_config.MAGMAROUTER_API_KEY
OPENROUTER_API_KEY = tenshi_config.OPENROUTER_API_KEY
GPT_API_KEY = tenshi_config.GPT_API_KEY


_GROK_TIMEOUT = build_llm_timeout()


def _aux_deadline_sec() -> float:
    """辅助调用的单次硬上限（秒）；<= 0 表示关闭（退回只靠 SDK 读写超时）。

    见 src/api_resilience.py：这些非用户直读的环节（前置路由 / NSFW 改写 /
    图像线索推断）失败只影响上下文丰富度，因此给一个较短的「到点即弃」上限，
    避免任一档卡住时把整条链拖到 SDK 的 300s 读超时。
    """
    try:
        return float(getattr(tenshi_config, "REPLY_AUX_LLM_TIMEOUT_SEC", 0.0) or 0.0)
    except Exception:
        return 0.0


def _bounded(text_fn, label: str):
    """按 `_aux_deadline_sec` 限时执行一次辅助调用；到点抛 LlmDeadlineExceeded。"""
    timeout = _aux_deadline_sec()
    return call_with_deadline(
        text_fn,
        timeout_sec=timeout if timeout > 0 else None,
        label=label,
    )


def _build_openai_client(api_key: str, base_url: str) -> OpenAI:
    """构建 OpenAI 兼容客户端(Grok / OpenRouter),统一关闭内置重试并设超时。"""
    return build_openai_compat_client(api_key, base_url)

_HIGH_RISK_KEYWORDS = (
    "自杀", "自残", "杀人", "死亡", "急救", "报警", "法律", "起诉", "合同", "违约",
    "医疗", "诊断", "药", "处方", "投资", "股票", "杠杆", "贷款", "税务", "隐私",
    "密码", "密钥", "漏洞", "生产事故", "事故", "safety", "legal", "medical", "finance",
)
_MEDIUM_COMPLEXITY_KEYWORDS = (
    "架构", "设计", "重构", "调试", "排查", "性能", "优化", "迁移", "部署", "并发",
    "多步骤", "方案", "权衡", "tradeoff", "refactor", "debug", "performance", "migration",
)


def _apply_dialog_grade_hard_rules(score: int, query: str, history_context: str) -> tuple[int, str]:
    """
    对 LLM 分数做可复现的硬逻辑校准，抑制随机漂移：
    - 命中高风险关键词，最少抬到 high 阈值
    - 命中中复杂度关键词，最少抬到 medium 阈值

    仅扫描【当前用户输入 query】，不扫描历史上下文：
    历史里只要出现过一次 医疗/合同/药/事故 等词，就会把之后每一轮都抬到 high，
    导致分级恒为 72（score=high_th）——之前 Gemini 分级"一直输出 72"就是这个原因。
    """
    text = f"{query or ''}".lower()
    adjusted = max(0, min(100, int(score)))
    reason_tags = []
    high_th = int(getattr(tenshi_config, "CONVERSATION_GRADE_HIGH_THRESHOLD", 72) or 72)
    med_th = int(getattr(tenshi_config, "CONVERSATION_GRADE_MEDIUM_THRESHOLD", 42) or 42)

    if any(k.lower() in text for k in _HIGH_RISK_KEYWORDS):
        adjusted = max(adjusted, high_th)
        reason_tags.append("hard_rule:risk>=high")
    elif any(k.lower() in text for k in _MEDIUM_COMPLEXITY_KEYWORDS):
        adjusted = max(adjusted, med_th)
        reason_tags.append("hard_rule:complexity>=medium")

    return adjusted, ",".join(reason_tags)


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _compute_score_from_rubric_fields(parsed: dict) -> tuple[int, str]:
    """
    优先使用结构化子分重算总分，避免 LLM 直接拍一个总分。
    返回：(score, source_tag)
    """
    if not isinstance(parsed, dict):
        return 50, "score_fallback_default"

    i = _safe_int(parsed.get("importance_score"), -1)
    c = _safe_int(parsed.get("complexity_score"), -1)
    r = _safe_int(parsed.get("risk_score"), -1)
    t = _safe_int(parsed.get("tooling_score"), -1)
    if min(i, c, r, t) >= 0:
        i = max(0, min(30, i))
        c = max(0, min(30, c))
        r = max(0, min(30, r))
        t = max(0, min(10, t))
        return i + c + r + t, "score_from_subscores"

    raw_score = _safe_int(parsed.get("score"), 50)
    return max(0, min(100, raw_score)), "score_from_llm_score"

def _messages_to_openai_format(system_prompt, messages):
    """转换 messages 为 OpenAI 兼容格式。"""
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, list):
            text_content = ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content += block.get("text", "")
            content = text_content
        out.append({"role": role, "content": content})
    return out


def _messages_to_openai_format_with_tools(system_prompt, messages):
    """含 tool / tool_calls 的 OpenAI 兼容格式。"""
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in messages:
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
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": msg.get("content", "") or "",
                    "tool_calls": tool_calls,
                }
            )
            continue
        out.append({"role": role, "content": content})
    return out


def _call_openai_compat_text(
    *,
    label: str,
    default_name: str,
    default_api_key: str | None,
    default_base_url: str | None,
    model_name,
    system_prompt,
    messages,
    llm_path=None,
    stream_callback=None,
):
    """OpenAI 兼容网关的纯文本调用骨架（MagmaRouter / OpenRouter 共用）。

    传入 stream_callback 时用流式请求，逐块把增量文本回调出去（NSFW 回复第一组需要
    「可传上下文 + 开流式 + 不可工具」）；否则一次拿完整文本。
    返回 (content_text, "")。
    """
    path = llm_path or {
        "name": default_name,
        "api_key": default_api_key,
        "base_url": default_base_url,
    }
    client = _build_openai_client(
        path.get("api_key") or default_api_key,
        path.get("base_url") or default_base_url,
    )
    api_messages = _messages_to_openai_format(system_prompt, messages)
    try:
        if not callable(stream_callback):
            ensure_active()
            completion = client.chat.completions.create(
                model=model_name,
                messages=api_messages,
                temperature=0.7,
                stream=False,
            )
            content = completion.choices[0].message.content or ""
            return content, ""

        ensure_active()
        stream = client.chat.completions.create(
            model=model_name,
            messages=api_messages,
            temperature=0.7,
            stream=True,
        )
        parts: list[str] = []
        for event in stream:
            delta = ""
            try:
                delta = event.choices[0].delta.content or ""
            except (AttributeError, IndexError, TypeError):

                delta = ""
            if not delta:
                continue
            parts.append(delta)
            try:
                ensure_active()
                stream_callback(delta)
            except Exception:
                pass
        return "".join(parts), ""
    except Exception as e:
        logger.error(f"{label} API 调用失败：{format_exception_inline(e)}")
        raise e


def call_magmarouter(model_name, system_prompt, messages, llm_path=None, stream_callback=None):
    """通过 OpenAI 兼容层调用 MagmaRouter（纯文本，不带 tools；grok 槽主路）。"""
    return _call_openai_compat_text(
        label="MagmaRouter",
        default_name="MagmaRouter",
        default_api_key=MAGMAROUTER_API_KEY,
        default_base_url=tenshi_config.MAGMAROUTER_BASE_URL,
        model_name=model_name,
        system_prompt=system_prompt,
        messages=messages,
        llm_path=llm_path,
        stream_callback=stream_callback,
    )


def call_openrouter(model_name, system_prompt, messages, llm_path=None, stream_callback=None):
    """通过 OpenAI 兼容层调用 OpenRouter（纯文本，不带 tools；MagmaRouter 之后的备选）。"""
    return _call_openai_compat_text(
        label="OpenRouter",
        default_name="OpenRouter",
        default_api_key=OPENROUTER_API_KEY,
        default_base_url=tenshi_config.OPENROUTER_BASE_URL,
        model_name=model_name,
        system_prompt=system_prompt,
        messages=messages,
        llm_path=llm_path,
        stream_callback=stream_callback,
    )


def call_openrouter_vision(
    model_name: str,
    prompt: str,
    image_b64: str,
    media_type: str,
    llm_path=None,
    timeout_sec: float | None = None,
):
    """OpenRouter 视觉兜底(图片识别):OpenAI 兼容 chat.completions + image_url(data URL)。

    图片识别原本只走 Gemini(genai 协议);本函数让它在所有 Gemini 通路都失败时
    仍能经 OpenRouter 的 google/gemini-* 视觉模型完成识别(2026-10 降级链要求)。
    返回 (text, "") 与其它 provider 的纯文本调用同形。
    """
    import base64

    path = llm_path or {
        "name": "OpenRouter",
        "api_key": OPENROUTER_API_KEY,
        "base_url": tenshi_config.OPENROUTER_BASE_URL,
    }
    client = _build_openai_client(
        path.get("api_key") or OPENROUTER_API_KEY,
        path.get("base_url") or tenshi_config.OPENROUTER_BASE_URL,
    )
    data_url = f"data:{media_type};base64,{image_b64}"
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.7,
        "stream": False,
    }

    def _do():
        ensure_active()
        completion = client.chat.completions.create(**payload)
        return completion.choices[0].message.content or ""

    try:
        if timeout_sec and timeout_sec > 0:
            content = call_with_deadline(_do, timeout_sec=timeout_sec, label="OpenRouter vision")
        else:
            content = _do()
        return content, ""
    except Exception as e:
        logger.error(f"OpenRouter vision 调用失败：{format_exception_inline(e)}")
        raise


def call_gpt(model_name, system_prompt, messages, llm_path=None, reasoning_effort=None):
    """通过 OpenAI 官方/兼容层调用 GPT（对话分级 / NSFW 前置路由等 JSON 任务）。

    使用 GPT_REASONING_EFFORT（默认 xhigh，GPT-5.2+ 支持）。
    注意：GPT-Flash(gpt-6-luna) 仅支持默认 temperature=1，传其他值会 400（unsupported_value）。
    NSFW/对话分级走这里（不带 tools）：一旦上游因 reasoning_effort 报错，
    `_chat_completions_create` 会**去掉该参数**重试一次——与主回复同一条规则：
    遇到冲突就不带思考强度，不换端点、不换模型。
    """
    path = llm_path or {
        "name": "GPT",
        "api_key": GPT_API_KEY,
        "base_url": tenshi_config.GPT_BASE_URL,
    }
    client = _build_openai_client(
        path.get("api_key") or GPT_API_KEY,
        path.get("base_url") or tenshi_config.GPT_BASE_URL,
    )
    api_messages = _messages_to_openai_format(system_prompt, messages)
    try:
        completion = _chat_completions_create(
            client,
            {
                "model": model_name,
                "messages": api_messages,
                "reasoning_effort": reasoning_effort or getattr(tenshi_config, "GPT_REASONING_EFFORT", "xhigh"),
                "stream": False,
            },
            label="GPT",
        )
        content = completion.choices[0].message.content or ""
        return content, ""
    except Exception as e:
        logger.error(f"GPT API 调用失败：{format_exception_inline(e)}")
        raise e























_REASONING_EFFORT_PATTERN = re.compile(r"reasoning[_.]?effort|'reasoning'|\"reasoning\"", re.I)


def _mentions_reasoning_effort(exc: BaseException) -> bool:
    """错误是否指向 reasoning_effort 参数本身（而不是别的 400 原因）。"""
    return bool(_REASONING_EFFORT_PATTERN.search(str(exc) or ""))


def _gpt_tool_reasoning_effort() -> str | None:
    """带 tools 调用时的思考强度；返回 None 表示**不带**该参数（默认）。

    GPT-5.4 曾拒绝「tools + reasoning_effort」，gpt-6-luna 沿用同一保守规则：默认不传；
    只有在明确知道该模型/代理支持这个组合时，才用 env 显式打开。
    """
    raw = getattr(tenshi_config, "GPT_TOOL_REASONING_EFFORT", None)
    val = str(raw).strip() if raw is not None else ""
    if not val or val.lower() in ("omit", "off", "none", "unset"):
        return None
    return val


def _chat_completions_create(client, kwargs: dict, *, label: str):
    """Chat Completions 调用 + 「遇到冲突就不带思考强度」的兜底。

    上游若因 reasoning_effort 报错（tools 冲突、或该模型不支持这个档位），
    就地**去掉这个可选参数**重试一次——既不换端点也不换模型：一个可选参数不该让整条模型链
    判死（这次事故就是这么烧掉的）。去掉后仍失败时异常照常抛出，上层继续换通路/换模型。
    """
    try:
        ensure_active()
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "reasoning_effort" not in kwargs or not _mentions_reasoning_effort(exc):
            raise
        logger.warning(
            f"{label}：reasoning_effort={kwargs.get('reasoning_effort')!r} 被上游拒绝"
            f"（{exc}），改为不带思考强度重试一次"
        )

    retry = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
    try:
        ensure_active()
        result = client.chat.completions.create(**retry)
    except Exception as exc:
        logger.error(f"{label}：去掉 reasoning_effort 后仍失败：{format_exception_inline(exc)}")
        raise
    logger.warning(f"{label}：已改为不带思考强度重试成功")
    return result


def call_gpt_with_tools(model_name, system_prompt, messages, tools, llm_path=None):
    """以 OpenAI 兼容 tool calling 方式调用 GPT-Flash（主对话 low 档随机首选之一）。

    与 call_gpt 的差别：带 tools / tool_choice，供主对话工具循环使用。
    历史模型在 chat/completions 下不允许「tools + reasoning_effort」共存，因此这里默认
    **不带思考强度**（GPT_TOOL_REASONING_EFFORT 留空 = 不传该参数）；若显式配了值而被打回，
    `_chat_completions_create` 会去掉该参数重试一次，不会让整条链判死。
    这里不传 temperature（GPT-Flash 只接受默认值）。stream=False。

    返回 (content_text, tool_calls_list)，元素形如 {id, type, function:{name, arguments}}。
    """
    path = llm_path or {
        "name": "OpenAI",
        "api_key": GPT_API_KEY,
        "base_url": tenshi_config.GPT_BASE_URL,
    }
    client = _build_openai_client(
        path.get("api_key") or GPT_API_KEY,
        path.get("base_url") or tenshi_config.GPT_BASE_URL,
    )
    api_messages = _messages_to_openai_format_with_tools(system_prompt, messages)
    kwargs: dict = {"model": model_name, "messages": api_messages, "stream": False}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    effort = _gpt_tool_reasoning_effort()
    if effort:
        kwargs["reasoning_effort"] = effort
    try:
        completion = _chat_completions_create(client, kwargs, label="GPT 工具调用")
        choice_msg = completion.choices[0].message
        content = getattr(choice_msg, "content", None) or ""
        raw_tool_calls = getattr(choice_msg, "tool_calls", None) or []
        return content, _normalize_tool_calls(raw_tool_calls)
    except Exception as e:
        logger.error(f"GPT 工具调用 API 失败：{format_exception_inline(e)}")
        raise e


def call_grok(model_name, system_prompt, messages, llm_path=None):
    """
    通过 OpenAI 兼容层调用 Grok（xAI）API；支持官方与代理通路。
    """
    path = llm_path or {
        "name": "Official",
        "api_key": GROK_API_KEY,
        "base_url": tenshi_config.GROK_BASE_URL,
    }
    client = _build_openai_client(
        path.get("api_key") or GROK_API_KEY,
        path.get("base_url") or tenshi_config.GROK_BASE_URL,
    )

    grok_messages = _messages_to_openai_format(system_prompt, messages)

    try:

        ensure_active()
        completion = client.chat.completions.create(
            model=model_name,
            messages=grok_messages,
            temperature=0.7,
            stream=False
        )

        content = completion.choices[0].message.content or ""
        return content, ""

    except Exception as e:
        logger.error(f"Grok API 调用失败：{format_exception_inline(e)}")
        raise e




def _call_openai_compat_with_tools(
    *,
    label: str,
    default_name: str,
    default_api_key: str | None,
    default_base_url: str | None,
    model_name,
    system_prompt,
    messages,
    tools,
    llm_path=None,
):
    """OpenAI 兼容网关的 tool calling 骨架（MagmaRouter / OpenRouter 共用）。"""
    path = llm_path or {
        "name": default_name,
        "api_key": default_api_key,
        "base_url": default_base_url,
    }
    client = _build_openai_client(
        path.get("api_key") or default_api_key,
        path.get("base_url") or default_base_url,
    )
    api_messages = _messages_to_openai_format_with_tools(system_prompt, messages)
    try:
        ensure_active()
        completion = client.chat.completions.create(
            model=model_name,
            messages=api_messages,
            temperature=0.7,
            stream=False,
            tools=tools or [],
            tool_choice="auto",
        )
        choice_msg = completion.choices[0].message
        content = getattr(choice_msg, "content", None) or ""
        raw_tool_calls = getattr(choice_msg, "tool_calls", None) or []
        tool_calls = _normalize_tool_calls(raw_tool_calls)
        return content, tool_calls
    except Exception as e:
        logger.error(f"{label} 工具调用 API 失败：{format_exception_inline(e)}")
        raise e


def call_magmarouter_with_tools(model_name, system_prompt, messages, tools, llm_path=None):
    """以 OpenAI 兼容 tool calling 方式调用 MagmaRouter（grok 槽主路）。"""
    return _call_openai_compat_with_tools(
        label="MagmaRouter",
        default_name="MagmaRouter",
        default_api_key=MAGMAROUTER_API_KEY,
        default_base_url=tenshi_config.MAGMAROUTER_BASE_URL,
        model_name=model_name,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        llm_path=llm_path,
    )


def call_openrouter_with_tools(model_name, system_prompt, messages, tools, llm_path=None):
    """以 OpenAI 兼容 tool calling 方式调用 OpenRouter（MagmaRouter 之后的备选）。"""
    return _call_openai_compat_with_tools(
        label="OpenRouter",
        default_name="OpenRouter",
        default_api_key=OPENROUTER_API_KEY,
        default_base_url=tenshi_config.OPENROUTER_BASE_URL,
        model_name=model_name,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        llm_path=llm_path,
    )


def call_grok_with_tools(model_name, system_prompt, messages, tools, llm_path=None):
    """
    以 OpenAI 兼容的 tool calling 方式调用 Grok（xAI）API。

    返回：
      (content_text, tool_calls_list)
    其中 tool_calls_list 是规范化后的列表，元素形如：
      {id, type, function:{name, arguments}}
    """
    path = llm_path or {
        "name": "Official",
        "api_key": GROK_API_KEY,
        "base_url": tenshi_config.GROK_BASE_URL,
    }
    client = _build_openai_client(
        path.get("api_key") or GROK_API_KEY,
        path.get("base_url") or tenshi_config.GROK_BASE_URL,
    )

    grok_messages = _messages_to_openai_format_with_tools(system_prompt, messages)

    try:
        ensure_active()
        completion = client.chat.completions.create(
            model=model_name,
            messages=grok_messages,
            temperature=0.7,
            stream=False,
            tools=tools or [],
            tool_choice="auto",
        )

        choice_msg = completion.choices[0].message
        content = getattr(choice_msg, "content", None) or ""

        raw_tool_calls = getattr(choice_msg, "tool_calls", None) or []
        tool_calls = _normalize_tool_calls(raw_tool_calls)
        return content, tool_calls

    except Exception as e:
        logger.error(f"Grok 工具调用 API 失败：{format_exception_inline(e)}")
        raise e


_ROUTING_REFUSAL_PHRASES = (
    "我不能协助",
    "我无法协助",
    "不能协助你",
    "无法协助你",
    "无法完成这个内容",
    "不能完成这个内容",
    "i can't help",
    "i cannot help",
    "i cannot assist",
    "i can't assist",
    "i'm unable to",
    "i am unable to",
    "as an ai",
    "against my guidelines",
    "违反我的",
    "不符合我的使用政策",
)


def _is_routing_refusal(text: str) -> bool:
    """模型拒答视为 NSFW 匹配（如「我不能协助你完成这个内容」）。"""
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    return any(p in lower or p in raw for p in _ROUTING_REFUSAL_PHRASES)


def _is_invalid_routing_response(text: str, parsed: dict | None) -> bool:
    """判断前置路由输出是否“无效”。

    善意推定规则：
    - 只有在“无法解析成 JSON dict”（非法 JSON）时，才认为无效并继续容灾；
    - 若模型缺少 `nsfw` 字段，则视为“未给出明确否定信号”，按 NSFW=false 安全默认处理（不跳过该次输出）。
    """
    raw = (text or "").strip()
    if not raw:
        return True
    if not isinstance(parsed, dict):
        return True
    return False


def _safe_pre_dialog_routing_fallback(reason: str) -> dict:
    return {
        "nsfw": False,
        "low_mood_intervention": False,
        "dialog_grade": {"score": 50, "tier": "medium", "reason": reason},
    }


def _build_dialog_grade_from_parsed(parsed: dict, query: str, history_context: str) -> dict:
    score, score_source = _compute_score_from_rubric_fields(parsed)
    llm_tier = str(parsed.get("tier", "")).strip().lower()
    score, hard_reason = _apply_dialog_grade_hard_rules(score, query, history_context)
    max_th = int(getattr(tenshi_config, "CONVERSATION_GRADE_MAX_THRESHOLD", 88) or 88)
    high_th = int(getattr(tenshi_config, "CONVERSATION_GRADE_HIGH_THRESHOLD", 72) or 72)
    med_th = int(getattr(tenshi_config, "CONVERSATION_GRADE_MEDIUM_THRESHOLD", 42) or 42)
    if score >= max_th:
        tier = "max"
    elif score >= high_th:
        tier = "high"
    elif score >= med_th:
        tier = "medium"
    else:
        tier = "low"
    reason = str(parsed.get("reason", "") or "").strip()
    reason = f"{reason} | {score_source}".strip(" |")
    if hard_reason:
        reason = f"{reason} | {hard_reason}".strip(" |")
    if llm_tier and llm_tier not in ("max", "high", "medium", "low"):
        reason = f"{reason} | llm_tier_invalid:{llm_tier}".strip(" |")
    return {"score": score, "tier": tier, "reason": reason}


def _parse_pre_dialog_routing_response(text: str) -> tuple[dict | None, dict | None]:
    """解析合并路由 JSON；返回 (pre_route_fields, grade_parsed)。"""
    raw = (text or "").strip()
    if not raw:
        return None, None

    lowered = raw.lower()
    if lowered in ("true", "false"):
        return {"nsfw": lowered == "true", "low_mood_intervention": False}, None

    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"(\{[\s\S]*\})", raw)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except Exception:
                parsed = None

    if not isinstance(parsed, dict):
        return None, None

    def _as_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return False

    pre_route = {
        "nsfw": _as_bool(parsed.get("nsfw")),
        "low_mood_intervention": _as_bool(parsed.get("low_mood_intervention")),
    }
    return pre_route, parsed


def _invoke_pre_dialog_routing_llm(provider: str, system_prompt: str, user_prompt: str) -> str:
    """前置分流/分级的单 provider 调用：**走统一 path 构造（代理 → OpenRouter → 官方）**。

    2026-10 口径：除 MagmaRouter（其 xploded 模型在 OpenRouter 无对应 slug）外，
    每个 provider 都必须有 OpenRouter 同款兜底；本函数不再各自直连 SDK。
    """
    messages = [{"role": "user", "content": user_prompt}]

    if provider == "claude":
        from tenshi_reply import _plain_text_call

        model_name = tenshi_config.claude_lite_primary()
        return _plain_text_call(
            "claude", model_name, system_prompt, messages, reasoning_effort="xhigh"
        )

    if provider == "gemini":

        from tenshi_reply import call_gemini
        from llm_routing import build_gemini_paths
        import provider_health

        model_name = tenshi_config.gemini_lite_primary()
        paths = provider_health.filter_paths(
            "gemini",
            build_gemini_paths(tenshi_config.GEMINI_API_KEY, tenshi_config.GEMINI_PROXIES),
        )
        last_exc: Exception | None = None
        for path in paths:
            try:
                if path.get("openrouter") or path.get("protocol") == "openai":
                    slug = (
                        tenshi_config.openrouter_fallback_slug(model_name)
                        or f"{path.get('vendor_prefix', '')}{model_name}"
                    )
                    response, _ = call_openrouter(
                        slug, system_prompt, messages, llm_path=path
                    )
                    return response
                response, _, _ = call_gemini(
                    model_name=model_name,
                    system_prompt=system_prompt,
                    messages=messages,
                    llm_path=path,
                )
                return response
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("gemini pre-routing: no usable path")

    if provider in ("gpt", "grok", "deepseek"):
        from llm_routing import (
            build_deepseek_paths,
            build_gpt_paths,
            build_grok_paths,
        )
        import provider_health

        if provider == "gpt":
            model_name = tenshi_config.gpt_flash_primary()
            paths = build_gpt_paths(
                tenshi_config.GPT_API_KEY,
                tenshi_config.GPT_BASE_URL,
                tenshi_config.GPT_PROXIES,
            )
        elif provider == "grok":
            model_name = tenshi_config.grok_primary()
            paths = build_grok_paths(
                tenshi_config.GROK_API_KEY,
                tenshi_config.GROK_BASE_URL,
                tenshi_config.GROK_PROXIES,
            )
        else:
            model_name = tenshi_config.deepseek_preprocess_primary()
            paths = build_deepseek_paths(
                tenshi_config.DEEPSEEK_API_KEY,
                tenshi_config.DEEPSEEK_BASE_URL,
                tenshi_config.DEEPSEEK_PROXIES,
            )
        paths = provider_health.filter_paths(provider, paths)
        last_exc = None
        for path in paths:
            try:
                is_openrouter = bool(path.get("openrouter")) or path.get("name") == "OpenRouter"
                if is_openrouter:
                    slug = (
                        tenshi_config.openrouter_fallback_slug(model_name)
                        or f"{path.get('vendor_prefix', '')}{model_name}"
                    )
                    response, _ = call_openrouter(
                        slug, system_prompt, messages, llm_path=path
                    )
                    return response
                if provider == "gpt":
                    response, _ = call_gpt(
                        model_name, system_prompt, messages, llm_path=path
                    )
                elif provider == "grok":
                    response, _ = call_grok(
                        model_name, system_prompt, messages, llm_path=path
                    )
                else:
                    from deepseek_modules import call_deepseek

                    response, _ = call_deepseek(
                        model_name, system_prompt, messages, llm_path=path
                    )
                return response
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"{provider} pre-routing: no usable path")

    if provider == "magmarouter":


        response, _ = call_magmarouter(
            model_name=tenshi_config.magmarouter_primary(),
            system_prompt=system_prompt,
            messages=messages,
        )
        return response
    if provider == "openrouter":
        response, _ = call_openrouter(
            model_name=tenshi_config.openrouter_primary(),
            system_prompt=system_prompt,
            messages=messages,
        )
        return response

    from deepseek_modules import call_deepseek

    response, _ = call_deepseek(
        model_name=tenshi_config.deepseek_preprocess_primary(),
        system_prompt=system_prompt,
        messages=messages,
    )
    return response


def _nsfw_pre_dialog_routing_match(reason: str) -> dict:
    return {
        "nsfw": True,
        "low_mood_intervention": False,
        "dialog_grade": {"score": 100, "tier": "high", "reason": reason},
    }


def _pre_routing_deadlines() -> tuple[float, float]:
    """读前置路由的两个上限（秒）：(单模型上限, 整链总预算)；<=0 表示不设该上限。"""
    per_provider = float(getattr(tenshi_config, "PRE_ROUTING_PROVIDER_TIMEOUT_SEC", 0.0) or 0.0)
    total_budget = float(getattr(tenshi_config, "PRE_ROUTING_TOTAL_TIMEOUT_SEC", 0.0) or 0.0)
    return per_provider, total_budget


def _pre_routing_min_attempt() -> float:
    """剩余预算小于这个「最小可用片」就不再启动下一个模型（<=0 关闭）。"""
    return float(getattr(tenshi_config, "PRE_ROUTING_MIN_ATTEMPT_SEC", 0.0) or 0.0)


def run_pre_dialog_routing(query: str, history_context: str = "") -> dict:
    """
    前置分流 + 对话分级：GPT-Flash(gpt-6-luna, xhigh) → Gemini Lite → Grok → MagmaRouter → OpenRouter → DeepSeek 逐级容灾。
    返回：
      {
        "nsfw": bool,
        "low_mood_intervention": bool,
        "dialog_grade": {"score": int, "tier": str, "reason": str},
      }
    拒答视为 NSFW 匹配；若无明确否定信号（如缺少 nsfw 字段）则按 NSFW=false 安全默认。

    超时兜底（主回复不等卡死的模型）：
      * 每个模型最多等 PRE_ROUTING_PROVIDER_TIMEOUT_SEC（默认 20s），到点即放弃该模型、
        直接切下一个，不再让一个挂起的请求把整轮回复拖满全局读超时（300s）；
      * 整条链另有 PRE_ROUTING_TOTAL_TIMEOUT_SEC 总预算（默认 90s，序列变长后上调），
        用尽即停止尝试、回退安全默认。

    序列：GPT-Flash → Gemini Lite → Grok → (OpenRouter) → DeepSeek，
    之后按「Flash 双兜底」规则追加 DeepSeek Flash / Gemini Flash
    （原序列已含该 provider 的不重复追加，见 tenshi_reply._pre_dialog_routing_providers）。
    """
    if "SAFE" in query:
        logger.info("前置路由：因包含 SAFE 关键词而跳过（安全）。")
        return _safe_pre_dialog_routing_fallback("safe keyword")
    if "R18" in query:
        logger.info("前置路由：因包含 R18 关键词而判定 NSFW。")
        return {
            "nsfw": True,
            "low_mood_intervention": False,
            "dialog_grade": {"score": 100, "tier": "high", "reason": "r18 keyword"},
        }

    system_prompt = getattr(tenshi_config, "GROK_PRE_DIALOG_ROUTING_PROMPT", "").strip()
    if not system_prompt:
        system_prompt = (
            getattr(tenshi_config, "GROK_PRE_ROUTE_PROMPT", "").strip()
            + "\n\n"
            + getattr(tenshi_config, "GROK_DIALOG_GRADE_PROMPT", "").strip()
        )

    user_prompt = (
        "Latest user input:\n"
        f"{query or ''}\n\n"
        "Recent context (may be empty):\n"
        f"{history_context or ''}\n"
    )

    response_text = ""



    try:
        from tenshi_reply import _pre_dialog_routing_providers

        routing_providers = list(_pre_dialog_routing_providers())
    except Exception as e:

        logger.warning(f"前置路由 provider 序列构建失败，回退原序列：{format_exception_inline(e)}")
        routing_providers = ["gpt", "gemini", "grok"]
        if tenshi_config.MAGMAROUTER_API_KEY:
            routing_providers.append("magmarouter")
        if tenshi_config.OPENROUTER_API_KEY:
            routing_providers.append("openrouter")
        routing_providers.append("deepseek")
    provider_labels = {
        "magmarouter": "MagmaRouter",
        "openrouter": "OpenRouter",
        "grok": "Grok",
        "gemini": "Gemini",
        "gpt": "GPT",
        "deepseek": "DeepSeek",
        "claude": "Claude Haiku",
    }

    per_provider_timeout, total_budget = _pre_routing_deadlines()
    min_attempt = _pre_routing_min_attempt()
    chain_deadline = time.monotonic() + total_budget if total_budget > 0 else None


    try:
        import provider_health

        before = list(routing_providers)
        routing_providers = [p for p in routing_providers if provider_health.provider_enabled(p)]
        banned = [p for p in before if p not in routing_providers]
        if banned:
            logger.warning(
                f"[provider-health] 前置路由按探活结果跳过 {'、'.join(banned)}（该类别的通路全红）。"
            )
    except Exception as e:
        logger.warning(f"[provider-health] 前置路由 gate 失败（fail-open）：{format_exception_inline(e)}")

    for provider in routing_providers:
        label = provider_labels.get(provider, provider)

        attempt_timeout = per_provider_timeout
        if chain_deadline is not None:
            remaining = chain_deadline - time.monotonic()



            if remaining <= 0 or (min_attempt > 0 and remaining < min_attempt):
                logger.warning(
                    "前置+分级路由总预算（PRE_ROUTING_TOTAL_TIMEOUT_SEC）已用尽"
                    f"（剩余 {remaining:.3f}s < 最小可用片 {min_attempt:.3f}s），"
                    "停止尝试剩余模型，回退安全默认。"
                )
                break
            attempt_timeout = min(attempt_timeout, remaining) if attempt_timeout > 0 else remaining

        if attempt_timeout > 0:
            logger.info(f"前置+分级路由：正在调用 {label}（上限 {attempt_timeout:.1f}s）...")
        else:
            logger.info(f"前置+分级路由：正在调用 {label}...")
        try:
            response_text = call_with_deadline(
                lambda p=provider: _invoke_pre_dialog_routing_llm(p, system_prompt, user_prompt),
                timeout_sec=attempt_timeout,
                label=f"pre-routing:{label}",
            )
        except LlmDeadlineExceeded:
            logger.warning(
                f"前置+分级路由 {label} 超过 {attempt_timeout:.1f}s 无返回，直接切下一个模型。"
            )
            continue
        except Exception as e:
            logger.error(f"前置+分级路由 {label} 调用失败：{format_exception_inline(e)}")
            continue

        pre_route, grade_parsed = _parse_pre_dialog_routing_response(response_text)
        if _is_routing_refusal(response_text):
            logger.info(f"前置+分级路由 {label} 拒答，视为 NSFW 匹配。")
            return _nsfw_pre_dialog_routing_match(f"refusal:{label.lower()}")

        if _is_invalid_routing_response(response_text, grade_parsed):
            logger.warning(f"前置+分级路由 {label} 输出无效，尝试下一容灾或安全默认。")
            continue

        dialog_grade = _build_dialog_grade_from_parsed(grade_parsed, query, history_context)
        result = {
            "nsfw": bool(pre_route.get("nsfw")),
            "low_mood_intervention": bool(pre_route.get("low_mood_intervention")),
            "dialog_grade": dialog_grade,
        }
        logger.info(
            f"前置+分级路由结果（{label}）：nsfw={result['nsfw']} "
            f"low_mood_intervention={result['low_mood_intervention']} "
            f"tier={dialog_grade.get('tier')} score={dialog_grade.get('score')} "
            f"(i={_safe_int(grade_parsed.get('importance_score'), -1)} "
            f"c={_safe_int(grade_parsed.get('complexity_score'), -1)} "
            f"r={_safe_int(grade_parsed.get('risk_score'), -1)} "
            f"t={_safe_int(grade_parsed.get('tooling_score'), -1)})"
        )
        return result

    logger.error("前置+分级路由全部失败或输出无效，回退为安全默认（NSFW=false）。")
    return _safe_pre_dialog_routing_fallback("routing fallback")



def _call_grok_slot_text(
    system_prompt: str,
    user_prompt: str,
    *,
    prefer_reasoning_grok: bool = False,
) -> tuple[str, str]:
    """MagmaRouter 主路 → OpenRouter 备选 → Grok 容灾的简单文本调用。

    每一档都有自己的硬上限（`REPLY_AUX_LLM_TIMEOUT_SEC`，默认 45s）：
    到点即放弃该档、切下一档；各档都失败时把最后一次异常抛给调用方
    （`rewrite_nsfw_content` 会回退为原文，绝不返回错误占位）。
    """
    messages = [{"role": "user", "content": user_prompt}]
    grok_model = (
        next((m for m in tenshi_config.grok_models() if "reasoning" in m), tenshi_config.grok_primary())
        if prefer_reasoning_grok
        else tenshi_config.grok_primary()
    )
    last_err: Exception | None = None
    if tenshi_config.MAGMAROUTER_API_KEY:
        try:
            return _bounded(
                lambda: call_magmarouter(
                    model_name=tenshi_config.magmarouter_primary(),
                    system_prompt=system_prompt,
                    messages=messages,
                ),
                "grok-slot-text:magmarouter",
            )
        except Exception as e:
            last_err = e
            logger.warning(
                f"MagmaRouter slot call failed, falling back to OpenRouter/Grok: {format_exception_inline(e)}"
            )
    if tenshi_config.OPENROUTER_API_KEY:
        try:
            return _bounded(
                lambda: call_openrouter(
                    model_name=tenshi_config.openrouter_primary(),
                    system_prompt=system_prompt,
                    messages=messages,
                ),
                "grok-slot-text:openrouter",
            )
        except Exception as e:
            last_err = e
            logger.warning(f"OpenRouter slot call failed, falling back to Grok: {format_exception_inline(e)}")
    try:
        return _bounded(
            lambda: call_grok(
                model_name=grok_model,
                system_prompt=system_prompt,
                messages=messages,
            ),
            "grok-slot-text:grok",
        )
    except Exception as e:
        last_err = e
        logger.error(f"Grok slot call also failed: {format_exception_inline(e)}")
    if last_err is not None:
        raise last_err
    raise RuntimeError("grok slot text: no usable API path configured")


def rewrite_nsfw_content(user_query, bot_response):
    """
    将对话内容改写为 SFW（适合公开场合）。
    返回：(sfw_user_query, sfw_bot_response)
    """
    logger.info("正在将 NSFW 内容改写为 SFW...")
    system_prompt = tenshi_config.GROK_NSFW_REWRITE_PROMPT
    try:

        user_rewrite_prompt = (
            "Rewrite the following user query to be Safe For Work (SFW) while keeping "
            "the original intent as much as possible:\n\n"
            f"{user_query}"
        )
        sfw_user_query, _ = _call_grok_slot_text(
            system_prompt,
            user_rewrite_prompt,
            prefer_reasoning_grok=True,
        )


        bot_rewrite_prompt = (
            "Rewrite the following bot response to be Safe For Work (SFW) while keeping "
            "the character's personality:\n\n"
            f"{bot_response}"
        )
        sfw_bot_response, _ = _call_grok_slot_text(
            system_prompt,
            bot_rewrite_prompt,
            prefer_reasoning_grok=True,
        )
        sfw_user_query = _extract_sfw_rewrite_text(
            sfw_user_query,
            fallback=user_query,
            preferred_keys=["user_query", "query", "rewritten_user_query", "user"],
        )
        sfw_bot_response = _extract_sfw_rewrite_text(
            sfw_bot_response,
            fallback=bot_response,
            preferred_keys=["bot_response", "bot_answer", "response", "answer", "rewritten_bot_response"],
        )
        return sfw_user_query, sfw_bot_response

    except Exception as e:
        logger.error(f"NSFW 改写失败：{format_exception_inline(e)}")

        return user_query, bot_response


def _extract_sfw_rewrite_text(raw_text, fallback: str, preferred_keys: list[str]) -> str:
    """从模型返回中提取真正改写文本，避免把结构化包装写入日志/回复。"""
    text = (raw_text or "").strip()
    if not text:
        return fallback


    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    candidate = fence_match.group(1).strip() if fence_match else text

    def _value_from_obj(obj):
        if not isinstance(obj, dict):
            return None
        for key in preferred_keys:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None


    try:
        parsed = json.loads(candidate)
        extracted = _value_from_obj(parsed)
        if extracted:
            return extracted
    except Exception:
        pass


    obj_match = re.search(r"(\{[\s\S]*\})", candidate)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(1))
            extracted = _value_from_obj(parsed)
            if extracted:
                return extracted
        except Exception:
            pass


    lower_text = text.lower()
    if "rewrite the following" in lower_text and preferred_keys:
        return fallback

    return text
