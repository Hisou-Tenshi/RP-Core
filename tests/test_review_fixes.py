# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from rp_core import full_runtime as host
from rp_core import full_service as service
from rp_core import maintenance
from rp_core.data_init import initialize_data
from rp_core.legacy import write_json
from rp_core.protocol import RPCError


@pytest.fixture
def data(tmp_path, monkeypatch):
    initialize_data(tmp_path, driver="tenshi-v1")
    write_json(tmp_path / "identity.json", {"allowed_user_ids": [1]})
    monkeypatch.setenv("RP_ALLOW_LIVE", "1")
    return tmp_path


def test_instance_aliases_prefer_selected_repository_and_nonempty_credentials(data, monkeypatch):
    monkeypatch.setenv("RP_DATA_REPOSITORY", "example/selected")
    monkeypatch.setenv("GH_REPO", "example/default")
    monkeypatch.setenv("RP_INSTANCE_TOKEN", "synthetic-instance")
    monkeypatch.setenv("TENSHI_BOT_ACTION_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-existing")
    env = host.environment(data, data, shadow=False)
    assert env["REPO_NAME"] == "example/selected"
    assert env["GITHUB_TOKEN"] == "synthetic-instance"  # noqa: S105
    monkeypatch.setenv("RP_INSTANCE_TOKEN", "")
    monkeypatch.setenv("GH_TOKEN", "")
    assert host.environment(data, data, shadow=False)["GITHUB_TOKEN"] == "synthetic-existing"  # noqa: S105


@pytest.mark.parametrize(
    "identity",
    [
        {"allowed_user_ids": [1, 2]},
        {"allowed_user_ids": [1], "telegram": {"allowed_chat_ids": [2]}},
        {"allowed_user_ids": [1], "telegram": {"chat_id": -99}},
    ],
)
def test_full_identity_rejects_unsupported_shared_sessions(data, identity):
    write_json(data / "identity.json", identity)
    with pytest.raises(RPCError, match="one owner"):
        host.validate_layout(data)


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (None, "delivery_unknown"),
        ("delivery_unknown", "delivery_unknown"),
        ("sent", "sent"),
        ("no_send", "no_send"),
    ],
)
def test_host_checks_explicit_reply_outcome(data, monkeypatch, marker, expected):
    class Process:
        def __init__(self, *args, env, **kwargs):
            if marker:
                Path(env["RP_RESULT_PATH"]).write_text(json.dumps({"status": marker}))

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(host, "dependency_status", lambda: {"synthetic": True})
    monkeypatch.setattr(host.subprocess, "Popen", Process)
    assert host.run_full(data, "reply")["status"] == expected


def test_probe_failure_is_bounded_and_does_not_block_maintenance(data, monkeypatch):
    write_json(data / "inbox/probe.json", {"full_task": "probe"})
    calls = []

    def run(root, task, **kwargs):
        calls.append(task)
        if task == "probe":
            raise RuntimeError("synthetic")
        return {"status": "completed"}

    monkeypatch.setattr(service, "run_full", run)
    config = {"persistence": {"driver": "local"}, "maintenance_seconds": {"cleanup": 60}}
    for _ in range(4):
        service.run_queue(data, config)
    assert calls.count("probe") == 3
    assert calls.count("cleanup") == 1
    receipt = data / ".rp/full-receipts" / (hashlib.sha256(b"probe").hexdigest() + ".json")
    assert json.loads(receipt.read_text())["status"] == "retryable_failure"


def test_maintenance_persists_task_and_ledger_once(data, monkeypatch):
    monkeypatch.setattr(service, "merge_staging", lambda root: None)
    monkeypatch.setattr(service, "run_full", lambda *a, **k: {"status": "completed"})
    saved = []
    monkeypatch.setattr(
        service,
        "persist",
        lambda root, branch: saved.append(json.loads((root / ".rp/maintenance.json").read_text())),
    )
    service.run_queue(
        data, {"persistence": {"driver": "git"}, "maintenance_seconds": {"cleanup": 60}}
    )
    assert len(saved) == 1
    assert "cleanup" in saved[0]


def test_cleanup_keeps_referenced_attachments_and_rotates_logs(data):
    (data / "data").mkdir()
    (data / "downloads").mkdir()
    (data / "logs").mkdir()
    keep, remove = data / "downloads/keep.png", data / "downloads/old.png"
    keep.write_bytes(b"synthetic")
    remove.write_bytes(b"synthetic")
    os.utime(keep, (1, 1))
    os.utime(remove, (1, 1))
    write_json(data / "data/tenshi_log.json", [{"local_files": ["/old-view/downloads/keep.png"]}])
    (data / "logs/full-process.log").write_text("synthetic private log")
    result = maintenance.cleanup(data, now=1_800_000_000)
    assert result == {"rotated": 1, "attachments_removed": 1}
    assert keep.exists() and not remove.exists()
    assert (data / "logs/full-process.log").read_bytes() == b""
    assert list((data / "logs/archive").rglob("*.gz"))


