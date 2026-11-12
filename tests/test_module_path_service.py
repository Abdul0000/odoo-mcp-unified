from pathlib import Path

from odoo_agent_server.services.module_path_service import (
    canonical_module_relative_path,
    is_ignored_source_path,
    module_name_for_repo_relative,
    source_ownership,
)


def test_canonical_module_paths_support_nested_roots_without_guessing():
    root = "addons/vertical/custom_sale"

    assert canonical_module_relative_path("custom_sale", "models/sale.py", root) == f"{root}/models/sale.py"
    assert canonical_module_relative_path("custom_sale", "custom_sale/views/sale.xml", root) == f"{root}/views/sale.xml"
    assert canonical_module_relative_path("custom_sale", f"{root}/security/ir.model.access.csv", root) == f"{root}/security/ir.model.access.csv"


def test_vendor_prefix_and_source_ownership_are_explicit(tmp_path):
    project = tmp_path / "repo"
    custom = project / "custom_sale" / "models" / "sale.py"

    assert is_ignored_source_path(("static", "lib", "vendor.js")) is True
    assert is_ignored_source_path(("static", "src", "js", "app.js")) is False
    assert source_ownership(custom, project) == "client_custom"
    assert source_ownership(Path("/home/odoo/src/enterprise/sale/models/sale.py")) == "enterprise_readonly"
    assert source_ownership(Path("/home/odoo/src/odoo/addons/sale/models/sale.py")) == "community_core_readonly"


def test_manifest_owner_resolution_handles_nested_addon(tmp_path):
    project = tmp_path / "repo"
    module = project / "addons" / "vertical" / "custom_sale"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")

    assert module_name_for_repo_relative(
        project,
        "addons/vertical/custom_sale/models/sale.py",
    ) == "custom_sale"
