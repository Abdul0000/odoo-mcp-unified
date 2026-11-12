from __future__ import annotations

from tests.conftest import advance_clock_on_job_wait, patch_module_clock
import asyncio
import subprocess
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus, CommandResult, DeployedModuleVerifyRequest, JobRecord, JobStatus, JobStep, OdooDebugRecordAssertion, OdooResult, SafeDeployModuleRequest, ResumeJobRequest
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.services.repo_service import RepoService
from odoo_agent_server.services import store_service
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.services.workflow_guard_service import safe_deploy_shell_block
from odoo_agent_server.routers.workflow import _infer_registry_fields_from_python, _pre_push_verify_files
from odoo_agent_server.routers import tools, workflow


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_manual_module_deploy_stops_after_confirmed_safe_deploy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"module_name": "aurvio_website", "project_path": str(tmp_path / "repo")},
    ))
    store.finish_job(job, JobStatus.passed, {
        "status": "passed",
        "module_name": "aurvio_website",
        "deploy_confirmed": True,
    })

    blocked = safe_deploy_shell_block("odoo.module_deploy", module_name="aurvio_website")

    assert blocked is not None
    assert blocked.status == "ok"
    assert blocked.result["status"] == "already_deploy_confirmed"
    assert blocked.result["tool_exit_allowed"] is True
    assert blocked.result["next_call"] is None


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "feature_branch"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Abdul0000/test_18.git"], cwd=path, check=True)
    (path / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.local", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_registry_field_inference_prefers_explicit_name_over_mixin_inherit(tmp_path):
    project = tmp_path / "repo"
    source = project / "clinic_management" / "models" / "clinic_patient.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from odoo import fields, models\n\n"
        "class ClinicPatient(models.Model):\n"
        "    _name = 'clinic.patient'\n"
        "    _inherit = ['mail.thread', 'mail.activity.mixin']\n"
        "    note = fields.Text()\n",
        encoding="utf-8",
    )

    inferred = _infer_registry_fields_from_python(
        project,
        "clinic_management",
        [str(source)],
    )

    assert inferred == {"clinic.patient": ["note"]}


def test_safe_deploy_starter_failure_returns_report(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))

    class FakeApproval:
        def ensure_allowed(self, *args, **kwargs):
            return None

    class BrokenStore:
        def create_job(self, job):
            raise OSError("state store unavailable")

    monkeypatch.setattr(workflow, "ApprovalService", lambda: FakeApproval())
    monkeypatch.setattr(workflow, "JsonStore", lambda: BrokenStore())

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(tmp_path),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] field",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        timeout_seconds=900,
    )))

    assert result.status == "failed"
    assert result.job_id == ""
    assert result.steps[0].name == "safe_deploy_start"
    assert "state store unavailable" in result.summary["error"]


def test_safe_deploy_endpoint_starts_job_for_grok_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)

    def fake_submit(callback):
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] test_qwen field before quotation template in sale order form",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
        timeout_seconds=900,
    )))

    assert result.status == "running"
    assert result.job_id
    assert result.summary["module_name"] == "sale_order_notebook_tab"
    assert result.summary["next_call"]["tool"] == "jobs.get"
    assert JsonStore().load_session_state()["last_safe_deploy_job_id"] == result.job_id


def test_safe_deploy_waits_and_returns_finished_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)

    def fake_run(payload, job_id):
        store = JsonStore()
        job = store.get_job(job_id)
        store.add_step(job, JobStep(name="git_push", status="passed"))
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.passed, {"module_name": payload.module_name, "sha": "abc1234"})

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "_run_safe_deploy", fake_run)
    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] field",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=True,
        wait_timeout_seconds=5,
    )))

    assert result.status == "passed"
    assert result.summary["module_name"] == "sale_order_notebook_tab"
    assert result.summary["sha"] == "abc1234"
    assert any(step.name == "git_push" for step in result.steps)


@pytest.mark.integration
def test_git_commit_configures_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "git_author_name", "MCP Bot")
    monkeypatch.setattr(settings, "git_author_email", "mcp@example.local")
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=False)
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=False)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = GitService().commit(str(repo), "change readme", ["README.md"])

    assert result.exit_code == 0
    assert subprocess.check_output(["git", "config", "user.name"], cwd=repo, text=True).strip() == "MCP Bot"
    assert subprocess.check_output(["git", "config", "user.email"], cwd=repo, text=True).strip() == "mcp@example.local"


def test_git_push_target_validation_rejects_wrong_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_branch", "feature_branch")
    monkeypatch.setattr(settings, "target_git_repo", "git@github.com:Abdul0000/test_18.git")
    repo = tmp_path / "repo"
    _init_repo(repo)

    result = GitService().validate_target(str(repo), branch="main", remote="origin")

    assert result.exit_code == 2
    assert "expected feature_branch" in result.stderr


def test_repo_write_blocks_git_internal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    try:
        RepoService().write_file(str(repo), ".git/config", "bad")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:  # pragma: no cover
        raise AssertionError("Expected .git write to be blocked")


def test_odoosh_module_update_parser_fails_on_skipped_module():
    result = CommandResult(command=["odoo-bin"], cwd="odoosh:ssh", exit_code=0, stderr="module dynamic_ai_report_builder: not installable, skipped")

    classified = OdooShService()._classify_module_update_result(result)

    assert classified.exit_code == 3
    assert "MCP_CLASSIFIED_FAILURE" in classified.stderr


def test_approval_failed_command_is_not_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "git.commit")
    store = JsonStore()
    approval = store.create_approval(ApprovalRecord(action="git.commit", summary="commit", status=ApprovalStatus.approved))

    service = ApprovalService()
    service.ensure_allowed("git.commit", approval.id, {})
    service.finalize_command(approval.id, exit_code=1, success_reason="ok", failure_reason="failed")

    assert JsonStore().get_approval(approval.id).status == ApprovalStatus.failed


