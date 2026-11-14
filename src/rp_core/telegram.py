# SPDX-License-Identifier: AGPL-3.0-only
"""Transport-neutral event normalization shared by the public polling driver."""

import hashlib
from datetime import UTC, datetime


def normalize(update: dict, identity: dict) -> dict | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    actor = message.get("from", {})
    allowed = {str(value) for value in identity.get("allowed_user_ids", [])}
    if str(actor.get("id")) not in allowed or actor.get("is_bot"):
        return None
    chat_id = str(message["chat"]["id"])
    allowed_chats = {
        str(value) for value in identity.get("telegram", {}).get("allowed_chat_ids", [])
    }
    if allowed_chats and chat_id not in allowed_chats:
        return None
    content = []
    if message.get("text"):
        content.append({"type": "text", "text": message["text"]})
    if message.get("location"):
        content.append(
            {"type": "location", **{k: message["location"][k] for k in ("latitude", "longitude")}}
        )
    photo = message.get("photo", [])
    attachment = photo[-1] if photo else message.get("document")
    if attachment:
        content.append(
            {
                "type": "attachment",
                "reference": attachment["file_id"],
                "media_type": attachment.get(
                    "mime_type", "image/jpeg" if photo else "application/octet-stream"
                ),
                "caption": message.get("caption", ""),
            }
        )
    if not content:
        return None
    event_id = (
        "telegram-" + hashlib.sha256(f"{chat_id}:{update['update_id']}".encode()).hexdigest()[:40]
    )
    return {
        "id": event_id,
        "session_id": chat_id,
        "kind": "message",
        "occurred_at": datetime.fromtimestamp(message["date"], UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "actor": {
            "id": str(actor["id"]),
            "display_name": str(actor.get("username") or actor.get("first_name") or actor["id"]),
        },
        "content": content,
        "reply_to": str(message["message_id"]),
        "extensions": {"telegram": {"message_id": str(message["message_id"]), "chat_id": chat_id}},
    }
