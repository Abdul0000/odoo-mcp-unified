import json

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, validate

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import (
    _ensure_provider_control_envelope,
    _normalize_remote_structured,
    _provider_safe_next_calls,
    _tool_to_mcp,
    _tools_for_profile,
)
from odoo_agent_server.routers.tools import _tool_output_schemas, tool_output_schema
from odoo_agent_server.schemas import JobRecord, JobStatus, OdooResult
from odoo_agent_server.services.store_service import JsonStore


def test_output_schemas_resolve_for_pydantic_backed_tools():
    schemas = _tool_output_schemas()
    # Real, known pydantic-model-backed tools must resolve.
    for name in ("workflow.coding_task_plan", "git.status", "repo.read", "workflow.capability_check"):
        assert name in schemas
        schema = schemas[name]
        assert schema.get("type") == "object"
        assert "properties" in schema


def test_plain_dict_tools_receive_truthful_dynamic_output_schema():
    schema = tool_output_schema("connector.health")
    assert schema["type"] == "object"
    assert "status" in schema["properties"]
    assert "service" in schema["properties"]
    assert schema["additionalProperties"] is True


def test_tool_output_schema_helper_resolves_aliases_and_returns_deepcopy():
    first = tool_output_schema("workflow.coding_task_plan")
    assert first is not None
    first["properties"]["mutated"] = {"type": "string"}
    second = tool_output_schema("workflow.coding_task_plan")
    assert "mutated" not in second["properties"]


def test_unresolvable_tool_receives_safe_generic_object_schema():
    schema = tool_output_schema("not.a.real.tool")
    assert schema["type"] == "object"
    assert {"status", "message", "error", "next_call"} <= set(schema["properties"])


def test_mcp_tools_list_includes_output_schema_when_available():
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    resp = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    assert tools, "expected at least one tool in tools/list"
    assert all("outputSchema" in tool for tool in tools)
    for tool in tools:
        assert tool["outputSchema"].get("type") == "object"


def test_all_provider_profiles_expose_output_schema_for_every_tool():
    for profile in (None, "provider", "chatgpt", "claude", "grok", "qwen"):
        tools = [_tool_to_mcp(tool, profile) for tool in _tools_for_profile(profile)]
        assert tools and all("outputSchema" in tool for tool in tools)
        for tool in tools:
            Draft202012Validator.check_schema(tool["outputSchema"])


def test_provider_output_schemas_require_the_shared_control_envelope():
    required = {
        "status", "outcome", "terminal", "completion_required", "success_allowed",
        "next_call", "input_required", "error",
    }
    for profile in ("provider", "chatgpt", "claude", "grok", "qwen"):
        for tool in (_tool_to_mcp(item, profile) for item in _tools_for_profile(profile)):
            assert required <= set(tool["outputSchema"]["required"])


def test_provider_runtime_result_validates_against_advertised_schema(monkeypatch):
    async def fake_dispatch(_name, _arguments):
        return {"status": "needs_writes", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    tools = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()["result"]["tools"]
    schema = next(tool["outputSchema"] for tool in tools if tool["name"] == "workflow_odoo_code_change")
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "workflow_odoo_code_change",
                "arguments": {"module_name": "x_module", "task_description": "Update the module"},
            },
        },
    )
    structured = response.json()["result"]["structuredContent"]
    validate(instance=structured, schema=schema)
    assert structured["state_class"] == "blocked"
    assert structured["completion_required"] is False
    assert structured["input_required"] is False
    assert structured["success_allowed"] is False


def test_control_envelope_classifies_terminal_and_active_states_without_status_substrings():
    cases = [
        ({"status": "module_not_found", "success_allowed": False}, "blocked", True),
        ({"status": "debug_implemented_unverified", "success_allowed": False}, "blocked", True),
        ({"status": "debug_max_cycles_reached", "success_allowed": False}, "blocked", True),
        ({"status": "batch_verification_failed", "completion_required": True,
          "next_call": {"tool": "workflow.odoo_code_change", "arguments": {}}}, "action_required", False),
        ({"status": "needs_clarification", "completion_required": True,
          "next_call": {"tool": "workflow.odoo_code_change", "required_generated_arguments": ["clarification_answer"]}},
         "input_required", False),
    ]
    for source, expected_state, expected_terminal in cases:
        result = _ensure_provider_control_envelope(source)
        assert result["state_class"] == expected_state
        assert result["terminal"] is expected_terminal
        assert result["completion_required"] is (not expected_terminal)
        assert result["success_allowed"] is False


