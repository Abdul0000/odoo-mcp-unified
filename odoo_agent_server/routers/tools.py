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
            "START HERE for any Odoo code change. Opens a module on the client's GitHub branch with no clone: returns ws_id, "
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
            "Push your edits as one atomic GitHub commit (no clone, never force); message is optional (auto-generated). Survives a moving branch by replaying edits on the newer head; "
            "retries are idempotent. Blocked only by lint errors, a protected branch, or a true same-line conflict. "
            "The result's next_call runs the Odoo.sh deploy proof; a push alone is not a deployment."
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
            "CALL THIS FIRST in every session before any other tool. "
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
    ToolDescriptor(name="qa.self_run",
        title="Run code quality checks", description="Run Odoo custom module static QA and optional pytest/git status", read_only=True),
    ToolDescriptor(name="repo.prepare",
        title="Clone or update repository", description="Clone or refresh target repo and checkout/pull the target branch before coding", requires_approval=True),
    ToolDescriptor(name="repo.list_custom_modules",
        title="List custom Odoo modules", description="Discover custom Odoo modules from repo __manifest__.py files", read_only=True),
    ToolDescriptor(name="repo.list",
        title="List repository files", description="List files inside an allowed project path", read_only=True),
    ToolDescriptor(name="repo.read",
        title="Read file contents", description="Read a UTF-8 file. Default max 300KB. For large files pass max_bytes=500000. Returns file content and line count.", read_only=True),
    ToolDescriptor(
        name="repo.read_range",
        title="Read file line range",
        description="Read exact line-numbered slices from a UTF-8 file. Use after repo.search or when repo.read is too large.",
        read_only=True,
    ),
    ToolDescriptor(
        name="repo.search",
        title="Search repository text",
        description="Bounded text search inside a repo path with line numbers and small context. Use before reading large or unknown files.",
        read_only=True,
    ),
    ToolDescriptor(
        name="repo.write",
        title="Write file contents",
        description=(
            "Write a complete file. Single-pass writes are supported up to 1000 lines. "
            "For files >1000 lines, use CHUNKED MODE to avoid token limits: "
            "Split file into chunks; follow-up chunks have no single-pass warning limit. "
            "First call: chunk_index=0, total_chunks=N. "
            "Next calls: chunk_index=1,2...N-1 with total_chunks=N. "
            "Each chunk is appended. Response shows progress and confirms when complete. "
            "NEVER write partial files without chunked mode. Inside an Odoo addon, Python writes automatically "
            "create missing __init__.py files and propagate safe relative imports without changing the manifest."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="repo.write_many_smart",
        title="Write multiple files smartly",
        description=(
            "PREFERRED for multi-file writes. Auto-selects fast-safe lock+backup+rollback for small related edits "
            "and falls back to git temp-worktree atomic for large/risky writes. Keeps quality high while avoiding "
            "slow repo.write_many_atomic for normal OWL/POS/website/XML changes. Each file should use "
            "relative_path; path is also accepted as a compatibility alias. Odoo Python package init files/imports "
            "are added to the same protected transaction automatically."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="repo.write_many_fast_safe",
        title="Write small multi-file change fast",
        description=(
            "Fast-safe small multi-file writer: validates paths, locks repo, backs up original files, writes through "
            "temporary files, and rolls back on failure. Use for <= configured limits; still run git.diff and changed-files verification after."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(name="repo.write_many_atomic",
        title="Write multiple files safely", description="Slowest/premium safety path: write many files as one isolated git temp-worktree transaction. Use for large/risky refactors only.", requires_approval=True),
    ToolDescriptor(name="repo.delete",
        title="Delete file from repo", description="Delete a file inside an allowed project path", requires_approval=True),
    ToolDescriptor(name="repo.apply_patch",
        title="Apply code patch", description="[LEGACY — use repo.apply_patch_atomic instead] Check/apply a unified diff patch", requires_approval=True, deprecated=True),
    ToolDescriptor(name="repo.apply_patch_atomic",
        title="Apply patch safely (atomic)", description="Apply a patch through an isolated temp worktree/branch before touching the real checkout; new Odoo Python files receive package __init__.py wiring inside the same transaction", requires_approval=True),
    ToolDescriptor(name="repo.clean_pyc",
        title="Clean compiled Python files", description="Remove .pyc and __pycache__ from a project", requires_approval=True),
    ToolDescriptor(name="git.status",
        title="Check Git status", description="Read git status", read_only=True),
    ToolDescriptor(name="git.current_branch",
        title="Check current Git branch", description="Read current git branch", read_only=True),
    ToolDescriptor(name="git.diff",
        title="Review code changes (diff)", description="Read git diff", read_only=True),
    ToolDescriptor(name="git.commit",
        title="Commit code changes", description="Create a git commit", requires_approval=True),
    ToolDescriptor(name="git.push",
        title="Push code to GitHub", description="Direct git.push is blocked for Odoo module code changes. Use workflow.safe_deploy_module so push, deploy, upgrade, and verification remain one controlled job. Non-module pushes may still use this tool.", requires_approval=True),
    ToolDescriptor(
        name="odoosh.module_install_ssh",
        title="Install module via SSH",
        description=(
            "[LEGACY — odoo.module_deploy auto-falls-back to SSH on Access Denied, no need to call this directly] "
            "Install a NEW module via Odoo.sh SSH using odoo-bin -i flag. "
            "Do not call this directly after a fresh code change. Use workflow.safe_deploy_module instead, "
            "which pushes code and verifies the remote HEAD before upgrading. This tool will refuse to run "
            "if local commits are not yet pushed to the remote branch."
        ),
        deprecated=True,
    ),
    ToolDescriptor(name="odoosh.health",
        title="Check Odoo.sh connection", description="Check Odoo.sh SSH health", read_only=True),
    ToolDescriptor(name="odoosh.git_status",
        title="Check Odoo.sh Git status", description="Read Odoo.sh deployed git status", read_only=True),
    ToolDescriptor(
        name="odoosh.check_commit",
        title="Check Odoo.sh deployment status",
        description=(
            "NON-BLOCKING single source checkout check: returns immediately with deployed=true/false. "
            "deployed=true only means ~/src/user has the SHA; safe_deploy_module also checks recent "
            "Odoo.sh build/test failure evidence and changed /home/odoo/src/user file hashes before module upgrade. Never use odoosh.wait_for_commit."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.wait_for_commit",
        title="Wait for Odoo.sh deployment", description="[DEPRECATED - use odoosh.check_commit in a polling loop] Blocking wait for Odoo.sh deploy", read_only=True),
    ToolDescriptor(name="odoosh.verify_module_manifest",
        title="Verify module manifest", description="Verify a module manifest exists and is installable on Odoo.sh", read_only=True),
    ToolDescriptor(
        name="odoosh.read_core_file",
        title="Read Odoo.sh core source file",
        description=(
            "Read exact installed Odoo.sh source from /home/odoo/src/odoo/addons, /home/odoo/src/enterprise, or /home/odoo/src/user. "
            "Use when workflow.find_core_file/list_module_files identifies a native Odoo file that must be inspected before POS/OWL/XML coding. "
            "For POS native files use base='odoo_addons' and relative_path like 'point_of_sale/static/src/app/generic_components/order_widget/order_widget.xml'."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.list_core_files",
        title="List Odoo.sh core source files",
        description=(
            "List exact installed Odoo.sh source files under /home/odoo/src/odoo/addons, /home/odoo/src/enterprise, or /home/odoo/src/user. "
            "Use to browse native Odoo module folders before reading exact files."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.read_user_file",
        title="Read deployed user file",
        description="Read the actual Odoo.sh /home/odoo/src/user file content only when proving an already-deployed/live claim or investigating deployed-source mismatch. Do not call before editing simple custom XML/CSS/SCSS UI text/style; use repo.read on the checkout target file instead.",
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.list_user_files",
        title="List deployed user files",
        description="List actual Odoo.sh /home/odoo/src/user files for a custom module/subdirectory. Use before claiming code already exists on Odoo.sh.",
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.log",
        title="Read Odoo.sh server log", description="Tail an Odoo.sh log", read_only=True),
    ToolDescriptor(name="odoosh.grep_errors",
        title="Search Odoo.sh error logs", description="Search Odoo.sh logs for errors", read_only=True),
    ToolDescriptor(name="odoosh.error_pack",
        title="Collect Odoo.sh error details", description="Return Odoo.sh tracebacks plus root-cause hints/files/XML/model names", read_only=True),
    ToolDescriptor(
        name="odoosh.module_deploy_ssh",
        title="Deploy module via SSH",
        description=(
            "SSH smart install/upgrade via Odoo.sh. Background job. Use only when you explicitly need SSH; "
            "odoo.module_deploy falls back to this automatically. Do not call this directly after a fresh code change. "
            "Use workflow.safe_deploy_module instead, which pushes code and verifies the remote HEAD before upgrading. "
            "This tool will refuse to run if local commits are not yet pushed to the remote branch."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="odoosh.module_update",
        title="Update module on Odoo.sh",
        description=(
            "Run Odoo module update through Odoo.sh SSH. Do not call this directly after a fresh code change. "
            "Use workflow.safe_deploy_module instead, which pushes code and verifies the remote HEAD before upgrading. "
            "This tool will refuse to run if local commits are not yet pushed to the remote branch."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="odoosh.query",
        title="Query Odoo via SSH",
        description=(
            "Read-only Odoo model query through Odoo.sh SSH using odoo-bin shell. "
            "Use when Odoo shell login/password is missing or failing. Requires client_name in multi-client mode; "
            "does not need Odoo email/password, only Odoo.sh SSH key access."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.health",
        title="Check Odoo server health", description="Check Odoo Odoo shell health", read_only=True),
    ToolDescriptor(
        name="odoo.query",
        title="Smart Odoo functional query",
        description=(
            "Best tool for simple functional Odoo questions from any MCP client: list users, employees, "
            "draft invoices, unpaid invoices, customers, vendors, products, sale orders, purchase orders, or modules. "
            "Takes a natural-language query and returns parsed model/domain/fields plus live Odoo records. "
            "Runs read-only Odoo shell through the selected client's Odoo.sh SSH context."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.module_status",
        title="Check module install status", description="Read Odoo module state (installed/uninstalled/not found). Do not call repeatedly while locating files or before simple custom UI/style edits; use repo.search/repo.read, then write, then workflow.safe_deploy_module.", read_only=True),
    ToolDescriptor(
        name="odoo.module_deploy",
        title="Install or upgrade module",
        description=(
            "SMART INSTALL/UPGRADE — always use this for ALL first-time installs and upgrades. "
            "If a recent manual git.push was recorded, this tool first verifies the pushed SHA, recent "
            "Odoo.sh failure logs, and changed /home/odoo/src/user file hashes before touching the DB. "
            "Runs install/upgrade through Odoo.sh SSH and verifies source/deploy state in the same background job. "
            "For MCP providers this never blocks for final success: it returns job_id + next_call. status=running is not success; call jobs.get until terminal passed/failed before telling the user deployed/upgraded."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(name="odoo.module_install",
        title="Install Odoo module", description="[LEGACY] Direct button_immediate_install — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.module_update",
        title="Upgrade Odoo module", description="[LEGACY] Direct button_immediate_upgrade — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.search_read",
        title="Query Odoo records", description="Run Odoo search_read safely. Uses Odoo.sh SSH first, then HTTPS JSON-RPC read-only fallback when SSH is refused/unavailable for a client.", read_only=True),
    ToolDescriptor(
        name="odoo.feature_inspect",
        title="Inspect live Odoo feature",
        description=(
            "OPTIONAL LIVE CHECK after source analysis. Inspect up to 10 exact technical models, business fields, "
            "selections, views, actions, menus, ACLs, rules and counts in one Odoo.sh SSH/Odoo-shell startup. "
            "Use instead of multiple parallel odoo.search_read calls; do not call when source evidence already "
            "answers the question and the user did not request installed/live state."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.read_group",
        title="Aggregate Odoo records", description="Return live grouped counts, sums, averages, minima, or maxima with safe read_group. Example: account.move customer invoices grouped by state.", read_only=True),
    ToolDescriptor(
        name="odoo.fields_get",
        title="List Odoo model fields",
        description=(
            "Read-only discovery of the fields on one live Odoo model: name, type, relation, "
            "required, readonly, store. Use before writing code that references a field you did not "
            "confirm from installed source."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoo.list_models",
        title="List Odoo models",
        description=(
            "Read-only discovery of live Odoo models from ir.model, optionally filtered by a "
            "technical-name substring. Use to find the exact model before reading records or source."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoo.record_create",
        title="Create Odoo records",
        description=(
            "Create a single or bounded batch of live business records as the configured "
            "Odoo user. Requires an explicit request_id and verifies created records by reading them back."
        ),
        destructive=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="odoo.record_write",
        title="Update Odoo records",
        description=(
            "Update live business records with optional expected_values concurrency protection, "
            "then return before/after read-back evidence. Direct state writes are prohibited."
        ),
        destructive=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="odoo.record_action",
        title="Run approved Odoo business action",
        description=(
            "Execute a public Odoo business action on bounded record IDs. Private/system CRUD methods remain "
            "blocked; posting, stock validation, returns, completion, cancellation, and similar actions require approval."
        ),
        destructive=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="odoo.flow_test",
        title="Run Odoo functional flow",
        description=(
            "Preview or execute a standard recipe or arbitrary ordered dynamic create/write/action/verify "
            "flow with safe prior-step references, without entering the code/deploy pipeline."
        ),
        destructive=True,
        idempotent=True,
    ),
    ToolDescriptor(
        name="odoo.mutation_audit",
        title="Read Odoo mutation audit",
        description="Read functional-operation evidence by request_id and client without changing Odoo.",
        read_only=True,
    ),
    ToolDescriptor(name="browser.screenshot",
        title="Check browser page", description="Navigate an Odoo page through external CDP browser and capture console/network errors; screenshot_path is optional", read_only=True),
    ToolDescriptor(name="browser.pos_flow",
        title="Run POS end-to-end flow", description="Login, open POS, click product, pay, validate order in one CDP browser session with console/network capture at every step. Use for any POS OWL change.", read_only=True),
    ToolDescriptor(name="approvals.list",
        title="List pending approvals", description="List pending/recent approval records", read_only=True),
    ToolDescriptor(name="approvals.request",
        title="Request approval", description="Create an approval request", requires_approval=False),
    ToolDescriptor(
        name="approvals.approve",
        description="Approve this exact pending action only after explicit user consent.",
    ),
    ToolDescriptor(name="jobs.list",
        title="List background jobs", description="List recent connector jobs", read_only=True),
    ToolDescriptor(name="jobs.get",
        title="Check background job status", description="Read one connector job by id; waits server-side by default until done or timeout to avoid fragile client polling. Only treat the deployment as complete when response.deploy_confirmed == true. A finished job without this field set to true means the deploy did not fully complete — do not report success to the user.", read_only=True),
    ToolDescriptor(name="audit.list",
        title="View audit log", description="List recent audit events", read_only=True),
    # Fix 3: Resume failed pipeline from last successful step
    ToolDescriptor(
        name="workflow.resume_job",
        title="Resume interrupted job",
        description=(
            "Fix 3 — Resume a failed safe_deploy_module or start_session pipeline from its last "
            "successful step. Pass job_id when available; if omitted, the connector resumes the "
            "last saved safe-deploy job from session state or the latest matching job. Instead of "
            "starting over from step 1, the agent continues from where it left off. Optionally "
            "override the start step with override_step (qa | commit | push | wait_odoosh | "
            "verify_deployed_code | install | upgrade | module_update | smoke_test)."
        ),
        requires_approval=True,
    ),
    # Fix 7: Session state persistence
    ToolDescriptor(
        name="session.save_state",
        title="Save session state",
        description=(
            "Fix 7 — Persist session context (last_sha, module_name, step_history, etc.) so a new "
            "MCP agent session after a restart can reload it with session.load_state instead "
            "of rediscovering everything from scratch."
        ),
    ),
    ToolDescriptor(
        name="session.load_state",
        title="Load saved session state",
        description="Fix 7 — Load the last persisted session state (sha, module, steps, etc.).",
        read_only=True,
    ),
    # Fix 10: Explicit ensure-dependencies
    ToolDescriptor(
        name="odoo.ensure_dependencies",
        title="Install missing dependencies",
        description=(
            "Fix 10 — Auto-install any missing/uninstalled Odoo module dependencies before "
            "installing or upgrading the target module. Call this when odoo.module_deploy fails "
            "with a dependency error. Previously the agent had to install deps manually one by one."
        ),
        requires_approval=True,
    ),
    # ---------------------------------------------------------------------------
    # V14 — Self-Learning Agent: Core File Discovery
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.find_core_file",
        title="Find Odoo core source file",
        description=(
            "V14 SELF-LEARNING: Locate an Odoo core source file by module + type + keyword. "
            "CALL THIS before any _inherit or depends addition to read the real field/method definitions. "
            "Search priority: /home/odoo/src/odoo/addons → /home/odoo/src/enterprise → /home/odoo/src/user fallback for other related custom dependencies. "
            "Do not use this to read the current custom module; read that from the active checkout with repo.read. "
            "Returns best_match path — then call repo.read(best_match) to read the actual source. "
            "Example: module_name='sale', file_type='model', keyword='order' "
            "→ /home/odoo/src/odoo/addons/sale/models/sale_order.py"
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.find_similar_view",
        title="Find similar Odoo view",
        description=(
            "V14 SELF-LEARNING: Locate an Odoo core view XML file by model + view_type. "
            "CALL THIS before writing any <xpath expr='...'> to read the exact node structure. "
            "Search priority: odoo/addons → enterprise → user fallback for other related custom dependencies. "
            "Returns best_match path — then call repo.read(best_match) to inspect real xpath/attrs. "
            "Example: model='sale.order', view_type='form' "
            "→ /home/odoo/src/enterprise/sale/views/sale_order_views.xml"
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.list_module_files",
        title="Browse module files",
        description=(
            "Browse all files in a core or custom module directory when you don't know the "
            "exact filename/keyword yet. Use subdir='models'|'views'|'static/src' to narrow. "
            "Search priority: odoo/addons → enterprise → user fallback for other related custom dependencies. "
            "Use this BEFORE find_core_file when unsure what files exist, or find_core_file "
            "directly when you already know module+type+keyword."
        ),
        read_only=True,
    ),
    # ---------------------------------------------------------------------------
    # V14+ — Pillar 2: Adaptive Scope Advisor
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.scope_advice",
        title="Get code-reading scope advice",
        description=(
            "V15 MANDATORY DUAL READ: Analyse custom module files and return the adaptive source-read plan before ANY code write, including XML/CSS/SCSS/UI edits. "
            "Call AFTER repo.read on custom files and BEFORE repo.write/repo.apply_patch. "
            "Search priority for related source is current custom module → /home/odoo/src/odoo/addons first → /home/odoo/src/enterprise fallback → /home/odoo/src/user only for other custom dependencies. "
            "Scans manifest depends, XML inherit_id/xpath/template refs, _inherit (L1), mixins (L2), computed/relational fields (L3), cross-module refs (L4). "
            "Returns ordered ScopeReadInstruction items — execute ALL mandatory instructions up to L2 before writing. "
            "Example: custom_module_name='my_sale_ext', custom_file_paths=['/path/to/models/sale_order.py', '/path/to/views/view.xml']"
        ),
        read_only=True,
    ),
    # ---------------------------------------------------------------------------
    # V14+ — Pillar 3: Pre-Push Syntax Verifier
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.verify_against_core",
        title="Verify code against core",
        description=(
            "V14+ PRE-PUSH DEFAULT: Cross-check written files against Odoo core source before git.push. "
            "CALL THIS before every git.push. If push_blocked=True, fix CRITICAL findings and call again (max 3 cycles). "
            "workflow.odoo_code_change may continue only with an explicit auditable verification_override; force mode requires the exact confirmation phrase and never bypasses blocking QA/parser/manifest errors. "
            "Verifies: Python — _inherit model existence, depends[] field names, comodel_name modules, related= chains, compute= method presence. "
            "XML — xpath @name nodes in core view, <field name=...> vs core model fields. "
            "Manifest — depends[] module names in /home/odoo/src/* roots. "
            "push_blocked=False means push is safe to proceed."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.verify_pos_change",
        title="Verify POS change (static + live browser)",
        description=(
            "V17 MANDATORY FOR POS CHANGES: Combines workflow.verify_against_core (static, incl. real node --check JS parse) "
            "with a live browser.pos_flow run (login -> open POS -> click product -> payment -> validate) under ONE bounded "
            "fix_cycle counter (max 3, same contract as verify_against_core). Only runs the live browser step if written_files "
            "look POS-related AND static checks have zero CRITICAL findings. Use this instead of verify_against_core alone "
            "whenever the task touches point_of_sale JS/XML/OWL — static checks cannot prove the POS UI actually still works."
        ),
        read_only=True,
    ),
]

_TOOL_INPUT_MODELS = {
    "connector.health": EmptyRequest,
    "skills.list": SkillListRequest,
    "skills.read": SkillReadRequest,
    "artifacts.read": ArtifactReadRequest,
    "client.check": ClientCheckRequest,
    "workflow.agent_instructions": EmptyRequest,
    "workflow.capability_check": EmptyRequest,
    "workflow.coding_task_plan": CodingTaskPlanRequest,
    "workflow.odoo_code_change": OdooCodeChangeRequest,
    "workflow.resume_continuation": ContinuationResumeRequest,
    "workflow.odoo_debug_and_fix": OdooDebugAndFixRequest,
    "workflow.module_analyze": ModuleAnalyzeRequest,
    "rea.xray_module": ReaXrayRequest,
    "rea.upstream_compare": ReaUpstreamRequest,
    "rea.api_inspect": ReaApiInspectRequest,
    "rea.runtime_trace": ReaRuntimeTraceRequest,
    "rea.impact_predict": ReaImpactRequest,
    "rea.self_heal": ReaSelfHealRequest,
    "workflow.module_test": OdooModuleTestRequest,
    "workflow.module_install_and_check": OdooModuleRequest,
    "workflow.frontend_visual_implement": FrontendVisualImplementRequest,
    "workflow.smart_tool_router": SmartToolRouteRequest,
    "workflow.repo_index": RepoIndexRequest,
    "workflow.full_verify": FullVerifyRequest,
    "workflow.safe_deploy_module": SafeDeployModuleRequest,
    "workflow.verify_deployed_module": DeployedModuleVerifyRequest,
    "workflow.start_session": StartSessionRequest,
    "workflow.registry_smoke_test": RegistrySmokeTestRequest,
    "workflow.restore_last_good_commit": RestoreLastGoodCommitRequest,
    "workflow.browser_menu_crawl": MenuCrawlerRequest,
    "workflow.frontend_pattern_preflight": FrontendPatternPreflightRequest,
    "qa.self_run": SelfRunRequest,
    "repo.prepare": RepoPrepareRequest,
    "repo.list_custom_modules": RepoCustomModulesRequest,
    "repo.list": RepoListRequest,
    "repo.read": RepoReadRequest,
    "repo.read_range": RepoReadRangeRequest,
    "repo.search": RepoSearchRequest,
    "repo.write": RepoWriteRequest,
    "repo.write_many_smart": RepoWriteManySmartRequest,
    "repo.write_many_fast_safe": RepoWriteManyFastSafeRequest,
    "repo.write_many_atomic": RepoWriteManyAtomicRequest,
    "repo.delete": RepoDeleteRequest,
    "repo.apply_patch": RepoPatchRequest,
    "repo.apply_patch_atomic": RepoAtomicPatchRequest,
    "repo.clean_pyc": RepoCleanPycRequest,
    "git.status": BaseProjectRequest,
    "git.current_branch": BaseProjectRequest,
    "git.diff": GitDiffRequest,
    "git.commit": GitCommitRequest,
    "git.push": GitPushRequest,
    "odoosh.module_install_ssh": OdooModuleRequest,
    "odoosh.query": OdooShQueryRequest,
    "odoosh.health": ClientOnlyRequest,
    "odoosh.git_status": ClientOnlyRequest,
    "odoosh.check_commit": OdooShCheckCommitRequest,
    "odoosh.wait_for_commit": OdooShWaitForCommitRequest,
    "odoosh.verify_module_manifest": OdooShVerifyManifestRequest,
    "odoosh.read_core_file": OdooShCoreFileReadRequest,
    "odoosh.list_core_files": OdooShCoreFilesListRequest,
    "odoosh.read_user_file": OdooShUserFileReadRequest,
    "odoosh.list_user_files": OdooShUserFilesListRequest,
    "odoosh.log": OdooShLogRequest,
    "odoosh.grep_errors": OdooShCommandRequest,
    "odoosh.error_pack": OdooShCommandRequest,
    "odoosh.module_deploy_ssh": OdooShModuleUpdateRequest,
    "odoosh.module_update": OdooShModuleUpdateRequest,
    "odoo.health": ClientOnlyRequest,
    "odoo.query": OdooFunctionalQueryRequest,
    "odoo.module_status": OdooModuleRequest,
    "odoo.module_deploy": OdooModuleRequest,
    "odoo.module_install": OdooModuleRequest,
    "odoo.module_update": OdooModuleRequest,
    "odoo.search_read": OdooSearchReadRequest,
    "odoo.feature_inspect": OdooFeatureInspectRequest,
    "odoo.read_group": OdooReadGroupRequest,
    "odoo.fields_get": OdooFieldsGetRequest,
    "odoo.list_models": OdooListModelsRequest,
    "odoo.record_create": OdooRecordCreateRequest,
    "odoo.record_write": OdooRecordWriteRequest,
    "odoo.record_action": OdooRecordActionRequest,
    "odoo.flow_test": OdooFlowTestRequest,
    "odoo.mutation_audit": OdooMutationAuditRequest,
    "browser.screenshot": BrowserCheckRequest,
    "browser.pos_flow": PosFlowCheckRequest,
    "approvals.list": LimitRequest,
    "approvals.request": ApprovalCreateRequest,
    "approvals.approve": ApprovalDecisionRequest,
    "jobs.list": LimitRequest,
    "jobs.get": JobGetRequest,
    "audit.list": AuditListRequest,
    # Fix 3/7/10
    "workflow.resume_job": ResumeJobRequest,
    "session.save_state": SessionStateSaveRequest,
    "session.load_state": SessionStateLoadRequest,
    "odoo.ensure_dependencies": EnsureDependenciesRequest,
    # V14: Self-Learning Agent
    "workflow.find_core_file": FindCoreFileRequest,
    "workflow.find_similar_view": FindSimilarViewRequest,
    "workflow.list_module_files": ListModuleFilesRequest,
    "workflow.scope_advice": ScopeAdviceRequest,
    "workflow.verify_against_core": SyntaxVerifyRequest,
    "workflow.verify_pos_change": PosChangeVerifyRequest,
}


@lru_cache(maxsize=None)
def _cached_tool_input_schema(internal_name: str) -> dict:
    model = _TOOL_INPUT_MODELS.get(internal_name, EmptyRequest)
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", False)
    return schema


def tool_input_schema(name: str) -> dict:
    # Return an isolated copy because provider compatibility cleanup may reshape
    # nested schema dictionaries. Pydantic generation itself is cached once.
    return deepcopy(_cached_tool_input_schema(internal_tool_name(name)))


@router.get("/list", response_model=list[ToolDescriptor])
def list_tools(include_deprecated: bool = False, strict_names: bool = True) -> list[ToolDescriptor]:
    # If global approval is disabled, do not advertise approval as required even
    # when an old ODOO_AGENT_APPROVAL_REQUIRED_ACTIONS value is still present.
    required = settings.approval_required_actions if settings.require_approval else set()

    def approval_required(name: str) -> bool:
        return name in required or any(
            rule.endswith(".*") and (
                name.startswith(rule[:-1]) or rule.startswith(name + ".")
            )
            for rule in required
        )

    visible = _TOOL_DESCRIPTORS if include_deprecated else [t for t in _TOOL_DESCRIPTORS if not t.deprecated]
    ordered = sorted(visible, key=lambda item: _agent_priority(item.name))
    result = [item.model_copy(update={"requires_approval": approval_required(item.name)}) for item in ordered]
    if strict_names:
        return [_external_tool_descriptor(item) for item in result]
    return result


@router.post("/call", response_model=ToolCallResult)
async def call_tool(payload: ToolCallRequest) -> ToolCallResult:
    external_name = payload.name
    name = internal_tool_name(payload.name)
    try:
        result = await _dispatch(name, payload.arguments)
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        return ToolCallResult(name=external_name, status="ok", result=result)
    except HTTPException as exc:
        return ToolCallResult(name=external_name, status="failed", error=str(exc.detail))
    except ValidationError as exc:
        return ToolCallResult(name=external_name, status="failed", error=format_tool_validation_error(exc))
    except Exception as exc:  # noqa: BLE001
        return ToolCallResult(name=external_name, status="failed", error=str(exc))


def format_tool_validation_error(exc: ValidationError) -> str:
    """Return concise agent-facing validation text without Pydantic internals."""
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{loc}: {message}")
    details = "; ".join(parts) if parts else str(exc)
    return f"Tool input invalid: {details}"


def _force_mcp_async_deploy_args(args: dict) -> dict:
    """For MCP providers, never block inside direct deploy/upgrade tools.

    Direct deploy tools are compatibility tools. They must start a background job
    and return job_id/next_call so the provider polls jobs.get. Final success is
    only allowed from a terminal job result.
    """
    data = dict(args or {})
    data["wait_for_result"] = False
    data["wait_timeout_seconds"] = 0
    data.setdefault("poll_interval_seconds", 2.0)
    return data



def _infer_client_name_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    for item in get_odoo_clients():
        client = normalise_client(item)
        name = str(client.get("name") or "").strip()
        if name and name.lower() in lowered:
            return name
    return None


def _odoo_service_for_client(client=None):
    if client is not None:
        try:
            return OdooService(client)
        except TypeError:
            return OdooService()
    return OdooService()


def _resolve_query_client(client_name: str | None, query_text: str | None = None):
    requested = client_name or _infer_client_name_from_text(query_text or "")
    if not requested:
        # Let resolver auto-select only when there is exactly one complete client
        # or legacy env is in use. If multiple clients exist, it raises an
        # explicit ambiguity error instead of silently using a saved/default repo.
        return ClientResolverService().resolve_client(require_complete=True)
    client = ClientResolverService().resolve_client(client_name=requested, require_complete=True)
    return client


def _rea_runtime_registry(client_name: str | None, dependencies: dict[str, Any]) -> dict[str, Any]:
    """Compare bounded static addon evidence with one explicitly selected live Odoo registry."""
    if not client_name:
        return {
            "status": "skipped",
            "reason": "Pass client_name to opt into live registry confirmation.",
            "evidence_level": "static_only",
        }
    try:
        client = _resolve_query_client(client_name)
        service = _odoo_service_for_client(client)
        static_models = {
            str(row.get("model"))
            for row in dependencies.get("model_ownership", [])
            if isinstance(row, dict) and row.get("model")
        }
        for relation in dependencies.get("cross_module_relationships", []):
            if not isinstance(relation, dict):
                continue
            for key in ("model", "source_model", "target_model"):
                if relation.get(key):
                    static_models.add(str(relation[key]))
        module_names = {str(dependencies.get("module_name") or "")}
        module_names.update(
            str(row.get("module"))
            for key in ("transitive_dependencies", "reverse_dependents")
            for row in dependencies.get(key, [])
            if isinstance(row, dict) and row.get("module")
        )
        module_names.update(
            str(row.get("source_module"))
            for row in dependencies.get("cross_module_relationships", [])
            if isinstance(row, dict) and row.get("source_module")
        )
        module_names.update(
            str(row.get("target_module"))
            for row in dependencies.get("cross_module_relationships", [])
            if isinstance(row, dict) and row.get("target_module")
        )
        static_models = sorted(static_models)[:250]
        module_names = sorted(name for name in module_names if name)[:150]

        def read_rows(model: str, domain: list, fields: list[str], limit: int) -> tuple[list[dict], str | None]:
            result = service.search_read(
                model, domain, fields, limit=limit, offset=0, order=None,
                context=None, timeout_seconds=90,
            )
            payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                error = payload.get("error") if isinstance(payload, dict) else None
                return [], str(error or f"{model} registry query failed")
            rows = payload.get("result")
            if not isinstance(rows, list):
                return [], f"{model} registry query returned an unexpected result shape"
            return rows, None

        model_rows, model_error = read_rows(
            "ir.model", [["model", "in", static_models]],
            ["model", "name", "transient", "modules"], max(1, len(static_models)),
        ) if static_models else ([], None)
        field_rows, field_error = read_rows(
            "ir.model.fields", [["model", "in", static_models]],
            ["model", "name", "ttype", "relation", "required", "store"], 3000,
        ) if static_models else ([], None)
        module_rows, module_error = read_rows(
            "ir.module.module", [["name", "in", module_names]],
            ["name", "state", "latest_version"], max(1, len(module_names)),
        ) if module_names else ([], None)
        return ReaRuntimeRegistryService().analyze(
            dependencies=dependencies,
            model_rows=model_rows,
            field_rows=field_rows,
            module_rows=module_rows,
            query_errors={
                key: value for key, value in (
                    ("ir.model", model_error),
                    ("ir.model.fields", field_error),
                    ("ir.module.module", module_error),
                ) if value
            },
            requested_model_count=len(static_models),
            requested_module_count=len(module_names),
            client_name=client.name if client else client_name,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unavailable",
            "evidence_level": "static_only",
            "client_name": client_name,
            "error": str(exc)[:500],
            "coverage": {"complete": False},
        }


def _search_read_via_shell(
    *,
    model: str,
    domain: list,
    fields: list[str],
    limit: int,
    offset: int = 0,
    order: str | None = None,
    context: dict | None = None,
    client_name: str | None = None,
    query_text: str | None = None,
    timeout_seconds: int = 120,
) -> dict:
    client = _resolve_query_client(client_name, query_text)
    shell_result = _odoo_service_for_client(client).search_read(model, domain, fields, limit, offset, order, context)
    shell_dumped = shell_result.model_dump(mode="json") if hasattr(shell_result, "model_dump") else shell_result
    if isinstance(shell_dumped, dict) and shell_dumped.get("status") == "ok":
        return {
            "status": "ok",
            "source": "odoo_ssh_or_https_jsonrpc",
            "client_name": client.name if client else None,
            "odoo": shell_dumped,
            "attempt": shell_dumped,
            "ssh_fallback_used": False,
            "message": "Odoo query completed. SSH is preferred; HTTPS JSON-RPC read-only fallback is used automatically when SSH is unavailable.",
        }

    # Preserve the original explicit Odoo.sh fallback path for compatibility and
    # tests. In normal runtime OdooService already attempted SSH + HTTPS JSON-RPC,
    # but direct tests/legacy monkeypatches may still expect this second path.
    ssh_result = OdooShService(client).search_read(model, domain, fields, limit, offset, order, context, timeout_seconds)
    ssh_dumped = ssh_result.model_dump(mode="json") if hasattr(ssh_result, "model_dump") else ssh_result
    if isinstance(ssh_dumped, dict) and ssh_dumped.get("status") == "ok":
        return {
            "status": "ok",
            "source": "odoosh_ssh_fallback",
            "client_name": client.name if client else None,
            "odoo": ssh_dumped,
            "shell_attempt": shell_dumped,
            "ssh_fallback_used": True,
            "message": "Primary Odoo query failed; Odoo.sh SSH fallback was used.",
        }
    return {
        "status": ssh_dumped.get("status") if isinstance(ssh_dumped, dict) else "failed",
        "source": "odoo_ssh_or_https_jsonrpc_failed",
        "client_name": client.name if client else None,
        "odoo": ssh_dumped,
        "shell_attempt": shell_dumped,
        "ssh_fallback_used": True,
        "message": "Odoo query failed through primary path and Odoo.sh fallback.",
    }


def _read_group_via_shell(req: OdooReadGroupRequest, query_text: str | None = None) -> dict:
    client = _resolve_query_client(req.client_name, query_text)
    result = _odoo_service_for_client(client).read_group(
        req.model, req.domain, req.fields, req.groupby, req.lazy, req.context, req.timeout_seconds
    )
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return {
        "status": dumped.get("status") if isinstance(dumped, dict) else "failed",
        "source": "odoo_ssh_or_https_jsonrpc",
        "client_name": getattr(client, "name", None),
        "odoo": dumped,
        "message": "Live Odoo read_group completed through SSH or HTTPS fallback." if isinstance(dumped, dict) and dumped.get("status") == "ok" else "Live Odoo read_group failed.",
    }


def _odoo_functional_query(req: OdooFunctionalQueryRequest) -> object:
    """Map common functional Odoo questions to safe read-only search_read.

    It tries Odoo shell first. When shell authentication fails but the selected
    client has Odoo.sh SSH settings, it automatically falls back to a read-only
    ``odoo-bin shell`` query over SSH. This lets users read business records
    without an Odoo email/password, as long as SSH access is working.
    """
    text = (req.query or "").strip().lower()
    limit = req.limit

    model = "res.partner"
    domain: list = []
    fields = ["name", "email", "phone", "active"]
    order = "id desc"
    intent = "partners"

    if any(word in text for word in ["user", "users", "login"]):
        model = "res.users"
        domain = []
        if "inactive" in text or "archived" in text:
            domain = [("active", "=", False)]
        elif "all" not in text:
            domain = [("active", "=", True)]
        fields = ["name", "login", "active", "partner_id", "company_ids"]
        order = "id asc"
        intent = "users"
    elif "employee" in text or "employees" in text or "staff" in text:
        model = "hr.employee"
        domain = []
        if "inactive" in text or "archived" in text:
            domain = [("active", "=", False)]
        elif "all" not in text:
            domain = [("active", "=", True)]
        fields = ["name", "work_email", "work_phone", "department_id", "job_id", "active"]
        order = "id asc"
        intent = "employees"
    elif "invoice" in text or "invoices" in text:
        model = "account.move"
        move_type = "in_invoice" if any(w in text for w in ["vendor", "bill", "bills", "supplier"]) else "out_invoice"
        domain = [("move_type", "=", move_type)]
        if "draft" in text:
            domain.append(("state", "=", "draft"))
        elif "posted" in text or "confirmed" in text:
            domain.append(("state", "=", "posted"))
        if "unpaid" in text or "not paid" in text or "open" in text:
            domain.append(("payment_state", "in", ["not_paid", "partial"]))
        elif "paid" in text and "unpaid" not in text and "not paid" not in text:
            domain.append(("payment_state", "=", "paid"))
        if "cancel" in text:
            domain.append(("state", "=", "cancel"))
        fields = ["name", "partner_id", "invoice_date", "invoice_date_due", "amount_total", "amount_residual", "currency_id", "state", "payment_state"]
        order = "invoice_date desc, id desc"
        intent = "vendor_bills" if move_type == "in_invoice" else "customer_invoices"
    elif any(w in text for w in ["customer", "customers", "client", "clients"]):
        model = "res.partner"
        domain = [("customer_rank", ">", 0)]
        fields = ["name", "email", "phone", "customer_rank", "active"]
        order = "customer_rank desc, name asc"
        intent = "customers"
    elif any(w in text for w in ["vendor", "vendors", "supplier", "suppliers"]):
        model = "res.partner"
        domain = [("supplier_rank", ">", 0)]
        fields = ["name", "email", "phone", "supplier_rank", "active"]
        order = "supplier_rank desc, name asc"
        intent = "vendors"
    elif "product" in text or "products" in text:
        model = "product.product"
        domain = []
        if "can be sold" in text or "sale" in text or "sell" in text:
            domain.append(("sale_ok", "=", True))
        if "can be purchased" in text or "purchase" in text or "buy" in text:
            domain.append(("purchase_ok", "=", True))
        fields = ["display_name", "default_code", "list_price", "standard_price", "qty_available", "uom_id", "active"]
        order = "id desc"
        intent = "products"
    elif "sale order" in text or "sales order" in text or "quotation" in text or "quotations" in text:
        model = "sale.order"
        domain = []
        if "draft" in text or "quotation" in text:
            domain.append(("state", "in", ["draft", "sent"]))
        elif "done" in text:
            domain.append(("state", "=", "done"))
        elif "cancel" in text:
            domain.append(("state", "=", "cancel"))
        elif "confirmed" in text or "sale" in text:
            domain.append(("state", "=", "sale"))
        fields = ["name", "partner_id", "date_order", "amount_total", "currency_id", "state", "invoice_status"]
        order = "date_order desc, id desc"
        intent = "sale_orders"
    elif "purchase order" in text or "rfq" in text or "purchase" in text:
        model = "purchase.order"
        domain = []
        if "draft" in text or "rfq" in text:
            domain.append(("state", "in", ["draft", "sent"]))
        elif "done" in text:
            domain.append(("state", "=", "done"))
        elif "cancel" in text:
            domain.append(("state", "=", "cancel"))
        elif "confirmed" in text or "purchase" in text:
            domain.append(("state", "=", "purchase"))
        fields = ["name", "partner_id", "date_order", "amount_total", "currency_id", "state", "invoice_status"]
        order = "date_order desc, id desc"
        intent = "purchase_orders"
    elif "module" in text or "modules" in text or "apps" in text:
        model = "ir.module.module"
        domain = []
        if "installed" in text:
            domain.append(("state", "=", "installed"))
        elif "uninstalled" in text:
            domain.append(("state", "=", "uninstalled"))
        fields = ["name", "shortdesc", "state", "latest_version", "installed_version"]
        order = "name asc"
        intent = "modules"

    data = _search_read_via_shell(
        model=model,
        domain=domain,
        fields=fields,
        limit=limit,
        offset=0,
        order=order,
        context=None,
        client_name=req.client_name,
        query_text=req.query,
    )
    return {
        "status": data.get("status"),
        "intent": intent,
        "source": data.get("source"),
        "client_name": data.get("client_name"),
        "parsed": {"model": model, "domain": domain, "fields": fields, "limit": limit, "order": order},
        "odoo": data.get("odoo"),
        "shell_attempt": data.get("shell_attempt"),
        "ssh_fallback_used": data.get("ssh_fallback_used"),
        "message": data.get("message"),
    }


def _resolve_code_change_project(payload: OdooCodeChangeRequest) -> tuple[str | None, str]:
    """Resolve the client/project for the provider-safe composite workflow."""
    client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path,
        require_complete=False,
    )
    project_path = payload.project_path or (client.path if client else None)
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path or client_name is required for workflow.odoo_code_change")
    resolved_client_name = client.name if client else payload.client_name
    return resolved_client_name, str(project_path)


def _safe_module_relative(
    module_name: str,
    relative_path: str,
    module_root_rel: str | None = None,
) -> str:
    """Normalize a provider path into the resolved addon root inside project_path."""
    try:
        return canonical_module_relative_path(module_name, relative_path, module_root_rel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ensure_root_module_scaffold(repo: RepoService, project: Path, payload: OdooCodeChangeRequest) -> None:
    """Create only the minimal root-level skeleton needed for first module reads."""
    if not payload.create_module:
        return
    module_name = payload.module_name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", module_name):
        raise HTTPException(status_code=400, detail="create_module requires an exact lowercase technical module_name")
    if not (project / ".git").exists():
        raise HTTPException(status_code=400, detail="create_module requires project_path to be the repository checkout root")
    if (project / "__manifest__.py").exists() or (project / "__openerp__.py").exists():
        raise HTTPException(status_code=400, detail="project_path points to an addon, not the repository checkout root")
    module_dir = (project / module_name).resolve()
    if module_dir.parent != project.resolve():
        raise HTTPException(status_code=400, detail="New modules must be direct children of the repository checkout root")
    if (module_dir / "__manifest__.py").exists():
        return

    supplied = {_safe_module_relative(module_name, item.relative_path): item.content for item in payload.writes if item.content is not None}
    skeleton = [
        RepoWriteItem(
            relative_path=f"{module_name}/__init__.py",
            content=supplied.get(f"{module_name}/__init__.py", ""),
        ),
        RepoWriteItem(
            relative_path=f"{module_name}/__manifest__.py",
            content=supplied.get(
                f"{module_name}/__manifest__.py",
                repr({"name": module_name.replace("_", " ").title(), "depends": ["base"], "installable": True}) + "\n",
            ),
        ),
    ]
    result = repo.write_many_fast_safe(str(project), skeleton)
    if result.exit_code != 0:
        raise HTTPException(status_code=400, detail=result.stderr or "Could not create root-level module scaffold")


def _read_text_safe(path: Path, limit: int = 300_000) -> str:
    data = path.read_text(encoding="utf-8")
    return data[:limit]


def _write_generation_output_schema(*, has_missing_targets: bool) -> list[dict[str, Any]]:
    """Return truthful continuation shapes for existing and missing files."""
    full_content = {
        "relative_path": "Exact missing/new path from contexts or suggested_write_targets",
        "content": "Complete production-ready file content",
    }
    search_replace = {
        "relative_path": "Exact existing path copied from a read context",
        "search": "Exact source text to replace (existing files only)",
        "replace": "Complete replacement source text",
        "regex": False,
        "count": 1,
    }
    return [full_content, search_replace] if has_missing_targets else [search_replace, full_content]


def _parse_manifest_depends(manifest_path: Path) -> list[str]:
    try:
        data = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    depends = data.get("depends", []) if isinstance(data, dict) else []
    return [str(dep) for dep in depends if isinstance(dep, str) and dep]


def _append_unique(values: list[str], raw: Any) -> None:
    value = str(raw or "").strip()
    if value and value not in values:
        values.append(value)


def _target_hint(payload: OdooCodeChangeRequest) -> str:
    return str(payload.target_hint or payload.target_text or "").strip()


def _change_request_values(value: Any, *, limit: int = 12) -> list[str]:
    """Return short string leaves from a structured change request for focused search.

    This keeps the workflow generic: the connector can inspect around method names,
    field names, XML ids, route names, labels, or visible text without hardcoding a
    task-specific prompt.
    """
    found: list[str] = []
    def visit(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, str):
            text = item.strip()
            if 2 <= len(text) <= 200 and text not in found:
                found.append(text)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
    visit(value)
    return found


_TASK_CLUE_STOPWORDS = {
    "add", "change", "create", "delete", "development", "display", "existing",
    "field", "file", "fix", "function", "method", "module", "odoo", "remove",
    "report", "template", "update", "view", "website",
}


def _task_description_search_clues(description: str, *, limit: int = 10) -> list[str]:
    """Extract concrete code clues from natural language without guessing domain terms.

    Quoted labels, CSS selectors, XML IDs, routes, and callable identifiers are
    safe exact-search inputs. Generic prose is deliberately ignored so repository
    inspection stays focused and deterministic.
    """
    text = strip_vcs_target_context(description)
    candidates: list[str] = []
    patterns = (
        r"`([^`\r\n]{2,160})`",
        r'"([^"\r\n]{2,160})"',
        r"'([^'\r\n]{2,160})'",
        r"(?<![\w-])([.#][A-Za-z_][\w-]{1,100})",
        r"\b([A-Za-z_][\w]*\.[A-Za-z_][\w.]*)\b",
        r"\b([A-Za-z_][\w]{2,})\s*\(",
        r"\b(?:xml\s*id|xmlid|selector|method|function|class|field|template|view|route)\s*[:=]?\s*[`'\"]?([/.#A-Za-z_][\w./#-]{1,159})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1).strip().rstrip(".,:;")
            if (
                len(value) >= 2
                and value.lower() not in _TASK_CLUE_STOPWORDS
                and value not in candidates
            ):
                candidates.append(value)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _infer_code_change_search_patterns(payload: OdooCodeChangeRequest) -> list[str]:
    patterns: list[str] = []
    for raw in payload.search_patterns:
        _append_unique(patterns, raw)
    _append_unique(patterns, _target_hint(payload))
    _append_unique(patterns, payload.intent)
    # Pull safe, short clues from structured change_request. This is generic and
    # works for labels, method names, fields, XML ids, routes, CSS selectors, etc.
    for raw in _change_request_values(payload.change_request):
        _append_unique(patterns, raw)
    for raw in _task_description_search_clues(payload.task_description):
        _append_unique(patterns, raw)
    return patterns[:10]


def _analysis_index_target_paths(analysis: dict[str, Any] | None, payload: OdooCodeChangeRequest) -> list[str]:
    """Rank exact custom files from the cached module-analysis evidence."""
    if not isinstance(analysis, dict):
        return []
    patterns = [item.lower() for item in _infer_code_change_search_patterns(payload) if len(item.strip()) >= 3]
    task_type = _normalized_task_type(payload)
    folder_hints: set[str] = set()
    if task_type in {"python_model", "model", "orm", "field"}:
        folder_hints.add("models")
    elif task_type in {"controller", "route", "api"}:
        folder_hints.update({"controllers", "controller"})
    elif task_type in {"xml_view", "qweb_template", "data_xml", "security_csv"}:
        folder_hints.update({"views", "security", "data", "report", "reports", "wizard", "wizards"})
    elif task_type in {"js_owl", "pos_owl", "scss_css", "style"}:
        folder_hints.add("static")

    ranked: list[tuple[float, str]] = []
    for item in analysis.get("source_evidence") or []:
        path = str(item.get("path") or "").replace("\\", "/")
        if not path:
            continue
        content = str(item.get("content") or "").lower()
        path_lower = path.lower()
        first = path_lower.split("/", 1)[0]
        score = 2.0 if first in folder_hints else 0.0
        for pattern in patterns:
            if pattern in path_lower:
                score += 4.0
            if pattern in content:
                technical = any(char in pattern for char in ("_", ".", "/", "#", "[", "]"))
                score += 3.0 if technical else 1.0
        if score > 0:
            ranked.append((score, path))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    if not ranked:
        return []
    # Keep the strong target cluster only.  Low-score matches such as a common
    # requested field label in an unrelated model must not expand a surgical
    # task back into a module-wide read.
    best_score = ranked[0][0]
    minimum_score = max(3.0, best_score * 0.55)
    return [
        path for score, path in ranked
        if score >= minimum_score
        and Path(path).name not in {"__manifest__.py", "__openerp__.py"}
    ][:4]


def _exact_model_target_cluster(
    project: Path,
    module_root_rel: str,
    payload: OdooCodeChangeRequest,
    ranked_paths: list[str],
) -> list[str]:
    """Complete and narrow an exact model task to its owning model/view files.

    Cached module evidence can rank an adjacent model above the primary form
    view when both repeat a generic business word.  When the request provides
    an exact Odoo model, resolve its Python declaration and form-view record
    directly from the selected custom addon.  This is a bounded local scan and
    never reads Core/Enterprise source.
    """
    resolution = payload.target_resolution
    model = str(getattr(resolution, "model", None) or payload.change_request.get("model") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", model):
        return ranked_paths

    module_dir = (project / module_root_rel).resolve()
    task_text = " ".join(filter(None, (
        payload.task_description,
        payload.intent,
        payload.target_hint,
        _normalized_task_type(payload),
    ))).lower()
    needs_view = any(token in task_text for token in ("view", "form", "notebook", "page", "xml"))
    model_literal = re.compile(
        rf"\b_(?:name|inherit)\s*=\s*['\"]{re.escape(model)}['\"]"
    )
    view_model = re.compile(
        rf"<field\b[^>]*\bname\s*=\s*['\"]model['\"][^>]*>\s*{re.escape(model)}\s*</field>",
        re.I,
    )

    exact: list[str] = []
    scan_specs = [("models", "*.py", model_literal)]
    if needs_view:
        scan_specs.append(("views", "*.xml", view_model))
    for folder, pattern, matcher in scan_specs:
        root = module_dir / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(pattern)):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                continue
            if matcher.search(content):
                exact.append(path.relative_to(module_dir).as_posix())
                if len(exact) >= 4:
                    break

    # A complete exact cluster is authoritative and removes adjacent-model
    # noise.  If either required side is absent, retain ranked evidence so the
    # normal resolver can diagnose rather than guessing.
    has_model = any(path.startswith("models/") for path in exact)
    has_view = any(path.startswith("views/") for path in exact)
    if has_model and (has_view or not needs_view):
        return exact
    return list(dict.fromkeys([*exact, *ranked_paths]))[:4]


def _new_model_scaffold_target_paths(
    project: Path,
    module_root_rel: str,
    payload: OdooCodeChangeRequest,
) -> list[str]:
    """Return deterministic wiring and new-file paths for an exact new model."""
    resolution = payload.target_resolution
    mode = str(getattr(resolution, "mode", "auto") or "auto")
    kind = str(getattr(resolution, "kind", "auto") or "auto")
    model = str(getattr(resolution, "model", None) or getattr(resolution, "identifier", None) or "").strip()
    if mode != "new" or kind != "model" or not re.fullmatch(
        r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", model
    ):
        return []

    module_dir = (project / module_root_rel).resolve()
    stem = model.replace(".", "_")
    menu_candidates = sorted((module_dir / "views").glob("*menu*.xml")) if (module_dir / "views").is_dir() else []
    menu_path = (
        menu_candidates[0].relative_to(project).as_posix()
        if menu_candidates
        else f"{module_root_rel.rstrip('/')}/views/{stem}_menus.xml"
    )
    return list(dict.fromkeys([
        f"{module_root_rel.rstrip('/')}/__manifest__.py",
        f"{module_root_rel.rstrip('/')}/models/__init__.py",
        f"{module_root_rel.rstrip('/')}/security/ir.model.access.csv",
        menu_path,
        f"{module_root_rel.rstrip('/')}/models/{stem}.py",
        f"{module_root_rel.rstrip('/')}/views/{stem}_views.xml",
    ]))


def _new_model_scaffold_is_self_contained(
    *,
    project: Path,
    module_root_rel: str,
    payload: OdooCodeChangeRequest,
    target_files: list[str],
) -> bool:
    """Skip installed-source fan-out for a locally closed new-model scaffold."""
    expected = _new_model_scaffold_target_paths(project, module_root_rel, payload)
    if not expected:
        return False
    normalized_targets = {
        str(path).replace("\\", "/").lstrip("./")
        for path in target_files
        if str(path).strip()
    }
    if not set(expected).issubset(normalized_targets):
        return False

    resolution = payload.target_resolution
    target_model = str(getattr(resolution, "model", None) or getattr(resolution, "identifier", None) or "")
    module_dir = (project / module_root_rel).resolve()
    owned_models: set[str] = set()
    models_dir = module_dir / "models"
    if models_dir.is_dir():
        for path in models_dir.rglob("*.py"):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                continue
            owned_models.update(re.findall(
                r"\b_name\s*=\s*['\"]([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)['\"]",
                source,
            ))

    task_text = " ".join(filter(None, (
        payload.task_description,
        payload.intent,
        payload.target_hint,
        json.dumps(payload.change_request, ensure_ascii=False, sort_keys=True, default=str),
    ))).lower()
    referenced_models = set(re.findall(
        r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b",
        task_text,
    ))
    referenced_models = {
        model for model in referenced_models
        if not model.startswith(("ir.", "base.", "fields.", "models."))
    }
    return referenced_models.issubset(owned_models | {target_model})


def _contract_candidate_paths(
    project: Path,
    module_path: str,
    payload: OdooCodeChangeRequest,
    contract: dict[str, Any],
    *,
    limit: int = 24,
) -> list[str]:
    """Find all likely implementation/test files for a persisted task contract."""
    module_dir = project / module_path
    patterns = {
        value.lower()
        for value in [
            *_infer_code_change_search_patterns(payload),
            *(contract.get("required_methods") or []),
            *(contract.get("required_symbols") or []),
        ]
        if str(value).strip()
    }
    symbol_words: set[str] = set()
    for symbol in contract.get("required_symbols") or []:
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", str(symbol))
        symbol_words.update(part.lower() for part in parts if len(part) >= 4 and part.lower() not in {"async", "response"})

    ranked: list[tuple[float, str]] = []
    for path in module_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".xml", ".csv", ".scss", ".css"}:
            continue
        try:
            path.resolve().relative_to(module_dir.resolve())
        except (OSError, ValueError):
            continue
        relative = path.relative_to(project).as_posix()
        module_relative = path.relative_to(module_dir).as_posix()
        if is_ignored_source_path(path.relative_to(module_dir).parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")[:500_000].lower()
        path_lower = module_relative.lower()
        score = 0.0
        for pattern in patterns:
            normalized = pattern.removesuffix("()")
            if normalized in path_lower:
                score += 6.0
            if normalized in content:
                score += 8.0
        for word in symbol_words:
            if word in path_lower:
                score += 3.0
            elif word in content:
                score += 1.0
        if "tests" in (contract.get("required_layers") or []) and path_lower.startswith("tests/"):
            score += 10.0
        if path.suffix.lower() == ".py" and path_lower.startswith(("models/", "services/", "controllers/")):
            score += 1.0
        if score > 0:
            ranked.append((score, relative))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    result = [path for _, path in ranked[:limit]]

    if "tests" in (contract.get("required_layers") or []):
        existing_tests = [path for path in result if f"{module_path.rstrip('/')}/tests/" in f"/{path}"]
        if not existing_tests:
            result.append(f"{module_path.rstrip('/')}/tests/test_requested_flow.py")
    return list(dict.fromkeys(result))[:limit]


def _file_preview(content: str, max_chars: int = 6000) -> str:
    if len(content) <= max_chars:
        return content
    return content[: max_chars // 2] + f"\n\n...[{len(content)-max_chars} chars omitted]...\n\n" + content[-max_chars // 2 :]


def _exact_write_context(
    project: Path,
    custom_reads: list[dict[str, Any]],
    *,
    payload: OdooCodeChangeRequest | None = None,
    max_chars: int = 10_000,
    max_files: int = 8,
) -> list[dict[str, Any]]:
    """Return exact source units across all relevant task files within budget."""
    context_patterns: list[str] = []
    if payload is not None:
        context_patterns.extend(
            pattern.strip().removesuffix("()")
            for pattern in _infer_code_change_search_patterns(payload)
            if pattern.strip()
        )
        context_patterns.extend(re.findall(
            r"\b(?:[A-Z][A-Za-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*|[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)\b",
            payload.task_description,
        ))
    context_patterns = list(dict.fromkeys(pattern for pattern in context_patterns if pattern))
    candidates = [
        item for item in custom_reads
        if item.get("status") == "read"
        and str(item.get("relative_path") or "")
        and not str(item.get("relative_path") or "").endswith("__manifest__.py")
    ]
    if not candidates:
        candidates = [item for item in custom_reads if item.get("status") == "read"]
    candidates.sort(key=lambda item: (
        0 if item.get("match_lines") else 1,
        0 if item.get("read_mode") == "match_range" else 1,
        str(item.get("relative_path") or ""),
    ))

    contexts: list[dict[str, Any]] = []
    remaining = max_chars
    for item in candidates[:max_files]:
        rel = str(item.get("relative_path") or "")
        target = project / rel
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = content.splitlines(keepends=True)
        match_lines = sorted({
            int(line) for line in item.get("match_lines") or []
            if str(line).isdigit() and int(line) > 0
        })
        if not match_lines and payload is not None:
            match_lines = [
                line_number
                for line_number, line in enumerate(lines, start=1)
                if any(pattern in line for pattern in context_patterns)
            ][:20]
        file_contexts: list[dict[str, Any]] = []

        if target.suffix == ".py" and match_lines:
            try:
                tree = ast.parse(content)
                blocks: set[tuple[int, int]] = set()
                function_ranges = [
                    (int(node.lineno), int(node.end_lineno or node.lineno))
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                for line in match_lines:
                    enclosing = [pair for pair in function_ranges if pair[0] <= line <= pair[1]]
                    if enclosing:
                        blocks.add(min(enclosing, key=lambda pair: pair[1] - pair[0]))
                for start, end in sorted(blocks):
                    source = "".join(lines[start - 1:end])
                    if len(source) > remaining:
                        continue
                    matched = [
                        pattern for pattern in context_patterns
                        if pattern and pattern in source
                    ]
                    file_contexts.append({
                            "relative_path": rel,
                            "context_type": "complete_python_function",
                            "complete_unit": True,
                            "start_line": start,
                            "end_line": end,
                            "content": source,
                            "matched_identifiers": matched,
                        })
                    remaining -= len(source)
                    if remaining < 1000:
                        break
            except SyntaxError:
                pass

        if not file_contexts and len(content) <= remaining:
            file_contexts.append({
                "relative_path": rel,
                "context_type": "complete_file",
                "complete_unit": True,
                "start_line": 1,
                "end_line": len(lines),
                "content": content,
            })
            remaining -= len(content)

        if not file_contexts and match_lines and remaining >= 1000:
            center = match_lines[0]
            start = max(1, center - 100)
            end = min(len(lines), center + 100)
            source = "".join(lines[start - 1:end])
            if len(source) > remaining:
                source = source[:remaining]
                end = start + source.count("\n")
            file_contexts.append({
                "relative_path": rel,
                "context_type": "exact_match_range",
                "complete_unit": False,
                "start_line": start,
                "end_line": end,
                "content": source,
            })
            remaining -= len(source)

        contexts.extend(file_contexts)
        if remaining < 1000:
            break
    return contexts


def _sha256_path(path: Path) -> str | None:
    """Hash one inspected file without following missing/non-file targets."""
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _workflow_task_contract(
    payload: OdooCodeChangeRequest,
    *,
    module_name: str,
    target_resolution: dict[str, Any],
    inspected_paths: list[str],
) -> dict[str, Any]:
    """Build the small immutable task identity carried into the write pass."""
    normalized_task = re.sub(r"\s+", " ", payload.task_description.strip())
    target = {
        key: target_resolution.get(key)
        for key in ("mode", "kind", "identifier", "model")
    }
    identity = {
        "version": 1,
        "module_name": module_name,
        "task_sha256": hashlib.sha256(normalized_task.encode("utf-8")).hexdigest(),
        "task_type": _normalized_task_type(payload),
        "intent": payload.intent,
        "target": target,
        "scope_mode": "module" if target.get("kind") == "module_scope" else "focused",
        "inspected_paths": sorted(dict.fromkeys(inspected_paths)),
    }
    identity["contract_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return identity


def _build_validated_write_contract(
    *,
    payload: OdooCodeChangeRequest,
    project: Path,
    module_name: str,
    module_path: str,
    manifest_path: str,
    target_resolution: dict[str, Any],
    module_contract: dict[str, Any],
    validator_findings: list[dict[str, Any]],
    custom_reads: list[dict[str, Any]],
    core_reads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the immutable server-owned handoff used by the write pass.

    Target interpretation is complete at this point. The continuation needs to
    prove that source evidence is still current, not infer the target again.
    """
    external_evidence: list[dict[str, str]] = []
    for item in core_reads:
        if item.get("status") != "read" or not item.get("path"):
            continue
        path = Path(str(item["path"])).expanduser().resolve()
        digest = _sha256_path(path)
        if digest:
            external_evidence.append({"path": str(path), "sha256": digest})

    inspected_paths = list(dict.fromkeys(
        str(item.get("relative_path") or "")
        for item in custom_reads
        if item.get("status") == "read" and item.get("relative_path")
    ))
    task_contract = _workflow_task_contract(
        payload,
        module_name=module_name,
        target_resolution=target_resolution,
        inspected_paths=inspected_paths,
    )
    raw = {
        "version": _VALIDATED_WRITE_CONTRACT_VERSION,
        "module_name": module_name,
        "module_path": module_path,
        "manifest_path": manifest_path,
        "target_resolution": target_resolution,
        "module_contract": module_contract,
        "validator_findings": validator_findings,
        "inspected_paths": inspected_paths,
        "external_evidence": external_evidence,
        "task_contract": task_contract,
    }
    signature_payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    raw["signature"] = hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()
    return raw


def _validated_session_write_contract(
    coding_session: dict[str, Any] | None,
    payload: OdooCodeChangeRequest,
    project: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a current write contract or a concrete stale-evidence reason.

    Legacy sessions deliberately fall back to the full workflow. A present but
    changed contract is stale and must never be silently reinterpreted.
    """
    if not coding_session or not payload.writes or payload.clarification_answer:
        return None, None
    evidence = coding_session.get("evidence")
    contract = dict((evidence or {}).get("write_contract") or {}) if isinstance(evidence, dict) else {}
    if not contract:
        return None, None
    signature = str(contract.pop("signature", ""))
    expected = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    contract["signature"] = signature
    if not signature or signature != expected:
        return None, "validated_write_contract_changed"
    if int(contract.get("version") or 0) != _VALIDATED_WRITE_CONTRACT_VERSION:
        return None, "validated_write_contract_version_changed"
    if str(contract.get("module_name") or "") != str(coding_session.get("module_name") or ""):
        return None, "validated_write_contract_module_changed"
    module = (project / str(contract.get("module_path") or "")).resolve()
    try:
        module.relative_to(project.resolve())
    except ValueError:
        return None, "validated_write_contract_path_escaped"
    if module != Path(str(coding_session.get("module_path") or "")).resolve() or not module.is_dir():
        return None, "validated_write_contract_module_missing"
    status_value = str(coding_session.get("status") or "")
    allowed_statuses = {"ready_to_write", "repair_required"}
    if payload.total_batches > 1:
        allowed_statuses.add("written")
    if status_value not in allowed_statuses:
        return None, f"coding_session_phase_{status_value or 'unknown'}"
    try:
        resolution = OdooTargetResolutionResult.model_validate(contract.get("target_resolution") or {})
    except ValidationError:
        return None, "validated_target_contract_invalid"
    if resolution.writes_blocked:
        return None, "validated_target_contract_blocks_writes"
    task_contract = contract.get("task_contract")
    if not isinstance(task_contract, dict):
        return None, "validated_task_contract_missing"
    expected_task_contract = _workflow_task_contract(
        payload,
        module_name=str(contract.get("module_name") or ""),
        target_resolution=resolution.model_dump(mode="json"),
        inspected_paths=list(contract.get("inspected_paths") or []),
    )
    if task_contract.get("contract_sha256") != expected_task_contract.get("contract_sha256"):
        return None, "validated_task_contract_changed"
    for item in contract.get("external_evidence") or []:
        path = Path(str(item.get("path") or "")).expanduser().resolve()
        if _sha256_path(path) != str(item.get("sha256") or ""):
            return None, f"inspected_dependency_changed:{path}"
    return contract, None


def _validate_contract_write_paths(
    *,
    project: Path,
    module_root_rel: str,
    target_resolution: OdooTargetResolutionResult,
    writes: list[OdooCodeFileChange],
) -> None:
    """Bind generated writes to the verified custom-module boundary."""
    if target_resolution.writes_blocked:
        raise HTTPException(status_code=409, detail="Validated target contract blocks writes")
    planned = {
        _safe_module_relative(module_root_rel.rsplit("/", 1)[-1], item.relative_path, module_root_rel)
        for item in writes
    }
    module = (project / module_root_rel).resolve()
    for rel in planned:
        try:
            (project / rel).resolve().relative_to(module)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"Write escaped validated module contract: {rel}") from exc


def _inline_write_contexts(
    contexts: list[dict[str, Any]],
    *,
    max_chars: int = 10_000,
) -> tuple[list[dict[str, Any]], bool]:
    """Keep complete source units inline; move overflow to an artifact."""
    selected: list[dict[str, Any]] = []
    used = 0
    for item in contexts:
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if used + size > max_chars:
            break
        selected.append(item)
        used += size
    return selected, len(selected) == len(contexts)


def _module_hint_tokens(value: str) -> set[str]:
    stop = {"module", "addon", "custom", "odoo", "the", "form"}
    return {part for part in re.split(r"[^a-z0-9]+", value.lower()) if len(part) >= 3 and part not in stop}


def _normalise_module_hint(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _resolve_fuzzy_module(payload: OdooCodeChangeRequest, modules: list[Any]) -> tuple[Any | None, dict[str, Any]]:
    """Resolve a human module hint to an actual custom module.

    Human-facing module hints may differ from technical module names.
    This function keeps path safety by selecting only from the discovered
    custom-module list.
    """
    requested = str(payload.module_name or "").strip()
    exact_matches = [item for item in modules if item.module_name == requested]
    if len(exact_matches) == 1:
        exact = exact_matches[0]
        return exact, {"requested": requested, "resolved": exact.module_name, "strategy": "exact", "score": 1.0}
    if len(exact_matches) > 1:
        return None, {
            "requested": requested,
            "strategy": "ambiguous_duplicate_module",
            "candidates": [
                {"module": item.module_name, "path": str(getattr(item, "path", "")), "score": 1.0}
                for item in exact_matches
            ],
        }

    hint_parts = [requested, payload.module_hint or "", payload.task_description or ""]
    hint_parts.extend(str(p or "") for p in payload.search_patterns)
    hint_parts.extend(str(p or "") for p in payload.target_files)
    hint_text = " ".join(part for part in hint_parts if part).strip()
    hint_norm = _normalise_module_hint(hint_text)
    hint_tokens = _module_hint_tokens(hint_text)
    candidates: list[tuple[float, Any, dict[str, Any]]] = []
    for item in modules:
        name = str(item.module_name)
        haystack = f"{name} {getattr(item, 'path', '')}"
        name_norm = _normalise_module_hint(name)
        name_tokens = _module_hint_tokens(haystack.replace("_", " "))
        score = 0.0
        reasons: list[str] = []
        if requested and _normalise_module_hint(requested) == name_norm:
            score = max(score, 0.98); reasons.append("normalized_exact")
        if hint_norm and name_norm and (name_norm in hint_norm or hint_norm in name_norm):
            score = max(score, 0.9); reasons.append("substring")
        if hint_tokens and name_tokens:
            overlap = len(hint_tokens & name_tokens)
            if overlap:
                score = max(score, 0.55 + min(overlap, 4) * 0.1); reasons.append(f"token_overlap:{overlap}")
        if requested:
            ratio = difflib.SequenceMatcher(None, _normalise_module_hint(requested), name_norm).ratio()
            if ratio >= 0.55:
                score = max(score, ratio * 0.8); reasons.append(f"fuzzy:{ratio:.2f}")
        if score:
            candidates.append((score, item, {"module": name, "score": round(score, 3), "reasons": reasons}))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if candidates:
        best_score, best, best_meta = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        if best_score >= 0.72 and (len(candidates) == 1 or best_score - second_score >= 0.08):
            return best, {"requested": requested, "resolved": best.module_name, "strategy": "fuzzy", "score": round(best_score, 3), "candidates": [meta for _, _, meta in candidates[:5]]}
    return None, {"requested": requested, "strategy": "not_found", "candidates": [meta for _, _, meta in candidates[:5]]}


def _resolve_module_from_source_evidence(
    project: Path,
    modules: list[Any],
    payload: ModuleAnalyzeRequest,
) -> tuple[Any | None, dict[str, Any], list[str]]:
    """Resolve a human feature label only from literal custom-source evidence.

    Relationship expansion is intentionally excluded here: it is valuable once
    an owner is known, but common XML/CSV relations can produce huge scores in
    unrelated addons and must never decide module ownership.
    """
    generic_patterns = {
        "addon", "custom", "data", "file", "form", "model", "module",
        "odoo", "report", "template", "view",
    }
    literal_patterns = [
        pattern
        for pattern in ModuleAnalysisService._normalise_patterns(payload.search_patterns)
        if pattern not in generic_patterns
    ]
    if not literal_patterns:
        for raw in (payload.module_hint, payload.module_name):
            value = str(raw or "").strip().strip("`\"'").casefold()
            if len(value) >= 4 and value not in literal_patterns:
                literal_patterns.append(value)
    candidates: list[tuple[int, Any, dict[str, Any]]] = []
    for item in modules[:50]:
        module_path = (project / str(item.path)).resolve()
        try:
            module_path.relative_to(project.resolve())
        except (OSError, ValueError):
            continue
        matches: list[dict[str, Any]] = []
        declared_models: set[str] = set()
        backend_patterns: set[str] = set()
        scanned_files = 0
        for path in sorted(module_path.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in ModuleAnalysisService._extensions
                or is_ignored_source_path(path.relative_to(module_path).parts)
            ):
                continue
            scanned_files += 1
            if scanned_files > 2000:
                break
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                continue
            rel = path.relative_to(module_path).as_posix()
            rel_folded = rel.casefold()
            content_folded = content.casefold()
            path_hits = [pattern for pattern in literal_patterns if pattern in rel_folded]
            content_hits = [pattern for pattern in literal_patterns if pattern in content_folded]
            matched = list(dict.fromkeys([*path_hits, *content_hits]))
            if not matched:
                continue
            relative_parts = path.relative_to(module_path).parts
            is_backend_source = (
                path.suffix.lower() == ".py"
                and bool(relative_parts)
                and relative_parts[0] in {"models", "controllers", "controller", "wizard", "wizards"}
            )
            if is_backend_source:
                backend_patterns.update(matched)
                for pattern in matched:
                    if "." not in pattern or not re.fullmatch(r"[a-z0-9_.]+", pattern):
                        continue
                    declaration = re.compile(
                        rf"\b_name\s*=\s*(['\"])\s*{re.escape(pattern)}\s*\1",
                        re.IGNORECASE,
                    )
                    if declaration.search(content):
                        declared_models.add(pattern)
            score = (len(path_hits) * 100) + (len(content_hits) * 25)
            matches.append({
                "path": f"{str(item.path).replace(chr(92), '/').strip('/')}/{rel}",
                "score": score,
                "path_hits": path_hits,
                "content_hits": content_hits,
                "matched_patterns": matched,
            })
        matches.sort(key=lambda match: (-int(match["score"]), str(match["path"])))
        if not matches:
            continue
        top_score = int(matches[0].get("score") or 0)
        total_literal_hits = sum(
            len(match.get("path_hits") or []) + len(match.get("content_hits") or [])
            for match in matches
        )
        candidates.append((top_score, item, {
            "module": item.module_name,
            "path": str(item.path),
            "score": top_score,
            "literal_hits": total_literal_hits,
            "ownership_evidence": {
                "declared_models": sorted(declared_models),
                "backend_patterns": sorted(backend_patterns),
            },
            "matches": matches[:5],
        }))
    candidates.sort(key=lambda row: (-row[0], str(row[1].module_name)))
    if not candidates:
        return None, {
            "strategy": "source_not_found",
            "reason": "No literal path or source-content match was found.",
            "literal_patterns": literal_patterns,
            "candidates": [],
        }, []
    best_score, best, best_meta = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0
    declaration_coverage = [
        (
            len((meta.get("ownership_evidence") or {}).get("declared_models") or []),
            score,
            item,
            meta,
        )
        for score, item, meta in candidates
    ]
    max_declaration_coverage = max(row[0] for row in declaration_coverage)
    owner_resolved = False
    if max_declaration_coverage:
        declaring = [row for row in declaration_coverage if row[0] == max_declaration_coverage]
        if len(declaring) != 1:
            return None, {
                "strategy": "source_ambiguous",
                "reason": "Multiple addons declare equally strong technical-model ownership.",
                "literal_patterns": literal_patterns,
                "candidates": [meta for _, _, meta in candidates[:5]],
            }, []
        _, best_score, best, best_meta = declaring[0]
        owner_resolved = True
    elif len(candidates) > 1 and best_score == second_score:
        return None, {
            "strategy": "source_ambiguous",
            "reason": "Multiple addons have equally strong literal matches without unique ownership evidence.",
            "literal_patterns": literal_patterns,
            "candidates": [meta for _, _, meta in candidates[:5]],
        }, []
    runner_up_score = max(
        (score for score, item, _ in candidates if item is not best),
        default=0,
    )
    ordered_candidate_meta = [best_meta] + [
        meta for _, item, meta in candidates if item is not best
    ]
    module_prefix = str(best.path).replace("\\", "/").strip("/") + "/"
    target_paths = []
    for match in best_meta["matches"]:
        value = str(match.get("path") or "").replace("\\", "/").strip("/")
        if value.startswith(module_prefix):
            value = value[len(module_prefix):]
        if value:
            target_paths.append(value)
    return best, {
        "requested": payload.module_name,
        "resolved": best.module_name,
        "strategy": (
            "literal_source_owner_evidence"
            if owner_resolved
            else "literal_source_evidence"
        ),
        "score": best_score,
        "score_margin": best_score - runner_up_score,
        "literal_patterns": literal_patterns,
        "candidates": ordered_candidate_meta[:5],
    }, target_paths


def _style_value(style: dict[str, Any], *names: str) -> str | None:
    for name in names:
        if name in style and style[name] not in {None, ""}:
            return str(style[name]).strip()
    return None


def _css_style_declarations(style: dict[str, Any]) -> list[str]:
    declarations: list[str] = []
    color = _style_value(style, "color")
    font_style = _style_value(style, "font_style", "fontStyle")
    font_weight = _style_value(style, "font_weight", "fontWeight")
    if color:
        declarations.append(f"color: {color} !important;")
    if font_style:
        declarations.append(f"font-style: {font_style} !important;")
    if font_weight:
        weight = "700" if font_weight.lower() == "bold" else font_weight
        declarations.append(f"font-weight: {weight} !important;")
    return declarations


def _focused_search(
    repo: RepoService,
    project_path: str,
    module_path: str,
    patterns: list[str],
    max_matches: int,
    task_type: str = "auto",
) -> tuple[list[dict[str, Any]], list[str], dict[str, list[int]]]:
    """Exact-first, low-noise search for inspect/autopatch mode."""
    preferred_suffixes = (".xml", ".scss", ".css", ".js", ".py")
    task = str(task_type or "auto").lower()
    primary_suffixes = {
        "python_model": (".py",),
        "controller": (".py",),
        "xml_view": (".xml",),
        "qweb_template": (".xml",),
        "report": (".xml", ".py"),
        "js_owl": (".js", ".xml"),
        "pos_owl": (".js", ".xml"),
        "js_report": (".js", ".xml"),
        "scss_css": (".scss", ".css"),
        "style": (".scss", ".css", ".xml"),
    }.get(task, preferred_suffixes)

    def suffix_rank(raw_path: str) -> int:
        lowered = raw_path.lower()
        for index, suffix in enumerate(primary_suffixes):
            if lowered.endswith(suffix):
                return index
        return len(primary_suffixes) + (0 if lowered.endswith(preferred_suffixes) else 1)

    search_results: list[dict[str, Any]] = []
    rel_paths: list[str] = []
    seen_paths: set[str] = set()
    match_lines: dict[str, list[int]] = {}
    for pattern in patterns[:10]:
        try:
            result = repo.search(project_path, pattern, module_path, max(1, min(max_matches, 25)), 24, False)
            matches = result.get("matches", []) or []
            # Rank the owning source type first; XML references must not outrank
            # Python declarations for model tasks (and vice versa for view work).
            matches = sorted(
                matches,
                key=lambda m: (
                    suffix_rank(str(m.get("path", ""))),
                    1 if task != "controller" and "/controllers/" in str(m.get("path", "")) else 0,
                    str(m.get("path", "")),
                    int(m.get("line") or 0),
                ),
            )[: max(1, min(max_matches, 10))]
            search_results.append({
                "pattern": pattern,
                "count": result.get("count"),
                "focused_count": len(matches),
                "matches": matches,
            })
            for match in matches:
                rel = str(match.get("path") or "")
                if rel and rel.endswith(preferred_suffixes) and rel not in seen_paths:
                    seen_paths.add(rel)
                    rel_paths.append(rel)
                if rel and rel.endswith(preferred_suffixes):
                    line = int(match.get("line") or 0)
                    if line > 0 and line not in match_lines.setdefault(rel, []):
                        match_lines[rel].append(line)
            # Stop only after an exact match in the task's primary source type.
            if any(
                str(m.get("path", "")).lower().endswith(primary_suffixes)
                and pattern.lower() in str(m.get("text", "")).lower()
                for m in matches
            ):
                break
        except Exception as exc:  # noqa: BLE001
            search_results.append({"pattern": pattern, "status": "failed", "error": str(exc)})
    return search_results, rel_paths, match_lines


def _normalized_task_type(payload: OdooCodeChangeRequest) -> str:
    task_type = str(payload.task_type or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    return task_type or "auto"


def _normalized_patch_strategy(payload: OdooCodeChangeRequest) -> str:
    strategy = str(payload.patch_strategy or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    return strategy or "auto"


def _style_request(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Merge backward-compatible style with generic change_request style fields."""
    merged: dict[str, Any] = {}
    cr = payload.change_request or {}
    if isinstance(cr.get("style"), dict):
        merged.update(cr["style"])
    for key in ("color", "font_style", "fontStyle", "font_weight", "fontWeight", "background", "background_color"):
        if key in cr and cr[key] not in {None, ""}:
            merged[key] = cr[key]
    merged.update(payload.style or {})
    return merged


def _text_replace_request(payload: OdooCodeChangeRequest) -> tuple[str | None, str | None]:
    """Extract generic text replacement instructions from change_request.

    This supports many task phrasings without becoming task-specific:
      {type: text_replace, old: X, new: Y}
      {search: X, replace: Y}
      {target: X, value: Y}
    """
    cr = payload.change_request or {}
    if not isinstance(cr, dict):
        return None, None
    old = cr.get("old") or cr.get("old_text") or cr.get("from") or cr.get("search") or cr.get("target") or cr.get("target_text")
    new = cr.get("new") or cr.get("new_text") or cr.get("to") or cr.get("replace") or cr.get("replacement") or cr.get("value")
    if old is None and _normalized_task_type(payload) in {"text_replace", "replace_text"}:
        old = _target_hint(payload)
    if old is None or new is None:
        return None, None
    return str(old), str(new)


def _task_description_text_replace(description: str) -> tuple[str | None, str | None]:
    """Parse only explicit, quoted natural-language replacement instructions."""
    text = str(description or "").strip()
    patterns = (
        r"\b(?:replace|change|rename)\s+[`\"']([^`\"'\r\n]{1,300})[`\"']\s+(?:with|to)\s+[`\"']([^`\"'\r\n]{1,300})[`\"']",
        r"[`\"']([^`\"'\r\n]{1,300})[`\"']\s*->\s*[`\"']([^`\"'\r\n]{1,300})[`\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1) != match.group(2):
            return match.group(1), match.group(2)
    return None, None


def _iter_autopatch_candidate_files(module_dir: Path, target_files: list[str], project: Path) -> list[Path]:
    suffix_order = {".xml": 0, ".scss": 1, ".css": 2, ".js": 3, ".py": 4, ".csv": 5}
    candidates: list[Path] = []
    seen: set[Path] = set()
    for rel in target_files or []:
        path = (project / rel).resolve()
        try:
            path.relative_to(module_dir.resolve())
        except ValueError:
            continue
        if path.is_file() and path.suffix in suffix_order and path not in seen:
            seen.add(path); candidates.append(path)
    if candidates:
        return candidates
    for path in module_dir.rglob("*"):
        if not path.is_file() or path.suffix not in suffix_order:
            continue
        if any(part in {".git", "__pycache__", "node_modules"} for part in path.parts):
            continue
        if path not in seen:
            seen.add(path); candidates.append(path)
    return sorted(candidates, key=lambda p: (suffix_order.get(p.suffix, 99), len(p.as_posix()), p.as_posix()))


def _build_text_replace_autopatch(
    project: Path,
    module_path: str,
    target_files: list[str],
    old: str,
    new: str,
    *,
    require_unique: bool = False,
) -> list[OdooCodeFileChange]:
    if not old or new is None:
        return []
    module_dir = project / module_path
    matches: list[tuple[Path, int]] = []
    for file_path in _iter_autopatch_candidate_files(module_dir, target_files, project):
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        count = content.count(old)
        if count <= 0:
            continue
        matches.append((file_path, count))
        if not require_unique:
            break
        if sum(item[1] for item in matches) > 1:
            return []
    if not matches or (require_unique and sum(item[1] for item in matches) != 1):
        return []
    file_path = matches[0][0]
    rel = file_path.relative_to(project).as_posix()
    # Use search/replace rather than full content rewrite so the diff remains
    # minimal and deterministic across all text-like task types.
    return [OdooCodeFileChange(relative_path=rel, search=old, replace=new, count=1)]


def _build_safe_generic_autopatch(project: Path, module_path: str, payload: OdooCodeChangeRequest, custom_rel_paths: list[str]) -> tuple[list[OdooCodeFileChange], str | None, str | None]:
    """Build a deterministic autopatch for supported safe cases.

    This is intentionally generic and conservative.  It handles only changes that
    can be generated without hallucinating Odoo code (style rule or exact text
    replacement).  Python/XML/JS/POS/security/report tasks still use the same
    universal workflow, but return focused inspection context and request explicit
    writes when no deterministic patch exists.
    """
    task_type = _normalized_task_type(payload)
    strategy = _normalized_patch_strategy(payload)
    target = _target_hint(payload)
    style = _style_request(payload)
    cr_type = str((payload.change_request or {}).get("type") or "").strip().lower().replace("-", "_").replace(" ", "_")

    inferred_old, inferred_new = _task_description_text_replace(payload.task_description)
    if inferred_old is not None and inferred_new is not None:
        writes = _build_text_replace_autopatch(
            project,
            module_path,
            [],
            inferred_old,
            inferred_new,
            require_unique=True,
        )
        if writes:
            return writes, "task_description_text_replace", None
        return [], "task_description_text_replace", "replacement_not_unique_or_not_found"

    if not payload.allow_autopatch and not payload.style and not payload.change_request:
        return [], None, "autopatch_not_requested"

    if style and target and (task_type in {"auto", "style", "scss_css", "css", "qweb_template", "xml_view"} or strategy in {"auto", "safe_autopatch", "style_rule", "inline_style"} or cr_type in {"style", "change_style", "update_style"}):
        writes = _build_simple_style_autopatch(project, module_path, target, style)
        if writes:
            return writes, "style_rule", None
        return [], "style_rule", "target_text_or_class_not_found"

    old, new = _text_replace_request(payload)
    if old is not None and new is not None and (task_type in {"auto", "text", "text_replace", "replace_text", "qweb_template", "xml_view", "data_xml", "scss_css", "js_owl", "pos_owl", "python_model"} or strategy in {"auto", "safe_autopatch", "text_replace"} or cr_type in {"text_replace", "replace_text", "rename_label", "change_text"}):
        writes = _build_text_replace_autopatch(project, module_path, custom_rel_paths, old, new)
        if writes:
            return writes, "text_replace", None
        return [], "text_replace", "search_text_not_found"

    return [], None, "unsupported_autopatch_for_task_type"


def _build_simple_style_autopatch(project: Path, module_path: str, target_text: str, style: dict[str, Any]) -> list[OdooCodeFileChange]:
    declarations = _css_style_declarations(style)
    if not target_text or not declarations:
        return []
    module_dir = project / module_path
    # Prefer existing class in the template, then add/update a small frontend CSS rule.
    for xml_file in sorted(module_dir.rglob("*.xml")):
        try:
            text = xml_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if target_text not in text:
            continue
        class_match = re.search(r'<([a-zA-Z0-9:_-]+)([^>]*class=["\']([^"\']+)["\'][^>]*)>\s*' + re.escape(target_text), text, re.S)
        class_name = None
        if class_match:
            classes = [part for part in class_match.group(3).split() if part]
            class_name = next((cls for cls in classes if cls.startswith("o_") or cls.startswith("s_")), classes[0] if classes else None)
        if class_name:
            scss_files = sorted((module_dir / "static" / "src" / "scss").glob("*.scss"))
            css_files = sorted((module_dir / "static" / "src" / "css").glob("*.css"))
            for style_file in [*scss_files, *css_files]:
                try:
                    old_css = style_file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                rule_re = re.compile(rf"\.{re.escape(class_name)}\s*\{{(?P<body>.*?)\}}", re.S)
                declaration_block = "\n".join(f"    {line}" for line in declarations)
                if rule_re.search(old_css):
                    new_css = rule_re.sub(lambda m: f".{class_name} {{\n{declaration_block}\n}}", old_css, count=1)
                else:
                    new_css = old_css.rstrip() + f"\n\n.{class_name} {{\n{declaration_block}\n}}\n"
                rel = style_file.relative_to(project).as_posix()
                return [OdooCodeFileChange(relative_path=rel, content=new_css)]
        # Fallback: inline style on the matching element.
        inline = " ".join(line.replace(" !important", "") for line in declarations)
        new_text, count = re.subn(
            r'(<[^>]+>\s*)' + re.escape(target_text),
            lambda m: m.group(1).replace(">", f' style="{inline}">', 1) + target_text if " style=" not in m.group(1) else m.group(0),
            text,
            count=1,
        )
        if count:
            rel = xml_file.relative_to(project).as_posix()
            return [OdooCodeFileChange(relative_path=rel, content=new_text)]
    return []





_TASK_PROFILES = {
    "python_model": {"frontend": False, "live_verifier": "registry_smoke_test", "scope": "model"},
    "controller": {"frontend": False, "live_verifier": "endpoint_smoke", "scope": "controller"},
    "xml_view": {"frontend": False, "live_verifier": "backend_view_open", "scope": "view"},
    "qweb_template": {"frontend": True, "live_verifier": "report_or_page_render", "scope": "qweb"},
    "report": {"frontend": False, "live_verifier": "report_render", "scope": "report"},
    "js_report": {"frontend": True, "live_verifier": "report_ui_smoke", "scope": "frontend"},
    "js_owl": {"frontend": True, "live_verifier": "owl_ui_smoke", "scope": "frontend"},
    "pos_owl": {"frontend": True, "live_verifier": "pos_flow", "scope": "pos"},
    "scss_css": {"frontend": True, "live_verifier": "page_render", "scope": "style"},
    "manifest": {"frontend": False, "live_verifier": "module_upgrade", "scope": "manifest"},
    "data_xml": {"frontend": False, "live_verifier": "module_upgrade", "scope": "data"},
    "init": {"frontend": False, "live_verifier": "module_import", "scope": "init"},
    "security_csv": {"frontend": False, "live_verifier": "access_smoke", "scope": "security"},
    "cron": {"frontend": False, "live_verifier": "method_smoke", "scope": "backend"},
    "payment": {"frontend": False, "live_verifier": "payment_route_smoke", "scope": "payment"},
    "payment_frontend": {"frontend": True, "live_verifier": "payment_ui_smoke", "scope": "payment"},
}

def _task_profile(payload: OdooCodeChangeRequest, rel_paths: list[str] | None = None) -> dict[str, Any]:
    task_type = _normalized_task_type(payload)
    profile = dict(_TASK_PROFILES.get(task_type, {"frontend": False, "live_verifier": "targeted_smoke", "scope": "auto"}))
    paths = [str(p).lower().replace("\\", "/") for p in (rel_paths or [])]

    def is_pos_path(path: str) -> bool:
        segments = [segment for segment in path.split("/") if segment]
        return (
            "point_of_sale" in segments
            or "_assets_pos" in path
            or any(segment in {"pos", "pos_store"} for segment in segments)
        )

    def is_frontend_xml(path: str) -> bool:
        return path.endswith(".xml") and (
            "/static/src/" in path
            or "/static/" in path
            or "/qweb/" in path
            or "/templates/" in path
        )

    if any(is_pos_path(path) for path in paths):
        profile.update({"frontend": True, "live_verifier": "pos_flow", "scope": "pos"})
    elif any(path.endswith((".js", ".scss", ".css")) or is_frontend_xml(path) for path in paths):
        profile["frontend"] = True
    return profile


def _adaptive_workflow_profile(
    payload: OdooCodeChangeRequest,
    module_contract: dict[str, Any],
    rel_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Choose bounded inspection/write behavior from task scope, not agent guesses."""
    paths = list(dict.fromkeys(str(path) for path in (rel_paths or []) if str(path).strip()))
    required_layers = list(dict.fromkeys(module_contract.get("required_layers") or []))
    required_units = len(module_contract.get("required_methods") or []) + len(module_contract.get("required_symbols") or [])
    known_file_count = max(
        len(payload.writes) * payload.total_batches,
        len(payload.target_files),
        len(paths),
    )
    full_module = bool(payload.create_module or module_contract.get("full_module_requested"))
    if payload.total_batches > 1 or full_module or known_file_count > 15 or len(required_layers) >= 6 or required_units > 15:
        tier = "large"
    elif known_file_count > 5 or len(required_layers) >= 3 or required_units > 6:
        tier = "medium"
    else:
        tier = "small"
    settings_by_tier = {
        "small": {"file_budget": 8, "inspection_file_limit": 12, "local_batch_size": 8},
        "medium": {"file_budget": 20, "inspection_file_limit": 30, "local_batch_size": 10},
        "large": {"file_budget": 50, "inspection_file_limit": 50, "local_batch_size": 15},
    }
    profile = settings_by_tier[tier]
    return {
        "tier": tier,
        **profile,
        "known_file_count": known_file_count,
        "required_layer_count": len(required_layers),
        "required_unit_count": required_units,
        "full_module": full_module,
        "multi_batch": payload.total_batches > 1,
        "deployment_policy": "one_commit_one_deploy_after_contract_closure",
        "batch_policy": (
            "single_complete_write_set"
            if tier == "small"
            else "dependency_ordered_local_batches_no_partial_deploy"
        ),
    }


def _material_clarification(payload: OdooCodeChangeRequest) -> dict[str, Any] | None:
    """Detect explicit unresolved product choices; technical uncertainty stays autonomous."""
    if str(payload.clarification_answer or "").strip():
        return None
    text = " ".join(
        str(value or "")
        for value in (payload.task_description, payload.intent, payload.target_hint)
    ).strip()
    lowered = text.lower()
    ambiguity_markers = (
        "either ", " or maybe ", "whichever ", "any suitable ", "as appropriate",
        "as needed", "some users", "certain users", "whatever is best",
        "you decide", "not sure", "koi bhi", "jo behtar",
    )
    material_markers = (
        "permission", "security", "access", "group", "user", "company", "record rule",
        "delete", "archive", "migrate", "default", "workflow", "state", "approval",
        "visible", "button", "field", "view", "portal", "website",
    )
    if not any(marker in lowered for marker in ambiguity_markers):
        return None
    if not any(marker in lowered for marker in material_markers):
        return None
    return {
        "required": True,
        "reason": "A material user-visible, security, data, or workflow choice remains explicitly unresolved.",
        "question": (
            "Please confirm the exact desired behavior/actor/default for the unresolved choice before code is changed."
        ),
        "source_excerpt": text[:500],
        "writes_blocked": True,
    }


def _target_edit_policy_rule(strategy: str) -> str:
    if strategy == "direct_edit":
        return (
            "Edit the existing client-owned custom source directly; do not add a duplicate "
            "_inherit, inherited view, or patch layer."
        )
    if strategy == "inherit_extension":
        return (
            "Do not modify protected source. Implement the change in the selected custom addon "
            "using the version-appropriate Odoo extension mechanism."
        )
    return "Do not guess target ownership; resolve the exact source owner before generating writes."


def _safe_unique_python_function_rebase(
    current: str,
    search: str,
    replace: str,
    *,
    minimum_similarity: float = 0.72,
) -> tuple[str | None, str]:
    """Rebase a stale complete-function replacement only when its target is unique."""
    try:
        search_tree = ast.parse(textwrap.dedent(search))
        replace_tree = ast.parse(textwrap.dedent(replace))
        current_tree = ast.parse(current)
    except SyntaxError:
        return None, "search, replacement, or current Python source is not syntactically complete"

    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    if (
        len(search_tree.body) != 1
        or len(replace_tree.body) != 1
        or not isinstance(search_tree.body[0], function_types)
        or not isinstance(replace_tree.body[0], function_types)
    ):
        return None, "search and replacement must each contain exactly one complete Python function"

    search_node = search_tree.body[0]
    replace_node = replace_tree.body[0]
    if type(search_node) is not type(replace_node) or search_node.name != replace_node.name:
        return None, "search and replacement function identities differ"

    candidates = [
        node for node in ast.walk(current_tree)
        if isinstance(node, function_types) and node.name == search_node.name
    ]
    if len(candidates) != 1:
        return None, f"function {search_node.name!r} has {len(candidates)} matches in the current file"
    current_node = candidates[0]
    if type(current_node) is not type(search_node):
        return None, "current function sync/async type differs from the supplied search"
    if ast.dump(current_node.args, include_attributes=False) != ast.dump(search_node.args, include_attributes=False):
        return None, "current function signature differs from the supplied search"

    current_lines = current.splitlines(keepends=True)
    start = int(current_node.lineno) - 1
    end = int(current_node.end_lineno or current_node.lineno)
    current_unit = "".join(current_lines[start:end])
    normalized_current = " ".join(textwrap.dedent(current_unit).split())
    normalized_search = " ".join(textwrap.dedent(search).split())
    similarity = difflib.SequenceMatcher(None, normalized_search, normalized_current).ratio()
    if similarity < minimum_similarity:
        return None, (
            f"current function similarity {similarity:.2f} is below the safe "
            f"{minimum_similarity:.2f} threshold"
        )

    indentation = re.match(r"[ \t]*", current_lines[start]).group(0)
    replacement_lines = textwrap.dedent(replace).strip("\r\n").splitlines()
    replacement_unit = "\n".join(
        f"{indentation}{line}" if line else ""
        for line in replacement_lines
    )
    if current_unit.endswith(("\n", "\r")):
        replacement_unit += "\n"
    return "".join([*current_lines[:start], replacement_unit, *current_lines[end:]]), (
        f"rebased unique function {search_node.name!r} at similarity {similarity:.2f}"
    )


def _code_batch_fingerprint(writes: list[OdooCodeFileChange]) -> str:
    payload = [item.model_dump(mode="json") for item in writes]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _code_operation_id(
    payload: OdooCodeChangeRequest,
    *,
    client_name: str | None,
    project_path: str,
    module_name: str,
) -> str:
    if payload.operation_id:
        return str(payload.operation_id)
    normalized = "|".join((
        str(client_name or ""),
        str(Path(project_path).resolve()),
        module_name,
        _normalized_task_type(payload),
        payload.intent or "",
        re.sub(r"\s+", " ", payload.task_description.strip().lower()),
    ))
    return "codeop-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _persist_operation_handoff(
    store: JsonStore,
    handoff: OdooOperationHandoff,
    *,
    client_name: str | None,
) -> dict[str, Any]:
    """Persist recovery metadata without making it a core-workflow dependency."""
    if not hasattr(store, "save_operation_handoff"):
        return handoff.model_copy(
            update={"persistence_status": "unavailable"}
        ).model_dump(mode="json")
    try:
        return store.save_operation_handoff(handoff, client_name=client_name)
    except Exception as exc:
        return handoff.model_copy(
            update={
                "persistence_status": "failed",
                "persistence_error_type": type(exc).__name__,
            }
        ).model_dump(mode="json")


def _save_code_operation_handoff(
    store: JsonStore,
    *,
    payload: OdooCodeChangeRequest,
    client_name: str | None,
    project_path: str,
    module_name: str,
    target_files: list[str],
    status: str,
    current_blocker: str | None,
    next_action: str,
    completed_checkpoints: list[str] | None = None,
    acceptance_evidence_remaining: list[str] | None = None,
) -> dict[str, Any]:
    """Persist only multi-phase continuation state, never source/log payloads."""
    handoff = OdooOperationHandoff(
        operation_id=_code_operation_id(
            payload,
            client_name=client_name,
            project_path=project_path,
            module_name=module_name,
        ),
        client_name=str(client_name)[:200] if client_name else None,
        project_path=str(Path(project_path).resolve())[:2000],
        module_name=module_name[:200],
        objective=payload.task_description[:5000],
        locked_scope=[
            f"module:{module_name}",
            f"task_type:{_normalized_task_type(payload)}",
            f"intent:{payload.intent or 'unspecified'}",
        ],
        decisions_with_reasons=[
            "Use the existing server-owned code-change, QA, Git, deploy, and upgrade pipeline.",
            "Preserve installed-source ownership and the resolved edit policy.",
        ],
        target_files=[
            path[:500]
            for path in dict.fromkeys(str(path) for path in target_files if str(path))
        ][:100],
        completed_checkpoints=list(dict.fromkeys(completed_checkpoints or []))[:100],
        current_blocker=str(current_blocker)[:3000] if current_blocker else None,
        next_action=next_action[:3000],
        acceptance_evidence_remaining=list(dict.fromkeys(
            acceptance_evidence_remaining or []
        ))[:50],
        diagnosis=payload.investigation_diagnosis,
        status=typing.cast(Any, status),
    )
    return _persist_operation_handoff(store, handoff, client_name=client_name)


def _prepare_code_batch(
    store: JsonStore,
    *,
    client_name: str | None,
    project_path: str,
    module_name: str,
    payload: OdooCodeChangeRequest,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Validate ordered/idempotent local batches without permitting partial deploy."""
    if payload.total_batches <= 1:
        return None, None, False
    operation_id = str(payload.operation_id)
    fingerprint = _code_batch_fingerprint(payload.writes)
    state = store.load_session_state(client_name)
    operations = dict(state.get("code_change_batch_operations") or {})
    record = dict(operations.get(operation_id) or {})
    if record:
        if (
            record.get("project_path") != str(Path(project_path).resolve())
            or record.get("module_name") != module_name
            or int(record.get("total_batches") or 0) != payload.total_batches
        ):
            raise HTTPException(status_code=409, detail="operation_id is already bound to a different project/module/batch contract")
    else:
        record = {
            "operation_id": operation_id,
            "project_path": str(Path(project_path).resolve()),
            "module_name": module_name,
            "total_batches": payload.total_batches,
            "completed": {},
            "staged_paths": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    completed = dict(record.get("completed") or {})
    existing = completed.get(str(payload.batch_index))
    if existing is not None:
        if existing != fingerprint:
            raise HTTPException(status_code=409, detail="completed batch cannot be replaced with different writes")
        return record, fingerprint, True
    expected_index = next(
        (index for index in range(payload.total_batches) if str(index) not in completed),
        payload.total_batches,
    )
    if payload.batch_index != expected_index:
        raise HTTPException(
            status_code=409,
            detail=f"Out-of-order batch: expected batch_index={expected_index}, received {payload.batch_index}",
        )
    operations[operation_id] = record
    if len(operations) > 20:
        operations = dict(list(operations.items())[-20:])
    store.save_session_state({"code_change_batch_operations": operations}, client_name=client_name)
    _save_code_operation_handoff(
        store,
        payload=payload,
        client_name=client_name,
        project_path=project_path,
        module_name=module_name,
        target_files=[item.relative_path for item in payload.writes],
        status="active",
        current_blocker=None,
        next_action=f"Apply and verify local batch {payload.batch_index + 1} of {payload.total_batches}.",
        completed_checkpoints=["client_validated", "batch_contract_bound"],
        acceptance_evidence_remaining=[
            "all_local_batches_verified",
            "whole_module_qa",
            "terminal_deployment_proof",
        ],
    )
    return record, fingerprint, False


def _complete_code_batch(
    store: JsonStore,
    *,
    client_name: str | None,
    record: dict[str, Any],
    batch_index: int,
    fingerprint: str,
    written_paths: list[str],
) -> dict[str, Any]:
    state = store.load_session_state(client_name)
    operations = dict(state.get("code_change_batch_operations") or {})
    current = dict(operations.get(record["operation_id"]) or record)
    completed = dict(current.get("completed") or {})
    completed[str(batch_index)] = fingerprint
    current["completed"] = completed
    current["staged_paths"] = list(dict.fromkeys([
        *(current.get("staged_paths") or []),
        *written_paths,
    ]))[-1000:]
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    operations[record["operation_id"]] = current
    store.save_session_state({"code_change_batch_operations": operations}, client_name=client_name)
    try:
        if not (
            hasattr(store, "get_operation_handoff")
            and hasattr(store, "save_operation_handoff")
        ):
            return current
        handoff = OdooOperationHandoff.model_validate(
            store.get_operation_handoff(record["operation_id"], client_name=client_name)
        )
        checkpoint = f"batch_{batch_index}_verified"
        if checkpoint not in handoff.completed_checkpoints:
            handoff.completed_checkpoints.append(checkpoint)
        handoff.target_files = list(dict.fromkeys([
            *handoff.target_files,
            *written_paths,
        ]))[-100:]
        handoff.next_action = "Continue with the next dependency-ordered local batch."
        store.save_operation_handoff(handoff, client_name=client_name)
    except Exception:
        # Batch completion remains authoritative; handoff is only a sidecar.
        pass
    return current


def _record_code_batch_deploy(
    store: JsonStore,
    *,
    client_name: str | None,
    record: dict[str, Any],
    job_id: str | None,
) -> dict[str, Any]:
    """Persist the server-owned deploy checkpoint so a replay cannot deploy twice."""
    state = store.load_session_state(client_name)
    operations = dict(state.get("code_change_batch_operations") or {})
    current = dict(operations.get(record["operation_id"]) or record)
    if job_id:
        current["deploy_job_id"] = job_id
        current["deploy_started_at"] = datetime.now(timezone.utc).isoformat()
    operations[record["operation_id"]] = current
    store.save_session_state({"code_change_batch_operations": operations}, client_name=client_name)
    try:
        if not (
            hasattr(store, "get_operation_handoff")
            and hasattr(store, "save_operation_handoff")
        ):
            return current
        handoff = OdooOperationHandoff.model_validate(
            store.get_operation_handoff(record["operation_id"], client_name=client_name)
        )
        handoff.completed_checkpoints = list(dict.fromkeys([
            *handoff.completed_checkpoints,
            "all_local_batches_verified",
            "whole_module_qa_passed",
            "safe_deploy_started",
        ]))
        handoff.current_blocker = None
        handoff.next_action = f"Poll the owned safe-deploy job {job_id} to a terminal result."
        handoff.acceptance_evidence_remaining = ["terminal_deployment_proof"]
        store.save_operation_handoff(handoff, client_name=client_name)
    except Exception:
        # Deployment ownership remains with the job even if sidecar sync fails.
        pass
    return current


def _core_relevance_terms(payload: OdooCodeChangeRequest, rel_paths: list[str]) -> list[str]:
    values = [*_infer_code_change_search_patterns(payload), *rel_paths]
    terms: list[str] = []
    for value in values:
        for token in re.split(r"[^a-zA-Z0-9_]+", str(value).lower()):
            if len(token) >= 3 and token not in _TASK_CLUE_STOPWORDS and token not in terms:
                terms.append(token)
    return terms[:30]


def _core_path_relevance(path: Path, terms: list[str], suffixes: set[str], task_type: str, original_index: int = 0) -> tuple[int, int, str]:
    text = path.as_posix().lower()
    name = path.name.lower()
    stem = path.stem.lower()
    score = 0
    for term in terms:
        if term == stem or term == name:
            score += 8
        elif term in name:
            score += 5
        elif term in text:
            score += 2
    if path.suffix.lower() in suffixes:
        score += 4
    area_hints = {
        "python_model": "/models/",
        "controller": "/controllers/",
        "xml_view": "/views/",
        "qweb_template": "/views/",
        "report": "/report/",
        "js_report": "/static/",
        "js_owl": "/static/",
        "pos_owl": "/static/",
        "scss_css": "/static/",
    }
    if area_hints.get(task_type) and area_hints[task_type] in text:
        score += 5
    return (-score, original_index, text)


def _rank_core_required_paths(paths: list[str], payload: OdooCodeChangeRequest, rel_paths: list[str], limit: int) -> list[str]:
    unique = list(dict.fromkeys(str(path) for path in paths if str(path).strip()))
    terms = _core_relevance_terms(payload, rel_paths)
    suffixes = {Path(path).suffix.lower() for path in rel_paths if Path(path).suffix}
    ranked = sorted(
        enumerate(unique),
        key=lambda item: _core_path_relevance(Path(item[1]), terms, suffixes, _normalized_task_type(payload), item[0]),
    )
    return [path for _, path in ranked[: max(1, min(limit, 10))]]


def _rank_core_directory_files(directory: Path, payload: OdooCodeChangeRequest, rel_paths: list[str]) -> list[Path]:
    terms = _core_relevance_terms(payload, rel_paths)
    suffixes = {Path(path).suffix.lower() for path in rel_paths if Path(path).suffix}
    files = [
        path for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".xml", ".scss", ".css"}
    ]
    return sorted(
        files,
        key=lambda path: _core_path_relevance(path, terms, suffixes, _normalized_task_type(payload)),
    )


def _evidence_signature(project: Path, module_name: str, payload: OdooCodeChangeRequest, rel_paths: list[str]) -> str:
    import hashlib
    rows=[]
    for rel in sorted(set(rel_paths)):
        path=project/rel
        try:
            st=path.stat(); rows.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            rows.append((rel, None, None))
    raw=json.dumps({"module":module_name,"task_type":_normalized_task_type(payload),"paths":rows},sort_keys=True,default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _looks_like_frontend_code_change(payload: OdooCodeChangeRequest, rel_paths: list[str]) -> bool:
    """Return True only when the change actually touches frontend code.

    Website/payment words alone are not enough: controller/model Python changes
    must not trigger dozens of JS/OWL/POS reads.
    """
    # Trust an explicitly frontend task type, but do not let unrelated files
    # discovered during broad source search rewrite a backend task profile.
    profile = _task_profile(payload, [])
    if profile.get("frontend"):
        return True

    normalized_paths = [str(path).lower().replace("\\", "/") for path in rel_paths]
    frontend_file = any(
        path.endswith((".js", ".scss", ".css"))
        or (
            path.endswith(".xml")
            and any(marker in path for marker in ("/static/src/", "/static/", "/qweb/", "/templates/"))
        )
        for path in normalized_paths
    )
    backend_file = any(
        path.endswith(".py")
        or (path.endswith(".xml") and not any(marker in path for marker in ("/static/src/", "/static/", "/qweb/", "/templates/")))
        for path in normalized_paths
    )
    # Focused backend inspection can discover unrelated static files from the
    # same addon. Mixed evidence is not a frontend change unless frontend work
    # is explicit in the task profile or supplied writes. A frontend-only
    # target remains authoritative for callers that classified it broadly.
    if frontend_file and not backend_file:
        return True
    text = " ".join([
        payload.task_description or "", payload.intent or "", payload.target_hint or "",
    ]).lower()
    strong_markers = (
        "owl component", "qweb template", "javascript", "scss", "css",
        "frontend asset", "website editor", "public widget", "pos screen",
        "payment form js",
    )
    return any(marker in text for marker in strong_markers)


def _focused_scope_file_paths(
    *,
    project: Path,
    module_name: str,
    module_root_rel: str,
    custom_file_paths: list[str],
    target_files: list[str],
    writes: list[OdooCodeFileChange],
) -> tuple[list[str], bool]:
    """Select only explicit task/write files for adaptive scope analysis.

    Search evidence may contain many unrelated files from the same addon. It
    must not turn a one-model fix into a module-wide dependency/frontend scan.
    """
    available = {str(Path(path).resolve()): str(Path(path).resolve()) for path in custom_file_paths}
    requested: list[str] = []
    raw_paths = [str(item) for item in target_files]
    raw_paths.extend(str(write.relative_path) for write in writes)
    for raw in raw_paths:
        try:
            relative = _safe_module_relative(module_name, raw, module_root_rel)
            resolved = str((project / relative).resolve())
        except (HTTPException, OSError, ValueError):
            continue
        if resolved in available and resolved not in requested:
            requested.append(resolved)
    if requested:
        return requested, True
    if raw_paths:
        # An explicit missing/new file must not broaden a surgical task into a
        # module-wide scan. Keep only manifest evidence until the target exists.
        manifests = [
            path for path in custom_file_paths
            if Path(path).name in {"__manifest__.py", "__openerp__.py"}
        ]
        return manifests, True
    return list(custom_file_paths), False


def _exact_custom_targets_are_self_contained(
    *,
    project: Path,
    module_name: str,
    module_root_rel: str,
    target_files: list[str],
    writes: list[OdooCodeFileChange],
) -> bool:
    """Recognise small exact custom-file edits that need no installed-source fan-out.

    This is deliberately conservative.  A Python extension class without its
    own ``_name`` or an XML/QWeb inheritance reference still takes the normal
    Core/Enterprise inspection path.  A client-owned model declaring ``_name``
    may include standard mixins without turning a local field/view edit into a
    broad parent-model scan.
    """
    raw_paths = [str(item) for item in target_files if str(item).strip()]
    raw_paths.extend(str(write.relative_path) for write in writes if str(write.relative_path).strip())
    raw_paths = list(dict.fromkeys(raw_paths))
    if not raw_paths or len(raw_paths) > 3:
        return False

    supported = False
    for raw in raw_paths:
        try:
            relative = _safe_module_relative(module_name, raw, module_root_rel)
            path = (project / relative).resolve()
            path.relative_to((project / module_root_rel).resolve())
        except (HTTPException, OSError, ValueError):
            return False
        if not path.is_file() or path.suffix.lower() not in {".py", ".xml"}:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        supported = True
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return False
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                attributes = {
                    target.id
                    for statement in node.body
                    if isinstance(statement, ast.Assign)
                    for target in statement.targets
                    if isinstance(target, ast.Name)
                }
                if "_inherits" in attributes:
                    return False
                if "_inherit" in attributes and "_name" not in attributes:
                    return False
        else:
            for match in re.finditer(
                r"\b(?:inherit_id|t-call|t-inherit)\s*=\s*['\"]([^'\"]+)['\"]|"
                r"<field\b[^>]*\bname\s*=\s*['\"]inherit_id['\"][^>]*\bref\s*=\s*['\"]([^'\"]+)['\"]",
                text,
                flags=re.I,
            ):
                reference = next((value for value in match.groups() if value), "")
                if "." in reference and reference.split(".", 1)[0] != module_name:
                    return False
    return supported


def _scope_focus_identifiers(payload: OdooCodeChangeRequest) -> list[str]:
    """Return exact technical symbols that may safely constrain scope advice.

    Scope discovery is intentionally broad when the user asks for a complete
    module/redesign. For a surgical task, structured target fields and exact
    snake-case/dotted identifiers take precedence over descriptive prose.
    """
    text = " ".join(filter(None, (
        strip_vcs_target_context(payload.task_description),
        payload.target_hint,
        payload.intent,
    )))
    if payload.create_module or re.search(
        r"\b(?:module[- ]wide|whole module|entire module|full module|complete (?:module )?redesign|all files)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return []

    values: list[str] = []

    def add(value: Any) -> None:
        candidate = str(value or "").strip().strip("`'\"")
        if not candidate or candidate in values:
            return
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
            if "_" not in candidate and candidate.lower() in _TASK_CLUE_STOPWORDS:
                return
            values.append(candidate)
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", candidate):
            values.append(candidate)

    add(payload.target_resolution.identifier)
    add(payload.target_resolution.model)
    for key in ("field", "method", "identifier", "xml_id", "route", "template", "model"):
        add((payload.change_request or {}).get(key))
    for candidate in re.findall(
        r"(?<![\w])(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?![\w])",
        text,
    ):
        add(candidate)
    return values[:20]


def _static_frontend_fast_path(rel_paths: list[str]) -> bool:
    paths = [str(path).lower().replace("\\", "/") for path in rel_paths if str(path).strip()]
    return bool(paths) and all(
        "/static/" in path and path.endswith((".js", ".jsx", ".ts", ".tsx", ".xml", ".scss", ".css"))
        for path in paths
    )


def _git_status_changed_module_files(project_path: str, module_path: str) -> list[str]:
    """Return dirty/tracked/untracked files under module_path from git status.

    workflow.odoo_code_change may be re-called after a verify failure with a
    tiny follow-up write.  Committing only the latest write can leave earlier
    runtime JS/XML edits dirty and undeployed.  This helper makes the composite
    workflow deploy all current dirty module code files, not only the last
    writes[] list.
    """
    project = Path(project_path)
    result = GitService().status(project_path)
    changed: list[str] = []
    prefix = module_path.strip("/") + "/"
    for raw in (result.stdout or "").splitlines():
        if not raw or raw.startswith("##") or len(raw) < 4:
            continue
        rel = raw[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        if rel.startswith(prefix) and (project / rel).is_file():
            if rel.endswith((".py", ".xml", ".js", ".scss", ".css", ".csv")) or rel.endswith("__manifest__.py"):
                changed.append(rel)
    return list(dict.fromkeys(changed))


def _persist_module_contract(
    store: JsonStore,
    *,
    client_name: str,
    project_path: str,
    module_name: str,
    current_contract: dict[str, Any] | None,
    task_description: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep deliverables across continuations without leaking them into new tasks."""
    state = store.load_session_state(client_name)
    contracts = dict(state.get("module_contracts") or {})
    previous = contracts.get(module_name) if isinstance(contracts.get(module_name), dict) else {}
    resolved_project = str(Path(project_path).resolve())
    same_project = str(previous.get("project_path") or "") == resolved_project
    current_requirements = str(task_description or "").strip()
    lowered_requirements = current_requirements.lower()
    trivial_followup = lowered_requirements in {
        "continue", "implement", "apply", "apply kro", "fix", "fix kro", "push", "deploy", "yes", "haan", "han",
    }
    previous_task = str(previous.get("task_description") or "").strip()
    explicit_continuation = bool(
        trivial_followup
        or current_requirements == previous_task
        or lowered_requirements.startswith("continue the persisted module-contract implementation")
        or re.match(
            r"^(?:also|additionally|further|continue|same task|for the same task|to the original (?:task|module scope))\b",
            lowered_requirements,
        )
    )
    continue_previous = bool(same_project and previous and explicit_continuation)
    previous_contract = previous.get("contract") if continue_previous else None
    if isinstance(previous_contract, dict) and "tests_explicitly_requested" not in previous_contract:
        legacy_sources = [
            *list(previous.get("requirement_sources") or []),
            previous_task,
        ]
        legacy_tests_explicit = any(
            OdooModuleValidator.tests_explicitly_requested(source)
            for source in legacy_sources
            if str(source or "").strip()
        )
        previous_contract = dict(previous_contract)
        previous_contract["tests_explicitly_requested"] = legacy_tests_explicit
        if not legacy_tests_explicit:
            previous_contract["required_packages"] = [
                item for item in previous_contract.get("required_packages") or []
                if item != "tests"
            ]
            previous_contract["required_layers"] = [
                item for item in previous_contract.get("required_layers") or []
                if item != "tests"
            ]
            previous_contract["required_test_symbols"] = []
    merged = OdooModuleValidator.merge_contracts(previous_contract, current_contract)
    unchanged = continue_previous and previous_contract == merged
    requirement_sources = list(previous.get("requirement_sources") or []) if continue_previous else []
    new_requirement = bool(current_requirements and not trivial_followup and current_requirements not in requirement_sources)
    if new_requirement:
        requirement_sources.append(current_requirements)
    requirement_sources = requirement_sources[-5:]
    record = {
        "project_path": resolved_project,
        "module_name": module_name,
        "contract": merged,
        "status": previous.get("status", "pending") if unchanged and not new_requirement else "pending",
        "repair_attempts": int(previous.get("repair_attempts") or 0) if unchanged and not new_requirement else 0,
        "requirement_sources": requirement_sources,
        "task_description": previous_task if trivial_followup and previous_task else current_requirements,
        "updated_at": time.time(),
    }
    contracts[module_name] = record
    store.save_session_state({"module_contracts": contracts}, client_name=client_name)
    return merged, record


def _update_module_contract_status(
    store: JsonStore,
    *,
    client_name: str,
    module_name: str,
    status: str,
    increment_repair: bool = False,
) -> dict[str, Any]:
    state = store.load_session_state(client_name)
    contracts = dict(state.get("module_contracts") or {})
    record = dict(contracts.get(module_name) or {})
    record["status"] = status
    record["repair_attempts"] = (int(record.get("repair_attempts") or 0) + 1) if increment_repair else 0
    record["updated_at"] = time.time()
    contracts[module_name] = record
    store.save_session_state({"module_contracts": contracts}, client_name=client_name)
    return record


def _contract_continuation_task(task_description: str, module_name: str) -> str:
    if len(task_description) <= 2000:
        return task_description
    return (
        f"Continue the persisted module-contract implementation and structural repair for {module_name}. "
        "The connector retained the original complete requirements; implement every returned contract item and finding."
    )


def _code_change_continuation_arguments(
    payload: OdooCodeChangeRequest,
    *,
    client_name: str | None,
    project_path: str,
    module_name: str,
    target_files: list[str] | None = None,
    preserve_writes: bool,
    batch_index: int | None = None,
    coding_session_id: str | None = None,
) -> dict[str, Any]:
    """Preserve the exact request contract across every code-change continuation."""
    arguments = payload.model_dump(mode="json", exclude_none=True)
    arguments.pop("clarification_answer", None)
    if not preserve_writes:
        arguments.pop("writes", None)
    arguments.update({
        "client_name": client_name,
        "project_path": project_path,
        "module_name": module_name,
        "task_description": _contract_continuation_task(payload.task_description, module_name),
    })
    if target_files is not None:
        arguments["target_files"] = target_files
    if batch_index is not None:
        arguments["batch_index"] = batch_index
    effective_session_id = coding_session_id or payload.coding_session_id
    if effective_session_id:
        arguments["coding_session_id"] = effective_session_id
    return arguments


def _restore_coding_session_task(
    payload: OdooCodeChangeRequest,
    coding_session: dict[str, Any],
) -> tuple[OdooCodeChangeRequest, bool]:
    """Keep a resumed inspect-then-write call on its server-owned task scope.

    Natural-language refinements made while generating writes are not a new
    task.  The caller must omit ``coding_session_id`` to begin a materially new
    scope; user answers to a workflow clarification continue through the
    dedicated ``clarification_answer`` field.
    """
    canonical = str(coding_session.get("task_description") or "").strip()
    if not canonical or canonical == payload.task_description.strip():
        return payload, False
    return payload.model_copy(update={"task_description": canonical}), True


def _lock_resolved_target(
    payload: OdooCodeChangeRequest,
    target_resolution: OdooTargetResolutionResult,
) -> OdooCodeChangeRequest:
    """Carry one verified target unchanged through inspect-then-write calls."""
    if target_resolution.writes_blocked or not target_resolution.identifier:
        return payload
    try:
        locked_hint = OdooTargetResolutionHint.model_validate({
            "mode": target_resolution.mode,
            "kind": target_resolution.kind,
            "identifier": target_resolution.identifier,
            "model": target_resolution.model,
            "require_live": payload.target_resolution.require_live,
        })
    except ValidationError:
        # Aggregate scopes such as module_scope are already preserved by the
        # task contract and must not be forced into a single-object hint.
        return payload
    return payload.model_copy(update={
        "target_resolution": locked_hint
    })


def _workflow_module_analyze(payload: ModuleAnalyzeRequest) -> dict[str, Any]:
    """Analyze one custom addon locally without entering the coding pipeline."""
    client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path,
        require_complete=False,
    )
    effective_client_name = payload.client_name or (client.name if client else None)
    if effective_client_name:
        ClientGateService().require_validated_client(effective_client_name, "workflow.module_analyze")
    project_path = payload.project_path or (client.path if client else None)
    if not project_path:
        raise HTTPException(status_code=400, detail="client_name or project_path is required for workflow.module_analyze")
    repo = RepoService()
    project = repo.project(project_path)
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Existing checkout not found: {project}")
    modules = repo.list_custom_modules(str(project), max_depth=5, include_uninstallable=True)
    module, resolution = _resolve_fuzzy_module(payload, modules)
    source_targets: list[str] = []
    if not module:
        module, source_resolution, source_targets = _resolve_module_from_source_evidence(
            project,
            modules,
            payload,
        )
        if module:
            resolution = source_resolution
        else:
            resolution = {
                **resolution,
                "source_resolution": source_resolution,
            }
    if not module:
        return {
            "status": "module_ambiguous" if resolution.get("strategy") == "ambiguous_duplicate_module" else "module_not_found",
            "message": (
                "Multiple addon directories use the requested technical module name; specify the exact module root before continuing."
                if resolution.get("strategy") == "ambiguous_duplicate_module"
                else "No custom addon matched the supplied module hint."
            ),
            "project_path": str(project),
            "module_resolution": resolution,
            "available_modules": [item.module_name for item in modules[:50]],
            "success_allowed": False,
            "completion_required": False,
        }
    module_path = project / module.path
    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module_path,
        module_name=module.module_name,
        analysis_goal=payload.analysis_goal,
        max_files=payload.max_files,
        max_total_chars=payload.max_total_chars,
        target_files=[*payload.target_files, *source_targets],
        search_patterns=payload.search_patterns,
        module_candidates=modules,
    )
    result.update({
        "client_name": effective_client_name,
        "project_path": str(project),
        "module_resolution": resolution,
    })
    if payload.live_verify:
        live_models = payload.live_models or list((result.get("feature_contract") or {}).get("technical_identifiers") or [])
        if not live_models:
            result["live_verification"] = {
                "status": "skipped",
                "reason": "No exact technical models were resolved from the focused source evidence.",
                "ssh_shell_startups": 0,
            }
        else:
            live = OdooService(client).feature_inspect(live_models[:10], timeout_seconds=60)
            result["live_verification"] = live.model_dump(mode="json")
            result["feature_contract"]["live_verification_needed"] = False
            result["feature_contract"]["live_verified"] = live.status == "ok"
    return result

def _is_installability_repair_request(payload: OdooCodeChangeRequest) -> bool:
    text = " ".join(filter(None, [payload.task_description, payload.intent, payload.target_hint])).lower()
    return bool(re.search(
        r"(?:\bnot[ _-]?installable\b|\buninstallable\b|\binstallability\b|"
        r"\b(?:install|upgrade)\s+(?:failure|error)\b|\brepair\b.{0,40}\bmanifest\b|\bfix\b.{0,40}\bmanifest\b)",
        text,
    ))


_ALWAYS_BLOCKING_QA_CODES = {
    "MODULE_NOT_FOUND",
    "MODULE_NOT_DIRECTORY",
    "MISSING_MANIFEST",
    "MISSING_INIT",
    "MANIFEST_SYNTAX_ERROR",
    "MANIFEST_PARSE_ERROR",
    "MANIFEST_NOT_DICT",
    "MANIFEST_MISSING_KEY",
    "MANIFEST_BAD_DEPENDS",
    "MANIFEST_VERSION_CHANGED",
}


def _qa_finding_fingerprint(item: Any) -> tuple[str, str, str]:
    data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item or {})
    raw_file = str(data.get("file") or "").replace("\\", "/")
    try:
        normalized_file = Path(raw_file).resolve().as_posix().casefold() if raw_file else ""
    except Exception:
        normalized_file = raw_file.casefold()
    message = re.sub(r"\s+", " ", str(data.get("message") or "")).strip().casefold()
    return str(data.get("code") or ""), normalized_file, message


def _partition_surgical_qa_errors(
    baseline_findings: list[dict[str, Any]],
    current_errors: list[Any],
    *,
    strict_module_validation: bool,
) -> tuple[list[Any], list[Any]]:
    """Separate new/task-critical failures from unrelated pre-existing debt."""
    if strict_module_validation:
        return list(current_errors), []
    baseline = {_qa_finding_fingerprint(item) for item in baseline_findings}
    blocking: list[Any] = []
    preexisting: list[Any] = []
    for item in current_errors:
        code = str(getattr(item, "code", "") or "")
        task_required = code.startswith("REQUESTED_") or code.startswith("COMPLETE_MODULE_")
        if task_required or code in _ALWAYS_BLOCKING_QA_CODES or _qa_finding_fingerprint(item) not in baseline:
            blocking.append(item)
        else:
            preexisting.append(item)
    return blocking, preexisting


_VERIFICATION_OVERRIDEABLE_CODES = {
    # Static model/view resolver ambiguity. These may be safely overridden only
    # when every CRITICAL finding belongs to this policy set.
    "view_field_not_in_core_model",
    "related_field_exists",
    "xpath_match_count",
}

_VERIFICATION_MODE_ALIASES = {
    "none": "strict",
    "irrelevant_only": "allow_unverified",
}


def _canonical_verification_override_mode(value: str | None) -> str:
    mode = str(value or "strict").strip()
    return _VERIFICATION_MODE_ALIASES.get(mode, mode)


def _force_verification_case_id(
    *,
    client_name: str | None,
    project_path: str,
    module_name: str,
    git_diff: str,
    verify: Any,
) -> str:
    """Return a stable authorization key for one exact force-deploy case.

    The grant changes whenever the working-tree diff or CRITICAL verifier findings
    change, so an approval cannot silently carry over to different code or risk.
    """
    critical = [
        item.model_dump(mode="json")
        for item in verify.findings
        if item.severity.value == "CRITICAL"
    ]
    normalized = {
        "client_name": str(client_name or ""),
        "project_path": str(Path(project_path).resolve()),
        "module_name": module_name,
        "git_diff_sha256": hashlib.sha256((git_diff or "").encode("utf-8")).hexdigest(),
        "critical_findings": critical,
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return "forcecase-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _resolve_force_override_authorization(
    store: JsonStore,
    payload: OdooCodeChangeRequest,
    *,
    client_name: str | None,
    project_path: str,
    module_name: str,
    git_diff: str,
    verify: Any,
) -> dict[str, Any]:
    """Persist and reuse one explicit force confirmation for the same case.

    A grant is scoped to the exact dirty diff and CRITICAL finding set. It is not
    reusable after code or verifier risk changes. Grants expire after 24 hours.
    """
    if _canonical_verification_override_mode(payload.verification_override) != "force":
        return {"authorized": False, "source": None, "case_id": None}

    case_id = _force_verification_case_id(
        client_name=client_name,
        project_path=project_path,
        module_name=module_name,
        git_diff=git_diff,
        verify=verify,
    )
    now = time.time()
    state = store.load_session_state(client_name)
    grants = dict(state.get("verification_force_grants") or {})
    grants = {
        key: value
        for key, value in grants.items()
        if now - float((value or {}).get("authorized_at") or 0) <= 86400
    }

    if payload.verification_override_confirmation == "I AUTHORIZE FORCE PUSH AND DEPLOY":
        grants[case_id] = {
            "authorized_at": now,
            "client_name": client_name,
            "project_path": str(Path(project_path).resolve()),
            "module_name": module_name,
            "reason": str(payload.verification_override_reason or "").strip(),
        }
        if len(grants) > 20:
            grants = dict(sorted(
                grants.items(),
                key=lambda item: float((item[1] or {}).get("authorized_at") or 0),
            )[-20:])
        store.save_session_state({"verification_force_grants": grants}, client_name=client_name)
        return {"authorized": True, "source": "explicit_confirmation", "case_id": case_id}

    if case_id in grants:
        if state.get("verification_force_grants") != grants:
            store.save_session_state({"verification_force_grants": grants}, client_name=client_name)
        return {"authorized": True, "source": "persisted_case_grant", "case_id": case_id}

    if state.get("verification_force_grants") != grants:
        store.save_session_state({"verification_force_grants": grants}, client_name=client_name)
    return {"authorized": False, "source": None, "case_id": case_id}


def _verification_override_decision(
    payload: OdooCodeChangeRequest,
    verify: Any,
    *,
    force_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify verifier findings under a reusable, auditable policy.

    This decision applies only to workflow.verify_against_core. Blocking QA,
    syntax, parser, manifest, and module-structure errors are evaluated earlier
    and remain non-overrideable.
    """
    critical = [item for item in verify.findings if item.severity.value == "CRITICAL"]
    requested_mode = str(payload.verification_override or "strict")
    mode = _canonical_verification_override_mode(requested_mode)
    accepted = False
    rejected_codes: list[str] = []
    if mode == "allow_unverified" and critical:
        rejected_codes = sorted({
            str(getattr(item, "code", "") or getattr(item, "check", "") or "unknown")
            for item in critical
            if str(getattr(item, "code", "") or getattr(item, "check", "") or "")
            not in _VERIFICATION_OVERRIDEABLE_CODES
        })
        accepted = not rejected_codes
    elif mode == "force":
        accepted = bool(
            (force_authorization or {}).get("authorized")
            or payload.verification_override_confirmation == "I AUTHORIZE FORCE PUSH AND DEPLOY"
        )
    return {
        "requested": mode != "strict",
        "requested_mode": requested_mode,
        "mode": mode,
        "accepted": accepted,
        "reason": str(payload.verification_override_reason or "").strip() or None,
        "authorization_source": (force_authorization or {}).get("source"),
        "authorization_case_id": (force_authorization or {}).get("case_id"),
        "confirmation_required": bool(mode == "force" and not accepted),
        "critical_count": len(critical),
        "overrideable_codes": sorted(_VERIFICATION_OVERRIDEABLE_CODES),
        "rejected_codes": rejected_codes,
        "findings": [item.model_dump(mode="json") for item in critical],
    }


_CODING_WORKFLOW_MAX_WORKERS = max(1, int(getattr(settings, "coding_workflow_max_workers", 2)))
_CODING_WORKFLOW_MAX_QUEUE = max(0, int(getattr(settings, "coding_workflow_max_queue", 2)))
_CODING_WORKFLOW_ADMISSION_CAPACITY = _CODING_WORKFLOW_MAX_WORKERS + _CODING_WORKFLOW_MAX_QUEUE
_CODING_WORKFLOW_ADMISSION = threading.BoundedSemaphore(_CODING_WORKFLOW_ADMISSION_CAPACITY)
_CODING_WORKFLOW_EXECUTOR = ThreadPoolExecutor(
    max_workers=_CODING_WORKFLOW_MAX_WORKERS,
    thread_name_prefix="odoo-code-change",
)
_CODING_WORKFLOW_JOB_LOCK = threading.Lock()
_CODING_WORKFLOW_INSTANCE_ID = uuid4().hex
_CODING_WORKFLOW_HEARTBEAT_SECONDS = 5
_CODING_WORKFLOW_WORKER_LEASE_SECONDS = 30


def _code_change_async_fingerprint(payload: OdooCodeChangeRequest) -> str:
    data = payload.model_dump(mode="json", exclude_none=True)
    data.pop("wait_for_result", None)
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _workflow_failure_metadata(exc: BaseException, progress: dict[str, Any] | None) -> dict[str, Any]:
    """Classify connector failures without conflating them with Odoo code."""
    detail = str(getattr(exc, "detail", exc) or "")
    lowered = detail.lower()
    stage = str((progress or {}).get("current_phase") or "unknown")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timed out" in lowered or "timeout" in lowered:
        origin = "provider_transport" if any(token in lowered for token in ("ssh", "http", "json-rpc", "network")) else "connector_pipeline"
        retryable = True
        strategy = "retry_provider_bounded" if origin == "provider_transport" else "resume_connector"
    elif any(token in lowered for token in ("project path not found", "module not found", "missing checkout")):
        origin = "missing_context"
        retryable = False
        strategy = "load_exact_source"
    elif any(token in lowered for token in ("ambiguous", "clarification", "multiple targets")):
        origin = "missing_context"
        retryable = False
        strategy = "ask_material_question"
    else:
        origin = "connector_pipeline"
        retryable = False
        strategy = "resume_connector"
    return {
        "failure_origin": origin,
        "failure_stage": stage,
        "retryable": retryable,
        "next_strategy": strategy,
    }


def _code_change_resume_metadata(job: JobRecord) -> dict[str, Any]:
    """Build a client-carried recovery token without relying on local job state."""
    if job.job_type != "workflow.odoo_code_change":
        return {}
    resume_arguments = (job.request or {}).get("resume_arguments")
    if not isinstance(resume_arguments, dict):
        return {}
    try:
        payload = OdooCodeChangeRequest.model_validate(resume_arguments)
        token = ContinuationTokenService().issue_code_change(payload)
    except (ValidationError, ContinuationTokenError) as exc:
        return {
            "resume_token_available": False,
            "resume_token_error": str(exc),
        }
    return {
        **token,
        "resume_token_available": True,
        "resume_next_call": {
            "tool": "workflow.resume_continuation",
            "arguments": {"resume_token": token["resume_token"]},
        },
    }


def _code_change_job_result(job: JobRecord, *, operation_id: str, reused: bool) -> dict[str, Any]:
    resume_metadata = _code_change_resume_metadata(job)
    if job.status not in {JobStatus.queued, JobStatus.running} and isinstance(job.result, dict):
        result = dict(job.result)
        result.setdefault("job_id", job.id)
        result.setdefault("operation_id", operation_id)
        result["async_job_reused"] = reused
        result.update(resume_metadata)
        return result
    return {
        "status": "running",
        "state_class": "running",
        "message": "The durable code-change operation is running. Poll jobs.get; do not submit the task again.",
        "job_id": job.id,
        "operation_id": operation_id,
        "async_job_reused": reused,
        "protected_work_started": True,
        "completion_required": True,
        "success_allowed": False,
        "poll_after_seconds": 2,
        "next_call": job.next_call,
        **resume_metadata,
    }


async def _workflow_resume_continuation(payload: ContinuationResumeRequest) -> dict[str, Any]:
    """Rehydrate a code-change request from a ChatGPT-carried signed token."""
    try:
        arguments = ContinuationTokenService().decode_code_change(payload.resume_token)
        request = OdooCodeChangeRequest.model_validate(arguments)
    except (ContinuationTokenError, ValidationError) as exc:
        return {
            "status": "resume_token_invalid",
            "state_class": "blocked",
            "message": str(exc),
            "success_allowed": False,
            "completion_required": False,
            "next_call": None,
        }
    result = await _workflow_odoo_code_change(request.model_copy(update={
        "wait_for_result": False,
        "coding_session_id": None,
        "workspace_session_id": None,
    }))
    result["resumed_from_client_token"] = True
    return result


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _code_change_worker_is_orphaned(job: JobRecord, store: JsonStore) -> bool:
    """Return true only when another process owned an expired worker lease."""
    if job.job_type != "workflow.odoo_code_change" or job.status not in {
        JobStatus.queued,
        JobStatus.running,
    }:
        return False
    request = dict(job.request or {})
    if request.get("owner_instance_id") == _CODING_WORKFLOW_INSTANCE_ID:
        return False
    operation_id = str(request.get("operation_id") or "")
    progress = store.get_workflow_progress(operation_id) if operation_id else None
    heartbeat_at = _parse_utc_timestamp(
        (progress or {}).get("worker_heartbeat_at")
        or (progress or {}).get("last_progress_at")
        or job.created_at
    )
    if heartbeat_at is None:
        return True
    return (
        datetime.now(timezone.utc) - heartbeat_at
    ).total_seconds() > _CODING_WORKFLOW_WORKER_LEASE_SECONDS


def _interrupt_orphaned_code_change_job(job: JobRecord, store: JsonStore) -> JobRecord:
    """Close a provably dead worker while preserving an exact recovery call."""
    if not _code_change_worker_is_orphaned(job, store):
        return job
    request = dict(job.request or {})
    operation_id = str(request.get("operation_id") or "")
    resume_arguments = request.get("resume_arguments")
    if isinstance(resume_arguments, dict):
        next_call = {"tool": "workflow.odoo_code_change", "arguments": resume_arguments}
    else:
        legacy_arguments = {
            key: request[key]
            for key in ("client_name", "project_path", "module_name")
            if request.get(key)
        }
        legacy_arguments["wait_for_result"] = False
        next_call = {
            "tool": "workflow.odoo_code_change",
            "arguments": legacy_arguments,
            "required_generated_arguments": ["task_description"],
        }
    interrupted = store.finish_job(job, JobStatus.failed, {
        "status": "operation_interrupted",
        "state_class": "action_required",
        "error": "The owning connector worker stopped and its heartbeat lease expired.",
        "operation_id": operation_id,
        "success_allowed": False,
        "completion_required": True,
        "next_call": next_call,
    })
    if operation_id:
        store.update_workflow_progress(operation_id, {
            "status": "operation_interrupted",
            "current_phase": "worker_interrupted",
            "last_progress_at": datetime.now(timezone.utc).isoformat(),
            "terminal": True,
        })
    return interrupted


def _recover_code_change_job_if_orphaned(job: JobRecord, store: JsonStore) -> JobRecord:
    """Resume an orphan from its persisted request; never replace a live worker."""
    if not _code_change_worker_is_orphaned(job, store):
        return job
    interrupted = _interrupt_orphaned_code_change_job(job, store)
    resume_arguments = (interrupted.result or {}).get("next_call", {}).get("arguments")
    if not isinstance(resume_arguments, dict):
        return interrupted
    try:
        payload = OdooCodeChangeRequest.model_validate(resume_arguments)
    except ValidationError:
        return interrupted
    started = _start_or_reuse_code_change_job(payload)
    return store.get_job(str(started.get("job_id") or interrupted.id))


def _start_or_reuse_code_change_job(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Persist provider work before execution and deduplicate transport retries."""
    store = JsonStore()
    operation_id = WorkflowProgressService.operation_id(payload)
    fingerprint = _code_change_async_fingerprint(payload)
    job_id = f"codejob-{fingerprint[:40]}"

    with _CODING_WORKFLOW_JOB_LOCK:
        try:
            candidate = store.get_job(job_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            candidate = None
        if candidate is not None:
            if candidate.status in {JobStatus.queued, JobStatus.running}:
                candidate = _interrupt_orphaned_code_change_job(candidate, store)
                if candidate.status not in {JobStatus.queued, JobStatus.running}:
                    candidate = None
            elif isinstance(candidate.result, dict) and candidate.result.get("status") == "operation_interrupted":
                candidate = None
            if candidate is not None:
                return _code_change_job_result(candidate, operation_id=operation_id, reused=True)

        resume_arguments = payload.model_dump(mode="json", exclude_none=True)
        resume_arguments["wait_for_result"] = False
        job = store.create_job(JobRecord(
            id=job_id,
            job_type="workflow.odoo_code_change",
            status=JobStatus.running,
            request={
                "operation_id": operation_id,
                "request_fingerprint": fingerprint,
                "owner_instance_id": _CODING_WORKFLOW_INSTANCE_ID,
                "resume_arguments": resume_arguments,
                "client_name": payload.client_name,
                "project_path": payload.project_path,
                "module_name": payload.module_name,
            },
        ))

    synchronous_payload = payload.model_copy(update={"wait_for_result": True})

    def _background() -> None:
        worker_store = JsonStore()
        heartbeat_stop = threading.Event()
        worker_progress = WorkflowProgressService()

        def _heartbeat() -> None:
            worker_progress.worker_heartbeat(operation_id, _CODING_WORKFLOW_INSTANCE_ID)
            while not heartbeat_stop.wait(_CODING_WORKFLOW_HEARTBEAT_SECONDS):
                worker_progress.worker_heartbeat(operation_id, _CODING_WORKFLOW_INSTANCE_ID)

        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            name=f"odoo-code-heartbeat-{job.id[-8:]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = asyncio.run(_workflow_odoo_code_change(synchronous_payload))
            worker_store.finish_job(worker_store.get_job(job.id), JobStatus.passed, result)
        except BaseException as exc:  # noqa: BLE001
            failure_metadata = _workflow_failure_metadata(
                exc,
                worker_store.get_workflow_progress(operation_id),
            )
            worker_store.finish_job(worker_store.get_job(job.id), JobStatus.failed, {
                "status": "failed",
                "state_class": "failed",
                "error": str(getattr(exc, "detail", exc)),
                "error_code": type(exc).__name__,
                "operation_id": operation_id,
                "success_allowed": False,
                "completion_required": False,
                **failure_metadata,
            })
        finally:
            heartbeat_stop.set()

    try:
        submit_background(_background)
    except Exception as exc:  # noqa: BLE001
        failed = store.finish_job(store.get_job(job.id), JobStatus.failed, {
            "status": "operation_not_started",
            "state_class": "failed",
            "error": f"The connector could not submit the durable worker: {exc}",
            "operation_id": operation_id,
            "success_allowed": False,
            "completion_required": False,
        })
        return _code_change_job_result(failed, operation_id=operation_id, reused=False)
    return _code_change_job_result(job, operation_id=operation_id, reused=False)


def _is_explicit_read_only_inspection(payload: OdooCodeChangeRequest) -> bool:
    return (
        str(payload.patch_strategy or "").strip().lower() == "inspect_only"
        and not payload.writes
        and not payload.create_module
    )


def _inspect_only_module_analysis_request(payload: OdooCodeChangeRequest) -> ModuleAnalyzeRequest:
    return ModuleAnalyzeRequest(
        client_name=payload.client_name,
        project_path=payload.project_path,
        module_name=payload.module_name,
        module_hint=payload.module_hint,
        analysis_goal=payload.task_description,
        max_files=max(10, min(30, len(payload.target_files) + 10)),
        max_total_chars=12000,
        search_patterns=list(payload.search_patterns),
        target_files=list(payload.target_files),
    )


def _checkout_missing_redirect(payload: OdooCodeChangeRequest) -> dict[str, Any] | None:
    """Same-turn redirect to the ws_* lane when this host has no local checkout."""
    if payload.project_path:
        return None  # caller asked for a specific checkout; keep the existing behavior
    try:
        client_name, project_path = _resolve_code_change_project(payload)
        client = ClientResolverService().resolve_client(client_name=client_name, require_complete=False)
    except HTTPException:
        return None
    if client is None or not client.repo or Path(project_path).expanduser().is_dir():
        return None
    return build_ws_redirect(
        client_name=client.name,
        module_name=payload.module_name,
        create_module=payload.create_module,
        task_description=payload.task_description,
        search_patterns=payload.search_patterns,
        target_files=getattr(payload, "target_files", None) or [],
        commit_message=getattr(payload, "commit_message", None),
    )


async def _workflow_odoo_code_change(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Run blocking work off-loop with bounded admission and no cancellation."""
    if _is_explicit_read_only_inspection(payload):
        result = await asyncio.to_thread(
            _workflow_module_analyze,
            _inspect_only_module_analysis_request(payload),
        )
        if isinstance(result, dict):
            result.update({
                "routed_from": "workflow.odoo_code_change",
                "route": "read_only_module_analysis",
                "side_effects": {
                    "files_modified": False,
                    "commit_created": False,
                    "push_started": False,
                    "deployment_started": False,
                },
            })
        return result
    redirect = _checkout_missing_redirect(payload)
    if redirect is not None:
        return redirect
    if not payload.wait_for_result:
        started = _start_or_reuse_code_change_job(payload)
        if started.get("state_class") != "running" or not started.get("job_id"):
            return started
        # Preserve the fast single-call experience for ordinary work while the
        # durable job protects longer work from provider transport timeouts.
        # This is a bounded observation only; timing out never cancels or
        # restarts the protected workflow.
        initial_wait = max(0, min(
            15,
            int(getattr(settings, "workflow_watchdog_wait_cap_seconds", 30) or 0),
        ))
        if initial_wait <= 0:
            return started
        waited = await asyncio.to_thread(
            JsonStore().wait_job,
            str(started["job_id"]),
            initial_wait,
            0.25,
        )
        return _code_change_job_result(
            waited,
            operation_id=str(started["operation_id"]),
            reused=bool(started.get("async_job_reused")),
        )

    admission = _CODING_WORKFLOW_ADMISSION
    if not admission.acquire(blocking=False):
        retry_arguments = payload.model_dump(mode="json", exclude_none=True)
        return {
            "status": "workflow_busy",
            "state_class": "action_required",
            "message": "The bounded coding workflow is at capacity; no work started. Retry this same call after the stated delay.",
            "operation_id": WorkflowProgressService.operation_id(payload),
            "retry_after_seconds": 2,
            "poll_after_seconds": 2,
            "protected_work_started": False,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": retry_arguments,
            },
        }
    loop = asyncio.get_running_loop()
    submitted_at = time.perf_counter()
    worker_started_at: list[float] = []
    progress = WorkflowProgressService()
    progress_id = progress.operation_id(payload)
    progress.start(
        progress_id,
        client_name=payload.client_name,
        module_name=payload.module_name,
    )

    def run() -> dict[str, Any]:
        worker_started_at.append(time.perf_counter())
        queue_wait_ms = round((worker_started_at[0] - submitted_at) * 1000)
        progress.phase(progress_id, "starting", queue_wait_ms=queue_wait_ms)
        try:
            result = asyncio.run(_workflow_odoo_code_change_impl(payload))
            if isinstance(result, dict):
                metrics = dict(result.get("orchestration_metrics") or {})
                metrics["queue_wait_ms"] = queue_wait_ms
                metrics["admission_capacity"] = _CODING_WORKFLOW_ADMISSION_CAPACITY
                result["orchestration_metrics"] = metrics
                result["workflow_progress"] = progress.finish(
                    progress_id,
                    status=str(result.get("status") or "completed"),
                    metrics=metrics,
                )
            return result
        except BaseException as exc:
            progress.fail(progress_id, exc)
            raise
        finally:
            # Release only after protected work actually finishes. A disconnected
            # or cancelled caller must not free capacity while its worker remains
            # active in the background.
            admission.release()

    try:
        future = loop.run_in_executor(_CODING_WORKFLOW_EXECUTOR, run)
    except Exception:
        admission.release()
        raise
    result = await asyncio.shield(future)
    return result


async def _workflow_odoo_code_change_impl(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Provider-safe composite Odoo code-change workflow.

    This is intentionally server-owned to remove MCP provider overhead/confusion:
    provider calls this one tool, the connector performs the v15 custom+core read,
    applies explicit edits, verifies, starts safe deploy, and returns a job_id for jobs.get.
    """
    workflow_started = time.perf_counter()
    phase_timings: dict[str, int] = {}
    progress = WorkflowProgressService()
    progress_id = progress.operation_id(payload)

    def phase_started(name: str, **extra: Any) -> None:
        progress.phase(progress_id, name, **extra)

    def phase_done(name: str, started: float, **extra: Any) -> None:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        phase_timings[name] = elapsed_ms
        progress.phase_done(progress_id, name, elapsed_ms, **extra)

    def orchestration_metrics() -> dict[str, Any]:
        return {
            "total_ms": round((time.perf_counter() - workflow_started) * 1000),
            "phases_ms": dict(phase_timings),
        }

    prepare_started = time.perf_counter()
    phase_started("repo_prepare", client_name=payload.client_name, module_name=payload.module_name)
    resolved_client_name, project_path = _resolve_code_change_project(payload)
    client_name = resolved_client_name or payload.client_name
    repo = RepoService()
    gate = PreWriteGateService()
    state_store = JsonStore()
    coding_sessions = CodingSessionService()
    coding_workspaces = CodingWorkspaceService()
    coding_session: dict[str, Any] | None = None
    coding_workspace: dict[str, Any] | None = None
    workspace_module_context: dict[str, Any] = {}
    coding_session_resumed = False
    validated_write_contract: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []

    if payload.coding_session_id:
        try:
            coding_session = coding_sessions.resume(
                payload.coding_session_id,
                client_name=client_name,
                project_path=project_path,
                task_description=payload.task_description,
            )
        except HTTPException as exc:
            if exc.status_code not in {400, 404}:
                raise
            coding_session = {
                "id": payload.coding_session_id,
                "valid": False,
                "status": "stale",
                "resume_reason": "session_missing_or_invalid",
            }
        if not coding_session.get("valid"):
            refresh_arguments = payload.model_dump(mode="json", exclude_none=True)
            refresh_arguments.pop("coding_session_id", None)
            refresh_arguments.pop("writes", None)
            refresh_arguments["project_path"] = project_path
            return {
                "status": "coding_session_stale",
                "message": (
                    "The module changed after focused inspection, so the generated writes were not applied. "
                    "Refresh the coding session from current source, then regenerate the write set."
                ),
                "client_name": client_name,
                "project_path": project_path,
                "module_name": coding_session.get("module_name") or payload.module_name,
                "coding_session": coding_sessions.public_summary(coding_session),
                "writes_blocked": True,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": refresh_arguments,
                    "required_generated_arguments": [],
                },
            }
        payload, task_scope_restored = _restore_coding_session_task(payload, coding_session)
        coding_session_resumed = True
        payload = payload.model_copy(update={
            "module_name": str(coding_session.get("module_name") or payload.module_name),
        })
        if not payload.workspace_session_id and coding_session.get("workspace_session_id"):
            payload = payload.model_copy(update={
                "workspace_session_id": coding_session.get("workspace_session_id"),
            })
        project_path = str(coding_session["project_path"])
        state_store.save_session_state({
            "workflow_prepare_cache": {
                "project_path": project_path,
                "at": time.time(),
                "branch": None,
                "sha": coding_session.get("base_sha"),
            }
        }, client_name=client_name)
        steps.append({
            "step": "workflow.coding_session",
            "status": "resumed",
            "session_id": payload.coding_session_id,
            "bundle_version": coding_session.get("bundle_version"),
            "base_sha": coding_session.get("base_sha"),
            "task_scope_restored": task_scope_restored,
        })

    # 1) Prepare checkout once.  In real multi-client use the client config supplies
    # repo URL/branch; for already-materialized local checkouts/tests, avoid failing
    # just because TARGET_GIT_REPO is not configured.
    cached_prepare = (state_store.load_session_state(client_name).get("workflow_prepare_cache") or {})
    cached_path = str(cached_prepare.get("project_path") or "")
    cached_at = float(cached_prepare.get("at") or 0)
    project = repo.project(project_path)
    if cached_path and Path(cached_path).resolve() == project.resolve() and (project / ".git").exists() and time.time() - cached_at < 300:
        project_path = str(project)
        steps.append({"step": "repo.prepare", "status": "cached", "project_path": project_path, "max_age_seconds": 300})
    else:
        try:
            prepare = repo.prepare_repo(project_path=project_path, client_name=client_name)
            project_path = prepare.project_path
            project = Path(project_path)
            steps.append({"step": "repo.prepare", "status": "ok", "project_path": project_path, "branch": prepare.branch, "sha": prepare.sha})
            state_store.save_session_state({"workflow_prepare_cache": {"project_path": project_path, "at": time.time(), "branch": prepare.branch, "sha": prepare.sha}}, client_name=client_name)
        except HTTPException as exc:
            detail = str(exc.detail)
            project = repo.project(project_path)
            if exc.status_code == 400 and "repo_url is required" in detail and (project / ".git").exists():
                steps.append({"step": "repo.prepare", "status": "skipped_existing_checkout", "project_path": str(project), "reason": detail})
                project_path = str(project)
            else:
                raise
    phase_done("repo_prepare", prepare_started)

    if coding_session_resumed:
        validated_write_contract, contract_stale_reason = _validated_session_write_contract(
            coding_session,
            payload,
            project,
        )
        if contract_stale_reason:
            refresh_arguments = payload.model_dump(mode="json", exclude_none=True)
            refresh_arguments.pop("coding_session_id", None)
            refresh_arguments.pop("writes", None)
            return {
                "status": "coding_session_stale",
                "message": "Validated inspection evidence changed; no write was applied.",
                "reason": contract_stale_reason,
                "client_name": client_name,
                "project_path": project_path,
                "module_name": str(coding_session.get("module_name") or payload.module_name),
                "coding_session": coding_sessions.public_summary(coding_session),
                "writes_blocked": True,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": refresh_arguments,
                    "required_generated_arguments": [],
                },
            }

    # 2) Discover/validate module once, with a safe fuzzy resolver for human hints.
    discovery_started = time.perf_counter()
    phase_started("module_discovery")
    modules: list[CustomModuleItem] = []
    if validated_write_contract:
        module_path = str(validated_write_contract["module_path"])
        manifest_path = str(validated_write_contract["manifest_path"])
        module_dir_from_contract = (project / module_path).resolve()
        manifest_from_contract = (project / manifest_path).resolve()
        if not module_dir_from_contract.is_dir() or not manifest_from_contract.is_file():
            refresh_arguments = payload.model_dump(mode="json", exclude_none=True)
            refresh_arguments.pop("coding_session_id", None)
            refresh_arguments.pop("writes", None)
            return {
                "status": "coding_session_stale",
                "state_class": "action_required",
                "message": "The validated module or manifest is no longer present; no write was applied.",
                "reason": "validated_module_layout_changed",
                "writes_blocked": True,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": refresh_arguments,
                },
            }
        module = CustomModuleItem(
            module_name=str(validated_write_contract["module_name"]),
            path=module_path,
            manifest_path=manifest_path,
        )
        module_resolution = {
            "requested": payload.module_name,
            "resolved": module.module_name,
            "strategy": "validated_write_contract",
            "score": 1.0,
        }
    else:
        _ensure_root_module_scaffold(repo, project, payload)
        modules = repo.list_custom_modules(project_path, max_depth=5, include_uninstallable=False)
        module, module_resolution = _resolve_fuzzy_module(payload, modules)
    if not module:
        ambiguous_module = module_resolution.get("strategy") == "ambiguous_duplicate_module"
        return {
            "status": "failed",
            "error": (
                f"Multiple custom module directories use technical name '{payload.module_name}'. "
                "Select the exact discovered module root; no file was read or written."
                if ambiguous_module
                else f"Custom module not found from hint: {payload.module_name}"
            ),
            "module_resolution": module_resolution,
            "available_modules": [item.module_name for item in modules[:50]],
            "success_allowed": False,
        }
    effective_module_name = module.module_name
    phase_done("module_discovery", discovery_started)
    if coding_session_resumed and effective_module_name != str(coding_session.get("module_name") or ""):
        refresh_arguments = payload.model_dump(mode="json", exclude_none=True)
        refresh_arguments.pop("coding_session_id", None)
        refresh_arguments.pop("writes", None)
        refresh_arguments["module_name"] = effective_module_name
        return {
            "status": "coding_session_stale",
            "state_class": "action_required",
            "message": "The resumed coding session belongs to a different module; no write was applied.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "writes_blocked": True,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": refresh_arguments,
            },
        }
    deploy_diagnostics = state_store.load_session_state(client_name).get("module_deploy_diagnostics") or {}
    deploy_diagnostic = deploy_diagnostics.get(effective_module_name) if isinstance(deploy_diagnostics, dict) else None
    installability_repair = _is_installability_repair_request(payload)
    if (
        installability_repair
        and isinstance(deploy_diagnostic, dict)
        and deploy_diagnostic.get("source_repair_allowed") is False
    ):
        evidence = "\n".join(str(item) for item in payload.investigation_evidence)
        has_concrete_source_error = bool(
            evidence.strip()
            and re.search(r"(?:Traceback \(most recent call last\)|\b(?:ImportError|ModuleNotFoundError|ParseError|ValidationError|KeyError|AttributeError|TypeError|ValueError|AssertionError)\b)", evidence)
        )
        if not has_concrete_source_error:
            return {
                "status": "diagnosis_required",
                "message": "SOURCE_REPAIR_BLOCKED: the deployed manifest was already read directly. Registry state alone cannot start workflow.odoo_code_change.",
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "deploy_diagnostic": deploy_diagnostic,
                "writes_blocked": True,
                "source_repair_allowed": False,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "odoosh.error_pack",
                    "arguments": {"client_name": client_name, "tail_lines": 500},
                },
                "resume_rule": "Only retry workflow.odoo_code_change when investigation_evidence contains a concrete Odoo traceback/exception. Missing dependencies must be installed, not repaired in this addon's manifest blindly.",
            }
    effective_task_description = payload.task_description
    if str(payload.clarification_answer or "").strip():
        effective_task_description = (
            f"{payload.task_description}\n\nConfirmed user clarification:\n"
            f"{str(payload.clarification_answer).strip()}"
        )
    current_contract = (
        dict(validated_write_contract.get("module_contract") or {})
        if validated_write_contract
        else OdooModuleValidator.requested_contract(effective_task_description, payload.create_module)
    )
    issue_fingerprint = code_change_issue_fingerprint(
        project_path=project_path,
        module_name=effective_module_name,
        task_description=effective_task_description,
        task_type=_normalized_task_type(payload),
        intent=payload.intent,
        target_hint=_target_hint(payload),
    )
    module_dir = project / module.path
    module_root_rel = module.path
    workspace_started = time.perf_counter()
    phase_started("workspace_activation", client_name=client_name, module_name=effective_module_name)
    coding_workspace = coding_workspaces.open_or_resume(
        workspace_session_id=payload.workspace_session_id,
        client_name=client_name,
        project_path=project_path,
    )
    workspace_activation = coding_workspaces.activate_module(
        str(coding_workspace["id"]),
        module_name=effective_module_name,
        module_path=str(module_dir),
        validated_fingerprint=(
            str(coding_session.get("module_fingerprint") or "")
            if coding_session_resumed and coding_session
            else None
        ),
        validated_base_sha=(
            str(coding_session.get("base_sha") or "")
            if coding_session_resumed and coding_session
            else None
        ),
    )
    coding_workspace = workspace_activation["workspace"]
    workspace_module_context = workspace_activation["module"]
    payload = payload.model_copy(update={
        "workspace_session_id": coding_workspace["id"],
    })
    steps.append({
        "step": "workflow.coding_workspace",
        "status": workspace_activation["status"],
        "workspace_session_id": coding_workspace["id"],
        "module_count": len(coding_workspace.get("modules") or {}),
        "evicted_module": workspace_activation.get("evicted_module"),
    })
    phase_done(
        "workspace_activation",
        workspace_started,
        cache_hit=bool(workspace_activation.get("cache_hit")),
        workspace_cache_hit=bool(workspace_activation.get("cache_hit")),
        write_handoff_mode="validated_contract" if validated_write_contract else "full",
    )
    if validated_write_contract:
        module_contract = dict(validated_write_contract.get("module_contract") or {})
        stored_contracts = state_store.load_session_state(client_name).get("module_contracts") or {}
        contract_record = dict(stored_contracts.get(effective_module_name) or {})
        contract_record.setdefault("status", "pending")
        contract_record.setdefault("requirement_sources", [payload.task_description])
    else:
        module_contract, contract_record = _persist_module_contract(
            state_store,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            current_contract=current_contract,
            task_description=effective_task_description,
        )
    adaptive_profile = _adaptive_workflow_profile(
        payload,
        module_contract,
        [
            *payload.target_files,
            *(write.relative_path for write in payload.writes),
        ],
    )
    persisted_requirements = "\n\n--- Additional requirement ---\n\n".join(contract_record.get("requirement_sources") or [])
    analysis_started = time.perf_counter()
    phase_started("validation_and_index")
    cached_workspace_analysis = workspace_module_context.get("analysis_index")
    cached_validator_findings = (
        cached_workspace_analysis.get("validator_findings")
        if isinstance(cached_workspace_analysis, dict)
        else None
    )
    if validated_write_contract:
        reusable_validator_findings = list(validated_write_contract.get("validator_findings") or [])
        structure_findings = [
            finding for finding in reusable_validator_findings
            if str(finding.get("severity") or "") == "error"
        ]
        steps.append({"step": "workflow.module_validator", "status": "validated_write_contract"})
    elif coding_session_resumed and isinstance(cached_validator_findings, list):
        reusable_validator_findings = list(cached_validator_findings)
        structure_findings = [
            finding for finding in reusable_validator_findings
            if str(finding.get("severity") or "") == "error"
        ]
        steps.append({"step": "workflow.module_validator", "status": "workspace_cache"})
    else:
        baseline_findings = OdooModuleValidator().validate(module_dir, module_contract=module_contract)
        reusable_validator_findings = [
            finding.model_dump(mode="json")
            for finding in baseline_findings
            if finding.severity.value in {"error", "warning"}
        ]
        structure_findings = [
            finding.model_dump(mode="json")
            for finding in baseline_findings
            if finding.severity.value == "error"
        ]
        steps.append({"step": "workflow.module_validator", "status": "built"})
    manifest_rel = module.manifest_path
    manifest_abs = project / manifest_rel
    steps.append({"step": "repo.list_custom_modules", "status": "ok", "module": effective_module_name, "module_path": module.path, "module_resolution": module_resolution})

    # For ordinary inspect-then-write requests, reuse the same local architecture
    # cache as workflow.module_analyze. Explicit writes/targets already provide a
    # faster deterministic path and do not need a whole-module index.
    analysis_index: dict[str, Any] | None = None
    analysis_source_cache: dict[str, dict[str, Any]] = {}
    if not payload.writes and not payload.target_files:
        if isinstance(cached_workspace_analysis, dict):
            analysis_index = dict(cached_workspace_analysis)
            analysis_index["cache_hit"] = True
            analysis_index["analysis_goal"] = payload.task_description
            analysis_status = "workspace_cache"
        else:
            analysis_index = ModuleAnalysisService().analyze(
                project=project,
                module_path=module_dir,
                module_name=effective_module_name,
                analysis_goal=payload.task_description,
                max_files=adaptive_profile["inspection_file_limit"],
                max_total_chars={"small": 12000, "medium": 18000, "large": 25000}[adaptive_profile["tier"]],
                validator_findings=reusable_validator_findings,
            )
            coding_workspace = coding_workspaces.store_analysis(
                str(coding_workspace["id"]),
                module_name=effective_module_name,
                analysis_index=analysis_index,
            )
            analysis_status = "cached" if analysis_index.get("cache_hit") else "built"
        analysis_source_cache = {
            f"{module.path.rstrip('/')}/{str(item.get('path') or '').lstrip('/')}": item
            for item in analysis_index.get("source_evidence") or []
            if item.get("path")
        }
        steps.append({
            "step": "workflow.module_analyze.index",
            "status": analysis_status,
            "files": analysis_index.get("inventory", {}).get("returned_files", 0),
            "git_sha": analysis_index.get("git_sha"),
        })
    phase_done(
        "validation_and_index",
        analysis_started,
        cache_hit=bool(
            workspace_activation.get("cache_hit")
            or (analysis_index and analysis_index.get("cache_hit"))
        ),
        analysis_cache_hit=bool(analysis_index and analysis_index.get("cache_hit")),
    )
    inspection_started = time.perf_counter()
    phase_started(
        "focused_inspection",
        write_handoff_mode="validated_contract" if validated_write_contract else "full",
    )

    # 3) Read custom manifest + target/custom files.  This also records gate evidence.
    custom_rel_paths: list[str] = [manifest_rel]
    if not validated_write_contract:
        for raw in payload.target_files:
            rel = _safe_module_relative(effective_module_name, raw, module_root_rel)
            if rel not in custom_rel_paths:
                custom_rel_paths.append(rel)
    for write in payload.writes:
        rel = _safe_module_relative(effective_module_name, write.relative_path, module_root_rel)
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)

    # Inspect/autopatch mode uses exact-first focused search to avoid huge noisy reads.
    search_results: list[dict[str, Any]] = []
    focused_match_lines: dict[str, list[int]] = {}
    direct_targets: list[str] = []
    if not payload.writes:
        phase_started("focused_target_search")
        patterns = _infer_code_change_search_patterns(payload)
        focused_paths: list[str] = []
        direct_targets = list(dict.fromkeys(
            _safe_module_relative(effective_module_name, raw, module_root_rel)
            for raw in payload.target_files
            if str(raw or "").strip()
        ))
        if not direct_targets:
            direct_targets = _new_model_scaffold_target_paths(project, module.path, payload)
            if direct_targets:
                search_results.append({
                    "engine": "new_model_scaffold",
                    "status": "ok",
                    "target_count": len(direct_targets),
                    "paths": direct_targets,
                })
                for rel in direct_targets:
                    if rel not in custom_rel_paths:
                        custom_rel_paths.append(rel)
        if direct_targets:
            search_results.append({"engine": "direct_target", "status": "ok", "target_count": len(direct_targets)})
            for target_rel in direct_targets[:50]:
                if not (project / target_rel).is_file():
                    search_results.append({"engine": "direct_target", "status": "missing", "path": target_rel})
                    continue
                if target_rel not in focused_paths:
                    focused_paths.append(target_rel)
                if not patterns:
                    continue
                target_results, _, target_lines = _focused_search(
                    repo,
                    project_path,
                    target_rel,
                    patterns,
                    payload.max_search_matches,
                    _normalized_task_type(payload),
                )
                search_results.extend(target_results)
                for rel, lines in target_lines.items():
                    for line in lines:
                        if line not in focused_match_lines.setdefault(rel, []):
                            focused_match_lines[rel].append(line)

        if not focused_paths:
            cached_targets = _analysis_index_target_paths(analysis_index, payload)
            cached_targets = _exact_model_target_cluster(
                project,
                module.path,
                payload,
                cached_targets,
            )
            for module_rel in cached_targets:
                rel = f"{module.path.rstrip('/')}/{module_rel.lstrip('/')}"
                if (project / rel).is_file() and rel not in focused_paths:
                    focused_paths.append(rel)
            if focused_paths:
                search_results.append({
                    "engine": "module_analysis_cache",
                    "status": "ok",
                    "confidence": 0.9,
                    "target_count": len(focused_paths),
                    "paths": focused_paths[:8],
                })

        if not focused_paths:
            try:
                graph_search = OdooCodeSearchService().search(
                    project_path=project_path,
                    module_path=module.path,
                    task_description=payload.task_description,
                    target_hint=_target_hint(payload),
                    intent=payload.intent,
                    search_patterns=payload.search_patterns,
                    task_type=_normalized_task_type(payload),
                    max_results=min(payload.max_search_matches, 10),
                    module_fingerprint=str(workspace_module_context.get("module_fingerprint") or "") or None,
                )
                search_results.append(graph_search)
                for match in graph_search.get("matches") or []:
                    rel = str(match.get("path") or "")
                    if rel and rel not in focused_paths:
                        focused_paths.append(rel)
                    for line in match.get("lines") or []:
                        line_no = int(line or 0)
                        if line_no > 0 and line_no not in focused_match_lines.setdefault(rel, []):
                            focused_match_lines[rel].append(line_no)
            except Exception as exc:  # noqa: BLE001
                search_results.append({"engine": "odoo_code_graph", "status": "failed", "error": str(exc)})

        # Preserve the old lexical search as a bounded fallback only when the
        # Odoo graph cannot produce confident target files.
        graph_result = next((item for item in search_results if item.get("engine") in {"module_analysis_cache", "odoo_code_graph"}), {})
        discovery_ok = bool(direct_targets and focused_paths) or (
            bool(focused_paths)
            and float(graph_result.get("confidence") or 0) >= 0.45
            and not bool(graph_result.get("fallback_required"))
        )
        if not discovery_ok:
            lexical_results, lexical_paths, lexical_lines = _focused_search(
                repo,
                project_path,
                module.path,
                patterns,
                payload.max_search_matches,
                _normalized_task_type(payload),
            )
            search_results.extend(lexical_results)
            for rel in lexical_paths:
                if rel not in focused_paths:
                    focused_paths.append(rel)
            for rel, lines in lexical_lines.items():
                for line in lines:
                    if line not in focused_match_lines.setdefault(rel, []):
                        focused_match_lines[rel].append(line)
        for rel in focused_paths[: adaptive_profile["inspection_file_limit"]]:
            if rel and rel not in custom_rel_paths:
                custom_rel_paths.append(rel)
        if not direct_targets and (
            not focused_paths
            or adaptive_profile["full_module"]
            or "tests" in (module_contract.get("required_layers") or [])
        ):
            for rel in _contract_candidate_paths(project, module.path, payload, module_contract):
                if rel and rel not in custom_rel_paths:
                    custom_rel_paths.append(rel)

    generated_writes: list[OdooCodeFileChange] = []
    generated_autopatch_strategy: str | None = None
    generated_autopatch_reason: str | None = None
    if not payload.writes:
        generated_writes, generated_autopatch_strategy, generated_autopatch_reason = _build_safe_generic_autopatch(project, module.path, payload, custom_rel_paths)
        for write in generated_writes:
            rel = _safe_module_relative(effective_module_name, write.relative_path, module_root_rel)
            if rel not in custom_rel_paths:
                custom_rel_paths.append(rel)

    custom_file_paths_abs: list[str] = []
    custom_reads: list[dict[str, Any]] = []
    # One manifest plus up to 50 requested target/write paths. Keep this cap in
    # sync with OdooCodeChangeRequest so accepted writes are never left unread.
    custom_read_limit = (
        len(custom_rel_paths)
        if validated_write_contract
        else 51 if payload.writes
        else adaptive_profile["inspection_file_limit"] + 1
    )
    phase_started("custom_source_read")
    for rel in list(dict.fromkeys(custom_rel_paths))[:custom_read_limit]:
        target = repo.target(project_path, rel)
        if not target.exists() or not target.is_file():
            custom_reads.append({"relative_path": rel, "status": "missing"})
            continue
        try:
            gate.record_repo_inspection(project_path=project_path, relative_path=rel, client_name=client_name, action="workflow.odoo_code_change.read_custom", is_content_read=True)
            custom_file_paths_abs.append(str(target.resolve()))
            match_lines = sorted(focused_match_lines.get(rel, []))
            if match_lines:
                range_reads: list[dict[str, Any]] = []
                covered_until = 0
                for center in match_lines:
                    if center <= covered_until or len(range_reads) >= 3:
                        continue
                    ranged = repo.read_range(project_path, rel, start_line=max(1, center - 40), max_lines=81)
                    covered_until = int(ranged["end_line"])
                    range_reads.append(ranged)
                custom_reads.append({
                    "relative_path": rel,
                    "status": "read",
                    "read_mode": "match_range",
                    "size": target.stat().st_size,
                    "ranges": [{"start_line": item["start_line"], "end_line": item["end_line"]} for item in range_reads],
                    "total_lines": range_reads[0]["total_lines"],
                    "match_lines": match_lines[:20],
                    "preview": "\n\n... focused match window ...\n\n".join(str(item["content"]) for item in range_reads),
                })
            else:
                cached_source = analysis_source_cache.get(rel)
                if cached_source is not None:
                    custom_reads.append({
                        "relative_path": rel,
                        "status": "read",
                        "read_mode": "module_analysis_cache",
                        "size": target.stat().st_size,
                        "preview": _file_preview(str(cached_source.get("content") or ""), 3000),
                    })
                else:
                    content = _read_text_safe(target)
                    custom_reads.append({"relative_path": rel, "status": "read", "read_mode": "file_preview", "size": target.stat().st_size, "preview": _file_preview(content, 3000)})
        except UnicodeDecodeError:
            custom_reads.append({"relative_path": rel, "status": "binary_skipped"})
    steps.append({"step": "repo.read_custom", "status": "ok", "count": len([r for r in custom_reads if r.get("status") == "read"])})
    evidence_signature = _evidence_signature(project, effective_module_name, payload, custom_rel_paths)
    session_state = JsonStore().load_session_state(client_name=client_name)
    evidence_cache = (session_state.get("code_change_evidence") or {}) if isinstance(session_state, dict) else {}
    cached_evidence = evidence_cache.get(evidence_signature) if isinstance(evidence_cache, dict) else None
    if coding_session_resumed and isinstance(coding_session.get("evidence"), dict):
        session_evidence = coding_session["evidence"]
        cached_evidence = {
            "scope": session_evidence.get("scope"),
            "frontend_preflight": session_evidence.get("frontend_preflight"),
            "core_paths": list(session_evidence.get("core_paths") or []),
            "task_profile": session_evidence.get("task_profile"),
        }
        steps.append({
            "step": "workflow.inspection_evidence",
            "status": "cached_session",
            "session_id": coding_session.get("id"),
        })

    # Ensure scope has at least manifest when nothing else exists.
    if not custom_file_paths_abs and manifest_abs.exists():
        custom_file_paths_abs.append(str(manifest_abs.resolve()))

    # 4) Scope advice with corrected installed-source priority (core addons first).
    phase_started("scope_resolution")
    # Scope must follow the files actually being changed. Including every user-
    # supplied target file made focused controller fixes scan unrelated models
    # and escalate to L3/L4. Keep the manifest plus exact write files on write
    # passes; inspection mode keeps the focused search files.
    discovered_target_files = payload.target_files or direct_targets or focused_paths
    scope_file_paths, focused_scope = _focused_scope_file_paths(
        project=project,
        module_name=effective_module_name,
        module_root_rel=module_root_rel,
        custom_file_paths=custom_file_paths_abs,
        target_files=discovered_target_files,
        writes=list(payload.writes or generated_writes),
    )

    frontend_preflight = None
    frontend_paths = [
        str(Path(path).resolve().relative_to(project.resolve())).replace("\\", "/")
        for path in scope_file_paths
    ]
    write_rel_paths = [
        _safe_module_relative(effective_module_name, w.relative_path, module_root_rel)
        for w in (payload.writes or generated_writes)
    ]
    frontend_fast_path = _static_frontend_fast_path(write_rel_paths)
    exact_custom_scope = _exact_custom_targets_are_self_contained(
        project=project,
        module_name=effective_module_name,
        module_root_rel=module_root_rel,
        target_files=discovered_target_files,
        writes=list(payload.writes or generated_writes),
    ) or _new_model_scaffold_is_self_contained(
        project=project,
        module_root_rel=module_root_rel,
        payload=payload,
        target_files=[
            *discovered_target_files,
            *[str(write.relative_path) for write in (payload.writes or generated_writes)],
        ],
    )
    scope = None
    scope_payload: ScopeAdviceRequest | None = None
    if frontend_fast_path:
        steps.append({"step": "workflow.scope_advice", "status": "skipped", "reason": "static frontend fast path"})
    elif exact_custom_scope:
        from ..schemas import ScopeAdviceResult, ScopeLevel
        scope_payload = ScopeAdviceRequest(
            custom_module_name=effective_module_name,
            custom_file_paths=scope_file_paths,
            task_description=payload.task_description,
            client_name=client_name,
            focus_identifiers=_scope_focus_identifiers(payload),
        )
        scope = ScopeAdviceResult(
            custom_module=effective_module_name,
            reached_level=ScopeLevel.L0,
            instructions=[],
            mandatory_count=0,
            total_count=0,
            focus_applied=True,
            focus_identifiers=scope_payload.focus_identifiers,
            warning=(
                "Exact client-owned files are self-contained; installed-source fan-out is not required. "
                "Normal QA, registry, upgrade, and deployment verification remain mandatory."
            ),
        )
        gate.record_scope_advice(scope, scope_payload)
        steps.append({
            "step": "workflow.scope_advice",
            "status": "skipped_exact_custom_scope",
            "mandatory_count": 0,
            "reached_level": "L0",
            "focus_applied": True,
        })
    else:
        scope_payload = ScopeAdviceRequest(
            custom_module_name=effective_module_name,
            custom_file_paths=scope_file_paths,
            task_description=payload.task_description,
            client_name=client_name,
            focus_identifiers=_scope_focus_identifiers(payload),
        )
        if cached_evidence and cached_evidence.get("scope"):
            from ..schemas import ScopeAdviceResult
            scope = ScopeAdviceResult.model_validate(cached_evidence["scope"])
            steps.append({"step": "workflow.scope_advice", "status": "cached", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level), "focus_applied": scope.focus_applied})
        else:
            scope = scope_advice(scope_payload)
            steps.append({"step": "workflow.scope_advice", "status": "ok", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level), "focus_applied": scope.focus_applied})

    frontend_preflight_payload: FrontendPatternPreflightRequest | None = None
    if _looks_like_frontend_code_change(payload, frontend_paths):
        frontend_preflight_payload = FrontendPatternPreflightRequest(
            client_name=client_name,
            task_description=payload.task_description,
            target_area="auto",
            target_module_name=effective_module_name,
            custom_file_paths=custom_file_paths_abs,
            planned_file_paths=frontend_paths,
        )
        if cached_evidence and cached_evidence.get("frontend_preflight"):
            from ..schemas import FrontendPatternPreflightResult
            frontend_preflight = FrontendPatternPreflightResult.model_validate(cached_evidence["frontend_preflight"])
            steps.append({"step": "workflow.frontend_pattern_preflight", "status": "cached", "target_area": frontend_preflight.target_area, "mandatory_count": frontend_preflight.mandatory_read_count})
        else:
            frontend_preflight = frontend_pattern_preflight(frontend_preflight_payload)
            steps.append({
                "step": "workflow.frontend_pattern_preflight",
                "status": "ok",
                "target_area": frontend_preflight.target_area,
                "mandatory_count": frontend_preflight.mandatory_read_count,
            })
    else:
        frontend_preflight = None

    # 4a) Manifest dependency fallback: scope_advice should detect them, but make sure
    # common dependency manifests are read with /home/odoo/src/odoo/addons priority.
    existing_scope_paths = {item.path for item in scope.instructions} if scope is not None else set()
    extra_core_reads: list[str] = []
    fallback_dependencies = (
        []
        if frontend_fast_path or coding_session_resumed or focused_scope
        else _parse_manifest_depends(manifest_abs)
    )
    for dep in fallback_dependencies:
        if dep == effective_module_name:
            continue
        if any(
            Path(str(path)).name == "__manifest__.py"
            and Path(str(path)).parent.name == dep
            for path in existing_scope_paths
        ):
            continue
        try:
            found = find_core_file(FindCoreFileRequest(module_name=dep, file_type="manifest", keyword=dep, client_name=client_name))
            if found.best_match and found.best_match not in existing_scope_paths:
                extra_core_reads.append(found.best_match)
        except Exception:
            continue

    mandatory_paths = [item.path for item in scope.instructions if item.mandatory and not str(item.path).startswith("NOT_FOUND:")] if scope is not None else []
    mandatory_paths.extend(path for path in extra_core_reads if path not in mandatory_paths)
    if frontend_preflight is not None:
        for item in frontend_preflight.instructions:
            if item.mandatory and not str(item.path).startswith("NOT_FOUND:") and item.path not in mandatory_paths:
                mandatory_paths.append(item.path)

    # Keep native-source inspection surgical. Deterministic one-file edits need
    # at most five relevant source files; complex/two-pass changes may inspect ten.
    core_read_limit = 5 if generated_writes else 10
    ranked_mandatory_paths = _rank_core_required_paths(mandatory_paths, payload, write_rel_paths or frontend_paths, core_read_limit)
    selected_required = set(ranked_mandatory_paths)
    if scope is not None and scope_payload is not None:
        selected_scope_instructions = [
            item for item in scope.instructions
            if not item.mandatory or str(item.path).startswith("NOT_FOUND:") or item.path in selected_required
        ]
        scope = scope.model_copy(update={
            "instructions": selected_scope_instructions,
            "mandatory_count": sum(1 for item in selected_scope_instructions if item.mandatory),
            "total_count": len(selected_scope_instructions),
        })
        # scope_advice recorded the unbounded plan; replace it with the selected
        # relevance-ranked plan so the hard pre-write gate enforces the same reads.
        gate.record_scope_advice(scope, scope_payload)
    if frontend_preflight is not None and frontend_preflight_payload is not None:
        selected_frontend_instructions = [
            item for item in frontend_preflight.instructions
            if not item.mandatory or str(item.path).startswith("NOT_FOUND:") or item.path in selected_required
        ]
        frontend_preflight = frontend_preflight.model_copy(update={
            "instructions": selected_frontend_instructions,
            "mandatory_read_count": sum(1 for item in selected_frontend_instructions if item.mandatory),
        })
        gate.record_frontend_preflight(frontend_preflight, frontend_preflight_payload)

    session_core_paths = list((cached_evidence or {}).get("core_paths") or []) if coding_session_resumed else []
    core_reads: list[dict[str, Any]] = []
    phase_started("core_source_read", cache_hit=bool(session_core_paths))
    if session_core_paths:
        core_reads = [
            {"path": str(path), "status": "read", "read_mode": "coding_session_cache"}
            for path in session_core_paths[:core_read_limit]
        ]
    else:
        core_files: list[Path] = []
        core_file_seen: set[str] = set()
        directory_extras: list[Path] = []
        for raw_path in ranked_mandatory_paths:
            path = Path(str(raw_path)).expanduser()
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if resolved.is_dir():
                files = _rank_core_directory_files(resolved, payload, write_rel_paths or frontend_paths)
                if not files:
                    core_reads.append({"path": str(resolved), "status": "dir_no_readable_files"})
                    continue
                first = files[0]
                if str(first) not in core_file_seen:
                    core_file_seen.add(str(first)); core_files.append(first)
                directory_extras.extend(files[1:])
                continue
            if not resolved.exists() or not resolved.is_file():
                core_reads.append({"path": str(resolved), "status": "missing"})
                continue
            if str(resolved) not in core_file_seen:
                core_file_seen.add(str(resolved)); core_files.append(resolved)

        terms = _core_relevance_terms(payload, write_rel_paths or frontend_paths)
        suffixes = {Path(path).suffix.lower() for path in (write_rel_paths or frontend_paths) if Path(path).suffix}
        directory_extras.sort(key=lambda path: _core_path_relevance(path, terms, suffixes, _normalized_task_type(payload)))
        for file_path in directory_extras:
            if len(core_files) >= core_read_limit:
                break
            if str(file_path) not in core_file_seen:
                core_file_seen.add(str(file_path)); core_files.append(file_path)

        for resolved in core_files[:core_read_limit]:
            try:
                _ = _read_text_safe(resolved, 200_000)
                gate.record_repo_inspection(project_path=str(resolved.parent), relative_path=resolved.name, client_name=client_name, action="workflow.odoo_code_change.read_core", is_content_read=True)
                core_reads.append({"path": str(resolved), "status": "read"})
            except Exception as exc:  # noqa: BLE001
                core_reads.append({"path": str(resolved), "status": "failed", "error": str(exc)})
    steps.append({
        "step": "repo.read_core",
        "status": "cached_session" if session_core_paths else "ok",
        "count": len([r for r in core_reads if r.get("status") == "read"]),
        "limit": core_read_limit,
        "required_candidates": len(list(dict.fromkeys(mandatory_paths))),
        "selected_required": len(ranked_mandatory_paths),
        "selection": "relevance_ranked",
    })
    hybrid_custom_matches = next(
        (item.get("matches") or [] for item in search_results if item.get("engine") == "odoo_code_graph"),
        [],
    )
    hybrid_by_path = {
        str(item.get("path") or ""): item
        for item in hybrid_custom_matches
        if item.get("path")
    }
    hybrid_custom_paths = list(hybrid_by_path)
    if not hybrid_custom_paths:
        hybrid_custom_paths = [
            str(item.get("relative_path") or "")
            for item in custom_reads
            if item.get("status") == "read"
            and item.get("relative_path")
            and Path(str(item.get("relative_path"))).name not in {"__manifest__.py", "__openerp__.py"}
        ]
    search_results.append({
        "engine": "hybrid_odoo_file_finder",
        "status": "ok",
        "module_root": module_root_rel,
        "primary_files": [
            {
                "path": path,
                "ownership": "client_custom",
                "writable": True,
                "confidence": hybrid_by_path.get(path, {}).get("confidence", 1.0 if path in direct_targets else None),
                "role": hybrid_by_path.get(path, {}).get("role", "primary" if path in direct_targets else "candidate"),
            }
            for path in hybrid_custom_paths[:5]
        ],
        "installed_source_files": [
            {
                "path": str(item.get("path") or ""),
                "ownership": source_ownership(str(item.get("path") or ""), project),
                "writable": False,
            }
            for item in core_reads
            if item.get("status") == "read" and item.get("path")
        ],
        "channels": ["direct_path", "cached_module_index", "symbol_graph", "scoped_lexical", "installed_source_scope"],
        "write_policy": "Only client_custom files are writable; installed core, Enterprise, and related source are evidence only.",
    })
    cache = dict(evidence_cache) if isinstance(evidence_cache, dict) else {}
    cache[evidence_signature] = {
        "scope": scope.model_dump(mode="json") if scope is not None else None,
        "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
        "core_paths": [r.get("path") for r in core_reads if r.get("status") == "read"],
        "task_profile": _task_profile(payload, frontend_paths),
    }
    if len(cache) > 20:
        cache = dict(list(cache.items())[-20:])
    JsonStore().save_session_state({"code_change_evidence": cache}, client_name=client_name)

    phase_started("target_resolution")
    resolution_client = ClientResolverService().resolve_client(
        client_name=client_name,
        project_path=project_path,
        require_complete=False,
    )
    planned_writes = list(payload.writes or generated_writes)
    explicit_file_operation = bool(planned_writes) and all(
        bool(str(write.relative_path or "").strip()) for write in planned_writes
    ) and not (payload.target_resolution and payload.target_resolution.identifier)
    if validated_write_contract:
        target_resolution = OdooTargetResolutionResult.model_validate(
            validated_write_contract.get("target_resolution") or {}
        )
        _validate_contract_write_paths(
            project=project,
            module_root_rel=module_root_rel,
            target_resolution=target_resolution,
            writes=planned_writes,
        )
    elif payload.patch_strategy == "explicit_writes" and explicit_file_operation:
        target_resolution = OdooTargetResolutionResult(
            status="scope_verified",
            mode="existing",
            kind="file",
            identifier=effective_module_name,
            source_verified=True,
            writes_blocked=False,
            reason="Exact module-relative file operations were supplied; object-level target resolution is not required.",
            edit_policy={
                "ownership": "client_custom",
                "strategy": "direct_edit",
                "direct_edit_paths": [write.relative_path for write in planned_writes[:20]],
                "protected_source_paths": [],
                "reason": "All requested paths are constrained to the validated client custom module.",
            },
        )
    else:
        target_resolution = ExistingTargetResolutionService().resolve(
            project=project,
            module_path=module_dir,
            module_name=effective_module_name,
            task_description=effective_task_description,
            target_hint=_target_hint(payload),
            intent=payload.intent,
            task_type=_normalized_task_type(payload),
            change_request=payload.change_request,
            hint=payload.target_resolution,
            candidate_paths=[
                *[project / str(item.get("relative_path")) for item in custom_reads if item.get("status") == "read"],
                *[Path(str(item.get("path"))) for item in core_reads if item.get("status") == "read"],
            ],
            planned_paths=[write.relative_path for write in planned_writes],
            client=resolution_client,
            create_module=payload.create_module,
        )
    target_resolution_data = target_resolution.model_dump(mode="json")
    steps.append({
        "step": "workflow.resolve_existing_target",
        "status": "validated_write_contract" if validated_write_contract else target_resolution.status,
        "duration_ms": target_resolution.duration_ms,
        "cache_hit": target_resolution.cache_hit,
        "live_checked": target_resolution.live_checked,
    })
    if target_resolution.writes_blocked:
        return {
            "status": "needs_clarification",
            "message": "Focused inspection could not safely resolve the exact Odoo target. No write was applied.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "steps": steps,
            "orchestration_metrics": orchestration_metrics(),
            "target_resolution": target_resolution_data,
            "clarification_gate": {
                "required": True,
                "question": target_resolution.question,
                "reason": target_resolution.reason,
            },
            "writes_blocked": True,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    target_files=[
                        str(item.get("relative_path"))
                        for item in custom_reads
                        if item.get("status") == "read" and item.get("relative_path")
                    ],
                    preserve_writes=True,
                ),
                "required_generated_arguments": ["clarification_answer"],
            },
        }

    payload = _lock_resolved_target(payload, target_resolution)
    diagnosis_session_id = (
        payload.investigation_diagnosis.debug_session_id
        if payload.investigation_diagnosis is not None
        else None
    )
    trusted_diagnosis_evidence = (
        state_store.trusted_debug_evidence_ids(
            client_name,
            debug_session_id=diagnosis_session_id,
            module_name=effective_module_name,
            task_description=payload.task_description,
        )
        if diagnosis_session_id
        else set()
    )
    failure_guard = repeated_failure_guard_status(
        client_name=client_name,
        fingerprint=issue_fingerprint,
        task_description=payload.task_description,
        investigation_evidence=payload.investigation_evidence,
        investigation_diagnosis=payload.investigation_diagnosis,
        trusted_evidence_ids=trusted_diagnosis_evidence,
        functional_verification_passed=payload.functional_verification_passed,
    )
    if failure_guard.get("blocked"):
        debug_operation_id = _code_operation_id(
            payload,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
        )
        return {
            "status": "investigation_required",
            "state_class": "action_required",
            "message": (
                "The same user-visible issue remains after two technically successful attempts. "
                "Further writes are blocked until the execution flow and runtime state are investigated."
            ),
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "target_resolution": target_resolution_data,
            "failure_guard": failure_guard,
            "investigation_read_plan": {
                "custom_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                "native_files": [r.get("path") for r in core_reads if r.get("status") == "read"],
                "selection_rule": (
                    "Select additional exact files only from inheritance, imports, dependencies, "
                    "field definitions, method calls, templates, and runtime evidence. "
                    "Do not scan unrelated source trees."
                ),
                "required_evidence": [
                    "entry point to final output flow",
                    "framework-owned methods and computed values involved",
                    "expected runtime state compared with actual runtime state",
                    "new evidence-based failing-point hypothesis",
                ],
            },
            "success_allowed": False,
            "completion_required": True,
            "writes_blocked": True,
            "next_call": {
                "tool": "workflow.odoo_debug_and_fix",
                "arguments": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "issue_description": payload.task_description,
                    "expected_result": "The originally requested behavior passes an independent runtime verification after deployment.",
                    "operation_id": debug_operation_id,
                    "action": "start",
                },
            },
        }

    writes_to_apply = list(payload.writes or generated_writes)
    coding_session_id = payload.coding_session_id or _code_operation_id(
        payload,
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
    )
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", coding_session_id):
        coding_session_id = "codesess-" + hashlib.sha256(
            coding_session_id.encode("utf-8")
        ).hexdigest()[:24]
    write_contract = validated_write_contract or _build_validated_write_contract(
        payload=payload,
        project=project,
        module_name=effective_module_name,
        module_path=module.path,
        manifest_path=module.manifest_path,
        target_resolution=target_resolution_data,
        module_contract=module_contract,
        validator_findings=reusable_validator_findings,
        custom_reads=custom_reads,
        core_reads=core_reads,
    )
    coding_session_evidence = {
        "evidence_signature": evidence_signature,
        "scope": scope.model_dump(mode="json") if scope is not None else None,
        "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
        "core_paths": [r.get("path") for r in core_reads if r.get("status") == "read" and r.get("path")],
        "target_resolution": target_resolution_data,
        "task_profile": _task_profile(payload, frontend_paths),
        "module_contract": module_contract,
        "structure_findings": structure_findings,
        "write_contract": write_contract,
        "task_contract": write_contract.get("task_contract"),
    }
    session_target_files = [
        str(r.get("relative_path"))
        for r in custom_reads
        if r.get("status") == "read" and r.get("relative_path")
    ]
    if coding_session_resumed:
        coding_session = coding_sessions.mark(
            coding_session_id,
            "ready_to_write",
            evidence=coding_session_evidence,
            target_files=session_target_files,
            workspace_session_id=str(coding_workspace["id"]),
        )
        coding_session["valid"] = True
    else:
        coding_session = coding_sessions.open(
            session_id=coding_session_id,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            module_path=str(module_dir),
            task_description=payload.task_description,
            target_files=session_target_files,
            evidence=coding_session_evidence,
            status_value="ready_to_write",
            create_bundle=False,
            workspace_session_id=str(coding_workspace["id"]),
            snapshot_metadata=workspace_module_context,
        )
        coding_session["valid"] = True
        steps.append({
            "step": "workflow.coding_session",
            "status": "opened",
            "session_id": coding_session_id,
            "bundle_version": coding_session.get("bundle_version"),
            "base_sha": coding_session.get("base_sha"),
        })
    payload = payload.model_copy(update={"coding_session_id": coding_session_id})

    if not writes_to_apply:
        phase_done("focused_inspection", inspection_started)
        complete_write_context = _exact_write_context(
            project,
            custom_reads,
            payload=payload,
            max_chars=100_000,
            max_files=50,
        )
        write_context, inline_context_complete = _inline_write_contexts(complete_write_context)
        write_context_text = "\n".join(
            str(item.get("content") or "") for item in complete_write_context
        )
        context_artifact = None
        if not inline_context_complete:
            context_artifact = state_store.create_artifact(
                "code_change_write_context",
                "workflow.odoo_code_change",
                {
                    "coding_session_id": coding_session_id,
                    "module_name": effective_module_name,
                    "contexts": complete_write_context,
                },
            )
        required_identifiers = list(dict.fromkeys(
            [*(module_contract.get("required_methods") or []), *(module_contract.get("required_symbols") or [])]
        ))
        custom_read_metadata = [
            {key: value for key, value in item.items() if key != "preview"}
            for item in custom_reads[:50]
        ]
        clarification = _material_clarification(payload)
        if clarification:
            resolved_target_files = [
                str(r.get("relative_path"))
                for r in custom_reads
                if r.get("status") == "read" and r.get("relative_path")
            ]
            handoff = _save_code_operation_handoff(
                state_store,
                payload=payload,
                client_name=client_name,
                project_path=project_path,
                module_name=effective_module_name,
                target_files=resolved_target_files,
                status="blocked",
                current_blocker="A material product/scope choice remains unresolved.",
                next_action="Ask the returned single clarification, then continue the same operation.",
                completed_checkpoints=["client_validated", "focused_inspection", "target_resolution"],
                acceptance_evidence_remaining=[
                    "clarification_answer",
                    "complete_write_set",
                    "qa_and_core_verification",
                    "terminal_deployment_proof",
                ],
            )
            JsonStore().save_session_state({
                "pending_code_change": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "task_description": payload.task_description,
                    "task_type": _normalized_task_type(payload),
                    "intent": payload.intent,
                    "target_hint": _target_hint(payload),
                    "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                    "status": "awaiting_user_clarification",
                    "clarification": clarification,
                    "handoff": handoff,
                    "request": _code_change_continuation_arguments(
                        payload,
                        client_name=client_name,
                        project_path=project_path,
                        module_name=effective_module_name,
                        target_files=[
                            str(r.get("relative_path"))
                            for r in custom_reads
                            if r.get("status") == "read" and r.get("relative_path")
                        ],
                        preserve_writes=True,
                    ),
                }
            }, client_name=client_name)
            return {
                "status": "needs_clarification",
                "message": "Focused inspection is complete, but a material product choice remains unresolved. Ask the user one concise question before writes.",
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "coding_session": coding_sessions.public_summary(coding_session),
                "coding_workspace": coding_workspaces.public_summary(coding_workspace),
                "steps": steps,
                "workflow_profile": adaptive_profile,
                "clarification_gate": clarification,
                "handoff": handoff,
                "module_contract": module_contract,
                "contract_status": contract_record.get("status", "pending"),
                "write_generation": {"required": False, "blocked_by": "material_clarification"},
                "writes_blocked": True,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": _code_change_continuation_arguments(
                        payload,
                        client_name=client_name,
                        project_path=project_path,
                        module_name=effective_module_name,
                        target_files=[
                            str(r.get("relative_path"))
                            for r in custom_reads
                            if r.get("status") == "read" and r.get("relative_path")
                        ],
                        preserve_writes=True,
                    ),
                    "required_generated_arguments": ["clarification_answer"],
                },
            }
        # Persist unresolved implementation context so short follow-ups such as
        # "yes", "implement", "push", or "revert" can continue the latest task.
        # This is an internal continuation contract, not a user-facing result.
        resolved_target_files = [
            str(r.get("relative_path"))
            for r in custom_reads
            if r.get("status") == "read" and r.get("relative_path")
        ]
        handoff = _save_code_operation_handoff(
            state_store,
            payload=payload,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            target_files=resolved_target_files,
            status="active",
            current_blocker="Exact writes have not yet been generated from the focused source contexts.",
            next_action="Generate one complete write set from write_generation.contexts and continue this operation.",
            completed_checkpoints=["client_validated", "focused_inspection", "target_resolution"],
            acceptance_evidence_remaining=[
                "complete_write_set",
                "qa_and_core_verification",
                "terminal_deployment_proof",
            ],
        )
        JsonStore().save_session_state({
            "pending_code_change": {
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "task_description": payload.task_description,
                "task_type": _normalized_task_type(payload),
                "intent": payload.intent,
                "target_hint": _target_hint(payload),
                "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                "status": "awaiting_internal_writes",
                "coding_session_id": coding_session_id,
                "handoff": handoff,
                "request": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    target_files=[
                        str(r.get("relative_path"))
                        for r in custom_reads
                        if r.get("status") == "read" and r.get("relative_path")
                    ],
                    preserve_writes=False,
                    coding_session_id=coding_session_id,
                ),
            }
        }, client_name=client_name)
        inspection_artifact = state_store.create_artifact(
            "code_change_inspection",
            "workflow.odoo_code_change",
            {
                "custom_reads": custom_read_metadata,
                "search_results": search_results,
                "scope_advice": scope.model_dump(mode="json") if scope is not None else None,
                "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
                "core_reads": core_reads,
                "task_profile": _task_profile(payload, frontend_paths),
                "persisted_requirements": persisted_requirements,
            },
        )
        return {
            "status": "needs_writes",
            "message": "INTERNAL_CONTINUATION_REQUIRED: inspection is complete. Build a non-empty writes array from write_generation.contexts, merge it into next_call.arguments, and call workflow.odoo_code_change again. Do not execute the empty continuation or present manual edits to the user.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "steps": steps,
            "orchestration_metrics": orchestration_metrics(),
            "target_resolution": target_resolution_data,
            "task_contract": write_contract.get("task_contract"),
            "inspection_artifact": inspection_artifact,
            "task_type": _normalized_task_type(payload),
            "intent": payload.intent,
            "target_hint": _target_hint(payload),
            "patch_strategy": _normalized_patch_strategy(payload),
            "generated_autopatch_strategy": generated_autopatch_strategy,
            "workflow_profile": adaptive_profile,
            "module_contract": module_contract,
            "contract_status": contract_record.get("status", "pending"),
            "repair_required": bool(structure_findings),
            "structure_findings": structure_findings,
            "success_allowed": False,
            "completion_required": True,
            "writes_required": True,
            "write_generation": {
                "required": True,
                "edit_policy": target_resolution_data["edit_policy"],
                "contexts": write_context,
                "inline_context_complete": inline_context_complete,
                "context_artifact": context_artifact,
                "context_artifact_next_call": (
                    {
                            "tool": "artifacts_read",
                        "arguments": {
                            "artifact_id": context_artifact["artifact_id"],
                            "offset": 0,
                            "max_chars": 12000,
                        },
                        "repeat_until": "next_offset is null",
                    }
                    if context_artifact
                    else None
                ),
                "required_contract": module_contract,
                "identifier_coverage": {
                    "required": required_identifiers,
                    "present_in_context": [value for value in required_identifiers if value in write_context_text],
                    "not_in_returned_context": [value for value in required_identifiers if value not in write_context_text],
                },
                "suggested_write_targets": [
                    item.get("relative_path") for item in custom_read_metadata
                    if item.get("status") == "missing"
                ],
                "output_schema": _write_generation_output_schema(
                    has_missing_targets=any(
                        item.get("status") == "missing"
                        for item in custom_read_metadata
                    ),
                ),
                "rules": [
                    "Generate one complete write set for every required contract item before making next_call.",
                    "When context_artifact is present, read it completely through artifacts_read before generating writes; do not restart inspection.",
                    _target_edit_policy_rule(target_resolution.edit_policy.strategy),
                    "For every missing/new target, use content with the complete file; search/replace is valid only for an existing file returned with status=read.",
                    "Parent directories for content writes are created automatically; never ask the user to pre-create models, views, static, or tests directories.",
                    "Do not submit import, manifest, security, or scaffold-only writes when functional identifiers or tests remain missing.",
                    "Prefer literal search/replace with count=1 from a complete source unit.",
                    "Preserve unrelated code and do not rewrite omitted source.",
                ],
            },
            "handoff": handoff,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    target_files=[
                        str(r.get("relative_path"))
                        for r in custom_reads
                        if r.get("status") == "read" and r.get("relative_path")
                    ],
                    preserve_writes=False,
                    coding_session_id=coding_session_id,
                ),
                "required_generated_arguments": ["writes"],
            },
        }

    phase_done("focused_inspection", inspection_started)
    write_started = time.perf_counter()
    phase_started("write_and_checkpoint")
    batch_record, batch_fingerprint, batch_already_staged = _prepare_code_batch(
        state_store,
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
        payload=payload,
    )
    if (
        batch_already_staged
        and batch_record is not None
        and payload.batch_index < payload.total_batches - 1
    ):
        next_index = payload.batch_index + 1
        return {
            "status": "batch_already_staged",
            "message": "This exact local batch was already verified and staged; no writes or deployment were repeated.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "workflow_profile": adaptive_profile,
            "batch": {
                "operation_id": payload.operation_id,
                "batch_index": payload.batch_index,
                "total_batches": payload.total_batches,
                "completed_batches": sorted(int(index) for index in (batch_record.get("completed") or {})),
            },
            "success_allowed": False,
            "completion_required": next_index < payload.total_batches,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    preserve_writes=False,
                    batch_index=next_index,
                ),
                "required_generated_arguments": ["writes"],
            } if next_index < payload.total_batches else None,
        }

    # 5) Resolve the complete write set in memory, then apply it through one
    # guarded rollback-safe batch transaction.
    final_batch_replay = bool(batch_already_staged and batch_record is not None)
    if final_batch_replay and batch_record.get("deploy_job_id"):
        job_id = str(batch_record["deploy_job_id"])
        return {
            "status": "deploy_already_started",
            "message": "The final batch and deploy checkpoint already exist; no writes, commit, push, or deployment were repeated.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "workflow_profile": adaptive_profile,
            "job_id": job_id,
            "completion_required": True,
            "success_allowed": False,
            "next_call": {
                "tool": "jobs.get",
                "arguments": {
                    "job_id": job_id,
                    "wait_for_result": True,
                    "wait_timeout_seconds": 600,
                    "poll_interval_seconds": 1.0,
                },
            },
        }
    written_files_abs: list[str] = [
        str((project / path).resolve())
        for path in ((batch_record or {}).get("staged_paths") or [])
        if (project / path).resolve().is_relative_to(project.resolve())
    ] if final_batch_replay else []
    write_results: list[dict[str, Any]] = (
        [{"status": "skipped", "reason": "final_batch_already_staged"}]
        if final_batch_replay
        else []
    )
    writes_for_this_call = [] if final_batch_replay else writes_to_apply
    prepared_write_items: list[RepoWriteItem] = []
    prepared_write_meta: list[dict[str, Any]] = []
    delete_rel_paths: list[str] = []
    for change in writes_for_this_call:
        rel = _safe_module_relative(effective_module_name, change.relative_path, module_root_rel)
        application_mode = "delete" if change.operation == "delete" else "full_content"
        if change.operation == "delete":
            delete_rel_paths.append(rel)
            continue
        if change.content is not None and (change.search is not None or change.replace is not None):
            raise HTTPException(status_code=400, detail=f"Provide either content or search/replace, not both, for {rel}")
        if change.content is not None:
            new_content = change.content
        else:
            if change.search is None or change.replace is None:
                raise HTTPException(status_code=400, detail=f"Missing content or search+replace for {rel}")
            target = project / rel
            if not target.is_file():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "new_file_requires_content",
                        "message": (
                            f"Cannot apply search/replace to missing file {rel}. "
                            "Resend this write with content containing the complete file. "
                            "Its parent directories will be created automatically."
                        ),
                        "relative_path": rel,
                        "required_write": {
                            "relative_path": rel,
                            "content": "Complete production-ready file content",
                        },
                    },
                )
            old = target.read_text(encoding="utf-8")
            if change.regex:
                new_content, replaced = re.subn(change.search, change.replace, old, count=change.count or 0)
                application_mode = "regex"
            else:
                replaced = old.count(change.search) if change.count == 0 else min(old.count(change.search), change.count)
                new_content = old.replace(change.search, change.replace, change.count or -1)
                application_mode = "literal"
                if replaced <= 0 and target.suffix.lower() == ".py" and change.count in {0, 1}:
                    rebased, rebase_reason = _safe_unique_python_function_rebase(
                        old,
                        change.search,
                        change.replace,
                    )
                    if rebased is not None:
                        new_content = rebased
                        replaced = 1
                        application_mode = "rebased_unique_python_function"
            if replaced <= 0:
                detail = f"Search text not found in {rel}"
                if not change.regex and target.suffix.lower() == ".py" and change.count in {0, 1}:
                    detail += f"; safe function refresh was not applicable: {rebase_reason}"
                raise HTTPException(status_code=409, detail=detail)
        prepared_write_items.append(RepoWriteItem(
            relative_path=rel,
            content=new_content,
            create_dirs=True,
            mode=change.mode,
            force=change.force,
        ))
        prepared_write_meta.append({
            "relative_path": rel,
            "application_mode": application_mode,
        })

    batch_strategy = "none"
    if prepared_write_items:
        batch_result = repo_write_many_smart(RepoWriteManySmartRequest(
            project_path=project_path,
            client_name=client_name,
            files=prepared_write_items,
            delete_paths=delete_rel_paths,
            strategy="auto",
            timeout_seconds=180,
        ))
        if batch_result.exit_code != 0:
            raise HTTPException(
                status_code=409,
                detail=batch_result.stderr or batch_result.stdout or "Batch write failed and was rolled back.",
            )
        batch_strategy = "fast_safe" if "SMART_STRATEGY=fast_safe" in batch_result.stdout else "atomic"
        for metadata in prepared_write_meta:
            rel = str(metadata["relative_path"])
            target = project / rel
            written_abs = str(target.resolve())
            if written_abs not in written_files_abs:
                written_files_abs.append(written_abs)
            write_results.append({
                **metadata,
                "status": "written",
                "size": target.stat().st_size if target.is_file() else 0,
                "batch_strategy": batch_strategy,
            })
        auto_match = re.search(r"auto_wired_init_files=(\[[^\n]*\])", batch_result.stdout or "")
        if auto_match:
            try:
                auto_paths = ast.literal_eval(auto_match.group(1))
            except (SyntaxError, ValueError):
                auto_paths = []
            for auto_rel in auto_paths if isinstance(auto_paths, list) else []:
                auto_abs = str((project / str(auto_rel)).resolve())
                if auto_abs not in written_files_abs and Path(auto_abs).is_file():
                    written_files_abs.append(auto_abs)
                    write_results.append({
                        "relative_path": str(auto_rel),
                        "status": "auto_wired",
                        "size": Path(auto_abs).stat().st_size,
                        "batch_strategy": batch_strategy,
                    })

        for rel in delete_rel_paths:
            if (project / rel).exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"Batch reported success but delete target still exists: {rel}",
                )
            write_results.append({
                "relative_path": rel,
                "status": "deleted",
                "application_mode": "delete",
                "batch_strategy": batch_strategy,
            })
    else:
        # A delete-only request has no file payload for the public batch schema;
        # each delete remains protected by its existing gate and audit path.
        for rel in delete_rel_paths:
            target = project / rel
            if not target.exists():
                write_results.append({"relative_path": rel, "status": "already_absent", "application_mode": "delete"})
                continue
            result = repo_delete(RepoDeleteRequest(project_path=project_path, client_name=client_name, relative_path=rel))
            write_results.append({"relative_path": rel, "status": result.get("status", "deleted"), "application_mode": "delete"})

    if writes_for_this_call:
        steps.append({
            "step": "repo.write_batch",
            "status": "ok",
            "batch_strategy": batch_strategy,
            "completed_writes": len(writes_for_this_call),
            "total_writes": len(writes_for_this_call),
            "lock_transactions": 1 if prepared_write_items else len(delete_rel_paths),
            "deploy_started": False,
        })
    steps.append({
        "step": "repo.write",
        "status": "replayed" if final_batch_replay else "ok",
        "written_count": 0 if final_batch_replay else len(written_files_abs),
    })
    phase_done("write_and_checkpoint", write_started)
    if writes_for_this_call:
        session_targets = [
            _safe_module_relative(effective_module_name, change.relative_path, module_root_rel)
            for change in writes_for_this_call
        ]
        coding_workspace = coding_workspaces.refresh_module(
            str(coding_workspace["id"]),
            module_name=effective_module_name,
            status_value="refreshed_after_write",
        )
        refreshed_workspace_module = dict(
            (coding_workspace.get("modules") or {}).get(effective_module_name) or {}
        )
        coding_session = coding_sessions.refresh(
            coding_session_id,
            status_value="written",
            target_files=session_targets,
            evidence=coding_session_evidence,
            create_bundle=False,
            snapshot_metadata=refreshed_workspace_module,
        )
        steps.append({
            "step": "workflow.coding_session_checkpoint",
            "status": "workspace_snapshot_reused",
            "session_id": coding_session_id,
            "bundle_version": coding_session.get("bundle_version"),
        })
        steps.append({
            "step": "workflow.coding_workspace_checkpoint",
            "status": "refreshed_after_write",
            "workspace_session_id": coding_workspace["id"],
            "module_name": effective_module_name,
        })

    if batch_record is not None and payload.batch_index < payload.total_batches - 1:
        batch_verify = verify_against_core(SyntaxVerifyRequest(
            written_files=written_files_abs,
            project_path=project_path,
            client_name=client_name,
            fix_cycle=0,
        ))
        steps.append({
            "step": "workflow.verify_batch",
            "status": batch_verify.status,
            "push_blocked": batch_verify.push_blocked,
            "critical_count": batch_verify.critical_count,
        })
        if batch_verify.push_blocked:
            return {
                "status": "batch_verification_failed",
                "message": "The local batch was written but failed changed-file verification. Repair this same batch before continuing; nothing was committed or deployed.",
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "coding_session": coding_sessions.public_summary(coding_session),
                "coding_workspace": coding_workspaces.public_summary(coding_workspace),
                "workflow_profile": adaptive_profile,
                "steps": steps,
                "write_results": write_results,
                "verify_against_core": batch_verify.model_dump(mode="json"),
                "batch": {
                    "operation_id": payload.operation_id,
                    "batch_index": payload.batch_index,
                    "total_batches": payload.total_batches,
                    "deployed": False,
                },
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": _code_change_continuation_arguments(
                        payload,
                        client_name=client_name,
                        project_path=project_path,
                        module_name=effective_module_name,
                        preserve_writes=False,
                        batch_index=payload.batch_index,
                    ),
                    "required_generated_arguments": ["writes"],
                },
            }
        completed_batch = _complete_code_batch(
            state_store,
            client_name=client_name,
            record=batch_record,
            batch_index=payload.batch_index,
            fingerprint=str(batch_fingerprint),
            written_paths=[
                Path(path).resolve().relative_to(project.resolve()).as_posix()
                for path in written_files_abs
            ],
        )
        next_index = payload.batch_index + 1
        return {
            "status": "batch_staged",
            "message": "Local batch passed changed-file verification. No commit, push, deployment, module upgrade, or full-module QA was started.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "workflow_profile": adaptive_profile,
            "steps": steps,
            "target_resolution": target_resolution_data,
            "write_results": write_results,
            "verify_against_core": batch_verify.model_dump(mode="json"),
            "batch": {
                "operation_id": payload.operation_id,
                "batch_index": payload.batch_index,
                "next_batch_index": next_index,
                "total_batches": payload.total_batches,
                "completed_batches": sorted(int(index) for index in completed_batch["completed"]),
                "staged_path_count": len(completed_batch.get("staged_paths") or []),
                "deployed": False,
            },
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    preserve_writes=False,
                    batch_index=next_index,
                ),
                "required_generated_arguments": ["writes"],
            },
        }

    # 6) Diff, QA, verify against core before deploy.
    diff = GitService().diff(project_path, staged=False, timeout_seconds=60)
    dirty_module_paths = _git_status_changed_module_files(project_path, module.path)
    deploy_paths = list(dict.fromkeys(
        [str(Path(path).resolve().relative_to(project.resolve()).as_posix()) for path in written_files_abs]
        + dirty_module_paths
    ))
    verify_files_abs = [
        str((project / path).resolve())
        for path in deploy_paths
        if (project / path).resolve().is_relative_to(project.resolve())
    ]
    try:
        deploy_impact = classify_deploy_impact(
            project_path=project_path,
            module_name=effective_module_name,
            changed_paths=deploy_paths,
            git_diff=diff.stdout,
            deploy_mode=payload.deploy_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    verification_started = time.perf_counter()
    phase_started("qa_and_core_verification")
    qa = self_run(SelfRunRequest(
        project_path=project_path,
        client_name=client_name,
        module_name=effective_module_name,
        module_root_rel=module_root_rel,
        task_description=(persisted_requirements or payload.task_description)[-20000:],
        create_module=payload.create_module,
        module_contract=module_contract,
        run_pytest="tests" in (module_contract.get("required_layers") or []),
        include_git_status=True,
    ))
    verify = verify_against_core(SyntaxVerifyRequest(
        written_files=verify_files_abs,
        project_path=project_path,
        client_name=client_name,
        fix_cycle=0,
    ))
    qa_errors = [item for item in qa.findings if item.severity.value == "error"]
    strict_module_validation = bool(payload.create_module or current_contract.get("full_module_requested"))
    blocking_qa_errors, preexisting_qa_errors = _partition_surgical_qa_errors(
        structure_findings,
        qa_errors,
        strict_module_validation=strict_module_validation,
    )
    qa_struct = qa.model_dump(mode="json")
    qa_struct["module_wide_status"] = qa.status
    qa_struct["status"] = "failed" if blocking_qa_errors else "warning" if preexisting_qa_errors else qa.status
    qa_struct["scope_gate"] = {
        "mode": "strict_module" if strict_module_validation else "baseline_delta",
        "blocking_error_count": len(blocking_qa_errors),
        "preexisting_nonblocking_count": len(preexisting_qa_errors),
        "preexisting_nonblocking_findings": [item.model_dump(mode="json") for item in preexisting_qa_errors],
    }
    steps.append({"step": "git.diff", "status": "ok" if diff.exit_code == 0 else "failed", "exit_code": diff.exit_code})
    steps.append({
        "step": "qa.self_run",
        "status": "failed" if blocking_qa_errors else "passed_with_preexisting_findings" if preexisting_qa_errors else qa.status,
        "mode": "strict_module" if strict_module_validation else "baseline_delta",
        "blocking_errors": len(blocking_qa_errors),
        "preexisting_nonblocking": len(preexisting_qa_errors),
    })
    force_authorization = _resolve_force_override_authorization(
        state_store,
        payload,
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
        git_diff=diff.stdout,
        verify=verify,
    )
    override_decision = _verification_override_decision(
        payload,
        verify,
        force_authorization=force_authorization,
    )
    verification_effectively_blocked = bool(verify.push_blocked and not override_decision["accepted"])
    steps.append({
        "step": "workflow.verify_against_core",
        "status": verify.status,
        "push_blocked": verify.push_blocked,
        "critical_count": verify.critical_count,
        "override": override_decision,
    })
    if verify.push_blocked and override_decision["accepted"]:
        steps.append({
            "step": "workflow.verification_override",
            "status": "accepted",
            "mode": override_decision["mode"],
            "critical_count": override_decision["critical_count"],
            "reason": override_decision["reason"],
            "authorization_source": override_decision.get("authorization_source"),
            "authorization_case_id": override_decision.get("authorization_case_id"),
        })
    phase_done("qa_and_core_verification", verification_started)

    if (
        verify.push_blocked
        and override_decision["mode"] == "force"
        and not override_decision["accepted"]
        and not blocking_qa_errors
    ):
        continuation = _code_change_continuation_arguments(
            payload,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            target_files=deploy_paths,
            preserve_writes=False,
            batch_index=payload.batch_index if batch_record is not None else None,
        )
        continuation.pop("verification_override_confirmation", None)
        return {
            "status": "verification_authorization_required",
            "message": (
                "Force verification override requires one explicit confirmation for this exact "
                "diff and verifier finding set. After confirmation, the server will reuse the "
                "case-scoped grant for all continuations of the same deployment."
            ),
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "git_diff": {"exit_code": diff.exit_code, "stdout": diff.stdout[-12000:], "stderr": diff.stderr[-4000:]},
            "qa": qa_struct,
            "verify_against_core": verify.model_dump(mode="json"),
            "verification_override": override_decision,
            "confirmation_phrase": "I AUTHORIZE FORCE PUSH AND DEPLOY",
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": continuation,
                "required_generated_arguments": ["verification_override_confirmation"],
            },
        }

    if blocking_qa_errors or verification_effectively_blocked:
        core_errors = [item for item in verify.findings if item.severity.value == "CRITICAL"] if verification_effectively_blocked else []
        repair_findings = (
            [item.model_dump(mode="json") for item in blocking_qa_errors]
            + [item.model_dump(mode="json") for item in core_errors]
        )
        repair_record = _update_module_contract_status(
            state_store,
            client_name=client_name,
            module_name=effective_module_name,
            status="pending",
            increment_repair=True,
        )
        repair_attempts = int(repair_record.get("repair_attempts") or 0)
        repair_targets = list(dict.fromkeys(
            [str(item.file) for item in blocking_qa_errors if item.file]
            + [str(item.file) for item in core_errors if item.file]
            + [str(r.get("relative_path")) for r in custom_reads if r.get("status") == "read" and r.get("relative_path")]
        ))
        if (coding_session.get("bundle") or {}).get("sha256"):
            coding_session = coding_sessions.mark(
                coding_session_id,
                "repair_required",
                target_files=repair_targets,
                repair_attempts=repair_attempts,
            )
        else:
            coding_session = coding_sessions.refresh(
                coding_session_id,
                status_value="repair_required",
                target_files=repair_targets,
                evidence=coding_session_evidence,
            )
            coding_session = coding_sessions.mark(
                coding_session_id,
                "repair_required",
                repair_attempts=repair_attempts,
            )
        return {
            "status": "internal_repair_required",
            "message": "INTERNAL_CONTINUATION_REQUIRED: a newly introduced or task-required verification error remains. Apply only the returned blocking findings and immediately rerun workflow.odoo_code_change; do not stop, summarize, or ask the user to repair files.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "steps": steps,
            "target_resolution": target_resolution_data,
            "write_results": write_results,
            "git_diff": {"exit_code": diff.exit_code, "stdout": diff.stdout[-12000:], "stderr": diff.stderr[-4000:]},
            "qa": qa_struct,
            "verify_against_core": verify.model_dump(mode="json"),
            "module_contract": module_contract,
            "workflow_profile": adaptive_profile,
            "persisted_requirements": persisted_requirements,
            "contract_status": "pending",
            "repair_required": True,
            "repair_attempts": repair_attempts,
            "max_repair_attempts": 3,
            "terminal": False,
            "must_continue": repair_attempts <= 3,
            "summary_allowed": False,
            "tool_exit_allowed": False,
            "blocking_scope": "new_task_required_or_integrity_only",
            "structure_findings": repair_findings,
            "module_structure_complete": False,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": _code_change_continuation_arguments(
                    payload,
                    client_name=client_name,
                    project_path=project_path,
                    module_name=effective_module_name,
                    target_files=repair_targets,
                    preserve_writes=False,
                    batch_index=payload.batch_index if batch_record is not None else None,
                ),
                "required_generated_arguments": ["writes"],
            } if repair_attempts <= 3 else None,
        }

    if batch_record is not None:
        _complete_code_batch(
            state_store,
            client_name=client_name,
            record=batch_record,
            batch_index=payload.batch_index,
            fingerprint=str(batch_fingerprint),
            written_paths=deploy_paths,
        )

    _update_module_contract_status(
        state_store,
        client_name=client_name,
        module_name=effective_module_name,
        status="fulfilled",
    )

    if not payload.auto_deploy:
        coding_session = coding_sessions.mark(coding_session_id, "ready_to_deploy")
        handoff = _save_code_operation_handoff(
            state_store,
            payload=payload,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            target_files=deploy_paths,
            status="active",
            current_blocker="Deployment was intentionally not started because auto_deploy=false.",
            next_action="Continue this operation with auto_deploy=true when deployment is authorized.",
            completed_checkpoints=["writes_applied", "qa_passed", "core_verification_passed"],
            acceptance_evidence_remaining=["terminal_deployment_proof"],
        )
        return {
            "status": "ready_to_deploy",
            "state_class": "input_required",
            "message": "Requested change passed scoped verification. auto_deploy=false, so no deploy job was started.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "steps": steps,
            "orchestration_metrics": orchestration_metrics(),
            "write_results": write_results,
            "qa": qa_struct,
            "verify_against_core": verify.model_dump(mode="json"),
            "module_structure_complete": not preexisting_qa_errors,
            "surgical_scope_verified": True,
            "module_contract": module_contract,
            "workflow_profile": adaptive_profile,
            "contract_status": "fulfilled",
            "handoff": handoff,
            "success_allowed": False,
            "completion_required": True,
            "input_required": True,
            "input_request": {
                "kind": "authorization",
                "field": "auto_deploy",
                "prompt": "Authorize deployment of the already verified change.",
                "required_value": True,
            },
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": {
                    **payload.model_dump(mode="json", exclude_none=True),
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "coding_session_id": coding_session_id,
                    "auto_deploy": True,
                },
                "required_generated_arguments": ["auto_deploy"],
            },
        }

    commit_message = payload.commit_message or f"Update {effective_module_name}: {payload.task_description[:180]}"
    verification_proof_id = VerificationProofService().create(
        project=project,
        module_name=effective_module_name,
        module_root_rel=module_root_rel,
        paths=deploy_paths,
        verifier_result=verify.model_dump(mode="json"),
        accepted=bool(not verify.push_blocked or override_decision["accepted"]),
    )
    deploy_payload = SafeDeployModuleRequest(
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
        module_root_rel=module_root_rel,
        commit_message=commit_message,
        paths=deploy_paths,
        run_qa=False,
        verification_proof_id=verification_proof_id,
        wait_for_result=False,
        wait_timeout_seconds=0,
        upgrade_module=deploy_impact.upgrade_required,
        deploy_impact=deploy_impact.impact,
        upgrade_reasons=list(deploy_impact.reasons),
        verification_assertion=payload.verification_assertion,
        verification_overridden=bool(verify.push_blocked and override_decision["accepted"]),
        verification_override_mode=override_decision["mode"] if override_decision["accepted"] else "strict",
        verification_override_reason=override_decision["reason"] if override_decision["accepted"] else None,
        verification_override_findings=override_decision["findings"] if override_decision["accepted"] else [],
        force_new_deploy=payload.force_new_deploy,
    )
    try:
        coding_session = coding_sessions.mark(coding_session_id, "deploying")
        phase_started("deploy_handoff")
        deploy = await safe_deploy_module(deploy_payload)
        deploy_struct = deploy.model_dump(mode="json") if hasattr(deploy, "model_dump") else deploy
    except HTTPException as exc:
        deploy_detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        coding_session = coding_sessions.mark(
            coding_session_id,
            "deploy_blocked",
            deploy_blocker=str(deploy_detail.get("message") or deploy_detail)[:3000],
        )
        handoff = _save_code_operation_handoff(
            state_store,
            payload=payload,
            client_name=client_name,
            project_path=project_path,
            module_name=effective_module_name,
            target_files=deploy_paths,
            status="blocked",
            current_blocker=str(deploy_detail.get("message") or deploy_detail),
            next_action="Resolve the deploy-validation blocker, then resume the same operation without repeating writes or QA.",
            completed_checkpoints=["writes_applied", "qa_passed", "core_verification_passed"],
            acceptance_evidence_remaining=["safe_deploy_job", "terminal_deployment_proof"],
        )
        return {
            "status": "deploy_blocked",
            "state_class": "action_required",
            "message": "Code was written and verified, but safe deploy was blocked by client validation.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "coding_session": coding_sessions.public_summary(coding_session),
            "coding_workspace": coding_workspaces.public_summary(coding_workspace),
            "steps": steps + [{"step": "workflow.safe_deploy_module", "status": "blocked", "detail": deploy_detail}],
            "write_results": write_results,
            "git_diff": {"exit_code": diff.exit_code, "stdout_preview": diff.stdout[-12000:], "stderr_preview": diff.stderr[-4000:]},
            "qa": qa_struct,
            "verify_against_core": verify.model_dump(mode="json"),
            "deploy_validation": deploy_detail,
            "handoff": handoff,
            "success_allowed": False,
            "completion_required": True,
            "source_deployed": False,
            "module_upgraded": False,
            "visible_claim_allowed": False,
            "allowed_summary": "Code was written and verified, but not pushed/deployed/upgraded. Fix the deploy validation blocker, then rerun with auto_deploy=true.",
            "next_call": {
                "tool": "client.check",
                "arguments": {
                    "client_name": client_name,
                    "validate": True,
                    "force_refresh": True,
                },
            },
            "resume_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": {
                    **payload.model_dump(mode="json", exclude_none=True),
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "coding_session_id": coding_session_id,
                    "auto_deploy": True,
                },
            },
        }
    job_id = deploy_struct.get("job_id") if isinstance(deploy_struct, dict) else None
    coding_session = coding_sessions.mark(coding_session_id, "deploying", deploy_job_id=job_id)
    if batch_record is not None:
        _record_code_batch_deploy(
            state_store,
            client_name=client_name,
            record=batch_record,
            job_id=job_id,
        )
    handoff = _save_code_operation_handoff(
        state_store,
        payload=payload,
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
        target_files=deploy_paths,
        status="active",
        current_blocker=None,
        next_action=f"Poll the owned safe-deploy job {job_id} to a terminal result.",
        completed_checkpoints=[
            "writes_applied",
            "qa_passed",
            "core_verification_passed",
            "safe_deploy_started",
        ],
        acceptance_evidence_remaining=["terminal_deployment_proof"],
    )
    if job_id and hasattr(state_store, "bind_operation_handoff_to_job"):
        try:
            state_store.bind_operation_handoff_to_job(
                str(job_id),
                str(handoff["operation_id"]),
                client_name=client_name,
            )
        except Exception:
            # The deploy job remains authoritative when optional recovery
            # metadata cannot be attached.
            pass
    record_code_change_deploy_attempt(client_name=client_name, fingerprint=issue_fingerprint, job_id=job_id)
    frontend_touched = any(str(path).endswith((".xml", ".scss", ".css", ".js")) for path in deploy_paths)
    return {
        "status": "deploy_started" if deploy_struct.get("status") in {"running", "already_running", "already_in_progress", "resumable_existing_job"} else deploy_struct.get("status", "deploy_started"),
        "message": "Code change workflow completed through verify phase; safe deploy is server-owned. Poll jobs.get until terminal result before final answer.",
        "client_name": client_name,
        "project_path": project_path,
        "module_name": effective_module_name,
        "coding_session": coding_sessions.public_summary(coding_session),
        "coding_workspace": coding_workspaces.public_summary(coding_workspace),
        "steps": steps,
        "orchestration_metrics": orchestration_metrics(),
        "target_resolution": target_resolution_data,
        "write_results": write_results,
        "generated_autopatch": bool(generated_writes and not payload.writes),
        "generated_autopatch_strategy": generated_autopatch_strategy,
        "task_type": _normalized_task_type(payload),
        "intent": payload.intent,
        "target_hint": _target_hint(payload),
        "patch_strategy": _normalized_patch_strategy(payload),
        "git_diff": {"exit_code": diff.exit_code, "stdout_preview": diff.stdout[-12000:], "stderr_preview": diff.stderr[-4000:]},
        "qa": qa_struct,
        "verify_against_core": verify.model_dump(mode="json"),
        "deploy_impact": deploy_impact.as_dict(),
        "safe_deploy": deploy_struct,
        "job_id": job_id,
        "handoff": handoff,
        "completion_required": True,
        "success_allowed": False,
        "source_pushed": False,
        "source_on_odoosh": False,
        "source_deployed": False,
        "module_upgraded": False,
        "core_verified": not verify.push_blocked,
        "verification_overridden": bool(verify.push_blocked and override_decision["accepted"]),
        "verification_override": override_decision,
        "module_structure_complete": not preexisting_qa_errors,
        "surgical_scope_verified": True,
        "module_contract": module_contract,
        "workflow_profile": adaptive_profile,
        "contract_status": "fulfilled",
        "frontend_touched": frontend_touched,
        "allowed_summary": "Code was written and verified; deploy is running. Do not claim pushed/deployed/upgraded until jobs.get returns terminal passed with deploy_confirmed=true.",
        "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}} if job_id else None,
    }


_DEBUG_RUNTIME_MARKERS = (
    "error", "exception", "traceback", "failed", "failure", "broken", "runtime",
    "controller", "cron", "scheduled", "browser", "console", "network", "api",
    "webhook", "timeout", "intermittent", "access denied", "permission", "slow",
    "performance", "install", "upgrade", "registry", "500", "rpc_error",
)


def _detect_debug_surface(payload: OdooDebugAndFixRequest) -> str:
    if payload.surface != "auto":
        return payload.surface
    text = " ".join([
        payload.issue_description,
        payload.expected_result,
        *payload.reproduction_steps,
    ]).lower()
    routes = (
        ("frontend", ("owl", "javascript", " js ", "browser", "console", "network", "pos", "website", "asset")),
        ("install_upgrade", ("install", "upgrade", "registry", "parseerror", "module load")),
        ("security", ("access denied", "accesserror", "permission", "record rule", "acl")),
        ("performance", ("slow", "performance", "timeout", "query count", "n+1")),
        ("cron", ("cron", "scheduled", "batch", "queue")),
        ("controller", ("controller", "route", "http", "endpoint")),
        ("integration", ("api", "webhook", "integration", "external", "request", "response")),
    )
    for surface, markers in routes:
        if any(marker in text for marker in markers):
            return surface
    return "backend"


def _debug_requires_runtime_loop(payload: OdooDebugAndFixRequest) -> bool:
    if (
        payload.surface != "auto"
        or payload.action == "start"
        or payload.runtime_evidence
        or payload.runtime_replay
        or payload.report_replay
    ):
        return True
    text = " ".join([payload.issue_description, *payload.reproduction_steps]).lower()
    for marker in _DEBUG_RUNTIME_MARKERS:
        if marker.isalnum() and len(marker) <= 4:
            if re.search(rf"\b{re.escape(marker)}\b", text):
                return True
        elif marker in text:
            return True
    return False


def _redact_debug_text(value: str, max_chars: int = 12_000) -> str:
    """Redact common credentials/PII before persistence or model output."""
    text = str(value or "")
    patterns = (
        (r"(?i)\b(authorization\s*:\s*bearer)\s+\S+", r"\1 <redacted>"),
        (r"(?i)\b(password|passwd|token|api[_-]?key|secret)\b(\s*[:=]\s*)[^\s,;]+", r"\1\2<redacted>"),
        (r"(?i)(https?://[^/\s:@]+:)[^@\s/]+@", r"\1<redacted>@"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "<redacted-email>"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n...[{len(text) - max_chars} redacted chars omitted]...\n" + text[-half:]


def _debug_evidence_fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", " ", _redact_debug_text(value).lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _debug_operation_id(
    payload: OdooDebugAndFixRequest,
    *,
    client_name: str | None,
    project_path: str,
) -> str:
    if payload.operation_id:
        return payload.operation_id
    normalized = "|".join((
        str(client_name or ""),
        str(Path(project_path).expanduser()),
        payload.module_name.strip().lower(),
        re.sub(r"\s+", " ", payload.issue_description.strip().lower()),
        re.sub(r"\s+", " ", payload.expected_result.strip().lower()),
    ))
    return "dbgop-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _debug_writes_fingerprint(writes: list[OdooCodeFileChange]) -> str:
    payload = [item.model_dump(mode="json") for item in writes]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _debug_session_store(
    store: JsonStore,
    session: dict[str, Any],
    *,
    client_name: str | None,
) -> dict[str, Any]:
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    if hasattr(store, "save_debug_session"):
        store.save_debug_session(session, client_name=client_name)
    else:  # pragma: no cover - compatibility for third-party stores
        state = store.load_session_state(client_name=client_name)
        sessions = dict(state.get("debug_sessions") or {}) if isinstance(state, dict) else {}
        sessions[str(session["id"])] = session
        store.save_session_state({"debug_sessions": sessions}, client_name=client_name)
    if session.get("operation_id") and hasattr(store, "save_operation_handoff"):
        _persist_operation_handoff(
            store,
            _debug_operation_handoff(session),
            client_name=client_name,
        )
    return session


def _debug_session_load(
    store: JsonStore,
    session_id: str,
    *,
    client_name: str | None,
) -> dict[str, Any]:
    if hasattr(store, "get_debug_session"):
        return dict(store.get_debug_session(session_id, client_name=client_name))
    state = store.load_session_state(client_name=client_name)
    sessions = state.get("debug_sessions") if isinstance(state, dict) else None
    session = sessions.get(session_id) if isinstance(sessions, dict) else None
    if isinstance(session, dict):
        return dict(session)
    raise HTTPException(status_code=404, detail="Debug session not found for the selected client.")


def _debug_find_operation(
    store: JsonStore,
    operation_id: str,
    *,
    client_name: str | None,
) -> dict[str, Any] | None:
    if hasattr(store, "find_debug_session_by_operation"):
        return store.find_debug_session_by_operation(operation_id, client_name=client_name)
    state = store.load_session_state(client_name=client_name)
    sessions = state.get("debug_sessions") if isinstance(state, dict) else None
    if isinstance(sessions, dict):
        for session in reversed(list(sessions.values())):
            if isinstance(session, dict) and session.get("operation_id") == operation_id:
                return dict(session)
    return None


def _debug_record_evidence(
    session: dict[str, Any],
    *,
    kind: str,
    source: str,
    summary: str,
    trusted: bool,
    passed: bool | None = None,
    job_id: str | None = None,
) -> bool:
    value = _redact_debug_text(summary, 7_500)
    if not value.strip():
        return False
    fingerprint = _debug_evidence_fingerprint(f"{kind}|{source}|{passed}|{job_id}|{value}")
    ledger = list(session.get("evidence_ledger") or [])
    if any(item.get("fingerprint") == fingerprint for item in ledger if isinstance(item, dict)):
        return False
    record = OdooDebugEvidenceRecord(
        kind=kind,
        source=source,
        summary=value,
        trusted=trusted,
        passed=passed,
        fingerprint=fingerprint,
        correlation_id=session.get("correlation_id"),
        job_id=job_id,
    )
    ledger.append(record.model_dump(mode="json"))
    session["evidence_ledger"] = ledger[-50:]
    return True


def _debug_trusted_evidence_ids(session: dict[str, Any]) -> list[str]:
    return [
        str(item["id"])
        for item in session.get("evidence_ledger") or []
        if isinstance(item, dict) and item.get("trusted") is True and item.get("id")
    ]


def _debug_infer_diagnosis(session: dict[str, Any]) -> OdooFailureDiagnosis:
    """Classify only evidence visible to the connector; remain unknown otherwise."""
    evidence_text = "\n".join(
        str(item.get("summary") or "")
        for item in session.get("evidence_ledger") or []
        if isinstance(item, dict) and item.get("trusted") is True
    )
    # Origin controls whether another repair may be authorized, so classify
    # from server-trusted ledger entries only. User narrative remains useful
    # for search/reproduction but cannot be combined with unrelated evidence
    # to manufacture a custom-code diagnosis.
    text = evidence_text.lower()
    module_name = str(session.get("module_name") or "").strip().lower()
    debug_session_id = str(session.get("id") or "") or None
    trusted_ids = _debug_trusted_evidence_ids(session)[-20:]

    connector_markers = (
        "project path not found",
        "unterminated quoted string",
        "connector command",
        "checkout is missing",
        "error-pack collection failed",
        "ssh configuration",
    )
    provider_markers = (
        "rate limit",
        "too many requests",
        "provider timeout",
        "provider unavailable",
        "service unavailable",
    )
    custom_markers = (
        f"/{module_name}/" if module_name else "",
        f"addons/{module_name}/" if module_name else "",
        f"{module_name}\\" if module_name else "",
    )

    if any(marker and marker in text for marker in connector_markers):
        return OdooFailureDiagnosis(
            origin="connector_pipeline",
            crux="Connector checkout, command, SSH, or orchestration failed independently of module behavior.",
            verified_facts=["The trusted connector evidence contains a connector-pipeline failure marker."],
            hypothesis="Repair or resume the connector checkpoint without changing Odoo module code.",
            evidence_ids=trusted_ids,
            debug_session_id=debug_session_id,
            next_strategy="resume_connector",
        )
    if any(marker in text for marker in provider_markers):
        return OdooFailureDiagnosis(
            origin="provider_transport",
            crux="The external provider transport failed before module behavior could be verified.",
            verified_facts=["The trusted evidence contains a provider transport or rate-limit marker."],
            hypothesis="Retry the provider operation within a bounded policy; do not patch module code.",
            evidence_ids=trusted_ids,
            debug_session_id=debug_session_id,
            next_strategy="retry_provider_bounded",
        )
    if (
        trusted_ids
        and "traceback" in text
        and any(marker and marker in text for marker in custom_markers)
    ):
        return OdooFailureDiagnosis(
            origin="custom_code",
            crux=f"The trusted traceback enters changed or installed custom addon {module_name}.",
            verified_facts=["A trusted traceback references the selected custom addon."],
            unknowns=["The exact causal statement must be confirmed against the referenced source unit."],
            hypothesis="Inspect the earliest custom-addon frame and repair the violated runtime contract.",
            evidence_ids=trusted_ids,
            debug_session_id=debug_session_id,
            next_strategy="repair_custom_code",
        )
    return OdooFailureDiagnosis(
        origin="unknown",
        crux="Trusted evidence does not yet distinguish module code, baseline, connector, provider, or context failure.",
        verified_facts=["Evidence collection completed but no safe origin classification was proven."] if trusted_ids else [],
        unknowns=["The earliest causal boundary remains unverified."],
        hypothesis="Collect one focused primary-source observation before authorizing another repair.",
        evidence_ids=trusted_ids,
        debug_session_id=debug_session_id,
        next_strategy="collect_focused_evidence",
    )


def _debug_resolve_diagnosis(
    session: dict[str, Any],
    supplied: OdooFailureDiagnosis | dict[str, Any] | None = None,
) -> OdooFailureDiagnosis:
    inferred = _debug_infer_diagnosis(session)
    if supplied is None:
        return inferred
    candidate = OdooFailureDiagnosis.model_validate(supplied)
    trusted_ids = set(_debug_trusted_evidence_ids(session))
    if candidate.debug_session_id != session.get("id"):
        return inferred
    if not candidate.evidence_ids or not set(candidate.evidence_ids).issubset(trusted_ids):
        return inferred
    if candidate.origin != inferred.origin:
        return inferred
    if candidate.origin == "custom_code" and candidate.next_strategy != "repair_custom_code":
        return inferred
    if candidate.origin != "custom_code" and candidate.next_strategy == "repair_custom_code":
        return inferred
    return candidate


def _debug_operation_handoff(session: dict[str, Any]) -> OdooOperationHandoff:
    truth_gate = _debug_truth_gate(session)
    phase = str(session.get("phase") or "diagnosing")
    status = "completed" if phase == "completed" else ("blocked" if phase == "blocked" else "active")
    next_action = {
        "awaiting_writes": "Generate the complete evidence-backed writes and continue this debug session.",
        "awaiting_clarification": "Answer the one material clarification and continue this debug session.",
        "deploying": f"Poll the owned safe-deploy job {session.get('job_id')} through this debug session.",
        "recovery_required": "Resume from the stored checkpoint without repeating completed writes or deploy steps.",
        "blocked": "Resolve the stored blocker with focused primary-source evidence.",
        "completed": "Stop and report the verified terminal result.",
    }.get(phase, "Continue focused diagnosis from the stored evidence pointers.")
    diagnosis = session.get("diagnosis")
    objective = (
        f"{session.get('issue_description', '')}\n"
        f"Expected: {session.get('expected_result', '')}"
    )[:5000]
    return OdooOperationHandoff(
        operation_id=str(session["operation_id"]),
        client_name=str(session.get("client_name"))[:200] if session.get("client_name") else None,
        project_path=str(session.get("project_path"))[:2000] if session.get("project_path") else None,
        module_name=str(session.get("module_name"))[:200] if session.get("module_name") else None,
        objective=objective,
        locked_scope=[
            f"surface:{session.get('surface') or 'auto'}",
            f"correlation_id:{session.get('correlation_id') or 'unset'}",
        ],
        decisions_with_reasons=[
            "The normal code-change and safe-deploy workflows remain the only mutation/deployment owners.",
            "Only trusted evidence-linked custom-code diagnosis can authorize a repeated repair.",
        ],
        target_files=[
            str(path)[:500] for path in (session.get("diagnosed_paths") or [])
        ][:100],
        completed_checkpoints=[
            value
            for value, present in (
                ("runtime_evidence_collected", bool(session.get("evidence_ledger"))),
                ("writes_applied", int(session.get("fix_cycles") or 0) > 0),
                ("safe_deploy_started", bool(session.get("job_id"))),
                ("deployment_confirmed", bool(session.get("deploy_confirmed") or session.get("already_deployed_confirmed"))),
            )
            if present
        ],
        failed_attempt_fingerprints=list(session.get("write_fingerprints") or [])[-20:],
        current_blocker=(
            None
            if status == "completed"
            else str(session.get("last_code_change_status") or truth_gate.get("missing_proof") or phase)
        ),
        next_action=next_action,
        acceptance_evidence_remaining=list(truth_gate.get("missing_proof") or []),
        diagnosis=OdooFailureDiagnosis.model_validate(diagnosis) if diagnosis else None,
        status=typing.cast(Any, status),
        created_at=str(session.get("created_at") or datetime.now(timezone.utc).isoformat()),
    )


def _debug_instrumentation_cleanup_status(session: dict[str, Any]) -> dict[str, Any]:
    """Prove that connector-requested temporary logging is absent locally."""
    if not session.get("instrumentation_ever_used"):
        return {"required": False, "passed": True, "remaining_paths": []}
    marker = str(session.get("instrumentation_marker") or "")
    project = Path(str(session.get("project_path") or "")).expanduser()
    candidates = [
        str(path).replace("\\", "/").lstrip("./")
        for path in session.get("instrumentation_paths") or []
        if str(path).strip()
    ]
    remaining: list[str] = []
    missing: list[str] = []
    for relative_path in candidates[:50]:
        target = project / relative_path
        try:
            if not target.is_file():
                missing.append(relative_path)
            elif marker and marker in target.read_text(encoding="utf-8", errors="replace"):
                remaining.append(relative_path)
        except OSError:
            missing.append(relative_path)
    return {
        "required": True,
        "passed": not remaining and not missing,
        "remaining_paths": remaining[:20],
        "missing_paths": missing[:20],
        "marker_fingerprint": hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16] if marker else None,
    }


def _debug_truth_gate(session: dict[str, Any]) -> dict[str, Any]:
    deployed = bool(session.get("deploy_confirmed") or session.get("already_deployed_confirmed"))
    baseline = session.get("baseline_assertion") if isinstance(session.get("baseline_assertion"), dict) else {}
    replay = session.get("replay_assertion") if isinstance(session.get("replay_assertion"), dict) else {}
    runtime_replay_configured = bool(session.get("runtime_replay"))
    report_replay_configured = bool(session.get("report_replay"))
    baseline_runtime = (
        session.get("baseline_runtime_replay")
        if isinstance(session.get("baseline_runtime_replay"), dict)
        else {}
    )
    replay_runtime = (
        session.get("replay_runtime_replay")
        if isinstance(session.get("replay_runtime_replay"), dict)
        else {}
    )
    baseline_report = (
        session.get("baseline_report_replay")
        if isinstance(session.get("baseline_report_replay"), dict)
        else {}
    )
    replay_report = (
        session.get("replay_report_replay")
        if isinstance(session.get("replay_report_replay"), dict)
        else {}
    )
    has_discriminating_contract = bool(
        session.get("verification_assertion")
        and baseline
        and baseline.get("query_status") == "ok"
        and baseline.get("passed") is False
    )
    independent_replay_passed = bool(
        has_discriminating_contract
        and replay.get("query_status") == "ok"
        and replay.get("passed") is True
    )
    manual_replay_evidence = any(
        isinstance(item, dict)
        and item.get("kind") == "manual_observation"
        and item.get("source") == "client"
        and item.get("passed") is True
        for item in session.get("evidence_ledger") or []
    )
    manual_replay_passed = bool(
        session.get("reproduction_passed") is True
        and session.get("expected_result_verified") is True
        and manual_replay_evidence
    )
    runtime_baseline_discriminating = bool(
        runtime_replay_configured
        and baseline_runtime.get("action_executed") is True
        and (
            baseline_runtime.get("passed") is False
            or (
                baseline.get("query_status") == "ok"
                and baseline.get("passed") is False
            )
        )
    )
    runtime_replay_passed = bool(
        runtime_replay_configured
        and replay_runtime.get("action_executed") is True
        and replay_runtime.get("passed") is True
        and int(replay_runtime.get("fresh_error_count") or 0) == 0
    )
    report_baseline_discriminating = bool(
        report_replay_configured
        and baseline_report.get("action_executed") is True
        and baseline_report.get("passed") is False
    )
    report_replay_passed = bool(
        report_replay_configured
        and replay_report.get("action_executed") is True
        and replay_report.get("passed") is True
        and int(replay_report.get("fresh_error_count") or 0) == 0
    )
    if report_replay_configured:
        same_scenario_replay_passed = report_replay_passed
        discriminating_baseline = report_baseline_discriminating
        independent_replay_passed = report_replay_passed
    elif runtime_replay_configured:
        same_scenario_replay_passed = runtime_replay_passed
        discriminating_baseline = bool(has_discriminating_contract and runtime_baseline_discriminating)
    else:
        same_scenario_replay_passed = manual_replay_passed
        discriminating_baseline = has_discriminating_contract
    cleanup = _debug_instrumentation_cleanup_status(session)
    instrumentation_cleanup_passed = bool(cleanup.get("passed"))
    verified = bool(
        deployed
        and discriminating_baseline
        and independent_replay_passed
        and same_scenario_replay_passed
        and instrumentation_cleanup_passed
    )
    if verified:
        claim_status = "verified_fixed"
    elif session.get("phase") == "blocked":
        claim_status = "blocked_with_evidence"
    elif deployed:
        claim_status = "deployed_awaiting_verified_replay"
    elif int(session.get("fix_cycles") or 0) > 0:
        claim_status = "implemented_unverified"
    else:
        claim_status = "diagnosed"
    return {
        "claim_status": claim_status,
        "verified_fixed": verified,
        "deploy_proof": deployed,
        "manual_replay_passed": manual_replay_passed,
        "runtime_replay_configured": runtime_replay_configured,
        "report_replay_configured": report_replay_configured,
        "runtime_baseline_discriminating": runtime_baseline_discriminating,
        "runtime_replay_passed": runtime_replay_passed,
        "report_baseline_discriminating": report_baseline_discriminating,
        "report_replay_passed": report_replay_passed,
        "instrumentation_cleanup": cleanup,
        "instrumentation_cleanup_passed": instrumentation_cleanup_passed,
        "same_scenario_replay_passed": same_scenario_replay_passed,
        "independent_replay_passed": independent_replay_passed,
        "discriminating_baseline": discriminating_baseline,
        "success_allowed": verified,
        "missing_proof": [
            label
            for label, present in (
                ("deploy", deployed),
                ("pre-fix failing live assertion/action", discriminating_baseline),
                ("post-replay passing live assertion", independent_replay_passed),
                ("same-scenario replay confirmation", same_scenario_replay_passed),
                ("temporary instrumentation removed", instrumentation_cleanup_passed),
            )
            if not present
        ],
    }


def _debug_public_session(session: dict[str, Any]) -> dict[str, Any]:
    truth_gate = _debug_truth_gate(session)
    return {
        "id": session.get("id"),
        "operation_id": session.get("operation_id"),
        "correlation_id": session.get("correlation_id"),
        "client_name": session.get("client_name"),
        "project_path": session.get("project_path"),
        "module_name": session.get("module_name"),
        "surface": session.get("surface"),
        "debug_plan": session.get("debug_plan") or [],
        "phase": session.get("phase"),
        "fix_cycles": session.get("fix_cycles", 0),
        "max_fix_cycles": session.get("max_fix_cycles", 3),
        "max_surgical_files": max(12, int(session.get("max_surgical_files") or 12)),
        "job_id": session.get("job_id"),
        "deploy_confirmed": session.get("deploy_confirmed", False),
        "already_deployed_confirmed": session.get("already_deployed_confirmed", False),
        "runtime_replay_configured": bool(session.get("runtime_replay")),
        "report_replay_configured": bool(session.get("report_replay")),
        "baseline_runtime_replay_passed": (
            session.get("baseline_runtime_replay", {}).get("passed")
            if isinstance(session.get("baseline_runtime_replay"), dict)
            else None
        ),
        "replay_runtime_replay_passed": (
            session.get("replay_runtime_replay", {}).get("passed")
            if isinstance(session.get("replay_runtime_replay"), dict)
            else None
        ),
        "baseline_report_replay_passed": (
            session.get("baseline_report_replay", {}).get("passed")
            if isinstance(session.get("baseline_report_replay"), dict)
            else None
        ),
        "replay_report_replay_passed": (
            session.get("replay_report_replay", {}).get("passed")
            if isinstance(session.get("replay_report_replay"), dict)
            else None
        ),
        "evidence_count": len(session.get("evidence_ledger") or []),
        "trusted_evidence_count": sum(
            1 for item in session.get("evidence_ledger") or []
            if isinstance(item, dict) and item.get("trusted") is True
        ),
        "diagnosis": session.get("diagnosis"),
        "handoff": {
            "operation_id": session.get("operation_id"),
            "status": (
                "completed"
                if session.get("phase") == "completed"
                else ("blocked" if session.get("phase") == "blocked" else "active")
            ),
            "reload_from_store": True,
        },
        "truth_gate": truth_gate,
    }


def _append_debug_evidence(
    session: dict[str, Any],
    values: list[str],
    *,
    kind: str = "manual_observation",
    source: str = "client",
    trusted: bool = False,
    passed: bool | None = None,
    job_id: str | None = None,
) -> int:
    evidence = list(session.get("evidence") or [])
    fingerprints = list(session.get("evidence_fingerprints") or [])
    known = set(fingerprints)
    added = 0
    for raw in values:
        value = _redact_debug_text(raw, 8_000)
        if not value.strip():
            continue
        fingerprint = _debug_evidence_fingerprint(value)
        if fingerprint in known:
            _debug_record_evidence(
                session,
                kind=kind,
                source=source,
                summary=value,
                trusted=trusted,
                passed=passed,
                job_id=job_id,
            )
            continue
        evidence.append(value)
        fingerprints.append(fingerprint)
        known.add(fingerprint)
        _debug_record_evidence(
            session,
            kind=kind,
            source=source,
            summary=value,
            trusted=trusted,
            passed=passed,
            job_id=job_id,
        )
        added += 1
    session["evidence"] = evidence[-20:]
    session["evidence_fingerprints"] = fingerprints[-40:]
    return added


def _debug_validate_surgical_writes(payload: OdooDebugAndFixRequest, session: dict[str, Any]) -> str:
    effective_file_budget = max(12, int(session.get("max_surgical_files") or 12))
    if len(payload.writes) > effective_file_budget:
        raise HTTPException(
            status_code=409,
            detail=f"Surgical fix allows at most {effective_file_budget} files in one cycle.",
        )
    total_chars = sum(
        len(item.content or "") + len(item.search or "") + len(item.replace or "")
        for item in payload.writes
    )
    if total_chars > int(session.get("max_surgical_content_chars") or 200_000):
        raise HTTPException(
            status_code=409,
            detail="Surgical fix content budget exceeded; split scope only after new diagnosis evidence.",
        )
    module_name = str(session.get("module_name") or "").strip()
    diagnosed_paths = {
        str(path).replace("\\", "/").lstrip("./")
        for path in session.get("diagnosed_paths") or []
        if str(path).strip()
    }
    if diagnosed_paths and module_name:
        unexpected: list[str] = []
        for item in payload.writes:
            path = _safe_module_relative(module_name, item.relative_path)
            companion = (
                path.startswith(f"{module_name}/tests/")
                or path.endswith("/__init__.py")
                or path == f"{module_name}/__manifest__.py"
                or path == f"{module_name}/security/ir.model.access.csv"
            )
            if path not in diagnosed_paths and not companion:
                unexpected.append(path)
        if unexpected:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Surgical scope rejected undiagnosed paths: "
                    + ", ".join(unexpected[:10])
                    + ". Gather focused evidence for these files before expanding the fix."
                ),
            )
    marker = str(session.get("instrumentation_marker") or "")
    instrumentation_paths = list(session.get("instrumentation_paths") or [])
    if marker:
        for item in payload.writes:
            write_text = "\n".join(filter(None, (item.content, item.search, item.replace)))
            if marker not in write_text:
                continue
            path = _safe_module_relative(module_name, item.relative_path)
            if path not in instrumentation_paths:
                instrumentation_paths.append(path)
        if instrumentation_paths:
            session["instrumentation_ever_used"] = True
            session["instrumentation_paths"] = instrumentation_paths[-50:]
    fingerprint = _debug_writes_fingerprint(payload.writes)
    _debug_record_evidence(
        session,
        kind="write_scope",
        source="odoo_mcp",
        summary=f"files={len(payload.writes)} content_chars={total_chars} fingerprint={fingerprint}",
        trusted=True,
        passed=True,
    )
    return fingerprint


def _debug_extract_diagnosed_paths(code_result: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key not in {"relative_path", "path", "file", "target_file"}:
            return
        candidate = str(value or "").replace("\\", "/").strip()
        if (
            candidate
            and not candidate.startswith("/")
            and ":" not in candidate
            and candidate not in paths
        ):
            paths.append(candidate.lstrip("./"))

    for key in ("write_generation", "structure_findings", "custom_reads"):
        visit(code_result.get(key), key)
    return paths[:100]


def _debug_assertion_payload(assertion: OdooDebugRecordAssertion | dict[str, Any] | None) -> dict[str, Any] | None:
    if assertion is None:
        return None
    if isinstance(assertion, OdooDebugRecordAssertion):
        return assertion.model_dump(mode="json")
    return OdooDebugRecordAssertion.model_validate(assertion).model_dump(mode="json")


def _debug_runtime_replay_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _debug_report_replay_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


async def _debug_run_report_replay(
    session: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    replay = _debug_report_replay_payload(session.get("report_replay"))
    if replay is None:
        return {
            "phase": phase,
            "action_executed": False,
            "passed": False,
            "error": "No report replay contract is configured.",
        }
    try:
        client = ClientResolverService().resolve_client(
            client_name=session.get("client_name"),
            project_path=session.get("project_path"),
            require_complete=False,
        )
        result = await asyncio.to_thread(
            OdooShService(client).report_replay,
            replay,
            correlation_id=str(session.get("correlation_id") or ""),
        )
        proof = dict(result.result) if isinstance(result.result, dict) else {}
        proof.update({
            "phase": phase,
            "status": result.status,
            "passed": bool(result.status == "ok" and proof.get("passed") is True),
            "error": _redact_debug_text(result.error or str(proof.get("error") or ""), 4_000) or None,
        })
    except Exception as exc:  # noqa: BLE001
        proof = {
            "phase": phase,
            "status": "failed",
            "action_executed": False,
            "committed": False,
            "passed": False,
            "fresh_error_count": 0,
            "fresh_errors": [],
            "error": _redact_debug_text(f"{type(exc).__name__}: {exc}", 4_000),
        }
    _debug_record_evidence(
        session,
        kind="artifact_replay",
        source="odoo_live",
        summary=json.dumps(proof, ensure_ascii=False, sort_keys=True, default=str),
        trusted=True,
        passed=bool(proof.get("passed")),
        job_id=str(session.get("job_id") or "") or None,
    )
    return proof


async def _debug_collect_correlated_logs(session: dict[str, Any], tail_lines: int = 150) -> str:
    try:
        client = ClientResolverService().resolve_client(
            client_name=session.get("client_name"),
            project_path=session.get("project_path"),
            require_complete=False,
        )
        result = await asyncio.to_thread(
            OdooShService(client).correlated_logs,
            str(session.get("correlation_id") or ""),
            tail_lines,
        )
        return _redact_debug_text(result.stdout or result.stderr, 12_000)
    except Exception as exc:  # noqa: BLE001
        return _redact_debug_text(f"Correlated-log collection failed: {type(exc).__name__}: {exc}", 4_000)


async def _debug_run_runtime_replay(
    session: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    replay = _debug_runtime_replay_payload(session.get("runtime_replay"))
    if replay is None:
        return {
            "phase": phase,
            "action_executed": False,
            "passed": False,
            "error": "No runtime replay contract is configured.",
        }
    if phase == "baseline":
        replay = {**replay, "commit": False, "confirm_mutation": False}
    try:
        client = ClientResolverService().resolve_client(
            client_name=session.get("client_name"),
            project_path=session.get("project_path"),
            require_complete=False,
        )
        result = await asyncio.to_thread(
            OdooShService(client).runtime_replay,
            replay,
            correlation_id=str(session.get("correlation_id") or ""),
        )
        proof = dict(result.result) if isinstance(result.result, dict) else {}
        proof.update({
            "phase": phase,
            "status": result.status,
            "passed": bool(result.status == "ok" and proof.get("passed") is True),
            "error": _redact_debug_text(result.error or str(proof.get("error") or ""), 4_000) or None,
        })
    except Exception as exc:  # noqa: BLE001
        proof = {
            "phase": phase,
            "status": "failed",
            "action_executed": False,
            "committed": False,
            "passed": False,
            "fresh_error_count": 0,
            "fresh_errors": [],
            "error": _redact_debug_text(f"{type(exc).__name__}: {exc}", 4_000),
        }
    _debug_record_evidence(
        session,
        kind="runtime_replay",
        source="odoo_live",
        summary=json.dumps(proof, ensure_ascii=False, sort_keys=True, default=str),
        trusted=True,
        passed=bool(proof.get("passed")),
        job_id=str(session.get("job_id") or "") or None,
    )
    return proof


async def _debug_run_live_assertion(
    session: dict[str, Any],
    assertion_value: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    proof = await asyncio.to_thread(
        LiveAssertionService().evaluate,
        assertion_value,
        client_name=session.get("client_name"),
        project_path=session.get("project_path"),
        phase=phase,
    )
    passed = bool(proof.get("passed"))
    _debug_record_evidence(
        session,
        kind="baseline_assertion" if phase == "baseline" else "replay_assertion",
        source="odoo_live",
        summary=json.dumps(proof, ensure_ascii=False, sort_keys=True),
        trusted=True,
        passed=passed,
    )
    return proof


def _debug_recover_owned_job(store: JsonStore, session: dict[str, Any]):
    """Find a safe-deploy job created after this fix cycle began."""
    started_raw = session.get("operation_started_at")
    try:
        started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except Exception:
        started = None
    for job in store.list_jobs(limit=100):
        if job.job_type != "safe_deploy_module":
            continue
        request = job.request or {}
        if request.get("module_name") != session.get("module_name"):
            continue
        if str(request.get("project_path") or "") != str(session.get("project_path") or ""):
            continue
        if started is not None:
            try:
                created = datetime.fromisoformat(str(job.created_at).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < started:
                    continue
            except Exception:
                continue
        return job
    return None


def _debug_task_description(session: dict[str, Any]) -> str:
    evidence = "\n\n".join(session.get("evidence") or [])[-12_000:]
    reproduction = "\n".join(f"- {step}" for step in session.get("reproduction_steps") or []) or "- Reproduce the reported entry point with the correlation ID."
    return (
        "Diagnose and completely fix this persisted Odoo runtime issue.\n\n"
        f"Issue: {session['issue_description']}\n"
        f"Expected result: {session['expected_result']}\n"
        f"Detected surface: {session['surface']}\n"
        f"Correlation ID: {session['correlation_id']}\n"
        f"Reproduction steps:\n{reproduction}\n\n"
        "Source-first contract: inspect the existing custom implementation and its XML/action entry point first; "
        "then inspect the exact installed Odoo.sh Community source with Enterprise fallback only when the owning addon is Enterprise. "
        "Derive the real method/report/controller/template from source and registry evidence; never guess an action or treat local variables, QA labels, or evidence tokens as technical targets. "
        "Edit an owned custom addon directly; never edit Community/Enterprise source, and inherit it through custom code when native behavior must change.\n\n"
        f"Structured diagnosis:\n{json.dumps(session.get('diagnosis') or {}, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Sanitized runtime evidence:\n{evidence or 'No traceback captured yet; inspect the exact entry point and add bounded instrumentation only when needed.'}\n\n"
        "Implementation contract: identify the evidence-backed root cause; make the smallest complete multi-file fix; "
        f"preserve unrelated behavior. If static source comparison cannot prove the cause, add minimal temporary _logger instrumentation using exact marker {session.get('instrumentation_marker')}; "
        "deploy and replay once, keep only decisive correlated lines, then remove that instrumentation in the functional fix; "
        "never log credentials or sensitive record values; add or update focused mocked/regression tests for the failing flow. "
        "Do not submit scaffold/import/security-only writes while functional behavior remains missing. "
        "The existing code-change and safe-deploy workflows remain the sole owners of writes, Git, Odoo.sh deployment, and module upgrade."
    )[:20_000]


def _debug_code_task_type(surface: str) -> str:
    return {
        "frontend": "js_owl",
        "controller": "controller",
        "install_upgrade": "python_model",
        "security": "security_csv",
    }.get(surface, "python_model")


def _debug_collect_error_pack(client_name: str | None, tail_lines: int) -> str:
    try:
        client = ClientResolverService().resolve_client(client_name=client_name, require_complete=False)
        result = OdooShService(client).error_pack(tail_lines)
        return _redact_debug_text(
            f"exit_code={result.exit_code}\n{result.stdout}\n{result.stderr}",
            12_000,
        )
    except Exception as exc:  # noqa: BLE001
        return _redact_debug_text(f"Error-pack collection failed: {type(exc).__name__}: {exc}", 4_000)


def _debug_next_call(
    session: dict[str, Any],
    *,
    action: str,
    required_generated_arguments: list[str] | None = None,
) -> dict[str, Any]:
    call = {
        "tool": "workflow.odoo_debug_and_fix",
        "arguments": {
            "client_name": session.get("client_name"),
            "project_path": session.get("project_path"),
            "module_name": session.get("module_name"),
            "issue_description": session.get("issue_description"),
            "expected_result": session.get("expected_result"),
            "reproduction_steps": session.get("reproduction_steps") or [],
            "surface": session.get("surface"),
            "action": action,
            "debug_session_id": session.get("id"),
            "operation_id": session.get("operation_id"),
            "correlation_id": session.get("correlation_id"),
            "verification_assertion": session.get("verification_assertion"),
            "runtime_replay": session.get("runtime_replay"),
            "report_replay": session.get("report_replay"),
            "failure_diagnosis": session.get("diagnosis"),
            "max_fix_cycles": session.get("max_fix_cycles", 3),
            "max_surgical_files": max(12, int(session.get("max_surgical_files") or 12)),
            "max_surgical_content_chars": session.get("max_surgical_content_chars", 200_000),
            "job_id": session.get("job_id"),
        },
    }
    if action == "poll":
        call["arguments"].update({
            "wait_for_result": True,
            "wait_timeout_seconds": 30,
            "poll_interval_seconds": 1.0,
        })
    pending_writes = session.get("pending_clarification_writes")
    if action == "continue" and isinstance(pending_writes, list) and pending_writes:
        call["arguments"]["writes"] = pending_writes
    if required_generated_arguments:
        call["required_generated_arguments"] = required_generated_arguments
    return call


async def _debug_begin_code_inspection(
    payload: OdooDebugAndFixRequest,
    session: dict[str, Any],
    store: JsonStore,
) -> dict[str, Any]:
    try:
        code_result = await _workflow_odoo_code_change(OdooCodeChangeRequest(
            client_name=session.get("client_name"),
            project_path=session.get("project_path"),
            module_name=session["module_name"],
            task_description=_debug_task_description(session),
            task_type=_debug_code_task_type(str(session["surface"])),
            intent="fix_bug",
            patch_strategy="explicit_writes" if payload.writes else "auto",
            change_request={
                "scope": "module",
                "debug_context_only": True,
                "surface": session.get("surface"),
            },
            target_files=[],
            writes=payload.writes,
            clarification_answer=payload.clarification_answer,
            commit_message=f"Fix {session['module_name']}: {session['issue_description'][:180]}",
            auto_deploy=payload.auto_deploy,
            deploy_mode="auto",
            investigation_evidence=list(session.get("evidence") or [])[-20:],
            investigation_diagnosis=(
                OdooFailureDiagnosis.model_validate(session["diagnosis"])
                if session.get("diagnosis")
                else None
            ),
        ))
    except Exception as exc:
        session["phase"] = "recovery_required"
        _debug_record_evidence(
            session,
            kind="recovery",
            source="odoo_mcp",
            summary=f"Code workflow interrupted: {type(exc).__name__}: {exc}",
            trusted=True,
            passed=False,
        )
        _debug_session_store(store, session, client_name=session.get("client_name"))
        raise
    effective_module = str(code_result.get("module_name") or session.get("module_name") or "").strip()
    if effective_module:
        session["module_name"] = effective_module
    raw_diagnosed_paths = _debug_extract_diagnosed_paths(code_result)
    diagnosed_paths: list[str] = []
    for path in raw_diagnosed_paths:
        try:
            normalized = _safe_module_relative(effective_module, path)
        except HTTPException:
            continue
        if normalized not in diagnosed_paths:
            diagnosed_paths.append(normalized)
    if diagnosed_paths:
        session["diagnosed_paths"] = diagnosed_paths
    code_status = str(code_result.get("status") or "")
    job_id = code_result.get("job_id")
    if job_id:
        session["job_id"] = job_id
        session["phase"] = "deploying"
        session.pop("active_write_fingerprint", None)
        _debug_record_evidence(
            session,
            kind="qa",
            source="odoo_mcp",
            summary=f"Code workflow accepted the diagnosed writes and started safe deploy job {job_id}.",
            trusted=True,
            passed=True,
            job_id=str(job_id),
        )
        _debug_session_store(store, session, client_name=session.get("client_name"))
        return {
            "status": "debug_deploying",
            "message": "The existing code-change workflow verified the fix and started its owned safe-deploy job. Continue through this debug session; do not start another deploy.",
            "debug_session": _debug_public_session(session),
            "code_change": code_result,
            "job_id": job_id,
            "success_allowed": False,
            "completion_required": True,
            "next_call": _debug_next_call(session, action="poll"),
        }

    if code_status in {"needs_writes", "internal_repair_required"}:
        session["phase"] = "awaiting_writes"
        session.pop("active_write_fingerprint", None)
        contexts = code_result.get("write_generation")
        _debug_record_evidence(
            session,
            kind="code_inspection",
            source="odoo_mcp",
            summary=json.dumps(
                {
                    "status": code_status,
                    "context_count": len(contexts.get("contexts") or []) if isinstance(contexts, dict) else 0,
                    "structure_findings": len(code_result.get("structure_findings") or []),
                },
                sort_keys=True,
            ),
            trusted=True,
            passed=True,
        )
        _debug_session_store(store, session, client_name=session.get("client_name"))
        return {
            "status": "debug_needs_writes",
            "message": "Generate one complete evidence-backed write set from the embedded code context, then continue the same debug session.",
            "debug_session": _debug_public_session(session),
            "diagnosis_evidence": list(session.get("evidence") or [])[-3:],
            "code_change": code_result,
            "write_generation": code_result.get("write_generation"),
            "structure_findings": code_result.get("structure_findings") or [],
            "success_allowed": False,
            "completion_required": True,
            "next_call": _debug_next_call(session, action="continue", required_generated_arguments=["writes"]),
        }

    if code_status == "needs_clarification":
        session["phase"] = "awaiting_clarification"
        session["pending_clarification_writes"] = [
            write.model_dump(mode="json") for write in payload.writes
        ]
        session["clarification_gate"] = code_result.get("clarification_gate") or {
            "required": True,
            "question": (code_result.get("target_resolution") or {}).get("question"),
        }
        _debug_session_store(store, session, client_name=session.get("client_name"))
        return {
            "status": "debug_needs_clarification",
            "message": "The nested code-change workflow needs one material clarification. Continue this same debug session with clarification_answer; no writes were discarded.",
            "debug_session": _debug_public_session(session),
            "clarification_gate": session["clarification_gate"],
            "code_change": code_result,
            "success_allowed": False,
            "completion_required": True,
            "next_call": _debug_next_call(
                session,
                action="continue",
                required_generated_arguments=["clarification_answer"],
            ),
        }

    session["phase"] = "blocked"
    session["last_code_change_status"] = code_status
    _debug_session_store(store, session, client_name=session.get("client_name"))
    return {
        "status": "debug_blocked",
        "message": "The existing code-change workflow returned a non-deployable terminal blocker.",
        "debug_session": _debug_public_session(session),
        "code_change": code_result,
        "success_allowed": False,
        "completion_required": False,
        "next_call": None,
    }


async def _workflow_odoo_debug_and_fix(payload: OdooDebugAndFixRequest) -> dict[str, Any]:
    """Evidence-first runtime debugger that never duplicates mutation/deploy logic."""
    store = JsonStore()
    if payload.action in {"continue", "poll", "verify"} and not payload.debug_session_id:
        raise HTTPException(status_code=400, detail=f"action={payload.action} requires debug_session_id.")
    if payload.action == "start" and payload.debug_session_id:
        raise HTTPException(status_code=400, detail="action=start cannot reuse an existing debug_session_id.")
    seed = OdooCodeChangeRequest(
        client_name=payload.client_name,
        project_path=payload.project_path,
        module_name=payload.module_name,
        task_description=payload.issue_description,
        auto_deploy=False,
    )
    resolved_client_name, resolved_project_path = _resolve_code_change_project(seed)
    client_name = resolved_client_name or payload.client_name

    if not payload.debug_session_id:
        if not _debug_requires_runtime_loop(payload):
            ClientGateService().require_validated_client(client_name, "workflow.odoo_debug_and_fix")
            fast_code_request = OdooCodeChangeRequest(
                client_name=client_name,
                project_path=resolved_project_path,
                module_name=payload.module_name,
                task_description=f"{payload.issue_description}\nExpected result: {payload.expected_result}",
                task_type=_debug_code_task_type(_detect_debug_surface(payload)),
                intent="fix_bug",
                patch_strategy="explicit_writes" if payload.writes else "auto",
                writes=payload.writes,
                clarification_answer=payload.clarification_answer,
                commit_message=f"Fix {payload.module_name}: {payload.issue_description[:180]}",
                auto_deploy=payload.auto_deploy,
                verification_assertion=payload.verification_assertion,
            )
            return {
                "status": "routed_to_code_change_fast_path",
                "message": "No runtime-debug signal was detected. Use the existing focused code-change workflow without log collection or debug cycles.",
                "surface": _detect_debug_surface(payload),
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": fast_code_request.model_dump(mode="json", exclude_none=True),
                },
            }

        ClientGateService().require_validated_client(client_name, "odoosh.error_pack")
        operation_id = _debug_operation_id(
            payload,
            client_name=client_name,
            project_path=resolved_project_path,
        )
        existing_session = _debug_find_operation(store, operation_id, client_name=client_name)
        if existing_session:
            if (
                payload.module_name not in {
                    existing_session.get("module_name"),
                    existing_session.get("requested_module_name"),
                }
                or existing_session.get("project_path") != resolved_project_path
            ):
                raise HTTPException(status_code=409, detail="operation_id is already bound to another debug scope.")
            return await _workflow_odoo_debug_and_fix(payload.model_copy(update={
                "action": "auto",
                "debug_session_id": existing_session.get("id"),
                "operation_id": operation_id,
                "writes": [],
            }))
        assertion_payload = _debug_assertion_payload(payload.verification_assertion)
        runtime_replay_payload = _debug_runtime_replay_payload(payload.runtime_replay)
        report_replay_payload = _debug_report_replay_payload(payload.report_replay)
        correlation_id = payload.correlation_id or f"dbg-{uuid4().hex[:12]}"
        session = {
            "id": uuid4().hex,
            "operation_id": operation_id,
            "correlation_id": correlation_id,
            "client_name": client_name,
            "project_path": resolved_project_path,
            "module_name": payload.module_name,
            "requested_module_name": payload.module_name,
            "issue_description": _redact_debug_text(payload.issue_description, 10_000),
            "expected_result": _redact_debug_text(payload.expected_result, 5_000),
            "reproduction_steps": [
                _redact_debug_text(step, 1_000) for step in payload.reproduction_steps
            ],
            "surface": _detect_debug_surface(payload),
            "phase": "diagnosing",
            "fix_cycles": 0,
            "max_fix_cycles": payload.max_fix_cycles,
            "max_surgical_files": max(12, payload.max_surgical_files),
            "max_surgical_content_chars": payload.max_surgical_content_chars,
            "job_id": None,
            "deploy_confirmed": False,
            "evidence": [],
            "evidence_fingerprints": [],
            "evidence_ledger": [],
            "verification_assertion": assertion_payload,
            "runtime_replay": runtime_replay_payload,
            "report_replay": report_replay_payload,
            "debug_plan": [
                "Trace the exact custom entry point and compare the installed native Odoo.sh implementation.",
                "Replay the same bounded scenario; instrument only when source evidence is insufficient.",
                "Apply the smallest custom fix, deploy, replay, and prove temporary instrumentation is removed.",
            ],
            "instrumentation_marker": f"ODOO_MCP_DEBUG:{correlation_id}",
            "instrumentation_ever_used": False,
            "instrumentation_paths": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _append_debug_evidence(
            session,
            payload.runtime_evidence,
            kind="manual_observation",
            source="client",
            trusted=False,
        )
        if runtime_replay_payload:
            session["baseline_runtime_replay"] = await _debug_run_runtime_replay(
                session,
                phase="baseline",
            )
            _append_debug_evidence(
                session,
                [json.dumps(session["baseline_runtime_replay"], ensure_ascii=False, sort_keys=True)],
                kind="runtime_replay",
                source="odoo_live",
                trusted=True,
                passed=bool(session["baseline_runtime_replay"].get("passed")),
            )
        if report_replay_payload:
            session["baseline_report_replay"] = await _debug_run_report_replay(
                session,
                phase="baseline",
            )
        if not runtime_replay_payload and not report_replay_payload:
            error_pack = await asyncio.to_thread(_debug_collect_error_pack, client_name, payload.tail_lines)
            _append_debug_evidence(
                session,
                [error_pack],
                kind="runtime_log",
                source="odoo_mcp",
                trusted=True,
            )
        if assertion_payload:
            session["baseline_assertion"] = await _debug_run_live_assertion(
                session,
                assertion_payload,
                phase="baseline",
            )
        session["diagnosis"] = _debug_resolve_diagnosis(
            session,
            payload.failure_diagnosis,
        ).model_dump(mode="json")
        _debug_session_store(store, session, client_name=client_name)
        return await _debug_begin_code_inspection(payload, session, store)

    session = _debug_session_load(store, payload.debug_session_id, client_name=client_name)
    if payload.module_name not in {
        session.get("module_name"),
        session.get("requested_module_name"),
    }:
        raise HTTPException(status_code=409, detail="Debug session module does not match this request.")
    if payload.operation_id and payload.operation_id != session.get("operation_id"):
        raise HTTPException(status_code=409, detail="operation_id does not match this debug session.")
    supplied_assertion = _debug_assertion_payload(payload.verification_assertion)
    if supplied_assertion is not None and supplied_assertion != session.get("verification_assertion"):
        raise HTTPException(status_code=409, detail="verification_assertion is immutable after debug start.")
    supplied_runtime_replay = _debug_runtime_replay_payload(payload.runtime_replay)
    if supplied_runtime_replay is not None and supplied_runtime_replay != session.get("runtime_replay"):
        raise HTTPException(status_code=409, detail="runtime_replay is immutable after debug start.")
    supplied_report_replay = _debug_report_replay_payload(payload.report_replay)
    if supplied_report_replay is not None and supplied_report_replay != session.get("report_replay"):
        raise HTTPException(status_code=409, detail="report_replay is immutable after debug start.")
    if session.get("phase") == "completed":
        truth_gate = _debug_truth_gate(session)
        if not truth_gate["verified_fixed"]:
            session["phase"] = "awaiting_replay"
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_implemented_unverified",
                "message": "Legacy/manual completion is not trusted. Supply the immutable live assertion contract and start a new operation for independent verification.",
                "debug_session": _debug_public_session(session),
                "claim_status": truth_gate["claim_status"],
                "success_allowed": False,
                "completion_required": False,
                "next_call": None,
            }
        return {
            "status": "debug_verified",
            "message": "This debug session already completed with deploy and replay proof.",
            "debug_session": _debug_public_session(session),
            "deploy_confirmed": bool(session.get("deploy_confirmed")),
            "already_deployed_confirmed": bool(session.get("already_deployed_confirmed")),
            "reproduction_passed": True,
            "expected_result_verified": True,
            "success_allowed": True,
            "completion_required": False,
            "next_call": None,
        }

    added_evidence = _append_debug_evidence(
        session,
        payload.runtime_evidence,
        kind="manual_observation",
        source="client",
        trusted=False,
    )
    session["diagnosis"] = _debug_resolve_diagnosis(
        session,
        payload.failure_diagnosis or session.get("diagnosis"),
    ).model_dump(mode="json")
    if session.get("phase") == "awaiting_clarification":
        if not str(payload.clarification_answer or "").strip():
            return {
                "status": "debug_needs_clarification",
                "message": "Continue this same debug session with the requested clarification_answer.",
                "debug_session": _debug_public_session(session),
                "clarification_gate": session.get("clarification_gate"),
                "success_allowed": False,
                "completion_required": True,
                "next_call": _debug_next_call(
                    session,
                    action="continue",
                    required_generated_arguments=["clarification_answer"],
                ),
            }
        session["phase"] = "diagnosing"
        session.pop("clarification_gate", None)
        pending_writes = session.pop("pending_clarification_writes", [])
        _debug_session_store(store, session, client_name=client_name)
        continued_payload = payload
        if not payload.writes and pending_writes:
            continued_payload = payload.model_copy(update={
                "writes": [OdooCodeFileChange.model_validate(write) for write in pending_writes],
            })
        return await _debug_begin_code_inspection(continued_payload, session, store)

    if payload.writes:
        write_fingerprint = _debug_validate_surgical_writes(payload, session)
        retrying_interrupted_write = (
            session.get("phase") in {"applying_fix", "recovery_required"}
            and session.get("active_write_fingerprint") == write_fingerprint
        )
        if session.get("phase") != "awaiting_writes" and not retrying_interrupted_write:
            raise HTTPException(
                status_code=409,
                detail=f"Debug writes are only accepted in awaiting_writes phase, not {session.get('phase')}.",
            )
        if (
            not retrying_interrupted_write
            and int(session.get("fix_cycles") or 0) >= int(session.get("max_fix_cycles") or 3)
        ):
            session["phase"] = "blocked"
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_max_cycles_reached",
                "message": "Maximum evidence-based fix cycles reached. No further writes are allowed without a new debug session and revised scope.",
                "debug_session": _debug_public_session(session),
                "success_allowed": False,
                "completion_required": False,
                "next_call": None,
            }
        if not retrying_interrupted_write:
            session["fix_cycles"] = int(session.get("fix_cycles") or 0) + 1
        session["active_write_fingerprint"] = write_fingerprint
        session["operation_started_at"] = session.get("operation_started_at") or datetime.now(timezone.utc).isoformat()
        session["phase"] = "applying_fix"
        _debug_session_store(store, session, client_name=client_name)
        return await _debug_begin_code_inspection(payload, session, store)

    if session.get("phase") in {"applying_fix", "recovery_required"} and not session.get("job_id"):
        recovered_job = _debug_recover_owned_job(store, session)
        if recovered_job:
            session["job_id"] = recovered_job.id
            session["phase"] = "deploying"
            session.pop("active_write_fingerprint", None)
            _debug_record_evidence(
                session,
                kind="recovery",
                source="job_store",
                summary=f"Recovered owned safe-deploy job {recovered_job.id} after interrupted response.",
                trusted=True,
                passed=True,
                job_id=recovered_job.id,
            )
            _debug_session_store(store, session, client_name=client_name)
        else:
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_recovery_requires_same_writes",
                "message": "The previous write call was interrupted before an owned deploy job was persisted. Retry the exact same writes; a different patch is blocked.",
                "debug_session": _debug_public_session(session),
                "success_allowed": False,
                "completion_required": True,
                "next_call": _debug_next_call(
                    session,
                    action="continue",
                    required_generated_arguments=["writes"],
                ),
            }

    job_id = payload.job_id or session.get("job_id")
    owned_job_id = session.get("job_id")
    if payload.job_id and payload.job_id != owned_job_id:
        raise HTTPException(status_code=409, detail="job_id is not owned by this debug session.")
    if job_id and (payload.action in {"auto", "poll", "continue", "verify"} or session.get("phase") == "deploying"):
        if payload.wait_for_result:
            job = await asyncio.to_thread(
                store.wait_job,
                job_id,
                payload.wait_timeout_seconds,
                payload.poll_interval_seconds,
            )
        else:
            job = store.get_job(job_id)
        job_data = job.model_dump(mode="json")
        if job.status in {JobStatus.queued, JobStatus.running}:
            session["phase"] = "deploying"
            session["job_id"] = job_id
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_deploying",
                "message": "The existing safe-deploy job is still running; continue the same debug session.",
                "debug_session": _debug_public_session(session),
                "job": job_data,
                "success_allowed": False,
                "completion_required": True,
                "next_call": _debug_next_call(session, action="poll"),
            }

        if job.deploy_confirmed or job.already_deployed_confirmed:
            session["deploy_confirmed"] = bool(job.deploy_confirmed)
            session["already_deployed_confirmed"] = bool(job.already_deployed_confirmed)
            session["phase"] = "awaiting_replay"
            session["job_id"] = job_id
            _debug_record_evidence(
                session,
                kind="deploy",
                source="job_store",
                summary=json.dumps({
                    "job_id": job_id,
                    "deploy_confirmed": bool(job.deploy_confirmed),
                    "already_deployed_confirmed": bool(job.already_deployed_confirmed),
                    "status": getattr(job.status, "value", str(job.status)),
                }, sort_keys=True),
                trusted=True,
                passed=True,
                job_id=str(job_id),
            )
            assertion_payload = session.get("verification_assertion")
            if session.get("runtime_replay"):
                session["replay_runtime_replay"] = await _debug_run_runtime_replay(
                    session,
                    phase="replay",
                )
                session["reproduction_passed"] = bool(
                    session["replay_runtime_replay"].get("passed")
                )
                session["replay_assertion"] = await _debug_run_live_assertion(
                    session,
                    assertion_payload,
                    phase="replay",
                )
                session["expected_result_verified"] = bool(
                    session["replay_assertion"].get("passed")
                )
                runtime_summary = json.dumps(
                    session["replay_runtime_replay"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                verification_added = _append_debug_evidence(
                    session,
                    [runtime_summary],
                    kind="runtime_replay",
                    source="odoo_live",
                    trusted=True,
                    passed=bool(session["reproduction_passed"]),
                    job_id=str(job_id),
                )
                has_verification_evidence = True
            elif session.get("report_replay"):
                session["replay_report_replay"] = await _debug_run_report_replay(
                    session,
                    phase="replay",
                )
                session["reproduction_passed"] = bool(
                    session["replay_report_replay"].get("passed")
                )
                session["expected_result_verified"] = bool(
                    session["replay_report_replay"].get("passed")
                )
                verification_added = 1
                has_verification_evidence = True
            else:
                session["reproduction_passed"] = payload.reproduction_passed
                session["expected_result_verified"] = payload.expected_result_verified
                verification_added = _append_debug_evidence(
                    session,
                    payload.verification_evidence,
                    kind="manual_observation",
                    source="client",
                    trusted=False,
                    passed=bool(
                        payload.reproduction_passed is True
                        and payload.expected_result_verified is True
                    ),
                    job_id=str(job_id),
                )
                has_verification_evidence = any(
                    str(item or "").strip() for item in payload.verification_evidence
                )
                if (
                    assertion_payload
                    and payload.reproduction_passed is True
                    and payload.expected_result_verified is True
                    and has_verification_evidence
                ):
                    session["replay_assertion"] = await _debug_run_live_assertion(
                        session,
                        assertion_payload,
                        phase="replay",
                    )
            cleanup_status = _debug_instrumentation_cleanup_status(session)
            session["instrumentation_cleanup"] = cleanup_status
            _debug_record_evidence(
                session,
                kind="instrumentation_cleanup",
                source="odoo_mcp",
                summary=json.dumps(cleanup_status, sort_keys=True),
                trusted=True,
                passed=bool(cleanup_status.get("passed")),
                job_id=str(job_id),
            )
            truth_gate = _debug_truth_gate(session)
            if truth_gate["verified_fixed"]:
                session["phase"] = "completed"
                session["verification_evidence"] = (
                    ["Trusted SSH/Odoo-shell replay and acceptance assertion passed."]
                    if session.get("runtime_replay") or session.get("report_replay")
                    else [
                        _redact_debug_text(item, 4_000) for item in payload.verification_evidence
                    ][-10:]
                )
                _debug_session_store(store, session, client_name=client_name)
                issue_fingerprint = code_change_issue_fingerprint(
                    project_path=str(session.get("project_path") or ""),
                    module_name=str(session.get("module_name") or ""),
                    task_description=str(session.get("issue_description") or ""),
                    task_type=_debug_code_task_type(str(session.get("surface") or "backend")),
                    intent="fix_bug",
                    target_hint="",
                )
                repeated_failure_guard_status(
                    client_name=client_name,
                    fingerprint=issue_fingerprint,
                    task_description=str(session.get("issue_description") or ""),
                    investigation_evidence=list(session.get("evidence") or []),
                    investigation_diagnosis=(
                        OdooFailureDiagnosis.model_validate(session["diagnosis"])
                        if session.get("diagnosis")
                        else None
                    ),
                    trusted_evidence_ids=_debug_trusted_evidence_ids(session),
                    functional_verification_passed=True,
                )
                return {
                    "status": "debug_verified",
                    "message": "Deployment, module upgrade, same-scenario replay, and expected-result verification all passed.",
                    "debug_session": _debug_public_session(session),
                    "job": job_data,
                    "deploy_confirmed": bool(job.deploy_confirmed),
                    "already_deployed_confirmed": bool(job.already_deployed_confirmed),
                    "reproduction_passed": True,
                    "expected_result_verified": True,
                    "claim_status": "verified_fixed",
                    "truth_gate": truth_gate,
                    "success_allowed": True,
                    "completion_required": False,
                    "next_call": None,
                }

            if (
                not truth_gate.get("instrumentation_cleanup_passed")
                and session.get("reproduction_passed") is True
                and session.get("expected_result_verified") is True
            ):
                cleanup = truth_gate.get("instrumentation_cleanup") or {}
                remaining_paths = list(cleanup.get("remaining_paths") or [])
                missing_paths = list(cleanup.get("missing_paths") or [])
                cleanup_paths = list(dict.fromkeys([*remaining_paths, *missing_paths]))
                if cleanup_paths:
                    session["diagnosed_paths"] = list(dict.fromkeys([
                        *(session.get("diagnosed_paths") or []),
                        *cleanup_paths,
                    ]))
                session["phase"] = "awaiting_writes"
                session["job_id"] = None
                session.pop("operation_started_at", None)
                _debug_session_store(store, session, client_name=client_name)
                return {
                    "status": "debug_needs_instrumentation_cleanup",
                    "message": "The scenario now passes, but temporary correlated instrumentation remains. Remove only those markers and redeploy before success can be claimed.",
                    "debug_session": _debug_public_session(session),
                    "remaining_paths": remaining_paths,
                    "missing_paths": missing_paths,
                    "success_allowed": False,
                    "completion_required": True,
                    "next_call": _debug_next_call(
                        session,
                        action="continue",
                        required_generated_arguments=["writes"],
                    ),
                }

            replay_failed = bool(
                session.get("reproduction_passed") is False
                or session.get("expected_result_verified") is False
                or (
                    isinstance(session.get("replay_assertion"), dict)
                    and session["replay_assertion"].get("passed") is False
                )
                or (
                    isinstance(session.get("replay_report_replay"), dict)
                    and session["replay_report_replay"].get("passed") is False
                )
            )
            if replay_failed:
                trusted_replay_failure = bool(
                    (
                        isinstance(session.get("replay_assertion"), dict)
                        and session["replay_assertion"].get("query_status") == "ok"
                        and session["replay_assertion"].get("passed") is False
                    )
                    or (
                        isinstance(session.get("replay_runtime_replay"), dict)
                        and session["replay_runtime_replay"].get("action_executed") is True
                        and session["replay_runtime_replay"].get("passed") is False
                    )
                    or (
                        isinstance(session.get("replay_report_replay"), dict)
                        and session["replay_report_replay"].get("action_executed") is True
                        and session["replay_report_replay"].get("passed") is False
                    )
                )
                if verification_added <= 0 and not trusted_replay_failure:
                    _debug_session_store(store, session, client_name=client_name)
                    return {
                        "status": "debug_needs_verification_evidence",
                        "message": "Replay failed, but no new console/log/DB/API evidence was supplied. Provide fresh evidence before another fix cycle.",
                        "debug_session": _debug_public_session(session),
                        "success_allowed": False,
                        "completion_required": True,
                        "next_call": _debug_next_call(session, action="verify", required_generated_arguments=["verification_evidence"]),
                    }
                error_pack = (
                    await _debug_collect_correlated_logs(session, min(payload.tail_lines, 150))
                    if session.get("runtime_replay") or session.get("report_replay")
                    else await asyncio.to_thread(_debug_collect_error_pack, client_name, payload.tail_lines)
                )
                _append_debug_evidence(
                    session,
                    [error_pack],
                    kind="correlated_log" if session.get("runtime_replay") or session.get("report_replay") else "runtime_log",
                    source="odoo_mcp",
                    trusted=True,
                    passed=False,
                    job_id=str(job_id),
                )
                if int(session.get("fix_cycles") or 0) >= int(session.get("max_fix_cycles") or 3):
                    session["phase"] = "blocked"
                    _debug_session_store(store, session, client_name=client_name)
                    return {
                        "status": "debug_max_cycles_reached",
                        "message": "Replay still fails after the maximum diagnosed fix cycles. The exact latest evidence is preserved in the debug session.",
                        "debug_session": _debug_public_session(session),
                        "success_allowed": False,
                        "completion_required": False,
                        "next_call": None,
                    }
                session["phase"] = "diagnosing"
                session["job_id"] = None
                session.pop("operation_started_at", None)
                _debug_session_store(store, session, client_name=client_name)
                return await _debug_begin_code_inspection(payload.model_copy(update={"writes": [], "auto_deploy": True}), session, store)

            if (
                payload.reproduction_passed is True
                and payload.expected_result_verified is True
                and has_verification_evidence
                and not truth_gate["verified_fixed"]
            ):
                session["phase"] = "awaiting_replay"
                _debug_session_store(store, session, client_name=client_name)
                return {
                    "status": "debug_implemented_unverified",
                    "message": (
                        "The fix is deployed and manually reported as working, but independent proof is missing. "
                        "A verified_fixed claim requires an immutable live assertion that failed before the fix and passes after replay."
                    ),
                    "debug_session": _debug_public_session(session),
                    "job": job_data,
                    "claim_status": truth_gate["claim_status"],
                    "truth_gate": truth_gate,
                    "deploy_confirmed": bool(job.deploy_confirmed),
                    "already_deployed_confirmed": bool(job.already_deployed_confirmed),
                    "success_allowed": False,
                    "completion_required": False,
                    "next_call": None,
                }

            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_awaiting_replay",
                "message": "Deploy is confirmed. Replay the same scenario and provide concrete verification evidence; deploy proof alone is not debug success.",
                "debug_session": _debug_public_session(session),
                "job": job_data,
                "deploy_confirmed": bool(job.deploy_confirmed),
                "already_deployed_confirmed": bool(job.already_deployed_confirmed),
                "claim_status": truth_gate["claim_status"],
                "truth_gate": truth_gate,
                "success_allowed": False,
                "completion_required": True,
                "next_call": _debug_next_call(
                    session,
                    action="verify",
                    required_generated_arguments=["reproduction_passed", "expected_result_verified", "verification_evidence"],
                ),
            }

        failure_evidence = _redact_debug_text(json.dumps(job_data, ensure_ascii=False, default=str), 8_000)
        error_pack = await asyncio.to_thread(_debug_collect_error_pack, client_name, payload.tail_lines)
        new_failure_evidence = _append_debug_evidence(
            session,
            [failure_evidence],
            kind="deploy",
            source="job_store",
            trusted=True,
            passed=False,
            job_id=str(job_id),
        )
        new_failure_evidence += _append_debug_evidence(
            session,
            [error_pack],
            kind="runtime_log",
            source="odoo_mcp",
            trusted=True,
            passed=False,
            job_id=str(job_id),
        )
        session["job_id"] = None
        session.pop("operation_started_at", None)
        if int(session.get("fix_cycles") or 0) >= int(session.get("max_fix_cycles") or 3):
            session["phase"] = "blocked"
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_max_cycles_reached",
                "message": "Safe deploy or module upgrade failed at the maximum cycle limit; exact terminal evidence is preserved.",
                "debug_session": _debug_public_session(session),
                "job": job_data,
                "success_allowed": False,
                "completion_required": False,
                "next_call": None,
            }
        if new_failure_evidence <= 0 and added_evidence <= 0:
            session["phase"] = "awaiting_evidence"
            _debug_session_store(store, session, client_name=client_name)
            return {
                "status": "debug_needs_new_evidence",
                "message": "The deploy failure fingerprint is unchanged. Supply new evidence instead of repeating the same patch.",
                "debug_session": _debug_public_session(session),
                "success_allowed": False,
                "completion_required": True,
                "next_call": _debug_next_call(session, action="continue", required_generated_arguments=["runtime_evidence"]),
            }
        session["phase"] = "diagnosing"
        _debug_session_store(store, session, client_name=client_name)
        return await _debug_begin_code_inspection(payload.model_copy(update={"writes": [], "auto_deploy": True}), session, store)

    if added_evidence > 0:
        session["phase"] = "diagnosing"
        _debug_session_store(store, session, client_name=client_name)
        return await _debug_begin_code_inspection(payload, session, store)

    return {
        "status": f"debug_{session.get('phase') or 'awaiting_evidence'}",
        "message": "Continue this debug session with the generated writes, owned job poll, or fresh runtime/replay evidence requested by the current phase.",
        "debug_session": _debug_public_session(session),
        "success_allowed": False,
        "completion_required": True,
        "next_call": _debug_next_call(
            session,
            action="continue",
            required_generated_arguments=(
                ["writes"]
                if session.get("phase") == "awaiting_writes"
                else ["clarification_answer"]
                if session.get("phase") == "awaiting_clarification"
                else ["runtime_evidence"]
            ),
        ),
    }


async def _workflow_frontend_visual_implement(payload: FrontendVisualImplementRequest) -> dict[str, Any]:
    """Generate deterministic frontend visual assets, then run the normal Odoo workflow.

    This tool is generic for POS/OWL/dashboard/website/backend visuals.  It is
    intentionally a thin planner/generator around workflow.odoo_code_change so
    it inherits the same custom/core reads, QA, verify_against_core, safe deploy,
    and jobs.get polling contract.
    """
    service = FrontendVisualService()
    writes, visual_plan = service.build_writes(payload, payload.module_name)
    if payload.create_module:
        area = (payload.target_area or "backend").lower()
        bundle = "web.assets_frontend" if area == "website" else "point_of_sale._assets_pos" if area == "pos" else "web.assets_backend"
        depends = ["web", "point_of_sale"] if area == "pos" else ["web"]
        asset_paths = [write.relative_path for write in writes if write.relative_path.endswith((".js", ".xml", ".scss", ".css"))]
        manifest = {
            "name": payload.module_name.replace("_", " ").title(),
            "depends": depends,
            "assets": {bundle: asset_paths},
            "installable": True,
            "application": True,
        }
        writes = [
            OdooCodeFileChange(relative_path=f"{payload.module_name}/__init__.py", content=""),
            OdooCodeFileChange(relative_path=f"{payload.module_name}/__manifest__.py", content=repr(manifest) + "\n"),
            *writes,
        ]
    target_files = list(dict.fromkeys([*payload.target_files, *[w.relative_path for w in writes]]))
    description = (
        f"Frontend visual implementation ({visual_plan.get('implementation_type')}): {payload.design_goal}. "
        "Generated visual assets must be treated as implementation files and verified/deployed through the normal Odoo workflow."
    )
    change_payload = OdooCodeChangeRequest(
        client_name=payload.client_name,
        project_path=payload.project_path,
        module_name=payload.module_name,
        create_module=payload.create_module,
        task_description=description[:20000],
        task_type="scss_css" if visual_plan.get("implementation_type") in {"svg_asset", "scss"} else "js_owl",
        intent="frontend_visual_implement",
        target_hint=payload.css_selector or payload.component_type,
        change_request={
            "type": "frontend_visual_implement",
            "target_area": payload.target_area,
            "component_type": payload.component_type,
            "allowed_methods": payload.allowed_methods,
            "reference_images": payload.reference_images,
            "visual_plan": visual_plan,
        },
        patch_strategy="explicit_writes",
        target_files=target_files,
        writes=writes,
        commit_message=payload.commit_message or f"Implement frontend visual {visual_plan.get('asset_name', 'asset')}",
        auto_deploy=payload.auto_deploy,
    )
    result = await _workflow_odoo_code_change(change_payload)
    if isinstance(result, dict):
        result.setdefault("visual_plan", visual_plan)
        result.setdefault("generated_visual_writes", [w.model_dump(mode="json") for w in writes])
        result.setdefault(
            "visual_implementation_note",
            "The implementation method was selected at runtime from the component behavior. Dynamic dimensions use SCSS; SVG is reserved for fixed artwork.",
        )
    return result


def _start_client_validation(req: ClientCheckRequest) -> dict[str, Any]:
    gate = ClientGateService()
    selected = gate.check(req.client_name, validate=False)
    if selected.get("requires_client_selection"):
        return selected

    client_name = str(selected.get("client_name") or req.client_name or "").strip()
    if not client_name:
        return selected
    if selected.get("validation", {}).get("validated") and not req.force_refresh:
        return gate.check(client_name, validate=True, force_refresh=False)

    store = JsonStore()
    with _CLIENT_VALIDATION_START_LOCK:
        running = None
        now = datetime.now(timezone.utc)
        for candidate in store.list_jobs(limit=100):
            if (
                candidate.job_type != "client_validation"
                or candidate.status not in {JobStatus.queued, JobStatus.running}
                or candidate.request.get("client_name") != client_name
            ):
                continue
            try:
                created = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
                created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                age_seconds = (now - created).total_seconds()
            except (TypeError, ValueError):
                age_seconds = _CLIENT_VALIDATION_STALE_SECONDS + 1
            if age_seconds <= _CLIENT_VALIDATION_STALE_SECONDS:
                running = candidate
                break
            store.finish_job(
                candidate,
                JobStatus.failed,
                {
                    "ok": False,
                    "validated": False,
                    "coding_ready": False,
                    "client_name": client_name,
                    "summary": "Stale client-validation job was closed after connector interruption.",
                    "next_call": None,
                },
            )
        if running is not None:
            return {
                "status": "validation_running",
                "validated": False,
                "coding_ready": False,
                "client_name": client_name,
                "job_id": running.id,
                "completion_required": True,
                "success_allowed": False,
                "poll_after_seconds": 2,
                "next_call": running.next_call,
            }

        job = store.create_job(JobRecord(
            job_type="client_validation",
            status=JobStatus.running,
            request={"client_name": client_name, "force_refresh": req.force_refresh},
        ))

    def _background() -> None:
        worker_store = JsonStore()
        try:
            result = ClientGateService().check(
                client_name,
                validate=True,
                force_refresh=req.force_refresh,
            )
            status_ = JobStatus.passed if result.get("validated") else JobStatus.failed
            worker_store.finish_job(worker_store.get_job(job.id), status_, result)
        except Exception as exc:  # noqa: BLE001
            worker_store.finish_job(
                worker_store.get_job(job.id),
                JobStatus.failed,
                {
                    "ok": False,
                    "validated": False,
                    "coding_ready": False,
                    "client_name": client_name,
                    "summary": "Client validation failed.",
                    "error": str(getattr(exc, "detail", exc)),
                    "next_call": None,
                },
            )

    submit_background(_background)
    return {
        "status": "validation_started",
        "validated": False,
        "coding_ready": False,
        "client_name": client_name,
        "job_id": job.id,
        "completion_required": True,
        "success_allowed": False,
        "poll_after_seconds": 2,
        "next_call": job.next_call,
    }


def _fields(req) -> list[dict]:
    return [f.model_dump(exclude_none=True) for f in req.fields]


# One row per workspace tool: (request model, handler(service, request)). Adding a tool is one row
# here plus its descriptor; there is no per-tool branching to keep in sync.
_WS_HANDLERS: dict[str, tuple[type[BaseModel], typing.Callable[[typing.Any, typing.Any], dict]]] = {
    "ws.open": (WsOpenRequest, lambda svc, r: svc.open(r.client_name or "", r.module_name, create=r.create, fresh=r.fresh, branch=r.branch)),
    "ws.status": (WsIdRequest, lambda svc, r: svc.status(r.ws_id)),
    "ws.outline": (WsOutlineRequest, lambda svc, r: svc.outline(r.ws_id, r.paths)),
    "ws.scaffold": (WsScaffoldRequest, lambda svc, r: svc.scaffold(r.ws_id, r.model, _fields(r), with_views=r.with_views, mail=r.mail, menu=r.menu, menu_parent=r.menu_parent, access_group=r.access_group, series=r.series, description=r.description)),
    "ws.extend": (WsExtendRequest, lambda svc, r: svc.extend(r.ws_id, r.model, _fields(r), view_ref=r.view_ref, after_field=r.after_field, position=r.position, view_type=r.view_type, file_name=r.file_name, series=r.series)),
    "ws.symbol": (WsSymbolRequest, lambda svc, r: svc.symbol(r.ws_id, r.model, r.field, r.series)),
    "ws.read": (WsReadRequest, lambda svc, r: svc.read(r.ws_id, r.paths, start_line=r.start_line, max_lines=r.max_lines)),
    "ws.grep": (WsGrepRequest, lambda svc, r: svc.grep(r.ws_id, r.pattern, glob=r.glob, regex=r.regex, scope=r.scope, context=r.context, max_matches=r.max_matches)),
    "ws.edit": (WsEditRequest, lambda svc, r: svc.edit(r.ws_id, [e.model_dump(exclude_none=True) for e in r.edits], strict=r.strict, autofix=r.autofix)),
    "ws.diff": (WsDiffRequest, lambda svc, r: svc.diff(r.ws_id, r.path)),
    "ws.lint": (WsIdRequest, lambda svc, r: svc.lint(r.ws_id)),
    "ws.commit": (WsCommitRequest, lambda svc, r: svc.commit(r.ws_id, r.message, push=r.push, branch=r.branch, allow_lint_errors=r.allow_lint_errors)),
    "ws.discard": (WsIdRequest, lambda svc, r: svc.discard(r.ws_id)),
}


# Single source of truth for ws.* input models (the registry above is derived, not duplicated).
_TOOL_INPUT_MODELS.update({_name: _model for _name, (_model, _) in _WS_HANDLERS.items()})


async def _dispatch_ws(name: str, args: dict) -> dict:
    """Workspace tools do blocking GitHub I/O: run off the event loop; errors come back as structured results."""
    entry = _WS_HANDLERS.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown workspace tool {name}")
    model, handler = entry
    req = model.model_validate(args)
    return await asyncio.to_thread(run_ws, lambda svc: handler(svc, req))


def _missing_checkout_redirect(name: str, args: dict, exc: HTTPException) -> dict[str, Any] | None:
    """Any workflow that dies on a missing local checkout becomes a same-turn ws_open redirect."""
    if name not in CHECKOUT_REDIRECT_TOOLS or exc.status_code != 404 or not is_missing_checkout(exc.detail):
        return None
    if not isinstance(args, dict) or args.get("project_path"):
        return None  # caller pinned a checkout path: keep the explicit error
    try:
        client = ClientResolverService().resolve_client(
            client_name=args.get("client_name"), require_complete=False
        )
    except HTTPException:
        return None
    if client is None or not client.repo:
        return None
    return redirect_from_arguments(args, client.name)


async def _dispatch(name: str, args: dict) -> object:
    try:
        return await _dispatch_inner(name, args)
    except HTTPException as exc:
        redirect = _missing_checkout_redirect(name, args, exc)
        if redirect is None:
            raise
        return redirect


async def _dispatch_inner(name: str, args: dict) -> object:
    if name.startswith("ws."):
        return await _dispatch_ws(name, args)
    if name == "connector.health":
        EmptyRequest.model_validate(args)
        return {
            "status": "ok",
            "service": "odoo-mcp-agent-connector",
            "runtime": runtime_identity(),
            "workflow_progress": WorkflowProgressService.summary(),
        }
    if name == "skills.list":
        req = SkillListRequest.model_validate(args)
        return SkillService().list(req.task_description)
    if name == "skills.read":
        req = SkillReadRequest.model_validate(args)
        return SkillService().read(req.name)
    if name == "artifacts.read":
        req = ArtifactReadRequest.model_validate(args)
        return JsonStore().read_artifact(req.artifact_id, req.offset, req.max_chars)
    if name == "client.check":
        req = ClientCheckRequest.model_validate(args)
        if req.run_validation:
            return _start_client_validation(req)
        return ClientGateService().check(req.client_name, validate=False, force_refresh=req.force_refresh)
    if name == "workflow.agent_instructions":
        try:
            from ..main import agent_instructions

            return agent_instructions()
        except Exception as exc:
            return {"error": str(exc), "fallback": "Call workflow.coding_task_plan for workflow guidance"}
    if name == "workflow.capability_check":
        EmptyRequest.model_validate(args)
        return capability_check()
    if name == "workflow.coding_task_plan":
        return coding_task_plan(CodingTaskPlanRequest.model_validate(args))
    if name == "workflow.odoo_code_change":
        return await _workflow_odoo_code_change(OdooCodeChangeRequest.model_validate(args))
    if name == "workflow.resume_continuation":
        return await _workflow_resume_continuation(ContinuationResumeRequest.model_validate(args))
    if name == "workflow.odoo_debug_and_fix":
        return await _workflow_odoo_debug_and_fix(OdooDebugAndFixRequest.model_validate(args))
    if name == "workflow.module_analyze":
        request = ModuleAnalyzeRequest.model_validate(args)
        return await asyncio.to_thread(_workflow_module_analyze, request)
    if name == "rea.xray_module":
        request = ReaXrayRequest.model_validate(args)
        if request.ws_id:
            def analyze(snapshot):
                graph = ReaXrayService().build(
                    module_path=snapshot["module_path"],
                    module_name=snapshot["module_name"],
                    max_files=request.max_files,
                )
                dependencies = ReaDependencyService().analyze(
                    workspace=snapshot["workspace"],
                    files=snapshot["files"],
                    module_name=snapshot["module_name"],
                )
                runtime_registry = (
                    _rea_runtime_registry(request.client_name, dependencies)
                    if request.include_runtime else {"status": "skipped", "reason": "include_runtime=false"}
                )
                dependencies["runtime_registry"] = runtime_registry
                return {
                    "status": "analyzed",
                    "module_name": snapshot["module_name"],
                    "rea_xray": graph,
                    "rea_dependencies": dependencies,
                    "workspace_source": snapshot["source"],
                    "workspace_coverage": snapshot["coverage"],
                }
            return await asyncio.to_thread(
                run_rea_workspace, request.ws_id, request.module_name, analyze,
                max_files=request.max_files,
            )
        analysis_request = ModuleAnalyzeRequest(
            client_name=request.client_name,
            project_path=request.project_path,
            module_name=request.module_name,
            max_files=request.max_files,
        )
        result = await asyncio.to_thread(_workflow_module_analyze, analysis_request)
        if result.get("status") != "analyzed":
            return result
        result["rea_xray"] = await asyncio.to_thread(
            ReaXrayService().build,
            module_path=Path(result["module_path"]),
            module_name=result["module_name"],
            max_files=request.max_files,
        )
        if request.include_runtime:
            result["rea_runtime_registry"] = _rea_runtime_registry(
                request.client_name,
                {
                    "module_name": result["rea_xray"].get("module_name", request.module_name),
                    "model_ownership": [
                        {"model": node.get("name"), "module": request.module_name}
                        for node in result["rea_xray"].get("nodes", [])
                        if isinstance(node, dict) and node.get("kind") == "model" and node.get("name")
                    ],
                    "cross_module_relationships": [],
                },
            )
        result["inspection_instructions"] = (
            "Use rea_xray.nodes and rea_xray.edges as the static architecture map. "
            "Every edge carries source-path evidence. Runtime registry evidence is separately "
            "labelled and confirms installed model/module metadata only; it does not prove a "
            "specific method executed. Treat missing registry evidence as unresolved, not absent."
        )
        return result
    if name == "rea.upstream_compare":
        request = ReaUpstreamRequest.model_validate(args)
        if request.ws_id:
            def analyze(snapshot):
                report = ReaUpstreamService().compare_workspace(
                    files=snapshot["files"],
                    module_name=snapshot["module_name"],
                    workspace=snapshot["workspace"],
                )
                return {
                    "status": report.get("status", "compared"),
                    "module_name": snapshot["module_name"],
                    "rea_upstream": report,
                    "workspace_source": snapshot["source"],
                    "workspace_coverage": snapshot["coverage"],
                }
            return await asyncio.to_thread(
                run_rea_workspace, request.ws_id, request.module_name, analyze,
            )
        base_request = ModuleAnalyzeRequest(client_name=request.client_name, project_path=request.project_path, module_name=request.module_name)
        result = await asyncio.to_thread(_workflow_module_analyze, base_request)
        if result.get("status") != "analyzed":
            return result
        result["rea_upstream"] = await asyncio.to_thread(
            ReaUpstreamService().compare, module_path=Path(result["module_path"]),
            module_name=result["module_name"], client_name=request.client_name,
        )
        return result
    if name == "rea.runtime_trace":
        request = ReaRuntimeTraceRequest.model_validate(args)
        return await asyncio.to_thread(
            ReaRuntimeTraceService().analyze, events=request.events,
            trace_id=request.trace_id, slow_threshold_ms=request.slow_threshold_ms,
        )
    if name == "rea.self_heal":
        request = ReaSelfHealRequest.model_validate(args)
        analysis_request = ModuleAnalyzeRequest(
            client_name=request.client_name,
            project_path=request.project_path,
            module_name=request.module_name,
            max_files=50,
        )
        analysis = await asyncio.to_thread(_workflow_module_analyze, analysis_request)
        if analysis.get("status") != "analyzed":
            return {
                "status": "blocked",
                "reason": "REA could not resolve a unique addon for the repair workflow.",
                "analysis": analysis,
                "writes_applied": False,
                "deployment_started": False,
            }
        module_path = Path(analysis["module_path"])
        xray = await asyncio.to_thread(
            ReaXrayService().build,
            module_path=module_path,
            module_name=analysis["module_name"],
            max_files=50,
        )
        upstream = await asyncio.to_thread(
            ReaUpstreamService().compare,
            module_path=module_path,
            module_name=analysis["module_name"],
            client_name=request.client_name,
        )
        api_inspection = await asyncio.to_thread(
            ReaApiInspectService().inspect,
            module_path=module_path,
            module_name=analysis["module_name"],
        )
        runtime_trace = None
        if request.runtime_events:
            runtime_trace = await asyncio.to_thread(
                ReaRuntimeTraceService().analyze,
                events=request.runtime_events,
            )
        impact_prediction = None
        if request.proposed_changes:
            impact_prediction = await asyncio.to_thread(
                ReaImpactService().predict,
                module_path=module_path,
                module_name=analysis["module_name"],
                changed_files=[item.model_dump() for item in request.proposed_changes],
                graph=xray,
                runtime_events=request.runtime_events,
                upstream_findings=upstream.get("findings", []),
                api_findings=api_inspection.get("findings", []),
            )
        def _rea_evidence_lines(report: dict[str, Any], limit: int = 12) -> list[str]:
            lines = []
            for item in (report.get("findings") or [])[:limit]:
                evidence = item.get("evidence") or {}
                location = evidence.get("path") or "unknown path"
                if evidence.get("line"):
                    location += f":{evidence['line']}"
                lines.append(
                    f"- {item.get('code') or item.get('type') or 'finding'} "
                    f"({item.get('severity') or 'info'}): {item.get('message') or item.get('symbol') or item.get('method') or ''} [{location}]"
                )
            return lines
        rea_context = [
            "REA EVIDENCE (static and advisory; verify against current source):",
            f"X-Ray: {len(xray.get('nodes', []))} nodes, {len(xray.get('edges', []))} edges; coverage={xray.get('coverage', {})}.",
            f"Upstream: {len(upstream.get('findings', []))} findings; coverage={upstream.get('coverage', {})}.",
            f"External APIs: {api_inspection.get('summary', {})}; coverage={api_inspection.get('coverage', {})}.",
            f"Runtime trace: {runtime_trace.get('summary', {}) if runtime_trace else 'not supplied'}.",
            f"Change impact: {impact_prediction.get('summary', {}) if impact_prediction else 'no proposed changes supplied'}.",
            *_rea_evidence_lines(upstream),
            *_rea_evidence_lines(api_inspection),
        ]
        debug_request = OdooDebugAndFixRequest.model_validate({
            "client_name": request.client_name,
            "project_path": request.project_path or analysis.get("project_path"),
            "module_name": analysis["module_name"],
            "issue_description": request.issue_description + "\n\n" + "\n".join(rea_context),
            "expected_result": request.expected_result,
            "reproduction_steps": request.reproduction_steps,
            "surface": request.surface,
            "action": request.action,
            "debug_session_id": request.debug_session_id,
            "runtime_evidence": [
                *request.runtime_evidence,
                *([f"REA runtime trace summary: {runtime_trace.get('summary', {})}"] if runtime_trace else []),
                *([f"REA impact prediction: {impact_prediction.get('summary', {})}; risk={impact_prediction.get('risk')}"] if impact_prediction else []),
            ],
            "writes": [item.model_dump() for item in request.writes],
            "auto_deploy": request.auto_deploy,
            "max_fix_cycles": request.max_fix_cycles,
        })
        repair = await _workflow_odoo_debug_and_fix(debug_request)
        return {
            "status": repair.get("status", "repair_workflow_result") if isinstance(repair, dict) else "repair_workflow_result",
            "module_name": analysis["module_name"],
            "rea_evidence": {
                "xray_summary": xray.get("summary", {}),
                "upstream": upstream,
                "api_inspection": api_inspection,
                "runtime_trace": runtime_trace,
                "impact_prediction": impact_prediction,
            },
            "repair_workflow": repair,
            "safety": {
                "rea_static_analysis_only": True,
                "writes_applied_by_rea": False,
                "deployment_enabled": request.auto_deploy,
            },
        }
    if name == "rea.impact_predict":
        request = ReaImpactRequest.model_validate(args)
        if request.ws_id:
            def analyze(snapshot):
                graph = ReaXrayService().build(
                    module_path=snapshot["module_path"],
                    module_name=snapshot["module_name"],
                    max_files=50,
                )
                upstream = ReaUpstreamService().compare_workspace(
                    files=snapshot["files"],
                    module_name=snapshot["module_name"],
                    workspace=snapshot["workspace"],
                )
                api_inspection = ReaApiInspectService().inspect(
                    module_path=snapshot["module_path"],
                    module_name=snapshot["module_name"],
                )
                dependencies = ReaDependencyService().analyze(
                    workspace=snapshot["workspace"],
                    files=snapshot["files"],
                    module_name=snapshot["module_name"],
                )
                runtime_registry = (
                    _rea_runtime_registry(request.client_name, dependencies)
                    if request.client_name else {"status": "skipped", "reason": "client_name not supplied"}
                )
                impact = ReaImpactService().predict(
                    module_path=snapshot["module_path"],
                    module_name=snapshot["module_name"],
                    changed_files=[item.model_dump() for item in request.changed_files],
                    graph=graph,
                    runtime_events=request.runtime_events,
                    upstream_findings=upstream.get("findings", []),
                    api_findings=api_inspection.get("findings", []),
                    dependency_map=dependencies,
                    runtime_registry=runtime_registry,
                )
                impact["runtime_registry"] = runtime_registry
                impact["upstream_coverage"] = upstream.get("coverage", {})
                impact["api_coverage"] = api_inspection.get("coverage", {})
                impact["architecture_graph"] = graph.get("summary", {})
                impact["dependency_coverage"] = dependencies.get("coverage", {})
                return {
                    "status": "predicted",
                    "module_name": snapshot["module_name"],
                    "rea_impact": impact,
                    "workspace_source": snapshot["source"],
                    "workspace_coverage": snapshot["coverage"],
                }
            return await asyncio.to_thread(
                run_rea_workspace, request.ws_id, request.module_name, analyze,
            )
        base_request = ModuleAnalyzeRequest(
            client_name=request.client_name,
            project_path=request.project_path,
            module_name=request.module_name,
            max_files=50,
        )
        result = await asyncio.to_thread(_workflow_module_analyze, base_request)
        if result.get("status") != "analyzed":
            return result
        graph = await asyncio.to_thread(
            ReaXrayService().build,
            module_path=Path(result["module_path"]),
            module_name=result["module_name"],
            max_files=50,
        )
        upstream = await asyncio.to_thread(
            ReaUpstreamService().compare,
            module_path=Path(result["module_path"]),
            module_name=result["module_name"],
            client_name=request.client_name,
        )
        api_inspection = await asyncio.to_thread(
            ReaApiInspectService().inspect,
            module_path=Path(result["module_path"]),
            module_name=result["module_name"],
        )
        runtime_registry = (
            _rea_runtime_registry(
                request.client_name,
                {
                    "module_name": result["module_name"],
                    "model_ownership": [
                        {"model": node.get("name"), "module": result["module_name"]}
                        for node in graph.get("nodes", [])
                        if isinstance(node, dict) and node.get("kind") == "model" and node.get("name")
                    ],
                    "cross_module_relationships": [],
                },
            )
            if request.client_name else {"status": "skipped", "reason": "client_name not supplied"}
        )
        result["rea_impact"] = await asyncio.to_thread(
            ReaImpactService().predict,
            module_path=Path(result["module_path"]),
            module_name=result["module_name"],
            changed_files=[item.model_dump() for item in request.changed_files],
            graph=graph,
            runtime_events=request.runtime_events,
            upstream_findings=upstream.get("findings", []),
            api_findings=api_inspection.get("findings", []),
            runtime_registry=runtime_registry,
        )
        result["rea_impact"]["upstream_coverage"] = upstream.get("coverage", {})
        result["rea_impact"]["api_coverage"] = api_inspection.get("coverage", {})
        result["rea_impact"]["architecture_graph"] = graph.get("summary", {})
        return result
    if name == "rea.api_inspect":
        request = ReaApiInspectRequest.model_validate(args)
        if request.ws_id:
            def analyze(snapshot):
                report = ReaApiInspectService().inspect(
                    module_path=snapshot["module_path"],
                    module_name=snapshot["module_name"],
                )
                return {
                    "status": "inspected",
                    "module_name": snapshot["module_name"],
                    "rea_api_inspection": report,
                    "workspace_source": snapshot["source"],
                    "workspace_coverage": snapshot["coverage"],
                }
            return await asyncio.to_thread(
                run_rea_workspace, request.ws_id, request.module_name, analyze,
            )
        base_request = ModuleAnalyzeRequest(client_name=request.client_name, project_path=request.project_path, module_name=request.module_name)
        result = await asyncio.to_thread(_workflow_module_analyze, base_request)
        if result.get("status") != "analyzed":
            return result
        result["rea_api_inspection"] = await asyncio.to_thread(
            ReaApiInspectService().inspect, module_path=Path(result["module_path"]), module_name=result["module_name"]
        )
        return result
    if name == "workflow.module_test":
        request = OdooModuleTestRequest.model_validate(args)
        return ModuleTestService().start(request, self_run)
    if name == "workflow.module_install_and_check":
        return odoo_module_deploy(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "workflow.frontend_visual_implement":
        return await _workflow_frontend_visual_implement(FrontendVisualImplementRequest.model_validate(args))
    if name == "workflow.smart_tool_router":
        return smart_tool_router(SmartToolRouteRequest.model_validate(args))
    if name == "workflow.repo_index":
        return repo_index(RepoIndexRequest.model_validate(args))
    if name == "workflow.full_verify":
        return await full_verify(FullVerifyRequest.model_validate(args))
    if name == "workflow.safe_deploy_module":
        return await safe_deploy_module(SafeDeployModuleRequest.model_validate(args))
    if name == "workflow.verify_deployed_module":
        return verify_deployed_module(DeployedModuleVerifyRequest.model_validate(args))
    if name == "workflow.start_session":
        return start_session(StartSessionRequest.model_validate(args))
    if name == "workflow.registry_smoke_test":
        return registry_smoke_test(RegistrySmokeTestRequest.model_validate(args))
    if name == "workflow.restore_last_good_commit":
        return restore_last_good_commit(RestoreLastGoodCommitRequest.model_validate(args))
    if name == "workflow.browser_menu_crawl":
        return await browser_menu_crawl(MenuCrawlerRequest.model_validate(args))
    if name == "workflow.frontend_pattern_preflight":
        return frontend_pattern_preflight(FrontendPatternPreflightRequest.model_validate(args))
    if name == "qa.self_run":
        return self_run(SelfRunRequest.model_validate(args))
    if name == "repo.prepare":
        return repo_prepare(RepoPrepareRequest.model_validate(args))
    if name == "repo.list_custom_modules":
        return repo_custom_modules(RepoCustomModulesRequest.model_validate(args))
    if name == "repo.list":
        req = RepoListRequest.model_validate(args)
        return RepoService().list_files(req.project_path, req.relative_path, req.max_depth, req.max_items)
    if name == "repo.read":
        req = RepoReadRequest.model_validate(args)
        return RepoService().read_file(req.project_path, req.relative_path, req.max_bytes)
    if name == "repo.read_range":
        req = RepoReadRangeRequest.model_validate(args)
        return RepoService().read_range(req.project_path, req.relative_path, req.start_line, req.max_lines)
    if name == "repo.search":
        req = RepoSearchRequest.model_validate(args)
        return RepoService().search(req.project_path, req.pattern, req.relative_path, req.max_matches, req.context_lines, req.regex)
    if name == "repo.write":
        return repo_write(RepoWriteRequest.model_validate(args))
    if name == "repo.write_many_smart":
        return repo_write_many_smart(RepoWriteManySmartRequest.model_validate(args))
    if name == "repo.write_many_fast_safe":
        return repo_write_many_fast_safe(RepoWriteManyFastSafeRequest.model_validate(args))
    if name == "repo.write_many_atomic":
        return repo_write_many_atomic(RepoWriteManyAtomicRequest.model_validate(args))
    if name == "repo.delete":
        return repo_delete(RepoDeleteRequest.model_validate(args))
    if name == "repo.apply_patch":
        return repo_apply_patch(RepoPatchRequest.model_validate(args))
    if name == "repo.apply_patch_atomic":
        return repo_apply_patch_atomic(RepoAtomicPatchRequest.model_validate(args))
    if name == "repo.clean_pyc":
        return repo_clean_pyc(RepoCleanPycRequest.model_validate(args))
    if name == "git.status":
        req = BaseProjectRequest.model_validate(args)
        return GitService().status(req.project_path)
    if name == "git.current_branch":
        req = BaseProjectRequest.model_validate(args)
        return GitService().current_branch(req.project_path)
    if name == "git.diff":
        req = GitDiffRequest.model_validate(args)
        return GitService().diff(req.project_path, req.staged, req.timeout_seconds)
    if name == "git.commit":
        return git_commit(GitCommitRequest.model_validate(args))
    if name == "git.push":
        return git_push(GitPushRequest.model_validate(args))
    if name == "odoosh.module_install_ssh":
        return odoosh_module_install_ssh(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.health":
        req = ClientOnlyRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).health()
    if name == "odoosh.git_status":
        req = ClientOnlyRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).git_status()
    if name == "odoosh.check_commit":
        req = OdooShCheckCommitRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).check_commit_deployed(req.expected_sha)
    if name == "odoosh.wait_for_commit":
        return odoosh_wait_for_commit(OdooShWaitForCommitRequest.model_validate(args))
    if name == "odoosh.verify_module_manifest":
        return odoosh_verify_module_manifest(OdooShVerifyManifestRequest.model_validate(args))
    if name == "odoosh.read_core_file":
        req = OdooShCoreFileReadRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).read_core_file(req.base, req.relative_path, req.max_bytes)
    if name == "odoosh.list_core_files":
        req = OdooShCoreFilesListRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).list_core_files(req.base, req.relative_path, req.max_items)
    if name == "odoosh.read_user_file":
        req = OdooShUserFileReadRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).read_user_file(req.relative_path, req.max_bytes)
    if name == "odoosh.list_user_files":
        req = OdooShUserFilesListRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).list_user_files(req.module_name, req.subdir, req.max_items)
    if name == "odoosh.log":
        req = OdooShLogRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).tail_log(req.log_name, req.tail_lines)
    if name == "odoosh.grep_errors":
        req = OdooShCommandRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).grep_errors(req.tail_lines)
    if name == "odoosh.error_pack":
        req = OdooShCommandRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return await asyncio.to_thread(OdooShService(client).error_pack, req.tail_lines)
    if name == "odoosh.module_deploy_ssh":
        return odoosh_module_deploy_ssh(OdooShModuleUpdateRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.module_update":
        return odoosh_module_update(OdooShModuleUpdateRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.query":
        req = OdooShQueryRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).search_read(req.model, req.domain, req.fields, req.limit, req.offset, req.order, req.context, req.timeout_seconds)
    if name == "odoo.health":
        req = ClientOnlyRequest.model_validate(args)
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).health()
    if name == "odoo.query":
        request = OdooFunctionalQueryRequest.model_validate(args)
        return await asyncio.to_thread(_odoo_functional_query, request)
    if name == "odoo.module_deploy":
        return odoo_module_deploy(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.module_status":
        req = OdooModuleRequest.model_validate(args)
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).module_status(req.module_name)
    if name == "odoo.module_install":
        return odoo_module_install(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.module_update":
        return odoo_module_update(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.search_read":
        req = OdooSearchReadRequest.model_validate(args)
        data = await asyncio.to_thread(
            _search_read_via_shell,
            model=req.model,
            domain=req.domain,
            fields=req.fields,
            limit=req.limit,
            offset=req.offset,
            order=req.order,
            context=req.context,
            client_name=req.client_name,
        )
        return data.get("odoo") if data.get("status") == "ok" else OdooResult(status="failed", result=data, error=data.get("message") or "Odoo search_read failed")
    if name == "odoo.feature_inspect":
        req = OdooFeatureInspectRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return await asyncio.to_thread(
            _odoo_service_for_client(client).feature_inspect,
            req.models,
            req.sample_fields,
            req.sample_limit,
            req.timeout_seconds,
        )
    if name == "odoo.read_group":
        req = OdooReadGroupRequest.model_validate(args)
        data = await asyncio.to_thread(_read_group_via_shell, req)
        return data.get("odoo") if data.get("status") == "ok" else OdooResult(status="failed", result=data, error=data.get("message"))
    if name == "odoo.fields_get":
        req = OdooFieldsGetRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return await asyncio.to_thread(_odoo_service_for_client(client).fields_get, req.model, req.limit)
    if name == "odoo.list_models":
        req = OdooListModelsRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return await asyncio.to_thread(
            _odoo_service_for_client(client).list_models,
            req.search,
            req.include_transient,
            req.limit,
        )
    if name == "odoo.record_create":
        req = OdooRecordCreateRequest.model_validate(args)
        return await asyncio.to_thread(FunctionalOperationService().create, req)
    if name == "odoo.record_write":
        req = OdooRecordWriteRequest.model_validate(args)
        return await asyncio.to_thread(FunctionalOperationService().write, req)
    if name == "odoo.record_action":
        req = OdooRecordActionRequest.model_validate(args)
        return await asyncio.to_thread(FunctionalOperationService().action, req)
    if name == "odoo.flow_test":
        req = OdooFlowTestRequest.model_validate(args)
        return await asyncio.to_thread(FunctionalOperationService().flow_test, req)
    if name == "odoo.mutation_audit":
        req = OdooMutationAuditRequest.model_validate(args)
        return FunctionalOperationService().audit(req)
    if name == "browser.pos_flow":
        req = PosFlowCheckRequest.model_validate(args)
        return await CdpBrowserService().run_pos_flow(req)
    if name == "browser.screenshot":
        req = BrowserCheckRequest.model_validate(args)
        return await CdpBrowserService().screenshot_odoo_page(
            req.base_url,
            req.login,
            req.password,
            req.start_path,
            req.screenshot_name,
            req.timeout_seconds,
            req.full_page,
        )
    if name == "approvals.list":
        req = LimitRequest.model_validate(args)
        return JsonStore().list_approvals(limit=req.limit)
    if name == "approvals.request":
        req = ApprovalCreateRequest.model_validate(args)
        return JsonStore().create_approval(ApprovalRecord(action=req.action, summary=req.summary, payload_preview=req.payload_preview))
    if name == "approvals.approve":
        req = ApprovalDecisionRequest.model_validate(args)
        store = JsonStore()
        approval = store.get_approval(req.approval_id)
        if approval.status != ApprovalStatus.pending:
            raise HTTPException(status_code=409, detail=f"Approval {req.approval_id} is {approval.status.value}, not pending.")
        return store.set_approval_status(req.approval_id, ApprovalStatus.approved, req.reason)
    if name == "jobs.list":
        req = LimitRequest.model_validate(args)
        return JsonStore().list_jobs(limit=req.limit)
    if name == "jobs.get":
        req = JobGetRequest.model_validate(args)
        watchdog = WorkflowWatchdogService()
        store = JsonStore()
        job = _recover_code_change_job_if_orphaned(store.get_job(req.job_id), store)
        initial_assessment = watchdog.annotate_job(job)
        # Once the same phase has crossed its soft deadline, return control to
        # the agent immediately instead of adding another blocking wait window.
        if (
            req.wait_for_result
            and job.status in {JobStatus.queued, JobStatus.running}
            and not initial_assessment.watchdog.deadline_exceeded
        ):
            job = await asyncio.to_thread(
                store.wait_job,
                req.job_id,
                req.wait_timeout_seconds,
                req.poll_interval_seconds,
            )
        annotated = watchdog.annotate_job(job)
        result = job.result if isinstance(job.result, dict) else {}
        if result.get("status") == "operation_interrupted":
            response = annotated.model_dump(mode="json")
            response.update({
                "state_class": "action_required",
                "recovery_required": True,
                "completion_required": True,
                "success_allowed": False,
                "next_call": result.get("next_call"),
            })
            response.update(_code_change_resume_metadata(job))
            return response
        if job.job_type == "workflow.odoo_code_change":
            response = annotated.model_dump(mode="json")
            response.update(_code_change_resume_metadata(job))
            return response
        return annotated
    if name == "audit.list":
        req = AuditListRequest.model_validate(args)
        return JsonStore().list_audit(limit=req.limit)
    # Fix 3: Resume pipeline from last successful step
    if name == "workflow.resume_job":
        req = ResumeJobRequest.model_validate(args)
        return await _resume_job(req)
    # Fix 7: Session state persistence
    if name == "session.save_state":
        req = SessionStateSaveRequest.model_validate(args)
        return JsonStore().save_session_state(req.state, client_name=req.client_name)
    if name == "session.load_state":
        req = SessionStateLoadRequest.model_validate(args)
        return JsonStore().load_session_state(client_name=req.client_name)
    # Fix 10: Auto-install dependencies
    if name == "odoo.ensure_dependencies":
        req = EnsureDependenciesRequest.model_validate(args)
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).ensure_dependencies(req.module_name)
    # ---------------------------------------------------------------------------
    # V14 — Self-Learning Agent: Core File Discovery
    # ---------------------------------------------------------------------------
    if name == "workflow.find_core_file":
        return find_core_file(FindCoreFileRequest.model_validate(args))
    if name == "workflow.find_similar_view":
        return find_similar_view(FindSimilarViewRequest.model_validate(args))
    if name == "workflow.list_module_files":
        return list_module_files(ListModuleFilesRequest.model_validate(args))
    if name == "workflow.scope_advice":
        return scope_advice(ScopeAdviceRequest.model_validate(args))
    if name == "workflow.verify_against_core":
        return verify_against_core(SyntaxVerifyRequest.model_validate(args))
    if name == "workflow.verify_pos_change":
        return await verify_pos_change(PosChangeVerifyRequest.model_validate(args))
    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# MCP outputSchema derivation (OpenAI/MCP 2025-06-18 "structured content"
# recommendation: declare the shape of a tool's result so the calling model
# doesn't have to guess it from prose). Pydantic-backed tools use their exact
# serialization schema. Dynamic dict/list tools use a permissive family schema
# that documents stable result fields while allowing tool-specific additions.
# ---------------------------------------------------------------------------


def _unwrap_optional_annotation(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _resolve_dispatch_return_annotation(node: ast.AST, namespace: dict) -> Any:
    """Side-effect-free resolution of the return type of the function/method
    called by a `return <expr>` branch inside _dispatch.

    Modules here use `from __future__ import annotations`, so
    inspect.signature(...).return_annotation is a string, not the class.
    typing.get_type_hints() evaluates those forward references using the
    target callable's own __globals__, which is what we need.
    """
    if not isinstance(node, ast.Call):
        return inspect.Signature.empty
    func_node = node.func
    target: Any = None
    try:
        if isinstance(func_node, ast.Attribute):
            base = func_node.value
            if isinstance(base, ast.Call) and isinstance(base.func, ast.Name):
                cls = namespace.get(base.func.id)
                if inspect.isclass(cls):
                    target = getattr(cls, func_node.attr, None)
        elif isinstance(func_node, ast.Name):
            target = namespace.get(func_node.id)
        if callable(target):
            hints = typing.get_type_hints(target)
            return hints.get("return", inspect.Signature.empty)
    except Exception:
        pass
    return inspect.Signature.empty


@lru_cache(maxsize=1)
def _tool_output_schemas() -> dict:
    schemas: dict[str, dict] = {}
    try:
        source = inspect.getsource(_dispatch_inner)
        tree = ast.parse(textwrap.dedent(source))
        namespace = globals()
        func_def = tree.body[0]
        for node in func_def.body:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "name"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
            ):
                continue
            tool_name = test.comparators[0].value
            return_node = None
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    return_node = stmt
            if return_node is None or return_node.value is None:
                continue
            value = return_node.value
            if isinstance(value, ast.Await):
                value = value.value
            annotation = _unwrap_optional_annotation(
                _resolve_dispatch_return_annotation(value, namespace)
            )
            if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                try:
                    schemas[tool_name] = annotation.model_json_schema(mode="serialization")
                except Exception:
                    continue
    except Exception:
        return {}
    return schemas


def _dynamic_tool_output_schema(name: str) -> dict:
    """Return a truthful minimum contract for dynamic tool results.

    MCP result normalization guarantees an object. No field is required because
    success, running, warning, and failure paths legitimately return different
    subsets. ``additionalProperties`` stays enabled for exact domain payloads.
    """
    internal = internal_tool_name(name)
    properties: dict[str, Any] = {
        "status": {"type": "string", "description": "Tool or workflow status."},
        "state_class": {
            "type": "string",
            "enum": ["succeeded", "running", "action_required", "input_required", "failed", "blocked"],
            "description": "Explicit lifecycle class; providers must not infer it from status prose.",
        },
        "message": {"type": "string", "description": "Human-readable outcome or next-step explanation."},
        "error": {"type": "string", "description": "Error detail when the operation failed."},
        "failure_origin": {"type": "string", "description": "Classified origin of a failed operation."},
        "failure_stage": {"type": "string", "description": "Exact workflow phase where failure occurred."},
        "retryable": {"type": "boolean", "description": "Whether repeating through the returned strategy is safe."},
        "next_strategy": {"type": "string", "description": "Bounded recovery strategy for a classified failure."},
        "next_call": {"type": "object", "description": "Exact follow-up tool call; null or absent when terminal.", "additionalProperties": True},
        "input_request": {"type": "object", "description": "Exact structured human input contract when state_class=input_required.", "additionalProperties": True},
        "resume_token": {"type": "string", "description": "Signed client-carried continuation token for ephemeral-server recovery."},
        "resume_token_expires_at": {"type": "string"},
        "resume_token_available": {"type": "boolean"},
        "resume_next_call": {"type": "object", "additionalProperties": True},
    }

    if internal == "connector.health":
        properties.update({"service": {"type": "string"}})
    elif internal == "skills.list":
        properties.update({
            "skills": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "count": {"type": "integer"},
            "loading": {"type": "string"},
            "recommendation": {"type": "object", "additionalProperties": True},
        })
    elif internal == "skills.read":
        properties.update({
            "name": {"type": "string"}, "description": {"type": "string"},
            "content": {"type": "string"}, "source": {"type": "string"},
            "exact": {"type": "boolean"},
        })
    elif internal == "client.check":
        properties.update({
            "validated": {"type": "boolean"}, "client_name": {"type": "string"},
            "coding_ready": {"type": "boolean"}, "deploy_ready": {"type": "boolean"},
            "summary": {"type": "string"}, "job_id": {"type": "string"},
            "completion_required": {"type": "boolean"}, "success_allowed": {"type": "boolean"},
        })
    elif internal == "workflow.module_analyze":
        properties.update({
            "module_name": {"type": "string"}, "project_path": {"type": "string"},
            "git_sha": {"type": "string"}, "cache_hit": {"type": "boolean"},
            "analysis_goal": {"type": "string"}, "manifest": {"type": "object", "additionalProperties": True},
            "inventory": {"type": "object", "additionalProperties": True},
            "architecture": {"type": "object", "additionalProperties": True},
            "validator_findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "source_evidence": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "complete_for_analysis": {"type": "boolean"}, "completion_required": {"type": "boolean"},
            "success_allowed": {"type": "boolean"}, "inspection_instructions": {"type": "string"},
        })
    elif internal.startswith("workflow."):
        properties.update({
            "job_id": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "summary": {"type": "object", "additionalProperties": True},
            "completion_required": {"type": "boolean"}, "success_allowed": {"type": "boolean"},
            "tool_exit_allowed": {"type": "boolean"}, "deploy_confirmed": {"type": "boolean"},
            "already_deployed_confirmed": {"type": "boolean"}, "terminal_action": {"type": "string"},
            "writes_required": {"type": "boolean"}, "inspection": {"type": "object", "additionalProperties": True},
            "write_generation": {"type": "object", "additionalProperties": True},
            "module_structure_complete": {"type": "boolean"},
            "module_contract": {"type": "object", "additionalProperties": True},
            "task_contract": {"type": "object", "additionalProperties": True},
            "persisted_requirements": {"type": "string"},
            "contract_status": {"type": "string"}, "repair_required": {"type": "boolean"},
            "repair_attempts": {"type": "integer"}, "structure_findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "terminal": {"type": "boolean"}, "must_continue": {"type": "boolean"},
            "summary_allowed": {"type": "boolean"}, "blocking_scope": {"type": "string"},
            "debug_session": {"type": "object", "additionalProperties": True},
            "diagnosis": {"type": "object", "additionalProperties": True},
            "handoff": {"type": "object", "additionalProperties": True},
            "diagnosis_evidence": {"type": "array", "items": {"type": "string"}},
            "reproduction_passed": {"type": "boolean"},
            "expected_result_verified": {"type": "boolean"},
            "claim_status": {"type": "string"},
            "truth_gate": {"type": "object", "additionalProperties": True},
        })
    elif internal.startswith("repo."):
        properties.update({
            "path": {"type": "string"}, "content": {"type": "string"},
            "matches": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "changed_files": {"type": "array", "items": {"type": "string"}},
        })
    elif internal.startswith("git."):
        properties.update({
            "sha": {"type": "string"}, "branch": {"type": "string"},
            "stdout": {"type": "string"}, "stderr": {"type": "string"},
            "exit_code": {"type": "integer"},
        })
    elif internal.startswith("odoosh."):
        properties.update({
            "deployed": {"type": "boolean"}, "expected_sha": {"type": "string"},
            "current_sha": {"type": "string"}, "stdout": {"type": "string"},
            "stderr": {"type": "string"}, "exit_code": {"type": "integer"},
            "job_id": {"type": "string"},
        })
    elif internal.startswith("odoo."):
        properties.update({
            "records": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "groups": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "count": {"type": "integer"}, "job_id": {"type": "string"},
            "stdout": {"type": "string"}, "stderr": {"type": "string"},
            "operation": {"type": "string"}, "request_id": {"type": "string"},
            "model": {"type": "string"}, "record_ids": {"type": "array", "items": {"type": "integer"}},
            "verified": {"type": "boolean"}, "success_allowed": {"type": "boolean"},
            "plan": {"type": "object", "additionalProperties": True},
            "stage_results": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        })
    elif internal.startswith("jobs.") or internal.startswith("approvals.") or internal.startswith("audit."):
        properties.update({
            "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "count": {"type": "integer"}, "job_id": {"type": "string"},
            "completion_required": {"type": "boolean"}, "deploy_confirmed": {"type": "boolean"},
            "recovery_required": {"type": "boolean"},
            "terminal_action": {"type": "string"},
        })
        if internal == "jobs.get":
            properties["watchdog"] = {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["active", "stalled", "terminal"]},
                    "phase": {"type": "string"},
                    "protected": {"type": "boolean"},
                    "stalled": {"type": "boolean"},
                    "idle_seconds": {"type": "number", "minimum": 0},
                    "phase_elapsed_seconds": {"type": "number", "minimum": 0},
                    "soft_deadline_seconds": {"type": "number", "exclusiveMinimum": 0},
                    "deadline_exceeded": {"type": "boolean"},
                    "unchanged_observations": {"type": "integer", "minimum": 0},
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                    "redirect_kind": {
                        "type": "string",
                        "enum": [
                            "none", "preserve_protected_work", "use_focused_evidence",
                            "load_exact_source", "reload_handoff", "resume_connector",
                            "retry_provider_bounded", "ask_material_question",
                        ],
                    },
                    "next_action": {
                        "type": ["object", "null"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "continue_with_backoff", "use_focused_evidence",
                                    "load_exact_source", "reload_handoff",
                                    "resume_connector", "retry_provider_bounded",
                                    "ask_material_question",
                                ],
                            },
                            "instruction": {"type": "string", "minLength": 3, "maxLength": 1000},
                        },
                        "required": ["type", "instruction"],
                        "additionalProperties": False,
                    },
                    "agent_instruction": {"type": ["string", "null"], "maxLength": 1000},
                    "poll_after_seconds": {"type": ["integer", "null"]},
                    "cancellation_supported": {"type": "boolean"},
                    "force_cancelled": {"type": "boolean"},
                },
                "required": [
                    "status", "phase", "protected", "stalled", "idle_seconds",
                    "phase_elapsed_seconds", "soft_deadline_seconds", "deadline_exceeded",
                    "unchanged_observations", "action", "reason",
                    "redirect_kind", "next_action",
                    "agent_instruction",
                    "cancellation_supported", "force_cancelled",
                ],
                "additionalProperties": False,
            }
    elif internal == "artifacts.read":
        properties.update({
            "artifact_id": {"type": "string"}, "kind": {"type": ["string", "null"]},
            "source_tool": {"type": ["string", "null"]}, "created_at": {"type": ["string", "null"]},
            "bytes": {"type": "integer"}, "offset": {"type": "integer"},
            "returned_chars": {"type": "integer"}, "next_offset": {"type": ["integer", "null"]},
            "text": {"type": "string"},
        })
    return {"type": "object", "properties": properties, "additionalProperties": True}


def tool_output_schema(name: str) -> dict:
    internal = internal_tool_name(name)
    return deepcopy(_tool_output_schemas().get(internal) or _dynamic_tool_output_schema(internal))


# ---------------------------------------------------------------------------
# Fix 3: Resume job implementation
# ---------------------------------------------------------------------------

_SAFE_DEPLOY_STEPS = [
    "git_diff",
    "git_changed_files",
    "qa_self_run",
    "verify_against_core_pre_push",
    "git_configure_identity",
    "git_validate_target",
    "git_commit",
    "git_head_commit_path_gate",
    "git_head_sha",
    "git_push",
    "git_remote_head_verify",
    "odoosh_check_commit",
    "odoosh_user_file_verify",
    "odoosh_verify_module_manifest",
    "odoo_module_deploy",
    "odoo_module_status",
    "registry_field_verify",
    "combined_live_verification",
    "registry_smoke_test",
]
_SAFE_DEPLOY_STEP_ALIASES = {
    "qa": "qa_self_run",
    "verify": "verify_against_core_pre_push",
    "commit": "git_commit",
    "push": "git_push",
    "wait_odoosh": "odoosh_check_commit",
    "odoosh": "odoosh_check_commit",
    "verify_deployed_code": "odoosh_user_file_verify",
    "verify_user_files": "odoosh_user_file_verify",
    "install": "odoo_module_deploy",
    "upgrade": "odoo_module_deploy",
    "module_install": "odoo_module_deploy",
    "module_update": "odoo_module_deploy",
    "module_upgrade": "odoo_module_deploy",
    "deploy": "odoo_module_deploy",
    "field_verify": "registry_field_verify",
    "registry_fields": "registry_field_verify",
    "smoke_test": "registry_smoke_test",
}
_RESUME_COMPLETED_STATUSES = {"ok", "passed", "skipped"}


def _step_payload(result: object) -> object:
    return result.model_dump(mode="json") if hasattr(result, "model_dump") else result


def _extract_sha_from_steps(job) -> str | None:
    for step in reversed(job.steps or []):
        result = step.result
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        if not isinstance(result, dict):
            continue
        for key in ("sha", "last_sha"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        stdout = str(result.get("stdout") or "")
        match = re.search(r"(?:^|\n)sha=([0-9a-fA-F]{7,40})(?:\n|$)", stdout)
        if match:
            return match.group(1)
        if step.name == "git_head_sha":
            value = stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
                return value
    return None


def _same_resume_project(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return str(left).rstrip("/\\") == str(right).rstrip("/\\")


def _job_matches_resume_request(job, req: "ResumeJobRequest", state: dict) -> bool:
    request = job.request or {}
    req_project = request.get("project_path") or state.get("project_path")
    req_module = request.get("module_name") or state.get("module_name")
    if req.project_path and req_project and not _same_resume_project(str(req_project), req.project_path):
        return False
    if req.module_name and req_module and str(req_module) != req.module_name:
        return False
    return True


def _result_for_completed_step(job, step_name: str) -> object | None:
    for step in reversed(job.steps or []):
        if step.name == step_name and step.status in _RESUME_COMPLETED_STATUSES:
            return step.result
    return None


def _resume_step_ok(resume_results: dict, job, step_name: str) -> bool:
    current = ((resume_results.get("steps_run") or {}).get(step_name) or {}).get("status")
    if current in {"ok", "passed", "skipped"}:
        return True
    return any(step.name == step_name and step.status in _RESUME_COMPLETED_STATUSES for step in (job.steps or []))


def _resume_deploy_confirmation(original_request: dict, resume_results: dict, job) -> dict:
    result: dict = {
        "status": "passed",
        "message": resume_results.get("message") or "Pipeline resumed and completed.",
        "module_name": original_request.get("module_name"),
        "resume": resume_results,
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
