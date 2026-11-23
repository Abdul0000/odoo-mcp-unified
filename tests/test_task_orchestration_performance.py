"""Concurrency optimizations must preserve durable ownership and exact-source proof."""
import asyncio
import json
import os
import threading

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from tests.test_execution_worker import execution, worker_for  # noqa: F401
from tests.test_task_coordinator import task  # noqa: F401
from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.task_schemas import TaskRun
from odoo_agent_server.execution_schemas import ProcessStart, WorkspaceFiles, WorkspacePublish
from odoo_agent_server.services.task_service import TaskBusy


def test_task_lock_releases_even_when_a_descriptor_was_inherited(task):  # noqa: F811
    ledger, tid, *_ = task
    duplicate = None
    try:
        with ledger.lock(tid) as descriptor:
            duplicate = os.dup(descriptor)
        with ledger.lock(tid):
            pass  # an unrelated child still holding the old descriptor cannot stall this task
    finally:
        if duplicate is not None:
            os.close(duplicate)


def test_terminal_command_receipt_requests_reconciliation_not_new_arguments(task):  # noqa: F811
    ledger, tid, *_ = task
    row = ledger.read(tid)
    body = {"status":"succeeded", "process_id":"a"*32}
    ledger.observe(row,"execution.process_poll",body)
    result = ledger.envelope(row,body)
    assert result["next_call"] == {"tool":"task_resume","arguments":{"task_id":tid}}
    assert "argument_request" not in result
    assert not result["success_allowed"]


@pytest.mark.asyncio
async def test_checkpoint_cancellation_drains_writer_before_unlocking(task, monkeypatch):  # noqa: F811
    ledger, tid, *_ = task
    entered, release = threading.Event(), threading.Event()
    original = ledger.save
    def slow_save(row):
        entered.set()
        assert release.wait(3)
        original(row)
    monkeypatch.setattr(ledger, "save", slow_save)
    async def write():
        with ledger.lock(tid):
            row = ledger.read(tid)
            row["phase"] = "saved_after_cancel"
            await ledger.checkpoint(row)
    pending = asyncio.create_task(write())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()  # repeated disconnects cannot release ownership either
        await asyncio.sleep(0)
        with pytest.raises(TaskBusy):
            with ledger.lock(tid):
                pass
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with ledger.lock(tid):
        assert ledger.read(tid)["phase"] == "saved_after_cancel"


@pytest.mark.asyncio
async def test_pre_dispatch_cancel_never_executes_mutation(task, monkeypatch):  # noqa: F811
    ledger, tid, _, wid, _ = task
    entered, release = threading.Event(), threading.Event()
    original = ledger.save
    def slow_save(row):
        entered.set()
        assert release.wait(3)
        original(row)
    monkeypatch.setattr(ledger, "save", slow_save)
    calls = []
    async def dispatch(*args):
        calls.append(args)
    request = TaskRun(task_id=tid, tool="execution_files", step_id="cancel-before-dispatch",
                      arguments={"workspace_id": wid, "operation": "write", "path": "cancel.py", "content": "x=1"})
    pending = asyncio.create_task(ledger.run(request, dispatch))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not calls
    assert ledger.read(tid)["steps"][request.step_id]["status"] == "dispatched"
    assert (await ledger.run(request, dispatch))["idempotent_replay"]
    assert not calls


