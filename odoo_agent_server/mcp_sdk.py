"""Official MCP transport, with the connector's existing domain/tool policy.

Standard HTTP clients use this adapter at the existing MCP URLs. Headerless
JSON-RPC integrations continue through the compatibility routes.
"""
from __future__ import annotations

import hashlib
import asyncio
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams, CallToolResult, GetPromptRequestParams,
    GetPromptResult, ListPromptsResult, ListResourcesResult, ListResourceTemplatesResult, ListToolsResult,
    ElicitRequest, ElicitRequestFormParams, InputRequiredResult,
    PaginatedRequestParams, Prompt, PromptMessage, ReadResourceRequestParams,
    ReadResourceResult, Resource, TextContent, TextResourceContents, Tool,
)
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import _request_token, require_token
from .config import settings
from .routers import mcp as bridge
from .services.prompt_service import load_client_check_system_prompt, load_mcp_system_prompt
from .services.runtime_identity_service import SERVICE_VERSION
from .services.mcp_telemetry import rpc_operation
from .services.mcp_clarification_service import McpClarificationService, digest
from .services.client_settings_store import atomic_json
from .services.async_io import durable_thread_call


_PROMPTS = {
    "client_check_system_prompt": (
        "Client Check Gate Prompt", "Client selection and validation instructions.",
        load_client_check_system_prompt,
    ),
    "mcp_system_prompt": (
        "MCP Workflow Instructions", "Workflow instructions; validate the selected client before operations.",
        load_mcp_system_prompt,
    ),
}
_RESOURCES = {
    "odoo-mcp://instructions/client-check": "client_check_system_prompt",
    "odoo-mcp://instructions/workflow": "mcp_system_prompt",
}
_PATHS = {"/mcp", "/claude/mcp", "/chatgpt/mcp", "/grok/mcp", "/qwen/mcp"}


def _invalid(message: str) -> MCPError:
    return MCPError(code=-32602, message=message)


def _no_cursor(params: PaginatedRequestParams | None) -> None:
    if params is not None and params.cursor:
        raise _invalid("This bounded catalog has no continuation cursor")


def _profile(ctx: ServerRequestContext) -> tuple[str, bool]:
    request = ctx.request
    requested = (
        (request.query_params.get("profile") or request.headers.get("x-mcp-profile") or "").strip().lower()
        if request is not None else ""
    )
    return (requested if requested in {"task", "execution"} else "provider",
            bridge._requested_minimal(request))


async def _list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
    _no_cursor(params)
    profile, minimal = _profile(ctx)
    return ListToolsResult(tools=[
        Tool.model_validate(item) for item in bridge._cached_tools_list(profile, minimal=minimal)
    ], ttl_ms=settings.mcp_cache_ttl_seconds * 1000, cache_scope="private")


async def _call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult | InputRequiredResult:
    # Profiles change discovery only. The shared provider policy still prevents
    # hidden low-level tools from bypassing workflow/verification guardrails.
    async with rpc_operation(ctx, params.name) as info:
        if params.request_state is not None:
            result = await _resume_clarification(ctx, params)
        elif params.input_responses is not None:
            raise _invalid("inputResponses requires its original requestState")
        else:
            result = await bridge.call_tool_result(params.name, params.arguments or {}, "provider")
        structured = result.get("structuredContent") or {}
        info.update(is_error=result.get("isError", False), **{
            key: structured[key] for key in ("task_id", "job_id", "operation_id", "process_id", "workspace_id")
            if key in structured
        })
        capabilities = ctx.session.client_capabilities
        elicitation = capabilities.elicitation if capabilities else None
        if (params.request_state is None and ctx.protocol_version >= "2026-07-28"
                and elicitation is not None and elicitation.form is not None):
            candidate = McpClarificationService.continuation(structured)
            if candidate:
                question, call, field = candidate
                service = McpClarificationService()
                try:
                    state = await asyncio.to_thread(service.issue, _principal(ctx), params.name, params.arguments or {}, call, field, result)
                except MCPError:
                    state = None  # Keep the custom input flow on admission contention.
                if state:
                    return InputRequiredResult(request_state=state, input_requests={
                        "business_clarification": ElicitRequest(params=ElicitRequestFormParams(
                            message=str(question.get("prompt") or "Clarify the requested business behavior")[:2000],
                            requested_schema={"type": "object", "properties": {"answer": {
                                "type": "string", "title": "Your answer", "minLength": 1, "maxLength": 4000,
                            }}, "required": ["answer"]},
                        )),
                    })
        return CallToolResult.model_validate(result)


