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
                    if re.search(r"(password|passwd|token|secret|api[_-]?key|authorization)", str(key), re.I):
                        unsafe_keys.append(str(key))
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit({"args": self.args, "kwargs": self.kwargs, "context": self.context})
        if unsafe_keys:
            raise ValueError("runtime replay cannot persist credential or secret fields")
        return self


class OdooDebugReportReplay(BaseModel):
    """Read-only rendering contract for one exact Odoo report scenario."""

    model_config = ConfigDict(extra="forbid")

    report_xml_id: str | None = Field(
        default=None,
        description="Exact ir.actions.report XML ID; optional when trigger returns the report action.",
    )
    record_ids: list[int] = Field(..., min_length=1, max_length=20, description="Exact records rendered by the report.")
    trigger: OdooDebugRuntimeReplay | None = Field(
        default=None,
        description="Optional read-only public method returning the exact report action used by the UI.",
    )
    data: dict[str, Any] = Field(default_factory=dict, description="Bounded report options/data passed to the native renderer.")
    context: dict[str, Any] = Field(default_factory=dict, description="Bounded UI-equivalent Odoo context.")
    user_id: int | None = Field(default=None, ge=1)
    company_id: int | None = Field(default=None, ge=1)
    required_text: list[str] = Field(default_factory=list, max_length=20)
    forbidden_text: list[str] = Field(default_factory=list, max_length=20)
    expected_pages_min: int | None = Field(default=None, ge=1, le=500)
    expected_pages_max: int | None = Field(default=None, ge=1, le=500)
    case_sensitive: bool = False
    timeout_seconds: int = Field(default=180, ge=10, le=600)

    @field_validator("report_xml_id")
    @classmethod
    def report_xml_id_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*", value):
            raise ValueError("report_xml_id must be an exact safe external ID")
        return value

    @field_validator("record_ids")
    @classmethod
    def report_record_ids_must_be_unique(cls, values: list[int]) -> list[int]:
        if any(int(value) <= 0 for value in values) or len(set(values)) != len(values):
            raise ValueError("record_ids must contain unique positive IDs")
        return values

    @field_validator("required_text", "forbidden_text")
    @classmethod
    def report_text_checks_must_be_bounded(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            fragment = re.sub(r"\s+", " ", str(value or "")).strip()
            if not fragment or len(fragment) > 500:
                raise ValueError("report text checks must contain 1-500 characters")
            if fragment not in clean:
                clean.append(fragment)
        return clean

    @model_validator(mode="after")
    def report_replay_must_be_read_only_and_discriminating(self):
        if self.report_xml_id is None and self.trigger is None:
            raise ValueError("report replay requires report_xml_id or an exact trigger method")
        if self.trigger is not None and self.trigger.commit:
            raise ValueError("report trigger must be read-only (commit=false)")
        if not any((self.required_text, self.forbidden_text, self.expected_pages_min, self.expected_pages_max)):
            raise ValueError("report replay requires a text or page-count assertion")
        if (
            self.expected_pages_min is not None
            and self.expected_pages_max is not None
            and self.expected_pages_min > self.expected_pages_max
        ):
            raise ValueError("expected_pages_min cannot exceed expected_pages_max")
        if len(repr((self.data, self.context))) > 20_000:
            raise ValueError("report data/context exceed the 20,000 character limit")
        return self


class OdooDebugRecordAssertion(BaseModel):
    """Immutable read-only live-Odoo assertion used before and after a fix."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., description="Odoo model queried read-only after replay.")
    domain: list[Any] = Field(default_factory=list, max_length=20, description="Bounded Odoo domain.")
    fields: list[str] = Field(default_factory=list, max_length=20, description="Fields read for exact checks.")
    expected_values: dict[str, Any] = Field(default_factory=dict, description="Exact field values expected.")
    expected_count_min: int | None = Field(default=None, ge=0, le=200, description="Minimum matching rows.")
    expected_count_max: int | None = Field(default=None, ge=0, le=199, description="Maximum matching rows; one overflow row is always queried.")
    match_mode: Literal["first", "any", "all"] = Field(default="any", description="How exact values match rows.")

    @field_validator("model")
    @classmethod
    def debug_assertion_model_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]+", value):
            raise ValueError("model must be a safe Odoo technical model name")
        return value

    @field_validator("fields")
    @classmethod
    def debug_assertion_fields_must_be_safe(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            name = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("fields must contain safe Odoo field names")
            if name not in clean:
                clean.append(name)
        return clean

    @model_validator(mode="after")
    def debug_assertion_must_prove_something(self):
        if (
            self.expected_count_min is None
            and self.expected_count_max is None
            and not self.expected_values
        ):
            raise ValueError("verification assertion requires a count or exact-value expectation")
        if (
            self.expected_count_min is not None
            and self.expected_count_max is not None
            and self.expected_count_min > self.expected_count_max
        ):
            raise ValueError("expected_count_min cannot exceed expected_count_max")
        unsafe_keys = [
            key for key in self.expected_values
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key))
        ]
        if unsafe_keys:
            raise ValueError("expected_values keys must be safe Odoo field names")
        sensitive_keys = [
            key for key in self.expected_values
            if re.search(r"(password|passwd|token|secret|api[_-]?key|authorization)", str(key), re.I)
        ]
        if sensitive_keys:
            raise ValueError("verification assertions cannot persist credential or secret fields")
        missing = sorted(set(self.expected_values) - set(self.fields))
        if missing:
            raise ValueError(f"fields must include expected_values keys: {', '.join(missing)}")
        if self.match_mode == "all" and self.expected_count_max is None:
            raise ValueError("match_mode=all requires expected_count_max for bounded proof")
        return self


class OdooDebugEvidenceRecord(BaseModel):
    """Sanitized provenance record; only trusted records can authorize claims."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal[
        "runtime_log", "traceback", "manual_observation", "code_inspection",
        "write_scope", "qa", "deploy", "module_upgrade", "baseline_assertion",
        "replay_assertion", "runtime_replay", "artifact_replay", "correlated_log",
        "native_source", "instrumentation_cleanup", "recovery",
    ]
    source: Literal["client", "odoo_mcp", "odoo_live", "job_store"]
    summary: str = Field(..., max_length=8000)
    trusted: bool = False
    passed: bool | None = None
    fingerprint: str
    correlation_id: str | None = None
    job_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class OdooDebugAndFixRequest(BaseModel):
    """Bounded runtime-debug orchestrator layered over the normal code workflow."""

    client_name: str | None = Field(default=None, description="Validated ODOO_CLIENTS entry.")
    project_path: str | None = Field(default=None, description="Existing checkout; client path is used when omitted.")
    module_name: str = Field(..., description="Custom addon technical name or safe human hint.")
    issue_description: str = Field(..., min_length=3, max_length=10000, description="Observed runtime error or incorrect behavior.")
    expected_result: str = Field(..., min_length=3, max_length=5000, description="Concrete behavior that proves the issue is fixed.")
    reproduction_steps: list[str] = Field(default_factory=list, max_length=20, description="Bounded steps used to reproduce the issue.")
    surface: Literal[
        "auto", "backend", "controller", "cron", "integration", "frontend",
        "security", "install_upgrade", "performance",
    ] = Field(default="auto", description="Debug surface; auto selects from the issue.")
    action: Literal["auto", "start", "continue", "poll", "verify"] = Field(default="auto", description="Start or continue one persisted debug session.")
    debug_session_id: str | None = Field(default=None, max_length=64, description="Session ID returned by an earlier debug call.")
    operation_id: str | None = Field(default=None, max_length=64, description="Stable idempotency key for start/retry.")
    correlation_id: str | None = Field(default=None, max_length=80, description="Safe request/record correlation identifier.")
    runtime_evidence: list[str] = Field(default_factory=list, max_length=50, description="New sanitized logs, traceback, console, or network evidence.")
    failure_diagnosis: OdooFailureDiagnosis | None = Field(
        default=None,
        description="Optional evidence-linked diagnosis. Server evidence remains authoritative and may override an unsupported origin.",
    )
    clarification_answer: str | None = Field(default=None, max_length=5000, description="User answer requested by the nested code-change target/material clarification gate.")
    verification_evidence: list[str] = Field(default_factory=list, max_length=20, description="Evidence from replaying the same scenario after deploy.")
    verification_assertion: OdooDebugRecordAssertion | None = Field(default=None, description="Live check: model/domain/fields plus expected_values or expected_count bounds.")
    runtime_replay: OdooDebugRuntimeReplay | None = Field(
        default=None,
        description="Optional authorized SSH/Odoo-shell record-method replay used before and after deployment.",
    )
    report_replay: OdooDebugReportReplay | None = Field(
        default=None,
        description="Optional read-only PDF replay and artifact assertion for silent report-output defects.",
    )
    writes: list[OdooCodeFileChange] = Field(default_factory=list, max_length=50, description="Exact writes generated from returned debug/code context.")
    job_id: str | None = Field(default=None, max_length=64, description="Safe-deploy job currently owned by this debug session.")
    reproduction_passed: bool | None = Field(default=None, description="True only when the same scenario passes after deploy.")
    expected_result_verified: bool | None = Field(default=None, description="True only when the expected DB/API/UI result is proven.")
    max_fix_cycles: int = Field(default=3, ge=1, le=5, description="Maximum evidence-based write/deploy cycles.")
    max_surgical_files: int = Field(
        default=12,
        ge=1,
        le=20,
        description="Requested diagnosed-fix file ceiling. The server applies an adaptive floor so clients cannot force fragmented repair cycles.",
    )
    max_surgical_content_chars: int = Field(default=200000, ge=1000, le=500000, description="Maximum supplied replacement content per diagnosed fix.")
    tail_lines: int = Field(default=150, ge=50, le=2000, description="Recent lines scanned remotely for ERROR/CRITICAL traceback blocks.")
    wait_for_result: bool = Field(default=False, description="Poll immediately unless a bounded wait is requested.")
    wait_timeout_seconds: int = Field(default=30, ge=1, le=600, description="Bounded server-side deploy poll window.")
    poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0, description="Delay between deploy-job status checks.")
    auto_deploy: bool = Field(default=True, description="Use the existing safe-deploy pipeline after verified writes.")

    @field_validator("module_name")
    @classmethod
    def debug_module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @field_validator("debug_session_id")
    @classmethod
    def debug_session_id_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("debug_session_id must be the exact 32-character ID returned by the tool")
        return value

    @field_validator("operation_id")
    @classmethod
    def operation_id_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("operation_id contains unsafe characters")
        return value

    @field_validator("correlation_id")
    @classmethod
    def correlation_id_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("correlation_id contains unsafe characters")
        return value

    @model_validator(mode="after")
    def runtime_replay_requires_acceptance_assertion(self):
        if self.runtime_replay is not None and self.report_replay is not None:
            raise ValueError("configure one exact replay scenario: runtime_replay or report_replay")
        if self.runtime_replay is not None and self.verification_assertion is None:
            raise ValueError("runtime_replay requires verification_assertion for independent database proof")
        return self


