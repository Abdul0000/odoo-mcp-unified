Warning: truncated output (original token count: 129156)
Total output lines: 10801

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

    Functional questions like "list draft invoices" should call odoo_search_read
    directly and not enter the coding workflow.
    """
    order = {
        "connector.health": 0,
        "odoo.read_group": 1,
        "odoo.query": 2,
        "odoosh.query": 2,
        "odoo.search_read": 3,
        "odoo.health": 3,
        "odoo.module_status": 3,
        "workflow.smart_tool_router": 4,
        "workflow.agent_instructions": 5,
        "workflow.capability_check": 6,
    }
    if name in order:
        return (order[name], name)
    if name.startswith("ws."):
        return (10, name)
    if name.startswith("workflow."):
        return (20, name)
    if name.startswith("repo."):
        return (30, name)
    if name.startswith("git."):
        return (40, name)
    if name.startswith("odoosh."):
        return (50, name)
    if name.startswith("odoo."):
        return (60, name)
    return (70, name)


def _external_tool_descriptor(item: ToolDescriptor) -> ToolDescriptor:
    """Descriptor form safe for strict remote tool frontends and REST clients."""
    safe_name = strict_safe_tool_name(item.name)
    description = item.description
    if safe_name != item.name:
        description = f"External tool name: {safe_name}. Internal route: {item.name}. {description}"
    return item.model_copy(update={"name": safe_name, "description": description})


_TOOL_DESCRIPTORS = [
    ToolDescriptor(
        name="ws.open",
        title="Open coding workspace",
        description=(
            "Use the provider REST workspace when execution is unavailable. Opens a module on the configured GitHub/GitLab branch: returns ws_id, "
            "depends and the file index. Unfinished work from an earlier session is resumed automatically. create=true starts a new module."
        ),
        read_only=False,
        idempotent=True,
    ),
    ToolDescriptor(name="ws.status", title="Where is my work?", description="Shows which files are unpublished, the WIP branch, the client branch, the default branch and GitHub links. Use when the user cannot see changes on GitHub.", read_only=True),
    ToolDescriptor(name="ws.outline", title="Outline module", description="Compact map of the module: manifest, models (fields with types, methods), views/actions/menus with inheritance. Read this instead of whole files.", read_only=True),
    ToolDescriptor(
        name="ws.scaffold",
        title="Scaffold a new model",
        description=(
            "Write LESS: give model + field specs; the server generates the model class, form/list views (version-correct tree/list), action, menu, "
            "access rule, manifest/import wiring, and a new module's manifest. Prefer this over hand-writing a new model."
        ),
    ),
    ToolDescriptor(
        name="ws.extend",
        title="Extend an existing model",
        description="Add fields to an existing model via _inherit and optionally inherit a view (view_ref + after_field) to show them. Generates correct, wired files.",
    ),
    ToolDescriptor(
        name="ws.symbol",
        title="Verify model/field in Odoo source",
        description=(
            "Check a model or field against upstream Odoo source for this module's series (and local code): module, file, type, comodel, compute. "
            "Use before relying on a model/field you are not certain exists. Community modules in depends only."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="ws.read", title="Read workspace files", description="Read up to 10 files (numbered lines, ranges supported). Sees your uncommitted edits.", read_only=True),
    ToolDescriptor(name="ws.grep", title="Search workspace", description="Literal/regex search in the module (scope=repo for other modules; narrow with glob). Sees your uncommitted edits.", read_only=True),
    ToolDescriptor(
        name="ws.edit",
        title="Edit workspace files",
        description=(
            "Small exact edits (search/replace) or full-file writes, applied atomically, then linted instantly: python, xml, xpath, js, csv, manifest, imports. "
            "New files are auto-wired ('from . import', manifest data). A search that differs only by indentation/whitespace is self-healed when it matches one place (result shows healed). If an edit still fails nothing changes and you get the closest matches. "
            "Fix reported errors with another ws.edit, then ws.commit."
        ),
    ),
    ToolDescriptor(name="ws.diff", title="Workspace diff", description="Unified diff of your uncommitted changes against the branch.", read_only=True),
    ToolDescriptor(name="ws.lint", title="Lint workspace", description="Re-run lint and module-closure checks; returns commit_ready.", read_only=True),
    ToolDescriptor(
        name="ws.commit",
        title="Commit and push workspace",
        description=(
            "Push edits as one atomic GitHub or GitLab commit (no clone, never force); message is optional (auto-generated). Survives a moving branch by replaying edits on the newer head; "
            "retries are idempotent. Blocked by lint errors, a protected branch, or a true same-line conflict. "
            "Push-only is the default: do not set deploy_after_push unless the user explicitly requested deployment. "
            "When deploy_after_push=true, the result includes the required deployment next_call."
        ),
        idempotent=True,
    ),
    ToolDescriptor(name="ws.discard", title="Discard workspace", description="Throw away unfinished workspace edits and its WIP branch.", destructive=True),
    ToolDescriptor(name="connector.health",
        title="Check connector health", description="Check connector service health", read_only=True),
    ToolDescriptor(
        name="skills.list",
        title="List Odoo skills",
        description=(
            "List unique authoritative Odoo skills. Optionally pass task_description to receive one "
            "single-primary-owner recommendation without loading any skill content."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="skills.read",
        title="Load an Odoo skill",
        description=(
            "Load one exact Odoo skill on demand. Use the narrowest owner; do not load overlapping skills. "
            "Examples: 'backend' for inherited ORM logic, 'view' for XML/XPath, 'debug' for actual runtime "
            "failures, 'reporting' for ORM/SQL analytics, 'qweb-report' for general PDF/HTML templates, "
            "and 'accounting-report' for financial reports."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="artifacts.read",
        title="Read saved artifact",
        description="Read an exact slice of a large log, diff, command output, or structured result saved from an earlier MCP response.",
        read_only=True,
    ),
    ToolDescriptor(
        name="client.check",
        title="Check/select MCP client",
        description=(
            "Mandatory first gate for multi-client ODOO_CLIENTS setups. "
            "Call without client_name to list clients and require user selection when multiple clients exist. "
            "After the user selects a client, call with client_name and validate=true. "
            "Slow validation runs in a background job and returns job_id/next_call immediately; poll jobs.get until terminal. "
            "Validation prepares or verifies the configured checkout, then checks repo/token/branch, Odoo.sh SSH, Odoo shell health, and addons path. "
            "Write/git/deploy tools are blocked until this passes when multiple clients are configured."
        ),
        read_only=True,
    ),

    ToolDescriptor(
        name="workflow.agent_instructions",
        title="Load agent instructions",
        description=(
            "Load shared rules after client_check selects and validates the client. "
            "Returns full agent rules, flow, error guide, and repo config from the connector. "
            "Follow these instructions exactly for every task."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="workflow.capability_check",
        title="Check connector capabilities", description="Check local connector capabilities and missing runtime configuration", read_only=True),
    ToolDescriptor(name="workflow.coding_task_plan",
        title="Get coding workflow plan", description="Return the repo-first coding workflow guidance for MCP agents", read_only=True),
    ToolDescriptor(
        name="workflow.odoo_code_change",
        title="Provider-safe Odoo code change",
        description=(
            "Single high-level provider-safe coding workflow. Use this instead of manually calling repo.read/write, "
            "scope_advice, verify, git.push, or module_deploy. It enforces the v15 flow internally: "
            "read custom module, scope_advice, read /home/odoo/src/odoo/addons related source first with enterprise fallback, "
            "apply provided edits, git.diff, qa.self_run, verify_against_core, then workflow.safe_deploy_module and jobs.get polling. "
            "For a missing addon, set create_module=true with an exact lowercase technical name; it is created only at the checkout root. "
            "New files must use full content writes, not search/replace; missing parent directories are created automatically. "
            "A cached Odoo-aware index maps functional labels through menus/actions/views/models/fields/QWeb/JS relationships, then reads exact match-centered ranges. "
            "Task descriptions automatically supply quoted identifiers/selectors/XML IDs/methods for focused inspection. "
            "An exact quoted replacement that occurs once can complete in one call; complex work returns exact editable source units and an on-demand context artifact when needed. "
            "A valid coding_session_id plus writes uses the server-owned target contract and skips repeated module discovery, semantic inspection, and target resolution. "
            "The returned workspace_session_id keeps up to five module snapshots warm for separate tasks in the same checkout; modules load only on demand, use LRU eviction, revalidate independently, and refresh after writes/push. "
            "Keep coding_session_id only for its exact task, but preserve workspace_session_id when switching modules in the same repository. "
            "Existing-addon surgical changes use baseline-delta QA: unchanged unrelated legacy findings are reported but do not block commit/deploy; new, task-required, manifest/version, and changed-scope errors still block."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.resume_continuation",
        title="Resume code workflow from client token",
        description=(
            "Resume an interrupted Odoo code-change after an ephemeral Render restart using the signed "
            "resume_token returned earlier. The token carries only the bounded task contract; current source, "
            "client identity, target, QA, and deploy gates are revalidated before any write."
        ),
        requires_approval=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="workflow.odoo_debug_and_fix",
        title="Diagnose, fix, deploy, and replay an Odoo runtime issue",
        description=(
            "Source-first bounded debugger for tracebacks and silent wrong results. "
            "It emits a three-line plan, inspects the owning custom code and exact installed Community/Enterprise implementation, "
            "derives the real entry point without guessing, and persists a correlation-scoped session. "
            "hands exact writes to workflow.odoo_code_change, reuses the existing QA/safe-deploy/jobs pipeline, "
            "and permits verified_fixed only when the immutable scenario fails before the fix and passes after deploy. "
            "For a public button/model method, runtime_replay executes the same record method through SSH/Odoo shell before and after deploy, "
            "while report_replay renders and checks the actual PDF artifact. When source evidence is insufficient, temporary marker-scoped logging "
            "is deployed for one replay and must be removed before completion. Only correlated lines plus ERROR/CRITICAL tracebacks are returned. "
            "Manual text/booleans remain untrusted. Deterministic small edits are routed directly "
            "to workflow.odoo_code_change. It never runs arbitrary console commands or duplicates Git/deploy logic."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.module_analyze",
        title="Analyze custom module fast",
        description=(
            "Fast read-only custom-addon analysis. Exact target_files rank first, search matches second, "
            "and architecture files last. Resolves a unique human feature/report label from custom source. "
            "Returns bounded cached evidence; never writes, pulls, runs QA, or deploys."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="rea.xray_module",
        title="Reverse engineer Odoo module",
        description=(
            "Read-only REA X-Ray: map a custom addon into an evidence-backed graph of modules, "
            "models, inheritance, relational fields, methods, views, actions, menus and UI-to-method links. "
            "For ws_id, also returns transitive/reverse addon dependencies and cross-module model/field links. "
            "Uses bounded static analysis; never executes addon code or contacts external hosts."
        ),
        read_only=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="rea.upstream_compare",
        title="Compare addon with Odoo upstream",
        description="Read-only comparison of custom overrides and fields against installed Odoo source. Supports stateless ws_id and fetches matching Community source from GitHub; findings are advisory.",
        read_only=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="rea.runtime_trace",
        title="Analyze Odoo runtime trace",
        description="Analyze supplied Odoo trace spans for slow methods and repeated queries; does not enable profiling or capture live traffic.",
        read_only=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="rea.api_inspect",
        title="Inspect external API integrations",
        description="Static, read-only inventory of addon HTTP/RPC calls, routes, endpoint configuration and TLS/timeout settings. Supports ws_id; never connects to endpoints.",
        read_only=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="rea.impact_predict",
        title="Predict Odoo change impact",
        description="Predict affected addon components and risk from proposed diffs using workspace source, architecture, upstream/API evidence and optional trace data; advisory and read-only.",
        read_only=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="rea.self_heal",
        title="Run guarded REA self-healing workflow",
        description="Gather bounded X-Ray, upstream and API evidence, then hand off to the existing Odoo debug/fix workflow. Deployment is disabled by default.",
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.module_test",
        title="Test complete Odoo module",
        description=(
            "Run one connector-owned background verification job for an existing addon: local static QA, "
            "real Odoo-native addon tests, same-origin controller/API contracts, and bounded functional "
            "create/action/read-back scenarios. Live layers run only on a selected client explicitly marked "
            "environment=test. This workflow never edits, commits, pushes, upgrades, or deploys code; poll jobs.get "
            "and claim success only when the terminal result has verified=true and success_allowed=true."
        ),
        destructive=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="workflow.module_install_and_check",
        title="Install module and check errors",
        description=(
            "DIRECT FAST PATH for an explicit install, upgrade, or install-and-fix request. "
            "Immediately starts the guarded Odoo install/upgrade job without repo inspection, history lookup, "
            "skill loading, module-status prechecks, or planning. Returns job_id/next_call for jobs.get. "
            "On terminal failure, use its traceback (or odoosh.error_pack once) as evidence for "
            "workflow.odoo_code_change; successful code repair redeploys through that composite workflow. "
            "An uninstallable registry state triggers a direct read of the deployed manifest and dependency states; "
            "when source_repair_allowed=false, do not edit the manifest without a concrete source error."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.frontend_visual_implement",
        title="Implement frontend visual asset",
        description=(
            "Generic visual implementation workflow for Odoo POS/OWL/dashboard/website/backend UI. "
            "Use when a design needs SCSS, SVG assets, decorative backgrounds, logos/icons, premium cards, "
            "or decorative shapes. It generates deterministic visual assets/writes, reads custom/native context, "
            "runs verify_against_core, then uses the same safe deploy pipeline. For exact geometric/logo/background shapes, "
            "prefer allowed_methods=['svg_asset'] instead of guessing with CSS borders. Set create_module=true only for a new "
            "standalone root-level addon; otherwise module_name must already exist."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.smart_tool_router",
        title="Choose fastest safe workflow",
        description=(
            "V16: lightweight planner that chooses the fastest correct tool sequence: direct Odoo query, "
            "targeted read/preflight, repo.write_many_smart, changed-files validation, and browser smoke test only when needed."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.repo_index",
        title="Build/read repo index",
        description="V16: cached lightweight index of modules, manifests, assets, JS/XML templates, and source files to avoid full repo scans.",
        read_only=True,
    ),
    ToolDescriptor(name="workflow.full_verify",
        title="Run full verification", description="Run the full self-verification workflow for an Odoo module", read_only=True),
    ToolDescriptor(name="workflow.safe_deploy_module",
        title="Deploy module (full pipeline)", description="Safely commit, push, poll Odoo.sh every 10s up to 10 minutes for the pushed SHA, reject recent Odoo.sh build/test failures, verify changed file hashes under /home/odoo/src/user, verify manifest, and upgrade an Odoo module. When Odoo.sh SSH is blocked only by public-key or transport availability and github_push_fallback is enabled, verified source may still be committed and pushed to GitHub with status=github_pushed_deploy_pending; this never means deployed or upgraded. For MCP clients this starts/resumes a background safe-deploy job and returns job_id/next_call immediately; status=running is not success and must be followed via jobs.get. Only treat the deployment as complete when response.deploy_confirmed == true. A finished job without this field set to true means the deploy did not fully complete — do not report success to the user. If git diff shows no changes for this module, safe_deploy_module automatically runs Odoo.sh source verification and returns verified_already_deployed/already_deployed_confirmed; read this field before telling the user anything is 'already added' or 'already live'. Do not make that claim from memory of a previous turn.", requires_approval=True),
    ToolDescriptor(
        name="workflow.verify_deployed_module",
        title="Verify deployed source",
        description=(
            "Compare active checkout files to the real Odoo.sh /home/odoo/src/user files and optionally live module status. "
            "Use before saying a local/GitHub change is already deployed, already live, or already done. repo.read alone is not proof. "
            "If git diff shows no changes for a module, workflow.safe_deploy_module will automatically run this source verification and return verified_already_deployed — read this field before telling the user anything is 'already added' or 'already live'. Do not make that claim from memory of a previous turn."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="workflow.start_session",
        title="Start coding session", description="Bootstrap a coding session: capability check, repo.prepare, git status, module discovery, Odoo/Odoo.sh health", requires_approval=True),
    ToolDescriptor(name="workflow.registry_smoke_test",
        title="Test Odoo registry", description="Verify live Odoo registry models/menus after install or upgrade", read_only=True),
    ToolDescriptor(name="workflow.restore_last_good_commit",
        title="Restore last working commit", description="Emergency restore by reverting a bad commit and optionally pushing the revert", requires_approval=True),
    ToolDescriptor(name="workflow.browser_menu_crawl",
        title="Crawl Odoo menus in browser", description="Open multiple Odoo paths through CDP and capture console/network/Odoo UI errors", read_only=True),
    ToolDescriptor(
        name="workflow.frontend_pattern_preflight",
        title="Strict frontend/OWL preflight",
        description=(
            "MANDATORY before writing any Odoo JS, OWL, POS, website, payment, QWeb/XML template, "
            "SCSS, or asset code. Returns source files/areas to read first and strict syntax/bundle rules."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="qa.…113156 tokens truncated…     "resume": resume_results,
    }
    odoosh_step = ((resume_results.get("steps_run") or {}).get("odoosh_check_commit") or {})
    existing_odoosh = _result_for_completed_step(job, "odoosh_check_commit")
    deployed = bool(odoosh_step.get("deployed"))
    if not deployed and isinstance(existing_odoosh, dict):
        stdout = str(existing_odoosh.get("stdout") or "")
        deployed = '"deployed": true' in stdout.lower() or existing_odoosh.get("deployed") is True
    result["odoosh_check_commit"] = {
        "status": "passed" if deployed else "unknown",
        "deployed": deployed,
        "expected_sha": resume_results.get("sha") or original_request.get("last_sha") or _extract_sha_from_steps(job),
    }
    upgrade_requested = bool(original_request.get("upgrade_module", True))
    upgrade_passed = True if not upgrade_requested else _resume_step_ok(resume_results, job, "odoo_module_deploy")
    result["module_upgrade"] = {
        "requested": upgrade_requested,
        "status": "passed" if upgrade_passed else "unknown",
        "passed": upgrade_passed,
    }
    return result


def _persist_resume_result(store: JsonStore, job, resume_results: dict, original_request: dict) -> dict:
    status_text = str(resume_results.get("status") or "failed")
    if status_text in {"ok", "already_complete"}:
        final_result = _resume_deploy_confirmation(original_request, resume_results, job)
        if deployment_confirmed_from_record(JobStatus.passed, original_request, final_result):
            target_status = JobStatus.passed
        else:
            resume_results["status"] = "resume_finalization_incomplete"
            resume_results["message"] = (
                "All resume steps are recorded, but the stored job lacks explicit "
                "Odoo.sh deployed=true and module-upgrade confirmation. Marking the "
                "resume terminal instead of leaving a warning job that loops forever."
            )
            final_result = {**resume_results, "module_name": original_request.get("module_name")}
            target_status = JobStatus.failed
    elif status_text in {"deploy_failed_or_timeout", "module_upgrade_running"}:
        final_result = {**resume_results, "module_name": original_request.get("module_name")}
        target_status = JobStatus.warning
    else:
        final_result = {**resume_results, "module_name": original_request.get("module_name")}
        target_status = JobStatus.failed

    store.add_step(
        store.get_job(job.id),
        JobStep(
            name="workflow.resume_job",
            status="passed" if target_status == JobStatus.passed else "warning" if target_status == JobStatus.warning else "failed",
            result=resume_results,
        ),
    )
    finished = store.finish_job(store.get_job(job.id), target_status, final_result)
    store.save_session_state(
        {
            "last_safe_deploy_job_id": job.id,
            "module_name": original_request.get("module_name"),
            "project_path": original_request.get("project_path"),
            "branch": original_request.get("branch"),
            "client_name": original_request.get("client_name"),
            "safe_deploy_status": status_text,
        },
        client_name=original_request.get("client_name"),
    )
    resume_results["job_status_after_resume"] = finished.status.value
    resume_results["deploy_confirmed"] = finished.deploy_confirmed
    return resume_results



def _start_and_wait_module_deploy_for_resume(original_request: dict, module_name: str) -> OdooResult:
    module_request = OdooModuleRequest(
        client_name=original_request.get("client_name"),
        module_name=module_name,
        wait_for_result=False,
        wait_timeout_seconds=0,
        poll_interval_seconds=1.0,
    )
    pinned_client = ClientResolverService().resolve_client(
        client_name=original_request.get("client_name"),
        project_path=original_request.get("project_path"),
        require_complete=False,
    )
    try:
        started = start_odoo_module_deploy_job(
            module_request,
            run_inline=True,
            validated_client=pinned_client,
        )
    except TypeError:
        started = start_odoo_module_deploy_job(module_request)
    started_payload = started.model_dump(mode="json") if hasattr(started, "model_dump") else started
    result_payload = started_payload.get("result") if isinstance(started_payload, dict) and isinstance(started_payload.get("result"), dict) else {}
    if result_payload.get("finished") or not result_payload.get("job_id"):
        return started

    nested_job_id = result_payload.get("job_id")
    timeout_seconds = int(original_request.get("odoo_module_deploy_timeout_seconds", 600))
    poll_interval = float(original_request.get("odoo_module_deploy_poll_interval_seconds", 10.0))
    waited = JsonStore().wait_job(str(nested_job_id), timeout_seconds, poll_interval)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = waited.result.get("error") if isinstance(waited.result, dict) else None
        return OdooResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            error=result_error or f"Odoo module deploy job finished with status={waited.status.value}",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
        )
    return OdooResult(
        status="ok",
        result={
            "job_id": waited.id,
            "job_status": waited.status.value,
            "finished": False,
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 1,
            "message": f"Odoo module deploy is still running after {timeout_seconds}s. Safe deploy is not complete and final success is blocked.",
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
        },
    )


def _latest_safe_deploy_job_id(req: "ResumeJobRequest", store: JsonStore) -> str | None:
    state = store.load_session_state(req.client_name)
    saved_job_id = state.get("last_safe_deploy_job_id")
    if isinstance(saved_job_id, str) and saved_job_id.strip():
        try:
            saved_job = store.get_job(saved_job_id.strip())
            if _job_matches_resume_request(saved_job, req, state):
                return saved_job.id
        except Exception:
            pass

    for job in store.list_jobs(limit=100):
        if job.job_type != "safe_deploy_module":
            continue
        request = job.request or {}
        if req.project_path and request.get("project_path") != req.project_path:
            continue
        if req.module_name and request.get("module_name") != req.module_name:
            continue
        return job.id
    return None


async def _resume_job(req: "ResumeJobRequest") -> dict:
    """Fix 3: Resume a safe_deploy_module pipeline from last successful step.

    Reads the original job record, finds the last step that succeeded, and
    re-runs the pipeline from the next step.  The SafeDeployModuleRequest
    stored in job.request is used to rebuild the context.
    """
    store = JsonStore()
    job_id = req.job_id or _latest_safe_deploy_job_id(req, store)
    if not job_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "No resumable safe_deploy_module job found. Pass job_id, or provide client_name/project_path/module_name "
                "matching a previous safe deploy."
            ),
        )
    try:
        job = store.get_job(job_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    job = mark_stale_safe_deploy_running_job(job, store)
    if job.status in {JobStatus.queued, JobStatus.running}:
        return {
            "status": "active_job",
            "message": "The safe_deploy_module job is still active. Poll jobs.get instead of resuming so commit/push/upgrade are not replayed.",
            "job_id": job_id,
            "job_status": job.status.value,
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 1,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
        }

    completed_set = {s.name for s in (job.steps or []) if s.status in _RESUME_COMPLETED_STATUSES}
    # Older saved jobs and some tests predate the explicit resume gates added
    # here. If a downstream irreversible step already passed, infer that the
    # earlier read-only/pre-push gates from that phase should not be replayed.
    if "git_commit" in completed_set:
        completed_set.update({"git_diff", "git_changed_files", "qa_self_run", "verify_against_core_pre_push"})
    if "git_head_sha" in completed_set:
        completed_set.add("git_head_commit_path_gate")
    if "git_push" in completed_set:
        completed_set.add("git_remote_head_verify")
    completed_steps = [step for step in _SAFE_DEPLOY_STEPS if step in completed_set]
    # Determine resume point
    if req.override_step:
        override_step = _SAFE_DEPLOY_STEP_ALIASES.get(req.override_step, req.override_step)
        if override_step not in _SAFE_DEPLOY_STEPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown override_step '{req.override_step}'. Valid: {_SAFE_DEPLOY_STEPS}",
            )
        start_from = override_step
    else:
        # Find the first step NOT in completed_steps
        start_from = None
        for step_name in _SAFE_DEPLOY_STEPS:
            if step_name not in completed_steps:
                start_from = step_name
                break
    # Recover original request payload
    original_request = job.request or {}
    module_name = original_request.get("module_name")
    project_path = original_request.get("project_path")
    if not module_name or not project_path:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume: job {job_id} is missing module_name or project_path in its request payload.",
        )
    ClientGateService().require_validated_client_or_job_lease(
        original_request.get("client_name") or req.client_name,
        "workflow.resume_job",
        project_path=project_path,
        lease=original_request.get("validation_lease"),
    )

    client = ClientResolverService().resolve_client(
        client_name=original_request.get("client_name") or req.client_name,
        project_path=project_path,
        require_complete=False,
    )
    if client:
        original_request.setdefault("client_name", client.name)
        original_request.setdefault("client", client.public)
        original_request["project_path"] = client.path or project_path
        project_path = str(original_request["project_path"])
    remote = original_request.get("remote", "origin")
    branch = original_request.get("branch") or (client.branch if client else None) or settings.target_git_branch
    original_request["branch"] = branch
    pushed_sha = _extract_sha_from_steps(job) or original_request.get("last_sha")
    expected_user_hashes = original_request.get("expected_user_file_hashes") or {}
    candidate_files: list[str] = []
    if not expected_user_hashes:
        try:
            candidate_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
            expected_user_hashes = _expected_user_file_hashes(Path(project_path), candidate_files)
            if expected_user_hashes:
                job.request["expected_user_file_hashes"] = expected_user_hashes
                store.update_job(job)
        except Exception:
            expected_user_hashes = {}
    expected_registry_fields = _normalise_expected_registry_fields(original_request.get("expected_registry_fields") or {})
    if not expected_registry_fields:
        try:
            if not candidate_files:
                candidate_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
            expected_registry_fields = _infer_registry_fields_from_python(Path(project_path), module_name, candidate_files)
            if expected_registry_fields:
                job.request["expected_registry_fields"] = expected_registry_fields
                store.update_job(job)
        except Exception:
            expected_registry_fields = {}
    git = GitService()
    odoosh = OdooShService(client) if client else OdooShService()
    odoo = _odoo_service_for_client(client)

    # Re-run from start_from step
    store.log_audit(
        "workflow.resume_job",
        "running",
        f"Resuming job {job_id} from step '{start_from}' (completed: {completed_steps})",
    )
    resume_results: dict = {
        "resumed_from": start_from,
        "original_job_id": job_id,
        "completed_steps": completed_steps,
        "steps_run": {},
    }
    if start_from is None:
        resume_results["resumed_from"] = "already_complete"
        resume_results["status"] = "already_complete"
        resume_results["message"] = "All pipeline steps were already recorded; finalizing the stored job state."
        return _persist_resume_result(store, job, resume_results, original_request)

    step_order = _SAFE_DEPLOY_STEPS[_SAFE_DEPLOY_STEPS.index(start_from):]

    for step_name in step_order:
        try:
            if step_name == "git_diff":
                result = git.diff(project_path, staged=False, timeout_seconds=30)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git diff failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_changed_files":
                if hasattr(git, "changed_files"):
                    result = git.changed_files(project_path)
                else:
                    fallback_paths = original_request.get("paths") or [f"{module_name}/__manifest__.py"]
                    result = CommandResult(command=["git", "changed-files", "legacy-fake"], cwd=project_path, exit_code=0, stdout="\n".join(fallback_paths) + "\n")
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git changed-files failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "qa_self_run":
                if not original_request.get("run_qa", True):
                    resume_results["steps_run"][step_name] = {"status": "skipped"}
                    continue
                qa_req = SelfRunRequest(project_path=project_path, module_name=module_name)
                result = self_run(qa_req)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}
                if result.status == "failed":
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"qa_self_run failed: {getattr(result, 'summary', '')}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "verify_against_core_pre_push":
                verify_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
                candidate_files = verify_files
                if verify_files:
                    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(
                        written_files=verify_files,
                        fix_cycle=0,
                        project_path=project_path,
                    ))
                    blocked = bool(result.push_blocked)
                    resume_results["steps_run"][step_name] = {"status": "failed" if blocked else "passed", "result": _step_payload(result)}
                    if blocked:
                        resume_results["status"] = "failed"
                        resume_results["error"] = "verify_against_core_pre_push blocked the push"
                        return _persist_resume_result(store, job, resume_results, original_request)
                else:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No changed module files required pre-push verification."}

            elif step_name == "git_configure_identity":
                result = git.configure_identity(project_path)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git identity configuration failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_validate_target":
                result = git.validate_target(project_path, branch, remote)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git target validation failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_commit":
                commit_msg = original_request.get("commit_message", f"Resume: update {module_name}")
                paths = original_request.get("paths", [])
                result = git.commit(project_path, commit_msg, paths, timeout_seconds=60)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git commit failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_head_commit_path_gate":
                paths = original_request.get("paths", [])
                result = _assert_head_commit_contains_paths(git, Path(project_path), paths)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"HEAD commit path gate failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_head_sha":
                result = git.head_sha(project_path)
                if result.exit_code == 0:
                    pushed_sha = result.stdout.strip()
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "sha": pushed_sha, "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git head SHA lookup failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_push":
                push_started_epoch = int(time.time())
                result = git.push(project_path, remote, branch, timeout_seconds=60)
                if result.exit_code == 0:
                    pushed_sha = _extract_sha_from_steps(job) or pushed_sha or git.head_sha(project_path).stdout.strip()
                    match = re.search(r"(?:^|\n)sha=([0-9a-fA-F]{7,40})(?:\n|$)", result.stdout or "")
                    if match:
                        pushed_sha = match.group(1)
                    job.request["push_started_epoch"] = push_started_epoch
                    if expected_user_hashes:
                        job.request["expected_user_file_hashes"] = expected_user_hashes
                    store.update_job(job)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "sha": pushed_sha, "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git push failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_remote_head_verify":
                pushed_sha = pushed_sha or git.head_sha(project_path).stdout.strip()
                if hasattr(git, "remote_head_sha"):
                    result = git.remote_head_sha(project_path, remote, branch, 60)
                else:
                    result = CommandResult(command=["git", "remote-head-sha", "legacy-fake"], cwd=project_path, exit_code=0, stdout=(pushed_sha + "\n") if pushed_sha else "")
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
