from __future__ import annotations

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService
from odoo_agent_server.config import settings


def test_jsonrpc_respects_per_client_ssl_verify_false(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoo_url="https://client-a.dev.odoo.com",
        odoo_db="client-a-db",
        odoo_login="admin@example.com",
        odoo_password="secret",
        odoo_verify_ssl=False,
    )

    assert OdooJsonRpcService(client)._verify_ssl() is False


def test_jsonrpc_defaults_to_global_ssl_setting(monkeypatch):
    monkeypatch.setattr(settings, "odoo_https_verify_ssl", True)
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoo_url="https://client-a.dev.odoo.com",
        odoo_db="client-a-db",
        odoo_login="admin@example.com",
        odoo_password="secret",
    )

    assert OdooJsonRpcService(client)._verify_ssl() is True


def test_jsonrpc_prefers_nested_odoo_exception_message(monkeypatch):
    import httpx
    import pytest

    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoo_url="https://client-a.dev.odoo.com",
        odoo_db="client-a-db",
        odoo_login="admin@example.com",
        odoo_password="secret",
    )
    service = OdooJsonRpcService(client)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "error": {
                    "message": "Odoo Server Error",
                    "data": {
                        "name": "odoo.exceptions.ValueError",
                        "message": "Invalid field 'detailed_type' on model 'product.product'",
                    },
                }
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(RuntimeError) as exc_info:
        service._call("common", "login", ["db", "login", "password"], 10)

    assert "Invalid field 'detailed_type'" in str(exc_info.value)
    assert "odoo.exceptions.ValueError" in str(exc_info.value)
    assert "Odoo Server Error" not in str(exc_info.value)



def test_configured_read_transport_never_escalates_https_denial_to_ssh(monkeypatch):
    from odoo_agent_server.services.odoo_service import OdooService
    from odoo_agent_server.schemas import OdooResult
    service = OdooService()
    monkeypatch.setattr(OdooJsonRpcService, 'configured', lambda self: True)
    monkeypatch.setattr(OdooJsonRpcService, 'search_read', lambda *a, **k: OdooResult(status='failed', error='AccessError'))
    monkeypatch.setattr(service, '_ssh', lambda: (_ for _ in ()).throw(AssertionError('ACL escalation')))
    assert service.search_read('res.partner').error == 'AccessError'


def test_login_and_query_share_one_remaining_budget(monkeypatch):
    import time
    service = OdooJsonRpcService()
    monkeypatch.setattr(service, 'configured', lambda: True)
    monkeypatch.setattr(service, '_api_key', lambda: None)
    def login(budget):
        time.sleep(.02)
        return 1
    budgets = []
    monkeypatch.setattr(service, '_login_uid', login)
    monkeypatch.setattr(service, '_call', lambda *args: budgets.append(args[-1]) or [])
    assert service.search_read('res.partner', timeout_seconds=.1).status == 'ok'
    assert 0 < budgets[0] < .09


def test_expired_watchdog_deadline_never_starts_https(monkeypatch):
    import time
    import pytest
    from odoo_agent_server.services.deadline_service import read_deadline
    token = read_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            OdooJsonRpcService()._call('common', 'login', [], 60)
    finally:
        read_deadline.reset(token)


def test_selected_client_never_borrows_global_https_identity(monkeypatch):
    monkeypatch.setattr(settings, 'odoo_base_url', 'https://other-client.invalid')
    monkeypatch.setattr(settings, 'odoo_db_name', 'other_db')
    monkeypatch.setattr(settings, 'odoo_login', 'admin')
    monkeypatch.setattr(settings, 'odoo_password', 'secret')
    client = ResolvedClient(name='A', repo='https://example.invalid/a.git', branch='staging', path='/tmp/a')
    assert not OdooJsonRpcService(client).configured()


def test_zero_read_budget_never_starts_transport(monkeypatch):
    from odoo_agent_server.services.odoo_service import OdooService
    monkeypatch.setattr(OdooJsonRpcService, 'configured', lambda self: (_ for _ in ()).throw(AssertionError('transport setup after deadline')))
    assert OdooService().search_read('res.partner', timeout_seconds=0).status == 'failed'


def test_protected_method_keeps_configured_network_timeout(monkeypatch):
    import httpx
    seen = []
    class Client:
        def __init__(self, **kwargs): seen.append(kwargs['timeout'])
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            return type('Response', (), {'raise_for_status': lambda self: None, 'json': lambda self: {'result': True}})()
    service = OdooJsonRpcService()
    monkeypatch.setattr(service, '_base_url', lambda: 'https://fixture.invalid')
    monkeypatch.setattr(httpx, 'Client', Client)
    assert service._call('object', 'execute_kw', ['db', 1, 'secret', 'model', 'write', [], {}], 60)
    assert seen[0].read == 60
