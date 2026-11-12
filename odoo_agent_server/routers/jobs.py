from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import AuditEvent, JobRecord, JobRecordWithWatchdog
from ..services.store_service import JsonStore
from ..services.workflow_watchdog_service import WorkflowWatchdogService

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[AuthDependency])


@router.get("", response_model=list[JobRecord])
def list_jobs(limit: int = 100) -> list[JobRecord]:
    return JsonStore().list_jobs(limit=limit)


@router.get("/audit/events", response_model=list[AuditEvent])
def list_audit(limit: int = 200) -> list[AuditEvent]:
    return JsonStore().list_audit(limit=limit)


@router.get("/{job_id}", response_model=JobRecordWithWatchdog)
def get_job(
    job_id: str,
    wait_for_result: bool = True,
    wait_timeout_seconds: int = 600,
    poll_interval_seconds: float = 1.0,
) -> JobRecordWithWatchdog:
    """Return one job with explicit deploy-success safety gates.

    This endpoint intentionally uses a short bounded wait by default so MCP/UI
    clients stay responsive. Callers must poll again while the job is running;
    running is never a deploy-success signal. For no-local-changes /
    already-existing claims, only say "already added", "already deployed", or
    "already live" when ``verified_already_deployed`` /
    ``already_deployed_confirmed`` is true.
    """
    store = JsonStore()
    watchdog = WorkflowWatchdogService()
    if wait_for_result:
        return watchdog.annotate_job(store.wait_job(job_id, wait_timeout_seconds, poll_interval_seconds))
    return watchdog.annotate_job(store.get_job(job_id))
