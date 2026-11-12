from __future__ import annotations

from tests.conftest import patch_module_clock
from odoo_agent_server.services import store_service
import json

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.routers import git
from odoo_agent_server.schemas import CommandResult, GitPushRequest, JobStatus
from odoo_agent_server.services import odoosh_service
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


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _BaseFakeOdooSh(odoosh_service.OdooShService):
    check_calls: list[str] = []
    deploy_calls: list[tuple[str, int]] = []

    @classmethod
    def reset(cls) -> None:
        cls.check_calls = []
        cls.deploy_calls = []

    def module_deploy_ssh(self, module_name: str, timeout_seconds: int) -> CommandResult:
        type(self).deploy_calls.append((module_name, timeout_seconds))
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="deploy ok")


class _DeploysOnThirdPoll(_BaseFakeOdooSh):
    def check_commit_deployed(self, expected_sha: str) -> CommandResult:
        type(self).check_calls.append(expected_sha)
        deployed = len(type(self).check_calls) >= 3
        return CommandResult(
            command=["odoosh.check_commit", expected_sha],
            cwd="odoosh:ssh",
            exit_code=0 if deployed else 1,
            stdout=json.dumps({"deployed": deployed, "expected_sha": expected_sha, "current_sha": expected_sha if deployed else "oldsha"}),
        )


class _NeverDeploys(_BaseFakeOdooSh):
    def check_commit_deployed(self, expected_sha: str) -> CommandResult:
        type(self).check_calls.append(expected_sha)
        return CommandResult(
            command=["odoosh.check_commit", expected_sha],
            cwd="odoosh:ssh",
            exit_code=1,
            stdout=json.dumps({"deployed": False, "expected_sha": expected_sha, "current_sha": "oldsha"}),
        )


@pytest.fixture(autouse=True)
def _isolated_state_and_sync_background(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "protected_branches_raw", "")
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "auto_upgrade_on_push", True)
    monkeypatch.setattr(git, "ApprovalService", lambda: _FakeApproval())
    monkeypatch.setattr(git, "submit_background", lambda callback: callback())
    _BaseFakeOdooSh.reset()
    yield


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "custom_addons" / "x_module" / "models").mkdir(parents=True)
    (root / "custom_addons" / "x_module" / "__manifest__.py").write_text("{'name': 'X Module'}\n", encoding="utf-8")
    (root / "custom_addons" / "x_module" / "models" / "a.py").write_text("# changed\n", encoding="utf-8")
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    return root


def _run_module_push(project, monkeypatch, fake_odoosh_cls):
    fake_clock = _FakeClock()
    patch_module_clock(monkeypatch, fake_clock, odoosh_service, store_service, git)
    monkeypatch.setattr(git, "GitService", lambda: _FakeGit("custom_addons/x_module/models/a.py\n"))
    monkeypatch.setattr(git, "OdooShService", fake_odoosh_cls)
    result = git.git_push(GitPushRequest(project_path=str(project), branch="dev"))
    marker = "AUTO_UPGRADE_ON_PUSH="
    follow_up = json.loads(next(line for line in result.stdout.splitlines() if line.startswith(marker)).removeprefix(marker))
    job = JsonStore().get_job(follow_up["follow_up_job_id"])
    return result, job, fake_clock


def test_post_push_auto_upgrade_polls_until_pushed_commit_is_deployed_then_upgrades(project, monkeypatch):
    result, job, fake_clock = _run_module_push(project, monkeypatch, _DeploysOnThirdPoll)

    assert result.exit_code == 2
    assert "Detected module x_module in this push" in result.stdout
    assert job.job_type == "post_push_auto_upgrade"
    assert job.status == JobStatus.passed
    assert job.request["expected_sha"] == "abc1234"
    assert job.request["deploy_poll_interval_seconds"] == 3.0
    assert job.request["deploy_timeout_seconds"] == 300
    assert job.result["status"] == "passed"
    assert job.result["poll_attempts"] == 3
    assert job.result["poll_elapsed_seconds"] == 10.0
    assert len([step for step in job.steps if step.name == "odoosh_check_commit_poll"]) == 3
    assert [step.status for step in job.steps if step.name == "odoosh_check_commit_poll"] == ["running", "running", "passed"]
    assert _DeploysOnThirdPoll.check_calls == ["abc1234", "abc1234", "abc1234"]
    assert _DeploysOnThirdPoll.deploy_calls == [("x_module", 120)]
    assert fake_clock.sleeps == [5.0, 5.0]


@pytest.mark.slow
def test_post_push_auto_upgrade_times_out_without_running_module_upgrade(project, monkeypatch):
    result, job, fake_clock = _run_module_push(project, monkeypatch, _NeverDeploys)

    assert result.exit_code == 2
    assert job.job_type == "post_push_auto_upgrade"
    assert job.status == JobStatus.failed
    assert job.result["status"] == "deploy_failed_or_timeout"
    assert job.result["sha"] == "abc1234"
    assert job.result["poll_timeout_seconds"] == 300
    assert job.result["poll_interval_seconds"] == 3.0
    assert job.result["poll_elapsed_seconds"] == 300.0
    assert job.result["poll_attempts"] == 25
    assert job.result["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert _NeverDeploys.deploy_calls == []
    assert len(_NeverDeploys.check_calls) == 25
    assert sum(fake_clock.sleeps) == 300.0
