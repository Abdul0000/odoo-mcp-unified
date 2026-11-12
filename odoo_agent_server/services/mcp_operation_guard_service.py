from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client_resolver_service import ClientResolverService
from .path_service import resolve_allowed_path
from .store_service import JsonStore


_DISCOVERY_TOOLS = {
    "repo.list_custom_modules",
    "repo.search",
    "workflow.list_module_files",
    "workflow.find_core_file",
    "workflow.find_similar_view",
    "workflow.scope_advice",
    "odoo.module_status",
    "odoosh.read_user_file",
    "odoosh.list_user_files",
    "workflow.verify_deployed_module",
}
_READ_TOOLS = {"repo.read", "repo.read_range", "repo.list", "repo.search"}
_WRITE_TOOLS = {
    "repo.write",
    "repo.write_many_smart",
    "repo.write_many_fast_safe",
    "repo.write_many_atomic",
    "repo.apply_patch",
    "repo.apply_patch_atomic",
}
_VERIFY_TOOLS = {"git.diff", "qa.self_run", "workflow.verify_against_core"}
_DEPLOY_TOOLS = {"workflow.safe_deploy_module"}
_POLL_TOOLS = {"jobs.get", "jobs.list", "workflow.resume_job"}
_ALLOWED_REPEAT_TOOLS = {"jobs.get", "jobs.list", "artifacts.read"}
_FUNCTIONAL_OPERATION_TOOLS = {
    "odoo.record_create",
    "odoo.record_write",
    "odoo.record_action",
    "odoo.flow_test",
    "odoo.mutation_audit",
}
_UNIQUE_ONCE_TOOLS = {"repo.list_custom_modules", "workflow.list_module_files", "workflow.scope_advice", "workflow.find_similar_view"}
_MAX_SEARCH_CALLS = 2
_FRONTEND_SUFFIXES = (".xml", ".css", ".scss", ".js", ".qweb")