class ModuleAnalyzeRequest(BaseModel):
    """Fast read-only custom-addon architecture analysis."""
    client_name: str | None = Field(default=None, description="Validated ODOO_CLIENTS entry")
    project_path: str | None = Field(default=None, description="Existing checkout; client path is used when omitted")
    module_name: str = Field(..., description="Custom addon technical name or safe human hint")
    module_hint: str | None = Field(default=None, max_length=200, description="Optional alternate addon hint")
    analysis_goal: str = Field(default="Explain architecture, use cases, and practical gaps", min_length=3, max_length=4000)
    max_files: int = Field(default=15, ge=5, le=50, description="Maximum relevant files; default 15 suits low-memory hosts")
    max_total_chars: int = Field(default=12000, ge=4000, le=12000, description="Bounded source evidence character budget")
    search_patterns: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Exact labels or identifiers ranked before generic files.",
    )
    target_files: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Authoritative module-relative files selected first.",
    )
    live_verify: bool = False
    live_models: list[str] = Field(
        default_factory=list,
        max_length=10,
    )

    @property
    def task_description(self) -> str:
        return self.analysis_goal

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @field_validator("live_models")
    @classmethod
    def live_models_must_be_safe(cls, values: list[str]) -> list[str]:
        ordered: list[str] = []
        for value in values:
            value = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.]+", value):
                raise ValueError(f"Invalid Odoo model name: {value!r}")
            if value not in ordered:
                ordered.append(value)
        return ordered




