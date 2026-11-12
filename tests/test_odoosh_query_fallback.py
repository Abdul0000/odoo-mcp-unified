import asyncio
import json

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import (
    OdooFlowTestRequest,
    OdooDynamicFlowStep,
    OdooFunctionalQueryRequest,
    OdooRecordActionRequest,
    OdooRecordCreateRequest,
    OdooRecordWriteRequest,
    OdooResult,
)
from odoo_agent_server.services.functional_operation_service import FunctionalOperationService


def _client_env():
    return json.dumps([
        {
            "name": "ClientA",
            "repo": "https://github.com/ClientA/ClientA.git",
            "branch": "22-06-2026",
            "path": "/workspace/repos/ClientA",
            "token": "ghp_test",
            "odoo_url": "https://client-a.example.odoo.com",
            "odoo_db": "client-a-db",
            "odoo_login": "bad@example.com",
            "odoo_password": "bad",
            "odoosh_db_name": "client-a-db",
            "odoosh_ssh_host": "client-a.example.odoo.com",
            "odoosh_ssh_port": 22,
            "odoosh_ssh_user": "34342188",
            "odoosh_ssh_private_key_b64": "ZmFrZQ==",
            "odoosh_odoo_path": "/home/odoo/src/odoo",
            "odoosh_user_addons_path": "/home/odoo/src/user",
        }
    ])


def test_odoo_query_falls_back_to_ssh_when_shell_fails(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context):
        return OdooResult(status="failed", error="Odoo Odoo shell authentication failed")

    def fake_ssh_search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None, timeout_seconds=120):
        assert model == "account.move"
        assert ["move_type", "=", "out_invoice"] in [[*item] for item in domain]
        return OdooResult(
            status="ok",
            database="client-a-db",
            result=[{"name": "INV/2026/00001", "amount_total": 100.0, "state": "posted"}],
        )

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._odoo_functional_query(
        OdooFunctionalQueryRequest(client_name="ClientA", query="latest customer invoices", limit=5)
    )

    assert result["status"] == "ok"
    assert result["source"] == "odoosh_ssh_fallback"
    assert result["ssh_fallback_used"] is True
    assert result["odoo"]["result"][0]["name"] == "INV/2026/00001"


def test_odoo_query_can_infer_client_name_from_text(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    seen = {}

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context):
        return OdooResult(status="failed", error="bad credentials")

    def fake_ssh_search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None, timeout_seconds=120):
        seen["client"] = self.client.name
        return OdooResult(status="ok", database="client-a-db", result=[])

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._odoo_functional_query(
        OdooFunctionalQueryRequest(query="Use ClientA and read users", limit=3)
    )

    assert result["status"] == "ok"
    assert result["client_name"] == "ClientA"
    assert seen["client"] == "ClientA"

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.schemas import CommandResult


def test_odoosh_search_read_parses_shell_marker(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        assert "./odoo-bin shell" in remote_script
        assert "MCP_ODOOSH_QUERY_JSON:" in remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout='noise\nMCP_ODOOSH_QUERY_JSON:{"status":"ok","result":[{"name":"INV/1"}],"count":1}\n',
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).search_read(
        "account.move",
        [["move_type", "=", "out_invoice"]],
        ["name"],
        limit=1,
    )
    assert result.status == "ok"
    assert result.database == "client-a-db"
    assert result.result == [{"name": "INV/1"}]


def test_odoosh_read_group_returns_live_invoice_state_counts(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        assert "model.read_group" in remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout=(
                'MCP_ODOOSH_QUERY_JSON:{"status":"ok","result":'
                '[{"state":"draft","state_count":4},{"state":"posted","state_count":21},'
                '{"state":"cancel","state_count":2}],"count":3}\n'
            ),
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).read_group(
        "account.move",
        [["move_type", "=", "out_invoice"]],
        ["state"],
        ["state"],
    )

    assert result.status == "ok"
    assert result.result[1] == {"state": "posted", "state_count": 21}


