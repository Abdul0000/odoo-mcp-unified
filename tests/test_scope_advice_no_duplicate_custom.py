from odoo_agent_server.services.core_finder_service import CoreFinderService


def test_scope_advice_does_not_duplicate_current_custom_manifest(tmp_path):
    module = tmp_path / "custom_mod"
    module.mkdir()
    manifest = module / "__manifest__.py"
    manifest.write_text("{'name': 'Custom'}\n", encoding="utf-8")
    model = module / "models.py"
    model.write_text("from odoo import models\n\nclass X(models.Model):\n    _name = 'x.model'\n", encoding="utf-8")

    result = CoreFinderService().scope_advice(
        custom_module_name="custom_mod",
        custom_file_paths=[str(manifest), str(model)],
        task_description="simple custom change",
    )

    assert result.mandatory_count == 0
    assert all(item.read_type != "custom_manifest" for item in result.instructions)
