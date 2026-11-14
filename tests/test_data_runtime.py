# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from rp_core.data_init import initialize_data
from rp_core.demo import EVENT
from rp_core.engine import Engine
from rp_core.legacy import TenshiData, write_json
from rp_core.protocol import RPCError, dumps, loads
from rp_core.providers import ModelRouter
from rp_core.runtime import PublicRuntime, run_data
from rp_core.settings import load_io, load_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "character"
    initialize_data(root, demo_event=True)
    return root


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "public-runtime.json"
    write_json(path, load_runtime(ROOT / "config/runtime/offline.json"))
    return path


def test_data_only_initialization_and_no_private_code_execution(data, config):
    assert not list(data.rglob("*.py"))
    assert not (data / "adapter.json").exists()
    (data / "adapter.py").write_text("raise RuntimeError('must never execute private code')")
    assert run_data(data, config)["processed"] == 1
    assert run_data(data, config)["processed"] == 0
    assert len((data / "state/outbox.jsonl").read_text().splitlines()) == 2


def test_cli_initialization_and_public_runtime(tmp_path):
    data = tmp_path / "private"
    subprocess.run(
        [sys.executable, "-m", "rp_core", "init-instance", "--path", str(data), "--demo-event"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rp_core",
            "run",
            "--data-root",
            str(data),
            "--config",
            str(ROOT / "config/runtime/offline.json"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert loads(result.stdout)["processed"] == 1
    assert not list(data.rglob("*.py"))


@pytest.mark.parametrize("binding", ["../outside", "/absolute/outside", ".git/config"])
def test_private_io_cannot_escape_data_directory(data, binding):
    write_json(
        data / "io.json", {"schema_version": 1, "driver": "native", "files": {"character": binding}}
    )
    with pytest.raises(RPCError):
        load_io(data)


def test_private_io_cannot_select_executable_adapter(data):
    write_json(
        data / "io.json", {"schema_version": 1, "driver": "native", "command": ["private.py"]}
    )
    with pytest.raises(RPCError):
        load_io(data)


def test_two_characters_share_public_model_and_policy_implementation(tmp_path, config, monkeypatch):
    seen = []

    def generate(_, params):
        seen.append(params)
        if params["purpose"] == "reply.plan":
            return {"output": {"decision": "reply", "intent": "Respond briefly."}}
        return {"output": {"decision": "reply", "messages": ["synthetic shared implementation"]}}

    monkeypatch.setattr(ModelRouter, "generate", generate)
    for name in ("navigator", "librarian"):
        root = tmp_path / name
        initialize_data(root, demo_event=True)
        write_json(
            root / "character.json",
            {"id": name, "name": name, "instructions": "You are a fictional " + name},
        )
        run_data(root, config)
    assert len(seen) == 4
    assert "navigator" in seen[0]["messages"][0]["content"]
    assert "librarian" in seen[2]["messages"][0]["content"]


@pytest.fixture
def legacy(data):
    write_json(data / "io.json", {"schema_version": 1, "driver": "tenshi-v1"})
    old_message = {
        "msg_id": 10,
        "time": 1,
        "user_query": "synthetic old question",
        "bot_answer": "synthetic old answer",
        "custom_legacy_flag": {"preserve": True},
    }
    write_json(
        data / "data/tenshi_log.json",
        [{"conversation_id": EVENT["session_id"], "chat_history": [old_message]}],
    )
    write_json(data / "Tenshi.json", [{"id": "2000-01-01", "text": "synthetic old diary"}])
    write_json(
        data / "data/knowledge_base.json", {"user_profile": {"fact": "synthetic old profile"}}
    )
    with sqlite3.connect(data / "data/tenshi_memory.db") as db:
        db.executescript(
            "CREATE TABLE memory_items(id TEXT PRIMARY KEY,content TEXT,created_at REAL,type TEXT,importance_score REAL,metadata_json TEXT,content_hash TEXT UNIQUE); CREATE TABLE memory_vectors(id TEXT PRIMARY KEY,embedding BLOB); CREATE TABLE embedding_cache(text_hash TEXT PRIMARY KEY,embedding BLOB,updated_at REAL,embedding_model TEXT);"
        )
        db.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?)",
            ("old", "synthetic old memory", 1, "event", 0.5, "{}", "old-hash"),
        )
        db.execute(
            "INSERT INTO memory_vectors VALUES (?,?)",
            ("old", struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)),
        )
        db.execute(
            "INSERT INTO embedding_cache VALUES (?,?,?,?)",
            ("old", b"original-cache", 1, "synthetic-embedding"),
        )
    return data, old_message


def test_old_data_is_read_without_migration_or_reembedding(legacy):
    root, _ = legacy
    database = root / "data/tenshi_memory.db"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    driver = TenshiData(root, load_io(root))
    assert driver.history(EVENT["session_id"])[0]["content"] == "synthetic old question"
    assert driver.notes() == ["synthetic old diary"]
    assert driver.memories("old") == ["synthetic old memory"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not (root / ".rp").exists()


def test_legacy_writeback_preserves_rows_vectors_cache_and_old_fields(legacy, config, monkeypatch):
    root, old_message = legacy
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "m",
                        "name": "memory.append",
                        "arguments": {"text": "synthetic new memory"},
                    },
                    {
                        "id": "n",
                        "name": "note.append",
                        "arguments": {"text": "synthetic new diary"},
                    },
                ]
            },
            {"output": {"decision": "reply", "intent": "Reply."}},
            {"output": {"decision": "reply", "messages": ["synthetic new answer"]}},
        ]
    )
    monkeypatch.setattr(ModelRouter, "generate", lambda *_: next(responses))
    assert run_data(root, config)["processed"] == 1
    assert run_data(root, config)["processed"] == 0
    messages = loads((root / "data/tenshi_log.json").read_bytes())[0]["chat_history"]
    assert len(messages) == 2 and messages[-1] == old_message
    assert messages[0]["bot_answer"] == "synthetic new answer"
    with sqlite3.connect(root / "data/tenshi_memory.db") as db:
        assert db.execute("SELECT embedding FROM memory_vectors WHERE id='old'").fetchone()[
            0
        ] == struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        assert (
            db.execute("SELECT embedding FROM embedding_cache").fetchone()[0] == b"original-cache"
        )
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 2
    assert (root / ".rp/state.sqlite").exists()


