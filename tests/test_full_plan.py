from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _make_module(root: Path) -> None:
    module = root / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import x_model\n", encoding="utf-8")
    (module / "models" / "x_model.py").write_text(
        "from odoo import models, fields\n\nclass XModel(models.Model):\n    _name = 'x.model'\n    name = fields.Char()\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name':'X Module','version':'18.0.1.0.0','depends':['base'],'data':['security/ir.model.access.csv','views/x_views.xml']}\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\naccess_x_model,x.model,model_x_model,,1,1,1,1\n",
        encoding="utf-8",
    )
    (module / "views" / "x_views.xml").write_text(
        "<odoo><record id='x_model_tree' model='ir.ui.view'><field name='name'>x.model.tree</field><field name='model'>x.model</field><field name='arch' type='xml'><tree><field name='name'/></tree></field></record></odoo>",
        encoding="utf-8",
    )


def test_approval_gate_can_block_and_then_allow_write(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "a.txt", "content": "hello"},
    )
    assert blocked.status_code == 409
    approval_id = blocked.json()["detail"]["approval_id"]

    approved = client.post("/approvals/approve", headers=_auth(), json={"approval_id": approval_id, "reason": "test"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    allowed = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "a.txt", "content": "hello", "approval_id": approval_id},
    )
    assert allowed.status_code == 200
    assert (project / "a.txt").read_text(encoding="utf-8") == "hello"
    monkeypatch.setattr(settings, "require_approval", False)


def test_tool_call_bridge_can_run_self_qa(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _make_module(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/tools/call",
        headers=_auth(),
        json={
            "name": "qa.self_run",
            "arguments": {"project_path": str(tmp_path), "module_name": "x_module", "run_pytest": False, "include_git_status": False},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["result"]["status"] == "passed"


def test_full_verify_runs_complete_non_remote_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _make_module(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/workflow/full-verify",
        headers=_auth(),
        json={
            "project_path": str(tmp_path),
            "module_name": "x_module",
            "run_pytest": False,
            "include_git_status": False,
            "include_git_diff": False,
            "include_odoosh_errors": False,
            "include_odoo_module_status": False,
            "take_browser_screenshot": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "passed"
    assert payload["summary"]["step_count"] >= 2
    assert any(step["name"] == "qa_self_run" for step in payload["steps"])