def test_feature_inspect_uses_one_shell_for_models_ui_and_security(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )
    calls = []

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        calls.append(remote_script)
        assert "ir.model.fields" in remote_script
        assert "ir.actions.act_window" in remote_script
        assert "ir.model.access" in remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout=(
                'MCP_ODOOSH_FEATURE_JSON:{"status":"ok","result":'
                '{"models":[{"model":"school.facility.room"}],"business_fields":[],"views":[],'
                '"actions":[],"menus":[],"access":[],"rules":[],"selections":[],'
                '"record_counts":{"school.facility.room":0},"samples":{},"model_errors":{}}}\n'
            ),
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).feature_inspect(
        ["school.facility.room", "school.facility.booking"],
        timeout_seconds=60,
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert result.result["record_counts"]["school.facility.room"] == 0


def test_provider_read_group_dispatches_safe_customer_invoice_aggregation(monkeypatch):
    class FakeOdoo:
        def read_group(self, model, domain, fields, groupby, lazy, context, timeout_seconds):
            assert model == "account.move"
            assert domain == [["move_type", "=", "out_invoice"]]
            assert fields == ["state"]
            assert groupby == ["state"]
            return OdooResult(status="ok", result=[{"state": "posted", "state_count": 21}])

    monkeypatch.setattr(tools, "_resolve_query_client", lambda client_name, query_text=None: object())
    monkeypatch.setattr(tools, "_odoo_service_for_client", lambda client: FakeOdoo())
    result = asyncio.run(tools._dispatch("odoo.read_group", {
        "client_name": "ClientA",
        "model": "account.move",
        "domain": [["move_type", "=", "out_invoice"]],
        "fields": ["state"],
        "groupby": ["state"],
    }))

    assert result["status"] == "ok"
    assert result["result"] == [{"state": "posted", "state_count": 21}]


class _FakeMutationRpc:
    def __init__(self):
        self.rows = {
            7: {"id": 7, "display_name": "SO007", "name": "SO007", "state": "draft", "partner_id": [3, "Customer"], "amount_total": 10.0}
        }
        self.created_calls = 0

    def configured(self):
        return True

    def fields_get(self, model):
        names = {
            "id", "display_name", "name", "state", "partner_id", "amount_total",
            "order_line", "client_order_ref", "ref", "move_type", "invoice_line_ids",
            "payment_state", "picking_id", "product_return_moves",
        }
        return OdooResult(status="ok", result={name: {"readonly": name in {"id", "display_name"}} for name in names})

    def check_access_rights(self, model, operation):
        return OdooResult(status="ok", result=True)

    def create_records(self, model, records, context):
        self.created_calls += 1
        record_id = 8
        self.rows[record_id] = {
            "id": record_id,
            "display_name": "SO008",
            "name": "SO008",
            "state": "draft",
            "amount_total": 25.0,
            **records[0],
        }
        return OdooResult(status="ok", result=record_id)

    def read_records(self, model, ids, fields, context):
        return OdooResult(
            status="ok",
            result=[{field: self.rows[item].get(field) for field in fields} for item in ids if item in self.rows],
        )

    def write_records(self, model, ids, values, context):
        for item in ids:
            self.rows[item].update(values)
        return OdooResult(status="ok", result=True)

    def run_action(self, model, ids, action, arguments, keyword_arguments, context):
        for item in ids:
            self.rows[item]["state"] = "posted" if action == "action_post" else "sale"
        return OdooResult(status="ok", result=True)


def _functional_service(tmp_path, monkeypatch):
    fake = _FakeMutationRpc()
    monkeypatch.setattr(settings, "functional_operations_enabled", True)
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(
        "odoo_agent_server.services.functional_operation_service.ClientResolverService.resolve_client",
        lambda self, client_name=None, require_complete=False: None,
    )
    monkeypatch.setattr(
        "odoo_agent_server.services.functional_operation_service.OdooJsonRpcService",
        lambda client=None: fake,
    )
    return FunctionalOperationService(), fake


def test_functional_operations_are_default_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "functional_operations_enabled", False)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    request = OdooRecordCreateRequest(
        request_id="disabled-create-001",
        model="sale.order",
        records=[{"partner_id": 3}],
    )

    with pytest.raises(HTTPException) as exc:
        FunctionalOperationService().create(request)
    assert exc.value.status_code == 403


