"""Monotonic read deadline shared across async dispatch and blocking transports."""
from __future__ import annotations

import time
from contextvars import ContextVar

read_deadline: ContextVar[float | None] = ContextVar("read_deadline", default=None)


def remaining(seconds: float) -> float:
    deadline = read_deadline.get()
    return max(0.0, min(seconds, deadline - time.monotonic())) if deadline is not None else max(0.0, seconds)
