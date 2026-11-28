"""Wire-level compatibility and real-client checks for the official SDK adapter."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import LATEST_MODERN_VERSION

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.mcp_sdk import transport_security
from odoo_agent_server.routers import mcp as bridge


def headers(**extra):
    return {"Authorization": f"Bearer {settings.api_token}",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": LATEST_MODERN_VERSION, **extra}


def rpc(client, method, params=None, *, path="/mcp", request_headers=None):
    h = request_headers or headers()
    if method == "initialize":
        h = {k: v for k, v in h.items() if k.lower() != "mcp-protocol-version"}
    params = params or {}
    if h.get("MCP-Protocol-Version") == LATEST_MODERN_VERSION:
        params = {**params, "_meta": modern_meta()}
        h = {**h, "Mcp-Method": method}
        if params.get("name") or params.get("uri"):
            h["Mcp-Name"] = params.get("name") or params["uri"]
    response = client.post(path, headers=h, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    })
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(line[5:].strip() for line in response.text.splitlines() if line.startswith("data:"))
        return httpx.Response(response.status_code, headers=response.headers, content=data, request=response.request)
    return response


def modern_meta():
    return {"io.modelcontextprotocol/protocolVersion": LATEST_MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {}}


@pytest.mark.parametrize("path", ["/mcp", "/claude/mcp", "/chatgpt/mcp", "/grok/mcp"])
def test_standard_handshake_and_catalog_preserve_existing_tool_contract(path):
    with TestClient(app, base_url="http://localhost") as client:
        response = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        }, path=path)
        assert response.status_code == 200
        init = response.json()["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["capabilities"]["tools"]["listChanged"] is False
        assert "resources" in init["capabilities"] and "prompts" in init["capabilities"]
        assert init["serverInfo"]["version"] != "1.8.0"
        standard = rpc(client, "tools/list", path=path, request_headers=headers(**{
            "MCP-Protocol-Version": "2025-06-18", "MCP-Session-Id": response.headers["mcp-session-id"],
        })).json()["result"]["tools"]
        compatibility = rpc(client, "tools/list", path=path, request_headers={
            "Authorization": f"Bearer {settings.api_token}",
        }).json()["result"]["tools"]
        assert standard == compatibility
        assert response.headers.get("X-Request-ID")


@pytest.mark.parametrize("auth", ["bearer", "api_key", "oauth"])
def test_sdk_retains_simple_auth_and_oauth(auth, monkeypatch):
    h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": LATEST_MODERN_VERSION}
    if auth == "bearer":
        h["Authorization"] = f"Bearer {settings.api_token}"
    elif auth == "api_key":
        h["X-API-Key"] = settings.api_token
    else:
        h["Authorization"] = "Bearer test-oauth"
        monkeypatch.setattr("odoo_agent_server.auth.OAuthStore.validate_access_token",
                            lambda self, token: token == "test-oauth")
    with TestClient(app, base_url="http://localhost") as client:
        assert rpc(client, "tools/list", request_headers=h).json()["result"]["tools"]
        denied = rpc(client, "tools/list", request_headers={"Accept": h["Accept"]})
        assert denied.status_code == 401
        assert "resource_metadata=" in denied.headers["WWW-Authenticate"]


def test_sdk_tools_use_shared_dispatch_and_preserve_error_and_continuation(monkeypatch):
    seen = []

    async def dispatch(name, args):
        seen.append((name, args))
        if name == "jobs.get":
            return {"status": "running", "job_id": "job-test", "completion_required": True,
                    "success_allowed": False, "next_call": {"tool": "jobs.get", "arguments": {"job_id": "job-test"}}}
        raise RuntimeError("controlled test failure")

    monkeypatch.setattr(bridge, "_dispatch", dispatch)
    with TestClient(app, base_url="http://localhost") as client:
        running = rpc(client, "tools/call", {"name": "jobs_get", "arguments": {"job_id": "job-test"}}).json()["result"]
        assert running["structuredContent"]["success_allowed"] is False
        assert running["structuredContent"]["next_call"]["tool"] == "jobs_get"
        assert running["isError"] is False
        failed = rpc(client, "tools/call", {"name": "connector_health"}).json()["result"]
        assert failed["isError"] is True
        assert failed["structuredContent"]["success_allowed"] is False
        assert seen[0][0] == "jobs.get" and seen[0][1]["job_id"] == "job-test"
        assert seen[0][1]["wait_timeout_seconds"] <= 30
        assert seen[1] == ("connector.health", {})


def test_sdk_prompts_and_resources_are_readable_and_unknown_uris_cannot_read_files():
    with TestClient(app, base_url="http://localhost") as client:
        prompts = rpc(client, "prompts/list").json()["result"]["prompts"]
        assert {p["name"] for p in prompts} == {"client_check_system_prompt", "mcp_system_prompt"}
        resources = rpc(client, "resources/list").json()["result"]["resources"]
        assert len(resources) == 3
        for resource in resources:
            if resource["name"] == "readiness_incidents":
                incident = rpc(client, "resources/read", {"uri": resource["uri"]}).json()["result"]["contents"][0]
                assert incident["mimeType"] == "application/json"
                assert isinstance(json.loads(incident["text"])["incidents"], list)
                continue
            text = rpc(client, "resources/read", {"uri": resource["uri"]}).json()["result"]["contents"][0]
            prompt = rpc(client, "prompts/get", {"name": resource["name"]}).json()["result"]
            assert text["mimeType"] == "text/plain"
            assert text["text"] == prompt["messages"][0]["content"]["text"]
        assert rpc(client, "resources/read", {"uri": "file:///etc/passwd"}).json()["error"]["code"] == -32602
        assert rpc(client, "prompts/get", {"name": "missing"}).json()["error"]["code"] == -32602


def test_sdk_validates_protocol_envelopes_and_acknowledges_notifications():
    with TestClient(app, base_url="http://localhost") as client:
        notification = client.post("/mcp", headers=headers(), json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert notification.status_code == 202 and not notification.content
        invalid = rpc(client, "tools/call", {"name": "connector_health", "arguments": []})
        assert invalid.status_code == 400 and invalid.json()["error"]["code"] == -32602
        unknown = rpc(client, "missing/method")
        assert unknown.json()["error"]["code"] == -32601
        malformed = client.post("/mcp", headers=headers(), content=b"{bad")
        assert malformed.status_code == 400
        assert rpc(client, "tools/list", {"cursor": "invalid"}).json()["error"]["code"] == -32602


@pytest.mark.parametrize("path", ["/qwen/mcp?access_token=temporary-test-token", "/qwen/mcp/temporary-test-token"])
def test_qwen_standard_transport_retains_url_token_compatibility(path, monkeypatch):
    monkeypatch.setattr(bridge.QwenTokenService, "validate", lambda self, token: token == "temporary-test-token")
    with TestClient(app, base_url="http://localhost") as client:
        h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": LATEST_MODERN_VERSION}
        assert rpc(client, "tools/list", path=path, request_headers=h).json()["result"]["tools"]
        assert rpc(client, "ping", path="/qwen/mcp?access_token=bad", request_headers=h).status_code == 401


def test_sdk_input_validation_and_hidden_tool_guard_do_not_dispatch(monkeypatch):
    async def forbidden(*args):
        pytest.fail("Invalid or hidden tool must not reach service dispatch")

    monkeypatch.setattr(bridge, "_dispatch", forbidden)
    with TestClient(app, base_url="http://localhost") as client:
        malformed = rpc(client, "tools/call", {"name": "connector_health", "arguments": []})
        assert malformed.json()["error"]["code"] == -32602
        hidden = rpc(client, "tools/call", {"name": "git_push", "arguments": {}}).json()["result"]
        assert hidden["structuredContent"]["success_allowed"] is False
        batch = client.post("/mcp", headers=headers(), json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        assert batch.status_code == 400


def test_legacy_sessions_are_bound_to_existing_credential_without_new_auth_scopes(monkeypatch):
    monkeypatch.setattr("odoo_agent_server.auth.OAuthStore.validate_access_token",
                        lambda self, token: token == "different-valid-token")
    with TestClient(app, base_url="http://localhost") as client:
        response = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "session-test", "version": "1"},
        })
        h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
             "MCP-Session-Id": response.headers["mcp-session-id"], "X-API-Key": settings.api_token}
        assert rpc(client, "ping", request_headers=h).json()["result"] == {}
        other = {k: v for k, v in h.items() if k != "X-API-Key"}
        other["Authorization"] = "Bearer different-valid-token"
        assert rpc(client, "ping", request_headers=other).status_code == 404
        assert client.delete("/mcp", headers=h).status_code == 200


def test_sdk_host_and_origin_validation_keep_local_and_configured_deployments(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://agent.example.com")
    monkeypatch.setattr(settings, "mcp_allowed_hosts", ["extra.example.com"])
    monkeypatch.setattr(settings, "mcp_allowed_origins", ["https://inspector.example.com"])
    security = transport_security()
    assert "agent.example.com" in security.allowed_hosts
    assert "extra.example.com" in security.allowed_hosts
    assert "https://inspector.example.com" in security.allowed_origins
    with TestClient(app, base_url="http://localhost") as client:
        assert rpc(client, "ping", request_headers=headers(Host="untrusted.example.com")).status_code == 421
        assert rpc(client, "ping", request_headers=headers(Origin="https://untrusted.example.com")).status_code == 403


@pytest.mark.asyncio
async def test_concurrent_profiles_are_request_scoped(monkeypatch):
    monkeypatch.setattr(settings, "execution_enabled", True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            async def catalog(profile):
                r = await client.post("/mcp", headers=headers(**{"X-MCP-Profile": profile, "Mcp-Method": "tools/list"}),
                                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": modern_meta()}})
                return r.json()["result"]["tools"]
            task, full, execution = await asyncio.gather(catalog("task"), catalog(""), catalog("execution"))
    assert {t["name"] for t in task} == {t["name"] for t in bridge._cached_tools_list("task")}
    assert len(task) < len(execution) < len(full)
    assert len(json.dumps(task)) < len(json.dumps(full)) / 2


@pytest.fixture
def http_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not runner.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert runner.started
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        runner.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_official_client_over_http(http_server, mode):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {settings.api_token}"}) as http:
        async with Client(streamable_http_client(http_server, http_client=http), mode=mode,
                          read_timeout_seconds=10) as client:
            tools = await client.list_tools()
            assert "connector_health" in {tool.name for tool in tools.tools}
            result = await client.call_tool("connector_health", {})
            assert result.is_error is False
            assert result.structured_content["status"] == "ok"
            resource = await client.read_resource("odoo-mcp://instructions/client-check")
            assert "Client Check Gate" in resource.contents[0].text
            prompt = await client.get_prompt("client_check_system_prompt")
            assert prompt.messages[0].content.text == resource.contents[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_sdk_client_cancellation_releases_running_dispatch(http_server, monkeypatch, mode):
    started, cancelled = threading.Event(), threading.Event()

    async def slow_dispatch(name, args):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(bridge, "_dispatch", slow_dispatch)
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {settings.api_token}"}) as http:
        async with Client(streamable_http_client(http_server, http_client=http), mode=mode, read_timeout_seconds=5) as client:
            pending = asyncio.create_task(client.call_tool("connector_health", {}))
            deadline = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            assert started.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            deadline = time.monotonic() + 3
            while not cancelled.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            assert cancelled.is_set()
