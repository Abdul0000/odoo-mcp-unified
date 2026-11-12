from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    JobRecord,
    JobStatus,
    OdooFeatureInspectRequest,
    OdooFieldsGetRequest,
    OdooListModelsRequest,
    OdooModuleRequest,
    OdooReadGroupRequest,
    OdooResult,
    OdooSearchReadRequest,
)
from ..services.approval_service import ApprovalService
from ..services.odoo_service import OdooService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_shell_block
from ..services.client_gate_service import ClientGateService
from ..services.client_resolver_service import ClientResolverService
from ..services.background_executor import submit_background

router = APIRouter(prefix="/odoo", tags=["odoo"], dependencies=[AuthDependency])


def _odoo_service_for_client(client=None):
    if client is not None:
        try:
            return OdooService(client)
        except TypeError:
            return OdooService()
    return OdooService()


def _prepare_validated_client(payload: OdooModuleRequest, action: str):
    ClientGateService().require_validated_client(payload.client_name, action)
    if payload.client_name:
        client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True)
        return client
    return None


def _poll_manual_push_deployed(odoosh: OdooShService, pushed_sha: str, timeout_seconds: int, interval_seconds: float) -> CommandResult:
    poller = getattr(odoosh, "poll_deployed_commit", None)
    if callable(poller):
        return poller(pushed_sha, timeout_seconds, interval_seconds)
    # Test doubles and legacy service adapters may only implement
    # check_commit_deployed; the shared implementation supports that contract.
    return OdooShService.poll_deployed_commit(odoosh, pushed_sha, timeout_seconds, interval_seconds)


def _direct_module_deploy_guard(payload: OdooModuleRequest, store: JsonStore) -> OdooResult | None:
    """Block direct install/upgrade after manual git.push until Odoo.sh is verified."""
    state = store.load_session_state(client_name=payload.client_name)
    push = state.get("last_git_push")
    if not isinstance(push, dict) or push.get("verified_for_module_deploy"):
        return None

    pushed_sha = str(push.get("sha") or "").strip()
    module_names = set(push.get("module_names") or [])
    if module_names and payload.module_name not in module_names:
        return None
    if not pushed_sha:
        return OdooResult(
            status="failed",
            error="Direct module deploy blocked: a recent git.push was recorded without a pushed SHA. Use workflow.safe_deploy_module or workflow.resume_job.",
            result={"status": "deploy_guard_missing_sha", "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}}},
        )

    client = _prepare_validated_client(payload, "odoo.module_deploy_guard")
    odoosh = OdooShService(client) if client else OdooShService()
    check = _poll_manual_push_deployed(
        odoosh,
        pushed_sha,
        payload.deploy_guard_timeout_seconds,
        payload.deploy_guard_poll_interval_seconds,
    )
    if check.exit_code != 0:
        return OdooResult(
            status="failed",
            error="Direct module deploy blocked: Odoo.sh has not deployed the recent git.push SHA, so module upgrade would target stale code.",
            result={
                "status": "deploy_failed_or_timeout",
                "sha": pushed_sha,
                "guard": check.model_dump(mode="json"),
                "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}},
            },
        )

    failure = odoosh.recent_build_failure(int(push.get("started_epoch") or 0))
    if failure.exit_code == 1:
        return OdooResult(
            status="failed",
            error="Direct module deploy blocked: Odoo.sh logs show build/test failure after the recent git.push.",
            result={
                "status": "deploy_failed",
                "sha": pushed_sha,
                "guard": failure.model_dump(mode="json"),
                "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
            },
        )

    expected_hashes = {
        rel: digest
        for rel, digest in (push.get("expected_user_file_hashes") or {}).items()
        if isinstance(rel, str) and rel.startswith(f"{payload.module_name}/")
    }
    if expected_hashes:
        hash_result = odoosh.verify_user_file_hashes(expected_hashes)
        if hash_result.exit_code != 0:
            return OdooResult(
                status="failed",
                error="Direct module deploy blocked: /home/odoo/src/user files do not match the recent pushed code.",
                result={
                    "status": "deploy_code_mismatch",
                    "sha": pushed_sha,
                    "verified_files": sorted(expected_hashes),
                    "guard": hash_result.model_dump(mode="json"),
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
                },
            )

    push["verified_for_module_deploy"] = True
    push["verified_module_name"] = payload.module_name
    store.save_session_state({
        "last_git_push": push,
        "safe_deploy_status": "manual_push_verified_for_module_deploy",
    }, client_name=payload.client_name)
    return None


def _background_job_response(store: JsonStore, job: JobRecord, payload: OdooModuleRequest, started_message: str) -> OdooResult:
    """Return a final result for wait_for_result=true; otherwise return job_id.

    A wait-for-result deploy/upgrade must never hand back an "in progress" or
    "check back later" message. It either blocks until the module deploy job
    passes/fails, or callers can opt into the old asynchronous behavior with
    `wait_for_result=false`.
    """
    if not payload.wait_for_result:
        return OdooResult(
            status="failed",
            error="Deploy/upgrade job started but is not complete. Poll jobs.get until terminal passed/failed.",
            result={
                "job_id": job.id,
                "job_status": job.status.value,
                "status": "running",
                "state_class": "running",
                "finished": False,
                "message": f"{started_message} This is not a confirmed deployment. Poll jobs.get until terminal passed/failed.",
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
            },
        )

    waited = store.wait_job(job.id, payload.wait_timeout_seconds, payload.poll_interval_seconds)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooResult(
            status="ok",
            result={
                "job_id": waited.id,
                "job_status": waited.status.value,
                "finished": True,
                "message": "Upgrade confirmed successful after 1 attempt(s).",
                "job": data,
            },
        )
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = None
        if isinstance(waited.result, dict):
            result_error = waited.result.get("error") or waited.result.get("message")
        final_error = result_error or f"Job finished with status={waited.status.value}"
        return OdooResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            result={
                "job_id": waited.id,
                "job_status": waited.status.value,
                "finished": True,
                "message": f"Upgrade failed after 1 attempt(s), last error: {final_error}" if waited.status == JobStatus.failed else f"Job finished with status={waited.status.value}",
                "job": data,
            },
            error=final_error,
        )

    if waited.status in {JobStatus.queued, JobStatus.running}:
        return OdooResult(
            status="failed",
            error=f"{started_message} is still running after the bounded server-side wait.",
            result={
                "job_id": waited.id,
                "job_status": waited.status.value,
                "status": "running",
                "finished": False,
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
                "job": data,
                "next_call": {
                    "tool": "jobs.get",
                    "arguments": {
                        "job_id": waited.id,
                        "wait_for_result": True,
                        "wait_timeout_seconds": 20,
                        "poll_interval_seconds": 2.0,
                    },
                },
            },
        )

    final_error = f"Module deploy did not produce a supported terminal result; job_status={waited.status.value}"
    return OdooResult(
        status="failed",
        result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "message": f"Upgrade failed after 1 attempt(s), last error: {final_error}", "job": data},
        error=final_error,
    )


