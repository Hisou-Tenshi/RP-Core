# SPDX-License-Identifier: AGPL-3.0-only
"""Public queue, authorization and persistence for the complete roleplay engine."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from rp_core.full_runtime import CODE, environment, run_full, sanitize_logs, validate_layout
from rp_core.legacy import write_json
from rp_core.protocol import EVENT, RPCError, validate
from rp_core.settings import IDENTITY_SCHEMA, read_json

TASKS = ("reply", "perception", "force-perception", "summary", "archive", "cleanup", "probe")


def merge_staging(root: Path):
    root = root.resolve()
    if os.environ.get("RP_ALLOW_LIVE") != "1":
        raise RPCError("live_disabled", "Staging reconciliation requires explicit live enablement.")
    validate_layout(root)
    (root / "logs").mkdir(exist_ok=True)
    env = environment(root, root, shadow=False)
    with (root / "logs/merge-staging.log").open("ab") as log:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        pending = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "bot-staging/*"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if shallow.stdout.strip() == b"true" and pending.returncode == 0 and pending.stdout.strip():
            # Fetch ancestry only when needed; retain the partial clone's blob filter.
            history = subprocess.run(
                ["git", "fetch", "--unshallow", "--filter=blob:none", "origin", "main"],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
            if history.returncode:
                raise RPCError("persistence_failed", "Recovery ancestry could not be fetched.")
        result = subprocess.run(
            [
                sys.executable,
                str(CODE / "scripts/tenshi_git_sync.py"),
                "--repo",
                str(root),
                "merge-staging",
            ],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
    sanitize_logs(root, env)
    if result.returncode:
        raise RPCError("persistence_failed", "Private staging reconciliation failed.")
    return {"reconciled": True}


def authorize(root: Path, payload: dict):
    identity = read_json(root / "identity.json")
    validate(identity, IDENTITY_SCHEMA)
    allowed = {str(value) for value in identity["allowed_user_ids"]}
    telegram = identity.get("telegram", {})
    chats = {str(value) for value in telegram.get("allowed_chat_ids", [])} or allowed
    if str(payload.get("user_id")) not in allowed or str(payload.get("chat_id")) not in chats:
        raise RPCError(
            "delivery_not_allowed", "The private identity does not authorize this event."
        )


def legacy_payload(item: dict) -> dict:
    if "legacy_payload" in item:
        if not isinstance(item["legacy_payload"], dict):
            raise RPCError("invalid_event", "Invalid full-runtime payload.")
        return item["legacy_payload"]
    event = item["event"]
    validate(event, EVENT)
    telegram = event.get("extensions", {}).get("telegram", {})
    content = event["content"]
    payload = {
        "text": chr(10).join(part["text"] for part in content if part["type"] == "text"),
        "chat_id": telegram.get("chat_id", event["session_id"]),
        "user_id": event["actor"]["id"],
        "msg_id": telegram.get("message_id", event["id"]),
        "date": int(
            datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).timestamp()
        ),
    }
    if any(part["type"] == "attachment" for part in content):
        raise RPCError(
            "full_payload_required", "Media events must preserve the complete Telegram payload."
        )
    for part in content:
        if part["type"] == "location":
            payload["location"] = {key: part[key] for key in ("latitude", "longitude")}
    return payload


def persist(root: Path, branch="main"):
    """Stage only private state; the mature sync helper handles retry/merge."""
    if not branch or branch.startswith("-") or ".." in branch:
        raise RPCError("invalid_configuration", "Invalid data branch.")
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, check=False
    )
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != root.resolve():
        raise RPCError("persistence_failed", "Data must be a separate private Git repository.")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True, check=False
    )
    if staged.returncode:
        raise RPCError(
            "persistence_failed", "Unexpected staged files require review before persistence."
        )
    files = [
        name
        for name in (
            "character.json",
            "identity.json",
            "io.json",
            "Tenshi.json",
            "data",
            "logs",
            "downloads",
            ".rp",
            "inbox",
        )
        if (root / name).exists()
    ]
    env = environment(root, root, shadow=False)
    output = root / "logs/git-sync.log"
    output.parent.mkdir(exist_ok=True)
    with output.open("ab") as log:
        result = subprocess.run(
            [
                sys.executable,
                str(CODE / "scripts/tenshi_git_sync.py"),
                "--repo",
                str(root),
                "push",
                "--tier",
                "reply",
                "--branch",
                branch,
                "--message",
                "Persist RP private state [skip ci]",
                "--push-when-clean",
                "--files",
                *files,
            ],
            env=env,
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
    sanitize_logs(root, env)
    if result.returncode:
        with output.open("ab") as log:
            recovery = subprocess.run(
                [
                    sys.executable,
                    str(CODE / "scripts/tenshi_git_sync.py"),
                    "--repo",
                    str(root),
                    "push",
                    "--tier",
                    "edge",
                    "--branch",
                    branch,
                    "--run-id",
                    uuid.uuid4().hex,
                    "--workflow",
                    "rp-core-recovery",
                    "--message",
                    "Preserve private RP recovery state [skip ci]",
                    "--push-when-clean",
                    "--files",
                    *files,
                ],
                env=env,
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
        sanitize_logs(root, env)
        if recovery.returncode == 0:
            raise RPCError(
                "persistence_staged",
                "State was preserved on a private recovery branch; reconcile before further delivery.",
                retryable=True,
            )
        raise RPCError(
            "persistence_failed",
            "Private persistence failed; inspect the private sync log before retrying.",
            retryable=True,
        )


def execute(root, task, payload, *, remote=False, branch="main"):
    if task not in TASKS:
        raise RPCError("invalid_params", "Unknown full-runtime task.")
    if task == "reply":
        authorize(root, payload)
    try:
        result = run_full(root, task, payload=payload)
    except Exception:
        if remote:
            persist(root, branch)
        raise
    if remote:
        persist(root, branch)
    return result


def run_queue(root: Path, config: dict, *, event_id=None, require_remote=False):
    root = root.resolve()
    validate_layout(root)
    if os.environ.get("RP_ALLOW_LIVE") != "1":
        raise RPCError("live_disabled", "Full-runtime execution has not been enabled.")
    remote = require_remote or config["persistence"]["driver"] == "git"
    branch = config["persistence"].get("branch", "main")
    if remote:
        merge_staging(root)
    receipts = root / ".rp/full-receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    counts = {"engine": "full", "processed": 0, "replayed": 0}
    inbox = root / "inbox"
    items = []
    for path in inbox.glob("*.json"):
        item = read_json(path)
        identifier = item.get("event", {}).get("id", path.stem)
        if not isinstance(identifier, str):
            raise RPCError("invalid_event", "The event ID must be a string.")
        if event_id is not None and event_id != identifier:
            continue
        receipt = receipts / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")
        previous = read_json(receipt, {})
        if previous.get("status") == "completed":
            continue
        if previous:
            raise RPCError(
                "delivery_uncertain",
                "A prior task may have delivered; reconcile the private receipt before replay.",
            )
        payload = (
            legacy_payload(item)
            if item.get("full_task", "reply") == "reply"
            else item.get("legacy_payload", {})
        )
        items.append(
            (
                int(payload.get("date", 0)),
                str(payload.get("msg_id", "")).zfill(20),
                identifier,
                item,
                payload,
                receipt,
            )
        )
    for _, _, identifier, item, payload, receipt in sorted(items, key=lambda row: row[:3])[
        : config.get("max_events", 25)
    ]:
        task = item.get("full_task", "reply")
        if task not in TASKS:
            raise RPCError("invalid_event", "Unknown queued full-runtime task.")
        if task == "reply":
            authorize(root, payload)
        write_json(receipt, {"event_id": identifier, "status": "running", "task": task})
        if remote:
            persist(root, branch)
        try:
            run_full(root, task, payload=payload)
        except Exception:
            write_json(receipt, {"event_id": identifier, "status": "uncertain", "task": task})
            if remote:
                persist(root, branch)
            raise
        write_json(receipt, {"event_id": identifier, "status": "completed", "task": task})
        if remote:
            persist(root, branch)
        counts["processed"] += 1
    if event_id is not None and not items:
        counts["replayed"] = 1
    if event_id is None:
        ledger = root / ".rp/maintenance.json"
        done = read_json(ledger, {})
        for task, interval in config.get("maintenance_seconds", {}).items():
            if task not in TASKS or task == "reply":
                raise RPCError("invalid_configuration", "Invalid maintenance task.")
            if time.time() - done.get(task, 0) < interval:
                continue
            execute(root, task, {}, remote=remote, branch=branch)
            done[task] = time.time()
            write_json(ledger, done)
            if remote:
                persist(root, branch)
    return counts
