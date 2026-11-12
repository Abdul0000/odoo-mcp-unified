from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    JobRecord,
    JobStatus,
    JobStep,
    ClientOnlyRequest,
    OdooModuleRequest,
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShQueryRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
)
from ..services.approval_service import ApprovalService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_command_block
from ..services.background_executor import submit_background
from ..services.git_service import GitService
from ..services.client_gate_service import ClientGateService
from ..services.client_resolver_service import ClientResolverService

router = APIRouter(prefix="/odoosh", tags=["odoosh"], dependencies=[AuthDependency])


def _validated_client_for_payload(payload: object, action: str):
    client_name = getattr(payload, "client_name", None)
    ClientGateService().require_validated_client(client_name, action)
    if client_name:
        client = ClientResolverService().resolve_client(client_name=client_name, require_complete=True)
        return client
    return None


def _service_for_client(client):
    if client is None:
        return OdooShService()
    try:
        return OdooShService(client)
    except TypeError:
        # Tests and older extensions sometimes monkeypatch OdooShService with a
        # zero-argument factory. Keep compatibility while real service still
        # receives the selected client.
        return OdooShService()


def _run(name: str, callback) -> CommandResult:
    try:
        return callback()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc




def _resolve_manual_project_path(payload) -> str | None:
    """Resolve the checkout path needed for local-vs-remote SHA proof."""
    explicit = getattr(payload, "project_path", None)
    if explicit:
        return str(explicit)
    store = JsonStore()
    state = store.load_session_state(client_name=getattr(payload, "client_name", None))
    state_project = state.get("project_path")
    if isinstance(state_project, str) and state_project.strip():
        return state_project.strip()
    module_name = getattr(payload, "module_name", None)
    for job in store.list_jobs(limit=100):
        if job.job_type != "safe_deploy_module":
            continue
        request = job.request or {}
        if module_name and request.get("module_name") != module_name:
            continue
        job_project = request.get("project_path")
        if isinstance(job_project, str) and job_project.strip():
            return job_project.strip()
    return None


def _manual_odoosh_pushed_sha_guard(action: str, payload, cwd: str = "odoosh:ssh") -> tuple[CommandResult | None, str | None]:
    """Fail closed unless manual Odoo.sh upgrades can prove code was pushed.

    expected_sha remains the explicit safest path: the callback waits for that
    SHA to be deployed before running odoo-bin. If the caller omits it, prove
    that the relevant local checkout's HEAD is already the remote branch HEAD;
    otherwise manual install/upgrade could run against stale Odoo.sh source.
    """
    expected_sha = (getattr(payload, "expected_sha", None) or "").strip()
    if expected_sha:
        return None, expected_sha

    project_path = _resolve_manual_project_path(payload)
    if not project_path:
        message = (
            "Refusing to deploy: expected_sha or project_path is required so the connector can prove "
            "local HEAD is pushed to the remote branch. Run workflow.safe_deploy_module instead."
        )
        return CommandResult(command=[action, "push-guard"], cwd=cwd, exit_code=2, stderr=message), None

    git = GitService()
    local = git.head_sha(project_path)
    if local.exit_code != 0:
        message = "Refusing to deploy: could not resolve local HEAD. Run workflow.safe_deploy_module first."
        return CommandResult(
            command=[action, "git", "rev-parse", "HEAD"],
            cwd=project_path,
            exit_code=2,
            stdout=local.stdout,
            stderr=f"{message}\n{local.stderr}".strip(),
        ), None
    local_sha = (local.stdout or "").strip()
    remote = git.remote_head_sha(
        project_path,
        getattr(payload, "remote", "origin") or "origin",
        getattr(payload, "branch", None),
        60,
    )
    if remote.exit_code != 0:
        message = "Refusing to deploy: could not resolve remote branch HEAD. Run workflow.safe_deploy_module or git.push first."
        return CommandResult(
            command=[action, "git", "remote-head-sha"],
            cwd=project_path,
            exit_code=2,
            stdout=remote.stdout,
            stderr=f"{message}\n{remote.stderr}".strip(),
        ), None
    remote_sha = (remote.stdout or "").strip()
    if not local_sha or not remote_sha or local_sha != remote_sha:
        message = (
            "Refusing to deploy: local commits are not pushed to remote. "
            f"Local HEAD={local_sha or 'unknown'}, Remote HEAD={remote_sha or 'unknown'}. "
            "Run workflow.safe_deploy_module or git.push first."
        )
        return CommandResult(
            command=[action, "git", "remote-head-verify"],
            cwd=project_path,
            exit_code=2,
            stdout=json.dumps({"project_path": project_path, "local_head": local_sha, "remote_head": remote_sha}),
            stderr=message,
        ), None
    return None, local_sha

