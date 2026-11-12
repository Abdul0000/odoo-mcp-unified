from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from .store_service import JsonStore


class WorkflowProgressService:
    """Small pollable progress ledger for synchronous composite workflows."""

    @staticmethod
    def operation_id(payload: Any) -> str:
        explicit = str(
            getattr(payload, "operation_id", None)
            or getattr(payload, "coding_session_id", None)
            or ""
        ).strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", explicit):
            return explicit
        identity = "\0".join((
            str(getattr(payload, "client_name", None) or ""),
            str(getattr(payload, "project_path", None) or ""),
            str(getattr(payload, "module_name", None) or ""),
            str(getattr(payload, "task_description", None) or ""),
        ))
        return "workflow-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def start(self, operation_id: str, *, client_name: str | None, module_name: str) -> dict[str, Any]:
        now = self._now()
        return JsonStore().update_workflow_progress(operation_id, {
            "workflow": "workflow_odoo_code_change",
            "status": "running",
            "current_phase": "queued",
            "phase_started_at": now,
            "started_at": now,
            "last_progress_at": now,
            "client_name": client_name,
            "module_name": module_name,
            "phases_ms": {},
            "cache_hit": None,
            "queue_wait_ms": 0,
            "terminal": False,
        })

    def phase(self, operation_id: str, phase: str, **extra: Any) -> dict[str, Any]:
        now = self._now()
        return JsonStore().update_workflow_progress(operation_id, {
            "status": "running",
            "current_phase": phase,
            "phase_started_at": now,
            "last_progress_at": now,
            "terminal": False,
            **self._safe_extra(extra),
        })

    def phase_done(
        self,
        operation_id: str,
        phase: str,
        elapsed_ms: int,
        **extra: Any,
    ) -> dict[str, Any]:
        current = JsonStore().get_workflow_progress(operation_id) or {}
        timings = dict(current.get("phases_ms") or {})
        timings[phase] = max(0, int(elapsed_ms))
        return JsonStore().update_workflow_progress(operation_id, {
            "phases_ms": timings,
            "last_progress_at": self._now(),
            **self._safe_extra(extra),
        })

    def worker_heartbeat(self, operation_id: str, owner_instance_id: str) -> dict[str, Any]:
        """Renew worker ownership without pretending that the workflow advanced."""
        return JsonStore().update_workflow_progress(operation_id, {
            "worker_owner_instance_id": owner_instance_id,
            "worker_heartbeat_at": self._now(),
        })

    def finish(
        self,
        operation_id: str,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        changes: dict[str, Any] = {
            "status": str(status or "completed")[:100],
            "current_phase": "completed",
            "last_progress_at": now,
            "finished_at": now,
            "terminal": True,
        }
        if metrics:
            changes["total_ms"] = max(0, int(metrics.get("total_ms") or 0))
            changes["queue_wait_ms"] = max(0, int(metrics.get("queue_wait_ms") or 0))
            if isinstance(metrics.get("phases_ms"), dict):
                changes["phases_ms"] = dict(metrics["phases_ms"])
        return JsonStore().update_workflow_progress(operation_id, changes)

    def fail(self, operation_id: str, exc: BaseException) -> dict[str, Any]:
        now = self._now()
        return JsonStore().update_workflow_progress(operation_id, {
            "status": "failed",
            "current_phase": "failed",
            "error_type": type(exc).__name__,
            "last_progress_at": now,
            "finished_at": now,
            "terminal": True,
        })

    @staticmethod
    def summary(limit: int = 5) -> dict[str, Any]:
        rows = JsonStore().list_workflow_progress(limit=max(1, min(int(limit), 20)))
        active = [row for row in rows if not bool(row.get("terminal"))]
        return {
            "active_count": len(active),
            "active": active,
            "recent": rows,
        }

    @staticmethod
    def _safe_extra(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "client_name",
            "module_name",
            "cache_hit",
            "workspace_cache_hit",
            "analysis_cache_hit",
            "queue_wait_ms",
            "write_handoff_mode",
        }
        return {key: value[key] for key in allowed if key in value}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
