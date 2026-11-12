from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus, BaseProjectRequest, CommandResult, GitCommitRequest, GitDiffRequest, GitPushRequest, JobRecord, JobStatus, JobStep
from ..services.approval_service import ApprovalService
from ..services.background_executor import submit_background
from ..services.git_service import GitService
from ..services.odoosh_service import OdooShService
from ..services.client_resolver_service import ClientResolverService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_command_block
from ..services.client_gate_service import ClientGateService

router = APIRouter(prefix="/git", tags=["git"], dependencies=[AuthDependency])
logger = logging.getLogger(__name__)



def _require_explicit_client_when_multiclient(payload: object, action: str) -> None:
    """Require client.check validation before git tools in multi-client mode."""
    ClientGateService().require_validated_client(getattr(payload, "client_name", None), action)


def _normalise_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    value = branch.strip()
    if value.startswith("refs/heads/"):
        value = value.removeprefix("refs/heads/")
    return value.lower()


def _is_protected_branch(branch: str | None) -> bool:
    normalised = _normalise_branch(branch)
    protected = {"main", "master", "production"} | {_normalise_branch(item) for item in settings.protected_branches}
    return bool(normalised and normalised in protected)


def _push_file_hashes(project_path: str, changed_stdout: str) -> dict[str, str]:
    project = Path(project_path).resolve()
    expected: dict[str, str] = {}
    allowed_suffixes = (".py", ".xml", ".js", ".scss", ".css", ".csv", "__manifest__.py")
    for raw_line in changed_stdout.splitlines():
        rel = raw_line.strip().replace("\\", "/")
        if not rel or rel.startswith(".git/") or not rel.endswith(allowed_suffixes):
            continue
        file_path = (project / rel).resolve()
        if not file_path.is_file() or not (file_path == project or project in file_path.parents):
            continue
        expected[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return expected


def _normalise_changed_path(raw_path: str) -> str | None:
    rel = raw_path.strip().replace("\\", "/").strip("/")
    if not rel or rel in {".", ".git"}:
        return None
    if rel.startswith("../") or "/../" in rel or rel.startswith(".git/"):
        return None
    return rel


def _detect_modules_from_paths(project_path: str, paths: list[str]) -> list[str]:
    """Return Odoo module technical names touched by relative git paths.

    A module is detected by walking from each changed path up to the checkout
    root and finding the nearest directory containing ``__manifest__.py``. This
    supports both root-level modules (``my_module/models/x.py``) and nested
    addon layouts (``addons/my_module/models/x.py``).
    """
    project = Path(project_path).resolve()
    names: set[str] = set()
    for raw_path in paths:
        rel = _normalise_changed_path(raw_path)
        if not rel:
            continue
        candidate = (project / rel).resolve()
        if not (candidate == project or project in candidate.parents):
            continue
        current = candidate.parent if candidate.name == "__manifest__.py" else candidate
        if not current.is_dir():
            current = current.parent
        while current == project or project in current.parents:
            if (current / "__manifest__.py").is_file():
                names.add(current.name)
                break
            if current == project:
                break
            current = current.parent
    return sorted(names)


def detect_module_from_paths(project_path: str, paths: list[str]) -> str | None:
    """Return the first Odoo module touched by relative git paths, if any."""
    modules = _detect_modules_from_paths(project_path, paths)
    return modules[0] if modules else None


def _module_names_from_paths(project_path: str, paths: list[str]) -> list[str]:
    return _detect_modules_from_paths(project_path, paths)


AUTO_UPGRADE_DEPLOY_POLL_INTERVAL_SECONDS = 3.0
AUTO_UPGRADE_DEPLOY_TIMEOUT_SECONDS = 300


def _json_payload(stdout: str | None) -> dict:
    try:
        payload = json.loads(stdout or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _start_auto_odoosh_upgrade_job(project_path: str, module_name: str, expected_sha: str, timeout_seconds: int, client_name: str | None = None) -> str:
    """Start the non-blocking post-push Odoo.sh deploy+upgrade job.

    This job is the follow-through for raw ``git.push``: it waits until Odoo.sh
    confirms that the pushed SHA is deployed, then upgrades the touched module.
    The HTTP ``git.push`` response stays fast and backwards compatible; clients
    poll this job through the existing jobs.get/jobs.list mechanism.
    """
    store = JsonStore()
    request = {
        "project_path": str(Path(project_path).resolve()),
        "module_name": module_name,
        "expected_sha": expected_sha,
        "deploy_poll_interval_seconds": AUTO_UPGRADE_DEPLOY_POLL_INTERVAL_SECONDS,
        "deploy_timeout_seconds": AUTO_UPGRADE_DEPLOY_TIMEOUT_SECONDS,
        "module_upgrade_timeout_seconds": timeout_seconds,
        "trigger": "git.push:auto_upgrade_on_push",
        "client_name": client_name,
    }
    job = store.create_job(JobRecord(job_type="post_push_auto_upgrade", status=JobStatus.running, request=request))

    def _step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(
            current,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                error=error,
            ),
        )

    def _finish(status_: JobStatus, result: dict) -> None:
        current = store.get_job(job.id)
        store.finish_job(current, status_, result)

    def _bg() -> None:
        try:
            resolver = ClientResolverService()
            client = resolver.resolve_client(client_name=client_name, project_path=project_path, require_complete=False)
            service = OdooShService(client) if client else OdooShService()

            def _poll_progress(attempt_payload: dict[str, object]) -> None:
                _step("odoosh_check_commit_poll", "passed" if attempt_payload.get("deployed") else "running", attempt_payload)

            poll_started = time.monotonic()
            poll_result = service.poll_deployed_commit(
                expected_sha,
                timeout_seconds=AUTO_UPGRADE_DEPLOY_TIMEOUT_SECONDS,
                interval_seconds=AUTO_UPGRADE_DEPLOY_POLL_INTERVAL_SECONDS,
                progress_cb=_poll_progress,
            )
            poll_elapsed = round(time.monotonic() - poll_started, 3)
            poll_payload = _json_payload(poll_result.stdout)
            deployed = poll_result.exit_code == 0 and bool(poll_payload.get("deployed"))
            _step("odoosh_check_commit", "passed" if deployed else "failed", poll_result)

            if not deployed:
                result = {
                    "status": "deploy_failed_or_timeout",
                    "message": "Odoo.sh did not report the pushed commit as deployed within the post-push polling window. Module upgrade was not attempted.",
                    "module_name": module_name,
                    "sha": expected_sha,
                    "poll_timeout_seconds": AUTO_UPGRADE_DEPLOY_TIMEOUT_SECONDS,
                    "poll_interval_seconds": AUTO_UPGRADE_DEPLOY_POLL_INTERVAL_SECONDS,
                    "poll_elapsed_seconds": poll_elapsed,
                    "poll_attempts": poll_payload.get("attempts"),
                    "guard": poll_result.model_dump(mode="json"),
                    "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}},
                }
                _finish(JobStatus.failed, result)
                store.save_session_state({
                    "last_post_push_auto_upgrade_job_id": job.id,
                    "last_sha": expected_sha,
                    "module_name": module_name,
                    "project_path": str(Path(project_path).resolve()),
                    "safe_deploy_status": "deploy_failed_or_timeout",
                    "client_name": client.name if client else client_name,
                }, client_name=client.name if client else client_name)
                store.log_audit(
                    "git.push.auto_odoosh_upgrade",
                    "deploy_failed_or_timeout",
                    module_name,
                    {"job_id": job.id, "expected_sha": expected_sha, "poll_attempts": poll_payload.get("attempts")},
                )
                return

            _step("post_push_deploy_confirmed", "passed", {
                "status": "deployed",
                "module_name": module_name,
                "sha": expected_sha,
                "poll_elapsed_seconds": poll_elapsed,
                "poll_attempts": poll_payload.get("attempts"),
            })

            upgrade_result = service.module_deploy_ssh(module_name, timeout_seconds)
            upgrade_passed = upgrade_result.exit_code == 0
            _step("odoosh_module_deploy_ssh", "passed" if upgrade_passed else "failed", upgrade_result)
            final_result = {
                "status": "passed" if upgrade_passed else "module_upgrade_failed",
                "message": (
                    f"Detected module {module_name}; Odoo.sh deployed {expected_sha} and module upgrade completed."
                    if upgrade_passed
                    else f"Detected module {module_name}; Odoo.sh deployed {expected_sha}, but module upgrade failed."
                ),
                "module_name": module_name,
                "sha": expected_sha,
                "poll_elapsed_seconds": poll_elapsed,
                "poll_attempts": poll_payload.get("attempts"),
                "deploy_check": poll_result.model_dump(mode="json"),
                "module_upgrade": upgrade_result.model_dump(mode="json"),
            }
            _finish(JobStatus.passed if upgrade_passed else JobStatus.failed, final_result)
            store.save_session_state({
                "last_post_push_auto_upgrade_job_id": job.id,
                "last_sha": expected_sha,
                "module_name": module_name,
                "project_path": str(Path(project_path).resolve()),
                "safe_deploy_status": "post_push_auto_upgrade_passed" if upgrade_passed else "module_upgrade_failed",
                "client_name": client.name if client else client_name,
            }, client_name=client.name if client else client_name)
            store.log_audit(
                "git.push.auto_odoosh_upgrade",
                "ok" if upgrade_passed else "failed",
                module_name,
                {"job_id": job.id, "expected_sha": expected_sha, "exit_code": upgrade_result.exit_code, "poll_attempts": poll_payload.get("attempts")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Auto Odoo.sh upgrade job failed after git.push")
            _step("post_push_auto_upgrade_exception", "failed", error=str(exc))
            _finish(JobStatus.failed, {"status": "post_push_auto_upgrade_error", "module_name": module_name, "sha": expected_sha, "error": str(exc)})
            store.log_audit(
                "git.push.auto_odoosh_upgrade",
                "failed",
                module_name,
                {"job_id": job.id, "expected_sha": expected_sha, "error": str(exc)},
            )

    submit_background(_bg)
    return job.id


def _append_auto_upgrade_note(result: CommandResult, module_name: str, job_id: str) -> CommandResult:
    note = (
        f"Detected module {module_name} in this push — Odoo.sh upgrade job started, job_id={job_id}. "
        "This is NOT deployed/upgraded yet. Poll with jobs.get until terminal passed/failed. Do not report success from git.push."
    )
    payload = {
        "status": "running",
        "follow_up_job_id": job_id,
        "job_id": job_id,
        "module_name": module_name,
        "completion_required": True,
        "success_allowed": False,
        "tool_exit_allowed": False,
        "poll_after_seconds": 5,
        "message": note,
        "next_call": {
            "tool": "jobs.get",
            "arguments": {
                "job_id": job_id,
                "wait_for_result": True,
                "wait_timeout_seconds": 20,
                "poll_interval_seconds": 2.0,
            },
        },
    }
    stdout = (result.stdout or "").rstrip()
    stdout = f"{stdout}\n{note}\nAUTO_UPGRADE_ON_PUSH={json.dumps(payload, sort_keys=True)}\n" if stdout else f"{note}\nAUTO_UPGRADE_ON_PUSH={json.dumps(payload, sort_keys=True)}\n"
    # Push itself succeeded, but the requested deploy/upgrade is not complete.
    # Non-zero exit_code prevents MCP agents from summarising git.push as a full deploy success.
    return result.model_copy(update={"stdout": stdout, "exit_code": 2})


def _ensure_protected_branch_approval(branch: str | None, approval_id: str | None) -> bool:
    if not _is_protected_branch(branch):
        return False
    store = JsonStore()
    if not approval_id:
        record = store.create_approval(ApprovalRecord(action="git.push", summary=f"Push protected branch {branch}", payload_preview={"branch": branch}))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "approval_required": True,
                "approval_id": record.id,
                "action": "git.push",
                "message": "Protected branch push requires explicit git.push approval.",
            },
        )
    record = store.get_approval(approval_id)
    if record.action != "git.push":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is for {record.action}, not git.push")
    if record.status != ApprovalStatus.approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is {record.status.value}, not approved")
    store.set_approval_status(approval_id, ApprovalStatus.running, reason="Protected branch push started")
    return True


@router.post("/status", response_model=CommandResult)
def git_status(payload: BaseProjectRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "git.status")
    return GitService().status(payload.project_path)


@router.post("/current-branch", response_model=CommandResult)
def git_current_branch(payload: BaseProjectRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "git.current_branch")
    return GitService().current_branch(payload.project_path)


@router.post("/diff", response_model=CommandResult)
def git_diff(payload: GitDiffRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "git.diff")
    return GitService().diff(payload.project_path, payload.staged, payload.timeout_seconds)


@router.post("/commit", response_model=CommandResult)
def git_commit(payload: GitCommitRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "git.commit")
    blocked = safe_deploy_command_block("git.commit", project_path=payload.project_path)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("git.commit", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = GitService().commit(payload.project_path, payload.message, payload.paths, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.commit succeeded", "git.commit failed")
    JsonStore().log_audit("git.commit", "ok" if result.exit_code == 0 else "failed", payload.message, {"paths": payload.paths})
    return result


@router.post("/push", response_model=CommandResult)
def git_push(payload: GitPushRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "git.push")
    blocked = safe_deploy_command_block("git.push", project_path=payload.project_path)
    if blocked:
        return blocked
    branch = payload.branch
    if branch is None:
        branch_result = GitService().current_branch(payload.project_path)
        if branch_result.exit_code == 0:
            branch = branch_result.stdout.strip()
    protected_approval = _ensure_protected_branch_approval(branch, payload.approval_id)
    approval = ApprovalService()
    if not protected_approval:
        approval.ensure_allowed("git.push", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    git = GitService()
    push_started_epoch = int(time.time())
    changed_result = git.head_changed_files(payload.project_path)
    changed_paths = [line.strip().replace("\\", "/") for line in (changed_result.stdout if changed_result.exit_code == 0 else "").splitlines() if line.strip()]
    result = git.push(payload.project_path, payload.remote, payload.branch, payload.set_upstream, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.push succeeded", "git.push failed")
    JsonStore().log_audit("git.push", "ok" if result.exit_code == 0 else "failed", branch or "current", {"remote": payload.remote})
    if result.exit_code == 0:
        sha_result = git.head_sha(payload.project_path)
        pushed_sha = sha_result.stdout.strip() if sha_result.exit_code == 0 else ""
        expected_hashes = _push_file_hashes(payload.project_path, changed_result.stdout if changed_result.exit_code == 0 else "")
        module_names = _module_names_from_paths(payload.project_path, changed_paths)
        JsonStore().save_session_state({
            "last_git_push": {
                "project_path": str(Path(payload.project_path).resolve()),
                "branch": branch or payload.branch,
                "remote": payload.remote,
                "sha": pushed_sha,
                "started_epoch": push_started_epoch,
                "changed_files": sorted(expected_hashes),
                "module_names": module_names,
                "expected_user_file_hashes": expected_hashes,
                "verified_for_module_deploy": False,
            },
            "last_sha": pushed_sha,
            "safe_deploy_status": "manual_push_pending_deploy_verification",
        }, client_name=payload.client_name)
        if settings.auto_upgrade_on_push and pushed_sha:
            try:
                module_name = detect_module_from_paths(payload.project_path, changed_paths)
                if module_name:
                    job_id = _start_auto_odoosh_upgrade_job(payload.project_path, module_name, pushed_sha, payload.timeout_seconds, payload.client_name)
                    result = _append_auto_upgrade_note(result, module_name, job_id)
                    safe_deploy_status = "manual_push_auto_upgrade_triggered"
                    try:
                        auto_job = JsonStore().get_job(job_id)
                        if auto_job.status == JobStatus.passed:
                            safe_deploy_status = "post_push_auto_upgrade_passed"
                        elif isinstance(auto_job.result, dict) and auto_job.result.get("status"):
                            safe_deploy_status = str(auto_job.result.get("status"))
                    except Exception:  # noqa: BLE001
                        pass
                    JsonStore().save_session_state({
                        "last_git_push": {
                            "project_path": str(Path(payload.project_path).resolve()),
                            "branch": branch or payload.branch,
                            "remote": payload.remote,
                            "sha": pushed_sha,
                            "started_epoch": push_started_epoch,
                            "changed_files": sorted(expected_hashes),
                            "module_names": module_names,
                            "expected_user_file_hashes": expected_hashes,
                            "verified_for_module_deploy": False,
                            "follow_up_job_id": job_id,
                            "auto_upgrade_module_name": module_name,
                        },
                        "last_sha": pushed_sha,
                        "safe_deploy_status": safe_deploy_status,
                        "client_name": payload.client_name,
                    }, client_name=payload.client_name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to auto-trigger Odoo.sh upgrade after successful git.push")
                try:
                    JsonStore().log_audit(
                        "git.push.auto_odoosh_upgrade",
                        "failed",
                        branch or "current",
                        {"error": str(exc), "sha": pushed_sha},
                    )
                except Exception:  # noqa: BLE001
                    pass
    return result
