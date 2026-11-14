# SPDX-License-Identifier: AGPL-3.0-only
"""All production logic lives here/public drivers; private repositories are data."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from rp_core.legacy import TenshiData, write_json
from rp_core.protocol import EVENT, RPCError, dumps, loads, validate
from rp_core.providers import ModelRouter, post_json
from rp_core.reference import TOOLS, SQLiteAdapter
from rp_core.runner import process_queue
from rp_core.settings import IDENTITY_SCHEMA, data_path, load_io, load_runtime, read_json
from rp_core.telegram import normalize


class PublicRuntime(SQLiteAdapter):
    def __init__(self, root: Path, config: dict, io: dict):
        self.public_config, self.io = config, io
        root = root.resolve()
        self.paths = {name: data_path(root, value) for name, value in io["files"].items()}
        self.legacy = TenshiData(root, io) if io["driver"] == "tenshi-v1" else None
        public_storage = {
            "state_directory": io["files"]["state"],
            "policy": config.get("policy", {}),
            "git_sync": config["persistence"]["driver"] == "git",
            "git_branch": config["persistence"].get("branch", "main"),
        }
        public_storage["events_file"] = io["files"]["events"]
        super().__init__(root, config=public_storage)
        self.polled = False
        self.router = ModelRouter(config["models"])
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS projections(session_id TEXT,event_id TEXT,PRIMARY KEY(session_id,event_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries(action_id TEXT PRIMARY KEY,receipt TEXT NOT NULL)"
            )

    def __enter__(self):
        return self

    def describe(self):
        return {
            **super().describe(),
            "implementation": "public",
            "delivery": self.public_config["delivery"]["driver"],
            "data_driver": self.io["driver"],
        }

    def __exit__(self, *_):
        self.close()

    def _state(self, db, session_id):
        exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        revision, payload = super()._state(db, session_id)
        if not exists and self.legacy is not None:
            payload["history"] = self.legacy.history(session_id)
        return revision, payload

    def load_state(self, params: dict) -> dict:
        with self.connection() as db:
            receipt = self._receipt(db, params)
            if receipt is not None:
                return {"receipt": receipt}
            revision, state = self._state(db, params["session_id"])
        character = read_json(self.paths["character"])
        notes, memories, summary = state["notes"], state["memories"], state["summary"]
        if self.legacy:
            notes = list(dict.fromkeys([*self.legacy.notes(), *notes]))
            memories = list(dict.fromkeys([*self.legacy.memories(), *memories]))
            summary = summary or self.legacy.knowledge()
        return {
            "snapshot": {
                "revision": str(revision),
                "character": character,
                "history": state["history"][:200]
                if params.get("job") == "compact"
                else state["history"][-200:],
                "notes": notes[-128:],
                "memories": memories[-128:],
                "summary": summary,
                "relationship": state["relationship"]
                or (self.legacy.relationship() if self.legacy else ""),
                "policy": self.public_config.get("policy", {}),
                "tools": deepcopy(TOOLS),
            }
        }

    def generate(self, params):
        return self.router.generate(params)

    def execute_tool(self, params):
        result = super().execute_tool(params)
        if self.legacy and params["name"] == "memory.search":
            result["memories"] = list(
                dict.fromkeys(
                    [*self.legacy.memories(params["arguments"]["query"]), *result["memories"]]
                )
            )[-20:]
        return result

    def next_event(self, params):
        inbox = self.paths["inbox"]
        queued = []
        if inbox.exists():
            with self.connection() as db:
                for path in inbox.glob("*.json"):
                    item = read_json(path)
                    item = item if "event" in item else {"event": item}
                    event = item["event"]
                    validate(event, EVENT)
                    if params.get("event_id") is not None and event["id"] != params["event_id"]:
                        continue
                    if db.execute(
                        "SELECT 1 FROM receipts WHERE session_id=? AND event_id=?",
                        (event["session_id"], event["id"]),
                    ).fetchone():
                        continue
                    timestamp = datetime.fromisoformat(
                        event["occurred_at"].upper().replace("Z", "+00:00")
                    ).timestamp()
                    message_id = str(
                        event.get("extensions", {}).get("telegram", {}).get("message_id", "0")
                    )
                    queued.append(
                        (timestamp, int(message_id) if message_id.isdigit() else 0, path.name, item)
                    )
        if queued:
            queued.sort(key=lambda item: item[:3])
            return queued[0][3]
        item = super().next_event(params)
        if item.get("event") is not None:
            return item
        if (
            not self.polled
            and self.public_config.get("ingress", {}).get("driver") == "telegram-poll"
        ):
            self.polled = True
            self._poll_telegram()
            return self.next_event(params)
        return item

    def _poll_telegram(self):
        ingress = self.public_config["ingress"]
        token = os.environ.get(ingress.get("token_env", "RP_TELEGRAM_TOKEN"), "")
        if not token:
            raise RPCError("missing_credential", "Telegram credential is not configured.")
        identity = read_json(self.paths["identity"])
        validate(identity, IDENTITY_SCHEMA, code="invalid_data_io")
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT value FROM runtime_metadata WHERE key='telegram_offset'"
            ).fetchone()
        offset = int(row[0]) if row else 0
        result = post_json(
            ingress.get("api_base", "https://api.telegram.org").rstrip("/")
            + "/bot"
            + token
            + "/getUpdates",
            {
                "offset": offset,
                "limit": 25,
                "timeout": 0,
                "allowed_updates": ["message", "edited_message"],
            },
        )
        if result.get("ok") is not True or not isinstance(result.get("result"), list):
            raise RPCError(
                "ingress_failed", "Telegram polling was not acknowledged.", retryable=True
            )
        for update in result["result"]:
            event = normalize(update, identity)
            if event is not None:
                write_json(self.paths["inbox"] / (event["id"] + ".json"), {"event": event})
            offset = max(offset, int(update["update_id"]) + 1)
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO runtime_metadata VALUES ('telegram_offset',?)",
                (str(offset),),
            )

    def flush_outbox(self, params):
        config = self.public_config["delivery"]
        if config["driver"] == "file":
            return super().flush_outbox(params)
        token = os.environ.get(config.get("token_env", "RP_TELEGRAM_TOKEN"), "")
        if not token:
            raise RPCError("missing_credential", "Telegram credential is not configured.")
        identity = read_json(self.paths["identity"])
        validate(identity, IDENTITY_SCHEMA, code="invalid_data_io")
        telegram = identity.get("telegram", {})
        allowed = {str(value) for value in telegram.get("allowed_chat_ids", [])}
        allowed.update(str(value) for value in identity.get("allowed_user_ids", []))
        count = 0
        with self.connection() as db:
            pending = db.execute(
                "SELECT id,session_id,payload FROM outbox WHERE exported=0 ORDER BY rowid"
            ).fetchall()
        for action_id, session_id, raw in pending:
            target = str(telegram.get("chat_id") or session_id)
            if target not in allowed:
                raise RPCError(
                    "delivery_not_allowed", "The destination is not in private identity data."
                )
            action = loads(raw)
            body = {"chat_id": target, "text": action["text"]}
            if str(action.get("reply_to", "")).isdigit():
                body["reply_parameters"] = {
                    "message_id": int(action["reply_to"]),
                    "allow_sending_without_reply": True,
                }
            result = post_json(
                config.get("api_base", "https://api.telegram.org").rstrip("/")
                + "/bot"
                + token
                + "/sendMessage",
                body,
            )
            if result.get("ok") is not True:
                raise RPCError(
                    "delivery_failed", "Telegram did not confirm delivery.", retryable=True
                )
            with self.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO deliveries VALUES (?,?)",
                    (action_id, dumps({"message_id": result["result"]["message_id"]})),
                )
                db.execute("UPDATE outbox SET exported=1 WHERE id=?", (action_id,))
            count += 1
        return {"delivered": count}

    def finalize(self, params):
        if self.legacy:
            with self.connection() as db:
                pending = db.execute(
                    "SELECT t.session_id,t.event_id,t.payload FROM transactions t LEFT JOIN projections p USING(session_id,event_id) WHERE p.event_id IS NULL ORDER BY t.rowid"
                ).fetchall()
            for sid, eid, raw in pending:
                self.legacy.project(loads(raw))
                with self.connection() as db:
                    db.execute("INSERT OR IGNORE INTO projections VALUES (?,?)", (sid, eid))
            delivery_states = {}
            with self.connection() as db:
                for sid, eid, raw in db.execute(
                    "SELECT session_id,event_id,payload FROM transactions"
                ):
                    actions = loads(raw)["result"]["actions"]
                    delivered, message_ids = True, []
                    for action in actions:
                        row = db.execute(
                            "SELECT exported FROM outbox WHERE id=?", (action["id"],)
                        ).fetchone()
                        delivered = delivered and bool(row and row[0])
                        receipt = db.execute(
                            "SELECT receipt FROM deliveries WHERE action_id=?", (action["id"],)
                        ).fetchone()
                        if receipt:
                            message_ids.append(loads(receipt[0])["message_id"])
                    mapped = self.io.get("session_map", {}).get(sid, sid)
                    delivery_states[(mapped, eid)] = (delivered, message_ids)
            self.legacy.mark_delivery(delivery_states)
        if not self.config["git_sync"]:
            return {"durable": True, "scope": "local"}
        self._persist_git()
        return {"durable": True, "scope": "remote"}

    def _persist_git(self):
        root = self._git("rev-parse", "--show-toplevel").stdout.decode().strip()
        if Path(root).resolve() != self.root:
            raise RPCError("persistence_failed", "Data must be a separate private repository.")
        paths = [self.io["files"]["state"]]
        if self.paths["inbox"].exists():
            paths.append(self.io["files"]["inbox"])
        if self.legacy:
            paths += [
                self.io["files"][name]
                for name in ("history", "notes", "memory")
                if self.paths[name].exists()
            ]
        self._git("add", "--", *paths)
        self._git("ls-files", "--error-unmatch", str(self.database.relative_to(self.root)))
        if self._git("diff", "--cached", "--quiet", "--", *paths, allow_difference=True).returncode:
            self._git("commit", "--only", "-m", "Persist RP character data [skip ci]", "--", *paths)
        branch = self.config["git_branch"]
        if not branch or branch.startswith("-") or ".." in branch:
            raise RPCError("invalid_configuration", "Invalid persistence branch.")
        self._git("fetch", "origin", branch)
        try:
            self._git("rebase", "origin/" + branch)
        except RPCError:
            self._git("rebase", "--abort", allow_difference=True)
            raise RPCError(
                "persistence_failed",
                "Concurrent data changes require recovery; no content was overwritten.",
                retryable=True,
            ) from None
        self._git("push", "origin", "HEAD:refs/heads/" + branch)


def run_data(
    root: Path,
    config_path: Path,
    *,
    io_path: Path | None = None,
    event_id: str | None = None,
    require_remote: bool = False,
):
    config = load_runtime(config_path)
    with PublicRuntime(root, config, load_io(root, io_path)) as runtime:
        return process_queue(
            runtime,
            max_events=config.get("max_events", 25),
            event_id=event_id,
            require_remote=require_remote,
        )
