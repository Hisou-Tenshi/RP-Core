# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rp_core import full_runtime as host
from rp_core import full_service as service
from rp_core.data_init import initialize_data
from rp_core.legacy import write_json
from rp_core.persona_export import export_persona
from rp_core.protocol import RPCError
from rp_core.settings import load_runtime


@pytest.fixture
def root(tmp_path):
    initialize_data(tmp_path, driver="tenshi-v1")
    write_json(
        tmp_path / "identity.json",
        {"allowed_user_ids": [1], "telegram": {"chat_id": 1, "allowed_chat_ids": [1]}},
    )
    return tmp_path


def test_full_config_is_public_and_has_maintenance():
    config = load_runtime(Path(__file__).parents[1] / "config/runtime/full.json")
    assert config["engine"] == "full"
    assert set(config["maintenance_seconds"]) == {
        "perception",
        "summary",
        "archive",
        "cleanup",
        "probe",
    }


def test_prepare_keeps_existing_database_and_history(root):
    (root / "data").mkdir()
    database = root / "data/tenshi_memory.db"
    database.write_bytes(b"existing binary data: do not rebuild")
    history = root / "data/tenshi_log.json"
    history.write_text('[{"existing":true}]')
    before = database.read_bytes(), history.read_bytes()
    host.prepare(root)
    assert (database.read_bytes(), history.read_bytes()) == before
    assert not (root / ".rp").exists()


def test_full_layout_refuses_native_or_custom_binding(root):
    write_json(root / "io.json", {"schema_version": 1, "driver": "native"})
    with pytest.raises(RPCError, match="canonical"):
        host.validate_layout(root)
    write_json(
        root / "io.json",
        {"schema_version": 1, "driver": "tenshi-v1", "files": {"memory": "other.db"}},
    )
    with pytest.raises(RPCError, match="canonical"):
        host.validate_layout(root)


def test_view_uses_public_code_without_private_imports(root):
    (root / "src").mkdir()
    (root / "src/tenshi_reply.py").write_text('raise RuntimeError("private code must not run")')
    with host.view(root) as workspace:
        assert (workspace / "src").resolve() == (host.CODE / "src").resolve()
        assert (workspace / "data").resolve() == root / "data"
        assert not (workspace / ".env").exists()
        assert not (workspace / ".git").exists()
        (workspace / "new-notes").write_text('[{"id":"synthetic"}]')
        (workspace / "new-notes").replace(workspace / "Tenshi.json")
    assert json.loads((root / "Tenshi.json").read_text()) == [{"id": "synthetic"}]


def test_symlink_escape_is_rejected(root):
    (root / "logs").symlink_to(root.parent)
    with pytest.raises(RPCError):
        host.validate_layout(root)


def test_calling_aliases_and_schedule_payload(root, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "synthetic-telegram")
    monkeypatch.setenv("TENSHI_BOT_ACTION_TOKEN", "synthetic-repo-token")
    monkeypatch.setenv("GH_REPO", "example/private")
    env = host.environment(
        root,
        root,
        shadow=False,
        payload={
            "file_ids": [{"file_id": "synthetic"}],
            "reply_to": {"text": "quoted"},
            "scheduled_force": True,
            "note": "synthetic schedule",
            "trigger_at_ms": 123,
        },
    )
    assert env["TELEGRAM_TOKEN"] == "synthetic-telegram"  # noqa: S105
    assert env["GITHUB_TOKEN"] == "synthetic-repo-token"  # noqa: S105
    assert env["REPO_NAME"] == "example/private"
    assert json.loads(env["MSG_FILE_IDS"])[0]["file_id"] == "synthetic"
    assert env["PERCEPTION_SCHEDULED_FORCE"] == "true"
    assert env["PERCEPTION_SCHEDULE_NOTE"] == "synthetic schedule"
    assert env["ADMIN_ID"] == "1"


def test_shadow_neutralizes_telegram_and_schedule_sync(root, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "never-use-this-token")
    monkeypatch.setenv("PERCEPTION_SCHEDULE_SYNC_URL", "https://example.invalid")
    env = host.environment(root, root, shadow=True)
    assert env["TELEGRAM_TOKEN"] != "never-use-this-token"  # noqa: S105
    assert not env["PERCEPTION_SCHEDULE_SYNC_URL"]
    assert env["RP_SHADOW"] == "1"


