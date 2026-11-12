from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from threading import Lock

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings, mask_repo_url
from ..schemas import (
    CapabilityCheckResult,
    CodingTaskPlanRequest,
    CodingTaskPlanResult,
    CommandResult,
    DeployedModuleVerifyRequest,
    FullVerifyRequest,
    StartSessionRequest,
    RegistrySmokeTestRequest,
    RestoreLastGoodCommitRequest,
    MenuCrawlerRequest,
    JobRecord,
    JobStatus,
    JobStep,
    WorkflowReport,
    SafeDeployModuleRequest,
    SelfRunRequest,
    OdooModuleRequest,
    OdooResult,
    # V14: Self-Learning Agent — Core Discovery
    FindCoreFileRequest,
    FindCoreFileResult,
    FindSimilarViewRequest,
    FindSimilarViewResult,
    ListModuleFilesRequest,
    ListModuleFilesResult,
    # V14+: Pillar 2 — Adaptive Scope Advisor
    ScopeAdviceRequest,
    ScopeAdviceResult,
    # V14+: Pillar 3 — Pre-Push Syntax Verifier
    SyntaxVerifyRequest,
    SyntaxVerifyResult,
    # V17: Combined static + live POS browser verification with bounded fix-cycle
    PosChangeVerifyRequest,
    PosChangeVerifyResult,
    PosFlowCheckRequest,
    # V15: Strict Odoo frontend/OWL/JS/XML preflight
    FrontendPatternPreflightRequest,
    FrontendPatternPreflightResult,
    # V16: fast/correct routing and repo index
    SmartToolRouteRequest,
    SmartToolRouteResult,
    SmartToolRouteStep,
    RepoIndexRequest,
    RepoIndexResult,
    RepoPrepareResult,
)
from ..services.approval_service import ApprovalService
from ..services.client_resolver_service import ClientResolverService
from ..services.client_gate_service import ClientGateService
from ..services.git_service import GitService
from ..services.odoo_service import OdooService
from ..services.odoosh_service import OdooShService
from ..services.path_service import resolve_allowed_path
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore
from ..services.background_executor import submit_background
from ..services.core_finder_service import CoreFinderService          # V14
from ..services.prewrite_gate_service import PreWriteGateService
from ..services.syntax_verifier_service import SyntaxVerifierService  # V14+
from ..services.frontend_pattern_service import FrontendPatternService  # V15
from ..services.repo_index_service import RepoIndexService  # V16
from ..services.live_assertion_service import LiveAssertionService
from ..services.verification_proof_service import VerificationProofService
from ..services.workflow_guard_service import (
    _RESUMABLE_SAFE_DEPLOY_RESULTS,
    find_blocking_safe_deploy,
    safe_deploy_block_payload,
)
from ..services.checkout_guard_service import ensure_module_exists_or_sync, maybe_ensure_known_module_current
from .qa import self_run
from .odoo import start_odoo_module_deploy_job

router = APIRouter(prefix="/workflow", tags=["workflow"], dependencies=[AuthDependency])

# Serializes the safe_deploy_module entry gate inside this process so two
# near-simultaneous retries cannot both pass the duplicate-job check before the
# first JobRecord is persisted. JsonStore still provides the durable state; this
# lock closes the in-process check/create window.
_SAFE_DEPLOY_ENTRY_LOCK = Lock()


def _has_fresh_scoped_changes(payload: SafeDeployModuleRequest) -> bool:
    """Return True when this request contains local source newer than a resumable job.

    A github_pushed_deploy_pending job owns the SHA it already pushed, but it must not
    block a later verified repair that is still present in the working tree. Only paths
    explicitly approved by the current safe-deploy request are considered.
    """
    try:
        changed = GitService().changed_files(payload.project_path)
    except Exception:
        return False
    if changed.exit_code != 0:
        return False
    approved = {str(path).strip().replace("\\", "/") for path in payload.paths if str(path).strip()}
    current = {line.strip().replace("\\", "/") for line in (changed.stdout or "").splitlines() if line.strip()}
    return bool(approved & current)


def _scoped_diff_fingerprint(payload: SafeDeployModuleRequest) -> str | None:
    """Hash the approved working-tree diff so resumable jobs cannot absorb new work."""
    try:
        git = GitService()
        changed = git.changed_files(payload.project_path)
        if changed.exit_code != 0:
            return None
        approved = sorted({str(path).strip().replace("\\", "/") for path in payload.paths if str(path).strip()})
        current = {line.strip().replace("\\", "/") for line in (changed.stdout or "").splitlines() if line.strip()}
        scoped = [path for path in approved if path in current]
        if not scoped:
            return None
        diff = git.diff(payload.project_path, staged=False, timeout_seconds=30)
        if diff.exit_code != 0:
            return None
        material = "\n".join(scoped) + "\n---DIFF---\n" + (diff.stdout or "")
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _odoosh_service_for_client(client=None):
    """Instantiate OdooShService with per-client config while keeping tests/fakes compatible."""
    if client is not None:
        try:
            return OdooShService(client)
        except TypeError:
            return OdooShService()
    return OdooShService()




def _classify_deployed_verification_failure(result: CommandResult) -> tuple[str, str]:
    """Differentiate real source mismatches from transport/shell failures."""
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    mismatch_markers = ("MISSING ", "MISMATCH ", "MISSING_MANIFEST", "MANIFEST_NOT_INSTALLABLE")
    if getattr(result, "timed_out", False):
        return (
            "deploy_verification_timeout",
            "Odoo.sh deployed-code verification timed out before a mismatch could be established.",
        )
    if any(marker in output for marker in mismatch_markers):
        return (
            "deploy_code_mismatch",
            "Odoo.sh source checkout did not match the changed local/GitHub files. Module upgrade was not attempted.",
        )
    return (
        "deploy_verification_command_failed",
        "The deployed-code verification command failed; no source mismatch was established and module upgrade was not attempted.",
    )

def _odoo_service_for_client(client=None):
    """Instantiate OdooService with per-client config while keeping tests/fakes compatible."""
    if client is not None:
        try:
            return OdooService(client)
        except TypeError:
            return OdooService()
    return OdooService()



def _looks_like_small_patch(task: str) -> bool:
    value = task.lower()
    small_terms = ("add field", "new field", "simple field", "notebook", "tab", "page", "xpath", "label")
    broad_terms = ("new module", "report", "dashboard", "integration", "migration", "workflow", "portal", "api")
    return any(term in value for term in small_terms) and not any(term in value for term in broad_terms)


def _looks_like_frontend_task(task: str) -> bool:
    value = task.lower().replace("-", "_")
    frontend_terms = (
        "js", "javascript", "owl", "component", "qweb", "xml template", "scss", "asset",
        "pos", "point_of_sale", "website", "snippet", "frontend", "editor",
        "payment", "checkout", "provider", "dashboard", "screen", "patch", "registry",
    )
    return any(term in value for term in frontend_terms)


_UI_PROOF_EXTENSIONS = {".xml", ".css", ".scss", ".js"}
_UI_PROOF_DIR_HINTS = {"views", "templates", "static"}
_UI_PROOF_STRICT_SKIP_HINTS = {"models", "controllers", "security", "wizard", "report", "data", "migrations", "tests"}


def _frontend_ui_files_requiring_live_proof(module_name: str, paths: list[str]) -> list[str]:
    """Return changed frontend/template/style files where deploy success is not visual proof.

    Odoo.sh deploy + module upgrade can pass while a website/asset/QWeb change is
    still not visibly correct because of asset bundles, selectors, inherited views,
    conditions, website cache, or wrong page path.  These files require browser/UI
    proof before an MCP agent may claim that the visible page changed.
    """
    result: list[str] = []
    module_prefix = f"{module_name.strip('/')}/" if module_name else ""
    for raw in paths or []:
        rel = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not rel or "\x00" in rel:
            continue
        if module_prefix and not rel.startswith(module_prefix):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix not in _UI_PROOF_EXTENSIONS:
            continue
        parts = {p for p in rel.split("/") if p}
        if parts & _UI_PROOF_STRICT_SKIP_HINTS:
            continue
        if suffix in {".css", ".scss", ".js"}:
            if "static" in parts:
                result.append(rel)
            continue
        if suffix == ".xml" and (parts & _UI_PROOF_DIR_HINTS):
            result.append(rel)
    return list(dict.fromkeys(result))


@router.post("/coding-task-plan", response_model=CodingTaskPlanResult)
def coding_task_plan(payload: CodingTaskPlanRequest) -> CodingTaskPlanResult:
    """Return the canonical repo-first coding workflow for MCP agents."""
    client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path or settings.target_project_path,
        require_complete=False,
    )
    repo_url = payload.repo_url or (client.repo if client else None) or settings.target_git_repo or "<repo_url>"
    branch = payload.branch or (client.branch if client else None) or settings.target_git_branch or "<target_branch>"
    project_path = payload.project_path or (client.path if client else None) or settings.target_project_path or f"{settings.workspace_root.rstrip('/')}/<repo_name>"
    module_name = payload.module_name or "<custom_module>"
    checkout_guard = None
    if payload.module_name and project_path and "<" not in str(project_path):
        checkout_guard = maybe_ensure_known_module_current(
            project_path=project_path,
            module_name=payload.module_name,
            branch=branch,
            client_name=client.name if client else payload.client_name,
            remote="origin",
            timeout_seconds=120,
            require_prior_success=True,
        )
        if checkout_guard and not checkout_guard.get("ok"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=checkout_guard.get("error") or "Checkout/module guard failed")
    small_patch = _looks_like_small_patch(payload.task)
    if small_patch:
        steps = [
            "Use the small-patch path: if session.load_state already has project_path/client_name, skip workflow.start_session; otherwise call workflow.start_session with include_odoo=false and include_odoosh=false.",
            "Call repo.prepare once, then read only the target module manifest plus the specific model/view files needed for the requested field or notebook change.",
            "If the patch touches JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, call workflow.frontend_pattern_preflight after reading custom files and read every mandatory source pattern before coding.",
            "For POS work, never use legacy load_fields('pos.config', ...) / models.load_fields or define/override _load_pos_data_fields on pos.config; read installed point_of_sale source and use the version's real loader/backend pattern.",
            "MANDATORY Odoo.sh source layer before writing: call workflow.scope_advice after reading custom files from the checkout, then repo.read every mandatory related installed-source path it returns. For editing, do not re-read the same current module from /home/odoo/src/user; /home/odoo/src/user is only for other related custom dependencies. For claims like already deployed/live/done, verify the current module with workflow.verify_deployed_module or odoosh.read_user_file.",
            "For field/view changes, never use direct ORM/Studio writes to ir.model.fields or ir.ui.view unless the user explicitly asked for no-code Studio customization. Normal development must patch Python/XML files in the custom module.",
            "For Odoo Python files, never import pydantic or create Pydantic BaseModel classes. Use Odoo ORM models.Model, fields, api, and XML views only.",
            "Patch the smallest file set with repo.apply_patch_atomic or repo.write_many_smart; use repo.write_many_atomic only for large/risky refactors. Do not run broad module discovery again after the target module is known.",
            "Run git.diff and qa.self_run with run_pytest=false unless Python tests are present or the patch touches shared logic.",
            "MANDATORY before deploy: run workflow.verify_against_core for changed .py/.xml/.js/__manifest__.py files. If push_blocked=true, fix and re-run; only then continue.",
            "Call workflow.safe_deploy_module with module_name, commit_message, changed paths, run_qa=false if qa.self_run already passed, wait_for_odoosh=true, upgrade_module=true, and expected_registry_fields for new fields when known. Do not manually chain git.commit/git.push/odoosh.check_commit/odoosh.module_update/odoo.module_deploy for the normal path; those lower-level tools are blocked once safe_deploy starts.",
            "If the repo already contains the requested change and no edit is needed, do not say done from repo.read alone; call workflow.verify_deployed_module and continue deploy/resume if /home/odoo/src/user does not match.",
            "If safe deploy returns retry_later, wait briefly and call workflow.resume_job with the job_id. If it returns deploy_running, call jobs.get on the nested job. Do not repeat commit/push.",
            "Verify the exact field with odoo.search_read or workflow.registry_smoke_test after module upgrade.",
        ]
        required_tools = [
            "session.load_state",
            "repo.prepare",
            "repo.read",
            "workflow.scope_advice",
            "repo.apply_patch_atomic",
            "repo.write_many_smart",
            "git.diff",
            "qa.self_run",
            "workflow.verify_against_core",
            "workflow.frontend_pattern_preflight",
            "workflow.safe_deploy_module",
            "workflow.verify_deployed_module",
            "workflow.resume_job",
            "jobs.get",
            "odoo.search_read",
        ]
        summary = "Fast path for a small targeted Odoo code patch."
    else:
        steps = [
            "Call repo.prepare with the target repo and branch. This clones if missing, otherwise fetches, checks out, and pulls the branch with --ff-only.",
            "Call repo.list_custom_modules on the returned project_path; choose the target custom module from __manifest__.py files, not from Odoo's installed module list only.",
            f"Read the target module files first: {module_name}/__manifest__.py, models, views, security, and relevant static files.",
            "For JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, call workflow.frontend_pattern_preflight with task_description, target_module_name, custom_file_paths, and planned_file_paths; then repo.read all mandatory pattern files returned before implementation.",
            "For POS work, never use legacy load_fields('pos.config', ...) / models.load_fields or define/override _load_pos_data_fields on pos.config; read installed point_of_sale source and use the version's real loader/backend pattern.",
            "MANDATORY Odoo.sh source layer before writing: call workflow.scope_advice after reading custom files from the checkout, then repo.read every mandatory related installed-source path it returns. For editing, do not re-read the same current module from /home/odoo/src/user; /home/odoo/src/user is only for other related custom dependencies. For claims like already deployed/live/done, verify the current module with workflow.verify_deployed_module or odoosh.read_user_file.",
            "For field/view changes, never use direct ORM/Studio writes to ir.model.fields or ir.ui.view unless the user explicitly asked for no-code Studio customization. Normal development must patch Python/XML files in the custom module.",
            "For Odoo Python files, never import pydantic or create Pydantic BaseModel classes. Use Odoo ORM models.Model, fields, api, and XML views only.",
            "Apply edits safely: use repo.apply_patch_atomic for multi-file diffs or repo.write_many_smart for normal multi-file writes. Use repo.write_many_atomic only for large/risky refactors, and repo.apply_patch/repo.write only for small single-file changes. Do not edit Odoo core, enterprise, .git, or unrelated modules.",
            "Run git.diff and qa.self_run. Treat pytest environment failures separately from real module errors.",
            "MANDATORY before deploy: run workflow.verify_against_core for changed .py/.xml/.js/__manifest__.py files. If push_blocked=true, fix and re-run; do not deploy while blocked.",
            "Call workflow.safe_deploy_module with module_name, commit_message, changed paths, wait_for_odoosh=true, and upgrade_module=true. It owns commit, push, Odoo.sh polling, deployed-source verification, module deploy, module status, and registry smoke tests.",
            "If the repo already contains the requested change and no edit is needed, do not say done from repo.read alone; call workflow.verify_deployed_module and continue deploy/resume if /home/odoo/src/user does not match.",
            "If safe_deploy returns retry_later, wait briefly and call workflow.resume_job with the job_id. If it returns deploy_running, call jobs.get on the nested job. Do not repeat commit/push.",
            "Finish only after safe deploy/resume reports passed, or report the exact blocker and next_call returned by the workflow.",
        ]
        required_tools = [
            "repo.prepare",
            "repo.list_custom_modules",
            "repo.read",
            "workflow.frontend_pattern_preflight",
            "workflow.scope_advice",
            "repo.apply_patch_atomic",
            "repo.write_many_smart",
            "repo.apply_patch",
            "git.diff",
            "qa.self_run",
            "workflow.verify_against_core",
            "workflow.safe_deploy_module",
            "workflow.verify_deployed_module",
            "workflow.resume_job",
            "jobs.get",
        ]
        summary = "Canonical repo-first coding workflow for Odoo MCP development."
    return CodingTaskPlanResult(
        summary=summary,
        ordered_steps=steps,
        required_tools=required_tools,
        guardrails=[
            "Always prepare/pull the repo before reading or patching code.",
            "Always use the configured target branch; never push to default/main by assumption.",
            "Discover custom modules from repo manifests before relying on Odoo database state.",
            "Use atomic multi-file tools for broad changes so incomplete states are not left in the checkout.",
            "Only patch the requested module and related files.",
            "Do not upgrade Odoo.sh until the expected pushed SHA is deployed.",
            "Do not answer by printing MCP JSON blocks for the user/client to execute; call the MCP tools yourself.",
            "Do not create fields/views by direct ir.model.fields or ir.ui.view database writes for normal module-code requests.",
            "For small patches, avoid repeated tool discovery, broad source reads, full health checks, and browser diagnostics unless they are needed to prove the requested change.",
            "For UI/POS/website changes, rely on static frontend verification, Odoo.sh deployment proof, module upgrade, and registry smoke tests in the deployment pipeline.",
            "For JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, frontend_pattern_preflight is blocking: do not write code until real framework source patterns are read.",
        ],
        defaults={
            "repo_url": repo_url,
            "branch": branch,
            "project_path": project_path,
            "client_name": client.name if client else payload.client_name,
            "module_name": module_name,
            "task": payload.task,
            "small_patch_fast_path": small_patch,
            **({"checkout_guard": checkout_guard} if checkout_guard and checkout_guard.get("sync_attempted") else {}),
        },
    )