def test_pre_push_verify_files_expands_directory_paths_after_write(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "custom_mod"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom'}\n", encoding="utf-8")
    (module / "models" / "x.py").write_text("from odoo import models\n", encoding="utf-8")
    (module / "static").mkdir()
    (module / "static" / "ignored.png").write_bytes(b"png")

    files = _pre_push_verify_files(repo, "custom_mod", ["custom_mod"], "")

    assert str(module / "__manifest__.py") in files
    assert str(module / "models" / "x.py") in files
    assert all(not path.endswith("ignored.png") for path in files)


def test_pre_push_verify_files_falls_back_to_git_status_when_paths_are_not_files(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "custom_mod"
    (module / "views").mkdir(parents=True)
    changed = module / "views" / "view.xml"
    changed.write_text("<odoo/>\n", encoding="utf-8")
    status = "## branch\n M custom_mod/views/view.xml\n M README.md\n"

    files = _pre_push_verify_files(repo, "custom_mod", ["README.md"], status)

    assert files == [str(changed)]


def test_safe_deploy_fails_without_upgrade_when_sha_is_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": false}')

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=False,
            odoosh_deploy_timeout_seconds=0,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_failed_or_timeout"
    assert finished.result["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert "odoosh_wait_for_commit" not in {step.name for step in finished.steps}
    assert any(step.name == "odoosh_check_commit" and step.status == "failed" for step in finished.steps)


@pytest.mark.slow
def test_safe_deploy_polls_odoosh_until_deployed_before_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"check": 0, "deploy": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            calls["check"] += 1
            deployed = calls["check"] >= 3
            return CommandResult(
                command=["odoosh", "check"],
                cwd="odoosh:ssh",
                exit_code=0 if deployed else 1,
                stdout='{"deployed": true}' if deployed else '{"deployed": false}',
            )

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"check": 3, "deploy": 1}
    assert any(step.name == "odoosh_check_commit" and step.status == "passed" for step in finished.steps)


def test_safe_deploy_blocks_upgrade_when_odoosh_build_failed_after_push(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"manifest": 0, "deploy": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="db99b7e7\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=db99b7e7\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true, "current_sha": "db99b7e7"}')

        def recent_build_failure(self, since_epoch):
            return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=1, stdout='{"failed": true, "message": "Test: Failed"}')

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_failed"
    assert calls == {"manifest": 0, "deploy": 0}
    assert any(step.name == "odoosh_build_failure_guard" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_verifies_changed_user_files_before_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"check": 0, "verify_user": 0, "manifest": 0, "deploy": 0}
    captured = {}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False

                def model_dump(self, mode="json"):
                    return {"push_blocked": False}

            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            calls["check"] += 1
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            calls["verify_user"] += 1
            captured.update(expected_hashes)
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"check": 1, "verify_user": 1, "manifest": 0, "deploy": 1}
    assert set(captured) == {"x_module/models/a.py"}
    assert any(step.name == "odoosh_deployed_change_verify" and step.status == "passed" for step in finished.steps)
    assert finished.request["expected_user_file_hashes"] == captured


def test_safe_deploy_blocks_upgrade_when_deployed_user_file_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"manifest": 0, "deploy": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False

                def model_dump(self, mode="json"):
                    return {"push_blocked": False}

            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(
                command=["odoosh", "verify_user_file_hashes"],
                cwd="odoosh:ssh",
                exit_code=1,
                stdout="MISMATCH x_module/models/a.py expected=local actual=remote",
            )

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_code_mismatch"
    assert calls == {"manifest": 0, "deploy": 0}
    assert any(step.name == "odoosh_deployed_change_verify" and step.status == "failed" for step in finished.steps)


def test_verify_deployed_module_compares_against_odoosh_user_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"verify": 0, "list": 0}

    class FakeOdooSh:
        def list_user_files(self, module_name, subdir="", max_items=200):
            calls["list"] += 1
            return {
                "found": True,
                "module_name": module_name,
                "base": "/home/odoo/src/user",
                "files": [{"relative_path": "x_module/models/a.py", "path": "/home/odoo/src/user/x_module/models/a.py", "size": 10}],
            }

        def verify_user_file_hashes(self, expected_hashes):
            calls["verify"] += 1
            assert set(expected_hashes) == {"x_module/models/a.py"}
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x_module/models/a.py")

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    result = workflow.verify_deployed_module(DeployedModuleVerifyRequest(
        project_path=str(project),
        module_name="x_module",
        paths=["x_module/models/a.py"],
    ))

    assert result["status"] == "failed"
    assert result["source_status"] == "mismatch"
    assert "Do not claim live/deployed/already done" in result["message"]
    assert result["odoosh_user_files"]["base"] == "/home/odoo/src/user"
    assert calls == {"verify": 1, "list": 1}