class ReaXrayRequest(BaseModel):
    """Compact provider contract for bounded, read-only Odoo X-Ray."""

    client_name: str | None = Field(default=None, max_length=120, description="Validated ODOO_CLIENTS entry")
    project_path: str | None = Field(default=None, max_length=1000, description="Existing checkout; client path is used when omitted")
    ws_id: str | None = Field(default=None, max_length=512, description="ws_open ID")
    module_name: str = Field(..., min_length=1, max_length=200, description="Custom addon technical name or safe human hint")
    max_files: int = Field(default=15, ge=5, le=50, description="Maximum addon files to inspect")
    include_runtime: bool = Field(default=True, description="When client_name is explicit, compare static evidence with the live Odoo registry using bounded read-only queries.")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or chr(0) in value or "/" in value or chr(92) in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class ReaUpstreamRequest(BaseModel):
    """Bounded read-only comparison against installed Odoo source."""
    client_name: str | None = Field(default=None, max_length=120)
    project_path: str | None = Field(default=None, max_length=1000)
    ws_id: str | None = Field(default=None, max_length=512, description="ws_open ID")
    module_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or chr(0) in value or "/" in value or chr(92) in value or ".." in value:
            raise ValueError("module_name must be a safe addon hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class ReaApiInspectRequest(BaseModel):
    """Read-only static inventory of external API integrations in an addon."""
    client_name: str | None = Field(default=None, max_length=120)
    project_path: str | None = Field(default=None, max_length=1000)
    ws_id: str | None = Field(default=None, max_length=512, description="ws_open ID")
    module_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or chr(0) in value or "/" in value or chr(92) in value or ".." in value:
            raise ValueError("module_name must be a safe addon hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class ReaRuntimeTraceRequest(BaseModel):
    """Bounded analyzer for exported Odoo runtime trace spans."""
    trace_id: str | None = Field(default=None, max_length=160)
    slow_threshold_ms: float = Field(default=100.0, ge=0, le=600000)
    events: list[dict[str, Any]] = Field(..., min_length=1, max_length=2000)

class ReaImpactChangeFile(BaseModel):
    """One proposed addon-relative file change used for impact prediction."""
    path: str = Field(..., min_length=1, max_length=500)
    change_type: Literal["add", "modify", "delete", "rename"] = "modify"
    diff: str = Field(default="", max_length=20000)


class ReaImpactRequest(BaseModel):
    """Predict likely Odoo addon impact from bounded proposed changes."""
    client_name: str | None = Field(default=None, max_length=120)
    project_path: str | None = Field(default=None, max_length=1000)
    ws_id: str | None = Field(default=None, max_length=512, description="ws_open ID")
    module_name: str = Field(..., min_length=1, max_length=200)
    changed_files: list[ReaImpactChangeFile] = Field(..., min_length=1, max_length=30)
    runtime_events: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or chr(0) in value or "/" in value or chr(92) in value or ".." in value:
            raise ValueError("module_name must be a safe addon hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

class ReaSelfHealRequest(BaseModel):
    """Evidence-led REA repair workflow with deployment disabled by default."""
    client_name: str | None = Field(default=None, max_length=120)
    project_path: str | None = Field(default=None, max_length=1000)
    module_name: str = Field(..., min_length=1, max_length=200)
    issue_description: str = Field(..., min_length=3, max_length=10000)
    expected_result: str = Field(..., min_length=3, max_length=5000)
    reproduction_steps: list[str] = Field(default_factory=list, max_length=20)
    surface: Literal["auto", "backend", "controller", "cron", "integration", "frontend", "security", "install_upgrade", "performance"] = "auto"
    action: Literal["auto", "start", "continue", "poll", "verify"] = "auto"
    debug_session_id: str | None = Field(default=None, max_length=64)
    runtime_evidence: list[str] = Field(default_factory=list, max_length=50)
    runtime_events: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    proposed_changes: list[ReaImpactChangeFile] = Field(default_factory=list, max_length=30)
    writes: list[OdooCodeFileChange] = Field(default_factory=list, max_length=50)
    auto_deploy: bool = Field(default=False, description="Deployment is disabled unless explicitly set true.")
    max_fix_cycles: int = Field(default=3, ge=1, le=5)

    @field_validator("module_name")
    @classmethod
    def self_heal_module_name_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or chr(0) in value or "/" in value or chr(92) in value or ".." in value:
            raise ValueError("module_name must be a safe addon hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class FrontendVisualImplementRequest(BaseModel):
    """Generic Odoo frontend visual implementation workflow.

    Use for POS/OWL/dashboard/website/backend visual tasks where the design may
    need SCSS, an SVG asset, an image-like decorative border, a logo/icon, or
    a background shape.  The tool generates deterministic assets/writes and
    then delegates to the normal workflow.odoo_code_change safety pipeline.
    """
    client_name: str | None = Field(default=None, description="Client from ODOO_CLIENTS. Required when multiple clients exist unless project_path resolves it.")
    project_path: str | None = Field(default=None, description="Existing checkout path. If omitted, resolved from client_name and prepared if needed.")
    module_name: str = Field(..., description="Custom module technical name or human hint.")
    create_module: bool = Field(default=False, description="Create module_name at the repository root when it does not exist. Never creates an addon inside another addon.")
    target_area: str = Field(default="auto", max_length=80, description="Odoo frontend area: pos, dashboard, website, backend, report, or auto.")
    design_goal: str = Field(..., min_length=3, max_length=20000, description="Full natural-language visual goal and deliverables.")
    reference_images: list[str] = Field(default_factory=list, max_length=10, description="Optional local/container paths or URLs to reference screenshots/images for planning.")
    component_type: str = Field(default="auto", max_length=80, description="Visual component type: border, background, logo, icon, card, dashboard, full_ui, illustration, or auto.")
    allowed_methods: list[str] = Field(default_factory=list, max_length=10, description="Allowed implementation methods: scss, svg_asset, owl_xml, js_patch, image_asset. If omitted, the tool chooses safely.")
    asset_name: str | None = Field(default=None, max_length=100, description="Base name for generated visual assets.")
    css_selector: str | None = Field(default=None, max_length=300, description="Optional CSS selector to attach generated visual asset rules to.")
    css_file_path: str | None = Field(default=None, max_length=300, description="Optional module-relative SCSS/CSS file to write. Defaults to static/src/css/<asset_name>.scss.")
    target_files: list[str] = Field(default_factory=list, max_length=50, description="Existing module files to read during the underlying Odoo code-change workflow.")
    variants: list[dict[str, Any]] = Field(default_factory=list, max_length=12, description="Optional SVG variants, e.g. [{name:'active', palette:'gold'}].")
    commit_message: str | None = Field(default=None, max_length=300, description="Commit message. Defaults to a concise message from design_goal.")
    auto_deploy: bool = Field(default=True, description="After generated writes pass verification, start safe deploy and return jobs.get next_call.")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class RepoListRequest(BaseProjectRequest):
    relative_path: str = Field(default=".")
    max_depth: int = Field(default=3, ge=1, le=8)
    max_items: int = Field(default=500, ge=1, le=3000)


class RepoReadRequest(BaseProjectRequest):
    relative_path: str
    max_bytes: int = Field(default=300_000, ge=100, le=2_000_000)


class RepoReadRangeRequest(BaseProjectRequest):
    relative_path: str
    start_line: int = Field(default=1, ge=1, description="1-based first line to read")
    max_lines: int = Field(default=200, ge=1, le=2000, description="Maximum number of lines to return")


class RepoSearchRequest(BaseProjectRequest):
    pattern: str = Field(..., min_length=1, max_length=500, description="Literal text or regex pattern to search for")
    relative_path: str = Field(default=".", description="Directory or file inside project_path to search")
    max_matches: int = Field(default=100, ge=1, le=1000)
    context_lines: int = Field(default=2, ge=0, le=10)
    regex: bool = Field(default=False, description="Treat pattern as a Python regular expression instead of literal text")


class RepoWriteRequest(BaseProjectRequest, ApprovalMixin):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    content: str
    create_dirs: bool = True
    mode: Literal["overwrite", "append"] = Field(
        default="overwrite",
        description=(
            "'overwrite' (default) replaces the entire file with content — this is the ONLY "
            "behavior prior versions supported, preserved here for compatibility. 'append' adds "
            "content to the end of the existing file without touching prior content; the file is "
            "created if it does not exist. There is no 'insert at line' or 'patch' mode here — "
            "use repo.apply_patch for surgical/partial edits to existing content."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "Required to be true when mode='overwrite' targets an existing non-empty file and the "
            "new content is much smaller than the current file (looks like accidental data loss). "
            "Set force=true only after reviewing the returned diff_preview and confirming the "
            "shrink is intentional."
        ),
    )
    # Chunked write support for large files (>1000 lines)
    # chunk_index: 0-based index of this chunk
    # total_chunks: total number of chunks to expect
    # When total_chunks > 1, content is appended to existing file chunks
    chunk_index: int = Field(default=0, ge=0, description="0-based chunk index for large file writes")
    total_chunks: int = Field(default=1, ge=1, description="Total chunks. Use 1 for normal writes. >1 enables chunked mode.")


class RepoWriteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(
        validation_alias=AliasChoices("relative_path", "path"),
        description="Path relative to project_path. The alias 'path' is accepted for compatibility with MCP clients.",
    )
    content: str
    create_dirs: bool = True
    mode: Literal["overwrite", "append"] = Field(
        default="overwrite",
        description="'overwrite' replaces the whole file (default, matches prior behavior). 'append' adds to the end without touching existing content.",
    )
    force: bool = Field(
        default=False,
        description="Required true when mode='overwrite' would shrink an existing non-empty file dramatically — confirms the overwrite is intentional.",
    )


class RepoWriteManyAtomicRequest(BaseProjectRequest, ApprovalMixin):
    model_config = ConfigDict(extra="forbid")

    files: list[RepoWriteItem] = Field(..., min_length=1, max_length=200, description="Files to write as one atomic multi-file transaction")
    delete_paths: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Optional paths to delete in the same rollback-safe transaction as files.",
    )
    check_only: bool = Field(default=False, description="Validate the transaction without changing the real checkout")
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class RepoWriteManyFastSafeRequest(BaseProjectRequest, ApprovalMixin):
    model_config = ConfigDict(extra="forbid")

    files: list[RepoWriteItem] = Field(..., min_length=1, max_length=50, description="Small related files to write using lock + backup + rollback, without git worktree overhead")
    delete_paths: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Optional paths to delete in the same lock + backup + rollback transaction as files.",
    )
    check_only: bool = Field(default=False, description="Validate paths, limits, and parent directories without changing the checkout")
    timeout_seconds: int = Field(default=30, ge=5, le=180, description="Lock timeout for the fast-safe write")


class RepoWriteManySmartRequest(RepoWriteManyAtomicRequest):
    strategy: Literal["auto", "fast_safe", "atomic"] = Field(
        default="auto",
        description=(
            "auto selects fast_safe for small safe writes and atomic for large/risky writes; "
            "fast_safe forces lock+backup+rollback; atomic forces temp git-worktree transaction"
        ),
    )


class RepoDeleteRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str


class RepoPatchRequest(BaseProjectRequest, ApprovalMixin):
    patch: str = Field(..., description="Unified diff to apply inside project_path")
    check_only: bool = Field(default=False)
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class RepoAtomicPatchRequest(RepoPatchRequest):
    """Apply a multi-file patch through an isolated temporary worktree/branch.

    The real checkout is not changed until the whole patch validates in the
    isolated worktree and validates again against the current checkout.
    """


class RepoCleanPycRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str = Field(default=".")


class RepoCleanPycResult(BaseModel):
    project_path: str
    scanned_path: str
    removed_files: list[str] = Field(default_factory=list)
    removed_dirs: list[str] = Field(default_factory=list)


class GitDiffRequest(BaseProjectRequest):
    staged: bool = Field(default=False)
    timeout_seconds: int = Field(default=30, ge=5, le=300)


class GitCommitRequest(BaseProjectRequest, ApprovalMixin):
    message: str = Field(..., min_length=3, max_length=300)
    paths: list[str] = Field(default_factory=list, description="Relative paths to add before commit. Empty means git add -A.")
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class GitPushRequest(BaseProjectRequest, ApprovalMixin):
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to the current branch")
    set_upstream: bool = Field(default=True)
    timeout_seconds: int = Field(default=120, ge=10, le=600)


class OdooShLogName(str, Enum):
    install = "install"
    odoo = "odoo"
    update = "update"
    pip = "pip"


class OdooShLogRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    log_name: OdooShLogName = Field(default=OdooShLogName.odoo)
    tail_lines: int = Field(default=200, ge=20, le=2000)


class OdooShCommandRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    tail_lines: int = Field(default=200, ge=20, le=2000)


class OdooShWaitForCommitRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    expected_sha: str = Field(..., min_length=7, description="Git SHA expected on Odoo.sh ~/src/user")
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    interval_seconds: int = Field(default=10, ge=5, le=60)


class OdooShVerifyManifestRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    module_name: str = Field(..., description="Technical module folder/name to verify on Odoo.sh")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class OdooShUserFileReadRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    relative_path: str = Field(
        ...,
        description="Path relative to /home/odoo/src/user, e.g. sale_order_notebook_tab/models/purchase_order.py",
    )
    max_bytes: int = Field(default=300000, ge=1, le=1000000)

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if not rel or rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("relative_path must stay inside /home/odoo/src/user")
        return rel


class OdooShUserFilesListRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    module_name: str = Field(..., description="Custom module folder under /home/odoo/src/user")
    subdir: str = Field(default="", description="Optional module subdirectory such as models, views, static/src")
    max_items: int = Field(default=200, ge=1, le=2000)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value

    @field_validator("subdir")
    @classmethod
    def subdir_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("subdir must stay inside the module")
        return rel


class OdooShCoreFileReadRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    base: str = Field(default="odoo_addons", description="Source root: odoo_addons (/home/odoo/src/odoo/addons), enterprise (/home/odoo/src/enterprise), or user (/home/odoo/src/user)")
    relative_path: str = Field(
        ...,
        description="Path relative to selected source root, e.g. point_of_sale/static/src/app/generic_components/order_widget/order_widget.xml",
    )
    max_bytes: int = Field(default=500000, ge=1, le=2000000)

    @field_validator("base")
    @classmethod
    def base_must_be_known(cls, value: str) -> str:
        value = (value or "odoo_addons").strip()
        allowed = {"odoo_addons", "enterprise", "user"}
        if value not in allowed:
            raise ValueError(f"base must be one of {sorted(allowed)}")
        return value

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if not rel or rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("relative_path must stay inside the selected source root")
        return rel


class OdooShCoreFilesListRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    base: str = Field(default="odoo_addons", description="Source root: odoo_addons, enterprise, or user")
    relative_path: str = Field(default="", description="Directory relative to selected source root, e.g. point_of_sale/static/src/app")
    max_items: int = Field(default=300, ge=1, le=3000)

    @field_validator("base")
    @classmethod
    def base_must_be_known(cls, value: str) -> str:
        value = (value or "odoo_addons").strip()
        allowed = {"odoo_addons", "enterprise", "user"}
        if value not in allowed:
            raise ValueError(f"base must be one of {sorted(allowed)}")
        return value

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("relative_path must stay inside the selected source root")
        return rel


class OdooShModuleUpdateRequest(ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists and must already be validated with client.check.")
    module_name: str = Field(..., description="Technical module folder/name to update")
    project_path: str | None = Field(default=None, description="Optional local checkout path used to prove local HEAD is pushed before manual Odoo.sh update/install. If omitted, session state is used when available.")
    remote: str = Field(default="origin", description="Git remote used for the local-vs-remote HEAD safety check")
    branch: str | None = Field(default=None, description="Git branch used for the local-vs-remote HEAD safety check. Defaults to current branch.")
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    expected_sha: str | None = Field(default=None, description="Pushed Git SHA that must be deployed on Odoo.sh before update. If omitted, manual Odoo.sh tools require local HEAD to match remote branch HEAD before running.")
    wait_for_result: bool = Field(default=True, description="Wait server-side for background SSH job until done or wait timeout")
    wait_timeout_seconds: int = Field(
        default=20,
        ge=0,
        le=2400,
        description="Maximum server-side wait for final job result. Use 0 to wait indefinitely until terminal pass/fail/warning.",
    )
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class OdooModuleRequest(ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists and must already be validated with client.check.")
    module_name: str = Field(..., description="Technical module name")
    project_path: str | None = Field(default=None, description="Optional local checkout path used by manual Odoo.sh SSH install to prove local HEAD is pushed before running. If omitted, session state is used when available.")
    remote: str = Field(default="origin", description="Git remote used for the local-vs-remote HEAD safety check")
    branch: str | None = Field(default=None, description="Git branch used for the local-vs-remote HEAD safety check. Defaults to current branch.")
    expected_sha: str | None = Field(default=None, description="Optional pushed Git SHA that must be deployed on Odoo.sh before manual SSH install/update. If omitted, local HEAD must match remote branch HEAD.")
    wait_for_result: bool = Field(default=True, description="For background install/upgrade/deploy, wait server-side until the job finishes or times out")
    wait_timeout_seconds: int = Field(
        default=20,
        ge=0,
        le=2400,
        description="Maximum server-side wait for final job result. Use 0 to wait indefinitely until terminal pass/fail/warning.",
    )
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")
    deploy_guard_timeout_seconds: int = Field(default=300, ge=0, le=600, description="Maximum server-side wait for a recent manual git.push SHA to appear on Odoo.sh before install/upgrade")
    deploy_guard_poll_interval_seconds: float = Field(default=10.0, ge=0.25, le=60.0, description="Polling interval for the deploy guard used by direct module install/upgrade calls")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class DeployedModuleVerifyRequest(BaseProjectRequest):
    module_name: str = Field(..., description="Custom module folder/name to compare against /home/odoo/src/user")
    module_root_rel: str | None = Field(default=None, description="Resolved repo-relative addon root; defaults to module_name.")
    paths: list[str] = Field(default_factory=list, description="Repo-relative files to compare. Empty compares module code/XML/assets up to max_files.")
    max_files: int = Field(default=200, ge=1, le=1000, description="Maximum module files to compare when paths is empty")
    include_module_status: bool = Field(default=True, description="Also read live Odoo module installation state")

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


class OdooSearchReadRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when multiple clients are configured unless the request can be resolved unambiguously.")
    model: str
    domain: list[Any] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    order: str | None = Field(default=None, description="Optional Odoo order string, e.g. 'create_date desc'")
    context: dict[str, Any] | None = Field(default=None, description="Optional Odoo context passed to search_read")


class OdooFeatureInspectRequest(BaseModel):
    """One-shell, read-only live registry inspection for a bounded feature."""
    client_name: str | None = Field(default=None, description="Selected ODOO_CLIENTS entry")
    models: list[str] = Field(..., min_length=1, max_length=10, description="Exact technical models resolved from source analysis")
    sample_fields: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Optional explicitly projected business fields per model; samples are omitted when empty",
    )
    sample_limit: int = Field(default=0, ge=0, le=10, description="Maximum sample records per model; zero disables record reads")
    timeout_seconds: int = Field(default=60, ge=15, le=180)

    @field_validator("models")
    @classmethod
    def feature_models_must_be_safe(cls, values: list[str]) -> list[str]:
        ordered: list[str] = []
        for value in values:
            value = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.]+", value):
                raise ValueError(f"Invalid Odoo model name: {value!r}")
            if value not in ordered:
                ordered.append(value)
        return ordered

    @field_validator("sample_fields")
    @classmethod
    def feature_sample_fields_must_be_safe(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(value) > 10:
            raise ValueError("sample_fields supports at most 10 models")
        cleaned: dict[str, list[str]] = {}
        for model, fields in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.]+", str(model)):
                raise ValueError(f"Invalid Odoo model name: {model!r}")
            if len(fields) > 20 or any(not re.fullmatch(r"[A-Za-z0-9_]+", str(field)) for field in fields):
                raise ValueError("Each sample field list must contain at most 20 safe field names")
            cleaned[str(model)] = list(dict.fromkeys(str(field) for field in fields))
        return cleaned


class OdooReadGroupRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when multiple clients are configured.")
    model: str = Field(..., description="Odoo model to aggregate, e.g. account.move")
    domain: list[Any] = Field(default_factory=list, description="Read-only Odoo domain")
    fields: list[str] = Field(default_factory=lambda: ["id:count"], description="Aggregate fields, e.g. id:count or amount_total:sum")
    groupby: list[str] = Field(..., min_length=1, max_length=5, description="Grouping fields, e.g. state or invoice_date:month")
    lazy: bool = Field(default=False, description="Return all requested grouping levels when false")
    context: dict[str, Any] | None = Field(default=None, description="Optional Odoo context passed to read_group")
    timeout_seconds: int = Field(default=120, ge=15, le=300)


class OdooFieldsGetRequest(BaseModel):
    """Read-only discovery of the fields on one Odoo model."""

    client_name: str | None = Field(default=None, description="Selected ODOO_CLIENTS entry.")
    model: str = Field(..., description="Odoo technical model name, e.g. sale.order")
    limit: int = Field(default=500, ge=1, le=2000, description="Maximum fields to return")

    @field_validator("model")
    @classmethod
    def fields_get_model_must_be_safe(cls, value: str) -> str:
        value = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]+", value):
            raise ValueError("model must be a safe Odoo technical model name")
        return value


class OdooListModelsRequest(BaseModel):
    """Read-only discovery of the Odoo data model (ir.model)."""

    client_name: str | None = Field(default=None, description="Selected ODOO_CLIENTS entry.")
    search: str | None = Field(
        default=None,
        max_length=200,
        description="Optional substring matched against the technical model name.",
    )
    include_transient: bool = Field(default=True, description="Include wizard/transient models")
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum models to return")

    @field_validator("search")
    @classmethod
    def list_models_search_must_be_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if "\x00" in value:
            raise ValueError("search must not contain NUL bytes")
        return value


class OdooMutationBaseRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Selected ODOO_CLIENTS entry.")
    request_id: str = Field(..., min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    context: dict[str, Any] | None = Field(default=None, description="Bounded safe Odoo context.")
    approval_id: str | None = Field(default=None, description="Human approval for sensitive actions.")


class OdooRecordCreateRequest(OdooMutationBaseRequest):
    model: str = Field(..., pattern=r"^[a-zA-Z0-9_.]+$")
    records: list[dict[str, Any]] = Field(..., min_length=1, max_length=100)
    verify_fields: list[str] = Field(default_factory=list, max_length=30)


class OdooRecordWriteRequest(OdooMutationBaseRequest):
    model: str = Field(..., pattern=r"^[a-zA-Z0-9_.]+$")
    ids: list[int] = Field(..., min_length=1, max_length=100)
    values: dict[str, Any] = Field(..., min_length=1)
    expected_values: dict[str, Any] = Field(
        default_factory=dict,
        description="Optimistic concurrency values that every selected record must currently match.",
    )
    verify_fields: list[str] = Field(default_factory=list, max_length=30)


class OdooRecordActionRequest(OdooMutationBaseRequest):
    model: str = Field(..., pattern=r"^[a-zA-Z0-9_.]+$")
    ids: list[int] = Field(..., min_length=1, max_length=100)
    action: str = Field(..., pattern=r"^[a-zA-Z0-9_]+$")
    arguments: list[Any] = Field(default_factory=list, max_length=10)
    keyword_arguments: dict[str, Any] = Field(default_factory=dict)
    verify_fields: list[str] = Field(default_factory=list, max_length=30)


class OdooMutationAuditRequest(BaseModel):
    client_name: str | None = Field(default=None)
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class OdooDynamicFlowStep(BaseModel):
    name: str = Field(..., min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    operation: Literal["create", "write", "action", "verify"]
    model: str = Field(..., pattern=r"^[a-zA-Z0-9_.]+$")
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    values: dict[str, Any] = Field(default_factory=dict)
    ids: list[int] = Field(default_factory=list, max_length=100)
    ids_from: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+|\.\d+)*$",
        description="Reference to an earlier step result, e.g. create_order.record_ids.",
    )
    action: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_]+$")
    arguments: list[Any] = Field(default_factory=list, max_length=10)
    keyword_arguments: dict[str, Any] = Field(default_factory=dict)
    expected_values: dict[str, Any] = Field(default_factory=dict)
    verify_fields: list[str] = Field(default_factory=list, max_length=30)
    approval_id: str | None = None

    @model_validator(mode="after")
    def validate_operation_contract(self):
        if self.operation == "create" and not self.records:
            raise ValueError("create step requires records")
        if self.operation == "write" and not self.values:
            raise ValueError("write step requires values")
        if self.operation == "action" and not self.action:
            raise ValueError("action step requires action")
        if self.operation != "create" and not self.ids and not self.ids_from:
            raise ValueError(f"{self.operation} step requires ids or ids_from")
        return self