@router.get("/capability-check", response_model=CapabilityCheckResult)
def capability_check() -> CapabilityCheckResult:
    checks: dict[str, dict] = {}
    checks["allowed_roots"] = {"ok": bool(settings.allowed_roots), "value": [str(p) for p in settings.allowed_roots]}
    checks["git"] = {"ok": shutil.which("git") is not None, "path": shutil.which("git")}
    checks["ssh"] = {"ok": shutil.which("ssh") is not None, "path": shutil.which("ssh")}
    checks["odoo_shell_config"] = {"ok": all([
        os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url,
        os.environ.get("ODOO_DB_NAME") or settings.odoo_db_name,
        os.environ.get("ODOO_LOGIN") or settings.odoo_login,
        os.environ.get("ODOO_PASSWORD") or settings.odoo_password,
    ])}
    checks["odoosh_config"] = {"ok": bool(settings.odoosh_ssh_host), "host_configured": bool(settings.odoosh_ssh_host)}
    checks["state_dir"] = {"ok": settings.state_path.exists(), "path": str(settings.state_path)}
    checks["browser_artifact_dir"] = {"ok": Path(settings.screenshot_dir).expanduser().resolve().parent.exists(), "path": settings.screenshot_dir}
    cdp_url = os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url
    checks["cdp_browser"] = {
        "ok": bool(cdp_url),
        "url_configured": bool(cdp_url),
        "note": "Set ODOO_AGENT_CDP_URL to a Chrome remote debugging endpoint for UI console diagnostics.",
    }
    failed = [key for key, item in checks.items() if not item.get("ok")]
    if not failed:
        status = "ready"
    elif len(failed) < len(checks):
        status = "partial"
    else:
        status = "failed"
    return CapabilityCheckResult(status=status, checks=checks)




@router.post("/start-session", response_model=WorkflowReport)
def start_session(payload: StartSessionRequest) -> WorkflowReport:
    """V15 Multi-Client bootstrap for every MCP agent session.

    Pass client_name to auto-resolve repo, branch, path, token, and Odoo creds
    from ODOO_CLIENTS env var. Falls back to TARGET_GIT_* env vars if no client_name.

    Also confirms runtime capability, prepares the repo, reads git status, and
    lists custom modules. Live Odoo/Odoo.sh checks are opt-in because client.check
    already validates the selected client and remote checks can take minutes.
    """
    # ── Multi-Client Resolution ──────────────────────────────────────────
    resolver = ClientResolverService()
    client = resolver.resolve_client(
        client_name=payload.client_name,
        project_path=settings.target_project_path,
        require_complete=False,
    )
    if client:
        payload = payload.model_copy(update={
            "client_name": client.name,
            "repo_url": payload.repo_url or client.repo,
            "branch": payload.branch or client.branch,
            "repo_name": payload.repo_name or Path(client.path).name,
            "workspace_root": payload.workspace_root or str(Path(client.path).parent),
        })

    store = JsonStore()
    job = store.create_job(JobRecord(job_type="start_session", status=JobStatus.running, request=payload.model_dump(mode="json")))

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(current, JobStep(name=name, status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result, error=error))

    flags: list[str] = []
    cap = capability_check()
    step("capability_check", cap.status, cap)
    if cap.status != "ready":
        flags.append("warning")

    prepared = None
    status_result = None
    try:
        existing_client_path = Path(client.path) if client and client.path else None
        if existing_client_path and (existing_client_path / ".git").is_dir() and not payload.refresh_repo:
            git = GitService()
            target = git.validate_target(str(existing_client_path), payload.branch, payload.remote)
            if target.exit_code != 0:
                raise HTTPException(status_code=409, detail=target.stderr or target.stdout or "Configured checkout target validation failed")
            sha = git.head_sha(str(existing_client_path))
            status_result = git.status(str(existing_client_path))
            if sha.exit_code != 0 or status_result.exit_code != 0:
                raise HTTPException(status_code=409, detail=sha.stderr or status_result.stderr or "Configured checkout is not ready")
            prepared = RepoPrepareResult(
                project_path=str(existing_client_path.resolve()),
                repo_url=client.repo,
                branch=payload.branch or client.branch,
                sha=sha.stdout.strip(),
                status=status_result.stdout,
                already_cloned=True,
                steps=[target, sha, status_result],
            )
        else:
            prepared = RepoService().prepare_repo(
                payload.repo_url,
                payload.branch,
                payload.repo_name,
                payload.workspace_root,
                payload.remote,
                payload.timeout_seconds,
                client.path if client else settings.target_project_path,
                client.name if client else payload.client_name,
            )
        step("repo_prepare", "passed", prepared)
        if status_result is None:
            status_result = GitService().status(prepared.project_path)
        step("git_status", "passed" if status_result.exit_code == 0 else "warning", status_result)
        modules = RepoService().list_custom_modules(prepared.project_path, payload.max_depth, include_uninstallable=False)
        step("repo_list_custom_modules", "passed", {"count": len(modules), "modules": [m.model_dump(mode="json") for m in modules]})
    except Exception as exc:  # noqa: BLE001
        step("repo_prepare", "failed", error=str(exc))
        flags.append("failed")

    if payload.include_odoo:
        try:
            odoo_health = _odoo_service_for_client(client).health()
            step("odoo_health", "passed" if odoo_health.status == "ok" else "warning", odoo_health)
            if odoo_health.status != "ok":
                flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoo_health", "warning", error=str(exc))
            flags.append("warning")

    if payload.include_odoosh:
        try:
            sh_health = _odoosh_service_for_client(client).health()
            step("odoosh_health", "passed" if sh_health.exit_code == 0 else "warning", sh_health)
            if sh_health.exit_code != 0:
                flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoosh_health", "warning", error=str(exc))
            flags.append("warning")

    final = "failed" if "failed" in flags else "warning" if "warning" in flags else "passed"
    final_job_status = JobStatus.failed if final == "failed" else JobStatus.warning if final == "warning" else JobStatus.passed
    finished = store.finish_job(store.get_job(job.id), final_job_status, {"status": final, "project_path": prepared.project_path if prepared else None})
    # Fix 7: persist session state so a new session after Render restart can reload it
    store.save_session_state({
        "last_job_id": job.id,
        "last_sha": prepared.sha if prepared else None,
        "project_path": prepared.project_path if prepared else None,
        "branch": prepared.branch if prepared else None,
        "start_session_status": final,
        "client_name": client.name if client else payload.client_name or None,
        "client": client.public if client else None,
        "repo_url_safe": mask_repo_url(payload.repo_url or "") if payload.repo_url else None,
        "odoo_url": client.odoo_url if client else os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url,
        "odoo_db": client.odoo_db if client else os.environ.get("ODOO_DB_NAME") or settings.odoo_db_name,
    }, client_name=client.name if client else payload.client_name or None)
    return WorkflowReport(status=final, job_id=job.id, steps=finished.steps, summary={"status": final, "project_path": prepared.project_path if prepared else None, "sha": prepared.sha if prepared else None})


@router.post("/registry-smoke-test", response_model=OdooResult)
def registry_smoke_test(payload: RegistrySmokeTestRequest) -> OdooResult:
    ClientGateService().require_validated_client(payload.client_name, "workflow.registry_smoke_test")
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    return OdooService(client).registry_smoke_test(payload.module_name, payload.model_names, payload.menu_names, payload.strict)


@router.post("/restore-last-good-commit", response_model=WorkflowReport)
def restore_last_good_commit(payload: RestoreLastGoodCommitRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="restore_last_good_commit", status=JobStatus.running, request=payload.model_dump(mode="json")))
    git = GitService()

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(current, JobStep(name=name, status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result, error=error))

    try:
        project_path = str(resolve_allowed_path(payload.project_path))
        step("resolve_project", "passed", {"project_path": project_path})
        target = git.validate_target(project_path, payload.branch or settings.target_git_branch, payload.remote)
        step("git_validate_target", "passed" if target.exit_code == 0 else "failed", target)
        if target.exit_code != 0:
            finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_validate_target"})
            return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_validate_target"})
        revert = git.revert_commit(project_path, payload.bad_sha, payload.timeout_seconds)
        step("git_revert_bad_sha", "passed" if revert.exit_code == 0 else "failed", revert)
        if revert.exit_code != 0:
            finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_revert_bad_sha"})
            return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_revert_bad_sha"})
        if payload.push:
            pushed = git.push(project_path, payload.remote, payload.branch or settings.target_git_branch, True, payload.timeout_seconds)
            step("git_push_restore", "passed" if pushed.exit_code == 0 else "failed", pushed)
            if pushed.exit_code != 0:
                finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_push_restore"})
                return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_push_restore"})
    except Exception as exc:  # noqa: BLE001
        step("exception", "failed", error=str(exc))
        finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"error": str(exc)})
        return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"error": str(exc)})
    finished = store.finish_job(store.get_job(job.id), JobStatus.passed, {"bad_sha": payload.bad_sha, "pushed": payload.push})
    return WorkflowReport(status="passed", job_id=job.id, steps=finished.steps, summary={"bad_sha": payload.bad_sha, "pushed": payload.push})


@router.post("/browser-menu-crawl", response_model=WorkflowReport)
async def browser_menu_crawl(payload: MenuCrawlerRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="browser_menu_crawl", status=JobStatus.running, request=payload.model_dump(mode="json")))
    flags: list[str] = []
    failed = False
    for index, path in enumerate(payload.paths, start=1):
        result = await CdpBrowserService().screenshot_odoo_page(payload.base_url, payload.login, payload.password, path, f"{payload.screenshot_prefix}_{index}.png", payload.timeout_seconds, full_page=True)
        status_ = "passed" if result.status == "ok" and not result.console_errors and not result.network_errors else "failed" if result.status == "failed" else "warning"
        if status_ == "failed":
            failed = True
        elif status_ != "passed":
            flags.append("warning")
        store.add_step(store.get_job(job.id), JobStep(name=f"browser_path:{path}", status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json")))
    final = "failed" if failed else "warning" if flags else "passed"
    job_status = JobStatus.failed if failed else JobStatus.warning if flags else JobStatus.passed
    finished = store.finish_job(store.get_job(job.id), job_status, {"status": final, "paths": payload.paths})
    return WorkflowReport(status=final, job_id=job.id, steps=finished.steps, summary={"status": final, "paths": payload.paths})


@router.post("/full-verify", response_model=WorkflowReport)
async def full_verify(payload: FullVerifyRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="full_verify", status=JobStatus.running, request=payload.model_dump(mode="json")))

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current_job = store.get_job(job.id)
        store.add_step(
            current_job,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                error=error,
            ),
        )

    status_flags: list[str] = []

    try:
        project_path = resolve_allowed_path(payload.project_path)
        module_path = project_path / (payload.module_root_rel or payload.module_name)
        step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path)})
    except Exception as exc:  # noqa: BLE001
        step("resolve_project", "failed", error=str(exc))
        finished = store.finish_job(job, JobStatus.failed, {"reason": "resolve_project failed"})
        return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "resolve_project"})

    if payload.clean_pyc:
        try:
            result = RepoService().clean_pyc(
                str(project_path),
                payload.module_root_rel or payload.module_name,
            )
            step("clean_pyc", "passed", result)
        except Exception as exc:  # noqa: BLE001
            step("clean_pyc", "failed", error=str(exc))
            status_flags.append("failed")

    qa_report = self_run(payload)
    step("qa_self_run", qa_report.status, qa_report)
    if qa_report.status == "failed":
        status_flags.append("failed")
    elif qa_report.status == "warning":
        status_flags.append("warning")

    if payload.include_git_diff:
        git_result = GitService().diff(str(project_path), staged=False, timeout_seconds=30)
        step("git_diff", "passed" if git_result.exit_code == 0 else "warning", git_result)
        if git_result.exit_code != 0:
            status_flags.append("warning")

    if payload.include_odoosh_errors:
        try:
            client = ClientResolverService().resolve_client(client_name=payload.client_name, project_path=payload.project_path, require_complete=False)
            err_result = _odoosh_service_for_client(client).grep_errors(tail_lines=300)
            status_ = "passed" if err_result.exit_code == 0 else "warning"
            step("odoosh_grep_errors", status_, err_result)
            if "Traceback" in err_result.stdout or "ERROR" in err_result.stdout:
                status_flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoosh_grep_errors", "warning", error=str(exc))
            status_flags.append("warning")

    if payload.include_odoo_module_status:
        client = ClientResolverService().resolve_client(client_name=payload.client_name, project_path=payload.project_path, require_complete=False)
        result = _odoo_service_for_client(client).module_status(payload.module_name)
        step("odoo_module_status", "passed" if result.status == "ok" else "warning", result)
        if result.status != "ok":
            status_flags.append("warning")

    if payload.take_browser_screenshot:
        result = await CdpBrowserService().screenshot_odoo_page(
            None,
            None,
            None,
            payload.browser_start_path,
            payload.screenshot_name,
            timeout_seconds=45,
            full_page=True,
        )
        step("browser_screenshot", "passed" if result.status == "ok" else "warning", result)
        if result.status != "ok" or result.console_errors or result.network_errors:
            status_flags.append("warning")

    final_status = "failed" if "failed" in status_flags else "warning" if "warning" in status_flags else "passed"
    final_job_status = JobStatus.failed if final_status == "failed" else JobStatus.warning if final_status == "warning" else JobStatus.passed
    fresh_job = store.get_job(job.id)
    finished = store.finish_job(
        fresh_job,
        final_job_status,
        {"status": final_status, "module_name": payload.module_name, "project_path": str(project_path)},
    )
    return WorkflowReport(
        status=final_status,
        job_id=job.id,
        steps=finished.steps,
        summary={
            "module_name": payload.module_name,
            "project_path": str(project_path),
            "step_count": len(finished.steps),
            "result": final_status,
        },
    )


