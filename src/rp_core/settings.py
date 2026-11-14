# SPDX-License-Identifier: AGPL-3.0-only
"""Public behavior configuration; private files can describe data, never commands."""

from __future__ import annotations

from pathlib import Path

from rp_core.protocol import IDENTIFIER, OBJECT, SCHEMAS, RPCError, loads, obj, validate

IO_SCHEMA = obj(
    {
        "schema_version": {"const": 1},
        "driver": {"enum": ["native", "tenshi-v1"]},
        "files": obj(
            {
                name: IDENTIFIER
                for name in (
                    "character",
                    "identity",
                    "events",
                    "inbox",
                    "state",
                    "history",
                    "notes",
                    "knowledge",
                    "memory",
                    "relationship",
                )
            }
        ),
        "session_map": {"type": "object", "additionalProperties": IDENTIFIER},
    },
    ("schema_version", "driver"),
)
PROVIDER_SCHEMA = obj(
    {
        "driver": {"enum": ["demo", "chat-completions"]},
        "base_url": IDENTIFIER,
        "base_url_env": IDENTIFIER,
        "model": IDENTIFIER,
        "api_key_env": IDENTIFIER,
        "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 180},
        "attempts": {"type": "integer", "minimum": 1, "maximum": 3},
        "parameters": OBJECT,
    },
    ("driver",),
)
RUNTIME_SCHEMA = obj(
    {
        "schema_version": {"const": 1},
        "policy": SCHEMAS["snapshot"]["properties"]["policy"],
        "models": obj(
            {
                "providers": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": PROVIDER_SCHEMA,
                },
                "routes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": IDENTIFIER,
                    },
                },
            },
            ("providers", "routes"),
        ),
        "delivery": obj(
            {
                "driver": {"enum": ["file", "telegram"]},
                "token_env": IDENTIFIER,
                "api_base": IDENTIFIER,
            },
            ("driver",),
        ),
        "persistence": obj(
            {"driver": {"enum": ["local", "git"]}, "branch": IDENTIFIER}, ("driver",)
        ),
        "max_events": {"type": "integer", "minimum": 1, "maximum": 100},
        "ingress": obj(
            {
                "driver": {"enum": ["none", "telegram-poll"]},
                "token_env": IDENTIFIER,
                "api_base": IDENTIFIER,
            },
            ("driver",),
        ),
    },
    ("schema_version", "models", "delivery", "persistence"),
)
IDENTITY_SCHEMA = obj(
    {
        "allowed_user_ids": {
            "type": "array",
            "maxItems": 10000,
            "items": {"type": ["integer", "string"]},
        },
        "telegram": obj(
            {
                "chat_id": {"type": ["integer", "string", "null"]},
                "allowed_chat_ids": {"type": "array", "items": {"type": ["integer", "string"]}},
            }
        ),
    },
    ("allowed_user_ids",),
)
SCHEMAS.update({"data-io": IO_SCHEMA, "runtime": RUNTIME_SCHEMA, "identity": IDENTITY_SCHEMA})
DEFAULT_FILES = {
    "character": "character.json",
    "identity": "identity.json",
    "events": "events.jsonl",
    "inbox": "inbox",
    "state": "state",
    "history": "data/tenshi_log.json",
    "notes": "Tenshi.json",
    "knowledge": "data/knowledge_base.json",
    "memory": "data/tenshi_memory.db",
    "relationship": "data/bond_state.json",
}


def data_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root) or path == root:
        raise RPCError("invalid_data_io", "Data paths must stay inside the private data root.")
    if any(part == ".git" for part in Path(relative).parts):
        raise RPCError("invalid_data_io", "Git internals are not data paths.")
    return path


def read_json(path: Path, default=None):
    if not path.exists() and default is not None:
        return default
    try:
        return loads(path.read_bytes())
    except OSError:
        raise RPCError(
            "invalid_configuration", "A required data or configuration file cannot be read."
        ) from None


def load_io(root: Path, path: Path | None = None) -> dict:
    io = read_json(path or root / "io.json")
    validate(io, IO_SCHEMA, code="invalid_data_io")
    # An explicit allowlist prevents v0.1-style executable adapter manifests.
    if set(io) - {"schema_version", "driver", "files", "session_map", "extensions"}:
        raise RPCError("invalid_data_io", "Private I/O bindings describe data only.")
    files = {**DEFAULT_FILES, **io.get("files", {})}
    if io["driver"] == "tenshi-v1" and "state" not in io.get("files", {}):
        files["state"] = ".rp"
    for name, relative in files.items():
        if name not in DEFAULT_FILES:
            raise RPCError("invalid_data_io", "Unknown private data binding.")
        data_path(root, relative)
    return {**io, "files": files}


def load_runtime(path: Path, _seen: tuple = ()) -> dict:
    path = path.resolve()
    if path in _seen or len(_seen) >= 4:
        raise RPCError(
            "invalid_configuration", "Public configuration inheritance contains a cycle."
        )
    config = read_json(path)
    if "extends" in config:
        base = load_runtime(path.parent / config["extends"], (*_seen, path))

        def merge(left, right):
            for key, value in right.items():
                if key in left and isinstance(left[key], dict) and isinstance(value, dict):
                    merge(left[key], value)
                else:
                    left[key] = value
            return left

        config = merge(base, {key: value for key, value in config.items() if key != "extends"})
    validate(config, RUNTIME_SCHEMA, code="invalid_configuration")
    providers = config["models"]["providers"]
    if "default" not in config["models"]["routes"]:
        raise RPCError("invalid_configuration", "A default model route is required.")
    for route in config["models"]["routes"].values():
        if any(name not in providers for name in route):
            raise RPCError("invalid_configuration", "A model route references an unknown provider.")
    return config
