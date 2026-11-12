from __future__ import annotations

from tests.conftest import WS_TOOLS
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus
from odoo_agent_server.services.store_service import JsonStore


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _init_repo(project: Path, branch: str = "main") -> None:
    project.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)


def _make_valid_module(root: Path, module_name: str = "x_module") -> None:
    module = root / module_name
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import x_model\n", encoding="utf-8")
    (module / "models" / "x_model.py").write_text(
        "from odoo import models, fields\n\nclass XModel(models.Model):\n    _name = 'x.model'\n    name = fields.Char()\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name':'X Module','version':'18.0.1.0.0','depends':['base'],'data':['security/ir.model.access.csv','views/x_views.xml']}\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x_model,x.model,model_x_model,,1,1,1,1\n",
        encoding="utf-8",
    )
    (module / "views" / "x_views.xml").write_text(
        "<odoo><record id='x_model_tree' model='ir.ui.view'><field name='name'>x.model.tree</field><field name='model'>x.model</field><field name='arch' type='xml'><tree><field name='name'/></tree></field></record></odoo>",
        encoding="utf-8",
    )


def test_invalid_auth_rejected_for_tool_surface():
    client = TestClient(app)
    response = client.get("/tools/list", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_symlink_escape_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (project / "link.txt").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable on this Windows host: {exc}")
    client = TestClient(app)
    response = client.post("/repo/read", headers=_auth(), json={"project_path": str(project), "relative_path": "link.txt"})
    assert response.status_code == 403


@pytest.mark.integration
def test_apply_patch_check_only_does_not_mutate_and_escape_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    _init_repo(project, branch="dev")
    patch = """diff --git a/README.md b/README.md\nindex e79c5e8..b6fc4c6 100644\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-initial\n+changed\n"""
    client = TestClient(app)
    checked = client.post("/repo/apply-patch", headers=_auth(), json={"project_path": str(project), "patch": patch, "check_only": True})
    assert checked.status_code == 200
    assert checked.json()["exit_code"] == 0
    assert (project / "README.md").read_text(encoding="utf-8") == "initial\n"

    bad_patch = """diff --git a/../evil.txt b/../evil.txt\n--- a/../evil.txt\n+++ b/../evil.txt\n@@ -0,0 +1 @@\n+bad\n"""
    rejected = client.post("/repo/apply-patch", headers=_auth(), json={"project_path": str(project), "patch": bad_patch})
    assert rejected.status_code == 400


def test_approval_is_single_use_when_global_approval_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    blocked = client.post("/repo/write", headers=_auth(), json={"project_path": str(project), "relative_path": "a.txt", "content": "one"})
    approval_id = blocked.json()["detail"]["approval_id"]
    client.post("/approvals/approve", headers=_auth(), json={"approval_id": approval_id})

    first = client.post("/repo/write", headers=_auth(), json={"project_path": str(project), "relative_path": "a.txt", "content": "one", "approval_id": approval_id})
    assert first.status_code == 200
    second = client.post("/repo/write", headers=_auth(), json={"project_path": str(project), "relative_path": "b.txt", "content": "two", "approval_id": approval_id})
    assert second.status_code == 403
    assert "consumed" in second.text
    monkeypatch.setattr(settings, "require_approval", False)


@pytest.mark.integration
def test_protected_branch_push_requires_real_approved_git_push_approval_even_when_global_approval_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)
    project = tmp_path / "repo"
    _init_repo(project, branch="main")
    client = TestClient(app)

    no_approval = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "main"})
    assert no_approval.status_code == 409

    wrong = JsonStore().create_approval(ApprovalRecord(action="repo.write", summary="wrong", status=ApprovalStatus.approved))
    wrong_response = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "main", "approval_id": wrong.id})
    assert wrong_response.status_code == 403

    pending = JsonStore().create_approval(ApprovalRecord(action="git.push", summary="pending", status=ApprovalStatus.pending))
    pending_response = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "main", "approval_id": pending.id})
    assert pending_response.status_code == 403


def test_tools_list_reflects_runtime_approval_policy(monkeypatch):
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "git.push,repo.apply_patch_atomic")
    client = TestClient(app)
    response = client.get("/tools/list", headers=_auth())
    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()}
    assert by_name["git_push"]["requires_approval"] is True
    assert by_name["repo_apply_patch_atomic"]["requires_approval"] is True
    assert by_name["repo_read"]["read_only"] is True


def test_mcp_initialize_list_and_call(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _make_valid_module(tmp_path)
    client = TestClient(app)

    init = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init.status_code == 200
    assert init.json()["result"]["capabilities"]["tools"]

    listed = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "connector_health",
        "skills_list",
        "skills_read",
        "client_check",
        "workflow_agent_instructions",
        "workflow_odoo_code_change",
        "workflow_resume_continuation",
        "workflow_odoo_debug_and_fix",
        "workflow_module_analyze",
                        "rea_self_heal",
        "workflow_module_test",
        "workflow_module_install_and_check",
        "workflow_frontend_visual_implement",
        "odoo_query",
        "odoo_search_read",
        "odoo_read_group",
        "odoo_fields_get",
        "odoo_list_models",
        "odoo_record_create",
        "odoo_record_write",
        "odoo_record_action",
        "odoo_flow_test",
        "odoo_mutation_audit",
    "approvals_approve",
        "artifacts_read",
        "workflow_resume_job",
        "jobs_list",
        "jobs_get",
        "odoosh_error_pack",
    } | WS_TOOLS

    called = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "connector_health", "arguments": {}},
        },
    )
    assert called.status_code == 200
    assert called.json()["result"]["isError"] is False
    assert called.json()["result"]["structuredContent"]["status"] == "ok"


