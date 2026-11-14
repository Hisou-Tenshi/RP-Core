# SPDX-License-Identifier: AGPL-3.0-only
"""Host the complete compatibility runtime using public code and private data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from rp_core.data_init import initialize_data
from rp_core.legacy import write_json
from rp_core.persona_export import export_persona
from rp_core.protocol import RPCError
from rp_core.settings import DEFAULT_FILES, load_io, read_json

CODE = Path(__file__).parent / "compat_runtime"
DEPENDENCIES = (
    "telegram",
    "numpy",
    "faiss",
    "google.genai",
    "openai",
    "anthropic",
    "zai",
    "pypdf",
    "PIL",
    "pillow_heif",
)


def dependency_status():
    result = {}
    for name in DEPENDENCIES:
        try:
            result[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            result[name] = False
    return result


def preflight(root: Path) -> dict:
    root = root.resolve()
    persona = root / "src/config/persona.py"
    card = root / "character.json"
    role_ready = card.is_file()
    if not role_ready and persona.is_file():
        role_ready = bool(export_persona(persona).get("instructions"))
    layout_valid = False
    if (root / "io.json").exists():
        try:
            validate_layout(root)
            layout_valid = True
        except RPCError:
            pass
    return {
        "engine": "full",
        "code_public": True,
        "character_available": role_ready,
        "full_layout_valid": layout_valid,
        "history_present": (root / "data/tenshi_log.json").is_file(),
        "memory_present": (root / "data/tenshi_memory.db").is_file(),
        "dependencies": dependency_status(),
        "credential_presence": {
            name: bool(os.environ.get(name))
            for name in (
                "TG_TOKEN",
                "ADMIN_ID",
                "QWEN_API_KEY",
                "OPENROUTER_API_KEY",
                "CLAUDE_API_KEY",
                "GEMINI_API_KEY",
                "GPT_API_KEY",
                "GLM_API_KEY",
            )
        },
        "modified": False,
    }


def prepare(root: Path) -> dict:
    if (root / "io.json").exists():
        validate_layout(root)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    created = []
    card = root / "character.json"
    legacy = root / "src/config/persona.py"
    if not card.exists() and legacy.is_file():
        write_json(card, export_persona(legacy, root / "src/config/fixed.py"))
        created.append("character.json")
    if not (root / "io.json").exists():
        initialize_data(root, driver="tenshi-v1")
        created.extend(["io.json", "identity.json"])
    if not (root / "identity.json").exists():
        write_json(root / "identity.json", {"allowed_user_ids": [], "telegram": {}})
        created.append("identity.json")
    identity = read_json(root / "identity.json")
    admin = os.environ.get("ADMIN_ID")
    if admin and not identity.get("allowed_user_ids"):
        identity["allowed_user_ids"] = [admin]
        identity["telegram"] = {"chat_id": admin, "allowed_chat_ids": [admin]}
        write_json(root / "identity.json", identity)
    catalog = root / "data/stickers.json"
    old_catalog = root / "src/config/stickers.json"
    if not catalog.exists() and old_catalog.is_file():
        write_json(catalog, read_json(old_catalog))
        created.append("data/stickers.json")
    validate_layout(root)
    return {
        "prepared": True,
        "created": created,
        "database_rebuilt": False,
        "private_code_executed": False,
    }


def validate_layout(root: Path):
    io = load_io(root)
    expected = {**DEFAULT_FILES, "state": ".rp"}
    if io["driver"] != "tenshi-v1" or any(
        io["files"][key] != value for key, value in expected.items()
    ):
        raise RPCError(
            "unsupported_full_layout",
            "The full engine requires canonical tenshi-v1 bindings; no database was changed.",
        )
    for name in ("data", "logs", "downloads", ".rp", "inbox", "Tenshi.json"):
        if not (root / name).resolve().is_relative_to(root.resolve()):
            raise RPCError(
                "invalid_data_io", "Full-runtime data must stay inside its private root."
            )
    for directory in ("data", "logs", "downloads", "inbox", ".rp"):
        if (root / directory).exists():
            for path in (root / directory).rglob("*"):
                if path.is_symlink():
                    raise RPCError(
                        "invalid_data_io", "Symlinks inside runtime data are not supported."
                    )
    return io


@contextmanager
def view(root: Path):
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="rp-full-view-") as temporary:
        workspace = Path(temporary)
        for name in ("data", "logs", "downloads"):
            (root / name).mkdir(parents=True, exist_ok=True)
            (workspace / name).symlink_to(root / name, target_is_directory=True)
        for name in ("src", "scripts", "skills"):
            (workspace / name).symlink_to(CODE / name, target_is_directory=True)
        note = root / "Tenshi.json"
        if not note.exists():
            write_json(note, [])
        (workspace / "Tenshi.json").symlink_to(note)
        for name, default in {
            "tenshi_log.json": [],
            "knowledge_base.json": {},
            "world_history.json": {},
        }.items():
            if not (root / "data" / name).exists():
                write_json(root / "data" / name, default)
        try:
            yield workspace
        finally:
            projected = workspace / "Tenshi.json"
            if projected.exists() and not projected.is_symlink():
                shutil.copy2(projected, note)


def environment(root: Path, workspace: Path, *, shadow: bool, payload: dict | None = None):
    env = dict(os.environ)
    env.update(
        {
            "RP_RUNTIME_ROOT": str(workspace),
            "RP_CHARACTER_PATH": str(root / "character.json"),
            "RP_DATA_ROOT": str(root),
            "RP_EXTERNAL_PERSISTENCE": "1",
            "RP_SHADOW": "1" if shadow else "0",
            "RP_SHADOW_TRACE": str(root / "logs/shadow-delivery.jsonl"),
            "PYTHONPATH": str(CODE / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    public_repo = Path(__file__).resolve().parents[2]
    env["RP_CODE_REPO_ROOT"] = str(public_repo)
    env["TELEGRAM_TOKEN"] = (
        env.get("TG_TOKEN") or env.get("RP_TELEGRAM_TOKEN") or env.get("TELEGRAM_TOKEN", "")
    )
    identity = read_json(root / "identity.json", {})
    admin = identity.get("telegram", {}).get("chat_id") or next(
        iter(identity.get("allowed_user_ids", [])), None
    )
    if admin is not None:
        env["ADMIN_ID"] = str(admin)
    env["REPO_NAME"] = env.get("GH_REPO", env.get("REPO_NAME", ""))
    env["GITHUB_TOKEN"] = env.get("TENSHI_BOT_ACTION_TOKEN", env.get("GITHUB_TOKEN", ""))
    env["OPENROUTER_API_KEY"] = env.get("OPENROUTER_API_KEY") or env.get("RP_MODEL_API_KEY", "")
    kb = root / "data/knowledge_base.json"
    env["RP_KB_BASE_SHA"] = hashlib.sha256(kb.read_bytes()).hexdigest() if kb.exists() else ""
    env["GITHUB_OUTPUT"] = str(root / "logs/runtime-outputs.txt")
    env["PROVIDER_HEALTH_PATH"] = str(root / "data/provider_health.json")
    if shadow:
        env["TELEGRAM_TOKEN"] = str(100000000) + ":" + "x" * 35
        env["ADMIN_ID"] = "1"
        env["PERCEPTION_SCHEDULE_SYNC_URL"] = ""
        env["PERCEPTION_SCHEDULE_SYNC_SECRET"] = ""
    payload = payload or {}
    if payload.get("file_ids") and not payload.get("file_id"):
        payload = {**payload, **payload["file_ids"][0]}
    mapping = {
        "text": "MSG_TEXT",
        "chat_id": "MSG_CHAT_ID",
        "user_id": "MSG_USER_ID",
        "msg_id": "MSG_ID",
        "file_id": "MSG_FILE_ID",
        "file_type": "MSG_FILE_TYPE",
        "file_name": "MSG_FILE_NAME",
        "date": "MSG_DATE",
        "weather": "CURRENT_WEATHER",
    }
    for name in mapping.values():
        env.pop(name, None)
    for key, name in mapping.items():
        if payload.get(key) is not None:
            env[name] = str(payload[key])
    for key, name in (
        ("file_ids", "MSG_FILE_IDS"),
        ("location", "MSG_LOCATION"),
        ("reply_to", "MSG_REPLY_TO"),
    ):
        if key in payload:
            env[name] = json.dumps(payload[key], ensure_ascii=False)
    env["IS_RANDOM_TRIGGER"] = str(bool(payload.get("is_random_trigger"))).lower()
    env["PERCEPTION_SCHEDULED_FORCE"] = str(bool(payload.get("scheduled_force"))).lower()
    env["PERCEPTION_SCHEDULE_NOTE"] = str(payload.get("note", ""))
    env["PERCEPTION_TRIGGER_AT_MS"] = str(payload.get("trigger_at_ms", 0))
    return env


def sanitize_logs(root: Path, env: dict):
    values = {
        value
        for key, value in env.items()
        if len(value) >= 8
        and any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    for path in (root / "logs").rglob("*"):
        if path.is_file() and path.stat().st_size < 64 * 1024 * 1024:
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                continue
            scrubbed = content
            for value in values:
                scrubbed = scrubbed.replace(value, "[REDACTED]")
            if scrubbed != content:
                path.write_text(scrubbed, encoding="utf-8")


def run_full(
    root: Path, task: str, *, payload: dict | None = None, shadow: bool = False, timeout: int = 1200
):
    root = root.resolve()
    validate_layout(root)
    if not shadow and os.environ.get("RP_ALLOW_LIVE") != "1":
        raise RPCError(
            "live_disabled", "Live operations require an explicit RP_ALLOW_LIVE=1 cutover."
        )
    if not (root / "character.json").is_file():
        raise RPCError(
            "character_required", "Prepare character data before running the full engine."
        )
    if not all(dependency_status().values()):
        raise RPCError("full_dependencies_required", "Install the public full dependency extra.")
    scripts = {
        "reply": ["src/telegram_tenshi_logger.py"],
        "perception": ["src/perception_layer.py"],
        "force-perception": ["src/perception_layer.py", "--force"],
        "summary": ["src/log_update.py"],
        "archive": ["scripts/weekly_archive_tenshi_log.py"],
        "cleanup": ["scripts/clean_tenshi_log.py", "--no-backup"],
        "probe": ["scripts/probe_provider_paths.py", "--no-all-models", "--timeout", "8"],
        "selfcheck": ["scripts/deps_selfcheck.py"],
    }
    if task not in scripts:
        raise RPCError("invalid_params", "Unknown full-runtime task.")
    with view(root) as workspace:
        env = environment(root, workspace, shadow=shadow, payload=payload)
        env["RP_ACTIVE_TASK"] = task
        output = root / "logs/full-process.log"
        with output.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", str(workspace / scripts[task][0]), *scripts[task][1:]],
                cwd=workspace,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code = 124
        sanitize_logs(root, env)
        skipped = task in ("perception", "force-perception") and code == 2
        if code != 0 and not skipped:
            raise RPCError(
                "full_runtime_failed",
                "The compatibility task failed; details remain in private logs.",
                retryable=True,
            )
        return {
            "engine": "full",
            "task": task,
            "status": "skipped" if skipped else "completed",
            "shadow": shadow,
            "public_code_only": True,
        }


def synthetic_shadow(*, task="selfcheck"):
    """Use credentials only with a newly created synthetic character and history."""
    with tempfile.TemporaryDirectory(prefix="rp-full-shadow-") as temporary:
        root = Path(temporary)
        initialize_data(root, driver="tenshi-v1")
        result = run_full(
            root,
            task,
            shadow=True,
            payload={
                "text": "这是一条合成测试消息，请只回复：测试成功。",
                "chat_id": 1,
                "user_id": 1,
                "msg_id": 1,
                "date": 1794614400,
            },
            timeout=600,
        )
        trace = root / "logs/shadow-delivery.jsonl"
        result["synthetic_data_only"] = True
        result["real_telegram_calls"] = 0
        result["delivery_captured"] = trace.exists() and trace.stat().st_size > 0
        history = read_json(root / "data/tenshi_log.json", [])
        messages = [
            message for conversation in history for message in conversation.get("chat_history", [])
        ]
        if task == "reply" and not any(item.get("bot_answer") for item in messages):
            raise RPCError(
                "shadow_incomplete", "The synthetic reply did not produce a persisted answer."
            )
        return result
