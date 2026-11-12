from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import JobRecord, JobStatus, OdooModuleTestRequest
from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.module_test_service import ModuleTestService
import odoo_agent_server.services.module_test_service as module_test_module


def _module(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    module = project / "demo_module"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Demo'}", encoding="utf-8")
    (module / "__init__.py").write_text("", encoding="utf-8")
    return project


def _client(project: Path, environment: str = "test") -> ResolvedClient:
    return ResolvedClient(
        name="Test",
        repo="https://example.invalid/repo.git",
        branch="test",
        path=str(project),
        environment=environment,
    )


def test_static_module_test_finishes_without_entering_live_or_deploy_lanes(monkeypatch, tmp_path):
    project = _module(tmp_path)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(module_test_module, "resolve_allowed_path", lambda value: project)
    monkeypatch.setattr(
        module_test_module.ClientResolverService,
        "resolve_client",
        lambda *args, **kwargs: _client(project, "production"),
    )
    monkeypatch.setattr(module_test_module, "submit_background", lambda callback: callback())

    class StaticReport:
        def model_dump(self, **kwargs):
            return {"status": "passed", "findings": [], "summary": {"errors": 0}}

    result = ModuleTestService().start(
        OdooModuleTestRequest(
            project_path=str(project),
            module_name="demo_module",
            layers=["static"],
        ),
        lambda request: StaticReport(),
    )

    assert result["status"] == "passed"
    assert result["result"]["verified"] is True
    assert result["result"]["writes_deployed"] is False


def test_live_module_test_layers_require_explicit_test_environment(monkeypatch, tmp_path):
    project = _module(tmp_path)
    monkeypatch.setattr(module_test_module, "resolve_allowed_path", lambda value: project)
    monkeypatch.setattr(
        module_test_module.ClientResolverService,
        "resolve_client",
        lambda *args, **kwargs: _client(project, "staging"),
    )

    with pytest.raises(HTTPException) as exc:
        ModuleTestService().start(
            OdooModuleTestRequest(
                project_path=str(project),
                module_name="demo_module",
                layers=["api"],
                api_cases=[{"name": "health", "path": "/health"}],
            ),
            lambda request: None,
        )

    assert exc.value.status_code == 400
    assert "environment=test" in str(exc.value.detail)


def test_module_test_job_requires_polling_until_terminal():
    job = JobRecord(job_type="workflow.module_test")

    assert job.completion_required is True
    assert job.success_allowed is False
    assert job.next_call["tool"] == "jobs.get"

    job.status = JobStatus.passed
    assert job.completion_required is False
    assert job.success_allowed is True
