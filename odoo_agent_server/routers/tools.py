Warning: truncated output (original token count: 129190)
Total output lines: 10813

from __future__ import annotations

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import difflib
import hashlib
import inspect
import json
import re
import textwrap
import threading
import time
import typing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from ..auth import AuthDependency
from ..config import settings, get_odoo_clients, normalise_client
from ..execution_tools import EXECUTION_TOOLS, dispatch_execution
from ..services.core_sync_service import bounded_inspection
from ..schemas import (
    CommandResult,
    WsExtendRequest,
    WsOutlineRequest,
    WsScaffoldRequest,
    WsSymbolRequest,
    WsCommitRequest,
    WsDiffRequest,
    WsEditRequest,
    WsGrepRequest,
    WsIdRequest,
    WsOpenRequest,
    WsReadRequest,
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalStatus,
    ArtifactReadRequest,
    AuditListRequest,
    BaseProjectRequest,
    BrowserCheckRequest,
    ClientCheckRequest,
    ClientOnlyRequest,
    DeployedModuleVerifyRequest,
    PosFlowCheckRequest,
    StartSessionRequest,
    RegistrySmokeTestRequest,
    RestoreLastGoodCommitRequest,
    MenuCrawlerRequest,
    CodingTaskPlanRequest,
    ContinuationResumeRequest,
    CustomModuleItem,
    OdooCodeChangeRequest,
    OdooDebugAndFixRequest,
    OdooDebugEvidenceRecord,
    OdooFailureDiagnosis,
    OdooFeatureInspectRequest,
    OdooOperationHandoff,
    OdooTargetResolutionHint,
    OdooTargetResolutionResult,
    OdooDebugRecordAssertion,
    ModuleAnalyzeRequest,
    ReaXrayRequest,
    ReaUpstreamRequest,
    ReaApiInspectRequest,
    ReaRuntimeTraceRequest,
    ReaImpactRequest,
    ReaSelfHealRequest,
    FrontendVisualImplementRequest,
    EmptyRequest,
    SkillListRequest,
    SkillReadRequest,
    FullVerifyRequest,
    SafeDeployModuleRequest,
    GitCommitRequest,
    GitDiffRequest,
    GitPushRequest,
    JobGetRequest,
    LimitRequest,
    OdooModuleRequest,
    OdooModuleTestRequest,
    OdooReadGroupRequest,
    OdooFieldsGetRequest,
    OdooListModelsRequest,
    OdooFlowTestRequest,
    OdooMutationAuditRequest,
    OdooRecordActionRequest,
    OdooRecordCreateRequest,
    OdooRecordWriteRequest,
    OdooCodeFileChange,
    OdooResult,
    OdooSearchReadRequest,
    OdooShQueryRequest,
    OdooShLogsReadRequest,
    OdooShShellDiagnosticsRequest,
    OdooFunctionalQueryRequest,
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShCoreFileReadRequest,
    OdooShCoreFilesListRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShUserFileReadRequest,
    OdooShUserFilesListRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
    RepoAtomicPatchRequest,
    RepoCleanPycRequest,
    RepoCustomModulesRequest,
    RepoDeleteRequest,
    RepoListRequest,
    RepoPatchRequest,
    RepoPrepareRequest,
    RepoReadRangeRequest,
    RepoReadRequest,
    RepoSearchRequest,
    RepoWriteManyAtomicRequest,
    RepoWriteManyFastSafeRequest,
    RepoWriteManySmartRequest,
    RepoWriteItem,
    RepoWriteRequest,
    SelfRunRequest,
    JobRecord,
    JobStatus,
    JobStep,
    deployment_confirmed_from_record,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    # Fix 3/7/10: new request schemas
    ResumeJobRequest,
    SessionStateSaveRequest,
    SessionStateLoadRequest,
    EnsureDependenciesRequest,
    # V14: Self-Learning Agent — Core Discovery
    FindCoreFileRequest,
    FindSimilarViewRequest,
    ListModuleFilesRequest,
    # V14+: Pillar 2 — Adaptive Scope Advisor
    ScopeAdviceRequest,
    # V14+: Pillar 3 — Pre-Push Syntax Verifier
    SyntaxVerifyRequest,
    PosChangeVerifyRequest,
    # V15: Strict Odoo frontend/OWL/JS/XML preflight
    FrontendPatternPreflightRequest,
    # V16: fast/correct router and repo index
    SmartToolRouteRequest,
    RepoIndexRequest,
)
from ..services.git_service import GitService
from ..services.client_resolver_service import ClientResolverService
from ..services.checkout_redirect_service import (
    CHECKOUT_REDIRECT_TOOLS,
    build_ws_redirect,
    is_missing_checkout,
    redirect_from_arguments,
)
from ..services.workspace_factory import run_ws
from ..services.client_gate_service import ClientGateService
from ..services.odoo_service import OdooService
from ..services.odoo_code_search_service import OdooCodeSearchService
from ..services.module_path_service import (
    canonical_module_relative_path,
    is_ignored_source_path,
    source_ownership,
)
from ..services.odoosh_service import OdooShService
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from ..services.prewrite_gate_service import PreWriteGateService
from ..services.syntax_verifier_service import SyntaxVerifierService
from ..services.workflow_guard_service import (
    code_change_issue_fingerprint,
    mark_stale_safe_deploy_running_job,
    record_code_change_deploy_attempt,
    repeated_failure_guard_status,
)
from ..services.workflow_watchdog_service import WorkflowWatchdogService
from .qa import self_run
from .repo import repo_delete, repo_write, repo_write_many_atomic, repo_write_many_fast_safe, repo_write_many_smart, repo_apply_patch, repo_apply_patch_atomic, repo_clean_pyc, repo_prepare, repo_custom_modules
from .git import git_commit, git_push
from .odoo import odoo_module_install, odoo_module_update, odoo_module_deploy, start_odoo_module_deploy_job
from .odoosh import odoosh_module_install_ssh, odoosh_module_deploy_ssh, odoosh_module_update, odoosh_wait_for_commit, odoosh_verify_module_manifest
from .workflow import capability_check, full_verify, safe_deploy_module, coding_task_plan, start_session, registry_smoke_test, restore_last_good_commit, browser_menu_crawl, find_core_file, find_similar_view, list_module_files, scope_advice, verify_against_core, verify_pos_change, frontend_pattern_preflight, smart_tool_router, repo_index, verify_deployed_module, _poll_odoosh_deployed, _pre_push_verify_files, _expected_user_file_hashes, _infer_registry_fields_from_python, _normalise_expected_registry_fields, _verify_registry_fields, _assert_head_commit_contains_paths
from ..schemas import ApprovalRecord
from ..services.store_service import JsonStore
from ..services.frontend_visual_service import FrontendVisualService
from ..services.deploy_impact_service import classify_deploy_impact
from ..services.skill_service import SkillService
from ..services.validator_service import OdooModuleValidator
from ..services.module_analysis_service import ModuleAnalysisService
from ..services.rea_xray_service import ReaXrayService
from ..services.rea_dependency_service import ReaDependencyService
from ..services.rea_runtime_registry_service import ReaRuntimeRegistryService
from ..services.rea_upstream_service import ReaUpstreamService
from ..services.rea_api_inspect_service import ReaApiInspectService
from ..services.rea_runtime_trace_service import ReaRuntimeTraceService
from ..services.rea_impact_service import ReaImpactService
from ..services.rea_workspace_service import run_rea_workspace
from ..services.live_assertion_service import LiveAssertionService
from ..services.functional_operation_service import FunctionalOperationService
from ..services.module_test_service import ModuleTestService
from ..services.existing_target_resolution_service import (
    ExistingTargetResolutionService,
    strip_vcs_target_context,
)
from ..services.coding_session_service import CodingSessionService, CodingWorkspaceService
from ..services.verification_proof_service import VerificationProofService
from ..services.background_executor import submit_background
from ..services.runtime_identity_service import runtime_identity
from ..services.workflow_progress_service import WorkflowProgressService
from ..services.continuation_token_service import ContinuationTokenError, ContinuationTokenService

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AuthDependency])

