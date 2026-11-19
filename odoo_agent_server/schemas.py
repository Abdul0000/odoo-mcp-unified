Warning: truncated output (original token count: 39518)
Total output lines: 3141

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class Finding(BaseModel):
    severity: Severity
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    hint: str | None = None


class CommandResult(BaseModel):
    command: list[str]
    cwd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class FileContent(BaseModel):
    path: str
    content: str
    size: int
    extra: dict[str, Any] = Field(default_factory=dict)


class FileListItem(BaseModel):
    path: str
    kind: Literal["file", "dir"]
    size: int | None = None


class BaseProjectRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Required when more than one ODOO_CLIENTS entry exists. Prevents accidental reuse of a saved/default project_path.")
    project_path: str = Field(..., description="Absolute path to the checked-out Odoo repo")


class ApprovalMixin(BaseModel):
    approval_id: str | None = Field(default=None, description="Required when approval gating is enabled for this action")




class EmptyRequest(BaseModel):
    """Empty argument object for no-input tools."""


class SkillListRequest(BaseModel):
    task_description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional task text; when provided, return one deterministic skill recommendation without loading its content.",
    )


class SkillReadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Exact skill name or supported short alias from skills.list.")


class ClientOnlyRequest(BaseModel):
    """Optional client selector for tools that otherwise have no inputs."""
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")


class ClientCheckRequest(BaseModel):
    """Select and validate the active ODOO_CLIENTS entry before MCP coding/deploy work."""
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Omit to list clients and require selection when more than one exists.")
    run_validation: bool = Field(
        default=False,
        validation_alias=AliasChoices("validate", "run_validation"),
        serialization_alias="validate",
        description="When true, verify repo/token/branch, checkout, Odoo shell, and Odoo.sh SSH before enabling write/deploy tools.",
    )
    force_refresh: bool = Field(default=False, description="Re-run validation even if this client was already validated in session state.")


class LimitRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=2000)


class AuditListRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)


class JobGetRequest(BaseModel):
    job_id: str = Field(..., min_length=1)
    wait_for_result: bool = Field(default=True, description="Wait server-side for a short bounded poll window; running is not success")
    wait_timeout_seconds: int = Field(
        default=600,
        ge=0,
        le=2400,
        description="Maximum server-side wait for this poll call. Use 0 only for CLI/internal integrations that can safely wait indefinitely.",
    )
    poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")


class ContinuationResumeRequest(BaseModel):
    resume_token: str = Field(
        ...,
        min_length=40,
        max_length=20000,
        description="Signed client-carried token returned by a previous code-change call.",
    )


class ArtifactReadRequest(BaseModel):
    artifact_id: str = Field(..., min_length=1, max_length=120, description="Artifact id returned by a previous tool response")
    offset: int = Field(default=0, ge=0, description="Zero-based character offset for text artifacts")
    max_chars: int = Field(default=12000, ge=1, le=50000, description="Maximum characters to return")


def _safe_module_root_rel(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).replace("\\", "/").strip().strip("/")
    if not clean or "\x00" in clean or any(part in {"", ".", ".."} for part in clean.split("/")):
        raise ValueError("module_root_rel must be a safe repository-relative addon path")
    if Path(clean).is_absolute() or clean == ".git" or clean.startswith(".git/"):
        raise ValueError("module_root_rel must stay inside the repository")
    return clean


class SelfRunRequest(BaseProjectRequest):
    module_name: str = Field(..., description="Custom module folder name inside the repo")
    module_root_rel: str | None = Field(
        default=None,
        description="Resolved repo-relative addon root. Defaults to module_name for root-level addons.",
    )
    task_description: str = Field(default="", max_length=20000, description="Original task used to enforce requested module features before push.")
    create_module: bool = Field(default=False, description="Whether this task creates a new addon; used by the module contract gate.")
    module_contract: dict[str, Any] = Field(default_factory=dict, description="Persisted project/module requirements merged with the current task contract.")
    run_pytest: bool = Field(default=True)
    test_strategy: Literal["auto", "pytest", "odoo_native", "none"] = Field(
        default="auto",
        description=(
            "auto runs bounded pytest only for compatible pure-Python tests and defers Odoo-native "
            "TransactionCase/SavepointCase tests to an Odoo test environment."
        ),
    )
    include_git_status: bool = Field(default=True)
    timeout_seconds: int = Field(default=90, ge=5, le=600)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @field_validator("module_root_rel")
    @classmethod
    def module_root_rel_must_be_safe(cls, value: str | None) -> str | None:
        return _safe_module_root_rel(value)


class SelfRunReport(BaseModel):
    status: Literal["passed", "failed", "warning"]
    project_path: str
    module_path: str
    module_name: str
    findings: list[Finding]
    commands: list[CommandResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == Severity.error)