@pytest.mark.asyncio
async def test_failed_pre_dispatch_checkpoint_prevents_side_effect(task, monkeypatch):  # noqa: F811
    ledger, tid, _, wid, _ = task
    def failed(_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(ledger, "save", failed)
    async def dispatch(*_):
        pytest.fail("Mutation ran without a durable checkpoint")
    with pytest.raises(OSError):
        await ledger.run(TaskRun(task_id=tid, tool="execution_files", arguments={
            "workspace_id": wid, "operation": "write", "path": "blocked.py", "content": "x=1"}), dispatch)


@pytest.mark.asyncio
async def test_resume_reads_are_bounded_parallel_and_observed_in_order(task, monkeypatch):  # noqa: F811
    ledger, tid, _, wid, _ = task
    row = ledger.read(tid)
    row["refs"].update(process_ids=["a"*32], job_ids=["b"*32,"c"*32,"d"*32], operation_ids=["e"*32])
    ledger.save(row)
    monkeypatch.setattr(settings, "task_poll_concurrency", 2)
    active, maximum = 0, 0
    two_started, release = asyncio.Event(), asyncio.Event()
    workspace_arguments = []
    async def dispatch(name, args):
        nonlocal active, maximum
        if name == "execution.workspace_status":
            workspace_arguments.append(args)
            return {"status":"running", "verification":{"ready":False}, "fresh_verification":False}
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_started.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return {"status":"running", **args}
        finally:
            active -= 1
    pending = asyncio.create_task(ledger.resume(tid, dispatch))
    try:
        await asyncio.wait_for(two_started.wait(), 1)
    finally:
        release.set()
    result = await pending
    assert maximum == 2
    assert [o["tool"] for o in result["observations"]] == [
        "execution.process_poll", "jobs.get", "jobs.get", "jobs.get", "execution.status", "execution.workspace_status"]
    assert workspace_arguments == [{"workspace_id":wid,"detail":"progress"}]
    assert not result["success_allowed"]


@pytest.mark.asyncio
async def test_resume_skips_settled_receipts_but_refreshes_source_and_retryable_jobs(task):  # noqa: F811
    ledger, tid, _, wid, _ = task
    row = ledger.read(tid)
    row["refs"].update(job_ids=["b"*32,"c"*32], process_ids=["a"*32])
    ledger.save(row)
    calls = []
    async def dispatch(name, args):
        calls.append((name, args))
        if name == "jobs.get":
            return {"id":args["job_id"],"status":"passed" if args["job_id"].startswith("b") else "warning", "deploy_confirmed":False}
        if name == "execution.process_poll":
            return {"status":"succeeded", "process_id":args["process_id"]}
        return {"status":"ready", "phase":"edit", "verification":{"ready":False}, "success_allowed":False}
    await ledger.resume(tid, dispatch)
    calls.clear()
    result = await ledger.resume(tid, dispatch)
    assert calls == [("jobs.get",{"job_id":"c"*32,"wait_for_result":False}),
                     ("execution.workspace_status",{"workspace_id":wid})]
    assert not result["success_allowed"]


def test_progress_status_never_runs_git_or_grants_proof(execution, monkeypatch):  # noqa: F811
    svc, wid, *_ = execution
    svc.store.update(wid, "workspace", phase="done", pushed=True, published_commit="a"*40)
    monkeypatch.setattr(svc, "_status", lambda *_: pytest.fail("Progress poll ran full verification"))
    result = svc.status(wid, detail="progress")
    assert result["fresh_verification"] is False and not result["verification"]["ready"]
    assert not result["success_allowed"] and result["completion_required"]
    assert result["next_call"]["arguments"]["detail"] == "full"


def test_progress_response_cannot_reuse_old_ready_claims(task):  # noqa: F811
    ledger, tid, *_ = task
    row = ledger.read(tid)
    row["claims"] = {"coding_verified":True,"push_verified":True,"deployment_verified":True}
    ledger.observe(row,"execution.workspace_status", {"fresh_verification":False,"verification":{"ready":True},
                   "pushed":True,"success_allowed":True,"phase":"done","published_commit":"a"*40})
    assert not any(row["claims"].values())
    assert not ledger.public(row)["success_allowed"]


def test_progress_poll_cannot_authorize_publishing_changed_source(execution, monkeypatch):  # noqa: F811
    svc, wid, *_ = execution
    svc.status(wid, detail="progress")
    svc.files(WorkspaceFiles(workspace_id=wid, operation="write", path="hello.py", content="print('edited')\n",
                             expected_sha256=svc.files(WorkspaceFiles(workspace_id=wid, operation="read", path="hello.py"))["sha256"]))
    with pytest.raises(HTTPException):
        svc.publish(WorkspacePublish(workspace_id=wid, message="Unverified", push=False, request_id="blocked-publish-001"))
    assert svc.status(wid)["fresh_verification"] is True
    assert not svc.status(wid)["verification"]["ready"]


def test_task_manifest_is_smaller_and_keeps_recovery_and_approval_tools():
    client = TestClient(app)
    headers = {"Authorization":"Bearer "+settings.api_token}
    payload = {"jsonrpc":"2.0","id":1,"method":"tools/list"}
    full = client.post("/mcp", json=payload, headers=headers).json()["result"]["tools"]
    task_tools = client.post("/mcp?profile=task", json=payload, headers=headers).json()["result"]["tools"]
    names = {tool["name"] for tool in task_tools}
    assert {"task_open","task_run","task_resume","task_deploy","task_test_plan","client_check","artifacts_read","approvals_approve"} <= names
    assert len(json.dumps(task_tools)) < len(json.dumps(full)) * .5
    assert "execution_publish" not in names
    via_header = client.post("/mcp", json=payload, headers={**headers,"X-MCP-Profile":"task"}).json()["result"]["tools"]
    assert via_header == task_tools