def test_resume_job_rechecks_deploy_without_replaying_commit_or_push(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"commit": 0, "push": 0}

    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={
            "project_path": str(project),
            "module_name": "x_module",
            "commit_message": "Deploy x_module",
            "remote": "origin",
            "branch": "main",
            "run_qa": False,
            "upgrade_module": False,
            "verify_ui_paths": [],
        },
        steps=[
            JobStep(name="qa_self_run", status="skipped"),
            JobStep(name="git_configure_identity", status="passed"),
            JobStep(name="git_validate_target", status="passed"),
            JobStep(name="git_commit", status="passed"),
            JobStep(name="git_head_sha", status="passed", result={"stdout": "abc1234\n"}),
            JobStep(name="git_push", status="passed", result={"stdout": "PUSH_OK\nsha=abc1234\n"}),
            JobStep(name="odoosh_check_commit", status="warning"),
        ],
    ))

    class FakeGit:
        def commit(self, *args, **kwargs):
            calls["commit"] += 1
            raise AssertionError("resume must not replay git commit")

        def push(self, *args, **kwargs):
            calls["push"] += 1
            raise AssertionError("resume must not replay git push")

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(tools, "GitService", lambda: FakeGit())
    monkeypatch.setattr(tools, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(tools, "OdooService", lambda: FakeOdooService())

    result = asyncio.run(tools._resume_job(ResumeJobRequest(job_id=job.id)))

    assert result["status"] == "ok"
    assert result["resumed_from"] == "odoosh_check_commit"
    assert calls == {"commit": 0, "push": 0}
    assert "git_commit" not in result["steps_run"]
    assert "git_push" not in result["steps_run"]
    finished = JsonStore().get_job(job.id)
    assert finished.status == JobStatus.passed
    assert finished.deploy_confirmed is True
    assert finished.result["resume"]["status"] == "ok"


def test_resume_job_without_id_uses_saved_job_and_upgrade_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"deploy": 0}

    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={
            "project_path": str(project),
            "module_name": "x_module",
            "commit_message": "Deploy x_module",
            "remote": "origin",
            "branch": "main",
            "run_qa": False,
            "upgrade_module": True,
            "verify_ui_paths": [],
        },
        steps=[
            JobStep(name="git_commit", status="passed"),
            JobStep(name="git_head_sha", status="passed", result={"stdout": "abc1234\n"}),
            JobStep(name="git_push", status="passed"),
            JobStep(name="odoosh_check_commit", status="passed", result={"deployed": True, "stdout": '{"deployed": true}'}),
            JobStep(name="odoosh_verify_module_manifest", status="passed"),
        ],
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "project_path": str(project), "module_name": "x_module"})

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(tools, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(tools, "OdooService", lambda: FakeOdooService())

    result = asyncio.run(tools._resume_job(ResumeJobRequest(override_step="upgrade")))

    assert result["status"] == "ok"
    assert result["original_job_id"] == job.id
    assert result["resumed_from"] == "odoo_module_deploy"
    assert calls == {"deploy": 1}
    assert "git_commit" not in result["steps_run"]
    assert "git_push" not in result["steps_run"]
    assert JsonStore().get_job(job.id).status == JobStatus.passed


def test_resume_job_active_job_returns_poll_instruction_without_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(tmp_path / "repo"), "module_name": "x_module"},
    ))

    result = asyncio.run(tools._resume_job(ResumeJobRequest(job_id=job.id)))

    assert result["status"] == "active_job"
    assert result["next_call"] == {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
    assert JsonStore().get_job(job.id).status == JobStatus.running


def test_resume_without_id_filters_stale_saved_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    project_a = tmp_path / "repo_a"
    project_b = tmp_path / "repo_b"
    job_a = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={"project_path": str(project_a), "module_name": "a_module"},
    ))
    job_b = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={"project_path": str(project_b), "module_name": "b_module"},
    ))
    store.save_session_state({"last_safe_deploy_job_id": job_a.id, "project_path": str(project_a), "module_name": "a_module"})

    selected = tools._latest_safe_deploy_job_id(ResumeJobRequest(project_path=str(project_b), module_name="b_module"), store)

    assert selected == job_b.id


def test_safe_deploy_wait_for_result_positive_timeout_returns_running(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(workflow, "submit_background", lambda callback: None)
    clock = _FakeClock()
    patch_module_clock(monkeypatch, clock, workflow, store_service)
    advance_clock_on_job_wait(monkeypatch, clock)
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        run_qa=False,
        wait_for_result=True,
        wait_timeout_seconds=1,
    )))

    assert result.status == "running"
    assert result.summary["next_call"] == {"tool": "jobs.get", "arguments": {"job_id": result.job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}


def test_safe_deploy_wait_loop_does_not_sleep_past_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    clock = _FakeClock()
    patch_module_clock(monkeypatch, clock, workflow, store_service)
    advance_clock_on_job_wait(monkeypatch, clock)

    started = clock.monotonic()
    result = workflow._wait_workflow_job_until_terminal(store, job.id, poll_interval_seconds=10, timeout_seconds=1)
    elapsed = clock.monotonic() - started

    assert result.status == JobStatus.running
    assert elapsed == 1




def test_ui_verification_warning_still_confirms_deploy_and_stops_polling(tmp_path, monkeypatch):
    """Frontend/UI proof can be a terminal warning, but deploy+upgrade is done.

    This is the case that caused agents to keep calling jobs.get even after
    Odoo.sh deployment and module upgrade had already succeeded: the job was no
    longer running, but deploy_confirmed stayed false only because live browser
    UI proof was missing.  The job must be terminal with no next_call.
    """
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={"project_path": str(tmp_path / "repo"), "module_name": "x_module", "upgrade_module": True},
        result={
            "status": "ui_verification_required",
            "odoosh_check_commit": {"status": "passed", "deployed": True},
            "module_upgrade": {"requested": True, "status": "passed", "passed": True},
            "success_allowed": False,
            "claim_blocked": "visible UI/page changed",
        },
    ))

    finished = store.wait_job(job.id, timeout_seconds=600, poll_interval_seconds=10.0)

    assert finished.status == JobStatus.warning
    assert finished.completion_required is False
    assert finished.deploy_confirmed is True
    assert finished.success_allowed is False
    assert finished.next_call is None
    assert finished.terminal_action == "stop_and_summarize"


