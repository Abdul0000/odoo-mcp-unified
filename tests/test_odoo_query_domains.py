import pytest
from pydantic import ValidationError

from odoo_agent_server.odoo_domain import normalize_domain
from odoo_agent_server.schemas import (
    OdooFunctionalQueryRequest, OdooSearchReadRequest, OdooReadGroupRequest,
)
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService


@pytest.mark.parametrize("domain,expected", [
    (None, []), ([], []),
    (["wage", ">", 100000], [["wage", ">", 100000]]),
    (["amount_total", ">=", 42], [["amount_total", ">=", 42]]),
    (["|", ["state", "=", "draft"], ["partner_id.name", "ilike", "Test"]],
     ["|", ["state", "=", "draft"], ["partner_id.name", "ilike", "Test"]]),
    ([["id", "in", [1, 2, 3]], ["active", "=", True]],
     [["id", "in", [1, 2, 3]], ["active", "=", True]]),
    (["!", ["state", "=", "cancel"]], ["!", ["state", "=", "cancel"]]),
])
def test_general_domain_normalization(domain, expected):
    assert normalize_domain(domain) == expected


@pytest.mark.parametrize("domain", [
    [["name", "=", "Test", "extra"]], ["|", ["id", "=", 1]],
    ["!"], ["name"], [["id", 1, 2]], "id > 1",
])
def test_invalid_domains_are_rejected(domain):
    with pytest.raises(ValueError, match="domain"):
        normalize_domain(domain)


@pytest.mark.parametrize("schema,extra", [
    (OdooSearchReadRequest, {}),
    (OdooReadGroupRequest, {"groupby": ["state"]}),
    (OdooFunctionalQueryRequest, {}),
])
def test_query_schemas_share_domain_validation(schema, extra):
    req = schema(model="custom.model", domain=["value", ">", 10], **extra)
    assert req.domain == [["value", ">", 10]]
    with pytest.raises(ValidationError):
        schema(model="custom.model", domain=[["value", ">", 10, 20]], **extra)


@pytest.mark.parametrize("json2", [False, True])
@pytest.mark.parametrize("method", ["search_read", "read_group"])
def test_transports_receive_canonical_domain(monkeypatch, json2, method):
    service = OdooJsonRpcService()
    monkeypatch.setattr(service, "configured", lambda: True)
    monkeypatch.setattr(service, "_api_key", lambda: "key" if json2 else None)
    monkeypatch.setattr(service, "_login_uid", lambda timeout: 7)
    seen = {}

    def rpc(service_name, operation, args, timeout):
        seen["domain"] = args[5][0]
        return []

    def json2_call(model, operation, params, timeout):
        seen["domain"] = params["domain"]
        return []

    monkeypatch.setattr(service, "_call", rpc)
    monkeypatch.setattr(service, "_json2_call", json2_call)
    args = ("custom.model", ["value", ">", 10], ["value"])
    result = service.search_read(*args) if method == "search_read" else service.read_group(*args, ["state"])
    assert result.status == "ok"
    assert seen["domain"] == [["value", ">", 10]]


def test_structured_query_preserves_arbitrary_parameters(monkeypatch):
    from odoo_agent_server.routers import tools
    seen = {}

    def read(**kwargs):
        seen.update(kwargs)
        return {"status": "ok", "odoo": {"result": [{"value": 11}]}}

    monkeypatch.setattr(tools, "_search_read_via_shell", read)
    req = OdooFunctionalQueryRequest(
        model="custom.model", domain=[["relation_id.name", "ilike", "Test"], ["value", ">", 10]],
        fields=["value", "relation_id"], offset=3, order="value desc", context={"active_test": False},
    )
    result = tools._odoo_functional_query(req)
    assert result["status"] == "ok"
    for key in ("model", "domain", "fields", "offset", "order", "context"):
        assert seen[key] == getattr(req, key)


def test_text_filter_is_not_silently_ignored(monkeypatch):
    from odoo_agent_server.routers import tools
    monkeypatch.setattr(tools, "_search_read_via_shell", lambda **kwargs: pytest.fail("must not run an unfiltered read"))
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(query="employees with wages > 100,000"))
    assert result["status"] == "failed"
    assert "explicit model" in result["error"]


def test_structured_parameters_require_model():
    with pytest.raises(ValidationError, match="model is required"):
        OdooFunctionalQueryRequest(query="employees", domain=[["value", ">", 10]])
