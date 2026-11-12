from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.services.command_runner_service import CommandRunnerService


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    (path / "sample.py").write_text("value = 1\\n", encoding="utf-8")
    return path


def test_git_status_runs_in_workspace_checkout(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace / "repo")
    result = CommandRunnerService().run(str(project), "git_status")
    assert result.exit_code == 0
    assert "sample.py" in result.stdout


def test_rejects_shell_command_strings(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace / "repo")
    with pytest.raises(HTTPException) as exc:
        CommandRunnerService().run(str(project), "bash -c id")
    assert exc.value.status_code == 400


def test_rejects_checkouts_outside_workspace_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(tmp_path / "outside")
    with pytest.raises(HTTPException) as exc:
        CommandRunnerService().run(str(project), "git_status")
    assert exc.value.status_code == 403


def test_rejects_relative_path_traversal(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_root", str(workspace))
    project = _repo(workspace / "repo")
    with pytest.raises(HTTPException):
        CommandRunnerService().run(str(project), "python_compile", relative_path="../secrets")
