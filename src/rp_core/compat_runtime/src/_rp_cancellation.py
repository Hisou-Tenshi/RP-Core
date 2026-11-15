# SPDX-License-Identifier: AGPL-3.0-only
"""Cooperative cancellation shared by nested model calls, callbacks and tools.

An in-flight SDK request may finish after cancellation. It cannot start another
request, publish a stream callback or dispatch a tool after its scope expires.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time


class CallCancelled(BaseException):
    """Unwind an abandoned worker through ordinary provider retry handlers."""


@dataclass
class CallScope:
    deadline: float | None = None
    parent: "CallScope | None" = None
    cancelled: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if self.parent is not None:
            self.parent.check()
        if self.cancelled.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        ):
            raise CallCancelled()


current_scope = ContextVar("rp_call_scope", default=None)
_tools_disabled = ContextVar("rp_tools_disabled", default=False)


def ensure_active():
    scope = current_scope.get()
    if scope is not None:
        scope.check()


def remaining_timeout(configured):
    """Clamp SDK network timeouts to every active parent call's deadline."""
    ensure_active()
    remaining = float(configured)
    scope = current_scope.get()
    while scope is not None:
        if scope.deadline is not None:
            remaining = min(remaining, max(0.001, scope.deadline - time.monotonic()))
        scope = scope.parent
    return remaining


def ensure_tools_allowed():
    ensure_active()
    if _tools_disabled.get():
        raise CallCancelled()


@contextmanager
def tools_disabled():
    token = _tools_disabled.set(True)
    try:
        yield
    finally:
        _tools_disabled.reset(token)
