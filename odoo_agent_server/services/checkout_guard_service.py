from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import settings
from ..schemas import JobStatus
from .git_service import GitService
from .module_path_service import module_name_for_repo_relative
from .path_service import resolve_allowed_path
from .store_service import JsonStore

_SUCCESSFUL_SAFE_DEPLOY_STATUSES = {
    "passed",
    "already_deployed_confirmed",
}


def _normal_path_for_compare(value: str | Path | None) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def stale_checkout_error(module_name: str, project_path: str | Path, branch: str | None) -> str:
    return (
        f"Module '{module_name}' not found at '{project_path}' even after syncing with remote branch "
        f"'{branch or '<unknown>'}'. This checkout may be stale, wiped, or pointed at the wrong "
        "project_path/branch for this module. Verify project_path and branch are correct before retrying."
    )


def _command_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _resolve_branch(project_path: Path, branch: str | None, git: GitService) -> str | None:
    if branch:
        return branch
    configured = settings.target_git_branch
    if configured:
        return configured
    current_branch = getattr(git, "current_branch", None)
    if callable(current_branch):
        try:
            result = current_branch(str(project_path))
            if getattr(result, "exit_code", 1) == 0:
                value = (getattr(result, "stdout", "") or "").strip()
                if value and value != "HEAD":
                    return value
        except Exception:
            return None
    return None


def module_was_successfully_deployed(
    *,
    project_path: str | Path,
    module_name: str,
    client_name: str | None = None,
    store: JsonStore | None = None,
) -> bool:
    """Return True when state/jobs show this project/module previously deployed.

    The checkout-current guard is intentionally strict for safe deploy, but for
    planning/read helpers we only auto-sync/fail-fast for module checkouts we
    know should already exist from a prior successful workflow job. This avoids
    blocking legitimate first-time/new-module planning.
    """
    if not module_name:
        return False
    store = store or JsonStore()
    target_project = _normal_path_for_compare(project_path)

    states = []
    try:
        states.append(store.load_session_state(client_name=client_name))
    except Exception:
        pass
    if client_name:
        try:
            states.append(store.load_session_state())
        except Exception:
            pass
    for state in states:
        if not isinstance(state, dict):
            continue
        if state.get("module_name") != module_name:
            continue
        if _normal_path_for_compare(state.get("project_path")) != target_project:
            continue
        if str(state.get("safe_deploy_status") or "") in _SUCCESSFUL_SAFE_DEPLOY_STATUSES:
            return True

    try:
        jobs = store.list_jobs(limit=200)
    except Exception:
        return False
    for job in jobs:
        if job.job_type != "safe_deploy_module" or job.status != JobStatus.passed:
            continue
        request = job.request or {}
        result = job.result if isinstance(job.result, dict) else {}
        if request.get("module_name") != module_name and result.get("module_name") != module_name:
            continue
        request_path = request.get("project_path") or result.get("project_path")
        if _normal_path_for_compare(request_path) != target_project:
            continue
        result_status = str(result.get("status") or "")
        if not result_status or result_status in _SUCCESSFUL_SAFE_DEPLOY_STATUSES:
            return True
    return False


def _working_tree_dirty(status_result: Any) -> bool:
    if getattr(status_result, "exit_code", 1) != 0:
        return False
    return any(line.strip() and not line.startswith("##") for line in (getattr(status_result, "stdout", "") or "").splitlines())


