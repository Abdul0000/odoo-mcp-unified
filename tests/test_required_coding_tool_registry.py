from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app, tool_registry_status
from odoo_agent_server.routers.tools import internal_tool_name


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_required_coding_workflows_are_registered():
    status = tool_registry_status()
    assert status["coding_workflow_ready"] is True
    assert status["missing_tools"] == []
    assert "workflow_odoo_code_change" in status["registered_tools"]


def test_health_reports_tool_registry():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["coding_workflow_ready"] is True
    assert payload["missing_tools"] == []


def test_namespaced_workflow_aliases_resolve():
    assert internal_tool_name("workflow_odoo_code_change") == "workflow.odoo_code_change"
    assert internal_tool_name("odoo_mcp.workflow_odoo_code_change") == "workflow.odoo_code_change"
    assert internal_tool_name("odoo_mcp_workflow_odoo_code_change") == "workflow.odoo_code_change"
