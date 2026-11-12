from pathlib import Path

import pytest

from odoo_agent_server.services.rea_impact_service import ReaImpactService


def _addon(tmp_path: Path) -> Path:
    root = tmp_path / "my_addon"
    (root / "models").mkdir(parents=True)
    (root / "__manifest__.py").write_text("{'name': 'My Addon', 'depends': ['base']}", encoding="utf-8")
    (root / "models" / "sale_order.py").write_text(
        "from odoo import models\nclass SaleOrder(models.Model):\n    _inherit = 'sale.order'\n    def _compute_amount(self):\n        pass\n",
        encoding="utf-8",
    )
    return root


def test_impact_prediction_follows_graph_and_marks_business_critical_change(tmp_path):
    root = _addon(tmp_path)
    graph = {
        "coverage": {"complete": True},
        "nodes": [
            {"id": "model:sale.order", "kind": "model", "name": "sale.order"},
            {"id": "method:sale.order._compute_amount", "kind": "method", "name": "_compute_amount", "model": "sale.order"},
            {"id": "view:my_addon.sale_form", "kind": "view", "name": "sale_form"},
        ],
        "edges": [
            {"source": "model:sale.order", "target": "method:sale.order._compute_amount", "kind": "has_method", "evidence": {"path": "models/sale_order.py", "line": 4}},
            {"source": "view:my_addon.sale_form", "target": "model:sale.order", "kind": "view_for", "evidence": {"path": "views/sale.xml"}},
        ],
    }
    result = ReaImpactService().predict(
        module_path=root,
        module_name="my_addon",
        changed_files=[{
            "path": "models/sale_order.py",
            "change_type": "modify",
            "diff": "-    def _compute_amount(self):\n+    def _compute_amount(self, discount=None):",
        }],
        graph=graph,
        runtime_events=[{"model": "sale.order", "method": "_compute_amount", "duration_ms": 120}],
        upstream_findings=[{"code": "METHOD_SIGNATURE_DRIFT", "model": "sale.order", "symbol": "_compute_amount"}],
        api_findings=[{"type": "outbound_client_call", "evidence": {"path": "models/sale_order.py", "line": 9}}],
    )
    assert result["status"] == "predicted"
    assert result["risk"] == "high"
    assert result["summary"]["impacted_components"] >= 2
    assert result["summary"]["runtime_trace_matches"] == 1
    assert result["summary"]["upstream_matches"] == 1
    assert result["summary"]["api_integration_matches"] == 1
    assert any(item["id"] == "method:sale.order._compute_amount" for item in result["impacted_components"])


def test_impact_prediction_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="safe addon-relative"):
        ReaImpactService().predict(
            module_path=_addon(tmp_path),
            module_name="my_addon",
            changed_files=[{"path": "../outside.py"}],
        )


def test_impact_prediction_requires_bounded_change_list(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 30"):
        ReaImpactService().predict(
            module_path=_addon(tmp_path),
            module_name="my_addon",
            changed_files=[],
        )
