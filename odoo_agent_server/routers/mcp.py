from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import ValidationError

from ..auth import AuthDependency
from ..config import settings
from ..services.oauth_service import OAuthStore
from ..services.prompt_service import load_mcp_system_prompt, load_client_check_system_prompt, shared_prompt_payload, client_check_prompt_payload
from ..services.qwen_token_service import QwenTokenService
from ..services.store_service import JsonStore
from ..services.toon_service import BLOCK_KEYS, render_tool_text
from ..services.mcp_operation_guard_service import McpOperationGuardService
from ..services.workflow_watchdog_service import WorkflowWatchdogService
from .tools import _dispatch, format_tool_validation_error, internal_tool_name, list_tools, strict_safe_tool_name, tool_input_schema, tool_output_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])
qwen_router = APIRouter(prefix="/qwen", tags=["mcp-qwen"] )
claude_router = APIRouter(prefix="/claude", tags=["mcp-claude"], dependencies=[AuthDependency])
chatgpt_router = APIRouter(prefix="/chatgpt", tags=["mcp-chatgpt"], dependencies=[AuthDependency])
grok_router = APIRouter(prefix="/grok", tags=["mcp-grok"], dependencies=[AuthDependency])


def _shell_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _dump_any(value: Any) -> Any:
    """Recursively convert Pydantic models (and lists/dicts of them) into
    plain JSON-safe values using model_dump(mode="json")."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump_any(item) for item in value]
    if isinstance(value, dict):
        return {k: _dump_any(v) for k, v in value.items()}
    return value


def _to_structured_content(result: Any, tool_name: str) -> dict[str, Any]:
    """Return the server's backward-compatible object response envelope.

    Several tools (repo.list, repo.list_custom_modules, jobs.list,
    approvals.list, audit.list, workflow tools returning matches, etc.)
    naturally return a bare list of Pydantic models. Older provider clients
    used by this connector reject a raw list at the top level with:
        "Input should be a valid dictionary [type=dict_type]"
    Wrap any non-dict result in {"items": [...], "count": N} so every tool
    call stays compatible while newer MCP clients can still consume it as JSON.
    """
    dumped = _dump_any(result)
    if isinstance(dumped, dict):
        return dumped
    if isinstance(dumped, list):
        return {"items": dumped, "count": len(dumped)}
    # Scalars (str/int/bool/None) — still must be a dict per spec.
    return {"result": dumped}


def _cap_text(text: str, max_chars: int = 24000) -> str:
    """Truncate an oversized tool-result text payload to keep agent context
    lean across long sessions. Keeps the head and tail (most relevant parts for
    errors/diffs/logs) and drops the middle with a clear marker.
    """
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    omitted = len(text) - max_chars
    return (
        f"{head}\n\n...[{omitted} chars omitted — output was too large for chat context. "
        f"Full data is still available in structuredContent, or re-call with a narrower "
        f"scope, e.g. fewer tail_lines / max_bytes / a more specific file path]...\n\n{tail}"
    )


def _preview_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    return f"{text[:head_chars]}\n\n...[{len(text) - max_chars} chars omitted; use artifacts.read for exact content]...\n\n{text[-tail_chars:]}"


def _artifact_next_call(artifact: dict[str, Any], max_chars: int = 12000) -> dict[str, Any]:
    return {
        "tool": "artifacts.read",
        "arguments": {"artifact_id": artifact["artifact_id"], "offset": 0, "max_chars": max_chars},
    }


_CONTROL_SCHEMA_VERSION = "2.0"
_CONTROL_REQUIRED_FIELDS = [
    "status",
    "state_class",
    "outcome",
    "terminal",
    "completion_required",
    "success_allowed",
    "next_call",
    "input_required",
    "input_request",
    "error",
]

_CONTROL_STATE_CLASSES = {
    "succeeded",
    "running",
    "action_required",
    "input_required",
    "failed",
    "blocked",
}
_ACTIVE_JOB_STATUSES = {"queued", "running", "started", "pending", "active"}
_HUMAN_INPUT_STATUSES = {
    "needs_clarification",
    "clarification_required",
    "verification_authorization_required",
    "authorization_required",
    "approval_required",
    "ready_to_deploy",
    "input_required",
}
_SUCCESS_STATUSES = {
    "ok",
    "passed",
    "success",
    "succeeded",
    "completed",
    "verified",
    "ready",
}


def _lift_nested_control(out: dict[str, Any]) -> dict[str, Any]:
    """Lift a wrapped job/workflow continuation to the provider boundary.

    Older Odoo result models store the real lifecycle under ``result``.  A
    provider must see that lifecycle at the top level; otherwise a started job
    looks like a terminal ``ok``/``failed`` response and polling stops.
    """
    nested = out.get("result")
    if not isinstance(nested, dict):
        return out
    control_keys = {
        "state_class", "completion_required", "success_allowed", "terminal",
        "next_call", "input_required", "input_request", "retryable",
    }
    nested_lifecycle = str(nested.get("status") or nested.get("job_status") or "").strip().lower()
    if not control_keys.intersection(nested) and nested_lifecycle not in {
        *_ACTIVE_JOB_STATUSES, "passed", "warning", "failed",
    }:
        return out

    lifted = dict(out)
    wrapper_status = str(lifted.get("status") or "")
    lifted.setdefault("wrapper_status", wrapper_status or None)
    nested_status = nested.get("status") or nested.get("job_status")
    if nested_status:
        lifted["status"] = nested_status
    for key in (
        "state_class", "completion_required", "success_allowed", "terminal",
        "next_call", "input_required", "input_request", "retryable",
        "retry_after_seconds", "poll_after_seconds", "job_id",
    ):
        if key in nested:
            lifted[key] = nested[key]
    if nested.get("error") is not None:
        lifted["error"] = nested["error"]
    elif nested.get("completion_required") is True:
        notice = lifted.get("error")
        if notice:
            lifted["wrapper_notice"] = notice
        lifted["error"] = None
    return lifted


def _build_input_request(out: dict[str, Any], next_call: dict[str, Any] | None) -> dict[str, Any]:
    existing = out.get("input_request")
    if isinstance(existing, dict):
        request = dict(existing)
        request.setdefault("resume_call", next_call)
        return request
    gate = out.get("clarification_gate")
    if isinstance(gate, dict):
        return {
            "kind": "clarification",
            "field": "clarification_answer",
            "prompt": str(gate.get("question") or out.get("message") or "Clarification is required."),
            "reason": gate.get("reason"),
            "resume_call": next_call,
        }
    required = []
    if isinstance(next_call, dict):
        required = list(next_call.get("required_generated_arguments") or [])
    return {
        "kind": "authorization" if "authoriz" in str(out.get("status") or "").lower() else "user_input",
        "field": required[0] if len(required) == 1 else None,
        "required_fields": required,
        "prompt": str(out.get("message") or "User input is required to continue."),
        "resume_call": next_call,
    }


_TOON_AUTO_TOOLS = {"odoo.search_read", "odoo.read_group", "odoo.query", "odoosh.query"}


def _render_result_text(structured: Any, internal_name: str) -> str:
    """Model-facing text for a tool result: TOON where it is smaller, JSON otherwise.

    structuredContent stays plain JSON for clients that consume it; only the text block changes.
    ODOO_AGENT_TOON_OUTPUT: auto (ws_* and tabular query tools), all, off.
    """
    mode = (settings.toon_output or "auto").strip().lower()
    lean = internal_name.startswith("ws.")
    if mode != "off" and (mode == "all" or lean or internal_name in _TOON_AUTO_TOOLS):
        return render_tool_text(structured, block_keys=BLOCK_KEYS if lean else None)
    return json.dumps(structured, ensure_ascii=False, default=str)


def _ensure_provider_control_envelope(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Add one explicit, invariant provider-facing lifecycle contract."""
    out = _lift_nested_control(dict(value) if isinstance(value, dict) else {"result": value})
    status_value = out.get("status")
    status_text = str(status_value or ("failed" if is_error else "ok"))
    status_key = status_text.strip().lower().replace("-", "_").replace(" ", "_")

    next_call = out.get("next_call") if isinstance(out.get("next_call"), dict) else None
    explicit_completion = out.get("completion_required")
    explicit_success = out.get("success_allowed")
    if isinstance(explicit_completion, bool):
        completion_required = explicit_completion
    else:
        completion_required = bool(next_call or status_key in _ACTIVE_JOB_STATUSES)

    if completion_required and next_call is None and out.get("job_id"):
        next_call = {
            "tool": "jobs.get",
            "arguments": {
                "job_id": str(out["job_id"]),
                "wait_for_result": True,
                "wait_timeout_seconds": 600,
            },
        }

    input_required = bool(out.get("input_required")) or isinstance(out.get("input_request"), dict) or status_key in _HUMAN_INPUT_STATUSES
    explicit_state = str(out.get("state_class") or "").strip().lower()
    if explicit_state not in _CONTROL_STATE_CLASSES:
        explicit_state = ""

    if explicit_state:
        state_class = explicit_state
    elif completion_required and input_required:
        state_class = "input_required"
    elif completion_required and next_call:
        called_tool = internal_tool_name(str(next_call.get("tool") or ""))
        state_class = "running" if called_tool == "jobs.get" or status_key in _ACTIVE_JOB_STATUSES else "action_required"
    elif completion_required:
        # A non-terminal state without either a callable continuation or a
        # structured input request is unsafe. Close it as a blocker instead of
        # making the model guess or ask a vague question.
        state_class = "blocked"
    elif is_error or status_key in {"failed", "error", "invalid"}:
        state_class = "failed"
    elif explicit_success is False or status_key == "warning":
        state_class = "blocked"
    elif explicit_success is True or status_key in _SUCCESS_STATUSES:
        state_class = "succeeded"
    else:
        # Plain read tools use domain-specific statuses. A completed tool call
        # with no error, no continuation, and no negative success gate is a
        # successful terminal result.
        state_class = "succeeded"

    if state_class in {"running", "action_required"} and next_call is None:
        state_class = "blocked"
    completion_required = state_class in {"running", "action_required", "input_required"}
    terminal = not completion_required
    input_required = state_class == "input_required"
    input_request = _build_input_request(out, next_call) if input_required else None
    if terminal and next_call is not None:
        out.setdefault("recovery_call", next_call)
        next_call = None
    outcome = {
        "succeeded": "success",
        "running": "pending",
        "action_required": "pending",
        "input_required": "input_required",
        "failed": "failed",
        "blocked": "blocked",
    }[state_class]
    success_allowed = bool(state_class == "succeeded" and explicit_success is not False)
    error_value = out.get("error")
    error_code = out.get("error_code") or out.get("error_type")
    retryable_value = out.get("retryable")

    out.update(
        {
            "schema_version": str(out.get("schema_version") or _CONTROL_SCHEMA_VERSION),
            "status": status_text,
            "state_class": state_class,
            "outcome": outcome,
            "terminal": terminal,
            "completion_required": completion_required,
            "success_allowed": success_allowed,
            "next_call": next_call,
            "input_required": input_required,
            "input_request": input_request,
            "error": error_value if error_value is not None else None,
            "error_code": str(error_code) if error_code is not None else None,
            "retryable": retryable_value if isinstance(retryable_value, bool) else None,
        }
    )

    # Non-terminal jobs always expose a durable resume path, so a client that
    # loses the poll loop can recover without resubmitting the heavy workflow.
    if (
        not terminal
        and isinstance(out.get("job_id"), str)
        and not isinstance(out.get("resume_next_call"), dict)
    ):
        out["resume_next_call"] = {
            "tool": "workflow.resume_job",
            "arguments": {"job_id": str(out["job_id"])},
        }

    # Enforce the lifecycle invariant after every branch above: terminal means
    # no next call, and non-terminal means a callable continuation exists.
    if out["terminal"]:
        out["completion_required"] = False
        if isinstance(out.get("next_call"), dict):
            out.setdefault("recovery_call", out["next_call"])
            out["next_call"] = None
    elif not isinstance(out.get("next_call"), dict):
        out["next_call"] = None
        out["completion_required"] = False
        out["terminal"] = True
        out["state_class"] = "blocked"
        out["outcome"] = "blocked"
        out["success_allowed"] = False
    return out