def test_resume_already_complete_warning_job_finalizes_without_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={"project_path": str(project), "module_name": "x_module", "upgrade_module": True},
        steps=[JobStep(name=name, status="passed") for name in tools._SAFE_DEPLOY_STEPS],
        result={"status": "module_upgrade_running", "module_name": "x_module"},
    ))

    result = asyncio.run(tools._resume_job(ResumeJobRequest(job_id=job.id)))
    finished = JsonStore().get_job(job.id)

    assert result["status"] == "resume_finalization_incomplete"
    assert result["job_status_after_resume"] == JobStatus.failed.value
    assert finished.status == JobStatus.failed
    assert finished.deploy_confirmed is False


def test_resume_after_fix_cycle_uses_latest_step_sha_not_stale_request(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module" / "models").mkdir(parents=True)
    (project / "x_module" / "__manifest__.py").write_text("{}", encoding="utf-8")
    (project / "x_module" / "models" / "a.py").write_text("x = 1\n", encoding="utf-8")
    polled = []

    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={
            "project_path": str(project),
            "module_name": "x_module",
            "upgrade_module": True,
            "last_sha": "abc1111",
            "paths": ["x_module/models/a.py"],
            "run_qa": False,
        },
        steps=[
            JobStep(name="git_configure_identity", status="passed"),
            JobStep(name="git_validate_target", status="passed"),
            JobStep(name="git_commit", status="passed"),
            JobStep(name="git_head_commit_path_gate", status="passed"),
            JobStep(name="git_head_sha", status="passed", result={"stdout": "abc1111\n"}),
            JobStep(name="git_push", status="passed", result={"stdout": "PUSH_OK\nsha=abc1111\n"}),
            JobStep(name="git_remote_head_verify", status="passed"),
            JobStep(name="fix_cycle_1_git_head_sha", status="passed", result={"stdout": "def2222\n"}),
            JobStep(name="fix_cycle_1_git_push", status="passed", result={"stdout": "PUSH_OK\nsha=def2222\n"}),
            JobStep(name="fix_cycle_1_git_remote_head_verify", status="passed"),
        ],
        result={"status": "deploy_failed_or_timeout", "module_name": "x_module"},
    ))

    class FakeGit:
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse"], cwd=project_path, exit_code=0, stdout="def2222\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            polled.append(sha)
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def recent_build_failure(self, since_epoch):
            return CommandResult(command=["odoosh", "recent"], cwd="odoosh:ssh", exit_code=0, stdout="{}")

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdoo:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={})

    monkeypatch.setattr(tools, "GitService", lambda: FakeGit())
    monkeypatch.setattr(tools, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(tools, "OdooService", lambda: FakeOdoo())
    monkeypatch.setattr(tools, "start_odoo_module_deploy_job", lambda req: OdooResult(status="ok", result={"finished": True, "module_name": req.module_name}))

    result = asyncio.run(tools._resume_job(ResumeJobRequest(job_id=job.id)))

    assert result["status"] == "ok"
    assert result["steps_run"]["odoosh_check_commit"]["sha"] == "def2222"
    assert polled == ["def2222"]


def test_stale_running_safe_deploy_returns_resumable_existing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "safe_deploy_stale_running_seconds", 1)
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        created_at=old,
        request={"project_path": str(project), "module_name": "x_module"},
    ))

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        wait_for_result=False,
    )))

    finished = JsonStore().get_job(job.id)
    assert result.status == "resumable_existing_job"
    assert result.summary["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert finished.status == JobStatus.warning
    assert finished.result["status"] == "stale_running_job"


def test_manual_git_commit_blocked_while_safe_deploy_owns_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(project), "module_name": "x_module"},
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "project_path": str(project), "module_name": "x_module"})

    from odoo_agent_server.routers.git import git_commit
    from odoo_agent_server.schemas import GitCommitRequest

    result = git_commit(GitCommitRequest(project_path=str(project), message="manual commit", paths=[]))

    assert result.exit_code == 2
    assert "workflow.safe_deploy_module already owns" in result.stderr
    assert '"tool": "jobs.get"' in result.stdout


def test_manual_odoosh_update_blocked_while_safe_deploy_owns_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(tmp_path / "repo"), "module_name": "x_module"},
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "module_name": "x_module"})

    from odoo_agent_server.routers.odoosh import odoosh_module_update
    from odoo_agent_server.schemas import OdooShModuleUpdateRequest

    result = odoosh_module_update(OdooShModuleUpdateRequest(module_name="x_module"))

    assert result.exit_code == 2
    assert "Manual odoosh.module_update is blocked" in result.stderr
    assert '"tool": "jobs.get"' in result.stdout


