# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen v1 fixtures and a separately implemented adapter contract."""

from __future__ import annotations

from pathlib import Path

from rp_core.engine import Engine
from rp_core.protocol import SCHEMAS, dumps, loads, validate

FIXTURES = Path(__file__).parent / "fixtures"


def test_frozen_v1_contract_fixtures_remain_accepted():
    request = loads((FIXTURES / "rp1-message.json").read_bytes())
    validate(request, SCHEMAS["request"])
    validate(request["params"]["event"], SCHEMAS["event"])
    validate(loads((FIXTURES / "rp1-silent-receipt.json").read_bytes()), SCHEMAS["receipt"])


class IndependentAdapter:
    """No SQLite/reference-adapter imports or dependence on its file structure."""

    def __init__(self):
        self.calls = []
        self.receipt = None

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "bridge.describe":
            return {"protocol": "rp/1", "methods": ["state.load", "state.commit", "model.generate"]}
        if method == "state.load":
            if self.receipt:
                return {"receipt": self.receipt}
            return {
                "snapshot": {
                    "revision": "opaque-old",
                    "character": {
                        "id": "neutral",
                        "name": "Neutral",
                        "instructions": "A patient navigator.",
                    },
                    "policy": {"two_stage": False},
                    "extensions": {"private-storage": "not-filesystem-based"},
                }
            }
        if method == "model.generate":
            validate(params, SCHEMAS["model-request"])
            return {"output": {"decision": "reply", "messages": ["A synthetic response."]}}
        if method == "state.commit":
            validate(params, SCHEMAS["commit"])
            assert params["expected_revision"] == "opaque-old"
            self.receipt = {**params["transaction"]["result"], "revision": "opaque-new"}
            return {"status": "committed", "result": self.receipt}
        raise AssertionError(method)


def test_independent_private_implementation_and_opaque_revisions():
    request = loads((FIXTURES / "rp1-message.json").read_bytes())
    adapter = IndependentAdapter()
    core = Engine(adapter)
    result = core.handle(request["method"], request["params"])
    assert result["revision"] == "opaque-new"
    assert result["actions"][0]["text"] == "A synthetic response."
    assert core.handle(request["method"], request["params"])["replayed"]
    assert sum(method == "model.generate" for method, _ in adapter.calls) == 1
    assert "private-storage" not in dumps(result)


def test_private_repo_can_add_a_new_deferred_tool_without_core_changes():
    class ExtendedAdapter(IndependentAdapter):
        def __init__(self):
            super().__init__()
            self.plans = 0
            self.effects = []

        def call(self, method, params):
            if method == "state.load":
                loaded = super().call(method, params)
                loaded["snapshot"]["policy"] = {"two_stage": True}
                loaded["snapshot"]["tools"] = [
                    {
                        "name": "example.private_platform_action",
                        "description": "A privately implemented extension.",
                        "mode": "deferred",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ]
                return loaded
            if method == "model.generate" and params["purpose"] == "reply.plan":
                self.plans += 1
                if self.plans == 1:
                    return {
                        "tool_calls": [
                            {
                                "id": "extension-1",
                                "name": "example.private_platform_action",
                                "arguments": {"value": "synthetic extension value"},
                            }
                        ]
                    }
                return {
                    "output": {
                        "decision": "reply",
                        "intent": "Acknowledge the privately queued action.",
                    }
                }
            if method == "state.commit":
                self.effects = params["transaction"]["effects"]
            return super().call(method, params)

    request = loads((FIXTURES / "rp1-message.json").read_bytes())
    adapter = ExtendedAdapter()
    result = Engine(adapter).handle(request["method"], request["params"])
    assert result["effects_queued"] == 1
    assert adapter.effects[0]["name"] == "example.private_platform_action"
    assert adapter.effects[0]["arguments"] == {"value": "synthetic extension value"}
