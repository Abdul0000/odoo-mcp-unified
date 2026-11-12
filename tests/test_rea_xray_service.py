from pathlib import Path

from odoo_agent_server.services.rea_xray_service import ReaXrayService


def _addon(tmp_path: Path) -> Path:
    module = tmp_path / "my_addon"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "__manifest__.py").write_text(
        "{'name': 'My addon', 'version': '18.0.1.0', 'depends': ['base', 'sale']}",
        encoding="utf-8",
    )
    (module / "models" / "sale.py").write_text(
        "from odoo import models, fields\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n"
        "    partner_id = fields.Many2one('res.partner')\n"
        "    def action_confirm(self):\n"
        "        return super().action_confirm()\n",
        encoding="utf-8",
    )
    (module / "views" / "sale.xml").write_text(
        "<odoo>"
        "<record id='view_sale_form' model='ir.ui.view'>"
        "<field name='model'>sale.order</field>"
        "<field name='inherit_id' ref='sale.view_order_form'/>"
        "<field name='arch' type='xml'><form><button name='action_confirm' type='object'/>"
        "<field name='partner_id'/></form></field>"
        "</record>"
        "<record id='sale_action' model='ir.actions.act_window'>"
        "<field name='res_model'>sale.order</field></record>"
        "<menuitem id='sale_menu' name='Sale' action='sale_action'/>"
        "</odoo>",
        encoding="utf-8",
    )
    return module


def test_rea_xray_graph_has_source_evidence(tmp_path):
    module = _addon(tmp_path)
    graph = ReaXrayService().build(module_path=module, module_name="my_addon")
    assert graph["status"] == "mapped"
    edges = graph["edges"]
    assert {"depends_on", "extends_model", "has_method", "has_field",
            "relates_to", "view_for", "inherits_view", "invokes_method",
            "displays_field", "opens_model", "opens_action"} <= {
                edge["kind"] for edge in edges
            }
    assert all(edge["evidence"]["path"] for edge in edges)
    assert any(node["id"] == "method:sale.order.action_confirm" for node in graph["nodes"])
    assert graph["coverage"]["complete"] is True


def test_rea_xray_is_bounded_and_reports_omissions(tmp_path):
    module = _addon(tmp_path)
    for index in range(9):
        (module / "models" / f"extra_{index}.py").write_text("x = 1\n", encoding="utf-8")
    graph = ReaXrayService().build(module_path=module, module_name="my_addon", max_files=3)
    assert graph["coverage"]["complete"] is False
    assert graph["coverage"]["omitted_files"]


def test_rea_xray_never_executes_source(tmp_path):
    module = _addon(tmp_path)
    marker = tmp_path / "executed"
    (module / "models" / "sale.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
        "class SaleOrder:\n    _inherit = 'sale.order'\n",
        encoding="utf-8",
    )
    graph = ReaXrayService().build(module_path=module, module_name="my_addon")
    assert graph["status"] == "mapped"
    assert not marker.exists()


def test_rea_xray_invalid_source_is_reported(tmp_path):
    module = _addon(tmp_path)
    (module / "models" / "sale.py").write_text("def broken(:\n", encoding="utf-8")
    graph = ReaXrayService().build(module_path=module, module_name="my_addon")
    assert any(warning["code"] == "PYTHON_PARSE_ERROR" for warning in graph["warnings"])
