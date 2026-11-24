"""Compatibility transports preserve credentials and JSON-RPC notification semantics."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import mcp as bridge
from odoo_agent_server.services.runtime_identity_service import SERVICE_VERSION

PATHS = ["/mcp", "/claude/mcp", "/chatgpt/mcp", "/grok/mcp",
         "/qwen/mcp?access_token=compat-test", "/qwen/mcp/compat-test"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bridge.QwenTokenService, "validate", lambda self, token: token == "compat-test")
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def auth():
    return {"Authorization": "Bearer " + settings.api_token}


@pytest.mark.parametrize("path", PATHS)
def test_compatibility_uses_configured_host_origin_policy(client, monkeypatch, path):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post(path, headers=auth(), json=payload).status_code == 200
    for extra, expected in [({"Host": "untrusted.example.com"}, 421),
                            ({"Origin": "https://untrusted.example.com"}, 403)]:
        assert client.post(path, headers={**auth(), **extra}, json=payload).status_code == expected
    monkeypatch.setattr(settings, "mcp_allowed_hosts", ["trusted.example.com"])
    monkeypatch.setattr(settings, "mcp_allowed_origins", ["https://inspector.example.com"])
    assert client.post(path, headers={**auth(), "Host": "trusted.example.com",
                                     "Origin": "https://inspector.example.com"}, json=payload).status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_notifications_have_no_body_and_never_dispatch_tools(client, monkeypatch, path):
    async def forbidden(*args, **kwargs):
        pytest.fail("An ID-less tool message must not dispatch")

    monkeypatch.setattr(bridge, "call_tool_result", forbidden)
    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "connector_health"}},
    ]
    for payload in [*notifications, notifications]:
        result = client.post(path, headers=auth(), json=payload)
        assert result.status_code == 202 and result.content == b""
    request = {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}
    result = client.post(path, headers=auth(), json=[notifications[0], request, notifications[1]])
    assert result.status_code == 200
    assert [row["id"] for row in result.json()] == [0]
    assert result.json()[0]["result"]["tools"]


@pytest.mark.parametrize("path", PATHS)
def test_compatibility_identity_and_invalid_envelopes(client, path):
    result = client.post(path, headers=auth(), json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert result.json()["result"]["serverInfo"]["version"] == SERVICE_VERSION
    for payload in [{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}, [], True]:
        result = client.post(path, headers=auth(), json=payload)
        assert result.json()["error"]["code"] == -32600


@pytest.mark.parametrize("kind", ["bearer", "api_key", "oauth"])
def test_compatibility_keeps_existing_authentication(client, monkeypatch, kind):
    monkeypatch.setattr("odoo_agent_server.auth.OAuthStore.validate_access_token",
                        lambda self, token: token == "compat-oauth")
    headers = {"bearer": auth(), "api_key": {"X-API-Key": settings.api_token},
               "oauth": {"Authorization": "Bearer compat-oauth"}}[kind]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for path in PATHS[:4]:
        assert client.post(path, headers=headers, json=payload).status_code == 200
        assert client.post(path, json=payload).status_code == 401
    assert client.post("/qwen/mcp?access_token=wrong", json=payload).status_code == 401