def test_safe_deploy_retries_instead_of_finishing_as_nested_upgrade_running(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        nested = JsonStore().create_job(JobRecord(job_type="odoo.module_deploy", status=JobStatus.running, request={"module_name": req.module_name}))
        return OdooResult(status="ok", result={"job_id": nested.id, "job_status": "running", "finished": False})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoo_module_deploy_timeout_seconds=0,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "upgrade_failed_after_retries"
    # An unmappable/nested upgrade-running error is verifier-blocked: retrying
    # would only re-push byte-identical code, so the loop must stop after one
    # actionable failure instead of exhausting max_fix_cycles.
    assert finished.result["attempts"] == 1
    assert "wait_for_result=true" in finished.result["last_real_error"]
    assert "in progress" not in str(finished.result).lower()
    assert any(step.name == "odoo_module_deploy" and step.status == "failed" for step in finished.steps)
    assert [step.name for step in finished.steps if step.name.startswith("upgrade_attempt_")] == ["upgrade_attempt_1"]


def test_safe_deploy_verifies_inferred_python_field_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    field_file = project / "x_module" / "models" / "purchase_order.py"
    field_file.parent.mkdir(parents=True)
    field_file.write_text("from odoo import fields, models\n\nclass PurchaseOrder(models.Model):\n    _inherit = 'purchase.order'\n\n    grok_y = fields.Char(string='Grok Y', tracking=True)\n", encoding="utf-8")
    calls = {"registry_fields": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/purchase_order.py\n")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None):
            calls["registry_fields"] += 1
            assert model == "ir.model.fields"
            assert ("model", "=", "purchase.order") in domain
            assert ("name", "in", ["grok_y"]) in domain
            return OdooResult(status="ok", result=[{"model": "purchase.order", "name": "grok_y", "ttype": "char"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/purchase_order.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"registry_fields": 1}
    assert finished.result["expected_registry_fields"] == {"purchase.order": ["grok_y"]}
    assert any(step.name == "registry_field_verify" and step.status == "passed" for step in finished.steps)


def test_safe_deploy_fails_when_inferred_field_missing_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    field_file = project / "x_module" / "models" / "purchase_order.py"
    field_file.parent.mkdir(parents=True)
    field_file.write_text("from odoo import fields, models\n\nclass PurchaseOrder(models.Model):\n    _inherit = 'purchase.order'\n\n    grok_y = fields.Char(string='Grok Y')\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/purchase_order.py\n")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)
        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)
        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")
        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')
        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        return OdooResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None):
            return OdooResult(status="ok", result=[])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/purchase_order.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "registry_field_missing"
    assert any(step.name == "registry_field_verify" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_duplicate_call_returns_existing_running_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    payload = SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_a",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
    )

    first = asyncio.run(workflow.safe_deploy_module(payload))
    second = asyncio.run(workflow.safe_deploy_module(payload))

    assert first.status == "running"
    assert second.status == "already_in_progress"
    assert second.job_id == first.job_id
    assert second.summary["blocking_reason"] == "active_job"
    assert second.summary["next_call"]["tool"] == "jobs.get"
    assert submit_calls == {"count": 1}
    assert len(JsonStore().list_jobs()) == 1


def test_safe_deploy_resumable_failure_returns_existing_job_to_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    store = JsonStore()
    existing = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={
            "project_path": str(project),
            "module_name": "sale_order_notebook_tab",
            "commit_message": "Add field grok_a",
        },
        result={"status": "deploy_failed_or_timeout", "message": "Odoo.sh did not deploy the pushed SHA yet."},
    ))
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_a retry",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
    )))

    assert result.status == "resumable_existing_job"
    assert result.job_id == existing.id
    assert result.summary["blocking_reason"] == "resumable_job"
    assert result.summary["result_status"] == "deploy_failed_or_timeout"
    assert result.summary["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": existing.id}}
    assert submit_calls == {"count": 0}
    assert len(JsonStore().list_jobs()) == 1


def test_safe_deploy_force_new_after_resumable_failure_starts_new_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    store = JsonStore()
    existing = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={
            "project_path": str(project),
            "module_name": "sale_order_notebook_tab",
            "commit_message": "Add field grok_a",
        },
        result={"status": "deploy_code_mismatch", "message": "Deployed source did not match the pushed files."},
    ))
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Force restart deploy",
        branch="feature_branch",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
        force_new_deploy=True,
    )))

    assert result.status == "running"
    assert result.job_id != existing.id
    assert submit_calls == {"count": 1}
    jobs = JsonStore().list_jobs()
    assert len(jobs) == 2
    assert {job.id for job in jobs} == {existing.id, result.job_id}



def test_safe_deploy_request_defaults_to_final_wait_and_qa_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        # Do not execute the background callback; this test asserts the HTTP
        # handler returns immediately with a resumable/pollable job id.
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    payload = SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_fast",
        branch="feature_branch",
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
    )

    assert payload.run_qa is True
    assert SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_fast",
    ).wait_for_result is True

    started = time.monotonic()
    result = asyncio.run(workflow.safe_deploy_module(payload))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result.status == "running"
    assert result.job_id
    assert result.summary["next_call"]["tool"] == "jobs.get"
    assert submit_calls == {"count": 1}


def test_run_safe_deploy_runs_qa_by_default_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    calls = {"qa": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)
        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)
        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)
        def head_changed_files(self, project_path, timeout_seconds=20):
            return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")
        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")
        def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
            return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    class QaReport:
        status = "passed"
        def model_dump(self, mode="json"):
            return {"status": "passed"}

    def fake_self_run(req):
        calls["qa"] += 1
        return QaReport()

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())
    monkeypatch.setattr(workflow, "self_run", fake_self_run)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            wait_for_odoosh=False,
            upgrade_module=False,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert calls == {"qa": 1}
    assert any(step.name == "qa_self_run" and step.status == "passed" for step in finished.steps)



def test_safe_deploy_no_changes_verifies_odoosh_source_and_confirms_already_deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"verify_user": 0, "list": 0, "commit": 0, "push": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="")
        def commit(self, *args, **kwargs):
            calls["commit"] += 1
            raise AssertionError("no-change safe_deploy must not commit")
        def push(self, *args, **kwargs):
            calls["push"] += 1
            raise AssertionError("no-change safe_deploy must not push")

    class FakeOdooSh:
        def list_user_files(self, module_name, subdir="", max_items=200):
            calls["list"] += 1
            return {"found": True, "module_name": module_name, "files": [{"relative_path": "x_module/models/a.py"}]}
        def verify_user_file_hashes(self, expected_hashes):
            calls["verify_user"] += 1
            assert set(expected_hashes) == {"x_module/models/a.py"}
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        paths=["x_module/models/a.py"],
        run_qa=False,
        wait_for_result=True,
        wait_timeout_seconds=5,
    )))

    finished = JsonStore().get_job(result.job_id)
    assert result.status == "passed"
    assert result.summary["no_changes_detected"] is True
    assert result.summary["verified_already_deployed"] is True
    assert result.summary["already_deployed_confirmed"] is True
    assert result.verified_already_deployed is True
    assert result.already_deployed_confirmed is True
    assert finished.verified_already_deployed is True
    assert finished.already_deployed_confirmed is True
    assert calls == {"verify_user": 1, "list": 1, "commit": 0, "push": 0}
    assert any(step.name == "verify_deployed_module_no_changes" and step.status == "passed" for step in finished.steps)
    assert "Verified against Odoo.sh: matched" in result.summary["message"]


