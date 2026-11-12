from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.schemas import JobRecord, JobStatus, OdooResult
from odoo_agent_server.services.odoo_service import OdooService
from odoo_agent_server.services.store_service import JsonStore


def test_jobs_get_waits_until_background_job_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="test.wait", status=JobStatus.running))

    def finish_later():
        time.sleep(0.3)
        store.finish_job(store.get_job(job.id), JobStatus.passed, {"ok": True})

    threading.Thread(target=finish_later, daemon=True).start()
    client = TestClient(app)
    response = client.post(
        "/tools/call",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "name": "jobs.get",
            "arguments": {"job_id": job.id, "wait_timeout_seconds": 2, "poll_interval_seconds": 0.25},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["result"]["status"] == "passed"
    assert data["result"]["result"] == {"ok": True}


def test_module_deploy_waits_and_returns_final_job(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)

    def fake_module_deploy(self, module_name):
        time.sleep(0.3)
        return OdooResult(status="ok", result={"module_name": module_name, "operation": "upgrade"})

    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)
    client = TestClient(app)
    response = client.post(
        "/tools/call",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "name": "odoo.module_deploy",
            "arguments": {"module_name": "sample_custom_module", "wait_timeout_seconds": 2, "poll_interval_seconds": 0.25},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    result = data["result"]
    assert result["status"] == "failed"
    assert result["result"]["finished"] is False
    assert result["result"]["success_allowed"] is False
    assert result["result"]["next_call"]["tool"] == "jobs.get"


def test_module_deploy_non_wait_mode_returns_job_id_for_polling(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)

    def slow_module_deploy(self, module_name):
        time.sleep(0.6)
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooService, "module_deploy", slow_module_deploy)
    client = TestClient(app)
    response = client.post(
        "/tools/call",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "name": "odoo.module_deploy",
            "arguments": {"module_name": "sample_custom_module", "wait_for_result": False, "poll_interval_seconds": 0.25},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    result = data["result"]["result"]
    assert result["finished"] is False
    assert result["job_status"] == "running"
    assert "job_id" in result


def test_module_deploy_wait_for_result_true_never_returns_progress_text(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)

    def slow_module_deploy(self, module_name):
        time.sleep(0.3)
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooService, "module_deploy", slow_module_deploy)
    client = TestClient(app)
    response = client.post(
        "/tools/call",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "name": "odoo.module_deploy",
            "arguments": {"module_name": "sample_custom_module", "wait_for_result": True, "wait_timeout_seconds": 0, "poll_interval_seconds": 0.25},
        },
    )

    assert response.status_code == 200
    data = response.json()
    text = str(data).lower()
    assert data["status"] == "ok"
    assert data["result"]["status"] == "failed"
    assert data["result"]["result"]["finished"] is False
    assert data["result"]["result"]["success_allowed"] is False
    assert data["result"]["result"]["next_call"]["tool"] == "jobs.get"
    assert "may take a few minutes" not in text
    assert "check back" not in text
