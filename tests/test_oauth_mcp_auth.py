from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _api_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_oauth_discovery_metadata_is_public():
    client = TestClient(app, base_url="https://agent.example.com")
    authz = client.get("/.well-known/oauth-authorization-server")
    assert authz.status_code == 200
    body = authz.json()
    assert body["authorization_endpoint"] == "https://agent.example.com/oauth/authorize"
    assert body["token_endpoint"] == "https://agent.example.com/oauth/token"
    assert body["registration_endpoint"] == "https://agent.example.com/oauth/register"
    assert "S256" in body["code_challenge_methods_supported"]

    resource = client.get("/.well-known/oauth-protected-resource")
    assert resource.status_code == 200
    assert resource.json()["resource"] == "https://agent.example.com/mcp"
    assert resource.json()["authorization_servers"] == ["https://agent.example.com"]


def test_unauthorized_mcp_advertises_oauth_resource_metadata():
    client = TestClient(app, base_url="https://agent.example.com")
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 401
    header = response.headers["www-authenticate"]
    assert "resource_metadata=" in header
    assert "https://agent.example.com/.well-known/oauth-protected-resource" in header


def test_dynamic_client_registration_pkce_authorization_code_and_mcp_access(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "oauth_auto_approve", True)
    client = TestClient(app, base_url="https://agent.example.com")

    register = client.post(
        "/oauth/register",
        json={
            "client_name": "ChatGPT test client",
            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert register.status_code == 201
    client_id = register.json()["client_id"]

    verifier = "test-verifier-abcdefghijklmnopqrstuvwxyz1234567890"
    authorize = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "state": "state-123",
            "scope": "odoo.agent",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    parsed = urlparse(authorize.headers["location"])
    query = parse_qs(parsed.query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    access_token = token.json()["access_token"]
    refresh_token = token.json()["refresh_token"]

    mcp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "connector.health", "arguments": {}}},
    )
    assert mcp.status_code == 200
    assert mcp.json()["result"]["structuredContent"]["status"] == "ok"

    refreshed = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != access_token


def test_manual_consent_requires_connector_token(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "oauth_auto_approve", False)
    client = TestClient(app, base_url="https://agent.example.com")
    verifier = "manual-verifier-abcdefghijklmnopqrstuvwxyz1234567890"
    params = {
        "response_type": "code",
        "client_id": settings.oauth_static_client_id,
        "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
        "state": "manual-state",
        "scope": "odoo.agent",
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200
    assert "Connector admin token" in page.text

    denied = client.post("/oauth/authorize", data={**params, "connector_token": "wrong"})
    assert denied.status_code == 403

    approved = client.post("/oauth/authorize", data={**params, "connector_token": settings.api_token}, follow_redirects=False)
    assert approved.status_code == 302
    assert "code=" in approved.headers["location"]


def test_legacy_bearer_api_token_still_works_after_oauth_upgrade():
    client = TestClient(app)
    response = client.get("/tools/list", headers=_api_auth())
    assert response.status_code == 200
    assert any(tool["name"] == "connector_health" for tool in response.json())