def _principal(ctx):
    user = ctx.request.scope.get("user") if ctx.request else None
    if not isinstance(user, AuthenticatedUser):
        raise _invalid("Clarification requires an authenticated request")
    return user.access_token.client_id


async def _resume_clarification(ctx, params):
    service = McpClarificationService()
    # claim's nonblocking flock remains held until the durable receipt exists.
    # A cancellation/crash leaves dispatching, never an automatically replayable mutation.
    with service.claim(params.request_state, _principal(ctx), params.name, params.arguments or {}) as (path, record):
        response = (params.input_responses or {}).get("business_clarification")
        if response is None or not hasattr(response, "action"):
            raise _invalid("Missing business clarification response")
        answer_fingerprint = digest({"action": response.action, "content": response.content})
        if record["phase"] == "completed":
            if record["answer_fingerprint"] != answer_fingerprint:
                raise _invalid("Clarification already completed with a different answer")
            return record["receipt"]
        if response.action != "accept":
            result = record["fallback"]
        else:
            answer = (response.content or {}).get("answer")
            if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 4000:
                raise _invalid("Clarification answer must contain 1-4000 characters")
            call = service.answered_call(record, answer.strip())
            record["phase"] = "dispatching"
            await _durable_clarification_write(path, record)
            result = await bridge.call_tool_result(call["tool"], call["arguments"], "provider")
        record.update(phase="completed", receipt=result, answer_fingerprint=answer_fingerprint)
        await _durable_clarification_write(path, record)
        return result


async def _durable_clarification_write(path, record):
    await durable_thread_call(atomic_json, path, record)


