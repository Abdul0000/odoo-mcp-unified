"""Exercise real task clones, durable queues and subprocess lifecycle without Docker."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.execution_schemas import AgentStart, ProcessStart, WorkspaceFiles, WorkspaceOpen, WorkspacePublish
from odoo_agent_server.execution_tools import EXECUTION_TOOLS, dispatch_execution
from odoo_agent_server.execution_worker import ExecutionWorker
from odoo_agent_server.main import app
from odoo_agent_server.services.execution_service import ExecutionService
from odoo_agent_server.services.execution_store import ExecutionStore
from odoo_agent_server.services.git_identity import same_repository


@pytest.fixture
def execution(tmp_path, monkeypatch):
    monkeypatch.delenv("ODOO_CLIENTS", raising=False)
    for key, value in {"execution_enabled": True, "execution_root": str(tmp_path / "execution"),
                       "allowed_roots_raw": str(tmp_path), "state_dir": str(tmp_path / "state"),
                       "target_project_path": None, "require_approval": False}.items():
        monkeypatch.setattr(settings, key, value)
    repo = tmp_path / "source"
    repo.mkdir()
    def git(*argv):
        return subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, check=True)
    git("init", "-b", "staging")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "hello.py").write_text("print('hello')\n")
    git("add", ".")
    git("commit", "-m", "base")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(repo), str(bare)], capture_output=True, check=True)
    git("remote", "add", "origin", str(bare))
    service = ExecutionService()
    req = WorkspaceOpen(project_path=str(repo), task_description="Test isolated coding", request_id="workspace-1")
    ws = service.open(req)
    return service, ws["workspace_id"], repo, bare


def worker_for(service, monkeypatch):
    worker = ExecutionWorker(service=service)
    monkeypatch.setattr(worker, "remove_container", lambda _id: None)
    monkeypatch.setattr(worker, "command", lambda record: record["request"]["argv"])
    return worker


def queue(service, workspace, code, **kwargs):
    request = ProcessStart(workspace_id=workspace, argv=[sys.executable, "-u", "-c", code],
                           request_id="command-" + str(time.time_ns()), **kwargs)
    result = service.start_process(request)
    return service.store.claim("process"), result


def test_workspace_isolated_idempotent_and_cas(execution):
    service, ws, source, _ = execution
    reopened = service.open(WorkspaceOpen(project_path=str(source), task_description="Test isolated coding", request_id="workspace-1"))
    assert reopened["workspace_id"] == ws
    read = service.files(WorkspaceFiles(workspace_id=ws, operation="read", path="hello.py"))
    assert read["sha256"] == hashlib.sha256((source / "hello.py").read_bytes()).hexdigest()
    service.files(WorkspaceFiles(workspace_id=ws, operation="write", path="hello.py", content="print('changed')\n", expected_sha256=read["sha256"]))
    assert (source / "hello.py").read_text() == "print('hello')\n"
    with pytest.raises(HTTPException, match="409"):
        service.files(WorkspaceFiles(workspace_id=ws, operation="write", path="hello.py", content="lost update", expected_sha256=read["sha256"]))


def test_traversal_and_git_metadata_rejected(execution):
    service, ws, source, _ = execution
    (service.root(ws) / "outside").symlink_to(source)
    (service.root(ws) / "metadata").symlink_to(service.root(ws) / ".git")
    for path in ["../source/hello.py", "/etc/passwd", ".git/config", "outside/hello.py", "metadata/config"]:
        with pytest.raises(HTTPException):
            service.files(WorkspaceFiles(workspace_id=ws, operation="read", path=path))


def test_queue_claim_and_idempotency_are_transactional(execution):
    service, ws, _, _ = execution
    request = ProcessStart(workspace_id=ws, argv=["python", "--version"], request_id="same-command")
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.start_process(request), range(4)))
    assert len({r["process_id"] for r in results}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: ExecutionStore().claim("process"), range(4)))
    assert sum(c is not None for c in claims) == 1
    with pytest.raises(HTTPException):
        service.start_process(request.model_copy(update={"argv": ["different"]}))


def test_real_output_offsets_and_failed_exit(execution, monkeypatch):
    service, ws, _, _ = execution
    worker = worker_for(service, monkeypatch)
    record, result = queue(service, ws, "print('abcdef'); raise SystemExit(7)")
    worker.execute(record)
    first = service.poll(result["process_id"], 0, 3)
    second = service.poll(result["process_id"], first["next_offset"], 10)
    assert first["output"] == "abc"
    assert second["output"] == "def\n"
    assert second["status"] == "failed" and second["exit_code"] == 7
    assert not second["success_allowed"]


def test_stdin_and_cancellation(execution, monkeypatch):
    service, ws, _, _ = execution
    worker = worker_for(service, monkeypatch)
    record, result = queue(service, ws, "import sys; print(sys.stdin.read())")
    service.store.send_input(record["id"], "hello", True)
    worker.execute(record)
    assert service.poll(result["process_id"])["output"] == "hello\n"
    record, _ = queue(service, ws, "import time; time.sleep(30)")
    thread = threading.Thread(target=worker.execute, args=(record,))
    thread.start()
    service.cancel(record["id"], "process")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert service.poll(record["id"])["status"] == "cancelled"


def test_time_and_output_limits(execution, monkeypatch):
    service, ws, _, _ = execution
    worker = worker_for(service, monkeypatch)
    record, _ = queue(service, ws, "import time; time.sleep(30)", timeout_seconds=1)
    worker.execute(record)
    assert service.poll(record["id"])["status"] == "timed_out"
    monkeypatch.setattr(settings, "execution_log_max_bytes", 32000)
    record, _ = queue(service, ws, "print('x' * 100000)")
    worker.execute(record)
    assert service.poll(record["id"])["status"] == "output_limit"
    assert (service.store.root / "logs" / (record["id"] + ".log")).stat().st_size == 32000


def test_pending_commands_block_file_access_and_failed_cleanup_quarantines(execution, monkeypatch):
    service, ws, _, _ = execution
    record, _ = queue(service, ws, "print('done')")
    with pytest.raises(HTTPException):
        service.files(WorkspaceFiles(workspace_id=ws, operation="read", path="hello.py"))
    worker = worker_for(service, monkeypatch)
    monkeypatch.setattr(worker, "remove_container", lambda _: False)
    worker.execute(record)
    assert service.poll(record["id"])["status"] == "cleanup_failed"
    with pytest.raises(HTTPException):
        service.status(ws)


def test_recovery_never_replays_ambiguous_commands(execution, monkeypatch):
    service, ws, _, _ = execution
    record, _ = queue(service, ws, "print('may have run')")
    worker_for(service, monkeypatch).recover()
    assert service.poll(record["id"])["status"] == "interrupted"
    assert service.store.claim("process") is None


def test_container_has_only_task_mount_no_network_and_no_secrets(execution):
    service, ws, _, _ = execution
    record, _ = queue(service, ws, "print('test')")
    command = ExecutionWorker(service=service).command(record)
    assert command[command.index("--network") + 1] == "none"
    assert "--cap-drop" in command and "--read-only" in command
    assert "--memory" in command and "--pids-limit" in command
    assert any("/workspace/.git,readonly" in part for part in command)
    assert not any("docker.sock" in part or "OPENAI_API_KEY" in part for part in command)


def test_publish_commits_and_verifies_remote_without_mutating_source(execution):
    service, ws, source, bare = execution
    service.files(WorkspaceFiles(workspace_id=ws, operation="write", path="new.py", content="VALUE = 42\n"))
    req = WorkspacePublish(workspace_id=ws, message="Add new value", request_id="publish-once")
    result = service.publish(req)
    assert result["pushed"] is True and result["deployment"] == "not_deployed"
    sha = subprocess.check_output(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/" + result["branch"]], text=True).strip()
    assert result["commit"] == sha
    assert not (source / "new.py").exists()
    assert service.publish(req)["id"] == result["id"]


def test_agent_missing_config_is_explicit(execution, monkeypatch):
    service, ws, _, _ = execution
    monkeypatch.setattr(settings, "agent_api_key", None)
    monkeypatch.setattr(settings, "agent_model", None)
    result = service.start_agent(AgentStart(workspace_id=ws, task="fix", request_id="agent-missing"))
    assert result["status"] == "configuration_required"
    assert result["missing"] == ["OPENAI_API_KEY", "OPENAI_MODEL"]


def test_agent_checkpoint_continuation_and_budget(execution, monkeypatch):
    service, ws, _, _ = execution
    monkeypatch.setattr(settings, "agent_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_model", "test-model")
    record = service.start_agent(AgentStart(workspace_id=ws, task="inspect", request_id="agent-test", max_turns=1))
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"id": "resp1", "status": "completed", "usage": {"total_tokens": 12}, "output": [
                {"type": "function_call", "name": "execution_workspace_status", "arguments": json.dumps({"workspace_id": ws}), "call_id": "call1"}]}
    def post(_self, _url, **kwargs):
        assert kwargs["json"]["max_output_tokens"] == 4000
        return Response()
    monkeypatch.setattr("odoo_agent_server.execution_worker.httpx.Client.post", post)
    worker = worker_for(service, monkeypatch)
    worker.agent_turn(service.store.claim("agent"))
    persisted = ExecutionStore().get(record["task_id"], "agent")
    assert persisted["previous_response_id"] == "resp1"
    assert persisted["next_input"][0]["call_id"] == "call1"
    assert persisted["total_tokens"] == 12
    worker.agent_turn(service.store.claim("agent"))
    assert service.agent_status(record["task_id"])["status"] == "budget_exhausted"


def test_execution_mcp_lane_is_independent_of_odoo_guard(execution, monkeypatch):
    service, ws, _, _ = execution
    monkeypatch.setattr(settings, "mcp_operation_guard_enabled", True)
    from odoo_agent_server.services.store_service import JsonStore
    JsonStore().save_session_state({"mcp_operation_guard": {"current": {"phase": "final"}}})
    client = TestClient(app)
    response = client.post("/mcp", headers={"Authorization": "Bearer " + settings.api_token}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "execution_files", "arguments": {"workspace_id": ws, "operation": "read", "path": "hello.py"}}})
    result = response.json()["result"]
    assert not result["isError"]
    assert result["structuredContent"]["content"] == "print('hello')\n"
    tools = client.post("/mcp?profile=execution", headers={"Authorization": "Bearer " + settings.api_token},
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert len(tools) < 25
    assert "execution_process_start" in {t["name"] for t in tools}


def test_mcp_exposes_execution_tools_and_disabled_mode(monkeypatch):
    monkeypatch.setattr(settings, "execution_enabled", True)
    monkeypatch.setattr(settings, "agent_api_key", None)
    monkeypatch.setattr(settings, "agent_model", None)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + settings.api_token}
    result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    names = {tool["name"] for tool in result["result"]["tools"]}
    assert {name.replace(".", "_") for name in EXECUTION_TOOLS if not name.startswith("execution.agent_")} <= names
    assert not any(name.startswith("execution_agent_") for name in names)
    monkeypatch.setattr(settings, "execution_enabled", False)
    with pytest.raises(HTTPException):
        dispatch_execution("execution.status", {})


@pytest.mark.parametrize("left,right,match", [
    ("git@github.com:acme/app.git", "https://github.com/acme/app", True),
    ("https://github.com/acme/app.git", "https://github.com/other/app.git", False),
    ("https://github.com/acme/app.git", "https://gitlab.com/acme/app.git", False),
])
def test_repository_identity(left, right, match):
    assert same_repository(left, right) is match


def test_json_state_allows_the_separate_worker_and_api_to_write(execution):
    service, workspace, _, _ = execution
    path = service.store.root / "records" / "workspace" / f"{workspace}.json"
    assert path.stat().st_mode & 0o660 == 0o660
    assert not list(service.store.root.rglob("*.sqlite*"))
    assert json.loads(path.read_text())["status"] == "ready"


def test_failed_checkpoint_keeps_the_previous_json_record(execution, monkeypatch):
    service, workspace, _, _ = execution
    path = service.store.root / "records" / "workspace" / f"{workspace}.json"
    before = path.read_bytes()
    def failed_replace(*_args):
        raise OSError("simulated storage interruption")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", failed_replace)
        with pytest.raises(OSError):
            service.store.update(workspace, "workspace", status="failed")
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    assert service.store.update(workspace, "workspace", checkpoint="recovered")["checkpoint"] == "recovered"
