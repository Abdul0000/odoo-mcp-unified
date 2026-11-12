from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.services.test_runner import TestRunner


def test_qa_self_run_fails_cleanly_for_missing_project_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    missing_project = tmp_path / "missing_repo"

    client = TestClient(app)
    response = client.post(
        "/qa/self-run",
        headers={"Authorization": "Bearer change-me"},
        json={
            "project_path": str(missing_project),
            "module_name": "sale_order_notebook_tab",
            "run_pytest": False,
            "include_git_status": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["summary"]["project_path_valid"] is False
    assert data["commands"] == []
    codes = {finding["code"] for finding in data["findings"]}
    assert "PROJECT_PATH_NOT_FOUND" in codes
    assert "MODULE_NOT_FOUND" in codes


def test_test_runner_missing_cwd_is_command_failure(tmp_path):
    missing = tmp_path / "missing_repo"

    result = TestRunner().git_status(missing)

    assert result.exit_code == 127
    assert str(missing) in result.cwd
    assert "could not start" in result.stderr


def test_session_load_state_does_not_reuse_stale_project_path(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    missing_project = tmp_path / "missing_repo"
    monkeypatch.setattr(settings, "state_dir", str(state_dir))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))

    store = JsonStore()
    store.save_session_state({"project_path": str(missing_project), "client_name": "demo"}, client_name="demo")

    state = store.load_session_state(client_name="demo")

    assert state["project_path"] is None
    assert state["project_path_valid"] is False
    assert state["stale_project_path"] == str(missing_project)
    assert "missing" in state["project_path_error"].lower() or "not a directory" in state["project_path_error"].lower()
