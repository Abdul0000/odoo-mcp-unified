from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..schemas import CommandResult, JobStatus, OdooFailureDiagnosis, OdooResult
from .store_service import JsonStore

_RESUMABLE_SAFE_DEPLOY_RESULTS = {
    "deploy_failed_or_timeout",
    "deploy_running",
    "module_upgrade_running",
    "module_upgrade_timeout",
    "deploy_code_mismatch",
    "github_pushed_deploy_pending",
}
_ACTIVE_JOB_STATUSES = {JobStatus.queued, JobStatus.running, JobStatus.warning}

# If the latest safe deploy failed at or before proving that code reached the
# remote branch, manual Odoo.sh install/upgrade tools must not run. They would
# only upgrade the database against stale source code. Keep this list broad and
# include legacy/current step names used across connector versions.
_PRE_PUSH_FAILED_STEPS = {
    "resolve_project",
    "git_status",
    "git_root_verify",
    "safe_deploy_path_guard",
    "deploy_path_gate",
    "safe_deploy_path_gate",
    "git_diff",
    "git_changed_files",
    "deploy_dirty_path_gate",
    "qa_self_run",
    "verify_against_core_pre_push",
    "git_configure_identity",
    "git_validate_target",
    "git_commit",
    "git_head_sha",
    "git_head_changed_files",
    "git_head_commit_path_gate",
    "git_commit_content_guard",
    "git_push",
    "git_remote_head_verify",
    "git_remote_sha_guard",
}



