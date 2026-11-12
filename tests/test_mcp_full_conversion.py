from tests.conftest import WS_TOOLS
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.tools import strict_safe_tool_name, list_tools


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



def test_mcp_initialize_and_core_read_tools_list():
    client = TestClient(app)
    initialized = client.post("/mcp", headers=_auth(), json=_mcp("initialize"))
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"]

    response = client.post("/mcp", headers=_auth(), json=_mcp("tools/list", request_id=2))
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == _PROVIDER_TOOLS
    assert all("inputSchema" in tool for tool in tools)


def test_rest_tools_keep_full_admin_surface_but_mcp_is_curated():
    client = TestClient(app)
    rest = client.get("/tools/list", headers=_auth())
    assert rest.status_code == 200
    rest_names = {strict_safe_tool_name(tool["name"]) for tool in rest.json()}
    assert "repo_read" in rest_names
    assert "workflow_safe_deploy_module" in rest_names

    mcp = client.post("/mcp", headers=_auth(), json=_mcp("tools/list"))
    assert mcp.status_code == 200
    mcp_names = {tool["name"] for tool in mcp.json()["result"]["tools"]}
    assert mcp_names == _PROVIDER_TOOLS
    assert mcp_names < rest_names


def test_provider_tool_call_dispatches_composite_tool(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "needs_writes", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "workflow_odoo_code_change",
                "arguments": {
                    "project_path": "/tmp/repo",
                    "module_name": "x_module",
                    "task_description": "inspect change",
                },
            },
        ),
    )
    assert response.status_code == 200
    assert captured["name"] == "workflow.odoo_code_change"
    assert response.json()["result"]["structuredContent"]["status"] == "needs_writes"


def test_provider_validation_errors_are_agent_friendly():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {"name": "workflow_odoo_code_change", "arguments": {"module_name": "bad name", "task_description": "x"}}),
    )
    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert payload["structuredContent"]["error_type"] == "tool_input_invalid"


def test_provider_unexpected_errors_are_structured_and_terminal(monkeypatch):
    async def fail_dispatch(_name, _arguments):
        raise RuntimeError("connector exploded")

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fail_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {"name": "connector_health", "arguments": {}}),
    )
    payload = response.json()["result"]
    structured = payload["structuredContent"]
    assert payload["isError"] is True
    assert structured["outcome"] == "failed"
    assert structured["terminal"] is True
    assert structured["completion_required"] is False
    assert structured["success_allowed"] is False
    assert structured["error_code"] == "RuntimeError"


def test_all_rest_tool_names_still_have_safe_mcp_aliases():
    for tool in list_tools(strict_names=False):
        safe = strict_safe_tool_name(tool.name)
        assert safe
        assert len(safe) <= 64
