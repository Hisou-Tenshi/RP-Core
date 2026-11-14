# SPDX-License-Identifier: AGPL-3.0-only
"""Public compatibility driver for the existing Tenshi JSON/SQLite data layout.

No schema migration, vector deletion, or embedding rebuild is performed. New
runtime receipts live separately; original files are projected before delivery.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from rp_core.protocol import RPCError, dumps
from rp_core.settings import data_path, read_json


def write_json(path: Path, value) -> None:
    rendered = dumps(value) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == rendered:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".rp-tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    with suppress(OSError):
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class TenshiData:
    def __init__(self, root: Path, io: dict):
        self.root, self.io = root, io
        self.paths = {name: data_path(root, relative) for name, relative in io["files"].items()}

    def history(self, session_id: str) -> list[dict]:
        target = self.io.get("session_map", {}).get(session_id, session_id)
        conversations = read_json(self.paths["history"], [])
        if not isinstance(conversations, list):
            raise RPCError("invalid_data_io", "Legacy history must be a conversation list.")
        history = []
        for conversation in conversations:
            if str(conversation.get("conversation_id", conversation.get("user_id"))) != target:
                continue
            for message in sorted(
                conversation.get("chat_history", []), key=lambda m: float(m.get("time", 0))
            ):
                for field, role in (("user_query", "user"), ("bot_answer", "assistant")):
                    content = message.get(field)
                    if (
                        isinstance(content, str)
                        and content.strip()
                        and not (role == "user" and message.get("perception_event"))
                    ):
                        history.append(
                            {"role": role, "content": content.replace("<br>", "\n")[:16000]}
                        )
        return history

    def notes(self) -> list[str]:
        entries = read_json(self.paths["notes"], [])
        if not isinstance(entries, list):
            raise RPCError("invalid_data_io", "Legacy diary must be an entry list.")
        return [
            entry["text"][:16000]
            for entry in sorted(entries, key=lambda e: str(e.get("id", "")))
            if isinstance(entry.get("text"), str)
        ][-20:]

    def memories(self, query: str = "") -> list[str]:
        path = self.paths["memory"]
        if not path.is_file():
            return []
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                if query:
                    rows = db.execute(
                        "SELECT content FROM memory_items WHERE instr(lower(content),lower(?))>0 ORDER BY created_at DESC LIMIT 20",
                        (query,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT content FROM memory_items WHERE type != 'chat' ORDER BY created_at DESC LIMIT 20"
                    ).fetchall()
        except sqlite3.Error:
            raise RPCError("invalid_data_io", "Legacy memory schema is not supported.") from None
        return [row[0][:8000] for row in reversed(rows)]

    def knowledge(self) -> str:
        return dumps(read_json(self.paths["knowledge"], {}))[:12000]

    def relationship(self) -> str:
        return dumps(read_json(self.paths["relationship"], {}))[:12000]

    def project(self, transaction: dict) -> None:
        event = transaction["event"]
        sid = self.io.get("session_map", {}).get(event["session_id"], event["session_id"])
        marker = event["id"]
        timestamp = datetime.fromisoformat(
            event["occurred_at"].upper().replace("Z", "+00:00")
        ).timestamp()
        if transaction["messages"]:
            conversations = read_json(self.paths["history"], [])
            conversation = next(
                (c for c in conversations if str(c.get("conversation_id")) == sid), None
            )
            if conversation is None:
                conversation = {"conversation_id": sid, "user_id": sid, "chat_history": []}
                conversations.append(conversation)
            history = conversation.setdefault("chat_history", [])
            if not any(m.get("rp_core_event_id") == marker for m in history):
                history.insert(
                    0,
                    {
                        "rp_core_event_id": marker,
                        "msg_id": marker,
                        "time": timestamp,
                        "user_query": "\n".join(c.get("text", "") for c in event["content"]),
                        "bot_answer": "<br>".join(
                            a["text"] for a in transaction["result"]["actions"]
                        ),
                        "replied_flag": not bool(transaction["result"]["actions"]),
                        "no_send": transaction["result"]["status"] == "silent",
                        "perception_event": event["kind"] == "perception",
                    },
                )
                write_json(self.paths["history"], conversations)
        for effect in transaction["effects"]:
            if effect["name"] == "note.append":
                notes = read_json(self.paths["notes"], [])
                day = event["occurred_at"][:10]
                page = next((n for n in notes if n.get("id") == day), None)
                if page is None:
                    page = {"id": day, "text": "", "rp_core_effects": []}
                    notes.insert(0, page)
                if effect["id"] not in page.get("rp_core_effects", []):
                    page["text"] = (
                        page.get("text", "") + "\n\n" + effect["arguments"]["text"]
                    ).strip()
                    page.setdefault("rp_core_effects", []).append(effect["id"])
                    write_json(self.paths["notes"], notes)
        memories = [
            effect["arguments"]["text"]
            for effect in transaction["effects"]
            if effect["name"] == "memory.append"
        ]
        memories += transaction.get("maintenance", {}).get("memories", [])
        if memories and self.paths["memory"].is_file():
            with closing(sqlite3.connect(self.paths["memory"])) as db, db:
                for text in memories:
                    content_hash = hashlib.sha256(text.encode()).hexdigest()
                    item_id = "rp-" + content_hash[:40]
                    db.execute(
                        "INSERT OR IGNORE INTO memory_items(id,content,created_at,type,importance_score,metadata_json,content_hash) VALUES (?,?,?,?,?,?,?)",
                        (
                            item_id,
                            text,
                            timestamp,
                            "event",
                            0.5,
                            dumps({"source": "rp-core", "event_id": marker}),
                            content_hash,
                        ),
                    )
                    if (
                        db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone()
                        and not db.execute(
                            "SELECT 1 FROM memory_fts WHERE id=?", (item_id,)
                        ).fetchone()
                    ):
                        db.execute(
                            "INSERT INTO memory_fts(id,content) VALUES (?,?)", (item_id, text)
                        )

    def mark_delivery(self, states: dict) -> None:
        conversations = read_json(self.paths["history"], [])
        changed = False
        for conversation in conversations:
            for message in conversation.get("chat_history", []):
                marker = message.get("rp_core_event_id")
                key = (
                    str(conversation.get("conversation_id", conversation.get("user_id"))),
                    marker,
                )
                if key not in states:
                    continue
                delivered, message_ids = states[key]
                if message.get("replied_flag") != delivered:
                    message["replied_flag"] = delivered
                    changed = True
                if message_ids and message.get("bot_message_id") != message_ids[-1]:
                    message["bot_message_id"] = message_ids[-1]
                    changed = True
        if changed:
            write_json(self.paths["history"], conversations)
