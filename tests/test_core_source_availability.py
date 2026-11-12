"""Tests for the core-source-availability guard in syntax_verifier_service.

Bug this fixes: comodel_module_exists, js_import_resolves, and
manifest_depends all hit their CRITICAL branch whenever a module/import
"isn't found" under /home/odoo/src/*. But that hardcoded tree only exists
locally on Odoo.sh; on a Render deployment (or any environment where
ODOOSH_SSH_HOST isn't configured), it never exists and core_sync_service's
rsync silently no-ops every time. Before this fix, that meant PERFECTLY
VALID Odoo code (a real comodel_name, a real @web/ import, a real
depends[] entry) would be reported as CRITICAL and block push — not
because the code was wrong, but because the verifier couldn't reach the
source tree to check it.

These tests simulate "core source unreachable" (no local roots, sync
disabled) and confirm the same findings are downgraded to WARNING with an
explanatory message, instead of silently blocking a legitimate push.
"""
from __future__ import annotations

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import SyntaxVerifyRequest, VerifySeverity
from odoo_agent_server.services import syntax_verifier_service as svs


def _write_addon(tmp_path, name, *, version="18.0.1.0.0", depends=None):
    addon = tmp_path / name
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text(
        repr({
            "name": name,
            "version": version,
            "depends": depends or ["base"],
        }),
        encoding="utf-8",
    )
    views = addon / "views"
    views.mkdir()
    return addon, views


def _write_parent_view(source_root, *, duplicate_partner=False):
    sale = source_root / "sale"
    views = sale / "views"
    views.mkdir(parents=True)
    second = '<field name="partner_id"/>' if duplicate_partner else ""
    (views / "sale_order_views.xml").write_text(
        "<odoo>"
        '<record id="view_order_form" model="ir.ui.view">'
        '<field name="name">sale.order.form</field>'
        '<field name="model">sale.order</field>'
        '<field name="arch" type="xml">'
        f'<form><sheet><field name="partner_id"/>{second}</sheet></form>'
        "</field>"
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )


@pytest.fixture
def core_source_unreachable(monkeypatch):
    """Simulate the Render-without-Odoo.sh-SSH scenario: no local source
    roots exist, and sync is disabled so it can never fetch them either.
    """
    monkeypatch.setattr(svs, "_SRC_ROOTS", [
        __import__("pathlib").Path("/nonexistent/src/user"),
        __import__("pathlib").Path("/nonexistent/src/enterprise"),
        __import__("pathlib").Path("/nonexistent/src/odoo"),
    ])
    monkeypatch.setattr(settings, "core_sync_enabled", False)
    monkeypatch.setattr(settings, "odoosh_ssh_host", None)


def test_core_source_available_is_false_when_unreachable(core_source_unreachable):
    assert svs._core_source_available() is False


def test_core_source_available_is_true_when_sync_configured(monkeypatch):
    monkeypatch.setattr(svs, "_SRC_ROOTS", [
        __import__("pathlib").Path("/nonexistent/src/user"),
        __import__("pathlib").Path("/nonexistent/src/enterprise"),
        __import__("pathlib").Path("/nonexistent/src/odoo"),
    ])
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(settings, "odoosh_ssh_host", "odoosh-build-host.example.com")
    assert svs._core_source_available() is True


def test_comodel_check_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    """This is the exact false-positive: a real comodel_name in a model
    file, checked with no way to reach core source, must not be CRITICAL.
    """
    py_file = tmp_path / "sale_order.py"
    py_file.write_text(
        "from odoo import fields, models\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n"
        "    custom_partner_id = fields.Many2one(comodel_name='res.partner')\n",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(py_file)]))
    comodel_findings = [f for f in result.findings if f.check == "comodel_module_exists"]
    assert comodel_findings, "Expected a comodel_module_exists finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in comodel_findings), (
        "comodel_module_exists must not be CRITICAL when core source is unreachable — "
        "this would false-positive block a valid push."
    )
    assert result.push_blocked is False


def test_manifest_depends_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'version': '1.0', 'depends': ['sale_management']}",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(manifest)]))
    depends_findings = [f for f in result.findings if f.check == "manifest_depends"]
    assert depends_findings, "Expected a manifest_depends finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in depends_findings)
    assert result.push_blocked is False


