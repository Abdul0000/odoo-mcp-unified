"""Execution tool registry shared by MCP and the autonomous worker."""
import time

from .execution_schemas import AgentId, AgentStart, ExecutionRequest, ProcessId, ProcessInput, ProcessPoll, ProcessStart, TaskPlan, WorkspaceFiles, WorkspaceId, WorkspaceOpen, WorkspacePublish
from .services.execution_service import ExecutionService


EXECUTION_TOOLS = {
    "execution.status": (ExecutionRequest, "Check worker heartbeat and recent task IDs. Reopen workspace status to resume; recent phases are last-known state, not verification proof.", True),
    "execution.workspace_open": (WorkspaceOpen, "Open isolated task on the configured branch; fetch and fast-forward by default, preserve source files. Retries are idempotent.", False),
    "execution.task_plan": (TaskPlan, "Declare immutable named verification commands and optional skills once per task. Failed checks allow repair; current-tree passes are required to publish.", False),
    "execution.workspace_status": (WorkspaceId, "Read task branch and changed files. Work remains unpublished until explicitly pushed and deployed.", True),
    "execution.files": (WorkspaceFiles, "List/read/search/diff or edit task files. Batch edits support exact replace/write/rename/delete with prevalidation and rollback. Existing edits require expected_sha256.", False),
    "execution.publish": (WorkspacePublish, "Require current-tree check evidence, commit and push to the configured branch, verify remote SHA. Does not deploy Odoo.", False),
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
        heartbeat = svc.store.root / "worker-heartbeat"
        age = time.time() - float(heartbeat.read_text()) if heartbeat.exists() else None
        from .config import settings
        return {"status": "ready" if age is not None and age < 120 else "worker_unavailable",
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "mode": "chatgpt_mcp", "requires_model_key": False,
                "max_parallel_commands": settings.execution_max_workers,
                "recent_tasks": [{"workspace_id": row["id"], "client_name": row.get("client_name"),
                                  "branch": row.get("branch"), "last_known_phase": row.get("phase", row["status"]),
                                  "updated_at": row["updated_at"]} for row in svc.store.recent("workspace")],
                "agent_configured": bool(settings.agent_api_key and settings.agent_model),
                "execution": "Docker; no network; task-only mount; bounded memory, runtime and logs"}
    if name == "execution.workspace_open":
        return svc.open(req)
    if name == "execution.task_plan":
        return svc.plan(req)
    if name == "execution.workspace_status":
        return svc.status(req.workspace_id)
    if name == "execution.files":
        return svc.files(req)
    if name == "execution.process_start":
        return svc.start_process(req)
    if name == "execution.publish":
        return svc.publish(req)
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
