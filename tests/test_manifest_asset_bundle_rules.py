import pytest

from odoo_agent_server.schemas import VerifySeverity
from odoo_agent_server.services import syntax_verifier_service
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService


def _write_asset(addon, relative_path):
    asset_path = addon / relative_path
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text("", encoding="utf-8")
    return f"{addon.name}/{relative_path}"


def test_addon_name_does_not_force_all_assets_into_pos_bundle(tmp_path):
    addon = tmp_path / "pos_chair_management"
    backend_assets = [
        _write_asset(addon, "static/src/design_editor/design_editor_fields.js"),
        _write_asset(addon, "static/src/design_editor/design_editor_fields.xml"),
        _write_asset(addon, "static/src/design_editor/design_editor.scss"),
    ]
    pos_assets = [
        _write_asset(addon, "static/src/pos/controlled_token_sequence.js"),
    ]
    preparation_assets = [
        _write_asset(addon, "static/src/app/services/preparation_display_service_patch.js"),
        _write_asset(addon, "static/src/app/models/order_patch.js"),
    ]
    manifest = addon / "__manifest__.py"
    manifest.write_text(
        repr({
            "name": "POS Chair Management",
            "version": "18.0.1.0.4",
            "assets": {
                "web.assets_backend": backend_assets,
                "point_of_sale._assets_pos": pos_assets,
                "pos_preparation_display.assets": preparation_assets,
            },
        }),
        encoding="utf-8",
    )

    findings = SyntaxVerifierService()._verify_manifest_assets(
        str(manifest),
        manifest.read_text(encoding="utf-8"),
    )

    bundle_findings = [
        finding
        for finding in findings
        if finding.check in {"manifest_pos_asset_bundle", "manifest_website_asset_bundle"}
    ]
    assert bundle_findings == []


def test_path_naming_does_not_claim_bundle_ownership(tmp_path):
    addon = tmp_path / "website_pos_editor_tools"
    assets = [
        _write_asset(addon, "static/src/pos/product_screen_patch.js"),
        _write_asset(addon, "static/src/website/backend_editor.js"),
        _write_asset(addon, "static/src/helpers/pos_website_editor.js"),
    ]
    manifest = addon / "__manifest__.py"
    manifest.write_text(
        repr({
            "name": "Custom Module",
            "version": "18.0.1.0.0",
            "assets": {"custom_application.assets": assets},
        }),
        encoding="utf-8",
    )

    findings = SyntaxVerifierService()._verify_manifest_assets(
        str(manifest),
        manifest.read_text(encoding="utf-8"),
    )

    bundle_findings = [
        finding
        for finding in findings
        if finding.check in {"manifest_pos_asset_bundle", "manifest_website_asset_bundle"}
    ]
    assert bundle_findings == []


@pytest.mark.parametrize("operation", ["replace", "before", "after", "remove"])
def test_asset_directives_resolve_installed_dependency(tmp_path, monkeypatch, operation):
    addon = tmp_path / "custom" / "sample"
    web = tmp_path / "core" / "addons" / "web"
    local = _write_asset(addon, "static/src/custom.js")
    target = _write_asset(web, "static/src/main.js")
    monkeypatch.setattr(syntax_verifier_service, "_iter_module_dirs", lambda name, client: [web] if name == "web" else [])
    directive = (operation, target) if operation == "remove" else (operation, target, local)
    content = repr({"depends": ["web"], "assets": {"web.assets_web": [directive]}})
    assert SyntaxVerifierService()._verify_manifest_assets(str(addon / "__manifest__.py"), content) == []

    (web / "static/src/main.js").unlink()
    findings = SyntaxVerifierService()._verify_manifest_assets(str(addon / "__manifest__.py"), content)
    assert [(item.check, item.severity) for item in findings] == [("manifest_asset_exists", VerifySeverity.CRITICAL)]


@pytest.mark.parametrize("major, expected", [(17, True), (18, False), (19, False)])
def test_headerless_native_javascript_uses_addon_series(tmp_path, major, expected):
    addon = tmp_path / "sample"
    _write_asset(addon, "static/src/example.js")
    (addon / "__manifest__.py").write_text(repr({"version": f"{major}.0.1.0.0"}))
    source = 'import { value } from "./other";\nexport const answer = value;\n'
    findings = SyntaxVerifierService()._verify_js_owl(str(addon / "static/src/example.js"), source, set())
    assert any(item.check == "js_odoo_module_header" for item in findings) == expected