def test_js_import_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    js_file = tmp_path / "product_screen_patch.js"
    js_file.write_text(
        "/** @odoo-module **/\n"
        "import { ProductScreen } from \"@point_of_sale/app/screens/product_screen/product_screen\";\n"
        "import { patch } from \"@web/core/utils/patch\";\n"
        "patch(ProductScreen.prototype, {});\n",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(js_file)]))
    import_findings = [f for f in result.findings if f.check == "js_import_resolves" and f.severity != VerifySeverity.INFO]
    assert import_findings, "Expected a js_import_resolves finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in import_findings), (
        "js_import_resolves must not be CRITICAL when core source is unreachable — "
        "this is a real @point_of_sale/ and @web/ import that simply couldn't be checked."
    )
    assert result.push_blocked is False


def test_top_level_notice_present_when_source_unreachable(tmp_path, core_source_unreachable):
    py_file = tmp_path / "harmless.py"
    py_file.write_text("from odoo import models\nclass X(models.Model):\n    _name = 'x.y'\n", encoding="utf-8")
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(py_file)]))
    codes = [f.code if hasattr(f, "code") else f.check for f in result.findings]
    assert "core_source_unavailable" in [f.check for f in result.findings]


def test_still_critical_when_source_available_and_dep_genuinely_missing(tmp_path, monkeypatch):
    """Sanity check the other direction: when core source IS reachable and
    a module genuinely doesn't exist, it must still be CRITICAL. The fix
    should only affect the "can't verify" case, not weaken real detection.
    """
    fake_root = tmp_path / "fake_src_user"
    fake_root.mkdir()
    (fake_root / "some_other_module").mkdir()  # non-empty root = source genuinely reachable
    monkeypatch.setattr(svs, "_SRC_ROOTS", [fake_root])
    monkeypatch.setattr(settings, "core_sync_enabled", False)
    monkeypatch.setattr(settings, "odoosh_ssh_host", None)

    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'version': '1.0', 'depends': ['totally_made_up_module_xyz']}",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(manifest)]))
    depends_findings = [f for f in result.findings if f.check == "manifest_depends"]
    assert depends_findings
    assert depends_findings[0].severity == VerifySeverity.CRITICAL, (
        "When a source root DOES exist locally (even if empty), that counts as "
        "'source available' — a genuinely missing module must still be CRITICAL."
    )


def test_view_contract_blocks_missing_dependency_and_legacy_v18_syntax(tmp_path, monkeypatch):
    source_root = tmp_path / "src"
    _write_parent_view(source_root)
    monkeypatch.setattr(svs, "_SRC_ROOTS", [source_root])
    addon, views = _write_addon(tmp_path, "x_view", depends=["base"])
    view_file = views / "sale_order_views.xml"
    view_file.write_text(
        "<odoo>"
        '<record id="sale_order_form_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[@name=&quot;partner_id&quot;]" position="after">'
        '<field name="x_note" attrs="{&quot;invisible&quot;: []}"/>'
        "</xpath>"
        "</field>"
        "</record>"
        '<record id="x_action" model="ir.actions.act_window">'
        '<field name="view_mode">tree,form</field>'
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(view_file)], project_path=str(addon))
    )
    critical_checks = {finding.check for finding in result.findings if finding.severity == VerifySeverity.CRITICAL}
    assert {"xml_owner_dependency", "view_modifiers_version", "view_type_version"} <= critical_checks
    assert result.push_blocked is True


@pytest.mark.parametrize(
    ("duplicate_partner", "expected_severity"),
    [(False, VerifySeverity.INFO), (True, VerifySeverity.CRITICAL)],
)
def test_view_contract_requires_exactly_one_parent_match(
    tmp_path,
    monkeypatch,
    duplicate_partner,
    expected_severity,
):
    source_root = tmp_path / "src"
    _write_parent_view(source_root, duplicate_partner=duplicate_partner)
    monkeypatch.setattr(svs, "_SRC_ROOTS", [source_root])
    addon, views = _write_addon(tmp_path, "x_view", depends=["base", "sale"])
    view_file = views / "sale_order_views.xml"
    view_file.write_text(
        "<odoo>"
        '<record id="sale_order_form_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[@name=&quot;partner_id&quot;]" position="after">'
        '<field name="x_note"/>'
        "</xpath>"
        "</field>"
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(view_file)], project_path=str(addon))
    )
    match_findings = [finding for finding in result.findings if finding.check == "xpath_match_count"]
    assert match_findings
    assert match_findings[0].severity == expected_severity
    assert ("matched exactly one" in match_findings[0].message) is (not duplicate_partner)