def _provider_safe_next_calls(value: Any) -> Any:
    """Convert next_call.tool values to callable remote-MCP tool names.

    Structured results are internal JSON, not actual Tool descriptors.  Provider
    clients only see safe names like ``jobs_get``; returning dotted internal names
    makes the recovery path non-callable and causes duplicate heavy retries.
    """
    if isinstance(value, list):
        return [_provider_safe_next_calls(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, child in value.items():
        if key == "tool" and isinstance(child, str):
            internal = internal_tool_name(child)
            # A dotted internal name is not callable by a strict remote-MCP
            # client even when the tool is on the provider surface, so any tool
            # reference (including recovery/next_call paths) is flattened to the
            # strict-safe name. Non-tool scalar values stay untouched.
            if "." in child or internal in _PROVIDER_TOOL_NAMES:
                out[key] = strict_safe_tool_name(internal)
            else:
                out[key] = child
            continue
        out[key] = _provider_safe_next_calls(child)
    if internal_tool_name(str(out.get("tool") or "")) == "jobs.get" and isinstance(out.get("arguments"), dict):
        arguments = dict(out["arguments"])
        requested = int(arguments.get("wait_timeout_seconds", 600) or 600)
        arguments["wait_timeout_seconds"] = WorkflowWatchdogService().provider_wait_timeout(requested)
        out["arguments"] = arguments
    return out


def _callable_continuation(call: Any) -> dict[str, Any] | None:
    """Return a callable continuation for a non-provider tool target.

    The provider manifest is intentionally small, so a server result can name a
    tool that the remote client never received in ``tools/list``. Returning such
    a name makes the client retry or fail with "unknown tool". Keep the original
    intent in ``requested_tool`` and point at a tool the client can actually call.
    """
    if not isinstance(call, dict):
        return None
    tool = str(call.get("tool") or "").strip()
    if not tool:
        return None
    if tool in {strict_safe_tool_name(name) for name in _PROVIDER_TOOL_NAMES}:
        return None
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    job_id = arguments.get("job_id") or call.get("job_id")
    if job_id:
        return {
            "tool": strict_safe_tool_name("jobs.get"),
            "arguments": {
                "job_id": str(job_id),
                "wait_for_result": True,
                "wait_timeout_seconds": 600,
                "poll_interval_seconds": 1.0,
            },
            "requested_tool": tool,
        }
    return {
        "tool": strict_safe_tool_name("workflow.agent_instructions"),
        "arguments": {},
        "requested_tool": tool,
    }


def _ensure_callable_continuations(structured: dict[str, Any]) -> dict[str, Any]:
    """Rewrite top-level continuation calls that the provider cannot invoke."""
    if not isinstance(structured, dict):
        return structured
    for key in ("next_call", "recovery_call", "resume_next_call"):
        replacement = _callable_continuation(structured.get(key))
        if replacement is not None:
            structured[key] = replacement
    input_request = structured.get("input_request")
    if isinstance(input_request, dict):
        replacement = _callable_continuation(input_request.get("resume_call"))
        if replacement is not None:
            input_request["resume_call"] = replacement
    return structured


def _normalize_remote_structured(value: Any, tool_name: str) -> dict[str, Any]:
    """Keep strict compatibility-profile MCP payloads bounded, with full data available by artifact.

    The visible content text was already capped, but several clients also load
    structuredContent into their tool-call state. That can hang on large logs,
    diffs, command output, or job records. This keeps high-signal previews in
    place and stores exact full data for focused follow-up reads.
    """
    store = JsonStore()
    full_json = json.dumps(value, ensure_ascii=False, default=str)
    if len(full_json) <= 18000:
        return value if isinstance(value, dict) else {"result": value}

    truncated_any = False

    def normalize(item: Any, *, list_limit: int = 50) -> Any:
        nonlocal truncated_any
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if isinstance(item, list):
            if len(item) > list_limit:
                truncated_any = True
            # Preserve the advertised array type. Metadata is added by the
            # containing object and the exact payload is retained as an artifact.
            return [normalize(child) for child in item[:list_limit]]
        if not isinstance(item, dict):
            text = str(item)
            if len(text) <= 8000:
                return item
            truncated_any = True
            # Keep scalar fields scalar so outputSchema types remain truthful.
            return _preview_text(text, 8000)

        out: dict[str, Any] = {}
        for key, child in item.items():
            if key in {"stdout", "stderr", "content"} and isinstance(child, str) and len(child) > 12000:
                artifact = store.create_artifact(key, tool_name, child)
                out[key] = _preview_text(child, 12000)
                out[f"{key}_truncated"] = True
                out[f"{key}_artifact"] = artifact
                out[f"{key}_next_call"] = _artifact_next_call(artifact)
            elif isinstance(child, list):
                limit = 25 if key == "steps" else 50
                out[key] = normalize(child, list_limit=limit)
                if len(child) > limit:
                    out[f"{key}_count"] = len(child)
                    out[f"{key}_truncated"] = True
            else:
                out[key] = normalize(child)
        return out

    normalized = normalize(value)
    if truncated_any and isinstance(normalized, dict):
        artifact = store.create_artifact("structured", tool_name, value)
        normalized["truncated"] = True
        normalized["details_ref"] = artifact
        normalized["details_next_call"] = _artifact_next_call(artifact)
        normalized["next_action"] = "Use details_next_call for exact output, or re-call the source tool with a narrower filter."
    normalized_json = json.dumps(normalized, ensure_ascii=False, default=str)
    if isinstance(normalized, dict) and len(normalized_json) <= 24000:
        return normalized

    artifact = store.create_artifact("structured", tool_name, value)
    source = normalized if isinstance(normalized, dict) else {}
    compact_keys = {
        "schema_version", "status", "state_class", "outcome", "phase", "job_id", "operation_id", "terminal",
        "completion_required", "success_allowed", "next_call", "input_required",
        "input_request", "resume_call", "error", "error_code", "retryable", "deploy_confirmed",
        "resume_token", "resume_token_expires_at", "resume_token_available", "resume_next_call",
        "reproduction_passed", "expected_result_verified", "claim_status", "truth_gate",
    }
    compact = {key: source[key] for key in compact_keys if key in source}
    compact.update({
        "truncated": True,
        "preview": _preview_text(full_json, 12000),
        "artifact": artifact,
        "details_ref": artifact,
        "details_next_call": _artifact_next_call(artifact),
        "next_action": "Use details_next_call for exact output; preserve next_call for workflow continuation.",
    })
    return compact


def _arguments_for_profile(tool_name: str, arguments: dict[str, Any], profile: str | None) -> dict[str, Any]:
    adjusted = dict(arguments)

    if tool_name == "workflow.odoo_code_change" and profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
        # The composite workflow performs repository, QA, and deployment I/O and
        # must not hold one provider HTTP request open. The server persists the
        # operation before returning, so a transport retry reuses the same job.
        adjusted["wait_for_result"] = False
        return adjusted

    if tool_name == "workflow.safe_deploy_module":
        # Hybrid safety contract for MCP browser clients (Grok/ChatGPT/Claude/Qwen):
        # preserve the full safe-deploy pipeline in a connector-owned background
        # job, but do not keep one MCP HTTP request open while SSH/Odoo.sh work
        # runs. A started/running response is never success; agents must poll the
        # returned next_call/jobs.get until a terminal verified result.
        adjusted["wait_for_result"] = False
        adjusted["wait_timeout_seconds"] = 0
        adjusted.setdefault("poll_interval_seconds", 2.0)
        return adjusted

    if tool_name == "jobs.get":
        # Poll briefly so the UI never hangs. A running response carries
        # completion_required=true and success_allowed=false, so providers cannot
        # honestly exit early or report success until a terminal job result is read.
        if "wait_for_result" not in adjusted:
            adjusted["wait_for_result"] = True
        if adjusted.get("wait_for_result") is True:
            requested_wait = min(max(int(adjusted.get("wait_timeout_seconds", 600) or 600), 1), 600)
            adjusted["wait_timeout_seconds"] = WorkflowWatchdogService().provider_wait_timeout(requested_wait)
            adjusted["poll_interval_seconds"] = min(max(float(adjusted.get("poll_interval_seconds", 1.0) or 1.0), 0.25), 10.0)
            return adjusted
        if profile not in {"provider", "claude", "chatgpt", "grok", "qwen"} or "wait_for_result" not in adjusted:
            return adjusted

    # Keep the legacy ChatGPT-only quick-wait clamp for non-safe-deploy tools.
    # Other MCP clients keep their previous behaviour unless they explicitly use
    # safe_deploy/jobs.get above.
    if profile not in {"provider", "claude", "chatgpt", "grok", "qwen"} or tool_name not in _CHATGPT_QUICK_WAIT_TOOLS:
        return adjusted

    adjusted["wait_for_result"] = False
    adjusted["wait_timeout_seconds"] = min(int(adjusted.get("wait_timeout_seconds", 5) or 5), 5)
    return adjusted


def _humanize_tool_name(name: str) -> str:
    """Fallback: convert 'git.push' -> 'Git Push' if no explicit title is set."""
    tail = name.split(".", 1)[-1]
    return tail.replace("_", " ").title()


def _mcp_safe_tool_name(name: str) -> str:
    return strict_safe_tool_name(name)


def _mcp_name_to_internal(name: str) -> str:
    return internal_tool_name(name)


# Unified provider-safe MCP profile for all remote providers.
# The detailed Odoo coding pipeline is server-owned by workflow.odoo_code_change;
# providers only request the change and poll jobs.get.  This removes duplicate
# repo/scope/deploy loops and prevents direct push/module-deploy bypasses.
_PROVIDER_TOOL_NAMES = {
    "ws.open",
    "ws.status",
    "ws.outline",
    "ws.scaffold",
    "ws.extend",
    "ws.symbol",
    "ws.read",
    "ws.grep",
    "ws.edit",
    "ws.diff",
    "ws.lint",
    "ws.commit",
    "ws.discard",
    "connector.health",
    "skills.list",
    "skills.read",
    "client.check",
    "workflow.agent_instructions",
    "workflow.odoo_code_change",
    "workflow.resume_continuation",
    "workflow.odoo_debug_and_fix",
    "workflow.module_analyze",
    "rea.xray_module",
    "rea.upstream_compare",
    "rea.runtime_trace",
    "rea.api_inspect",
    "rea.impact_predict",
    "rea.self_heal",
    "workflow.module_test",
    "workflow.module_install_and_check",
    "workflow.frontend_visual_implement",
    # Live read-only model access. OdooService uses Odoo.sh SSH first and falls
    # back to HTTPS JSON-RPC; no raw SQL or write method is exposed.
    "odoo.query",
    "odoo.search_read",
    "odoo.read_group",
    "odoo.fields_get",
    "odoo.list_models",
    "odoo.record_create",
    "odoo.record_write",
    "odoo.record_action",
    "odoo.flow_test",
    "odoo.mutation_audit",
    # This tool records an explicit user decision for one pending protected flow.
    "approvals.approve",
    # Source discovery and verification are server-owned steps inside the
    # composite workflows. Keeping them off the provider surface avoids extra
    # planning loops and repeated input-schema payloads.
    # Recovery/continuation tools that provider responses may legitimately return
    # in next_call.  Exposing them keeps the MCP contract honest and prevents
    # agents from retrying the heavy code-change workflow just to read logs or
    # resume a failed safe-deploy step.
    "artifacts.read",
    "workflow.resume_job",
    "jobs.list",
    "jobs.get",
    "odoosh.error_pack",
}

# Lean manifest for clients with a tight planning-context budget (for example
# Claude Code over a remote MCP transport). It keeps the server-owned composite
# workflow, the workspace lane, polling, and discovery, and drops tools that are
# only useful for manual REST/debug flows.
_PROVIDER_TOOL_NAMES_MINIMAL = {
    "ws.open",
    "ws.status",
    "ws.outline",
    "ws.scaffold",
    "ws.extend",
    "ws.symbol",
    "ws.read",
    "ws.grep",
    "ws.edit",
    "ws.diff",
    "ws.lint",
    "ws.commit",
    "ws.discard",
    "connector.health",
    "skills.list",
    "skills.read",
    "client.check",
    "workflow.agent_instructions",
    "workflow.odoo_code_change",
    "workflow.resume_continuation",
    "workflow.odoo_debug_and_fix",
    "workflow.module_analyze",
    "workflow.module_install_and_check",
    "workflow.frontend_visual_implement",
    "odoo.query",
    "odoo.search_read",
    "odoo.read_group",
    "odoo.fields_get",
    "odoo.list_models",
    "artifacts.read",
    "workflow.resume_job",
    "jobs.get",
    "odoosh.error_pack",
}

# Backward-compatible names retained for tests/imports; all provider routes use
# the same minimal flow.
_CLAUDE_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)
_CHATGPT_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)
_GROK_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)


