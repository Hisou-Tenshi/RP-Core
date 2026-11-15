# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from rp_core.cli import run_instance
from rp_core.demo import initialize_instance
from rp_core.engine import Engine
from rp_core.protocol import RPCError, digest, dumps, loads
from rp_core.reference import SQLiteAdapter
from rp_core.transport import JsonlClient


def test_two_real_process_boundaries_and_restart(instance):
    first = run_instance(instance / "instance.json")
    second = run_instance(instance / "instance.json")
    assert first["processed"] == 1 and second["processed"] == 0
    actions = [loads(line) for line in (instance / "state/outbox.jsonl").read_text().splitlines()]
    assert len(actions) == 2 and len({a["id"] for a in actions}) == 2
    with sqlite3.connect(instance / "state/state.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_actions_mode_rejects_local_only_persistence(instance):
    with pytest.raises(RPCError) as error:
        run_instance(instance / "instance.json", require_remote=True)
    assert error.value.code == "persistence_unavailable"
    assert not (instance / "state/outbox.jsonl").exists()


def test_state_and_receipt_rollback_on_unknown_effect(adapter, event):
    params = {
        "session_id": event["session_id"],
        "event_id": event["id"],
        "event_digest": digest(event),
        "expected_revision": "0",
        "transaction": {
            "event": event,
            "result": {
                "session_id": event["session_id"],
                "event_id": event["id"],
                "status": "silent",
                "actions": [],
                "effects_queued": 2,
            },
            "messages": [],
            "effects": [
                {"id": "a", "name": "note.append", "arguments": {"text": "rollback me"}},
                {"id": "b", "name": "not.declared", "arguments": {}},
            ],
        },
    }
    with pytest.raises(RPCError):
        adapter.commit_state(params)
    with adapter.connection() as db:
        assert adapter._state(db, event["session_id"])[1]["notes"] == []
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_stale_transaction_cannot_overwrite_newer_state(adapter, event):
    engine = Engine(adapter)
    identity = {
        "session_id": event["session_id"],
        "event_id": "other",
        "event_digest": digest("other"),
    }
    snapshot = adapter.load_state(identity)["snapshot"]
    transaction = engine._turn(event, snapshot)
    engine.handle("event.process", {"event": event})
    event["id"] = "other"
    transaction["event"] = event
    transaction["result"]["event_id"] = "other"
    result = adapter.commit_state(
        {**identity, "expected_revision": "0", "transaction": transaction}
    )
    assert result == {"status": "conflict"}


def test_schedule_is_durable_and_consumed_once(adapter, event):
    outputs = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "s1",
                        "name": "schedule.upsert",
                        "arguments": {
                            "id": "reminder",
                            "due_at": "2000-01-01T00:00:00Z",
                            "reason": "synthetic scheduled event",
                        },
                    }
                ]
            },
            {"output": {"decision": "silent", "intent": "Schedule only."}},
        ]
    )
    original = adapter.generate
    adapter.generate = lambda _: next(outputs)
    Engine(adapter).handle("event.process", {"event": event})
    item = adapter.next_event({})
    assert item["event"]["kind"] == "perception"
    adapter.generate = original
    Engine(adapter).handle("event.process", item)
    assert adapter.next_event({}) == {"event": None}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


def test_private_git_persistence_with_bare_local_remote(tmp_path):
    root, remote = tmp_path / "private", tmp_path / "remote.git"
    initialize_instance(root)
    config = loads((root / "adapter.json").read_bytes())
    (root / "adapter.json").write_text(dumps({**config, "git_sync": True}))
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(root, "init", "--initial-branch=main")
    git(root, "add", ".")
    git(root, "commit", "-m", "Synthetic instance")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    assert run_instance(root / "instance.json", require_remote=True)["processed"] == 1
    assert git(root, "rev-parse", "HEAD").stdout == git(remote, "rev-parse", "main").stdout
    tracked = git(root, "ls-files").stdout.decode()
    assert "state/state.sqlite" in tracked and "state/outbox.jsonl" in tracked
    assert git(root, "status", "--porcelain").stdout == b""


def test_git_sync_cannot_accidentally_commit_to_public_parent(instance):
    git(instance.parent, "init", "--initial-branch=main")
    adapter = SQLiteAdapter(instance)
    adapter.config["git_sync"] = True
    with pytest.raises(RPCError, match="separate private"):
        adapter.finalize({})
    adapter.close()


def test_jsonl_client_redacts_private_error_and_stderr(tmp_path, capfd):
    script = tmp_path / "adapter.py"
    script.write_text(
        "from rp_core.cli import serve_requests\nfrom rp_core.protocol import RPCError\nimport sys\nprint('private-stderr-canary', file=sys.stderr)\ndef fail(*_):\n    raise RPCError('private-code-canary', 'private-error-canary')\nserve_requests(fail)\n"
    )
    with JsonlClient([sys.executable, str(script)], timeout=2) as client:
        with pytest.raises(RPCError) as error:
            client.call("test", {})
        assert error.value.code == "adapter_error"
        assert "canary" not in str(error.value)
    captured = capfd.readouterr()
    assert "canary" not in captured.out + captured.err


def test_jsonl_client_timeout_kills_adapter():
    with JsonlClient([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.05) as client:
        with pytest.raises(RPCError) as error:
            client.call("test", {})
        assert error.value.code == "adapter_timeout"
        assert client.process.poll() is not None


def test_wrong_response_id_fails_closed(tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text(
        "import sys,json\nsys.stdin.readline()\nprint(json.dumps({'protocol':'rp/1','id':'wrong','ok':True,'result':{}}),flush=True)\n"
    )
    with JsonlClient([sys.executable, str(script)], timeout=2) as client:
        with pytest.raises(RPCError) as error:
            client.call("test", {})
        assert error.value.code == "invalid_adapter_response" and client.closed


def test_adapter_that_never_reads_large_request_times_out():
    with JsonlClient([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.05) as client:
        with pytest.raises(RPCError) as error:
            client.call("test", {"synthetic_padding": "x" * 200000})
        assert error.value.code == "adapter_timeout"
        assert client.process.poll() is not None


def test_fresh_example_never_overwrites_existing_directory(instance):
    with pytest.raises(RPCError):
        initialize_instance(instance)


def test_generated_instance_has_no_machine_specific_python_path(instance):
    command = loads((instance / "adapter.json").read_bytes())["model_command"]
    assert command[0] == "{python}"


def test_workflow_contracts_and_pinned_actions():
    root = Path(__file__).resolve().parents[1]
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            if job.get("permissions"):
                assert job["permissions"] == {"contents": "write"}
                if path.name == "keepalive.yml":
                    assert "vars.RP_LIVE_ENABLED == 'true'" in job["if"]
                    assert ".github/activity.json" in path.read_text()
                    assert "DATA_REPOSITORY" not in path.read_text()
                else:
                    assert path.name == "provider-health.yml"
                    assert job["needs"] == "plan"
                    assert "has_instances" in job["if"]
                    assert "default_branch" in workflow["jobs"]["plan"]["if"]
                    assert 'git add -- "health/' in path.read_text()
                    assert "repository:" not in path.read_text()
            for step in job.get("steps", []):
                if "uses" in step:
                    revision = step["uses"].split("@")[1]
                    assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
    runtime = (root / ".github/workflows/instance.yml").read_text()
    assert "--require-remote-persistence" in runtime
    assert "actions/upload-artifact" not in runtime
    assert "pull_request" not in runtime
