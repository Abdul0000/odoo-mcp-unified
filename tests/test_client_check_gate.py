from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.main import agent_instructions
from odoo_agent_server.routers import tools
from odoo_agent_server.routers.repo import repo_read
from odoo_agent_server.schemas import ClientCheckRequest, RepoReadRequest
from odoo_agent_server.services.client_gate_service import ClientGateService
from odoo_agent_server.services.store_service import JsonStore


def _client(name: str, repo: str, branch: str, path: str, **extra) -> dict:
    return {
        "name": name,
        "repo": repo,
        "branch": branch,
        "path": path,
        "token": extra.pop("token", "token-secret"),
        "odoo_url": extra.pop("odoo_url", f"https://{name.lower()}.dev.odoo.com"),
        "odoo_db": extra.pop("odoo_db", f"{name.lower()}_db"),
        "odoo_login": extra.pop("odoo_login", "user@acme.test"),
        "odoo_password": extra.pop("odoo_password", "secret-password"),
        "odoosh_db_name": extra.pop("odoosh_db_name", f"{name.lower()}_db"),
        "odoosh_ssh_host": extra.pop("odoosh_ssh_host", f"{name.lower()}.dev.odoo.com"),
        "odoosh_ssh_port": extra.pop("odoosh_ssh_port", 22),
        "odoosh_ssh_user": extra.pop("odoosh_ssh_user", "123456"),
        "odoosh_ssh_private_key_b64": extra.pop("odoosh_ssh_private_key_b64", "a2V5"),
        "odoosh_odoo_path": extra.pop("odoosh_odoo_path", "/home/odoo/src/odoo"),
        "odoosh_user_addons_path": extra.pop("odoosh_user_addons_path", "/home/odoo/src/user"),
        **extra,
    }


def _set_clients(monkeypatch, clients: list[dict]) -> None:
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))


def _make_checkout(path: Path) -> None:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    (path / "README.md").write_text("ok\n", encoding="utf-8")


def _patch_successful_validation(monkeypatch) -> None:
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["odoo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": ["ssh ok"]})


def test_client_check_lists_all_configured_clients_even_placeholders(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(tmp_path / "a")),
        _client("Client 2", "https://github.com/Abdul0000/client_2.git", "18.0", str(tmp_path / "b"), token="CHANGE_ME_CLIENT_2_GITHUB_TOKEN"),
    ])

    result = ClientGateService().check()

    assert result["requires_client_selection"] is True
    assert result["available_client_names"] == ["Alpha", "Client 2"]
    assert "CHANGE_ME" not in json.dumps(result["clients"])


def test_client_check_validation_saves_active_client_and_allows_guarded_repo_tool(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as missing_client:
        repo_read(RepoReadRequest(project_path=str(checkout), relative_path="README.md"))
    assert "requires_client_selection" in str(missing_client.value.detail)

    with pytest.raises(HTTPException) as not_validated:
        repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert "requires_validation" in str(not_validated.value.detail)

    _patch_successful_validation(monkeypatch)
    checked = ClientGateService().check(client_name="Alpha", validate=True)
    assert checked["ok"] is True
    assert checked["validated"] is True
    assert checked["next_call"]["tool"] == "workflow.agent_instructions"

    content = repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert content.content == "ok\n"


def test_mcp_client_validation_returns_background_job_before_remote_checks(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
    ])
    _patch_successful_validation(monkeypatch)
    callbacks = []
    monkeypatch.setattr(tools, "submit_background", callbacks.append)

    started = tools._start_client_validation(
        ClientCheckRequest(client_name="Alpha", validate=True),
    )

    assert started["status"] == "validation_started"
    assert started["completion_required"] is True
    assert started["validated"] is False
    assert started["next_call"]["tool"] == "jobs.get"
    assert len(callbacks) == 1
    running = JsonStore().get_job(started["job_id"])
    assert running.status.value == "running"

    callbacks[0]()

    terminal = JsonStore().get_job(started["job_id"])
    assert terminal.status.value == "passed"
    assert terminal.result["validated"] is True
    assert terminal.result["next_call"]["tool"] == "workflow.agent_instructions"


