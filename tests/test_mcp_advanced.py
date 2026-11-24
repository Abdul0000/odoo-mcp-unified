from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequestParams
from mcp.shared.exceptions import MCPError

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server import mcp_sdk
from odoo_agent_server.services.mcp_clarification_service import McpClarificationService
from odoo_agent_server.services.mcp_telemetry import metrics_snapshot, phase_event, logger
from odoo_agent_server.services.workflow_progress_service import WorkflowProgressService
from tests.test_mcp_sdk import headers, modern_meta, rpc, http_server as http_server


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mcp_progress_interval_ms", 50)


def tool_result(body):
    return {"content": [{"type": "text", "text": "Business question"}],
            "structuredContent": mcp_sdk.bridge._ensure_provider_control_envelope(body), "isError": False}


def clarification_post(client, *, state=None, response=None, capabilities=True, args=None, token=None):
    metadata = modern_meta()
    metadata["io.modelcontextprotocol/clientCapabilities"] = {"elicitation": {"form": {}}} if capabilities else {}
    params = {"name": "workflow_odoo_code_change", "arguments": args or {"operation_id": "operation-test"}, "_meta": metadata}
    if state:
        params["requestState"] = state
    if response:
        params["inputResponses"] = {"business_clarification": response}
    return client.post("/mcp", headers=headers(**{
        "Mcp-Method": "tools/call", "Mcp-Name": params["name"],
        **({"Authorization": "Bearer " + token} if token else {}),
    }), json={"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": params})


@pytest.fixture
def clarification_dispatch(monkeypatch):
    calls = []

    async def dispatch(name, args, profile):
        calls.append(dict(args))
        if args.get("clarification_answer"):
            return tool_result({"status": "ok", "answer": args["clarification_answer"], "operation_id": args["operation_id"]})
        return tool_result({"status": "needs_clarification", "input_required": True, "input_request": {
            "kind": "clarification", "prompt": "Which business behavior should apply?",
            "resume_call": {"tool": name, "arguments": dict(args)},
        }})

    monkeypatch.setattr(mcp_sdk.bridge, "call_tool_result", dispatch)
    return calls


def test_protocol_versions_empty_templates_and_truthful_capabilities():
    with TestClient(app, base_url="http://localhost") as client:
        assert rpc(client, "resources/templates/list").json()["result"]["resourceTemplates"] == []
        assert rpc(client, "resources/read", {"uri": "odoo-mcp://missing"}).json()["error"]["code"] == -32602
        init = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                         "clientInfo": {"name": "legacy", "version": "1"}})
        h = headers(**{"MCP-Protocol-Version": "2025-11-25", "MCP-Session-Id": init.headers["mcp-session-id"]})
        assert rpc(client, "resources/read", {"uri": "odoo-mcp://missing"}, request_headers=h).json()["error"]["code"] == -32002
        legacy = rpc(client, "initialize", request_headers={"Authorization": f"Bearer {settings.api_token}"})
        assert legacy.json()["result"]["capabilities"]["tools"]["listChanged"] is False


def test_private_cache_ttls_refresh_with_configuration_and_profiles(monkeypatch):
    with TestClient(app, base_url="http://localhost") as client:
        for method, params in [("tools/list", {}), ("prompts/list", {}), ("resources/list", {}),
                               ("resources/read", {"uri": "odoo-mcp://instructions/client-check"})]:
            result = rpc(client, method, params).json()["result"]
            assert result["ttlMs"] == settings.mcp_cache_ttl_seconds * 1000
            assert result["cacheScope"] == "private"
        monkeypatch.setattr(settings, "mcp_cache_ttl_seconds", 0)
        assert rpc(client, "tools/list").json()["result"]["ttlMs"] == 0
        full = rpc(client, "tools/list").json()["result"]["tools"]
        task = rpc(client, "tools/list", path="/mcp?profile=task").json()["result"]["tools"]
        assert len(task) < len(full)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_progress_tracks_phases_and_stops_after_rpc(http_server, monkeypatch, mode):
    late = threading.Event()

    async def dispatch(name, args):
        progress = WorkflowProgressService()
        await asyncio.to_thread(progress.start, "progress-test", client_name=None, module_name="sample")
        await asyncio.to_thread(progress.phase, "progress-test", "inspecting")
        await asyncio.sleep(.15)
        await asyncio.to_thread(progress.phase, "progress-test", "verifying")
        await asyncio.sleep(.15)

        async def background():
            await asyncio.sleep(.2)
            phase_event("late.background")
            late.set()

        asyncio.create_task(background())
        return {"status": "ok"}

    monkeypatch.setattr(mcp_sdk.bridge, "_dispatch", dispatch)
    updates = []

    async def show(value, total, message):
        updates.append((value, total, message))

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {settings.api_token}"}) as http:
        async with Client(streamable_http_client(http_server, http_client=http), mode=mode) as client:
            result = await client.call_tool("connector_health", {}, progress_callback=show)
            assert not result.is_error
            await asyncio.sleep(.1)
            count = len(updates)
            await asyncio.sleep(.2)
            assert late.is_set() and len(updates) == count
    assert "inspecting" in [row[2] for row in updates]
    assert "verifying" in [row[2] for row in updates]
    assert all(a[0] < b[0] for a, b in zip(updates, updates[1:]))
    assert all(row[1] is None for row in updates)