def _wait_workflow_job_until_terminal(store: JsonStore, job_id: str, poll_interval_seconds: float, timeout_seconds: int | None = None) -> JobRecord:
    """Block a wait_for_result safe-deploy call until the owning job is final.

    The safe-deploy worker contains its own bounded deploy/upgrade/fix loop. A
    caller that explicitly asks for `wait_for_result=true` should receive the
    terminal outcome of that loop, not an intermediate "running" message that
    shifts confirmation back to the user.
    """
    # timeout_seconds=0/None remains an explicit hard wait. JsonStore owns the
    # single bounded polling implementation used by every router.
    bounded_timeout = 0 if timeout_seconds is None or int(timeout_seconds) <= 0 else int(timeout_seconds)
    return store.wait_job(job_id, bounded_timeout, poll_interval_seconds)


@router.post("/safe-deploy-module", response_model=WorkflowReport)
async def safe_deploy_module(payload: SafeDeployModuleRequest) -> WorkflowReport:
    client_gate = ClientGateService()
    try:
        validation_lease = client_gate.issue_job_validation_lease(
            payload.client_name,
            action="workflow.safe_deploy_module",
            project_path=payload.project_path,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        validation = detail.get("validation") if isinstance(detail, dict) else None
        fallback_allowed = bool(
            payload.github_push_fallback
            and settings.github_push_fallback_enabled
            and isinstance(validation, dict)
            and validation.get("github_push_ready")
            and validation.get("deploy_fallback_eligible")
        )
        if not fallback_allowed:
            raise
        validation_lease = client_gate.issue_job_validation_lease(
            payload.client_name,
            action="workflow.safe_deploy_module.github_fallback",
            project_path=payload.project_path,
        )
        validation_lease["github_push_fallback"] = True
        validation_lease["deploy_fallback_reason"] = validation.get("deploy_fallback_reason")
        validation_lease["fingerprint"] = client_gate._job_lease_fingerprint(validation_lease)

    """Start the single owner deploy pipeline for one project/module.

    This entry point is intentionally idempotent for the same
    ``project_path + module_name``. Some MCP clients retry a tool call when the
    HTTP stream times out even though the first request already persisted a
    background job. Before creating a new JobRecord, we look for an existing
    active or resumable safe_deploy_module job and return that job instead of
    spawning a second thread that could race git commit/push or Odoo.sh module
    upgrade.
    """

    def _existing_job_report(job: JobRecord) -> WorkflowReport:
        request = job.request or {}
        result = job.result if isinstance(job.result, dict) else {}
        result_status = result.get("status")
        job_status = getattr(job.status, "value", str(job.status))
        block_payload = safe_deploy_block_payload("safe_deploy_module", job)

        if job.status in {JobStatus.queued, JobStatus.running}:
            report_status = "already_in_progress"
            message = (
                "A workflow.safe_deploy_module job is already in progress for this "
                "project_path + module_name. No new deploy thread was started; "
                "poll the existing job instead of replaying commit/push/upgrade or resume."
            )
            next_call = {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
            blocking_reason = "active_job"
        elif job.status == JobStatus.warning:
            report_status = "resumable_existing_job"
            message = (
                "A previous workflow.safe_deploy_module job for this project_path + "
                "module_name stopped in a warning/resumable state. No new deploy "
                "thread was started; resume the existing job, or call "
                "safe_deploy_module with force_new_deploy=true if a fresh deploy "
                "is genuinely required."
            )
            next_call = {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
            blocking_reason = "resumable_job"
        elif result_status in _RESUMABLE_SAFE_DEPLOY_RESULTS:
            report_status = "resumable_existing_job"
            message = (
                "A previous workflow.safe_deploy_module job for this project_path + "
                "module_name stopped in a resumable state. No new deploy thread was "
                "started; resume the existing job, or call safe_deploy_module with "
                "force_new_deploy=true if a fresh deploy is genuinely required."
            )
            next_call = {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
            blocking_reason = "resumable_job"
        else:
            report_status = "already_in_progress"
            # Defensive fallback; find_blocking_safe_deploy should only return
            # active/resumable safe deploy jobs.
            message = block_payload.get("message") or "Existing safe deploy job found."
            next_call = block_payload.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
            blocking_reason = "existing_job"

        return WorkflowReport(
            status=report_status,
            job_id=job.id,
            steps=job.steps,
            deploy_confirmed=job.deploy_confirmed,
            verified_already_deployed=job.verified_already_deployed,
            already_deployed_confirmed=job.already_deployed_confirmed,
            summary={
                **block_payload,
                **result,
                "status": report_status,
                "blocking_reason": blocking_reason,
                "message": message,
                "job_id": job.id,
                "job_status": job_status,
                "result_status": result_status,
                "module_name": request.get("module_name") or payload.module_name,
                "project_path": request.get("project_path") or payload.project_path,
                "force_new_deploy": False,
                "completion_required": job.status in {JobStatus.queued, JobStatus.running},
                "success_allowed": bool(job.deploy_confirmed or job.already_deployed_confirmed),
                "tool_exit_allowed": job.status not in {JobStatus.queued, JobStatus.running},
                "poll_after_seconds": 1 if job.status in {JobStatus.queued, JobStatus.running} else None,
                "next_call": next_call,
            },
        )

    try:
        approval = ApprovalService()
        approval.ensure_allowed("workflow.safe_deploy_module", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))

        with _SAFE_DEPLOY_ENTRY_LOCK:
            existing_job = None
            current_diff_fingerprint = _scoped_diff_fingerprint(payload)
            if not payload.force_new_deploy:
                existing_job = find_blocking_safe_deploy(
                    project_path=payload.project_path,
                    module_name=payload.module_name,
                    client_name=payload.client_name,
                )
                if (
                    existing_job is not None
                    and existing_job.status not in {JobStatus.queued, JobStatus.running}
                    and (
                        _has_fresh_scoped_changes(payload)
                        or (
                            current_diff_fingerprint is not None
                            and current_diff_fingerprint != (existing_job.request or {}).get("diff_fingerprint")
                        )
                    )
                ):
                    # The old resumable job owns an earlier pushed SHA. A new verified
                    # scoped diff must create a fresh commit/push instead of being
                    # redirected back to that stale deployment checkpoint. Active jobs
                    # remain exclusive and are never bypassed.
                    existing_job = None
            if existing_job is not None:
                return _existing_job_report(existing_job)

            store = JsonStore()
            job_request = payload.model_dump(mode="json")
            job_request["validation_lease"] = validation_lease
            job_request["diff_fingerprint"] = current_diff_fingerprint
            job = store.create_job(JobRecord(
                job_type="safe_deploy_module",
                status=JobStatus.running,
                request=job_request,
            ))
            store.save_session_state({
                "last_safe_deploy_job_id": job.id,
                "module_name": payload.module_name,
                "project_path": payload.project_path,
                "branch": payload.branch,
                "client_name": payload.client_name,
                "safe_deploy_status": "running",
            }, client_name=payload.client_name)

        def _background():
            try:
                _run_safe_deploy(payload, job.id)
            except Exception as exc:
                try:
                    store2 = JsonStore()
                    j = store2.get_job(job.id)
                    if j.status == JobStatus.running:
                        store2.finish_job(j, JobStatus.failed, {"error": f"Unhandled exception: {exc}"})
                except Exception:
                    # The foreground response already returned the job id. Avoid
                    # leaking background exception handling back to the MCP stream.
                    pass

        submit_background(_background)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return WorkflowReport(
            status="failed",
            job_id="",
            steps=[
                JobStep(
                    name="safe_deploy_start",
                    status="failed",
                    error=str(exc),
                )
            ],
                deploy_confirmed=False,
                verified_already_deployed=False,
                already_deployed_confirmed=False,
            summary={
                "message": "Safe deploy could not start. No commit or push was attempted.",
                "error": str(exc),
                "module_name": payload.module_name,
            },
        )

    if payload.wait_for_result:
        waited = await asyncio.to_thread(
            _wait_workflow_job_until_terminal,
            store,
            job.id,
            payload.poll_interval_seconds,
            payload.wait_timeout_seconds,
        )
        if waited.status in {JobStatus.queued, JobStatus.running}:
            return WorkflowReport(
                status="running",
                job_id=job.id,
                steps=waited.steps,
                deploy_confirmed=False,
                verified_already_deployed=False,
                already_deployed_confirmed=False,
                summary={
                    "status": "running",
                    "message": (
                        f"Safe deploy is still running after waiting {payload.wait_timeout_seconds}s. "
                        "This is not a final result. The agent/provider must continue with jobs.get "
                        "until status is passed/failed/warning and deploy_confirmed or already_deployed_confirmed is true for success."
                    ),
                    "completion_required": True,
                    "success_allowed": False,
                    "tool_exit_allowed": False,
                    "poll_after_seconds": 1,
                    "job_id": job.id,
                    "module_name": payload.module_name,
                    "next_call": {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
                },
            )
        final_status: Literal["passed", "failed", "warning", "running"] = "passed" if waited.status == JobStatus.passed else "failed" if waited.status == JobStatus.failed else "warning"
        result = waited.result if isinstance(waited.result, dict) else {"result": waited.result}
        return WorkflowReport(
            status=final_status,
            job_id=job.id,
            steps=waited.steps,
            deploy_confirmed=waited.deploy_confirmed,
            verified_already_deployed=waited.verified_already_deployed,
            already_deployed_confirmed=waited.already_deployed_confirmed,
            summary={
                **result,
                "job_id": job.id,
                "module_name": payload.module_name,
                "completion_required": False,
                "success_allowed": bool(waited.deploy_confirmed or waited.already_deployed_confirmed),
                "tool_exit_allowed": True,
                "message": result.get("message") or f"Safe deploy job finished with status {waited.status.value}.",
            },
        )

    return WorkflowReport(
        status="running",
        job_id=job.id,
        steps=[],
        deploy_confirmed=False,
        verified_already_deployed=False,
        already_deployed_confirmed=False,
        summary={
            "status": "running",
            "message": "Safe deploy is still running. This is not a final result; the agent/provider must continue with jobs.get until terminal confirmation. Do not report success from this response.",
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 1,
            "job_id": job.id,
            "module_name": payload.module_name,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
        },
    )


def _pre_push_verify_files(
    project: Path,
    module_name: str,
    paths: list[str],
    status_output: str,
    module_root_rel: str | None = None,
) -> list[str]:
    """Return completed written module files that must pass pre-push verification."""
    allowed_suffixes = (".py", ".xml", ".js", ".scss", ".css", ".csv", "__manifest__.py")
    module_root = (module_root_rel or module_name).strip("/")
    module_prefix = f"{module_root}/"

    def status_candidates() -> list[str]:
        found: list[str] = []
        for raw_line in status_output.splitlines():
            if not raw_line or raw_line.startswith("##"):
                continue
            path_part = raw_line[3:].strip() if len(raw_line) > 3 else raw_line.strip()
            if " -> " in path_part:
                path_part = path_part.rsplit(" -> ", 1)[-1].strip()
            if path_part:
                found.append(path_part)
        return found

    def collect(candidates: list[str]) -> list[str]:
        written: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            rel = candidate.replace("\\", "/").strip()
            if not rel or rel.startswith(".git/"):
                continue
            target = (project / rel).resolve()
            if not (target == project or project in target.parents):
                continue
            targets = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else []
            for file_target in targets:
                rel_posix = file_target.relative_to(project).as_posix()
                if not (rel_posix == f"{module_root}/__manifest__.py" or rel_posix.startswith(module_prefix)):
                    continue
                if not rel_posix.endswith(allowed_suffixes):
                    continue
                value = str(file_target)
                if value not in seen:
                    seen.add(value)
                    written.append(value)
        return written

    written = collect(paths) if paths else []
    if written:
        return written
    return collect(status_candidates())


def _expected_user_file_hashes(project: Path, verify_files: list[str]) -> dict[str, str]:
    """Return repo-relative SHA-256 hashes for changed files deployed to ~/src/user."""
    expected: dict[str, str] = {}
    root = project.resolve()
    for raw_path in verify_files:
        file_path = Path(raw_path).resolve()
        if not file_path.is_file() or not (file_path == root or root in file_path.parents):
            continue
        rel = file_path.relative_to(root).as_posix()
        if not rel or rel.startswith(".git/"):
            continue
        expected[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return expected


def _deployed_compare_files(
    project: Path,
    module_name: str,
    paths: list[str],
    max_files: int,
    module_root_rel: str | None = None,
) -> list[str]:
    if paths:
        return _pre_push_verify_files(project, module_name, paths, "", module_root_rel)
    allowed_suffixes = (".py", ".xml", ".js", ".scss", ".css", ".csv", "__manifest__.py")
    module_path = (project / (module_root_rel or module_name)).resolve()
    if not module_path.is_dir() or not (module_path == project or project in module_path.parents):
        return []
    files: list[str] = []
    for file_path in sorted(p for p in module_path.rglob("*") if p.is_file()):
        rel = file_path.relative_to(project).as_posix()
        if rel.startswith(".git/") or not rel.endswith(allowed_suffixes):
            continue
        files.append(str(file_path))
        if len(files) >= max_files:
            break
    return files


@router.post("/verify-deployed-module")
def verify_deployed_module(payload: DeployedModuleVerifyRequest) -> dict:
    """Compare active checkout files to real Odoo.sh /home/odoo/src/user files.

    This is the source-of-truth check for claims like "already added", "already
    deployed", or "live now". repo.read only proves the GitHub/local checkout;
    this proves the Odoo.sh user source.
    """
    project = resolve_allowed_path(payload.project_path)
    compare_files = _deployed_compare_files(
        project,
        payload.module_name,
        payload.paths,
        payload.max_files,
        payload.module_root_rel,
    )
    expected_hashes = _expected_user_file_hashes(project, compare_files)
    client = ClientResolverService().resolve_client(client_name=payload.client_name, project_path=payload.project_path, require_complete=False)
    odoosh = _odoosh_service_for_client(client)
    list_result = odoosh.list_user_files(payload.module_name, max_items=min(payload.max_files, 2000))
    if expected_hashes:
        hash_result = odoosh.verify_user_file_hashes(expected_hashes)
        source_status = "matched" if hash_result.exit_code == 0 else "mismatch"
    else:
        hash_result = CommandResult(
            command=["workflow.verify_deployed_module"],
            cwd=str(project),
            exit_code=2,
            stderr="No local module code/XML/assets files were found to compare.",
        )
        source_status = "no_local_files"
    module_status = None
    if payload.include_module_status:
        module_status = _odoo_service_for_client(client).module_status(payload.module_name).model_dump(mode="json")
    return {
        "status": "passed" if source_status == "matched" else "failed",
        "source_status": source_status,
        "message": (
            "Odoo.sh /home/odoo/src/user matches the active checkout for the compared files."
            if source_status == "matched"
            else "Odoo.sh /home/odoo/src/user does not prove the active checkout is deployed. Do not claim live/deployed/already done."
        ),
        "module_name": payload.module_name,
        "project_path": str(project),
        "compared_files": sorted(expected_hashes),
        "odoosh_user_files": list_result,
        "hash_check": hash_result.model_dump(mode="json"),
        "module_status": module_status,
    }



def _infer_registry_fields_from_python(
    project: Path,
    module_name: str,
    verify_files: list[str],
    module_root_rel: str | None = None,
) -> dict[str, list[str]]:
    """Infer Odoo model fields from changed Python files for post-upgrade smoke.

    This catches the exact class of false success where a field was committed but
    the live registry never loaded it.  It intentionally only reads the active
    checkout and verifies after upgrade through ir.model.fields.
    """
    root = project.resolve()
    inferred: dict[str, set[str]] = {}
    for raw_path in verify_files:
        file_path = Path(raw_path).resolve()
        if file_path.suffix != ".py" or not file_path.is_file() or not (file_path == root or root in file_path.parents):
            continue
        rel = file_path.relative_to(root).as_posix()
        if not rel.startswith(f"{module_root_rel or module_name}/"):
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            explicit_name: str | None = None
            inherited_names: list[str] = []
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                targets = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
                if not targets:
                    continue
                value = stmt.value
                names: list[str] = []
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    names = [value.value]
                elif isinstance(value, (ast.List, ast.Tuple)):
                    names = [
                        elt.value
                        for elt in value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                if "_name" in targets and names:
                    explicit_name = names[0]
                if "_inherit" in targets and names:
                    inherited_names.extend(names)
            # A class with both _name and mixin _inherit owns its fields on the
            # explicit model.  Only pure extension classes attach fields to the
            # inherited model.
            model_name = explicit_name or (inherited_names[0] if inherited_names else None)
            if not model_name:
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                is_odoo_field = isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == "fields"
                if not is_odoo_field:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        inferred.setdefault(model_name, set()).add(target.id)
    return {model: sorted(fields) for model, fields in sorted(inferred.items()) if fields}


def _normalise_expected_registry_fields(fields_by_model: dict[str, list[str]] | None) -> dict[str, list[str]]:
    normalised: dict[str, list[str]] = {}
    for model, names in (fields_by_model or {}).items():
        if not isinstance(model, str) or not model.strip():
            continue
        clean_names = sorted({str(name).strip() for name in names or [] if str(name).strip()})
        if clean_names:
            normalised[model.strip()] = clean_names
    return normalised


def _verify_registry_fields(fields_by_model: dict[str, list[str]], client=None) -> OdooResult:
    shell = _odoo_service_for_client(client)
    missing: dict[str, list[str]] = {}
    found: dict[str, list[str]] = {}
    raw_results: dict[str, object] = {}
    for model, field_names in sorted(fields_by_model.items()):
        result = shell.search_read(
            "ir.model.fields",
            [("model", "=", model), ("name", "in", field_names)],
            ["model", "name", "ttype", "field_description"],
            limit=max(len(field_names) + 5, 20),
            offset=0,
            order="name asc",
            context=None,
        )
        raw_results[model] = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if result.status != "ok":
            return OdooResult(status="failed", error=f"Could not verify registry fields for {model}: {result.error}", result={"raw_results": raw_results})
        records = result.result or []
        present = sorted({record.get("name") for record in records if isinstance(record, dict) and record.get("name")})
        found[model] = present
        absent = sorted(set(field_names) - set(present))
        if absent:
            missing[model] = absent
    if missing:
        return OdooResult(
            status="failed",
            error="Live Odoo registry is missing expected fields after module upgrade.",
            result={"expected": fields_by_model, "found": found, "missing": missing, "raw_results": raw_results},
        )
    return OdooResult(status="ok", result={"expected": fields_by_model, "found": found, "raw_results": raw_results})



def _normalise_safe_deploy_paths(
    project: Path,
    module_name: str,
    paths: list[str],
    module_root_rel: str | None = None,
) -> list[str]:
    """Validate deploy paths against the exact checkout safe_deploy will commit/push.

    This prevents the failure mode where the agent edits one checkout (for example
    /repos/test_18) but calls safe_deploy_module with another checkout (for example
    /workspace/repos/example_repo). Absolute paths are accepted only when they resolve
    inside the selected project, then they are converted to repo-relative paths.
    """
    clean: list[str] = []
    module_root = str(module_root_rel or module_name).replace("\\", "/").strip("/")
    seen: set[str] = set()
    for raw in paths or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if "\x00" in value:
            raise ValueError("Deploy path contains a NUL byte")
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not (resolved == project or project in resolved.parents):
                raise ValueError(
                    "PATH_MISMATCH: deploy path is outside project_path. "
                    f"project_path={project}; deploy_path={resolved}. "
                    "Edit and deploy must use the same checkout returned by repo.prepare."
                )
            rel = resolved.relative_to(project).as_posix()
        else:
            resolved = (project / candidate).resolve()
            if not (resolved == project or project in resolved.parents):
                raise ValueError(f"Deploy path escapes project_path: {value}")
            rel = resolved.relative_to(project).as_posix()
        if rel in {"", "."}:
            continue
        if rel == ".git" or rel.startswith(".git/"):
            raise ValueError("Deploy path inside .git is blocked")
        # Safe deploy is module-scoped. Refuse accidental unrelated files unless the
        # caller intentionally passes no paths and lets git add -A capture all changes.
        if module_root and not (rel == module_root or rel.startswith(f"{module_root}/")):
            raise ValueError(
                "DEPLOY_PATH_OUTSIDE_MODULE: safe_deploy_module received a path outside the target module. "
                f"module_name={module_name}; module_root_rel={module_root}; path={rel}. "
                "Use the correct module or split the deploy."
            )
        if rel not in seen:
            seen.add(rel)
            clean.append(rel)
    return clean


def _changed_file_set(result: CommandResult) -> set[str]:
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _paths_touch_changed_files(deploy_paths: list[str], changed_files: set[str]) -> bool:
    if not deploy_paths:
        return bool(changed_files)
    for rel in deploy_paths:
        if rel in changed_files:
            return True
        prefix = rel.rstrip("/") + "/"
        if any(item.startswith(prefix) for item in changed_files):
            return True
    return False


def _deploy_request_has_module_changes(
    module_name: str,
    deploy_paths: list[str],
    changed_files: set[str],
    module_root_rel: str | None = None,
) -> bool:
    """Return True when the safe-deploy request has local changes for this module scope.

    Empty ``paths`` means the caller did not name a specific file, not that a
    README-only diff should be treated as a module deploy. In that case, require
    at least one changed file under ``module_name`` before commit/push.
    """
    if deploy_paths:
        return _paths_touch_changed_files(deploy_paths, changed_files)
    module_root = (module_root_rel or module_name).rstrip("/")
    module_prefix = module_root + "/"
    return any(rel == module_root or rel.startswith(module_prefix) for rel in changed_files)


def _safe_deploy_already_deployed_result(
    *,
    project_path: Path,
    module_name: str,
    deploy_paths: list[str],
    changed_files: set[str],
    verify_result: dict[str, object],
) -> dict[str, object]:
    source_status = str(verify_result.get("source_status") or "unknown")
    matched = source_status == "matched"
    return {
        "status": "already_deployed_confirmed" if matched else "already_deployed_not_confirmed",
        "message": (
            f"No local changes to push. Verified against Odoo.sh: {source_status}. "
            "Do not claim already deployed/already added/already live unless "
            "verified_already_deployed is true."
        ),
        "module_name": module_name,
        "project_path": str(project_path),
        "deploy_paths": deploy_paths,
        "changed_files": sorted(changed_files),
        "no_changes_detected": True,
        "source_pushed": False,
        "source_on_odoosh": matched,
        "source_deployed": matched,
        "module_upgraded": False,
        "ui_checked": False,
        "visible_change_confirmed": False,
        "deploy_confirmed": False,
        "verified_already_deployed": matched,
        "already_deployed_confirmed": matched,
        "verify_deployed_module": verify_result,
        "next_call": None if matched else {
            "tool": "workflow.verify_deployed_module",
            "arguments": {"project_path": str(project_path), "module_name": module_name, "paths": deploy_paths},
        },
    }


def _assert_head_commit_contains_paths(git: GitService, project_path: Path, deploy_paths: list[str]) -> CommandResult:
    if not hasattr(git, "head_changed_files"):
        return CommandResult(command=["git", "head-commit-path-gate", "legacy-fake"], cwd=str(project_path), exit_code=0, stdout="Skipped only because GitService test double has no head_changed_files().\n")
    changed = git.head_changed_files(str(project_path))
    if changed.exit_code != 0:
        return changed
    head_files = {line.strip() for line in (changed.stdout or "").splitlines() if line.strip()}
    if not head_files:
        return CommandResult(command=["git", "diff-tree", "--name-only", "HEAD"], cwd=str(project_path), exit_code=1, stderr="HEAD commit contains no changed files; refusing deploy.")
    if deploy_paths and not _paths_touch_changed_files(deploy_paths, head_files):
        return CommandResult(
            command=["git", "diff-tree", "--name-only", "HEAD"],
            cwd=str(project_path),
            exit_code=1,
            stdout="\n".join(sorted(head_files)) + "\n",
            stderr=(
                "HEAD commit does not contain the requested deploy paths. "
                "This usually means the agent edited a different checkout than project_path."
            ),
        )
    return CommandResult(command=["git", "head-commit-path-gate"], cwd=str(project_path), exit_code=0, stdout="HEAD commit contains expected deploy files.\n")

def _wait_nested_module_job(
    store: JsonStore,
    nested_job_id: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> JobRecord | None:
    """Poll a nested odoo.module_deploy job until terminal or hard timeout.

    The safe-deploy worker must not report an intermediate module-upgrade state
    as a completed deploy. A timeout is therefore returned as a failed upgrade
    attempt so the bounded error-pack/fix/retry loop can continue in the same
    safe-deploy job.
    """
    timeout = max(int(timeout_seconds or 0), 0)
    interval = max(min(float(poll_interval_seconds or 10.0), 60.0), 0.25)
    deadline = time.monotonic() + timeout
    while True:
        waited = store.get_job(nested_job_id)
        if waited.status not in {JobStatus.queued, JobStatus.running}:
            return waited
        if time.monotonic() >= deadline:
            return None
        sleep_for = min(interval, max(deadline - time.monotonic(), 0.0))
        time.sleep(max(sleep_for, 0.25))


def _start_and_wait_module_deploy(payload: SafeDeployModuleRequest) -> OdooResult:
    module_request = OdooModuleRequest(
        client_name=payload.client_name,
        module_name=payload.module_name,
        wait_for_result=True,
        wait_timeout_seconds=min(max(int(payload.odoo_module_deploy_timeout_seconds or 1), 1), 25),
        poll_interval_seconds=max(min(float(payload.odoo_module_deploy_poll_interval_seconds or 10.0), 10.0), 0.25),
    )
    pinned_client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path,
        require_complete=False,
    )
    try:
        started = start_odoo_module_deploy_job(
            module_request,
            run_inline=True,
            validated_client=pinned_client,
        )
    except TypeError:
        # Compatibility with existing test doubles and external overrides.
        started = start_odoo_module_deploy_job(module_request)
    started_payload = started.model_dump(mode="json") if hasattr(started, "model_dump") else started
    result_payload = started_payload.get("result") if isinstance(started_payload, dict) and isinstance(started_payload.get("result"), dict) else {}
    if result_payload.get("finished") is True or not result_payload.get("job_id"):
        return started

    nested_job_id = str(result_payload.get("job_id"))
    waited = _wait_nested_module_job(
        JsonStore(),
        nested_job_id,
        payload.odoo_module_deploy_timeout_seconds,
        payload.odoo_module_deploy_poll_interval_seconds,
    )
    if waited is None:
        return OdooResult(
            status="failed",
            error=(
                f"Odoo module deploy did not reach a terminal pass/fail state within "
                f"{payload.odoo_module_deploy_timeout_seconds}s while wait_for_result=true."
            ),
            result={
                "job_id": nested_job_id,
                "job_status": "timeout",
                "finished": True,
                "status": "module_upgrade_timeout",
            },
        )

    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = waited.result.get("error") if isinstance(waited.result, dict) else None
        if not result_error and isinstance(waited.result, dict):
            result_error = waited.result.get("message")
        return OdooResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            error=result_error or f"Odoo module deploy job finished with status={waited.status.value}",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
        )
    return OdooResult(
        status="failed",
        error=f"Odoo module deploy returned unexpected non-terminal job status={waited.status.value}",
        result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
    )


def _poll_odoosh_deployed(
    odoosh: OdooShService,
    pushed_sha: str,
    timeout_seconds: int,
    interval_seconds: float,
    progress_cb=None,
) -> CommandResult:
    """Compatibility wrapper around the shared Odoo.sh commit polling loop."""
    poller = getattr(odoosh, "poll_deployed_commit", None)
    if callable(poller):
        return poller(
            expected_sha=pushed_sha,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            progress_cb=progress_cb,
        )
    # Test fakes and older service-like adapters may only implement
    # check_commit_deployed(); still exercise the same shared loop.
    from ..services.odoosh_service import OdooShService as SharedOdooShService

    return SharedOdooShService.poll_deployed_commit(
        odoosh,
        expected_sha=pushed_sha,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        progress_cb=progress_cb,
    )



def _safe_deploy_payload(value) -> dict[str, object]:
    """Return a plain dict for Pydantic/model-like command results."""
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    return value if isinstance(value, dict) else {"value": value}


def _module_upgrade_success(value) -> bool:
    """True only for explicit finished/success module-upgrade proof."""
    payload = _safe_deploy_payload(value)
    if payload.get("status") != "ok":
        return False
    nested = payload.get("result")
    if not isinstance(nested, dict):
        return False
    if nested.get("finished") is not True:
        return False
    job_status = nested.get("job_status")
    if job_status is not None and str(job_status) not in {JobStatus.passed.value, "ok", "passed"}:
        return False
    nested_job = nested.get("job")
    if isinstance(nested_job, dict):
        nested_status = nested_job.get("status")
        if nested_status is not None and str(nested_status) != JobStatus.passed.value:
            return False
    return True


def _module_upgrade_running(value) -> bool:
    payload = _safe_deploy_payload(value)
    nested = payload.get("result")
    if not isinstance(nested, dict):
        return False
    if nested.get("finished") is False:
        return True
    return str(nested.get("job_status") or "") in {JobStatus.running.value, JobStatus.queued.value}


def _extract_error_pack_json(error_pack: CommandResult | None) -> dict[str, object]:
    if error_pack is None:
        return {}
    stdout = error_pack.stdout or ""
    marker = "MCP_ERROR_PACK"
    if marker not in stdout:
        return {}
    after_marker = stdout.split(marker, 1)[1]
    json_part = after_marker.split("TRACEBACK_BLOCKS", 1)[0].strip()
    if not json_part:
        return {}
    try:
        parsed = json.loads(json_part)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _safe_deploy_error_text(deploy_result=None, error_pack: CommandResult | None = None) -> str:
    parts: list[str] = []
    if deploy_result is not None:
        payload = _safe_deploy_payload(deploy_result)
        for key in ("error", "message"):
            value = payload.get(key)
            if value:
                parts.append(str(value))
        nested = payload.get("result")
        if isinstance(nested, dict):
            nested_job = nested.get("job")
            if isinstance(nested_job, dict):
                nested_result = nested_job.get("result")
                if isinstance(nested_result, dict):
                    for key in ("error", "message"):
                        value = nested_result.get(key)
                        if value:
                            parts.append(str(value))
                elif nested_result:
                    parts.append(str(nested_result))
            for key in ("error", "message"):
                value = nested.get(key)
                if value:
                    parts.append(str(value))
    if error_pack is not None:
        pack_json = _extract_error_pack_json(error_pack)
        if pack_json:
            hints = pack_json.get("hints")
            files = pack_json.get("files")
            xml_locations = pack_json.get("xml_locations")
            parts.append(
                "odoosh.error_pack: "
                f"hints={hints or []}; files={files or []}; xml_locations={xml_locations or []}"
            )
        text = "\n".join(value for value in (error_pack.stdout, error_pack.stderr) if value)
        if text:
            parts.append(text[-4000:])
    clean: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = str(part).strip()
        if value and value not in seen:
            seen.add(value)
            clean.append(value)
    return "\n\n".join(clean) or "Module upgrade failed; odoosh.error_pack returned no additional detail."


def _error_pack_implicated_files(
    *,
    project_path: Path,
    module_name: str,
    error_pack: CommandResult | None,
    fallback_files: list[str],
) -> list[str]:
    """Map Odoo.sh traceback file paths back to local module files for verification."""
    allowed_suffixes = (".py", ".xml", ".js", ".scss", ".css", ".csv", "__manifest__.py")
    raw_candidates: list[str] = []
    pack_json = _extract_error_pack_json(error_pack)
    for value in pack_json.get("files") or []:
        raw_candidates.append(str(value))
    for value in pack_json.get("xml_locations") or []:
        raw_candidates.append(str(value).split(":", 1)[0])

    text = "\n".join(value for value in ((error_pack.stdout if error_pack else ""), (error_pack.stderr if error_pack else "")) if value)
    raw_candidates.extend(re.findall(r'File "([^"]+)"', text))
    raw_candidates.extend(match[0] for match in re.findall(r"while parsing\s+([^:\n]+):(\d+)", text, flags=re.I))
    raw_candidates.extend(
        re.findall(
            rf"((?:/home/odoo/src/user/)?(?:[^\s:'\"<>]+/)*{re.escape(module_name)}/[^\s:'\"<>]+)",
            text,
        )
    )

    local_files: list[str] = []
    seen: set[str] = set()

    def add_local(raw: str) -> None:
        value = raw.strip().strip("'\"`.,;)")
        if not value:
            return
        value = value.replace("\\", "/")
        if ":" in value and not value.startswith("/"):
            value = value.split(":", 1)[0]
        rel: str | None = None
        user_prefix = "/home/odoo/src/user/"
        if value.startswith(user_prefix):
            rel = value[len(user_prefix):]
        elif value.startswith(f"{module_name}/") or value == f"{module_name}/__manifest__.py":
            rel = value
        else:
            marker = f"/{module_name}/"
            if marker in value:
                rel = f"{module_name}/" + value.split(marker, 1)[1]
        if not rel or not (rel == f"{module_name}/__manifest__.py" or rel.startswith(f"{module_name}/")):
            return
        path = (project_path / rel).resolve()
        if not (path.is_file() and (path == project_path or project_path in path.parents)):
            return
        if not path.as_posix().endswith(allowed_suffixes):
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            local_files.append(key)

    for candidate in raw_candidates:
        add_local(candidate)
    for fallback in fallback_files:
        add_local(str(Path(fallback).resolve() if Path(fallback).is_absolute() else fallback))
        if Path(fallback).is_absolute():
            path = Path(fallback).resolve()
            if path.is_file() and (path == project_path or project_path in path.parents) and path.as_posix().endswith(allowed_suffixes):
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    local_files.append(key)
    return local_files


def _run_safe_deploy(payload: SafeDeployModuleRequest, job_id: str) -> None:
    """Background worker — runs the full commit→push→wait→upgrade pipeline."""
    store = JsonStore()
    git = GitService()
    odoosh = None
    approval = ApprovalService()
    pushed_sha: str | None = None
    odoosh_deploy_confirmation: dict[str, object] | None = None
    module_upgrade_confirmation: dict[str, object] = {
        "requested": bool(payload.upgrade_module),
        "status": "pending" if payload.upgrade_module else "skipped",
        "passed": None if payload.upgrade_module else True,
    }
    rollback_attempted = False
    last_step_mark = time.monotonic()

    def _step(name: str, status_: str, result=None, error: str | None = None) -> None:
        nonlocal last_step_mark
        now = time.monotonic()
        duration_ms = max(0, int((now - last_step_mark) * 1000))
        last_step_mark = now
        job = store.get_job(job_id)
        store.add_step(
            job,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=duration_ms,
                result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                error=error,
            ),
        )

    def _save_state(status_text: str, extra: dict[str, object] | None = None) -> None:
        state = {
            "last_safe_deploy_job_id": job_id,
            "last_sha": pushed_sha,
            "module_name": payload.module_name,
            "project_path": str(project_path),  # type: ignore[name-defined]
            "branch": branch,  # type: ignore[name-defined]
            "client_name": client.name if client else payload.client_name,  # type: ignore[name-defined]
            "safe_deploy_status": status_text,
        }
        if client:  # type: ignore[name-defined]
            state["client"] = client.public  # type: ignore[name-defined]
        if extra:
            state.update(extra)
        store.save_session_state(state, client_name=client.name if client else payload.client_name or None)  # type: ignore[name-defined]

    def _rollback_after_failure(reason: str) -> None:
        nonlocal rollback_attempted
        if rollback_attempted or not pushed_sha:
            return
        rollback_attempted = True
        try:
            remote_head = git.remote_head_sha(str(project_path), payload.remote, branch, 60)  # type: ignore[name-defined]
            current_head = git.head_sha(str(project_path))
            remote_sha = (remote_head.stdout or "").strip() if remote_head.exit_code == 0 else ""
            current_sha = (current_head.stdout or "").strip() if current_head.exit_code == 0 else ""
            if remote_sha != pushed_sha or current_sha != pushed_sha:
                _step("rollback_guard", "warning", {
                    "reason": reason,
                    "message": "Automatic rollback skipped because the job no longer exclusively owns local and remote HEAD.",
                    "pushed_sha": pushed_sha,
                    "remote_head": remote_sha,
                    "local_head": current_sha,
                    "rollback_commit_created": False,
                })
                return
            revert_result = git.revert_commit(str(project_path), pushed_sha, 180)  # type: ignore[name-defined]
            _step("rollback_git_revert", "passed" if revert_result.exit_code == 0 else "failed", revert_result)
            if revert_result.exit_code == 0:
                push_back = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))  # type: ignore[name-defined]
                _step("rollback_git_push", "passed" if push_back.exit_code == 0 else "failed", {
                    "result": push_back.model_dump(mode="json") if hasattr(push_back, "model_dump") else push_back,
                    "rollback_commit_created": True,
                })
        except Exception as exc:  # noqa: BLE001
            _step("rollback_exception", "failed", error=f"{reason}: {exc}")

    def _fail(name: str, result=None, error: str | None = None) -> None:
        _step(name, "failed", result, error)
        if name == "odoosh_verify_module_manifest":
            _rollback_after_failure(name)
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.failed, {"failed_step": name, "module_name": payload.module_name, "rollback_attempted": rollback_attempted})
        approval.mark_failed(payload.approval_id, f"workflow.safe_deploy_module failed at {name}")

    try:
        resolver = ClientResolverService()
        client = resolver.resolve_client(
            client_name=payload.client_name,
            project_path=payload.project_path,
            require_complete=False,
        )
        odoosh = _odoosh_service_for_client(client)
        project_path = resolve_allowed_path(client.path if client else payload.project_path)
    except Exception as exc:
        _fail("resolve_project", error=str(exc))
        return

    branch = payload.branch or (client.branch if client else None) or settings.target_git_branch
    module_root_rel = payload.module_root_rel or payload.module_name
    module_path = project_path / module_root_rel
    checkout_guard = ensure_module_exists_or_sync(
        project_path=project_path,
        module_name=payload.module_name,
        module_root_rel=module_root_rel,
        branch=branch,
        remote=payload.remote,
        git=git,
        timeout_seconds=min(payload.timeout_seconds, 300),
    )
    if checkout_guard.get("sync_attempted"):
        _step(
            "ensure_checkout_current",
            "passed" if checkout_guard.get("ok") else "failed",
            checkout_guard,
            None if checkout_guard.get("ok") else str(checkout_guard.get("error") or "Checkout/module guard failed"),
        )
    if not checkout_guard.get("ok"):
        job = store.get_job(job_id)
        result = {
            "status": "checkout_stale_or_missing",
            "message": checkout_guard.get("error") or "Checkout/module guard failed",
            "error": checkout_guard.get("error") or "Checkout/module guard failed",
            "module_name": payload.module_name,
            "project_path": str(project_path),
            "branch": branch,
            "checkout_guard": checkout_guard,
        }
        store.finish_job(job, JobStatus.failed, result)
        _save_state("checkout_stale_or_missing", {"checkout_error": result["error"]})
        approval.mark_failed(payload.approval_id, "workflow.safe_deploy_module checkout/module guard failed")
        return
    baseline_local_sha = str(checkout_guard.get("local_head") or "")
    baseline_remote_sha = str(checkout_guard.get("remote_head") or "")
    if baseline_local_sha or baseline_remote_sha:
        _step("checkout_baseline_lock", "passed", {
            "local_head": baseline_local_sha,
            "remote_head": baseline_remote_sha,
            "branch": branch,
            "dirty": checkout_guard.get("dirty"),
            "ahead": checkout_guard.get("ahead"),
            "behind": checkout_guard.get("behind"),
        })

    try:
        deploy_paths = _normalise_safe_deploy_paths(
            project_path,
            payload.module_name,
            payload.paths,
            module_root_rel,
        )
    except Exception as exc:  # noqa: BLE001
        _fail("deploy_path_gate", error=str(exc))
        return
    _step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path), "deploy_paths": deploy_paths})

    status_result = git.status(str(project_path))
    if status_result.exit_code != 0:
        _fail("git_status", status_result)
        return
    _step("git_status", "passed", status_result)

    diff_result = git.diff(str(project_path), staged=False, timeout_seconds=30)
    if diff_result.exit_code != 0:
        _fail("git_diff", diff_result)
        return
    _step("git_diff", "passed", diff_result)

    if hasattr(git, "changed_files"):
        changed_files_result = git.changed_files(str(project_path))
    else:
        fallback_paths = deploy_paths or [f"{module_root_rel}/__manifest__.py"]
        changed_files_result = CommandResult(command=["git", "changed-files", "legacy-fake"], cwd=str(project_path), exit_code=0, stdout="\n".join(fallback_paths) + "\n")
    if changed_files_result.exit_code != 0:
        _fail("git_changed_files", changed_files_result)
        return
    changed_files = _changed_file_set(changed_files_result)
    _step("git_changed_files", "passed", changed_files_result)
    no_changes_detected = not _deploy_request_has_module_changes(
        payload.module_name,
        deploy_paths,
        changed_files,
        module_root_rel,
    )
    if no_changes_detected:
        try:
            verify_result = verify_deployed_module(DeployedModuleVerifyRequest(
                project_path=str(project_path),
                module_name=payload.module_name,
                module_root_rel=module_root_rel,
                paths=deploy_paths,
                include_module_status=False,
                client_name=client.name if client else payload.client_name,
            ))
        except Exception as exc:  # noqa: BLE001
            verify_result = {
                "status": "failed",
                "source_status": "verification_error",
                "message": f"workflow.verify_deployed_module failed: {exc}. Do not claim already deployed/already live.",
                "module_name": payload.module_name,
                "project_path": str(project_path),
                "compared_files": [],
                "error": str(exc),
            }
        verified = verify_result.get("source_status") == "matched"
        _step("verify_deployed_module_no_changes", "passed" if verified else "failed", verify_result)
        previous_state = store.load_session_state(client_name=client.name if client else payload.client_name or None)
        pending_statuses = {
            "deploy_failed",
            "deploy_code_mismatch",
            "deploy_failed_or_timeout",
            "module_upgrade_running",
            "module_upgrade_timeout",
            "upgrade_failed_after_retries",
        }
        pending_upgrade = (
            verified
            and payload.upgrade_module
            and previous_state.get("module_name") == payload.module_name
            and str(previous_state.get("safe_deploy_status") or "") in pending_statuses
        )
        if pending_upgrade:
            _step("no_changes_resume_upgrade", "passed", {
                "message": "No local diff, but source matches Odoo.sh and the previous safe-deploy state did not confirm module upgrade; continuing with module upgrade.",
                "previous_status": previous_state.get("safe_deploy_status"),
            })
            head_sha_result = git.head_sha(str(project_path))
            pushed_sha = str(previous_state.get("last_sha") or (head_sha_result.stdout or "").strip() or "")
            if head_sha_result.exit_code == 0:
                _step("git_head_sha", "passed", head_sha_result)
            odoosh_deploy_confirmation = {
                "status": "passed",
                "deployed": True,
                "source_status": "matched",
                "message": "Odoo.sh source hash already matched active checkout; resumed from pending upgrade.",
                "verify_deployed_module": verify_result,
            }
            manifest_result = odoosh.verify_module_manifest(payload.module_name)
            _step("odoosh_verify_module_manifest", "passed" if manifest_result.exit_code == 0 else "failed", manifest_result)
            if manifest_result.exit_code != 0:
                _fail("odoosh_verify_module_manifest", manifest_result)
                return
            deploy_result = _start_and_wait_module_deploy(payload)
            deploy_passed = _module_upgrade_success(deploy_result)
            _step("odoo_module_deploy", "passed" if deploy_passed else "failed", deploy_result)
            if not deploy_passed:
                error_pack = odoosh.error_pack(150)
                _step("upgrade_error_pack_1", "passed" if error_pack.exit_code == 0 else "warning", error_pack)
                result = {
                    "status": "upgrade_failed_after_source_resume",
                    "message": _safe_deploy_error_text(deploy_result, error_pack),
                    "module_name": payload.module_name,
                    "sha": pushed_sha,
                    "source_pushed": False,
                    "source_on_odoosh": True,
                    "source_deployed": True,
                    "module_upgraded": False,
                    "ui_checked": False,
                    "visible_change_confirmed": False,
                    "deploy_confirmed": False,
                    "success_allowed": False,
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
                }
                job = store.get_job(job_id)
                store.finish_job(job, JobStatus.failed, result)
                _save_state("upgrade_failed_after_source_resume", {"module_upgrade_pending": True})
                approval.mark_failed(payload.approval_id, "workflow.safe_deploy_module resumed source deployment but module upgrade failed")
                return
            module_upgrade_confirmation = {
                "requested": True,
                "status": "passed",
                "passed": True,
                "attempt": 1,
                "attempts": 1,
                "message": "Upgrade confirmed successful after resuming from already-deployed source.",
                "result": _safe_deploy_payload(deploy_result),
            }
            _step("post_upgrade_smoke", "skipped", {
                "message": "Skipped redundant post-upgrade shell sessions; the successful module upgrade is the terminal confirmation."
            })
            final_result = {
                "status": "passed",
                "message": module_upgrade_confirmation["message"],
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "odoosh_check_commit": odoosh_deploy_confirmation,
                "module_upgrade": module_upgrade_confirmation,
                "source_pushed": False,
                "source_on_odoosh": True,
                "source_deployed": True,
                "module_upgraded": True,
                "deploy_confirmed": True,
                "success_allowed": True,
            }
            job = store.get_job(job_id)
            store.finish_job(job, JobStatus.passed, final_result)
            _save_state("passed", {"module_upgrade_pending": False})
            approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module resumed and upgraded already-deployed source")
            return

        final_result = _safe_deploy_already_deployed_result(
            project_path=project_path,
            module_name=payload.module_name,
            deploy_paths=deploy_paths,
            changed_files=changed_files,
            verify_result=verify_result,
        )
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.passed if verified else JobStatus.failed, final_result)
        store.save_session_state({
            "last_safe_deploy_job_id": job_id,
            "module_name": payload.module_name,
            "project_path": str(project_path),
            "client_name": client.name if client else payload.client_name,
            "safe_deploy_status": final_result["status"],
            "verified_already_deployed": verified,
            "module_upgrade_pending": False,
        }, client_name=client.name if client else payload.client_name or None)
        if verified:
            approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module verified already deployed")
        else:
            approval.mark_failed(payload.approval_id, "workflow.safe_deploy_module found no local changes but Odoo.sh source did not match")
        return

    idempotent_clean_retry = False
    idempotent_head_path_gate: CommandResult | None = None

    if payload.run_qa:
        qa_report = self_run(SelfRunRequest(
            project_path=str(project_path),
            module_name=payload.module_name,
            module_root_rel=module_root_rel,
            run_pytest=True,
            include_git_status=True,
            timeout_seconds=min(payload.timeout_seconds, 600),
        ))
        _step("qa_self_run", qa_report.status, qa_report)
        if qa_report.status == "failed":
            _fail("qa_self_run", qa_report)
            return

    verify_files = _pre_push_verify_files(
        project_path,
        payload.module_name,
        deploy_paths,
        status_result.stdout,
        module_root_rel,
    )
    verification_proof = VerificationProofService().validate(
        payload.verification_proof_id,
        project=project_path,
        module_name=payload.module_name,
        module_root_rel=module_root_rel,
        paths=deploy_paths,
    )
    if verification_proof:
        _step("verify_against_core_pre_push", "passed", {
            "status": "reused_exact_diff",
            "verification_proof_id": payload.verification_proof_id,
            "message": "Reused the code-change verifier result because HEAD and the complete binary working-tree diff are unchanged.",
        })
    elif verify_files:
        verify_result = SyntaxVerifierService().verify(SyntaxVerifyRequest(
            written_files=verify_files,
            fix_cycle=0,
            project_path=str(project_path),
            client_name=client.name if client else payload.client_name,
        ))
        _step("verify_against_core_pre_push", "failed" if verify_result.push_blocked else "passed", verify_result)
        if verify_result.push_blocked:
            _fail("verify_against_core_pre_push", verify_result)
            return
    else:
        _step(
            "verify_against_core_pre_push",
            "warning",
            {
                "message": (
                    "No changed module .py/.xml/.js/.scss/.css/.csv/__manifest__.py files were detected for pre-push verification. "
                    "Push gate passed only because there were no verifiable code files in this deploy request."
                )
            },
        )
    expected_user_hashes = _expected_user_file_hashes(project_path, verify_files)
    expected_registry_fields = _normalise_expected_registry_fields(payload.expected_registry_fields) or _infer_registry_fields_from_python(
        project_path,
        payload.module_name,
        verify_files,
        module_root_rel,
    )

    identity_result = git.configure_identity(str(project_path))
    if identity_result.exit_code != 0:
        _fail("git_configure_identity", identity_result)
        return
    _step("git_configure_identity", "passed", identity_result)

    target_result = git.validate_target(str(project_path), branch, payload.remote)
    if target_result.exit_code != 0:
        _fail("git_validate_target", target_result)
        return
    _step("git_validate_target", "passed", target_result)

    if baseline_local_sha and baseline_remote_sha:
        current_local = git.head_sha(str(project_path))
        current_remote = git.remote_head_sha(str(project_path), payload.remote, branch, 60)
        current_local_sha = (current_local.stdout or "").strip() if current_local.exit_code == 0 else ""
        current_remote_sha = (current_remote.stdout or "").strip() if current_remote.exit_code == 0 else ""
        baseline_ok = current_local_sha == baseline_local_sha and current_remote_sha == baseline_remote_sha
        _step("pre_commit_baseline_verify", "passed" if baseline_ok else "failed", {
            "baseline_local_sha": baseline_local_sha,
            "baseline_remote_sha": baseline_remote_sha,
            "current_local_sha": current_local_sha,
            "current_remote_sha": current_remote_sha,
            "message": "Commit baseline is unchanged." if baseline_ok else "Local or remote HEAD changed after inspection; refusing to commit stale work.",
        })
        if not baseline_ok:
            _fail("pre_commit_baseline_verify", error="Remote/local baseline changed before commit; rebase the preserved diff on the latest remote HEAD.")
            return

    if idempotent_clean_retry:
        _step("git_commit", "skipped", {"message": "Skipped duplicate commit because requested paths are already represented in HEAD."})
        head_path_gate = idempotent_head_path_gate or _assert_head_commit_contains_paths(git, project_path, deploy_paths)
        _step("git_head_commit_path_gate", "passed", head_path_gate)
        sha_result = git.head_sha(str(project_path))
        if sha_result.exit_code != 0:
            _fail("git_head_sha", sha_result)
            return
        pushed_sha = sha_result.stdout.strip()
        _step("git_head_sha", "passed", sha_result)
        push_started_epoch = int(time.time())
        _step("git_push", "skipped", {"message": "Skipped duplicate push; remote SHA will be verified before Odoo.sh polling.", "sha": pushed_sha})
    else:
        commit_result = git.commit(str(project_path), payload.commit_message, deploy_paths, min(payload.timeout_seconds, 300))
        if commit_result.exit_code != 0:
            _fail("git_commit", commit_result)
            return
        _step("git_commit", "passed", commit_result)

        if baseline_local_sha and callable(getattr(git, "head_parent_sha", None)):
            parent_result = git.head_parent_sha(str(project_path))
            parent_sha = (parent_result.stdout or "").strip() if parent_result.exit_code == 0 else ""
            parent_ok = parent_sha == baseline_local_sha
            _step("git_commit_parent_gate", "passed" if parent_ok else "failed", {
                "expected_parent": baseline_local_sha,
                "actual_parent": parent_sha,
                "message": "New commit is based on the locked checkout HEAD." if parent_ok else "New commit parent does not match the locked checkout HEAD.",
            })
            if not parent_ok:
                _fail("git_commit_parent_gate", error="New commit parent does not match pre-commit baseline; refusing to push.")
                return

        head_path_gate = _assert_head_commit_contains_paths(git, project_path, deploy_paths)
        if head_path_gate.exit_code != 0:
            _fail("git_head_commit_path_gate", head_path_gate)
            return
        _step("git_head_commit_path_gate", "passed", head_path_gate)

        sha_result = git.head_sha(str(project_path))
        if sha_result.exit_code != 0:
            _fail("git_head_sha", sha_result)
            return
        pushed_sha = sha_result.stdout.strip()
        _step("git_head_sha", "passed", sha_result)

        push_started_epoch = int(time.time())
        push_result = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))
        if push_result.exit_code != 0:
            _fail("git_push", push_result)
            return
        _step("git_push", "passed", push_result)

    if hasattr(git, "remote_head_sha"):
        remote_head = git.remote_head_sha(str(project_path), payload.remote, branch, 60)
    else:
        remote_ok = "PUSH_OK" in (push_result.stdout or "") and f"sha={pushed_sha}" in (push_result.stdout or "")  # type: ignore[possibly-undefined]
        remote_head = CommandResult(
            command=["git", "remote-head-sha", "legacy-fake"],
            cwd=str(project_path),
            exit_code=0 if remote_ok else 1,
            stdout=(pushed_sha + "\n") if remote_ok else "",
            stderr="GitService test double has no remote_head_sha()." if not remote_ok else "",
        )
    remote_sha = (remote_head.stdout or "").strip()
    if remote_head.exit_code != 0 or remote_sha != pushed_sha:
        _fail(
            "git_remote_head_verify",
            remote_head,
            f"Push was not proven on remote branch. expected_sha={pushed_sha}; remote_sha={remote_sha or '<missing>'}",
        )
        return
    _step("git_remote_head_verify", "passed", remote_head)

    pushed_job = store.get_job(job_id)
    pushed_job.request["push_started_epoch"] = push_started_epoch
    pushed_job.request["last_sha"] = pushed_sha
    pushed_job.request["branch"] = branch
    pushed_job.request["client_name"] = client.name if client else payload.client_name
    if client:
        pushed_job.request["client"] = client.public
    pushed_job.request["expected_user_file_hashes"] = expected_user_hashes
    pushed_job.request["expected_registry_fields"] = expected_registry_fields
    store.update_job(pushed_job)

    validation_lease = (pushed_job.request or {}).get("validation_lease") or {}
    github_fallback_active = bool(
        payload.github_push_fallback
        and settings.github_push_fallback_enabled
        and isinstance(validation_lease, dict)
        and validation_lease.get("github_push_fallback")
    )
    if github_fallback_active:
        final_result = {
            "status": "github_pushed_deploy_pending",
            "message": (
                "Verified source was committed and pushed to GitHub. Odoo.sh deployment and module upgrade remain pending "
                "because Odoo.sh SSH/public-key access is unavailable."
            ),
            "module_name": payload.module_name,
            "sha": pushed_sha,
            "pushed_sha": pushed_sha,
            "branch": branch,
            "remote": payload.remote,
            "github_pushed": True,
            "source_pushed": True,
            "source_on_odoosh": False,
            "source_deployed": False,
            "module_upgraded": False,
            "deploy_confirmed": False,
            "success_allowed": True,
            "completion_required": False,
            "deploy_fallback_reason": validation_lease.get("deploy_fallback_reason") or "odoosh_ssh_unavailable",
            "next_call": {
                "tool": "workflow.resume_job",
                "arguments": {"job_id": job_id, "override_step": "wait_odoosh"},
            },
        }
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.warning, final_result)
        _save_state("github_pushed_deploy_pending", {
            "github_pushed": True,
            "source_deployed": False,
            "module_upgrade_pending": bool(payload.upgrade_module),
        })
        approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module pushed verified source through GitHub fallback")
        return

    def _wait_for_odoosh_deployment(step_prefix: str = "") -> bool:
        nonlocal odoosh_deploy_confirmation, push_started_epoch
        if not payload.wait_for_odoosh:
            return True

        def _odoosh_poll_progress(attempt_payload: dict[str, object]) -> None:
            attempt = int(attempt_payload.get("attempt") or 0)
            # Keep the job responsive without appending/re-writing one large
            # JSON step on every SSH poll. Record first, every fifth, and final.
            if attempt_payload.get("deployed") or attempt == 1 or attempt % 5 == 0:
                _step(f"{step_prefix}odoosh_check_commit_poll", "passed" if attempt_payload.get("deployed") else "running", attempt_payload)

        check_result = _poll_odoosh_deployed(
            odoosh,
            pushed_sha or "",
            payload.odoosh_deploy_timeout_seconds,
            payload.odoosh_deploy_poll_interval_seconds,
            progress_cb=_odoosh_poll_progress,
        )
        deployed = check_result.exit_code == 0 and '"deployed": true' in (check_result.stdout or "").lower()
        _step(f"{step_prefix}odoosh_check_commit", "passed" if deployed else "failed", check_result)
        if not deployed:
            job = store.get_job(job_id)
            result = {
                "status": "deploy_failed_or_timeout",
                "message": "Odoo.sh did not report the pushed commit as deployed within the polling window. Module upgrade was not attempted.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "poll_timeout_seconds": payload.odoosh_deploy_timeout_seconds,
                "poll_interval_seconds": payload.odoosh_deploy_poll_interval_seconds,
                "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job_id}},
            }
            store.finish_job(job, JobStatus.failed, result)
            _save_state("deploy_failed_or_timeout")
            return False
        try:
            raw_payload = json.loads(check_result.stdout or "{}")
            check_payload = raw_payload if isinstance(raw_payload, dict) else {}
        except Exception:
            check_payload = {}
        deployed_observed_epoch = int(time.time())
        deployed_job = store.get_job(job_id)
        deployed_job.request["sha_deployed_observed_epoch"] = deployed_observed_epoch
        store.update_job(deployed_job)
        odoosh_deploy_confirmation = {
            "status": "passed",
            "deployed": True,
            "expected_sha": pushed_sha,
            "observed_epoch": deployed_observed_epoch,
            "payload": check_payload,
            "command_result": check_result.model_dump(mode="json"),
        }
        failure_guard = getattr(odoosh, "recent_build_failure", None)
        if callable(failure_guard):
            failure_result = failure_guard(deployed_observed_epoch)
            failure_status = "failed" if failure_result.exit_code == 1 else "passed" if failure_result.exit_code == 0 else "warning"
            _step(f"{step_prefix}odoosh_build_failure_guard", failure_status, failure_result)
            if failure_result.exit_code == 1:
                job = store.get_job(job_id)
                result = {
                    "status": "deploy_failed",
                    "message": "Odoo.sh reported failure evidence after this push. Module upgrade was not attempted.",
                    "module_name": payload.module_name,
                    "sha": pushed_sha,
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
                }
                store.finish_job(job, JobStatus.failed, result)
                _save_state("deploy_failed")
                return False
        return True

    if not _wait_for_odoosh_deployment():
        return

    manifest_changed = any(Path(path).name in {"__manifest__.py", "__openerp__.py"} for path in deploy_paths)
    if payload.wait_for_odoosh and (expected_user_hashes or manifest_changed):
        combined_verify = getattr(odoosh, "verify_deployed_changes", None)
        if callable(combined_verify):
            code_verify = combined_verify(expected_user_hashes, module_name=payload.module_name, verify_manifest=manifest_changed)
        else:
            code_verify = odoosh.verify_user_file_hashes(expected_user_hashes)
            if code_verify.exit_code == 0 and manifest_changed:
                code_verify = odoosh.verify_module_manifest(payload.module_name)
        _step("odoosh_deployed_change_verify", "passed" if code_verify.exit_code == 0 else "failed", code_verify)
        if code_verify.exit_code != 0:
            job = store.get_job(job_id)
            failure_status, failure_message = _classify_deployed_verification_failure(code_verify)
            result = {
                "status": failure_status,
                "message": failure_message,
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "verified_files": sorted(expected_user_hashes),
                "next_call": (
                    {"tool": "workflow.resume_job", "arguments": {"job_id": job_id, "override_step": "verify_deployed_code"}}
                    if failure_status != "deploy_code_mismatch"
                    else {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
                ),
            }
            store.finish_job(job, JobStatus.failed, result)
            _save_state(failure_status)
            return
    else:
        _step("odoosh_deployed_change_verify", "skipped", {"message": "No changed deploy files or manifest required verification."})

    def _fetch_upgrade_error_pack(attempt: int, deploy_result) -> CommandResult:
        try:
            error_pack = odoosh.error_pack(150)
        except Exception as exc:  # noqa: BLE001
            error_pack = CommandResult(
                command=["odoosh.error_pack", "150"],
                cwd="odoosh:ssh",
                exit_code=1,
                stderr=f"odoosh.error_pack failed: {exc}",
            )
        _step(
            f"upgrade_error_pack_{attempt}",
            "passed" if error_pack.exit_code == 0 else "warning",
            error_pack,
            None if error_pack.exit_code == 0 else error_pack.stderr,
        )
        return error_pack

    fix_cycle_blocked_by_verifier = False

    def _run_fix_cycle(attempt: int, deploy_result, error_pack: CommandResult) -> bool:
        nonlocal verify_files, expected_user_hashes, expected_registry_fields, pushed_sha, push_started_epoch, fix_cycle_blocked_by_verifier
        fix_cycle_blocked_by_verifier = False
        implicated_files = _error_pack_implicated_files(
            project_path=project_path,
            module_name=payload.module_name,
            error_pack=error_pack,
            fallback_files=verify_files,
        )
        verifier_payload = None
        verifier_blocked = False
        verifier_error = None
        if implicated_files:
            try:
                verifier_payload = SyntaxVerifierService().verify(SyntaxVerifyRequest(
                    written_files=implicated_files,
                    fix_cycle=attempt,
                    project_path=str(project_path),
                ))
                verifier_blocked = bool(verifier_payload.push_blocked)
            except Exception as exc:  # noqa: BLE001
                verifier_error = str(exc)
                verifier_blocked = True
        else:
            verifier_error = "No local .py/.xml/.js/.scss/.css/.csv/__manifest__.py file could be mapped from odoosh.error_pack."
            verifier_blocked = True

        fix_result = {
            "attempt": attempt,
            "implicated_files": implicated_files,
            "upgrade_error": _safe_deploy_error_text(deploy_result, error_pack),
            "verifier": verifier_payload.model_dump(mode="json") if hasattr(verifier_payload, "model_dump") else verifier_payload,
            "verifier_error": verifier_error,
            "message": (
                "Verifier-guided fix cycle completed; recommitting and repushing before retry."
                if not verifier_blocked else
                "Verifier still blocks the implicated files; no retry commit was attempted for this cycle."
            ),
        }
        _step(f"fix_cycle_{attempt}", "failed" if verifier_blocked else "passed", fix_result, verifier_error)
        if verifier_blocked:
            # Blocked either because the verifier rejected mappable files, OR because
            # no local file could be mapped from the error at all. Both mean this
            # fix cycle cannot make a different attempt with the current inputs —
            # looping again would just re-push identical code and repeat the same
            # failure. Stop the outer loop in either case.
            fix_cycle_blocked_by_verifier = True
            return False

        retry_paths: list[str]
        if implicated_files:
            retry_paths = [Path(path).resolve().relative_to(project_path).as_posix() for path in implicated_files]
        else:
            retry_paths = deploy_paths
        retry_paths = list(dict.fromkeys(retry_paths or deploy_paths))

        commit_result = git.commit(
            str(project_path),
            f"{payload.commit_message} [safe-deploy fix cycle {attempt}]",
            retry_paths,
            min(payload.timeout_seconds, 300),
        )
        _step(f"fix_cycle_{attempt}_git_commit", "passed" if commit_result.exit_code == 0 else "failed", commit_result)
        if commit_result.exit_code != 0:
            return False

        head_path_gate = _assert_head_commit_contains_paths(git, project_path, retry_paths)
        _step(f"fix_cycle_{attempt}_git_head_commit_path_gate", "passed" if head_path_gate.exit_code == 0 else "failed", head_path_gate)
        if head_path_gate.exit_code != 0:
            return False

        sha_result = git.head_sha(str(project_path))
        _step(f"fix_cycle_{attempt}_git_head_sha", "passed" if sha_result.exit_code == 0 else "failed", sha_result)
        if sha_result.exit_code != 0:
            return False
        pushed_sha = sha_result.stdout.strip()
        push_started_epoch = int(time.time())

        push_result = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))
        _step(f"fix_cycle_{attempt}_git_push", "passed" if push_result.exit_code == 0 else "failed", push_result)
        if push_result.exit_code != 0:
            return False

        if hasattr(git, "remote_head_sha"):
            remote_head = git.remote_head_sha(str(project_path), payload.remote, branch, 60)
        else:
            remote_ok = "PUSH_OK" in (push_result.stdout or "") and f"sha={pushed_sha}" in (push_result.stdout or "")
            remote_head = CommandResult(
                command=["git", "remote-head-sha", "legacy-fake"],
                cwd=str(project_path),
                exit_code=0 if remote_ok else 1,
                stdout=(pushed_sha + "\n") if remote_ok else "",
                stderr="GitService test double has no remote_head_sha()." if not remote_ok else "",
            )
        remote_sha = (remote_head.stdout or "").strip()
        _step(
            f"fix_cycle_{attempt}_git_remote_head_verify",
            "passed" if remote_head.exit_code == 0 and remote_sha == pushed_sha else "failed",
            remote_head,
            None if remote_head.exit_code == 0 and remote_sha == pushed_sha else f"expected_sha={pushed_sha}; remote_sha={remote_sha or '<missing>'}",
        )
        if remote_head.exit_code != 0 or remote_sha != pushed_sha:
            return False

        verify_files = _pre_push_verify_files(
            project_path,
            payload.module_name,
            retry_paths,
            git.status(str(project_path)).stdout if hasattr(git, "status") else "",
            module_root_rel,
        ) or implicated_files
        expected_user_hashes = _expected_user_file_hashes(project_path, verify_files)
        expected_registry_fields = _normalise_expected_registry_fields(payload.expected_registry_fields) or _infer_registry_fields_from_python(
            project_path,
            payload.module_name,
            verify_files,
            module_root_rel,
        )
        job = store.get_job(job_id)
        job.request["push_started_epoch"] = push_started_epoch
        job.request["last_sha"] = pushed_sha
        job.request["branch"] = branch
        job.request["expected_user_file_hashes"] = expected_user_hashes
        job.request["expected_registry_fields"] = expected_registry_fields
        store.update_job(job)

        if not _wait_for_odoosh_deployment(f"fix_cycle_{attempt}_"):
            return False
        if payload.wait_for_odoosh and expected_user_hashes:
            code_verify = odoosh.verify_user_file_hashes(expected_user_hashes)
            _step(f"fix_cycle_{attempt}_odoosh_user_file_verify", "passed" if code_verify.exit_code == 0 else "failed", code_verify)
            if code_verify.exit_code != 0:
                return False
        manifest_result = odoosh.verify_module_manifest(payload.module_name)
        _step(f"fix_cycle_{attempt}_odoosh_verify_module_manifest", "passed" if manifest_result.exit_code == 0 else "failed", manifest_result)
        return manifest_result.exit_code == 0

    if payload.upgrade_module:
        _step("db_rollback_checkpoint", "warning", {"note": "Code rollback is automatic, but database schema/data rollback requires an Odoo.sh backup or duplicate staging before upgrade. Create/confirm backup before destructive migrations."})
        max_fix_cycles = max(1, int(payload.max_fix_cycles or 5))
        last_deploy_result = None
        last_error_pack: CommandResult | None = None
        last_real_error = ""
        attempts_made = 0
        upgrade_passed = False
        for attempt in range(1, max_fix_cycles + 1):
            attempts_made = attempt
            _save_state("running", {"upgrade_attempt": attempt, "max_fix_cycles": max_fix_cycles})
            deploy_result = _start_and_wait_module_deploy(payload)
            last_deploy_result = deploy_result
            deploy_payload = _safe_deploy_payload(deploy_result)
            deploy_passed = _module_upgrade_success(deploy_result)
            # A wait-for-result safe deploy cannot complete in an intermediate
            # nested upgrade state. Treat any non-success, including a nested
            # timeout/non-terminal result, as a failed upgrade attempt and keep
            # the bounded error-pack/fix/retry loop inside this same job.
            attempt_status = "passed" if deploy_passed else "failed"
            _step(f"upgrade_attempt_{attempt}", attempt_status, deploy_result)
            if attempt == 1:
                _step("odoo_module_deploy", attempt_status, deploy_result)
            if deploy_passed:
                module_upgrade_confirmation = {
                    "requested": True,
                    "status": "passed",
                    "passed": True,
                    "attempt": attempt,
                    "attempts": attempt,
                    "message": f"Upgrade confirmed successful after {attempt} attempt(s).",
                    "result": deploy_payload,
                }
                upgrade_passed = True
                break

            last_error_pack = _fetch_upgrade_error_pack(attempt, deploy_result)
            last_real_error = _safe_deploy_error_text(deploy_result, last_error_pack)
            if attempt >= max_fix_cycles:
                break
            fixed = _run_fix_cycle(attempt, deploy_result, last_error_pack)
            if not fixed:
                if fix_cycle_blocked_by_verifier:
                    break
                continue

        if not upgrade_passed:
            job = store.get_job(job_id)
            attempts_text = f"{attempts_made} attempt" if attempts_made == 1 else f"{attempts_made} attempts"
            result = {
                "status": "upgrade_failed_after_retries",
                "message": f"Upgrade failed after {attempts_text}, last error: {last_real_error or _safe_deploy_error_text(last_deploy_result, last_error_pack)}",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "attempts": attempts_made,
                "max_fix_cycles": max_fix_cycles,
                "last_real_error": last_real_error or _safe_deploy_error_text(last_deploy_result, last_error_pack),
                "last_upgrade_result": _safe_deploy_payload(last_deploy_result) if last_deploy_result is not None else None,
                "last_error_pack": last_error_pack.model_dump(mode="json") if hasattr(last_error_pack, "model_dump") else None,
                "module_upgrade": {
                    "requested": True,
                    "status": "failed",
                    "passed": False,
                    "attempts": attempts_made,
                },
                "odoosh_check_commit": odoosh_deploy_confirmation,
                "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
            }
            store.finish_job(job, JobStatus.failed, result)
            _save_state("upgrade_failed_after_retries", {"last_upgrade_error": result["last_real_error"]})
            approval.mark_failed(payload.approval_id, "workflow.safe_deploy_module module upgrade failed after retries")
            return

    if expected_registry_fields:
        field_verify = _verify_registry_fields(expected_registry_fields, client)
        _step("registry_field_verify", "passed" if field_verify.status == "ok" else "failed", field_verify)
        if field_verify.status != "ok":
            job = store.get_job(job_id)
            result = {
                "status": "registry_field_missing",
                "message": "Module upgrade finished but the live Odoo registry is missing expected fields. Do not claim success.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "expected_registry_fields": expected_registry_fields,
                "registry_field_verify": field_verify.model_dump(mode="json"),
                "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}},
            }
            store.finish_job(job, JobStatus.failed, result)
            _save_state("registry_field_missing")
            return
    else:
        _step("registry_field_verify", "skipped", {"message": "No expected Python field additions were inferred or provided."})

    live_verification = None
    if payload.verification_assertion is not None:
        try:
            live_verification = LiveAssertionService().evaluate(
                payload.verification_assertion,
                client_name=client.name if client else payload.client_name,
                project_path=str(project_path),
                phase="post_deploy",
            )
        except Exception as exc:  # noqa: BLE001
            live_verification = {
                "phase": "post_deploy",
                "query_status": "failed",
                "passed": False,
                "error": str(exc)[:2000],
            }
        _step(
            "combined_live_verification",
            "passed" if live_verification.get("passed") else "failed",
            live_verification,
        )
        if not live_verification.get("passed"):
            job = store.get_job(job_id)
            result = {
                "status": "live_verification_failed",
                "message": "Source and module upgrade completed, but the task-specific live assertion did not pass.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "live_verification": live_verification,
                "odoosh_check_commit": odoosh_deploy_confirmation,
                "module_upgrade": module_upgrade_confirmation,
                "source_deployed": True,
                "module_upgraded": bool(payload.upgrade_module and module_upgrade_confirmation.get("passed")),
                "deploy_confirmed": False,
                "success_allowed": False,
            }
            store.finish_job(job, JobStatus.failed, result)
            _save_state("live_verification_failed")
            approval.mark_failed(payload.approval_id, "workflow.safe_deploy_module live assertion failed")
            return
    else:
        _step("combined_live_verification", "skipped", {
            "message": "No task-specific live record/count assertion was supplied; source, upgrade, and inferred registry checks remain authoritative."
        })

    fresh_job = store.get_job(job_id)
    final_result = {
        "status": "passed",
        "message": module_upgrade_confirmation.get("message") if payload.upgrade_module else "Safe deploy finished without module upgrade because upgrade_module=false.",
        "module_name": payload.module_name,
        "sha": pushed_sha,
        "expected_registry_fields": expected_registry_fields,
        "odoosh_check_commit": odoosh_deploy_confirmation,
        "module_upgrade": module_upgrade_confirmation,
        "live_verification": live_verification,
        "source_pushed": True,
        "source_on_odoosh": bool(odoosh_deploy_confirmation and odoosh_deploy_confirmation.get("deployed")),
        "source_deployed": bool(odoosh_deploy_confirmation and odoosh_deploy_confirmation.get("deployed")),
        "module_upgrade_requested": bool(payload.upgrade_module),
        "module_upgraded": bool(payload.upgrade_module and module_upgrade_confirmation.get("passed")),
        "deploy_impact": payload.deploy_impact,
        "upgrade_reasons": payload.upgrade_reasons,
        "deploy_confirmed": bool(odoosh_deploy_confirmation and odoosh_deploy_confirmation.get("deployed")) and (not payload.upgrade_module or bool(module_upgrade_confirmation.get("passed"))),
        "success_allowed": True,
        "completion_required": False,
        "tool_exit_allowed": True,
        "next_call": None,
        "terminal_action": "stop_and_summarize",
        "timing_summary": {
            "measured_stage_count": len(fresh_job.steps),
            "measured_total_ms": sum(int(step.duration_ms or 0) for step in fresh_job.steps),
            "slowest_stages": [
                {"name": step.name, "duration_ms": int(step.duration_ms or 0)}
                for step in sorted(fresh_job.steps, key=lambda item: int(item.duration_ms or 0), reverse=True)[:5]
            ],
        },
    }
    store.finish_job(fresh_job, JobStatus.passed, final_result)
    approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module succeeded")
    _save_state("passed")


