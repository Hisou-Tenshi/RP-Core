# SPDX-License-Identifier: AGPL-3.0-only
"""Shared model routing. Provider settings live in the public configuration."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request

from rp_core.demo import model_handler
from rp_core.protocol import MAX_FRAME_BYTES, MODEL, RPCError, dumps, loads, validate


def post_json(url: str, payload: dict, *, headers: dict | None = None, timeout: float = 60) -> dict:
    started = time.monotonic()
    stream_requested = payload.get("stream") is True
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    ):
        raise RPCError("invalid_configuration", "API endpoints require HTTPS.")
    if parsed.username or parsed.password:
        raise RPCError("invalid_configuration", "Credentials must not be embedded in URLs.")
    request = urllib.request.Request(  # noqa: S310 -- URL scheme checked above.
        url,
        data=dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as result:
            if stream_requested and result.headers.get_content_type() == "text/event-stream":
                return _read_chat_stream(result, started + timeout)
            raw = result.read(MAX_FRAME_BYTES + 1)
    except urllib.error.HTTPError as exc:
        with exc:
            if stream_requested and exc.code in (400, 405, 422, 501):
                message = str(exc.reason).lower() + " " + _read_error_detail(exc)
                if "stream" in message and any(
                    cue in message
                    for cue in ("not support", "unsupported", "not implemented", "stream=false")
                ):
                    raise RPCError("stream_unsupported", "The model route cannot stream.") from None
        raise RPCError(
            "provider_unavailable", "An external API request failed.", retryable=True
        ) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise RPCError(
            "provider_unavailable", "An external API request failed.", retryable=True
        ) from None
    if len(raw) > MAX_FRAME_BYTES:
        raise RPCError("frame_too_large", "External API response exceeds the size limit.")
    value = loads(raw)
    if not isinstance(value, dict):
        raise RPCError("invalid_model_output", "External API returned a non-object response.")
    return value


def _read_error_detail(response):
    try:
        return response.read(4096).decode(errors="replace").lower()
    except OSError:
        return ""


def _read_chat_stream(response, first_text_deadline: float) -> dict:
    """Collect an SSE completion within the same response size limit as JSON."""
    text, calls, event = [], {}, []
    received_text, finished, total = False, False, 0
    while True:
        if not received_text and time.monotonic() >= first_text_deadline:
            raise TimeoutError("Model did not stream text before the deadline.")
        line = response.readline(MAX_FRAME_BYTES - total + 1)
        if not line:
            break
        total += len(line)
        if total > MAX_FRAME_BYTES:
            raise RPCError("frame_too_large", "External API response exceeds the size limit.")
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            event.append(line[5:].removeprefix(b" "))
        if line or not event:
            continue
        data = b"\n".join(event)
        event.clear()
        if data == b"[DONE]":
            finished = True
            break
        chunk = loads(data)
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise RPCError("invalid_model_output", "The provider stream returned an error.")
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        finished = finished or bool(choice.get("finish_reason"))
        delta = choice.get("delta") or {}
        content = delta.get("content") or ""
        if content:
            if not received_text and time.monotonic() >= first_text_deadline:
                raise TimeoutError("Model did not stream text before the deadline.")
            received_text = received_text or bool(content.strip())
            text.append(content)
        for fragment in delta.get("tool_calls") or []:
            call = calls.setdefault(
                fragment["index"], {"id": "", "function": {"name": "", "arguments": ""}}
            )
            if fragment.get("id"):
                call["id"] = fragment["id"]
            function = fragment.get("function") or {}
            call["function"]["name"] += function.get("name") or ""
            call["function"]["arguments"] += function.get("arguments") or ""
    if not finished or not (text or calls):
        raise RPCError("invalid_model_output", "The provider stream ended without a completion.")
    return {
        "choices": [
            {"message": {"content": "".join(text), "tool_calls": [calls[i] for i in sorted(calls)]}}
        ]
    }


class ModelRouter:
    def __init__(self, config: dict):
        self.config = config

    def generate(self, params: dict) -> dict:
        routes = self.config["routes"]
        chain = routes.get(params["purpose"], routes["default"])
        for name in chain:
            provider = self.config["providers"][name]
            for attempt in range(provider.get("attempts", 1)):
                try:
                    result = self._one(provider, params)
                    validate(result, MODEL, code="invalid_model_output")
                    if not result.get("tool_calls"):
                        validate(
                            result.get("output"),
                            params["response_schema"],
                            code="invalid_model_output",
                        )
                    return result
                except (RPCError, KeyError, TypeError, IndexError, AttributeError):
                    if attempt + 1 < provider.get("attempts", 1):
                        time.sleep(min(2**attempt, 4))
        raise RPCError("models_unavailable", "All configured model routes failed.", retryable=True)

    @staticmethod
    def _one(provider: dict, params: dict) -> dict:
        if provider["driver"] == "demo":
            return model_handler("model.generate", params)
        key = os.environ.get(provider.get("api_key_env", "RP_MODEL_API_KEY"), "")
        if not key:
            raise RPCError("missing_credential", "A model credential is not configured.")
        messages = [
            *native_messages(params["messages"]),
            {
                "role": "system",
                "content": "Return a JSON object matching this schema: "
                + dumps(params["response_schema"]),
            },
        ]
        payload = {
            **provider.get("parameters", {}),
            "model": provider["model"],
            "messages": messages,
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        if params.get("tools"):
            names = [tool["name"].replace(".", "__") for tool in params["tools"]]
            if len(set(names)) != len(names):
                raise RPCError(
                    "invalid_configuration", "Tool names collide in the provider protocol."
                )
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"].replace(".", "__"),
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in params["tools"]
            ]
            payload["tool_choice"] = "auto"
        base_url = (
            os.environ.get(provider["base_url_env"], "")
            if provider.get("base_url_env")
            else provider.get("base_url", "")
        )
        if not base_url:
            raise RPCError("missing_credential", "A provider endpoint is not configured.")
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + key}
        timeout = provider.get("timeout_seconds", 90)
        started = time.monotonic()
        try:
            response = post_json(url, payload, headers=headers, timeout=timeout)
        except RPCError as exc:
            if exc.code != "stream_unsupported":
                raise
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise RPCError(
                    "provider_unavailable", "The model route timed out.", retryable=True
                ) from exc
            response = post_json(
                url, {**payload, "stream": False}, headers=headers, timeout=remaining
            )
        message = response["choices"][0]["message"]
        calls = []
        tool_names = {
            tool["name"].replace(".", "__"): tool["name"] for tool in params.get("tools", [])
        }
        for call in message.get("tool_calls", []):
            function = call["function"]
            calls.append(
                {
                    "id": call["id"],
                    "name": tool_names.get(function["name"], function["name"]),
                    "arguments": loads(function["arguments"]),
                }
            )
        return (
            {"tool_calls": calls}
            if calls
            else {"output": loads(message["content"]), "tool_calls": []}
        )


def native_messages(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        if message.get("tool_calls"):
            result.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"].replace(".", "__"),
                                "arguments": dumps(call["arguments"]),
                            },
                        }
                        for call in message["tool_calls"]
                    ],
                }
            )
        elif message.get("tool_results"):
            result.extend(
                {
                    "role": "tool",
                    "tool_call_id": item["tool_call_id"],
                    "content": dumps(item["result"]),
                }
                for item in message["tool_results"]
            )
        else:
            result.append({"role": message["role"], "content": message["content"]})
    return result