def test_mrtr_resume_and_replay_use_one_durable_continuation(clarification_dispatch):
    with TestClient(app, base_url="http://localhost") as client:
        initial = clarification_post(client).json()["result"]
        assert initial["resultType"] == "input_required"
        state = initial["requestState"]
        assert "operation-test" not in state
        response = {"action": "accept", "content": {"answer": "Keep existing behavior"}}
        for _ in range(2):
            completed = clarification_post(client, state=state, response=response).json()["result"]
            assert completed["structuredContent"]["answer"] == "Keep existing behavior"
        assert len(clarification_dispatch) == 2
        assert clarification_dispatch[-1]["operation_id"] == "operation-test"
    # Recreate the SDK manager as a restart would; receipt and identity survive.
    with TestClient(app, base_url="http://localhost") as restarted:
        completed = clarification_post(restarted, state=state, response=response).json()["result"]
        assert completed["structuredContent"]["answer"] == "Keep existing behavior"
        assert len(clarification_dispatch) == 2


def test_mrtr_binding_tampering_expiry_and_changed_answer(clarification_dispatch, monkeypatch):
    monkeypatch.setattr("odoo_agent_server.auth.OAuthStore.validate_access_token", lambda self, token: token == "another-valid-token")
    with TestClient(app, base_url="http://localhost") as client:
        state = clarification_post(client).json()["result"]["requestState"]
        answer = {"action": "accept", "content": {"answer": "A"}}
        for kwargs in [{"state": state[:-1] + "z"}, {"state": state, "token": "another-valid-token"},
                       {"state": state, "args": {"operation_id": "different"}}]:
            assert clarification_post(client, response=answer, **kwargs).json()["error"]["code"] == -32602
        assert len(clarification_dispatch) == 1
        clarification_post(client, state=state, response=answer)
        assert clarification_post(client, state=state, response={"action": "accept", "content": {"answer": "B"}}).json()["error"]["code"] == -32602
        path = settings.state_path / "mcp-clarifications" / (state + ".json")
        record = json.loads(path.read_text())
        record["expires"] = 0
        path.write_text(json.dumps(record))
        assert clarification_post(client, state=state, response=answer).json()["error"]["code"] == -32602


def test_mrtr_decline_and_unsupported_clients_keep_existing_flow(clarification_dispatch):
    with TestClient(app, base_url="http://localhost") as client:
        fallback = clarification_post(client, capabilities=False).json()["result"]
        assert fallback["resultType"] == "complete" and fallback["structuredContent"]["input_request"]
        state = clarification_post(client).json()["result"]["requestState"]
        declined = clarification_post(client, state=state, response={"action": "decline"}).json()["result"]
        assert declined["structuredContent"]["input_request"]
        assert len(clarification_dispatch) == 2


@pytest.mark.asyncio
async def test_mrtr_cancelled_dispatch_is_never_replayed(monkeypatch):
    service = McpClarificationService()
    principal = hashlib.sha256(settings.api_token.encode()).hexdigest()
    call = {"tool": "workflow_odoo_code_change", "arguments": {"operation_id": "once-only"}}
    state = service.issue(principal, call["tool"], {}, call, "clarification_answer", tool_result({}))

    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mcp_sdk.bridge, "call_tool_result", cancelled)
    user = mcp_sdk.AuthenticatedUser(mcp_sdk.AccessToken(token=principal, client_id=principal, scopes=[]))
    ctx = SimpleNamespace(request=SimpleNamespace(scope={"user": user}))
    params = CallToolRequestParams(name=call["tool"], arguments={}, request_state=state,
                                  input_responses={"business_clarification": {"action": "accept", "content": {"answer": "yes"}}})
    with pytest.raises(asyncio.CancelledError):
        await mcp_sdk._resume_clarification(ctx, params)
    with pytest.raises(MCPError, match="reconciliation"):
        await mcp_sdk._resume_clarification(ctx, params)


