# SPDX-License-Identifier: AGPL-3.0-only
"""Reference private adapter: SQLite transactions, receipts and a file outbox.

This implementation has no vendor API client and no platform sender. Replace its
model process and delivery method in the private repository to connect real I/O.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from rp_core import PROTOCOL
from rp_core.protocol import (
    COMMIT,
    EVENT,
    MAX_FRAME_BYTES,
    RPCError,
    digest,
    dumps,
    loads,
    obj,
    validate,
)
from rp_core.transport import JsonlClient

_TEXT_ARG = obj({"text": {"type": "string", "minLength": 1, "maxLength": 8000}}, ("text",))
TOOLS = [
    {
        "name": "memory.search",
        "description": "Read matching stored memories.",
        "mode": "read",
        "input_schema": obj({"query": {"type": "string", "maxLength": 1000}}, ("query",)),
    },
    {
        "name": "memory.append",
        "description": "Remember an explicitly supported fact after commit.",
        "mode": "deferred",
        "input_schema": _TEXT_ARG,
    },
    {
        "name": "note.append",
        "description": "Append a private fictional diary note after commit.",
        "mode": "deferred",
        "input_schema": _TEXT_ARG,
    },
    {
        "name": "schedule.upsert",
        "description": "Schedule a future perception event after commit.",
        "mode": "deferred",
        "input_schema": obj(
            {
                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                "due_at": {"type": "string", "format": "date-time"},
                "reason": {"type": "string", "maxLength": 4000},
            },
            ("id", "due_at", "reason"),
        ),
    },
    {
        "name": "schedule.cancel",
        "description": "Cancel a scheduled perception event after commit.",
        "mode": "deferred",
        "input_schema": obj({"id": {"type": "string", "minLength": 1, "maxLength": 128}}, ("id",)),
    },
]


class SQLiteAdapter:
    def __init__(self, root: Path, config: dict | None = None):
        self.root = root.resolve()
        self.config = (
            config if config is not None else loads((self.root / "adapter.json").read_bytes())
        )
        self.state_dir = self.root / self.config.get("state_directory", "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.state_dir / "state.sqlite"
        self.model: JsonlClient | None = None
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (session_id TEXT, event_id TEXT, digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS transactions (session_id TEXT, event_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, session_id TEXT, payload TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS archives (session_id TEXT, event_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(session_id,event_id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self) -> None:
        if self.model is not None:
            self.model.close()

    def describe(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "methods": [
                "state.load",
                "state.commit",
                "model.generate",
                "tool.execute",
                "runtime.next_event",
                "runtime.finalize",
                "outbox.flush",
            ],
            "delivery": "file-export-only",
        }

    def call(self, method: str, params: dict) -> dict:
        handlers = {
            "bridge.describe": lambda _: self.describe(),
            "state.load": self.load_state,
            "state.commit": self.commit_state,
            "model.generate": self.generate,
            "tool.execute": self.execute_tool,
            "runtime.next_event": self.next_event,
            "runtime.finalize": self.finalize,
            "outbox.flush": self.flush_outbox,
        }
        if method not in handlers:
            raise RPCError("method_not_found", "Unknown reference-adapter method.")
        return handlers[method](params)

    def _state(self, db: sqlite3.Connection, session_id: str) -> tuple[int, dict]:
        row = db.execute(
            "SELECT revision,payload FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row:
            return row[0], loads(row[1])
        return 0, {
            "history": [],
            "memories": [],
            "notes": [],
            "relationship": "",
            "summary": "",
            "scheduled": {},
            "relationship_proposals": [],
        }

    @staticmethod
    def _receipt(db: sqlite3.Connection, params: dict) -> dict | None:
        row = db.execute(
            "SELECT digest,result FROM receipts WHERE session_id=? AND event_id=?",
            (params["session_id"], params["event_id"]),
        ).fetchone()
        if not row:
            return None
        if row[0] != params["event_digest"]:
            raise RPCError(
                "idempotency_conflict", "The event ID was already used for different input."
            )
        return loads(row[1])

    def load_state(self, params: dict) -> dict:
        with self.connection() as db:
            receipt = self._receipt(db, params)
            if receipt is not None:
                return {"receipt": receipt}
            revision, payload = self._state(db, params["session_id"])
        character = loads((self.root / "character.json").read_bytes())
        # Compact a bounded oldest prefix; never delete history the model did not see.
        history = (
            payload["history"][:200]
            if params.get("job") == "compact"
            else payload["history"][-200:]
        )
        snapshot = {
            "revision": str(revision),
            "character": character,
            "history": history,
            "memories": payload["memories"][-128:],
            "notes": payload["notes"][-128:],
            "relationship": payload["relationship"],
            "summary": payload["summary"],
            "policy": self.config.get("policy", {}),
            "tools": deepcopy(TOOLS),
        }
        return {"snapshot": snapshot}

    def commit_state(self, params: dict) -> dict:
        validate(params, COMMIT)
        tx = params["transaction"]
        sid, eid = params["session_id"], params["event_id"]
        if (
            tx["event"]["session_id"] != sid
            or tx["event"]["id"] != eid
            or tx["result"]["session_id"] != sid
            or tx["result"]["event_id"] != eid
        ):
            raise RPCError("invalid_params", "Transaction identities do not match.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(db, params)
            if receipt is not None:
                return {"status": "duplicate", "result": receipt}
            revision, payload = self._state(db, sid)
            if str(revision) != params["expected_revision"]:
                return {"status": "conflict"}
            payload["history"].extend(tx["messages"])
            tools = {tool["name"]: tool for tool in TOOLS}
            for effect in tx["effects"]:
                tool = tools.get(effect["name"])
                if tool is None or tool["mode"] != "deferred":
                    raise RPCError("tool_not_allowed", "Unknown deferred effect.")
                validate(effect["arguments"], tool["input_schema"])
                args = effect["arguments"]
                if effect["name"] == "memory.append":
                    payload["memories"].append(args["text"])
                elif effect["name"] == "note.append":
                    payload["notes"].append(args["text"])
                elif effect["name"] == "schedule.upsert":
                    payload["scheduled"][args["id"]] = args
                elif effect["name"] == "schedule.cancel":
                    payload["scheduled"].pop(args["id"], None)
            maintenance = tx.get("maintenance", {})
            if maintenance.get("kind") == "summarize":
                payload["summary"] = maintenance["summary"]
                payload["memories"].extend(maintenance["memories"])
            elif maintenance.get("kind") == "compact":
                source_count = maintenance["history_count"]
                prefix = payload["history"][:source_count]
                if digest(prefix) != maintenance["history_digest"]:
                    raise RPCError(
                        "invalid_params", "Compaction source does not match its snapshot."
                    )
                count = min(maintenance["keep_last"], source_count)
                archived = prefix[:-count] if count else prefix
                db.execute("INSERT INTO archives VALUES (?,?,?)", (sid, eid, dumps(archived)))
                retained = prefix[-count:] if count else []
                payload["history"] = retained + payload["history"][source_count:]
                payload["summary"] = maintenance["summary"]
            elif maintenance.get("kind") == "reflect":
                payload["relationship_proposals"].append({"event_id": eid, **maintenance})
            result = {**tx["result"], "revision": str(revision + 1)}
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (sid, revision + 1, dumps(payload)),
            )
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?)",
                (sid, eid, params["event_digest"], dumps(result)),
            )
            db.execute("INSERT INTO transactions VALUES (?,?,?)", (sid, eid, dumps(tx)))
            for action in result["actions"]:
                db.execute(
                    "INSERT INTO outbox(id,session_id,payload) VALUES (?,?,?)",
                    (action["id"], sid, dumps(action)),
                )
        return {"status": "committed", "result": result}

    def generate(self, params: dict) -> dict:
        if self.model is None:
            command = self.config.get("model_command")
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(s, str) for s in command)
            ):
                raise RPCError("invalid_adapter", "Configure a private model command.")
            command = [sys.executable if s == "{python}" else s for s in command]
            self.model = JsonlClient(
                command, cwd=self.root, timeout=self.config.get("model_timeout_seconds", 90)
            )
        return self.model.call("model.generate", params)

    def execute_tool(self, params: dict) -> dict:
        if params.get("name") != "memory.search":
            raise RPCError("tool_not_allowed", "Only declared read tools can execute immediately.")
        validate(params.get("arguments"), TOOLS[0]["input_schema"])
        with self.connection() as db:
            _, payload = self._state(db, params["session_id"])
        query = params["arguments"]["query"].casefold()
        return {
            "memories": [text for text in payload["memories"] if query in text.casefold()][-20:]
        }

    def next_event(self, params: dict) -> dict:
        selected = params.get("event_id")
        queue = self.root / self.config.get("events_file", "events.jsonl")
        if queue.is_file():
            with queue.open("rb") as stream, self.connection() as db:
                while line := stream.readline(MAX_FRAME_BYTES + 1):
                    if len(line) > MAX_FRAME_BYTES:
                        raise RPCError("frame_too_large", "Queue entry exceeds the frame limit.")
                    if not line.strip():
                        continue
                    entry = loads(line)
                    item = entry if "event" in entry else {"event": entry}
                    event = item["event"]
                    validate(event, EVENT)
                    if selected is not None and event["id"] != selected:
                        continue
                    exists = db.execute(
                        "SELECT 1 FROM receipts WHERE session_id=? AND event_id=?",
                        (event["session_id"], event["id"]),
                    ).fetchone()
                    if not exists:
                        return item
        now = datetime.now(UTC)
        with self.connection() as db:
            for sid, raw in db.execute("SELECT id,payload FROM sessions ORDER BY id"):
                for scheduled in loads(raw).get("scheduled", {}).values():
                    if datetime.fromisoformat(scheduled["due_at"].replace("Z", "+00:00")) > now:
                        continue
                    eid = "scheduled-" + digest([sid, scheduled["id"], scheduled["due_at"]])[:40]
                    if selected is not None and selected != eid:
                        continue
                    if db.execute(
                        "SELECT 1 FROM receipts WHERE session_id=? AND event_id=?", (sid, eid)
                    ).fetchone():
                        continue
                    return {
                        "event": {
                            "id": eid,
                            "session_id": sid,
                            "kind": "perception",
                            "occurred_at": scheduled["due_at"],
                            "content": [{"type": "text", "text": scheduled["reason"]}],
                        }
                    }
        return {"event": None}

    def flush_outbox(self, _: dict) -> dict:
        # A reference file sink, explicitly NOT a Telegram/other-platform sender.
        with self.connection() as db:
            rows = db.execute("SELECT id,payload FROM outbox ORDER BY rowid").fetchall()
            temporary = self.state_dir / "outbox.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                for _, payload in rows:
                    stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.state_dir / "outbox.jsonl")
            db.execute("UPDATE outbox SET exported=1 WHERE exported=0")
        return {"exported": len(rows), "delivery": "file-export-only"}

    def _git(self, *args: str, allow_difference: bool = False) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "RP instance",
            "GIT_AUTHOR_EMAIL": "rp-instance@example.invalid",
            "GIT_COMMITTER_NAME": "RP instance",
            "GIT_COMMITTER_EMAIL": "rp-instance@example.invalid",
        }
        result = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, timeout=90, env=env
        )
        if result.returncode not in ((0, 1) if allow_difference else (0,)):
            raise RPCError(
                "persistence_failed",
                "Private Git persistence failed; no automatic merge was attempted.",
                retryable=True,
            )
        return result

    def finalize(self, _: dict) -> dict:
        if not self.config.get("git_sync", False):
            return {"durable": True, "scope": "local"}
        root = self._git("rev-parse", "--show-toplevel").stdout.decode().strip()
        if Path(root).resolve() != self.root:
            raise RPCError(
                "persistence_failed", "Git sync requires a separate private repository root."
            )
        self._git("add", "--", "state/")
        self._git("ls-files", "--error-unmatch", "state/state.sqlite")
        if self._git(
            "diff", "--cached", "--quiet", "--", "state/", allow_difference=True
        ).returncode:
            self._git(
                "commit", "-m", "Persist RP instance state [skip ci]", "--only", "--", "state/"
            )
        branch = self.config.get("git_branch", "main")
        if not isinstance(branch, str) or branch.startswith("-"):
            raise RPCError("invalid_adapter", "Invalid private Git branch.")
        self._git("push", "origin", "HEAD:refs/heads/" + branch)
        return {"durable": True, "scope": "remote"}


def serve_reference(root: Path) -> None:
    from rp_core.cli import serve_requests

    adapter = SQLiteAdapter(root)
    try:
        serve_requests(adapter.call)
    finally:
        adapter.close()
