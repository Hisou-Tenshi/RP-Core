# SPDX-License-Identifier: AGPL-3.0-only
"""One queue lifecycle shared by public data runtimes and optional RPC clients."""

from contextlib import suppress

from rp_core import PROTOCOL
from rp_core.engine import Engine
from rp_core.protocol import RPCError


def process_queue(
    adapter, *, max_events: int = 25, event_id: str | None = None, require_remote: bool = False
) -> dict:
    counts = {"protocol": PROTOCOL, "processed": 0, "replayed": 0}
    engine = Engine(adapter)
    methods = engine.negotiate()["methods"]
    if not {"runtime.next_event", "runtime.finalize"}.issubset(methods):
        raise RPCError("invalid_adapter", "Batch mode requires the runtime methods.")

    def persist(phase):
        result = adapter.call("runtime.finalize", {"phase": phase, "success": True})
        if result.get("durable") is not True or (
            require_remote and result.get("scope") != "remote"
        ):
            raise RPCError(
                "persistence_unavailable", "Required persistence was not confirmed.", retryable=True
            )

    try:
        for _ in range(max_events):
            item = adapter.call("runtime.next_event", {"event_id": event_id})
            if item.get("event") is None:
                break
            method = item.get("method", "event.process")
            if method not in ("event.process", "maintenance.run"):
                raise RPCError(
                    "invalid_adapter_response", "Queue returned an unsupported operation."
                )
            result = engine.handle(method, {k: v for k, v in item.items() if k != "method"})
            counts["processed"] += 1
            counts["replayed"] += int(result.get("replayed", False))
            if event_id is not None:
                break
        persist("before_delivery")
        if "outbox.flush" in methods:
            adapter.call("outbox.flush", {})
        persist("after_delivery")
    except Exception:
        with suppress(Exception):
            adapter.call("runtime.finalize", {"phase": "failure", "success": False})
        raise
    return counts
