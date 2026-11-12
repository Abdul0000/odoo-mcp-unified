from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.odoo import start_odoo_module_deploy_job
from odoo_agent_server.schemas import CommandResult, OdooResult
from odoo_agent_server.schemas import OdooCodeChangeRequest, OdooModuleRequest
from odoo_agent_server.routers.tools import _is_installability_repair_request
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.odoo_service import OdooService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.services.store_service import JsonStore


def test_odoosh_run_shell_streams_complex_script_over_stdin(monkeypatch):
    service = OdooShService()
    captured = {}
    monkeypatch.setattr(service, "_target", lambda: object())
    monkeypatch.setattr(service, "_base_ssh_command", lambda target: ["ssh", "odoo@example"])

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("odoo_agent_server.services.odoosh_service.subprocess.run", fake_run)
    script = "value='quoted'; printf '%s\\n' \"$value\"\ncat <<'PY'\n{'nested': \"quotes\"}\nPY"

    result = service.run_shell(script)

    assert result.exit_code == 0
    assert captured["command"] == ["ssh", "odoo@example", "sh", "-s"]
    assert captured["kwargs"]["input"] == script + "\n"
    assert script not in captured["command"]


def test_approval_required_actions_do_not_block_when_global_approval_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "repo.write,odoo.module_deploy,odoosh.module_update")

    # Must not raise even though the action list contains the action.
    ApprovalService().ensure_allowed("odoo.module_deploy", None, {"module_name": "x"})


def test_tools_list_reports_no_approval_when_global_approval_disabled(monkeypatch):
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.module_deploy,odoosh.module_update")
    client = TestClient(app)

    response = client.get("/tools/list", headers={"Authorization": f"Bearer {settings.api_token}"})

    assert response.status_code == 200
    items = {row["name"]: row for row in response.json()}
    assert items["odoo_module_deploy"]["requires_approval"] is False
    assert items["odoosh_module_update"]["requires_approval"] is False


