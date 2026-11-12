import pytest
from pydantic import ValidationError

from odoo_agent_server.schemas import OdooEndpointTestCase
from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoo_endpoint_test_service import OdooEndpointTestService
import odoo_agent_server.services.odoo_endpoint_test_service as endpoint_module


@pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example/x", r"/safe\bad"])
def test_endpoint_case_rejects_non_same_origin_paths(path):
    with pytest.raises(ValidationError):
        OdooEndpointTestCase(name="unsafe", path=path)


def test_endpoint_case_requires_explicit_mutation_at_composite_boundary():
    from odoo_agent_server.schemas import OdooModuleTestRequest

    with pytest.raises(ValidationError):
        OdooModuleTestRequest(
            project_path="/repo",
            module_name="demo",
            layers=["api"],
            api_cases=[{"name": "write", "method": "POST", "path": "/api/write"}],
        )


def test_endpoint_runner_authenticates_and_verifies_json_subset(monkeypatch):
    class Response:
        status_code = 200
        text = '{"result": {"ok": true, "extra": 1}}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"ok": True, "extra": 1}}

    class Session:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, **kwargs):
            assert path == "/web/session/authenticate"
            response = Response()
            response.json = lambda: {"result": {"uid": 7}}
            return response

        def request(self, method, path, **kwargs):
            assert (method, path) == ("GET", "/api/status")
            return Response()

    monkeypatch.setattr(endpoint_module.httpx, "Client", Session)
    client = ResolvedClient(
        name="Test",
        repo="x",
        branch="test",
        path="/repo",
        environment="test",
        odoo_url="https://odoo.example",
        odoo_db="db",
        odoo_login="tester",
        odoo_password="secret",
    )

    result = OdooEndpointTestService(client).run_case(
        OdooEndpointTestCase(
            name="status",
            path="/api/status",
            expected_json_subset={"result": {"ok": True}},
        )
    )

    assert result["passed"] is True
    assert result["verified"] is True
