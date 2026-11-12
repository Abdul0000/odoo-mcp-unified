from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import ApprovalCreateRequest, ApprovalDecisionRequest, ApprovalRecord, ApprovalStatus
from ..services.store_service import JsonStore

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[AuthDependency])


@router.get("", response_model=list[ApprovalRecord])
def list_approvals(limit: int = 100) -> list[ApprovalRecord]:
    return JsonStore().list_approvals(limit=limit)


@router.post("/request", response_model=ApprovalRecord)
def request_approval(payload: ApprovalCreateRequest) -> ApprovalRecord:
    return JsonStore().create_approval(ApprovalRecord(action=payload.action, summary=payload.summary, payload_preview=payload.payload_preview))


@router.post("/approve", response_model=ApprovalRecord)
def approve(payload: ApprovalDecisionRequest) -> ApprovalRecord:
    return JsonStore().set_approval_status(payload.approval_id, ApprovalStatus.approved, payload.reason)


@router.post("/reject", response_model=ApprovalRecord)
def reject(payload: ApprovalDecisionRequest) -> ApprovalRecord:
    return JsonStore().set_approval_status(payload.approval_id, ApprovalStatus.rejected, payload.reason)
