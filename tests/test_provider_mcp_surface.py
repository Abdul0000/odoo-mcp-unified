import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import provider_tool_names_safe
from odoo_agent_server.schemas import OdooCodeChangeRequest, OdooCodeFileChange

_PROVIDER_TOOLS = {
    "ws_open",
    "ws_status",
    "ws_outline",
    "ws_scaffold",
    "ws_extend",
    "ws_symbol",
    "ws_read",
    "ws_grep",
    "ws_edit",
    "ws_diff",
    "ws_lint",
    "ws_commit",
    "ws_discard",
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
}


def _mcp_tools_list():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    return payload["result"]["tools"]


def test_provider_mcp_tools_list_exposes_core_read_tools_on_single_mcp_endpoint():
    tools = _mcp_tools_list()
    names = {tool["name"] for tool in tools}
    assert _PROVIDER_TOOLS <= names
    assert len(names) == len(_PROVIDER_TOOLS)
    assert set(provider_tool_names_safe()) == names


def test_provider_exposes_all_read_only_rea_analysis_tools():
    tools = {tool["name"]: tool for tool in _mcp_tools_list()}
    rea_read_tools = {
        "rea_xray_module",
        "rea_upstream_compare",
        "rea_runtime_trace",
        "rea_api_inspect",
        "rea_impact_predict",
    }

    assert rea_read_tools | {"rea_self_heal"} <= set(tools)
    for name in rea_read_tools:
        tool = tools[name]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["idempotentHint"] is True


def test_rea_xray_provider_description_mentions_phase9_dependency_intelligence():
    tools = {tool["name"]: tool for tool in _mcp_tools_list()}
    description = tools["rea_xray_module"]["description"]
    assert "reverse dependencies" in description
    assert len(description) <= 220

def test_rea_xray_exposes_optional_live_registry_switch():
    from odoo_agent_server.schemas import ReaXrayRequest

    request = ReaXrayRequest(module_name="example_addon")
    assert request.include_runtime is True
    static_request = ReaXrayRequest(module_name="example_addon", include_runtime=False)
    assert static_request.include_runtime is False


def test_rea_analysis_tools_accept_stateless_workspace_ids():
    tools = {tool["name"]: tool for tool in _mcp_tools_list()}
    for name in (
        "rea_xray_module",
        "rea_upstream_compare",
        "rea_api_inspect",
        "rea_impact_predict",
    ):
        properties = tools[name]["inputSchema"]["properties"]
        assert "ws_id" in properties
        assert properties["ws_id"]["type"] == "string"


def test_functional_mutation_tools_advertise_side_effects_and_idempotency():
    tools = {tool["name"]: tool for tool in _mcp_tools_list()}

    for name in ("odoo_record_create", "odoo_record_write", "odoo_record_action", "odoo_flow_test"):
        annotations = tools[name]["annotations"]
        assert annotations["readOnlyHint"] is False
        assert annotations["destructiveHint"] is True
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is True

    assert tools["odoo_mutation_audit"]["annotations"]["readOnlyHint"] is True


def test_dynamic_flow_step_contract_is_exposed_to_remote_providers():
    flow = next(tool for tool in _mcp_tools_list() if tool["name"] == "odoo_flow_test")
    step = flow["inputSchema"]["properties"]["steps"]["items"]["properties"]

    assert {
        "name", "operation", "model", "records", "values", "ids", "ids_from",
        "action", "arguments", "keyword_arguments", "expected_values", "verify_fields",
    } <= set(step)


def test_provider_mcp_workflow_schema_contains_autopatch_fields():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_code_change")
    properties = workflow["inputSchema"]["properties"]
    for field in (
        "module_hint",
        "task_type",
        "intent",
        "target_hint",
        "change_request",
        "patch_strategy",
        "target_text",
        "style",
        "allow_autopatch",
    ):
        assert field in properties

    descriptions = [
        value.get("description", "")
        for value in properties.values()
        if isinstance(value, dict)
    ]
    assert all(len(description) <= 80 for description in descriptions)
    assert "investigation_diagnosis" in properties
    assert "handoff" in workflow["outputSchema"]["properties"]


def test_provider_debug_workflow_exposes_persisted_replay_contract():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_debug_and_fix")
    properties = workflow["inputSchema"]["properties"]

    for field in (
        "issue_description",
        "expected_result",
        "debug_session_id",
        "operation_id",
        "correlation_id",
        "runtime_evidence",
        "verification_evidence",
        "verification_assertion",
        "runtime_replay",
        "writes",
        "job_id",
        "reproduction_passed",
        "expected_result_verified",
        "max_fix_cycles",
        "max_surgical_files",
        "max_surgical_content_chars",
        "failure_diagnosis",
    ):
        assert field in properties
    assert properties["max_fix_cycles"]["maximum"] == 5
    assert properties["writes"]["maxItems"] == 50
    assertion_properties = properties["verification_assertion"]["properties"]
    assert {
        "model", "domain", "fields", "expected_values",
        "expected_count_min", "expected_count_max", "match_mode",
    } <= set(assertion_properties)
    replay_properties = properties["runtime_replay"]["properties"]
    assert {
        "model", "record_ids", "method", "args", "kwargs", "context",
        "user_id", "company_id", "commit", "confirm_mutation", "timeout_seconds",
    } <= set(replay_properties)

    output = workflow["outputSchema"]["properties"]
    assert "debug_session" in output
    assert "deploy_confirmed" in output
    assert "reproduction_passed" in output
    assert "expected_result_verified" in output
    assert "claim_status" in output
    assert "truth_gate" in output
    assert "diagnosis" in output
    assert "handoff" in output