def provider_tool_names_safe() -> list[str]:
    """Return the exact strict tool names advertised to remote provider clients.

    Keep root/health metadata, tests, and MCP tools/list in sync from one
    source of truth so deployments cannot appear to expose only the old five
    tools after recovery tools are added.
    """
    ordered = [tool.name for tool in _tools_for_profile("provider")]
    return [_force_remote_tool_name(strict_safe_tool_name(name), idx) for idx, name in enumerate(ordered)]


_CHATGPT_QUICK_WAIT_TOOLS = {
    "jobs.get",
    "odoo.module_deploy",
    "odoo.module_install",
    "odoo.module_update",
    "odoosh.module_deploy_ssh",
    "odoosh.module_update",
}


_PROVIDER_COMPACT_TOOL_DESCRIPTIONS = {
    "client.check": (
        "Select Odoo client; validate=true checks checkout, Git, SSH, shell and addons. Slow checks return job_id. No writes before validation passes."
    ),
    "workflow.odoo_code_change": (
        "Implement or repair addon; resolves checkout, applies writes, runs QA/core checks and safe deploy. For needs_writes, preserve coding_session_id/workspace_session_id and supply writes."
    ),
    "workflow.resume_continuation": (
        "Resume an interrupted code-change after an ephemeral Render restart. Pass the latest signed resume_token "
        "returned by workflow_odoo_code_change or jobs_get. It restores the bounded task contract and revalidates "
        "current checkout, source, target, QA, and deploy gates before any mutation."
    ),
    "workflow.odoo_debug_and_fix": (
        "Source-first Odoo debugger. Preserve IDs and returned calls; claim fixed only when truth_gate.verified_fixed=true."
    ),
    "workflow.module_analyze": (
        "Read-only addon architecture analysis with bounded source evidence and optional live checks."
    ),
    "rea.xray_module": (
        "Read-only addon graph with transitive/reverse dependencies, cross-module model/field links, and optional live Odoo registry confirmation when client_name is explicit."
    ),
    "rea.upstream_compare": (
        "Compare workspace overrides and fields with matching Odoo Community source via GitHub; advisory only."
    ),
    "rea.api_inspect": (
        "Static HTTP/RPC, routes, timeout and TLS inventory from source or ws_id; no network requests."
    ),
    "rea.impact_predict": (
        "Predict changed-component impact using graph, upstream, API, trace, and optional live registry evidence when client_name is explicit."
    ),
    "workflow.module_install_and_check": (
        "Guarded Odoo module install/upgrade job. Repair source only from a concrete traceback or dependency error; uninstallable registry state alone is not permission to edit code."
    ),
    "workflow.frontend_visual_implement": (
        "Odoo UI/website/POS visual workflow with deterministic asset writes and safe deploy. Use create_module=true only for a standalone root addon; otherwise modify the validated custom addon without editing Odoo core."
    ),
}


