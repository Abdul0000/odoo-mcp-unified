from fastapi import HTTPException

from odoo_agent_server.services.core_finder_service import CoreFinderService
from odoo_agent_server.schemas import ScopeAdviceRequest
from odoo_agent_server.routers.workflow import scope_advice


CLIENTS_JSON = '''[
  {"name":"Client A","repo":"https://example.com/client-a.git","branch":"main","path":"/tmp/client-a"},
  {"name":"ClientA","repo":"https://example.com/client-a.git","branch":"main","path":"/tmp/client-a"}
]'''


def test_core_scope_advice_respects_explicit_client_name_in_multiclient(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    result = CoreFinderService().scope_advice(
        custom_module_name="sample_website_module",
        custom_file_paths=[],
        task_description="style-only website change",
        client_name="ClientA",
    )

    assert result.custom_module == "sample_website_module"


def test_core_scope_advice_without_client_name_still_blocks_ambiguous_multiclient(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    try:
        CoreFinderService().scope_advice(
            custom_module_name="sample_website_module",
            custom_file_paths=[],
            task_description="style-only website change",
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Multiple ODOO_CLIENTS entries match" in str(exc.detail)
    else:
        raise AssertionError("Expected ambiguous multi-client scope advice to require client_name")


def test_workflow_scope_advice_forwards_client_name_to_core_finder(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    result = scope_advice(
        ScopeAdviceRequest(
            custom_module_name="sample_website_module",
            custom_file_paths=[],
            task_description="style-only website change",
            client_name="ClientA",
        )
    )

    assert result.custom_module == "sample_website_module"


def test_related_xml_lookup_forwards_client_name_without_name_error(tmp_path, monkeypatch):
    custom = tmp_path / "solar_views.xml"
    custom.write_text(
        '<odoo><record id="solar_view" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '</record></odoo>',
        encoding="utf-8",
    )
    sale = tmp_path / "sale"
    sale.mkdir()
    (sale / "sale_order_views.xml").write_text(
        '<odoo><record id="view_order_form" model="ir.ui.view"/></odoo>',
        encoding="utf-8",
    )
    seen_clients = []
    service = CoreFinderService()

    def fake_module_dir(root_path, root, module_name, client_name=None):
        seen_clients.append(client_name)
        return sale if module_name == "sale" else None

    monkeypatch.setattr(service, "_module_dir_with_sync", fake_module_dir)
    result = service.scope_advice(
        custom_module_name="solar_energy_management",
        custom_file_paths=[str(custom)],
        task_description="Remove groups attributes from Solar XML",
        search_roots=[str(tmp_path)],
        client_name="odoo 18",
    )

    assert "odoo 18" in seen_clients
    assert any(item.read_type == "related_xml_view" for item in result.instructions)
