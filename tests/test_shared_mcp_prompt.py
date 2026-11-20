from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_prompt_files_keep_original_mcp_and_separate_client_gate():
    root = Path(__file__).resolve().parents[1]
    assert (root / "MCP_SYSTEM_PROMPT.md").is_file()
    assert (root / "CLIENT_CHECK_SYSTEM_PROMPT.md").is_file()
    assert not (root / "CHATGPT_SYSTEM_PROMPT.md").exists()


def test_all_mcp_profiles_expose_same_system_prompt():
    root = Path(__file__).resolve().parents[1]
    expected = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client = TestClient(app)

    for path in ("/mcp/system-prompt", "/chatgpt/system-prompt", "/grok/system-prompt", "/claude/system-prompt"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        payload = response.json()
        assert payload["source"] == "MCP_SYSTEM_PROMPT.md"
        assert payload["text"] == expected


def test_initialize_uses_client_gate_prompt_then_mcp_prompt_is_fetchable_for_every_profile():
    root = Path(__file__).resolve().parents[1]
    expected_mcp = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    expected_gate = (root / "CLIENT_CHECK_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client = TestClient(app)

    for path in ("/mcp", "/chatgpt/mcp", "/grok/mcp", "/claude/mcp"):
        init = client.post(path, headers=_auth(), json=_mcp("initialize"))
        assert init.status_code == 200
        result = init.json()["result"]
        assert result["prompt_source"] == "CLIENT_CHECK_SYSTEM_PROMPT.md"
        assert result["next_prompt_source_after_client_validation"] == "MCP_SYSTEM_PROMPT.md"
        assert result["instructions"] == expected_gate

        listed = client.post(path, headers=_auth(), json=_mcp("prompts/list", request_id=2))
        assert listed.status_code == 200
        names = [item["name"] for item in listed.json()["result"]["prompts"]]
        assert names == ["client_check_system_prompt", "mcp_system_prompt"]

        fetched_gate = client.post(
            path,
            headers=_auth(),
            json=_mcp("prompts/get", {"name": "client_check_system_prompt"}, request_id=3),
        )
        assert fetched_gate.status_code == 200
        assert fetched_gate.json()["result"]["messages"][0]["content"]["text"] == expected_gate

        fetched_mcp = client.post(
            path,
            headers=_auth(),
            json=_mcp("prompts/get", {"name": "mcp_system_prompt"}, request_id=4),
        )
        assert fetched_mcp.status_code == 200
        assert fetched_mcp.json()["result"]["messages"][0]["content"]["text"] == expected_mcp


def test_provider_prompts_use_callable_remote_tool_names():
    root = Path(__file__).resolve().parents[1]
    main_prompt = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client_prompt = (root / "CLIENT_CHECK_SYSTEM_PROMPT.md").read_text(encoding="utf-8")

    for marker in (
        "skills_list", "skills_read", "workflow_odoo_code_change",
        "workflow_odoo_debug_and_fix", "workflow_module_analyze",
        "workflow_module_test", "workflow_module_install_and_check",
        "workflow_frontend_visual_implement", "workflow_resume_job",
        "workflow_resume_continuation",
        "odoo_query", "odoo_search_read", "odoo_read_group", "jobs_get",
    ):
        assert marker in main_prompt
    assert "client_check" in client_prompt
    assert "jobs_get" in client_prompt
    assert "Do not ask “continue?” after an authorized task" in client_prompt

    for unsafe in (
        "skills.list", "skills.read", "workflow.odoo_code_change",
        "workflow.odoo_debug_and_fix", "workflow.module_analyze",
        "workflow.module_test", "workflow.module_install_and_check",
        "workflow.frontend_visual_implement", "workflow.resume_job",
        "odoo.query", "odoo.search_read", "odoo.read_group", "jobs.get",
    ):
        assert f"`{unsafe}" not in main_prompt
    assert "`client.check" not in client_prompt
    assert "`jobs.get" not in client_prompt


def test_mcp_system_prompt_keeps_required_generic_contract():
    root = Path(__file__).resolve().parents[1]
    text = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    assert len(text) < 11_000

    required_markers = [
        "## COMPOSITE WORKFLOW",
        "## ON-DEMAND SKILLS",
        "skills_list",
        "skills_read",
        "missing_context",
        "context_degradation",
        "trusted evidence",
        "workflow_odoo_code_change",
        "workflow_module_analyze",
        "workflow_module_install_and_check",
        "workflow_frontend_visual_implement",
        "odoo_query",
        "odoo_search_read",
        "odoo_read_group",
        "no development skill/source inspection",
        "workflow_resume_job",
        "workflow_resume_continuation",
        "workflow_busy",
        "jobs_get",
        "deploy_confirmed=true",
        "## CODE QUALITY CONTRACT",
        "minimal, secure, production-ready code for the requested scope",
        "Prevent N+1s",
        "parameterized SQL only when justified",
        "Keep deterministic fixes surgical",
        "workflow_profile",
        "needs_clarification",
        "dependency-ordered batches for large",
        "Odoo addon tests require Odoo",
        "pinned validated client identity",
        "single-owner recommendation",
        "## EXECUTION KERNEL",
        "decisive, evidence-bound and scope-locked",
        "minimum sufficient evidence",
        "inspect further only when evidence changes",
        "one focused inspection, one complete write set",
        "one deploy cycle",
        "Never repeat completed phases",
        "stop at terminal success",
        "## TASK ROUTER",
        "workflow_agent_instructions",
        "`task_open` needs a target",
        "never default push/deploy to coding",
        "provider_flow.code_change",
        "Never mix lanes or choose from skill examples",
        "re-plan during writes, retry, polling, repair or resume",
        "## REAL CODER MODE",
        "You are the engineer, not a narrator",
        "Diagnose, repair and continue after recoverable failures",
        "publishes that exact commit",
        "Run every returned `next_call`",
        "Running/stalled is nonterminal",
        "`operation_interrupted`",
        "authorized pending scope",
        "Never ask “continue?” after authorization",
        "resume the same workspace/session/job",
        "## VIEW AND XML GATE",
        "unexpected multiple matches",
        "Studio/database overlays",
        "Resolve target, owner, installed definition",
        "at most one useful live lookup",
        "source_verified_live_unverified",
        "means module scope",
    ]
    for marker in required_markers:
        assert marker in text

    hidden_direct_calls = [
        "repo.prepare",
        "repo.read",
        "git.diff",
        "workflow.frontend_pattern_preflight",
        "workflow.verify_against_core",
        "workflow.safe_deploy_module",
    ]
    for marker in hidden_direct_calls:
        assert marker not in text
