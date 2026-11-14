# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from rp_core.data_init import initialize_data
from rp_core.legacy import write_json
from rp_core.protocol import RPCError, dumps, loads
from rp_core.providers import ModelRouter, post_json
from rp_core.runtime import run_data
from rp_core.settings import load_runtime
from rp_core.telegram import normalize

ROOT = Path(__file__).resolve().parents[1]
UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 2,
        "date": 1767225600,
        "from": {"id": 123, "first_name": "Synthetic"},
        "chat": {"id": 123},
        "text": "synthetic message",
    },
}
IDENTITY = {"allowed_user_ids": [123], "telegram": {"allowed_chat_ids": [123]}}


@pytest.fixture
def api():
    requests, responses = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                {
                    "path": self.path,
                    "payload": loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            status, body = responses.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(dumps(body).encode())

        def log_message(self, *_):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port), requests, responses
    server.shutdown()
    thread.join()
    server.server_close()


def test_public_provider_network_protocol_and_fallback(api, monkeypatch):
    base, requests, responses = api
    monkeypatch.setenv("RP_MODEL_API_KEY", "synthetic-placeholder")
    responses.extend(
        [
            (500, {"error": "synthetic-sensitive-canary"}),
            (
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": dumps({"decision": "silent", "intent": "No reply."})
                            }
                        }
                    ]
                },
            ),
        ]
    )
    provider = {
        "driver": "chat-completions",
        "base_url": base,
        "model": "synthetic",
        "api_key_env": "RP_MODEL_API_KEY",
    }
    router = ModelRouter(
        {"providers": {"one": provider, "two": provider}, "routes": {"default": ["one", "two"]}}
    )
    result = router.generate(
        {
            "purpose": "reply.plan",
            "messages": [{"role": "user", "content": "synthetic"}],
            "response_schema": {"type": "object"},
            "tools": [],
        }
    )
    assert result["output"]["decision"] == "silent" and len(requests) == 2
    assert requests[0]["payload"]["model"] == "synthetic"


def test_native_tool_history_is_mapped_without_private_provider_code(api, monkeypatch):
    base, requests, responses = api
    monkeypatch.setenv("RP_MODEL_API_KEY", "synthetic-placeholder")
    responses.append(
        (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "new",
                                    "function": {
                                        "name": "memory__search",
                                        "arguments": '{"query":"synthetic"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    call = {"id": "old", "name": "memory.search", "arguments": {"query": "old"}}
    result = ModelRouter._one(
        {"driver": "chat-completions", "base_url": base, "model": "synthetic"},
        {
            "messages": [
                {"role": "assistant", "content": "compatibility fallback", "tool_calls": [call]},
                {
                    "role": "user",
                    "content": "compatibility fallback",
                    "tool_results": [{"tool_call_id": "old", "result": {"memories": []}}],
                },
            ],
            "response_schema": {},
            "tools": [
                {"name": "memory.search", "description": "read", "input_schema": {"type": "object"}}
            ],
        },
    )
    assert result["tool_calls"][0]["name"] == "memory.search"
    assert requests[0]["payload"]["messages"][1]["role"] == "tool"


def test_polling_model_and_delivery_all_run_from_public_code(api, monkeypatch, tmp_path):
    base, requests, responses = api
    root = tmp_path / "private"
    initialize_data(root)
    write_json(root / "identity.json", IDENTITY)
    config = load_runtime(ROOT / "config/runtime/offline.json")
    config["ingress"] = {"driver": "telegram-poll", "api_base": base}
    config["delivery"] = {"driver": "telegram", "api_base": base}
    public = tmp_path / "public.json"
    write_json(public, config)
    monkeypatch.setenv("RP_TELEGRAM_TOKEN", "synthetic-placeholder")
    responses.extend(
        [
            (200, {"ok": True, "result": [UPDATE]}),
            (200, {"ok": True, "result": {"message_id": 10}}),
            (200, {"ok": True, "result": {"message_id": 11}}),
        ]
    )
    assert run_data(root, public)["processed"] == 1
    assert len(requests) == 3 and requests[0]["path"].endswith("/getUpdates")
    assert all(item["payload"]["chat_id"] == "123" for item in requests[1:])
    assert not list(root.rglob("*.py"))


def test_identity_authorization_is_fail_closed():
    assert normalize(UPDATE, {"allowed_user_ids": []}) is None
    assert (
        normalize(UPDATE, {"allowed_user_ids": [123], "telegram": {"allowed_chat_ids": [999]}})
        is None
    )
    assert normalize(UPDATE, IDENTITY)["session_id"] == "123"


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/data", "http://example.invalid/api", "https://user:password@example.invalid/api"],
)
def test_public_http_driver_rejects_insecure_urls(url):
    with pytest.raises(RPCError):
        post_json(url, {})
