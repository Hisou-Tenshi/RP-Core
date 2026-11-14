# SPDX-License-Identifier: AGPL-3.0-only
"""Protocol constants and machine-readable, vendor-neutral contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, SchemaError, ValidationError

from rp_core import PROTOCOL

MAX_FRAME_BYTES = 1_048_576
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 256}
TEXT = {"type": "string", "maxLength": 131072}
OBJECT = {"type": "object"}
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time")
def _date_time(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:[.]\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value
    ):
        return False
    try:
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


class RPCError(Exception):
    """Only explicitly safe messages may cross a public process boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    # Additional optional fields are accepted for additive v1 evolution.
    return {"type": "object", "properties": properties, "required": list(required)}


CONTENT = {
    "type": "array",
    "maxItems": 32,
    "items": {
        "oneOf": [
            obj({"type": {"const": "text"}, "text": TEXT}, ("type", "text")),
            obj(
                {
                    "type": {"const": "attachment"},
                    "reference": IDENTIFIER,
                    "media_type": IDENTIFIER,
                    "caption": TEXT,
                },
                ("type", "reference", "media_type"),
            ),
            obj(
                {
                    "type": {"const": "location"},
                    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                },
                ("type", "latitude", "longitude"),
            ),
        ]
    },
}
EVENT = obj(
    {
        "id": IDENTIFIER,
        "session_id": IDENTIFIER,
        "kind": {"enum": ["message", "perception"]},
        "occurred_at": {"type": "string", "format": "date-time"},
        "actor": obj({"id": IDENTIFIER, "display_name": IDENTIFIER}, ("id",)),
        "content": CONTENT,
        "reply_to": IDENTIFIER,
        "context": OBJECT,
        "extensions": OBJECT,
    },
    ("id", "session_id", "kind", "occurred_at", "content"),
)
MESSAGE = obj(
    {"role": {"enum": ["user", "assistant"]}, "content": TEXT},
    ("role", "content"),
)
TOOL = obj(
    {
        "name": IDENTIFIER,
        "description": TEXT,
        "mode": {"enum": ["read", "deferred"]},
        "input_schema": {"type": ["object", "boolean"]},
    },
    ("name", "description", "mode", "input_schema"),
)
SNAPSHOT = obj(
    {
        "revision": IDENTIFIER,
        "character": obj(
            {"id": IDENTIFIER, "name": IDENTIFIER, "instructions": TEXT},
            ("id", "name", "instructions"),
        ),
        "history": {"type": "array", "items": MESSAGE, "maxItems": 1000},
        "memories": {"type": "array", "items": TEXT, "maxItems": 128},
        "notes": {"type": "array", "items": TEXT, "maxItems": 128},
        "relationship": TEXT,
        "summary": TEXT,
        "tools": {"type": "array", "items": TOOL, "maxItems": 64},
        "policy": obj(
            {
                "two_stage": {"type": "boolean"},
                "allow_silence": {"type": "boolean"},
                "max_tool_rounds": {"type": "integer", "minimum": 0, "maximum": 8},
                "history_limit": {"type": "integer", "minimum": 0, "maximum": 100},
                "max_messages": {"type": "integer", "minimum": 1, "maximum": 20},
                "max_message_chars": {"type": "integer", "minimum": 1, "maximum": 20000},
            }
        ),
        "extensions": OBJECT,
    },
    ("revision", "character"),
)
ACTION = obj(
    {"id": IDENTIFIER, "kind": {"const": "message"}, "text": TEXT, "reply_to": IDENTIFIER},
    ("id", "kind", "text"),
)
RESULT = obj(
    {
        "event_id": IDENTIFIER,
        "session_id": IDENTIFIER,
        "status": {"enum": ["replied", "silent", "maintained"]},
        "actions": {"type": "array", "items": ACTION, "maxItems": 20},
        "effects_queued": {"type": "integer", "minimum": 0},
        "revision": IDENTIFIER,
    },
    ("event_id", "session_id", "status", "actions", "effects_queued"),
)
EFFECT = obj(
    {"id": IDENTIFIER, "name": IDENTIFIER, "arguments": OBJECT},
    ("id", "name", "arguments"),
)
TRANSACTION = obj(
    {
        "event": EVENT,
        "result": RESULT,
        "messages": {"type": "array", "items": MESSAGE, "maxItems": 21},
        "effects": {"type": "array", "items": EFFECT, "maxItems": 128},
        "private": OBJECT,
        "maintenance": OBJECT,
    },
    ("event", "result", "messages", "effects"),
)
COMMIT = obj(
    {
        "session_id": IDENTIFIER,
        "event_id": IDENTIFIER,
        "event_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "expected_revision": IDENTIFIER,
        "transaction": TRANSACTION,
    },
    ("session_id", "event_id", "event_digest", "expected_revision", "transaction"),
)
REQUEST = obj(
    {"protocol": {"const": PROTOCOL}, "id": IDENTIFIER, "method": IDENTIFIER, "params": OBJECT},
    ("protocol", "id", "method", "params"),
)
ERROR = obj(
    {"code": IDENTIFIER, "message": TEXT, "retryable": {"type": "boolean"}},
    ("code", "message", "retryable"),
)
RESPONSE = {
    "oneOf": [
        obj(
            {
                "protocol": {"const": PROTOCOL},
                "id": IDENTIFIER,
                "ok": {"const": True},
                "result": OBJECT,
            },
            ("protocol", "id", "ok", "result"),
        ),
        obj(
            {
                "protocol": {"const": PROTOCOL},
                "id": {"type": ["string", "null"]},
                "ok": {"const": False},
                "error": ERROR,
            },
            ("protocol", "id", "ok", "error"),
        ),
    ]
}
MODEL = obj(
    {
        "output": {"type": ["object", "null"]},
        "tool_calls": {
            "type": "array",
            "maxItems": 16,
            "items": obj(
                {"id": IDENTIFIER, "name": IDENTIFIER, "arguments": OBJECT},
                ("id", "name", "arguments"),
            ),
        },
    }
)
MANIFEST = obj(
    {
        "protocol": {"const": PROTOCOL},
        "adapter": obj(
            {
                "command": {"type": "array", "minItems": 1, "maxItems": 32, "items": IDENTIFIER},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
            },
            ("command",),
        ),
        "max_events": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ("protocol", "adapter"),
)
RECEIPT = {**RESULT, "required": [*RESULT["required"], "revision"]}
STATE_LOAD_REQUEST = obj(
    {
        "session_id": IDENTIFIER,
        "event_id": IDENTIFIER,
        "event_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    ("session_id", "event_id", "event_digest"),
)
STATE_LOAD_RESULT = {
    "oneOf": [obj({"snapshot": SNAPSHOT}, ("snapshot",)), obj({"receipt": RECEIPT}, ("receipt",))]
}
COMMIT_RESULT = {
    "oneOf": [
        obj(
            {"status": {"enum": ["committed", "duplicate"]}, "result": RECEIPT},
            ("status", "result"),
        ),
        obj({"status": {"const": "conflict"}}, ("status",)),
    ]
}
MODEL_REQUEST = obj(
    {
        "purpose": IDENTIFIER,
        "messages": {
            "type": "array",
            "maxItems": 256,
            "items": obj(
                {"role": {"enum": ["system", "user", "assistant"]}, "content": TEXT},
                ("role", "content"),
            ),
        },
        "response_schema": OBJECT,
        "tools": {"type": "array", "maxItems": 64, "items": TOOL},
    },
    ("purpose", "messages", "response_schema", "tools"),
)
BRIDGE_DESCRIPTION = obj(
    {
        "protocol": {"const": PROTOCOL},
        "methods": {"type": "array", "items": IDENTIFIER, "uniqueItems": True},
    },
    ("protocol", "methods"),
)
SCHEMAS = {
    "receipt": RECEIPT,
    "state-load-request": STATE_LOAD_REQUEST,
    "state-load-result": STATE_LOAD_RESULT,
    "commit-result": COMMIT_RESULT,
    "model-request": MODEL_REQUEST,
    "bridge-description": BRIDGE_DESCRIPTION,
    "request": REQUEST,
    "response": RESPONSE,
    "event": EVENT,
    "snapshot": SNAPSHOT,
    "result": RESULT,
    "transaction": TRANSACTION,
    "commit": COMMIT,
    "model": MODEL,
    "manifest": MANIFEST,
}


def validate(value: Any, schema: dict | bool, *, code: str = "invalid_params") -> None:
    try:
        Draft202012Validator(schema, format_checker=FORMAT_CHECKER).validate(value)
    except (ValidationError, SchemaError, TypeError, ValueError, RecursionError):
        raise RPCError(code, "Payload does not satisfy the protocol contract.") from None


def check_tool_schema(schema: dict | bool) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(item, str) or not item.startswith("#")
                ):
                    raise RPCError("invalid_adapter", "External schema references are unsupported.")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise RPCError("invalid_adapter", "Adapter supplied an invalid tool schema.") from None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def loads(value: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite number")

    def number(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite number")
        return parsed

    try:
        result = json.loads(
            value, object_pairs_hook=pairs, parse_constant=constant, parse_float=number
        )
        json.dumps(result, ensure_ascii=False).encode("utf-8")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise RPCError("invalid_json", "Expected one finite UTF-8 JSON object per line.") from None


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def response(
    request_id: str | None, *, result: dict | None = None, error: RPCError | None = None
) -> dict:
    base = {"protocol": PROTOCOL, "id": request_id, "ok": error is None}
    return {**base, "error": error.payload()} if error else {**base, "result": result or {}}
