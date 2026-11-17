import json
import re

from fastapi.testclient import TestClient

from odoo_agent_server.main import app
from odoo_agent_server.config import settings


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_health_and_root_are_backend_only():
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["service"] == "odoo-mcp-agent-connector"
    assert health.json()["runtime"]["service_version"]
    assert re.fullmatch(r"[a-f0-9]{40}|unavailable", health.json()["runtime"]["commit_sha"])
    assert re.fullmatch(r"[a-f0-9]{64}|unavailable", health.json()["runtime"]["prompt_sha256"])
    assert "workflow_progress" in health.json()
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "Odoo MCP Agent Connector"
    assert root.json()["ui"] == "not_required"
    assert root.json()["openapi"] == "/openapi.json"
    assert root.json()["mcp"] == "/mcp"
    assert root.json()["compatibility_mcp"]["chatgpt"] == "/chatgpt/mcp"


def test_tools_are_exposed_for_direct_chatgpt():
    client = TestClient(app)
    response = client.get("/tools/list", headers=_auth())
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert "qa_self_run" in names
    assert "repo_apply_patch_atomic" in names
    assert "git_push" in names
    assert "odoosh_grep_errors" in names
    assert "odoosh_read_user_file" in names
    assert "workflow_verify_deployed_module" in names
    assert "browser_screenshot" in names
    assert "skills_list" in names
    assert "skills_read" in names


def test_capability_check_loads():
    client = TestClient(app)
    response = client.get("/workflow/capability-check", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "partial", "failed"}
    assert "git" in payload["checks"]
    assert "allowed_roots" in payload["checks"]


def test_agent_instructions_require_complete_safe_deploy_flow():
    client = TestClient(app)
    response = client.get("/agent-instructions")
    assert response.status_code == 200
    instructions = response.json()["instructions"]
    rules = "\n".join(instructions["rules"])
    provider_flow = instructions["provider_flow"]
    skills = instructions["skills"]

    assert len(instructions["rules"]) <= 2
    assert "workflow_odoo_code_change" in rules
    assert "hidden repo, Git" in rules
    assert "Pydantic code inside Odoo addons" in rules
    assert provider_flow["code_change"] == ("execution_workspace_open" if settings.execution_enabled else "workflow_odoo_code_change")
    assert provider_flow["composite_fallback"] == "workflow_odoo_code_change"
    assert provider_flow["visual_change"] == "workflow_frontend_visual_implement"
    assert provider_flow["continuation"] == [
        "jobs_get", "workflow_resume_job", "workflow_resume_continuation",
    ]
    assert "deploy_confirmed=true" in provider_flow["terminal_success"]
    assert provider_flow["terminal_action"] == "stop_and_summarize"
    assert skills["mode"] == "on_demand"
    assert skills["list_tool"] == "skills_list"
    assert skills["read_tool"] == "skills_read"
    # Re-measured after adding the WORKSPACE LANE prompt section (14.1 KB).
    assert len(json.dumps(response.json(), ensure_ascii=False)) < 15_000
    assert "available" not in skills
    assert response.json()["runtime"]["prompt_sha256"]

    forbidden_sections = {
        "small_patch_fast_path",
        "mandatory_dual_read",
        "core_reading_rules",
        "strict_frontend_owl_js_xml_preflight",
        "adaptive_scope_levels",
        "pre_push_verify",
        "large_file_writing",
        "flow",
        "error_guide",
        "new_tools_v14plus",
    }
    assert not (forbidden_sections & instructions.keys())


def test_readiness_requires_fresh_worker_and_runtime_proof(tmp_path, monkeypatch):
    import json, time
    from fastapi.testclient import TestClient
    from odoo_agent_server.config import settings
    from odoo_agent_server.main import app
    monkeypatch.setattr(settings, 'execution_enabled', True)
    monkeypatch.setattr(settings, 'execution_root', str(tmp_path))
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    assert client.get('/ready').status_code == 503
    (tmp_path / 'worker-heartbeat').write_text(json.dumps(time.time()))
    (tmp_path / 'capabilities.json').write_text(json.dumps({'image': settings.execution_image, 'image_id': 'sha256:proof', 'python': True}))
    assert client.get('/ready').status_code == 200
    (tmp_path / 'worker-heartbeat').write_text(json.dumps(time.time() - 60))
    assert client.get('/ready').status_code == 503


def test_agent_lane_metadata_tracks_execution_configuration(monkeypatch):
    from odoo_agent_server.main import agent_instructions
    for enabled, tool in [(False, 'workflow_odoo_code_change'), (True, 'execution_workspace_open')]:
        monkeypatch.setattr(settings, 'execution_enabled', enabled)
        assert agent_instructions()['instructions']['provider_flow']['code_change'] == tool
