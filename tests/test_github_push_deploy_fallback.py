from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.routers import workflow
from odoo_agent_server.schemas import SafeDeployModuleRequest
from odoo_agent_server.services.client_gate_service import ClientGateService
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.services.workflow_guard_service import _RESUMABLE_SAFE_DEPLOY_RESULTS


def _client(path: Path) -> dict:
    return {
        "name": "Alpha",
        "repo": "https://github.com/acme/alpha.git",
        "branch": "dev",
        "path": str(path),
        "token": "secret",
        "odoo_url": "https://alpha.dev.odoo.com",
        "odoo_db": "alpha",
        "odoo_login": "user@example.test",
        "odoo_password": "secret",
        "odoosh_db_name": "alpha",
        "odoosh_ssh_host": "alpha.dev.odoo.com",
        "odoosh_ssh_port": 22,
        "odoosh_ssh_user": "123",
        "odoosh_ssh_private_key_b64": "a2V5",
        "odoosh_odoo_path": "/home/odoo/src/odoo",
        "odoosh_user_addons_path": "/home/odoo/src/user",
    }


def test_client_validation_marks_publickey_failure_as_github_fallback_eligible(monkeypatch, tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps([_client(checkout)]))
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["jsonrpc ok"]})
    monkeypatch.setattr(
        ClientGateService,
        "_check_odoosh",
        lambda self, client: {
            "status": "failed",
            "addons_path_status": "skipped",
            "log": ["Permission denied (publickey)."],
        },
    )

    result = ClientGateService().check(client_name="Alpha", validate=True)

    assert result["deploy_ready"] is False
    assert result["github_push_ready"] is True
    assert result["deploy_fallback_eligible"] is True
    assert result["deploy_fallback_reason"] == "odoosh_ssh_unavailable"


def test_safe_deploy_starts_with_coding_lease_when_deploy_gate_is_publickey_blocked(monkeypatch, tmp_path):
    project = tmp_path / "repo"
    (project / "custom_module").mkdir(parents=True)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "github_push_fallback_enabled", True)

    calls = []

    def issue(self, client_name, *, action, project_path):
        calls.append(action)
        if action == "workflow.safe_deploy_module":
            raise HTTPException(
                status_code=409,
                detail={
                    "validation": {
                        "github_push_ready": True,
                        "deploy_fallback_eligible": True,
                        "deploy_fallback_reason": "odoosh_ssh_unavailable",
                    }
                },
            )
        return {
            "version": 1,
            "client_name": client_name,
            "project_path": str(project),
            "repo_fingerprint": "x",
            "branch": "dev",
            "capability": "coding_ready",
            "validated_at": "2026-01-01T00:00:00+00:00",
            "issued_at": "2026-01-01T00:00:00+00:00",
            "fingerprint": "temporary",
        }

    monkeypatch.setattr(ClientGateService, "issue_job_validation_lease", issue)
    monkeypatch.setattr(ClientGateService, "_job_lease_fingerprint", lambda self, identity: "signed")
    monkeypatch.setattr(workflow, "submit_background", lambda callback: None)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        client_name="Alpha",
        project_path=str(project),
        module_name="custom_module",
        commit_message="Push verified source",
        wait_for_result=False,
    )))

    assert calls == ["workflow.safe_deploy_module", "workflow.safe_deploy_module.github_fallback"]
    assert result.status == "running"
    job = JsonStore().get_job(result.job_id)
    lease = job.request["validation_lease"]
    assert lease["github_push_fallback"] is True
    assert lease["deploy_fallback_reason"] == "odoosh_ssh_unavailable"


def test_fallback_result_is_resumable_and_never_means_deployed():
    assert "github_pushed_deploy_pending" in _RESUMABLE_SAFE_DEPLOY_RESULTS
    request = SafeDeployModuleRequest(
        project_path="/tmp/repo",
        module_name="custom_module",
        commit_message="Push verified source",
    )
    assert request.github_push_fallback is True