_STRICT_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CLIENT_VALIDATION_START_LOCK = threading.Lock()
_CLIENT_VALIDATION_STALE_SECONDS = 900
_VALIDATED_WRITE_CONTRACT_VERSION = 2


def strict_safe_tool_name(name: str) -> str:
    """Return a strict MCP-client-safe tool name.

    Some frontends validate tool names with ^[a-zA-Z0-9_-]{1,64}$.
    Internal Odoo-agent tool names use dotted namespaces such as
    ``odoo.search_read`` and ``workflow.smart_tool_router``. Externally expose
    underscored names while accepting both formats on calls.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    safe = (safe or "tool")[:64]
    if _STRICT_TOOL_NAME_RE.match(safe):
        return safe
    return "tool"


def internal_tool_name(name: str) -> str:
    """Map provider/OpenAPI aliases back to the internal dotted name.

    Some connector hosts prepend the MCP server namespace to a tool name
    (for example ``odoo_mcp.workflow_odoo_code_change``) while strict MCP
    clients use ``workflow_odoo_code_change``. Accept all three forms so a
    refreshed connector cannot advertise a valid workflow and then reject it
    as an unknown resource at call time.
    """
    raw = str(name or "").strip()
    candidates = [raw]
    for prefix in ("odoo_mcp.", "odoo_mcp_", "odoo-mcp.", "odoo-mcp_"):
        if raw.startswith(prefix):
            candidates.append(raw[len(prefix):])
    for candidate in candidates:
        if candidate in _TOOL_INPUT_MODELS:
            return candidate
        for actual in _TOOL_INPUT_MODELS:
            if strict_safe_tool_name(actual) == candidate:
                return actual
    return raw


def _agent_priority(name: str) -> tuple[int, str]:
    """Put functional Odoo query tools early so MCP agents choose them first.

    Function…124190 tokens truncated…ke"], cwd=project_path, exit_code=0, stdout=(pushed_sha + "\n") if pushed_sha else "")
                remote_sha = (result.stdout or "").strip()
                ok = result.exit_code == 0 and remote_sha == pushed_sha
                resume_results["steps_run"][step_name] = {
                    "status": "ok" if ok else "failed",
                    "sha": pushed_sha,
                    "remote_sha": remote_sha,
                    "result": _step_payload(result),
                }
                if not ok:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"Remote branch SHA was not proven. expected_sha={pushed_sha}; remote_sha={remote_sha or '<missing>'}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_check_commit":
                pushed_sha = pushed_sha or git.head_sha(project_path).stdout.strip()
                check = _poll_odoosh_deployed(
                    odoosh,
                    pushed_sha,
                    int(original_request.get("odoosh_deploy_timeout_seconds", 600)),
                    float(original_request.get("odoosh_deploy_poll_interval_seconds", 10.0)),
                )
                deployed = check.exit_code == 0 and '"deployed": true' in (check.stdout or "").lower()
                resume_results["steps_run"][step_name] = {"status": "ok" if deployed else "warning", "sha": pushed_sha, "deployed": deployed, "result": _step_payload(check)}
                if not deployed:
                    resume_results["status"] = "deploy_failed_or_timeout"
                    resume_results["message"] = "Odoo.sh did not report the pushed commit as deployed within the polling window. Completed commit/push steps will not be replayed."
                    resume_results["next_call"] = {"tool": "workflow.resume_job", "arguments": {"job_id": job_id}}
                    return _persist_resume_result(store, job, resume_results, original_request)
                deployed_observed_epoch = int(time.time())
                original_request["sha_deployed_observed_epoch"] = deployed_observed_epoch
                job.request["sha_deployed_observed_epoch"] = deployed_observed_epoch
                store.update_job(job)
                failure_guard = getattr(odoosh, "recent_build_failure", None)
                if callable(failure_guard):
                    failure = failure_guard(deployed_observed_epoch)
                    resume_results["steps_run"]["odoosh_build_failure_guard"] = {"status": "failed" if failure.exit_code == 1 else "ok" if failure.exit_code == 0 else "warning", "result": _step_payload(failure)}
                    if failure.exit_code == 1:
                        resume_results["status"] = "deploy_failed"
                        resume_results["message"] = "Odoo.sh reported failure evidence after the push. Module upgrade was not attempted."
                        resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
                        return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_user_file_verify":
                if not expected_user_hashes:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No changed user files were available for hash verification."}
                    continue
                result = odoosh.verify_user_file_hashes(expected_user_hashes)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "verified_files": sorted(expected_user_hashes), "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "deploy_code_mismatch"
                    resume_results["message"] = "Odoo.sh source checkout did not match the changed local/GitHub files. Module upgrade was not attempted."
                    resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_verify_module_manifest":
                result = odoosh.verify_module_manifest(module_name)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"manifest verification failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoo_module_deploy":
                if not original_request.get("upgrade_module", True):
                    resume_results["steps_run"][step_name] = {"status": "skipped"}
                    continue
                result = _start_and_wait_module_deploy_for_resume(original_request, module_name)
                payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                nested = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else {}
                finished = bool(nested.get("finished"))
                running = result.status == "ok" and not finished
                resume_results["steps_run"][step_name] = {"status": "running" if running else result.status, "result": payload}
                if running:
                    resume_results["status"] = "module_upgrade_running"
                    resume_results["message"] = "Odoo module deploy/update is still running. Safe deploy is not complete; do not claim success."
                    resume_results["completion_required"] = True
                    resume_results["success_allowed"] = False
                    resume_results["tool_exit_allowed"] = False
                    resume_results["poll_after_seconds"] = 5
                    resume_results["next_call"] = nested.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": nested.get("job_id"), "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
                    return _persist_resume_result(store, job, resume_results, original_request)
                if result.status != "ok":
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"module deploy failed: {result.error}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoo_module_status":
                result = odoo.module_status(module_name)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}

            elif step_name == "registry_field_verify":
                if not expected_registry_fields:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No expected Python field additions were inferred or provided."}
                    continue
                result = _verify_registry_fields(expected_registry_fields)
                resume_results["steps_run"][step_name] = {"status": result.status, "expected_registry_fields": expected_registry_fields, "result": _step_payload(result)}
                if result.status != "ok":
                    resume_results["status"] = "registry_field_missing"
                    resume_results["message"] = "Module upgrade finished but the live Odoo registry is missing expected fields. Do not claim success."
                    resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "combined_live_verification":
                assertion = original_request.get("verification_assertion")
                if not assertion:
                    resume_results["steps_run"][step_name] = {"status": "skipped"}
                    continue
                result = LiveAssertionService().evaluate(
                    assertion,
                    client_name=original_request.get("client_name") or req.client_name,
                    project_path=project_path,
                    phase="post_deploy_resume",
                )
                resume_results["steps_run"][step_name] = {
                    "status": "ok" if result.get("passed") else "failed",
                    "result": result,
                }
                resume_results["live_verification"] = result
                if not result.get("passed"):
                    resume_results["status"] = "live_verification_failed"
                    resume_results["message"] = "Resumed source/upgrade steps passed, but the task-specific live assertion failed."
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "registry_smoke_test":
                result = odoo.registry_smoke_test(module_name, [], [], strict=False)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}


        except Exception as exc:
            resume_results["steps_run"][step_name] = {"status": "error", "error": str(exc)}
            resume_results["status"] = "failed"
            resume_results["error"] = f"Step '{step_name}' raised exception: {exc}"
            return _persist_resume_result(store, job, resume_results, original_request)

    resume_results["status"] = "ok"
    resume_results["message"] = f"Pipeline resumed and completed from step '{start_from}'"
    store.log_audit("workflow.resume_job", "ok", f"Completed resume of job {job_id}")
    return _persist_resume_result(store, job, resume_results, original_request)
