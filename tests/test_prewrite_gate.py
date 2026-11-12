from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.schemas import FrontendPatternPreflightResult
from odoo_agent_server.services.prewrite_gate_service import PreWriteGateService


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _make_module(project: Path) -> Path:
    module = project / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'X Module', 'installable': True}\n", encoding="utf-8")
    (module / "models" / "a.py").write_text("from odoo import models\n\nclass X(models.Model):\n    _name='x.model'\n", encoding="utf-8")
    return module


def test_static_frontend_write_uses_preflight_without_backend_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    module = _make_module(project)
    css = module / "static" / "src" / "scss" / "theme.scss"
    css.parent.mkdir(parents=True)
    css.write_text(".hero { color: red; }\n", encoding="utf-8")
    gate = PreWriteGateService()
    gate.record_repo_inspection(project_path=str(project), relative_path="x_module/static/src/scss/theme.scss")
    result = FrontendPatternPreflightResult(
        target_area="website",
        is_frontend_task=True,
        push_blocked_until_done=False,
        mandatory_read_count=0,
        instructions=[],
        message="focused frontend preflight complete",
    )
    gate.record_frontend_preflight(result, SimpleNamespace(
        client_name=None,
        target_module_name="x_module",
        custom_file_paths=[str(css)],
    ))

    gate.ensure_repo_write_allowed(
        project_path=str(project),
        relative_paths=["x_module/static/src/scss/theme.scss"],
        action="repo.write",
    )


def test_prewrite_gate_blocks_existing_addon_until_custom_read_and_scope_advice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    module = _make_module(project)
    client = TestClient(app)

    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["status"] == "prewrite_gate_blocked"
    assert detail["blocked"][0]["reason"] == "custom_module_not_read"

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py"},
    )
    assert read.status_code == 200

    blocked_scope = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert blocked_scope.status_code == 409
    assert blocked_scope.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"

    scope = client.post(
        "/workflow/scope-advice",
        headers=_auth(),
        json={
            "custom_module_name": "x_module",
            "custom_file_paths": [str(module / "__manifest__.py")],
            "task_description": "edit manifest author",
        },
    )
    assert scope.status_code == 200

    allowed = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert allowed.status_code == 200
    assert "Changed" in (module / "__manifest__.py").read_text(encoding="utf-8")


def test_prewrite_gate_does_not_block_non_addon_helper_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "notes.txt", "content": "ok\n"},
    )
    assert response.status_code == 200


def test_prewrite_gate_rejects_addon_nested_inside_existing_addon(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    _make_module(project)
    client = TestClient(app)

    response = client.post(
        "/repo/write",
        headers=_auth(),
        json={
            "project_path": str(project),
            "relative_path": "x_module/nested_module/__manifest__.py",
            "content": "{'name': 'Nested'}\n",
        },
    )

    assert response.status_code == 400
    assert "Nested Odoo addon rejected" in response.json()["detail"]
    assert not (project / "x_module" / "nested_module" / "__manifest__.py").exists()


def test_prewrite_gate_ui_requires_scope_after_target_read(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    module = _make_module(project)
    (module / "views").mkdir()
    target = module / "views" / "template.xml"
    target.write_text("<odoo><template id='x'>Prices are inclusive</template></odoo>\n", encoding="utf-8")
    client = TestClient(app)

    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo/>\n"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["blocked"][0]["reason"] == "custom_module_not_read"
    assert detail["gate_modes"] == ["mandatory_dual_read"]

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml"},
    )
    assert read.status_code == 200

    blocked_scope = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo><template id='x'><b style='color:red'>Prices are inclusive</b></template></odoo>\n"},
    )
    assert blocked_scope.status_code == 409
    assert blocked_scope.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"

    scope = client.post(
        "/workflow/scope-advice",
        headers=_auth(),
        json={
            "custom_module_name": "x_module",
            "custom_file_paths": [str(target)],
            "task_description": "style template text",
        },
    )
    assert scope.status_code == 200

    allowed = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo><template id='x'><b style='color:red'>Prices are inclusive</b></template></odoo>\n"},
    )
    assert allowed.status_code == 200
    assert "color:red" in target.read_text(encoding="utf-8")


def test_prewrite_gate_models_still_requires_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    _make_module(project)
    client = TestClient(app)

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/models/a.py"},
    )
    assert read.status_code == 200
    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/models/a.py", "content": "from odoo import models\n"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"
