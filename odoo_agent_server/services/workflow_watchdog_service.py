from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Awaitable, Callable

from ..config import settings
from ..schemas import JobRecord, JobRecordWithWatchdog, JobStatus, WatchdogAssessment


# Only explicitly read-only/provider-safe operations may be timed out. Unknown
# tools are protected, as are all coding, Git, deployment, upgrade, and database
# workflows. This avoids relying on fragile name-prefix guesses.
_CANCELLABLE_TOOLS = {
    "connector.health",
    "skills.list",
    "skills.read",
    "workflow.agent_instructions",
    "workflow.module_analyze",
    "odoo.query",
    "odoo.search_read",
    "odoo.feature_inspect",
    "odoo.read_group",
    "artifacts.read",
    "jobs.list",
    "jobs.get",
    "odoosh.error_pack",
}
_PROTECTED_PHASE_TOKENS = {
    "write",
    "patch",
    "commit",
    "push",
    "deploy",
    "upgrade",
    "install",
    "migration",
    "database",
    "transaction",
    "code_change",
    "debug_and_fix",
}
_SAFE_PHASE_TOKENS = {
    "read",
    "search",
    "inspect",
    "analy",
    "log",
    "browser",
    "poll",
    "status",
    "health",
    "artifact",
    "skill",
}
_SAFE_CODE_CHANGE_PHASES = {
    "module_discovery",
    "validation_and_index",
    "focused_inspection",
    "focused_target_search",
    "custom_source_read",
    "scope_resolution",
    "core_source_read",
    "target_resolution",
}
_PROTECTED_CODE_CHANGE_PHASES = {
    "queued",
    "starting",
    "repo_prepare",
    "workspace_activation",
    "write_and_checkpoint",
    "qa_and_core_verification",
    "commit_and_push",
    "deploying",
    "completed",
}
_WORKFLOW_PROGRESS_UNSET = object()


