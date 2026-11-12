from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException, status

from ..config import mask_repo_url, settings
from ..schemas import CommandResult, CustomModuleItem, FileContent, FileListItem, RepoCleanPycResult, RepoPrepareResult
from .client_resolver_service import ClientResolverService
from .path_service import resolve_allowed_path
from .module_path_service import is_ignored_source_path
from .lock_service import RepoLockService
from .odoo_package_wiring_service import OdooPackageWiringService

_TEXT_LIMIT_HINT = "File is too large for this endpoint. Increase max_bytes or use a narrower file."


class RepoService:
    """Safe local repository operations constrained by configured allowed roots."""

    _module_cache_lock = RLock()
    _module_cache: dict[tuple[str, int, bool, str], tuple[float, list[CustomModuleItem]]] = {}
    _module_cache_ttl_seconds = 300.0

    def project(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        if not project.exists() or not project.is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project path not found: {project}")
        return project

    def target(self, project_path: str, relative_path: str) -> Path:
        project = self.project(project_path)
        return self._safe_join(project, relative_path)


    def prepare_repo(
        self,
        repo_url: str | None = None,
        branch: str | None = None,
        repo_name: str | None = None,
        workspace_root: str | None = None,
        remote: str = "origin",
        timeout_seconds: int = 180,
        project_path: str | None = None,
        client_name: str | None = None,
    ) -> RepoPrepareResult:
        """Clone or refresh the target repository before any coding task.

        This is the repo-first entry point MCP agents should use for coding work:
        clone if missing; otherwise fetch, checkout, and pull the requested branch.
        """
        resolver = ClientResolverService()
        client = resolver.resolve_client(
            client_name=client_name,
            project_path=project_path or settings.target_project_path,
            require_complete=False,
        )
        use_client_repo = bool(client and (not repo_url or repo_url == client.repo))
        resolved_repo_url = (client.authenticated_repo if use_client_repo and client else None) or repo_url or settings.target_git_repo
        plain_repo_url = repo_url or (client.repo if client else None) or settings.target_git_repo
        resolved_branch = branch or (client.branch if client else None) or settings.target_git_branch
        resolved_project_path = project_path or (client.path if client else None) or settings.target_project_path
        if not resolved_repo_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="repo_url is required or TARGET_GIT_REPO must be configured")
        if not resolved_branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch is required or TARGET_GIT_BRANCH must be configured")
        if not self._safe_branch_name(resolved_branch):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe branch name: {resolved_branch}")
        if not self._safe_remote_name(remote):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe remote name: {remote}")

        if resolved_project_path:
            project = resolve_allowed_path(resolved_project_path)
            workspace = project.parent
            workspace.mkdir(parents=True, exist_ok=True)
            repo_name = project.name
        else:
            workspace = resolve_allowed_path(workspace_root or str(settings.workspace_path))
            workspace.mkdir(parents=True, exist_ok=True)
            if not repo_name:
                repo_name = self._repo_name_from_url(resolved_repo_url)
            if not self._safe_repo_folder_name(repo_name):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe repo_name: {repo_name}")
            project = (workspace / repo_name).resolve()
            if not (project == workspace or workspace in project.parents):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Resolved project path escapes workspace root")

        env = self._git_env()
        steps: list[CommandResult] = []
        already_cloned = project.exists()

        if already_cloned:
            if not (project / ".git").exists():
                if any(project.iterdir()):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Selected client path exists but is not a git repository and is not empty: {project}. "
                            "Refusing to clone over existing files."
                        ),
                    )
                clone = self._run(["git", "clone", "--filter=blob:none", "--branch", resolved_branch, "--single-branch", resolved_repo_url, str(project)], workspace, timeout_seconds, env=env)
                steps.append(clone)
                if clone.exit_code != 0:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=clone.stderr or clone.stdout or "git clone failed")
            remote_result = self._run(["git", "remote", "get-url", remote], project, 30, env=env)
            steps.append(remote_result)
            if remote_result.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=remote_result.stderr or remote_result.stdout or "Unable to read git remote")
            configured_remote = remote_result.stdout.strip()
            expected_remote = plain_repo_url
            if expected_remote and not self._remote_matches(configured_remote, expected_remote):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Configured remote mismatch: {configured_remote} != {expected_remote}")
            # Fetch exactly once. ``git pull`` would fetch a second time, so the
            # already-fetched remote branch is applied with a local ff-only merge.
            fetch = self._run(["git", "fetch", remote, resolved_branch], project, timeout_seconds, env=env)
            steps.append(fetch)
            if fetch.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=fetch.stderr or fetch.stdout or "git fetch failed")
            checkout = self._run(["git", "checkout", resolved_branch], project, timeout_seconds, env=env)
            steps.append(checkout)
            if checkout.exit_code != 0:
                # Create a local tracking branch when it only exists on the remote.
                checkout = self._run(["git", "checkout", "-B", resolved_branch, f"{remote}/{resolved_branch}"], project, timeout_seconds, env=env)
                steps.append(checkout)
                if checkout.exit_code != 0:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=checkout.stderr or checkout.stdout or "git checkout failed")
            merge = self._run(["git", "merge", "--ff-only", f"{remote}/{resolved_branch}"], project, timeout_seconds, env=env)
            steps.append(merge)
            if merge.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=merge.stderr or merge.stdout or "git merge --ff-only failed")
        else:
            project.parent.mkdir(parents=True, exist_ok=True)
            clone = self._run(["git", "clone", "--filter=blob:none", "--branch", resolved_branch, "--single-branch", resolved_repo_url, str(project)], workspace, timeout_seconds, env=env)
            steps.append(clone)
            if clone.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=clone.stderr or clone.stdout or "git clone failed")

        sha = self._run(["git", "rev-parse", "HEAD"], project, 30, env=env)
        steps.append(sha)
        if sha.exit_code != 0:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=sha.stderr or sha.stdout or "Unable to read HEAD SHA")
        git_status = self._run(["git", "status", "--short", "--branch"], project, 30, env=env)
        steps.append(git_status)

        return RepoPrepareResult(
            project_path=str(project),
            repo_url=plain_repo_url or resolved_repo_url,
            branch=resolved_branch,
            sha=sha.stdout.strip(),
            status=git_status.stdout,
            already_cloned=already_cloned,
            steps=steps,
        )

    def list_custom_modules(self, project_path: str, max_depth: int = 5, include_uninstallable: bool = False) -> list[CustomModuleItem]:
        """Discover custom Odoo modules from repo files, not from the Odoo database.

        Uses a Git/manifest-state token. Cache hits do not walk the repository;
        misses perform one manifest scan and reuse those exact results.
        """
        project = self.project(project_path)
        cache_token = self._module_cache_token(project, max_depth)
        cache_key = (str(project), max_depth, include_uninstallable, cache_token)
        now = time.monotonic()
        with self._module_cache_lock:
            cached = self._module_cache.get(cache_key)
            if cached and cached[0] > now:
                return list(cached[1])

        modules: list[CustomModuleItem] = []
        skip_dirs = {
            ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "node_modules", ".venv", "venv", "env", "odoo", "enterprise",
            "src/odoo", "src/enterprise", "static/lib",
        }
        for manifest in self._iter_manifest_files(project, max_depth, skip_dirs):
            try:
                manifest.resolve().relative_to(project)
            except (OSError, ValueError):
                continue
            meta = self._read_manifest_metadata(manifest)
            installable = meta.get("installable")
            if installable is False and not include_uninstallable:
                continue
            module_dir = manifest.parent
            modules.append(
                CustomModuleItem(
                    module_name=module_dir.name,
                    path=module_dir.relative_to(project).as_posix(),
                    manifest_path=manifest.relative_to(project).as_posix(),
                    installable=installable if isinstance(installable, bool) else None,
                    name=meta.get("name") if isinstance(meta.get("name"), str) else None,
                    version=meta.get("version") if isinstance(meta.get("version"), str) else None,
                    author=meta.get("author") if isinstance(meta.get("author"), str) else None,
                    application=meta.get("application") if isinstance(meta.get("application"), bool) else None,
                )
            )
        result = sorted(modules, key=lambda item: (item.module_name, item.path))
        with self._module_cache_lock:
            # Keep the cache bounded so long-running connector processes do not grow unbounded.
            if len(self._module_cache) > 32:
                self._module_cache.clear()
            self._module_cache[cache_key] = (time.monotonic() + self._module_cache_ttl_seconds, result)
        return list(result)

    def _module_cache_token(self, project: Path, max_depth: int) -> str:
        """Return a cheap token that invalidates when tracked/untracked manifests change."""
        if (project / ".git").exists():
            head = self._run(["git", "rev-parse", "HEAD"], project, 10, env=self._git_env())
            dirty = self._run(
                [
                    "git", "status", "--porcelain=v1", "--untracked-files=all", "--",
                    ":(glob)**/__manifest__.py", ":(glob)**/__openerp__.py",
                ],
                project,
                15,
                env=self._git_env(),
            )
            if head.exit_code == 0 and dirty.exit_code == 0:
                raw = f"{head.stdout.strip()}\0{dirty.stdout}\0{max_depth}"
                return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        count, latest = self._manifest_cache_stamp(project, max_depth)
        return f"fs:{count}:{latest}"

    def _iter_manifest_files(self, project: Path, max_depth: int, skip_dirs: set[str]):
        base_depth = len(project.parts)
        for current, dirs, files in os.walk(project):
            current_path = Path(current)
            rel_parts = current_path.relative_to(project).parts if current_path != project else ()
            depth = len(current_path.parts) - base_depth
            dirs[:] = sorted(
                d for d in dirs
                if d not in skip_dirs
                and "/".join((*rel_parts, d)) not in skip_dirs
                and depth < max_depth
            )
            if "__manifest__.py" in files and depth <= max_depth:
                yield current_path / "__manifest__.py"

    def _manifest_cache_stamp(self, project: Path, max_depth: int) -> tuple[int, float]:
        skip_dirs = {
            ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "node_modules", ".venv", "venv", "env", "odoo", "enterprise",
            "src/odoo", "src/enterprise", "static/lib",
        }
        count = 0
        latest = 0.0
        for manifest in self._iter_manifest_files(project, max_depth, skip_dirs):
            count += 1
            try:
                latest = max(latest, manifest.stat().st_mtime)
            except OSError:
                pass
        return count, latest

    def list_files(self, project_path: str, relative_path: str = ".", max_depth: int = 3, max_items: int = 500) -> list[FileListItem]:
        root = self.target(project_path, relative_path)
        if not root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {root}")
        if root.is_file():
            rel = root.relative_to(self.project(project_path)).as_posix()
            return [FileListItem(path=rel, kind="file", size=root.stat().st_size)]

        items: list[FileListItem] = []
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"})
            for dirname in dirs:
                rel = (current_path / dirname).relative_to(self.project(project_path)).as_posix()
                items.append(FileListItem(path=rel, kind="dir"))
                if len(items) >= max_items:
                    return items
            for filename in sorted(files):
                rel_path = current_path / filename
                rel = rel_path.relative_to(self.project(project_path)).as_posix()
                items.append(FileListItem(path=rel, kind="file", size=rel_path.stat().st_size))
                if len(items) >= max_items:
                    return items
        return items

    def read_file(self, project_path: str, relative_path: str, max_bytes: int = 300_000) -> FileContent:
        target = self.target(project_path, relative_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {target}")
        size = target.stat().st_size
        if size > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=_TEXT_LIMIT_HINT)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only UTF-8 text files are supported") from exc
        return FileContent(path=target.relative_to(self.project(project_path)).as_posix(), content=content, size=size)

    def read_range(self, project_path: str, relative_path: str, start_line: int = 1, max_lines: int = 200) -> dict:
        project = self.project(project_path)
        target = self._safe_join(project, relative_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {target}")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only UTF-8 text files are supported") from exc
        start = max(start_line, 1)
        end = min(start + max_lines - 1, len(lines))
        selected = lines[start - 1:end]
        numbered = "\n".join(f"{line_no}: {text}" for line_no, text in enumerate(selected, start=start))
        return {
            "path": target.relative_to(project).as_posix(),
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": end < len(lines),
            "next_call": {
                "tool": "repo.read_range",
                "arguments": {
                    "project_path": str(project),
                    "relative_path": target.relative_to(project).as_posix(),
                    "start_line": end + 1,
                    "max_lines": max_lines,
                },
            } if end < len(lines) else None,
            "content": numbered,
        }

    def search(self, project_path: str, pattern: str, relative_path: str = ".", max_matches: int = 100, context_lines: int = 2, regex: bool = False) -> dict:
        project = self.project(project_path)
        root = self._safe_join(project, relative_path)
        if not root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {root}")
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern))
        except re.error as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid regex pattern: {exc}") from exc
        files = [root] if root.is_file() else self._iter_search_files(root)
        matches: list[dict[str, object]] = []
        scanned_files = 0
        skipped_binary = 0
        for file_path in files:
            if len(matches) >= max_matches:
                break
            scanned_files += 1
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                skipped_binary += 1
                continue
            except OSError:
                continue
            for index, line in enumerate(lines):
                if not compiled.search(line):
                    continue
                context_start = max(0, index - context_lines)
                context_end = min(len(lines), index + context_lines + 1)
                context = [
                    {"line": line_no + 1, "text": lines[line_no]}
                    for line_no in range(context_start, context_end)
                ]
                rel = file_path.relative_to(project).as_posix()
                matches.append({"path": rel, "line": index + 1, "text": line, "context": context})
                if len(matches) >= max_matches:
                    break
        return {
            "pattern": pattern,
            "regex": regex,
            "relative_path": root.relative_to(project).as_posix() if root != project else ".",
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= max_matches,
            "scanned_files": scanned_files,
            "skipped_binary_files": skipped_binary,
            "next_action": "Use repo.read_range on a matched path and line number for exact surrounding code.",
        }

    def _iter_search_files(self, root: Path):
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = sorted(
                d for d in dirs
                if not is_ignored_source_path((current_path / d).relative_to(root).parts)
            )
            for filename in sorted(files):
                yield Path(current) / filename

    # Fix 2/6: line-count threshold above which chunked mode is recommended.
    _LARGE_FILE_LINE_THRESHOLD = 1000
    # Fix 6: unified-diff line limit to include in response (keeps payload small)
    _DIFF_PREVIEW_LINES = 60

    # Fix 7: below this fraction of prior size, a single-write overwrite of an
    # existing non-empty file is treated as a likely accidental data-loss and
    # rejected unless the caller explicitly passes force=True.
    _OVERWRITE_SHRINK_GUARD_RATIO = 0.3

    def write_file(
        self,
        project_path: str,
        relative_path: str,
        content: str,
        create_dirs: bool = True,
        chunk_index: int = 0,
        total_chunks: int = 1,
        mode: str = "overwrite",
        force: bool = False,
    ) -> FileContent:
        """Write a file, with optional chunked mode for large files (>1000 lines).

        Chunked mode (total_chunks > 1):
          chunk_index=0 → create/overwrite file with first chunk
          chunk_index=1..N-1 → append to existing partial file
          chunk_index=N-1 (last) → append final chunk, return complete file info

        Fix 2: Files written in a single call (total_chunks==1) that exceed
        _LARGE_FILE_LINE_THRESHOLD lines will carry a 'large_file_warning' in
        the response extra — MCP agents must use chunked mode next time.

        Fix 6: When overwriting an existing file in single-write mode (chunk_index==0,
        total_chunks==1), the response always includes a short unified diff showing
        what changed, allowing the agent to verify correctness before committing.

        Fix 7 (mode + shrink guard): mode='append' adds content to the end of an
        existing file without touching prior content, instead of replacing it —
        this was previously unsupported, causing silent full-file overwrites when
        an agent intended to append. mode='overwrite' (default) preserves the
        original behavior exactly. For mode='overwrite' against an existing
        non-empty file, if the new content is dramatically smaller than the old
        content (likely accidental truncation/overwrite) and force is not True,
        the write is rejected with a 409 and the diff preview so the caller can
        review before forcing it through.
        """
        self._reject_git_internal_path(relative_path)
        with RepoLockService().locked(project_path, owner="repo.write", timeout_seconds=60):
            project = self.project(project_path)
            target = self.target(project_path, relative_path)
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.exists():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent directory not found: {target.parent}"
                )

            # Fix 6: capture old content before overwrite for diff preview
            old_target_bytes = target.read_bytes() if target.exists() and target.is_file() else None
            old_content: str | None = None
            if total_chunks == 1 and target.exists() and target.is_file():
                try:
                    old_content = target.read_text(encoding="utf-8")
                except Exception:
                    old_content = None

            if total_chunks == 1 and mode == "append":
                # Fix 7: explicit append mode — never touches existing content.
                with open(target, "a", encoding="utf-8") as f:
                    f.write(content)
                content = (old_content or "") + content
            elif total_chunks == 1 and mode == "overwrite" and old_content is not None and old_content.strip():
                # Fix 7: shrink guard against accidental full-file overwrite.
                old_len = len(old_content)
                new_len = len(content)
                if not force and old_len > 200 and new_len < old_len * self._OVERWRITE_SHRINK_GUARD_RATIO:
                    diff_preview = self._unified_diff_preview(
                        old_content,
                        content,
                        str(target.relative_to(self.project(project_path)).as_posix()),
                        max_lines=self._DIFF_PREVIEW_LINES,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "overwrite_shrink_guard",
                            "message": (
                                f"Refusing overwrite: new content ({new_len} bytes) is far smaller than "
                                f"the existing file ({old_len} bytes) for {relative_path}. If you meant to "
                                f"append instead of replace, resend with mode='append'. If this overwrite "
                                f"is intentional, resend with force=true."
                            ),
                            "diff_preview": diff_preview,
                        },
                    )
                tmp = target.with_name(f".{target.name}.tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
            elif total_chunks == 1:
                # Normal single-write: atomic replace
                tmp = target.with_name(f".{target.name}.tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
            elif chunk_index == 0:
                # First chunk: create/overwrite
                tmp = target.with_name(f".{target.name}.chunk.tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
            else:
                # Subsequent chunks: append
                if not target.exists():
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Chunk {chunk_index}: base file missing — send chunk_index=0 first"
                    )
                with open(target, "a", encoding="utf-8") as f:
                    f.write(content)

            auto_wired: list[str] = []
            should_wire = target.suffix == ".py" and (total_chunks == 1 or chunk_index == total_chunks - 1)
            if should_wire:
                wiring_backups: dict[str, bytes | None] = {}
                primary_rel = target.relative_to(project).as_posix()
                try:
                    expanded, auto_wired = OdooPackageWiringService().expand(project, [{
                        "relative_path": primary_rel,
                        "content": target.read_text(encoding="utf-8"),
                        "create_dirs": create_dirs,
                        "mode": "overwrite",
                        "force": force,
                    }])
                    for item in expanded:
                        rel = str(item["relative_path"])
                        if rel == primary_rel:
                            continue
                        init_target = self._safe_join(project, rel)
                        wiring_backups[rel] = init_target.read_bytes() if init_target.is_file() else None
                        init_target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = init_target.with_name(f".{init_target.name}.tmp.{uuid4().hex[:8]}")
                        tmp.write_text(str(item["content"]), encoding="utf-8")
                        tmp.replace(init_target)
                except Exception as exc:  # noqa: BLE001
                    if old_target_bytes is None:
                        target.unlink(missing_ok=True)
                    else:
                        rollback = target.with_name(f".{target.name}.rollback.{uuid4().hex[:8]}")
                        rollback.write_bytes(old_target_bytes)
                        rollback.replace(target)
                    for rel, prior in wiring_backups.items():
                        init_target = self._safe_join(project, rel)
                        if prior is None:
                            init_target.unlink(missing_ok=True)
                        else:
                            rollback = init_target.with_name(f".{init_target.name}.rollback.{uuid4().hex[:8]}")
                            rollback.write_bytes(prior)
                            rollback.replace(init_target)
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Python write rolled back because Odoo package auto-wiring failed: {exc}",
                    ) from exc

            size = target.stat().st_size
            written_text = target.read_text(encoding="utf-8") if target.exists() else ""
            lines = written_text.count("\n")
            is_complete = (chunk_index == total_chunks - 1) or total_chunks == 1

            # Build extra metadata
            extra: dict = {}
            if total_chunks > 1:
                extra = {
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "complete": is_complete,
                    "total_lines": lines,
                    "message": (
                        f"File complete ({lines} lines, {size} bytes)"
                        if is_complete
                        else f"Chunk {chunk_index+1}/{total_chunks} written. Send next chunk with chunk_index={chunk_index+1}"
                    ),
                }
            else:
                # Fix 2: large file warning
                if lines > self._LARGE_FILE_LINE_THRESHOLD:
                    extra["large_file_warning"] = (
                        f"File has {lines} lines. Single-pass repo.write is supported up to "
                        f"{self._LARGE_FILE_LINE_THRESHOLD} lines. For larger files use chunked "
                        f"mode (total_chunks>1); follow-up chunks are append-only and do not have "
                        f"this single-pass warning limit."
                    )
                # Fix 6: diff preview on overwrite
                if old_content is not None:
                    diff_preview = self._unified_diff_preview(
                        old_content,
                        content,
                        str(target.relative_to(self.project(project_path)).as_posix()),
                        max_lines=self._DIFF_PREVIEW_LINES,
                    )
                    extra["diff_preview"] = diff_preview if diff_preview else "[no changes — content identical]"
                else:
                    extra["diff_preview"] = "[new file created]"
            if auto_wired:
                extra["auto_wired_init_files"] = auto_wired

            return FileContent(
                path=target.relative_to(self.project(project_path)).as_posix(),
                content=content if is_complete else f"[chunk {chunk_index+1}/{total_chunks} appended]",
                size=size,
                extra=extra,
            )

    @staticmethod
    def _unified_diff_preview(old: str, new: str, filename: str, max_lines: int = 60) -> str:
        """Return a short unified diff string (truncated to max_lines)."""
        import difflib
        diff_lines = list(difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=3,
        ))
        if not diff_lines:
            return ""
        if len(diff_lines) > max_lines:
            diff_lines = diff_lines[:max_lines]
            diff_lines.append(f"\n... (diff truncated at {max_lines} lines)\n")
        return "".join(diff_lines)

    def delete_file(self, project_path: str, relative_path: str) -> dict[str, str]:
        self._reject_git_internal_path(relative_path)
        with RepoLockService().locked(project_path, owner="repo.delete", timeout_seconds=60):
            target = self.target(project_path, relative_path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {target}")
            target.unlink()
            return {"deleted": target.relative_to(self.project(project_path)).as_posix()}

    def apply_patch(self, project_path: str, patch: str, check_only: bool = False, timeout_seconds: int = 60) -> CommandResult:
        project = self.project(project_path)
        self._validate_patch_paths(project, patch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            check_cmd = ["git", "apply", "--check", str(patch_path)]
            check = self._run(check_cmd, project, timeout_seconds)
            if check.exit_code != 0 or check_only:
                return check
            with RepoLockService().locked(project_path, owner="repo.apply_patch", timeout_seconds=timeout_seconds):
                return self._run(["git", "apply", str(patch_path)], project, timeout_seconds)
        finally:
            patch_path.unlink(missing_ok=True)


    def apply_patch_atomic(self, project_path: str, patch: str, check_only: bool = False, timeout_seconds: int = 60) -> CommandResult:
        """Apply a patch through an isolated temporary git worktree/branch.

        The real checkout remains untouched until:
        1) patch paths are validated,
        2) the patch applies cleanly in the isolated worktree,
        3) the resulting whole-repo diff applies cleanly to the real checkout.
        """
        project = self.project(project_path)
        self._validate_patch_paths(project, patch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            return self._run_atomic_worktree_transaction(
                project=project,
                transaction_name="repo.apply_patch_atomic",
                timeout_seconds=timeout_seconds,
                check_only=check_only,
                mutate_worktree=lambda worktree: self._apply_patch_inside_worktree(worktree, patch_path, timeout_seconds),
            )
        finally:
            patch_path.unlink(missing_ok=True)

    def write_many_atomic(
        self,
        project_path: str,
        files: list[object],
        check_only: bool = False,
        timeout_seconds: int = 60,
        delete_paths: list[str] | None = None,
    ) -> CommandResult:
        """Write many files as one atomic transaction via temp worktree/branch."""
        project = self.project(project_path)
        normalized_files = self._normalise_write_many_files(project, files, action="repo.write_many_atomic")
        normalized_deletes = self._normalise_delete_many_paths(
            project, delete_paths or [], normalized_files, action="repo.write_many_atomic"
        )

        return self._run_atomic_worktree_transaction(
            project=project,
            transaction_name="repo.write_many_atomic",
            timeout_seconds=timeout_seconds,
            check_only=check_only,
            mutate_worktree=lambda worktree: self._write_many_inside_worktree_with_wiring(
                worktree, normalized_files, normalized_deletes
            ),
        )

    def write_many_fast_safe(
        self,
        project_path: str,
        files: list[object],
        check_only: bool = False,
        timeout_seconds: int = 30,
        delete_paths: list[str] | None = None,
    ) -> CommandResult:
        """Write small related files without the heavy git-worktree transaction.

        This path is intentionally still safe: it validates every path, takes the
        repository lock, backs up original file bytes, writes through temporary
        files, and rolls back every touched file if any write fails.  It is the
        right default for small JS/XML/SCSS/Python edits where full git worktree
        isolation adds latency but not meaningful extra quality.
        """
        project = self.project(project_path)
        normalized_files = self._normalise_write_many_files(project, files, action="repo.write_many_fast_safe")
        normalized_deletes = self._normalise_delete_many_paths(
            project, delete_paths or [], normalized_files, action="repo.write_many_fast_safe"
        )
        normalized_files, auto_wired = self._expand_odoo_package_writes(project, normalized_files)
        normalized_deletes = self._normalise_delete_many_paths(
            project, normalized_deletes, normalized_files, action="repo.write_many_fast_safe"
        )
        allowed, reason = self._fast_safe_write_allowed(normalized_files, normalized_deletes)
        if not allowed:
            return CommandResult(
                command=["repo", "write_many_fast_safe"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Fast-safe limits exceeded: {reason}. Use repo.write_many_smart or repo.write_many_atomic.",
            )

        parent_error = self._precheck_write_many_parents(project, normalized_files)
        if parent_error:
            return CommandResult(command=["repo", "write_many_fast_safe", "--check"], cwd=str(project), exit_code=1, stderr=parent_error)

        total_bytes = sum(len(str(item["content"]).encode("utf-8")) for item in normalized_files)
        if check_only:
            return CommandResult(
                command=["repo", "write_many_fast_safe", "--check"],
                cwd=str(project),
                exit_code=0,
                stdout=(
                    f"Fast-safe check-only passed for {len(normalized_files)} file(s), "
                    f"{total_bytes} bytes total. Real checkout unchanged.\n"
                    f"files={', '.join(str(item['relative_path']) for item in normalized_files)}\n"
                    f"delete_paths={normalized_deletes}\n"
                    f"auto_wired_init_files={auto_wired}"
                ),
            )

        backups: dict[str, bytes | None] = {}
        changed: list[str] = []
        unchanged: list[str] = []
        write_started = time.monotonic()

        with RepoLockService().locked(str(project), owner="repo.write_many_fast_safe", timeout_seconds=timeout_seconds):
            try:
                for item in normalized_files:
                    rel = str(item["relative_path"])
                    target = self._safe_join(project, rel)
                    if bool(item.get("create_dirs", True)):
                        target.parent.mkdir(parents=True, exist_ok=True)
                    elif not target.parent.exists():
                        raise RuntimeError(f"Parent directory not found: {target.parent}")
                    if target.exists() and not target.is_file():
                        raise RuntimeError(f"Target exists but is not a file: {target}")
                    backups[rel] = target.read_bytes() if target.exists() else None
                for rel in normalized_deletes:
                    target = self._safe_join(project, rel)
                    if target.exists() and not target.is_file():
                        raise RuntimeError(f"Delete target exists but is not a file: {target}")
                    backups[rel] = target.read_bytes() if target.exists() else None

                for item in normalized_files:
                    rel = str(item["relative_path"])
                    target = self._safe_join(project, rel)
                    existing_bytes = backups.get(rel)
                    new_bytes = self._resolve_write_content(existing_bytes, item, rel)
                    if existing_bytes == new_bytes:
                        unchanged.append(rel)
                        continue
                    tmp = target.with_name(f".{target.name}.tmp.{uuid4().hex[:8]}")
                    tmp.write_bytes(new_bytes)
                    tmp.replace(target)
                    changed.append(rel)
                for rel in normalized_deletes:
                    target = self._safe_join(project, rel)
                    if target.exists():
                        target.unlink()
                        changed.append(rel)
                    else:
                        unchanged.append(rel)
            except Exception as exc:  # noqa: BLE001
                rollback_errors: list[str] = []
                for rel, old_bytes in backups.items():
                    target = self._safe_join(project, rel)
                    try:
                        if old_bytes is None:
                            target.unlink(missing_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            tmp = target.with_name(f".{target.name}.rollback.{uuid4().hex[:8]}")
                            tmp.write_bytes(old_bytes)
                            tmp.replace(target)
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_errors.append(f"{rel}: {rollback_exc}")
                stderr = f"Fast-safe write failed and rollback was attempted: {exc}"
                if rollback_errors:
                    stderr += "\nRollback errors:\n" + "\n".join(rollback_errors)
                return CommandResult(command=["repo", "write_many_fast_safe"], cwd=str(project), exit_code=1, stderr=stderr[-20000:])

        elapsed_ms = int((time.monotonic() - write_started) * 1000)
        return CommandResult(
            command=["repo", "write_many_fast_safe"],
            cwd=str(project),
            exit_code=0,
            stdout=(
                "Fast-safe write applied with repo lock + backups + rollback protection.\n"
                f"changed={changed}\n"
                f"unchanged={unchanged}\n"
                f"auto_wired_init_files={auto_wired}\n"
                f"files={len(normalized_files)} deletes={len(normalized_deletes)} total_bytes={total_bytes} elapsed_ms={elapsed_ms}\n"
                "Next: run git.diff and workflow.verify_against_core on these changed files only."
            ),
        )

    def write_many_smart(
        self,
        project_path: str,
        files: list[object],
        check_only: bool = False,
        timeout_seconds: int = 60,
        strategy: str = "auto",
        delete_paths: list[str] | None = None,
    ) -> CommandResult:
        """Choose the fastest safe writer without weakening validation quality."""
        project = self.project(project_path)
        normalized_files = self._normalise_write_many_files(project, files, action="repo.write_many_smart")
        normalized_deletes = self._normalise_delete_many_paths(
            project, delete_paths or [], normalized_files, action="repo.write_many_smart"
        )
        normalized_files, _ = self._expand_odoo_package_writes(project, normalized_files)
        strategy = (strategy or "auto").strip().lower()
        if strategy not in {"auto", "fast_safe", "atomic"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="strategy must be one of: auto, fast_safe, atomic")

        if strategy == "atomic":
            return self.write_many_atomic(
                project_path, files, check_only=check_only, timeout_seconds=timeout_seconds,
                delete_paths=normalized_deletes,
            )

        allowed, reason = self._fast_safe_write_allowed(normalized_files, normalized_deletes)
        if strategy == "fast_safe":
            return self.write_many_fast_safe(
                project_path, files, check_only=check_only, timeout_seconds=min(timeout_seconds, 180),
                delete_paths=normalized_deletes,
            )

        if allowed:
            result = self.write_many_fast_safe(
                project_path, files, check_only=check_only, timeout_seconds=min(timeout_seconds, 180),
                delete_paths=normalized_deletes,
            )
            result.stdout = "SMART_STRATEGY=fast_safe\n" + result.stdout
            return result

        result = self.write_many_atomic(
            project_path, files, check_only=check_only, timeout_seconds=timeout_seconds,
            delete_paths=normalized_deletes,
        )
        result.stdout = f"SMART_STRATEGY=atomic reason={reason}\n" + result.stdout
        return result

    def _normalise_write_many_files(self, project: Path, files: list[object], action: str) -> list[dict[str, object]]:
        seen: set[str] = set()
        normalized_files: list[dict[str, object]] = []
        for item in files:
            raw_path = str(getattr(item, "relative_path", ""))
            content = str(getattr(item, "content", ""))
            create_dirs = bool(getattr(item, "create_dirs", True))
            mode = str(getattr(item, "mode", "overwrite") or "overwrite")
            force = bool(getattr(item, "force", False))
            if mode not in ("overwrite", "append"):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid mode '{mode}' for {raw_path}: must be 'overwrite' or 'append'")
            self._reject_git_internal_path(raw_path)
            resolved = self._safe_join(project, raw_path)
            rel = resolved.relative_to(project).as_posix()
            if rel in seen:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Duplicate file in {action}: {rel}")
            seen.add(rel)
            normalized_files.append({"relative_path": rel, "content": content, "create_dirs": create_dirs, "mode": mode, "force": force})
        if not normalized_files:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="files must not be empty")
        return normalized_files

    def _normalise_delete_many_paths(
        self,
        project: Path,
        delete_paths: list[str],
        files: list[dict[str, object]],
        *,
        action: str,
    ) -> list[str]:
        write_paths = {str(item["relative_path"]) for item in files}
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_path in delete_paths:
            self._reject_git_internal_path(str(raw_path))
            resolved = self._safe_join(project, str(raw_path))
            rel = resolved.relative_to(project).as_posix()
            if rel in write_paths:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Path cannot be written and deleted in the same {action}: {rel}",
                )
            if rel in seen:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Duplicate delete path in {action}: {rel}",
                )
            seen.add(rel)
            normalized.append(rel)
        return normalized

    def _resolve_write_content(self, existing_bytes: bytes | None, item: dict[str, object], rel: str) -> bytes:
        """Apply mode + shrink-guard for one file inside a multi-file write. Returns final bytes to write."""
        new_content = str(item["content"])
        mode = str(item.get("mode", "overwrite"))
        force = bool(item.get("force", False))
        if mode == "append":
            old_text = existing_bytes.decode("utf-8") if existing_bytes else ""
            return (old_text + new_content).encode("utf-8")
        # overwrite mode: shrink guard against accidental data loss
        if existing_bytes is not None:
            old_len = len(existing_bytes)
            new_len = len(new_content.encode("utf-8"))
            if not force and old_len > 200 and new_len < old_len * self._OVERWRITE_SHRINK_GUARD_RATIO:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "overwrite_shrink_guard",
                        "message": (
                            f"Refusing overwrite of {rel}: new content ({new_len} bytes) is far smaller than "
                            f"the existing file ({old_len} bytes). If you meant to append, resend with mode='append'. "
                            f"If this overwrite is intentional, resend with force=true."
                        ),
                    },
                )
        return new_content.encode("utf-8")

    def _fast_safe_write_allowed(
        self,
        files: list[dict[str, object]],
        delete_paths: list[str] | None = None,
    ) -> tuple[bool, str]:
        max_files = max(1, int(getattr(settings, "fast_write_max_files", 8)))
        max_each = max(1, int(getattr(settings, "fast_write_max_bytes_each", 300_000)))
        max_total = max(1, int(getattr(settings, "fast_write_max_total_bytes", 750_000)))
        mutation_count = len(files) + len(delete_paths or [])
        if mutation_count > max_files:
            return False, f"mutation count {mutation_count} > max {max_files}"
        total = 0
        for item in files:
            rel = str(item["relative_path"])
            size = len(str(item["content"]).encode("utf-8"))
            if size > max_each:
                return False, f"{rel} size {size} bytes > max_each {max_each}"
            total += size
        if total > max_total:
            return False, f"total size {total} bytes > max_total {max_total}"
        return True, "within fast-safe limits"

    def _precheck_write_many_parents(self, project: Path, files: list[dict[str, object]]) -> str:
        for item in files:
            rel = str(item["relative_path"])
            target = self._safe_join(project, rel)
            if not bool(item.get("create_dirs", True)) and not target.parent.exists():
                return f"Parent directory not found: {target.parent}"
            if target.exists() and not target.is_file():
                return f"Target exists but is not a file: {target}"
        return ""

    def _apply_patch_inside_worktree(self, worktree: Path, patch_path: Path, timeout_seconds: int) -> CommandResult:
        check = self._run(["git", "apply", "--check", str(patch_path)], worktree, timeout_seconds)
        if check.exit_code != 0:
            return check
        applied = self._run(["git", "apply", str(patch_path)], worktree, timeout_seconds)
        if applied.exit_code != 0:
            return applied
        patch_text = patch_path.read_text(encoding="utf-8")
        python_files: list[dict[str, object]] = []
        for rel in self._patch_target_paths(patch_text):
            target = self._safe_join(worktree, rel)
            if target.suffix != ".py" or not target.is_file():
                continue
            python_files.append({
                "relative_path": rel,
                "content": target.read_text(encoding="utf-8"),
                "create_dirs": True,
                "mode": "overwrite",
                "force": False,
            })
        if not python_files:
            return applied
        try:
            expanded, auto_wired = OdooPackageWiringService().expand(worktree, python_files)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(
                command=["git", "apply", "+", "odoo-package-auto-wire"],
                cwd=str(worktree),
                exit_code=1,
                stderr=f"Odoo Python package auto-wiring failed: {exc}",
            )
        wired = self._write_many_inside_worktree(worktree, expanded)
        if wired.exit_code != 0:
            return wired
        return applied.model_copy(update={
            "command": ["git", "apply", "+", "odoo-package-auto-wire"],
            "stdout": (applied.stdout + f"\nauto_wired_init_files={auto_wired}").strip(),
        })

    @staticmethod
    def _patch_target_paths(patch: str) -> list[str]:
        paths: list[str] = []
        for line in patch.splitlines():
            if not line.startswith("+++ "):
                continue
            raw = line[4:].split("\t", 1)[0].strip().strip('"')
            if raw == "/dev/null":
                continue
            if raw.startswith("b/"):
                raw = raw[2:]
            if raw and raw not in paths:
                paths.append(raw)
        return paths

    def _write_many_inside_worktree(self, worktree: Path, files: list[dict[str, object]]) -> CommandResult:
        changed: list[str] = []
        for item in files:
            rel = str(item["relative_path"])
            target = self._safe_join(worktree, rel)
            if bool(item.get("create_dirs", True)):
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.exists():
                return CommandResult(
                    command=["repo", "write_many_atomic"],
                    cwd=str(worktree),
                    exit_code=1,
                    stderr=f"Parent directory not found: {target.parent}",
                )
            existing_bytes = target.read_bytes() if target.exists() else None
            try:
                new_bytes = self._resolve_write_content(existing_bytes, item, rel)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                return CommandResult(command=["repo", "write_many_atomic"], cwd=str(worktree), exit_code=1, stderr=detail)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(new_bytes)
            tmp.replace(target)
            changed.append(rel)
        return CommandResult(command=["repo", "write_many_atomic"], cwd=str(worktree), exit_code=0, stdout="Prepared files: " + ", ".join(changed))

    def _write_many_inside_worktree_with_wiring(
        self,
        worktree: Path,
        files: list[dict[str, object]],
        delete_paths: list[str] | None = None,
    ) -> CommandResult:
        try:
            expanded, auto_wired = OdooPackageWiringService().expand(worktree, files)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(
                command=["repo", "write_many_atomic", "+", "odoo-package-auto-wire"],
                cwd=str(worktree),
                exit_code=1,
                stderr=f"Odoo Python package auto-wiring failed: {exc}",
            )
        result = self._write_many_inside_worktree(worktree, expanded)
        if result.exit_code == 0:
            write_paths = {str(item["relative_path"]) for item in expanded}
            collision = next((rel for rel in delete_paths or [] if rel in write_paths), None)
            if collision:
                return CommandResult(
                    command=["repo", "write_many_atomic"], cwd=str(worktree), exit_code=1,
                    stderr=f"Path cannot be written and deleted in the same repo.write_many_atomic: {collision}",
                )
            deleted: list[str] = []
            for rel in delete_paths or []:
                target = self._safe_join(worktree, rel)
                if target.exists() and not target.is_file():
                    return CommandResult(
                        command=["repo", "write_many_atomic"], cwd=str(worktree), exit_code=1,
                        stderr=f"Delete target exists but is not a file: {target}",
                    )
                if target.exists():
                    target.unlink()
                    deleted.append(rel)
            result.stdout = result.stdout + f"\nauto_wired_init_files={auto_wired}"
            result.stdout = result.stdout + f"\ndeleted_files={deleted}"
        return result

    @staticmethod
    def _expand_odoo_package_writes(project: Path, files: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[str]]:
        try:
            return OdooPackageWiringService().expand(project, files)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Odoo Python package auto-wiring failed: {exc}",
            ) from exc

    def _run_atomic_worktree_transaction(self, project: Path, transaction_name: str, timeout_seconds: int, check_only: bool, mutate_worktree) -> CommandResult:
        if not (project / ".git").exists():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Atomic transactions require a git repository")
        env = self._git_env()
        temp_root = Path(tempfile.mkdtemp(prefix="repo_atomic_"))
        worktree = temp_root / "worktree"
        branch = f"atomic-edit-{uuid4().hex[:12]}"
        steps: list[CommandResult] = []

        def combined(exit_code: int, stdout: str = "", stderr: str = "", timed_out: bool = False) -> CommandResult:
            step_text = []
            for step in steps:
                label = " ".join(step.command)
                if step.exit_code != 0 or step.stdout or step.stderr:
                    step_text.append(f"$ {label}\nexit={step.exit_code}\nstdout={step.stdout}\nstderr={step.stderr}".strip())
            if stdout:
                step_text.append(stdout)
            return CommandResult(
                command=["repo", transaction_name],
                cwd=str(project),
                exit_code=exit_code,
                stdout="\n\n".join(step_text)[-20000:],
                stderr=stderr[-20000:],
                timed_out=timed_out,
            )

        try:
            add = self._run(["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"], project, timeout_seconds, env=env)
            steps.append(add)
            if add.exit_code != 0:
                return combined(add.exit_code, stderr=add.stderr or add.stdout, timed_out=add.timed_out)

            mutate = mutate_worktree(worktree)
            steps.append(mutate)
            if mutate.exit_code != 0:
                return combined(mutate.exit_code, stderr=mutate.stderr or mutate.stdout, timed_out=mutate.timed_out)

            # Make new files visible in git diff without staging content.
            add_intent = self._run(["git", "add", "-N", "--", "."], worktree, timeout_seconds, env=env)
            steps.append(add_intent)
            if add_intent.exit_code != 0:
                return combined(add_intent.exit_code, stderr=add_intent.stderr or add_intent.stdout, timed_out=add_intent.timed_out)

            diff = self._run(["git", "diff", "--binary", "--no-ext-diff"], worktree, timeout_seconds, env=env)
            steps.append(diff)
            if diff.exit_code != 0:
                return combined(diff.exit_code, stderr=diff.stderr or diff.stdout, timed_out=diff.timed_out)
            if not diff.stdout.strip():
                return combined(0, stdout="No changes produced by atomic transaction.")

            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
                handle.write(diff.stdout)
                final_patch = Path(handle.name)
            try:
                final_check = self._run(["git", "apply", "--check", str(final_patch)], project, timeout_seconds, env=env)
                steps.append(final_check)
                if final_check.exit_code != 0 or check_only:
                    return combined(final_check.exit_code, stdout="Atomic check-only completed; real checkout unchanged." if check_only and final_check.exit_code == 0 else "", stderr=final_check.stderr or final_check.stdout, timed_out=final_check.timed_out)

                with RepoLockService().locked(str(project), owner=transaction_name, timeout_seconds=timeout_seconds):
                    final_apply = self._run(["git", "apply", str(final_patch)], project, timeout_seconds, env=env)
                steps.append(final_apply)
                return combined(final_apply.exit_code, stdout="Atomic transaction applied to real checkout." if final_apply.exit_code == 0 else "", stderr=final_apply.stderr or final_apply.stdout, timed_out=final_apply.timed_out)
            finally:
                final_patch.unlink(missing_ok=True)
        finally:
            remove = self._run(["git", "worktree", "remove", "--force", str(worktree)], project, timeout_seconds, env=env) if worktree.exists() else None
            if remove:
                steps.append(remove)
            delete_branch = self._run(["git", "branch", "-D", branch], project, timeout_seconds, env=env)
            # Do not fail a successful transaction only because cleanup sees no branch.
            if delete_branch.exit_code != 0 and "not found" not in (delete_branch.stderr + delete_branch.stdout).lower():
                steps.append(delete_branch)
            shutil.rmtree(temp_root, ignore_errors=True)

    def clean_pyc(self, project_path: str, relative_path: str = ".") -> RepoCleanPycResult:
        root = self.target(project_path, relative_path)
        if not root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {root}")
        removed_files: list[str] = []
        removed_dirs: list[str] = []
        base = self.project(project_path)
        for pyc in root.rglob("*.pyc") if root.is_dir() else []:
            if pyc.is_file():
                removed_files.append(pyc.relative_to(base).as_posix())
                pyc.unlink(missing_ok=True)
        for cache_dir in sorted((p for p in root.rglob("__pycache__") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            removed_dirs.append(cache_dir.relative_to(base).as_posix())
            shutil.rmtree(cache_dir, ignore_errors=True)
        return RepoCleanPycResult(project_path=str(base), scanned_path=str(root), removed_files=removed_files, removed_dirs=removed_dirs)



    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if settings.git_ssh_key_path:
            env["GIT_SSH_COMMAND"] = f"ssh -i {settings.git_ssh_key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        return env

    def _repo_name_from_url(self, repo_url: str) -> str:
        cleaned = repo_url.rstrip("/")
        name = cleaned.rsplit("/", 1)[-1]
        if ":" in name and not name.endswith(".git"):
            name = name.rsplit(":", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name or "repo"

    def _remote_matches(self, configured: str, expected: str) -> bool:
        if configured == expected:
            return True
        return self._repo_name_from_url(configured) == self._repo_name_from_url(expected)

    def _safe_repo_folder_name(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9._-]+", value)) and value not in {".", ".."}

    def _safe_remote_name(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value)) and not value.startswith(("-", "/")) and ".." not in value

    def _safe_branch_name(self, value: str) -> bool:
        if value.startswith(("-", "/")) or value.endswith("/") or ".." in value or "@{" in value:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value))

    def _read_manifest_metadata(self, manifest: Path) -> dict:
        try:
            raw = manifest.read_text(encoding="utf-8")
            data = ast.literal_eval(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _reject_git_internal_path(self, relative_path: str) -> None:
        normalized = relative_path.replace("\\", "/").strip("/")
        if normalized == ".git" or normalized.startswith(".git/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Writing inside .git is blocked. Use dedicated git tools.",
            )

    def _safe_join(self, project: Path, relative_path: str) -> Path:
        if not relative_path:
            relative_path = "."
        rel = Path(relative_path)
        if rel.is_absolute() or "\x00" in relative_path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relative_path must be a relative path")
        resolved = (project / rel).resolve()
        if not (resolved == project or project in resolved.parents):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Path escapes project root: {relative_path}")
        return resolved

    def _validate_patch_paths(self, project: Path, patch: str) -> None:
        if not patch.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Patch is empty")

        def safe_patch_path(raw_path: str) -> bool:
            # Strip optional tab timestamp part from ---/+++ headers.
            candidate = raw_path.split("\t", 1)[0].strip().strip('"')
            if candidate == "/dev/null":
                return True
            if "\x00" in candidate or candidate.startswith(("/", "~")):
                return False
            # Standard git patches use a/ and b/ prefixes. We reject unusual
            # absolute/implicit targets because this connector should only patch
            # files inside project_path.
            if candidate.startswith(("a/", "b/")):
                candidate = candidate[2:]
            parts = [part for part in candidate.replace("\\", "/").split("/") if part]
            if not parts:
                return False
            if parts[0] == ".git":
                return False
            return all(part not in {".", ".."} for part in parts)

        for line in patch.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) < 4 or not safe_patch_path(parts[2]) or not safe_patch_path(parts[3]):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe path in patch header: {line[:180]}")
            elif line.startswith(("--- ", "+++ ")):
                raw_path = line[4:].split(" ", 1)[0]
                if not safe_patch_path(raw_path):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe path in patch header: {line[:180]}")

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str] | None = None) -> CommandResult:
        try:
            completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False, env=env)
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=mask_repo_url(completed.stdout[-20000:]),
                stderr=mask_repo_url(completed.stderr[-20000:]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else ""
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=124,
                stdout=mask_repo_url(stdout),
                stderr=mask_repo_url(stderr),
                timed_out=True,
            )
