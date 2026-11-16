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

TASKS = (
    "reply",
    "perception",
    "force-perception",
    "summary",
    "archive",
    "cleanup",
    "probe",
    "daily-backup",
    "weekly-backup",
)


def _porcelain_paths(status: bytes) -> list[str]:
    """Decode changed paths from Git's NUL-delimited porcelain-v1 output."""
    records = status.split(b"\0")
    paths = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        state = record[:2].decode("ascii", errors="ignore")
        paths.append(record[3:].decode("utf-8", errors="surrogateescape"))
        if "R" in state or "C" in state:
            if index < len(records) and records[index]:
                paths.append(records[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    return paths


def _logs_only(paths: list[str]) -> bool:
    return bool(paths) and all(path == "logs" or path.startswith("logs/") for path in paths)


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
    pending = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *files],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if pending.returncode:
        raise RPCError("persistence_failed", "Private changes could not be inspected.")
    changed_paths = _porcelain_paths(pending.stdout)
    if not changed_paths:
        return {"persisted": False, "reason": "no_changes"}
    if _logs_only(changed_paths):
        # Debug, trace and process logs are disposable diagnostics. Leave them
        # in the current runner; do not make a private Git commit for logs alone.
        return {"persisted": False, "reason": "logs_only"}
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
    except Exception as error:
        if task == "reply" and not (
            isinstance(error, RPCError) and error.code == "reply_retryable_failure"
        ):
            quarantine_reply(root, payload)
        if remote:
            persist(root, branch)
        raise
    if task == "reply" and result.get("status") == "delivery_unknown":
        quarantine_reply(root, payload)
    if remote:
        persist(root, branch)
    return result


def quarantine_reply(root: Path, payload: dict) -> bool:
    """Prevent a later reply from implicitly re-consuming an ambiguous old turn."""
    path = root / "data/tenshi_log.json"
    history = read_json(path, [])
    changed = False
    for conversation in history:
        if str(conversation.get("user_id")) != str(payload.get("user_id")):
            continue
        for message in conversation.get("chat_history", []):
            if str(message.get("msg_id")) == str(payload.get("msg_id")):
                if not message.get("rp_delivery_quarantined"):
                    message["rp_delivery_quarantined"] = True
                    changed = True
    if changed:
        write_json(path, history)
    return changed


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
    counts = {"engine": "full", "processed": 0, "replayed": 0, "quarantined": 0, "failed": 0}
    items = []
    safe_tasks = {"probe", "summary", "archive", "cleanup", "daily-backup", "weekly-backup"}
    matched = False
    for path in (root / "inbox").glob("*.json"):
        item = read_json(path)
        identifier = item.get("event", {}).get("id", path.stem)
        if not isinstance(identifier, str):
            raise RPCError("invalid_event", "The event ID must be a string.")
        if event_id is not None and event_id != identifier:
            continue
        matched = True
        task = item.get("full_task", "reply")
        if task not in TASKS:
            raise RPCError("invalid_event", "Unknown queued full-runtime task.")
        receipt = receipts / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")
        previous = read_json(receipt, {})
        if previous.get("status") == "completed":
            counts["replayed"] += 1
            continue
        attempts = int(previous.get("attempts", 0))
        if previous and previous.get("status") != "retryable_failure" and task not in safe_tasks:
            if task == "reply" and quarantine_reply(root, legacy_payload(item)) and remote:
                persist(root, branch)
            counts["quarantined"] += 1
            continue
        if attempts >= 3:
            counts["failed"] += 1
            continue
        payload = legacy_payload(item) if task == "reply" else item.get("legacy_payload", {})
        items.append(
            (
                int(payload.get("date", 0)),
                str(payload.get("msg_id", "")).zfill(20),
                identifier,
                task,
                payload,
                receipt,
                attempts,
            )
        )

    def save_receipt(
        receipt, identifier, task, status, attempts, *, outcome=None, event_payload=None
    ):
        value = {
            "event_id": identifier,
            "task": task,
            "status": status,
            "attempts": attempts,
            "updated_at": time.time(),
        }
        if outcome:
            value["outcome"] = outcome
        if status == "delivery_unknown" and task == "reply":
            quarantine_reply(root, event_payload or {})
        write_json(receipt, value)
        if remote:
            persist(root, branch)

    for _, _, identifier, task, payload, receipt, attempts in sorted(
        items, key=lambda row: row[:3]
    )[: config.get("max_events", 25)]:
        if task == "reply":
            authorize(root, payload)
        attempts += 1
        # Persist the guard before launching any process capable of Telegram delivery.
        save_receipt(receipt, identifier, task, "running", attempts)
        try:
            result = run_full(root, task, payload=payload)
        except Exception as error:
            safe = task in safe_tasks or (
                isinstance(error, RPCError) and error.code == "reply_retryable_failure"
            )
            status = "retryable_failure" if safe else "delivery_unknown"
            save_receipt(receipt, identifier, task, status, attempts, event_payload=payload)
            counts["failed" if safe else "quarantined"] += 1
            continue
        outcome = result.get("status") if isinstance(result, dict) else None
        if task == "reply" and outcome not in ("sent", "no_send", "already_completed"):
            status = "retryable_failure" if outcome == "retryable_failure" else "delivery_unknown"
            save_receipt(
                receipt, identifier, task, status, attempts, outcome=outcome, event_payload=payload
            )
            counts["failed" if status == "retryable_failure" else "quarantined"] += 1
            continue
        save_receipt(receipt, identifier, task, "completed", attempts, outcome=outcome)
        counts["processed"] += 1
    if event_id is not None and not matched:
        raise RPCError(
            "event_not_found", "The selected private event was not found.", retryable=True
        )
    if event_id is None:
        ledger = root / ".rp/maintenance.json"
        done = read_json(ledger, {})
        for task, interval in config.get("maintenance_seconds", {}).items():
            if task not in TASKS or task == "reply":
                raise RPCError("invalid_configuration", "Invalid maintenance task.")
            if time.time() - done.get(task, 0) < interval:
                continue
            try:
                execute(root, task, {}, remote=False, branch=branch)
            except Exception:
                counts["failed"] += 1
            else:
                done[task] = time.time()
                write_json(ledger, done)
            # Commit task data and the maintenance ledger together, also on failure.
            # Persistence failures remain fatal: no further delivery is safe then.
            if remote:
                persist(root, branch)
    return counts