class WorkflowWatchdogService:
    """Bound safe MCP observations while protecting all mutation-capable work.

    Tool execution is runtime-scoped and stateless, so separate users/tasks
    cannot share counters. In ``shadow`` mode it observes without changing the
    result. In ``safe_redirect`` mode only allowlisted read-only coroutines are
    cancelled after the configured budget; protected and unknown tools always
    run normally.
    """

    _job_states: dict[str, dict[str, Any]] = {}
    _job_states_lock = Lock()

    async def run_tool_call(
        self,
        tool_name: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if not settings.workflow_watchdog_enabled or tool_name not in _CANCELLABLE_TOOLS:
            return await operation()

        budget = float(settings.workflow_watchdog_observation_budget_seconds)
        started = time.monotonic()
        if settings.workflow_watchdog_mode == "shadow":
            return await operation()

        try:
            return await asyncio.wait_for(operation(), timeout=budget)
        except TimeoutError:
            elapsed = max(time.monotonic() - started, 0.0)
            return {
                "status": "watchdog_redirect",
                "state_class": "blocked",
                "phase": "read_only_operation",
                "protected": False,
                "stalled": True,
                "elapsed_seconds": round(elapsed, 1),
                "reason": "The read-only operation exceeded its configured runtime budget.",
                "action": "switch_strategy",
                "redirect_kind": "use_focused_evidence",
                "next_action": {
                    "type": "use_focused_evidence",
                    "instruction": (
                        "Do not repeat the same broad operation. Continue from focused evidence already "
                        "collected, or ask one precise user question only when the task contract is unclear."
                    ),
                },
                "cancellation_requested": True,
                "force_cancelled": False,
                "success_allowed": False,
                "completion_required": False,
                "terminal_action": "stop_and_summarize",
            }

    def annotate_job(self, job: JobRecord) -> JobRecordWithWatchdog:
        progress = self._workflow_progress(job)
        return JobRecordWithWatchdog(
            **job.model_dump(mode="python"),
            watchdog=WatchdogAssessment.model_validate(self.assess_job(job, progress)),
            workflow_progress=progress,
        )

    def assess_job(
        self,
        job: JobRecord,
        workflow_progress: dict[str, Any] | None | object = _WORKFLOW_PROGRESS_UNSET,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        progress = (
            self._workflow_progress(job)
            if workflow_progress is _WORKFLOW_PROGRESS_UNSET
            else workflow_progress
        )
        progress = progress if isinstance(progress, dict) else None
        phase = self._job_phase(job, progress)
        protected = self._job_is_protected(job, phase)
        terminal = job.status not in {JobStatus.queued, JobStatus.running}
        last_activity = self._job_last_activity(job, now, progress)
        idle_seconds = max((now - last_activity).total_seconds(), 0.0)
        phase_started = self._parse_time((progress or {}).get("phase_started_at"))
        phase_elapsed_seconds = max(
            (now - (phase_started or last_activity)).total_seconds(),
            0.0,
        )
        soft_deadline_seconds = float(settings.workflow_watchdog_observation_budget_seconds)
        deadline_exceeded = (
            not terminal
            and phase_started is not None
            and phase_elapsed_seconds >= soft_deadline_seconds
        )

        fingerprint = self._job_fingerprint(job, progress)
        with self._job_states_lock:
            state = self._job_states.get(job.id, {})
            unchanged = (
                int(state.get("unchanged_observations", 0) or 0) + 1
                if state.get("fingerprint") == fingerprint
                else 0
            )
            if terminal:
                self._job_states.pop(job.id, None)
            else:
                self._job_states[job.id] = {
                    "fingerprint": fingerprint,
                    "unchanged_observations": unchanged,
                    "observed_at": now.isoformat(),
                }
                self._prune_job_states()

        # A persisted phase timestamp is authoritative: the first poll after the
        # deadline must escalate. Legacy jobs without phase progress retain the
        # confirmation guard to avoid false positives from a coarse created_at.
        stalled = not terminal and (
            deadline_exceeded
            if phase_started is not None
            else (
                idle_seconds >= soft_deadline_seconds
                and unchanged >= settings.workflow_watchdog_stall_confirmations
            )
        )
        agent_instruction: str | None = None
        if terminal:
            action = "stop_polling"
            reason = "Job is terminal."
            redirect_kind = "none"
        elif protected and stalled:
            action = "continue_with_backoff"
            reason = (
                "The same protected phase exceeded its 2-minute soft deadline. "
                "It remains protected from unsafe cancellation."
            )
            redirect_kind = "preserve_protected_work"
            agent_instruction = (
                "SAME_PHASE_DEADLINE_EXCEEDED: do not wait silently, restart the task, or add optional "
                "inspection. Poll jobs.get immediately with wait_for_result=false. A live protected "
                "worker must finish its current atomic operation; an expired worker lease is recovered "
                "by jobs.get from the persisted checkpoint."
            )
        elif protected:
            action = "continue_with_backoff"
            reason = "Mutation-capable or unknown work is protected from cancellation."
            redirect_kind = "preserve_protected_work"
        elif stalled:
            action = "switch_strategy"
            reason = "Read-only job exceeded its budget without observable progress."
            redirect_kind = self._job_redirect_kind(job)
            agent_instruction = (
                "SAME_PHASE_DEADLINE_EXCEEDED: stop repeating this read-only step and execute "
                "watchdog.next_action now."
            )
        else:
            action = "continue"
            reason = "No confirmed stall."
            redirect_kind = "none"

        next_actions = {
            "preserve_protected_work": {
                "type": "continue_with_backoff",
                "instruction": "Keep the owned coding/deploy/database work running and poll after the returned backoff.",
            },
            "use_focused_evidence": {
                "type": "use_focused_evidence",
                "instruction": "Do not repeat the broad read; continue from focused evidence or narrow the source query.",
            },
            "load_exact_source": {
                "type": "load_exact_source",
                "instruction": "Load only the missing primary source identified by the diagnosis.",
            },
            "reload_handoff": {
                "type": "reload_handoff",
                "instruction": "Reload the persisted operation handoff instead of adding more context.",
            },
            "resume_connector": {
                "type": "resume_connector",
                "instruction": "Resume or repair the connector checkpoint without changing Odoo module code.",
            },
            "retry_provider_bounded": {
                "type": "retry_provider_bounded",
                "instruction": "Retry the provider transport within its bounded retry/backoff policy.",
            },
            "ask_material_question": {
                "type": "ask_material_question",
                "instruction": "Ask one precise question only because a material product decision remains unresolved.",
            },
        }

        return {
            "status": "terminal" if terminal else ("stalled" if stalled else "active"),
            "phase": phase,
            "protected": protected,
            "stalled": stalled,
            "idle_seconds": round(idle_seconds, 1),
            "phase_elapsed_seconds": round(phase_elapsed_seconds, 1),
            "soft_deadline_seconds": soft_deadline_seconds,
            "deadline_exceeded": deadline_exceeded,
            "unchanged_observations": unchanged,
            "action": action,
            "reason": reason,
            "redirect_kind": redirect_kind,
            "next_action": next_actions.get(redirect_kind),
            "agent_instruction": agent_instruction,
            "poll_after_seconds": (
                None
                if terminal
                else 2
                if deadline_exceeded
                else min(10, max(1, 2 ** min(unchanged, 3)))
            ),
            "cancellation_supported": False,
            "force_cancelled": False,
        }

    def provider_wait_timeout(self, requested_seconds: int) -> int:
        """Return the explicit provider-MCP wait without changing REST callers."""
        if (
            not settings.workflow_watchdog_enabled
            or settings.workflow_watchdog_mode == "shadow"
            or requested_seconds <= 0
        ):
            return requested_seconds
        return min(requested_seconds, settings.workflow_watchdog_wait_cap_seconds)

    def _prune_job_states(self) -> None:
        if len(self._job_states) <= 1000:
            return
        oldest = min(
            self._job_states,
            key=lambda item: str(self._job_states[item].get("observed_at") or ""),
        )
        self._job_states.pop(oldest, None)

    def _job_is_protected(self, job: JobRecord, phase: str) -> bool:
        normalized_phase = str(phase or "").strip().lower()
        # The composite job name contains ``code_change`` and previously made
        # every phase look mutation-capable. Classify its persisted phase first
        # so a stalled scope/search/read step can redirect while actual
        # write/commit/deploy work remains protected.
        if job.job_type == "workflow.odoo_code_change":
            if normalized_phase in _SAFE_CODE_CHANGE_PHASES:
                return False
            if normalized_phase in _PROTECTED_CODE_CHANGE_PHASES:
                return True
        if any(token in normalized_phase for token in _PROTECTED_PHASE_TOKENS):
            return True
        if any(token in normalized_phase for token in _SAFE_PHASE_TOKENS):
            return False
        text = str(job.job_type or "").lower()
        if any(token in text for token in _PROTECTED_PHASE_TOKENS):
            return True
        if any(token in text for token in _SAFE_PHASE_TOKENS):
            return False
        return True

    def _workflow_progress(self, job: JobRecord) -> dict[str, Any] | None:
        if job.job_type != "workflow.odoo_code_change":
            return None
        operation_id = str((job.request or {}).get("operation_id") or "")
        if not operation_id:
            return None
        try:
            from .store_service import JsonStore

            progress = JsonStore().get_workflow_progress(operation_id)
            return dict(progress) if isinstance(progress, dict) else None
        except Exception:
            return None

    def _job_phase(self, job: JobRecord, progress: dict[str, Any] | None = None) -> str:
        if job.status not in {JobStatus.queued, JobStatus.running}:
            return "terminal"
        if progress and progress.get("current_phase"):
            return str(progress["current_phase"])
        return str(job.steps[-1].name if job.steps else job.job_type or "unknown")

    def _job_last_activity(
        self,
        job: JobRecord,
        fallback: datetime,
        progress: dict[str, Any] | None = None,
    ) -> datetime:
        candidates = [self._parse_time(job.created_at)]
        for step in job.steps:
            candidates.extend((self._parse_time(step.started_at), self._parse_time(step.finished_at)))
        if progress:
            candidates.append(self._parse_time(progress.get("last_progress_at")))
        return max((item for item in candidates if item is not None), default=fallback)

    def _job_fingerprint(self, job: JobRecord, progress: dict[str, Any] | None = None) -> str:
        last_step = job.steps[-1].model_dump(mode="json") if job.steps else None
        result = job.result if isinstance(job.result, dict) else {}
        return repr(
            (
                job.status.value,
                len(job.steps),
                last_step,
                job.finished_at,
                result.get("status"),
                result.get("progress"),
                result.get("updated_at"),
                (progress or {}).get("current_phase"),
                (progress or {}).get("last_progress_at"),
            )
        )

    def _job_redirect_kind(self, job: JobRecord) -> str:
        result = job.result if isinstance(job.result, dict) else {}
        diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
        origin = str(result.get("failure_origin") or diagnosis.get("origin") or "")
        return {
            "missing_context": "load_exact_source",
            "context_degradation": "reload_handoff",
            "connector_pipeline": "resume_connector",
            "provider_transport": "retry_provider_bounded",
            "material_ambiguity": "ask_material_question",
        }.get(origin, "use_focused_evidence")

    def _parse_time(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
