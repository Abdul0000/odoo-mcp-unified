from __future__ import annotations

import asyncio
import inspect

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import JobStatus, ToolCallRequest
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers import tools, workflow


def test_tools_call_awaits_safe_deploy_module_and_returns_final_result(tmp_path, monkeypatch):
    """Regression: the MCP /tools bridge must await async safe_deploy_module.

    Without the await, tools.call_tool returns a coroutine object for
    workflow.safe_deploy_module. Real MCP clients then see a transport/server
    failure and the agent may incorrectly tell the user the code is only local
    and they should run the upgrade manually. The bridge must return the actual
    final WorkflowReport when wait_for_result=true.
    """
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)

    project = tmp_path / "repo"
    (project / "sample_custom_module" / "models").mkdir(parents=True)
    (project / "sample_custom_module" / "__manifest__.py").write_text("{'name': 'Sample Module'}\n", encoding="utf-8")

    def fake_run(payload, job_id):
        store = JsonStore()
        job = store.get_job(job_id)
        store.finish_job(
            job,
            JobStatus.passed,
            {
                "status": "passed",
                "message": "Upgrade confirmed successful after 1 attempt(s).",
                "module_name": payload.module_name,
                "module_upgrade": {
                    "requested": True,
                    "status": "passed",
                    "passed": True,
                    "attempt": 1,
                    "attempts": 1,
                    "message": "Upgrade confirmed successful after 1 attempt(s).",
                },
            },
        )

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "_run_safe_deploy", fake_run)
    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(tools.call_tool(ToolCallRequest(
        name="workflow.safe_deploy_module",
        arguments={
            "project_path": str(project),
            "module_name": "sample_custom_module",
            "commit_message": "Add emergency contact field",
            "paths": ["sample_custom_module/models/sample_model.py"],
            "branch": "feature_branch",
            "run_qa": False,
            "wait_for_odoosh": True,
            "upgrade_module": True,
            "wait_for_result": True,
        },
    )))

    assert result.status == "ok"
    assert not inspect.iscoroutine(result.result)
    assert isinstance(result.result, dict)
    assert result.result["status"] == "passed"
    assert result.result["summary"]["message"] == "Upgrade confirmed successful after 1 attempt(s)."
    assert "manual" not in str(result.result).lower()
    assert "transient server issue" not in str(result.result).lower()
