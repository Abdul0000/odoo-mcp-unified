"""Allowlisted development commands for a selected local repository.

This is deliberately not a shell. Every command is built as an argv list,
runs as the unprivileged MCP service user, and is constrained to an approved
project checkout beneath WORKSPACE_ROOT.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult
from .path_service import resolve_allowed_path
from .repo_service import RepoService

_ALLOWED_COMMANDS = {"git_status", "git_diff", "python_compile", "pytest", "ruff_check", "list_files"}
_MAX_OUTPUT = 12_000


class CommandRunnerService:
    def run(
        self,
        project_path: str,
        command: str,
        *,
        relative_path: str = ".",
        pattern: str | None = None,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        if command not in _ALLOWED_COMMANDS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported command. Allowed: {', '.join(sorted(_ALLOWED_COMMANDS))}")
        if not 1 <= timeout_seconds <= 180:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="timeout_seconds must be between 1 and 180")
        workspace = resolve_allowed_path(settings.workspace_root)
        project = resolve_allowed_path(project_path)
        if not project.is_dir() or not (project / ".git").exists():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_path must be an existing Git checkout")
        if not (project == workspace or workspace in project.parents):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Remote commands are restricted to WORKSPACE_ROOT checkouts")
        if project.resolve() != project:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project path must be canonical")
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", relative_path) or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relative_path must be a safe project-relative path")
        target = (project / relative_path).resolve()
        if not (target == project or project in target.parents):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="relative_path escapes project checkout")
        if target.is_symlink():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Symlink command targets are blocked")

        argv: list[str]
        cwd = project
        if command == "git_status":
            argv = ["git", "status", "--short", "--branch"]
        elif command == "git_diff":
            argv = ["git", "diff", "--no-ext-diff", "--", relative_path]
        elif command == "python_compile":
            if target.is_dir():
                argv = ["python", "-m", "compileall", "-q", str(target)]
            elif target.suffix == ".py" and target.is_file():
                argv = ["python", "-m", "py_compile", str(target)]
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="python_compile requires a Python file or directory")
        elif command == "pytest":
            if pattern:
                if len(pattern) > 120 or not re.fullmatch(r"[A-Za-z0-9_./*-]+", pattern) or ".." in Path(pattern).parts:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pytest pattern contains unsupported characters")
                selected = (project / pattern).resolve()
                if not (selected == project or project in selected.parents):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="pytest pattern escapes project checkout")
                argv = ["python", "-m", "pytest", "-q", pattern]
            else:
                argv = ["python", "-m", "pytest", "-q"]
        elif command == "ruff_check":
            argv = ["ruff", "check", "--output-format", "concise", relative_path]
        else:
            if not target.exists():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target path not found")
            from ..schemas import FileListItem
            items = RepoService().list_files(str(project), relative_path, max_depth=4, max_items=500)
            lines = [f"{item.kind}\t{item.path}\t{item.size if item.size is not None else ''}" for item in items]
            return CommandResult(command=["list_files", relative_path], cwd=str(project), exit_code=0, stdout="\n".join(lines)[:_MAX_OUTPUT])

        return RepoService()._run(argv, cwd, timeout_seconds, env=RepoService()._git_env()) if command.startswith("git_") else RepoService()._run(argv, cwd, timeout_seconds)
