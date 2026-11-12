"""Glue between the framework-free workspace engine and the connector's client config."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from fastapi import HTTPException

from ..config import settings
from .client_resolver_service import ClientResolverService
from .github_commit_service import GitHubError, GitHubRepo, parse_repo
from .gitlab_commit_service import GitLabRepo, parse_gitlab_repo
from .workspace_service import ClientTarget, WorkspaceError, WorkspaceService


def _resolve(name: str | None) -> ClientTarget:
    client = ClientResolverService().resolve_client(name or None, require_complete=True)
    if client is None:
        raise WorkspaceError("client_missing", "no client configured in ODOO_CLIENTS")
    token = client.token or settings.github_token
    if not token:
        raise WorkspaceError("git_token_missing", f"client '{client.name}' has no repository access token (set 'token' in ODOO_CLIENTS or GITHUB_TOKEN)")
    repo_url = client.repo or ""
    if "gitlab." in repo_url.lower() or "gitlab.com/" in repo_url.lower():
        try:
            base_url, project_path = parse_gitlab_repo(repo_url)
        except ValueError as exc:
            raise WorkspaceError("repo_url_invalid", str(exc), repo=client.public.get("repo")) from exc
        repository = _gitlab_repo(base_url, project_path, token)
    else:
        try:
            owner, repo_name = parse_repo(repo_url)
        except ValueError as exc:
            raise WorkspaceError("repo_provider_unsupported", str(exc), repo=client.public.get("repo"), hint="Use a GitHub or GitLab HTTPS/SSH repository URL.") from exc
        repository = _github_repo(owner, repo_name, token)
    return ClientTarget(client.name, repository, client.branch)


_REPOS: dict[tuple[str, str, str, str], Any] = {}


def _cache_key(provider: str, host: str, path: str, token: str) -> tuple[str, str, str, str]:
    return provider, host, path, hashlib.sha256(token.encode()).hexdigest()[:16]


def _github_repo(owner: str, repo: str, token: str) -> GitHubRepo:
    key = _cache_key("github", owner, repo, token)
    found = _REPOS.get(key)
    if found is None:
        if len(_REPOS) > 32:
            _REPOS.clear()
        found = _REPOS[key] = GitHubRepo(owner, repo, token)
    return found


def _gitlab_repo(base_url: str, project_path: str, token: str) -> GitLabRepo:
    key = _cache_key("gitlab", base_url, project_path, token)
    found = _REPOS.get(key)
    if found is None:
        if len(_REPOS) > 32:
            _REPOS.clear()
        found = _REPOS[key] = GitLabRepo(base_url, project_path, token)
    return found


def workspace_service() -> WorkspaceService:
    mode = "sync" if str(settings.ws_checkpoint_mode).strip().lower() == "sync" else "async"
    return WorkspaceService(_resolve, protected=settings.ws_protected_branches_raw, autocheckpoint=settings.ws_autocheckpoint, checkpoint_mode=mode, wip_files=settings.ws_wip_files)


def run_ws(fn: Callable[[WorkspaceService], dict[str, Any]]) -> dict[str, Any]:
    """Run a workspace operation and convert failures into structured, model-readable errors."""
    try:
        return fn(workspace_service())
    except WorkspaceError as exc:
        return exc.as_dict()
    except HTTPException as exc:  # client resolution/ambiguity
        detail = exc.detail if isinstance(exc.detail, (dict, str)) else str(exc.detail)
        return {"status": "error", "error_code": "client_resolution", "error": detail if isinstance(detail, str) else detail.get("message", "client could not be resolved"), "detail": detail if isinstance(detail, dict) else None, "hint": "pass client_name exactly as listed by client_check"}
    except GitHubError as exc:
        return {"status": "error", "error_code": f"git_provider_{exc.status}", "error": exc.message, "retryable": exc.retryable, "path": exc.path,
                "hint": "retry the same call" if exc.retryable else "fix token/permissions/branch, then retry"}
