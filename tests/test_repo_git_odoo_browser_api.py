from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.schemas import RepoWriteItem
from odoo_agent_server.services.repo_service import RepoService


def _auth():
    return {"Authorization": "Bearer change-me"}


def test_repo_read_write_and_clean_pyc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    write = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "custom/models/demo.py", "content": "print('ok')\n"},
    )
    assert write.status_code == 200
    assert write.json()["path"] == "custom/models/demo.py"

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "custom/models/demo.py"},
    )
    assert read.status_code == 200
    assert "print('ok')" in read.json()["content"]

    cache_dir = project / "custom" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "x.pyc").write_bytes(b"x")
    clean = client.post(
        "/repo/clean-pyc",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "custom"},
    )
    assert clean.status_code == 200
    assert "custom/__pycache__/x.pyc" in clean.json()["removed_files"]
    assert not cache_dir.exists()


def test_repo_write_many_smart_accepts_path_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/repo/write-many-smart",
        headers=_auth(),
        json={
            "project_path": str(project),
            "files": [
                {"path": "sale_order_notebook_tab/models/sale_order.py", "content": "from odoo import fields, models\n"},
                {"path": "sale_order_notebook_tab/views/sale_order_views.xml", "content": "<odoo/>\n"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["exit_code"] == 0
    assert (project / "sale_order_notebook_tab" / "models" / "sale_order.py").read_text(encoding="utf-8") == "from odoo import fields, models\n"
    assert (project / "sale_order_notebook_tab" / "views" / "sale_order_views.xml").read_text(encoding="utf-8") == "<odoo/>\n"


def test_repo_write_many_smart_writes_and_deletes_in_one_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    keep = project / "keep.txt"
    remove = project / "remove.txt"
    keep.write_text("before\n", encoding="utf-8")
    remove.write_text("obsolete\n", encoding="utf-8")

    result = RepoService().write_many_smart(
        str(project),
        [RepoWriteItem(relative_path="keep.txt", content="after\n")],
        delete_paths=["remove.txt"],
    )

    assert result.exit_code == 0
    assert keep.read_text(encoding="utf-8") == "after\n"
    assert not remove.exists()
    assert "deletes=1" in result.stdout


def test_repo_write_many_rejects_write_delete_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()

    with pytest.raises(Exception) as exc_info:
        RepoService().write_many_smart(
            str(project),
            [RepoWriteItem(relative_path="same.txt", content="new\n")],
            delete_paths=["same.txt"],
        )

    assert "cannot be written and deleted" in str(exc_info.value)


def test_repo_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)
    response = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "../evil.txt", "content": "bad"},
    )
    assert response.status_code == 403


@pytest.mark.integration
def test_git_status_current_branch_and_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True)
    (project / "a.txt").write_text("two\n", encoding="utf-8")

    client = TestClient(app)
    status = client.post("/git/status", headers=_auth(), json={"project_path": str(project)})
    assert status.status_code == 200
    assert status.json()["exit_code"] == 0

    diff = client.post("/git/diff", headers=_auth(), json={"project_path": str(project)})
    assert diff.status_code == 200
    assert "two" in diff.json()["stdout"]


def test_odoo_health_without_config_returns_failed(monkeypatch):
    for key in ("ODOO_BASE_URL", "ODOO_DB_NAME", "ODOO_LOGIN", "ODOO_PASSWORD", "ODOO_CLIENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "odoo_base_url", None)
    monkeypatch.setattr(settings, "odoo_db_name", None)
    monkeypatch.setattr(settings, "odoo_login", None)
    monkeypatch.setattr(settings, "odoo_password", None)
    client = TestClient(app)
    response = client.post("/odoo/health", headers=_auth())
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "ODOOSH_DB_NAME" in response.json()["error"]


def test_browser_requires_auth():
    client = TestClient(app)
    response = client.post("/browser/screenshot", json={"base_url": "http://example.com"})
    assert response.status_code == 401