class RepoPrepareRequest(ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, the active client is auto-resolved when possible.")
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
    project_path: str | None = Field(default=None, description="Exact checkout path. Defaults to selected ODOO_CLIENTS path or TARGET_PROJECT_PATH.")
    repo_name: str | None = Field(default=None, description="Folder name under workspace_root. Defaults to repo URL name or TARGET_PROJECT_PATH name.")
    workspace_root: str | None = Field(default=None, description="Workspace root for clones. Defaults to WORKSPACE_ROOT.")
    remote: str = Field(default="origin")
    timeout_seconds: int = Field(default=180, ge=30, le=900)


class RepoPrepareResult(BaseModel):
    project_path: str
    repo_url: str
    branch: str
    sha: str
    status: str
    already_cloned: bool
    steps: list[CommandResult] = Field(default_factory=list)


class RepoCustomModulesRequest(BaseProjectRequest):
    max_depth: int = Field(default=5, ge=1, le=10)
    include_uninstallable: bool = Field(default=False)


class CustomModuleItem(BaseModel):
    module_name: str
    path: str
    manifest_path: str
    installable: bool | None = None
    name: str | None = None
    version: str | None = None
    author: str | None = None
    application: bool | None = None


class CodingTaskPlanRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, the active client is auto-resolved when possible.")
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
    module_name: str | None = Field(default=None, description="Target custom module folder name, if known.")
    task: str = Field(..., min_length=1, description="Natural-language coding task.")
    project_path: str | None = Field(default=None, description="Existing checkout path, if already known.")


class CodingTaskPlanResult(BaseModel):
    summary: str
    ordered_steps: list[str]
    required_tools: list[str]
    guardrails: list[str]
    defaults: dict[str, Any] = Field(default_factory=dict)


class OdooCodeFileChange(BaseModel):
    """A single file change for the provider-safe composite Odoo workflow.

    Exactly one of `content` or `search`+`replace` should be provided.  The
    connector reads custom + core context, applies the edit, verifies, and starts
    safe deploy as one server-owned operation.
    """
    relative_path: str = Field(..., description="File path inside project_path, normally <module>/...")
    operation: Literal["write", "delete"] = Field(default="write", description="Explicit repository file operation. Defaults to backward-compatible write behavior.")
    mode: Literal["overwrite", "append"] = Field(default="overwrite", description="Write mode forwarded to repo.write for full-content writes.")
    force: bool = Field(default=False, description="Explicitly authorize protected overwrites such as intentional large file shrink operations.")
    content: str | None = Field(default=None, description="Complete replacement file content. Use only when the whole file is intentionally rewritten.")
    search: str | None = Field(default=None, description="Literal or regex text to replace in the existing file.")
    replace: str | None = Field(default=None, description="Replacement text when search is provided.")
    regex: bool = Field(default=False, description="Treat search as a Python regular expression.")
    count: int = Field(default=0, ge=0, le=1000, description="Maximum replacements. 0 means replace all occurrences.")

    @model_validator(mode="after")
    def validate_file_operation(self):
        if self.operation == "delete":
            if self.content is not None or self.search is not None or self.replace is not None:
                raise ValueError("Delete operations cannot include content or search/replace fields")
            if self.mode != "overwrite" or self.force:
                raise ValueError("Delete operations do not accept mode or force")
        elif self.mode == "append" and self.search is not None:
            raise ValueError("Append mode is only supported for full-content writes")
        return self


class OdooTargetResolutionHint(BaseModel):
    """Optional exact target contract for an existing-object change."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "existing", "new"] = "auto"
    kind: Literal[
        "auto", "model", "field", "method", "view", "xml_id", "template",
        "route", "component", "asset", "cron", "action", "menu", "security",
        "config", "record", "other",
    ] = "auto"
    identifier: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_.]+$")
    require_live: bool | None = None


class OdooTargetMatch(BaseModel):
    path: str
    line: int = Field(ge=1)
    kind: str
    identifier: str
    role: Literal["definition", "reference", "text"]
    model: str | None = None
    owner: str | None = None
    source: Literal["custom", "installed"]


class OdooTargetEditPolicy(BaseModel):
    ownership: Literal["client_custom", "odoo_core", "third_party", "mixed", "unknown"] = "unknown"
    strategy: Literal["direct_edit", "inherit_extension", "clarify"] = "clarify"
    direct_edit_paths: list[str] = Field(default_factory=list, max_length=20)
    protected_source_paths: list[str] = Field(default_factory=list, max_length=20)
    reason: str


class OdooTargetResolutionResult(BaseModel):
    status: Literal[
        "not_required", "scope_verified", "verified",
        "source_verified_live_unverified", "source_verified_live_missing",
        "not_found", "ambiguous", "collision",
    ]
    mode: Literal["existing", "new", "auto"]
    kind: str
    identifier: str | None = None
    model: str | None = None
    source_verified: bool = False
    live_required: bool = False
    live_checked: bool = False
    live_status: str = "not_applicable"
    matches: list[OdooTargetMatch] = Field(default_factory=list, max_length=20)
    cache_hit: bool = False
    duration_ms: int = Field(default=0, ge=0)
    writes_blocked: bool = False
    question: str | None = None
    reason: str
    edit_policy: OdooTargetEditPolicy = Field(
        default_factory=lambda: OdooTargetEditPolicy(
            reason="Target ownership was not established from focused source."
        )
    )


class OdooFailureDiagnosis(BaseModel):
    """Bounded, evidence-linked explanation of why a workflow failed."""

    model_config = ConfigDict(extra="forbid")

    failure_category: Literal['unknown','authentication','ssh_network','missing_dependency','provider_transport','custom_traceback','connector_pipeline'] = 'unknown'
    origin: Literal[
        "custom_code",
        "preexisting_baseline",
        "connector_pipeline",
        "provider_transport",
        "missing_context",
        "context_degradation",
        "unknown",
    ] = "unknown"
    crux: str = Field(..., min_length=3, max_length=2000)
    verified_facts: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    unknowns: list[str] = Field(default_factory=list, max_length=20)
    hypothesis: str = Field(..., min_length=3, max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    debug_session_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{32}$",
        description="Exact server-owned debug session containing evidence_ids.",
    )
    next_strategy: Literal[
        "repair_custom_code",
        "preserve_baseline",
        "resume_connector",
        "retry_provider_bounded",
        "load_exact_source",
        "reload_handoff",
        "ask_material_question",
        "collect_focused_evidence",
    ] = "collect_focused_evidence"

    @field_validator("evidence_ids")
    @classmethod
    def diagnosis_evidence_ids_must_be_safe(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            evidence_id = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", evidence_id):
                raise ValueError("evidence_ids must contain safe stable identifiers")
            if evidence_id not in clean:
                clean.append(evidence_id)
        return clean


class OdooOperationHandoff(BaseModel):
    """Durable, bounded continuation state for one multi-phase operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(..., min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    client_name: str | None = Field(default=None, max_length=200)
    project_path: str | None = Field(default=None, max_length=2000)
    module_name: str | None = Field(default=None, max_length=200)
    objective: str = Field(..., min_length=3, max_length=5000)
    locked_scope: list[str] = Field(default_factory=list, max_length=100)
    decisions_with_reasons: list[str] = Field(default_factory=list, max_length=50)
    target_files: list[str] = Field(default_factory=list, max_length=100)
    completed_checkpoints: list[str] = Field(default_factory=list, max_length=100)
    failed_attempt_fingerprints: list[str] = Field(default_factory=list, max_length=20)
    current_blocker: str | None = Field(default=None, max_length=3000)
    next_action: str = Field(..., min_length=3, max_length=3000)
    acceptance_evidence_remaining: list[str] = Field(default_factory=list, max_length=50)
    diagnosis: OdooFailureDiagnosis | None = None
    status: Literal["active", "blocked", "completed"] = "active"
    persistence_status: Literal["persisted", "unavailable", "failed"] = "persisted"
    persistence_error_type: str | None = Field(default=None, max_length=200)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def handoff_must_remain_bounded(self):
        if len(self.model_dump_json()) > 80_000:
            raise ValueError("operation handoff exceeds the bounded context contract")
        return self


class OdooCodeChangeRequest(BaseModel):
    """Provider-safe high-level Odoo code-change workflow.

    This is intentionally composite: remote clients should call this instead of manually
    orchestrating repo.read/write, scope_advice, verify, safe_deploy, and jobs.
    The server enforces the uploaded v15 flow internally: custom read +
    /home/odoo/src/odoo/addons source read first, enterprise fallback only if
    needed, verify_against_core before deploy, then safe_deploy job polling.
    """
    client_name: str | None = Field(default=None, description="Client from ODOO_CLIENTS. Required when multiple clients exist unless project_path resolves it.")
    project_path: str | None = Field(default=None, description="Existing checkout path. If omitted, resolved from client_name and prepared if needed.")
    module_name: str = Field(..., description="Custom module technical name or human hint. The workflow resolves fuzzy hints against actual custom modules before using paths.")
    create_module: bool = Field(default=False, description="Create a missing addon as a direct child of the repository checkout. Requires an exact lowercase technical module_name; nested addon creation is rejected.")
    task_description: str = Field(..., min_length=3, max_length=20000, description="Full natural-language task/change request. Preserve detailed deliverables; identifiers, selectors, XML IDs, routes, and method names are extracted automatically for focused inspection and structure validation.")
    search_patterns: list[str] = Field(default_factory=list, max_length=10, description="Optional search terms to locate target files when writes are not yet provided")
    # Generic task-routing fields.  These are optional shortcuts; explicit writes[] remain
    # the universal path for any Odoo task (Python, XML, QWeb, JS/OWL/POS, SCSS, CSV, data).
    module_hint: str | None = Field(default=None, max_length=200, description="Optional human module hint used by the fuzzy resolver, e.g. 'customer portal'.")
    task_type: str = Field(default="auto", max_length=80, description="Generic task type hint: auto, python_model, xml_view, qweb_template, js_owl, pos_owl, controller, security_csv, manifest, data_xml, scss_css, text_replace, style, etc.")
    intent: str | None = Field(default=None, max_length=160, description="Optional generic intent such as add_field, fix_bug, add_button, change_style, refactor, optimize, add_security, update_report, or modify_existing_feature.")
    target_hint: str | None = Field(default=None, max_length=500, description="Optional generic target hint for search/autopatch, such as a visible label, method, class, XML id, field, route, or file clue.")
    change_request: dict[str, Any] = Field(default_factory=dict, description="Optional structured generic change request. Safe autopatch currently supports style/text replacement shortcuts; all other task types fall back to inspection + writes.")
    target_resolution: OdooTargetResolutionHint = Field(
        default_factory=OdooTargetResolutionHint,
        description="Optional exact target contract. Auto mode stays source-first and only performs one bounded live lookup when useful.",
    )
    patch_strategy: str = Field(default="auto", max_length=80, description="Patch strategy hint: auto, inspect_only, explicit_writes, safe_autopatch, text_replace, style_rule, inline_style, etc.")
    # Backward-compatible convenience fields for simple style/text tasks. They are
    # normalized into target_hint/change_request internally and do not limit the workflow.
    target_text: str | None = Field(default=None, max_length=500, description="Backward-compatible exact visible text for focused inspection/autopatch. Prefer target_hint for new clients.")
    style: dict[str, Any] = Field(default_factory=dict, description="Backward-compatible style patch request. Supported keys: color, font_style/fontStyle, font_weight/fontWeight.")
    allow_autopatch: bool = Field(default=False, description="Enable supported structured style/text autopatches. An explicit quoted replacement such as Replace 'Old' with 'New' is always eligible when it occurs exactly once; ambiguous or complex work remains two-pass.")
    target_files: list[str] = Field(default_factory=list, max_length=50, description="Candidate/target files inside the module to read and use for scope advice")
    writes: list[OdooCodeFileChange] = Field(default_factory=list, max_length=50, description="Actual file edits to apply (maximum 50). If empty, an unambiguous deterministic small edit may complete in one call; complex work returns focused inspection context for an exact second-call write.")
    coding_session_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description=(
            "Server-owned SHA-bound coding session returned by a previous inspect/repair phase. "
            "Pass it unchanged with generated writes to reuse the prepared checkout, module bundle, and source evidence."
        ),
    )
    workspace_session_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description=(
            "Repo-bound workspace context returned by code-change. Preserve it across separate tasks "
            "in the same checkout to reuse up to five independently validated module contexts."
        ),
    )
    operation_id: str | None = Field(default=None, max_length=64, description="Stable idempotency key required for a multi-batch large-module write.")
    batch_index: int = Field(default=0, ge=0, le=19, description="Zero-based local write batch index.")
    total_batches: int = Field(default=1, ge=1, le=20, description="Total local write batches. Only the final batch can reach full QA/deploy.")
    commit_message: str | None = Field(default=None, max_length=300, description="Commit message for safe deploy. Defaults to a concise message from task_description.")
    auto_deploy: bool = Field(default=True, description="After successful write/diff/QA/core-verify, start workflow.safe_deploy_module in background and return jobs.get next_call")
    wait_for_result: bool = Field(
        default=True,
        description=(
            "Internal/REST compatibility mode. Provider MCP calls force this false so the connector "
            "returns a durable job immediately and transport timeouts cannot replay the workflow."
        ),
    )
    force_new_deploy: bool = Field(
        default=False,
        description=(
            "Start a fresh safe-deploy job even when an older resumable job exists for the same module. "
            "Use for a new verified working-tree change after a prior github_pushed_deploy_pending result."
        ),
    )
    verification_override: Literal["strict", "allow_unverified", "force", "none", "irrelevant_only"] = Field(
        default="strict",
        description=(
            "Explicit user-authorized override for workflow.verify_against_core only. "
            "'irrelevant_only' may continue when every CRITICAL finding is a known static-analysis ambiguity; "
            "'force' may continue despite CRITICAL verification findings. Neither mode bypasses blocking QA/parser/manifest errors."
        ),
    )
    verification_override_confirmation: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Required exact confirmation for verification_override='force': "
            "I AUTHORIZE FORCE PUSH AND DEPLOY"
        ),
    )
    verification_override_reason: str | None = Field(
        default=None,
        min_length=3,
        max_length=1000,
        description="Required human-readable reason for any verification override; persisted in deploy/audit output.",
    )
    deploy_mode: Literal["auto", "always_upgrade", "source_only"] = Field(default="auto", description="Automatic impact classification by default. always_upgrade forces -u; source_only is allowed only for verified non-structural changes.")
    max_search_matches: int = Field(default=20, ge=1, le=100, description="Max candidate matches returned during inspect mode")
    investigation_evidence: list[str] = Field(default_factory=list, max_length=50, description="Backward-compatible sanitized evidence summaries. Text alone never unlocks a repeated-failure repair.")
    investigation_diagnosis: "OdooFailureDiagnosis | None" = Field(
        default=None,
        description="Structured diagnosis required to unlock an evidence-backed repair after repeated deployed failures.",
    )
    functional_verification_passed: bool = Field(default=False, description="Set true only when the user-visible behavior was verified; resets repeated-failure attempt history for this issue.")
    clarification_answer: str | None = Field(
        default=None,
        max_length=5000,
        description="User answer to a material clarification requested after focused inspection.",
    )
    verification_assertion: OdooDebugRecordAssertion | None = Field(
        default=None,
        description="Optional bounded live Odoo record/count assertion executed once after deployment and module upgrade.",
    )

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @model_validator(mode="after")
    def code_change_batch_contract_must_be_safe(self):
        if self.batch_index >= self.total_batches:
            raise ValueError("batch_index must be smaller than total_batches")
        if self.total_batches > 1:
            operation_id = str(self.operation_id or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,64}", operation_id):
                raise ValueError("multi-batch writes require a stable 8-64 character operation_id")
            if not self.writes:
                raise ValueError("each explicit multi-batch call requires non-empty writes")
        if self.verification_override not in {"strict", "none"}:
            if not str(self.verification_override_reason or "").strip():
                raise ValueError("verification_override_reason is required when verification_override is enabled")
        if self.verification_override == "force":
            confirmation = self.verification_override_confirmation
            if confirmation is not None and confirmation != "I AUTHORIZE FORCE PUSH AND DEPLOY":
                raise ValueError(
                    "verification_override='force' requires exact confirmation when supplied: "
                    "I AUTHORIZE FORCE PUSH AND DEPLOY"
                )
        return self


