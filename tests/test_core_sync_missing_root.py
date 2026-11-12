from odoo_agent_server.services.core_finder_service import CoreFinderService


def test_find_core_file_syncs_when_search_root_does_not_exist(tmp_path, monkeypatch):
    remote_root = tmp_path / "home" / "odoo" / "src" / "enterprise"

    def fake_sync(root, module_name):
        assert root == str(remote_root)
        module_dir = remote_root / module_name / "models"
        module_dir.mkdir(parents=True)
        (module_dir / "payment_method.py").write_text("class PaymentMethod: pass\n", encoding="utf-8")
        return True

    monkeypatch.setattr("odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced", fake_sync)

    result = CoreFinderService().find_core_file(
        "point_of_sale",
        "model",
        "payment",
        search_roots=[str(remote_root)],
    )

    assert result.total_found == 1
    assert result.best_match and result.best_match.endswith("payment_method.py")


def test_list_module_files_syncs_when_search_root_does_not_exist(tmp_path, monkeypatch):
    remote_root = tmp_path / "home" / "odoo" / "src" / "enterprise"

    def fake_sync(root, module_name):
        module_dir = remote_root / module_name / "views"
        module_dir.mkdir(parents=True)
        (module_dir / "pos_views.xml").write_text("<odoo/>\n", encoding="utf-8")
        return True

    monkeypatch.setattr("odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced", fake_sync)

    result = CoreFinderService().list_module_files(
        "point_of_sale",
        "views",
        search_roots=[str(remote_root)],
    )

    assert result.found is True
    assert result.total_found == 1


def test_find_core_file_uses_bounded_symbol_fallback_across_enterprise(tmp_path):
    enterprise = tmp_path / "home" / "odoo" / "src" / "enterprise"
    models = enterprise / "sale" / "models"
    models.mkdir(parents=True)
    target = models / "sale_order.py"
    target.write_text(
        "class SaleOrder:\n    def _compute_enterprise_margin(self):\n        return 1\n",
        encoding="utf-8",
    )

    result = CoreFinderService().find_core_file(
        "sale",
        "model",
        "_compute_enterprise_margin",
        search_roots=[str(enterprise)],
    )

    assert result.total_found == 1
    assert result.best_match == str(target)