def test_private_backup_contains_consistent_sqlite_snapshot(data, tmp_path):
    (data / "data").mkdir()
    database = data / "data/tenshi_memory.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE synthetic (value)")
        connection.execute("INSERT INTO synthetic VALUES (42)")
        connection.commit()
        bundle = tmp_path / "backup.zip"
        maintenance.build_backup(data, bundle)
    with zipfile.ZipFile(bundle) as archive:
        assert not any(name.endswith(("-wal", "-shm")) for name in archive.namelist())
        extracted = tmp_path / "snapshot.db"
        extracted.write_bytes(archive.read("data/tenshi_memory.db"))
    with sqlite3.connect(extracted) as connection:
        assert connection.execute("SELECT value FROM synthetic").fetchone() == (42,)


def test_backup_refuses_public_destination_before_packaging(data, monkeypatch):
    monkeypatch.setattr(maintenance, "_github", lambda *a, **k: {"private": False})
    monkeypatch.setattr(maintenance, "build_backup", lambda *a: pytest.fail("must not package"))
    with pytest.raises(RPCError, match="must be.*private"):
        maintenance.backup(data, {"REPO_NAME": "example/selected"})


def test_release_backup_verifies_private_upload_and_retains_only_own_tags(data, monkeypatch):
    calls = []

    def github(env, path, *, method="GET", payload=None, **kwargs):
        calls.append((path, method))
        if path == "repos/example/private":
            return {"private": True, "full_name": "example/private"}
        if "/releases/tags/" in path:
            return None
        if method == "POST" and path.endswith("/releases"):
            return {"id": 1, "assets": []}
        if "assets?name=" in path:
            assert zipfile.is_zipfile(__import__("io").BytesIO(payload))
            return {"state": "uploaded"}
        if "?per_page=" in path:
            return [
                {"id": day, "tag_name": f"rp-core-daily-2026-01-{day:02d}"} for day in range(1, 10)
            ] + [{"id": 99, "tag_name": "unrelated-release"}]
        return None

    monkeypatch.setattr(maintenance, "_github", github)
    result = maintenance.backup(data, {"REPO_NAME": "example/private"})
    assert result["status"] == "completed"
    deleted = [path for path, method in calls if method == "DELETE"]
    assert deleted == ["repos/example/private/releases/2", "repos/example/private/releases/1"]


def test_log_redaction_handles_chunk_boundaries_and_new_credentials(data):
    (data / "logs").mkdir()
    log = data / "logs/debug.log"
    log.write_text("x" * 65530 + "synthetic-private-token" + " tail")
    host.sanitize_logs(data, {"MODEL_TOKEN": "synthetic-private-token"})
    assert log.read_text() == "x" * 65530 + "[REDACTED] tail"
    log.write_text("synthetic-second-token")
    host.sanitize_logs(data, {"MODEL_TOKEN": "synthetic-second-token"})
    assert log.read_text() == "[REDACTED]"


def test_ambiguous_history_is_marked_before_next_turn(data, monkeypatch):
    (data / "data").mkdir()
    write_json(
        data / "data/tenshi_log.json",
        [
            {
                "user_id": 1,
                "chat_history": [{"msg_id": "1", "user_query": "synthetic"}],
            }
        ],
    )
    write_json(
        data / "inbox/a.json",
        {
            "full_task": "reply",
            "legacy_payload": {
                "user_id": 1,
                "chat_id": 1,
                "msg_id": "1",
            },
        },
    )
    monkeypatch.setattr(service, "run_full", lambda *a, **k: {"status": "delivery_unknown"})
    result = service.run_queue(data, {"persistence": {"driver": "local"}})
    assert result["quarantined"] == 1
    history = json.loads((data / "data/tenshi_log.json").read_text())
    assert history[0]["chat_history"][0]["rp_delivery_quarantined"] is True


def test_cli_failure_counts_produce_failed_workflow_without_skipping_work(
    data, monkeypatch, capsys
):
    from rp_core import cli, runtime

    monkeypatch.setattr(
        runtime,
        "run_data",
        lambda *a, **k: {
            "engine": "full",
            "processed": 2,
            "quarantined": 1,
            "failed": 0,
        },
    )
    assert cli.main(["run", "--data-root", str(data), "--config", "synthetic.json"]) == 1
    assert json.loads(capsys.readouterr().out)["processed"] == 2
