from odoo_agent_server.services import syntax_verifier_service as verifier_module
from odoo_agent_server.schemas import OdooResult
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService


def _checks(findings):
    return [finding.check for finding in findings]


def test_named_model_mixins_use_class_local_fields_for_related_checks(monkeypatch):
    source = """from odoo import fields, models

class ClinicPrescription(models.Model):
    _name = 'clinic.prescription'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    appointment_id = fields.Many2one('clinic.appointment')
    patient_id = fields.Many2one(related='appointment_id.patient_id')

class ClinicPrescriptionLine(models.Model):
    _name = 'clinic.prescription.line'

    prescription_id = fields.Many2one('clinic.prescription')
    patient_id = fields.Many2one(related='prescription_id.patient_id')
"""
    monkeypatch.setattr(
        verifier_module,
        "_find_core_model_content",
        lambda model_name, client_name=None: "name = fields.Char()\n",
    )
    monkeypatch.setattr(verifier_module, "_module_exists_in_src", lambda *args, **kwargs: True)

    findings = SyntaxVerifierService()._verify_python_model("clinic_prescription.py", source)

    assert "related_field_exists" not in _checks(findings)


def test_extension_model_still_blocks_unknown_related_start_field(monkeypatch):
    source = """from odoo import fields, models

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invalid_partner_name = fields.Char(related='missing_partner_id.name')
"""
    monkeypatch.setattr(
        verifier_module,
        "_find_core_model_content",
        lambda model_name, client_name=None: (
            "class SaleOrder(models.Model):\n"
            "    partner_id = fields.Many2one('res.partner')\n"
        ),
    )
    monkeypatch.setattr(verifier_module, "_module_exists_in_src", lambda *args, **kwargs: True)

    findings = SyntaxVerifierService()._verify_python_model("sale_order.py", source)

    related = [finding for finding in findings if finding.check == "related_field_exists"]
    assert len(related) == 1
    assert "missing_partner_id" in related[0].message


def test_compute_method_is_checked_only_inside_owning_class(monkeypatch):
    source = """from odoo import fields, models

class FirstModel(models.Model):
    _inherit = 'sale.order'
    total = fields.Float(compute='_compute_total')

class SecondModel(models.Model):
    _name = 'second.model'
    def _compute_total(self):
        pass
"""
    monkeypatch.setattr(
        verifier_module,
        "_find_core_model_content",
        lambda model_name, client_name=None: "name = fields.Char()\n",
    )

    findings = SyntaxVerifierService()._verify_python_model("models.py", source)

    missing_compute = [finding for finding in findings if finding.check == "compute_method_defined"]
    assert len(missing_compute) == 1


def test_comodel_resolution_uses_declarations_and_manifest_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier_module, "_core_source_available", lambda client_name=None: True)
    addon = tmp_path / "rethink_browser_connector"
    dependency = tmp_path / "partner_extension"
    undeclared = tmp_path / "unrelated_addon"
    for root in (addon, dependency, undeclared):
        root.mkdir()
        (root / "__manifest__.py").write_text(
            repr({"depends": ["partner_extension"] if root == addon else []}),
            encoding="utf-8",
        )
    for root, model in (
        (addon, "rethink.browser.sync.log"),
        (dependency, "custom.partner.profile"),
        (undeclared, "custom.partner.unrelated"),
    ):
        (root / "records.py").write_text(
            f"from odoo import models\nclass Record(models.Model):\n    _name = {model!r}\n",
            encoding="utf-8",
        )
    source = """from odoo import fields, models
class Connection(models.Model):
    _name = 'rethink.browser.connection'
    logs = fields.One2many('rethink.browser.sync.log', 'connection_id')
    profile = fields.Many2one(comodel_name='custom.partner.profile')
    missing = fields.Many2one('rethink.browser.typo')
    unrelated = fields.Many2one('custom.partner.unrelated')
"""
    path = addon / "connection.py"
    path.write_text(source, encoding="utf-8")
    findings = SyntaxVerifierService()._verify_python_model(str(path), source)
    unresolved = [finding for finding in findings if finding.check == "comodel_module_exists"]
    assert len(unresolved) == 2
    assert "rethink.browser.typo" in unresolved[0].message
    assert "custom.partner.unrelated" in unresolved[1].message
    assert all(finding.severity.value == "CRITICAL" for finding in unresolved)


def test_comodel_registry_fallback_requires_exact_model_and_declared_dependency(tmp_path, monkeypatch):
    addon = tmp_path / "rethink_browser_connector"
    addon.mkdir()
    (addon / "__manifest__.py").write_text("{'depends': ['base']}", encoding="utf-8")
    client = object()
    calls = []

    def search_read(self, model, domain, fields, **kwargs):
        assert self.client is client
        calls.append(domain)
        return OdooResult(status="ok", result=[
            {"model": "res.company", "modules": "base, mail"},
            {"model": "sale.order", "modules": "sale"},
        ])

    monkeypatch.setattr(verifier_module.ClientResolverService, "resolve_client", lambda *a, **kw: client)
    monkeypatch.setattr(verifier_module.OdooService, "search_read", search_read)
    monkeypatch.setattr(verifier_module, "_iter_module_dirs", lambda *a, **kw: [])
    sources = verifier_module._ComodelSources("Anchor", None, ("res.company", "sale.order", "res.typo"))

    assert sources.resolves(addon, "res.company")
    assert not sources.resolves(addon, "sale.order")
    assert not sources.resolves(addon, "res.typo")
    assert calls == [[["model", "in", ["res.company", "sale.order", "res.typo"]]]]
