from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException

from odoo_agent_server.schemas import OdooRecordActionRequest
from odoo_agent_server.services.functional_operation_service import FunctionalOperationService


def _raise_approval(*args, **kwargs):
    raise HTTPException(
        status_code=409,
        detail={
            "approval_required": True,
            "approval_id": "approval-123",
            "action": "odoo.record_action.stock.picking.button_validate",
            "message": "Approval required",
        },
    )


def test_sensitive_action_returns_resumable_approval_instead_of_exception(monkeypatch):
    service = FunctionalOperationService()
    client = SimpleNamespace(name="Cortex V18")
    monkeypatch.setattr(service, "_runtime", lambda _name: (client, object()))
    monkeypatch.setattr(service, "_ensure_model", lambda _model: None)
    monkeypatch.setattr(service, "_metadata", lambda _rpc, _model: {
        "id": {}, "display_name": {}, "name": {}, "state": {},
    })
    monkeypatch.setattr(service, "_ensure_access", lambda *_args: None)
    monkeypatch.setattr(service, "_verify_fields", lambda _fields, requested, _values: ["id", "state"])
    monkeypatch.setattr(service, "_read_ok", lambda *_args: [{"id": 44, "state": "assigned"}])
    monkeypatch.setattr(service.approvals, "ensure_allowed", _raise_approval)

    result = service.action(OdooRecordActionRequest(
        client_name="Cortex V18",
        request_id="delivery-validate-001",
        model="stock.picking",
        ids=[44],
        action="button_validate",
        verify_fields=["id", "state"],
    ))

    assert result["status"] == "awaiting_approval"
    assert result["approval_id"] == "approval-123"
    assert result["success_allowed"] is False
    assert result["request_id"] == "delivery-validate-001"


def test_dynamic_flow_preserves_pending_approval_for_same_request_retry(monkeypatch):
    from odoo_agent_server.schemas import OdooFlowTestRequest

    service = FunctionalOperationService()
    client = SimpleNamespace(name="Cortex V18")
    monkeypatch.setattr(service, "_runtime", lambda _name: (client, object()))
    monkeypatch.setattr(service, "_ensure_model", lambda _model: None)
    monkeypatch.setattr(service, "_metadata", lambda _rpc, _model: {"id": {}, "state": {}})
    monkeypatch.setattr(service, "_ensure_access", lambda *_args: None)
    monkeypatch.setattr(service, "_save", lambda *_args: (_ for _ in ()).throw(AssertionError("pending flow must not be finalized")))
    monkeypatch.setattr(service, "action", lambda _request: {
        "status": "awaiting_approval",
        "request_id": "same-flow:step:0:validate",
        "model": "stock.picking",
        "action": "button_validate",
        "record_ids": [44],
        "approval_id": "approval-123",
        "approval_action": "odoo.record_action.stock.picking.button_validate",
        "verified": False,
        "success_allowed": False,
    })

    request = OdooFlowTestRequest(
        client_name="Cortex V18",
        request_id="same-flow",
        flow="inventory_transfer",
        mode="persistent_test",
        steps=[{
            "name": "validate",
            "operation": "action",
            "model": "stock.picking",
            "ids": [44],
            "action": "button_validate",
            "verify_fields": ["id", "state"],
        }],
    )
    result = service.flow_test(request)

    assert result["status"] == "awaiting_approval"
    assert result["approval"]["approval_id"] == "approval-123"
    assert result["success_allowed"] is False
    assert "same request_id" in result["next_action"]
