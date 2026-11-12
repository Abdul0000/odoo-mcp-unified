from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.services.mcp_operation_guard_service import McpOperationGuardService


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_grok_profile_exposes_only_curated_non_bypass_tools():
    client = TestClient(app)
    response = client.post("/grok/mcp", headers=_auth(), json=_mcp("tools/list"))
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}

    # Required hardened Grok path: one composite coding tool + job polling only.
    assert "workflow_odoo_code_change" in names
    assert "jobs_get" in names
    assert "client_check" in names

    # Hidden because they created Grok loops/bypasses in screenshots.
    assert "git_push" not in names
    assert "odoo_module_deploy" not in names
    assert "odoosh_module_update" not in names
    assert "odoosh_read_user_file" not in names
    assert "workflow_verify_deployed_module" not in names
    # Native source discovery/preflight is server-owned by the composite workflow.
    assert "workflow_list_module_files" not in names
    assert "workflow_find_core_file" not in names
    assert "workflow_frontend_pattern_preflight" not in names
    assert "odoosh_read_core_file" not in names
    assert "repo_read_range" not in names
    assert "artifacts_read" in names
    assert "workflow_resume_job" in names
    assert "odoosh_error_pack" in names
    assert "jobs_list" in names
    assert "workflow_safe_deploy_module" not in names
    assert "workflow_scope_advice" not in names
    assert "repo_read" not in names
    assert "repo_write" not in names


def test_grok_operation_guard_blocks_duplicate_scope_and_backwards_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "ClientA"
    module = project / "sample_website_module"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'x'}\n", encoding="utf-8")

    svc = McpOperationGuardService()
    base = {"client_name": "ClientA", "project_path": str(project), "module_name": "sample_website_module"}

    assert svc.before_tool_call("workflow.start_session", base) is None
    svc.after_tool_call("workflow.start_session", base, {"status": "ok"})

    assert svc.before_tool_call("workflow.scope_advice", base | {"custom_module_name": "sample_website_module"}) is None
    svc.after_tool_call("workflow.scope_advice", base | {"custom_module_name": "sample_website_module"}, {"status": "ok"})
    duplicate = svc.before_tool_call("workflow.scope_advice", base | {"custom_module_name": "sample_website_module"})
    assert duplicate is not None
    assert duplicate["status"] == "duplicate_guard"
    assert duplicate["success_allowed"] is False

    write_args = base | {"relative_path": "sample_website_module/static/src/scss/page_widget.scss"}
    assert svc.before_tool_call("repo.write", write_args) is None
    svc.after_tool_call("repo.write", write_args, {"status": "ok"})

    backwards = svc.before_tool_call("repo.read", base | {"relative_path": "sample_website_module/static/src/scss/page_widget.scss"})
    assert backwards is not None
    assert backwards["status"] == "phase_guard"
    assert backwards["next_call"]["tool"] == "workflow.odoo_code_change"

    repeated_write = svc.before_tool_call("repo.write", write_args)
    assert repeated_write is not None
    assert repeated_write["status"] == "phase_guard"


def test_grok_operation_guard_blocks_tools_after_terminal_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    svc = McpOperationGuardService()
    base = {"client_name": "Test", "project_path": str(project), "module_name": "x"}

    assert svc.before_tool_call("workflow.scope_advice", base) is None
    svc.after_tool_call("workflow.scope_advice", base, {"status": "ok"})
    assert svc.before_tool_call("repo.write", base | {"relative_path": "x/views/a.xml"}) is None
    svc.after_tool_call("repo.write", base | {"relative_path": "x/views/a.xml"}, {"status": "ok"})
    svc.after_tool_call("git.diff", base, {"status": "ok"})
    svc.after_tool_call("workflow.verify_against_core", base, {"status": "ok"})
    svc.after_tool_call("workflow.safe_deploy_module", base, {"status": "running", "job_id": "job1"})
    svc.after_tool_call("jobs.get", base | {"job_id": "job1"}, {"status": "passed", "id": "job1"})

    blocked = svc.before_tool_call("repo.write", base | {"relative_path": "x/views/a.xml"})
    assert blocked is not None
    assert blocked["status"] == "final_guard"
    assert blocked["next_call"] is None
    assert blocked["completion_required"] is False
    assert blocked["terminal_action"] == "stop_and_summarize"


def test_functional_operations_bypass_code_phase_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    service = McpOperationGuardService()
    arguments = {"client_name": "ClientA", "request_id": "sale-flow-001", "flow": "sale_order"}

    assert service.before_tool_call("odoo.flow_test", arguments) is None
    service.after_tool_call("odoo.flow_test", arguments, {"status": "preview"})
    assert service.before_tool_call("odoo.flow_test", arguments) is None