def test_live_default_is_disabled(root, monkeypatch):
    monkeypatch.delenv("RP_ALLOW_LIVE", raising=False)
    with pytest.raises(RPCError, match="cutover"):
        host.run_full(root, "reply")


def test_persona_export_never_executes_private_code(tmp_path):
    source = tmp_path / "persona.py"
    marker = tmp_path / "executed"
    source.write_text(
        'PERSONA_CARD = "Synthetic card"' + chr(10) + f'open({str(marker)!r}, "w").write("unsafe")'
    )
    assert export_persona(source)["instructions"] == "Synthetic card"
    assert not marker.exists()


def test_runtime_logs_redact_keys(root):
    (root / "logs").mkdir()
    log = root / "logs/test.log"
    log.write_text("before synthetic-secret-key after")
    host.sanitize_logs(root, {"MODEL_API_KEY": "synthetic-secret-key"})
    assert log.read_text() == "before [REDACTED] after"


def queue(root, identifier="a", message_id=1):
    write_json(
        root / f"inbox/{identifier}.json",
        {
            "full_task": "reply",
            "legacy_payload": {
                "text": "synthetic message",
                "user_id": 1,
                "chat_id": 1,
                "msg_id": message_id,
                "date": 1,
            },
        },
    )


def test_queue_processes_once_and_orders_numeric_messages(root, monkeypatch):
    monkeypatch.setenv("RP_ALLOW_LIVE", "1")
    queue(root, "a", 10)
    queue(root, "b", 2)
    calls = []
    monkeypatch.setattr(
        service, "run_full", lambda root, task, payload: calls.append(payload["msg_id"])
    )
    config = {"persistence": {"driver": "local"}}
    assert service.run_queue(root, config)["processed"] == 2
    assert calls == [2, 10]
    assert service.run_queue(root, config)["processed"] == 0


def test_queue_failure_requires_reconciliation(root, monkeypatch):
    monkeypatch.setenv("RP_ALLOW_LIVE", "1")
    queue(root)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure after possible delivery")

    monkeypatch.setattr(service, "run_full", fail)
    config = {"persistence": {"driver": "local"}}
    with pytest.raises(RuntimeError):
        service.run_queue(root, config)
    with pytest.raises(RPCError, match="reconcile"):
        service.run_queue(root, config)


def test_unauthorized_event_never_reaches_model(root, monkeypatch):
    monkeypatch.setenv("RP_ALLOW_LIVE", "1")
    queue(root)
    write_json(root / "identity.json", {"allowed_user_ids": []})
    with pytest.raises(RPCError, match="authorize"):
        service.run_queue(root, {"persistence": {"driver": "local"}})


def test_public_source_manifest_matches_published_bytes():
    manifest = json.loads((host.CODE / "source-manifest.json").read_text())
    assert len(manifest["files"]) == 58
    for item in manifest["files"]:
        assert hashlib.sha256((host.CODE / item["path"]).read_bytes()).hexdigest() == item.get(
            "reviewed_sha256", item["published_sha256"]
        )


def test_legacy_cache_refuses_executable_pickle(tmp_path):
    import io

    from rp_core.compat_runtime.src._rp_cache import load

    # GLOBAL builtins.eval must be refused before any reducer is invoked.
    payload = b"cbuiltins" + bytes([10]) + b"eval" + bytes([10]) + b"."
    with pytest.raises(Exception, match="Non-data pickle"):
        load(io.BytesIO(payload))


def test_legacy_numpy_cache_roundtrips_without_reembedding():
    import io
    import pickle

    from rp_core.compat_runtime.src._rp_cache import load

    np = pytest.importorskip("numpy")
    cache = {
        "format": 2,
        "model": "synthetic",
        "dim": 2,
        "embeddings": {"sample": np.array([1, 2], dtype=np.float32)},
    }
    for protocol in (3, 4, 5):
        result = load(io.BytesIO(pickle.dumps(cache, protocol=protocol)))
        assert result["model"] == cache["model"]
        np.testing.assert_array_equal(result["embeddings"]["sample"], cache["embeddings"]["sample"])


