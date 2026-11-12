from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import settings

from .routers.approvals import router as approvals_router
from .routers.browser import router as browser_router
from .routers.git import router as git_router
from .routers.jobs import router as jobs_router
from .routers.mcp import (
    router as mcp_router,
    qwen_router as qwen_mcp_router,
    claude_router as claude_mcp_router,
    chatgpt_router as chatgpt_mcp_router,
    grok_router as grok_mcp_router,
    provider_tool_names_safe,
)
from .routers.odoo import router as odoo_router
from .routers.odoosh import router as odoosh_router
from .routers.oauth import router as oauth_router
from .routers.qa import router as qa_router
from .routers.repo import router as repo_router
from .routers.tools import router as tools_router
from .routers.workflow import router as workflow_router
from .services.prompt_service import load_mcp_system_prompt, load_client_check_system_prompt
from .services.runtime_identity_service import SERVICE_VERSION, runtime_identity
from .services.workflow_progress_service import WorkflowProgressService

_HOSTED_MARKERS = ("RENDER", "KOYEB_APP_NAME", "FLY_APP_NAME", "DYNO", "K_SERVICE", "ODOO_AGENT_REQUIRE_STRONG_TOKEN")
if any(os.getenv(name, "").lower() not in {"", "0", "false", "no"} for name in _HOSTED_MARKERS) and settings.api_token == "change-me":
    raise RuntimeError("ODOO_AGENT_API_TOKEN must be set to a non-default secret on a hosted deployment")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_required_tool_registry()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Odoo MCP Agent Connector",
    version=SERVICE_VERSION,
    description=(
        "Provider-safe Odoo 16-19 development connector with server-owned source discovery, "
        "verification, Git, Odoo.sh deployment, recovery jobs, OAuth, and on-demand skills."
    ),
)


REQUIRED_CODING_TOOLS = {
    "workflow_odoo_code_change",
    "workflow_odoo_debug_and_fix",
    "workflow_resume_continuation",
    "workflow_resume_job",
}


def tool_registry_status() -> dict[str, object]:
    registered = set(provider_tool_names_safe())
    missing = sorted(REQUIRED_CODING_TOOLS - registered)
    return {
        "registered_tools": sorted(registered),
        "missing_tools": missing,
        "coding_workflow_ready": not missing,
    }


def validate_required_tool_registry() -> None:
    status = tool_registry_status()
    if status["missing_tools"]:
        raise RuntimeError(
            "Missing required MCP coding tools: "
            + ", ".join(status["missing_tools"])
        )


@app.get("/health")
def health() -> dict[str, object]:
    status = tool_registry_status()
    return {
        "status": "ok" if status["coding_workflow_ready"] else "degraded",
        "service": "odoo-mcp-agent-connector",
        "runtime": runtime_identity(),
        "workflow_progress": WorkflowProgressService.summary(),
        **status,
    }


@app.get("/agent-instructions")
def agent_instructions() -> dict:
    """Full agent system prompt stored in connector.
    MCP client system instructions can contain only:
    'Before any task, call workflow.agent_instructions first and follow exactly.'
    """
    from .config import settings, get_client_names
    from .services.client_resolver_service import ClientResolverService
    from .services.client_gate_service import ClientGateService
    client_names = get_client_names()
    if len(client_names) > 1 and not ClientGateService().any_validated_client():
        return {
            "version": "client-check-gate",
            "runtime": runtime_identity(),
            "prompt_source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
            "system_prompt": load_client_check_system_prompt(),
            "requires_client_selection": True,
            "requires_validation": True,
            "available_client_names": client_names,
            "next_action": "Call client_check, ask the user to select client_name, then call client_check(client_name=..., validate=true). After it passes, call workflow_agent_instructions again.",
        }
    try:
        active_client = ClientResolverService().resolve_client(require_complete=False)
    except Exception:
        active_client = None
    repo = (active_client.repo if active_client else None) or settings.target_git_repo or "<repo_url>"
    branch = (active_client.branch if active_client else None) or settings.target_git_branch or "<branch>"
    path = (active_client.path if active_client else None) or settings.target_project_path or "/workspace/repos/<repo>"

    return {
        "version": "v18-compact",
        "runtime": runtime_identity(),
        "prompt_source": "MCP_SYSTEM_PROMPT.md",
        "system_prompt": load_mcp_system_prompt(),
        "instructions": {
            "role": "Odoo 16-19 development agent. Use the provider-safe composite workflow; installed source is final authority.",
            "rules": [
                "Follow system_prompt exactly. Use workflow_odoo_code_change or its stated lane, execute returned next_call values, and trust server claim fields over prose.",
                "Never call hidden repo, Git, verification, or deploy tools, expose credentials, or put Pydantic code inside Odoo addons; terminal success requires the proof defined in system_prompt.",
            ],
            "provider_flow": {
                "code_change": "workflow_odoo_code_change",
                "debug_and_fix": "workflow_odoo_debug_and_fix",
                "module_analysis": "workflow_module_analyze",
                "visual_change": "workflow_frontend_visual_implement",
                "direct_install_check": "workflow_module_install_and_check",
                "continuation": ["jobs_get", "workflow_resume_job", "workflow_resume_continuation"],
                "diagnostics": ["artifacts_read", "odoosh_error_pack"],
                "terminal_success": ["deploy_confirmed=true", "already_deployed_confirmed=true"],
                "debug_terminal_success": "truth_gate.verified_fixed=true",
                "terminal_action": "stop_and_summarize",
                "inspect_then_write": "If the composite workflow requests writes, use its focused inspection result and call the same composite tool again with complete writes.",
            },
            "skills": {
                "mode": "on_demand",
                "list_tool": "skills_list",
                "read_tool": "skills_read",
                "selection": (
                    "For an unclear domain, call skills_list with task_description and load only its "
                    "single primary recommendation. Do not load overlapping skills."
                ),
            },
            "multi_client": {
                "available_clients": client_names if client_names else ["(using TARGET_GIT_* configuration)"],
                "selection_rule": "When more than one client exists, client_check must pass for the explicitly selected client before workflow calls.",
            },
            "config": {
                "repo_url": repo,
                "branch": branch,
                "project_path": path,
            },
        },
    }

@app.get("/")
def root() -> dict[str, object]:
    return {
        "service": "Odoo MCP Agent Connector",
        "runtime": runtime_identity(),
        "ui": "not_required",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "tools": "/tools/list",
        "mcp": "/mcp",
        "mcp_profile": "unified-provider-safe",
        # Derived from routers.mcp._PROVIDER_TOOL_NAMES, not hardcoded, so root
        # metadata stays consistent with the actual MCP tools/list response.
        "provider_tools": provider_tool_names_safe(),
        "compatibility_mcp": {
            "chatgpt": "/chatgpt/mcp",
            "claude": "/claude/mcp",
            "qwen": "/qwen/mcp",
            "grok": "/grok/mcp",
        },
        "workflow": ["/workflow/capability-check", "/workflow/full-verify", "/workflow/safe-deploy-module"],
    }


app.include_router(oauth_router)
app.include_router(qa_router)
app.include_router(repo_router)
app.include_router(git_router)
app.include_router(odoosh_router)
app.include_router(odoo_router)
app.include_router(browser_router)
app.include_router(approvals_router)
app.include_router(jobs_router)
app.include_router(tools_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(mcp_router)
app.include_router(qwen_mcp_router)
app.include_router(claude_mcp_router)
app.include_router(chatgpt_mcp_router)
app.include_router(grok_mcp_router)
app.include_router(workflow_router)
