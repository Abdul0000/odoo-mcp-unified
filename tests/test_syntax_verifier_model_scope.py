from odoo_agent_server.services import syntax_verifier_service as verifier_module
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