def test_public_registry_selects_instances_without_private_paths_in_output(tmp_path):
    spec = importlib.util.spec_from_file_location("registry", ROOT / "scripts/resolve_instances.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = tmp_path / "public/config/instances.json"
    write_json(registry.parent / "runtime/test.json", {})
    write_json(
        registry,
        {
            "schema_version": 1,
            "instances": [
                {
                    "id": name,
                    "repository_variable": "RP_" + name.upper(),
                    "runtime": "config/runtime/test.json",
                }
                for name in ("one", "two")
            ],
        },
    )
    result = module.resolve(registry, variables={"RP_ONE": "owner/private-secret-name"})
    assert [item["id"] for item in result["include"]] == ["one"]
    assert "private-secret-name" not in dumps(result)
    with pytest.raises(ValueError):
        module.resolve(registry, selected="unknown")


def test_legacy_log_marks_delivery_only_after_outbox_acknowledgement(legacy, config):
    root, _ = legacy
    with PublicRuntime(root, load_runtime(config), load_io(root)) as runtime:
        Engine(runtime).handle("event.process", {"event": EVENT})
        runtime.finalize({"phase": "before_delivery"})
        assert (
            loads((root / "data/tenshi_log.json").read_bytes())[0]["chat_history"][0][
                "replied_flag"
            ]
            is False
        )
        runtime.flush_outbox({})
        runtime.finalize({"phase": "after_delivery"})
        assert (
            loads((root / "data/tenshi_log.json").read_bytes())[0]["chat_history"][0][
                "replied_flag"
            ]
            is True
        )


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        },
    )


def test_fork_style_data_only_git_roundtrip(data, config, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(data, "init", "--initial-branch=main")
    git(data, "add", ".")
    git(data, "commit", "-m", "Synthetic character data")
    git(data, "remote", "add", "origin", str(remote))
    git(data, "push", "-u", "origin", "main")
    public = load_runtime(config)
    public["persistence"] = {"driver": "git", "branch": "main"}
    write_json(config, public)
    assert run_data(data, config, require_remote=True)["processed"] == 1
    clone = tmp_path / "fresh-character-checkout"
    git(tmp_path, "clone", str(remote), str(clone))
    assert run_data(clone, config, require_remote=True)["processed"] == 0
    assert not list(clone.rglob("*.py"))
    assert git(clone, "rev-parse", "HEAD").stdout == git(remote, "rev-parse", "main").stdout


def test_inbox_is_ordered_by_event_time_not_opaque_filename(data, config):
    (data / "events.jsonl").unlink()
    later = {**EVENT, "id": "later", "occurred_at": "2026-01-02T00:00:00Z"}
    earlier = {**EVENT, "id": "earlier", "occurred_at": "2026-01-01T00:00:00Z"}
    write_json(data / "inbox/a.json", {"event": later})
    write_json(data / "inbox/z.json", {"event": earlier})
    with PublicRuntime(data, load_runtime(config), load_io(data)) as runtime:
        assert runtime.next_event({})["event"]["id"] == "earlier"


def seed_git(data, remote, tmp_path):
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(data, "init", "--initial-branch=main")
    git(data, "add", ".")
    git(data, "commit", "-m", "Synthetic character data")
    git(data, "remote", "add", "origin", str(remote))
    git(data, "push", "-u", "origin", "main")


def test_public_git_driver_merges_a_concurrent_inbox_append(data, config, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    seed_git(data, remote, tmp_path)
    public = load_runtime(config)
    public["persistence"] = {"driver": "git", "branch": "main"}
    write_json(config, public)
    original = ModelRouter.generate
    appended = []

    def generate(self, params):
        if not appended:
            writer = tmp_path / "ingress-writer"
            git(tmp_path, "clone", str(remote), str(writer))
            write_json(
                writer / "inbox/concurrent.json", {"event": {**EVENT, "id": "concurrent-event"}}
            )
            git(writer, "add", "inbox/")
            git(writer, "commit", "-m", "Queue synthetic event")
            git(writer, "push")
            appended.append(True)
        return original(self, params)

    monkeypatch.setattr(ModelRouter, "generate", generate)
    assert run_data(data, config, require_remote=True)["processed"] == 1
    assert (data / "inbox/concurrent.json").exists()
    assert git(data, "rev-parse", "HEAD").stdout == git(remote, "rev-parse", "main").stdout
    assert run_data(data, config, require_remote=True)["processed"] == 1


def test_git_persistence_cannot_acknowledge_an_ignored_state_database(data, config, tmp_path):
    (data / ".gitignore").write_text("state/\n")
    remote = tmp_path / "remote.git"
    seed_git(data, remote, tmp_path)
    public = load_runtime(config)
    public["persistence"] = {"driver": "git", "branch": "main"}
    write_json(config, public)
    with pytest.raises(RPCError):
        run_data(data, config, require_remote=True)
    assert not (data / "state/outbox.jsonl").exists()
