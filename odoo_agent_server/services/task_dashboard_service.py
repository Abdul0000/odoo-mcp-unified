"""Sanitized dashboard projections of task, job and diagnosis evidence."""

from ..config import get_odoo_clients, settings
from .client_admin_service import ClientAdminService
from .store_service import JsonStore
from .task_service import TaskService, scrub


class TaskDashboardService:
    def snapshot(self, client_name=None):
        if client_name:
            client_name = ClientAdminService()._client(client_name)["name"]
        store = JsonStore()
        tasks = TaskService().list(client_name, 50)["items"]
        deployments = []
        for job in store.list_jobs(limit=200):
            if not any(word in job.job_type for word in ("deploy", "module_install", "module_update")):
                continue
            request = job.request or {}
            profile = request.get("client")
            name = request.get("client_name") or (profile.get("name") if isinstance(profile, dict) else None)
            if client_name and str(name).casefold() != client_name.casefold():
                continue
            result = job.result if isinstance(job.result, dict) else {}
            deployments.append(
                {
                    "job_id": job.id,
                    "client_name": name,
                    "module_name": request.get("module_name"),
                    "status": job.status.value,
                    "phase": job.steps[-1].name if job.steps else job.status.value,
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                    "deploy_confirmed": job.deploy_confirmed,
                    "module_upgraded": result.get("module_upgraded") if isinstance(result.get("module_upgraded"), bool) else None,
                    "source_deployed": result.get("source_deployed") if isinstance(result.get("source_deployed"), bool) else None,
                    "sha": result.get("sha") or request.get("last_sha"),
                    "message": scrub(result.get("message") or result.get("error") or result.get("failed_step") or ""),
                    "steps": [{"name": s.name, "status": s.status, "duration_ms": s.duration_ms} for s in job.steps[-30:]],
                }
            )
        diagnostics = []
        names = [client_name] if client_name else [c["name"] for c in get_odoo_clients()]
        for name in names:
            for row in store._read_list(store._debug_sessions_file_for_client(name))[-20:]:
                diagnosis = row.get("diagnosis") or {}
                from ..routers.tools import _debug_truth_gate

                truth = _debug_truth_gate(row)
                diagnostics.append(
                    {
                        "id": row.get("id"),
                        "client_name": name,
                        "module_name": row.get("module_name"),
                        "phase": row.get("phase"),
                        "updated_at": row.get("updated_at"),
                        "diagnosis": {k: scrub(diagnosis.get(k, "")) for k in ("origin", "failure_category", "crux", "next_strategy")},
                        "truth_gate": {"verified_fixed": truth.get("verified_fixed") is True},
                        "evidence": [
                            {"id": e.get("id"), "kind": e.get("kind"), "trusted": e.get("trusted") is True, "passed": e.get("passed"), "summary": scrub(e.get("summary", ""))[:800]}
                            for e in row.get("evidence_ledger", [])[-10:]
                        ],
                    }
                )
        workspaces = []
        if settings.execution_enabled:
            from .execution_store import ExecutionStore

            for row in ExecutionStore().recent("workspace", limit=30):
                if client_name and str(row.get("client_name", "")).casefold() != client_name.casefold():
                    continue
                workspaces.append(
                    {
                        "workspace_id": row["id"],
                        "client_name": row.get("client_name"),
                        "branch": row.get("branch"),
                        "status": row["status"],
                        "phase": row.get("phase"),
                        "commit": row.get("published_commit"),
                        "pushed": row.get("pushed") is True,
                        "updated_at": row.get("updated_at"),
                    }
                )
        return {
            "tasks": tasks,
            "deployments": deployments[:50],
            "diagnostics": sorted(diagnostics, key=lambda r: r.get("updated_at") or "", reverse=True)[:50],
            "workspaces": workspaces,
            "source": "persistent_server_records",
            "live_verification_performed": False,
        }

    def evidence(self, task_id):
        from fastapi import HTTPException
        from ..execution_schemas import WorkspaceFiles
        from .execution_service import ExecutionService

        task = TaskService().read(task_id)
        wid = task["refs"].get("workspace_id")
        result = {"task_id": task_id, "checks": [], "commands": [], "diff": "", "diff_available": False}
        if not wid:
            return {**result, "message": "No execution workspace is linked; see the task deployment/diagnostic evidence."}
        svc = ExecutionService()
        record = svc.store.get(wid, "workspace")
        if record.get("client_name") != task["client_name"]:
            raise HTTPException(409, "Workspace client differs from task")
        status = svc.status(wid)
        for check in record.get("checks", []):
            proof = record.get("check_results", {}).get(check["name"], {})
            result["checks"].append(
                {"name": check["name"], "layer": check.get("layer"), "status": proof.get("status", "not_run"), "command": scrub(" ".join(check.get("argv", []))), "tree": proof.get("tree")}
            )
        result["verification"] = status.get("verification", {})
        if result["verification"].get("reason") != "command_pending":
            try:
                diff = svc.files(WorkspaceFiles(workspace_id=wid, operation="diff"))
                result.update(diff=scrub(diff["diff"]), diff_available=True, diff_truncated=diff["truncated"] or len(diff["diff"]) > 2000)
            except HTTPException as exc:
                result["diff_message"] = scrub(exc.detail)
        process_ids = list(dict.fromkeys([*task["refs"].get("process_ids", []), *[p.get("process_id") for p in record.get("check_results", {}).values() if p.get("process_id")]]))
        for pid in process_ids[-5:]:
            process = svc.store.get(pid, "process")
            if process["request"]["workspace_id"] != wid:
                continue
            observed = svc.poll(pid, max_bytes=2000)
            result["commands"].append(
                {
                    "process_id": pid,
                    "status": observed["status"],
                    "exit_code": observed.get("exit_code"),
                    "command": scrub(" ".join(process["request"].get("argv", []))),
                    "output": scrub(observed["output"]),
                    "next_offset": observed["next_offset"],
                }
            )
        return result