def test_module_deploy_falls_back_to_ssh_when_update_list_access_denied(monkeypatch):
    def fake_execute_kw(self, model, method, args=None, kwargs=None, superuser=False):
        assert model == "ir.module.module"
        assert method == "update_list"
        return OdooResult(status="failed", error="<Fault 3: 'Access Denied'>")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_OK")

    monkeypatch.setattr(OdooService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooService().module_deploy("sample_custom_module")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert result.result["ssh"]["stdout"] == "SSH_OK"


def test_module_deploy_falls_back_to_ssh_when_module_missing_after_update_list(monkeypatch):
    def fake_execute_kw(self, model, method, args=None, kwargs=None, superuser=False):
        if model == "ir.module.module" and method == "update_list":
            return OdooResult(status="ok", result=True)
        if model == "ir.module.module" and method == "search_read":
            return OdooResult(status="ok", result=[])
        raise AssertionError(f"unexpected Odoo shell call: {model}.{method}")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_DEPLOY_OK")

    monkeypatch.setattr(OdooService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooService().module_deploy("new_custom_module")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert result.result["strategy"] == "hybrid_psql_state_then_single_cli_with_single_shell_fallback"
    assert result.result["ssh"]["stdout"] == "SSH_DEPLOY_OK"


def test_module_deploy_exposes_structured_uninstallable_diagnostic(monkeypatch):
    diagnostic = {
        "failure_kind": "registry_uninstallable_after_refresh",
        "deployed_manifest": {"installable": True, "depends": ["base"]},
        "dependency_states": [{"name": "base", "state": "installed"}],
        "source_repair_allowed": False,
    }
    monkeypatch.setattr(
        OdooShService,
        "module_deploy_ssh",
        lambda self, module_name, timeout_seconds=600: CommandResult(
            command=["odoosh.module_shell_deploy", module_name],
            cwd="odoosh:ssh",
            exit_code=3,
            stderr="MCP_MODULE_DEPLOY_DIAGNOSTIC:" + __import__("json").dumps(diagnostic),
        ),
    )

    result = OdooService().module_deploy("iot_device_management")

    assert result.status == "failed"
    assert result.result["deploy_diagnostic"] == diagnostic


def test_failed_deploy_persists_source_repair_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    diagnostic = {
        "failure_kind": "registry_uninstallable_after_refresh",
        "deployed_manifest": {"installable": True},
        "source_repair_allowed": False,
    }
    monkeypatch.setattr(
        OdooService,
        "module_deploy",
        lambda self, module_name: OdooResult(
            status="failed",
            error="registry diagnostic",
            result={"deploy_diagnostic": diagnostic},
        ),
    )

    result = start_odoo_module_deploy_job(OdooModuleRequest(
        module_name="iot_device_management",
        wait_timeout_seconds=2,
        poll_interval_seconds=0.25,
    ))

    assert result.status == "failed"
    stored = JsonStore().load_session_state()["module_deploy_diagnostics"]
    stored_diagnostic = stored["iot_device_management"]
    assert {key: stored_diagnostic[key] for key in diagnostic} == diagnostic
    assert stored_diagnostic["recorded_at"]


def test_internal_module_deploy_job_does_not_require_second_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.module_deploy")

    def fake_module_deploy(self, module_name):
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(OdooModuleRequest(
        module_name="sample_custom_module",
        wait_timeout_seconds=2,
        poll_interval_seconds=0.25,
    ))

    assert result.status == "ok"
    assert result.result["finished"] is True
    assert result.result["job_status"] == "passed"


def test_public_module_deploy_blocks_after_manual_push_until_sha_deployed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=1, stdout='{"deployed": false}')

    def fake_module_deploy(self, module_name):
        raise AssertionError("module_deploy must not run before Odoo.sh deploy guard passes")

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "failed"
    assert result.result["job_status"] == "failed"
    assert "stale code" in result.error
    assert result.result["job"]["result"]["result"]["status"] == "deploy_failed_or_timeout"


def test_public_module_deploy_blocks_when_manual_push_hash_mismatches(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def fake_recent(self, since_epoch):
        return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=0, stdout='{"failed": false}')

    def fake_hashes(self, expected):
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x_module/models/a.py")

    def fake_module_deploy(self, module_name):
        raise AssertionError("module_deploy must not run when deployed source mismatches")

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooShService, "recent_build_failure", fake_recent)
    monkeypatch.setattr(OdooShService, "verify_user_file_hashes", fake_hashes)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "failed"
    assert result.result["job_status"] == "failed"
    assert "/home/odoo/src/user" in result.error
    assert result.result["job"]["result"]["result"]["status"] == "deploy_code_mismatch"


def test_public_module_deploy_runs_after_manual_push_guard_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })
    calls = {"deploy": 0, "hash": 0}

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def fake_recent(self, since_epoch):
        return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=0, stdout='{"failed": false}')

    def fake_hashes(self, expected):
        calls["hash"] += 1
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

    def fake_module_deploy(self, module_name):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooShService, "recent_build_failure", fake_recent)
    monkeypatch.setattr(OdooShService, "verify_user_file_hashes", fake_hashes)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "ok"
    assert result.result["job_status"] == "passed"
    assert calls == {"deploy": 1, "hash": 1}
    assert JsonStore().load_session_state()["last_git_push"]["verified_for_module_deploy"] is True


def test_odoosh_module_deploy_ssh_uses_upgrade_for_installed_module(monkeypatch):
    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["module_name"] = module_name
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: "installed")
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("sample_custom_module")

    assert result.exit_code == 0
    assert called == {"module_name": "sample_custom_module", "flag": "-u"}
    assert "probe=psql mode=upgrade" in result.stdout


