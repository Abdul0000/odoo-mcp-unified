from __future__ import annotations

import json

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.routers import git
from odoo_agent_server.schemas import CommandResult, GitPushRequest
from odoo_agent_server.services.store_service import JsonStore


class _FakeApproval:
    def ensure_allowed(self, *args, **kwargs):
        return None

    def finalize_command(self, *args, **kwargs):
        return None


class _FakeGit:
    def __init__(self, changed_stdout: str):
        self.changed_stdout = changed_stdout

    def current_branch(self, project_path: str) -> CommandResult:
        return CommandResult(command=["git", "branch"], cwd=project_path, exit_code=0, stdout="dev\n")

    def head_changed_files(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout=self.changed_stdout)

    def push(self, project_path: str, remote: str = "origin", branch: str | None = None, set_upstream: bool = True, timeout_seconds: int = 120) -> CommandResult:
        return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\n")

    def head_sha(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")


class _FakeOdooSh:
    calls: list[tuple[str, str, int | None]] = []

    def poll_deployed_commit(self, expected_sha: str, timeout_seconds: int = 300, interval_seconds: float = 10.0, progress_cb=None) -> CommandResult:
        self.calls.append(("poll_deployed_commit", expected_sha, timeout_seconds))
        payload = {"deployed": True, "expected_sha": expected_sha, "attempts": 1, "elapsed_seconds": 0.0}
        if progress_cb:
            progress_cb({"attempt": 1, "deployed": True, "expected_sha": expected_sha})
        return CommandResult(command=["odoosh.check_commit_poll", expected_sha], cwd="odoosh:ssh", exit_code=0, stdout=json.dumps(payload))

    def module_deploy_ssh(self, module_name: str, timeout_seconds: int) -> CommandResult:
        self.calls.append(("module_deploy_ssh", module_name, timeout_seconds))
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="deploy ok")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "protected_branches_raw", "")
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "auto_upgrade_on_push", True)
    monkeypatch.setattr(git, "ApprovalService", lambda: _FakeApproval())
    monkeypatch.setattr(git, "submit_background", lambda callback: callback())
    _FakeOdooSh.calls = []
    yield


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "custom_addons" / "x_module" / "models").mkdir(parents=True)
    (root / "custom_addons" / "x_module" / "__manifest__.py").write_text("{'name': 'X Module'}\n", encoding="utf-8")
    (root / "custom_addons" / "x_module" / "models" / "a.py").write_text("# changed\n", encoding="utf-8")
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    return root


def test_git_push_touching_module_auto_triggers_odoosh_upgrade_job(project, monkeypatch):
    monkeypatch.setattr(git, "GitService", lambda: _FakeGit("custom_addons/x_module/models/a.py\n"))
    monkeypatch.setattr(git, "OdooShService", _FakeOdooSh)

    result = git.git_push(GitPushRequest(project_path=str(project), branch="dev"))

    assert result.exit_code == 2
    assert "Detected module x_module in this push" in result.stdout
    assert "Odoo.sh upgrade job started" in result.stdout
    assert "Poll with jobs.get" in result.stdout
    marker = "AUTO_UPGRADE_ON_PUSH="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    follow_up = json.loads(payload_line.removeprefix(marker))
    assert follow_up["module_name"] == "x_module"
    assert follow_up["follow_up_job_id"]

    jobs = JsonStore().list_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == follow_up["follow_up_job_id"]
    assert jobs[0].job_type == "post_push_auto_upgrade"
    assert jobs[0].request["module_name"] == "x_module"
    assert jobs[0].request["expected_sha"] == "abc1234"
    assert _FakeOdooSh.calls == [
        ("poll_deployed_commit", "abc1234", 300),
        ("module_deploy_ssh", "x_module", 120),
    ]

    state = JsonStore().load_session_state()
    assert state["last_git_push"]["follow_up_job_id"] == follow_up["follow_up_job_id"]
    assert state["last_git_push"]["auto_upgrade_module_name"] == "x_module"
    assert state["safe_deploy_status"] == "post_push_auto_upgrade_passed"


def test_git_push_readme_only_does_not_auto_trigger_odoosh_upgrade(project, monkeypatch):
    monkeypatch.setattr(git, "GitService", lambda: _FakeGit("README.md\n"))
    monkeypatch.setattr(git, "OdooShService", _FakeOdooSh)

    result = git.git_push(GitPushRequest(project_path=str(project), branch="dev"))

    assert result.exit_code == 0
    assert "AUTO_UPGRADE_ON_PUSH=" not in result.stdout
    assert "auto-triggered Odoo.sh upgrade" not in result.stdout
    assert JsonStore().list_jobs() == []
    assert _FakeOdooSh.calls == []
    state = JsonStore().load_session_state()
    assert state["last_git_push"]["module_names"] == []
    assert state["safe_deploy_status"] == "manual_push_pending_deploy_verification"