def test_client_validation_prepares_missing_configured_checkout(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "not_cloned"
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
    ])
    prepare_calls = []

    def prepare_repo(self, **kwargs):
        prepare_calls.append(kwargs)
        _make_checkout(checkout)
        return SimpleNamespace(project_path=str(checkout), branch="dev")

    monkeypatch.setattr(
        "odoo_agent_server.services.client_gate_service.RepoService.prepare_repo",
        prepare_repo,
    )
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["odoo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": ["ssh ok"]})

    checked = ClientGateService().check(client_name="Alpha", validate=True)

    assert checked["validated"] is True
    assert checked["checks"]["checkout"] == "ok"
    assert prepare_calls == [{"client_name": "Alpha", "timeout_seconds": 180}]
    assert checkout.is_dir()
    ClientGateService().require_validated_client("Alpha", "workflow.odoo_code_change")


def test_agent_instructions_returns_client_gate_until_validation_passes(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])

    before = agent_instructions()
    assert before["prompt_source"] == "CLIENT_CHECK_SYSTEM_PROMPT.md"
    assert before["requires_client_selection"] is True

    JsonStore().save_session_state(
        {
            "client_name": "Alpha",
            "active_client_name": "Alpha",
            "client_validated": True,
            "client_validation": {"validated": True, "client_name": "Alpha", "summary": "ok"},
        },
        client_name="Alpha",
    )

    after = agent_instructions()
    assert after["prompt_source"] == "MCP_SYSTEM_PROMPT.md"
    assert "MCP_SYSTEM_PROMPT" in after["system_prompt"] or after["system_prompt"]


def test_client_check_shell_failure_still_allows_coding_when_ssh_ok(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "failed", "error_type": "odoo_login_error", "log": ["shell failed"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": ["ssh ok"]})

    checked = ClientGateService().check(client_name="Alpha", validate=True)

    assert checked["ok"] is True
    assert checked["coding_ready"] is True
    assert checked["shell_ready"] is False
    assert checked["ssh_ready"] is True
    assert checked["deploy_ready"] is True
    assert "SSH fallback" in checked["summary"]

    content = repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert content.content == "ok\n"


def test_client_check_shell_ok_without_ssh_does_not_enable_safe_deploy(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["https jsonrpc ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "failed", "addons_path_status": "skipped", "log": ["ssh timeout"]})

    checked = ClientGateService().check(client_name="Alpha", validate=True)

    assert checked["ok"] is True
    assert checked["coding_ready"] is True
    assert checked["data_query_ready"] is True
    assert checked["shell_ready"] is True
    assert checked["ssh_ready"] is False
    assert checked["deploy_ready"] is False
    assert "Odoo.sh SSH is required" in checked["deploy_blocker"]

    with pytest.raises(HTTPException) as blocked:
        ClientGateService().require_validated_client("Alpha", "workflow.safe_deploy_module")
    assert blocked.value.detail["required_capability"] == "deploy_ready"


def test_server_owned_job_lease_survives_interactive_validation_expiry(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
    ])
    _patch_successful_validation(monkeypatch)

    gate = ClientGateService()
    gate.check(client_name="Alpha", validate=True)
    lease = gate.issue_job_validation_lease(
        "Alpha",
        action="workflow.safe_deploy_module",
        project_path=str(checkout),
    )
    stale = gate.validation_state("Alpha")
    stale["validated_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    JsonStore().save_session_state({"client_validation": stale}, client_name="Alpha")

    with pytest.raises(HTTPException):
        gate.require_validated_client("Alpha", "workflow.resume_job")
    gate.require_validated_client_or_job_lease(
        "Alpha",
        "workflow.resume_job",
        project_path=str(checkout),
        lease=lease,
    )