def test_full_verify_failure_creates_failed_job_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _make_valid_module(tmp_path)
    # Make the view invalid after creating the base module.
    (tmp_path / "x_module" / "views" / "x_views.xml").write_text("<odoo><broken></odoo>", encoding="utf-8")
    client = TestClient(app)
    response = client.post(
        "/workflow/full-verify",
        headers=_auth(),
        json={
            "project_path": str(tmp_path),
            "module_name": "x_module",
            "run_pytest": False,
            "include_git_status": False,
            "include_git_diff": False,
            "include_odoosh_errors": False,
            "include_odoo_module_status": False,
            "take_browser_screenshot": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    job_id = payload["job_id"]
    job = client.get(f"/jobs/{job_id}", headers=_auth())
    assert job.status_code == 200
    assert job.json()["status"] == "failed"
    audit = client.get("/jobs/audit/events", headers=_auth())
    assert audit.status_code == 200
    assert any(event["action"] == "job.update" for event in audit.json())


@pytest.mark.integration
def test_protected_branch_push_marks_failed_approval_when_command_fails_even_when_global_approval_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)
    project = tmp_path / "repo"
    _init_repo(project, branch="main")
    client = TestClient(app)

    approved = JsonStore().create_approval(ApprovalRecord(action="git.push", summary="push main", status=ApprovalStatus.approved))
    first = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "main", "approval_id": approved.id})
    assert first.status_code == 200
    assert JsonStore().get_approval(approved.id).status == ApprovalStatus.failed

    second = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "main", "approval_id": approved.id})
    assert second.status_code == 403
    assert "failed" in second.text


def test_protected_branch_matching_is_case_and_ref_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "protected_branches_raw", "main,production")
    project = tmp_path / "repo"
    _init_repo(project, branch="dev")
    client = TestClient(app)

    response = client.post("/git/push", headers=_auth(), json={"project_path": str(project), "remote": "origin", "branch": "refs/heads/Main"})
    assert response.status_code == 409
    assert response.json()["detail"]["approval_required"] is True


def test_absolute_patch_paths_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    _init_repo(project, branch="dev")
    client = TestClient(app)

    absolute_header_patch = """diff --git /tmp/evil.txt b/README.md\n--- /tmp/evil.txt\n+++ b/README.md\n@@ -1 +1 @@\n-initial\n+changed\n"""
    response = client.post("/repo/apply-patch", headers=_auth(), json={"project_path": str(project), "patch": absolute_header_patch, "check_only": True})
    assert response.status_code == 400
    assert "Unsafe path" in response.text

    absolute_plus_patch = """diff --git a/README.md b/README.md\n--- a/README.md\n+++ /tmp/evil.txt\n@@ -1 +1 @@\n-initial\n+changed\n"""
    response2 = client.post("/repo/apply-patch", headers=_auth(), json={"project_path": str(project), "patch": absolute_plus_patch, "check_only": True})
    assert response2.status_code == 400
    assert "Unsafe path" in response2.text


def test_tool_bridge_respects_write_approval_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/tools/call",
        headers=_auth(),
        json={"name": "repo.write", "arguments": {"project_path": str(project), "relative_path": "a.txt", "content": "blocked"}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert "approval_required" in payload["error"]
    assert not (project / "a.txt").exists()
    monkeypatch.setattr(settings, "require_approval", False)



@pytest.mark.integration
def test_write_many_atomic_applies_all_files_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    _init_repo(project, branch="dev")
    client = TestClient(app)

    failing = client.post(
        "/repo/write-many-atomic",
        headers=_auth(),
        json={
            "project_path": str(project),
            "files": [
                {"relative_path": "a.txt", "content": "A\n"},
                {"relative_path": "missing/b.txt", "content": "B\n", "create_dirs": False},
            ],
        },
    )
    assert failing.status_code == 200
    assert failing.json()["exit_code"] != 0
    assert not (project / "a.txt").exists()
    assert not (project / "missing" / "b.txt").exists()

    ok = client.post(
        "/repo/write-many-atomic",
        headers=_auth(),
        json={
            "project_path": str(project),
            "files": [
                {"relative_path": "a.txt", "content": "A\n"},
                {"relative_path": "missing/b.txt", "content": "B\n"},
            ],
        },
    )
    assert ok.status_code == 200
    assert ok.json()["exit_code"] == 0
    assert (project / "a.txt").read_text(encoding="utf-8") == "A\n"
    assert (project / "missing" / "b.txt").read_text(encoding="utf-8") == "B\n"
    branches = subprocess.run(["git", "branch", "--list", "atomic-edit-*"], cwd=project, text=True, capture_output=True, check=True)
    assert branches.stdout.strip() == ""


@pytest.mark.integration
def test_apply_patch_atomic_check_only_and_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    _init_repo(project, branch="dev")
    patch = """diff --git a/README.md b/README.md\nindex e79c5e8..b6fc4c6 100644\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-initial\n+atomic\n"""
    client = TestClient(app)
    checked = client.post("/repo/apply-patch-atomic", headers=_auth(), json={"project_path": str(project), "patch": patch, "check_only": True})
    assert checked.status_code == 200
    assert checked.json()["exit_code"] == 0
    assert (project / "README.md").read_text(encoding="utf-8") == "initial\n"

    applied = client.post("/repo/apply-patch-atomic", headers=_auth(), json={"project_path": str(project), "patch": patch})
    assert applied.status_code == 200
    assert applied.json()["exit_code"] == 0
    assert (project / "README.md").read_text(encoding="utf-8") == "atomic\n"