def test_cli_not_installable_failure_reads_deployed_manifest_before_any_repair(monkeypatch):
    diagnostic = {
        "failure_kind": "cli_not_installable",
        "deployed_manifest": {"installable": True, "depends": ["base"]},
        "dependency_states": [{"name": "base", "state": "installed"}],
        "source_repair_allowed": False,
    }
    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: "installed")
    monkeypatch.setattr(
        OdooShService,
        "_run_module_cli",
        lambda self, module_name, flag, timeout_seconds: CommandResult(
            command=["odoo-bin", flag, module_name],
            cwd="odoosh:ssh",
            exit_code=3,
            stderr=f"module {module_name}: not installable, skipped",
        ),
    )
    calls = {"diagnose": 0}

    def fake_diagnose(self, module_name):
        calls["diagnose"] += 1
        return diagnostic

    monkeypatch.setattr(OdooShService, "_diagnose_deployed_manifest", fake_diagnose)

    result = OdooShService().module_deploy_ssh("iot_device_management")

    assert result.exit_code == 3
    assert calls["diagnose"] == 1
    assert "MCP_MODULE_DEPLOY_DIAGNOSTIC:" in result.stderr
    assert '"installable": true' in result.stderr
    assert '"source_repair_allowed": false' in result.stderr


def test_deployed_manifest_diagnostic_parses_true_installable_and_dependency_states(monkeypatch):
    service = OdooShService()
    payload = {
        "status": "failed",
        "failure_kind": "cli_not_installable",
        "module": "iot_device_management",
        "deployed_manifest": {"installable": True, "depends": ["base"]},
        "dependency_states": [{"name": "base", "state": "installed"}],
        "source_repair_allowed": False,
    }
    monkeypatch.setattr(
        service,
        "run_shell",
        lambda script, timeout_seconds=30: CommandResult(
            command=["ssh"], cwd="odoosh:ssh", exit_code=0,
            stdout="MCP_DEPLOYED_MANIFEST_JSON:" + __import__("json").dumps(payload),
        ),
    )

    result = service._diagnose_deployed_manifest("iot_device_management")

    assert result["deployed_manifest"]["installable"] is True
    assert result["dependency_states"] == [{"name": "base", "state": "installed"}]
    assert result["source_repair_allowed"] is False


def test_temporary_odoo_process_includes_standard_enterprise_addons_paths(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src/odoo")
    monkeypatch.setattr(service, "_user_addons_path", lambda: "/home/odoo/src/user")

    paths = service._addons_path().split(",")

    assert "/home/odoo/src/odoo/odoo/addons" in paths
    assert "/home/odoo/src/enterprise" in paths
    assert "/home/odoo/src/themes" in paths
    assert "/home/odoo/src/design-themes" in paths
    assert "/home/odoo/src/user" in paths
    setup = service._addons_path_shell_setup()
    assert '[ -d "$MCP_ADDON_DIR" ]' in setup
    assert 'MCP_ADDONS_PATH="$MCP_ADDONS_PATH,$MCP_ADDON_DIR"' in setup


def test_stale_install_diagnostic_does_not_block_normal_feature_work():
    normal = OdooCodeChangeRequest(
        module_name="iot_device_management",
        task_description="Add a device status field and show it in the list view",
    )
    repair = OdooCodeChangeRequest(
        module_name="iot_device_management",
        task_description="Repair the manifest because Odoo says the module is not installable",
    )

    assert _is_installability_repair_request(normal) is False
    assert _is_installability_repair_request(repair) is True


def test_odoosh_module_deploy_ssh_uses_install_for_uninstalled_module(monkeypatch):
    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: "uninstalled")
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("new_module")

    assert result.exit_code == 0
    assert called["flag"] == "-i"
    assert "probe=psql mode=install" in result.stdout


def test_odoosh_module_deploy_uses_single_shell_fallback_when_psql_is_unavailable(monkeypatch):
    calls = {"shell": 0, "cli": 0}

    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: None)

    def fake_shell(self, module_name, timeout_seconds):
        calls["shell"] += 1
        return CommandResult(command=["odoo-bin", "shell"], cwd="odoosh:ssh", exit_code=0, stdout="MCP_HYBRID_DEPLOY probe=odoo_shell")

    def fake_cli(self, module_name, flag, timeout_seconds):
        calls["cli"] += 1
        return CommandResult(command=["odoo-bin", flag], cwd="odoosh:ssh", exit_code=0)

    monkeypatch.setattr(OdooShService, "_run_module_shell_deploy", fake_shell)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_cli)

    result = OdooShService().module_deploy_ssh("sample_custom_module")

    assert result.exit_code == 0
    assert calls == {"shell": 1, "cli": 0}


