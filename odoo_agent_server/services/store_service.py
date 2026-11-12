from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from threading import Lock
from typing import Any
from uuid import uuid4

try:  # pragma: no cover - platform-specific import
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

from fastapi import HTTPException, status

from ..config import get_odoo_clients, settings
from .job_retention import JobSignal, prune_job_dir
from ..schemas import (
    ApprovalRecord,
    ApprovalStatus,
    AuditEvent,
    JobRecord,
    JobStatus,
    JobStep,
    OdooFailureDiagnosis,
    OdooOperationHandoff,
)


class JsonStore:
    """Small JSON-file state store for approvals, jobs, and audit events.

    This keeps the first production-ready version dependency-light. Replace with
    PostgreSQL when multiple workers/servers are used.
    """

    _lock = Lock()  # in-process thread safety
    _job_signal = JobSignal()  # wakes wait_job the moment a job record changes
    _read_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

    @contextmanager
    def _file_lock(self, path: Path):
        """Cross-process file lock to prevent JSON corruption."""
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lf:
            try:
                if fcntl is not None:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                else:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                else:
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)

    def __init__(self) -> None:
        self.root = settings.state_path
        self.approvals_file = self.root / "approvals.json"
        self.jobs_file = self.root / "jobs.json"
        self.jobs_dir = self.root / "jobs"
        self.audit_file = self.root / "audit.json"
        self.mutations_file = self.root / "functional_mutations.json"
        self.workflow_progress_file = self.root / "workflow_progress.json"
        self.artifacts_dir = self.root / "artifacts"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_jobs()

    def _read_list(self, path: Path) -> list[dict[str, Any]]:
        with self._lock:
            if not path.exists():
                self._read_cache.pop(str(path), None)
                return []
            try:
                stat = path.stat()
                cache_key = str(path)
                cached = self._read_cache.get(cache_key)
                if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                    return [dict(row) for row in cached[2]]
                raw = json.loads(path.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError:
                raw = []
            rows = raw if isinstance(raw, list) else []
            try:
                stat = path.stat()
                self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            except OSError:
                pass
            return [dict(row) for row in rows]

    def _write_list(self, path: Path, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(rows, separators=(",", ":"))
            with self._file_lock(path):
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            try:
                stat = path.stat()
                self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            except OSError:
                self._read_cache.pop(str(path), None)

    def _update_list(self, path: Path, update) -> Any:
        """Atomically read, modify, and replace one JSON list."""
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock(path):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8") or "[]") if path.exists() else []
                except json.JSONDecodeError:
                    raw = []
                rows = raw if isinstance(raw, list) else []
                result = update(rows)
                payload = json.dumps(rows, separators=(",", ":"))
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            stat = path.stat()
            self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            return result

    def update_workflow_progress(self, operation_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge one bounded, secret-free workflow progress record."""
        def update(rows: list[dict[str, Any]]) -> dict[str, Any]:
            index = next(
                (idx for idx, row in enumerate(rows) if row.get("operation_id") == operation_id),
                None,
            )
            record = dict(rows[index]) if index is not None else {"operation_id": operation_id}
            record.update(changes)
            if index is None:
                rows.append(record)
            else:
                rows[index] = record
            rows.sort(key=lambda row: str(row.get("last_progress_at") or ""))
            del rows[:-50]
            return dict(record)

        return self._update_list(self.workflow_progress_file, update)

    def get_workflow_progress(self, operation_id: str) -> dict[str, Any] | None:
        for row in self._read_list(self.workflow_progress_file):
            if row.get("operation_id") == operation_id:
                return row
        return None

    def list_workflow_progress(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._read_list(self.workflow_progress_file)
        rows.sort(key=lambda row: str(row.get("last_progress_at") or ""), reverse=True)
        return rows[:max(1, min(int(limit), 50))]

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        self._update_list(self.approvals_file, lambda rows: rows.append(record.model_dump(mode="json")))
        self.log_audit("approval.request", "pending", record.summary, {"approval_id": record.id, "action": record.action})
        return record

    def list_approvals(self, limit: int = 100) -> list[ApprovalRecord]:
        rows = self._read_list(self.approvals_file)
        return [ApprovalRecord.model_validate(row) for row in rows[-limit:]][::-1]

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        for row in self._read_list(self.approvals_file):
            if row.get("id") == approval_id:
                return ApprovalRecord.model_validate(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    def set_approval_status(self, approval_id: str, target: ApprovalStatus, reason: str | None = None) -> ApprovalRecord:
        def update(rows):
            for index, row in enumerate(rows):
                if row.get("id") == approval_id:
                    record = ApprovalRecord.model_validate(row)
                    record.status = target
                    record.decided_at = datetime.now(timezone.utc).isoformat()
                    record.reason = reason
                    rows[index] = record.model_dump(mode="json")
                    return record
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

        record = self._update_list(self.approvals_file, update)
        self.log_audit("approval." + target.value, target.value, record.summary, {"approval_id": record.id, "action": record.action})
        return record

    def create_job(self, job: JobRecord) -> JobRecord:
        self._write_job(job)
        self._prune_job_files()
        self.log_audit("job.create", job.status.value, job.job_type, {"job_id": job.id})
        return job

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                records.append(JobRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        records.sort(key=lambda item: str(item.created_at or ""), reverse=True)
        return records[:limit]

    def get_job(self, job_id: str) -> JobRecord:
        path = self._job_path(job_id)
        try:
            return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job record is unreadable") from exc

    def wait_job(self, job_id: str, timeout_seconds: int = 600, poll_interval_seconds: float = 1.0) -> JobRecord:
        """Wait for a job to finish without requiring repeated client/tool calls.

        Safe deploy must not be treated as complete until its owning job reaches
        a terminal status. Use short positive waits for MCP/browser clients so
        pages stay responsive. A timeout value of 0 means wait indefinitely and
        should be reserved for CLI/internal callers.
        """
        deadline = None if timeout_seconds <= 0 else time.monotonic() + max(timeout_seconds, 0)
        interval = max(min(poll_interval_seconds, 10.0), 0.25)
        while True:
            version = self._job_signal.version(job_id)
            job = self.get_job(job_id)
            if job.status not in {JobStatus.queued, JobStatus.running}:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                return job
            wait = interval if deadline is None else min(interval, max(deadline - time.monotonic(), 0.0))
            # A write from this process wakes us immediately; the timeout still covers writers elsewhere.
            self._job_signal.wait_change(job_id, version, wait)

    def update_job(self, job: JobRecord) -> JobRecord:
        self._write_job(job)
        self._prune_job_files()
        # Running jobs can update on every deployment poll. Auditing every
        # heartbeat rewrites the complete audit JSON and adds no durable value;
        # create and terminal transitions remain audited.
        if job.status not in {JobStatus.queued, JobStatus.running}:
            self.log_audit("job.update", job.status.value, job.job_type, {"job_id": job.id})
        return job

    def _job_path(self, job_id: str) -> Path:
        value = str(job_id or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", value):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job_id")
        return self.jobs_dir / f"{value}.json"

    def _write_job(self, job: JobRecord) -> None:
        path = self._job_path(job.id)
        payload = job.model_dump_json()
        with self._file_lock(path):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        self._job_signal.bump(job.id)

    def _migrate_legacy_jobs(self) -> None:
        marker = self.jobs_dir / ".legacy_migrated"
        if marker.exists():
            return
        rows = self._read_list(self.jobs_file)
        for row in rows:
            try:
                job = JobRecord.model_validate(row)
                if not self._job_path(job.id).exists():
                    self._write_job(job)
            except (HTTPException, ValueError, OSError):
                continue
        marker.touch(exist_ok=True)
        self._prune_job_files()

    def _prune_job_files(self) -> None:
        """Bound terminal history while never evicting queued/running jobs (see job_retention)."""
        maximum = max(20, int(getattr(settings, "job_max_count", 500)))
        prune_job_dir(self.jobs_dir, maximum, {JobStatus.queued.value, JobStatus.running.value})

    def add_step(self, job: JobRecord, step: JobStep) -> JobRecord:
        step.finished_at = step.finished_at or datetime.now(timezone.utc).isoformat()
        job.steps.append(step)
        return self.update_job(job)

    def finish_job(self, job: JobRecord, status_: JobStatus, result: Any = None) -> JobRecord:
        job.status = status_
        job.finished_at = datetime.now(timezone.utc).isoformat()
        job.result = result
        finished = self.update_job(job)
        self._sync_operation_handoff_for_job(finished)
        return finished

    def bind_operation_handoff_to_job(
        self,
        job_id: str,
        operation_id: str,
        *,
        client_name: str | None = None,
    ) -> JobRecord:
        """Attach optional continuation metadata without changing job ownership."""
        job = self.get_job(job_id)
        request = dict(job.request or {})
        request["handoff_operation_id"] = operation_id
        if client_name and not request.get("client_name"):
            request["client_name"] = client_name
        job.request = request
        updated = self.update_job(job)
        if updated.status not in {JobStatus.queued, JobStatus.running}:
            self._sync_operation_handoff_for_job(updated)
        return updated

    def _sync_operation_handoff_for_job(self, job: JobRecord) -> None:
        """Best-effort terminal sidecar sync; it must never fail the owned job."""
        request = dict(job.request or {})
        operation_id = str(request.get("handoff_operation_id") or "")
        if not operation_id:
            return
        client_name = request.get("client_name")
        try:
            handoff = OdooOperationHandoff.model_validate(
                self.get_operation_handoff(operation_id, client_name=client_name)
            )
            result = job.result if isinstance(job.result, dict) else {}
            deploy_confirmed = bool(
                result.get("deploy_confirmed")
                or result.get("already_deployed_confirmed")
            )
            if job.status == JobStatus.passed and deploy_confirmed:
                handoff.status = "completed"
                handoff.current_blocker = None
                handoff.next_action = "Stop and return the terminal verified result."
                handoff.acceptance_evidence_remaining = []
                if "terminal_deployment_proof" not in handoff.completed_checkpoints:
                    handoff.completed_checkpoints.append("terminal_deployment_proof")
            else:
                handoff.status = "blocked"
                handoff.current_blocker = str(
                    result.get("failed_step")
                    or result.get("error")
                    or result.get("status")
                    or job.status.value
                )[:3000]
                handoff.next_action = (
                    f"Resume or diagnose safe-deploy job {job.id} from its terminal blocker."
                )
                handoff.acceptance_evidence_remaining = ["terminal_deployment_proof"]
            self.save_operation_handoff(handoff, client_name=client_name)
        except Exception:
            # Handoffs are recovery metadata. Their storage must never alter
            # the terminal state of the server-owned deployment job.
            return

    def log_audit(self, action: str, status_: str, summary: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent:
        event = AuditEvent(action=action, status=status_, summary=summary, metadata=metadata or {})
        def update(rows):
            rows.append(event.model_dump(mode="json"))
            if len(rows) > 2000:
                del rows[:-2000]

        self._update_list(self.audit_file, update)
        return event

    def list_audit(self, limit: int = 200) -> list[AuditEvent]:
        rows = self._read_list(self.audit_file)
        return [AuditEvent.model_validate(row) for row in rows[-limit:]][::-1]

    def save_mutation(self, record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(record.get("request_id") or "")
        client_name = str(record.get("client_name") or "")

        def update(rows):
            for index, row in enumerate(rows):
                if row.get("request_id") == request_id and str(row.get("client_name") or "") == client_name:
                    rows[index] = dict(record)
                    return dict(record)
            rows.append(dict(record))
            if len(rows) > 2000:
                del rows[:-2000]
            return dict(record)

        return self._update_list(self.mutations_file, update)

    def get_mutation(self, request_id: str, client_name: str | None = None) -> dict[str, Any] | None:
        for row in reversed(self._read_list(self.mutations_file)):
            if row.get("request_id") != request_id:
                continue
            if client_name is not None and str(row.get("client_name") or "") != client_name:
                continue
            return dict(row)
        return None

    def list_mutations(
        self,
        limit: int = 20,
        request_id: str | None = None,
        client_name: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_list(self.mutations_file)
        filtered = [
            row
            for row in rows
            if (request_id is None or row.get("request_id") == request_id)
            and (client_name is None or str(row.get("client_name") or "") == client_name)
        ]
        return [dict(row) for row in filtered[-limit:]][::-1]

    def create_artifact(self, kind: str, source_tool: str, payload: Any) -> dict[str, Any]:
        artifact_id = f"art_{uuid4().hex}"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        record = {
            "artifact_id": artifact_id,
            "kind": kind,
            "source_tool": source_tool,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bytes": len(text.encode("utf-8")),
            "text": text,
        }
        path = self.artifacts_dir / f"{artifact_id}.json"
        with self._file_lock(path):
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self._cleanup_artifacts(keep=path)
        self.log_audit("artifact.create", "ok", source_tool, {"artifact_id": artifact_id, "bytes": record["bytes"]})
        return {key: record[key] for key in ("artifact_id", "kind", "source_tool", "created_at", "bytes")}

    def _cleanup_artifacts(self, *, keep: Path | None = None) -> None:
        """Apply TTL, count, and aggregate-byte retention to artifact sidecars."""
        if not self.artifacts_dir.exists():
            return
        now = time.time()
        ttl = max(3600, int(getattr(settings, "artifact_ttl_seconds", 604800)))
        maximum = max(20, int(getattr(settings, "artifact_max_count", 500)))
        byte_limit = max(1_000_000, int(getattr(settings, "artifact_max_total_bytes", 250_000_000)))
        entries: list[tuple[float, int, Path]] = []
        for path in self.artifacts_dir.glob("art_*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path != keep and now - stat.st_mtime > ttl:
                path.unlink(missing_ok=True)
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        entries.sort(key=lambda item: item[0], reverse=True)
        total = sum(size for _mtime, size, _path in entries)
        for index, (_mtime, size, path) in enumerate(entries):
            if path == keep:
                continue
            if index < maximum and total <= byte_limit:
                continue
            path.unlink(missing_ok=True)
            total = max(0, total - size)

    def read_artifact(self, artifact_id: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", artifact_id)
        if safe_id != artifact_id or not safe_id.startswith("art_"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid artifact_id")
        path = self.artifacts_dir / f"{safe_id}.json"
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Artifact is unreadable") from exc
        text = str(record.get("text", ""))
        start = min(max(offset, 0), len(text))
        end = min(start + max(max_chars, 1), len(text))
        return {
            "artifact_id": safe_id,
            "kind": record.get("kind"),
            "source_tool": record.get("source_tool"),
            "created_at": record.get("created_at"),
            "bytes": record.get("bytes", len(text.encode("utf-8"))),
            "offset": start,
            "returned_chars": end - start,
            "next_offset": end if end < len(text) else None,
            "text": text[start:end],
        }

    # ---------------------------------------------------------------------------
    # Fix 7: Session state persistence — survives Render/Koyeb restarts.
    # The agent can save and reload: last_sha, module_name, step_history, and
    # any arbitrary context across sessions.
    #
    # Phase 3: multi-client isolation. The legacy unsuffixed session_state.json
    # keeps being written exactly as before (so session.load_state() with no
    # args behaves identically to today — nothing breaks for the no-client or
    # single-client case). When a client_name is known (passed explicitly, or
    # present inside the saved state dict — workflow.start_session already
    # includes it), an additional per-client file is written/read so two
    # different clients' sessions never silently bleed into each other.
    # ---------------------------------------------------------------------------

    @property
    def _session_file(self) -> Path:
        return self.root / "session_state.json"

    def _session_file_for_client(self, client_name: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", client_name)
        return self.root / f"session_state__{safe}.json"

    def _debug_sessions_file_for_client(self, client_name: str | None) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", client_name or "default")
        return self.root / f"debug_sessions__{safe}.json"

    def _operation_handoffs_file_for_client(self, client_name: str | None) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", client_name or "default")
        return self.root / f"operation_handoffs__{safe}.json"

    def save_operation_handoff(
        self,
        handoff: OdooOperationHandoff | dict[str, Any],
        client_name: str | None = None,
    ) -> dict[str, Any]:
        """Atomically upsert one bounded per-client continuation artifact."""
        record = OdooOperationHandoff.model_validate(handoff)
        path = self._operation_handoffs_file_for_client(client_name or record.client_name)

        def update(rows):
            for index, row in enumerate(rows):
                if row.get("operation_id") != record.operation_id:
                    continue
                existing = OdooOperationHandoff.model_validate(row)
                if existing.status == "completed":
                    if record.status != "completed":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Completed operation handoff is immutable.",
                        )
                    record.created_at = existing.created_at
                    record.updated_at = existing.updated_at
                    rows[index] = existing.model_dump(mode="json")
                    break
                record.created_at = existing.created_at
                record.updated_at = datetime.now(timezone.utc).isoformat()
                rows[index] = record.model_dump(mode="json")
                break
            else:
                rows.append(record.model_dump(mode="json"))
            rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
            if len(rows) > 50:
                del rows[:-50]

        self._update_list(path, update)
        self.log_audit(
            "operation_handoff.save",
            record.status,
            record.operation_id,
            {"client_name": client_name or record.client_name, "module_name": record.module_name},
        )
        return record.model_dump(mode="json")

    def get_operation_handoff(
        self,
        operation_id: str,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        for row in self._read_list(self._operation_handoffs_file_for_client(client_name)):
            if row.get("operation_id") == operation_id:
                return OdooOperationHandoff.model_validate(row).model_dump(mode="json")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation handoff not found.")

    def complete_operation_handoff(
        self,
        operation_id: str,
        *,
        client_name: str | None = None,
        next_action: str = "Stop and return the terminal verified result.",
    ) -> dict[str, Any]:
        current = OdooOperationHandoff.model_validate(
            self.get_operation_handoff(operation_id, client_name=client_name)
        )
        if current.status == "completed":
            return current.model_dump(mode="json")
        current.status = "completed"
        current.current_blocker = None
        current.next_action = next_action
        current.acceptance_evidence_remaining = []
        return self.save_operation_handoff(current, client_name=client_name)

    def save_debug_session(self, session: dict[str, Any], client_name: str | None = None) -> dict[str, Any]:
        """Atomically upsert one isolated debug session.

        Debug sessions use a dedicated per-client collection so concurrent
        workflows cannot overwrite the nested ``debug_sessions`` map inside the
        general session-state file.
        """
        record = dict(session)
        session_id = str(record.get("id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("debug session requires a safe 32-character id")
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = self._debug_sessions_file_for_client(client_name)

        def update(rows):
            for index, row in enumerate(rows):
                if row.get("id") == session_id:
                    rows[index] = record
                    break
            else:
                rows.append(record)
            rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
            if len(rows) > 50:
                del rows[:-50]

        self._update_list(path, update)
        return dict(record)

    def get_debug_session(self, session_id: str, client_name: str | None = None) -> dict[str, Any]:
        for row in self._read_list(self._debug_sessions_file_for_client(client_name)):
            if row.get("id") == session_id:
                return dict(row)
        # One-way compatibility with sessions created before isolated storage.
        legacy = self.load_session_state(client_name=client_name)
        sessions = legacy.get("debug_sessions") if isinstance(legacy, dict) else None
        row = sessions.get(session_id) if isinstance(sessions, dict) else None
        if isinstance(row, dict):
            self.save_debug_session(row, client_name=client_name)
            return dict(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug session not found for the selected client.")

    def find_debug_session_by_operation(self, operation_id: str, client_name: str | None = None) -> dict[str, Any] | None:
        rows = self._read_list(self._debug_sessions_file_for_client(client_name))
        for row in reversed(rows):
            if row.get("operation_id") == operation_id:
                return dict(row)
        return None

    def trusted_debug_evidence_ids(
        self,
        client_name: str | None = None,
        *,
        debug_session_id: str | None = None,
        module_name: str | None = None,
        task_description: str | None = None,
    ) -> set[str]:
        """Return trusted evidence only from the exact bound issue/module session."""
        if not debug_session_id:
            return set()
        normalized_task = re.sub(r"\s+", " ", str(task_description or "").lower())
        trusted: set[str] = set()
        for session in self._read_list(self._debug_sessions_file_for_client(client_name)):
            if session.get("id") != debug_session_id:
                continue
            session_modules = {
                str(session.get("module_name") or "").strip(),
                str(session.get("requested_module_name") or "").strip(),
            }
            if module_name and module_name not in session_modules:
                break
            session_issue = re.sub(
                r"\s+",
                " ",
                str(session.get("issue_description") or "").lower(),
            ).strip()
            if session_issue and session_issue not in normalized_task:
                break
            try:
                diagnosis = OdooFailureDiagnosis.model_validate(session.get("diagnosis"))
            except Exception:
                break
            if (
                diagnosis.debug_session_id != debug_session_id
                or diagnosis.origin != "custom_code"
                or diagnosis.next_strategy != "repair_custom_code"
            ):
                break
            diagnosed_ids = set(diagnosis.evidence_ids)
            for item in session.get("evidence_ledger") or []:
                if (
                    isinstance(item, dict)
                    and item.get("trusted") is True
                    and item.get("id") in diagnosed_ids
                ):
                    trusted.add(str(item["id"]))
            break
        return trusted

    def save_session_state(self, state: dict, client_name: str | None = None) -> dict:
        """Persist arbitrary session state (sha, module, step history, etc.).

        Called by workflow.start_session after success and by safe_deploy on
        each milestone so the agent can resume without repeating step 1.
        state dict is shallow-merged into the existing persisted state.

        In multi-client mode, writes only the per-client file when a client is
        known. The legacy global file is used only for no-client/single-client
        fallback state so one client's deploy state cannot bleed into another.
        """
        effective_client = client_name or state.get("client_name") or None
        from datetime import datetime, timezone

        def _write(path: Path) -> dict:
            with self._lock:
                with self._file_lock(path):
                    current: dict = {}
                    if path.exists():
                        try:
                            raw = json.loads(path.read_text(encoding="utf-8"))
                            current = raw if isinstance(raw, dict) else {}
                        except (OSError, json.JSONDecodeError):
                            current = {}
                    current.update(state)
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
                    tmp_path = path.with_suffix(path.suffix + ".tmp")
                    tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
                    tmp_path.replace(path)
                    return current

        if effective_client:
            return _write(self._session_file_for_client(effective_client))
        return _write(self._session_file)

    def load_session_state(self, client_name: str | None = None) -> dict:
        """Return last persisted session state, or empty dict if none.

        With no client_name, reads the legacy global file (same as before
        this feature existed). With client_name, reads that client's
        isolated state file — recommended once you have more than one client
        configured, so a resumed session can never pick up another client's
        project_path/sha by accident.
        """
        if not client_name and len(get_odoo_clients()) > 1:
            return {
                "requires_client_name": True,
                "message": "Multiple ODOO_CLIENTS entries are configured; pass client_name to load isolated session state.",
            }
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        if not path.exists():
            return {}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return self._with_validated_project_path(state if isinstance(state, dict) else {})

    def _with_validated_project_path(self, state: dict) -> dict:
        project_path = state.get("project_path")
        if not project_path:
            return state

        checked = dict(state)
        try:
            resolved = Path(str(project_path)).expanduser().resolve()
        except OSError as exc:
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Could not resolve project_path: {exc}"
            return checked

        allowed_roots = settings.allowed_roots
        if not allowed_roots or not any(resolved == root or root in resolved.parents for root in allowed_roots):
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Project path is outside allowed roots: {resolved}"
            return checked

        if not resolved.exists() or not resolved.is_dir():
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Project path is missing or not a directory: {resolved}"
            return checked

        checked["project_path"] = str(resolved)
        checked["project_path_valid"] = True
        checked.pop("stale_project_path", None)
        checked.pop("project_path_error", None)
        return checked

    def clear_session_state(self, client_name: str | None = None) -> None:
        """Wipe the session state file (e.g. after a successful full deploy)."""
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        path.unlink(missing_ok=True)