class OdooDebugRuntimeReplay(BaseModel):
    """Explicitly authorized Odoo-shell replay of one public record method."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model containing the button/action method.")
    record_ids: list[int] = Field(..., min_length=1, max_length=20, description="Exact records used for replay.")
    method: str = Field(..., description="Public method invoked on the selected recordset.")
    args: list[Any] = Field(default_factory=list, max_length=20, description="JSON arguments passed after the recordset.")
    kwargs: dict[str, Any] = Field(default_factory=dict, description="JSON keyword arguments passed to the method.")
    context: dict[str, Any] = Field(default_factory=dict, description="Bounded Odoo context for matching the UI action.")
    user_id: int | None = Field(default=None, ge=1, description="Optional Odoo user used for access-rule parity.")
    company_id: int | None = Field(default=None, ge=1, description="Optional active company used for replay.")
    commit: bool = Field(default=True, description="Commit successful action changes; failures always roll back.")
    confirm_mutation: bool = Field(
        default=False,
        description="Explicit authorization required when commit=true because action methods may mutate records.",
    )
    timeout_seconds: int = Field(default=120, ge=10, le=600, description="Bounded SSH/Odoo-shell execution timeout.")

    @field_validator("model")
    @classmethod
    def runtime_replay_model_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]+", value):
            raise ValueError("model must be a safe Odoo technical model name")
        return value

    @field_validator("method")
    @classmethod
    def runtime_replay_method_must_be_public(cls, value: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) or value.startswith("_"):
            raise ValueError("method must be a public Odoo method name")
        return value

    @field_validator("record_ids")
    @classmethod
    def runtime_replay_ids_must_be_unique(cls, values: list[int]) -> list[int]:
        if any(int(value) <= 0 for value in values):
            raise ValueError("record_ids must contain positive IDs")
        if len(set(values)) != len(values):
            raise ValueError("record_ids must be unique")
        return values

    @model_validator(mode="after")
    def runtime_replay_contract_must_be_bounded(self):
        if self.commit and not self.confirm_mutation:
            raise ValueError("commit=true requires confirm_mutation=true")
        encoded = repr((self.args, self.kwargs, self.context))
        if len(encoded) > 20_000:
            raise ValueError("runtime replay args/kwargs/context exceed the 20,000 character limit")
        unsafe_keys: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
              …23518 tokens truncated… be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @field_validator("file_type")
    @classmethod
    def file_type_must_be_valid(cls, value: str) -> str:
        allowed = {"model", "view", "wizard", "controller", "report", "data", "security", "static", "manifest", "any"}
        if value not in allowed:
            raise ValueError(f"file_type must be one of: {', '.join(sorted(allowed))}")
        return value


class CoreFileMatch(BaseModel):
    """A single match returned by workflow.find_core_file."""
    path: str = Field(..., description="Absolute path to the found file")
    relative_path: str = Field(..., description="Path relative to its search root")
    root: str = Field(..., description="Which search root this file was found in")
    priority: int = Field(..., description="1=odoo/core, 2=enterprise fallback, 3=user fallback — lower is higher priority")
    size: int = Field(..., description="File size in bytes")


class FindCoreFileResult(BaseModel):
    """Result from workflow.find_core_file."""
    module_name: str
    file_type: str
    keyword: str
    matches: list[CoreFileMatch] = Field(default_factory=list)
    best_match: str | None = Field(default=None, description="Absolute path of the highest-priority match (read this with repo.read)")
    searched_roots: list[str] = Field(default_factory=list)
    total_found: int = 0


class FindSimilarViewRequest(BaseModel):
    """V14: Locate an Odoo core view XML file by model + view_type.

    Use before inheriting/extending any view so the agent reads the
    exact xpath structure, field names, and string attributes before
    writing a <xpath expr="..."> element.

    Search priority: odoo/addons → enterprise → user fallback (same as find_core_file).
    """
    model: str = Field(
        ...,
        description="Odoo model technical name, e.g. 'sale.order', 'account.move', 'stock.picking'",
    )
    view_type: str = Field(
        ...,
        description="View type: 'form', 'tree', 'list', 'kanban', 'search', 'pivot', 'graph', 'calendar', 'any'",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to odoo/addons → enterprise → user fallback.",
    )
    client_name: str | None = Field(default=None, description="Optional ODOO_CLIENTS client used to save pre-write scope evidence in the isolated session state.")

    @field_validator("view_type")
    @classmethod
    def view_type_must_be_valid(cls, value: str) -> str:
        allowed = {"form", "tree", "list", "kanban", "search", "pivot", "graph", "calendar", "activity", "any"}
        if value not in allowed:
            raise ValueError(f"view_type must be one of: {', '.join(sorted(allowed))}")
        return value


class SimilarViewMatch(BaseModel):
    """A single match returned by workflow.find_similar_view."""
    path: str = Field(..., description="Absolute path to the view XML file")
    relative_path: str = Field(..., description="Relative path within its search root")
    root: str = Field(..., description="Which search root this was found in")
    priority: int = Field(..., description="1=odoo/core, 2=enterprise fallback, 3=user fallback")
    model_hint: str = Field(..., description="Model name extracted from file/path heuristic")
    size: int


class FindSimilarViewResult(BaseModel):
    """Result from workflow.find_similar_view."""
    model: str
    view_type: str
    matches: list[SimilarViewMatch] = Field(default_factory=list)
    best_match: str | None = Field(default=None, description="Absolute path of best match — read with repo.read before writing xpath")
    searched_roots: list[str] = Field(default_factory=list)
    total_found: int = 0


class ListModuleFilesRequest(BaseModel):
    """V14+ gap-fix: List all files inside a core or custom module directory.

    Use when you don't know the exact filename/keyword to search for and
    need to browse what exists first (e.g. before find_core_file).
    """
    module_name: str = Field(..., description="Odoo module technical name")
    subdir: str = Field(
        default="",
        description="Optional subdirectory to narrow listing, e.g. 'models', 'views', 'static/src'. Empty = whole module.",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to odoo/addons → enterprise → user fallback.",
    )
    client_name: str | None = Field(default=None, description="Optional ODOO_CLIENTS client used to save pre-write scope evidence in the isolated session state.")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class ModuleFileEntry(BaseModel):
    path: str
    relative_path: str
    size: int
    ext: str


class ListModuleFilesResult(BaseModel):
    module_name: str
    subdir: str
    found: bool
    module_path: str | None = None
    root: str | None = None
    priority: int | None = None
    files: list[ModuleFileEntry] = Field(default_factory=list)
    total_found: int = 0
    message: str | None = None


# ---------------------------------------------------------------------------
# Fix 3: Resume job from last successful step
# ---------------------------------------------------------------------------

class ResumeJobRequest(BaseModel):
    """Fix 3: Resume a safe_deploy_module job from its last successful step.

    Useful when a long multi-step pipeline (10+ steps) fails partway through.
    Instead of starting over from step 1, the agent calls workflow.resume_job
    with the job_id from the failed run.
    """
    job_id: str | None = Field(
        default=None,
        description=(
            "Job ID returned by a previous safe_deploy_module or start_session call. "
            "When omitted, the connector uses the last saved safe-deploy job from session state, "
            "or the latest matching safe_deploy_module job."
        ),
    )
    client_name: str | None = Field(default=None, description="Optional client session to load when job_id is omitted")
    project_path: str | None = Field(default=None, description="Optional project path filter when selecting the latest job without job_id")
    module_name: str | None = Field(default=None, description="Optional module filter when selecting the latest job without job_id")
    override_step: str | None = Field(
        default=None,
        description=(
            "If provided, start from this step name instead of the auto-detected last successful step. "
            "Supported values include actual safe-deploy step names such as "
            "'qa_self_run', 'git_commit', 'git_push', 'odoosh_check_commit', "
            "'odoo_module_deploy', and 'registry_smoke_test'. Aliases "
            "'qa', 'commit', 'push', 'wait_odoosh', 'install', 'upgrade', "
            "'module_install', 'module_update', and 'smoke_test' are accepted."
        ),
    )


# ---------------------------------------------------------------------------
# Fix 7: Session state request schemas
# ---------------------------------------------------------------------------

class SessionStateSaveRequest(BaseModel):
    """Fix 7: Save arbitrary key/value pairs to the session state store."""
    state: dict[str, Any] = Field(..., description="Key/value pairs to merge into the session state")
    client_name: str | None = Field(default=None, description="Phase 3: isolate this save to a specific client's session file. Omit for legacy global behavior.")


class SessionStateLoadRequest(BaseModel):
    """Fix 7: Load the last persisted session state."""
    client_name: str | None = Field(default=None, description="Phase 3: load this client's isolated session state instead of the legacy global one. Recommended once more than one client is configured.")


# ---------------------------------------------------------------------------
# Fix 10: Explicit ensure-dependencies tool request
# ---------------------------------------------------------------------------

class EnsureDependenciesRequest(ApprovalMixin):
    """Fix 10: Auto-install any missing/uninstalled Odoo module dependencies."""
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when multiple clients are configured.")
    module_name: str = Field(..., description="Technical module name whose dependencies should be satisfied")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


# ===========================================================================
# V14+ — Pillar 2: Adaptive Scope Advisor
# ===========================================================================

class ScopeLevel(str, Enum):
    """Adaptive scope level reached during analysis."""
    L0 = "L0"  # Baseline: custom module already read from checkout
    L1 = "L1"  # _inherit chain: read parent model
    L2 = "L2"  # Mixin/abstract: read all mixins
    L3 = "L3"  # Computed fields / ir.model: scan full models/ dir
    L4 = "L4"  # Cross-module comodel: read related module core too


class ScopeReadInstruction(BaseModel):
    """A single mandatory read instruction returned by scope_advice."""
    level: ScopeLevel = Field(..., description="Scope level that triggered this instruction")
    reason: str = Field(..., description="Why this file must be read")
    read_type: str = Field(
        ...,
        description=(
            "Instruction type: core_model | core_model_missing | "
            "core_mixin | core_models_dir_scan | cross_module_manifest"
        ),
    )
    path: str = Field(..., description="Absolute path (use repo.read on this path)")
    mandatory: bool = Field(True, description="If True, must be completed before any code is written")


class ScopeAdviceRequest(BaseModel):
    """V14+: Request adaptive scope analysis for a coding task.

    Call this AFTER reading custom module files from the active repo checkout
    and BEFORE writing any code. The service scans those custom files for
    _inherit, mixins, computed fields, and cross-module refs, then returns an
    ordered list of additional installed-source reads. It should not duplicate
    the current custom module from /home/odoo/src/user.
    """
    custom_module_name: str = Field(
        ...,
        description="Technical name of the custom module being developed, e.g. 'my_sale_ext'",
    )
    custom_file_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Absolute paths of custom Python files already read by the agent "
            "(e.g. /workspace/repos/myrepo/my_module/models/sale_order.py). "
            "Used to detect _inherit, mixins, computed fields for adaptive level detection."
        ),
    )
    task_description: str = Field(
        default="",
        description="Brief description of the coding task (used for logging only)",
    )
    focus_identifiers: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Exact fields, methods, models, XML IDs, routes, or symbols already resolved for this task. "
            "When present, Python scope analysis is limited to the matching source units plus their "
            "model identity instead of scanning unrelated classes in a large file."
        ),
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to odoo/addons → enterprise → user fallback.",
    )
    client_name: str | None = Field(default=None, description="Optional ODOO_CLIENTS client used to save pre-write scope evidence in the isolated session state.")


class ScopeAdviceResult(BaseModel):
    """Adaptive scope advice — ordered list of mandatory read instructions."""
    custom_module: str
    reached_level: ScopeLevel = Field(
        ...,
        description="Highest scope level triggered by the analysis",
    )
    instructions: list[ScopeReadInstruction] = Field(
        default_factory=list,
        description="Ordered list of reads the agent MUST execute before writing code",
    )
    mandatory_count: int = Field(0, description="Number of mandatory (blocking) instructions")
    total_count: int = Field(0, description="Total instructions including optional")
    focus_applied: bool = Field(
        False,
        description="True when exact identifiers constrained analysis to matching source units.",
    )
    focus_identifiers: list[str] = Field(
        default_factory=list,
        description="Exact identifiers used to constrain adaptive scope analysis.",
    )
    warning: str = Field(
        "",
        description="Always-present warning: push blocked until verify_against_core passes",
    )


# ===========================================================================
# V14+ — Pillar 3: Pre-Push Syntax Verifier
# ===========================================================================

class VerifySeverity(str, Enum):
    """Severity of a syntax verify finding."""
    CRITICAL = "CRITICAL"  # Push is BLOCKED until fixed
    WARNING  = "WARNING"   # Should be reviewed but does not block push
    INFO     = "INFO"      # Informational — cross-check passed


class VerifyFinding(BaseModel):
    """A single finding from verify_against_core."""
    file: str = Field(..., description="Absolute path of the file containing the issue")
    severity: VerifySeverity = Field(..., description="CRITICAL blocks push; WARNING is advisory")
    check: str = Field(..., description="Name of the check that fired")
    message: str = Field(..., description="Human-readable description of the mismatch")
    fix_hint: str = Field("", description="What to do to fix this issue")
    line: int | None = Field(default=None, description="Line number in `file` where the issue was found, when known (e.g. real JS parse errors).")

    @computed_field  # type: ignore[misc]
    @property
    def code(self) -> str:
        """Backward-compatible alias used by older tests/clients."""
        return self.check


class SyntaxVerifyRequest(BaseModel):
    """V14+: Pre-push cross-check of written files against Odoo core source.

    Call this BEFORE every git.push. If push_blocked=True in the response,
    fix all CRITICAL findings and call again (max 3 fix cycles).

    Checks performed:
      Python (.py):
        - _inherit model exists in core
        - depends=[] field names exist in core model
        - comodel_name module exists in src roots
        - related= first field exists in core model
        - compute= method defined in the same file

      XML (.xml):
        - xpath @name nodes exist in core view source
        - <field name=...> fields checked against core model

      Manifest (__manifest__.py):
        - depends[] module names exist in /home/odoo/src/* roots
        - asset paths exist and are in the right Odoo bundle

      Frontend assets (.js/.scss/.xml QWeb):
        - JS/OWL module header, import aliases, legacy/modern mixing, template names
        - QWeb t-name/t-inherit/t-call/xpath source existence
        - SCSS patterns that often break Odoo sass compilation
    """
    written_files: list[str] = Field(
        ...,
        description=(
            "Absolute paths of files written by the agent that need pre-push verification. "
            "Include ALL .py, .xml, .js, .scss, .css, .csv, and __manifest__.py files changed in this task."
        ),
    )
    fix_cycle: int | None = Field(
        default=0,
        description="Current fix cycle number (0-indexed). After 3 cycles, the service reports and stops.",
    )
    project_path: str | None = Field(
        default=None,
        description="Optional repo root. When provided, impact-aware validation can include only directly related companion files.",
    )
    client_name: str | None = Field(
        default=None,
        description="Optional ODOO_CLIENTS client context used by core-source sync/verification.",
    )
    impact_aware: bool | None = Field(
        default=None,
        description="Default true via env. Adds only related companion files for template/assets checks; does not scan the full repo.",
    )
    max_related_files: int = Field(
        default=20, ge=0, le=100,
        description="Maximum related files to add during impact-aware validation.",
    )


class SyntaxVerifyResult(BaseModel):
    """Result of workflow.verify_against_core pre-push check."""
    push_blocked: bool = Field(
        ...,
        description="True = push BLOCKED, fix CRITICAL findings first. False = push is allowed.",
    )
    status: str = Field(..., description="Human-readable summary of the verification result")
    files_checked: list[str] = Field(default_factory=list, description="Files that were successfully verified")
    findings: list[VerifyFinding] = Field(default_factory=list, description="All findings (CRITICAL + WARNING + INFO)")
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    fix_cycle: int = 0


class PosChangeVerifyRequest(BaseModel):
    """Combined static + live-browser verification for a POS-touching change.

    This exists because verify_against_core alone only catches static
    pattern mistakes (regex + now real JS parse errors); it never proves the
    POS UI actually still works. This wraps verify_against_core AND
    browser.pos_flow behind one bounded fix-cycle counter, mirroring the
    existing verify_against_core contract (push_blocked / fix_cycle / max 3).
    """
    written_files: list[str] = Field(..., description="Absolute paths of files changed in this task (.py/.xml/.js/.scss/.css/.csv/__manifest__.py).")
    fix_cycle: int = Field(default=0, description="Current fix cycle number (0-indexed). After 3 cycles, stop and report to the user.")
    project_path: str | None = Field(default=None)
    run_pos_flow: bool = Field(default=True, description="If true and any written_files look POS-related, also run a live browser.pos_flow check.")
    pos_flow_request: "PosFlowCheckRequest | None" = Field(default=None, description="Optional overrides (base_url/login/product_name/etc.) for the POS flow step.")


class PosChangeVerifyResult(BaseModel):
    push_blocked: bool
    status: str
    static_result: SyntaxVerifyResult
    pos_flow_result: "PosFlowCheckResult | None" = None
    fix_cycle: int = 0
    message: str

# ===========================================================================
# V15 — Strict Odoo Frontend / OWL / JS / XML Preflight
# ===========================================================================

class FrontendReadInstruction(BaseModel):
    """One source file or module area that must be explored before frontend coding."""
    source: str = Field(..., description="custom | odoo_source")
    module: str = Field(..., description="Odoo module technical name, or '(custom)'")
    subdir: str = Field("", description="Module subdirectory to inspect, e.g. static/src/app")
    reason: str = Field(..., description="Why this pattern source must be read")
    path: str = Field(..., description="Absolute path to read, or NOT_FOUND hint if the source was not found")
    mandatory: bool = Field(True, description="True means the agent must read this before writing code")


class FrontendPatternPreflightRequest(BaseModel):
    """Blocking preflight for Odoo JS/OWL/POS/website/payment/XML asset work.

    Call this after reading the target custom files and before writing any JS,
    OWL component, QWeb/XML template, SCSS asset, POS screen/store patch,
    website snippet/editor code, or payment frontend code.
    """
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS used for client-aware core source discovery.")
    task_description: str = Field(..., description="User task or short implementation description")
    target_area: str = Field(
        default="auto",
        description="auto | pos | website | web | payment. Use auto unless the area is known.",
    )
    target_module_name: str | None = Field(
        default=None,
        description="Custom or core module being edited, if known. Used to inspect local static/src and views first.",
    )
    custom_file_paths: list[str] = Field(
        default_factory=list,
        description="Absolute paths of existing custom files already identified/read for the task.",
    )
    planned_file_paths: list[str] = Field(
        default_factory=list,
        description="Planned changed/new file paths, useful for area auto-detection.",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override source roots. Defaults to odoo/addons → enterprise → user fallback.",
    )


class FrontendPatternPreflightResult(BaseModel):
    """Strict frontend preflight result consumed by the agent before writing."""
    target_area: str = Field(..., description="Detected target area: pos | website | web | payment | none")
    is_frontend_task: bool = Field(..., description="True when strict JS/OWL/XML preflight applies")
    push_blocked_until_done: bool = Field(..., description="True means do not write/push until mandatory reads are completed")
    mandatory_read_count: int = Field(0, description="Number of mandatory read instructions")
    instructions: list[FrontendReadInstruction] = Field(default_factory=list)
    explorer_calls: list[dict[str, str]] = Field(
        default_factory=list,
        description="Suggested workflow.list_module_files calls used/needed for source exploration",
    )
    required_asset_bundles: list[str] = Field(default_factory=list, description="Allowed/expected asset bundles for this area")
    strict_rules: list[str] = Field(default_factory=list, description="Rules the agent must follow while implementing")
    validation_steps: list[str] = Field(default_factory=list, description="Checks to run after implementation")
    message: str = Field(..., description="Human-readable preflight summary")


# ===========================================================================
# V16 — Fast/correct router, repo index, impact-aware validation
# ===========================================================================

class SmartToolRouteRequest(BaseModel):
    """Return the fastest safe MCP workflow for the current task."""
    task_description: str = Field(..., description="User task or implementation summary")
    project_path: str | None = Field(default=None, description="Repo path if already known")
    planned_file_paths: list[str] = Field(default_factory=list, description="Files likely to be read/written")
    changed_file_count: int | None = Field(default=None, ge=0, description="Expected number of files to change")
    risk_level: Literal["auto", "low", "medium", "high"] = Field(default="auto")
    operation: Literal["auto", "read", "write", "deploy"] = Field(
        default="auto",
        description="Explicit intent. Use auto only when the caller genuinely cannot determine whether the task reads, changes, or deploys code.",
    )
    target_surface: Literal["auto", "backend", "frontend"] = Field(
        default="auto",
        description="Optional explicit surface. It takes precedence over fuzzy task wording and inferred file evidence.",
    )


class SmartToolRouteStep(BaseModel):
    order: int
    tool: str
    reason: str
    required: bool = True


class SmartToolRouteResult(BaseModel):
    task_type: str
    frontend_required: bool
    recommended_write_tool: str
    validation_scope: str
    browser_check: str
    steps: list[SmartToolRouteStep] = Field(default_factory=list)
    fast_reason: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"
    evidence: list[str] = Field(default_factory=list)
    clarification_required: bool = False


class RepoIndexRequest(BaseProjectRequest):
    force_refresh: bool = Field(default=False, description="Rebuild even if a fresh cached index exists")
    max_files: int = Field(default=5000, ge=100, le=50000, description="Safety cap for indexed files")


class RepoIndexModule(BaseModel):
    name: str
    path: str
    manifest_path: str
    depends: list[str] = Field(default_factory=list)
    assets: dict[str, list[str]] = Field(default_factory=dict)


class RepoIndexResult(BaseModel):
    project_path: str
    cached: bool
    generated_at: str
    total_files: int = 0
    modules: list[RepoIndexModule] = Field(default_factory=list)
    python_files: list[str] = Field(default_factory=list)
    xml_files: list[str] = Field(default_factory=list)
    js_files: list[str] = Field(default_factory=list)
    scss_files: list[str] = Field(default_factory=list)
    manifest_files: list[str] = Field(default_factory=list)
    template_names: dict[str, str] = Field(default_factory=dict, description="template name -> file path")
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- workspace (ws_*) tools
class WsOpenRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client from ODOO_CLIENTS (omit when only one).")
    module_name: str = Field(..., description="Technical module name, e.g. my_module.")
    create: bool = Field(default=False, description="Start a new module that does not exist yet.")
    fresh: bool = Field(default=False, description="Discard unfinished WIP from an earlier session instead of resuming it.")
    branch: str | None = Field(default=None, description="Branch to work on. Defaults to the client's branch.")


class WsEditItem(BaseModel):
    path: str = Field(..., description="Module-relative path (models/x.py). Prefix repo: for repository-root files.")
    op: Literal["replace", "write", "append", "delete"] | None = Field(default=None, description="Inferred: content->write, search->replace.")
    search: str | None = Field(default=None, description="Exact text to find (replace).")
    replace: str | None = Field(default=None, description="Replacement text (replace).")
    content: str | None = Field(default=None, description="Full file text (write) or text to add (append).")
    regex: bool = Field(default=False, description="Treat search as a regex.")
    expect: int = Field(default=1, ge=0, le=500, description="Expected match count; 0 replaces all.")


class WsEditRequest(BaseModel):
    ws_id: str
    edits: list[WsEditItem] = Field(..., min_length=1, max_length=40, description="Applied atomically: if one fails nothing changes.")
    strict: bool = Field(default=False, description="Roll the batch back when lint finds errors.")
    autofix: bool = Field(default=True, description="Auto-add 'from . import x' and manifest data entries for NEW files.")


class WsReadRequest(BaseModel):
    ws_id: str
    paths: list[str] = Field(..., min_length=1, max_length=10)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=300, ge=1, le=1000)


class WsGrepRequest(BaseModel):
    ws_id: str
    pattern: str
    glob: str | None = Field(default=None, description="Filter, e.g. *.xml or sale_*/**/*.py.")
    regex: bool = False
    scope: Literal["module", "repo"] = Field(default="module", description="repo searches other modules too (narrow with glob).")
    context: int = Field(default=0, ge=0, le=5)
    max_matches: int = Field(default=40, ge=1, le=100)


class WsDiffRequest(BaseModel):
    ws_id: str
    path: str | None = None


class WsIdRequest(BaseModel):
    ws_id: str


class WsCommitRequest(BaseModel):
    ws_id: str
    message: str | None = Field(default=None, min_length=3, max_length=500, description="Optional; a conventional message is generated from the changed files when omitted.")
    push: bool = Field(default=True, description="False only checks readiness.")
    branch: str | None = Field(default=None, description="Push to this NEW branch instead of the client's branch.")
    deploy_after_push: bool = Field(default=False, description="Set true only when the user explicitly requested deployment; otherwise the push stops after publishing.")
    allow_lint_errors: bool = Field(default=False, description="Only when the user explicitly accepts a known-broken push.")


class WsFieldSpec(BaseModel):
    name: str = Field(..., description="snake_case field name.")
    type: Literal["Char", "Text", "Html", "Integer", "Float", "Boolean", "Date", "Datetime", "Selection", "Many2one", "One2many", "Many2many", "Monetary", "Binary"] = "Char"
    comodel: str | None = Field(default=None, description="Target model for Many2one/One2many/Many2many.")
    inverse: str | None = Field(default=None, description="One2many only: the Many2one field name on the other model.")
    options: list[str] | None = Field(default=None, description="Selection only: ['draft:Draft','done:Done'].")
    required: bool = False
    string: str | None = None
    default: str | None = Field(default=None, description="Selection default key.")
    currency_field: str | None = None


class WsOutlineRequest(BaseModel):
    ws_id: str
    paths: list[str] | None = Field(default=None, description="Limit to these files; default is the whole module.")


class WsScaffoldRequest(BaseModel):
    ws_id: str
    model: str = Field(..., description="New model, e.g. my.thing.")
    fields: list[WsFieldSpec] = Field(..., min_length=1, max_length=40)
    with_views: bool = Field(default=True, description="Generate form/list views, action and menu.")
    mail: bool = Field(default=False, description="Add mail.thread/activity mixins, chatter and the mail dependency.")
    menu: str | None = Field(default=None, description="Menu label (default from the model name).")
    menu_parent: str | None = Field(default=None, description="Parent menu xmlid, e.g. sale.sale_menu_root.")
    access_group: str = "base.group_user"
    series: int | None = Field(default=None, ge=14, le=25, description="Odoo major version. Required for a brand-new module; otherwise read from the manifest.")
    description: str | None = None


class WsExtendRequest(BaseModel):
    ws_id: str
    model: str = Field(..., description="Existing model to extend via _inherit, e.g. sale.order.")
    fields: list[WsFieldSpec] = Field(..., min_length=1, max_length=40)
    view_ref: str | None = Field(default=None, description="xmlid of the view to inherit, e.g. sale.view_order_form.")
    after_field: str | None = Field(default=None, description="Existing field in that view to anchor next to (required with view_ref).")
    position: Literal["after", "before", "inside"] = "after"
    view_type: Literal["form", "list", "tree"] = "form"
    file_name: str | None = Field(default=None, description="Override the generated file stem.")
    series: int | None = Field(default=None, ge=14, le=25)


class WsSymbolRequest(BaseModel):
    ws_id: str
    model: str
    field: str | None = Field(default=None, description="Also report this field's type/comodel/compute.")
    series: int | None = Field(default=None, ge=14, le=25)
