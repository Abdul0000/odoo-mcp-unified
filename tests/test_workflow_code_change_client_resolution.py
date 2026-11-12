from types import SimpleNamespace

from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import OdooCodeChangeRequest


def test_resolve_code_change_project_returns_resolved_client_name(monkeypatch, tmp_path):
    resolved = SimpleNamespace(name="Client A", path=str(tmp_path))
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: resolved,
    )

    payload = OdooCodeChangeRequest(
        client_name="Client A",
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name == "Client A"
    assert project_path == str(tmp_path)


def test_resolve_code_change_project_uses_explicit_path_without_client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: None,
    )

    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name is None
    assert project_path == str(tmp_path)
