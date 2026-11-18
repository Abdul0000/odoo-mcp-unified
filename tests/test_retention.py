from __future__ import annotations
from datetime import datetime, timezone
import json
import os
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.test_execution_worker import execution  # noqa: F401
from odoo_agent_server.config import settings
from odoo_agent_server.execution_schemas import WorkspaceFiles, ProcessStart
from odoo_agent_server.services.client_settings_store import atomic_json
from odoo_agent_server.services.execution_store import ExecutionStore
from odoo_agent_server.services.retention_service import RetentionService
from odoo_agent_server.services import mutation_journal
from odoo_agent_server.services.functional_operation_service import FunctionalOperationService
from odoo_agent_server.schemas import OdooRecordCreateRequest
from odoo_agent_server.services.task_service import TaskService


@pytest.fixture(autouse=True)
def private_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "screenshot_dir", str(tmp_path / "screenshots"))


def age_workspace(service, wid, age):
    path = service.store._path(wid, "workspace")
    row = service.store._read(path)
    row.update(created=age, updated=age)
    service.store._write(path, row)


def test_old_clean_workspace_and_logs_expire_but_request_ids_cannot_replay(execution, monkeypatch):  # noqa: F811
    svc, wid, _, _ = execution
    now = time.time()
    old = now - 172801
    age_workspace(svc, wid, old)
    request = {"workspace_id": wid, "argv": ["python", "--version"], "request_id": "expired-command-001"}
    record, _ = svc.store.create("process", "expired-command-001", request, status="succeeded")
    path = svc.store._path(record["id"], "process")
    row = svc.store._read(path)
    row.update(created=old, updated=old)
    svc.store._write(path, row)
    logs = svc.store.root / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / (record["id"] + ".log")
    log.write_text("private command output")
    os.utime(log, (old, old))
    result = RetentionService(now).run()
    assert result["removed"]["workspaces"] == 1 and not svc.root(wid).exists()
    assert not log.exists()
    assert svc.store.get(wid, "workspace")["status"] == "expired"
    assert not svc.store.recent("workspace", 64)
    with pytest.raises(HTTPException, match="410"):
        svc.store.existing("process", "expired-command-001", request)
    assert "private command output" not in path.read_text()


def test_uncommitted_code_and_live_commands_are_preserved(execution):  # noqa: F811
    svc, wid, _, _ = execution
    now = time.time()
    old = now - 172801
    svc.files(WorkspaceFiles(workspace_id=wid, operation="write", path="unfinished.py", content="USER_CODE = 1\n"))
    age_workspace(svc, wid, old)
    result = RetentionService(now).run()
    assert result["removed"]["protected_workspaces"] == 1
    assert (svc.root(wid) / "unfinished.py").read_text() == "USER_CODE = 1\n"
    assert svc.store.get(wid, "workspace")["status"] == "ready"
    record = svc.start_process(ProcessStart(workspace_id=wid, argv=["python", "--version"], request_id="active-command-001"))
    age_workspace(svc, wid, old)
    RetentionService(now).run()
    assert svc.store.get(wid, "workspace")["status"] == "ready"
    assert svc.store.get(record["process_id"], "process")["status"] == "queued"


def test_locked_workspace_is_not_expired(execution):  # noqa: F811
    svc, wid, _, _ = execution
    now = time.time()
    age_workspace(svc, wid, now - 172801)
    with svc.store.workspace_lock(wid):
        RetentionService(now).run()
    assert svc.root(wid).exists() and svc.store.get(wid, "workspace")["status"] == "ready"


def test_task_and_history_payloads_expire_without_touching_config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "execution_enabled", False)
    now = time.time()
    old = datetime.fromtimestamp(now - 172801, timezone.utc).isoformat()
    recent = datetime.fromtimestamp(now, timezone.utc).isoformat()
    state = settings.state_path
    tid = "a" * 32
    atomic_json(state / "tasks" / (tid + ".json"), {"task_id": tid, "created_at": old, "updated_at": old, "description": "private old task", "refs": {}})
    atomic_json(state / "audit.json", [{"created_at": old, "summary": "old secret"}, {"created_at": recent, "summary": "current"}])
    atomic_json(state / "jobs" / "old-job-001.json", {"id": "old-job-001", "status": "passed", "created_at": old})
    atomic_json(state / "jobs" / "active-job-001.json", {"id": "active-job-001", "status": "running", "created_at": old, "request": {}})
    atomic_json(state / "client_settings.json", {"version": 1, "clients": {"actual": {"odoo_password": "keep"}}})
    result = RetentionService(now).run()
    assert result["removed"]["tasks"] == 1 and not (state / "tasks" / (tid + ".json")).exists()
    assert (state / "task-tombstones" / (tid + ".json")).read_text() == '{"expired": true}'
    with pytest.raises(HTTPException, match="410"):
        TaskService().read(tid)
    assert json.loads((state / "audit.json").read_text()) == [{"created_at": recent, "summary": "current"}]
    assert not (state / "jobs" / "old-job-001.json").exists() and (state / "jobs" / "active-job-001.json").exists()
    assert "keep" in (state / "client_settings.json").read_text()


def test_expired_mutation_keeps_only_hash_marker_and_blocks_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "execution_enabled", False)
    monkeypatch.delenv("ODOO_CLIENTS", raising=False)
    now = time.time()
    old = now - 172801
    marker = mutation_journal.path("Alpha", "old-create-001")
    atomic_json(marker, {"state": "completed", "response": {"private": "old customer data"}})
    os.utime(marker, (old, old))
    RetentionService(now).run()
    assert json.loads(marker.read_text()) == {"state": "expired"}
    service = FunctionalOperationService()
    monkeypatch.setattr(service, "_runtime", lambda _: (SimpleNamespace(name="Alpha"), object()))
    with pytest.raises(HTTPException, match="410"):
        service.create(OdooRecordCreateRequest(client_name="Alpha", request_id="old-create-001", model="res.partner", records=[{"name": "Duplicate"}]))


def test_cache_expiry_never_follows_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "execution_enabled", False)
    now = time.time()
    old = now - 172801
    root = settings.state_path / "artifacts"
    root.mkdir()
    external = tmp_path / "source.py"
    external.write_text("keep")
    (root / "link.json").symlink_to(external)
    expired = root / "old.json"
    expired.write_text("old payload")
    os.utime(expired, (old, old))
    RetentionService(now).run()
    assert external.read_text() == "keep" and (root / "link.json").is_symlink() and not expired.exists()


def test_root_sweeper_only_removes_explicit_tombstones_and_never_follows_links(tmp_path):
    from deploy.purge_expired_workspaces import purge

    root = tmp_path / "execution"
    records = root / "records" / "workspace"
    records.mkdir(parents=True)
    workspaces = root / "workspaces"
    workspaces.mkdir()
    expired = "a" * 32
    live = "b" * 32
    link = "c" * 32
    for wid in (expired, live):
        (workspaces / wid).mkdir()
        (workspaces / wid / "code.py").write_text("code")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "keep").write_text("keep")
    (workspaces / link).symlink_to(external, target_is_directory=True)
    for wid in (expired, link):
        atomic_json(records / (wid + ".json"), {"id": wid, "kind": "workspace", "status": "expired", "data": {}})
    result = purge(root)
    assert result["expired_directories_removed"] == 2
    assert not (workspaces / expired).exists() and (workspaces / live / "code.py").exists()
    assert (external / "keep").read_text() == "keep"