def _same_project(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return str(left).rstrip("/") == str(right).rstrip("/")


def _job_result_status(job: Any) -> str | None:
    result = getattr(job, "result", None)
    if isinstance(result, dict):
        value = result.get("status")
        return str(value) if value else None
    return None


def _parse_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _job_last_activity_epoch(job: Any) -> float | None:
    latest = _parse_epoch(getattr(job, "created_at", None))
    for step in getattr(job, "steps", None) or []:
        for key in ("finished_at", "started_at"):
            parsed = _parse_epoch(getattr(step, key, None))
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
    return latest


def mark_stale_safe_deploy_running_job(job: Any, store: JsonStore | None = None):
    """Convert an orphaned running safe-deploy job into a resumable warning.

    Safe deploy workers are in-process threads. After a process restart their
    JobRecord can remain ``running`` forever. Only mark a job stale when no step
    activity has been recorded within the configured threshold.
    """
    if getattr(job, "job_type", None) != "safe_deploy_module" or job.status not in {JobStatus.queued, JobStatus.running}:
        return job
    threshold = max(int(settings.safe_deploy_stale_running_seconds or 0), 0)
    if threshold <= 0:
        return job
    last_activity = _job_last_activity_epoch(job)
    if last_activity is None or time.time() - last_activity <= threshold:
        return job
    store = store or JsonStore()
    request = job.request or {}
    result = {
        "status": "stale_running_job",
        "message": (
            "The safe_deploy_module job was left running without recent activity. "
            "The background worker is presumed gone; resume the stored pipeline state."
        ),
        "module_name": request.get("module_name"),
        "project_path": request.get("project_path"),
        "last_activity_epoch": last_activity,
        "stale_after_seconds": threshold,
        "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}},
    }
    return store.finish_job(job, JobStatus.warning, result)


def find_blocking_safe_deploy(
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    client_name: str | None = None,
    limit: int = 100,
):
    """Return an active/resumable safe_deploy_module job that owns deploy flow.

    Agents were able to start workflow.safe_deploy_module and then bypass it with
    raw git.commit/git.push/odoosh.module_update calls.  This guard makes the
    workflow state authoritative: once a safe deploy exists and is running or
    resumable, lower-level deploy tools must fail closed and point to resume.
    """
    store = JsonStore()
    candidates = []
    state = store.load_session_state(client_name)
    saved_job_id = state.get("last_safe_deploy_job_id")
    if isinstance(saved_job_id, str) and saved_job_id.strip():
        try:
            candidates.append(store.get_job(saved_job_id.strip()))
        except Exception:
            pass
    candidates.extend(job for job in store.list_jobs(limit=limit) if job.job_type == "safe_deploy_module")

    seen: set[str] = set()
    for job in candidates:
        if job.id in seen or job.job_type != "safe_deploy_module":
            continue
        seen.add(job.id)
        job = mark_stale_safe_deploy_running_job(job, store)
        request = job.request or {}
        req_project = request.get("project_path") or state.get("project_path")
        req_module = request.get("module_name") or state.get("module_name")
        if project_path and req_project and not _same_project(project_path, str(req_project)):
            continue
        if module_name and req_module and str(req_module) != module_name:
            continue
        result_status = _job_result_status(job)
        if job.status in _ACTIVE_JOB_STATUSES or result_status in _RESUMABLE_SAFE_DEPLOY_RESULTS:
            return job
    return None


def find_latest_confirmed_safe_deploy(
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    limit: int = 100,
):
    """Return the latest matching safe deploy only when it already confirmed success."""
    for job in JsonStore().list_jobs(limit=limit):
        if job.job_type != "safe_deploy_module":
            continue
        request = job.request or {}
        if project_path and request.get("project_path") and not _same_project(project_path, str(request["project_path"])):
            continue
        if module_name and request.get("module_name") and str(request["module_name"]) != module_name:
            continue
        result = job.result if isinstance(job.result, dict) else {}
        confirmed = bool(result.get("deploy_confirmed") or result.get("already_deployed_confirmed"))
        return job if job.status == JobStatus.passed and confirmed else None
    return None


def _confirmed_safe_deploy_payload(action: str, job: Any) -> dict[str, Any]:
    result = job.result if isinstance(job.result, dict) else {}
    return {
        "status": "already_deploy_confirmed",
        "message": (
            f"Skipped redundant {action}: workflow.safe_deploy_module already confirmed this module. "
            "Stop calling deploy tools and return the final summary immediately."
        ),
        "job_id": job.id,
        "job_status": getattr(job.status, "value", str(job.status)),
        "module_name": (job.request or {}).get("module_name"),
        "deploy_confirmed": bool(result.get("deploy_confirmed")),
        "already_deployed_confirmed": bool(result.get("already_deployed_confirmed")),
        "completion_required": False,
        "success_allowed": True,
        "tool_exit_allowed": True,
        "next_call": None,
    }


def safe_deploy_block_payload(action: str, job: Any) -> dict[str, Any]:
    request = job.request or {}
    job_status = getattr(job.status, "value", str(job.status))
    active = job.status in {JobStatus.queued, JobStatus.running}
    next_call = (
        {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
        if active
        else {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    )
    recovery = "Use jobs.get to poll the active job" if active else "Use workflow.resume_job"
    return {
        "status": "safe_deploy_active",
        "message": (
            f"Manual {action} is blocked because workflow.safe_deploy_module already owns "
            "commit → push → Odoo.sh polling → module upgrade for this module. "
            f"{recovery}; do not replay commit/push/update."
        ),
        "job_id": job.id,
        "job_status": job_status,
        "module_name": request.get("module_name"),
        "project_path": request.get("project_path"),
        "completion_required": active,
        "success_allowed": False,
        "tool_exit_allowed": not active,
        "poll_after_seconds": 1 if active else None,
        "next_call": next_call,
    }


def _job_failed_step(job: Any) -> str | None:
    result = getattr(job, "result", None)
    if isinstance(result, dict):
        value = result.get("failed_step")
        return str(value) if value else None
    return None


def find_latest_pre_push_failed_safe_deploy(
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    client_name: str | None = None,
    limit: int = 250,
):
    """Return the newest safe_deploy job that failed before code was pushed.

    This deliberately checks terminal failed jobs too, not just active or
    resumable jobs. A later manual Odoo.sh upgrade after such a failed safe
    deploy is unsafe because the intended local changes were never proven to be
    committed/pushed to the remote branch.
    """
    store = JsonStore()
    state = store.load_session_state(client_name)
    candidates = [job for job in store.list_jobs(limit=limit) if job.job_type == "safe_deploy_module"]
    for job in candidates:
        request = job.request or {}
        req_project = request.get("project_path") or state.get("project_path")
        req_module = request.get("module_name") or state.get("module_name")
        if project_path and req_project and not _same_project(project_path, str(req_project)):
            continue
        if module_name and req_module and str(req_module) != module_name:
            continue
        failed_step = _job_failed_step(job)
        if job.status == JobStatus.failed and failed_step in _PRE_PUSH_FAILED_STEPS:
            return job
        # The most recent job for this project/module is authoritative. If it
        # does not match a pre-push failure, do not scan older failures and block
        # a newer successful/resumable deploy attempt.
        return None
    return None


def safe_deploy_pre_push_failure_payload(action: str, job: Any) -> dict[str, Any]:
    request = job.request or {}
    failed_step = _job_failed_step(job) or "unknown"
    message = (
        "The last safe_deploy_module attempt failed before code was pushed "
        f"(failed_step={failed_step}). No new code exists on the remote branch "
        "beyond what was already deployed. Fix the underlying issue and re-run "
        "workflow.safe_deploy_module; do not call the manual upgrade tool directly."
    )
    return {
        "status": "safe_deploy_failed_before_push",
        "message": message,
        "failed_step": failed_step,
        "job_id": job.id,
        "job_status": getattr(job.status, "value", str(job.status)),
        "module_name": request.get("module_name"),
        "project_path": request.get("project_path"),
        "next_call": {"tool": "workflow.safe_deploy_module", "arguments": {"project_path": request.get("project_path"), "module_name": request.get("module_name")}},
    }


def safe_deploy_command_block(
    action: str,
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    cwd: str | None = None,
) -> CommandResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if job:
        payload = safe_deploy_block_payload(action, job)
        return CommandResult(
            command=[action],
            cwd=cwd or project_path or "workflow.safe_deploy_module",
            exit_code=2,
            stdout=json.dumps(payload),
            stderr=payload["message"],
        )
    failed_job = find_latest_pre_push_failed_safe_deploy(project_path=project_path, module_name=module_name)
    if failed_job:
        payload = safe_deploy_pre_push_failure_payload(action, failed_job)
        return CommandResult(
            command=[action],
            cwd=cwd or project_path or "workflow.safe_deploy_module",
            exit_code=2,
            stdout=json.dumps(payload),
            stderr=payload["message"],
        )
    return None


def safe_deploy_shell_block(action: str, *, module_name: str | None = None, project_path: str | None = None) -> OdooResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if job:
        payload = safe_deploy_block_payload(action, job)
        return OdooResult(
            status="failed",
            error=payload["message"],
            result=payload,
        )
    failed_job = find_latest_pre_push_failed_safe_deploy(project_path=project_path, module_name=module_name)
    if failed_job:
        payload = safe_deploy_pre_push_failure_payload(action, failed_job)
        return OdooResult(
            status="failed",
            error=payload["message"],
            result=payload,
        )
    confirmed_job = find_latest_confirmed_safe_deploy(project_path=project_path, module_name=module_name)
    if confirmed_job:
        payload = _confirmed_safe_deploy_payload(action, confirmed_job)
        return OdooResult(status="ok", result=payload)
    return None


_REPEATED_FAILURE_MARKERS = re.compile(
    r"\b(still|again|same issue|same problem|not fixed|not resolved|persists?|continues?|remaining)\b",
    re.IGNORECASE,
)


def code_change_issue_fingerprint(
    *,
    project_path: str,
    module_name: str,
    task_description: str,
    task_type: str | None = None,
    intent: str | None = None,
    target_hint: str | None = None,
) -> str:
    """Build a stable, generic issue key without task- or module-specific rules."""
    anchor = (target_hint or "").strip().lower()
    if not anchor:
        anchor = _REPEATED_FAILURE_MARKERS.sub(" ", task_description or "")
        anchor = re.sub(r"[^a-z0-9_.-]+", " ", anchor.lower()).strip()[:500]
    payload = "|".join((
        str(Path(project_path).expanduser()),
        module_name.strip().lower(),
        (task_type or "auto").strip().lower(),
        (intent or "").strip().lower(),
        anchor,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def repeated_failure_guard_status(
    *,
    client_name: str | None,
    fingerprint: str,
    task_description: str,
    investigation_evidence: list[str] | None = None,
    investigation_diagnosis: OdooFailureDiagnosis | dict[str, Any] | None = None,
    trusted_evidence_ids: list[str] | set[str] | None = None,
    functional_verification_passed: bool = False,
) -> dict[str, Any]:
    """Evaluate the generic two-attempt escalation guard.

    Existing workflows are unchanged unless the request explicitly indicates
    that the symptom persists and two prior deploy jobs for the same issue have
    completed successfully without functional verification. A third repair
    requires a custom-code diagnosis linked to trusted server evidence; free
    text alone cannot unlock another write/deploy cycle.
    """
    store = JsonStore()
    state = store.load_session_state(client_name)
    guard_state = dict(state.get("code_change_failure_guard") or {})
    entry = dict(guard_state.get(fingerprint) or {})

    if functional_verification_passed:
        guard_state.pop(fingerprint, None)
        store.save_session_state({"code_change_failure_guard": guard_state}, client_name=client_name)
        return {"blocked": False, "passed_attempts": 0, "reset": True}

    passed_attempts = 0
    valid_job_ids: list[str] = []
    for job_id in entry.get("job_ids") or []:
        try:
            job = store.get_job(str(job_id))
        except Exception:
            continue
        result = job.result if isinstance(job.result, dict) else {}
        if job.status == JobStatus.passed and bool(result.get("deploy_confirmed")):
            passed_attempts += 1
            valid_job_ids.append(str(job_id))
        elif job.status in _ACTIVE_JOB_STATUSES:
            valid_job_ids.append(str(job_id))

    entry["job_ids"] = valid_job_ids[-5:]
    guard_state[fingerprint] = entry
    store.save_session_state({"code_change_failure_guard": guard_state}, client_name=client_name)

    recurrence_reported = bool(_REPEATED_FAILURE_MARKERS.search(task_description or ""))
    evidence = [str(item).strip() for item in (investigation_evidence or []) if str(item).strip()]
    diagnosis = (
        OdooFailureDiagnosis.model_validate(investigation_diagnosis)
        if investigation_diagnosis is not None
        else None
    )
    trusted_ids = {str(item) for item in (trusted_evidence_ids or []) if str(item)}
    linked_trusted_ids = (
        sorted(set(diagnosis.evidence_ids) & trusted_ids)
        if diagnosis is not None
        else []
    )
    diagnosis_authorizes_repair = bool(
        diagnosis is not None
        and diagnosis.origin == "custom_code"
        and diagnosis.next_strategy == "repair_custom_code"
        and bool(diagnosis.debug_session_id)
        and diagnosis.crux.strip()
        and diagnosis.hypothesis.strip()
        and linked_trusted_ids
    )
    blocked = recurrence_reported and passed_attempts >= 2 and not diagnosis_authorizes_repair
    return {
        "blocked": blocked,
        "passed_attempts": passed_attempts,
        "recurrence_reported": recurrence_reported,
        "investigation_evidence_present": bool(evidence),
        "diagnosis_present": diagnosis is not None,
        "diagnosis_origin": diagnosis.origin if diagnosis is not None else None,
        "trusted_evidence_ids": linked_trusted_ids,
        "diagnosis_authorizes_repair": diagnosis_authorizes_repair,
    }


def record_code_change_deploy_attempt(
    *, client_name: str | None, fingerprint: str, job_id: str | None
) -> None:
    if not job_id:
        return
    store = JsonStore()
    state = store.load_session_state(client_name)
    guard_state = dict(state.get("code_change_failure_guard") or {})
    entry = dict(guard_state.get(fingerprint) or {})
    job_ids = [str(value) for value in (entry.get("job_ids") or []) if value]
    if job_id not in job_ids:
        job_ids.append(job_id)
    entry["job_ids"] = job_ids[-5:]
    guard_state[fingerprint] = entry
    store.save_session_state({"code_change_failure_guard": guard_state}, client_name=client_name)
