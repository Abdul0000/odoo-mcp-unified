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
    assert len(text) < 12_000

    required_markers = [
        "## COMPOSITE WORKFLOW",
        "## ON-DEMAND SKILLS",
        "skills_list",
        "skills_read",
        "odoo-website-development",
        "odoo-pos-development",
        "odoo-accounting-development",
        "odoo-manufacturing-development",
        "odoo-inventory-development",
        "odoo-sales-development",
        "odoo-purchase-development",
        "odoo-hr-payroll-development",
        "odoo-crm-pipeline-development",
        "odoo-helpdesk-development",
        "odoo-view-development",
        "odoo-backend-development",
        "odoo-frontend-owl-development",
        "odoo-integration-development",
        "odoo-security-development",
        "odoo-runtime-debugging",
        "odoo-test-development",
        "missing_context",
        "context_degradation",
        "trusted evidence",
        "odoo-code-review",
        "odoo-upgrade-migration",
        "odoo-ai-mcp-development",
        "single primary-owner recommendation",
        "workflow_odoo_code_change",
        "workflow_module_analyze",
        "workflow_module_install_and_check",
        "workflow_frontend_visual_implement",
        "odoo_query",
        "odoo_search_read",
        "odoo_read_group",
        "do not load a development skill or inspect source",
        "workflow_resume_job",
        "workflow_resume_continuation",
        "workflow_busy",
        "jobs_get",
        "deploy_confirmed=true",
        "## CODE QUALITY CONTRACT",
        "production-ready, task-scoped code",
        "Prevent N+1 queries",
        "parameterized SQL only when measurably justified",
        "A small deterministic fix remains surgical",
        "workflow_profile",
        "needs_clarification",
        "dependency-ordered local batches",
        "Odoo-native addon tests",
        "pinned validated client identity",
        "## EXECUTION KERNEL",
        "decisive, evidence-bound, scope-locked, and outcome-driven",
        "minimum sufficient evidence",
        "lock scope and stop inspecting or re-planning",
        "one focused inspection, one complete write set",
        "one deployment cycle when enabled",
        "Never repeat a completed phase",
        "Stop immediately on verified terminal success",
        "## PRE-EXECUTION PLAN",
        "No plan block for code work: act first",
        "Never generate a plan during `needs_writes`",
        "## REAL CODER MODE",
        "You are the engineer, not a narrator",
        "Never stop on a recoverable failure",
        "\"implement\", \"push\", \"do it\", \"go\", \"continue\" all mean run the whole chain now",
        "A result carrying `next_call` is an instruction",
        "A watchdog `stalled` label is not a terminal result",
        "`operation_interrupted`",
        "A short follow-up such as `push` means continue the pending workflow",
        "omit it for a new task",
        "locks the canonical inspected task",
        "## VIEW AND XML GATE",
        "unexpected multiple matches",
        "Studio/database overlays",
        "exact technical target",
        "at most one useful live lookup",
        "source_verified_live_unverified",
        "denotes module scope",
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