# ---------------------------------------------------------------------------
# V14 — Self-Learning Agent: Core File Discovery
# ---------------------------------------------------------------------------

@router.post("/find-core-file", response_model=FindCoreFileResult)
def find_core_file(payload: FindCoreFileRequest) -> FindCoreFileResult:
    """V14: Locate an Odoo core source file by module + type + keyword.

    Search priority for installed-source reads:
      1. /home/odoo/src/enterprise — Odoo enterprise
      2. /home/odoo/src/odoo       — Odoo community
      3. /home/odoo/src/user       — fallback for other related custom dependencies

    Use before any ``_inherit`` so the agent reads the exact field
    definitions, method signatures, and model structure from source
    rather than hallucinating them.

    Example:
        module_name="sale", file_type="model", keyword="order"
        → /home/odoo/src/odoo/addons/sale/models/sale_order.py
    """
    return CoreFinderService().find_core_file(
        module_name=payload.module_name,
        file_type=payload.file_type,
        keyword=payload.keyword,
        search_roots=payload.search_roots or None,
        client_name=payload.client_name,
    )


@router.post("/find-similar-view", response_model=FindSimilarViewResult)
def find_similar_view(payload: FindSimilarViewRequest) -> FindSimilarViewResult:
    """V14: Locate an Odoo core view XML file by model + view_type.

    Use before writing any ``<xpath expr="...">`` block so the agent
    reads the exact attribute names, field references, and node structure
    from the real source file rather than guessing.

    Search priority: odoo/addons → enterprise → user fallback (same as find_core_file).

    Example:
        model="sale.order", view_type="form"
        → /home/odoo/src/enterprise/sale/views/sale_order_views.xml
    """
    return CoreFinderService().find_similar_view(
        model=payload.model,
        view_type=payload.view_type,
        search_roots=payload.search_roots or None,
        client_name=payload.client_name,
    )