def test_safe_deploy_no_changes_mismatch_blocks_already_deployed_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"verify_user": 0, "list": 0, "commit": 0, "push": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="")
        def commit(self, *args, **kwargs):
            calls["commit"] += 1
            raise AssertionError("no-change safe_deploy must not commit")
        def push(self, *args, **kwargs):
            calls["push"] += 1
            raise AssertionError("no-change safe_deploy must not push")

    class FakeOdooSh:
        def list_user_files(self, module_name, subdir="", max_items=200):
            calls["list"] += 1
            return {"found": True, "module_name": module_name, "files": [{"relative_path": "x_module/models/a.py"}]}
        def verify_user_file_hashes(self, expected_hashes):
            calls["verify_user"] += 1
            assert set(expected_hashes) == {"x_module/models/a.py"}
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x_module/models/a.py")

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        paths=["x_module/models/a.py"],
        run_qa=False,
        wait_for_result=True,
        wait_timeout_seconds=5,
    )))

    finished = JsonStore().get_job(result.job_id)
    assert result.status == "failed"
    assert result.summary["no_changes_detected"] is True
    assert result.summary["verified_already_deployed"] is False
    assert result.summary["already_deployed_confirmed"] is False
    assert result.verified_already_deployed is False
    assert result.already_deployed_confirmed is False
    assert finished.verified_already_deployed is False
    assert finished.result["status"] == "already_deployed_not_confirmed"
    assert calls == {"verify_user": 1, "list": 1, "commit": 0, "push": 0}
    assert any(step.name == "verify_deployed_module_no_changes" and step.status == "failed" for step in finished.steps)
    assert "Do not claim already deployed" in result.summary["message"]


def test_safe_deploy_with_real_module_changes_uses_regular_commit_push_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    calls = {"commit": 0, "push": 0, "verify_user": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="diff --git a/x_module/models/a.py b/x_module/models/a.py\n")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)
        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)
        def commit(self, project_path, message, paths, timeout_seconds=60):
            calls["commit"] += 1
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)
        def head_changed_files(self, project_path, timeout_seconds=20):
            return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")
        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            calls["push"] += 1
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")
        def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
            return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def list_user_files(self, *args, **kwargs):
            calls["verify_user"] += 1
            raise AssertionError("normal change path must not run no-change verification")
        def verify_user_file_hashes(self, expected_hashes):
            calls["verify_user"] += 1
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooService:
        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooService", lambda: FakeOdooService())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=False,
            upgrade_module=False,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"commit": 1, "push": 1, "verify_user": 0}
    assert finished.result.get("no_changes_detected") is not True
    assert any(step.name == "git_commit" and step.status == "passed" for step in finished.steps)
    assert any(step.name == "git_push" and step.status == "passed" for step in finished.steps)
    assert not any(step.name == "verify_deployed_module_no_changes" for step in finished.steps)



class _SafeDeployRetrySyntaxVerifier:
    def __init__(self, calls):
        self.calls = calls

    def verify(self, req):
        self.calls["verify"] += 1

        class Result:
            push_blocked = False

            def model_dump(self, mode="json"):
                return {
                    "push_blocked": False,
                    "fix_cycle": req.fix_cycle,
                    "files_checked": list(req.written_files),
                }

        return Result()


class _SafeDeployRetryGit:
    def __init__(self, calls):
        self.calls = calls
        self.sha = "abc0000"

    def status(self, project_path):
        return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")

    def diff(self, project_path, staged=False, timeout_seconds=30):
        return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="diff --git a/x_module/models/a.py b/x_module/models/a.py\n")

    def changed_files(self, project_path):
        return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")

    def configure_identity(self, project_path):
        return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

    def validate_target(self, project_path, branch=None, remote="origin"):
        return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

    def commit(self, project_path, message, paths, timeout_seconds=60):
        self.calls["commit"] += 1
        self.sha = f"abc{self.calls['commit']:04d}"
        return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0, stdout=f"commit {self.sha}\n")

    def head_changed_files(self, project_path, timeout_seconds=20):
        return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")

    def head_sha(self, project_path):
        return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout=f"{self.sha}\n")

    def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
        self.calls["push"] += 1
        return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout=f"PUSH_OK\nsha={self.sha}\n")

    def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
        return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout=f"{self.sha}\n")


class _SafeDeployRetryOdooSh:
    def __init__(self, calls):
        self.calls = calls

    def check_commit_deployed(self, sha):
        self.calls["odoosh_check"] += 1
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def verify_user_file_hashes(self, expected_hashes):
        self.calls["verify_user"] += 1
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def verify_module_manifest(self, module_name):
        self.calls["manifest"] += 1
        return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def error_pack(self, tail_lines=800):
        self.calls["error_pack"] += 1
        stdout = """MCP_ERROR_PACK
{
  "summary": "Odoo.sh traceback/error pack",
  "files": ["/home/odoo/src/user/x_module/models/a.py"],
  "xml_locations": [],
  "models_mentioned": [],
  "hints": ["Python import failure: check __init__.py imports, renamed files, and manifest dependencies."]
}

TRACEBACK_BLOCKS
Traceback (most recent call last):
  File "/home/odoo/src/user/x_module/models/a.py", line 1, in <module>
RuntimeError: real upgrade failure
"""
        return CommandResult(command=["odoosh.error_pack", str(tail_lines)], cwd="odoosh:ssh", exit_code=0, stdout=stdout)


