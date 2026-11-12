from tests.conftest import WS_TOOLS
import pytest
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


_PROVIDER_TOOLS = {
    "connector_health",
    "skills_list",
    "skills_read",
    "client_check",
    "workflow_agent_instructions",
    "workflow_odoo_code_change",
    "workflow_resume_continuation",
    "workflow_odoo_debug_and_fix",
    "workflow_module_analyze",
    "rea_xray_module",
    "rea_upstream_compare",
    "rea_runtime_trace",
    "rea_api_inspect",
    "rea_impact_predict",
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



def _tool_names(path="/mcp"):
    client = TestClient(app)
    response = client.post(path, headers=_auth(), json=_mcp("tools/list"))
    assert response.status_code == 200
    return {tool["name"] for tool in response.json()["result"]["tools"]}


@pytest.mark.slow
def test_all_provider_mcp_endpoints_use_same_safe_core_read_surface():
    for path in ("/mcp", "/chatgpt/mcp", "/grok/mcp", "/claude/mcp"):
        assert _tool_names(path) == _PROVIDER_TOOLS


def test_provider_profile_hides_direct_repo_git_and_deploy_tools():
    names = _tool_names("/chatgpt/mcp")
    forbidden = {
        "repo_read", "repo_search", "repo_write", "repo_read_range",
        "workflow_scope_advice", "workflow_safe_deploy_module",
        "git_push", "odoo_module_deploy", "odoosh_module_update",
    }
    assert not (names & forbidden)


def test_hidden_direct_tool_call_is_blocked_with_next_action():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "repo_read",
                "arguments": {"project_path": "/tmp", "relative_path": "x.py"},
            },
        ),
    )
    assert response.status_code == 200
    structured = response.json()["result"]["structuredContent"]
    assert structured["status"] == "tool_profile_blocked"
    assert structured["success_allowed"] is False
    assert structured["next_call"]["tool"] == "workflow_odoo_code_change"


def test_jobs_get_uses_watchdog_bounded_provider_poll(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "running", "job_id": arguments["job_id"]}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {"name": "jobs_get", "arguments": {"job_id": "job-1", "wait_for_result": True, "wait_timeout_seconds": 999}}),
    )
    assert response.status_code == 200
    assert captured["name"] == "jobs.get"
    assert captured["arguments"]["wait_for_result"] is True
    assert captured["arguments"]["wait_timeout_seconds"] == settings.workflow_watchdog_wait_cap_seconds
    assert captured["arguments"]["poll_interval_seconds"] == 1.0


def test_root_provider_tools_metadata_matches_mcp_surface():
    client = TestClient(app)
    root = client.get("/")
    assert root.status_code == 200
    assert set(root.json()["provider_tools"]) == _PROVIDER_TOOLS
