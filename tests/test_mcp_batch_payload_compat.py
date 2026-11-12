from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _headers(monkeypatch):
    # Auth intentionally follows the original settings-backed token behaviour.
    # Do not rely on mutating ODOO_AGENT_API_TOKEN after app/settings import.
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_chatgpt_mcp_accepts_json_shell_batch(monkeypatch):
    client = TestClient(app)
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    response = client.post("/chatgpt/mcp", json=payload, headers=_headers(monkeypatch))

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["id"] == 1
    assert body[1]["id"] == 2
    assert "tools" in body[1]["result"]


def test_standard_mcp_accepts_json_shell_batch(monkeypatch):
    client = TestClient(app)
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    response = client.post("/mcp", json=payload, headers=_headers(monkeypatch))

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["id"] == 1
    assert body[1]["id"] == 2