def test_functional_create_is_verified_and_idempotent(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    request = OdooRecordCreateRequest(
        request_id="sale-create-001",
        model="sale.order",
        records=[{"partner_id": 3, "order_line": [[0, 0, {"product_id": 4, "product_uom_qty": 1}]]}],
        verify_fields=["state", "amount_total"],
    )

    first = service.create(request)
    second = service.create(request)

    assert first["status"] == "ok"
    assert first["verified"] is True
    assert second["idempotent_replay"] is True
    assert fake.created_calls == 1


def test_functional_write_blocks_stale_expected_values(tmp_path, monkeypatch):
    service, _fake = _functional_service(tmp_path, monkeypatch)
    request = OdooRecordWriteRequest(
        request_id="sale-write-001",
        model="sale.order",
        ids=[7],
        values={"client_order_ref": "new-ref"},
        expected_values={"state": "sale"},
    )

    with pytest.raises(HTTPException) as exc:
        service.write(request)
    assert exc.value.status_code == 409


def test_flow_preview_performs_zero_writes(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    result = service.flow_test(
        OdooFlowTestRequest(
            request_id="sale-flow-preview-001",
            flow="sale_order",
            mode="preview",
            values={"partner_id": 3},
            stages=["confirm"],
        )
    )

    assert result["status"] == "preview"
    assert result["write_executed"] is False
    assert fake.created_calls == 0


def test_sensitive_action_requires_payload_bound_human_approval(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    fake.rows[7].update({"move_type": "out_invoice", "payment_state": "not_paid"})
    request = OdooRecordActionRequest(
        request_id="invoice-post-001",
        model="account.move",
        ids=[7],
        action="action_post",
    )

    result = service.action(request)
    assert result["status"] == "awaiting_approval"
    assert result["approval_id"]
    assert result["success_allowed"] is False


def test_action_is_not_verified_when_expected_state_transition_did_not_happen(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    fake.run_action = lambda *args, **kwargs: OdooResult(status="ok", result={"type": "ir.actions.act_window"})
    result = service.action(
        OdooRecordActionRequest(
            request_id="sale-confirm-no-transition-001",
            model="sale.order",
            ids=[7],
            action="action_confirm",
        )
    )

    assert result["status"] == "ok"
    assert result["verified"] is False
    assert result["success_allowed"] is False


def test_public_custom_action_is_allowed_without_static_allowlist(tmp_path, monkeypatch):
    service, _fake = _functional_service(tmp_path, monkeypatch)
    result = service.action(
        OdooRecordActionRequest(
            request_id="sale-custom-action-001",
            model="sale.order",
            ids=[7],
            action="action_custom_process",
        )
    )

    assert result["status"] == "ok"
    assert result["verification_mode"] == "rpc_result_and_read_back"
    assert result["success_allowed"] is True


def test_inventory_return_flow_is_supported_in_preview(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    result = service.flow_test(
        OdooFlowTestRequest(
            request_id="stock-return-preview-001",
            flow="inventory_return",
            mode="preview",
            values={"picking_id": 42, "product_return_moves": [[0, 0, {"product_id": 4, "quantity": 1}]]},
            stages=["create_return"],
        )
    )

    assert result["status"] == "preview"
    assert result["plan"]["model"] == "stock.return.picking"
    assert fake.created_calls == 0


def test_dynamic_flow_chains_create_action_and_verify_with_safe_references(tmp_path, monkeypatch):
    service, fake = _functional_service(tmp_path, monkeypatch)
    result = service.flow_test(
        OdooFlowTestRequest(
            request_id="dynamic-sale-flow-001",
            flow="custom_sale_validation",
            mode="persistent_test",
            steps=[
                OdooDynamicFlowStep(
                    name="create_order",
                    operation="create",
                    model="sale.order",
                    records=[{"partner_id": 3}],
                    verify_fields=["state"],
                ),
                OdooDynamicFlowStep(
                    name="confirm_order",
                    operation="action",
                    model="sale.order",
                    ids_from="create_order.record_ids",
                    action="action_confirm",
                    verify_fields=["state"],
                ),
                OdooDynamicFlowStep(
                    name="verify_order",
                    operation="verify",
                    model="sale.order",
                    ids_from="create_order.record_ids",
                    expected_values={"state": "sale"},
                ),
            ],
        )
    )

    assert result["status"] == "ok"
    assert result["verified"] is True
    assert result["outputs"]["verify_order"]["records"][0]["state"] == "sale"
    assert fake.created_calls == 1
