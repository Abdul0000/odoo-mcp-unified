from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus
from .store_service import JsonStore


class ApprovalService:
    def __init__(self) -> None:
        self.store = JsonStore()

    def ensure_allowed(
        self,
        action: str,
        approval_id: str | None,
        payload_preview: dict[str, Any] | None = None,
        *,
        force: bool = False,
        bind_payload: bool = False,
    ) -> None:
        """Enforce the configured one-time approval policy for mutations."""
        # Global approval switch is authoritative. If ODOO_AGENT_REQUIRE_APPROVAL=false,
        # old/stale ODOO_AGENT_APPROVAL_REQUIRED_ACTIONS values must not block tools.
        # This fixes Render envs where REQUIRE_APPROVAL=false but the action list
        # still contains deploy/update actions from older builds.
        required_actions = settings.approval_required_actions
        action_is_required = action in required_actions or any(
            item.endswith(".*") and action.startswith(item[:-1])
            for item in required_actions
        )
        approval_required = force or (
            bool(settings.require_approval)
            and (not required_actions or action_is_required)
        )
        if not approval_required:
            self.store.log_audit(action, "allowed", "Approval not required", {"approval_required": False})
            return

        if not approval_id:
            record = self.store.create_approval(ApprovalRecord(action=action, summary=f"Approval required for {action}", payload_preview=payload_preview or {}))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "approval_required": True,
                    "approval_id": record.id,
                    "action": action,
                    "message": "Approval required. Approve this approval_id then retry the same tool call.",
                },
            )

        record = self.store.get_approval(approval_id)
        if record.action != action:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is for {record.action}, not {action}")
        if record.status != ApprovalStatus.approved:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is {record.status.value}, not approved")
        if bind_payload and json.dumps(record.payload_preview, sort_keys=True, default=str) != json.dumps(payload_preview or {}, sort_keys=True, default=str):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Approval {approval_id} does not match this exact mutation payload",
            )
        self.store.set_approval_status(approval_id, ApprovalStatus.running, reason="Action started")

    def mark_consumed(self, approval_id: str | None, reason: str | None = None) -> None:
        if approval_id:
            self.store.set_approval_status(approval_id, ApprovalStatus.consumed, reason=reason or "Action succeeded")

    def mark_failed(self, approval_id: str | None, reason: str | None = None) -> None:
        if approval_id:
            self.store.set_approval_status(approval_id, ApprovalStatus.failed, reason=reason or "Action failed")

    def finalize_command(self, approval_id: str | None, exit_code: int, success_reason: str, failure_reason: str) -> None:
        if exit_code == 0:
            self.mark_consumed(approval_id, success_reason)
        else:
            self.mark_failed(approval_id, failure_reason)

    def finalize_status(self, approval_id: str | None, ok: bool, success_reason: str, failure_reason: str) -> None:
        if ok:
            self.mark_consumed(approval_id, success_reason)
        else:
            self.mark_failed(approval_id, failure_reason)