@router.post("/list-module-files", response_model=ListModuleFilesResult)
def list_module_files(payload: ListModuleFilesRequest) -> ListModuleFilesResult:
    """V14+ gap-fix: List all files inside a core or custom module directory.

    Use when you don't know the exact filename/keyword to search for yet —
    browse what exists first, then call find_core_file with a keyword, or
    repo.read directly on a file you spot here.

    Search priority: odoo/addons → enterprise → user fallback (same as find_core_file).

    Example:
        module_name="sale", subdir="models"
        → lists every file under sale/models/ in whichever root has it first.
    """
    return CoreFinderService().list_module_files(
        module_name=payload.module_name,
        subdir=payload.subdir,
        search_roots=payload.search_roots or None,
        client_name=payload.client_name,
    )


# ===========================================================================
# V14+ — Pillar 2: Adaptive Scope Advisor
# ===========================================================================

@router.post("/scope-advice", response_model=ScopeAdviceResult)
def scope_advice(payload: ScopeAdviceRequest) -> ScopeAdviceResult:
    """V14+: Analyse custom module files and return adaptive mandatory read plan.

    MANDATORY: Call this AFTER reading custom module files and BEFORE writing
    any code. The service scans the provided custom file paths for _inherit
    declarations, mixin usage, computed/relational fields, and cross-module
    comodel references. It then returns an ordered list of ScopeReadInstruction
    objects specifying everything that must be read from Odoo core before coding.

    Adaptive scope levels triggered automatically:
      L0 — always: custom module manifest (baseline, always included)
      L1 — _inherit detected → parent core model file(s)
      L2 — mixin / abstract detected → mail.thread, portal.mixin, etc.
      L3 — computed or relational fields / ir.model → full models/ dir scan
      L4 — cross-module comodel → that module's core manifest + model

    Each ScopeReadInstruction includes:
      - level: which level triggered it
      - reason: why it must be read
      - path: absolute path → call repo.read(path) on each mandatory instruction
      - mandatory: True = blocking (must be done before any code write)

    Returns:
      reached_level: highest level triggered
      mandatory_count: number of blocking reads required
      warning: "Push BLOCKED until verify_against_core passes"

    Example:
        custom_module_name='my_sale_ext',
        custom_file_paths=['/workspace/repos/repo/my_sale_ext/models/sale_order.py'],
        task_description='Add custom field to sale.order'
    """
    result = CoreFinderService().scope_advice(
        custom_module_name=payload.custom_module_name,
        custom_file_paths=payload.custom_file_paths,
        task_description=payload.task_description,
        search_roots=payload.search_roots or None,
        client_name=payload.client_name,
        focus_identifiers=payload.focus_identifiers,
    )
    PreWriteGateService().record_scope_advice(result, payload)
    return result