def _existing_post_push_job_response(store: JsonStore, payload: OdooModuleRequest) -> OdooResult | None:
    """Return the existing post-push deploy job instead of starting a duplicate direct deploy.

    After raw git.push auto-starts a follow-up upgrade job, direct odoo.module_deploy
    must not create a second deploy job or report success. It must direct the agent
    to jobs.get until the existing job is terminal.
    """
    state = store.load_session_state(client_name=payload.client_name)
    push = state.get("last_git_push")
    if not isinstance(push, dict):
        return None
    job_id = str(push.get("follow_up_job_id") or "").strip()
    if not job_id:
        return None
    module_names = set(push.get("module_names") or [])
    if module_names and payload.module_name not in module_names:
        return None
    try:
        job = store.get_job(job_id)
    except Exception:
        return None
    data = job.model_dump(mode="json")
    if job.status in {JobStatus.queued, JobStatus.running}:
        return OdooResult(
            status="failed",
            error="Deploy/upgrade is still running. This is not confirmed success; poll jobs.get.",
            result={
                "status": "running",
                "job_id": job.id,
                "job_status": job.status.value,
                "finished": False,
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
                "poll_after_seconds": 5,
                "message": "Existing post-push deploy/upgrade job is still running. Do not report deployed/upgraded yet.",
                "job": data,
                "next_call": {
                    "tool": "jobs.get",
                    "arguments": {
                        "job_id": job.id,
                        "wait_for_result": True,
                        "wait_timeout_seconds": 20,
                        "poll_interval_seconds": 2.0,
                    },
                },
            },
        )
    if job.status == JobStatus.passed:
        return OdooResult(status="ok", result={"status": "passed", "job_id": job.id, "job_status": job.status.value, "finished": True, "success_allowed": True, "job": data})
    return OdooResult(status="failed", error="Existing post-push deploy/upgrade job failed or ended with warning.", result={"status": job.status.value, "job_id": job.id, "job_status": job.status.value, "finished": True, "success_allowed": False, "job": data})


@router.post("/health", response_model=OdooResult)
def odoo_health() -> OdooResult:
    client = ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).health()