def _background_command_job(
    name: str,
    request: dict,
    callback: Callable[[], CommandResult],
    approval_id: str | None = None,
    wait_for_result: bool = True,
    wait_timeout_seconds: int = 20,
    poll_interval_seconds: float = 2.0,
) -> CommandResult:
    """Start a long Odoo.sh SSH command in the background and return immediately.

    Render and many MCP HTTP clients can drop long requests at about 30 seconds.
    This wrapper persists the command as a job so MCP agents can poll jobs.get.
    """
    store = JsonStore()
    approval = ApprovalService()
    job = store.create_job(JobRecord(job_type=name, status=JobStatus.running, request=request))

    def _step(status_: str, result: CommandResult | None = None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(
            current,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if result else None,
                error=error,
            ),
        )

    def _bg() -> None:
        try:
            result = callback()
            _step("passed" if result.exit_code == 0 else "failed", result)
            current = store.get_job(job.id)
            store.finish_job(current, JobStatus.passed if result.exit_code == 0 else JobStatus.failed, result.model_dump(mode="json"))
            approval.finalize_command(approval_id, result.exit_code, f"{name} succeeded", f"{name} failed")
            store.log_audit(name, "ok" if result.exit_code == 0 else "failed", request.get("module_name", name), {"job_id": job.id})
        except Exception as exc:  # noqa: BLE001
            _step("failed", error=str(exc))
            current = store.get_job(job.id)
            store.finish_job(current, JobStatus.failed, {"error": str(exc)})
            approval.mark_failed(approval_id, f"{name} failed")
            store.log_audit(name, "failed", request.get("module_name", name), {"job_id": job.id, "error": str(exc)})

    submit_background(_bg)
    if wait_for_result:
        waited = store.wait_job(job.id, wait_timeout_seconds, poll_interval_seconds)
        payload = {
            "job_id": waited.id,
            "status": waited.status.value,
            "finished": waited.status not in {JobStatus.queued, JobStatus.running},
            "job": waited.model_dump(mode="json"),
        }
        if not payload["finished"]:
            payload.update({
                "message": (
                    f"Command still running after {wait_timeout_seconds}s server-side wait. "
                    "This is not confirmed success. Call jobs.get until terminal passed/failed."
                ),
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
                "poll_after_seconds": 5,
                "next_call": {
                    "tool": "jobs.get",
                    "arguments": {
                        "job_id": waited.id,
                        "wait_for_result": True,
                        "wait_timeout_seconds": 20,
                        "poll_interval_seconds": 2.0,
                    },
                },
            })
        return CommandResult(
            command=[name, "background"],
            cwd="odoosh:ssh",
            exit_code=0 if waited.status in {JobStatus.queued, JobStatus.running, JobStatus.passed} else 1,
            stdout=json.dumps(payload),
            stderr="" if waited.status != JobStatus.failed else json.dumps(waited.result or {}),
        )
    return CommandResult(
        command=[name, "background"],
        cwd="odoosh:ssh",
        exit_code=0,
        stdout=json.dumps({
            "job_id": job.id,
            "status": "running",
            "finished": False,
            "message": "Command started in background. This is not confirmed success. Poll jobs.get until terminal passed/failed.",
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 5,
            "next_call": {
                "tool": "jobs.get",
                "arguments": {
                    "job_id": job.id,
                    "wait_for_result": True,
                    "wait_timeout_seconds": 20,
                    "poll_interval_seconds": 2.0,
                },
            },
        }),
    )


@router.post("/query")
def odoosh_query(payload: OdooShQueryRequest):
    """Read-only search_read via Odoo.sh SSH/Odoo shell. Does not require Odoo shell login."""
    client = None
    if payload.client_name:
        client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True)
    return OdooShService(client).search_read(
        payload.model,
        payload.domain,
        payload.fields,
        payload.limit,
        payload.offset,
        payload.order,
        payload.context,
        payload.timeout_seconds,
    )


@router.post("/health", response_model=CommandResult)
def odoosh_health(payload: ClientOnlyRequest | None = None) -> CommandResult:
    payload = payload or ClientOnlyRequest()
    client = _validated_client_for_payload(payload, "odoosh.health")
    return _run("odoosh.health", lambda: _service_for_client(client).health())


@router.post("/git-status", response_model=CommandResult)
def odoosh_git_status(payload: ClientOnlyRequest | None = None) -> CommandResult:
    payload = payload or ClientOnlyRequest()
    client = _validated_client_for_payload(payload, "odoosh.git_status")
    return _run("odoosh.git_status", lambda: _service_for_client(client).git_status())


@router.post("/deployed-sha", response_model=CommandResult)
def odoosh_deployed_sha(payload: ClientOnlyRequest | None = None) -> CommandResult:
    payload = payload or ClientOnlyRequest()
    client = _validated_client_for_payload(payload, "odoosh.deployed_sha")
    return _run("odoosh.deployed_sha", lambda: _service_for_client(client).deployed_sha())


