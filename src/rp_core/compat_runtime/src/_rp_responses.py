# SPDX-License-Identifier: AGPL-3.0-only
"""Adapt the existing Chat client boundary for opt-in Responses-only gateways.

Stateless requests replay the existing history and tool IDs. No prompt, model,
database, legacy gateway, or outer retry/failover policy is changed.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from openai.types.chat import ChatCompletion, ChatCompletionChunk


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def request_options(kwargs):
    source = dict(kwargs)
    messages = source.pop("messages")
    result = {"model": source.pop("model"), "input": [], "store": False}
    for message in messages:
        role, content = message["role"], message.get("content")
        if role == "tool":
            if not message.get("tool_call_id"):
                raise ValueError("Tool output requires its original tool call ID")
            result["input"].append({"type": "function_call_output", "call_id": message["tool_call_id"],
                                    "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if content:
            if isinstance(content, list):
                converted = []
                for block in content:
                    kind = block.get("type")
                    if kind == "text":
                        converted.append({"type": "input_text", "text": block["text"]})
                    elif kind == "image_url" and role == "user":
                        image = block["image_url"]
                        converted.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
                    else:
                        raise ValueError("Unsupported content block at Responses boundary")
                content = "".join(block["text"] for block in converted) if role == "assistant" else converted
            result["input"].append({"role": role, "content": content})
        for tool in message.get("tool_calls") or []:
            function = tool["function"]
            result["input"].append({"type": "function_call", "call_id": tool["id"],
                                    "name": function["name"], "arguments": function["arguments"]})
    tools = source.pop("tools", None)
    if tools:
        result["tools"] = []
        for tool in tools:
            if tool.get("type") != "function":
                raise ValueError("This compatibility boundary supports function tools only")
            result["tools"].append({"type": "function", **tool["function"], "strict": tool["function"].get("strict", False)})
    choice = source.pop("tool_choice", None)
    if choice is not None and tools:
        result["tool_choice"] = ({"type": "function", "name": choice["function"]["name"]}
                                 if isinstance(choice, dict) else choice)
    effort = source.pop("reasoning_effort", None)
    if effort:
        result["reasoning"] = {"effort": effort}
    limit = source.pop("max_completion_tokens", source.pop("max_tokens", None))
    if limit is not None:
        result["max_output_tokens"] = limit
    fmt = source.pop("response_format", None)
    if fmt:
        result["text"] = {"format": {"type": "json_schema", **fmt["json_schema"]}
                          if fmt.get("type") == "json_schema" else fmt}
    source.pop("stream_options", None)  # Responses streaming always carries terminal usage.
    allowed = {"stream", "temperature", "top_p", "parallel_tool_calls", "timeout", "extra_headers", "extra_body", "extra_query"}
    if set(source) - allowed:
        raise ValueError("Unsupported Chat options at Responses boundary: " + ", ".join(sorted(set(source) - allowed)))
    result.update(source)
    return result


def finish_reason(response, tools=False):
    status = value(response, "status", "completed")
    if status == "incomplete" and value(value(response, "incomplete_details", {}), "reason") == "max_output_tokens":
        return "length"
    if status not in (None, "completed"):
        raise RuntimeError("Responses gateway did not complete the request")
    return "tool_calls" if tools else "stop"


def usage(response):
    source = value(response, "usage")
    if not source:
        return None
    inp, out = value(source, "input_tokens", 0) or 0, value(source, "output_tokens", 0) or 0
    result = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": value(source, "total_tokens", inp + out)}
    details = value(source, "input_tokens_details")
    if details:
        result["prompt_tokens_details"] = {"cached_tokens": value(details, "cached_tokens", 0) or 0}
    details = value(source, "output_tokens_details")
    if details:
        result["completion_tokens_details"] = {"reasoning_tokens": value(details, "reasoning_tokens", 0) or 0}
    return result


def completion(response, model):
    texts, calls, refusals = [], [], []
    for item in value(response, "output", []) or []:
        if value(item, "type") == "function_call":
            calls.append({"id": value(item, "call_id"), "type": "function",
                          "function": {"name": value(item, "name"), "arguments": value(item, "arguments", "")}})
        if value(item, "type") == "message":
            for block in value(item, "content", []) or []:
                if value(block, "type") == "output_text":
                    texts.append(value(block, "text", ""))
                elif value(block, "type") == "refusal":
                    refusals.append(value(block, "refusal", ""))
    return ChatCompletion(id=value(response, "id", "rp-response"), created=int(value(response, "created_at", time.time())),
                          model=value(response, "model", model), object="chat.completion", usage=usage(response),
                          choices=[{"index": 0, "finish_reason": finish_reason(response, bool(calls)),
                                    "message": {"role": "assistant", "content": "".join(texts),
                                                "tool_calls": calls or None, "refusal": "".join(refusals) or None}}])


def chunks(stream, model):
    tools, completed = {}, False
    def chunk(delta, finish=None, response=None):
        return ChatCompletionChunk(id=value(response, "id", "rp-response"), created=int(time.time()),
                                   model=model, object="chat.completion.chunk", usage=usage(response),
                                   choices=[{"index": 0, "delta": delta, "finish_reason": finish}])
    try:
        for event in stream:
            kind = value(event, "type")
            if kind == "response.output_text.delta":
                yield chunk({"content": value(event, "delta", "")})
            elif kind == "response.output_item.added" and value(value(event, "item", {}), "type") == "function_call":
                item = value(event, "item")
                index = len(tools)
                tools[value(event, "output_index")] = index
                yield chunk({"tool_calls": [{"index": index, "id": value(item, "call_id"), "type": "function",
                                             "function": {"name": value(item, "name"), "arguments": value(item, "arguments", "")}}]})
            elif kind == "response.function_call_arguments.delta":
                index = tools.get(value(event, "output_index"))
                if index is None:
                    raise RuntimeError("Responses tool delta has no matching tool call")
                yield chunk({"tool_calls": [{"index": index, "function": {"arguments": value(event, "delta", "")}}]})
            elif kind in ("response.completed", "response.incomplete"):
                response = value(event, "response")
                yield chunk({}, finish_reason(response, bool(tools)), response)
                completed = True
            elif kind in ("error", "response.failed"):
                raise RuntimeError("Responses gateway stream failed")
        if not completed:
            raise RuntimeError("Responses stream ended without a terminal response")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


class ResponsesChatClient:
    def __init__(self, client):
        self._client = client
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        options = request_options(kwargs)
        response = self._client.responses.create(**options)
        return chunks(response, options["model"]) if options.get("stream") else completion(response, options["model"])

    def __getattr__(self, name):
        return getattr(self._client, name)
