from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import (
    CodingTaskPlanRequest,
    CommandResult,
    JobRecord,
    JobStatus,
    OdooResult,
    SafeDeployModuleRequest,
)
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers import workflow
from odoo_agent_server.services import checkout_guard_service


class PassingSyntaxVerifier:
    def verify(self, req):
        class Result:
            push_blocked = False

            def model_dump(self, mode="json"):
                return {"push_blocked": False, "written_files": req.written_files}

        return Result()


class PassingOdooSh:
    def check_commit_deployed(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def verify_user_file_hashes(self, expected_hashes):
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

    def verify_module_manifest(self, module_name):
        return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")


class PassingOdooService:
    def module_status(self, module_name):
        return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
        return OdooResult(status="ok", result={"module": module_name})


class SafeDeployGit:
    def __init__(self, project: Path, *, create_on_sync: bool, fail_if_sync_called: bool = False) -> None:
        self.project = project
        self.create_on_sync = create_on_sync
        self.fail_if_sync_called = fail_if_sync_called
        self.calls: list[str] = []
        self.sha = "abc1234"

    def fetch_reset_hard(self, project_path, remote="origin", branch=None, timeout_seconds=120):
        self.calls.append("fetch_reset_hard")
        if self.fail_if_sync_called:
            raise AssertionError("fetch_reset_hard should not be called when the module already exists")
        if self.create_on_sync:
            module = Path(project_path) / "x_module"
            module.mkdir(parents=True, exist_ok=True)
            (module / "__manifest__.py").write_text("{'name': 'X Module', 'installable': True}\n", encoding="utf-8")
        return CommandResult(command=["git", "fetch-reset-hard"], cwd=project_path, exit_code=0, stdout="HEAD is now at abc1234\n")

    def status(self, project_path):
        self.calls.append("status")
        return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/__manifest__.py\n")

    def diff(self, project_path, staged=False, timeout_seconds=30):
        self.calls.append("diff")
        return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

    def configure_identity(self, project_path):
        self.calls.append("configure_identity")
        return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

    def validate_target(self, project_path, branch=None, remote="origin"):
        self.calls.append("validate_target")
        return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

    def commit(self, project_path, message, paths, timeout_seconds=60):
        self.calls.append("commit")
        return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

    def head_changed_files(self, project_path, timeout_seconds=20):
        self.calls.append("head_changed_files")
        return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/__manifest__.py\n")

    def head_sha(self, project_path):
        self.calls.append("head_sha")
        return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout=f"{self.sha}\n")

    def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
        self.calls.append("push")
        return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout=f"PUSH_OK\nsha={self.sha}\n")

    def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
        self.calls.append("remote_head_sha")
        return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout=f"{self.sha}\n")


class MissingAfterPullGit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_reset_hard(self, project_path, remote="origin", branch=None, timeout_seconds=120):
        self.calls.append("fetch_reset_hard")
        return CommandResult(command=["git", "fetch-reset-hard"], cwd=project_path, exit_code=0, stdout="HEAD is now at abc1234\n")

    def status(self, project_path):  # pragma: no cover - this must never run
        self.calls.append("status")
        raise AssertionError("workflow should fail fast before git.status or exploratory diagnosis")


def _install_passing_deploy_fakes(monkeypatch, fake_git):
    monkeypatch.setattr(workflow, "GitService", lambda: fake_git)
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: PassingSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: PassingOdooSh())
    monkeypatch.setattr(workflow, "OdooService", lambda: PassingOdooService())
    monkeypatch.setattr(
        workflow,
        "start_odoo_module_deploy_job",
        lambda req: OdooResult(status="ok", result={"finished": True, "job_status": "passed", "module_name": req.module_name}),
    )


def _payload(project: Path) -> SafeDeployModuleRequest:
    return SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        paths=["x_module/__manifest__.py"],
        branch="test_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        odoosh_deploy_timeout_seconds=5,
        odoosh_deploy_poll_interval_seconds=0.25,
    )