def test_stale_uninstallable_state_forces_update_list_shell_refresh(monkeypatch):
    calls = {"shell": 0, "cli": 0}
    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: "uninstallable")

    def fake_shell(self, module_name, timeout_seconds):
        calls["shell"] += 1
        return CommandResult(
            command=["odoo-bin", "shell"], cwd="odoosh:ssh", exit_code=0,
            stdout="MCP_HYBRID_DEPLOY probe=odoo_shell mode=install state=uninstalled",
        )

    def fake_cli(self, module_name, flag, timeout_seconds):
        calls["cli"] += 1
        return CommandResult(command=["odoo-bin", flag], cwd="odoosh:ssh", exit_code=0)

    monkeypatch.setattr(OdooShService, "_run_module_shell_deploy", fake_shell)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_cli)

    result = OdooShService().module_deploy_ssh("iot_device_management")

    assert result.exit_code == 0
    assert calls == {"shell": 1, "cli": 0}


def test_not_found_state_also_refreshes_apps_registry_before_install(monkeypatch):
    calls = {"shell": 0, "cli": 0}
    monkeypatch.setattr(OdooShService, "_module_state_via_psql", lambda self, module_name: "not_found")
    monkeypatch.setattr(
        OdooShService,
        "_run_module_shell_deploy",
        lambda self, module_name, timeout_seconds: (
            calls.__setitem__("shell", calls["shell"] + 1)
            or CommandResult(command=["odoo-bin", "shell"], cwd="odoosh:ssh", exit_code=0, stdout="refreshed")
        ),
    )
    monkeypatch.setattr(
        OdooShService,
        "_run_module_cli",
        lambda self, module_name, flag, timeout_seconds: (
            calls.__setitem__("cli", calls["cli"] + 1)
            or CommandResult(command=["odoo-bin", flag], cwd="odoosh:ssh", exit_code=0)
        ),
    )

    OdooShService().module_deploy_ssh("iot_device_management")

    assert calls == {"shell": 1, "cli": 0}


def test_fast_module_state_probe_uses_read_only_psql_without_odoo_startup(monkeypatch):
    captured = {}
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")

    def fake_run_shell(script, timeout_seconds):
        captured["script"] = script
        captured["timeout"] = timeout_seconds
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout="installed\n")

    monkeypatch.setattr(service, "run_shell", fake_run_shell)

    state = service._module_state_via_psql("sample_custom_module")

    assert state == "installed"
    assert "SELECT state FROM ir_module_module" in captured["script"]
    assert "odoo-bin" not in captured["script"]
    assert captured["timeout"] == 15


def test_single_shell_fallback_contains_state_and_both_orm_operations(monkeypatch):
    captured = {}
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src")
    monkeypatch.setattr(service, "_addons_path", lambda: "/home/odoo/src/addons")

    def fake_run_shell(script, timeout_seconds):
        captured["script"] = script
        marker = "MCP_MODULE_SHELL_DEPLOY_JSON:"
        payload = '{"status":"ok","module":"x_module","state_before":"uninstalled","operation":"install"}'
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout=marker + payload)

    monkeypatch.setattr(service, "run_shell", fake_run_shell)

    result = service._run_module_shell_deploy("x_module", 600)

    assert result.exit_code == 0
    assert "modules.update_list()" in captured["script"]
    assert "before == 'uninstallable'" in captured["script"]
    assert "ast.literal_eval" in captured["script"]
    assert "deployed_manifest" in captured["script"]
    assert "dependency_states" in captured["script"]
    assert "source_repair_allowed" in captured["script"]
    assert "button_immediate_install()" in captured["script"]
    assert "button_immediate_upgrade()" in captured["script"]
    assert "probe=odoo_shell mode=install" in result.stdout
