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
    assert running.status.value == "queued"

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
    monkeypatch.setattr(ClientGateService, "_git_remote_transport_error", lambda self, url: None)

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
    assert prepare_calls == [{"client_name": "Alpha", "timeout_seconds": settings.client_checkout_timeout_seconds}]
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

    # Persisted flags without config identity are deliberately not reusable.
    assert agent_instructions()["requires_client_selection"] is True
    _make_checkout(tmp_path / "a")
    _patch_successful_validation(monkeypatch)
    ClientGateService().check(client_name="Alpha", validate=True)
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
    assert "SSH" in checked["summary"]
    assert checked["data_query_ready"] is False
    assert checked["full_validation_passed"] is False
    assert "ordinary data queries remain unavailable" in checked["summary"]

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


def test_gitlab_validation_persists_phase_progress_and_separate_transport_results(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _set_clients(monkeypatch, [_client("Alpha", "https://gitlab.internal/acme/a.git", "dev", str(tmp_path / "repo"))])
    callbacks = []
    monkeypatch.setattr(tools, "submit_background", callbacks.append)
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "network_verified": True, "log": ["native Git ready"]})
    def https_probe(self, client):
        job = JsonStore().get_job(started["job_id"])
        assert job.result["phase"] == "odoo_shell"
        assert job.result["checks"]["gitlab"] == "ok"
        assert "github" not in job.result["checks"]
        assert job.steps[-1].name == "odoo_shell" and job.steps[-1].status == "running"
        return {"status": "ok", "log": ["HTTPS read succeeded"]}
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", https_probe)
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "failed", "log": ["SSH timed out"]})
    started = tools._start_client_validation(ClientCheckRequest(client_name="Alpha", validate=True))
    callbacks[0]()
    finished = JsonStore().get_job(started["job_id"])
    assert finished.result["coding_ready"] is True
    assert finished.result["data_query_ready"] is True
    assert finished.result["deploy_ready"] is False
    assert finished.result["full_validation_passed"] is False
    assert [step.name for step in finished.steps] == ["config", "checkout", "coding_workflows", "odoo_shell", "odoosh_ssh"]
    assert finished.steps[-1].status == "failed"