@pytest.mark.integration
def test_safe_deploy_module_missing_locally_syncs_then_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    fake_git = SafeDeployGit(project, create_on_sync=True)
    _install_passing_deploy_fakes(monkeypatch, fake_git)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    workflow._run_safe_deploy(_payload(project), job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert (project / "x_module" / "__manifest__.py").exists()
    assert fake_git.calls[0] == "fetch_reset_hard"
    assert "status" in fake_git.calls
    assert any(step.name == "ensure_checkout_current" and step.status == "passed" for step in finished.steps)
    assert any(step.name == "git_status" for step in finished.steps)


def test_safe_deploy_module_missing_after_sync_fails_fast_without_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    fake_git = MissingAfterPullGit()
    monkeypatch.setattr(workflow, "GitService", lambda: fake_git)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    workflow._run_safe_deploy(_payload(project), job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "checkout_stale_or_missing"
    assert "Module 'x_module' not found" in finished.result["error"]
    assert "even after syncing with remote branch 'test_branch'" in finished.result["error"]
    assert "stale, wiped, or pointed at the wrong project_path/branch" in finished.result["error"]
    assert fake_git.calls == ["fetch_reset_hard"]
    assert not any(step.name == "git_status" for step in finished.steps)
    assert any(step.name == "ensure_checkout_current" and step.status == "failed" for step in finished.steps)


@pytest.mark.integration
def test_safe_deploy_module_present_does_not_pull_or_add_checkout_step(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    module = project / "x_module"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'X Module', 'installable': True}\n", encoding="utf-8")
    fake_git = SafeDeployGit(project, create_on_sync=False, fail_if_sync_called=True)
    _install_passing_deploy_fakes(monkeypatch, fake_git)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    workflow._run_safe_deploy(_payload(project), job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert "fetch_reset_hard" not in fake_git.calls
    assert not any(step.name == "ensure_checkout_current" for step in finished.steps)


def test_coding_task_plan_syncs_known_prior_success_module_before_returning_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    JsonStore().save_session_state(
        {
            "project_path": str(project),
            "module_name": "x_module",
            "safe_deploy_status": "passed",
        }
    )
    fake_git = SafeDeployGit(project, create_on_sync=True)
    monkeypatch.setattr(checkout_guard_service, "GitService", lambda: fake_git)

    result = workflow.coding_task_plan(CodingTaskPlanRequest(
        project_path=str(project),
        module_name="x_module",
        branch="test_branch",
        task="Add one field",
    ))

    assert result.defaults["checkout_guard"]["status"] == "synced"
    assert (project / "x_module" / "__manifest__.py").exists()
    assert fake_git.calls == ["fetch_reset_hard"]


def test_coding_task_plan_known_prior_success_fails_fast_when_sync_does_not_restore_module(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    JsonStore().save_session_state(
        {
            "project_path": str(project),
            "module_name": "x_module",
            "safe_deploy_status": "passed",
        }
    )
    fake_git = MissingAfterPullGit()
    monkeypatch.setattr(checkout_guard_service, "GitService", lambda: fake_git)

    with pytest.raises(HTTPException) as excinfo:
        workflow.coding_task_plan(CodingTaskPlanRequest(
            project_path=str(project),
            module_name="x_module",
            branch="test_branch",
            task="Add one field",
        ))

    assert excinfo.value.status_code == 409
    assert "Module 'x_module' not found" in str(excinfo.value.detail)
    assert fake_git.calls == ["fetch_reset_hard"]


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True,
    )
    return completed.stdout.strip()


def _make_remote_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "test_branch", str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.com")
    (seed / "x_module").mkdir()
    (seed / "x_module" / "__manifest__.py").write_text("{'name': 'X'}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "test_branch")
    _git(tmp_path, "clone", "-b", "test_branch", str(remote), str(work))
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.com")
    return remote, seed, work


def test_checkout_guard_fast_forwards_clean_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _remote, seed, work = _make_remote_pair(tmp_path)
    (seed / "x_module" / "models.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "remote update")
    remote_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "test_branch")

    result = checkout_guard_service.ensure_module_exists_or_sync(
        project_path=work,
        module_name="x_module",
        branch="test_branch",
    )

    assert result["ok"] is True
    assert result["status"] == "fast_forwarded"
    assert result["local_head"] == remote_sha
    assert _git(work, "rev-parse", "HEAD") == remote_sha


def test_checkout_guard_blocks_dirty_checkout_when_remote_advanced(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _remote, seed, work = _make_remote_pair(tmp_path)
    (work / "x_module" / "__manifest__.py").write_text("{'name': 'Local edit'}\n", encoding="utf-8")
    (seed / "x_module" / "models.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "remote update")
    _git(seed, "push", "origin", "test_branch")

    result = checkout_guard_service.ensure_module_exists_or_sync(
        project_path=work,
        module_name="x_module",
        branch="test_branch",
    )

    assert result["ok"] is False
    assert result["status"] == "remote_advanced_dirty"
    assert result["dirty"] is True
    assert "preserve the diff" in result["error"]


def test_checkout_guard_blocks_unpushed_local_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _remote, _seed, work = _make_remote_pair(tmp_path)
    (work / "x_module" / "models.py").write_text("VALUE = 4\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "local only")

    result = checkout_guard_service.ensure_module_exists_or_sync(
        project_path=work,
        module_name="x_module",
        branch="test_branch",
    )

    assert result["ok"] is False
    assert result["status"] == "local_ahead"
    assert result["ahead"] == 1
    assert "refusing to bundle" in result["error"]


def test_nested_relative_path_resolves_manifest_owner(tmp_path):
    project = tmp_path / "repo"
    module = project / "addons" / "vertical" / "custom_sale"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")

    resolved = checkout_guard_service.module_name_from_relative_path(
        "addons/vertical/custom_sale/models/sale.py",
        str(project),
    )

    assert resolved == "custom_sale"


def test_nested_missing_relative_path_uses_odoo_source_directory_boundary(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    resolved = checkout_guard_service.module_name_from_relative_path(
        "addons/vertical/custom_sale/models/new_file.py",
        str(project),
    )

    assert resolved == "custom_sale"