class _SafeDeployRetryOdooService:
    def module_status(self, module_name):
        return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
        return OdooResult(status="ok", result={"module": module_name})


def _retry_project(tmp_path, monkeypatch, success_on=None, max_fix_cycles=5):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    calls = {
        "commit": 0,
        "push": 0,
        "verify": 0,
        "deploy": 0,
        "error_pack": 0,
        "odoosh_check": 0,
        "verify_user": 0,
        "manifest": 0,
    }
    git = _SafeDeployRetryGit(calls)
    odoosh = _SafeDeployRetryOdooSh(calls)

    def fake_deploy(req):
        calls["deploy"] += 1
        if success_on is not None and calls["deploy"] >= success_on:
            return OdooResult(status="ok", result={"finished": True, "job_status": "passed", "module_name": req.module_name})
        return OdooResult(
            status="failed",
            error=f"real upgrade failure {calls['deploy']}",
            result={"finished": True, "job_status": "failed", "error": f"real upgrade failure {calls['deploy']}"},
        )

    monkeypatch.setattr(workflow, "GitService", lambda: git)
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: _SafeDeployRetrySyntaxVerifier(calls))
    monkeypatch.setattr(workflow, "OdooShService", lambda: odoosh)
    monkeypatch.setattr(workflow, "OdooService", lambda: _SafeDeployRetryOdooService())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    payload = SafeDeployModuleRequest(
        project_path=str(project),
        module_name="x_module",
        commit_message="Deploy x_module",
        paths=["x_module/models/a.py"],
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        max_fix_cycles=max_fix_cycles,
    )
    return store, job, payload, calls