def _ensure_checkout_matches_remote_head(
    *,
    project: Path,
    branch: str | None,
    remote: str,
    git: GitService,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Fetch and prove checkout ancestry without discarding local work.

    Clean behind-only checkouts are fast-forwarded. Dirty checkouts are preserved,
    but deploy is blocked when the remote advanced or local HEAD is ahead/diverged.
    Test doubles that do not implement the freshness API keep legacy behavior.
    """
    required = ("fetch_remote", "compare_with_remote", "head_sha", "remote_head_sha", "status")
    if not all(callable(getattr(git, name, None)) for name in required):
        return {"checked": False, "status": "unsupported_by_git_adapter", "ok": True}

    status_result = git.status(str(project))
    if getattr(status_result, "exit_code", 1) != 0:
        return {"checked": True, "status": "status_failed", "ok": False, "error": getattr(status_result, "stderr", "") or "git status failed"}
    dirty = _working_tree_dirty(status_result)
    fetch_result = git.fetch_remote(str(project), remote=remote, timeout_seconds=timeout_seconds)
    if getattr(fetch_result, "exit_code", 1) != 0:
        return {"checked": True, "status": "fetch_failed", "ok": False, "dirty": dirty, "error": getattr(fetch_result, "stderr", "") or "git fetch failed", "fetch_result": _command_dump(fetch_result)}

    local_result = git.head_sha(str(project))
    remote_result = git.remote_head_sha(str(project), remote=remote, branch=branch, timeout_seconds=min(timeout_seconds, 60))
    compare_result = git.compare_with_remote(str(project), remote=remote, branch=branch, timeout_seconds=min(timeout_seconds, 60))
    if any(getattr(item, "exit_code", 1) != 0 for item in (local_result, remote_result, compare_result)):
        return {
            "checked": True, "status": "comparison_failed", "ok": False, "dirty": dirty,
            "error": getattr(compare_result, "stderr", "") or getattr(remote_result, "stderr", "") or "Unable to compare local and remote HEAD",
        }
    local_head = (getattr(local_result, "stdout", "") or "").strip()
    remote_head = (getattr(remote_result, "stdout", "") or "").strip()
    ahead_text, behind_text = (getattr(compare_result, "stdout", "") or "0 0").strip().split()[:2]
    ahead, behind = int(ahead_text), int(behind_text)
    result = {
        "checked": True, "ok": True, "status": "current", "dirty": dirty,
        "local_head": local_head, "remote_head": remote_head, "ahead": ahead, "behind": behind,
        "branch": branch, "remote": remote,
    }
    if ahead and behind:
        result.update({"ok": False, "status": "diverged", "error": "Local checkout diverged from the remote branch; refusing to commit or push."})
        return result
    if ahead:
        result.update({"ok": False, "status": "local_ahead", "error": "Local checkout contains commits not present on the remote branch; refusing to bundle or replay them in a new deploy."})
        return result
    if behind:
        if dirty:
            result.update({"ok": False, "status": "remote_advanced_dirty", "error": "Remote branch advanced while the checkout has local changes; preserve the diff and rebase it on the latest remote HEAD before deploying."})
            return result
        fast_forward = getattr(git, "fast_forward", None)
        if not callable(fast_forward):
            result.update({"ok": False, "status": "fast_forward_unavailable", "error": "Checkout is behind remote and the Git adapter cannot fast-forward safely."})
            return result
        ff_result = fast_forward(str(project), remote=remote, branch=branch, timeout_seconds=timeout_seconds)
        if getattr(ff_result, "exit_code", 1) != 0:
            result.update({"ok": False, "status": "fast_forward_failed", "error": getattr(ff_result, "stderr", "") or "git merge --ff-only failed", "fast_forward_result": _command_dump(ff_result)})
            return result
        refreshed = git.head_sha(str(project))
        result.update({
            "status": "fast_forwarded", "local_head_before": local_head,
            "local_head": (getattr(refreshed, "stdout", "") or "").strip(),
            "ahead": 0, "behind": 0, "fast_forward_result": _command_dump(ff_result),
        })
    elif dirty:
        result["status"] = "dirty_current"
    return result


def ensure_module_exists_or_sync(
    *,
    project_path: str | Path,
    module_name: str,
    module_root_rel: str | None = None,
    branch: str | None,
    remote: str = "origin",
    git: GitService | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Ensure ``project_path/module_name`` exists, syncing the checkout if needed.

    When the module folder is already present, this performs no git network work.
    When it is missing, the function does exactly one authoritative sync
    (``git fetch`` + ``git reset --hard origin/<branch>`` via GitService) and
    re-checks the module path. It never falls back to broad ``ls -R``/``find``
    exploration; callers can fail immediately with the returned clear error.
    """
    project = resolve_allowed_path(str(project_path))
    module_path = project / (module_root_rel or module_name)
    git = git or GitService()
    resolved_branch = _resolve_branch(project, branch, git)

    result: dict[str, Any] = {
        "ok": True,
        "status": "present",
        "project_path": str(project),
        "module_name": module_name,
        "module_root_rel": module_root_rel or module_name,
        "module_path": str(module_path),
        "branch": resolved_branch,
        "remote": remote,
        "sync_attempted": False,
    }

    if module_path.exists():
        freshness = _ensure_checkout_matches_remote_head(
            project=project, branch=resolved_branch, remote=remote, git=git, timeout_seconds=timeout_seconds,
        )
        result["freshness"] = freshness
        result.update({
            "ok": bool(freshness.get("ok", True)),
            "status": str(freshness.get("status") or "present"),
            "local_head": freshness.get("local_head"),
            "remote_head": freshness.get("remote_head"),
            "ahead": freshness.get("ahead"),
            "behind": freshness.get("behind"),
            "dirty": freshness.get("dirty"),
        })
        if not result["ok"]:
            result["error"] = freshness.get("error")
        return result

    result.update({"ok": False, "status": "sync_failed", "sync_attempted": True})
    sync_result = None
    try:
        fetch_reset = getattr(git, "fetch_reset_hard", None)
        if callable(fetch_reset):
            sync_result = fetch_reset(str(project), remote=remote, branch=resolved_branch, timeout_seconds=timeout_seconds)
        else:
            pull_rebase = getattr(git, "pull_rebase", None)
            if not callable(pull_rebase):
                raise RuntimeError("GitService has no fetch_reset_hard() or pull_rebase() method")
            sync_result = pull_rebase(str(project), remote=remote, branch=resolved_branch, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        result["error"] = stale_checkout_error(module_name, project, resolved_branch)
        result["sync_error"] = str(exc)
        return result

    result["sync_result"] = _command_dump(sync_result)
    if getattr(sync_result, "exit_code", 1) != 0:
        result["error"] = stale_checkout_error(module_name, project, resolved_branch)
        result["sync_error"] = getattr(sync_result, "stderr", "") or getattr(sync_result, "stdout", "") or "git sync failed"
        return result

    if module_path.exists():
        result.update({"ok": True, "status": "synced", "error": None})
        return result

    result.update({
        "ok": False,
        "status": "missing_after_sync",
        "error": stale_checkout_error(module_name, project, resolved_branch),
    })
    return result


def maybe_ensure_known_module_current(
    *,
    project_path: str | Path,
    module_name: str | None,
    branch: str | None,
    client_name: str | None = None,
    remote: str = "origin",
    git: GitService | None = None,
    timeout_seconds: int = 120,
    require_prior_success: bool = True,
) -> dict[str, Any] | None:
    """Run the checkout guard when there is a concrete module/project target.

    ``None`` means the guard was intentionally skipped: no module was known, or
    ``require_prior_success`` was true and there is no evidence this module was
    previously deployed successfully in this session/job store.
    """
    if not module_name or module_name.startswith("<"):
        return None
    if require_prior_success and not module_was_successfully_deployed(
        project_path=project_path,
        module_name=module_name,
        client_name=client_name,
    ):
        return None
    return ensure_module_exists_or_sync(
        project_path=project_path,
        module_name=module_name,
        branch=branch,
        remote=remote,
        git=git,
        timeout_seconds=timeout_seconds,
    )


def module_name_from_relative_path(relative_path: str, project_path: str | None = None) -> str | None:
    if project_path:
        resolved = module_name_for_repo_relative(Path(project_path), relative_path)
        if resolved:
            return resolved
    value = str(relative_path or "").replace("\\", "/").strip("/")
    if not value or value.startswith("."):
        return None
    parts = value.split("/")
    source_dirs = {"models", "views", "controllers", "security", "wizard", "wizards", "report", "reports", "data", "demo", "static", "tests"}
    for index, part in enumerate(parts):
        if part in source_dirs and index > 0:
            return parts[index - 1]
    if parts[-1] in {"__manifest__.py", "__openerp__.py", "__init__.py"} and len(parts) > 1:
        return parts[-2]
    first = parts[0]
    if not first or first in {"..", ".git"}:
        return None
    return first