class McpOperationGuardService:
    """Small provider-side orchestration guard for Grok remote MCP.

    Grok benefits from a narrow tool profile, but it can still call the same
    discovery/read/status tools repeatedly or jump backward after a write.  This
    guard keeps a lightweight operation state in the existing per-client session
    file and returns deterministic next_action payloads instead of letting the
    agent spiral through duplicate tools.
    """

    def __init__(self, store: JsonStore | None = None) -> None:
        self.store = store or JsonStore()

    # ------------------------------------------------------------------
    # Public helpers used at the MCP boundary
    # ------------------------------------------------------------------

    def canonicalize_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return arguments
        data = dict(arguments)
        project_path = data.get("project_path")
        if not project_path:
            return data
        try:
            project = resolve_allowed_path(str(project_path))
        except Exception:
            return data

        def norm(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return self._normalise_relative_path(project, value)

        if "relative_path" in data:
            data["relative_path"] = norm(data.get("relative_path"))
        if "path" in data and "relative_path" not in data:
            data["path"] = norm(data.get("path"))
        if isinstance(data.get("files"), list):
            files: list[Any] = []
            for item in data.get("files") or []:
                if not isinstance(item, dict):
                    files.append(item)
                    continue
                copy = dict(item)
                if "relative_path" in copy:
                    copy["relative_path"] = norm(copy.get("relative_path"))
                if "path" in copy and "relative_path" not in copy:
                    copy["path"] = norm(copy.get("path"))
                files.append(copy)
            data["files"] = files
        return data

    def before_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if tool_name in _FUNCTIONAL_OPERATION_TOOLS:
            return None
        client_name = self._client_name(arguments)
        state = self._load(client_name)
        guard = state.setdefault("mcp_operation_guard", {})
        op = guard.setdefault("current", self._new_operation(arguments))
        # Session bootstrap/client selection starts a fresh operation context so
        # a completed previous task cannot block the next one.  Also reset when
        # the user clearly moved to a different client/project/module.
        if tool_name in {"workflow.agent_instructions", "workflow.start_session", "client.check"} or self._identity_changed(op, arguments):
            op = self._new_operation(arguments)
            guard["current"] = op
        self._refresh_operation_identity(op, arguments)

        sig = self._signature(tool_name, arguments)
        phase = str(op.get("phase") or "discovery")
        counts = op.get("tool_counts") or {}
        if tool_name in _UNIQUE_ONCE_TOOLS and int(counts.get(tool_name, 0) or 0) >= 1:
            return self._guard_payload(
                status="duplicate_guard",
                message=f"{tool_name} is already satisfied for this operation. Do not repeat it.",
                operation=op,
                next_call=self._next_call(op),
            )
        if tool_name == "repo.search" and int(counts.get(tool_name, 0) or 0) >= _MAX_SEARCH_CALLS:
            return self._guard_payload(
                status="duplicate_guard",
                message="Repository search has already run enough for this operation. Continue with the targeted read/write/verify step instead of searching again.",
                operation=op,
                next_call=self._next_call(op),
            )

        # Terminal state takes precedence over duplicate status: the model should
        # stop and answer instead of doing more work.
        if phase == "final" and tool_name not in {"client.check", "workflow.start_session", "workflow.agent_instructions", "jobs.get"}:
            return self._guard_payload(
                status="final_guard",
                message="This operation is terminal. Stop and answer with verified facts only, or start a new session for a new change.",
                operation=op,
                next_call=self._next_call(op),
            )

        # Once an edit is made, Grok must not jump back into discovery/read loops.
        # It must diff/QA/verify, then safe_deploy, then jobs.get.
        if phase in {"verify", "deploy", "poll"} and tool_name in (_DISCOVERY_TOOLS | _READ_TOOLS):
            return self._guard_payload(
                status="phase_guard",
                message=(
                    f"Operation already reached phase '{phase}'. Do not go back to discovery/read/status tools. "
                    "Continue with git.diff, QA, verify_against_core, safe_deploy_module, or jobs.get as appropriate."
                ),
                operation=op,
                next_call=self._next_call(op),
            )
        if phase == "verify" and tool_name in _WRITE_TOOLS and op.get("written_files") and not op.get("diff_seen"):
            return self._guard_payload(
                status="phase_guard",
                message="A write already occurred. Run git.diff/qa/verify before writing the same operation again.",
                operation=op,
                next_call=self._next_call(op),
            )
        if phase == "deploy" and tool_name not in (_DEPLOY_TOOLS | {"workflow.verify_against_core", "qa.self_run", "git.diff"}):
            return self._guard_payload(
                status="phase_guard",
                message="Code verification is complete. Next required step is workflow.safe_deploy_module, not more edits or discovery.",
                operation=op,
                next_call=self._next_call(op),
            )
        if phase == "poll" and tool_name not in _POLL_TOOLS:
            return self._guard_payload(
                status="phase_guard",
                message="Safe deploy job is already active. Poll jobs.get until terminal passed/failed/warning; do not run other tools.",
                operation=op,
                next_call=self._next_call(op),
            )
        # Same-tool/same-arguments duplicate guard. Do not block polling.
        if tool_name not in _ALLOWED_REPEAT_TOOLS and op.get("last_signature") == sig:
            return self._guard_payload(
                status="duplicate_guard",
                message="This exact tool call was already performed in this operation. Do not repeat it; continue with the next workflow phase.",
                operation=op,
                next_call=self._next_call(op),
            )

        # Enforce the v15 dual-read path for every write. The repository's own
        # pre-write gate still verifies exact mandatory reads, but Grok should be
        # redirected before it starts write loops.
        if tool_name in _WRITE_TOOLS and not op.get("scope_advice_done"):
            return self._guard_payload(
                status="dual_read_required",
                message=(
                    "Before writing, read the custom module files, call workflow.scope_advice(max_level=L2), "
                    "then read mandatory related source from /home/odoo/src/odoo/addons first with enterprise fallback only if needed."
                ),
                operation=op,
                next_call=self._next_call(op),
            )

        op["last_signature"] = sig
        op["last_tool"] = tool_name
        op["last_arguments_hash"] = sig.rsplit(":", 1)[-1]
        op["updated_at"] = self._now()
        self._save(state, client_name)
        return None

    def after_tool_call(self, tool_name: str, arguments: dict[str, Any], structured: dict[str, Any]) -> None:
        if tool_name in _FUNCTIONAL_OPERATION_TOOLS:
            return
        client_name = self._client_name(arguments)
        state = self._load(client_name)
        guard = state.setdefault("mcp_operation_guard", {})
        op = guard.setdefault("current", self._new_operation(arguments))
        self._refresh_operation_identity(op, arguments)
        now = self._now()

        op.setdefault("history", []).append({"tool": tool_name, "at": now})
        op["history"] = op.get("history", [])[-40:]
        counts = op.setdefault("tool_counts", {})
        counts[tool_name] = int(counts.get(tool_name, 0) or 0) + 1

        if tool_name in _READ_TOOLS:
            op.setdefault("read_tools", []).append(tool_name)
            op["phase"] = op.get("phase") or "discovery"
        if tool_name == "workflow.scope_advice":
            op["scope_advice_done"] = True
            op["phase"] = "dual_read"
        if tool_name in _WRITE_TOOLS:
            op["phase"] = "verify"
            op.setdefault("written_files", []).extend(self._changed_paths(arguments))
        elif tool_name == "git.diff":
            op["diff_seen"] = True
            op["phase"] = "verify"
        elif tool_name == "qa.self_run":
            op["qa_seen"] = True
            op["phase"] = "verify"
        elif tool_name == "workflow.verify_against_core":
            op["core_verified"] = True
            op["phase"] = "deploy"
        elif tool_name == "workflow.safe_deploy_module":
            op["phase"] = "poll"
            job_id = self._find_job_id(structured)
            if job_id:
                op["job_id"] = job_id
        elif tool_name == "jobs.get":
            job_id = self._find_job_id(structured) or arguments.get("job_id")
            if job_id:
                op["job_id"] = job_id
            status_value = str(structured.get("status") or structured.get("state") or "").lower()
            # JobRecord payloads often use status=passed/failed/warning/running.
            if status_value in {"passed", "failed", "warning", "cancelled"}:
                op["phase"] = "final"
                op["terminal_status"] = status_value
            else:
                op["phase"] = "poll"

        op["updated_at"] = now
        self._save(state, client_name)

    def annotate_result(self, tool_name: str, arguments: dict[str, Any], structured: dict[str, Any]) -> dict[str, Any]:
        """Add Grok-visible guardrails to terminal results.

        This does not change deploy execution. It prevents a provider from
        turning source deployment into an unverified visual/UI claim.
        """
        if not isinstance(structured, dict):
            return structured
        if tool_name in _FUNCTIONAL_OPERATION_TOOLS:
            return structured
        op = self._load(self._client_name(arguments)).get("mcp_operation_guard", {}).get("current", {})
        out = dict(structured)
        status_value = str(out.get("status") or out.get("state") or "").lower()
        result = out.get("result")
        if isinstance(result, dict) and not status_value:
            status_value = str(result.get("status") or result.get("state") or "").lower()
        if tool_name in {"workflow.safe_deploy_module", "jobs.get"}:
            if status_value in {"running", "queued", "pending", "active"}:
                out.setdefault("completion_required", True)
                out.setdefault("success_allowed", False)
                out.setdefault("tool_exit_allowed", False)
                out.setdefault("final_answer_guard", "Do not answer success while this job is running. Call jobs.get until terminal passed/failed/warning.")
            if status_value in {"passed", "warning", "failed", "cancelled"}:
                frontend_changed = self._operation_touched_frontend(op)
                out.setdefault("source_deploy_terminal", True)
                out.setdefault("terminal_status", status_value)
                if frontend_changed:
                    out.setdefault("ui_verified", False)
                    out.setdefault("visible_claim_allowed", False)
                    out.setdefault("final_answer_guard", (
                        "You may report only source/deploy facts. Do NOT say the checkout/page/UI now shows the change "
                        "unless a browser verification tool has confirmed the exact page visually."
                    ))
                    out.setdefault("allowed_final_summary", (
                        "Source change was pushed/deployed/upgraded. Live UI visibility was not browser-verified."
                    ))
                    out.setdefault("forbidden_claims", [
                        "checkout page now shows it",
                        "refresh and you will see it",
                        "visible on the website",
                        "UI confirmed",
                    ])
        return out

    def tool_profile_block(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        op = self._load(self._client_name(arguments)).get("mcp_operation_guard", {}).get("current", {})
        next_call = {
            "tool": "workflow.odoo_code_change",
            "arguments_hint": "Use the unified composite workflow for coding tasks; it owns read, write, verify, safe deploy, and returns jobs.get when ready.",
        }
        if tool_name == "jobs.get":
            next_call = {"tool": "jobs.get", "arguments_hint": "Poll the job_id returned by workflow.odoo_code_change."}
        return self._guard_payload(
            status="tool_profile_blocked",
            message=(
                f"{tool_name} is hidden/blocked for provider MCP endpoints to prevent bypassing the mandatory flow. "
                "Use workflow.odoo_code_change for coding tasks, then jobs.get for polling only."
            ),
            operation=op,
            next_call=next_call,
        )

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _load(self, client_name: str | None) -> dict[str, Any]:
        key = client_name or "__grok_global__"
        state = self.store.load_session_state(client_name=key)
        return state if isinstance(state, dict) and not state.get("requires_client_name") else {}

    def _save(self, state: dict[str, Any], client_name: str | None) -> None:
        key = client_name or "__grok_global__"
        self.store.save_session_state({"mcp_operation_guard": state.get("mcp_operation_guard", {})}, client_name=key)

    def _client_name(self, arguments: dict[str, Any]) -> str | None:
        client_name = arguments.get("client_name") if isinstance(arguments, dict) else None
        if client_name:
            return str(client_name)
        project_path = arguments.get("project_path") if isinstance(arguments, dict) else None
        if project_path:
            try:
                client = ClientResolverService().resolve_client(project_path=str(project_path), require_complete=False)
                if client:
                    return client.name
            except Exception:
                return None
        return None


    def _identity_changed(self, op: dict[str, Any], arguments: dict[str, Any]) -> bool:
        if not op or not isinstance(arguments, dict):
            return False
        if str(op.get("phase") or "") not in {"final", "poll"}:
            return False
        for key in ("client_name", "project_path", "module_name"):
            incoming = arguments.get(key)
            existing = op.get(key)
            if incoming and existing and str(incoming) != str(existing):
                return True
        return False

    def _new_operation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        return {
            "operation_id": hashlib.sha1(f"{now}:{json.dumps(arguments, default=str, sort_keys=True)}".encode()).hexdigest()[:12],
            "phase": "discovery",
            "created_at": now,
            "updated_at": now,
            "client_name": arguments.get("client_name") if isinstance(arguments, dict) else None,
            "project_path": arguments.get("project_path") if isinstance(arguments, dict) else None,
            "module_name": arguments.get("module_name") if isinstance(arguments, dict) else None,
        }

    def _refresh_operation_identity(self, op: dict[str, Any], arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            return
        for key in ("client_name", "project_path", "module_name"):
            if arguments.get(key) and not op.get(key):
                op[key] = arguments.get(key)

    def _signature(self, tool_name: str, arguments: dict[str, Any]) -> str:
        normalized = self.canonicalize_arguments(tool_name, arguments or {})
        text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        return f"{tool_name}:{hashlib.sha256(text.encode()).hexdigest()}"

    def _normalise_relative_path(self, project: Path, raw: str) -> str:
        value = str(raw or "").replace("\\", "/").strip()
        if not value:
            return value
        project_name = project.name
        candidates: list[str] = []
        # Absolute path inside the active checkout.
        try:
            p = Path(value).expanduser()
            if p.is_absolute():
                resolved = p.resolve()
                if resolved == project or project in resolved.parents:
                    return resolved.relative_to(project).as_posix()
        except Exception:
            pass
        # Common hallucinated path aliases.
        stripped = value.lstrip("/")
        if stripped.startswith(f"repos/{project_name}/"):
            candidates.append(stripped[len(f"repos/{project_name}/"):])
        if stripped.startswith(f"workspace/repos/{project_name}/"):
            candidates.append(stripped[len(f"workspace/repos/{project_name}/"):])
        if stripped.startswith(f"{project_name}/"):
            candidates.append(stripped[len(project_name) + 1:])
        candidates.append(stripped)
        for candidate in candidates:
            if not candidate or candidate.startswith("..") or "\x00" in candidate:
                continue
            try:
                resolved = (project / candidate).resolve()
                if resolved == project or project in resolved.parents:
                    return resolved.relative_to(project).as_posix()
            except Exception:
                continue
        return value

    def _changed_paths(self, arguments: dict[str, Any]) -> list[str]:
        if not isinstance(arguments, dict):
            return []
        if isinstance(arguments.get("relative_path"), str):
            return [arguments["relative_path"]]
        paths: list[str] = []
        for item in arguments.get("files") or []:
            if isinstance(item, dict):
                path = item.get("relative_path") or item.get("path")
                if path:
                    paths.append(str(path))
        return paths

    def _operation_touched_frontend(self, op: dict[str, Any]) -> bool:
        paths: list[str] = []
        for key in ("written_files", "target_files"):
            value = op.get(key) if isinstance(op, dict) else None
            if isinstance(value, list):
                paths.extend(str(item).lower() for item in value)
        return any(path.endswith(_FRONTEND_SUFFIXES) or "/views/" in path or "/static/" in path for path in paths)

    def _find_job_id(self, structured: dict[str, Any]) -> str | None:
        for key in ("job_id", "id"):
            value = structured.get(key)
            if isinstance(value, str) and value:
                return value
        next_call = structured.get("next_call")
        if isinstance(next_call, dict):
            args = next_call.get("arguments") or {}
            value = args.get("job_id") if isinstance(args, dict) else None
            if isinstance(value, str) and value:
                return value
        result = structured.get("result")
        if isinstance(result, dict):
            return self._find_job_id(result)
        return None

    def _next_call(self, op: dict[str, Any]) -> dict[str, Any] | None:
        phase = str((op or {}).get("phase") or "discovery")
        if phase == "poll":
            job_id = op.get("job_id")
            return {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
        if phase == "final":
            return None
        return {
            "tool": "workflow.odoo_code_change",
            "arguments_hint": "Continue the current composite code-change workflow; source reads, verification, Git, and deploy are server-owned.",
        }

    def _guard_payload(self, *, status: str, message: str, operation: dict[str, Any], next_call: dict[str, Any] | None) -> dict[str, Any]:
        terminal = str((operation or {}).get("phase") or "") == "final"
        return {
            "status": status,
            "state_class": "blocked" if terminal else "action_required",
            "message": message,
            "phase": (operation or {}).get("phase"),
            "operation_id": (operation or {}).get("operation_id"),
            "completion_required": not terminal,
            "success_allowed": False,
            "tool_exit_allowed": terminal,
            "terminal_action": "stop_and_summarize" if terminal else None,
            "next_call": next_call,
        }

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
