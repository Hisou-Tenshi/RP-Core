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
            raw = result.read(MAX_FRAME_BYTES + 1)
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
                except (RPCError, KeyError, TypeError, IndexError):
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
            "stream": False,
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
        response = post_json(
            url,
            payload,
            headers={"Authorization": "Bearer " + key},
            timeout=provider.get("timeout_seconds", 90),
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
