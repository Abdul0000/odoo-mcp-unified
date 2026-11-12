from odoo_agent_server.config import settings
from odoo_agent_server.services import odoo_code_search_service as search_module
from odoo_agent_server.services.odoo_code_search_service import OdooCodeSearchService


def _build_module(tmp_path):
    project = tmp_path / "repo"
    module = project / "rethink_bh_connector"
    (module / "models" / "billing").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "__manifest__.py").write_text(
        "{'name': 'Rethink BH Connector', 'depends': ['base', 'account'], 'data': ['views/billing_ready_views.xml']}\n",
        encoding="utf-8",
    )
    (module / "models" / "billing" / "billing_ready.py").write_text(
        """from odoo import fields, models


class BillingReady(models.Model):
    _name = "rethink.billing.ready"
    _description = "Billing Ready Records"

    billing_code_id = fields.Many2one("rethink.billing.code", string="Billing Code")
    rate = fields.Float(related="billing_code_id.rate", string="Rate", store=True)
""",
        encoding="utf-8",
    )
    (module / "views" / "billing_ready_views.xml").write_text(
        """<odoo>
    <record id="view_billing_ready_tree" model="ir.ui.view">
        <field name="name">Billing Ready Records</field>
        <field name="model">rethink.billing.ready</field>
        <field name="arch" type="xml">
            <list string="Billing Ready Records">
                <field name="billing_code_id"/>
                <field name="rate"/>
            </list>
        </field>
    </record>
</odoo>
""",
        encoding="utf-8",
    )
    return project, module


def test_odoo_search_maps_functional_label_to_model_and_view(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Add a visible field 'Rate' to Billing Ready Records from billing_code_id.rate.",
        target_hint="Billing Ready Records billing_code_id rate",
        intent="add_field",
        task_type="python_model",
    )

    paths = [match["path"] for match in result["matches"]]
    assert result["engine"] == "odoo_code_graph"
    assert "Billing Ready Records" in result["clues"]
    assert "billing_code_id.rate" in result["clues"]
    assert "rethink_bh_connector/models/billing/billing_ready.py" in paths
    assert "rethink_bh_connector/views/billing_ready_views.xml" in paths
    assert all(match["lines"] for match in result["matches"][:2])
    assert result["confidence"] >= 0.8


def test_odoo_search_follows_xml_res_model_relationship_to_python(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Update the Billing Ready Records list view.",
        target_hint="Billing Ready Records",
        task_type="xml_view",
    )

    by_path = {match["path"]: match for match in result["matches"]}
    model_match = by_path["rethink_bh_connector/models/billing/billing_ready.py"]
    assert any("relationship target" in reason or "exact description" in reason for reason in model_match["reasons"])


def test_clue_expansion_is_bounded_and_generates_functional_variants():
    clues = OdooCodeSearchService.expand_clues(
        "Change method _compute_rate() for Billing Ready Records and XML ID rethink.view_billing_ready.",
        "Billing Ready Records billing_code_id rate",
        "add_field",
        [],
    )

    assert "Billing Ready Records" in clues
    assert "billing_code_id" in clues
    assert "rethink.view_billing_ready" in clues
    assert "_compute_rate" in clues
    assert len(clues) <= 25


def test_search_traces_menu_to_action_to_python_model_across_files(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    (module / "views" / "billing_menu.xml").write_text(
        '<odoo><menuitem id="menu_billing_ready" name="Billing Ready Records" action="action_billing_ready"/></odoo>\n',
        encoding="utf-8",
    )
    (module / "views" / "billing_action.xml").write_text(
        """<odoo>
    <record id="action_billing_ready" model="ir.actions.act_window">
        <field name="name">Ready Queue</field>
        <field name="res_model">rethink.billing.ready</field>
    </record>
</odoo>
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Change Billing Ready Records",
        target_hint="Billing Ready Records",
        task_type="auto",
    )

    paths = {match["path"] for match in result["matches"]}
    assert "rethink_bh_connector/views/billing_menu.xml" in paths
    assert "rethink_bh_connector/views/billing_action.xml" in paths
    assert "rethink_bh_connector/models/billing/billing_ready.py" in paths


def test_search_index_cache_invalidates_when_dirty_file_changes(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", True)
    monkeypatch.setattr(settings, "pattern_cache_dir", str(tmp_path / "cache"))
    service = OdooCodeSearchService()
    arguments = {
        "project_path": str(project),
        "module_path": module.relative_to(project).as_posix(),
        "task_description": "Inspect Billing Ready Records",
        "target_hint": "Billing Ready Records",
    }

    first = service.search(**arguments)
    second = service.search(**arguments)
    model_file = module / "models" / "billing" / "billing_ready.py"
    model_file.write_text(model_file.read_text(encoding="utf-8") + "\n# cache invalidation\n", encoding="utf-8")
    third = service.search(**arguments)

    assert first["cached"] is False
    assert second["cached"] is True
    assert third["cached"] is False


def test_search_builds_one_universal_module_index(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    unrelated = module / "models" / "unrelated"
    unrelated.mkdir()
    for index in range(30):
        (unrelated / f"noise_{index}.py").write_text(
            f'class Noise{index}:\n    value = "unrelated"\n', encoding="utf-8"
        )
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Change Billing Ready Records rate",
        target_hint="Billing Ready Records billing_code_id.rate",
        task_type="python_model",
    )

    assert result["strategy"] == "hybrid_ranked_module_index"
    assert result["indexed_files"] >= 30
    assert result["scanned_files"] >= 33
    assert result["indexed_files"] == result["scanned_files"]


def test_search_reuses_same_index_for_a_different_query(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", True)
    monkeypatch.setattr(settings, "pattern_cache_dir", str(tmp_path / "cache"))
    service = OdooCodeSearchService()

    first = service.search(
        project_path=str(project), module_path=module.relative_to(project).as_posix(),
        task_description="Inspect Billing Ready Records", target_hint="Billing Ready Records",
    )
    second = service.search(
        project_path=str(project), module_path=module.relative_to(project).as_posix(),
        task_description="Inspect the billing action menu", target_hint="billing action",
    )

    assert first["cached"] is False
    assert second["cached"] is True


def test_search_skips_vendored_static_lib_and_marks_custom_ownership(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    vendor = module / "static" / "lib" / "vendor"
    vendor.mkdir(parents=True)
    for index in range(25):
        (vendor / f"noise_{index}.js").write_text(
            f'const billing_code_id = "vendor-{index}";\n', encoding="utf-8"
        )
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Change billing_code_id on Billing Ready Records",
        target_hint="billing_code_id",
        task_type="python_model",
    )

    assert result["indexed_files"] == 3
    assert all("/static/lib/" not in f"/{item['path']}" for item in result["matches"])
    assert result["matches"][0]["ownership"] == "client_custom"
    assert result["matches"][0]["writable"] is True
    assert result["matches"][0]["role"] == "primary"


def test_python_task_ranks_field_definition_before_xml_reference(tmp_path, monkeypatch):
    project, module = _build_module(tmp_path)
    monkeypatch.setattr(search_module, "resolve_allowed_path", lambda value: project.resolve())
    monkeypatch.setattr(settings, "repo_index_enabled", False)

    result = OdooCodeSearchService().search(
        project_path=str(project),
        module_path=module.relative_to(project).as_posix(),
        task_description="Change the billing_code_id field logic",
        target_hint="billing_code_id",
        task_type="python_model",
    )

    assert result["matches"][0]["path"].endswith("models/billing/billing_ready.py")
    assert result["channels"] == [
        "symbol_graph", "path_match", "label_value", "bounded_relationship_expansion"
    ]
