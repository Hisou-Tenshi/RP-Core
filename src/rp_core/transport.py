# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, sequential JSONL RPC. No shell or adapter stderr is forwarded."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from rp_core import PROTOCOL
from rp_core.protocol import MAX_FRAME_BYTES, RESPONSE, RPCError, dumps, loads, validate


class Port(Protocol):
    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class JsonlClient:
    def __init__(self, command: list[str], *, cwd: Path | None = None, timeout: float = 120):
        self.timeout = timeout
        self.buffer = bytearray()
        self.closed = False
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            raise RPCError("adapter_unavailable", "Adapter could not be started.") from None
        self.selector = selectors.DefaultSelector()
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def __enter__(self) -> JsonlClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.selector.close()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()

    def _write_frame(self, frame: bytes, deadline: float) -> None:
        assert self.process.stdin is not None
        with selectors.DefaultSelector() as writer:
            writer.register(self.process.stdin, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(frame):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writer.select(remaining):
                    raise RPCError("adapter_timeout", "Adapter request timed out.", retryable=True)
                try:
                    offset += os.write(self.process.stdin.fileno(), frame[offset : offset + 65536])
                except BlockingIOError:
                    continue

    def _read_line(self, deadline: float) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > MAX_FRAME_BYTES:
                    raise RPCError("frame_too_large", "Adapter response exceeds the frame limit.")
                line = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                return line
            if len(self.buffer) > MAX_FRAME_BYTES:
                raise RPCError("frame_too_large", "Adapter response exceeds the frame limit.")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise RPCError("adapter_timeout", "Adapter response timed out.", retryable=True)
            assert self.process.stdout is not None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RPCError(
                    "adapter_disconnected", "Adapter closed its response stream.", retryable=True
                )
            self.buffer.extend(chunk)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise RPCError("adapter_disconnected", "Adapter connection is closed.", retryable=True)
        request_id = uuid.uuid4().hex
        frame = (
            dumps({"protocol": PROTOCOL, "id": request_id, "method": method, "params": params})
            + "\n"
        ).encode()
        if len(frame) > MAX_FRAME_BYTES:
            raise RPCError("frame_too_large", "Request exceeds the frame limit.")
        try:
            deadline = time.monotonic() + self.timeout
            self._write_frame(frame, deadline)
            reply = loads(self._read_line(deadline))
            validate(reply, RESPONSE, code="invalid_adapter_response")
            if reply["id"] != request_id:
                raise RPCError("invalid_adapter_response", "Adapter response ID does not match.")
        except (OSError, BrokenPipeError):
            self.close()
            raise RPCError(
                "adapter_disconnected", "Adapter transport failed.", retryable=True
            ) from None
        except RPCError:
            self.close()
            raise
        if not reply["ok"]:
            # Private adapters may accidentally put credentials or user text in errors.
            # Preserve retryability, but never reflect their free-form error messages.
            retryable = reply["error"]["retryable"]
            raise RPCError(
                "adapter_error", "Private adapter rejected the operation.", retryable=retryable
            )
        return reply["result"]
