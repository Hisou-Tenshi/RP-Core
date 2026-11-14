# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import io
import json

import pytest
from jsonschema import Draft202012Validator

from rp_core.cli import serve_requests
from rp_core.protocol import (
    MAX_FRAME_BYTES,
    SCHEMAS,
    RPCError,
    check_tool_schema,
    dumps,
    loads,
    validate,
)


def request(method="capabilities", **extra):
    return {"protocol": "rp/1", "id": "test", "method": method, "params": {}, **extra}


@pytest.mark.parametrize("schema", SCHEMAS.values())
def test_exported_schemas_are_valid(schema):
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("raw", ["{", '{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', b"\xff"])
def test_invalid_json_and_nonfinite_numbers_rejected(raw):
    with pytest.raises(RPCError) as error:
        loads(raw)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(occurred_at="yesterday"),
        lambda e: e.update(kind="unknown"),
        lambda e: e.update(id=""),
        lambda e: e.update(content=[{"type": "location", "latitude": 100, "longitude": 1}]),
    ],
)
def test_event_validation(event, mutate):
    mutate(event)
    with pytest.raises(RPCError):
        validate(event, SCHEMAS["event"])


def test_rpc_recovers_after_bad_frame_without_logging_payload():
    canary = "synthetic-sensitive-canary"
    source = io.BytesIO(
        b"{not-json " + canary.encode() + b"\n" + (dumps(request()) + "\n").encode()
    )
    sink = io.BytesIO()
    serve_requests(lambda *_: {"ready": True}, source, sink)
    raw = sink.getvalue()
    assert canary.encode() not in raw
    replies = [loads(line) for line in raw.splitlines()]
    assert replies[0]["error"]["code"] == "invalid_json"
    assert replies[1]["result"] == {"ready": True}


def test_large_frame_is_drained_before_next_request():
    source = io.BytesIO(b"x" * (MAX_FRAME_BYTES + 2) + b"\n" + (dumps(request()) + "\n").encode())
    sink = io.BytesIO()
    serve_requests(lambda *_: {}, source, sink)
    replies = [loads(line) for line in sink.getvalue().splitlines()]
    assert len(replies) == 2
    assert replies[0]["error"]["code"] == "frame_too_large" and replies[1]["ok"]


def test_unhandled_exception_is_redacted():
    def broken(*_):
        raise RuntimeError("synthetic secret and private message")

    source = io.BytesIO((dumps(request()) + "\n").encode())
    sink = io.BytesIO()
    serve_requests(broken, source, sink)
    assert b"synthetic secret" not in sink.getvalue()
    assert loads(sink.getvalue())["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/private"},
        {"properties": {"x": {"$dynamicRef": "file:///private"}}},
    ],
)
def test_tool_schemas_cannot_resolve_remote_or_file_references(schema):
    with pytest.raises(RPCError):
        check_tool_schema(schema)


def test_local_schema_references_supported():
    schema = {
        "$defs": {"text": {"type": "string"}},
        "type": "object",
        "properties": {"text": {"$ref": "#/$defs/text"}},
    }
    check_tool_schema(schema)
    validate({"text": "synthetic"}, schema)


def test_wrong_major_version_has_explicit_error():
    sink = io.BytesIO()
    serve_requests(
        lambda *_: {}, io.BytesIO((json.dumps(request(protocol="rp/2")) + "\n").encode()), sink
    )
    assert loads(sink.getvalue())["error"]["code"] == "unsupported_protocol"


@pytest.mark.parametrize("raw", ['{"x":1e999}', '{"x":-1e999}'])
def test_overflow_numbers_are_not_finite_json(raw):
    with pytest.raises(RPCError) as error:
        loads(raw)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize("timestamp", ["2026-01-01T12:00:00", "2026-02-30T12:00:00Z", "yesterday"])
def test_timestamp_requires_valid_calendar_date_and_timezone(event, timestamp):
    event["occurred_at"] = timestamp
    with pytest.raises(RPCError):
        validate(event, SCHEMAS["event"])