def test_provider_workflow_accepts_up_to_fifty_atomic_writes():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_code_change")
    assert workflow["inputSchema"]["properties"]["writes"]["maxItems"] == 50

    writes = [OdooCodeFileChange(relative_path=f"x_module/views/view_{index}.xml", content="<odoo/>") for index in range(50)]
    request = OdooCodeChangeRequest(module_name="x_module", task_description="Update module XML", writes=writes)
    assert len(request.writes) == 50
    with pytest.raises(ValidationError):
        OdooCodeChangeRequest(module_name="x_module", task_description="Update module XML", writes=[*writes, writes[0]])


def test_provider_tool_contract_stays_within_context_budget():
    import json

    tools = _mcp_tools_list()
    # Budget re-measured after adding the explicit approval-continuation action. Keep headroom small so growth is noticed.
    assert len(json.dumps(tools, ensure_ascii=False)) < 80_100


def test_provider_tool_descriptions_are_complete_and_semantically_compact():
    tools = {tool["name"]: tool for tool in _mcp_tools_list()}
    required = {
        "client_check": "validation passes",
        "workflow_odoo_code_change": "needs_writes",
        "workflow_odoo_debug_and_fix": "verified_fixed=true",
        "workflow_module_install_and_check": "not permission to edit code",
        "workflow_frontend_visual_implement": "without editing Odoo core",
    }
    for name, marker in required.items():
        description = tools[name]["description"]
        assert len(description) <= 500
        assert description.endswith(".")
        assert marker in description


def test_provider_workflow_exposes_safe_large_module_batches():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_code_change")
    properties = workflow["inputSchema"]["properties"]

    assert {"operation_id", "batch_index", "total_batches"} <= set(properties)
    assert properties["total_batches"]["maximum"] == 20
    request = OdooCodeChangeRequest(
        module_name="x_module",
        task_description="Implement the first dependency-ordered module batch",
        operation_id="large-module-001",
        batch_index=0,
        total_batches=2,
        writes=[OdooCodeFileChange(relative_path="x_module/models/a.py", content="VALUE = 1\n")],
    )
    assert request.total_batches == 2
    with pytest.raises(ValidationError, match="operation_id"):
        request.model_copy(update={"operation_id": None}).model_validate(
            request.model_copy(update={"operation_id": None}).model_dump()
        )
    with pytest.raises(ValidationError, match="batch_index"):
        OdooCodeChangeRequest(
            module_name="x_module",
            task_description="Invalid final index",
            operation_id="large-module-002",
            batch_index=2,
            total_batches=2,
            writes=[OdooCodeFileChange(relative_path="x_module/models/a.py", content="VALUE = 1\n")],
        )


def test_provider_install_fast_path_dispatches_without_planning(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "running", "job_id": "install-job", "next_call": {"tool": "jobs.get"}}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "workflow_module_install_and_check",
                "arguments": {"client_name": "Test 18", "module_name": "iot_device_management"},
            },
        },
    )

    assert response.status_code == 200
    assert captured["name"] == "workflow.module_install_and_check"
    assert captured["arguments"]["module_name"] == "iot_device_management"


def test_provider_code_change_forces_durable_nonblocking_execution(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured.update(name=name, arguments=arguments)
        return {
            "status": "running",
            "job_id": "code-job",
            "completion_required": True,
            "success_allowed": False,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": "code-job"}},
        }

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "workflow_odoo_code_change",
                "arguments": {"module_name": "x_module", "task_description": "Update the module"},
            },
        },
    )
    assert response.status_code == 200
    assert captured["name"] == "workflow.odoo_code_change"
    assert captured["arguments"]["wait_for_result"] is False


def test_provider_workflow_preserves_long_module_requirements():
    request = OdooCodeChangeRequest(
        module_name="x_module",
        create_module=True,
        task_description="Build a comprehensive module. " + ("Detailed requirement. " * 300),
    )
    assert len(request.task_description) > 2000


def test_provider_health_exposes_live_identity_and_workflow_progress():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "connector_health", "arguments": {}},
        },
    )

    assert response.status_code == 200
    structured = response.json()["result"]["structuredContent"]
    assert structured["runtime"]["service_version"]
    assert structured["runtime"]["commit_sha"]
    assert structured["runtime"]["prompt_sha256"]
    assert {"active_count", "active", "recent"} <= set(structured["workflow_progress"])