def test_view_contract_flags_fragile_xpath_and_noupdate_security_proof(tmp_path, monkeypatch):
    source_root = tmp_path / "src"
    _write_parent_view(source_root)
    monkeypatch.setattr(svs, "_SRC_ROOTS", [source_root])
    addon, views = _write_addon(tmp_path, "x_view", depends=["base", "sale"])
    view_file = views / "sale_order_views.xml"
    view_file.write_text(
        '<odoo><data noupdate="1">'
        '<record id="sale_order_form_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[1]" position="after">'
        '<field name="x_note" groups="base.group_user" invisible="not x_enabled"/>'
        "</xpath>"
        "</field>"
        "</record>"
        "</data></odoo>",
        encoding="utf-8",
    )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(view_file)], project_path=str(addon))
    )
    findings = {finding.check: finding for finding in result.findings}
    assert findings["xpath_stability"].severity == VerifySeverity.CRITICAL
    assert findings["view_noupdate_upgrade"].severity == VerifySeverity.WARNING
    assert findings["view_security_live_proof"].severity == VerifySeverity.INFO


def test_unresolved_view_parent_is_warning_when_source_unreachable(
    tmp_path,
    core_source_unreachable,
):
    addon, views = _write_addon(tmp_path, "x_view", depends=["base", "sale"])
    view_file = views / "sale_order_views.xml"
    view_file.write_text(
        "<odoo>"
        '<record id="sale_order_form_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[@name=&quot;partner_id&quot;]" position="after">'
        '<field name="x_note"/>'
        "</xpath>"
        "</field>"
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(view_file)], project_path=str(addon))
    )
    parent_findings = [finding for finding in result.findings if finding.check == "view_parent_resolves"]
    assert parent_findings
    assert parent_findings[0].severity == VerifySeverity.WARNING
    assert result.push_blocked is False


def test_view_contract_checks_intermediate_inheritance_level(tmp_path, monkeypatch):
    source_root = tmp_path / "src"
    _write_parent_view(source_root)
    sale_extra = source_root / "sale" / "views" / "sale_order_intermediate.xml"
    sale_extra.write_text(
        "<odoo>"
        '<record id="view_order_form_intermediate" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[@name=&quot;missing_field&quot;]" position="after">'
        '<field name="note"/>'
        "</xpath>"
        "</field>"
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )
    monkeypatch.setattr(svs, "_SRC_ROOTS", [source_root])
    addon, views = _write_addon(tmp_path, "x_view", depends=["base", "sale"])
    view_file = views / "sale_order_views.xml"
    view_file.write_text(
        "<odoo>"
        '<record id="sale_order_form_inherit" model="ir.ui.view">'
        '<field name="inherit_id" ref="sale.view_order_form_intermediate"/>'
        '<field name="arch" type="xml">'
        '<xpath expr="//field[@name=&quot;partner_id&quot;]" position="after">'
        '<field name="x_note"/>'
        "</xpath>"
        "</field>"
        "</record>"
        "</odoo>",
        encoding="utf-8",
    )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(view_file)], project_path=str(addon))
    )
    chain_findings = [finding for finding in result.findings if finding.check == "view_inheritance_chain"]
    assert chain_findings
    assert "sale.view_order_form_intermediate" in chain_findings[0].message
    assert chain_findings[0].severity == VerifySeverity.WARNING


def test_view_contract_reports_same_target_priority_interaction(tmp_path, monkeypatch):
    source_root = tmp_path / "src"
    _write_parent_view(source_root)
    monkeypatch.setattr(svs, "_SRC_ROOTS", [source_root])
    addon, views = _write_addon(tmp_path, "x_view", depends=["base", "sale"])
    first_view = views / "first.xml"
    second_view = views / "second.xml"
    for path, xmlid, priority in (
        (first_view, "first_inherit", 10),
        (second_view, "second_inherit", 30),
    ):
        path.write_text(
            "<odoo>"
            f'<record id="{xmlid}" model="ir.ui.view">'
            '<field name="inherit_id" ref="sale.view_order_form"/>'
            f'<field name="priority">{priority}</field>'
            '<field name="arch" type="xml">'
            '<xpath expr="//field[@name=&quot;partner_id&quot;]" position="after">'
            '<field name="x_note"/>'
            "</xpath>"
            "</field>"
            "</record>"
            "</odoo>",
            encoding="utf-8",
        )

    result = svs.SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(first_view)], project_path=str(addon))
    )
    collisions = [finding for finding in result.findings if finding.check == "view_priority_collision"]
    assert collisions
    assert "10 and 30" in collisions[0].message