# ===========================================================================
# V14+ — Pillar 3: Pre-Push Syntax Verifier
# ===========================================================================

@router.post("/verify-against-core", response_model=SyntaxVerifyResult)
def verify_against_core(payload: SyntaxVerifyRequest) -> SyntaxVerifyResult:
    """V14+: Pre-push cross-check of written files against Odoo core source.

    MANDATORY: Call this BEFORE every git.push. If push_blocked=True, fix all
    CRITICAL findings and call verify_against_core again (max 3 fix cycles).

    What is verified (per file type):

    Python model files (.py):
      - _inherit model name exists in core (warns if not found)
      - depends=[] field names exist in the inherited core model (CRITICAL if not)
      - comodel_name module exists in /home/odoo/src/* (CRITICAL if not)
      - related= first field exists in core model (CRITICAL if not)
      - compute= method is defined in the same file (CRITICAL if missing)

    XML view files (.xml):
      - xpath @name nodes found in core view source (CRITICAL if not)
      - <field name=...> cross-checked against core model fields (WARNING if not)

    Manifest files (__manifest__.py):
      - depends[] module names exist in /home/odoo/src/* roots (CRITICAL if not)

    Push logic:
      push_blocked=True  → Fix CRITICAL findings, then call verify_against_core again
      push_blocked=False → Proceed with git.push
      fix_cycle >= 3     → Report to user and STOP retrying

    Example:
        written_files=[
            '/workspace/repos/repo/my_sale_ext/__manifest__.py',
            '/workspace/repos/repo/my_sale_ext/models/sale_order.py',
            '/workspace/repos/repo/my_sale_ext/views/sale_order_views.xml',
        ],
        fix_cycle=0
    """
    return SyntaxVerifierService().verify(payload)


