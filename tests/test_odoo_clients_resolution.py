from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import get_odoo_clients, settings
from odoo_agent_server.routers.workflow import coding_task_plan, start_session
from odoo_agent_server.schemas import CodingTaskPlanRequest, StartSessionRequest
from odoo_agent_server.services.client_resolver_service import ClientResolverService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.repo_service import RepoService


def _client(name: str, repo: str, branch: str, path: str, **extra) -> dict:
    return {
        "name": name,
        "repo": repo,
        "branch": branch,
        "path": path,
        "odoo_url": extra.pop("odoo_url", f"https://{name.lower()}.example.com"),
        "odoo_db": extra.pop("odoo_db", f"{name.lower()}_db"),
        "odoo_login": extra.pop("odoo_login", "user@example.com"),
        "odoo_password": extra.pop("odoo_password", "super-secret"),
        "token": extra.pop("token", "token-secret"),
        **extra,
    }


def _set_clients(monkeypatch, clients: list[dict]) -> None:
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))


def _init_origin(tmp_path: Path, branch: str = "dev") -> Path:
    source = tmp_path / "source"
    bare = tmp_path / "origin.git"
    source.mkdir()
    subprocess.run(["git", "init", "-b", branch], cwd=source, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=source, check=True)
    (source / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=source, check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True, text=True)
    return bare


def test_resolver_selects_explicit_name(monkeypatch, tmp_path):
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "beta", str(tmp_path / "b")),
    ]
    _set_clients(monkeypatch, clients)

    resolved = ClientResolverService().resolve_client(client_name="Beta")

    assert resolved is not None
    assert resolved.name == "Beta"
    assert resolved.branch == "beta"


def test_resolver_exposes_validated_environment(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client(
            "Test",
            "git@example.com:test.git",
            "test",
            str(tmp_path / "test"),
            environment="TEST",
        )
    ])

    resolved = ClientResolverService().resolve_client()

    assert resolved is not None
    assert resolved.environment == "test"
    assert resolved.public["environment"] == "test"


def test_resolver_rejects_global_odoo_url_and_db_when_multiple_clients(monkeypatch, tmp_path):
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a"), odoo_url="https://a.example.com", odoo_db="a"),
        _client("Beta", "git@example.com:b.git", "beta", str(tmp_path / "b"), odoo_url="https://b.example.com", odoo_db="b"),
    ]
    _set_clients(monkeypatch, clients)
    monkeypatch.setenv("ODOO_BASE_URL", "https://b.example.com")
    monkeypatch.setenv("ODOO_DB_NAME", "b")

    with pytest.raises(HTTPException) as exc_info:
        ClientResolverService().resolve_client()

    assert exc_info.value.status_code == 409
    assert "Choose one client_name" in str(exc_info.value.detail)


def test_resolver_matches_project_path(monkeypatch, tmp_path):
    target = tmp_path / "client-b"
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "beta", str(target)),
    ]
    _set_clients(monkeypatch, clients)

    resolved = ClientResolverService().resolve_client(project_path=str(target))

    assert resolved is not None
    assert resolved.name == "Beta"


def test_resolver_auto_selects_single_client(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [_client("Only", "git@example.com:o.git", "dev", str(tmp_path / "only"))])

    resolved = ClientResolverService().resolve_client()

    assert resolved is not None
    assert resolved.name == "Only"


def test_get_odoo_clients_accepts_shell_quoted_escaped_json(monkeypatch):
    monkeypatch.setenv(
        "ODOO_CLIENTS",
        "'[{\\\"name\\\":\\\"Client A\\\",\\\"repo\\\":\\\"git@example.com:test.git\\\",\\\"branch\\\":\\\"feature_branch\\\",\\\"path\\\":\\\"/workspace/repos/odoo_project\\\"}]'",
    )

    clients = get_odoo_clients()

    assert clients[0]["name"] == "Client A"
    assert clients[0]["branch"] == "feature_branch"
    assert clients[0]["path"] == "/workspace/repos/odoo_project"