def test_authenticated_metrics_and_safe_trace_correlation(monkeypatch, caplog):
    from odoo_agent_server.services.store_service import JsonStore
    from odoo_agent_server.schemas import JobRecord
    logger.addHandler(caplog.handler)

    async def dispatch(name, args):
        job = await asyncio.to_thread(JsonStore().create_job, JobRecord(job_type="test"))
        assert job.trace_context["request_id"] == "trace-test"
        return {"status": "ok", "job_id": job.id}

    monkeypatch.setattr(mcp_sdk.bridge, "_dispatch", dispatch)
    try:
        with TestClient(app, base_url="http://localhost") as client:
            rpc(client, "tools/call", {"name": "connector_health", "arguments": {"password": "do-not-log"}},
                request_headers=headers(**{"Mcp-Method": "tools/call", "Mcp-Name": "connector_health", "X-Request-ID": "trace-test"}))
            assert client.get("/mcp/metrics").status_code == 401
            data = client.get("/mcp/metrics", headers=headers()).json()
            assert data["tools"]["connector_health"]["calls"] > 0
        assert "operation.end" in caplog.text and "trace-test" in caplog.text
        assert "do-not-log" not in caplog.text
        assert metrics_snapshot()["connector_health"]["max_ms"] >= 0
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.asyncio
async def test_deployment_readonly_transport_checks(http_server):
    from deploy.mcp_transport_check import check
    result = await check(http_server, settings.api_token, expected_ttl_ms=settings.mcp_cache_ttl_seconds * 1000)
    assert result["api_key"] == "passed"
    assert result["modes"]["legacy"]["sse_responses"] > 0
    assert result["modes"]["auto"]["reconnected"] is True
    for mode in ("auto", "legacy"):
        assert result["modes"][mode]["progress_streaming"] is True
        assert result["modes"][mode]["progress_events"] >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["missing", "bunched"])
async def test_deployment_check_rejects_missing_or_buffered_progress(http_server, monkeypatch, behavior):
    from deploy.mcp_transport_check import check

    async def broken_probe(ctx):
        if behavior == "bunched":
            await ctx.session.report_progress(1, message="transport.probe.started")
            await ctx.session.report_progress(2, message="transport.probe.ready")

    monkeypatch.setattr(mcp_sdk, "_transport_progress_probe", broken_probe)
    with pytest.raises(RuntimeError, match="progress missing or buffered"):
        await check(http_server, settings.api_token)


@pytest.mark.asyncio
async def test_official_client_completes_standard_clarification(http_server, clarification_dispatch):
    from mcp.types import ElicitResult
    prompts = []

    async def answer(context, params):
        prompts.append(params.message)
        return ElicitResult(action="accept", content={"answer": "Keep current behavior"})

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {settings.api_token}"}) as http:
        async with Client(streamable_http_client(http_server, http_client=http), elicitation_callback=answer) as client:
            result = await client.call_tool("workflow_odoo_code_change", {"operation_id": "official-client"})
    assert len(prompts) == 1
    assert result.structured_content["answer"] == "Keep current behavior"
    assert len(clarification_dispatch) == 2


def test_legacy_session_capacity_and_metrics_remain_bounded(monkeypatch):
    monkeypatch.setattr(settings, "mcp_max_sessions", 1)
    monkeypatch.setattr(settings, "mcp_session_idle_seconds", 15)
    with TestClient(app, base_url="http://localhost") as client:
        params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "bounded", "version": "1"}}
        first = rpc(client, "initialize", params)
        assert first.status_code == 200
        second = rpc(client, "initialize", params)
        assert second.status_code == 503
        h = headers(**{"MCP-Protocol-Version": "2025-11-25", "MCP-Session-Id": first.headers["mcp-session-id"]})
        assert client.delete("/mcp", headers=h).status_code == 200
        assert rpc(client, "initialize", params).status_code == 200


@pytest.mark.integration
def test_legacy_idle_session_expires_and_client_can_reconnect(monkeypatch):
    import time
    monkeypatch.setattr(settings, "mcp_session_idle_seconds", 15)
    with TestClient(app, base_url="http://localhost") as client:
        params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "expiry", "version": "1"}}
        init = rpc(client, "initialize", params)
        h = headers(**{"MCP-Protocol-Version": "2025-11-25", "MCP-Session-Id": init.headers["mcp-session-id"]})
        time.sleep(16)
        assert rpc(client, "ping", request_headers=h).status_code == 404
        assert rpc(client, "initialize", params).status_code == 200


@pytest.mark.asyncio
async def test_durable_write_drains_under_level_cancellation():
    import anyio
    from odoo_agent_server.services.async_io import durable_thread_call
    entered, release, written, cancelled = (threading.Event() for _ in range(4))

    def write():
        entered.set()
        release.wait(2)
        written.set()

    async def operation():
        try:
            await durable_thread_call(write)
        except asyncio.CancelledError:
            assert written.is_set()
            cancelled.set()
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(operation)
        while not entered.is_set():
            await asyncio.sleep(.001)
        asyncio.get_running_loop().call_later(.05, release.set)
        group.cancel_scope.cancel()
    assert cancelled.is_set() and written.is_set()
