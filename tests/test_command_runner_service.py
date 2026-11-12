from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.services.command_runner_service import CommandRunnerService


def _repo(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    return project


def test_git_status_runs_in_repo_without_shell(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace)

    result = CommandRunnerService().run(str(project), "git_status")

    assert result.exit_code == 0
    assert "sample.py" in result.stdout
    assert result.command == ["git", "status", "--short", "--branch"]


def test_rejects_unknown_commands(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace)

    with pytest.raises(HTTPException) as exc:
        CommandRunnerService().run(str(project), "bash -c id")

    assert exc.value.status_code == 400


def test_rejects_paths_outside_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(outside)

    with pytest.raises(HTTPException) as exc:
        CommandRunnerService().run(str(project), "git_status")

    assert exc.value.status_code == 403


def test_rejects_relative_path_escape(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace)

    with pytest.raises(HTTPException):
        CommandRunnerService().run(str(project), "python_compile", relative_path="../outside.py")
