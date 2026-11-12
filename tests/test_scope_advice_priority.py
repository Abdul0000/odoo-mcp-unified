from pathlib import Path

from odoo_agent_server.services.core_finder_service import CoreFinderService


def test_scope_advice_reads_manifest_dependencies_core_before_enterprise(tmp_path):
    project = tmp_path / "repo"
    module = project / "x_module"
    module.mkdir(parents=True)
    manifest = module / "__manifest__.py"
    manifest.write_text("{'name': 'X', 'depends': ['sale']}\n", encoding="utf-8")

    core = tmp_path / "src" / "odoo"
    enterprise = tmp_path / "src" / "enterprise"
    user = tmp_path / "src" / "user"
    (core / "addons" / "sale").mkdir(parents=True)
    (enterprise / "sale").mkdir(parents=True)
    (core / "addons" / "sale" / "__manifest__.py").write_text("{'name': 'Sale Core'}\n", encoding="utf-8")
    (enterprise / "sale" / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}\n", encoding="utf-8")

    result = CoreFinderService().scope_advice(
        "x_module",
        [str(manifest)],
        "small UI change",
        search_roots=[str(core), str(enterprise), str(user)],
    )

    mandatory = [item.path for item in result.instructions if item.mandatory]
    assert mandatory
    assert mandatory[0] == str(core / "addons" / "sale" / "__manifest__.py")
    assert all("enterprise/sale/__manifest__.py" not in path for path in mandatory)


def test_scope_advice_limits_large_python_file_to_exact_field_unit(tmp_path):
    module = tmp_path / "repo" / "aba_recon_v2"
    model = module / "models" / "recon_session.py"
    model.parent.mkdir(parents=True)
    model.write_text(
        "from odoo import fields, models\n\n"
        "class MatchLine(models.Model):\n"
        "    _name = 'aba.recon.match.line'\n"
        "    invoice_id = fields.Many2one('account.move')\n"
        "    unrelated_picking_id = fields.Many2one('stock.picking')\n",
        encoding="utf-8",
    )
    core = tmp_path / "src" / "odoo"
    for dependency in ("account", "stock"):
        target = core / "addons" / dependency
        target.mkdir(parents=True)
        (target / "__manifest__.py").write_text(
            f"{{'name': '{dependency}'}}\n",
            encoding="utf-8",
        )

    result = CoreFinderService().scope_advice(
        "aba_recon_v2",
        [str(model)],
        "Fix invoice_id on aba.recon.match.line",
        search_roots=[str(core)],
        focus_identifiers=["invoice_id", "aba.recon.match.line"],
    )

    paths = [item.path.replace("\\", "/") for item in result.instructions]
    assert result.focus_applied is True
    assert result.focus_identifiers == ["invoice_id", "aba.recon.match.line"]
    assert any("/account/" in path for path in paths)
    assert not any("/stock/" in path for path in paths)
