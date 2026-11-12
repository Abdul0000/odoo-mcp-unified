from odoo_agent_server.schemas import CommandResult
from odoo_agent_server.services.odoosh_service import OdooShService


def test_changed_files_and_manifest_use_sequential_verified_helpers(monkeypatch):
    service = OdooShService()
    calls = []
    monkeypatch.setattr(service, "_user_addons_path", lambda: "/home/odoo/src/user")

    def fake_run(script, timeout_seconds=45):
        calls.append(script)
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(service, "run_shell", fake_run)
    result = service.verify_deployed_changes(
        {"x_module/__manifest__.py": "a" * 64},
        module_name="x_module",
        verify_manifest=True,
    )

    assert result.exit_code == 0
    assert len(calls) == 2
    assert "sha256sum" in calls[0]
    assert "__manifest__.py" in calls[1]
    assert "MODULE_MANIFEST_OK" in calls[1]


def test_manifest_verification_is_not_run_after_hash_failure(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(
        service,
        "verify_user_file_hashes",
        lambda hashes: CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x"),
    )
    called = False

    def manifest(_module):
        nonlocal called
        called = True
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0)

    monkeypatch.setattr(service, "verify_module_manifest", manifest)
    result = service.verify_deployed_changes({"x": "a" * 64}, module_name="x", verify_manifest=True)
    assert result.exit_code == 1
    assert called is False
