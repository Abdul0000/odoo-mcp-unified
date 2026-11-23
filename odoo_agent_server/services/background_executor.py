from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Any
from threading import BoundedSemaphore

from fastapi import HTTPException

from ..config import settings


# Bound long-running connector work so one busy flow cannot create unbounded
# daemon threads and make jobs.get/search_read sluggish. Two workers let a
# parent safe-deploy job wait for its nested module-deploy job without starving
# the executor; larger deployments can opt in to more.
_EXECUTOR = ThreadPoolExecutor(
    # Accept legacy deployments configured with 1, but never recreate the
    # parent/nested-job starvation fixed by the two-worker default.
    max_workers=max(2, settings.background_max_workers),
    thread_name_prefix="odoo_agent_job",
)


# Bound queued plus running callbacks; reject before retaining another closure.
_CAPACITY = BoundedSemaphore(max(2, settings.background_max_workers) * 4)


def submit_background(callback: Callable[[], Any]) -> Future[Any]:
    """Submit bounded background work and expose submission failures to callers."""
    capacity = _CAPACITY
    if not capacity.acquire(blocking=False):
        raise HTTPException(503, detail={"error_code": "background_capacity_exhausted",
                                       "retry_after_seconds": 5}, headers={"Retry-After": "5"})
    try:
        future = _EXECUTOR.submit(callback)
    except BaseException:
        capacity.release()
        raise
    future.add_done_callback(lambda _: capacity.release())
    return future


def submit_owned_background(callback, store, job_id, *, submit=None, approval=None, approval_id=None):
    """A rejected submission must never leave a job or approval running."""
    from ..schemas import JobStatus

    try:
        return (submit or submit_background)(callback)
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
        try:
            store.finish_job(store.get_job(job_id), JobStatus.failed, {
                "status": "operation_not_started",
                "error_code": detail.get("error_code", "background_submission_failed"),
                "retry_after_seconds": detail.get("retry_after_seconds", 5),
                "error": str(exc), "success_allowed": False, "completion_required": False,
            })
        finally:
            if approval:
                approval.mark_failed(approval_id, "Background worker could not be submitted")
        raise
