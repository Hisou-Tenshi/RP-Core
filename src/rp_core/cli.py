# SPDX-License-Identifier: AGPL-3.0-only
"""Public CLI and a reusable JSONL server for privately implemented adapters."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
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
from rp_core.runner import process_queue
from rp_core.settings import load_io
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
    with adapter:
        return process_queue(
            adapter,
            max_events=manifest.get("max_events", 25),
            event_id=event_id,
            require_remote=require_remote,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RP-Core: public engine, private instance I/O")
    commands = parser.add_subparsers(dest="command", required=True)
    full = commands.add_parser("full", help="Complete public compatibility runtime")
    full.add_argument("--data-root", type=Path)
    full.add_argument(
        "--task",
        choices=[
            "preflight",
            "prepare",
            "shadow",
            "selfcheck",
            "probe",
            "reply",
            "perception",
            "force-perception",
            "summary",
            "archive",
            "cleanup",
            "daily-backup",
            "weekly-backup",
            "merge-staging",
        ],
        default="preflight",
    )
    full.add_argument("--shadow-task", choices=["selfcheck", "probe", "reply"], default="selfcheck")
    full.add_argument("--persist", action="store_true")
    full.add_argument(
        "--payload-file", type=Path, help="Private JSON payload for a single full-runtime task"
    )
    full.add_argument("--branch", default="main")
    commands.add_parser("capabilities", help="Print supported core capabilities")
    schema = commands.add_parser("schema", help="Print a JSON Schema contract")
    schema.add_argument("name", choices=sorted(SCHEMAS))
    for command in ("serve", "run-instance"):
        child = commands.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "run-instance":
            child.add_argument("--event-id")
            child.add_argument("--require-remote-persistence", action="store_true")
    runtime = commands.add_parser(
        "run", help="Run public logic against a data-only private repository"
    )
    runtime.add_argument("--data-root", type=Path, required=True)
    runtime.add_argument("--config", type=Path, required=True)
    runtime.add_argument("--io", type=Path)
    runtime.add_argument("--event-id")
    runtime.add_argument("--require-remote-persistence", action="store_true")
    inspect = commands.add_parser(
        "inspect-data", help="Validate I/O paths without creating or migrating state"
    )
    inspect.add_argument("--data-root", type=Path, required=True)
    inspect.add_argument("--io", type=Path)
    initialize = commands.add_parser(
        "init-instance", help="Create a synthetic offline private-instance example"
    )
    initialize.add_argument("--path", type=Path, required=True)
    initialize.add_argument("--demo-event", action="store_true")
    initialize.add_argument("--driver", choices=["native", "tenshi-v1"], default="native")
    commands.add_parser("demo-model", help="Run the offline scripted model; not a live LLM")
    args = parser.parse_args(argv)
    try:
        if args.command == "full":
            from rp_core.full_runtime import preflight, prepare, synthetic_shadow
            from rp_core.full_service import execute, persist

            if args.task == "shadow":
                result = synthetic_shadow(task=args.shadow_task)
            elif args.data_root is None:
                raise RPCError("invalid_params", "This task requires --data-root.")
            elif args.task == "preflight":
                result = preflight(args.data_root)
            elif args.task == "prepare":
                result = prepare(args.data_root)
                if args.persist:
                    persist(args.data_root.resolve(), args.branch)
            elif args.task == "merge-staging":
                from rp_core.full_service import merge_staging

                result = merge_staging(args.data_root)
            else:
                from rp_core.settings import read_json

                payload = read_json(args.payload_file) if args.payload_file else {}
                result = execute(
                    args.data_root, args.task, payload, remote=args.persist, branch=args.branch
                )
            print(dumps(result))
            if result.get("status") in ("retryable_failure", "delivery_unknown"):
                return 1
        elif args.command == "capabilities":
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
            from rp_core.data_init import initialize_data

            initialize_data(args.path, demo_event=args.demo_event, driver=args.driver)
            print(dumps({"initialized": True, "mode": "data-only"}))
        elif args.command == "run":
            from rp_core.runtime import run_data

            result = run_data(
                args.data_root,
                args.config,
                io_path=args.io,
                event_id=args.event_id,
                require_remote=args.require_remote_persistence,
            )
            print(dumps(result))
            if result.get("quarantined"):
                # An isolated event stays visible for manual reconciliation, but it
                # must not keep an otherwise healthy scheduled run permanently red.
                print(
                    f"::warning::{result['quarantined']} private event(s) isolated for reconciliation",
                    file=sys.stderr,
                )
            if result.get("failed"):
                return 1
        elif args.command == "inspect-data":
            io = load_io(args.data_root, args.io)
            print(dumps({"driver": io["driver"], "bindings_valid": True, "modified": False}))
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