def test_generic_running_and_warning_jobs_have_consistent_lifecycle_contract():
    running = _ensure_provider_control_envelope(
        JobRecord(job_type="odoo.module.deploy", status=JobStatus.running).model_dump(mode="json")
    )
    assert running["state_class"] == "running"
    assert running["terminal"] is False
    assert running["next_call"]["tool"] == "jobs.get"

    warning = _ensure_provider_control_envelope(
        JobRecord(job_type="new.future.job", status=JobStatus.warning).model_dump(mode="json")
    )
    assert warning["state_class"] == "blocked"
    assert warning["terminal"] is True
    assert warning["success_allowed"] is False


def test_nested_async_odoo_result_lifts_poll_contract_to_top_level():
    wrapped = OdooResult(
        status="failed",
        error="Background job started but is not complete.",
        result={
            "status": "running",
            "job_id": "install-job",
            "completion_required": True,
            "success_allowed": False,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": "install-job"}},
        },
    )
    result = _ensure_provider_control_envelope(wrapped.model_dump(mode="json"))
    assert result["state_class"] == "running"
    assert result["job_id"] == "install-job"
    assert result["terminal"] is False
    assert result["next_call"]["arguments"]["job_id"] == "install-job"
    assert result["error"] is None


def test_nonterminal_state_without_call_or_input_closes_as_blocker():
    result = _ensure_provider_control_envelope({
        "status": "investigation_required",
        "state_class": "action_required",
        "completion_required": True,
        "success_allowed": False,
    })
    assert result["state_class"] == "blocked"
    assert result["terminal"] is True
    assert result["input_required"] is False


def test_description_result_field_remains_a_valid_property_schema():
    tool = next(
        _tool_to_mcp(item, "chatgpt")
        for item in _tools_for_profile("chatgpt")
        if item.name == "skills.read"
    )
    assert tool["outputSchema"]["properties"]["description"] == {"type": "string"}


def test_job_output_schema_uses_serialization_fields():
    schema = tool_output_schema("jobs.get")
    assert {"deploy_confirmed", "completion_required", "next_call", "terminal_action"} <= set(schema["properties"])


def test_large_structured_result_preserves_array_types_and_workflow_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    original_next_call = {
        "tool": "jobs.get",
        "arguments": {"job_id": "job-1", "wait_for_result": True},
    }
    source = _ensure_provider_control_envelope(
        {
            "status": "running",
            "job_id": "job-1",
            "completion_required": True,
            "success_allowed": False,
            "next_call": original_next_call,
            "records": [{"id": index, "value": "x" * 200} for index in range(100)],
        }
    )

    normalized = _provider_safe_next_calls(_normalize_remote_structured(source, "jobs.get"))

    assert isinstance(normalized["records"], list)
    assert len(normalized["records"]) == 50
    assert normalized["records_count"] == 100
    assert normalized["records_truncated"] is True
    assert normalized["next_call"]["tool"] == "jobs_get"
    assert normalized["next_call"]["arguments"]["job_id"] == "job-1"
    assert normalized["details_next_call"]["tool"] == "artifacts_read"
    assert normalized["completion_required"] is True
    assert normalized["success_allowed"] is False
    assert normalized["terminal"] is False
    assert json.dumps(normalized)


def test_artifact_read_schema_matches_store_payload_fields():
    properties = tool_output_schema("artifacts.read")["properties"]
    assert {
        "artifact_id", "kind", "source_tool", "created_at", "bytes", "offset",
        "returned_chars", "next_offset", "text",
    } <= set(properties)
    assert "content" not in properties
    assert "truncated" not in properties


def test_provider_artifact_read_result_validates_against_advertised_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    artifact = JsonStore().create_artifact("structured", "test.source", {"value": "exact"})
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    tools = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()["result"]["tools"]
    schema = next(tool["outputSchema"] for tool in tools if tool["name"] == "artifacts_read")
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "artifacts_read",
                "arguments": {"artifact_id": artifact["artifact_id"], "offset": 0, "max_chars": 12000},
            },
        },
    )
    structured = response.json()["result"]["structuredContent"]
    validate(instance=structured, schema=schema)
    assert structured["text"]
    assert structured["artifact_id"] == artifact["artifact_id"]
