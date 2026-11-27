from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException, status

from ..config import mask_repo_url, settings
from ..schemas import CommandResult
from .client_resolver_service import ClientResolverService
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .lock_service import RepoLockService
from .source_artifact_policy import validate_git_artifacts
from .git_identity import same_repository


def path_in_commit_scope(path: str, scope: list[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in scope)


class GitService:
    def __init__(self) -> None:
        self.repo = RepoService()

    def _ssh_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # GIT_SSH_COMMAND may already be set by start.sh (base64 key inject).
        # If GIT_SSH_KEY_PATH is also configured, prefer that so the connector
        # config is the single source of truth.
        key = settings.git_ssh_key_path
        if key:
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
        return env

    def _project(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        if not (project / ".git").exists():
            result = self._run(["git", "rev-parse", "--show-toplevel"], project, 20)
            if result.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_path is not a git repository")
        return project

    def status(self, project_path: str) -> CommandResult:
        return self._run(["git", "status", "--short", "--branch"], self._project(project_path), 30)

    def current_branch(self, project_path: str) -> CommandResult:
        return self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._project(project_path), 20)

    def head_sha(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return self._run(["git", "rev-parse", "HEAD"], self._project(project_path), timeout_seconds)

    def head_changed_files(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return self._run(["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"], self._project(project_path), timeout_seconds)

    def diff(self, project_path: str, staged: bool = False, timeout_seconds: int = 30) -> CommandResult:
        command = ["git", "diff", "--cached"] if staged else ["git", "diff"]
        return self._run(command, self._project(project_path), timeout_seconds)


    def changed_files(self, project_path: str, timeout_seconds: int = 30) -> CommandResult:
        """Return all unstaged/staged/untracked changed files relative to repo root."""
        project = self._project(project_path)
        commands = [
            ["git", "diff", "--name-only"],
            ["git", "diff", "--cached", "--name-only"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        ]
        seen: list[str] = []
        stdout_parts: list[str] = []
        for command in commands:
            result = self._run(command, project, timeout_seconds)
            if result.exit_code != 0:
                return result
            stdout_parts.append(result.stdout or "")
            for line in (result.stdout or "").splitlines():
                rel = line.strip()
                if rel and rel not in seen:
                    seen.append(rel)
        return CommandResult(
            command=["git", "changed-files"],
            cwd=str(project),
            exit_code=0,
            stdout="\n".join(seen) + ("\n" if seen else ""),
        )

    def fetch_remote(self, project_path: str, remote: str = "origin", timeout_seconds: int = 120) -> CommandResult:
        """Fetch remote refs without modifying the working tree."""
        project = self._project(project_path)
        return self._run(["git", "fetch", "--prune", remote], project, timeout_seconds)

    def compare_with_remote(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 30) -> CommandResult:
        """Return ``ahead behind`` counts for HEAD versus ``remote/branch``."""
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD", None}:
            return CommandResult(command=["git", "rev-list"], cwd=str(project), exit_code=2, stderr="Cannot compare remote without an explicit branch.")
        result = self._run(["git", "rev-list", "--left-right", "--count", f"HEAD...{remote}/{branch}"], project, timeout_seconds)
        if result.exit_code != 0:
            return result
        parts = (result.stdout or "").strip().replace("\t", " ").split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return CommandResult(command=result.command, cwd=str(project), exit_code=2, stdout=result.stdout, stderr="Unexpected rev-list ahead/behind output.")
        return CommandResult(command=["git", "compare-with-remote", remote, branch], cwd=str(project), exit_code=0, stdout=f"{parts[0]} {parts[1]}\n")

    def fast_forward(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 120) -> CommandResult:
        """Fast-forward a clean checkout to the fetched remote branch only."""
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD", None}:
            return CommandResult(command=["git", "merge", "--ff-only"], cwd=str(project), exit_code=2, stderr="Cannot fast-forward without an explicit branch.")
        return self._run(["git", "merge", "--ff-only", f"{remote}/{branch}"], project, timeout_seconds)

    def head_parent_sha(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        """Return the first parent of HEAD for commit-parent safety assertions."""
        return self._run(["git", "rev-parse", "HEAD^"], self._project(project_path), timeout_seconds)

    def remote_head_sha(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 30) -> CommandResult:
        """Read the remote branch SHA after push so safe deploy can prove GitHub received it."""
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD", None}:
            return CommandResult(command=["git", "ls-remote", remote], cwd=str(project), exit_code=2, stderr="Cannot verify remote SHA without an explicit branch.")
        result = self._run(["git", "ls-remote", "--heads", remote, branch], project, timeout_seconds)
        if result.exit_code != 0:
            return result
        line = (result.stdout or "").strip().splitlines()[:1]
        if not line:
            return CommandResult(command=["git", "ls-remote", "--heads", remote, branch], cwd=str(project), exit_code=1, stderr=f"Remote branch not found: {remote}/{branch}")
        sha = line[0].split()[0]
        return CommandResult(command=["git", "remote-head-sha", remote, branch], cwd=str(project), exit_code=0, stdout=sha + "\n")

    def configure_identity(self, project_path: str) -> CommandResult:
        project = self._project(project_path)
        name_result = self._run(["git", "config", "user.name", settings.git_author_name], project, 20)
        if name_result.exit_code != 0:
            return name_result
        return self._run(["git", "config", "user.email", settings.git_author_email], project, 20)

    def validate_target(self, project_path: str, branch: str | None = None, remote: str = "origin") -> CommandResult:
        project = self._project(project_path)
        client = ClientResolverService().resolve_client(project_path=str(project), require_complete=False)
        current = self.current_branch(project_path)
        if current.exit_code != 0:
            return current
        current_branch = current.stdout.strip()
        expected_branch = (client.branch if client else None) or settings.target_git_branch
        expected_repo = (client.repo if client else None) or settings.target_git_repo
        target_branch = branch or expected_branch or current_branch

        if expected_branch and target_branch != expected_branch:
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push to {target_branch}; expected {expected_branch}",
            )
        if expected_branch and current_branch not in {expected_branch, "HEAD"}:
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push from branch {current_branch}; expected {expected_branch}",
            )

        remote_result = self._run(["git", "remote", "get-url", remote], project, 20)
        if remote_result.exit_code != 0:
            return remote_result
        remote_url = remote_result.stdout.strip()
        if expected_repo and not self._remote_matches(remote_url, expected_repo):
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push to remote {remote_url}; expected {expected_repo}",
            )
        return CommandResult(command=["git", "validate-target"], cwd=str(project), exit_code=0, stdout="Target git remote/branch validated.\n")


    def fetch_reset_hard(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 120) -> CommandResult:
        """Force the local checkout to match origin/<branch>.

        Used by workflow checkout guards when a module was known to exist from a
        prior deploy but the local working tree no longer contains it, which can
        happen after an ephemeral workspace reset or when a different checkout
        instance pushed the module earlier.
        """
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD", None}:
            return CommandResult(
                command=["git", "fetch", remote, "&&", "git", "reset", "--hard", f"{remote}/<branch>"],
                cwd=str(project),
                exit_code=2,
                stderr="Cannot sync checkout without an explicit branch.",
            )
        remote_result = self._run(["git", "remote", "get-url", remote], project, 20)
        if remote_result.exit_code != 0:
            return remote_result
        fetch = self._run(["git", "fetch", remote], project, timeout_seconds)
        if fetch.exit_code != 0:
            return fetch
        return self._run(["git", "reset", "--hard", f"{remote}/{branch}"], project, timeout_seconds)

    def pull_rebase(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 120) -> CommandResult:
        """Legacy pre-commit hook: inspect upstream without rebasing verified source.

        Integration belongs in the isolated workspace sync path, where snapshots
        preserve both intents and a changed tree invalidates verification. This
        hook never stashes, resets, rebases or aborts an operator's Git operation.
        The caller holds the repository lock through preflight and commit.
        """
        project = self._project(project_path)
        command = ["git", "sync-preflight", remote]
        git_dir = self._run(["git", "rev-parse", "--absolute-git-dir"], project, 20)
        if git_dir.exit_code != 0:
            return git_dir
        state_dir = Path(git_dir.stdout.strip())
        active = [name for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
                  if (state_dir / name).exists()]
        unmerged = self._run(["git", "ls-files", "--unmerged"], project, 20)
        if unmerged.exit_code != 0:
            return unmerged
        if active or unmerged.stdout.strip():
            return CommandResult(command=command, cwd=str(project), exit_code=1,
                                 stderr="GIT_OPERATION_IN_PROGRESS: Existing merge/rebase or unresolved index preserved. Resolve or abort that operation before committing.")
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD"}:
            return CommandResult(command=command, cwd=str(project), exit_code=0,
                                 stdout="Detached HEAD; no upstream integration attempted.\n")
        remote_result = self._run(["git", "remote", "get-url", remote], project, 20)
        if remote_result.exit_code != 0:
            if remote_result.exit_code != 2:
                return remote_result
            return CommandResult(command=command, cwd=str(project), exit_code=0,
                                 stdout=f"Remote {remote} not configured; local commit only.\n")
        advertised = self._run(["git", "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}"], project, min(timeout_seconds, 30))
        if advertised.exit_code != 0:
            return advertised
        if not advertised.stdout.strip():
            return CommandResult(command=command, cwd=str(project), exit_code=0,
                                 stdout="Remote target branch does not exist; no upstream integration needed.\n")
        # Fetch exactly the target branch. FETCH_HEAD is read under the same
        # repository lock; a second remote update will still fail normal push.
        fetch = self._run(["git", "fetch", "--no-tags", "--no-recurse-submodules", "--", remote, f"refs/heads/{branch}"], project, timeout_seconds)
        if fetch.exit_code != 0:
            return fetch
        fetched = self._run(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"], project, 20)
        if fetched.exit_code != 0:
            return fetched
        ancestor = self._run(["git", "merge-base", "--is-ancestor", fetched.stdout.strip(), "HEAD"], project, 20)
        if ancestor.exit_code == 1:
            return CommandResult(command=command, cwd=str(project), exit_code=1,
                                 stderr="SYNC_REQUIRED: Remote advanced or diverged. Local commits, index, working tree and untracked files are unchanged. Sync in an isolated execution workspace, resolve both intents, rerun verification, then retry normal push. No in-place rebase or force push was performed.")
        if ancestor.exit_code != 0:
            return ancestor
        return CommandResult(command=command, cwd=str(project), exit_code=0,
                             stdout="HEAD already contains the fetched remote tip; source unchanged.\n")

    def prospective_commit_tree(self, project_path: str, paths: list[str], timeout_seconds: int = 60) -> str:
        """Snapshot a scoped commit without altering the checkout's real index."""
        project = self._project(project_path)
        with tempfile.TemporaryDirectory(prefix="odoo-deploy-index-") as temporary:
            env = {**self._ssh_env(), "GIT_INDEX_FILE": str(Path(temporary) / "index")}
            for args in (["read-tree", "HEAD"], ["--literal-pathspecs", "add", "-A", "--", *paths], ["write-tree"]):
                result = self._run(["git", *args], project, timeout_seconds, env=env)
                if result.exit_code:
                    raise HTTPException(409, f"Cannot snapshot deploy tree: {result.stderr or result.stdout}")
        return result.stdout.strip()

    def commit_tree(self, project_path: str, sha: str) -> str:
        result = self._run(["git", "rev-parse", "--verify", f"{sha}^{{tree}}"], self._project(project_path), 20)
        if result.exit_code:
            raise HTTPException(409, "Cannot read the committed deploy tree")
        return result.stdout.strip()

    def _staged_scope_error(self, project: Path, scope: list[str], timeout_seconds: int) -> CommandResult | None:
        command = ["git", "diff", "--cached", "--no-renames", "--name-only", "-z", "--"]
        result = self._run(command, project, timeout_seconds)
        if result.exit_code:
            return result
        if len(result.stdout) >= 20000:
            return result.model_copy(update={"exit_code": 1, "stderr": "Staged path list exceeds the review limit. Split the commit."})
        if any(not path_in_commit_scope(path, scope) for path in result.stdout.split("\0") if path):
            return result.model_copy(update={"exit_code": 1, "stderr": "Unrelated staged paths exist. Preserve them in a separate commit or use an isolated workspace."})
        return None

    def commit(self, project_path: str, message: str, paths: list[str], timeout_seconds: int = 60) -> CommandResult:
        project = self._project(project_path)
        with RepoLockService().locked(str(project), owner="git.commit", timeout_seconds=min(timeout_seconds, 120)):
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
            sync_result = self.pull_rebase(str(project), "origin", branch, min(timeout_seconds, 120))
            if sync_result.exit_code != 0:
                return sync_result
            identity_result = self.configure_identity(str(project))
            if identity_result.exit_code != 0:
                return identity_result
            if paths:
                scope = [self.repo.target(str(project), rel).relative_to(project).as_posix() for rel in paths]
                if "." in scope:
                    raise HTTPException(400, "Explicit commit paths must identify files or directories below the repository root")
                scope_error = self._staged_scope_error(project, scope, timeout_seconds)
                if scope_error is not None:
                    return scope_error
                add_command = ["git", "--literal-pathspecs", "add", "--", *scope]
            else:
                add_command = ["git", "add", "-A"]
            add_result = self._run(add_command, project, timeout_seconds)
            if add_result.exit_code != 0:
                return add_result
            if paths:
                scope_error = self._staged_scope_error(project, scope, timeout_seconds)
                if scope_error is not None:
                    return scope_error
            diff_result = self._run(["git", "diff", "--cached", "--quiet"], project, timeout_seconds)
            if diff_result.exit_code == 0:
                return CommandResult(command=["git", "commit", "-m", message], cwd=str(project), exit_code=1, stderr="No staged changes to commit.")
            if diff_result.exit_code != 1:
                return diff_result
            validate_git_artifacts(project, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", "--"])
            return self._run(["git", "commit", "-m", message], project, timeout_seconds)


    def revert_commit(self, project_path: str, sha: str, timeout_seconds: int = 120) -> CommandResult:
        project = self._project(project_path)
        if not sha or not all(c in "0123456789abcdefABCDEF" for c in sha[:40]):
            return CommandResult(command=["git", "revert", sha], cwd=str(project), exit_code=2, stderr="Unsafe or empty SHA for revert.")
        return self._run(["git", "revert", "--no-edit", sha], project, timeout_seconds)

    def push(self, project_path: str, remote: str = "origin", branch: str | None = None, set_upstream: bool = True, timeout_seconds: int = 120, *, verified_sha: str | None = None) -> CommandResult:
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(project_path)
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        client = ClientResolverService().resolve_client(project_path=str(project), require_complete=False)
        if not branch:
            branch = (client.branch if client else None) or settings.target_git_branch
        if branch in {"", "HEAD"}:
            return CommandResult(command=["git", "push"], cwd=str(project), exit_code=2, stderr="Cannot push detached HEAD without explicit branch.")

        validation = self.validate_target(str(project), branch, remote)
        if validation.exit_code != 0:
            return validation

        command = ["git", "push"]
        source = verified_sha or "HEAD"
        if verified_sha:
            command.extend(["--", remote, f"{verified_sha}:refs/heads/{branch}"])
        elif set_upstream:
            command.extend(["-u", remote, f"HEAD:{branch}"])
        else:
            command.extend([remote, f"HEAD:{branch}"])
        with RepoLockService().locked(str(project), owner="git.push", timeout_seconds=min(timeout_seconds, 120)):
            from .execution_service import git
            tracking = f"refs/remotes/{remote}/{branch}"
            try:
                base = git(["rev-parse", "--verify", tracking], project).strip()
            except HTTPException:
                base = None
            validate_git_artifacts(project, ["diff", "--name-only", "-z", "--diff-filter=ACMRT", base, source, "--"]
                                   if base else ["ls-tree", "-r", "-z", "--name-only", source])
            result = self._run(command, project, timeout_seconds)
            failure_text = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
            if result.exit_code != 0 and any(marker in failure_text for marker in ("non-fast-forward", "fetch first", "remote contains work")):
                return CommandResult(command=command, cwd=str(project), exit_code=1,
                                     stderr="SYNC_REQUIRED: Remote advanced. Fetch/merge in an isolated execution workspace, rerun verification, then retry normal push. No automatic unverified rebase/push was performed.")
        if result.exit_code == 0:
            current_sha = self.head_sha(str(project)).stdout.strip()
            sha = verified_sha or current_sha
            result.stdout = (result.stdout or "") + f"\nPUSH_OK\nbranch={branch}\nsha={sha}\n"
            if verified_sha and current_sha != verified_sha:
                result.stdout += "CODING_WORKSPACE_SYNC=deferred\n"
                return result
            # Workspace context is an optimization, never a push dependency.
            # Rebind unchanged module snapshots to the pushed SHA and refresh
            # only modules whose source fingerprint changed.
            try:
                from .coding_session_service import CodingWorkspaceService

                changed_result = self.head_changed_files(str(project))
                changed_paths = (
                    [line.strip() for line in (changed_result.stdout or "").splitlines() if line.strip()]
                    if changed_result.exit_code == 0
                    else []
                )
                workspace_sync = CodingWorkspaceService().schedule_project_sync(
                    str(project),
                    sha,
                    changed_paths,
                )
                result.stdout += "CODING_WORKSPACE_SYNC=" + json.dumps(
                    workspace_sync,
                    separators=(",", ":"),
                    sort_keys=True,
                ) + "\n"
            except Exception:  # noqa: BLE001 - cache maintenance cannot fail a valid push
                result.stdout += "CODING_WORKSPACE_SYNC=deferred\n"
        return result

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int, *, env: dict[str, str] | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env if env is not None else self._ssh_env(),
            )
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=mask_repo_url(completed.stdout[-20000:]),
                stderr=mask_repo_url(completed.stderr[-20000:]),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=124,
                stdout=mask_repo_url((exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else ""),
                stderr=mask_repo_url((exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else ""),
                timed_out=True,
            )

    def _remote_matches(self, configured: str, expected: str) -> bool:
        return same_repository(configured, expected)