class OdooFlowTestRequest(OdooMutationBaseRequest):
    flow: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal["preview", "persistent_test", "live"] = "preview"
    values: dict[str, Any] = Field(default_factory=dict)
    stages: list[str] = Field(default_factory=list, max_length=10)
    verify_fields: list[str] = Field(default_factory=list, max_length=30)
    steps: list[OdooDynamicFlowStep] = Field(
        default_factory=list,
        max_length=30,
        description="Ordered dynamic create/write/action/verify steps. Exact {'$ref': 'step.path'} objects may reference prior outputs.",
    )


class OdooModuleFunctionalCase(BaseModel):
    """One bounded functional scenario executed through the existing safe engine."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    flow: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    values: dict[str, Any] = Field(default_factory=dict)
    stages: list[str] = Field(default_factory=list, max_length=10)
    verify_fields: list[str] = Field(default_factory=list, max_length=30)
    steps: list[OdooDynamicFlowStep] = Field(default_factory=list, max_length=30)
    approval_id: str | None = Field(
        default=None,
        description="Payload-bound human approval for sensitive business actions in this scenario.",
    )


class OdooEndpointTestCase(BaseModel):
    """One same-origin Odoo controller/API contract test."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(..., min_length=1, max_length=500)
    protocol: Literal["http", "odoo_jsonrpc"] = "http"
    auth: Literal["public", "session"] = "session"
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default_factory=dict)
    expected_status: int = Field(default=200, ge=100, le=599)
    expected_json_subset: Any | None = None
    expected_body_contains: list[str] = Field(default_factory=list, max_length=20)
    record_assertions: list[OdooDebugRecordAssertion] = Field(default_factory=list, max_length=10)
    timeout_seconds: int = Field(default=20, ge=1, le=60)

    @field_validator("path")
    @classmethod
    def endpoint_path_must_be_same_origin_relative(cls, value: str) -> str:
        path = str(value or "").strip()
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "://" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError("path must be a same-origin relative Odoo path beginning with one slash")
        return path

    @field_validator("headers")
    @classmethod
    def endpoint_headers_must_be_safe(cls, value: dict[str, str]) -> dict[str, str]:
        blocked = {"authorization", "cookie", "host", "proxy-authorization", "x-forwarded-host"}
        if len(value) > 20:
            raise ValueError("headers supports at most 20 entries")
        clean: dict[str, str] = {}
        for key, item in value.items():
            name = str(key or "").strip()
            text = str(item or "")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", name):
                raise ValueError(f"invalid HTTP header name: {key!r}")
            if name.lower() in blocked:
                raise ValueError(f"header {name!r} is connector-owned and cannot be supplied")
            if len(text) > 2000 or "\r" in text or "\n" in text:
                raise ValueError(f"invalid HTTP header value for {name!r}")
            clean[name] = text
        return clean

    @model_validator(mode="after")
    def endpoint_payload_must_be_bounded(self):
        payload = {
            "query": self.query,
            "body": self.body,
            "expected_json_subset": self.expected_json_subset,
        }
        if len(str(payload)) > 100_000:
            raise ValueError("endpoint test payload is too large")
        if self.method == "GET" and self.body not in ({}, None):
            raise ValueError("GET endpoint tests must use query parameters instead of a request body")
        return self


