# SPDX-License-Identifier: AGPL-3.0-only
"""Generate a private repository containing data and declarative I/O only."""

from pathlib import Path

from rp_core.demo import CHARACTER, EVENT
from rp_core.protocol import RPCError, dumps


def initialize_data(path: Path, *, demo_event: bool = False, driver: str = "native") -> None:
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    files = {
        "io.json": dumps({"schema_version": 1, "driver": driver}),
        "character.json": dumps(CHARACTER),
        "identity.json": dumps(
            {"allowed_user_ids": [], "telegram": {"chat_id": None, "allowed_chat_ids": []}}
        ),
        ".gitignore": ".env\n.env.*\n__pycache__/\n*.rp-tmp\n",
    }
    if demo_event:
        files["events.jsonl"] = dumps(EVENT) + "\n"
    if (path / "io.json").exists():
        raise RPCError("instance_exists", "Refusing to overwrite existing character-data bindings.")
    for name, text in files.items():
        if (path / name).exists():
            continue
        (path / name).write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
