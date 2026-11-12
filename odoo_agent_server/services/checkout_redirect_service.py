"""Route code-change requests to the stateless ws_* lane when no checkout exists.

Hosts without a persistent disk never have /workspace/repos/<client>. The checkout
lane (workflow_odoo_code_change) then failed in milliseconds with "Project path not
found" and the calling agent stopped to explain instead of working. This builds the
same-turn redirect instead: an action_required result whose next_call is ws_open, plus
the task carried over so no context is lost. Framework-free so it is unit-testable.
"""
from __future__ import annotations

from typing import Any, Iterable

WS_FLOW = (
    "ws_open (next_call) -> ws_grep/ws_outline/ws_read for exact context -> "
    "ws_edit with small search/replace edits until commit_ready -> ws_commit"
)


def build_ws_redirect(
    *,
    client_name: str,
    module_name: str,
    create_module: bool = False,
    task_description: str = "",
    search_patterns: Iterable[str] = (),
    target_files: Iterable[str] = (),
    commit_message: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"client_name": client_name, "module_name": module_name}
    if create_module:
        arguments["create"] = True
    carry = {
        "task_description": task_description,
        "search_patterns": [str(p) for p in search_patterns if str(p).strip()],
        "target_files": [str(p) for p in target_files if str(p).strip()],
        "commit_message": commit_message or "",
    }
    return {
        "status": "use_ws_lane",
        "state_class": "action_required",
        "message": (
            "No local checkout exists on this host, so the checkout lane cannot run. "
            "Nothing failed and nothing changed. Run next_call now, in this same turn, then "
            f"follow: {WS_FLOW}. Do not stop to explain or ask; just do the work."
        ),
        "protected_work_started": False,
        "success_allowed": False,
        "completion_required": True,
        "client_name": client_name,
        "module_name": module_name,
        "next_call": {"tool": "ws.open", "arguments": arguments},
        "carry_over": carry,
    }


MISSING_CHECKOUT_MARKERS = ("project path not found", "existing checkout not found")

# Workflows that need a local checkout only to read/edit source, which the ws_* lane covers.
CHECKOUT_REDIRECT_TOOLS = frozenset({
    "workflow.odoo_code_change",
    "workflow.module_analyze",
    "workflow.odoo_debug_and_fix",
    "workflow.frontend_visual_implement",
})


def is_missing_checkout(detail: object) -> bool:
    text = str(detail or "").lower()
    return any(marker in text for marker in MISSING_CHECKOUT_MARKERS)


def redirect_from_arguments(arguments: dict[str, Any], client_name: str) -> dict[str, Any] | None:
    """Build the redirect for any workflow's raw arguments; None when ws_open could not be called."""
    module_name = str(arguments.get("module_name") or "").strip()
    if not module_name or not client_name:
        return None
    task = (
        arguments.get("task_description")
        or arguments.get("issue_description")
        or arguments.get("design_goal")
        or ""
    )
    return build_ws_redirect(
        client_name=client_name,
        module_name=module_name,
        create_module=bool(arguments.get("create_module")),
        task_description=str(task),
        search_patterns=arguments.get("search_patterns") or [],
        target_files=arguments.get("target_files") or [],
        commit_message=arguments.get("commit_message"),
    )