def test_resolver_ambiguous_clients_lists_safe_names(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as exc:
        ClientResolverService().resolve_client()

    detail = str(exc.value.detail)
    assert "Alpha" in detail
    assert "Beta" in detail
    assert "token-secret" not in detail
    assert "super-secret" not in detail


def test_incomplete_single_client_requires_repo_branch_path(monkeypatch):
    _set_clients(monkeypatch, [{"name": "Broken", "repo": "git@example.com:b.git"}])

    with pytest.raises(HTTPException) as exc:
        ClientResolverService().resolve_client()

    detail = str(exc.value.detail)
    assert "repo/branch/path" in detail
    assert "odoo_password" not in detail


@pytest.mark.integration
def test_repo_prepare_uses_selected_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "client-checkout"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_repo", None)
    monkeypatch.setattr(settings, "target_git_branch", None)
    monkeypatch.setattr(settings, "target_project_path", None)
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    prepared = RepoService().prepare_repo(client_name="Client")

    assert prepared.project_path == str(checkout.resolve())
    assert prepared.branch == "dev"
    assert (checkout / ".git").exists()


@pytest.mark.integration
def test_repo_prepare_clones_into_existing_empty_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "empty-checkout"
    checkout.mkdir()
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    prepared = RepoService().prepare_repo()

    assert prepared.project_path == str(checkout.resolve())
    assert (checkout / "README.md").exists()


@pytest.mark.integration
def test_repo_prepare_rejects_non_git_non_empty_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "occupied"
    checkout.mkdir()
    (checkout / "local.txt").write_text("do not overwrite\n", encoding="utf-8")
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    with pytest.raises(HTTPException) as exc:
        RepoService().prepare_repo()

    assert "not a git repository and is not empty" in str(exc.value.detail)
    assert "repo config" not in str(exc.value.detail).lower()


@pytest.mark.integration
def test_git_validate_target_uses_client_branch_and_repo(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "client-checkout"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_repo", None)
    monkeypatch.setattr(settings, "target_git_branch", None)
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])
    RepoService().prepare_repo()

    ok = GitService().validate_target(str(checkout), branch="dev")
    bad = GitService().validate_target(str(checkout), branch="main")

    assert ok.exit_code == 0
    assert bad.exit_code == 2
    assert "expected dev" in bad.stderr


def test_coding_task_plan_defaults_from_single_client(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [_client("Client", "git@example.com:c.git", "dev", str(tmp_path / "checkout"), token="")])

    result = coding_task_plan(CodingTaskPlanRequest(task="Add a field"))

    assert result.defaults["client_name"] == "Client"
    assert result.defaults["repo_url"] == "git@example.com:c.git"
    assert result.defaults["branch"] == "dev"
    assert result.defaults["project_path"] == str(tmp_path / "checkout")


def test_coding_task_plan_uses_fast_path_for_small_field_change(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [_client("Client", "git@example.com:c.git", "dev", str(tmp_path / "checkout"), token="")])

    result = coding_task_plan(CodingTaskPlanRequest(task="add one field test_ai in sale order notebook"))

    assert result.defaults["small_patch_fast_path"] is True
    assert "repo.list_custom_modules" not in result.required_tools
    assert "odoo.search_read" in result.required_tools
    assert "workflow.safe_deploy_module" in result.required_tools
    assert "workflow.resume_job" in result.required_tools
    assert "repo.write_many_smart" in result.required_tools
    assert "repo.write_many_atomic" not in result.required_tools
    assert "git.commit" not in result.required_tools
    assert "git.push" not in result.required_tools
    assert "odoo.module_deploy" not in result.required_tools
    assert "small-patch path" in result.ordered_steps[0]
    plan_text = "\n".join(result.ordered_steps + result.guardrails)
    assert "ir.model.fields" in plan_text
    assert "ir.ui.view" in plan_text
    assert "Pydantic" in plan_text
    assert "printing MCP JSON blocks" in plan_text


@pytest.mark.integration
def test_start_session_saves_safe_client_metadata(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "session-checkout"
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout))])

    report = start_session(StartSessionRequest(include_odoo=False, include_odoosh=False))

    assert report.status in {"passed", "warning"}
    session_file = state / "session_state__Client.json"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    text = json.dumps(data)
    assert data["client_name"] == "Client"
    assert data["project_path"] == str(checkout.resolve())
    assert "token-secret" not in text
    assert "super-secret" not in text
    assert "oauth2:" not in text


@pytest.mark.integration
def test_start_session_reuses_existing_checkout_without_fetch(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "existing-checkout"
    subprocess.run(["git", "clone", "--branch", "dev", str(origin), str(checkout)], check=True, capture_output=True, text=True)
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout))])

    def unexpected_prepare(*args, **kwargs):
        raise AssertionError("existing checkout must not fetch/pull during fast session start")

    monkeypatch.setattr(RepoService, "prepare_repo", unexpected_prepare)
    report = start_session(StartSessionRequest())

    assert report.status in {"passed", "warning"}
    assert report.summary["project_path"] == str(checkout.resolve())