@pytest.mark.slow
def test_safe_deploy_upgrade_fail_then_succeeds_on_retry(tmp_path, monkeypatch):
    store, job, payload, calls = _retry_project(tmp_path, monkeypatch, success_on=2)

    workflow._run_safe_deploy(payload, job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert finished.result["status"] == "passed"
    assert finished.result["module_upgrade"]["passed"] is True
    assert finished.result["module_upgrade"]["attempt"] == 2
    assert calls["deploy"] == 2
    assert calls["error_pack"] == 1
    assert calls["commit"] == 2
    assert calls["push"] == 2
    assert finished.request["last_sha"] == "abc0002"
    steps = {step.name: step.status for step in finished.steps}
    assert steps["upgrade_attempt_1"] == "failed"
    assert steps["fix_cycle_1"] == "passed"
    assert steps["upgrade_attempt_2"] == "passed"


@pytest.mark.slow
def test_safe_deploy_upgrade_fails_all_5_cycles_with_real_error(tmp_path, monkeypatch):
    store, job, payload, calls = _retry_project(tmp_path, monkeypatch, success_on=None, max_fix_cycles=5)

    workflow._run_safe_deploy(payload, job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "upgrade_failed_after_retries"
    assert finished.result["max_fix_cycles"] == 5
    assert finished.result["attempts"] == 5
    assert "real upgrade failure 5" in finished.result["last_real_error"]
    assert "RuntimeError: real upgrade failure" in finished.result["last_real_error"]
    assert finished.result["next_call"] == {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
    assert "pushed & upgrade triggered" not in finished.result["message"].lower()
    assert "deployed successfully" not in finished.result["message"].lower()
    assert calls["deploy"] == 5
    assert calls["error_pack"] == 5
    assert calls["commit"] == 5
    assert calls["push"] == 5
    step_names = [step.name for step in finished.steps]
    assert [f"upgrade_attempt_{i}" for i in range(1, 6)] == [name for name in step_names if name.startswith("upgrade_attempt_")]
    assert [f"fix_cycle_{i}" for i in range(1, 5)] == [name for name in step_names if name.startswith("fix_cycle_") and name.count("_") == 2]


def test_safe_deploy_stops_retry_loop_when_fix_cycle_is_blocked(tmp_path, monkeypatch):
    store, job, payload, calls = _retry_project(tmp_path, monkeypatch, success_on=None, max_fix_cycles=5)

    class BlockingFixVerifier:
        def verify(self, req):
            calls["verify"] += 1

            class Result:
                push_blocked = req.fix_cycle > 0

                def model_dump(self, mode="json"):
                    return {
                        "push_blocked": self.push_blocked,
                        "fix_cycle": req.fix_cycle,
                        "files_checked": list(req.written_files),
                    }

            return Result()

    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: BlockingFixVerifier())

    workflow._run_safe_deploy(payload, job.id)

    finished = store.get_job(job.id)
    step_names = [step.name for step in finished.steps]
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "upgrade_failed_after_retries"
    assert finished.result["attempts"] == 1
    assert calls["deploy"] == 1
    assert calls["commit"] == 1
    assert calls["push"] == 1
    assert "fix_cycle_1" in step_names
    assert "upgrade_attempt_2" not in step_names


def test_safe_deploy_upgrade_succeeds_first_try_without_wasted_retries(tmp_path, monkeypatch):
    store, job, payload, calls = _retry_project(tmp_path, monkeypatch, success_on=1)

    workflow._run_safe_deploy(payload, job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls["deploy"] == 1
    assert calls["error_pack"] == 0
    assert calls["commit"] == 1
    assert calls["push"] == 1
    step_names = [step.name for step in finished.steps]
    assert "upgrade_attempt_1" in step_names
    assert not any(name.startswith("fix_cycle_") for name in step_names)
    assert all(step.duration_ms is not None for step in finished.steps)
    assert finished.result["timing_summary"]["measured_stage_count"] > 0


def test_safe_deploy_runs_one_combined_live_assertion_after_upgrade(tmp_path, monkeypatch):
    store, job, payload, _calls = _retry_project(tmp_path, monkeypatch, success_on=1)
    payload = payload.model_copy(update={
        "verification_assertion": OdooDebugRecordAssertion(
            model="ir.cron",
            fields=["active", "interval_number"],
            expected_values={"active": True, "interval_number": 1},
            expected_count_min=1,
        ),
    })
    monkeypatch.setattr(
        workflow.LiveAssertionService,
        "evaluate",
        lambda self, assertion, **kwargs: {
            "phase": "post_deploy",
            "query_status": "ok",
            "passed": True,
            "model": "ir.cron",
            "count": 1,
            "matched_rows": 1,
        },
    )

    workflow._run_safe_deploy(payload, job.id)

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert finished.result["live_verification"]["passed"] is True
    steps = {step.name: step.status for step in finished.steps}
    assert steps["combined_live_verification"] == "passed"


def test_safe_deploy_job_stays_running_until_upgrade_loop_exits(tmp_path, monkeypatch):
    store, job, payload, calls = _retry_project(tmp_path, monkeypatch, success_on=1)
    deploy_started = Event()
    allow_deploy_finish = Event()

    def slow_deploy(req):
        calls["deploy"] += 1
        deploy_started.set()
        assert allow_deploy_finish.wait(timeout=5)
        return OdooResult(status="ok", result={"finished": True, "job_status": "passed", "module_name": req.module_name})

    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", slow_deploy)
    worker = Thread(target=workflow._run_safe_deploy, args=(payload, job.id))
    worker.start()
    assert deploy_started.wait(timeout=5)

    in_progress = store.get_job(job.id)
    assert in_progress.status == JobStatus.running
    assert in_progress.result is None
    assert not any("pushed & upgrade triggered" in str(step.result).lower() for step in in_progress.steps)

    allow_deploy_finish.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert finished.result["module_upgrade"]["passed"] is True


def _run_safe_deploy_endpoint_retry_case(tmp_path, monkeypatch, *, success_on, max_fix_cycles=5):
    store, _unused_job, payload, calls = _retry_project(
        tmp_path,
        monkeypatch,
        success_on=success_on,
        max_fix_cycles=max_fix_cycles,
    )

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "submit_background", fake_submit)
    payload = payload.model_copy(update={
        "wait_for_result": True,
        "wait_timeout_seconds": 5,
        "force_new_deploy": True,
    })
    result = asyncio.run(workflow.safe_deploy_module(payload))
    finished = JsonStore().get_job(result.job_id)
    return result, finished, calls


@pytest.mark.slow
def test_safe_deploy_wait_for_result_fail_once_then_success_returns_confirmed_final_result(tmp_path, monkeypatch):
    result, finished, calls = _run_safe_deploy_endpoint_retry_case(tmp_path, monkeypatch, success_on=2)

    assert result.status == "passed"
    assert finished.status == JobStatus.passed
    assert calls["deploy"] == 2
    assert result.summary["message"] == "Upgrade confirmed successful after 2 attempt(s)."
    assert result.summary["module_upgrade"]["attempts"] == 2
    assert result.summary["module_upgrade"]["passed"] is True
    assert "check back" not in str(result.summary).lower()
    assert "may take a few minutes" not in str(result.summary).lower()
    assert "in progress" not in str(result.summary).lower()


@pytest.mark.slow
def test_safe_deploy_wait_for_result_fail_all_retries_returns_real_last_error(tmp_path, monkeypatch):
    result, finished, calls = _run_safe_deploy_endpoint_retry_case(tmp_path, monkeypatch, success_on=None, max_fix_cycles=5)

    assert result.status == "failed"
    assert finished.status == JobStatus.failed
    assert calls["deploy"] == 5
    assert result.summary["status"] == "upgrade_failed_after_retries"
    assert result.summary["attempts"] == 5
    assert "Upgrade failed after 5 attempts, last error:" in result.summary["message"]
    assert "real upgrade failure 5" in result.summary["message"]
    assert "RuntimeError: real upgrade failure" in result.summary["last_real_error"]
    assert result.summary["next_call"] == {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 150}}
    assert "check back" not in str(result.summary).lower()
    assert "may take a few minutes" not in str(result.summary).lower()
    assert "in progress" not in str(result.summary).lower()


@pytest.mark.slow
def test_safe_deploy_wait_for_result_response_never_uses_progress_wording(tmp_path, monkeypatch):
    success, _finished_success, _calls_success = _run_safe_deploy_endpoint_retry_case(tmp_path, monkeypatch, success_on=1)
    success_text = str(success.summary).lower()
    assert "upgrade confirmed successful after 1 attempt(s)" in success_text
    assert "may take a few minutes" not in success_text
    assert "check back" not in success_text
    assert "in progress" not in success_text

    # Run the failed case in a separate state dir/project to avoid the previous
    # finished job and monkeypatch state interacting with the second scenario.
    result, _finished_failure, _calls_failure = _run_safe_deploy_endpoint_retry_case(tmp_path / "failure", monkeypatch, success_on=None, max_fix_cycles=5)
    failure_text = str(result.summary).lower()
    assert "upgrade failed after 5 attempts" in failure_text
    assert "may take a few minutes" not in failure_text
    assert "check back" not in failure_text
    assert "in progress" not in failure_text


def test_frontend_ui_files_require_browser_proof_for_visible_claims():
    files = workflow._frontend_ui_files_requiring_live_proof(
        "sample_website_module",
        [
            "sample_website_module/views/website_templates.xml",
            "sample_website_module/static/src/scss/page_widget.scss",
            "sample_website_module/models/order.py",
            "sample_website_module/security/ir.model.access.csv",
        ],
    )
    assert files == [
        "sample_website_module/views/website_templates.xml",
        "sample_website_module/static/src/scss/page_widget.scss",
    ]
