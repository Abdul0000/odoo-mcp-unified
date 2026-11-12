from __future__ import annotations

import httpx

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService


def _service() -> OdooJsonRpcService:
    client = ResolvedClient(
        name="test",
        repo="https://github.com/example/odoo.git",
        branch="main",
        path="/tmp/odoo",
        odoo_url="https://example.odoo.com",
        odoo_db="example",
        odoo_api_key="test-api-key",
    )
    return OdooJsonRpcService(client)


def test_json2_search_read_uses_named_parameters(monkeypatch):
    service = _service()
    captured = {}

    def fake_call(model, method, params, timeout_seconds):
        captured.update(model=model, method=method, params=params)
        return [{"id": 7, "name": "Example"}]

    monkeypatch.setattr(service, "_json2_call", fake_call)
    result = service.search_read(
        "res.partner",
        [["is_company", "=", True]],
        ["name"],
        limit=10,
        offset=2,
        order="name asc",
        context={"lang": "en_US"},
    )

    assert result.status == "ok"
    assert captured == {
        "model": "res.partner",
        "method": "search_read",
        "params": {
            "domain": [["is_company", "=", True]],
            "fields": ["name"],
            "limit": 10,
            "offset": 2,
            "order": "name asc",
            "context": {"lang": "en_US"},
        },
    }


def test_json2_read_group_uses_named_parameters(monkeypatch):
    service = _service()
    captured = {}

    def fake_call(model, method, params, timeout_seconds):
        captured.update(model=model, method=method, params=params)
        return [{"__count": 2}]

    monkeypatch.setattr(service, "_json2_call", fake_call)
    result = service.read_group(
        "sale.order",
        [["state", "=", "sale"]],
        ["amount_total:sum"],
        ["date_order:month"],
        context={"active_test": False},
    )

    assert result.status == "ok"
    assert captured["method"] == "read_group"
    assert captured["params"] == {
        "domain": [["state", "=", "sale"]],
        "fields": ["amount_total:sum"],
        "groupby": ["date_order:month"],
        "lazy": False,
        "context": {"active_test": False},
    }


def test_json2_fields_get_uses_api_key_transport(monkeypatch):
    service = _service()
    captured = {}

    def fake_call(model, method, params, timeout_seconds):
        captured.update(model=model, method=method, params=params)
        return {"name": {"type": "char"}}

    monkeypatch.setattr(service, "_json2_call", fake_call)
    result = service.fields_get("res.partner")

    assert result.status == "ok"
    assert captured == {
        "model": "res.partner",
        "method": "fields_get",
        "params": {"attributes": ["type", "readonly", "required", "string", "relation"]},
    }


def test_json2_only_client_does_not_require_legacy_password():
    service = _service()
    assert service.configured()
    assert service._api_key() == "test-api-key"


def test_json2_request_uses_bearer_key_and_database_header(monkeypatch):
    service = _service()
    seen = {}

    class FakeResponse:
        content = b'{"ok": true}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            seen.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert service._json2_call("res.partner", "search_read", {"domain": []}, 30) == {"ok": True}
    assert seen["url"] == "https://example.odoo.com/json/2/res.partner/search_read"
    assert seen["headers"]["Authorization"] == "bearer test-api-key"
    assert seen["headers"]["X-Odoo-Database"] == "example"
    assert seen["json"] == {"domain": []}



def test_json2_create_write_and_action_use_named_parameters(monkeypatch):
    service = _service()
    calls = []

    def fake_call(model, method, params, timeout_seconds):
        calls.append((model, method, params))
        return [21] if method == "create" else True

    monkeypatch.setattr(service, "_json2_call", fake_call)

    created = service.create_records("res.partner", [{"name": "JSON-2"}], {"lang": "en_US"})
    written = service.write_records("res.partner", [21], {"name": "Updated"}, {"lang": "en_US"})
    acted = service.run_action("sale.order", [42], "action_confirm", context={"lang": "en_US"})

    assert created.status == "ok" and created.result == [21]
    assert written.status == "ok" and written.result is True
    assert acted.status == "ok"
    assert calls == [
        ("res.partner", "create", {"vals_list": [{"name": "JSON-2"}], "context": {"lang": "en_US"}}),
        ("res.partner", "write", {"ids": [21], "vals": {"name": "Updated"}, "context": {"lang": "en_US"}}),
        ("sale.order", "action_confirm", {"ids": [42], "context": {"lang": "en_US"}}),
    ]


def test_json2_rejects_positional_action_arguments(monkeypatch):
    service = _service()
    monkeypatch.setattr(
        service,
        "_json2_call",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not send positional args")),
    )

    result = service.run_action("sale.order", [42], "some_action", arguments=["unexpected"])

    assert result.status == "failed"
    assert "named parameters only" in result.error