@router.post("/wait-for-commit", response_model=CommandResult)
def odoosh_wait_for_commit(payload: OdooShWaitForCommitRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.wait_for_commit")
    return _run("odoosh.wait_for_commit", lambda: _service_for_client(client).wait_for_commit(payload.expected_sha, payload.timeout_seconds, payload.interval_seconds))


@router.post("/check-commit", response_model=CommandResult)
def odoosh_check_commit(payload: OdooShCheckCommitRequest) -> CommandResult:
    """Non-blocking single check — returns immediately with deployed=true/false in stdout JSON."""
    client = _validated_client_for_payload(payload, "odoosh.check_commit")
    return _run("odoosh.check_commit", lambda: _service_for_client(client).check_commit_deployed(payload.expected_sha))


@router.post("/verify-module-manifest", response_model=CommandResult)
def odoosh_verify_module_manifest(payload: OdooShVerifyManifestRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.verify_module_manifest")
    return _run("odoosh.verify_module_manifest", lambda: _service_for_client(client).verify_module_manifest(payload.module_name))


@router.post("/log", response_model=CommandResult)
def odoosh_log(payload: OdooShLogRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.log")
    return _run("odoosh.log", lambda: _service_for_client(client).tail_log(payload.log_name, payload.tail_lines))


@router.post("/grep-errors", response_model=CommandResult)
def odoosh_grep_errors(payload: OdooShCommandRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.grep_errors")
    return _run("odoosh.grep_errors", lambda: _service_for_client(client).grep_errors(payload.tail_lines))


@router.post("/module-install-ssh", response_model=CommandResult)
def odoosh_module_install_ssh(payload: OdooModuleRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.module_install_ssh")
    blocked = safe_deploy_command_block(
        "odoosh.module_install_ssh",
        project_path=payload.project_path,
        module_name=payload.module_name,
        cwd="odoosh:ssh",
    )
    if blocked:
        return blocked
    push_guard, resolved_sha = _manual_odoosh_pushed_sha_guard("odoosh.module_install_ssh", payload)
    if push_guard:
        return push_guard
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_install_ssh", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = _service_for_client(client)

    def _callback() -> CommandResult:
        if resolved_sha:
            wait = service.wait_for_commit(resolved_sha, payload.deploy_guard_timeout_seconds, None)
            if wait.exit_code != 0:
                return wait
        return service.module_install_ssh(payload.module_name, 1800)

    return _background_command_job(
        "odoosh.module_install_ssh",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        _callback,
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )


@router.post("/module-deploy-ssh", response_model=CommandResult)
def odoosh_module_deploy_ssh(payload: OdooShModuleUpdateRequest) -> CommandResult:
    """SSH smart install/upgrade. Background job: poll jobs.get."""
    client = _validated_client_for_payload(payload, "odoosh.module_deploy_ssh")
    blocked = safe_deploy_command_block(
        "odoosh.module_deploy_ssh",
        project_path=payload.project_path,
        module_name=payload.module_name,
        cwd="odoosh:ssh",
    )
    if blocked:
        return blocked
    push_guard, resolved_sha = _manual_odoosh_pushed_sha_guard("odoosh.module_deploy_ssh", payload)
    if push_guard:
        return push_guard
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_deploy_ssh", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = _service_for_client(client)

    def _callback() -> CommandResult:
        if resolved_sha:
            wait = service.wait_for_commit(resolved_sha, payload.timeout_seconds, None)
            if wait.exit_code != 0:
                return wait
        return service.module_deploy_ssh(payload.module_name, payload.timeout_seconds)

    return _background_command_job(
        "odoosh.module_deploy_ssh",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        _callback,
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )


@router.post("/module-update", response_model=CommandResult)
def odoosh_module_update(payload: OdooShModuleUpdateRequest) -> CommandResult:
    client = _validated_client_for_payload(payload, "odoosh.module_update")
    blocked = safe_deploy_command_block(
        "odoosh.module_update",
        project_path=payload.project_path,
        module_name=payload.module_name,
        cwd="odoosh:ssh",
    )
    if blocked:
        return blocked
    push_guard, resolved_sha = _manual_odoosh_pushed_sha_guard("odoosh.module_update", payload)
    if push_guard:
        return push_guard
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = _service_for_client(client)

    def _callback() -> CommandResult:
        if resolved_sha:
            wait = service.wait_for_commit(resolved_sha, payload.timeout_seconds, None)
            if wait.exit_code != 0:
                return wait
        return service.module_update(payload.module_name, payload.timeout_seconds)

    return _background_command_job(
        "odoosh.module_update",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        _callback,
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )


