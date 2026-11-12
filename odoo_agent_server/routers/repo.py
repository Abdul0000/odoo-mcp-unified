from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    FileContent,
    FileListItem,
    CustomModuleItem,
    RepoCleanPycRequest,
    RepoCleanPycResult,
    RepoCustomModulesRequest,
    RepoDeleteRequest,
    RepoListRequest,
    RepoAtomicPatchRequest,
    RepoPatchRequest,
    RepoPrepareRequest,
    RepoPrepareResult,
    RepoReadRangeRequest,
    RepoReadRequest,
    RepoSearchRequest,
    RepoWriteManyAtomicRequest,
    RepoWriteManyFastSafeRequest,
    RepoWriteManySmartRequest,
    RepoWriteRequest,
)
from ..services.approval_service import ApprovalService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore
from ..services.client_resolver_service import ClientResolverService
from ..services.checkout_guard_service import maybe_ensure_known_module_current, module_name_from_relative_path
from ..services.client_gate_service import ClientGateService
from ..services.prewrite_gate_service import PreWriteGateService

router = APIRouter(prefix="/repo", tags=["repo"], dependencies=[AuthDependency])


def _require_explicit_client_when_multiclient(payload: object, action: str) -> None:
    """Require client.check validation before repo tools in multi-client mode."""
    ClientGateService().require_validated_client(getattr(payload, "client_name", None), action)


def _preview(payload: object) -> dict[str, object]:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else {}
    if "content" in data:
        data["content"] = f"<redacted text length={len(str(data['content']))}>"
    if "patch" in data:
        data["patch"] = f"<redacted patch length={len(str(data['patch']))}>"
    if "files" in data and isinstance(data["files"], list):
        redacted = []
        for item in data["files"]:
            if isinstance(item, dict):
                copy = dict(item)
                if "content" in copy:
                    copy["content"] = f"<redacted text length={len(str(copy['content']))}>"
                redacted.append(copy)
            else:
                redacted.append(item)
        data["files"] = redacted
    return data


def _ensure_known_relative_module_current(project_path: str, relative_path: str) -> None:
    module_name = module_name_from_relative_path(relative_path, project_path)
    if not module_name:
        return
    resolver = ClientResolverService()
    client = resolver.resolve_client(project_path=project_path, require_complete=False)
    branch = client.branch if client else None
    guard = maybe_ensure_known_module_current(
        project_path=project_path,
        module_name=module_name,
        branch=branch,
        client_name=client.name if client else None,
        remote="origin",
        timeout_seconds=120,
        require_prior_success=True,
    )
    if guard and not guard.get("ok"):
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=guard.get("error") or "Checkout/module guard failed")


@router.post("/prepare", response_model=RepoPrepareResult)
def repo_prepare(payload: RepoPrepareRequest) -> RepoPrepareResult:
    _require_explicit_client_when_multiclient(payload, "repo.prepare")
    approval = ApprovalService()
    approval.ensure_allowed("repo.prepare", payload.approval_id, _preview(payload))
    try:
        result = RepoService().prepare_repo(
            payload.repo_url,
            payload.branch,
            payload.repo_name,
            payload.workspace_root,
            payload.remote,
            payload.timeout_seconds,
            payload.project_path,
            payload.client_name,
        )
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.prepare failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.prepare succeeded")
    JsonStore().log_audit("repo.prepare", "ok", result.project_path, _preview(payload))
    return result


@router.post("/custom-modules", response_model=list[CustomModuleItem])
def repo_custom_modules(payload: RepoCustomModulesRequest) -> list[CustomModuleItem]:
    _require_explicit_client_when_multiclient(payload, "repo.list_custom_modules")
    return RepoService().list_custom_modules(payload.project_path, payload.max_depth, payload.include_uninstallable)


@router.post("/list", response_model=list[FileListItem])
def repo_list(payload: RepoListRequest) -> list[FileListItem]:
    _require_explicit_client_when_multiclient(payload, "repo.list")
    _ensure_known_relative_module_current(payload.project_path, payload.relative_path)
    result = RepoService().list_files(payload.project_path, payload.relative_path, payload.max_depth, payload.max_items)
    PreWriteGateService().record_repo_inspection(project_path=payload.project_path, relative_path=payload.relative_path, client_name=payload.client_name, action="repo.list", is_content_read=False)
    return result


@router.post("/read", response_model=FileContent)
def repo_read(payload: RepoReadRequest) -> FileContent:
    _require_explicit_client_when_multiclient(payload, "repo.read")
    _ensure_known_relative_module_current(payload.project_path, payload.relative_path)
    result = RepoService().read_file(payload.project_path, payload.relative_path, payload.max_bytes)
    PreWriteGateService().record_repo_inspection(project_path=payload.project_path, relative_path=payload.relative_path, client_name=payload.client_name, action="repo.read", is_content_read=True)
    return result


@router.post("/read-range", response_model=dict)
def repo_read_range(payload: RepoReadRangeRequest) -> dict:
    _require_explicit_client_when_multiclient(payload, "repo.read_range")
    _ensure_known_relative_module_current(payload.project_path, payload.relative_path)
    result = RepoService().read_range(payload.project_path, payload.relative_path, payload.start_line, payload.max_lines)
    PreWriteGateService().record_repo_inspection(project_path=payload.project_path, relative_path=payload.relative_path, client_name=payload.client_name, action="repo.read_range", is_content_read=True)
    return result


@router.post("/search", response_model=dict)
def repo_search(payload: RepoSearchRequest) -> dict:
    _require_explicit_client_when_multiclient(payload, "repo.search")
    _ensure_known_relative_module_current(payload.project_path, payload.relative_path)
    result = RepoService().search(payload.project_path, payload.pattern, payload.relative_path, payload.max_matches, payload.context_lines, payload.regex)
    PreWriteGateService().record_repo_inspection(project_path=payload.project_path, relative_path=payload.relative_path, client_name=payload.client_name, action="repo.search", is_content_read=False)
    return result


@router.post("/write", response_model=FileContent)
def repo_write(payload: RepoWriteRequest) -> FileContent:
    _require_explicit_client_when_multiclient(payload, "repo.write")
    PreWriteGateService().ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=[payload.relative_path], client_name=payload.client_name, action="repo.write")
    approval = ApprovalService()
    approval.ensure_allowed("repo.write", payload.approval_id, _preview(payload))
    try:
        result = RepoService().write_file(payload.project_path, payload.relative_path, payload.content, payload.create_dirs, chunk_index=payload.chunk_index, total_chunks=payload.total_chunks, mode=payload.mode, force=payload.force)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.write failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.write succeeded")
    JsonStore().log_audit("repo.write", "ok", payload.relative_path, _preview(payload))
    return result


@router.post("/delete", response_model=dict[str, str])
def repo_delete(payload: RepoDeleteRequest) -> dict[str, str]:
    _require_explicit_client_when_multiclient(payload, "repo.delete")
    approval = ApprovalService()
    approval.ensure_allowed("repo.delete", payload.approval_id, _preview(payload))
    try:
        result = RepoService().delete_file(payload.project_path, payload.relative_path)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.delete failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.delete succeeded")
    JsonStore().log_audit("repo.delete", "ok", payload.relative_path, _preview(payload))
    return result


@router.post("/apply-patch", response_model=CommandResult)
def repo_apply_patch(payload: RepoPatchRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "repo.apply_patch")
    gate = PreWriteGateService()
    patch_paths = gate.paths_from_patch(payload.patch)
    gate.ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=patch_paths, client_name=payload.client_name, action="repo.apply_patch", check_only=payload.check_only)
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.apply_patch", payload.approval_id, _preview(payload))
    result = RepoService().apply_patch(payload.project_path, payload.patch, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.apply_patch succeeded", "repo.apply_patch failed")
    if not payload.check_only or result.exit_code != 0:
        JsonStore().log_audit("repo.apply_patch", "ok" if result.exit_code == 0 else "failed", "apply", _preview(payload))
    return result






@router.post("/write-many-fast-safe", response_model=CommandResult)
def repo_write_many_fast_safe(payload: RepoWriteManyFastSafeRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "repo.write_many_fast_safe")
    PreWriteGateService().ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=[item.relative_path for item in payload.files] + payload.delete_paths, client_name=payload.client_name, action="repo.write_many_fast_safe", check_only=payload.check_only)
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_fast_safe", payload.approval_id, _preview(payload))
    result = RepoService().write_many_fast_safe(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds, payload.delete_paths)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_fast_safe succeeded", "repo.write_many_fast_safe failed")
    if not payload.check_only or result.exit_code != 0:
        JsonStore().log_audit("repo.write_many_fast_safe", "ok" if result.exit_code == 0 else "failed", "apply", _preview(payload))
    return result


@router.post("/write-many-smart", response_model=CommandResult)
def repo_write_many_smart(payload: RepoWriteManySmartRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "repo.write_many_smart")
    PreWriteGateService().ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=[item.relative_path for item in payload.files] + payload.delete_paths, client_name=payload.client_name, action="repo.write_many_smart", check_only=payload.check_only)
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_smart", payload.approval_id, _preview(payload))
    result = RepoService().write_many_smart(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds, payload.strategy, payload.delete_paths)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_smart succeeded", "repo.write_many_smart failed")
    if not payload.check_only or result.exit_code != 0:
        JsonStore().log_audit("repo.write_many_smart", "ok" if result.exit_code == 0 else "failed", payload.strategy, _preview(payload))
    return result


@router.post("/write-many-atomic", response_model=CommandResult)
def repo_write_many_atomic(payload: RepoWriteManyAtomicRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "repo.write_many_atomic")
    PreWriteGateService().ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=[item.relative_path for item in payload.files] + payload.delete_paths, client_name=payload.client_name, action="repo.write_many_atomic", check_only=payload.check_only)
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_atomic", payload.approval_id, _preview(payload))
    result = RepoService().write_many_atomic(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds, payload.delete_paths)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_atomic succeeded", "repo.write_many_atomic failed")
    if not payload.check_only or result.exit_code != 0:
        JsonStore().log_audit("repo.write_many_atomic", "ok" if result.exit_code == 0 else "failed", "apply", _preview(payload))
    return result


@router.post("/apply-patch-atomic", response_model=CommandResult)
def repo_apply_patch_atomic(payload: RepoAtomicPatchRequest) -> CommandResult:
    _require_explicit_client_when_multiclient(payload, "repo.apply_patch_atomic")
    gate = PreWriteGateService()
    patch_paths = gate.paths_from_patch(payload.patch)
    gate.ensure_repo_write_allowed(project_path=payload.project_path, relative_paths=patch_paths, client_name=payload.client_name, action="repo.apply_patch_atomic", check_only=payload.check_only)
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.apply_patch_atomic", payload.approval_id, _preview(payload))
    result = RepoService().apply_patch_atomic(payload.project_path, payload.patch, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.apply_patch_atomic succeeded", "repo.apply_patch_atomic failed")
    if not payload.check_only or result.exit_code != 0:
        JsonStore().log_audit("repo.apply_patch_atomic", "ok" if result.exit_code == 0 else "failed", "apply", _preview(payload))
    return result


@router.post("/clean-pyc", response_model=RepoCleanPycResult)
def repo_clean_pyc(payload: RepoCleanPycRequest) -> RepoCleanPycResult:
    _require_explicit_client_when_multiclient(payload, "repo.clean_pyc")
    approval = ApprovalService()
    approval.ensure_allowed("repo.clean_pyc", payload.approval_id, _preview(payload))
    try:
        result = RepoService().clean_pyc(payload.project_path, payload.relative_path)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.clean_pyc failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.clean_pyc succeeded")
    JsonStore().log_audit("repo.clean_pyc", "ok", payload.relative_path, _preview(payload))
    return result
