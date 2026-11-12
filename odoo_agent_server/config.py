from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import List, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Odoo MCP agent connector."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_token: str = Field(default="change-me", alias="ODOO_AGENT_API_TOKEN")
    allowed_roots_raw: str = Field(default="/workspace/repos,/tmp,/home/odoo/src", alias="ODOO_AGENT_ALLOWED_ROOTS")
    state_dir: str = Field(default="/tmp/odoo_agent_state", alias="ODOO_AGENT_STATE_DIR")
    screenshot_dir: str = Field(default="/tmp/odoo_agent_screenshots", alias="ODOO_AGENT_SCREENSHOT_DIR")
    cdp_url: str | None = Field(default=None, alias="ODOO_AGENT_CDP_URL")

    # Approval defaults protect high-impact writes; read-only operations remain available.
    require_approval: bool = Field(default=True, alias="ODOO_AGENT_REQUIRE_APPROVAL")
    approval_required_actions_raw: str = Field(default="repo.prepare,repo.write,repo.delete,repo.apply_patch,repo.apply_patch_atomic,repo.clean_pyc,git.commit,git.push,odoo.module_install,odoo.module_update,odoosh.module_install_ssh,odoosh.module_deploy_ssh,workflow.safe_deploy_module,odoo.record_action.*", alias="ODOO_AGENT_APPROVAL_REQUIRED_ACTIONS")
    protected_branches_raw: str = Field(default="", alias="ODOO_AGENT_PROTECTED_BRANCHES")
    # ws_* workspace tools (GitHub/GitLab REST API; no clone, no local state)
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    ws_protected_branches_raw: str = Field(default="main,master,production,prod,live", alias="ODOO_AGENT_WS_PROTECTED_BRANCHES")
    ws_autocheckpoint: bool = Field(default=True, alias="ODOO_AGENT_WS_AUTOCHECKPOINT")
    ws_wip_files: bool = Field(default=True, alias="ODOO_AGENT_WS_WIP_FILES")  # show changed files on the WIP branch
    ws_checkpoint_mode: str = Field(default="async", alias="ODOO_AGENT_WS_CHECKPOINT_MODE")  # async | sync
    # TOON text rendering for tool results: auto (tabular-heavy tools), all, off
    toon_output: str = Field(default="auto", alias="ODOO_AGENT_TOON_OUTPUT")

    # Git deployment guardrails. These are optional but strongly recommended in production.
    target_git_repo: str | None = Field(default=None, alias="TARGET_GIT_REPO")
    target_git_branch: str | None = Field(default=None, alias="TARGET_GIT_BRANCH")
    target_project_path: str | None = Field(default=None, alias="TARGET_PROJECT_PATH")
    workspace_root: str = Field(default="/workspace/repos", alias="WORKSPACE_ROOT")
    git_ssh_key_path: str | None = Field(default=None, alias="GIT_SSH_KEY_PATH")
    git_author_name: str = Field(default="Odoo MCP Agent", alias="GIT_AUTHOR_NAME")
    git_author_email: str = Field(default="odoo-mcp@example.local", alias="GIT_AUTHOR_EMAIL")

    # OAuth 2.1 / MCP App authentication for generic and compatibility MCP clients.
    public_base_url: str | None = Field(default=None, alias="ODOO_AGENT_PUBLIC_BASE_URL")
    oauth_issuer: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_ISSUER")
    oauth_default_scope: str = Field(default="odoo.agent", alias="ODOO_AGENT_OAUTH_DEFAULT_SCOPE")
    oauth_auto_approve: bool = Field(default=False, alias="ODOO_AGENT_OAUTH_AUTO_APPROVE")
    oauth_static_client_id: str = Field(default="odoo-agent-mcp", alias="ODOO_AGENT_OAUTH_STATIC_CLIENT_ID")
    oauth_static_client_secret: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET")
    oauth_token_secret: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_TOKEN_SECRET")
    oauth_allowed_redirect_uris_raw: str = Field(default="", alias="ODOO_AGENT_OAUTH_ALLOWED_REDIRECT_URIS")
    # OAuth 2.1 for native/CLI clients (for example Claude Code) uses a
    # loopback HTTP redirect. Allow http://localhost and http://127.0.0.1
    # automatically unless an explicit allowlist is configured.
    oauth_allow_loopback_redirects: bool = Field(default=True, alias="ODOO_AGENT_OAUTH_ALLOW_LOOPBACK_REDIRECTS")
    # Headless/dev convenience: auto-approve OAuth authorization requests whose
    # redirect target is loopback, so native clients can complete the flow
    # without a browser consent form. Disable for shared/public deployments.
    oauth_auto_approve_loopback: bool = Field(default=True, alias="ODOO_AGENT_OAUTH_AUTO_APPROVE_LOOPBACK")
    oauth_authorization_code_ttl_seconds: int = Field(default=600, alias="ODOO_AGENT_OAUTH_AUTH_CODE_TTL_SECONDS")
    oauth_access_token_ttl_seconds: int = Field(default=3600, alias="ODOO_AGENT_OAUTH_ACCESS_TOKEN_TTL_SECONDS")
    oauth_refresh_token_ttl_seconds: int = Field(default=2592000, alias="ODOO_AGENT_OAUTH_REFRESH_TOKEN_TTL_SECONDS")
    continuation_token_secret: str | None = Field(default=None, alias="ODOO_AGENT_CONTINUATION_TOKEN_SECRET")
    continuation_token_ttl_seconds: int = Field(
        default=2592000,
        ge=3600,
        le=7776000,
        alias="ODOO_AGENT_CONTINUATION_TOKEN_TTL_SECONDS",
    )
    # Recover dynamic OAuth clients after ephemeral-state redeploys.
    # When enabled, unknown dcr_* client IDs are accepted only with normal
    # redirect validation. Compatibility redirect hosts remain allowed for
    # static/recovered clients when no explicit allowlist is configured.
    oauth_accept_unknown_dcr_clients: bool = Field(default=False, alias="ODOO_AGENT_OAUTH_ACCEPT_UNKNOWN_DCR_CLIENTS")

    # Optional compatibility for MCP clients that cannot send Authorization
    # headers from their app UI. Prefer Authorization: Bearer <token>. Enable
    # query-token auth only for private HTTPS deployments.
    query_token_auth_enabled: bool = Field(default=False, alias="ODOO_AGENT_QUERY_TOKEN_AUTH_ENABLED")

    # Fast-safe write tuning.  The smart writer uses this path for normal small
    # multi-file edits and keeps the slower git-worktree transaction for larger
    # or riskier changes.  Validation is still handled by the normal verifier
    # on the exact files changed by the task.
    fast_write_max_files: int = Field(default=50, alias="ODOO_AGENT_FAST_WRITE_MAX_FILES")
    fast_write_max_bytes_each: int = Field(default=500_000, alias="ODOO_AGENT_FAST_WRITE_MAX_BYTES_EACH")
    fast_write_max_total_bytes: int = Field(default=5_000_000, alias="ODOO_AGENT_FAST_WRITE_MAX_TOTAL_BYTES")

    # SHA-bound coding sessions keep one module snapshot warm across the
    # inspect -> writes -> repair continuation without weakening final QA.
    coding_session_ttl_seconds: int = Field(default=3600, ge=300, le=86400, alias="ODOO_AGENT_CODING_SESSION_TTL_SECONDS")
    coding_session_max_count: int = Field(default=20, ge=2, le=200, alias="ODOO_AGENT_CODING_SESSION_MAX_COUNT")
    coding_session_bundle_max_files: int = Field(default=2000, ge=50, le=20000, alias="ODOO_AGENT_CODING_SESSION_BUNDLE_MAX_FILES")
    coding_session_bundle_max_bytes: int = Field(default=50_000_000, ge=1_000_000, le=500_000_000, alias="ODOO_AGENT_CODING_SESSION_BUNDLE_MAX_BYTES")
    coding_session_max_total_bytes: int = Field(default=250_000_000, ge=10_000_000, le=5_000_000_000, alias="ODOO_AGENT_CODING_SESSION_MAX_TOTAL_BYTES")
    coding_workspace_max_modules: int = Field(default=5, ge=1, le=10, alias="ODOO_AGENT_CODING_WORKSPACE_MAX_MODULES")
    coding_workspace_ttl_seconds: int = Field(default=14400, ge=300, le=86400, alias="ODOO_AGENT_CODING_WORKSPACE_TTL_SECONDS")
    coding_workspace_max_count: int = Field(default=20, ge=2, le=200, alias="ODOO_AGENT_CODING_WORKSPACE_MAX_COUNT")
    coding_workspace_max_total_bytes: int = Field(
        default=500_000_000,
        ge=10_000_000,
        le=10_000_000_000,
        alias="ODOO_AGENT_CODING_WORKSPACE_MAX_TOTAL_BYTES",
    )
    job_max_count: int = Field(default=500, ge=20, le=5000, alias="ODOO_AGENT_JOB_MAX_COUNT")
    artifact_ttl_seconds: int = Field(default=604800, ge=3600, le=7776000, alias="ODOO_AGENT_ARTIFACT_TTL_SECONDS")
    artifact_max_count: int = Field(default=500, ge=20, le=5000, alias="ODOO_AGENT_ARTIFACT_MAX_COUNT")
    artifact_max_total_bytes: int = Field(default=250_000_000, ge=1_000_000, le=5_000_000_000, alias="ODOO_AGENT_ARTIFACT_MAX_TOTAL_BYTES")

    # Fast/correct agent optimisations. These keep strict validation while avoiding
    # repeated full-source scans on every OWL/POS/website task.
    pattern_cache_enabled: bool = Field(default=True, alias="ODOO_AGENT_PATTERN_CACHE_ENABLED")
    pattern_cache_seconds: int = Field(default=3600, alias="ODOO_AGENT_PATTERN_CACHE_SECONDS")
    pattern_cache_dir: str | None = Field(default=None, alias="ODOO_AGENT_PATTERN_CACHE_DIR")
    repo_index_enabled: bool = Field(default=True, alias="ODOO_AGENT_REPO_INDEX_ENABLED")
    repo_index_cache_seconds: int = Field(default=3600, alias="ODOO_AGENT_REPO_INDEX_CACHE_SECONDS")
    verify_impact_aware_default: bool = Field(default=True, alias="ODOO_AGENT_VERIFY_IMPACT_AWARE_DEFAULT")

    # Best-effort deploy follow-through for raw git.push fallback paths.
    auto_upgrade_on_push: bool = Field(default=True, alias="ODOO_AGENT_AUTO_UPGRADE_ON_PUSH")
    github_push_fallback_enabled: bool = Field(default=True, alias="ODOO_AGENT_GITHUB_PUSH_FALLBACK_ENABLED")
    safe_deploy_stale_running_seconds: int = Field(default=7200, alias="ODOO_AGENT_SAFE_DEPLOY_STALE_RUNNING_SECONDS")
    # Two workers are the safe minimum: safe-deploy can wait for a nested
    # module-deploy job submitted to the same bounded executor.
    background_max_workers: int = Field(default=2, ge=1, le=4, alias="ODOO_AGENT_BACKGROUND_MAX_WORKERS")
    coding_workflow_max_workers: int = Field(default=2, ge=1, le=8, alias="ODOO_AGENT_CODING_WORKFLOW_MAX_WORKERS")
    coding_workflow_max_queue: int = Field(default=2, ge=0, le=16, alias="ODOO_AGENT_CODING_WORKFLOW_MAX_QUEUE")
    mcp_operation_guard_enabled: bool = Field(default=False, alias="ODOO_AGENT_OPERATION_GUARD_ENABLED")
    # Phase-aware watchdog. It never force-cancels write/deploy/database work.
    # In safe_redirect mode it can time-bound an allowlisted read-only MCP call
    # and return a concrete next action; shadow remains available for diagnostics.
    workflow_watchdog_enabled: bool = Field(default=True, alias="ODOO_AGENT_WATCHDOG_ENABLED")
    workflow_watchdog_mode: Literal["shadow", "safe_redirect"] = Field(
        default="safe_redirect",
        alias="ODOO_AGENT_WATCHDOG_MODE",
    )
    workflow_watchdog_observation_budget_seconds: int = Field(
        default=120,
        ge=15,
        le=3600,
        alias="ODOO_AGENT_WATCHDOG_OBSERVATION_BUDGET_SECONDS",
    )
    workflow_watchdog_stall_confirmations: int = Field(
        default=2,
        ge=2,
        le=10,
        alias="ODOO_AGENT_WATCHDOG_STALL_CONFIRMATIONS",
    )
    workflow_watchdog_wait_cap_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        alias="ODOO_AGENT_WATCHDOG_WAIT_CAP_SECONDS",
    )

    # Qwen URL-token fallback hardening. Qwen app may not pass headers, so the
    # server can mint a short-lived URL token that maps to the real API/OAuth token.
    qwen_short_token_ttl_seconds: int = Field(default=3600, alias="ODOO_AGENT_QWEN_SHORT_TOKEN_TTL_SECONDS")

    # Odoo shell defaults.
    odoo_base_url: str | None = Field(default=None, alias="ODOO_BASE_URL")
    odoo_db_name: str | None = Field(default=None, alias="ODOO_DB_NAME")
    odoo_login: str | None = Field(default=None, alias="ODOO_LOGIN")
    odoo_password: str | None = Field(default=None, alias="ODOO_PASSWORD")
    odoo_api_key: str | None = Field(default=None, alias="ODOO_API_KEY")
    odoo_https_verify_ssl: bool = Field(default=True, alias="ODOO_HTTPS_VERIFY_SSL")
    functional_operations_enabled: bool = Field(
        default=False,
        alias="ODOO_AGENT_FUNCTIONAL_OPERATIONS_ENABLED",
    )
    functional_max_batch_records: int = Field(
        default=20,
        ge=1,
        le=100,
        alias="ODOO_AGENT_FUNCTIONAL_MAX_BATCH_RECORDS",
    )


    # Odoo.sh SSH connector settings. Use a development/staging build host.
    odoosh_ssh_host: str | None = Field(default=None, alias="ODOOSH_SSH_HOST")
    odoosh_ssh_user: str = Field(default="odoo", alias="ODOOSH_SSH_USER")
    odoosh_ssh_port: int = Field(default=22, alias="ODOOSH_SSH_PORT")
    odoosh_ssh_key_path: str | None = Field(default=None, alias="ODOOSH_SSH_KEY_PATH")
    odoosh_ssh_private_key_b64: str | None = Field(default=None, alias="ODOOSH_SSH_PRIVATE_KEY_B64")
    odoosh_db_name: str | None = Field(default=None, alias="ODOOSH_DB_NAME")
    odoosh_odoo_path: str = Field(default="/home/odoo/src/odoo", alias="ODOOSH_ODOO_PATH")
    odoosh_user_addons_path: str = Field(default="/home/odoo/src/user", alias="ODOOSH_USER_ADDONS_PATH")
    odoosh_wait_timeout_seconds: int = Field(default=600, alias="ODOOSH_WAIT_TIMEOUT_SECONDS")
    odoosh_wait_interval_seconds: int = Field(default=3, alias="ODOOSH_WAIT_INTERVAL_SECONDS")

    # Phase 1 — lazy on-demand core source sync (find_core_file / verify_against_core).
    core_sync_enabled: bool = Field(default=True, alias="CORE_SYNC_ENABLED")
    core_sync_max_age_seconds: int = Field(default=3600, alias="CORE_SYNC_MAX_AGE_SECONDS")

    @property
    def allowed_roots(self) -> List[Path]:
        roots: list[Path] = []
        for raw in self.allowed_roots_raw.split(","):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw).expanduser().resolve())
        return roots

    @property
    def approval_required_actions(self) -> set[str]:
        return {item.strip() for item in self.approval_required_actions_raw.split(",") if item.strip()}

    @property
    def protected_branches(self) -> set[str]:
        return {item.strip() for item in self.protected_branches_raw.split(",") if item.strip()}

    @property
    def workspace_path(self) -> Path:
        path = Path(self.workspace_root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


    @property
    def oauth_allowed_redirect_uris(self) -> list[str]:
        return [item.strip() for item in self.oauth_allowed_redirect_uris_raw.split(",") if item.strip()]

    @property
    def state_path(self) -> Path:
        path = Path(self.state_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_path(self) -> Path:
        raw = self.pattern_cache_dir or str(self.state_path / "cache")
        path = Path(raw).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def effective_odoosh_ssh_key_path(self) -> Path | None:
        if self.odoosh_ssh_key_path:
            return Path(self.odoosh_ssh_key_path).expanduser().resolve()
        if not self.odoosh_ssh_private_key_b64:
            return None
        key_dir = self.state_path / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.odoosh_ssh_private_key_b64.encode("ascii")).hexdigest()[:16]
        key_path = key_dir / f"odoosh_key_{digest}"
        if not key_path.exists():
            raw = base64.b64decode(self.odoosh_ssh_private_key_b64.encode("ascii"))
            key_path.write_bytes(raw)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        return key_path


settings = Settings()


# ===========================================================================
# V15 — Multi-Client Support
# ===========================================================================

import json as _json
import os as _os
import re as _re
import codecs as _codecs


def get_odoo_clients() -> list[dict]:
    """Parse ODOO_CLIENTS JSON env variable and return list of client configs.

    Each client dict should have:
      name, repo, branch, path, token,
      environment (optional: test, development, staging, production),
      odoo_url, odoo_db, odoo_login, odoo_password, odoo_verify_ssl,
      optional per-client Odoo.sh keys:
      odoosh_db_name, odoosh_ssh_host, odoosh_ssh_port,
      odoosh_ssh_user, odoosh_ssh_key_path, odoosh_ssh_private_key_b64,
      odoosh_odoo_path, odoosh_user_addons_path
    """
    raw = _os.getenv("ODOO_CLIENTS", "[]")
    for candidate in _odoo_client_json_candidates(raw):
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, str):
                parsed = _json.loads(parsed)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pass
    return []