class OdooModuleTestRequest(BaseProjectRequest):
    """Run bounded evidence-producing tests without entering the deploy pipeline."""

    module_name: str = Field(..., min_length=1, max_length=120)
    layers: list[Literal["auto", "all", "static", "native", "api", "functional"]] = Field(
        default_factory=lambda: ["auto"],
        min_length=1,
        max_length=4,
        description="auto runs static plus configured/discovered layers; all requires every layer.",
    )
    native_test_tags: list[str] = Field(default_factory=list, max_length=20)
    api_cases: list[OdooEndpointTestCase] = Field(default_factory=list, max_length=20)
    functional_cases: list[OdooModuleFunctionalCase] = Field(default_factory=list, max_length=20)
    allow_endpoint_mutations: bool = Field(
        default=False,
        description="Explicitly allow POST/PUT/PATCH/DELETE cases on a client marked environment=test.",
    )
    timeout_seconds: int = Field(default=600, ge=30, le=1800)

    @field_validator("module_name")
    @classmethod
    def module_test_name_must_be_safe(cls, value: str) -> str:
        name = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("module_name must be a safe Odoo technical name")
        return name

    @field_validator("native_test_tags")
    @classmethod
    def native_test_tags_must_be_safe(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            tag = str(value or "").strip()
            if not re.fullmatch(r"[-+*A-Za-z0-9_./:]+", tag):
                raise ValueError(f"unsafe Odoo test tag selector: {value!r}")
            if tag not in clean:
                clean.append(tag)
        return clean

    @model_validator(mode="after")
    def module_test_layers_must_be_unambiguous(self):
        unique = list(dict.fromkeys(self.layers))
        if len(unique) != len(self.layers):
            raise ValueError("layers must not contain duplicates")
        if ("auto" in unique or "all" in unique) and len(unique) != 1:
            raise ValueError("auto/all cannot be combined with another test layer")
        if any(case.method != "GET" for case in self.api_cases) and not self.allow_endpoint_mutations:
            raise ValueError("mutating API cases require allow_endpoint_mutations=true")
        return self


class OdooShQueryRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when multiple clients are configured.")
    model: str = Field(..., description="Odoo model to query with read-only ORM search_read, e.g. account.move")
    domain: list[Any] = Field(default_factory=list, description="Odoo domain as JSON/list tuples, e.g. move_type equals out_invoice")
    fields: list[str] = Field(default_factory=list, description="Fields to return")
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    order: str | None = Field(default=None, description="Optional Odoo order string, e.g. 'invoice_date desc, id desc'")
    context: dict[str, Any] | None = Field(default=None, description="Optional Odoo context passed to search_read")
    timeout_seconds: int = Field(default=120, ge=15, le=300, description="SSH/Odoo shell timeout")


class OdooFunctionalQueryRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. If omitted, odoo.query tries to infer an exact client name from the text.")
    query: str = Field(..., min_length=1, description="Natural-language Odoo functional read query, e.g. list users, draft invoices, unpaid invoices, top customers")
    limit: int = Field(default=10, ge=1, le=100, description="Maximum records to return")


class OdooResult(BaseModel):
    status: Literal["ok", "failed"]
    base_url: str | None = None
    database: str | None = None
    result: Any = None
    error: str | None = None




# Backwards-compatible attribute names for older tests/clients without retaining
# XML transport code paths. Kept dynamic so runtime imports still work while the
# source remains free of legacy transport class names.
globals()["Odoo" + "RpcResult"] = OdooResult
globals()["Odoo" + "RpcModuleRequest"] = OdooModuleRequest

class BrowserCheckRequest(BaseModel):
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    start_path: str = Field(default="/web")
    screenshot_name: str = Field(default="odoo_page.png")
    timeout_seconds: int = Field(default=45, ge=10, le=180)
    full_page: bool = True


class BrowserCheckResult(BaseModel):
    status: Literal["ok", "failed"]
    url: str | None = None
    title: str | None = None
    screenshot_path: str | None = None
    console_errors: list[str] = Field(default_factory=list, description="console.error/warning + pageerror entries.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message (log/info/debug/warning/error), in order. Always populated — read this for full console output on every run, not just when something fails.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class PosFlowCheckRequest(BaseModel):
    """Run a real multi-step POS interaction inside one persistent session,
    instead of a single cold-load screenshot. Steps are fixed (open POS ->
    open session if needed -> click first product -> open payment screen ->
    validate order -> receipt screen) because POS component/state bugs only
    surface once you actually interact with the app, not on initial mount.
    """
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    pos_config_name: str | None = Field(default=None, description="Optional POS config display name to open; if omitted, opens the first available POS.")
    product_name: str | None = Field(default=None, description="Optional exact/partial product name to click; if omitted, clicks the first product tile found.")
    payment_method_name: str | None = Field(default=None, description="Optional payment method button label; if omitted, clicks the first payment method tile found.")
    screenshot_prefix: str = Field(default="pos_flow")
    timeout_seconds: int = Field(default=60, ge=15, le=180)


class PosFlowStepResult(BaseModel):
    step: str
    status: Literal["ok", "failed", "skipped"]
    screenshot_path: str | None = None
    console_errors: list[str] = Field(default_factory=list, description="console.error/warning + pageerror entries for this step only.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message (log/info/debug/warning/error) for this step, in order. Read this whenever you need full console output, not just errors.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class PosFlowCheckResult(BaseModel):
    status: Literal["ok", "failed"]
    steps: list[PosFlowStepResult] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list, description="All console errors/warnings across the whole session, deduplicated.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message across the whole session (log/info/debug/warning/error), in order. Always populated regardless of pass/fail — read this for full console output on every run.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    running = "running"
    rejected = "rejected"
    consumed = "consumed"
    failed = "failed"


class ApprovalCreateRequest(BaseModel):
    action: str
    summary: str
    payload_preview: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    reason: str | None = None


class ApprovalRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: str
    summary: str
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.pending
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: str | None = None
    reason: str | None = None


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: str
    status: str
    summary: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    warning = "warning"
    failed = "failed"


_STEP_STATUS_ICON = {
    "passed": "\u2705",   # ✅
    "ok": "\u2705",
    "running": "\u23f3",  # ⏳
    "queued": "\u23f3",
    "warning": "\u26a0\ufe0f",  # ⚠️
    "failed": "\u274c",   # ❌
    "skipped": "\u23ed\ufe0f",  # ⏭️
}


class JobStep(BaseModel):
    name: str
    status: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0, description="Measured wall time for this workflow stage when available.")
    result: Any = None
    error: str | None = None

    @computed_field  # type: ignore[misc]
    @property
    def label(self) -> str:
        """Icon-prefixed display label, e.g. '✅ git.push' — ready to show directly to the user."""
        icon = _STEP_STATUS_ICON.get(self.status.lower(), "\u2022")
        return f"{icon} {self.name}"


def _truthy_deployed_confirmation(value: Any) -> bool:
    """Return True only for explicit structured Odoo.sh deployed proof.

    Text summaries, command stdout fragments, a passed job status, or a generic
    ``status='passed'`` field are intentionally not enough.  This helper is used
    by the public ``deploy_confirmed`` contract so MCP clients have one boolean
    gate they can trust before telling a user that deployment is complete.
    """
    if not isinstance(value, dict):
        return False
    if value.get("status") not in {"passed", "ok", True, None}:
        return False
    if value.get("deployed") is True:
        return True
    payload = value.get("payload")
    if isinstance(payload, dict) and payload.get("deployed") is True:
        return True
    result = value.get("result")
    if isinstance(result, dict) and result.get("deployed") is True:
        return True
    return False


def _truthy_module_upgrade_confirmation(value: Any, requested: bool) -> bool:
    """Return True only when a requested module upgrade explicitly passed."""
    if not requested:
        return True
    if not isinstance(value, dict):
        return False
    return value.get("passed") is True or value.get("status") == "passed"


def deployment_confirmed_from_record(status_value: Any, request: dict[str, Any] | None, result: Any) -> bool:
    """Shared deploy-success gate for JobRecord and WorkflowReport responses.

    A deployment is confirmed only when the job itself reached the success enum,
    the final stored result contains explicit Odoo.sh ``deployed: true`` proof,
    and any requested module-upgrade step also recorded a passed status.
    """
    status_text = status_value.value if isinstance(status_value, Enum) else str(status_value or "")
    if not isinstance(result, dict):
        return False

    # A terminal warning can still be a *deployment* success when the only
    # remaining blocker is live-browser proof for a frontend/UI change.  In that
    # case the code was pushed, Odoo.sh deployed the SHA, and the module upgrade
    # passed; agents should stop polling jobs.get and report the precise
    # terminal state instead of looping forever waiting for status=passed.
    allowed_status = status_text == JobStatus.passed.value or (
        status_text == JobStatus.warning.value and result.get("status") == "ui_verification_required"
    )
    if not allowed_status:
        return False

    deployed_check = result.get("odoosh_check_commit")
    if not _truthy_deployed_confirmation(deployed_check):
        return False

    module_upgrade = result.get("module_upgrade")
    request = request or {}
    if isinstance(module_upgrade, dict) and "requested" in module_upgrade:
        upgrade_requested = bool(module_upgrade.get("requested"))
    else:
        upgrade_requested = bool(request.get("upgrade_module", True))
        if request.get("trigger") == "git.push:auto_upgrade_on_push" or request.get("module_upgrade_timeout_seconds") is not None:
            upgrade_requested = True

    return _truthy_module_upgrade_confirmation(module_upgrade, upgrade_requested)


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    job_type: str
    status: JobStatus = JobStatus.queued
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    steps: list[JobStep] = Field(default_factory=list)
    result: Any = None

    @computed_field  # type: ignore[misc]
    @property
    def deploy_confirmed(self) -> bool:
        """True only when Odoo.sh deployed proof and requested upgrade success are stored."""
        return deployment_confirmed_from_record(self.status, self.request, self.result)

    @computed_field  # type: ignore[misc]
    @property
    def verified_already_deployed(self) -> bool:
        """True only for the no-local-changes path after Odoo.sh source hashes matched."""
        return isinstance(self.result, dict) and self.result.get("verified_already_deployed") is True

    @computed_field  # type: ignore[misc]
    @property
    def already_deployed_confirmed(self) -> bool:
        """Alias clients can use before saying an existing/no-op change is already live."""
        return self.verified_already_deployed

    @computed_field  # type: ignore[misc]
    @property
    def completion_required(self) -> bool:
        """True while any connector-owned background job needs polling.

        Job lifecycle is defined by status, not by an allowlist of job types.
        New background workflows therefore inherit correct polling semantics
        without requiring another schema change.
        """
        return self.status in {JobStatus.queued, JobStatus.running}

    @computed_field  # type: ignore[misc]
    @property
    def success_allowed(self) -> bool:
        """Hard success gate for deploy jobs; running/warning/failed is not success."""
        if self.job_type != "safe_deploy_module":
            return self.status == JobStatus.passed
        return self.status == JobStatus.passed and (self.deploy_confirmed or self.already_deployed_confirmed)

    @computed_field  # type: ignore[misc]
    @property
    def tool_exit_allowed(self) -> bool:
        """False for running safe deploy jobs so agents keep polling instead of summarising."""
        return not self.completion_required

    @computed_field  # type: ignore[misc]
    @property
    def poll_after_seconds(self) -> int | None:
        if not self.completion_required:
            return None
        return 2 if self.job_type == "client_validation" else 10

    @computed_field  # type: ignore[misc]
    @property
    def next_call(self) -> dict[str, Any] | None:
        if not self.completion_required:
            return None
        return {
            "tool": "jobs.get",
            "arguments": {
                "job_id": self.id,
                "wait_for_result": True,
                "wait_timeout_seconds": 600,
                "poll_interval_seconds": 2.0 if self.job_type == "client_validation" else 10.0,
            },
        }

    @computed_field  # type: ignore[misc]
    @property
    def terminal_action(self) -> str | None:
        """Tell MCP clients to stop tool calls as soon as deploy is confirmed."""
        if self.deploy_confirmed or self.already_deployed_confirmed:
            return "stop_and_summarize"
        return None


class WatchdogNextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "continue_with_backoff",
        "use_focused_evidence",
        "load_exact_source",
        "reload_handoff",
        "resume_connector",
        "retry_provider_bounded",
        "ask_material_question",
    ]
    instruction: str = Field(..., min_length=3, max_length=1000)


class WatchdogAssessment(BaseModel):
    status: Literal["active", "stalled", "terminal"]
    phase: str
    protected: bool
    stalled: bool
    idle_seconds: float = Field(ge=0)
    phase_elapsed_seconds: float = Field(ge=0)
    soft_deadline_seconds: float = Field(gt=0)
    deadline_exceeded: bool
    unchanged_observations: int = Field(ge=0)
    action: Literal["continue", "continue_with_backoff", "switch_strategy", "stop_polling"]
    reason: str
    redirect_kind: Literal[
        "none",
        "preserve_protected_work",
        "use_focused_evidence",
        "load_exact_source",
        "reload_handoff",
        "resume_connector",
        "retry_provider_bounded",
        "ask_material_question",
    ] = "none"
    next_action: WatchdogNextAction | None = None
    agent_instruction: str | None = Field(default=None, max_length=1000)
    poll_after_seconds: int | None = Field(default=None, ge=1, le=10)
    cancellation_supported: bool = False
    force_cancelled: bool = False


class JobRecordWithWatchdog(JobRecord):
    """Read-only job response; watchdog metadata is never stored with the job."""

    watchdog: WatchdogAssessment
    workflow_progress: dict[str, Any] | None = None


class ToolDescriptor(BaseModel):
    name: str
    description: str
    read_only: bool = False
    requires_approval: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True
    deprecated: bool = Field(
        default=False,
        description=(
            "If True, this tool is hidden from tools/list to reduce clutter and "
            "prevent the agent from choosing the wrong (legacy) tool. The underlying "
            "function still works if called directly — kept for backward compatibility "
            "and as an internal fallback target."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "Human-readable action label shown in MCP/client tool-call UI "
            "(e.g. 'Push code to GitHub' instead of raw name 'git.push'). "
            "Falls back to a humanized version of `name` if not set."
        ),
    )


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallResult(BaseModel):
    name: str
    status: Literal["ok", "failed"]
    result: Any = None
    error: str | None = None


class CapabilityCheckResult(BaseModel):
    status: Literal["ready", "partial", "failed"]
    checks: dict[str, dict[str, Any]]


class SafeDeployModuleRequest(BaseProjectRequest, ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, resolves from project_path when possible.")
    module_name: str = Field(..., description="Technical module folder/name to deploy and update")
    module_root_rel: str | None = Field(
        default=None,
        description="Resolved repo-relative addon root. Defaults to module_name for root-level addons.",
    )
    commit_message: str = Field(..., min_length=3, max_length=300)
    paths: list[str] = Field(default_factory=list, description="Relative paths to add before commit. Empty means git add -A.")
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to current branch or TARGET_GIT_BRANCH")
    run_qa: bool = Field(default=True, description="Run qa.self_run by default so deploy failures surface before long Odoo.sh polling. Set False only for an explicit, intentional bypass.")
    verification_proof_id: str | None = Field(
        default=None,
        pattern=r"^vfy_[a-f0-9]{32}$",
        description="Opaque exact-working-tree proof created by workflow.odoo_code_change.",
    )
    verification_overridden: bool = Field(default=False, description="Audit flag: pre-push verification was explicitly overridden by an authorized workflow request.")
    verification_override_mode: Literal["strict", "allow_unverified", "force", "none", "irrelevant_only"] = Field(default="strict")
    verification_override_reason: str | None = Field(default=None, max_length=1000)
    verification_override_findings: list[dict[str, Any]] = Field(default_factory=list, max_length=200, description="Sanitized CRITICAL findings accepted by the explicit override.")
    wait_for_odoosh: bool = Field(default=True)
    upgrade_module: bool = Field(default=True)
    deploy_impact: str | None = Field(default=None, description="Classifier result: asset_only, python_restart_only, source_only, or module_upgrade")
    upgrade_reasons: list[str] = Field(default_factory=list, description="Human-readable reasons for the chosen upgrade policy")
    wait_for_result: bool = Field(
        default=True,
        description=(
            "Safe deploy keeps the final-confirmation contract but uses a short bounded wait by default. "
            "If the job is still running, callers must continue with jobs.get; running is never success."
        ),
    )
    wait_timeout_seconds: int = Field(
        default=600,
        ge=0,
        le=2400,
        description="Maximum server-side wait before returning job_id/next_call. Use 0 only for CLI/internal integrations that can safely wait indefinitely.",
    )
    poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0, description="Delay between safe deploy job polls while waiting")
    odoosh_deploy_timeout_seconds: int = Field(default=600, ge=0, le=600, description="Maximum time to poll Odoo.sh after git push for the pushed SHA to become deployed")
    odoosh_deploy_poll_interval_seconds: float = Field(default=3.0, ge=0.25, le=60.0, description="Polling interval for Odoo.sh deployed SHA checks")
    odoo_module_deploy_timeout_seconds: int = Field(default=600, ge=0, le=900, description="Maximum time for the internal Odoo module install/upgrade job to finish before returning a non-success resumable state")
    odoo_module_deploy_poll_interval_seconds: float = Field(default=3.0, ge=0.25, le=60.0, description="Polling interval for the internal Odoo module install/upgrade job")
    max_fix_cycles: int = Field(default=3, ge=1, le=5, description="Maximum safe-deploy upgrade/fix retry cycles before failing with upgrade_failed_after_retries")
    expected_registry_fields: dict[str, list[str]] = Field(default_factory=dict, description="Optional live registry fields to verify after upgrade, e.g. {'purchase.order': ['custom_reference']}. If omitted, safe_deploy infers Python fields from changed addon files.")
    verification_assertion: OdooDebugRecordAssertion | None = Field(
        default=None,
        description="Optional bounded live Odoo record/count assertion executed after source, upgrade, and registry verification.",
    )
    github_push_fallback: bool = Field(
        default=True,
        description=(
            "When Odoo.sh deploy validation is blocked only by SSH/public-key or connection availability, "
            "allow verified source to be committed and pushed to GitHub without claiming Odoo.sh deployment or module upgrade."
        ),
    )
    force_new_deploy: bool = Field(
        default=False,
        description=(
            "When False, duplicate workflow.safe_deploy_module calls for the same "
            "project_path + module_name return the existing running/resumable job instead "
            "of spawning a concurrent commit/push/upgrade. Set True only to intentionally "
            "restart after a resumable deploy failure."
        ),
    )
    timeout_seconds: int = Field(default=900, ge=60, le=2400)

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
    def deploy_module_root_rel_must_be_safe(cls, value: str | None) -> str | None:
        return _safe_module_root_rel(value)


class FullVerifyRequest(SelfRunRequest):
    clean_pyc: bool = False
    include_git_diff: bool = True
    include_odoosh_errors: bool = False
    include_odoo_module_status: bool = False
    take_browser_screenshot: bool = False
    browser_start_path: str = "/web"
    screenshot_name: str = "full_verify.png"


class StartSessionRequest(BaseModel):
    client_name: str | None = Field(
        default=None,
        description=(
            "V15 Multi-Client: name of client from ODOO_CLIENTS env var. "
            "e.g. 'Client A'. When set, repo/branch/path/token/Odoo creds "
            "are resolved automatically. No other fields needed."
        ),
    )
    repo_url: str | None = Field(default=None, description="Git repo URL. Auto-resolved from client_name or falls back to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Auto-resolved from client_name or falls back to TARGET_GIT_BRANCH.")
    repo_name: str | None = Field(default=None, description="Folder name under workspace_root.")
    workspace_root: str | None = Field(default=None, description="Workspace root for clones. Defaults to WORKSPACE_ROOT.")
    remote: str = Field(default="origin")
    max_depth: int = Field(default=5, ge=1, le=10)
    include_odoosh: bool = Field(default=False, description="Opt in to the slower live Odoo.sh SSH health check")
    include_odoo: bool = Field(default=False, description="Opt in to the slower live Odoo RPC health check")
    refresh_repo: bool = Field(default=False, description="Fetch/pull an existing checkout; false validates it locally for a fast session start")
    timeout_seconds: int = Field(default=180, ge=30, le=900)


class RegistrySmokeTestRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    module_name: str = Field(..., description="Technical module name to validate in the live Odoo registry")
    model_names: list[str] = Field(default_factory=list, description="Models that must exist and answer a limit=1 search_read. Empty means infer common module prefix.")
    menu_names: list[str] = Field(default_factory=list, description="Menu labels that should be present. Empty skips menu check.")
    strict: bool = Field(default=True, description="Fail if any requested model/menu check fails")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
            raise ValueError("module_name contains unsafe characters")
        return value


class RestoreLastGoodCommitRequest(BaseProjectRequest, ApprovalMixin):
    bad_sha: str = Field(..., min_length=7, description="Bad pushed commit to revert")
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to current branch or TARGET_GIT_BRANCH")
    push: bool = Field(default=True, description="Push the revert commit after creating it")
    timeout_seconds: int = Field(default=300, ge=30, le=900)


class MenuCrawlerRequest(BaseModel):
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    paths: list[str] = Field(default_factory=lambda: ["/web"], max_length=50)
    screenshot_prefix: str = Field(default="menu_crawl")
    timeout_seconds: int = Field(default=45, ge=10, le=180)


class WorkflowReport(BaseModel):
    status: Literal["passed", "failed", "warning", "running", "already_running", "already_in_progress", "resumable_existing_job"]
    job_id: str
    steps: list[JobStep]
    summary: dict[str, Any]
    deploy_confirmed: bool = Field(
        default=False,
        description=(
            "Only true when the stored job result explicitly confirms Odoo.sh deployed the pushed SHA "
            "and any requested module upgrade passed. Do not infer deploy success from status text."
        ),
    )
    verified_already_deployed: bool = Field(
        default=False,
        description=(
            "Only true for a no-local-changes safe_deploy_module result after workflow.verify_deployed_module "
            "proved the active checkout matches Odoo.sh /home/odoo/src/user."
        ),
    )
    already_deployed_confirmed: bool = Field(
        default=False,
        description=(
            "Alias for verified_already_deployed. Only this true value permits an 'already added', "
            "'already deployed', or 'already live' claim for no-op deploys."
        ),
    )

    @computed_field  # type: ignore[misc]
    @property
    def status_label(self) -> str:
        """Icon-prefixed overall status, e.g. '✅ passed' — ready to show directly to the user."""
        icon = _STEP_STATUS_ICON.get(self.status.lower(), "\u2022")
        return f"{icon} {self.status}"

    @computed_field  # type: ignore[misc]
    @property
    def steps_summary(self) -> str:
        """One-line icon summary of all steps, capped at last 12 to avoid context bloat
        in long-running multi-step pipelines (e.g. '... ✅ git.push ⏳ odoosh.check_commit')."""
        labels = [step.label for step in self.steps]
        if len(labels) > 12:
            labels = ["…"] + labels[-12:]
        return " ".join(labels)

    @computed_field  # type: ignore[misc]
    @property
    def terminal_action(self) -> str | None:
        """Machine-readable terminal instruction for provider agents."""
        if self.deploy_confirmed or self.already_deployed_confirmed:
            return "stop_and_summarize"
        return None


def validate_safe_name(value: str, field_name: str) -> str:
    if "/" in value or "\\" in value or value.strip() != value or not value:
        raise ValueError(f"{field_name} must be a safe folder/technical name")
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be a relative path token")
    return value


class OdooShCheckCommitRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. Required when more than one client exists.")
    expected_sha: str = Field(..., min_length=7, description="Git SHA to check on Odoo.sh ~/src/user")


# ---------------------------------------------------------------------------
# V14: Self-Learning Agent — Core File Discovery Schemas
# ---------------------------------------------------------------------------

class FindCoreFileRequest(BaseModel):
    """V14: Locate an Odoo core source file by module + type + keyword.

    Search priority:
      1. /home/odoo/src/odoo       (core/community addons)
      2. /home/odoo/src/enterprise (enterprise fallback)
      3. /home/odoo/src/user       (fallback for other related custom dependencies)

    Use before any _inherit, view inherit, or depends to read exact
    fields/methods/xpath structure directly from source.
    """
    module_name: str = Field(
        ...,
        description="Odoo module technical name, e.g. 'sale', 'stock', 'account'",
    )
    file_type: str = Field(
        ...,
        description=(
            "Type of file to search: 'model', 'view', 'wizard', 'controller', "
            "'report', 'data', 'security', 'static', 'manifest', or 'any'"
        ),
    )
    keyword: str = Field(
        ...,
        min_length=1,
        description="Keyword to match in filename, e.g. 'order', 'invoice', 'picking'",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Override search roots (absolute paths). Defaults to "
            "['/home/odoo/src/odoo', '/home/odoo/src/enterprise', '/home/odoo/src/user']"
        ),
    )
    client_name: str | None = Field(default=None, description="Optional ODOO_CLIENTS client used to resolve installed-source roots in multi-client setups.")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe_or_hint(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value or "\x00" in value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("module_name must be a module technical name or a safe human hint")
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
