"""Regression tests for the remote-MCP / Claude Code gap fixes.

Covers: callable tool names in next_call, the lean provider manifest, the
terminal/completion invariant, validation-error recovery, live model discovery
tools, and loopback OAuth redirects.
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import mcp as mcp_router
from odoo_agent_server.schemas import OdooResult
from odoo_agent_server.services import odoo_service
from odoo_agent_server.services.oauth_service import _validate_redirect_uri, is_loopback_host

_STRICT_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _client() -> TestClient:
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _tools(client: TestClient, query: str = "") -> list[dict]:
    response = client.post(
        f"/mcp{query}",
        headers=_headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    return response.json()["result"]["tools"]


def test_all_advertised_tool_names_are_strict_mcp_safe():
    names = {tool["name"] for tool in _tools(_client())}
    assert names
    assert all(_STRICT_NAME.match(name) for name in names), names


def test_new_live_discovery_tools_are_advertised_and_dispatched(monkeypatch):
    client = _client()
    names = {tool["name"] for tool in _tools(client)}
    assert {"odoo_fields_get", "odoo_list_models"} <= names

    captured: dict[str, tuple] = {}

    def fake_fields(self, model, limit=500):
        captured["fields"] = (model, limit)
        return OdooResult(status="ok", result=[])

    def fake_models(self, search=None, include_transient=True, limit=200):
        captured["models"] = (search, include_transient, limit)
        return OdooResult(status="ok", result=[])

    monkeypatch.setattr(odoo_service.OdooService, "fields_get", fake_fields)
    monkeypatch.setattr(odoo_service.OdooService, "list_models", fake_models)

    fields = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "odoo_fields_get", "arguments": {"model": "sale.order", "limit": 10}},
        },
    )
    assert fields.json()["result"]["structuredContent"]["status"] == "ok"
    assert captured["fields"] == ("sale.order", 10)

    models = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "odoo_list_models",
                "arguments": {"search": "sale", "include_transient": False, "limit": 5},
            },
        },
    )
    assert models.json()["result"]["structuredContent"]["status"] == "ok"
    assert captured["models"] == ("sale", False, 5)


def test_minimal_profile_is_a_strict_smaller_subset():
    client = _client()
    full = {tool["name"] for tool in _tools(client)}
    minimal = {tool["name"] for tool in _tools(client, "?profile=minimal")}
    assert minimal < full
    assert {"workflow_odoo_code_change", "jobs_get", "odoo_fields_get"} <= minimal
    assert "odoo_record_create" not in minimal


def test_next_call_tool_is_always_flattened_to_a_callable_name():
    flattened = mcp_router._provider_safe_next_calls(
        {"next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": "x"}}}
    )
    assert flattened["next_call"]["tool"] == "workflow_resume_job"
    assert _STRICT_NAME.match(flattened["next_call"]["tool"])


def test_terminal_result_cannot_require_a_call():
    terminal = mcp_router._ensure_provider_control_envelope({"status": "passed"})
    assert terminal["terminal"] is True
    assert terminal["completion_required"] is False
    assert terminal["next_call"] is None

    explicit = mcp_router._ensure_provider_control_envelope(
        {
            "status": "passed",
            "completion_required": False,
            "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": "x"}},
        }
    )
    assert explicit["terminal"] is True
    assert explicit["next_call"] is None
    assert explicit["recovery_call"]["tool"] == "workflow.resume_job"


def test_non_terminal_job_exposes_next_and_resume_calls():
    result = mcp_router._ensure_provider_control_envelope({"status": "running", "job_id": "job-1"})
    assert result["terminal"] is False
    assert result["completion_required"] is True
    assert result["next_call"]["tool"] == "jobs.get"
    assert result["resume_next_call"]["tool"] == "workflow.resume_job"


def test_non_provider_continuation_is_rewritten_to_a_callable_tool():
    result = mcp_router._ensure_callable_continuations(
        {"next_call": {"tool": "workflow.safe_deploy_module", "arguments": {"job_id": "j-1"}}}
    )
    assert result["next_call"]["tool"] == "jobs_get"
    assert result["next_call"]["requested_tool"] == "workflow.safe_deploy_module"

    no_job = mcp_router._ensure_callable_continuations(
        {"recovery_call": {"tool": "repo.read", "arguments": {}}}
    )
    assert no_job["recovery_call"]["tool"] == "workflow_agent_instructions"


def test_validation_error_carries_a_callable_next_call():
    response = _client().post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "odoo_fields_get", "arguments": {"bad": 1}},
        },
    )
    body = response.json()["result"]
    assert body["isError"] is True
    structured = body["structuredContent"]
    assert structured["next_call"]["tool"] == "workflow_agent_instructions"
    assert structured["state_class"] == "action_required"
    assert structured["terminal"] is False


def test_loopback_oauth_redirects_are_allowed_and_external_http_is_not():
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    _validate_redirect_uri("http://localhost:53682/callback", allow_chatgpt_defaults=True)
    _validate_redirect_uri("http://127.0.0.1:53682/callback", allow_chatgpt_defaults=True)
    with pytest.raises(HTTPException):
        _validate_redirect_uri("http://evil.example.com/callback", allow_chatgpt_defaults=True)
    with pytest.raises(HTTPException):
        _validate_redirect_uri("http://user:pass@localhost:53682/callback", allow_chatgpt_defaults=True)
