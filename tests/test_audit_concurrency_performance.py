"""Regressions for concurrent use, client isolation and bounded discovery."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import odoo, tools, workflow
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus, JobRecord, JobStatus, OdooModuleRequest, ResumeJobRequest, SafeDeployModuleRequest
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.http_client_pool import HttpClientPool
from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService
from odoo_agent_server.services.oauth_service import OAuthStore
from odoo_agent_server.services.repo_index_service import RepoIndexService
from odoo_agent_server.services.source_snapshot_service import source_files
from odoo_agent_server.services.store_service import JsonStore


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    return JsonStore()


def test_simple_bearer_and_api_key_auth_work_without_oauth_consent(state, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "private-fixture-token")
    client = TestClient(app)
    for headers in ({"Authorization": "Bearer private-fixture-token"}, {"X-API-Key": "private-fixture-token"}):
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert response.status_code == 200 and "result" in response.json()


def test_loopback_redirect_does_not_grant_unauthenticated_remote_access(state, monkeypatch):
    monkeypatch.setattr(settings, "oauth_auto_approve", False)
    # Check the actual default rather than overriding the setting under test.
    assert settings.model_fields["oauth_auto_approve_loopback"].default is False
    monkeypatch.setattr(settings, "oauth_auto_approve_loopback", False)
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = TestClient(app, base_url="https://public.example").get("/oauth/authorize", params={
        "response_type": "code", "client_id": settings.oauth_static_client_id,
        "redirect_uri": "http://localhost/callback", "code_challenge_method": "S256", "code_challenge": challenge,
    }, follow_redirects=False)
    assert response.status_code == 200 and "Connector admin token" in response.text
    assert "location" not in response.headers


def test_one_approval_has_only_one_concurrent_owner(state):
    record = state.create_approval(ApprovalRecord(action="git.push", summary="Concurrent publish", status=ApprovalStatus.approved))
    barrier = threading.Barrier(8)

    def consume(_):
        barrier.wait(timeout=5)
        try:
            ApprovalService().ensure_allowed("git.push", record.id, force=True)
            return True
        except HTTPException as exc:
            assert exc.status_code == 403
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(consume, range(8))) == 1
    assert state.get_approval(record.id).status == ApprovalStatus.running


def test_one_oauth_code_issues_only_one_token_under_concurrent_exchange(state):
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    arguments = {"client_id": settings.oauth_static_client_id, "redirect_uri": "http://localhost/callback"}
    code = OAuthStore().create_code(**arguments, scope="odoo.agent", code_challenge=challenge, code_challenge_method="S256")
    barrier = threading.Barrier(8)

    def exchange(_):
        barrier.wait(timeout=5)
        try:
            return bool(OAuthStore().exchange_code(**arguments, code=code, code_verifier=verifier)["access_token"])
        except HTTPException as exc:
            assert exc.status_code == 400
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(exchange, range(8))) == 1


def _consume_from_worker(arguments):
    directory, approval_id, code, verifier = arguments
    settings.state_dir = directory
    try:
        ApprovalService().ensure_allowed("git.push", approval_id, force=True)
        approval = True
    except HTTPException:
        approval = False
    try:
        OAuthStore().exchange_code(code=code, client_id=settings.oauth_static_client_id,
                                   redirect_uri="http://localhost/callback", code_verifier=verifier)
        oauth = True
    except HTTPException:
        oauth = False
    return approval, oauth


@pytest.mark.integration
def test_single_use_claims_are_atomic_across_api_processes(state):
    record = state.create_approval(ApprovalRecord(action="git.push", summary="Multi-worker", status=ApprovalStatus.approved))
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = OAuthStore().create_code(client_id=settings.oauth_static_client_id, redirect_uri="http://localhost/callback",
                                    scope="odoo.agent", code_challenge=challenge, code_challenge_method="S256")
    arguments = (str(state.root), record.id, code, verifier)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = list(executor.map(_consume_from_worker, [arguments] * 4))
    assert sum(item[0] for item in results) == 1
    assert sum(item[1] for item in results) == 1


def test_concurrent_oauth_registration_preserves_all_clients(state):
    def register(index):
        return OAuthStore().register_client({"client_name": str(index), "redirect_uris": ["http://localhost/callback"]})["client_id"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = set(executor.map(register, range(16)))
    assert set(OAuthStore()._read()["clients"]) == ids


def test_recovery_never_falls_back_to_another_client(state):
    other = state.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.failed, request={"client_name": "Client B"}))
    assert tools._latest_safe_deploy_job_id(ResumeJobRequest(client_name="Client A"), state) is None
    assert tools._latest_safe_deploy_job_id(ResumeJobRequest(client_name="client b"), state) == other.id


@pytest.mark.asyncio
async def test_explicit_wrong_client_job_is_rejected_before_recovery(state, monkeypatch):
    job = state.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running, request={"client_name": "B"}))
    monkeypatch.setattr(tools, "mark_stale_safe_deploy_running_job", lambda *_: pytest.fail("Wrong client reached recovery"))
    with pytest.raises(HTTPException) as exc:
        await tools._resume_job(ResumeJobRequest(job_id=job.id, client_name="A"))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("route", ["odoo_module_install", "odoo_module_update", "start_odoo_module_deploy_job"])
def test_rejected_worker_submission_finalizes_job(state, monkeypatch, route):
    monkeypatch.setattr(odoo, "_prepare_validated_client", lambda *_: None)
    monkeypatch.setattr(odoo, "safe_deploy_shell_block", lambda *_, **__: None)
    monkeypatch.setattr(odoo.ApprovalService, "ensure_allowed", lambda *_, **__: None)
    def rejected(_):
        raise HTTPException(503, {"error_code": "background_capacity_exhausted"})
    monkeypatch.setattr(odoo, "submit_background", rejected)
    with pytest.raises(HTTPException) as exc:
        getattr(odoo, route)(OdooModuleRequest(module_name="audit_module"))
    assert exc.value.status_code == 503
    job = state.list_jobs()[0]
    assert job.status == JobStatus.failed
    assert job.result["error_code"] == "background_capacity_exhausted"
    assert job.result["success_allowed"] is False


@pytest.mark.asyncio
async def test_rejected_safe_deploy_submission_finalizes_job_and_approval(state, monkeypatch, tmp_path):
    from odoo_agent_server.services import published_workspace_service
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "workflow.safe_deploy_module")
    monkeypatch.setattr(published_workspace_service, "validate_published_workspace", lambda *_: None)
    monkeypatch.setattr(workflow.ClientGateService, "issue_job_validation_lease", lambda *_, **__: {})
    monkeypatch.setattr(workflow, "_scoped_diff_fingerprint", lambda *_: "fixture")
    monkeypatch.setattr(workflow, "find_blocking_safe_deploy", lambda **_: None)
    record = state.create_approval(ApprovalRecord(action="workflow.safe_deploy_module", summary="Deploy", status=ApprovalStatus.approved))
    def rejected(_):
        raise HTTPException(503, {"error_code": "background_capacity_exhausted"})
    monkeypatch.setattr(workflow, "submit_background", rejected)
    with pytest.raises(HTTPException) as exc:
        await workflow.safe_deploy_module(SafeDeployModuleRequest(project_path=str(tmp_path), module_name="audit_module", commit_message="Audit", approval_id=record.id))
    assert exc.value.status_code == 503
    assert state.list_jobs()[0].status == JobStatus.failed
    assert state.get_approval(record.id).status == ApprovalStatus.failed


@pytest.mark.parametrize("payload,code", [(42, -32600), ([], -32600), ([42], -32600),
    ({"id": 1, "method": "tools/call", "params": [1]}, -32602),
    ({"id": 1, "method": "tools/call", "params": {"name": "connector_health", "arguments": []}}, -32602)])
@pytest.mark.parametrize("endpoint", ["/mcp", "/chatgpt/mcp", "/claude/mcp", "/grok/mcp"])
def test_malformed_rpc_returns_protocol_error(state, payload, code, endpoint):
    response = TestClient(app).post(endpoint, headers={"Authorization": f"Bearer {settings.api_token}"}, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert (body[0] if isinstance(body, list) else body)["error"]["code"] == code


@pytest.mark.asyncio
async def test_concurrent_health_requests_finish_while_log_io_is_running(state, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def logs(*_):
        started.set()
        release.wait(25)
        finished.set()
        return {"status": "ok"}
    monkeypatch.setattr(settings, "mcp_operation_guard_enabled", False)
    monkeypatch.setattr(tools.ClientGateService, "require_validated_client", lambda *_: None)
    monkeypatch.setattr(tools, "_resolve_query_client", lambda *_: None)
    monkeypatch.setattr(tools.OdooShService, "logs_read", logs)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/mcp", headers={"Authorization": f"Bearer {settings.api_token}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "odoosh_logs_read", "arguments": {"last_hours": 1}},
        }))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            responses = await asyncio.wait_for(asyncio.gather(*(client.get("/health") for _ in range(8))), timeout=10)
            assert all(response.status_code == 200 for response in responses)
            assert not finished.is_set() and not pending.done(), "Health requests did not finish while log I/O was blocked"
        finally:
            release.set()
            response, = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(response, httpx.Response) and response.status_code == 200


def test_job_index_reads_only_requested_records_and_recovers_external_changes(state, monkeypatch):
    jobs = [state.create_job(JobRecord(job_type="indexed", status=JobStatus.running)) for _ in range(25)]
    state.list_jobs(limit=3)  # initialize/repair index before observing reads
    original = Path.read_text
    opened = []
    def read(path, *args, **kwargs):
        if path.parent == state.jobs_dir and path.suffix == ".json":
            opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert len(state.list_jobs(limit=3)) == 3
    assert len(opened) == 3
    state._job_path(jobs[-1].id).unlink()
    assert jobs[-1].id not in {job.id for job in state.list_jobs(limit=100)}
    imported = JobRecord(job_type="imported")
    state._job_path(imported.id).write_text(imported.model_dump_json())
    assert imported.id in {job.id for job in state.list_jobs(limit=100)}


def test_connection_pool_reuses_identity_and_never_crosses_credentials_or_tls(monkeypatch):
    factory = httpx.Client
    clients = []
    def create(**kwargs):
        client = factory(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(httpx, "Client", create)
    pool = HttpClientPool(maximum=2)
    try:
        with pool.acquire("client-a", verify=True) as first:
            first.cookies.set("session", "a")
        with pool.acquire("client-a", verify=True) as reused:
            assert reused is first
        with pool.acquire("client-b", verify=True) as other:
            assert other is not first and not other.cookies
        with pool.acquire("client-a", verify=False) as tls:
            assert tls is not first and not tls.cookies
        assert first.is_closed  # least-recent idle client was evicted
    finally:
        pool.close()
    assert all(client.is_closed for client in clients)


def test_busy_connection_pool_never_evicts_an_inflight_client():
    pool = HttpClientPool(maximum=1)
    try:
        with pool.acquire("busy", verify=True) as busy:
            with pool.acquire("overflow", verify=True) as temporary:
                assert not busy.is_closed and not temporary.is_closed
            assert temporary.is_closed and not busy.is_closed
            pool.close()
            assert not busy.is_closed  # shutdown waits for lease completion
        assert busy.is_closed
    finally:
        pool.close()


def test_rpc_calls_reuse_connections_with_per_request_timeouts_and_key_isolation(monkeypatch):
    from odoo_agent_server.services import odoo_jsonrpc_service
    pool = HttpClientPool()
    monkeypatch.setattr(odoo_jsonrpc_service, "odoo_http_clients", pool)
    factory = httpx.Client
    created, requests = [], []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "session=first; Path=/"})
    def create(**kwargs):
        client = factory(transport=httpx.MockTransport(respond), **kwargs)
        created.append(client)
        return client
    monkeypatch.setattr(httpx, "Client", create)
    client = ResolvedClient(name="a", repo="https://example/repo", branch="dev", path="/tmp/repo",
                            odoo_url="https://odoo.example", odoo_db="a", odoo_api_key="key-a")
    try:
        rpc = OdooJsonRpcService(client)
        rpc._json2_call("res.partner", "search_read", {}, 10)
        rpc._json2_call("res.partner", "search_read", {}, 30)
        OdooJsonRpcService(replace(client, odoo_api_key="key-b"))._json2_call("res.partner", "search_read", {}, 10)
        assert len(created) == 2
        assert requests[0].extensions["timeout"]["read"] != requests[1].extensions["timeout"]["read"]
        assert requests[1].headers["cookie"] == "session=first"
        assert "cookie" not in requests[2].headers
        assert requests[2].headers["authorization"] == "bearer key-b"
    finally:
        pool.close()


def test_corrupt_oauth_state_is_preserved_for_repair(state):
    file = state.root / "oauth.json"
    file.write_text("{broken")
    with pytest.raises(HTTPException) as exc:
        OAuthStore().register_client({"redirect_uris": ["http://localhost/callback"]})
    assert exc.value.status_code == 503
    assert file.read_text() == "{broken"


def test_source_inventory_detects_add_delete_and_preserved_mtime_edits(state, tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    source = project / "a.xml"
    source.write_text('<t t-name="first"/>')
    service = RepoIndexService()
    before = service.index(str(project))
    assert service.index(str(project)).cached
    stat = source.stat()
    source.write_text('<t t-name="other"/>')
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = service.index(str(project))
    assert not changed.cached and changed.template_names != before.template_names
    added = project / "b.py"
    added.write_text("VALUE = 1\n")
    assert added in list(source_files(project))
    added.unlink()
    assert added not in list(source_files(project))


def test_bounded_index_walks_once_and_hashes_only_its_coverage(state, tmp_path, monkeypatch):
    project = tmp_path / "bounded"
    project.mkdir()
    for index in range(10):
        (project / f"file{index}.py").write_text("VALUE = 1\n")
    service = RepoIndexService()
    original = service._walk
    walked = []
    def walk(root):
        for path in original(root):
            walked.append(path)
            yield path
    monkeypatch.setattr(service, "_walk", walk)
    result = service.index(str(project), max_files=2)
    assert len(walked) == 3 and len(result.python_files) == 2
    assert any("stopped" in note for note in result.notes)