_POS_FILE_MARKERS = ("point_of_sale", "/pos/", "pos_", "_pos.")


def _looks_pos_related(written_files: list[str]) -> bool:
    lowered = [f.lower() for f in written_files]
    return any(any(marker in f for marker in _POS_FILE_MARKERS) for f in lowered)


@router.post("/verify-pos-change", response_model=PosChangeVerifyResult)
async def verify_pos_change(payload: PosChangeVerifyRequest) -> PosChangeVerifyResult:
    """V17: Bounded fix-loop for POS changes — static check AND live browser check.

    verify_against_core alone (even with the real node --check parse added in
    V17) only proves the JS/XML/manifest are syntactically and pattern-wise
    sound. It cannot prove the POS screen actually renders, that clicking a
    product still works, or that the payment/validate flow doesn't throw at
    runtime. This endpoint runs BOTH under one fix_cycle counter, exactly
    mirroring the existing verify_against_core contract:

      push_blocked=True, fix_cycle < 3  → fix CRITICAL/failed findings, call
                                            again with fix_cycle+1
      push_blocked=True, fix_cycle >= 3 → STOP retrying, report to user
      push_blocked=False                → safe to git.push

    The live POS flow only runs if written_files look POS-related (or
    run_pos_flow is forced True) — non-POS changes skip straight to the
    static-only result to avoid unnecessary browser cost.
    """
    static_request = SyntaxVerifyRequest(
        written_files=payload.written_files,
        fix_cycle=payload.fix_cycle,
        project_path=payload.project_path,
    )
    static_result = SyntaxVerifierService().verify(static_request)

    pos_flow_result = None
    is_pos = _looks_pos_related(payload.written_files)

    # Never bother driving a real browser if static checks already found
    # CRITICAL issues — fix those first, they're cheaper and faster to loop on.
    if payload.run_pos_flow and is_pos and static_result.critical_count == 0:
        flow_req = payload.pos_flow_request or PosFlowCheckRequest()
        pos_flow_result = await CdpBrowserService().run_pos_flow(flow_req)

    push_blocked = static_result.push_blocked or (pos_flow_result is not None and pos_flow_result.status == "failed")

    if payload.fix_cycle >= 3 and push_blocked:
        message = (
            f"STOP: fix_cycle={payload.fix_cycle} reached the max of 3 with issues still unresolved. "
            "Report the remaining findings/console errors to the user instead of retrying again."
        )
    elif not is_pos:
        message = "No POS-related files detected in written_files — static verification only was run."
    elif static_result.critical_count > 0:
        message = "Static CRITICAL findings exist — fix those first, then call verify_pos_change again to run the live POS flow."
    elif pos_flow_result is None:
        message = "POS flow check was skipped (run_pos_flow=False)."
    elif pos_flow_result.status == "failed":
        failed_steps = [s.step for s in pos_flow_result.steps if s.status == "failed"]
        message = f"Live POS flow FAILED at step(s): {', '.join(failed_steps) or 'unknown'}. Review console_errors/console_all for that step, fix, and call again with fix_cycle+1."
    else:
        message = "Static checks and live POS flow both passed. Safe to git.push."

    return PosChangeVerifyResult(
        push_blocked=push_blocked,
        status="failed" if push_blocked else "passed",
        static_result=static_result,
        pos_flow_result=pos_flow_result,
        fix_cycle=payload.fix_cycle,
        message=message,
    )