def test_persistence_uses_private_recovery_branch_after_main_failure(root, monkeypatch):
    from types import SimpleNamespace

    commands = []

    def fake(command, **kwargs):
        commands.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=str(root).encode())
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout=b"")
        tier = command[command.index("--tier") + 1]
        return SimpleNamespace(returncode=1 if tier == "reply" else 0)

    monkeypatch.setattr(service.subprocess, "run", fake)
    with pytest.raises(RPCError) as error:
        service.persist(root)
    assert error.value.code == "persistence_staged"
    assert commands[-1][commands[-1].index("--tier") + 1] == "edge"


def test_real_shallow_private_git_persistence(root, monkeypatch):
    import subprocess

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    def git(*args, cwd=root):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout

    remote = root.parent / (root.name + "-origin.git")
    git("init", "--bare", str(remote))
    git("init", "-b", "main")
    git("config", "user.name", "Synthetic author")
    git("config", "user.email", "synthetic@example.invalid")
    git("add", ".")
    git("commit", "-m", "Synthetic data")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "main")
    shallow = root.parent / (root.name + "-shallow")
    git("clone", "--depth", "1", "--branch", "main", remote.as_uri(), str(shallow))
    assert git("rev-parse", "--is-shallow-repository", cwd=shallow).strip() == b"true"
    write_json(shallow / ".rp/synthetic.json", {"preserved": True})
    service.persist(shallow)
    stored = git("--git-dir", str(remote), "show", "main:.rp/synthetic.json")
    assert json.loads(stored) == {"preserved": True}


def test_shadow_reply_checks_nested_legacy_history(monkeypatch):
    def fake(root, task, **kwargs):
        write_json(
            root / "data/tenshi_log.json",
            [
                {
                    "conversation_id": "1",
                    "chat_history": [{"msg_id": "1", "bot_answer": "Synthetic reply"}],
                }
            ],
        )
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs/shadow-delivery.jsonl").write_text('{"method":"send_message"}')
        return {"status": "completed"}

    monkeypatch.setattr(host, "run_full", fake)
    result = host.synthetic_shadow(task="reply")
    assert result["status"] == "completed"
    assert result["delivery_captured"] is True
    assert result["real_telegram_calls"] == 0


def test_shadow_diagnostics_never_return_private_log_excerpts(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/process.log").write_text(
        "Synthetic private prompt and credential: keep-private; APITimeoutError"
    )
    result = host.shadow_diagnostics(tmp_path)
    assert result == ["APITimeoutError", "TimeoutError"]
    assert "keep-private" not in json.dumps(result)


def test_shadow_captures_direct_http_streaming_and_blocks_real_dns(tmp_path):
    import os
    import subprocess
    import sys

    pytest.importorskip("telegram")
    trace = tmp_path / "trace.jsonl"
    code = """
import json, requests, socket
r = requests.post('https://api.telegram.org/bot1:synthetic/sendMessage', json={'chat_id':1,'text':'synthetic'})
r.raise_for_status()
assert r.json()['ok'] is True
try:
    socket.getaddrinfo('api.telegram.org',443)
except OSError:
    blocked=True
else:
    blocked=False
print(json.dumps({'blocked':blocked, 'fake_url':r.url.startswith('https://shadow.invalid/')}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "PYTHONPATH": str(host.CODE / "src"),
            "RP_SHADOW": "1",
            "RP_UNIT_TESTS": "1",
            "RP_SHADOW_TRACE": str(trace),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {"blocked": True, "fake_url": True}
    assert json.loads(trace.read_text())["method"] == "sendMessage"


def test_probe_summary_never_exposes_route_keys_or_urls(monkeypatch):
    def fake(root, task, **kwargs):
        write_json(
            root / "data/provider_health.json",
            {
                "providers": {
                    "claude": {
                        "enabled": True,
                        "paths": {
                            "private-route": {
                                "ok": True,
                                "key_masked": "keep-private",
                                "base_url": "keep-private",
                            }
                        },
                    },
                    "private-provider": {"enabled": True, "paths": {}},
                }
            },
        )
        return {"status": "completed"}

    monkeypatch.setattr(host, "run_full", fake)
    result = host.synthetic_shadow(task="probe")
    assert result["providers"] == {
        "claude": {"enabled": True, "available_paths": 1, "checked_paths": 1}
    }
    assert "keep-private" not in json.dumps(result)
    assert "private-provider" not in json.dumps(result)