def _provider_tool_description(tool) -> str:
    """Return a complete semantic description without cutting a sentence mid-word."""
    description = _PROVIDER_COMPACT_TOOL_DESCRIPTIONS.get(tool.name) or " ".join(str(tool.description).split())
    if len(description) <= 220:
        return description
    compact = description[:217].rsplit(" ", 1)[0].rstrip(" ,;:")
    return compact + "."

_REMOTE_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _force_remote_tool_name(name: Any, fallback_index: int = 0) -> str:
    """Final guard for strict remote-MCP name validation.

    Some clients reject the whole connection when even one returned tool name
    contains a dot or other punctuation. This function is intentionally used at
    the final serialization boundary, not only in descriptors, so no future code
    path can accidentally leak dotted internal names.
    """
    value = strict_safe_tool_name(str(name or f"tool_{fallback_index}"))
    value = re.sub(r"[^a-zA-Z0-9_-]", "_", value).strip("_") or f"tool_{fallback_index}"
    value = value[:64]
    if _REMOTE_TOOL_NAME_RE.match(value):
        return value
    return f"tool_{fallback_index}"[:64]


def _force_remote_tool_list(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hard-sanitize every tools/list item right before returning to clients."""
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, tool in enumerate(tools):
        item = dict(tool)
        name = _force_remote_tool_name(item.get("name"), idx)
        if name in seen:
            suffix = f"_{idx}"
            name = f"{name[:64-len(suffix)]}{suffix}"
        seen.add(name)
        item["name"] = name
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            item["inputSchema"] = {"type": "object", "properties": {}}
        item.setdefault("description", "Odoo MCP tool")
        sanitized.append(item)
    return sanitized


_TOOLS_LIST_CACHE: dict[tuple[str, int, bool], list[dict[str, Any]]] = {}


def _cached_tools_list(profile: str, *, minimal: bool = False) -> list[dict[str, Any]]:
    """tools/list is rebuilt (schema generation + cleanup for every tool) only when settings change.

    Keyed by a hash of the full settings snapshot, so any configuration change invalidates it.
    """
    dump = getattr(settings, "model_dump_json", None)
    fingerprint = None
    if callable(dump):
        try:
            fingerprint = hash(dump())
        except Exception:  # noqa: BLE001 - never let caching break tools/list
            fingerprint = None
    if fingerprint is not None:
        hit = _TOOLS_LIST_CACHE.get((profile, fingerprint, minimal))
        if hit is not None:
            return list(hit)
    built = _force_remote_tool_list([
        _tool_to_mcp(tool, profile=profile)
        for tool in _tools_for_profile(profile, minimal=minimal)
    ])
    if fingerprint is not None:
        if len(_TOOLS_LIST_CACHE) > 16:
            _TOOLS_LIST_CACHE.clear()
        _TOOLS_LIST_CACHE[(profile, fingerprint, minimal)] = built
    return list(built)


def _is_claude_request(request: Request | None) -> bool:
    if request is None:
        return False
    headers = request.headers
    joined = " ".join(
        str(headers.get(name, ""))
        for name in ("user-agent", "x-client-name", "anthropic-version", "mcp-client")
    ).lower()
    return "claude" in joined or "anthropic" in joined


def _tools_for_profile(profile: str | None = None, *, minimal: bool = False) -> list[Any]:
    tools = list_tools(strict_names=False)
    # All remote/provider MCP endpoints intentionally expose the same tiny tool
    # surface.  Full low-level tools remain available through REST /tools for
    # debugging/admin use, but not to provider agents. A lean subset is available
    # for clients with a tight planning-context budget.
    allowed = _PROVIDER_TOOL_NAMES_MINIMAL if minimal else _PROVIDER_TOOL_NAMES
    return [tool for tool in tools if tool.name in allowed]


def _requested_minimal(request: Request | None) -> bool:
    """Return True when the client asked for the lean provider manifest."""
    if request is None:
        return False
    value = (
        request.query_params.get("profile")
        or request.headers.get("x-mcp-profile")
        or ""
    ).strip().lower()
    return value in {"minimal", "lean", "claude-minimal", "claude_code_minimal"}


def _remote_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a compact JSON schema accepted by strict remote MCP clients.

    Claude's remote-MCP frontend can fail the message stream while loading tools
    when schemas contain lots of generated Pydantic metadata or nested $defs. This
    removes non-essential metadata and simplifies nullable anyOf values.
    """
    definitions = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value

        if "$ref" in value:
            ref = str(value.get("$ref") or "")
            definition_name = ref.rsplit("/", 1)[-1]
            if definition_name in {
                "OdooDebugRecordAssertion",
                "OdooDebugReportReplay",
                "OdooDebugRuntimeReplay",
                "OdooDynamicFlowStep",
            }:
                target = definitions.get(definition_name)
                if isinstance(target, dict):
                    return clean(target)
            return {"type": "object"}
        if "anyOf" in value:
            non_null = [item for item in value.get("anyOf", []) if not (isinstance(item, dict) and item.get("type") == "null")]
            if len(non_null) == 1:
                base = clean(non_null[0])
                if "description" in value and isinstance(base, dict):
                    base.setdefault("description", " ".join(str(value["description"]).split())[:36])
                return base
            return {
                "type": "string",
                "description": " ".join(str(value.get("description", "JSON value")).split())[:36],
            }

        out: dict[str, Any] = {}
        for key, child in value.items():
            if key in {"$defs", "title", "default", "examples", "computed_fields"}:
                continue
            if key == "description" and isinstance(child, str):
                # Tool descriptions carry the workflow contract. Field docs only
                # need a compact hint; long examples materially inflate every
                # tools/list response and the provider's planning context.
                out[key] = " ".join(str(child).split())[:36]
                continue
            if key == "additionalProperties":
                # Keep strict false, but drop complex schemas under additionalProperties.
                out[key] = child if isinstance(child, bool) else True
                continue
            out[key] = clean(child)
        return out

    cleaned = clean(schema)
    if not isinstance(cleaned, dict):
        cleaned = {"type": "object"}
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


# Backward-compatible internal aliases for integrations/tests that imported the
# former vendor-specific helper names.
_normalize_chatgpt_structured = _normalize_remote_structured
_claude_schema = _remote_compatible_schema


def _tool_to_mcp(tool, profile: str | None = None) -> dict[str, Any]:
    title = getattr(tool, "title", None) or _humanize_tool_name(tool.name)
    output_schema = tool_output_schema(tool.name)
    if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
        # Some remote-MCP builds are strict and can fail while
        # validating the initial tool list. For ChatGPT, keep initialize/tools/list
        # anonymous but mark every tool call as OAuth-protected so the client can
        # link/authenticate at call time instead of failing the connector handshake.
        item = {
            "name": _mcp_safe_tool_name(tool.name),
            "description": _provider_tool_description(tool),
            "inputSchema": _remote_compatible_schema(tool_input_schema(tool.name)),
            "annotations": {
                "readOnlyHint": bool(tool.read_only),
                "destructiveHint": bool(tool.destructive or tool.requires_approval),
                "idempotentHint": bool(tool.idempotent),
                "openWorldHint": bool(tool.open_world),
            },
        }
        if output_schema:
            # Provider planning needs the stable control envelope, not every
            # tool-specific diagnostic branch. Full structured data is still
            # returned at call time and large results remain artifact-readable.
            compact_output = {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "state_class": {"type": "string"},
                    "outcome": {"type": "string"},
                    "message": {"type": "string"},
                    "job_id": {"type": ["string", "null"]},
                    "terminal": {"type": "boolean"},
                    "success_allowed": {"type": "boolean"},
                    "completion_required": {"type": "boolean"},
                    "next_call": {"type": ["object", "null"]},
                    "input_required": {"type": "boolean"},
                    "input_request": {"type": ["object", "null"]},
                    "error": {"type": ["string", "object", "null"]},
                },
                "required": list(_CONTROL_REQUIRED_FIELDS),
                "additionalProperties": True,
            }
            preserve = {
                "workflow.odoo_code_change": {"handoff", "coding_session", "coding_workspace", "inspection_artifact", "write_generation"},
                "workflow.odoo_debug_and_fix": {
                    "debug_session", "deploy_confirmed", "reproduction_passed", "expected_result_verified",
                    "claim_status", "truth_gate", "diagnosis", "handoff",
                },
                "skills.read": {"name", "description", "content"},
                "artifacts.read": {
                    "artifact_id", "kind", "source_tool", "created_at", "bytes",
                    "offset", "returned_chars", "next_offset", "text",
                },
            }.get(tool.name, set())
            source_properties = output_schema.get("properties") or {}
            for key in preserve:
                if key in source_properties:
                    property_schema = _remote_compatible_schema(source_properties[key])
                    if source_properties[key].get("type") != "object":
                        property_schema.pop("properties", None)
                    compact_output["properties"][key] = property_schema
            item["outputSchema"] = compact_output
        return item
    item = {
        "name": _mcp_safe_tool_name(tool.name),
        "title": title,
        "description": (
            f"Internal tool: {tool.name}. "
            f"{tool.description}"
        ),
        "inputSchema": tool_input_schema(tool.name),
        "annotations": {
            "title": title,
            "readOnlyHint": bool(tool.read_only),
            "destructiveHint": bool(tool.destructive or tool.requires_approval),
            "idempotentHint": bool(tool.idempotent),
            "openWorldHint": bool(tool.open_world),
        },
    }
    if output_schema:
        item["outputSchema"] = output_schema
    return item





