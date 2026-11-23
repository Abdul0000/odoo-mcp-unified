"""Bounded RPC progress and secret-free request/job correlation.

The request context propagates through asyncio/thread offloading. Detached jobs
may retain correlation, but the closed sink cannot send late RPC notifications.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import re
import threading
import time
import uuid

from opentelemetry import trace

from ..config import settings

logger = logging.getLogger("odoo_mcp.operations")
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False
_context: ContextVar[dict | None] = ContextVar("mcp_operation", default=None)
_metrics_lock = threading.Lock()
_metrics: dict[str, dict] = {}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def correlation() -> dict[str, str]:
    return dict((_context.get() or {}).get("correlation", {}))


def _ids(values):
    return {key: str(value) for key, value in values.items()
            if key in {"task_id", "job_id", "operation_id", "process_id", "workspace_id"}
            and _ID.fullmatch(str(value or ""))}


def phase_event(phase: str, *, origin=None, **ids) -> None:
    """Report only phase labels and opaque IDs; never arguments, paths or logs."""
    context = _context.get()
    if not context and not origin:
        return
    phase = str(phase)
    if not _ID.fullmatch(phase):
        phase = "working"
    safe_ids = _ids(ids)
    logger.info(json.dumps({"event": "operation.phase", **(correlation() or origin or {}), "phase": phase, **safe_ids}))
    sink = (context or {}).get("sink")
    if sink:
        sink.offer(phase)
    span = trace.get_current_span()
    for key, value in safe_ids.items():
        span.set_attribute("odoo." + key, value)


def metrics_snapshot():
    with _metrics_lock:
        return {name: {key: value for key, value in row.items() if key != "samples"} | {
            "recent_p95_ms": sorted(row["samples"])[max(0, int(len(row["samples"]) * .95) - 1)]
            if row["samples"] else 0,
        } for name, row in _metrics.items()}


class ProgressSink:
    def __init__(self, ctx):
        self.ctx = ctx
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=32)
        self.active = True
        self.last = None
        self.count = 0

    def offer(self, phase):
        if self.active:
            try:
                self.loop.call_soon_threadsafe(self._offer, phase)
            except RuntimeError:
                pass  # The originating RPC/event loop already closed.

    def _offer(self, phase):
        if self.active and phase != self.last:
            self.last = phase
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(phase)

    async def run(self):
        while True:
            phase = await self.queue.get()
            self.count += 1
            try:
                await asyncio.wait_for(self.ctx.session.report_progress(
                    self.count, message=phase,
                ), timeout=1)
            except Exception:
                return  # A slow/closed progress channel must not fail a tool.
            await asyncio.sleep(settings.mcp_progress_interval_ms / 1000)


@asynccontextmanager
async def rpc_operation(ctx, name: str):
    request = ctx.request
    supplied = request.headers.get("x-request-id", "") if request else ""
    request_id = supplied if _ID.fullmatch(supplied) else uuid.uuid4().hex[:12]
    current_span = trace.get_current_span().get_span_context()
    trace_id = format(current_span.trace_id, "032x") if current_span.is_valid else uuid.uuid4().hex
    tool = name if _ID.fullmatch(name) else "unknown"
    # The SDK owns token/callback negotiation in each protocol era and makes
    # report_progress a no-op when the caller did not opt in.
    sink = ProgressSink(ctx)
    token = _context.set({"correlation": {"request_id": request_id, "trace_id": trace_id, "tool": tool}, "sink": sink})
    sender = asyncio.create_task(sink.run()) if sink else None
    started = time.monotonic()
    outcome = "ok"
    result_info = {}
    logger.info(json.dumps({"event": "operation.start", **correlation()}))
    try:
        phase_event("request.accepted")
        yield result_info
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        if sink:
            sink.active = False
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
        elapsed = round((time.monotonic() - started) * 1000, 2)
        outcome = "error" if result_info.get("is_error") else outcome
        logger.info(json.dumps({"event": "operation.end", **correlation(), "outcome": outcome,
                                "elapsed_ms": elapsed, **_ids(result_info)}))
        with _metrics_lock:
            bucket = tool if tool in _metrics or len(_metrics) < 128 else "other"
            row = _metrics.setdefault(bucket, {"calls": 0, "errors": 0, "cancelled": 0,
                                              "total_ms": 0, "max_ms": 0, "samples": deque(maxlen=256)})
            row["calls"] += 1
            row["errors"] += outcome == "error"
            row["cancelled"] += outcome == "cancelled"
            row["total_ms"] = round(row["total_ms"] + elapsed, 2)
            row["max_ms"] = max(row["max_ms"], elapsed)
            row["samples"].append(elapsed)
        _context.reset(token)
