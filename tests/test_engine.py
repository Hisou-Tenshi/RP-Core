# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from copy import deepcopy

import pytest

from rp_core.engine import Engine
from rp_core.protocol import RPCError, dumps, loads


def payload(adapter, sid="demo-session"):
    with adapter.connection() as db:
        return adapter._state(db, sid)[1]


def test_complete_reply_and_replay_do_not_regenerate(adapter, event):
    engine = Engine(adapter)
    first = engine.handle("event.process", {"event": event})
    assert first["status"] == "replied" and first["revision"] == "1"
    assert len(first["actions"]) == 2
    adapter.generate = lambda _: pytest.fail("replay called the model")
    second = engine.handle("event.process", {"event": event})
    assert second["replayed"] is True
    assert second["actions"] == first["actions"]
    assert len(payload(adapter)["history"]) == 3
    with adapter.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2


def test_changed_input_cannot_reuse_event_id(adapter, event):
    engine = Engine(adapter)
    engine.handle("event.process", {"event": event})
    event["content"][0]["text"] = "different synthetic input"
    with pytest.raises(RPCError, match="different input"):
        engine.handle("event.process", {"event": event})


def test_character_is_loaded_from_private_adapter(adapter, instance, event):
    (instance / "character.json").write_text(
        dumps(
            {
                "id": "other",
                "name": "Another Character",
                "instructions": "A quiet librarian on the moon.",
            }
        )
    )
    seen = []
    original = adapter.generate
    adapter.generate = lambda p: seen.append(deepcopy(p)) or original(p)
    Engine(adapter).handle("event.process", {"event": event})
    assert "quiet librarian" in seen[0]["messages"][0]["content"]
    assert "天子" not in seen[0]["messages"][0]["content"]


@pytest.mark.parametrize("kind", ["message", "perception"])
def test_silence_is_success_and_keeps_deferred_effects(adapter, event, kind):
    event["kind"] = kind
    outputs = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "n1",
                        "name": "note.append",
                        "arguments": {"text": "synthetic private note"},
                    }
                ]
            },
            {"output": {"decision": "silent", "intent": "No message needed."}},
        ]
    )
    adapter.generate = lambda _: next(outputs)
    result = Engine(adapter).handle("event.process", {"event": event})
    assert result["status"] == "silent" and result["actions"] == []
    assert result["effects_queued"] == 1
    state = payload(adapter)
    assert state["notes"] == ["synthetic private note"]
    assert len(state["history"]) == (1 if kind == "message" else 0)
    assert "No message needed" not in str(result)


def test_render_failure_does_not_apply_effects_or_receipts(adapter, event):
    outputs = iter(
        [
            {
                "tool_calls": [
                    {"id": "n1", "name": "note.append", "arguments": {"text": "must not persist"}}
                ]
            },
            {"output": {"decision": "reply", "intent": "Respond."}},
            {"output": {"decision": "reply", "messages": ["<analysis>private content</analysis>"]}},
        ]
    )
    adapter.generate = lambda _: next(outputs)
    with pytest.raises(RPCError) as error:
        Engine(adapter).handle("event.process", {"event": event})
    assert error.value.code == "unsafe_model_output"
    with adapter.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert payload(adapter)["notes"] == []


@pytest.mark.parametrize(
    "output",
    [
        {"decision": "reply", "messages": []},
        {"decision": "reply", "messages": ["   "]},
        {"decision": "silent", "messages": ["contradiction"]},
        {"decision": "reply", "messages": ["Error: upstream unavailable"]},
        {"decision": "reply", "messages": ["x" * 4001]},
        {"decision": "reply", "messages": ["x"] * 9},
        {"decision": "reply"},
    ],
)
def test_bad_render_is_not_committed(adapter, event, output):
    adapter.config["policy"] = {"two_stage": False}
    adapter.generate = lambda _: {"output": output}
    with pytest.raises(RPCError):
        Engine(adapter).handle("event.process", {"event": event})
    assert payload(adapter)["history"] == []


def test_tool_allowlist_and_argument_validation(adapter, event):
    for name, args, code in [
        ("private.delete_all", {}, "tool_not_allowed"),
        ("note.append", {"text": 3}, "invalid_tool_arguments"),
    ]:
        adapter.generate = lambda _, name=name, args=args: {
            "tool_calls": [{"id": "bad", "name": name, "arguments": args}]
        }
        with pytest.raises(RPCError) as error:
            Engine(adapter).handle("event.process", {"event": event})
        assert error.value.code == code


