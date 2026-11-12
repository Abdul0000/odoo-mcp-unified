from odoo_agent_server.schemas import CommandResult
from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoosh_service import OdooShService


def _client(environment="test"):
    return ResolvedClient(
        name="Test",
        repo="x",
        branch="test",
        path="/repo",
        environment=environment,
        odoosh_db_name="test-db",
        odoosh_odoo_path="/home/odoo/src/odoo",
    )


def test_native_test_runner_uses_fixed_odoo_test_command(monkeypatch):
    service = OdooShService(_client())
    captured = {}

    def run_shell(script, timeout_seconds):
        captured["script"] = script
        captured["timeout"] = timeout_seconds
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0)

    monkeypatch.setattr(service, "run_shell", run_shell)
    result = service.run_module_tests("demo_module", ["+post_install", "/demo_module"], 120)

    assert result.exit_code == 0
    assert result.command == ["odoosh.module_tests", "demo_module", "+post_install,/demo_module"]
    assert "--test-tags +post_install,/demo_module" in captured["script"]
    assert "--stop-after-init" in captured["script"]
    assert captured["timeout"] == 120


def test_native_test_runner_blocks_non_test_client():
    result = OdooShService(_client("staging")).run_module_tests("demo_module")

    assert result.exit_code == 2
    assert "environment=test" in result.stderr
