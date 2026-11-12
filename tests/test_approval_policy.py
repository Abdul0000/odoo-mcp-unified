from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.main import app

from odoo_agent_server.config import settings
from odoo_agent_server.services.approval_service import ApprovalService


def test_action_family_approval_rule_requires_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.record_action.*")

    with pytest.raises(HTTPException) as exc:
        ApprovalService().ensure_allowed("odoo.record_action.sale.order.action_confirm", None, {"ids": [1]})

    assert exc.value.status_code == 409
    assert exc.value.detail["approval_required"] is True


def test_read_only_action_is_not_covered_by_mutation_approval_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "git.push,odoo.record_action.*")

    ApprovalService().ensure_allowed("odoo.search_read", None, {"model": "res.partner"})



def test_tools_list_advertises_wildcard_action_approval(monkeypatch):
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.record_action.*")
    response = TestClient(app).get("/tools/list", headers={"Authorization": f"Bearer {settings.api_token}"})

    assert response.status_code == 200
    items = {row["name"]: row for row in response.json()}
    assert items["odoo_record_action"]["requires_approval"] is True