# ===========================================================================
# V15 — Strict Odoo Frontend / OWL / JS / XML Preflight
# ===========================================================================

@router.post("/frontend-pattern-preflight", response_model=FrontendPatternPreflightResult)
def frontend_pattern_preflight(payload: FrontendPatternPreflightRequest) -> FrontendPatternPreflightResult:
    """V15: Blocking source exploration plan for Odoo frontend work.

    MANDATORY for any POS, OWL, JS, website, payment, QWeb/XML template,
    frontend SCSS, or asset-bundle task before writing code.  It returns the
    exact source areas/files the agent must read so the implementation matches
    the installed Odoo framework pattern instead of guessing.
    """
    result = FrontendPatternService().preflight(payload)
    PreWriteGateService().record_frontend_preflight(result, payload)
    return result


# ===========================================================================
# V16 — Smart router + repo index
# ===========================================================================

@router.post("/smart-tool-router", response_model=SmartToolRouteResult)
def smart_tool_router(payload: SmartToolRouteRequest) -> SmartToolRouteResult:
    """Return a fast but strict tool plan for the current task.

    This is a planner only; it does not mutate code. It prevents the agent from
    calling heavy tools for simple Odoo queries while still enforcing frontend
    preflight, smart writes, changed-file verification, and browser checks when
    needed.
    """
    text = (payload.task_description or "").lower().replace("-", "_")
    planned = [str(path).lower().replace("\\", "/") for path in payload.planned_file_paths]

    def matched_terms(terms: tuple[str, ...]) -> list[str]:
        return [term for term in terms if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text)]

    def planned_frontend_path(path: str) -> bool:
        segments = [segment for segment in path.split("/") if segment]
        if path.endswith((".js", ".scss", ".css")):
            return True
        if path.endswith(".xml") and any(marker in path for marker in ("/static/src/", "/static/", "/qweb/", "/templates/")):
            return True
        return (
            "point_of_sale" in segments
            or "_assets_pos" in path
            or any(segment in {"pos", "pos_store"} for segment in segments)
        )

    evidence: list[str] = []
    frontend_paths = [path for path in planned if planned_frontend_path(path)]
    frontend_terms = matched_terms(("javascript", "js", "owl", "qweb", "scss", "pos", "point_of_sale", "website", "snippet", "frontend", "checkout"))
    if payload.target_surface != "auto":
        frontend = payload.target_surface == "frontend"
        evidence.append(f"explicit target_surface={payload.target_surface}")
    elif frontend_paths:
        frontend = True
        evidence.append(f"frontend file evidence: {frontend_paths[0]}")
    else:
        frontend = bool(frontend_terms)
        if frontend_terms:
            evidence.append(f"frontend terms: {', '.join(frontend_terms)}")

    read_hits = matched_terms(("list", "show", "find", "search", "count", "get", "read", "display", "inspect", "query", "status"))
    write_hits = matched_terms(("add", "fix", "implement", "create", "update", "change", "delete", "remove", "write", "patch", "modify", "refactor"))
    deploy_hits = matched_terms(("deploy", "install", "upgrade", "push", "release"))
    source_hits = matched_terms(("code", "method", "file", "manifest", "view", "xpath", "xml", "python", "javascript", "js", "owl", "scss", "qweb"))

    if payload.operation != "auto":
        intent = payload.operation
        confidence = "high"
        evidence.append(f"explicit operation={intent}")
    elif deploy_hits:
        intent = "deploy"
        confidence = "high" if not read_hits else "medium"
        evidence.append(f"deploy terms: {', '.join(deploy_hits)}")
    elif write_hits or (payload.changed_file_count or 0) > 0:
        intent = "write"
        confidence = "high" if write_hits and not read_hits else "medium"
        if write_hits:
            evidence.append(f"write terms: {', '.join(write_hits)}")
        if (payload.changed_file_count or 0) > 0:
            evidence.append("changed_file_count indicates a code change")
    elif read_hits:
        intent = "read"
        confidence = "high" if source_hits else "medium"
        evidence.append(f"read terms: {', '.join(read_hits)}")
    else:
        intent = "ambiguous"
        confidence = "low"
        evidence.append("no explicit operation or reliable read/write/deploy signal")

    source_read = intent == "read" and bool(source_hits or planned)
    data_query = intent == "read" and not source_read
    clarification_required = intent == "ambiguous"
    high_risk = payload.risk_level == "high" or any(term in text for term in (
        "new module", "migration", "large refactor", "rewrite", "multi module", "security", "access rights", "accounting", "payment provider"
    ))
    file_count = payload.changed_file_count if payload.changed_file_count is not None else len(payload.planned_file_paths)

    steps: list[SmartToolRouteStep] = []
    order = 1
    def add(tool: str, reason: str, required: bool = True) -> None:
        nonlocal order
        steps.append(SmartToolRouteStep(order=order, tool=tool, reason=reason, required=required))
        order += 1

    if data_query:
        add("odoo.search_read", "Direct live-data query; no repo or frontend preflight needed.")
        return SmartToolRouteResult(
            task_type="odoo_data_query",
            frontend_required=False,
            recommended_write_tool="none",
            validation_scope="none",
            browser_check="none",
            steps=steps,
            fast_reason="Fastest path: no code write requested.",
            confidence=confidence,
            evidence=evidence,
        )

    if source_read or clarification_required:
        add("repo.prepare", "Resolve the target repository without mutating code.")
        add("workflow.repo_index", "Discover relevant modules and source files.", required=False)
        add("repo.read", "Read the exact source before deciding whether a change is required.")
        return SmartToolRouteResult(
            task_type="source_read" if source_read else "ambiguous",
            frontend_required=frontend,
            recommended_write_tool="none",
            validation_scope="none",
            browser_check="none",
            steps=steps,
            fast_reason="Read-only discovery is the safe fallback; no write or deploy is authorized by fuzzy routing.",
            confidence=confidence,
            evidence=evidence,
            clarification_required=clarification_required,
        )

    add("repo.prepare", "Ensure the correct target repo/branch is available before coding.")
    add("workflow.repo_index", "Use cached repo index to find manifests/assets/templates without full filesystem scans.", required=False)
    add("repo.read", "Read the exact custom files involved before changing them.")
    if frontend:
        add("workflow.frontend_pattern_preflight", "Mandatory targeted OWL/POS/website/payment pattern read before JS/XML/SCSS writes.")
    else:
        add("workflow.scope_advice", "Mandatory backend/model/view source-scope advice before Python/XML writes.")

    write_tool = "repo.write_many_atomic" if high_risk or file_count > settings.fast_write_max_files else "repo.write_many_smart"
    add(write_tool, "Use atomic only for high-risk/large changes; otherwise smart writer keeps lock+backup+rollback without worktree overhead.")
    add("git.diff", "Review exact changes before validation.")
    add("workflow.verify_against_core", "Validate changed files with impact-aware related companions only.")
    if frontend:
        add("browser.screenshot", "Smoke-test affected POS/website/backend page and inspect console/network errors after deploy.", required=False)

    return SmartToolRouteResult(
        task_type="frontend_code" if frontend else "backend_code",
        frontend_required=frontend,
        recommended_write_tool=write_tool,
        validation_scope="impact-aware changed files",
        browser_check="required for frontend changes" if frontend else "only if views/UI changed",
        steps=steps,
        fast_reason="Strict validation remains; speed comes from repo index cache, pattern cache, smart writes, and changed-files-only checks.",
        confidence=confidence,
        evidence=evidence,
        clarification_required=False,
    )


@router.post("/repo-index", response_model=RepoIndexResult)
def repo_index(payload: RepoIndexRequest) -> RepoIndexResult:
    """Build or return a cached lightweight Odoo repo index."""
    return RepoIndexService().index(payload.project_path, force_refresh=payload.force_refresh, max_files=payload.max_files)