def _odoo_client_json_candidates(raw: str) -> list[str]:
    value = raw.strip()
    candidates = [value]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        candidates.append(value[1:-1])
    for item in list(candidates):
        if "\\\"" in item or "\\'" in item:
            candidates.append(item.replace('\\"', '"').replace("\\'", "'"))
        try:
            decoded = _codecs.decode(item, "unicode_escape")
            if decoded != item:
                candidates.append(decoded)
        except Exception:
            pass
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def normalise_client(client: dict) -> dict:
    """Return a string-normalized client dict without changing secret values."""
    normalized: dict[str, str] = {}
    for key in (
        "name",
        "repo",
        "branch",
        "path",
        "token",
        "environment",
        "odoo_url",
        "odoo_db",
        "odoo_login",
        "odoo_password",
        "odoo_api_key",
        "odoo_verify_ssl",
        "odoosh_db_name",
        "odoosh_ssh_host",
        "odoosh_ssh_port",
        "odoosh_ssh_user",
        "odoosh_ssh_key_path",
        "odoosh_ssh_private_key_b64",
        "odoosh_odoo_path",
        "odoosh_user_addons_path",
    ):
        value = client.get(key)
        if value is not None:
            normalized[key] = str(value).strip()
    return normalized


def safe_client_public(client: dict) -> dict:
    """Expose non-secret client metadata for errors, jobs, and session state."""
    normalized = normalise_client(client)
    return {
        "name": normalized.get("name"),
        "repo": mask_repo_url(normalized.get("repo", "")) if normalized.get("repo") else None,
        "branch": normalized.get("branch"),
        "path": normalized.get("path"),
        "environment": normalized.get("environment"),
        "odoo_url": normalized.get("odoo_url"),
        "odoo_db": normalized.get("odoo_db"),
        "odoo_verify_ssl": normalized.get("odoo_verify_ssl"),
        "odoosh_db_name": normalized.get("odoosh_db_name"),
        "odoosh_ssh_host": normalized.get("odoosh_ssh_host"),
        "odoosh_ssh_port": normalized.get("odoosh_ssh_port"),
        "odoosh_ssh_user": normalized.get("odoosh_ssh_user"),
        "odoosh_odoo_path": normalized.get("odoosh_odoo_path"),
        "odoosh_user_addons_path": normalized.get("odoosh_user_addons_path"),
    }


def get_client_names() -> list[str]:
    """Return just the client display names — safe to expose to agent."""
    return [normalise_client(c).get("name", "") for c in get_odoo_clients() if normalise_client(c).get("name")]


def get_client(name: str) -> dict:
    """Lookup a client by name (case-insensitive). Raises ValueError if not found."""
    for c in get_odoo_clients():
        normalized = normalise_client(c)
        if normalized.get("name", "").lower() == name.lower():
            return normalized
    available = get_client_names()
    raise ValueError(
        f"Client '{name}' not found in ODOO_CLIENTS. "
        f"Available: {available}"
    )


def make_authenticated_repo_url(repo: str, token: str | None) -> str:
    """Inject GitHub token into HTTPS repo URL for authenticated git operations."""
    if not token:
        return repo
    if "oauth2:" in repo or f"{token}@" in repo:
        return repo  # already injected
    return repo.replace("https://", f"https://oauth2:{token}@")


def mask_repo_url(repo: str) -> str:
    """Mask token in repo URL for safe logging — never log raw tokens."""
    return _re.sub(r"oauth2:[^@]+@", "oauth2:***@", repo)
