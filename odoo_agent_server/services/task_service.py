"""Bounded persistent task ledger over existing execution/deployment workflows.

No model loop or privileged executor: task.run delegates to the normal dispatcher.
A dispatched mutation is never replayed from this ledger after an uncertain exit.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from fastapi import HTTPException
from pydantic import ValidationError

from ..config import get_odoo_clients, settings
from .client_admin_service import ClientAdminService
from .client_settings_store import atomic_json

POLL_TOOLS = {"execution.workspace_status", "execution.process_poll", "execution.status", "jobs.get", "odoo.mutation_audit"}
RUN_TOOLS = POLL_TOOLS | {
    "client.check",
    "execution.workspace_open",
    "execution.workspace_sync",
    "execution.files",
    "execution.task_plan",
    "execution.process_start",
    "execution.process_cancel",
    "execution.publish",
    "workflow.odoo_code_change",
    "workflow.odoo_debug_and_fix",
    "workflow.resume_job",
    "workflow.resume_continuation",
    "workflow.safe_deploy_module",
    "workflow.verify_deployed_module",
    "workflow.verify_against_core",
    "workflow.scope_advice",
    "workflow.find_core_file",
    "workflow.frontend_visual_implement",
    "workflow.module_test",
    "workflow.module_install_and_check",
    "odoo.read_group",
    "odoo.list_models",
    "workflow.module_analyze",
    "repo.read",
    "repo.search",
    "repo.list",
    "git.diff",
    "odoo.search_read",
    "odoo.fields_get",
    "odoo.query",
    "odoosh.query",
    "odoosh.error_pack",
    "odoo.mutation_preview",
    "odoo.record_create",
    "odoo.record_write",
    "odoo.record_action",
    "odoo.flow_test",
    "browser.check",
    "skills.read",
    "skills.list",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def scrub(value):
    text = str(value)
    secrets = [settings.api_token, settings.github_token, settings.agent_api_key]
    for client in get_odoo_clients():
        secrets.extend(client.get(k) for k in ("token", "odoo_password", "odoo_api_key", "odoosh_ssh_private_key_b64"))
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[hidden]")
    text = re.sub(r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+", r"\1=[hidden]", text)
    return re.sub(r"(https?://)[^/\s@]+@", r"\1[hidden]@", text)[:2000]


class TaskService:
    def __init__(self):
        self.root = settings.state_path / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise HTTPException(404, "Task not found")
        return self.root / (task_id + ".json")

    @contextmanager
    def lock(self, task_id):
        path = self.path(task_id).with_suffix(".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409, "Task is already processing a step; poll task_status.") from None
            yield
        finally:
            os.close(fd)

    def read(self, task_id):
        if (settings.state_path / 'task-tombstones' / self.path(task_id).name).exists():
            raise HTTPException(410, 'Task history expired under the retention policy.')
        try:
            return json.loads(self.path(task_id).read_text())
        except FileNotFoundError:
            raise HTTPException(404, "Task not found") from None
        except (ValueError, OSError):
            raise HTTPException(503, "Task checkpoint is unreadable; inspect preserved state.") from None

    def save(self, task):
        task["updated_at"] = now()
        atomic_json(self.path(task["task_id"]), task)

    def open(self, request):
        client = ClientAdminService()._client(request.client_name)
        task_id = hashlib.sha256((str(client["name"]).casefold() + "\0" + request.request_id).encode()).hexdigest()[:32]
        contract = request.model_dump()
        contract["client_name"] = client["name"]
        fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        with self.lock(task_id):
            if self.path(task_id).exists() or (settings.state_path / 'task-tombstones' / self.path(task_id).name).exists():
                task = self.read(task_id)
                if task["fingerprint"] != fingerprint:
                    raise HTTPException(409, "request_id already belongs to another task contract")
            else:
                if sum(1 for _ in self.root.glob("*.json")) >= settings.job_max_count:
                    raise HTTPException(429, "Task retention capacity reached; archive completed task checkpoints before creating more.")
                task = {
                    "task_id": task_id,
                    "fingerprint": fingerprint,
                    "client_revision": ClientAdminService.revision(client),
                    "client_name": client["name"],
                    "description": scrub(request.description),
                    "target": request.target,
                    "module_name": request.module_name,
                    "branch": client.get("branch"),
                    "created_at": now(),
                    "updated_at": now(),
                    "phase": "inspect",
                    "steps": {},
                    "events": [],
                    "refs": {},
                    "claims": {},
                }
                self.save(task)
        return self.public(task)

    def current(self, task):
        client = ClientAdminService()._client(task["client_name"])
        return ClientAdminService.revision(client) == task["client_revision"]

    def public(self, task):
        current = self.current(task)
        claims = task.get("claims", {}) if current else {}
        target = task["target"]
        complete = bool(claims.get({"coding": "coding_verified", "push": "push_verified", "deploy": "deployment_verified", "functional": "functional_verified"}[target]))
        if target == "deploy" and task.get("debug_required"):
            complete = complete and claims.get("debug_verified") is True
        return {k: v for k, v in task.items() if k not in {"fingerprint", "client_revision", "steps"}} | {
            "status": "stale_configuration" if not current else "completed" if complete else task.get("phase", "inspect"),
            "success_allowed": complete,
            "completion_required": not complete,
            "configuration_current": current,
            "claims": claims,
            "next_call": {"tool": "task_resume", "arguments": {"task_id": task["task_id"]}} if not complete and current else None,
            "step_count": len(task["steps"]),
        }

    def envelope(self, task, tool_result=None, **extra):
        snapshot = self.public(task)
        out = {
            "task_id": task["task_id"],
            "task": snapshot,
            "status": snapshot["status"],
            "success_allowed": snapshot["success_allowed"],
            "completion_required": not snapshot["success_allowed"],
            "deploy_confirmed": snapshot["claims"].get("deployment_verified") is True,
            "next_call": None,
            **extra,
        }
        if tool_result is not None:
            out["tool_result"] = tool_result
        if snapshot["success_allowed"]:
            out.update(state_class="succeeded", terminal=True, completion_required=False)
            return out
        if not snapshot["configuration_current"]:
            out.update(state_class="blocked", terminal=True, completion_required=False)
            return out
        body = tool_result if isinstance(tool_result, dict) else {}
        if isinstance(body.get("summary"), dict):
            body = {**body, **body["summary"]}

        def wrap(call):
            if not isinstance(call, dict):
                return None
            from ..routers.tools import internal_tool_name

            name = internal_tool_name(call.get("tool", ""))
            if name == "task.deploy":
                return call
            if name not in RUN_TOOLS:
                return None
            arguments = dict(call.get("arguments") or {})
            if name == "execution.publish" and task["target"] == "coding":
                arguments["push"] = False
            digest = hashlib.sha256(json.dumps({"tool": name, "arguments": arguments}, sort_keys=True).encode()).hexdigest()[:24]
            return {"tool": "task_run", "arguments": {"task_id": task["task_id"], "step_id": "next-" + digest, "tool": name.replace(".", "_"), "arguments": arguments}}

        input_request = body.get("input_request")
        if isinstance(input_request, dict):
            out.update(state_class="input_required", input_required=True, input_request={**input_request, "resume_call": wrap(input_request.get("resume_call"))}, terminal=False)
            return out
        argument_request = body.get("argument_request")
        if isinstance(argument_request, dict):
            wrapped = wrap(argument_request)
            if wrapped:
                wrapped["required_generated_arguments"] = ["arguments." + key for key in argument_request.get("required_generated_arguments", [])]
                wrapped["required_generated_arguments"].append("step_id")
                wrapped["instruction"] = argument_request.get("instruction", "Generate the missing technical arguments inside task_run.arguments.")
                out.update(state_class="action_required", argument_request=wrapped, terminal=False)
                return out
        raw_call = body.get("next_call")
        if isinstance(raw_call, dict) and "clarification_answer" in raw_call.get("required_generated_arguments", []):
            gate = body.get("clarification_gate") or {}
            out.update(
                state_class="input_required",
                terminal=False,
                input_required=True,
                input_request={"kind": "clarification", "prompt": gate.get("question") or body.get("message") or "Resolve the material clarification.", "resume_call": wrap(raw_call)},
            )
            return out
        if isinstance(raw_call, dict) and raw_call.get("required_generated_arguments"):
            wrapped = wrap(raw_call)
            if wrapped:
                wrapped["required_generated_arguments"] = ["arguments." + key for key in raw_call["required_generated_arguments"]]
                wrapped["required_generated_arguments"].append("step_id")
                wrapped["instruction"] = "Generate the missing technical arguments inside task_run.arguments, preserving the returned contract."
                out.update(state_class="action_required", argument_request=wrapped, terminal=False)
                return out
        next_call = wrap(raw_call)
        if next_call:
            out.update(state_class="action_required", next_call=next_call, terminal=False)
            return out
        if body.get("input_required") or body.get("status") in {"awaiting_approval", "approval_required"}:
            out.update(
                state_class="input_required",
                terminal=False,
                input_required=True,
                input_request={"kind": "user_input", "prompt": str(body.get("message") or "Resolve the explicit underlying operation gate."), "resume_call": next_call},
            )
            return out
        if body.get("status") in {"failed", "error", "reconciliation_required", "needs_review", "configuration_required"} or task.get("phase") in {
            "reconciliation_required",
            "deployment_sha_mismatch",
        }:
            out.update(state_class="blocked", terminal=True, completion_required=False)
            return out
        if task["refs"].get("pending_job_ids") or task["refs"].get("pending_operation_ids"):
            out.update(state_class="running", terminal=False, next_call={"tool": "task_resume", "arguments": {"task_id": task["task_id"]}})
            return out
        if snapshot["claims"].get("push_verified") and task["target"] == "deploy":
            if not task.get("module_name"):
                out.update(
                    state_class="action_required",
                    terminal=False,
                    argument_request={
                        "tool": "task_deploy",
                        "arguments": {"task_id": task["task_id"], "step_id": "deploy-" + task["task_id"]},
                        "required_generated_arguments": ["module_name"],
                        "instruction": "Select the actual changed addon from the verified commit; supply module_root_rel for nested addons.",
                    },
                )
                return out
            out.update(
                state_class="action_required",
                terminal=False,
                next_call={"tool": "task_deploy", "arguments": {"task_id": task["task_id"], "step_id": "deploy-" + task.get("commit_sha", task["task_id"])[:24]}},
            )
            return out
        if task["refs"].get("pending_job_ids") or task["refs"].get("pending_operation_ids"):
            out.update(state_class="running", terminal=False, next_call={"tool": "task_resume", "arguments": {"task_id": task["task_id"]}})
            return out
        if not task["refs"].get("workspace_id") and task["target"] != "functional":
            out.update(
                state_class="action_required",
                terminal=False,
                next_call={
                    "tool": "task_run",
                    "arguments": {
                        "task_id": task["task_id"],
                        "step_id": "open-workspace",
                        "tool": "execution_workspace_open",
                        "arguments": {"client_name": task["client_name"], "task_description": task["description"], "request_id": "task-" + task["task_id"]},
                    },
                },
            )
            return out
        out.update(
            state_class="action_required",
            terminal=False,
            argument_request={
                "tool": "task_run",
                "arguments": {"task_id": task["task_id"]},
                "required_generated_arguments": ["step_id", "tool", "arguments"],
                "instruction": "Choose the next task-scoped inspection/edit/test or functional preview from current source and acceptance criteria. Do not repeat completed mutations.",
            },
        )
        return out

    def list(self, client_name=None, limit=30):
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[: settings.job_max_count]:
            task = self.read(path.stem)
            if client_name and task["client_name"].casefold() != client_name.casefold():
                continue
            try:
                rows.append(self.public(task))
            except HTTPException:
                rows.append({"task_id": task["task_id"], "client_name": task["client_name"], "status": "client_removed", "success_allowed": False})
            if len(rows) >= limit:
                break
        return {"items": rows}

    def bind(self, task, name, args):
        if name not in RUN_TOOLS:
            raise HTTPException(422, "Tool is not supported by the task coordinator; use its normal MCP tool directly.")
        from ..routers.tools import _TOOL_INPUT_MODELS
        from ..routers.mcp import _PROVIDER_TOOL_NAMES

        if name not in _PROVIDER_TOOL_NAMES:
            raise HTTPException(422, "Task coordinator cannot expose a provider-hidden tool")
        model = _TOOL_INPUT_MODELS.get(name)
        if model is None:
            raise HTTPException(422, "Tool is not registered")
        args = dict(args)
        client = ClientAdminService()._client(task["client_name"])
        if "client_name" in model.model_fields and not args.get("client_name"):
            args["client_name"] = task["client_name"]
        if "project_path" in model.model_fields and model.model_fields["project_path"].is_required() and not args.get("project_path"):
            args["project_path"] = client["path"]
        if "workspace_id" in model.model_fields and not args.get("workspace_id") and task["refs"].get("workspace_id"):
            args["workspace_id"] = task["refs"]["workspace_id"]
        for key in ("process_id", "operation_id", "job_id"):
            if key in model.model_fields and not args.get(key):
                candidates = task["refs"].get("pending_" + key + "s") or task["refs"].get(key + "s", [])
                if len(candidates) == 1:
                    args[key] = candidates[0]
        if name == "execution.workspace_open":
            args.setdefault("task_description", task["description"])
        if name == "execution.publish":
            args.setdefault("push", task["target"] != "coding")
            args.setdefault("message", task["description"])
        if name == "execution.process_start" and args.get("check_name") and not args.get("argv") and task["refs"].get("workspace_id"):
            from .execution_service import ExecutionService

            checks = ExecutionService().workspace(task["refs"]["workspace_id"]).get("checks", [])
            check = next((item for item in checks if item["name"] == args["check_name"]), None)
            if check:
                for key in ("argv", "cwd", "timeout_seconds"):
                    if key in check:
                        args.setdefault(key, check[key])
        # Validate types before scope comparisons such as casefold/path handling.
        # Keep the original supplied/default distinction for authorization below.
        model.model_validate(args)
        if name == "jobs.get":
            args["wait_for_result"] = False
        if name.startswith("workflow.") and "wait_for_result" in model.model_fields:
            args["wait_for_result"] = False
        if name == "execution.publish" and task["target"] == "coding" and args.get("push", True):
            raise HTTPException(409, "Coding-only task cannot push; use push=false or open an explicitly requested push/deploy task")
        if name in {"workflow.odoo_code_change", "workflow.odoo_debug_and_fix", "workflow.frontend_visual_implement"} and task["target"] != "deploy":
            if args.get("auto_deploy") is True:
                raise HTTPException(409, "This task did not request deployment")
            args["auto_deploy"] = False
        if name == "workflow.resume_job":
            if task["target"] != "deploy":
                raise HTTPException(409, "This task did not request deployment")
            if args.get("job_id"):
                from .store_service import JsonStore

                original = JsonStore().get_job(args["job_id"]).request
                if task.get("commit_sha") and original.get("last_sha") != task["commit_sha"] and original.get("execution_workspace_id") != task["refs"].get("workspace_id"):
                    raise HTTPException(409, "Existing deployment belongs to a different task commit")
        if name == "workflow.resume_continuation":
            from .continuation_token_service import ContinuationTokenService, ContinuationTokenError

            try:
                contract = ContinuationTokenService().decode_code_change(args.get("resume_token", ""))
            except ContinuationTokenError:
                raise HTTPException(409, "Invalid continuation token") from None
            if (
                str(contract.get("client_name") or "").casefold() != task["client_name"].casefold()
                or contract.get("module_name") != task.get("module_name")
                or (contract.get("auto_deploy") and task["target"] != "deploy")
            ):
                raise HTTPException(409, "Continuation contract differs from this task")
        if name == "odoo.mutation_preview":
            nested = dict(args.get("arguments") or {})
            if nested.get("client_name") and nested["client_name"].casefold() != task["client_name"].casefold():
                raise HTTPException(409, "Preview client differs from task client")
            nested["client_name"] = task["client_name"]
            args["arguments"] = nested
        if "client_name" in model.model_fields:
            supplied = args.get("client_name")
            if supplied and supplied.casefold() != task["client_name"].casefold():
                raise HTTPException(409, "Task cannot change client")
            args["client_name"] = task["client_name"]
        if task.get("module_name") and args.get("module_name") and args["module_name"] != task["module_name"]:
            raise HTTPException(409, "Task module differs from its pinned module")
        if "project_path" in model.model_fields and args.get("project_path"):
            if Path(args["project_path"]).resolve() != Path(client["path"]).resolve():
                raise HTTPException(409, "Task project must match the configured client checkout")
        if name in {"jobs.get", "workflow.resume_job"} and not args.get("job_id"):
            raise HTTPException(409, "Resume requires the exact task-linked job_id")
        if args.get("branch") and args["branch"] != task["branch"]:
            raise HTTPException(409, "Task branch differs from its pinned client branch")
        if name == "execution.workspace_open" and task["refs"].get("workspace_id"):
            expected = hashlib.sha256(("workspace:" + str(args.get("request_id", ""))).encode()).hexdigest()[:32]
            if expected != task["refs"]["workspace_id"]:
                raise HTTPException(409, "Task already owns a workspace; preserve it and use a follow-up task for another checkout")
        if args.get("workspace_id") and args["workspace_id"] != task["refs"].get("workspace_id"):
            raise HTTPException(409, "Workspace is not linked to this task")
        for key in ("process_id", "operation_id", "job_id"):
            if args.get(key) and args[key] not in task["refs"].get(key + "s", []):
                raise HTTPException(409, key + " is not linked to this task")
        return model.model_validate(args).model_dump(mode="json")

    async def guided(self, task_id, dispatch):
        """One server-selected step; never invent code, tests or approval intent."""
        import asyncio
        from ..task_schemas import TaskRun

        state = await self.resume(task_id, dispatch)
        if state.get("success_allowed") or state.get("input_required") or state.get("state_class") == "blocked" or state.get("uncertain_steps"):
            return state
        call = state.get("next_call") or {}
        if call.get("tool") == "task_run":
            return await self.run(TaskRun.model_validate(call["arguments"]), dispatch)
        argument = state.get("argument_request") or {}
        proposed = argument.get("arguments") or {}
        if proposed.get("tool") == "execution_task_plan":
            from .verification_planner import suggest_checks
            from .execution_service import ExecutionService

            task = self.read(task_id)
            wid = task["refs"].get("workspace_id")
            if wid:
                proposal = await asyncio.to_thread(suggest_checks, wid)
                state["test_plan"] = proposal
                if not proposal["missing_layers"] and not proposal["blockers"] and proposal["suggested_checks"]:
                    ws = ExecutionService().workspace(wid)
                    old = ws.get("checks", [])
                    names = {check["name"] for check in old}
                    checks = old + [check for check in proposal["suggested_checks"] if check["name"] not in names]
                    return await self.run(TaskRun(task_id=task_id, tool="execution_task_plan", arguments={
                        "checks": checks, "required_layers": sorted(set(ws.get("required_layers", [])) | set(proposal["required_layers"])),
                        "max_repair_attempts": ws.get("max_repair_attempts", 3),
                    }), dispatch)
        state["guidance"] = "Call task_run(task_id) for server-selected workflow steps. Supply tool/arguments only for source inspection, code edits, unresolved test requirements or conflict resolutions. Follow input_request for business decisions/approval."
        return state

    async def run(self, request, dispatch):
        from ..routers.tools import internal_tool_name, _TOOL_DESCRIPTORS

        if not request.tool:
            if request.arguments:
                raise HTTPException(422, "Supply tool with arguments, or task_id alone to follow the server next step")
            return await self.guided(request.task_id, dispatch)
        with self.lock(request.task_id):
            task = self.read(request.task_id)
            if not self.current(task):
                raise HTTPException(409, "Client configuration changed; open a new task and revalidate preserved work.")
            name = internal_tool_name(request.tool)
            from ..routers.tools import _TOOL_INPUT_MODELS

            step_id = request.step_id or "auto-" + hashlib.sha256(json.dumps({"tool": name, "arguments": request.arguments}, sort_keys=True).encode()).hexdigest()[:24]
            supplied = dict(request.arguments)
            model = _TOOL_INPUT_MODELS.get(name)
            if model and "request_id" in model.model_fields:
                supplied.setdefault("request_id", "task-" + request.task_id[:12] + "-" + step_id[:32])
            try:
                args = self.bind(task, name, supplied)
            except ValidationError as exc:
                errors = [{"field": ".".join(map(str, error["loc"])), "type": error["type"], "message": error["msg"][:200]}
                          for error in exc.errors(include_input=False, include_context=False)[:12]]
                result = self.envelope(task)
                result.update(status="invalid_arguments", dispatched=False, success_allowed=False,
                              completion_required=True, state_class="action_required", terminal=False, next_call=None,
                              tool_result={"argument_errors": errors}, argument_request={
                                  "tool": "task_run", "arguments": {"task_id": task["task_id"], "tool": name.replace(".", "_")},
                                  "required_generated_arguments": ["arguments." + error["field"] for error in errors],
                                  "instruction": "Correct these exact fields; nothing was dispatched. Client/workspace/request IDs are server-bound; step_id is optional.",
                              })
                return result
            fingerprint = hashlib.sha256(json.dumps({"tool": name, "args": args}, sort_keys=True).encode()).hexdigest()
            readonly = next(t.read_only for t in _TOOL_DESCRIPTORS if t.name == name) or (name == "execution.files" and args.get("operation") in {"list", "read", "search", "diff"})
            previous = task["steps"].get(step_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "step_id already belongs to different arguments")
                if not readonly:
                    return self.envelope(task, idempotent_replay=True, step=previous, next_action="Call task_resume to reconcile existing server jobs; do not replay a dispatched mutation.")
            if not previous and len(task["steps"]) >= 200:
                raise HTTPException(409, "Task step budget reached; inspect evidence and open a follow-up task.")
            step = {"tool": name, "fingerprint": fingerprint, "status": "dispatched", "started_at": now(), "read_only": readonly}
            task["steps"][step_id] = step
            if name in {"execution.workspace_open", "execution.process_start", "execution.publish", "execution.workspace_sync"}:
                kind, key, ref = (
                    ("workspace", args.get("request_id"), "workspace_id")
                    if name == "execution.workspace_open"
                    else ("process", args.get("request_id"), "process_ids")
                    if name == "execution.process_start"
                    else ("operation", f"sync:{args.get('request_id')}", "operation_ids")
                    if name == "execution.workspace_sync"
                    else ("operation", f"publish:{args.get('request_id')}:{args.get('approval_id') or 'pending'}", "operation_ids")
                )
                rid = hashlib.sha256((kind + ":" + key).encode()).hexdigest()[:32]
                step["record_id"] = rid
                if ref == "workspace_id":
                    task["refs"][ref] = rid
                else:
                    task["refs"][ref] = list(dict.fromkeys([*task["refs"].get(ref, []), rid]))[-50:]
            if name in {"odoo.record_create", "odoo.record_write", "odoo.record_action", "odoo.flow_test"}:
                task["refs"]["mutation_request_id"] = args.get("request_id")
            task["phase"] = "running"
            if not readonly:
                task["claims"] = {}
            self.save(task)  # Before the side effect. A lost response cannot cause replay.
            try:
                result = await dispatch(name, args)
                result = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                if isinstance(result, dict) and result.get("status") == "awaiting_approval" and result.get("approval_id") and "input_request" not in result:
                    result["input_request"] = {
                        "kind": "approval",
                        "approval_id": result["approval_id"],
                        "requires_explicit_user_consent": True,
                        "prompt": result.get("next_action") or "Approve this exact protected action.",
                        "resume_call": {"tool": name, "arguments": {**args, "approval_id": result["approval_id"]}},
                    }
                self.observe(task, name, result, args)
                step.update(status="observed", finished_at=now())
                self.save(task)
                return self.envelope(task, result)
            except BaseException as exc:
                step.update(status="read_failed" if readonly else "reconciliation_required", finished_at=now(), error=scrub(getattr(exc, "detail", type(exc).__name__)))
                task["phase"] = step["status"]
                self.save(task)
                raise

    def observe(self, task, name, result, args=None):
        result = result if isinstance(result, dict) else {}
        args = args or {}
        original = result
        if isinstance(result.get("summary"), dict):
            result = {**result, **result["summary"]}
        if isinstance(result.get("result"), dict):
            result = {**result, **result["result"]}
        # Job model exposes computed safety gates. Keep those authoritative.
        for gate in ("deploy_confirmed", "success_allowed"):
            if gate in original:
                result[gate] = original[gate]
        if name == "jobs.get" and original.get("id"):
            result["job_id"] = original["id"]
        debug = result.get("debug_session") if isinstance(result.get("debug_session"), dict) else {}
        if debug:
            task["debug_required"] = True
            result = {**result, "debug_session_id": debug.get("id"), "job_id": result.get("job_id") or debug.get("job_id"), "diagnosis": result.get("diagnosis") or debug.get("diagnosis")}
        if not task.get("module_name") and isinstance(result.get("module_name"), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["module_name"]):
            task["module_name"] = result["module_name"]
        for key in ("workspace_id", "job_id", "process_id", "operation_id", "debug_session_id", "coding_session_id"):
            value = result.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", value):
                if key == "operation_id" and not name.startswith("execution."):
                    continue
                if key == "workspace_id":
                    task["refs"][key] = value
                else:
                    task["refs"][key + "s"] = list(dict.fromkeys([*task["refs"].get(key + "s", []), value]))[-50:]
        if result.get("job_id") and (name == "workflow.safe_deploy_module" or (name == "jobs.get" and original.get("job_type") == "safe_deploy_module")):
            task["refs"]["deploy_job_id"] = result["job_id"]
        active = {"queued", "running", "already_running", "already_in_progress", "resumable_existing_job"}
        for key, poll_name in (("job_id", "jobs.get"), ("operation_id", "execution.status")):
            value = result.get(key)
            if key == "operation_id" and not name.startswith("execution."):
                continue
            if not value:
                continue
            pending = set(task["refs"].get("pending_" + key + "s", []))
            if str(result.get("status") or "") in active:
                pending.add(value)
            else:
                pending.discard(value)
            task["refs"]["pending_" + key + "s"] = sorted(pending)
        claims = task.setdefault("claims", {})
        # Never infer deployment or business correctness from an exit code or push.
        if name == "execution.status":
            # Publish receipts describe the operation, not the current tree.
            # Only a fresh workspace_status may grant push verification.
            task["phase"] = str(result.get("status") or "publish")
        elif name == "execution.workspace_status":
            claims["coding_verified"] = result.get("verification", {}).get("ready") is True
            if not claims["coding_verified"]:
                claims["deployment_verified"] = False
            claims["push_verified"] = result.get("pushed") is True and result.get("success_allowed") is True
            task["phase"] = result.get("phase", "inspect")
        elif name in {"workflow.odoo_code_change", "workflow.odoo_debug_and_fix", "workflow.frontend_visual_implement"}:
            claims["deployment_verified"] = result.get("deploy_confirmed") is True and result.get("success_allowed") is True
            truth = result.get("truth_gate") or debug.get("truth_gate") or {}
            if debug:
                claims["debug_verified"] = truth.get("verified_fixed") is True
            if result.get("status") == "ready_to_deploy":
                claims["coding_verified"] = (
                    result.get("surgical_scope_verified") is True
                    and result.get("contract_status") == "fulfilled"
                    and result.get("verify_against_core", {}).get("push_blocked") is False
                    and result.get("qa", {}).get("status") in {"passed", "warning"}
                )
            task["phase"] = str(result.get("phase") or result.get("status") or "inspect")
        elif name in {"workflow.safe_deploy_module", "workflow.resume_job", "jobs.get", "workflow.verify_deployed_module"}:
            claims["deployment_verified"] = result.get("deploy_confirmed") is True and result.get("success_allowed") is True
            task["phase"] = str(result.get("status") or "deploy")
        elif name in {"odoo.record_create", "odoo.record_write", "odoo.record_action", "odoo.flow_test"}:
            claims["functional_verified"] = result.get("verified") is True and result.get("success_allowed") is True
            task["phase"] = str(result.get("status") or "functional")
            task["refs"]["mutation_request_id"] = args.get("request_id")
        else:
            task["phase"] = str(result.get("phase") or result.get("status") or "inspect")[:80]
        if name == "execution.workspace_status" and result.get("pushed") is True and result.get("success_allowed") is True:
            published = result.get("commit") or result.get("published_commit")
            if isinstance(published, str) and re.fullmatch("[0-9a-f]{40}", published):
                task["published_commit_sha"] = published
        expected_sha = task.get("published_commit_sha")
        observed_sha = result.get("sha") or result.get("commit") or result.get("published_commit")
        if (
            name in {"jobs.get", "workflow.safe_deploy_module", "workflow.resume_job", "workflow.odoo_code_change", "workflow.odoo_debug_and_fix", "workflow.frontend_visual_implement"}
            and claims.get("deployment_verified")
            and expected_sha
            and observed_sha != expected_sha
        ):
            claims["deployment_verified"] = False
            task["phase"] = "deployment_sha_mismatch"
        event = {
            "at": now(),
            "tool": name,
            "status": str(result.get("status") or task["phase"])[:80],
            "verified": result.get("verified") is True,
            "success_allowed": result.get("success_allowed") is True,
        }
        if result.get("diagnosis"):
            diagnosis = result["diagnosis"]
            if isinstance(diagnosis, dict):
                task["diagnosis"] = {k: scrub(diagnosis[k]) for k in ("origin", "failure_category", "crux", "next_strategy") if k in diagnosis}
        for key in ("commit", "commit_sha", "published_commit", "sha"):
            if isinstance(result.get(key), str) and re.fullmatch("[0-9a-f]{40}", result[key]):
                if not expected_sha or result[key] == expected_sha:
                    task["commit_sha"] = result[key]
        task["events"] = [*task["events"], event][-100:]

    async def resume(self, task_id, dispatch):
        with self.lock(task_id):
            task = self.read(task_id)
            if not self.current(task):
                return self.public(task)
            refs = task["refs"]
            uncertain_deploy = any(s["tool"] == "task.deploy" and s["status"] in {"dispatched", "reconciliation_required"} for s in task["steps"].values())
            if uncertain_deploy and not refs.get("deploy_job_id") and refs.get("workspace_id"):
                from .store_service import JsonStore

                for job in JsonStore().list_jobs(limit=settings.job_max_count):
                    request = job.request or {}
                    if request.get("execution_workspace_id") == refs["workspace_id"] and request.get("client_name") == task["client_name"] and request.get("module_name") == task.get("module_name"):
                        refs["deploy_job_id"] = job.id
                        refs["job_ids"] = list(dict.fromkeys([*refs.get("job_ids", []), job.id]))[-50:]
                        for step in task["steps"].values():
                            if step["tool"] == "task.deploy" and step["status"] in {"dispatched", "reconciliation_required"}:
                                step.update(status="reconciled", finished_at=now())
                        break
            calls = []
            for process_id in refs.get("process_ids", [])[-1:]:
                calls.append(("execution.process_poll", {"process_id": process_id, "max_bytes": 2000}))
            if refs.get("mutation_request_id"):
                calls.append(("odoo.mutation_audit", {"client_name": task["client_name"], "request_id": refs["mutation_request_id"], "limit": 1}))
            for job_id in refs.get("job_ids", [])[-3:]:
                calls.append(("jobs.get", {"job_id": job_id, "wait_for_result": False}))
            for operation_id in refs.get("operation_ids", [])[-1:]:
                calls.append(("execution.status", {"operation_id": operation_id}))
            if refs.get("workspace_id"):
                calls.append(("execution.workspace_status", {"workspace_id": refs["workspace_id"]}))
            observations = []
            for name, args in calls:
                try:
                    result = await dispatch(name, args)
                    result = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                    self.observe(task, name, result, args)
                    for step in task["steps"].values():
                        expected = {"execution.workspace_open": "workspace_id", "execution.process_start": "process_id", "execution.publish": "operation_id", "execution.workspace_sync": "operation_id"}.get(step["tool"])
                        if expected and step.get("record_id") == args.get(expected) and step["status"] in {"dispatched", "reconciliation_required"}:
                            step.update(status="reconciled", finished_at=now())
                    observations.append({"tool": name, "result": result})
                except HTTPException as exc:
                    observations.append({"tool": name, "error": scrub(exc.detail)})
            uncertain = [key for key, s in task["steps"].items() if s["status"] in {"dispatched", "reconciliation_required"}]
            if (uncertain and not calls) or (uncertain_deploy and not refs.get("deploy_job_id")):
                task["phase"] = "reconciliation_required"
            latest = observations[-1].get("result") if observations else None
            if refs.get("deploy_job_id"):
                for observation in observations:
                    value = observation.get("result") or {}
                    if value.get("id") == refs["deploy_job_id"] or value.get("job_id") == refs["deploy_job_id"]:
                        latest = value
            pending_jobs = set(refs.get("pending_job_ids", []))
            pending_ops = set(refs.get("pending_operation_ids", []))
            for observation in observations:
                value = observation.get("result") or {}
                if value.get("id") in pending_jobs or value.get("job_id") in pending_jobs or value.get("operation_id") in pending_ops:
                    latest = value
            if refs.get("debug_session_ids"):
                from .store_service import JsonStore
                from ..routers.tools import _debug_public_session, _debug_next_call

                try:
                    session = JsonStore().get_debug_session(refs["debug_session_ids"][-1], task["client_name"])
                    public = _debug_public_session(session)
                    latest = {
                        "status": public.get("phase"),
                        "debug_session": public,
                        "truth_gate": public["truth_gate"],
                        "deploy_confirmed": public.get("deploy_confirmed"),
                        "success_allowed": public["truth_gate"].get("verified_fixed") is True,
                        "next_call": _debug_next_call(session, action="poll") if session.get("phase") not in {"blocked", "completed"} else None,
                    }
                    self.observe(task, "workflow.odoo_debug_and_fix", latest)
                except HTTPException:
                    pass
            self.save(task)
            return self.envelope(
                task,
                latest,
                observations=observations,
                uncertain_steps=uncertain,
                next_action="Continue the returned existing workflow next_call. Uncertain mutations require read-back reconciliation; no mutation was replayed."
                if calls
                else "Inspect source and choose the next scoped tool through task_run; no mutation was replayed.",
            )

    async def deploy(self, request, dispatch):
        import asyncio
        from ..schemas import SafeDeployModuleRequest
        from .client_resolver_service import ClientResolverService
        from .repo_service import RepoService
        from .execution_service import ExecutionService
        from ..routers.workflow import safe_deploy_module
        from .published_workspace_service import validate_published_workspace

        with self.lock(request.task_id):
            task = self.read(request.task_id)
            from .server_control_service import assert_no_maintenance

            assert_no_maintenance()
            if not self.current(task) or task["target"] != "deploy":
                raise HTTPException(409, "Deployment needs a current target=deploy task")
            if task.get("module_name") and request.module_name and request.module_name != task["module_name"]:
                raise HTTPException(409, "Deployment module differs from pinned task module")
            if not task.get("module_name"):
                if not request.module_name:
                    raise HTTPException(422, "Select the changed module for deployment")
                task["module_name"] = request.module_name
            wid = task["refs"].get("workspace_id")
            if not wid:
                raise HTTPException(409, "Task has no linked execution workspace")
            fingerprint = hashlib.sha256(json.dumps(request.model_dump(exclude={"approval_id"}), sort_keys=True).encode()).hexdigest()
            previous = task["steps"].get(request.step_id)
            if previous and previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "step_id already belongs to another deployment")
            if previous and not (previous.get("status") == "awaiting_approval" and request.approval_id == previous.get("approval_id")):
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "step_id already belongs to another deployment")
                return self.envelope(task, idempotent_replay=True, next_action="Poll task_resume; existing deployment was not repeated.")
            status = await asyncio.to_thread(ExecutionService().status, wid)
            if not (status.get("pushed") and status.get("success_allowed")):
                raise HTTPException(409, "Publish and verify the exact task commit before deployment")
            client = ClientResolverService().resolve_client(task["client_name"], require_complete=False)
            await asyncio.to_thread(RepoService().sync_configured_checkout, client)
            payload = SafeDeployModuleRequest(
                client_name=client.name,
                project_path=client.path,
                module_name=task["module_name"],
                module_root_rel=request.module_root_rel,
                branch=client.branch,
                commit_message=task["description"][:300],
                execution_workspace_id=wid,
                verification_assertion=request.verification_assertion,
                approval_id=request.approval_id,
                wait_for_result=False,
                github_push_fallback=False,
            )
            evidence = await asyncio.to_thread(validate_published_workspace, payload)
            payload.paths = evidence["paths"]
            from .deploy_impact_service import classify_deploy_impact
            from .execution_service import git

            diff = await asyncio.to_thread(git, ["show", "--format=", "--no-ext-diff", "--no-textconv", evidence["sha"], "--", *(evidence["paths"])], client.path)
            impact = classify_deploy_impact(
                project_path=client.path, module_name=task["module_name"], changed_paths=evidence["paths"], git_diff=diff, deploy_mode="always_upgrade" if len(diff) >= 32000 else "auto"
            )
            payload.upgrade_module = impact.upgrade_required
            payload.deploy_impact = impact.impact
            payload.upgrade_reasons = list(impact.reasons)
            step = {"tool": "task.deploy", "fingerprint": fingerprint, "status": "dispatched", "started_at": now()}
            task["steps"][request.step_id] = step
            task["phase"] = "deploy"
            task["claims"].pop("deployment_verified", None)
            self.save(task)
            try:
                report = await safe_deploy_module(payload)
                result = report.model_dump(mode="json")
                self.observe(task, "workflow.safe_deploy_module", result)
                step.update(status="observed", finished_at=now())
                self.save(task)
                return self.envelope(task, result)
            except BaseException as exc:
                detail = getattr(exc, "detail", None)
                if isinstance(exc, HTTPException) and isinstance(detail, dict) and detail.get("approval_required"):
                    step.update(status="awaiting_approval", approval_id=detail.get("approval_id"))
                    task["phase"] = "awaiting_approval"
                    self.save(task)
                    resume = {"tool": "task_deploy", "arguments": {**request.model_dump(), "approval_id": detail.get("approval_id")}}
                    return self.envelope(
                        task,
                        {
                            "status": "awaiting_approval",
                            "input_request": {
                                "kind": "approval",
                                "approval_id": detail.get("approval_id"),
                                "requires_explicit_user_consent": True,
                                "prompt": detail.get("message") or "Approve the exact deployment request.",
                                "resume_call": resume,
                            },
                        },
                    )
                step.update(status="reconciliation_required", error=scrub(getattr(exc, "detail", type(exc).__name__)))
                task["phase"] = "reconciliation_required"
                self.save(task)
                raise

    def knowledge(self, client_name, refresh=False):
        client = ClientAdminService()._client(client_name)
        detail = ClientAdminService().detail(client["name"])
        from .client_knowledge_service import ClientKnowledgeService

        return ClientKnowledgeService().read(client, detail, refresh)