def test_failed_checkout_does_not_hide_live_transport_results(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _set_clients(monkeypatch, [_client("Alpha", "https://gitlab.internal/acme/a.git", "dev", str(tmp_path / "repo"))])
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "failed", "log": ["clone timed out"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["HTTPS ready"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "failed", "log": ["SSH unavailable"]})
    result = ClientGateService().check("Alpha", validate=True)
    assert result["coding_ready"] is False
    assert result["data_query_ready"] is True
    assert result["checks"]["odoo_shell"] == "ok"
    assert result["checks"]["odoosh_ssh"] == "failed"


def test_validation_readiness_expires_and_configuration_changes_invalidate(monkeypatch, tmp_path):
    checkout = tmp_path / 'alpha'; _make_checkout(checkout)
    monkeypatch.setattr(settings, 'state_dir', str(tmp_path / 'state'))
    client = _client('Alpha', 'https://github.com/acme/a.git', 'staging', str(checkout))
    _set_clients(monkeypatch, [client]); _patch_successful_validation(monkeypatch)
    gate = ClientGateService()
    gate.check('Alpha', validate=True)
    assert gate.check('Alpha')['ok']
    state = gate.store.load_session_state(client_name='Alpha')
    state['client_validation']['validated_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    gate.store.save_session_state(state, client_name='Alpha')
    assert not gate.check('Alpha')['ok'] and gate.check('Alpha')['requires_validation']
    gate.check('Alpha', validate=True, force_refresh=True)
    _set_clients(monkeypatch, [{**client, 'branch': 'new-staging'}])
    assert not gate.check('Alpha')['ok']


@pytest.fixture
def branch_change_checkout(tmp_path, monkeypatch):
    import subprocess
    from odoo_agent_server.services.client_resolver_service import ResolvedClient
    monkeypatch.setattr(settings, 'allowed_roots_raw', str(tmp_path))
    monkeypatch.setattr(settings, 'state_dir', str(tmp_path / 'state'))
    original = tmp_path / 'original'; original.mkdir()
    def git(*args, cwd=original):
        return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    git('init', '-b', 'old'); git('config','user.name','Test'); git('config','user.email','test@example.invalid')
    (original/'source.txt').write_text('old'); git('add','.'); git('commit','-m','old')
    old_sha = git('rev-parse','HEAD'); git('checkout','-b','new')
    (original/'source.txt').write_text('new'); git('commit','-am','new')
    remote = tmp_path/'origin.git'; git('clone','--bare',str(original),str(remote))
    checkout = tmp_path/'checkout'; git('clone','--single-branch','--branch','old',str(remote),str(checkout))
    client = ResolvedClient(name='Branch change', repo=str(remote), branch='new', path=str(checkout))
    return client, checkout, old_sha, lambda *args: git(*args,cwd=checkout)


def test_remote_validation_timeout_is_reported_as_unverified_transport(branch_change_checkout, monkeypatch):
    from odoo_agent_server.services.repo_service import RepoService

    client, _, _, _ = branch_change_checkout
    from dataclasses import replace

    client = replace(client, branch="old")
    calls = []

    def timed_out(self, command, cwd, timeout_seconds, env=None):
        calls.append((command, timeout_seconds))
        return SimpleNamespace(exit_code=124, stdout="", stderr="", timed_out=True)

    monkeypatch.setattr(RepoService, "_run", timed_out)
    result = ClientGateService()._check_checkout(client)

    assert result["status"] == "failed"
    assert result["error_type"] == "git_remote_timeout"
    assert "Authentication and branch availability were not verified" in result["log"][0]
    assert calls[0][1] == ClientGateService.GIT_REMOTE_CHECK_TIMEOUT_SECONDS
    assert f"http.connectTimeout={ClientGateService.GIT_REMOTE_CONNECT_TIMEOUT_SECONDS}" in calls[0][0]


def test_unreachable_git_host_fails_before_git_process(branch_change_checkout, monkeypatch):
    import socket
    from dataclasses import replace
    from odoo_agent_server.services.repo_service import RepoService

    client, _, _, _ = branch_change_checkout
    client = replace(client, branch="old", repo="https://gitlab.internal/acme/a.git")
    attempts = []

    def connect(address, timeout):
        attempts.append((address, timeout))
        raise TimeoutError("network unavailable")

    def git_process(*args, **kwargs):
        raise AssertionError("Git subprocess must not start when TCP preflight fails")

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(RepoService, "_run", git_process)
    result = ClientGateService()._check_checkout(client)

    assert result["status"] == "failed"
    assert result["error_type"] == "git_remote_timeout"
    assert "TCP connection timed out after 5s" in result["log"][0]
    assert "authentication and branch availability were not verified" in result["log"][0]
    assert attempts == [(('gitlab.internal', 443), 5)]


def test_validation_safely_syncs_changed_branch_in_single_branch_checkout(branch_change_checkout):
    client, checkout, old_sha, git = branch_change_checkout
    result = ClientGateService()._check_checkout(client)
    assert result['status']=='ok', result
    assert git('branch','--show-current')=='new'
    assert (checkout/'source.txt').read_text()=='new'
    assert git('rev-parse','old')==old_sha
    assert git('rev-parse','--abbrev-ref','--symbolic-full-name','@{upstream}')=='origin/new'


def test_branch_update_never_discards_dirty_work(branch_change_checkout):
    client, checkout, _, git = branch_change_checkout
    (checkout/'source.txt').write_text('uncommitted work')
    result=ClientGateService()._check_checkout(client)
    assert result['status']=='failed'
    assert (checkout/'source.txt').read_text()=='uncommitted work'
    assert git('branch','--show-current')=='old'


def test_branch_update_preserves_divergent_target_commits(branch_change_checkout):
    client, checkout, _, git = branch_change_checkout
    git('checkout','-b','new')
    (checkout/'private.txt').write_text('local-only')
    git('-c','user.name=Test','-c','user.email=test@example.invalid','add','.')
    git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','unpublished')
    local=git('rev-parse','new'); git('checkout','old')
    result=ClientGateService()._check_checkout(client)
    assert result['status']=='failed' and git('rev-parse','new')==local
    assert git('branch','--show-current')=='old'


def test_new_checkout_uses_configured_large_repository_budget(tmp_path, monkeypatch):
    from odoo_agent_server.services.client_resolver_service import ResolvedClient
    from odoo_agent_server.services.repo_service import RepoService
    client = ResolvedClient(name="Large repo", repo="https://gitlab.internal/acme/a.git", branch="staging", path=str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "client_checkout_timeout_seconds", 900)
    monkeypatch.setattr(ClientGateService, "_git_remote_transport_error", lambda self, url: None)
    captured = {}
    def prepare(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(project_path=client.path, branch=client.branch)
    monkeypatch.setattr(RepoService, "prepare_repo", prepare)
    assert ClientGateService()._check_checkout(client)["status"] == "ok"
    assert captured == {"client_name": client.name, "timeout_seconds": 900}


def test_missing_checkout_fails_fast_for_unreachable_git_host(tmp_path, monkeypatch):
    from odoo_agent_server.services.client_resolver_service import ResolvedClient
    from odoo_agent_server.services.repo_service import RepoService
    client = ResolvedClient(name="Unreachable", repo="https://gitlab.internal/acme/a.git", branch="staging", path=str(tmp_path / "missing"))
    monkeypatch.setattr(ClientGateService, "_git_remote_transport_error", lambda self, url: ("git_remote_timeout", "Git host unreachable; credentials unverified"))
    def forbidden(*args, **kwargs):
        raise AssertionError("Do not start a long clone when connection preflight fails")
    monkeypatch.setattr(RepoService, "prepare_repo", forbidden)
    result = ClientGateService()._check_checkout(client)
    assert result["status"] == "failed" and result["error_type"] == "git_remote_timeout"