@router.post("/module-status", response_model=OdooResult)
def odoo_module_status(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_status")
    return _odoo_service_for_client(client).module_status(payload.module_name)


@router.post("/module-install", response_model=OdooResult)
def odoo_module_install(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_install")
    """Background install — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_shell_block("odoo.module_install", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_install", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_install", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or _odoo_service_for_client(client).module_install(payload.module_name)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_install succeeded", "odoo.module_install failed")
        store.log_audit("odoo.module_install", result.status, payload.module_name)

    submit_background(_bg)
    return _background_job_response(store, job, payload, "Install started.")


@router.post("/module-update", response_model=OdooResult)
def odoo_module_update(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_update")
    """Background upgrade — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_shell_block("odoo.module_update", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or _odoo_service_for_client(client).module_update(payload.module_name)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_update succeeded", "odoo.module_update failed")
        store.log_audit("odoo.module_update", result.status, payload.module_name)

    submit_background(_bg)
    return _background_job_response(store, job, payload, "Upgrade started.")


def start_odoo_module_deploy_job(
    payload: OdooModuleRequest,
    approval: ApprovalService | None = None,
    enforce_deploy_guard: bool = False,
    run_inline: bool = False,
    validated_client=None,
) -> OdooResult:
    # A server-owned safe-deploy job already validated and pinned this client
    # before commit/push. Reuse that resolved identity so a long Odoo.sh wait
    # cannot expire the session between source deployment and module upgrade.
    client = validated_client or _prepare_validated_client(payload, "odoo.module_deploy")
    """Start smart deploy in the existing background-job path.

    Public router calls pass an ApprovalService after approval succeeds. Internal
    workflows that already enforced their own approval can reuse the same job
    path without asking for a second, mismatched approval.
    """
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_deploy", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store) if enforce_deploy_guard else None
            result = guarded or _odoo_service_for_client(client).module_deploy(payload.module_name)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        session_client = client.name if client else payload.client_name
        session = store.load_session_state(session_client)
        diagnostics = dict(session.get("module_deploy_diagnostics") or {})
        deploy_diagnostic = result.result.get("deploy_diagnostic") if isinstance(result.result, dict) else None
        if isinstance(deploy_diagnostic, dict):
            diagnostics[payload.module_name] = {
                **deploy_diagnostic,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        elif result.status == "ok":
            diagnostics.pop(payload.module_name, None)
        store.save_session_state({"module_deploy_diagnostics": diagnostics}, client_name=session_client)
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        if approval:
            approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_deploy succeeded", "odoo.module_deploy failed")
        store.log_audit("odoo.module_deploy", result.status, payload.module_name)

    # Composite safe-deploy already runs in a background worker. Starting a
    # nested job on the same bounded executor can starve when concurrent parent
    # jobs occupy all workers, so internal callers execute this child inline.
    if run_inline:
        _bg()
    else:
        submit_background(_bg)
    return _background_job_response(store, job, payload, "Deploy started.")


@router.post("/module-deploy", response_model=OdooResult)
def odoo_module_deploy(payload: OdooModuleRequest) -> OdooResult:
    _prepare_validated_client(payload, "odoo.module_deploy")
    """Smart deploy: update_list → check state → install/upgrade. Background job."""
    blocked = safe_deploy_shell_block("odoo.module_deploy", module_name=payload.module_name)
    if blocked:
        return blocked
    store = JsonStore()
    existing = _existing_post_push_job_response(store, payload)
    if existing:
        return existing
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_deploy", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    return start_odoo_module_deploy_job(payload, approval, enforce_deploy_guard=True)


@router.post("/search-read", response_model=OdooResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooResult:
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).search_read(payload.model, payload.domain, payload.fields, payload.limit, payload.offset, payload.order, payload.context)


@router.post("/feature-inspect", response_model=OdooResult)
def odoo_feature_inspect(payload: OdooFeatureInspectRequest) -> OdooResult:
    """Inspect related models/UI/security with one Odoo.sh shell startup."""
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).feature_inspect(
        payload.models,
        sample_fields=payload.sample_fields,
        sample_limit=payload.sample_limit,
        timeout_seconds=payload.timeout_seconds,
    )


@router.post("/fields-get", response_model=OdooResult)
def odoo_fields_get(payload: OdooFieldsGetRequest) -> OdooResult:
    """Read-only field discovery for one live Odoo model."""
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).fields_get(payload.model, payload.limit)


@router.post("/list-models", response_model=OdooResult)
def odoo_list_models(payload: OdooListModelsRequest) -> OdooResult:
    """Read-only model discovery from the live Odoo registry."""
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).list_models(payload.search, payload.include_transient, payload.limit)


@router.post("/read-group", response_model=OdooResult)
def odoo_read_group(payload: OdooReadGroupRequest) -> OdooResult:
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return _odoo_service_for_client(client).read_group(
        payload.model, payload.domain, payload.fields, payload.groupby, payload.lazy, payload.context, payload.timeout_seconds
    )