def test_read_tool_round_and_deferred_effect_share_one_commit(adapter, event):
    outputs = iter(
        [
            {
                "tool_calls": [
                    {"id": "r1", "name": "memory.search", "arguments": {"query": "walk"}},
                    {
                        "id": "w1",
                        "name": "memory.append",
                        "arguments": {"text": "synthetic walk invitation"},
                    },
                ]
            },
            {"output": {"decision": "reply", "intent": "Accept the walk."}},
            {"output": {"decision": "reply", "messages": ["Let's walk."]}},
        ]
    )
    seen = []

    def generate(params):
        seen.append(deepcopy(params))
        return next(outputs)

    adapter.generate = generate
    result = Engine(adapter).handle("event.process", {"event": event})
    assert result["effects_queued"] == 1
    assert payload(adapter)["memories"] == ["synthetic walk invitation"]
    assert "tool_results" in seen[1]["messages"][-1]["content"]
    assert any("tool_results" in message["content"] for message in seen[2]["messages"])


def test_tool_round_limit(adapter, event):
    adapter.config["policy"] = {"max_tool_rounds": 0}
    adapter.generate = lambda _: {
        "tool_calls": [{"id": "r", "name": "memory.search", "arguments": {"query": "walk"}}]
    }
    with pytest.raises(RPCError) as error:
        Engine(adapter).handle("event.process", {"event": event})
    assert error.value.code == "tool_limit"


def test_state_conflict_is_retryable_and_not_delivered(adapter, event):
    adapter.commit_state = lambda _: {"status": "conflict"}
    with pytest.raises(RPCError) as error:
        Engine(adapter).handle("event.process", {"event": event})
    assert error.value.code == "state_conflict" and error.value.retryable
    assert payload(adapter)["history"] == []


@pytest.mark.parametrize("job", ["summarize", "compact", "reflect"])
def test_maintenance_and_relationship_approval_boundary(adapter, event, job):
    engine = Engine(adapter)
    engine.handle("event.process", {"event": event})
    event["id"] = "maintenance-" + job
    result = engine.handle("maintenance.run", {"event": event, "job": job, "keep_last": 1})
    assert result["status"] == "maintained" and result["actions"] == []
    state = payload(adapter)
    if job == "summarize":
        assert state["summary"] and state["memories"]
    elif job == "compact":
        assert len(state["history"]) == 1
        with adapter.connection() as db:
            assert len(loads(db.execute("SELECT payload FROM archives").fetchone()[0])) == 2
    else:
        assert state["relationship"] == ""
        assert len(state["relationship_proposals"]) == 1


def test_unknown_operation_does_not_call_adapter(adapter):
    adapter.call = lambda *_: pytest.fail("unknown method reached adapter")
    with pytest.raises(RPCError) as error:
        Engine(adapter).handle("unknown", {})
    assert error.value.code == "method_not_found"


@pytest.mark.parametrize("version", ["rp/2", None, 1])
def test_adapter_protocol_negotiation(adapter, version):
    adapter.describe = lambda: {"protocol": version, "methods": []}
    with pytest.raises(RPCError) as error:
        Engine(adapter).negotiate()
    assert error.value.code == "unsupported_protocol"


def test_optional_fields_are_forward_compatible(adapter, event):
    event["future_optional_metadata"] = {"example": True}
    result = Engine(adapter).handle("event.process", {"event": event, "future_option": True})
    assert result["status"] == "replied"


def test_compaction_never_discards_an_unseen_history_suffix(adapter, event):
    history = [{"role": "user", "content": f"synthetic-{index}"} for index in range(250)]
    with adapter.connection() as db:
        _, state = adapter._state(db, event["session_id"])
        state["history"] = history
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (event["session_id"], 1, dumps(state)))
    seen = []
    original = adapter.generate
    adapter.generate = lambda p: seen.append(deepcopy(p)) or original(p)
    Engine(adapter).handle("maintenance.run", {"event": event, "job": "compact", "keep_last": 1})
    assert payload(adapter)["history"] == history[199:]
    supplied = [message["content"] for message in seen[0]["messages"]]
    assert "synthetic-0" in supplied and "synthetic-199" in supplied
    assert "synthetic-249" not in supplied
    with adapter.connection() as db:
        archived = loads(db.execute("SELECT payload FROM archives").fetchone()[0])
        assert archived == history[:199]
