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
def test_client_source_cache_is_isolated_and_failed_refresh_never_uses_other_client(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from odoo_agent_server.config import settings
    from odoo_agent_server.services import core_sync_service as sync
    from odoo_agent_server.services.client_resolver_service import ResolvedClient
    monkeypatch.setattr(settings, 'state_dir', str(tmp_path / 'state'))
    monkeypatch.setattr(settings, 'core_sync_enabled', True)
    clients = {name:ResolvedClient(name=name, repo=f'https://example.invalid/{name}.git', branch='staging',path=str(tmp_path/name),odoosh_ssh_host=name,odoosh_ssh_user='odoo') for name in ['A','B']}
    monkeypatch.setattr(sync, '_resolve_client_for_sync', lambda client_name=None, client=None: client or clients.get(client_name))
    monkeypatch.setattr(sync, '_target_for_sync', lambda c:SimpleNamespace(host=c.name,user='odoo',port=22,key_path=None))
    monkeypatch.setattr(sync, '_deployed_sha', lambda c:None)
    remote='/home/odoo/src/odoo/addons'
    def rsync(argv, **kwargs):
        if 'B:' in argv[-2]:
            return SimpleNamespace(returncode=255,stderr='connection refused')
        destination=__import__('pathlib').Path(argv[-1]);destination.mkdir(parents=True,exist_ok=True)
        (destination/'models').mkdir(exist_ok=True);(destination/'models'/'sale_order.py').write_text('VERSION="A"\n')
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(sync.subprocess,'run',rsync)
    sync._negative_cache.clear()
    assert sync.ensure_module_synced(remote,'sale',client=clients['A'])
    a=sync.cache_root(remote,clients['A']);b=sync.cache_root(remote,clients['B'])
    assert a != b
    assert not sync.ensure_module_synced(remote,'sale',client=clients['B'])
    assert CoreFinderService()._module_dir_with_sync(b,str(b),'sale',client_name='B') is None
    assert (a/'sale/models/sale_order.py').read_text() == 'VERSION="A"\n'
    identity=a/'sale/.source_identity.json';value=json.loads(identity.read_text());value['synced_at']=0;identity.write_text(json.dumps(value))
    monkeypatch.setattr(sync.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=255,stderr='connection refused'))
    assert not sync.ensure_module_synced(remote,'sale',client=clients['A'])


def test_exhausted_source_budget_does_not_open_another_ssh_connection(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from odoo_agent_server.config import settings
    from odoo_agent_server.services import core_sync_service as sync
    monkeypatch.setattr(settings,'core_sync_enabled',True)
    monkeypatch.setattr(sync,'_resolve_client_for_sync',lambda **kw:None)
    monkeypatch.setattr(sync,'_target_for_sync',lambda c:SimpleNamespace(host='fake',user='odoo',port=22,key_path=None))
    monkeypatch.setattr(sync,'_deployed_sha',lambda c:(_ for _ in ()).throw(AssertionError('unexpected SSH')))
    token=sync._deadline.set(time.monotonic()-1)
    try:
        assert not sync.ensure_module_synced(str(tmp_path),'sale')
    finally:
        sync._deadline.reset(token)