async def _list_prompts(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListPromptsResult:
    _no_cursor(params)
    return ListPromptsResult(prompts=[
        Prompt(name=name, title=title, description=description, arguments=[])
        for name, (title, description, _) in _PROMPTS.items()
    ], ttl_ms=settings.mcp_cache_ttl_seconds * 1000, cache_scope="private")


async def _get_prompt(ctx: ServerRequestContext, params: GetPromptRequestParams) -> GetPromptResult:
    aliases = {"client_check": "client_check_system_prompt", "CLIENT_CHECK_SYSTEM_PROMPT": "client_check_system_prompt",
               "system_prompt": "mcp_system_prompt", "MCP_SYSTEM_PROMPT": "mcp_system_prompt"}
    prompt = _PROMPTS.get(aliases.get(params.name, params.name))
    if prompt is None:
        raise _invalid("Unknown prompt")
    _, description, loader = prompt
    return GetPromptResult(description=description, messages=[
        PromptMessage(role="user", content=TextContent(type="text", text=loader()))
    ])


async def _list_resource_templates(ctx, params):
    _no_cursor(params)
    return ListResourceTemplatesResult(resource_templates=[], ttl_ms=settings.mcp_cache_ttl_seconds * 1000, cache_scope="private")


async def _list_resources(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListResourcesResult:
    _no_cursor(params)
    return ListResourcesResult(resources=[
        Resource(uri=uri, name=name, title=_PROMPTS[name][0],
                 description=_PROMPTS[name][1], mime_type="text/plain")
        for uri, name in _RESOURCES.items()
    ], ttl_ms=settings.mcp_cache_ttl_seconds * 1000, cache_scope="private")


async def _read_resource(ctx: ServerRequestContext, params: ReadResourceRequestParams) -> ReadResourceResult:
    name = _RESOURCES.get(params.uri)
    if name is None:
        raise MCPError(code=-32602 if ctx.protocol_version >= "2026-07-28" else -32002, message="Resource not found")
    return ReadResourceResult(contents=[TextResourceContents(
        uri=params.uri, mime_type="text/plain", text=_PROMPTS[name][2](),
    )], ttl_ms=settings.mcp_cache_ttl_seconds * 1000, cache_scope="private")


server = Server(
    "odoo-mcp-agent-connector", version=SERVICE_VERSION,
    instructions=load_client_check_system_prompt() + (
        "\nFor focused coding use profile=task: after client validation call task_open, "
        "then task_run; reconcile pending work with task_resume. "
        "Only explicit deployment requests may use task_deploy."
    ),
    on_list_tools=_list_tools, on_call_tool=_call_tool,
    on_list_prompts=_list_prompts, on_get_prompt=_get_prompt,
    on_list_resources=_list_resources, on_read_resource=_read_resource,
    on_list_resource_templates=_list_resource_templates,
)


def transport_security() -> TransportSecuritySettings:
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
    for base in (settings.public_base_url, settings.oauth_issuer):
        if base:
            url = urlsplit(base)
            if url.netloc:
                hosts.append(url.netloc)
                origins.append(f"{url.scheme}://{url.netloc}")
    return TransportSecuritySettings(
        allowed_hosts=list(dict.fromkeys([*hosts, *settings.mcp_allowed_hosts])),
        allowed_origins=list(dict.fromkeys([*origins, *settings.mcp_allowed_origins])),
    )


def create_sdk_app():
    # Session managers are single-use. Build one for each host lifespan so
    # restarts/test lifespans cannot reuse a stopped task group. Keep SDK
    # streaming/cancellation and bounded legacy sessions; durable task/job
    # state remains in the existing services, independent of these sessions.
    return server.streamable_http_app(
        stateless_http=False, json_response=False, max_sessions=settings.mcp_max_sessions,
        session_idle_timeout=settings.mcp_session_idle_seconds, transport_security=transport_security(),
    )


class MCPTransportMiddleware:
    """Select the SDK by standard transport headers without breaking old clients."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path.rstrip("/")
        qwen_path = path.startswith("/qwen/mcp/")
        standard = (
            "text/event-stream" in request.headers.get("accept", "").lower()
            or "mcp-protocol-version" in request.headers
            or "mcp-session-id" in request.headers
            or request.method == "DELETE"
        )
        if not standard or (path not in _PATHS and not qwen_path):
            await self.app(scope, receive, send)
            return
        try:
            if path == "/qwen/mcp" or qwen_path:
                token = path.removeprefix("/qwen/mcp/") if qwen_path else request.query_params.get("access_token")
                await run_in_threadpool(bridge._validate_qwen_url_token, token)
            else:
                await run_in_threadpool(require_token, request, request.headers.get("authorization"),
                                        request.headers.get("x-api-key"))
                token = _request_token(request, request.headers.get("authorization"), request.headers.get("x-api-key"))
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                               headers=exc.headers)(scope, receive, send)
            return
        # Map aliases to one transport; retain query/profile and headers. Context
        # comes from the SDK's per-message request, never global client state.
        sdk_app = getattr(scope["app"].state, "mcp_sdk_app", None)
        if sdk_app is None:
            await JSONResponse({"detail": "MCP transport is not ready"}, status_code=503)(scope, receive, send)
            return
        # Existing credentials remain sufficient. Supply an opaque principal so
        # SDK legacy sessions cannot be reused with a different valid token.
        principal = hashlib.sha256((token or "").encode()).hexdigest()
        sdk_scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "root_path": "",
                     "user": AuthenticatedUser(AccessToken(token=principal, client_id=principal, scopes=[]))}
        await sdk_app(sdk_scope, receive, send)