def test_resolver_accepts_per_client_odoosh_settings(monkeypatch, tmp_path):
    for key in (
        "ODOOSH_DB_NAME",
        "ODOOSH_SSH_HOST",
        "ODOOSH_SSH_PORT",
        "ODOOSH_SSH_USER",
        "ODOOSH_SSH_KEY_PATH",
        "ODOOSH_SSH_PRIVATE_KEY_B64",
        "ODOOSH_ODOO_PATH",
        "ODOOSH_USER_ADDONS_PATH",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(settings, "odoosh_db_name", None)
    monkeypatch.setattr(settings, "odoosh_ssh_host", None)
    monkeypatch.setattr(settings, "odoosh_ssh_user", "odoo")
    monkeypatch.setattr(settings, "odoosh_ssh_port", 22)
    monkeypatch.setattr(settings, "odoosh_odoo_path", "/home/odoo/src/odoo")
    monkeypatch.setattr(settings, "odoosh_user_addons_path", "/home/odoo/src/user")
    client_data = _client(
        "Client",
        "git@example.com:c.git",
        "dev",
        str(tmp_path / "checkout"),
        token="",
        odoosh_db_name="client-db",
        odoosh_ssh_host="client.dev.odoo.com",
        odoosh_ssh_port=2222,
        odoosh_ssh_user="123456",
        odoosh_odoo_path="/custom/odoo",
        odoosh_user_addons_path="/custom/user",
        odoosh_ssh_private_key_b64="a2V5",
    )
    _set_clients(monkeypatch, [client_data])

    resolved = ClientResolverService().resolve_client(client_name="Client")
    public = resolved.public

    assert resolved is not None
    assert resolved.odoosh_db_name == "client-db"
    assert resolved.odoosh_ssh_host == "client.dev.odoo.com"
    assert resolved.odoosh_ssh_port == 2222
    assert resolved.odoosh_ssh_user == "123456"
    assert resolved.odoosh_odoo_path == "/custom/odoo"
    assert resolved.odoosh_user_addons_path == "/custom/user"
    assert public["odoosh_ssh_host"] == "client.dev.odoo.com"
    assert "a2V5" not in json.dumps(public)

    ClientResolverService().apply_odoo_env(resolved)

    # Multi-client isolation: selected client data stays on ResolvedClient and
    # must not be copied into process-wide settings where another client/job can
    # accidentally reuse it.
    assert settings.odoosh_db_name is None
    assert settings.odoosh_ssh_host is None


def test_odoosh_service_prefers_selected_client_settings(monkeypatch, tmp_path):
    from odoo_agent_server.services.odoosh_service import OdooShService

    monkeypatch.delenv("ODOOSH_SSH_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("ODOOSH_SSH_KEY_PATH", raising=False)
    monkeypatch.setattr(settings, "odoosh_ssh_private_key_b64", None)
    monkeypatch.setattr(settings, "odoosh_ssh_key_path", None)
    monkeypatch.setattr(settings, "odoosh_db_name", "global-db")
    monkeypatch.setattr(settings, "odoosh_ssh_host", "global.dev.odoo.com")
    monkeypatch.setattr(settings, "odoosh_ssh_user", "global-user")
    monkeypatch.setattr(settings, "odoosh_ssh_port", 22)
    monkeypatch.setattr(settings, "odoosh_odoo_path", "/global/odoo")
    monkeypatch.setattr(settings, "odoosh_user_addons_path", "/global/user")
    client = ClientResolverService()._to_resolved(
        _client(
            "Client",
            "git@example.com:c.git",
            "dev",
            str(tmp_path / "checkout"),
            token="",
            odoosh_db_name="client-db",
            odoosh_ssh_host="client.dev.odoo.com",
            odoosh_ssh_port="2222",
            odoosh_ssh_user="client-user",
            odoosh_odoo_path="/client/odoo",
            odoosh_user_addons_path="/client/user",
        ),
        require_complete=True,
    )

    service = OdooShService(client)
    target = service._target()

    assert target.host == "client.dev.odoo.com"
    assert target.user == "client-user"
    assert target.port == 2222
    assert service._db_name() == "client-db"
    assert service._odoo_path() == "/client/odoo"
    assert service._user_addons_path() == "/client/user"


def test_start_session_requires_explicit_client_when_multiple_configured(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as exc:
        start_session(StartSessionRequest(include_odoo=False, include_odoosh=False))

    detail = str(exc.value.detail)
    assert "Multiple ODOO_CLIENTS" in detail
    assert "Alpha" in detail
    assert "Beta" in detail
    assert "client_name" in detail


def test_repo_tools_require_explicit_client_when_multiple_configured(monkeypatch, tmp_path):
    from odoo_agent_server.routers.repo import repo_read
    from odoo_agent_server.schemas import RepoReadRequest

    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as exc:
        repo_read(RepoReadRequest(project_path=str(tmp_path / "a"), relative_path="README.md"))

    detail = str(exc.value.detail)
    assert "Multiple ODOO_CLIENTS" in detail
    assert "repo.read" in detail
    assert "Alpha" in detail
    assert "Beta" in detail


def test_git_tools_require_explicit_client_when_multiple_configured(monkeypatch, tmp_path):
    from odoo_agent_server.routers.git import git_status
    from odoo_agent_server.schemas import BaseProjectRequest

    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as exc:
        git_status(BaseProjectRequest(project_path=str(tmp_path / "a")))

    detail = str(exc.value.detail)
    assert "Multiple ODOO_CLIENTS" in detail
    assert "git.status" in detail
    assert "Alpha" in detail
    assert "Beta" in detail


def test_resolver_parses_per_client_odoo_verify_ssl(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a"), odoo_verify_ssl="false"),
    ])

    resolved = ClientResolverService().resolve_client(client_name="Alpha")

    assert resolved is not None
    assert resolved.odoo_verify_ssl is False