@router.post("")
async def mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Lightweight JSON-RPC MCP-compatible endpoint.

    It exposes initialize, tools/list, and tools/call over one HTTPS POST route.
    The same service functions are used by REST/OpenAPI and this MCP-style bridge.
    """
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="provider", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="provider", request=request, minimal=minimal)


async def _handle_shell(
    payload: dict[str, Any],
    profile: str | None = None,
    request: Request | None = None,
    minimal: bool = False,
) -> dict[str, Any] | None:
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _shell_response(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    # Clients must refresh the tool manifest after a connector
                    # update/redeploy. Advertising listChanged avoids stale
                    # manifests that produce "Resource not found" for newly
                    # registered composite workflows.
                    "tools": {"listChanged": True},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "odoo-mcp-agent-connector", "version": "1.8.0"},
                "instructions": load_client_check_system_prompt(),
                "prompt_source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
                "next_prompt_source_after_client_validation": "MCP_SYSTEM_PROMPT.md",
            },
        )
    if method == "prompts/list":
        return _shell_response(
            request_id,
            {
                "prompts": [
                    {
                        "name": "client_check_system_prompt",
                        "title": "Client Check Gate Prompt",
                        "description": "Pre-MCP client selection and validation gate from CLIENT_CHECK_SYSTEM_PROMPT.md.",
                        "arguments": [],
                    },
                    {
                        "name": "mcp_system_prompt",
                        "title": "Original MCP System Prompt",
                        "description": "Exact content from MCP_SYSTEM_PROMPT.md. Load after client.check validation passes.",
                        "arguments": [],
                    },
                ]
            },
        )
    if method == "prompts/get":
        name = params.get("name")
        if name in {"client_check_system_prompt", "client_check", "CLIENT_CHECK_SYSTEM_PROMPT"}:
            prompt = load_client_check_system_prompt()
            return _shell_response(
                request_id,
                {
                    "description": "Pre-MCP client selection and validation gate from CLIENT_CHECK_SYSTEM_PROMPT.md.",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": prompt},
                        }
                    ],
                    "source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
                },
            )
        if name not in {"mcp_system_prompt", "system_prompt", "MCP_SYSTEM_PROMPT"}:
            return _shell_response(request_id, error={"code": -32602, "message": f"Unknown prompt: {name}"})
        prompt = load_mcp_system_prompt()
        return _shell_response(
            request_id,
            {
                "description": "Exact content from MCP_SYSTEM_PROMPT.md. Load after client.check validation passes.",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt},
                    }
                ],
                "source": "MCP_SYSTEM_PROMPT.md",
            },
        )
    if method == "tools/list":
        return _shell_response(request_id, {"tools": _cached_tools_list(profile, minimal=minimal)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return _shell_response(request_id, error={"code": -32602, "message": "tools/call requires params.name"})
        internal_name = _mcp_name_to_internal(name)
        try:
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and internal_name not in _PROVIDER_TOOL_NAMES:
                guard_structured = McpOperationGuardService().tool_profile_block(internal_name, arguments if isinstance(arguments, dict) else {})
                guard_structured = _provider_safe_next_calls(_ensure_provider_control_envelope(guard_structured))
                text = json.dumps(guard_structured, ensure_ascii=False, default=str)
                return _shell_response(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": guard_structured,
                        "isError": False,
                    },
                )
            arguments = _arguments_for_profile(internal_name, arguments, profile)
            if settings.mcp_operation_guard_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                arguments = op_guard.canonicalize_arguments(internal_name, arguments)
                guarded = op_guard.before_tool_call(internal_name, arguments)
                if guarded:
                    guarded = _provider_safe_next_calls(_ensure_provider_control_envelope(guarded))
                    text = json.dumps(guarded, ensure_ascii=False, default=str)
                    return _shell_response(
                        request_id,
                        {
                            "content": [{"type": "text", "text": text}],
                            "structuredContent": guarded,
                            "isError": False,
                        },
                    )
            if settings.workflow_watchdog_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                result = await WorkflowWatchdogService().run_tool_call(
                    internal_name,
                    lambda: _dispatch(internal_name, arguments),
                )
            else:
                result = await _dispatch(internal_name, arguments)
            structured = _to_structured_content(result, tool_name=internal_name)
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                structured = _provider_safe_next_calls(structured)
            if settings.mcp_operation_guard_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                op_guard.after_tool_call(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
                structured = op_guard.annotate_result(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                if internal_name.startswith("ws."):
                    # Workspace tools return small, bounded, self-describing results: no lifecycle envelope.
                    structured = _provider_safe_next_calls(structured if isinstance(structured, dict) else {"result": structured})
                else:
                    structured = _ensure_provider_control_envelope(structured)
                    if internal_name != "artifacts.read":
                        structured = _normalize_remote_structured(structured, internal_name)
                    structured = _provider_safe_next_calls(_ensure_provider_control_envelope(structured))
                    structured = _ensure_callable_continuations(structured)
            text = _render_result_text(structured, internal_name)
            # Context-bloat guard: in long sessions, oversized tool outputs (large diffs,
            # logs, file reads) compound across many calls and slow down/derail the model.
            # Compatibility profiles also receive bounded structuredContent with artifact refs.
            text = _cap_text(text, max_chars=(30000 if internal_name.startswith("ws.") else 12000) if profile in {"provider", "claude", "chatgpt", "grok", "qwen"} else 24000)
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            )
        except ValidationError as exc:
            message = format_tool_validation_error(exc)
            structured_error = _provider_safe_next_calls(
                _ensure_provider_control_envelope(
                    {
                        "status": "failed",
                        "error": message,
                        "error_type": "tool_input_invalid",
                        "error_code": "tool_input_invalid",
                        "retryable": False,
                        # Give the model one concrete, callable recovery step
                        # instead of a raw Pydantic dump it cannot act on.
                        "next_call": {
                            "tool": "workflow.agent_instructions",
                            "arguments": {},
                        },
                    },
                    is_error=True,
                )
            )
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": structured_error,
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            structured_error = _provider_safe_next_calls(
                _ensure_provider_control_envelope(
                    {
                        "status": "failed",
                        "error": message,
                        "error_code": type(exc).__name__,
                        "retryable": None,
                    },
                    is_error=True,
                )
            )
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": structured_error,
                    "isError": True,
                },
            )
    return _shell_response(request_id, error={"code": -32601, "message": f"Unknown method: {method}"})


@claude_router.get("/mcp")
def claude_mcp_health() -> dict[str, Any]:
    return {"status": "ok", "endpoint": "unified-provider-mcp", "transport": "http-post-json-rpc", "tools_profile": "unified-provider-safe"}


@claude_router.post("/mcp")
async def claude_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="claude", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="claude", request=request, minimal=minimal)


@chatgpt_router.get("/mcp")
def chatgpt_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "unified-provider-mcp",
        "transport": "http-post-json-rpc",
        "tool_names": "strict_regex_safe",
    }


@chatgpt_router.get("/tools-debug")
def chatgpt_tools_debug() -> dict[str, Any]:
    tools_payload = _force_remote_tool_list([_tool_to_mcp(tool, profile="chatgpt") for tool in _tools_for_profile("chatgpt")])
    bad = [tool.get("name") for tool in tools_payload if not _REMOTE_TOOL_NAME_RE.match(str(tool.get("name", "")))]
    return {
        "status": "ok" if not bad else "bad_names",
        "count": len(tools_payload),
        "bad_names": bad,
        "first_names": [tool.get("name") for tool in tools_payload[:20]],
    }


@chatgpt_router.post("/mcp")
async def chatgpt_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="chatgpt", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="chatgpt", request=request, minimal=minimal)


@grok_router.get("/mcp")
def grok_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "grok-mcp",
        "transport": "http-post-json-rpc",
        "tool_names": "strict_regex_safe",
        "profile": "unified-provider-safe",
    }


@grok_router.get("/tools-debug")
def grok_tools_debug() -> dict[str, Any]:
    # Grok gets a narrow, safe profile per xAI Remote MCP guidance: fewer tools, no low-level deploy bypasses.
    tools_payload = _force_remote_tool_list([_tool_to_mcp(tool, profile="grok") for tool in _tools_for_profile("grok")])
    bad = [tool.get("name") for tool in tools_payload if not _REMOTE_TOOL_NAME_RE.match(str(tool.get("name", "")))]
    return {
        "status": "ok" if not bad else "bad_names",
        "count": len(tools_payload),
        "bad_names": bad,
        "first_names": [tool.get("name") for tool in tools_payload[:20]],
    }


@grok_router.post("/mcp")
async def grok_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    # Keep Grok on the same hybrid-safe contract as ChatGPT: short bounded waits,
    # OAuth/tool-call auth, strict tool names, and success_allowed=false until terminal proof.
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="grok", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="grok", request=request, minimal=minimal)


@router.get("/system-prompt")
def mcp_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@router.get("/client-check-prompt")
def mcp_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@claude_router.get("/system-prompt")
def claude_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@chatgpt_router.get("/system-prompt")
def chatgpt_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@chatgpt_router.get("/client-check-prompt")
def chatgpt_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@grok_router.get("/system-prompt")
def grok_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@grok_router.get("/client-check-prompt")
def grok_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@claude_router.get("/client-check-prompt")
def claude_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


def _validate_qwen_url_token(token: str | None) -> None:
    """Validate a token embedded in the Qwen-specific URL fallback.

    Some Qwen app builds POST to the MCP endpoint without forwarding custom
    Authorization headers. The normal /mcp endpoint remains the preferred,
    standards-aligned endpoint. This fallback lets a private HTTPS deployment
    place the existing ODOO_AGENT_API_TOKEN in the URL path or query string:

        /qwen/mcp/<ODOO_AGENT_API_TOKEN>
        /qwen/mcp?access_token=<ODOO_AGENT_API_TOKEN>

    The token is still checked with constant-time comparison and OAuth access
    tokens are also accepted for compatibility.
    """
    import secrets

    value = (token or "").strip()
    if value and QwenTokenService().validate(value):
        return
    if value and secrets.compare_digest(value, settings.api_token):
        return
    if value and OAuthStore().validate_access_token(value):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid Qwen MCP access token")


@qwen_router.post("/token", dependencies=[AuthDependency])
def qwen_short_token() -> dict[str, Any]:
    """Mint a short-lived Qwen URL token after normal bearer/OAuth auth.

    Use this instead of putting the permanent ODOO_AGENT_API_TOKEN in Qwen app
    URLs. Configure Qwen with the returned qwen_mcp_url and Auth=None.
    """
    return QwenTokenService().mint()


@qwen_router.get("/system-prompt/{access_token}")
def qwen_system_prompt_path(access_token: str) -> dict[str, str]:
    _validate_qwen_url_token(access_token)
    return shared_prompt_payload()


@qwen_router.get("/system-prompt")
def qwen_system_prompt_query(access_token: str = Query(default="")) -> dict[str, str]:
    _validate_qwen_url_token(access_token)
    return shared_prompt_payload()


@qwen_router.get("/mcp/{access_token}")
def qwen_mcp_path_health(access_token: str) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.get("/mcp")
def qwen_mcp_query_health(access_token: str = Query(default="")) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.post("/mcp/{access_token}")
async def qwen_mcp_path_shell(access_token: str, request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="qwen", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="qwen", request=request, minimal=minimal)


@qwen_router.post("/mcp")
async def qwen_mcp_query_shell(request: Request, payload: Any = Body(...), access_token: str = Query(default="")) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    minimal = _requested_minimal(request)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="qwen", request=request, minimal=minimal)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="qwen", request=request, minimal=minimal)
