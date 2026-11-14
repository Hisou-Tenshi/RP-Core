# SPDX-License-Identifier: AGPL-3.0-only
"""Public CLI and a reusable JSONL server for privately implemented adapters."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from rp_core import PROTOCOL
from rp_core.engine import Engine
from rp_core.protocol import (
    MANIFEST,
    MAX_FRAME_BYTES,
    REQUEST,
    SCHEMAS,
    RPCError,
    dumps,
    loads,
    response,
    validate,
)
from rp_core.transport import JsonlClient


def serve_requests(
    handler: Callable[[str, dict], dict],
    source: BinaryIO | None = None,
    sink: BinaryIO | None = None,
) -> None:
    source = source if source is not None else sys.stdin.buffer
    sink = sink if sink is not None else sys.stdout.buffer
    while line := source.readline(MAX_FRAME_BYTES + 1):
        request_id = None
        try:
            if len(line) > MAX_FRAME_BYTES:
                while not line.endswith(b"\n"):
                    line = source.readline(MAX_FRAME_BYTES + 1)
                    if not line:
                        break
                raise RPCError("frame_too_large", "Request exceeds the frame limit.")
            request = loads(line)
            if isinstance(request, dict):
                candidate = request.get("id")
                if isinstance(candidate, str) and 0 < len(candidate) <= 256:
                    request_id = candidate
                if request.get("protocol") != PROTOCOL:
                    raise RPCError("unsupported_protocol", "This process supports rp/1.")
            validate(request, REQUEST, code="invalid_request")
            result = handler(request["method"], request["params"])
            if not isinstance(result, dict):
                raise RPCError("internal_error", "Handler returned an invalid response.")
            reply = response(request_id, result=result)
        except RPCError as error:
            reply = response(request_id, error=error)
        except Exception:
            # Never serialize exception text, tracebacks, input, or environment.
            reply = response(request_id, error=RPCError("internal_error", "Operation failed."))
        encoded = (dumps(reply) + "\n").encode()
        if len(encoded) > MAX_FRAME_BYTES:
            encoded = (
                dumps(
                    response(
                        request_id,
                        error=RPCError("frame_too_large", "Response exceeds the frame limit."),
                    )
                )
                + "\n"
            ).encode()
        sink.write(encoded)
        sink.flush()


def open_adapter(manifest_path: Path) -> tuple[JsonlClient, dict]:
    path = manifest_path.resolve()
    try:
        raw = path.read_bytes()
    except OSError:
        raise RPCError("invalid_manifest", "Instance manifest cannot be read.") from None
    if len(raw) > MAX_FRAME_BYTES:
        raise RPCError("invalid_manifest", "Instance manifest exceeds the frame limit.")
    manifest = loads(raw)
    validate(manifest, MANIFEST, code="invalid_manifest")
    config = manifest["adapter"]
    command = [sys.executable if arg == "{python}" else arg for arg in config["command"]]
    return JsonlClient(
        command, cwd=path.parent, timeout=config.get("timeout_seconds", 120)
    ), manifest


def run_instance(
    manifest_path: Path, event_id: str | None = None, *, require_remote: bool = False
) -> dict:
    adapter, manifest = open_adapter(manifest_path)
    counts = {"protocol": PROTOCOL, "processed": 0, "replayed": 0}
    with adapter:
        engine = Engine(adapter)
        declared = engine.negotiate()
        if not {"runtime.next_event", "runtime.finalize"}.issubset(declared["methods"]):
            raise RPCError("invalid_adapter", "Batch mode requires the runtime adapter methods.")
        try:
            for _ in range(manifest.get("max_events", 25)):
                item = adapter.call("runtime.next_event", {"event_id": event_id})
                if item.get("event") is None:
                    break
                method = item.get("method", "event.process")
                if method not in ("event.process", "maintenance.run"):
                    raise RPCError(
                        "invalid_adapter_response", "Queue returned an unsupported operation."
                    )
                result = engine.handle(
                    method, {key: value for key, value in item.items() if key != "method"}
                )
                counts["processed"] += 1
                counts["replayed"] += int(result.get("replayed", False))
                if event_id is not None:
                    break
            # Persist pending actions before any externally visible delivery.
            finalized = adapter.call(
                "runtime.finalize", {"phase": "before_delivery", "success": True}
            )
            _check_durability(finalized, require_remote)
            if "outbox.flush" in declared["methods"]:
                adapter.call("outbox.flush", {})
            finalized = adapter.call(
                "runtime.finalize", {"phase": "after_delivery", "success": True}
            )
            _check_durability(finalized, require_remote)
        except Exception:
            # Do not log a secondary private-adapter error over the original failure.
            with suppress(Exception):
                adapter.call("runtime.finalize", {"phase": "failure", "success": False})
            raise
    return counts


def _check_durability(result: dict, require_remote: bool) -> None:
    if result.get("durable") is not True or (require_remote and result.get("scope") != "remote"):
        raise RPCError(
            "persistence_unavailable",
            "Adapter has not confirmed the required persistence.",
            retryable=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP-Core: public engine, private instance I/O")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Print supported core capabilities")
    schema = commands.add_parser("schema", help="Print a JSON Schema contract")
    schema.add_argument("name", choices=sorted(SCHEMAS))
    for command in ("serve", "run-instance"):
        child = commands.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "run-instance":
            child.add_argument("--event-id")
            child.add_argument("--require-remote-persistence", action="store_true")
    initialize = commands.add_parser(
        "init-instance", help="Create a synthetic offline private-instance example"
    )
    initialize.add_argument("--path", type=Path, required=True)
    commands.add_parser("demo-model", help="Run the offline scripted model; not a live LLM")
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            print(dumps(Engine.capabilities()))
        elif args.command == "schema":
            print(
                dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        **SCHEMAS[args.name],
                    }
                )
            )
        elif args.command == "init-instance":
            from rp_core.demo import initialize_instance

            initialize_instance(args.path)
            print(dumps({"initialized": True, "mode": "offline-demo"}))
        elif args.command == "demo-model":
            from rp_core.demo import model_handler

            serve_requests(model_handler)
        elif args.command == "serve":
            adapter, _ = open_adapter(args.manifest)
            with adapter:
                serve_requests(Engine(adapter).handle)
        else:
            print(
                dumps(
                    run_instance(
                        args.manifest, args.event_id, require_remote=args.require_remote_persistence
                    )
                )
            )
        return 0
    except RPCError as error:
        print(dumps({"ok": False, "error": error.payload()}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "Operation failed.",
                        "retryable": False,
                    },
                }
            ),
            file=sys.stderr,
        )
        return 1
