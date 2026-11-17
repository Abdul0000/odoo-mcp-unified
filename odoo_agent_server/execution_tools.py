"""Execution tool registry shared by MCP and the autonomous worker."""
import time

from .execution_schemas import AgentId, AgentStart, ExecutionStatus, ProcessId, ProcessInput, ProcessPoll, ProcessStart, TaskPlan, WorkspaceFiles, WorkspaceId, WorkspaceOpen, WorkspacePublish
from .services.execution_service import ExecutionService


EXECUTION_TOOLS = {
    "execution.status": (ExecutionStatus, "Poll operation_id for a queued publish, or inspect worker capabilities and recent task IDs. Recent phases are not verification proof.", True),
    "execution.workspace_open": (WorkspaceOpen, "Start isolated task initialization; poll workspace_status until ready. Default same branch with fresh fetch; scope_paths narrows checkout to module directories.", False),
    "execution.task_plan": (TaskPlan, "Declare typed verification layers/commands and optional skills. Plans may be strengthened; current-tree checks prove only their declared scope. ORM requires a configured runtime.", False),
    "execution.workspace_status": (WorkspaceId, "Read task branch, changes and requested commit/push completion. Deployment requires separate Odoo evidence.", True),
    "execution.files": (WorkspaceFiles, "List/read/search/diff or edit task files. Batch edits support exact replace/write/rename/delete with prevalidation and rollback. Existing edits require expected_sha256.", False),
    "execution.publish": (WorkspacePublish, "Queue verified commit/push; poll execution_status(operation_id). push=false completes local commit without a follow-up push; push=true confirms remote SHA. Deployment is separate. May require approval.", False),
    "execution.process_start": (ProcessStart, "Queue argv in the task container: no network or credentials. Returns process_id, not a completed result.", False),
    "execution.process_poll": (ProcessPoll, "Read bounded command logs by byte offset and terminal exit status.", True),
    "execution.process_input": (ProcessInput, "Send bounded stdin to a queued/running command; eof closes input.", False),
    "execution.process_cancel": (ProcessId, "Request cancellation of a task command and its container.", False),
    "execution.agent_start": (AgentStart, "Queue a bounded persistent coding task using the worker OPENAI_API_KEY and OPENAI_MODEL. No automatic push/deploy.", False),
    "execution.agent_status": (AgentId, "Read persistent agent progress, token usage and final summary.", True),
    "execution.agent_cancel": (AgentId, "Stop an agent task and request cancellation of its running command.", False),
}
AGENT_TOOL_NAMES = {"execution_task_plan", "execution_workspace_status", "execution_files", "execution_process_start", "execution_process_poll"}


def dispatch_execution(name, args, *, service=None):
    name = name.replace("execution_", "execution.", 1) if name.startswith("execution_") else name
    model = EXECUTION_TOOLS[name][0]
    req = model.model_validate(args)
    svc = service or ExecutionService()
    if name == "execution.status":
        if req.operation_id:
            return svc.operation_status(req.operation_id)
        heartbeat = svc.store.root / "worker-heartbeat"
        age = time.time() - float(heartbeat.read_text()) if heartbeat.exists() else None
        from .config import settings
        return {"status": "ready" if age is not None and age < 120 else "worker_unavailable",
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "mode": "chatgpt_mcp", "requires_model_key": False,
                "max_parallel_commands": settings.execution_max_workers,
                "runtime_capabilities": svc.store._read(svc.store.root / "capabilities.json") or {},
                "recent_tasks": [{"workspace_id": row["id"], "client_name": row.get("client_name"),
                                  "branch": row.get("branch"), "last_known_phase": row.get("phase", row["status"]),
                                  "updated_at": row["updated_at"]} for row in svc.store.recent("workspace")],
                "agent_configured": bool(settings.agent_api_key and settings.agent_model),
                "execution": "Docker; no network; task-only mount; bounded memory, runtime and logs"}
    if name == "execution.workspace_open":
        return svc.open(req, background=True)
    if name == "execution.task_plan":
        return svc.plan(req)
    if name == "execution.workspace_status":
        return svc.status(req.workspace_id)
    if name == "execution.files":
        return svc.files(req)
    if name == "execution.process_start":
        return svc.start_process(req)
    if name == "execution.publish":
        return svc.publish_background(req)
    if name == "execution.process_poll":
        return svc.poll(req.process_id, req.offset, req.max_bytes)
    if name == "execution.process_input":
        svc.store.send_input(req.process_id, req.data, req.eof)
        return {"status": "input_queued", "process_id": req.process_id}
    if name == "execution.process_cancel":
        return svc.cancel(req.process_id, "process")
    if name == "execution.agent_start":
        return svc.start_agent(req)
    if name == "execution.agent_status":
        return svc.agent_status(req.task_id)
    return svc.cancel(req.task_id, "agent")


def execution_function_tools(names):
    return [{"type": "function", "name": name.replace(".", "_"), "description": description,
             "parameters": model.model_json_schema(), "strict": False}
            for name, (model, description, _readonly) in EXECUTION_TOOLS.items() if name.replace(".", "_") in names]
