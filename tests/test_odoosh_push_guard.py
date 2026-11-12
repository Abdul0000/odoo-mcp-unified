from __future__ import annotations

import json

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import CommandResult, JobRecord, JobStatus, OdooModuleRequest, OdooShModuleUpdateRequest
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers import odoosh


class _FakeApproval:
    def ensure_allowed(self, *args, **kwargs):
        return None

    def finalize_command(self, *args, **kwargs):
        return None

    def mark_failed(self, *args, **kwargs):
        return None


class _FakeGit:
    def __init__(self, local_sha: str = "local123", remote_sha: str = "remote999") -> None:
        self.local_sha = local_sha
        self.remote_sha = remote_sha

    def head_sha(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout=f"{self.local_sha}\n")

    def remote_head_sha(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 30) -> CommandResult:
        return CommandResult(command=["git", "remote-head-sha", remote, branch or "HEAD"], cwd=project_path, exit_code=0, stdout=f"{self.remote_sha}\n")


class _FakeOdooSh:
    def __init__(self, calls: dict[str, int]) -> None:
        self.calls = calls

    def wait_for_commit(self, expected_sha: str, timeout_seconds: int, interval_seconds=None) -> CommandResult:
        self.calls["wait_for_commit"] = self.calls.get("wait_for_commit", 0) + 1
        return CommandResult(command=["odoosh", "wait-for-commit", expected_sha], cwd="odoosh:ssh", exit_code=0, stdout=json.dumps({"deployed": True, "expected_sha": expected_sha}))

    def module_deploy_ssh(self, module_name: str, timeout_seconds: int) -> CommandResult:
        self.calls["module_deploy_ssh"] = self.calls.get("module_deploy_ssh", 0) + 1
        return CommandResult(command=["odoo-bin", "-u", module_name], cwd="odoosh:ssh", exit_code=0, stdout="deploy ok")

    def module_update(self, module_name: str, timeout_seconds: int) -> CommandResult:
        self.calls["module_update"] = self.calls.get("module_update", 0) + 1
        return CommandResult(command=["odoo-bin", "-u", module_name], cwd="odoosh:ssh", exit_code=0, stdout="update ok")

    def module_install_ssh(self, module_name: str, timeout_seconds: int) -> CommandResult:
        self.calls["module_install_ssh"] = self.calls.get("module_install_ssh", 0) + 1
        return CommandResult(command=["odoo-bin", "-i", module_name], cwd="odoosh:ssh", exit_code=0, stdout="install ok")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(odoosh, "ApprovalService", lambda: _FakeApproval())
    yield


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "x_module").mkdir(parents=True)
    return root


def _run_background_inline(monkeypatch):
    monkeypatch.setattr(odoosh, "submit_background", lambda callback: callback())


@pytest.mark.parametrize(
    "tool_name,request_factory,call_function,service_call",
    [
        (
            "odoosh.module_deploy_ssh",
            lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True),
            odoosh.odoosh_module_deploy_ssh,
            "module_deploy_ssh",
        ),
        (
            "odoosh.module_update",
            lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True),
            odoosh.odoosh_module_update,
            "module_update",
        ),
        (
            "odoosh.module_install_ssh",
            lambda project: OdooModuleRequest(module_name="x_module", project_path=str(project), wait_for_result=True),
            odoosh.odoosh_module_install_ssh,
            "module_install_ssh",
        ),
    ],
)
def test_manual_odoosh_tools_refuse_when_local_head_is_not_pushed(tool_name, request_factory, call_function, service_call, project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(odoosh, "GitService", lambda: _FakeGit(local_sha="local111", remote_sha="remote222"))
    monkeypatch.setattr(odoosh, "OdooShService", lambda: _FakeOdooSh(calls))

    result = call_function(request_factory(project))

    assert result.exit_code == 2
    assert "Refusing to deploy: local commits are not pushed to remote" in result.stderr
    assert "Local HEAD=local111" in result.stderr
    assert "Remote HEAD=remote222" in result.stderr
    assert calls.get(service_call, 0) == 0
    assert calls.get("wait_for_commit", 0) == 0


@pytest.mark.parametrize(
    "request_factory,call_function,service_call",
    [
        (lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_deploy_ssh, "module_deploy_ssh"),
        (lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_update, "module_update"),
        (lambda project: OdooModuleRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_install_ssh, "module_install_ssh"),
    ],
)
def test_manual_odoosh_tools_allow_when_local_head_matches_remote(request_factory, call_function, service_call, project, monkeypatch):
    _run_background_inline(monkeypatch)
    calls: dict[str, int] = {}
    monkeypatch.setattr(odoosh, "GitService", lambda: _FakeGit(local_sha="sameabc", remote_sha="sameabc"))
    monkeypatch.setattr(odoosh, "OdooShService", lambda: _FakeOdooSh(calls))

    result = call_function(request_factory(project))

    assert result.exit_code == 0
    assert calls == {"wait_for_commit": 1, service_call: 1}


@pytest.mark.parametrize(
    "request_factory,call_function,service_call",
    [
        (lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_deploy_ssh, "module_deploy_ssh"),
        (lambda project: OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_update, "module_update"),
        (lambda project: OdooModuleRequest(module_name="x_module", project_path=str(project), wait_for_result=True), odoosh.odoosh_module_install_ssh, "module_install_ssh"),
    ],
)
def test_manual_odoosh_tools_block_after_safe_deploy_failed_before_push(request_factory, call_function, service_call, project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(odoosh, "GitService", lambda: _FakeGit(local_sha="sameabc", remote_sha="sameabc"))
    monkeypatch.setattr(odoosh, "OdooShService", lambda: _FakeOdooSh(calls))
    JsonStore().create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={"project_path": str(project), "module_name": "x_module"},
        result={"failed_step": "git_commit", "module_name": "x_module"},
    ))

    result = call_function(request_factory(project))

    assert result.exit_code == 2
    assert "failed before code was pushed" in result.stderr
    assert "failed_step=git_commit" in result.stderr
    assert calls.get(service_call, 0) == 0
    assert calls.get("wait_for_commit", 0) == 0


def test_manual_odoosh_deploy_still_blocks_resumable_after_odoosh_check_commit_timeout(project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(odoosh, "GitService", lambda: _FakeGit(local_sha="sameabc", remote_sha="sameabc"))
    monkeypatch.setattr(odoosh, "OdooShService", lambda: _FakeOdooSh(calls))
    job = JsonStore().create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={"project_path": str(project), "module_name": "x_module"},
        result={"status": "deploy_failed_or_timeout", "failed_step": "odoosh_check_commit", "module_name": "x_module"},
    ))

    result = odoosh.odoosh_module_deploy_ssh(OdooShModuleUpdateRequest(module_name="x_module", project_path=str(project), wait_for_result=True))

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "safe_deploy_active"
    assert payload["job_id"] == job.id
    assert payload["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert calls == {}
