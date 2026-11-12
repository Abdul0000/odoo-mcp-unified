import asyncio
import os
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from odoo_agent_server.routers import tools as tools_router
from odoo_agent_server.services import live_assertion_service
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.routers.tools import (
    _build_safe_generic_autopatch,
    _build_validated_write_contract,
    _analysis_index_target_paths,
    _contract_candidate_paths,
    _complete_code_batch,
    _code_batch_fingerprint,
    _code_change_continuation_arguments,
    _code_operation_id,
    _debug_infer_diagnosis,
    _debug_operation_handoff,
    _debug_requires_runtime_loop,
    _detect_debug_surface,
    _redact_debug_text,
    _exact_write_context,
    _exact_custom_targets_are_self_contained,
    _exact_model_target_cluster,
    _new_model_scaffold_is_self_contained,
    _new_model_scaffold_target_paths,
    _focused_search,
    _infer_code_change_search_patterns,
    _inline_write_contexts,
    _rank_core_directory_files,
    _rank_core_required_paths,
    _resolve_fuzzy_module,
    _resolve_module_from_source_evidence,
    _prepare_code_batch,
    _restore_coding_session_task,
    _save_code_operation_handoff,
    _task_description_search_clues,
    _target_edit_policy_rule,
    _validate_contract_write_paths,
    _validated_session_write_contract,
    _safe_unique_python_function_rebase,
    _safe_module_relative,
    _write_generation_output_schema,
)
from odoo_agent_server.config import settings
from odoo_agent_server.services.coding_session_service import CodingSessionService, CodingWorkspaceService
from odoo_agent_server.services.continuation_token_service import ContinuationTokenError, ContinuationTokenService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.verification_proof_service import VerificationProofService
from odoo_agent_server.services.workflow_progress_service import WorkflowProgressService
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers.workflow import _normalise_safe_deploy_paths
from odoo_agent_server.schemas import (
    JobRecord,
    JobStatus,
    CommandResult,
    DeployedModuleVerifyRequest,
    OdooCodeChangeRequest,
    OdooCodeFileChange,
    ContinuationResumeRequest,
    OdooDebugAndFixRequest,
    OdooFailureDiagnosis,
    OdooDebugRecordAssertion,
    OdooDebugReportReplay,
    OdooDebugRuntimeReplay,
    OdooResult,
    ModuleAnalyzeRequest,
    OdooTargetResolutionHint,
    OdooTargetResolutionResult,
)


def _payload(description: str, **overrides) -> OdooCodeChangeRequest:
    return OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description=description,
        auto_deploy=False,
        **overrides,
    )


class _DebugStore:
    def __init__(self):
        self.states = {}
        self.jobs = {}

    def load_session_state(self, client_name=None):
        return dict(self.states.get(client_name) or {})

    def save_session_state(self, state, client_name=None):
        current = dict(self.states.get(client_name) or {})
        current.update(state)
        self.states[client_name] = current
        return current

    def get_job(self, job_id):
        return self.jobs[job_id]

    def wait_job(self, job_id, timeout_seconds=600, poll_interval_seconds=1.0):
        return self.get_job(job_id)

    def list_jobs(self, limit=100):
        return list(self.jobs.values())[-limit:][::-1]


def _debug_payload(issue: str, **overrides) -> OdooDebugAndFixRequest:
    return OdooDebugAndFixRequest(
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        issue_description=issue,
        expected_result="The exact scenario completes and saves the expected record.",
        **overrides,
    )


def test_debug_diagnosis_uses_trusted_ledger_not_user_narrative():
    unrelated = {
        "id": "b" * 32,
        "module_name": "custom_sale",
        "issue_description": "Traceback in /custom_sale/models/order.py",
        "evidence_ledger": [
            {
                "id": "evidence-unrelated-001",
                "trusted": True,
                "summary": "Focused source inspection completed without a runtime traceback.",
            }
        ],
    }
    assert _debug_infer_diagnosis(unrelated).origin == "unknown"

    trusted_traceback = {
        **unrelated,
        "evidence_ledger": [
            {
                "id": "evidence-traceback-001",
                "trusted": True,
                "summary": "Traceback: File /srv/addons/custom_sale/models/order.py, line 42",
            }
        ],
    }
    assert _debug_infer_diagnosis(trusted_traceback).origin == "custom_code"

    claimed = OdooFailureDiagnosis(
        origin="custom_code",
        crux="Claimed unrelated custom failure.",
        hypothesis="Claimed unrelated repair hypothesis.",
        evidence_ids=["evidence-unrelated-001"],
        debug_session_id="b" * 32,
        next_strategy="repair_custom_code",
    )
    assert tools_router._debug_resolve_diagnosis(unrelated, claimed).origin == "unknown"


def test_code_operation_id_uses_the_complete_task_description():
    prefix = "A" * 2000
    first = _payload(prefix + " first distinct ending")
    second = _payload(prefix + " second distinct ending")
    arguments = {
        "client_name": "Test 18",
        "project_path": "C:/repo",
        "module_name": "custom_sale",
    }

    assert _code_operation_id(first, **arguments) != _code_operation_id(second, **arguments)


def test_code_change_continuation_preserves_coding_session():
    payload = _payload(
        "Continue the exact inspected change",
        coding_session_id="codeop-session-001",
        workspace_session_id="workspace-session-001",
    )

    arguments = _code_change_continuation_arguments(
        payload,
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        preserve_writes=False,
    )

    assert arguments["coding_session_id"] == "codeop-session-001"
    assert arguments["workspace_session_id"] == "workspace-session-001"


def test_coding_session_restores_canonical_task_on_provider_paraphrase():
    payload = _payload(
        "Refined implementation wording for the same inspected change",
        coding_session_id="codeop-session-001",
    )

    restored, changed = _restore_coding_session_task(
        payload,
        {"task_description": "Continue the exact inspected change"},
    )

    assert changed is True
    assert restored.task_description == "Continue the exact inspected change"
    assert restored.coding_session_id == "codeop-session-001"


@pytest.mark.asyncio
async def test_code_change_wrapper_keeps_event_loop_responsive(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    async def blocking_impl(_payload):
        time.sleep(0.15)
        return {"status": "ok"}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change_impl", blocking_impl)
    started = time.perf_counter()
    task = asyncio.create_task(tools_router._workflow_odoo_code_change(_payload("Small code change")))
    await asyncio.sleep(0.02)
    heartbeat_elapsed = time.perf_counter() - started
    result = await task

    assert heartbeat_elapsed < 0.10
    assert result["status"] == "ok"
    assert result["orchestration_metrics"]["queue_wait_ms"] >= 0
    assert result["workflow_progress"]["terminal"] is True
    assert result["workflow_progress"]["current_phase"] == "completed"
    recent = WorkflowProgressService.summary()["recent"]
    assert recent[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_inspect_only_routes_to_read_only_analyzer_without_durable_job(monkeypatch):
    captured = {}

    def analyze(request):
        captured["request"] = request
        return {"status": "analyzed", "source_evidence": [{"path": "models/order.py"}]}

    monkeypatch.setattr(tools_router, "_workflow_module_analyze", analyze)
    monkeypatch.setattr(
        tools_router,
        "_start_or_reuse_code_change_job",
        lambda _payload: pytest.fail("inspect_only must not start a durable code-change job"),
    )
    payload = _payload(
        "Read the exact custom report implementation",
        patch_strategy="inspect_only",
        intent="inspect",
        target_files=["models/order.py"],
        search_patterns=["Top Ten"],
        wait_for_result=False,
    )

    result = await tools_router._workflow_odoo_code_change(payload)

    assert result["status"] == "analyzed"
    assert result["route"] == "read_only_module_analysis"
    assert result["side_effects"]["files_modified"] is False
    assert captured["request"].target_files == ["models/order.py"]
    assert captured["request"].search_patterns == ["Top Ten"]


def test_human_feature_label_resolves_module_from_unique_source_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "repo_index_enabled", False)
    project = tmp_path / "repo"
    target = project / "feature_reports"
    unrelated = project / "other_custom"
    (target / "views").mkdir(parents=True)
    (unrelated / "views").mkdir(parents=True)
    (target / "__manifest__.py").write_text("{'name': 'Feature Reports'}", encoding="utf-8")
    (unrelated / "__manifest__.py").write_text("{'name': 'Other Custom'}", encoding="utf-8")
    (target / "views" / "top_ten.xml").write_text(
        "<odoo><record id='top_ten' model='ir.ui.view'>"
        "<field name='name'>Custom Top Ten Report</field></record></odoo>",
        encoding="utf-8",
    )
    (unrelated / "views" / "other.xml").write_text(
        "<odoo><record id='other' model='ir.ui.view'><field name='name'>Other</field></record></odoo>",
        encoding="utf-8",
    )
    modules = [
        SimpleNamespace(module_name="feature_reports", path="feature_reports"),
        SimpleNamespace(module_name="other_custom", path="other_custom"),
    ]
    payload = ModuleAnalyzeRequest(
        module_name="custom top ten report",
        analysis_goal="Read the custom Top Ten report",
        search_patterns=["Top Ten"],
    )

    module, resolution, targets = _resolve_module_from_source_evidence(project, modules, payload)

    assert module.module_name == "feature_reports"
    assert resolution["strategy"] == "literal_source_evidence"
    assert targets[0] == "views/top_ten.xml"


def test_human_feature_label_rejects_relationship_only_false_positive(monkeypatch, tmp_path):
    project = tmp_path / "repo"
    first = project / "rethink_bh_connector"
    second = project / "tebra_integration"
    (first / "security").mkdir(parents=True)
    (second / "views").mkdir(parents=True)
    (first / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read\n"
        "access_x,x,model_rethink_appointment,base.group_user,1\n",
        encoding="utf-8",
    )
    (second / "views" / "insurance.xml").write_text(
        "<odoo><record id='insurance' model='ir.ui.view'>"
        "<field name='name'>Insurance Collection</field></record></odoo>",
        encoding="utf-8",
    )
    modules = [
        SimpleNamespace(module_name="rethink_bh_connector", path="rethink_bh_connector"),
        SimpleNamespace(module_name="tebra_integration", path="tebra_integration"),
    ]
    payload = ModuleAnalyzeRequest(
        module_name="custom top ten report",
        analysis_goal="Read the custom Top Ten report",
        search_patterns=["Top Ten", "top_ten"],
    )

    module, resolution, targets = _resolve_module_from_source_evidence(project, modules, payload)

    assert module is None
    assert targets == []
    assert resolution["strategy"] == "source_not_found"
    assert resolution["candidates"] == []


def test_human_feature_label_returns_ambiguous_for_equal_literal_matches(monkeypatch, tmp_path):
    project = tmp_path / "repo"
    modules = []
    for name in ("report_alpha", "report_beta"):
        root = project / name / "views"
        root.mkdir(parents=True)
        (root / "top_ten.xml").write_text(
            "<odoo><record id='top_ten' model='ir.ui.view'>"
            "<field name='name'>Top Ten</field></record></odoo>",
            encoding="utf-8",
        )
        modules.append(SimpleNamespace(module_name=name, path=name))
    payload = ModuleAnalyzeRequest(
        module_name="custom top ten report",
        analysis_goal="Read Top Ten",
        search_patterns=["Top Ten"],
    )

    module, resolution, targets = _resolve_module_from_source_evidence(project, modules, payload)

    assert module is None
    assert targets == []
    assert resolution["strategy"] == "source_ambiguous"
    assert [item["module"] for item in resolution["candidates"]] == ["report_alpha", "report_beta"]


def test_human_feature_label_prefers_model_owner_over_dependent_ui_match(tmp_path):
    project = tmp_path / "repo"
    owner = project / "school_management"
    portal = project / "school_operations_portal"
    (owner / "models").mkdir(parents=True)
    (portal / "views").mkdir(parents=True)
    (owner / "models" / "facilities.py").write_text(
        "from odoo import fields, models\n"
        "class SchoolFacilityRoom(models.Model):\n"
        "    _name = 'school.facility.room'\n"
        "    room_type = fields.Selection([('classroom', 'Classroom')])\n",
        encoding="utf-8",
    )
    (portal / "views" / "facilities.xml").write_text(
        "<odoo><record id='action_school_ops_rooms' model='ir.actions.act_window'>"
        "<field name='res_model'>school.facility.room</field>"
        "<field name='name'>Classroom</field></record></odoo>",
        encoding="utf-8",
    )
    modules = [
        SimpleNamespace(module_name="school_management", path="school_management"),
        SimpleNamespace(module_name="school_operations_portal", path="school_operations_portal"),
    ]
    payload = ModuleAnalyzeRequest(
        module_name="school",
        analysis_goal="Analyze Classroom",
        search_patterns=["Classroom", "school.facility.room", "action_school_ops_rooms"],
    )

    module, resolution, targets = _resolve_module_from_source_evidence(project, modules, payload)

    assert module.module_name == "school_management"
    assert resolution["strategy"] == "literal_source_owner_evidence"
    assert resolution["candidates"][0]["ownership_evidence"]["declared_models"] == [
        "school.facility.room"
    ]
    assert targets == ["models/facilities.py"]


@pytest.mark.asyncio
async def test_provider_code_change_persists_and_reuses_identical_timeout_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "workflow_watchdog_wait_cap_seconds", 0)
    submitted = []
    monkeypatch.setattr(tools_router, "submit_background", submitted.append)
    payload = _payload(
        "Durable provider change",
        operation_id="provider-timeout-001",
        wait_for_result=False,
    )

    first = await tools_router._workflow_odoo_code_change(payload)
    second = await tools_router._workflow_odoo_code_change(payload)

    assert first["status"] == "running"
    assert first["job_id"] == second["job_id"]
    assert first["operation_id"] == "provider-timeout-001"
    assert second["async_job_reused"] is True
    assert len(submitted) == 1
    stored = JsonStore().get_job(first["job_id"])
    assert stored.job_type == "workflow.odoo_code_change"
    assert stored.request["operation_id"] == "provider-timeout-001"
    assert stored.completion_required is True


def test_provider_code_change_recovers_expired_foreign_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    submitted = []
    monkeypatch.setattr(tools_router, "submit_background", submitted.append)
    payload = _payload(
        "Recover durable provider change",
        operation_id="provider-orphan-001",
        wait_for_result=False,
    )
    fingerprint = tools_router._code_change_async_fingerprint(payload)
    job_id = f"codejob-{fingerprint[:40]}"
    resume_arguments = payload.model_dump(mode="json", exclude_none=True)
    JsonStore().create_job(JobRecord(
        id=job_id,
        job_type="workflow.odoo_code_change",
        status=JobStatus.running,
        created_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        request={
            "operation_id": "provider-orphan-001",
            "owner_instance_id": "dead-connector-instance",
            "resume_arguments": resume_arguments,
        },
    ))

    recovered = tools_router._recover_code_change_job_if_orphaned(
        JsonStore().get_job(job_id),
        JsonStore(),
    )

    assert recovered.status == JobStatus.running
    assert recovered.request["owner_instance_id"] == tools_router._CODING_WORKFLOW_INSTANCE_ID
    assert len(submitted) == 1


def test_provider_code_change_preserves_foreign_worker_with_fresh_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    submitted = []
    monkeypatch.setattr(tools_router, "submit_background", submitted.append)
    payload = _payload(
        "Reuse live provider change",
        operation_id="provider-live-worker-001",
        wait_for_result=False,
    )
    fingerprint = tools_router._code_change_async_fingerprint(payload)
    job_id = f"codejob-{fingerprint[:40]}"
    store = JsonStore()
    store.create_job(JobRecord(
        id=job_id,
        job_type="workflow.odoo_code_change",
        status=JobStatus.running,
        created_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        request={
            "operation_id": "provider-live-worker-001",
            "owner_instance_id": "other-live-instance",
            "resume_arguments": payload.model_dump(mode="json", exclude_none=True),
        },
    ))
    store.update_workflow_progress("provider-live-worker-001", {
        "worker_heartbeat_at": datetime.now(timezone.utc).isoformat(),
    })

    result = tools_router._start_or_reuse_code_change_job(payload)

    assert result["status"] == "running"
    assert result["async_job_reused"] is True
    assert submitted == []


def test_provider_code_change_reports_background_submission_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))

    def reject_submission(_callback):
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(tools_router, "submit_background", reject_submission)
    result = tools_router._start_or_reuse_code_change_job(_payload(
        "Submission failure",
        operation_id="provider-submit-failure-001",
        wait_for_result=False,
    ))

    assert result["status"] == "operation_not_started"
    assert result["success_allowed"] is False
    assert "executor unavailable" in result["error"]


def test_client_carried_resume_token_is_signed_bounded_and_safe(monkeypatch):
    monkeypatch.setattr(settings, "continuation_token_secret", "stable-render-secret")
    monkeypatch.setattr(settings, "continuation_token_ttl_seconds", 7200)
    payload = _payload(
        "Update the billing form Authorization: Bearer private-token",
        writes=[OdooCodeFileChange(relative_path="models/order.py", content="secret source")],
        coding_session_id="coding-session-001",
        workspace_session_id="workspace-session-001",
        verification_override="force",
        verification_override_reason="Explicit test authorization",
        verification_override_confirmation="I AUTHORIZE FORCE PUSH AND DEPLOY",
    )

    issued = ContinuationTokenService().issue_code_change(payload, now=1_000)
    decoded = ContinuationTokenService().decode_code_change(issued["resume_token"], now=1_001)

    assert len(issued["resume_token"]) < 20_000
    assert decoded["task_description"].endswith("Authorization: <redacted>")
    assert "private-token" not in decoded["task_description"]
    assert decoded["verification_override"] == "strict"
    assert decoded["wait_for_result"] is False
    assert "writes" not in decoded
    assert "coding_session_id" not in decoded
    assert "workspace_session_id" not in decoded
    assert "verification_override_confirmation" not in decoded


def test_client_carried_resume_token_rejects_tampering_and_expiry(monkeypatch):
    monkeypatch.setattr(settings, "continuation_token_secret", "stable-render-secret")
    monkeypatch.setattr(settings, "continuation_token_ttl_seconds", 3600)
    service = ContinuationTokenService()
    token = service.issue_code_change(_payload("Persist this task"), now=1_000)["resume_token"]
    replacement = "A" if token[-1] != "A" else "B"

    with pytest.raises(ContinuationTokenError, match="signature"):
        service.decode_code_change(token[:-1] + replacement, now=1_001)
    with pytest.raises(ContinuationTokenError, match="expired"):
        service.decode_code_change(token, now=5_000)


def test_client_carried_resume_token_requires_stable_secret(monkeypatch):
    monkeypatch.setattr(settings, "continuation_token_secret", None)
    monkeypatch.setattr(settings, "oauth_token_secret", None)
    monkeypatch.setattr(settings, "api_token", "change-me")

    with pytest.raises(ContinuationTokenError, match="stable"):
        ContinuationTokenService().issue_code_change(_payload("Persist safely"), now=1_000)


@pytest.mark.asyncio
async def test_client_token_resumes_after_ephemeral_state_loss(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "continuation_token_secret", "stable-render-secret")
    original_state = tmp_path / "render-instance-a"
    restarted_state = tmp_path / "render-instance-b"
    monkeypatch.setattr(settings, "state_dir", str(original_state))
    token = ContinuationTokenService().issue_code_change(
        _payload("Resume after Render restart", wait_for_result=False)
    )["resume_token"]
    monkeypatch.setattr(settings, "state_dir", str(restarted_state))
    captured = {}

    async def resumed(payload):
        captured["payload"] = payload
        return {"status": "running", "completion_required": True}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", resumed)
    result = await tools_router._workflow_resume_continuation(
        ContinuationResumeRequest(resume_token=token)
    )

    assert not restarted_state.exists()
    assert captured["payload"].task_description == "Resume after Render restart"
    assert captured["payload"].coding_session_id is None
    assert captured["payload"].workspace_session_id is None
    assert captured["payload"].wait_for_result is False
    assert result["resumed_from_client_token"] is True


@pytest.mark.asyncio
async def test_jobs_get_returns_client_resume_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "continuation_token_secret", "stable-render-secret")
    payload = _payload(
        "Return client resume token",
        operation_id="provider-resume-token-001",
        wait_for_result=False,
    )
    fingerprint = tools_router._code_change_async_fingerprint(payload)
    job_id = f"codejob-{fingerprint[:40]}"
    JsonStore().create_job(JobRecord(
        id=job_id,
        job_type="workflow.odoo_code_change",
        status=JobStatus.running,
        request={
            "operation_id": payload.operation_id,
            "owner_instance_id": tools_router._CODING_WORKFLOW_INSTANCE_ID,
            "resume_arguments": payload.model_dump(mode="json", exclude_none=True),
        },
    ))

    response = await tools_router._dispatch("jobs.get", {
        "job_id": job_id,
        "wait_for_result": False,
    })

    assert response["resume_token_available"] is True
    assert response["resume_token"]
    assert response["resume_next_call"]["tool"] == "workflow.resume_continuation"


@pytest.mark.asyncio
async def test_jobs_get_migrates_legacy_orphan_to_actionable_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    store.create_job(JobRecord(
        id="legacy-codejob-001",
        job_type="workflow.odoo_code_change",
        status=JobStatus.running,
        created_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        request={
            "operation_id": "legacy-operation-001",
            "client_name": "Test 18",
            "project_path": "C:/repo",
            "module_name": "custom_sale",
        },
    ))

    response = await tools_router._dispatch("jobs.get", {
        "job_id": "legacy-codejob-001",
        "wait_for_result": False,
    })

    assert response["state_class"] == "action_required"
    assert response["recovery_required"] is True
    assert response["completion_required"] is True
    assert response["next_call"]["tool"] == "workflow.odoo_code_change"
    assert response["next_call"]["required_generated_arguments"] == ["task_description"]


@pytest.mark.asyncio
async def test_provider_code_change_returns_short_completed_work_without_polling(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "workflow_watchdog_wait_cap_seconds", 2)

    async def quick_impl(_payload):
        return {"status": "needs_writes", "completion_required": True, "success_allowed": False,
                "next_call": {"tool": "workflow.odoo_code_change", "arguments": {}}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change_impl", quick_impl)
    result = await tools_router._workflow_odoo_code_change(_payload(
        "Quick durable inspection",
        operation_id="provider-quick-001",
        wait_for_result=False,
    ))

    assert result["status"] == "needs_writes"
    assert result["async_job_reused"] is False
    assert result["job_id"].startswith("codejob-")


@pytest.mark.asyncio
async def test_code_change_exposes_live_progress_while_worker_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    started = threading.Event()
    release = threading.Event()

    async def blocking_impl(_payload):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"status": "ok", "orchestration_metrics": {"total_ms": 10, "phases_ms": {}}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change_impl", blocking_impl)
    task = asyncio.create_task(tools_router._workflow_odoo_code_change(_payload("Observed code change")))
    while not started.is_set():
        await asyncio.sleep(0.01)

    progress = WorkflowProgressService.summary()
    assert progress["active_count"] == 1
    assert progress["active"][0]["current_phase"] == "starting"
    assert progress["active"][0]["terminal"] is False

    release.set()
    result = await task
    assert result["workflow_progress"]["terminal"] is True


@pytest.mark.asyncio
async def test_code_change_wrapper_rejects_excess_queue_before_work_starts(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    async def blocking_impl(_payload):
        started.set()
        release.wait(timeout=2)
        return {"status": "ok"}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change_impl", blocking_impl)
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(tools_router, "_CODING_WORKFLOW_ADMISSION", admission)
    monkeypatch.setattr(tools_router, "_CODING_WORKFLOW_ADMISSION_CAPACITY", 1)
    first = asyncio.create_task(tools_router._workflow_odoo_code_change(_payload("First code change")))
    assert await asyncio.to_thread(started.wait, 1)

    busy = await tools_router._workflow_odoo_code_change(_payload("Second code change"))
    release.set()
    completed = await first

    assert completed["status"] == "ok"
    assert busy["status"] == "workflow_busy"
    assert busy["protected_work_started"] is False
    assert busy["retry_after_seconds"] == 2


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_release_capacity_before_worker_finishes(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    async def blocking_impl(_payload):
        started.set()
        release.wait(timeout=2)
        return {"status": "ok"}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change_impl", blocking_impl)
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(tools_router, "_CODING_WORKFLOW_ADMISSION", admission)
    monkeypatch.setattr(tools_router, "_CODING_WORKFLOW_ADMISSION_CAPACITY", 1)
    first = asyncio.create_task(tools_router._workflow_odoo_code_change(_payload("Protected code change")))
    assert await asyncio.to_thread(started.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    busy = await tools_router._workflow_odoo_code_change(_payload("Queued after disconnect"))
    release.set()
    assert await asyncio.to_thread(admission.acquire, timeout=1)
    admission.release()

    assert busy["status"] == "workflow_busy"
    assert busy["protected_work_started"] is False


def test_nested_module_paths_use_resolved_root_without_changing_technical_name(tmp_path):
    project = tmp_path / "repo"
    nested = project / "addons" / "vertical" / "custom_sale"
    nested.mkdir(parents=True)
    path = _safe_module_relative("custom_sale", "models/sale.py", "addons/vertical/custom_sale")

    assert path == "addons/vertical/custom_sale/models/sale.py"
    assert _safe_module_relative("custom_sale", "custom_sale/views/sale.xml", "addons/vertical/custom_sale") == "addons/vertical/custom_sale/views/sale.xml"
    assert _normalise_safe_deploy_paths(
        project,
        "custom_sale",
        [path],
        "addons/vertical/custom_sale",
    ) == [path]

    with pytest.raises(ValueError):
        DeployedModuleVerifyRequest(
            project_path=str(project),
            module_name="custom_sale",
            module_root_rel="../escape",
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "models/../views/x.xml",
        "/tmp/x.py",
        "C:/temp/x.py",
        "backup/custom_sale/models/x.py",
    ],
)
def test_module_relative_path_rejects_ambiguous_or_absolute_inputs(unsafe_path):
    with pytest.raises(HTTPException) as excinfo:
        _safe_module_relative("custom_sale", unsafe_path, "addons/vertical/custom_sale")

    assert excinfo.value.status_code == 400


def test_duplicate_exact_module_names_are_not_silently_selected():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Change the custom sale model",
    )
    modules = [
        SimpleNamespace(module_name="custom_sale", path="addons/custom_sale"),
        SimpleNamespace(module_name="custom_sale", path="vendor/custom_sale"),
    ]

    resolved, details = _resolve_fuzzy_module(payload, modules)

    assert resolved is None
    assert details["strategy"] == "ambiguous_duplicate_module"
    assert [item["path"] for item in details["candidates"]] == [
        "addons/custom_sale", "vendor/custom_sale"
    ]


def test_module_fingerprint_detects_same_size_change_with_preserved_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project, modules = _workspace_repo(tmp_path, module_count=1)
    target = modules["custom_module_1"] / "models" / "record.py"
    service = CodingSessionService()
    before = service._module_fingerprint(modules["custom_module_1"])
    original = target.stat()
    target.write_text("VALUE = 9\n", encoding="utf-8")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert target.stat().st_size == original.st_size
    assert service._module_fingerprint(modules["custom_module_1"]) != before


def test_verification_proof_is_bound_to_exact_working_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project, modules = _workspace_repo(tmp_path, module_count=1)
    module = modules["custom_module_1"]
    rel = "custom_module_1/models/record.py"
    target = project / rel
    target.write_text("VALUE = 2\n", encoding="utf-8")
    service = VerificationProofService()
    proof_id = service.create(
        project=project,
        module_name="custom_module_1",
        module_root_rel="custom_module_1",
        paths=[rel],
        verifier_result={"push_blocked": False},
        accepted=True,
    )
    assert service.validate(
        proof_id,
        project=project,
        module_name="custom_module_1",
        module_root_rel="custom_module_1",
        paths=[rel],
    )
    original = target.stat()
    target.write_text("VALUE = 3\n", encoding="utf-8")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert service.validate(
        proof_id,
        project=project,
        module_name="custom_module_1",
        module_root_rel="custom_module_1",
        paths=[rel],
    ) is None


def _workspace_repo(tmp_path: Path, module_count: int = 6) -> tuple[Path, dict[str, Path]]:
    project = tmp_path / "repo"
    modules: dict[str, Path] = {}
    for index in range(1, module_count + 1):
        name = f"custom_module_{index}"
        module = project / name
        (module / "models").mkdir(parents=True)
        (module / "__manifest__.py").write_text(
            repr({"name": name, "depends": ["base"]}) + "\n",
            encoding="utf-8",
        )
        (module / "models" / "record.py").write_text(
            f"VALUE = {index}\n",
            encoding="utf-8",
        )
        modules[name] = module
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    return project, modules


def test_coding_workspace_loads_modules_on_demand_and_evicts_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "coding_workspace_max_modules", 5)
    project, modules = _workspace_repo(tmp_path)
    service = CodingWorkspaceService()
    workspace = service.open_or_resume(
        workspace_session_id=None,
        client_name="Test 18",
        project_path=str(project),
    )

    for name in list(modules)[:5]:
        activation = service.activate_module(
            workspace["id"], module_name=name, module_path=str(modules[name])
        )
        assert activation["status"] == "loaded_on_demand"
    service.activate_module(
        workspace["id"],
        module_name="custom_module_1",
        module_path=str(modules["custom_module_1"]),
    )
    sixth = service.activate_module(
        workspace["id"],
        module_name="custom_module_6",
        module_path=str(modules["custom_module_6"]),
    )

    cached = sixth["workspace"]["modules"]
    assert len(cached) == 5
    assert sixth["evicted_module"] == "custom_module_2"
    assert "custom_module_1" in cached
    assert "custom_module_6" in cached
    assert len(list(service.root.glob("*.zip"))) == 5


def test_coding_workspace_reuses_analysis_and_invalidates_it_after_write(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project, modules = _workspace_repo(tmp_path, module_count=1)
    module = modules["custom_module_1"]
    service = CodingWorkspaceService()
    workspace = service.open_or_resume(
        workspace_session_id="workspace-analysis-001",
        client_name="Test 18",
        project_path=str(project),
    )
    first = service.activate_module(
        workspace["id"], module_name="custom_module_1", module_path=str(module)
    )
    stored = service.store_analysis(
        workspace["id"],
        module_name="custom_module_1",
        analysis_index={"status": "analyzed", "source_evidence": []},
    )
    cached = service.activate_module(
        workspace["id"], module_name="custom_module_1", module_path=str(module)
    )
    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert cached["module"]["analysis_index"]["status"] == "analyzed"
    assert stored["modules"]["custom_module_1"]["analysis_index"] is not None

    (module / "models" / "record.py").write_text("VALUE = 99\n", encoding="utf-8")
    refreshed = service.refresh_module(
        workspace["id"], module_name="custom_module_1"
    )
    entry = refreshed["modules"]["custom_module_1"]
    assert entry["status"] == "refreshed_after_write"
    assert entry["analysis_index"] is None
    assert entry["bundle_version"] == 2


def test_resumed_session_reuses_verified_fingerprint_for_workspace_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project, modules = _workspace_repo(tmp_path, module_count=1)
    module = modules["custom_module_1"]
    workspace_service = CodingWorkspaceService()
    workspace = workspace_service.open_or_resume(
        workspace_session_id="workspace-scan-reuse-001",
        client_name="Test 18",
        project_path=str(project),
    )
    activation = workspace_service.activate_module(
        workspace["id"], module_name="custom_module_1", module_path=str(module)
    )
    entry = activation["module"]
    session_service = CodingSessionService()
    session_service.open(
        session_id="codeop-scan-reuse-001",
        client_name="Test 18",
        project_path=str(project),
        module_name="custom_module_1",
        module_path=str(module),
        task_description="Reuse exact inspected context",
        target_files=["custom_module_1/models/record.py"],
        evidence={},
        create_bundle=False,
        workspace_session_id=workspace["id"],
        snapshot_metadata={
            "module_fingerprint": entry["module_fingerprint"],
            "bundle": entry["bundle"],
        },
    )

    original = CodingSessionService._module_files
    scans: list[Path] = []

    def counted_files(module_path: Path) -> list[Path]:
        scans.append(module_path)
        return original(module_path)

    monkeypatch.setattr(CodingSessionService, "_module_files", staticmethod(counted_files))
    resumed = session_service.resume(
        "codeop-scan-reuse-001",
        client_name="Test 18",
        project_path=str(project),
        task_description="Reuse exact inspected context",
    )
    reused = workspace_service.activate_module(
        workspace["id"],
        module_name="custom_module_1",
        module_path=str(module),
        validated_fingerprint=resumed["module_fingerprint"],
        validated_base_sha=resumed["base_sha"],
    )

    assert resumed["valid"] is True
    assert reused["cache_hit"] is True
    assert reused["status"] == "cache_hit"
    assert scans == [module]


def test_coding_workspace_syncs_changed_and_unchanged_modules_after_push(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project, modules = _workspace_repo(tmp_path, module_count=2)
    service = CodingWorkspaceService()
    workspace = service.open_or_resume(
        workspace_session_id="workspace-push-sync-001",
        client_name="Test 18",
        project_path=str(project),
    )
    for name, module in modules.items():
        service.activate_module(workspace["id"], module_name=name, module_path=str(module))
        service.store_analysis(
            workspace["id"],
            module_name=name,
            analysis_index={"status": "analyzed", "module_name": name},
        )

    changed_file = modules["custom_module_1"] / "models" / "record.py"
    changed_file.write_text("VALUE = 500\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "change first module"], cwd=project, check=True, capture_output=True)
    pushed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()

    result = service.sync_project_after_push(str(project), pushed_sha)
    current = service.open_or_resume(
        workspace_session_id=workspace["id"],
        client_name="Test 18",
        project_path=str(project),
    )
    first = current["modules"]["custom_module_1"]
    second = current["modules"]["custom_module_2"]
    assert result["workspace_count"] == 1
    assert result["checked_modules"] == 2
    assert result["refreshed_modules"] == ["custom_module_1"]
    assert result["rebound_modules"] == ["custom_module_2"]
    assert first["analysis_index"] is None
    assert second["analysis_index"]["status"] == "analyzed"
    assert first["base_sha"] == second["base_sha"] == pushed_sha


def test_successful_git_push_runs_nonblocking_workspace_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project, _modules = _workspace_repo(tmp_path, module_count=1)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=project, check=True)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    called: dict[str, str] = {}

    def sync_after_push(_service, project_path, pushed_sha=None, changed_paths=None):
        called.update(project_path=project_path, pushed_sha=str(pushed_sha or ""))
        called["changed_paths"] = list(changed_paths or [])
        return {"status": "scheduled", "base_sha": pushed_sha}

    monkeypatch.setattr(CodingWorkspaceService, "schedule_project_sync", sync_after_push)
    git = GitService()
    monkeypatch.setattr(git, "_project", lambda _path: project.resolve())
    monkeypatch.setattr(
        git,
        "validate_target",
        lambda *_args, **_kwargs: CommandResult(
            command=["git", "validate-target"], cwd=str(project), exit_code=0
        ),
    )

    result = git.push(str(project), branch=branch)

    assert result.exit_code == 0
    assert called["project_path"] == str(project.resolve())
    assert called["changed_paths"]
    assert "CODING_WORKSPACE_SYNC=" in result.stdout
    assert called["pushed_sha"]
    assert "CODING_WORKSPACE_SYNC=" in result.stdout


def test_coding_session_is_sha_bound_and_refreshes_module_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    module = project / "custom_sale"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    model = module / "models" / "sale_order.py"
    model.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    service = CodingSessionService()
    opened = service.open(
        session_id="codeop-session-001",
        client_name="Test 18",
        project_path=str(project),
        module_name="custom_sale",
        module_path=str(module),
        task_description="Change the sale order behavior",
        target_files=["custom_sale/models/sale_order.py"],
        evidence={"core_paths": []},
    )
    bundle_path = service.root / opened["bundle"]["name"]
    with zipfile.ZipFile(bundle_path) as archive:
        assert "custom_sale/models/sale_order.py" in archive.namelist()
    assert service.resume(
        "codeop-session-001", client_name="Test 18", project_path=str(project),
        task_description="Change the sale order behavior",
    )["valid"] is True

    paraphrased_task = service.resume(
        "codeop-session-001", client_name="Test 18", project_path=str(project),
        task_description="Refined implementation wording for the same inspected task",
    )
    assert paraphrased_task["valid"] is True
    assert paraphrased_task["resume_reason"] == "snapshot_current_scope_locked"
    assert paraphrased_task["task_description"] == "Change the sale order behavior"

    model.write_text("VALUE = 222\n", encoding="utf-8")
    stale = service.resume(
        "codeop-session-001", client_name="Test 18", project_path=str(project),
        task_description="Change the sale order behavior",
    )
    assert stale["valid"] is False
    assert stale["resume_reason"] == "module_changed"

    refreshed = service.refresh("codeop-session-001", status_value="written")
    assert refreshed["bundle_version"] == 2
    assert service.resume(
        "codeop-session-001", client_name="Test 18", project_path=str(project),
        task_description="Change the sale order behavior",
    )["valid"] is True


def test_coding_session_becomes_stale_when_git_head_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    module = project / "custom_sale"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    service = CodingSessionService()
    service.open(
        session_id="codeop-session-002",
        client_name="Test 18",
        project_path=str(project),
        module_name="custom_sale",
        module_path=str(module),
        task_description="Change the sale order behavior",
        target_files=[],
        evidence={},
    )
    (project / "README.md").write_text("new commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "advance"], cwd=project, check=True, capture_output=True)

    stale = service.resume(
        "codeop-session-002", client_name="Test 18", project_path=str(project),
        task_description="Change the sale order behavior",
    )
    assert stale["valid"] is False
    assert stale["resume_reason"] == "git_head_changed"


def test_coding_session_can_checkpoint_direct_write_without_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    module = project / "custom_sale"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    service = CodingSessionService()
    opened = service.open(
        session_id="codeop-session-003",
        client_name="Test 18",
        project_path=str(project),
        module_name="custom_sale",
        module_path=str(module),
        task_description="Direct deterministic fix",
        target_files=[],
        evidence={},
        create_bundle=False,
    )
    assert opened["bundle"] == {}

    refreshed = service.refresh(
        "codeop-session-003", status_value="written", create_bundle=False
    )
    assert refreshed["bundle"] == {}
    assert refreshed["module_fingerprint"]
    assert service.resume(
        "codeop-session-003", client_name="Test 18", project_path=str(project),
        task_description="Direct deterministic fix",
    )["valid"] is True


def _write_contract_fixture(tmp_path):
    project = tmp_path / "repo"
    module = project / "custom_sale"
    target = module / "models" / "sale_order.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = module / "__manifest__.py"
    manifest.write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    dependency = tmp_path / "core" / "sale" / "models" / "sale_order.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("CORE = 1\n", encoding="utf-8")
    resolution = OdooTargetResolutionResult(
        status="verified",
        mode="existing",
        kind="method",
        identifier="action_confirm",
        source_verified=True,
        reason="Exact custom target verified.",
        edit_policy={
            "ownership": "client_custom",
            "strategy": "direct_edit",
            "direct_edit_paths": [str(target.resolve())],
            "protected_source_paths": [],
            "reason": "Edit the existing custom owner.",
        },
    )
    payload = OdooCodeChangeRequest(
        project_path=str(project),
        module_name="custom_sale",
        task_description="Update existing action_confirm method",
        coding_session_id="codeop-contract-001",
        writes=[OdooCodeFileChange(
            relative_path="custom_sale/models/sale_order.py",
            search="VALUE = 1",
            replace="VALUE = 2",
            count=1,
        )],
        auto_deploy=False,
    )
    contract = _build_validated_write_contract(
        payload=payload,
        project=project,
        module_name="custom_sale",
        module_path="custom_sale",
        manifest_path="custom_sale/__manifest__.py",
        target_resolution=resolution.model_dump(mode="json"),
        module_contract={"required_methods": ["action_confirm"]},
        validator_findings=[],
        custom_reads=[{
            "relative_path": "custom_sale/models/sale_order.py",
            "status": "read",
        }],
        core_reads=[{"path": str(dependency), "status": "read"}],
    )
    session = {
        "module_name": "custom_sale",
        "module_path": str(module.resolve()),
        "status": "ready_to_write",
        "evidence": {"write_contract": contract},
    }
    return project, dependency, resolution, contract, session, payload


def test_validated_write_contract_reuses_target_and_detects_dependency_change(tmp_path):
    project, dependency, resolution, _contract, session, payload = _write_contract_fixture(tmp_path)

    resumed, stale_reason = _validated_session_write_contract(session, payload, project)
    assert stale_reason is None
    assert resumed["target_resolution"]["identifier"] == "action_confirm"
    _validate_contract_write_paths(
        project=project,
        module_root_rel="custom_sale",
        target_resolution=resolution,
        writes=payload.writes,
    )

    dependency.write_text("CORE = 2\n", encoding="utf-8")
    resumed, stale_reason = _validated_session_write_contract(session, payload, project)
    assert resumed is None
    assert stale_reason.startswith("inspected_dependency_changed:")


def test_validated_write_contract_rejects_material_task_change(tmp_path):
    project, _dependency, _resolution, _contract, session, payload = _write_contract_fixture(tmp_path)
    changed = payload.model_copy(update={
        "task_description": "Modify a different method on another model",
    })

    resumed, stale_reason = _validated_session_write_contract(session, changed, project)

    assert resumed is None
    assert stale_reason == "validated_task_contract_changed"


def test_workflow_failure_metadata_separates_checkout_and_transport_failures():
    missing = tools_router._workflow_failure_metadata(
        RuntimeError("Project path not found: /workspace/repos/missing"),
        {"current_phase": "repo_prepare"},
    )
    transport = tools_router._workflow_failure_metadata(
        TimeoutError("SSH connection timed out"),
        {"current_phase": "core_source_read"},
    )

    assert missing == {
        "failure_origin": "missing_context",
        "failure_stage": "repo_prepare",
        "retryable": False,
        "next_strategy": "load_exact_source",
    }
    assert transport == {
        "failure_origin": "provider_transport",
        "failure_stage": "core_source_read",
        "retryable": True,
        "next_strategy": "retry_provider_bounded",
    }


def test_validated_write_contract_rejects_out_of_phase_replay(tmp_path):
    project, _dependency, _resolution, _contract, session, payload = _write_contract_fixture(tmp_path)
    session["status"] = "deploying"

    resumed, stale_reason = _validated_session_write_contract(session, payload, project)

    assert resumed is None
    assert stale_reason == "coding_session_phase_deploying"


def test_validated_write_contract_allows_owned_repair_and_batch_retry(tmp_path):
    project, _dependency, _resolution, _contract, session, payload = _write_contract_fixture(tmp_path)
    session["status"] = "repair_required"

    resumed, stale_reason = _validated_session_write_contract(session, payload, project)
    assert stale_reason is None
    assert resumed is not None

    session["status"] = "written"
    batch_payload = payload.model_copy(update={
        "operation_id": "batch-contract-001",
        "batch_index": 0,
        "total_batches": 2,
    })
    resumed, stale_reason = _validated_session_write_contract(session, batch_payload, project)
    assert stale_reason is None
    assert resumed is not None


def test_write_context_overflow_preserves_complete_units_for_artifact():
    contexts = [
        {"relative_path": f"module/models/file_{index}.py", "content": "x" * 6000}
        for index in range(3)
    ]

    inline, complete = _inline_write_contexts(contexts, max_chars=10_000)

    assert complete is False
    assert inline == [contexts[0]]
    assert contexts[1]["content"] == "x" * 6000


def test_exact_custom_model_and_view_scope_skips_installed_source_fanout(tmp_path):
    project = tmp_path / "repo"
    model = project / "clinic_management" / "models" / "clinic_patient.py"
    view = project / "clinic_management" / "views" / "clinic_patient_views.xml"
    model.parent.mkdir(parents=True)
    view.parent.mkdir(parents=True)
    model.write_text(
        "from odoo import fields, models\n"
        "class ClinicPatient(models.Model):\n"
        "    _name = 'clinic.patient'\n"
        "    _inherit = ['mail.thread', 'mail.activity.mixin']\n"
        "    name = fields.Char()\n",
        encoding="utf-8",
    )
    view.write_text(
        "<odoo><record id=\"clinic_patient_form\" model=\"ir.ui.view\">"
        "<field name=\"model\">clinic.patient</field>"
        "<field name=\"arch\" type=\"xml\"><form><sheet/></form></field>"
        "</record></odoo>",
        encoding="utf-8",
    )

    assert _exact_custom_targets_are_self_contained(
        project=project,
        module_name="clinic_management",
        module_root_rel="clinic_management",
        target_files=[
            "clinic_management/models/clinic_patient.py",
            "clinic_management/views/clinic_patient_views.xml",
        ],
        writes=[],
    ) is True


def test_cached_analysis_keeps_only_strong_prescription_target_cluster():
    analysis = {
        "source_evidence": [
            {
                "path": "models/clinic_patient.py",
                "content": "_name = 'clinic.patient'\nnote = fields.Text(string='Notes')\n",
            },
            {
                "path": "views/clinic_patient_views.xml",
                "content": '<field name="model">clinic.patient</field><field name="note"/>',
            },
            {
                "path": "models/clinic_prescription.py",
                "content": "_name = 'clinic.prescription'\ninstructions = fields.Text()\n",
            },
            {
                "path": "views/clinic_prescription_views.xml",
                "content": '<field name="model">clinic.prescription</field><form string="Prescription"/>',
            },
            {
                "path": "__manifest__.py",
                "content": "'views/clinic_prescription_views.xml'",
            },
        ],
    }
    payload = OdooCodeChangeRequest(
        module_name="clinic_management",
        task_description="Add a Notes notebook page to Prescriptions.",
        search_patterns=["prescription", "prescriptions", "clinic.prescription"],
        task_type="xml_view",
        target_hint="Prescriptions model and form view",
        auto_deploy=False,
    )

    targets = _analysis_index_target_paths(analysis, payload)

    assert targets == [
        "views/clinic_prescription_views.xml",
        "models/clinic_prescription.py",
    ]
    assert not any("patient" in path for path in targets)
    assert "__manifest__.py" not in targets


def test_exact_model_target_cluster_keeps_treatment_model_and_primary_view(tmp_path):
    project = tmp_path / "repo"
    module = project / "clinic_management"
    models = module / "models"
    views = module / "views"
    models.mkdir(parents=True)
    views.mkdir(parents=True)
    (models / "clinic_treatment.py").write_text(
        "from odoo import fields, models\n"
        "class ClinicTreatment(models.Model):\n"
        "    _name = 'clinic.treatment'\n"
        "    name = fields.Char()\n",
        encoding="utf-8",
    )
    (models / "clinic_appointment.py").write_text(
        "# Treatment references should not outrank the exact owner\n",
        encoding="utf-8",
    )
    (views / "clinic_treatment_views.xml").write_text(
        "<odoo><record id=\"view_clinic_treatment_form\" model=\"ir.ui.view\">"
        "<field name=\"model\">clinic.treatment</field>"
        "<field name=\"arch\" type=\"xml\"><form><sheet/></form></field>"
        "</record></odoo>",
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="clinic_management",
        task_description="Add a Notes notebook page to the Treatment form view.",
        task_type="xml_view",
        target_resolution={
            "mode": "new",
            "kind": "field",
            "identifier": "note",
            "model": "clinic.treatment",
            "require_live": False,
        },
    )

    result = _exact_model_target_cluster(
        project,
        "clinic_management",
        payload,
        ["models/clinic_appointment.py"],
    )

    assert result == [
        "models/clinic_treatment.py",
        "views/clinic_treatment_views.xml",
    ]


def test_new_model_scaffold_targets_exact_wiring_and_skips_core_fanout(tmp_path):
    project = tmp_path / "repo"
    module = project / "clinic_management"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "security").mkdir()
    (module / "__manifest__.py").write_text("{'name': 'Clinic'}\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import clinic_patient\n", encoding="utf-8")
    (module / "models" / "clinic_patient.py").write_text(
        "from odoo import models\nclass Patient(models.Model):\n    _name = 'clinic.patient'\n",
        encoding="utf-8",
    )
    (module / "models" / "clinic_doctor.py").write_text(
        "from odoo import models\nclass Doctor(models.Model):\n    _name = 'clinic.doctor'\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n",
        encoding="utf-8",
    )
    (module / "views" / "clinic_menus.xml").write_text(
        '<odoo><menuitem id="menu_clinic_root" name="Clinic"/></odoo>\n',
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="clinic_management",
        task_description=(
            "Add a new clinic.test model with patient_id linked to clinic.patient and "
            "doctor_id linked to clinic.doctor, list and form views, ACL, action, and menu."
        ),
        target_resolution={
            "mode": "new",
            "kind": "model",
            "identifier": "clinic.test",
            "model": "clinic.test",
            "require_live": False,
        },
    )

    targets = _new_model_scaffold_target_paths(project, "clinic_management", payload)

    assert targets == [
        "clinic_management/__manifest__.py",
        "clinic_management/models/__init__.py",
        "clinic_management/security/ir.model.access.csv",
        "clinic_management/views/clinic_menus.xml",
        "clinic_management/models/clinic_test.py",
        "clinic_management/views/clinic_test_views.xml",
    ]
    assert _new_model_scaffold_is_self_contained(
        project=project,
        module_root_rel="clinic_management",
        payload=payload,
        target_files=targets,
    ) is True


def test_exact_core_extension_still_requires_installed_source_scope(tmp_path):
    project = tmp_path / "repo"
    model = project / "custom_sale" / "models" / "sale_order.py"
    model.parent.mkdir(parents=True)
    model.write_text(
        "from odoo import models\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n",
        encoding="utf-8",
    )

    assert _exact_custom_targets_are_self_contained(
        project=project,
        module_name="custom_sale",
        module_root_rel="custom_sale",
        target_files=["custom_sale/models/sale_order.py"],
        writes=[],
    ) is False


@pytest.mark.asyncio
async def test_exact_custom_two_file_workflow_avoids_broad_scope_call(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    module = project / "clinic_management"
    model = module / "models" / "clinic_patient.py"
    view = module / "views" / "clinic_patient_views.xml"
    model.parent.mkdir(parents=True)
    view.parent.mkdir(parents=True)
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (model.parent / "__init__.py").write_text("from . import clinic_patient\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'Clinic', 'version': '18.0.1.0.0', 'depends': ['mail'], "
        "'data': ['views/clinic_patient_views.xml']}\n",
        encoding="utf-8",
    )
    model.write_text(
        "from odoo import fields, models\n"
        "class ClinicPatient(models.Model):\n"
        "    _name = 'clinic.patient'\n"
        "    _inherit = ['mail.thread', 'mail.activity.mixin']\n"
        "    name = fields.Char()\n",
        encoding="utf-8",
    )
    view.write_text(
        "<odoo><record id=\"clinic_patient_form\" model=\"ir.ui.view\">"
        "<field name=\"name\">clinic.patient.form</field>"
        "<field name=\"model\">clinic.patient</field>"
        "<field name=\"arch\" type=\"xml\"><form><sheet/></form></field>"
        "</record></odoo>",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    monkeypatch.setattr(
        tools_router.ClientResolverService,
        "resolve_client",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        tools_router,
        "scope_advice",
        lambda *_args, **_kwargs: pytest.fail("exact self-contained targets called broad scope advice"),
    )

    result = await tools_router._workflow_odoo_code_change_impl(OdooCodeChangeRequest(
        project_path=str(project),
        module_name="clinic_management",
        task_description=(
            "Add a Notes notebook page with a note field. Preserve existing security and push to test_18."
        ),
        task_type="xml_view",
        target_files=[
            "clinic_management/models/clinic_patient.py",
            "clinic_management/views/clinic_patient_views.xml",
        ],
        auto_deploy=False,
    ))

    assert result["status"] == "needs_writes"
    scope_step = next(step for step in result["steps"] if step["step"] == "workflow.scope_advice")
    core_step = next(step for step in result["steps"] if step["step"] == "repo.read_core")
    custom_step = next(step for step in result["steps"] if step["step"] == "repo.read_custom")
    assert scope_step["status"] == "skipped_exact_custom_scope"
    assert core_step["count"] == 0
    assert custom_step["count"] == 3
    assert result["target_resolution"]["identifier"] is None


@pytest.mark.asyncio
async def test_two_pass_write_handoff_does_not_repeat_module_or_target_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    module = project / "custom_sale"
    models = module / "models"
    models.mkdir(parents=True)
    (module / "__manifest__.py").write_text(
        "{'name': 'Custom Sale', 'version': '18.0.1.0.0', 'depends': ['sale']}\n",
        encoding="utf-8",
    )
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (models / "__init__.py").write_text("from . import sale_order\n", encoding="utf-8")
    target = models / "sale_order.py"
    target.write_text(
        "from odoo import models\n\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n\n"
        "    def action_confirm(self):\n"
        "        return super().action_confirm()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    monkeypatch.setattr(
        tools_router.ClientResolverService,
        "resolve_client",
        lambda *_args, **_kwargs: None,
    )

    first = await tools_router._workflow_odoo_code_change_impl(OdooCodeChangeRequest(
        project_path=str(project),
        module_name="custom_sale",
        task_description="Update existing action_confirm method",
        target_hint="action_confirm",
        task_type="python_model",
        target_files=["custom_sale/models/sale_order.py"],
        auto_deploy=False,
    ))
    assert first["status"] == "needs_writes"

    monkeypatch.setattr(
        tools_router.RepoService,
        "list_custom_modules",
        lambda *_args, **_kwargs: pytest.fail("module discovery repeated on validated write handoff"),
    )
    monkeypatch.setattr(
        tools_router.ExistingTargetResolutionService,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail("target resolution repeated on validated write handoff"),
    )
    arguments = dict(first["next_call"]["arguments"])
    arguments["auto_deploy"] = False
    arguments["writes"] = [{
        "relative_path": "custom_sale/models/sale_order.py",
        "search": "        return super().action_confirm()",
        "replace": "        result = super().action_confirm()\n        return result",
        "count": 1,
    }]

    second = await tools_router._workflow_odoo_code_change_impl(
        OdooCodeChangeRequest.model_validate(arguments)
    )

    assert second["status"] in {
        "ready_to_deploy", "verified_not_deployed", "qa_failed", "verification_failed",
    }
    assert "result = super().action_confirm()" in target.read_text(encoding="utf-8")
    assert any(
        step.get("status") == "validated_write_contract"
        for step in second.get("steps") or []
    )


def test_handoff_storage_failure_is_nonblocking():
    class FailingStore:
        def save_operation_handoff(self, *args, **kwargs):
            raise OSError("simulated sidecar failure")

    result = _save_code_operation_handoff(
        FailingStore(),
        payload=_payload("Apply one deterministic field change. " + ("detail " * 1200)),
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        target_files=["custom_sale/models/order.py"],
        status="active",
        current_blocker=None,
        next_action="Continue the owned code workflow.",
    )

    assert result["persistence_status"] == "failed"
    assert result["persistence_error_type"] == "OSError"
    assert len(result["objective"]) == 5000

    debug_handoff = _debug_operation_handoff({
        "id": "c" * 32,
        "operation_id": "dbgop-objective-boundary",
        "issue_description": "issue " * 1500,
        "expected_result": "expected " * 1000,
        "phase": "diagnosing",
    })
    assert len(debug_handoff.objective) == 5000


def _prepare_debug_workflow(monkeypatch):
    store = _DebugStore()
    monkeypatch.setattr(tools_router, "JsonStore", lambda: store)
    monkeypatch.setattr(
        tools_router,
        "_resolve_code_change_project",
        lambda payload: ("Test 18", "C:/repo"),
    )
    monkeypatch.setattr(
        tools_router.ClientGateService,
        "require_validated_client",
        lambda self, client_name, action: None,
    )
    monkeypatch.setattr(
        tools_router,
        "_debug_collect_error_pack",
        lambda client_name, tail_lines: "Traceback: controller failed at custom_sale/controllers/main.py:42",
    )
    return store


def test_debug_router_keeps_small_deterministic_changes_on_fast_path(monkeypatch):
    _prepare_debug_workflow(monkeypatch)

    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Rename the quotation label to Proposal")
    ))

    assert result["status"] == "routed_to_code_change_fast_path"
    assert result["next_call"]["tool"] == "workflow.odoo_code_change"


def test_large_code_batches_are_ordered_and_idempotent():
    store = _DebugStore()
    first = _payload(
        "Implement a large module in two local batches",
        operation_id="large-module-001",
        batch_index=0,
        total_batches=2,
        writes=[OdooCodeFileChange(relative_path="custom_sale/models/a.py", content="VALUE = 1\n")],
    )
    record, fingerprint, replay = _prepare_code_batch(
        store,
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        payload=first,
    )

    assert replay is False
    completed = _complete_code_batch(
        store,
        client_name="Test 18",
        record=record,
        batch_index=0,
        fingerprint=fingerprint,
        written_paths=["custom_sale/models/a.py"],
    )
    assert completed["completed"] == {"0": fingerprint}

    _record, _fingerprint, replay = _prepare_code_batch(
        store,
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        payload=first,
    )
    assert replay is True

    second = first.model_copy(update={
        "batch_index": 1,
        "writes": [OdooCodeFileChange(relative_path="custom_sale/views/a.xml", content="<odoo/>\n")],
    })
    _record, second_fingerprint, replay = _prepare_code_batch(
        store,
        client_name="Test 18",
        project_path="C:/repo",
        module_name="custom_sale",
        payload=second,
    )
    assert replay is False
    assert second_fingerprint == _code_batch_fingerprint(second.writes)


def test_large_code_batches_reject_out_of_order_writes():
    store = _DebugStore()
    out_of_order = _payload(
        "Implement a large module in three local batches",
        operation_id="large-module-002",
        batch_index=1,
        total_batches=3,
        writes=[OdooCodeFileChange(relative_path="custom_sale/models/b.py", content="VALUE = 2\n")],
    )

    with pytest.raises(Exception, match="Out-of-order batch"):
        _prepare_code_batch(
            store,
            client_name="Test 18",
            project_path="C:/repo",
            module_name="custom_sale",
            payload=out_of_order,
        )


def test_debug_workflow_uses_compact_default_error_pack():
    payload = _debug_payload("Controller raises an exception")

    assert payload.tail_lines == 150
    assert payload.wait_for_result is False
    assert payload.wait_timeout_seconds == 30


def test_debug_router_detects_runtime_surfaces_without_api_substring_false_positive():
    controller = _debug_payload("Controller returns HTTP 500", reproduction_steps=["Open /shop/cart"])
    frontend = _debug_payload("Browser console shows an OWL exception")
    simple = _debug_payload("Capitalize the proposal label")
    explicit = _debug_payload("Button does nothing", surface="frontend")

    assert _detect_debug_surface(controller) == "controller"
    assert _detect_debug_surface(frontend) == "frontend"
    assert _debug_requires_runtime_loop(controller) is True
    assert _debug_requires_runtime_loop(explicit) is True
    assert _debug_requires_runtime_loop(simple) is False


def test_debug_evidence_redacts_credentials_and_personal_email():
    value = _redact_debug_text(
        "Authorization: Bearer abc123 password=hunter2 api_key=secret "
        "https://user:pass@example.com user@example.com"
    )

    assert "abc123" not in value
    assert "hunter2" not in value
    assert "api_key=secret" not in value
    assert "user@example.com" not in value
    assert "<redacted>" in value
    assert "<redacted-email>" in value


def test_debug_runtime_session_reuses_code_workflow_and_requests_complete_writes(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)
    captured = {}

    async def fake_code_change(payload):
        captured["payload"] = payload
        return {
            "status": "needs_writes",
            "write_generation": {"contexts": [{"relative_path": "custom_sale/controllers/main.py"}]},
            "structure_findings": [],
        }

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Controller raises an exception when saving")
    ))

    assert result["status"] == "debug_needs_writes"
    assert result["debug_session"]["phase"] == "awaiting_writes"
    assert result["debug_session"]["evidence_count"] >= 2
    assert result["next_call"]["tool"] == "workflow.odoo_debug_and_fix"
    assert result["next_call"]["required_generated_arguments"] == ["writes"]
    assert captured["payload"].investigation_evidence
    assert store.states["Test 18"]["debug_sessions"]


def test_debug_clarification_preserves_writes_and_never_uses_correlation_as_target(monkeypatch):
    _prepare_debug_workflow(monkeypatch)
    captured = []

    async def fake_code_change(payload):
        captured.append(payload)
        if not payload.writes:
            return {
                "status": "needs_writes",
                "write_generation": {"contexts": [{"relative_path": "custom_sale/controllers/main.py"}]},
            }
        if not payload.clarification_answer:
            return {
                "status": "needs_clarification",
                "clarification_gate": {"required": True, "question": "Apply to portal users too?"},
            }
        return {"status": "deploy_started", "job_id": "deploy-after-clarification"}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Controller raises an exception when saving")
    ))
    session_id = start["debug_session"]["id"]
    write = OdooCodeFileChange(
        relative_path="custom_sale/controllers/main.py",
        content="def save():\n    return True\n",
    )
    clarification = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller raises an exception when saving",
            action="continue",
            debug_session_id=session_id,
            writes=[write],
        )
    ))

    assert clarification["status"] == "debug_needs_clarification"
    assert clarification["next_call"]["required_generated_arguments"] == ["clarification_answer"]
    assert clarification["next_call"]["arguments"]["writes"] == [write.model_dump(mode="json")]

    continued = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller raises an exception when saving",
            action="continue",
            debug_session_id=session_id,
            clarification_answer="Yes, include portal users.",
        )
    ))

    assert continued["status"] == "debug_deploying"
    assert captured[-1].writes == [write]
    assert captured[-1].clarification_answer == "Yes, include portal users."
    assert all(payload.target_hint is None for payload in captured)


def test_debug_session_owns_deploy_then_requires_and_accepts_replay_proof(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)
    calls = {"count": 0}

    async def fake_code_change(payload):
        calls["count"] += 1
        if payload.writes:
            return {"status": "deploy_started", "job_id": "deploy-1"}
        return {
            "status": "needs_writes",
            "write_generation": {"contexts": [{"relative_path": "custom_sale/controllers/main.py"}]},
        }

    async def fake_live_assertion(session, assertion, phase):
        return {
            "phase": phase,
            "query_status": "ok",
            "passed": phase == "replay",
            "assertion_fingerprint": "assertion-1",
        }

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    monkeypatch.setattr(tools_router, "_debug_run_live_assertion", fake_live_assertion)
    assertion = OdooDebugRecordAssertion(
        model="custom.sale",
        domain=[["id", "=", 42]],
        fields=["state"],
        expected_values={"state": "done"},
    )
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 while saving",
            verification_assertion=assertion,
        )
    ))
    session_id = start["debug_session"]["id"]
    continued = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 while saving",
            action="continue",
            debug_session_id=session_id,
            writes=[OdooCodeFileChange(
                relative_path="custom_sale/controllers/main.py",
                content="def save():\n    return True\n",
            )],
        )
    ))
    assert continued["status"] == "debug_deploying"
    assert continued["debug_session"]["fix_cycles"] == 1
    assert continued["next_call"]["arguments"]["wait_timeout_seconds"] == 30

    store.jobs["deploy-1"] = JobRecord(
        id="deploy-1",
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"upgrade_module": True},
        result={
            "odoosh_check_commit": {"deployed": True},
            "module_upgrade": {"requested": True, "passed": True},
        },
    )
    awaiting = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 while saving",
            action="poll",
            debug_session_id=session_id,
            job_id="deploy-1",
            wait_for_result=False,
        )
    ))
    assert awaiting["status"] == "debug_awaiting_replay"
    assert awaiting["deploy_confirmed"] is True
    assert awaiting["success_allowed"] is False

    verified = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 while saving",
            action="verify",
            debug_session_id=session_id,
            job_id="deploy-1",
            wait_for_result=False,
            reproduction_passed=True,
            expected_result_verified=True,
            verification_evidence=["POST /save returned 200 and record custom_sale(42) was updated"],
        )
    ))
    assert verified["status"] == "debug_verified"
    assert verified["deploy_confirmed"] is True
    assert verified["success_allowed"] is True
    assert calls["count"] == 2


def test_debug_manual_replay_text_cannot_self_authorize_success(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        if payload.writes:
            return {"status": "deploy_started", "job_id": "deploy-manual"}
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Controller returns 500 during submit")
    ))
    session_id = start["debug_session"]["id"]
    asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 during submit",
            action="continue",
            debug_session_id=session_id,
            writes=[OdooCodeFileChange(
                relative_path="custom_sale/controllers/main.py",
                content="def submit():\n    return True\n",
            )],
        )
    ))
    store.jobs["deploy-manual"] = JobRecord(
        id="deploy-manual",
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"upgrade_module": True},
        result={
            "odoosh_check_commit": {"deployed": True},
            "module_upgrade": {"requested": True, "passed": True},
        },
    )

    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Controller returns 500 during submit",
            action="verify",
            debug_session_id=session_id,
            job_id="deploy-manual",
            wait_for_result=False,
            reproduction_passed=True,
            expected_result_verified=True,
            verification_evidence=["I checked it and it works"],
        )
    ))

    assert result["status"] == "debug_implemented_unverified"
    assert result["success_allowed"] is False
    assert result["truth_gate"]["independent_replay_passed"] is False


def test_debug_start_retry_reuses_operation_without_duplicate_inspection(monkeypatch):
    _prepare_debug_workflow(monkeypatch)
    calls = {"count": 0}

    async def fake_code_change(payload):
        calls["count"] += 1
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    payload = _debug_payload(
        "Cron fails with a traceback",
        operation_id="debug-cron-42",
    )
    first = asyncio.run(tools_router._workflow_odoo_debug_and_fix(payload))
    retried = asyncio.run(tools_router._workflow_odoo_debug_and_fix(payload))

    assert retried["debug_session"]["id"] == first["debug_session"]["id"]
    assert retried["status"] == "debug_awaiting_writes"
    assert calls["count"] == 1


def test_debug_recovers_owned_deploy_job_after_interrupted_response(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Cron fails with a traceback")
    ))
    session_id = start["debug_session"]["id"]
    session = store.states["Test 18"]["debug_sessions"][session_id]
    session["phase"] = "applying_fix"
    session["operation_started_at"] = "2025-01-01T00:00:00+00:00"
    store.jobs["recovered-job"] = JobRecord(
        id="recovered-job",
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={
            "module_name": "custom_sale",
            "project_path": "C:/repo",
            "upgrade_module": True,
        },
    )

    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Cron fails with a traceback",
            action="poll",
            debug_session_id=session_id,
            wait_for_result=False,
        )
    ))

    assert result["status"] == "debug_deploying"
    assert result["job"]["id"] == "recovered-job"
    assert result["debug_session"]["job_id"] == "recovered-job"


def test_debug_surgical_budget_cannot_be_shrunk_below_server_floor(monkeypatch):
    _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Backend fails with a traceback", max_surgical_files=2)
    ))

    assert start["debug_session"]["max_surgical_files"] == 12

    with pytest.raises(Exception, match="at most 12 files"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload(
                "Backend fails with a traceback",
                action="continue",
                debug_session_id=start["debug_session"]["id"],
                writes=[
                    OdooCodeFileChange(relative_path=f"custom_sale/models/f{index}.py", content="x = 1\n")
                    for index in range(13)
                ],
            )
        ))


def test_debug_surgical_scope_rejects_undiagnosed_source_file(monkeypatch):
    _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        return {
            "status": "needs_writes",
            "module_name": "custom_sale",
            "write_generation": {
                "contexts": [{"relative_path": "custom_sale/controllers/main.py"}],
            },
        }

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Controller fails with a traceback")
    ))

    with pytest.raises(Exception, match="undiagnosed paths"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload(
                "Controller fails with a traceback",
                action="continue",
                debug_session_id=start["debug_session"]["id"],
                writes=[OdooCodeFileChange(
                    relative_path="custom_sale/models/unrelated.py",
                    content="x = 1\n",
                )],
            )
        ))


def test_write_generation_rule_matches_target_ownership_strategy():
    assert "directly" in _target_edit_policy_rule("direct_edit")
    assert "duplicate _inherit" in _target_edit_policy_rule("direct_edit")
    assert "Do not modify protected source" in _target_edit_policy_rule("inherit_extension")
    assert "Do not guess target ownership" in _target_edit_policy_rule("clarify")


def test_full_module_write_schema_puts_missing_file_content_shape_first():
    schema = _write_generation_output_schema(has_missing_targets=True)

    assert set(schema[0]) == {"relative_path", "content"}
    assert "missing/new" in schema[0]["relative_path"]
    assert "search" in schema[1]
    assert "existing files only" in schema[1]["search"]


def test_existing_file_write_schema_keeps_surgical_shape_first():
    schema = _write_generation_output_schema(has_missing_targets=False)

    assert "search" in schema[0]
    assert set(schema[1]) == {"relative_path", "content"}


def test_stale_search_rebases_unique_matching_python_function():
    current = (
        "class Attendance:\n"
        "    def action_download_attendance(self):\n"
        "        # deployed implementation differs slightly\n"
        "        punch_type = stored_attendance.punch_type\n"
        "        return punch_type\n"
    )
    stale_search = (
        "    def action_download_attendance(self):\n"
        "        punch_type = attendance.punch_type\n"
        "        return punch_type\n"
    )
    replacement = (
        "    def action_download_attendance(self):\n"
        "        punch_type = raw_attendance.punch\n"
        "        duplicate_atten_ids.write({'punch_type': punch_type})\n"
        "        return punch_type\n"
    )

    rebased, reason = _safe_unique_python_function_rebase(current, stale_search, replacement)

    assert rebased is not None
    assert "raw_attendance.punch" in rebased
    assert "duplicate_atten_ids.write" in rebased
    assert "stored_attendance.punch_type" not in rebased
    assert "rebased unique function" in reason


def test_stale_search_does_not_rebase_ambiguous_python_function():
    current = (
        "class First:\n"
        "    def action_download_attendance(self):\n"
        "        return 0\n"
        "class Second:\n"
        "    def action_download_attendance(self):\n"
        "        return 1\n"
    )
    search = "def action_download_attendance(self):\n    return 2\n"
    replacement = "def action_download_attendance(self):\n    return 3\n"

    rebased, reason = _safe_unique_python_function_rebase(current, search, replacement)

    assert rebased is None
    assert "2 matches" in reason


def test_stale_search_does_not_rebase_low_similarity_function():
    current = (
        "def action_download_attendance(self):\n"
        "    values = self._download_all_devices()\n"
        "    return self._reconcile(values, strict=True)\n"
    )
    search = (
        "def action_download_attendance(self):\n"
        "    punch_type = attendance.punch_type\n"
        "    return punch_type\n"
    )
    replacement = (
        "def action_download_attendance(self):\n"
        "    return raw_attendance.punch\n"
    )

    rebased, reason = _safe_unique_python_function_rebase(current, search, replacement)

    assert rebased is None
    assert "below the safe" in reason


def test_debug_live_assertion_is_evaluated_from_server_read(monkeypatch):
    class FakeOdoo:
        def search_read(self, *args, **kwargs):
            return OdooResult(
                status="ok",
                result=[{"state": "done", "response_id": 99}],
            )

    monkeypatch.setattr(
        tools_router.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        live_assertion_service.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(live_assertion_service, "OdooService", lambda client=None: FakeOdoo())
    session = {
        "correlation_id": "dbg-assertion",
        "client_name": "Test 18",
        "project_path": "C:/repo",
        "evidence_ledger": [],
    }
    assertion = OdooDebugRecordAssertion(
        model="custom.sale",
        domain=[["id", "=", 42]],
        fields=["state", "response_id"],
        expected_values={"state": "done", "response_id": 99},
        expected_count_min=1,
        expected_count_max=1,
        match_mode="any",
    )

    proof = asyncio.run(tools_router._debug_run_live_assertion(
        session,
        assertion.model_dump(mode="json"),
        phase="replay",
    ))

    assert proof["query_status"] == "ok"
    assert proof["passed"] is True
    assert session["evidence_ledger"][0]["trusted"] is True
    assert session["evidence_ledger"][0]["source"] == "odoo_live"


def test_debug_truth_gate_rejects_nondiscriminating_baseline():
    session = {
        "deploy_confirmed": True,
        "verification_assertion": {"model": "custom.sale"},
        "baseline_assertion": {"query_status": "ok", "passed": True},
        "replay_assertion": {"query_status": "ok", "passed": True},
        "reproduction_passed": True,
        "expected_result_verified": True,
        "evidence_ledger": [{
            "kind": "manual_observation",
            "source": "client",
            "passed": True,
        }],
    }

    truth = tools_router._debug_truth_gate(session)

    assert truth["verified_fixed"] is False
    assert truth["discriminating_baseline"] is False
    assert truth["success_allowed"] is False


def test_runtime_replay_requires_explicit_mutation_authorization_and_assertion():
    with pytest.raises(ValueError, match="confirm_mutation"):
        OdooDebugRuntimeReplay(
            model="zk.machine",
            record_ids=[7],
            method="action_download_attendance",
        )

    replay = OdooDebugRuntimeReplay(
        model="zk.machine",
        record_ids=[7],
        method="action_download_attendance",
        confirm_mutation=True,
    )
    with pytest.raises(ValueError, match="verification_assertion"):
        _debug_payload("Download action raises an error", runtime_replay=replay)


def test_report_replay_requires_read_only_discriminating_contract():
    with pytest.raises(ValueError, match="text or page-count"):
        OdooDebugReportReplay(
            report_xml_id="account.action_report_invoice",
            record_ids=[7],
        )

    trigger = OdooDebugRuntimeReplay(
        model="account.report",
        record_ids=[7],
        method="action_print_pdf",
        commit=False,
    )
    replay = OdooDebugReportReplay(
        record_ids=[7],
        trigger=trigger,
        required_text=["Patient"],
        forbidden_text=["Insurance Company", "Grand Total"],
        expected_pages_max=2,
    )

    payload = _debug_payload("Patient PDF contains unrelated sections", report_replay=replay)
    assert payload.report_replay.trigger.commit is False


def test_report_truth_gate_accepts_failed_baseline_and_clean_passing_artifact(tmp_path):
    session = {
        "project_path": str(tmp_path),
        "deploy_confirmed": True,
        "report_replay": {"record_ids": [7]},
        "baseline_report_replay": {
            "action_executed": True,
            "passed": False,
            "fresh_error_count": 0,
        },
        "replay_report_replay": {
            "action_executed": True,
            "passed": True,
            "fresh_error_count": 0,
        },
        "evidence_ledger": [],
    }

    truth = tools_router._debug_truth_gate(session)

    assert truth["verified_fixed"] is True
    assert truth["report_baseline_discriminating"] is True
    assert truth["report_replay_passed"] is True


def test_debug_truth_gate_blocks_remaining_temporary_instrumentation(tmp_path):
    target = tmp_path / "custom_sale" / "models" / "report.py"
    target.parent.mkdir(parents=True)
    target.write_text("_logger.info('ODOO_MCP_DEBUG:dbg-pdf')\n", encoding="utf-8")
    session = {
        "project_path": str(tmp_path),
        "deploy_confirmed": True,
        "report_replay": {"record_ids": [7]},
        "baseline_report_replay": {"action_executed": True, "passed": False},
        "replay_report_replay": {"action_executed": True, "passed": True, "fresh_error_count": 0},
        "instrumentation_ever_used": True,
        "instrumentation_marker": "ODOO_MCP_DEBUG:dbg-pdf",
        "instrumentation_paths": ["custom_sale/models/report.py"],
    }

    blocked = tools_router._debug_truth_gate(session)
    assert blocked["verified_fixed"] is False
    assert blocked["instrumentation_cleanup_passed"] is False

    target.write_text("# functional fix; temporary logger removed\n", encoding="utf-8")
    clean = tools_router._debug_truth_gate(session)
    assert clean["verified_fixed"] is True

    target.unlink()
    missing = tools_router._debug_truth_gate(session)
    assert missing["verified_fixed"] is False
    assert missing["instrumentation_cleanup"]["missing_paths"] == ["custom_sale/models/report.py"]


def test_debug_inspection_uses_module_scope_not_evidence_local_variable(monkeypatch):
    _prepare_debug_workflow(monkeypatch)
    captured = {}

    async def fake_code_change(payload):
        captured["payload"] = payload
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Patient PDF still includes unrelated sections",
            action="start",
            runtime_evidence=["selected_section=False while selected_section appeared in three frames"],
        )
    ))

    assert result["status"] == "debug_needs_writes"
    assert captured["payload"].change_request["scope"] == "module"
    assert captured["payload"].target_hint is None
    assert "selected_section" in captured["payload"].task_description
    assert len(result["debug_session"]["debug_plan"]) == 3


def test_target_resolver_treats_bare_evidence_identifier_as_search_context_only():
    resolver = tools_router.ExistingTargetResolutionService()
    hint = OdooTargetResolutionHint()

    assert resolver._authoritative_identifier(
        hint,
        None,
        "Runtime evidence says selected_section was false in local state.",
        {},
    ) is None
    assert resolver._authoritative_identifier(
        hint,
        None,
        "Fix method: action_print_pdf after source inspection.",
        {},
    ) == "action_print_pdf"


def test_runtime_replay_target_is_immutable_after_debug_start(monkeypatch):
    _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        return {"status": "needs_writes", "write_generation": {"contexts": []}}

    async def failed_runtime_replay(session, phase):
        return {
            "phase": phase,
            "status": "failed",
            "action_executed": True,
            "passed": False,
            "fresh_error_count": 1,
        }

    async def failed_assertion(session, assertion, phase):
        return {"phase": phase, "query_status": "ok", "passed": False}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    monkeypatch.setattr(tools_router, "_debug_run_runtime_replay", failed_runtime_replay)
    monkeypatch.setattr(tools_router, "_debug_run_live_assertion", failed_assertion)
    assertion = OdooDebugRecordAssertion(
        model="hr.attendance",
        fields=["id"],
        expected_count_min=1,
    )
    original = OdooDebugRuntimeReplay(
        model="zk.machine",
        record_ids=[7],
        method="action_download_attendance",
        confirm_mutation=True,
    )
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises an error",
            verification_assertion=assertion,
            runtime_replay=original,
        )
    ))
    changed = original.model_copy(update={"record_ids": [8]})

    with pytest.raises(Exception, match="runtime_replay is immutable"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload(
                "Download attendance raises an error",
                action="continue",
                debug_session_id=start["debug_session"]["id"],
                verification_assertion=assertion,
                runtime_replay=changed,
            )
        ))


def test_odoosh_runtime_replay_parses_trusted_action_result(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src/odoo")
    monkeypatch.setattr(service, "_addons_path_shell_setup", lambda: "")

    def fake_run_shell(script, timeout_seconds=60):
        assert "action_download_attendance" not in script
        assert "MCP_RUNTIME_REPLAY_JSON:" in script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout=(
                "MCP_RUNTIME_REPLAY_BEGIN:dbg-attendance\n"
                "MCP_RUNTIME_REPLAY_JSON:"
                '{"status":"ok","action_executed":true,"committed":true,"returned":true}\n'
                "MCP_RUNTIME_REPLAY_END:dbg-attendance\n"
            ),
        )

    monkeypatch.setattr(service, "run_shell", fake_run_shell)
    result = service.runtime_replay(
        {
            "model": "zk.machine",
            "record_ids": [7],
            "method": "action_download_attendance",
            "confirm_mutation": True,
            "commit": True,
        },
        correlation_id="dbg-attendance",
    )

    assert result.status == "ok"
    assert result.result["passed"] is True
    assert result.result["fresh_error_count"] == 0
    assert result.result["correlation_id"] == "dbg-attendance"


def test_odoosh_runtime_replay_rejects_fresh_error_even_when_method_returns(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src/odoo")
    monkeypatch.setattr(service, "_addons_path_shell_setup", lambda: "")
    monkeypatch.setattr(
        service,
        "run_shell",
        lambda script, timeout_seconds=60: CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout=(
                "MCP_RUNTIME_REPLAY_BEGIN:dbg-errors\n"
                "2026-07-30 12:00:00,000 ERROR test_db odoo.addons.custom: device sync failed\n"
                "MCP_RUNTIME_REPLAY_JSON:"
                '{"status":"ok","action_executed":true,"committed":true,"returned":true}\n'
                "MCP_RUNTIME_REPLAY_END:dbg-errors\n"
            ),
        ),
    )

    result = service.runtime_replay(
        {
            "model": "zk.machine",
            "record_ids": [7],
            "method": "action_download_attendance",
            "confirm_mutation": True,
            "commit": True,
        },
        correlation_id="dbg-errors",
    )

    assert result.status == "failed"
    assert result.result["passed"] is False
    assert result.result["fresh_error_count"] == 1


def test_odoosh_report_replay_returns_bounded_artifact_proof(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src/odoo")
    monkeypatch.setattr(service, "_addons_path_shell_setup", lambda: "")
    captured = {}

    def fake_run_shell(script, timeout_seconds=60):
        captured["script"] = script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout=(
                "MCP_REPORT_REPLAY_BEGIN:dbg-pdf\n"
                "MCP_REPORT_REPLAY_JSON:"
                '{"status":"ok","action_executed":true,"committed":false,'
                '"page_count":2,"pdf_bytes":2048,"pdf_sha256":"abc",'
                '"required_text":{"Patient":true},'
                '"forbidden_text":{"Insurance Company":true,"Grand Total":true},'
                '"page_count_ok":true,"artifact_assertion_passed":true}\n'
            ),
        )

    monkeypatch.setattr(service, "run_shell", fake_run_shell)
    monkeypatch.setattr(
        service,
        "correlated_logs",
        lambda correlation_id, tail_lines=150: CommandResult(
            command=["odoosh.correlated_logs"], cwd="odoosh:ssh", exit_code=0,
            stdout="2026-08-14 10:00:00 ERROR old unrelated request failed",
        ),
    )

    result = service.report_replay(
        {
            "report_xml_id": "custom_sale.patient_report",
            "record_ids": [7],
            "required_text": ["Patient"],
            "forbidden_text": ["Insurance Company", "Grand Total"],
            "expected_pages_max": 2,
        },
        correlation_id="dbg-pdf",
    )

    assert result.status == "ok"
    assert result.result["passed"] is True
    assert result.result["page_count"] == 2
    assert "text" not in result.result
    assert "finally:\n    env.cr.rollback()" in captured["script"]


def test_odoosh_report_replay_rejects_error_with_exact_correlation_marker(monkeypatch):
    service = OdooShService()
    monkeypatch.setattr(service, "_db_name", lambda: "test_db")
    monkeypatch.setattr(service, "_odoo_path", lambda: "/home/odoo/src/odoo")
    monkeypatch.setattr(service, "_addons_path_shell_setup", lambda: "")
    monkeypatch.setattr(
        service,
        "run_shell",
        lambda script, timeout_seconds=60: CommandResult(
            command=["ssh"], cwd="odoosh:ssh", exit_code=0,
            stdout=(
                "MCP_REPORT_REPLAY_BEGIN:dbg-pdf\n"
                "MCP_REPORT_REPLAY_JSON:"
                '{"status":"ok","action_executed":true,"committed":false,'
                '"page_count":1,"pdf_bytes":100,"pdf_sha256":"abc",'
                '"artifact_assertion_passed":true}\n'
            ),
        ),
    )
    monkeypatch.setattr(
        service,
        "correlated_logs",
        lambda correlation_id, tail_lines=150: CommandResult(
            command=["odoosh.correlated_logs"], cwd="odoosh:ssh", exit_code=0,
            stdout="2026-08-14 10:01:00 ERROR ODOO_MCP_DEBUG:dbg-pdf composite mismatch",
        ),
    )

    result = service.report_replay(
        {
            "report_xml_id": "custom_sale.patient_report",
            "record_ids": [7],
            "required_text": ["Patient"],
        },
        correlation_id="dbg-pdf",
    )

    assert result.status == "failed"
    assert result.result["fresh_error_count"] == 1


def test_runtime_truth_gate_accepts_trusted_replay_without_manual_claim():
    session = {
        "deploy_confirmed": True,
        "runtime_replay": {"model": "zk.machine"},
        "verification_assertion": {"model": "hr.attendance"},
        "baseline_runtime_replay": {
            "action_executed": True,
            "passed": False,
            "fresh_error_count": 1,
        },
        "replay_runtime_replay": {
            "action_executed": True,
            "passed": True,
            "fresh_error_count": 0,
        },
        "baseline_assertion": {"query_status": "ok", "passed": False},
        "replay_assertion": {"query_status": "ok", "passed": True},
        "evidence_ledger": [],
    }

    truth = tools_router._debug_truth_gate(session)

    assert truth["verified_fixed"] is True
    assert truth["runtime_replay_passed"] is True
    assert truth["manual_replay_passed"] is False
    assert truth["success_allowed"] is True


def test_debug_deploy_automatically_replays_ssh_action_and_assertion(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        if payload.writes:
            return {"status": "deploy_started", "job_id": "deploy-runtime"}
        return {
            "status": "needs_writes",
            "write_generation": {"contexts": [{"relative_path": "custom_sale/models/machine.py"}]},
        }

    async def fake_runtime_replay(session, phase):
        return {
            "phase": phase,
            "status": "failed" if phase == "baseline" else "ok",
            "action_executed": True,
            "passed": phase == "replay",
            "fresh_error_count": 1 if phase == "baseline" else 0,
            "fresh_errors": ["ValidationError"] if phase == "baseline" else [],
        }

    async def fake_live_assertion(session, assertion, phase):
        return {
            "phase": phase,
            "query_status": "ok",
            "passed": phase == "replay",
            "assertion_fingerprint": "attendance-assertion",
        }

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    monkeypatch.setattr(tools_router, "_debug_run_runtime_replay", fake_runtime_replay)
    monkeypatch.setattr(tools_router, "_debug_run_live_assertion", fake_live_assertion)
    assertion = OdooDebugRecordAssertion(
        model="hr.attendance",
        domain=[["employee_id", "=", 11]],
        fields=["check_out"],
        expected_count_min=1,
    )
    replay = OdooDebugRuntimeReplay(
        model="zk.machine",
        record_ids=[7],
        method="action_download_attendance",
        confirm_mutation=True,
    )
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises a ValidationError",
            verification_assertion=assertion,
            runtime_replay=replay,
        )
    ))
    session_id = start["debug_session"]["id"]
    asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises a ValidationError",
            action="continue",
            debug_session_id=session_id,
            verification_assertion=assertion,
            runtime_replay=replay,
            writes=[OdooCodeFileChange(
                relative_path="custom_sale/models/machine.py",
                content="VALUE = True\n",
            )],
        )
    ))
    store.jobs["deploy-runtime"] = JobRecord(
        id="deploy-runtime",
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"upgrade_module": True},
        result={
            "odoosh_check_commit": {"deployed": True},
            "module_upgrade": {"requested": True, "passed": True},
        },
    )

    verified = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises a ValidationError",
            action="poll",
            debug_session_id=session_id,
            job_id="deploy-runtime",
            verification_assertion=assertion,
            runtime_replay=replay,
        )
    ))

    assert verified["status"] == "debug_verified"
    assert verified["success_allowed"] is True
    assert verified["truth_gate"]["runtime_replay_passed"] is True
    assert verified["truth_gate"]["manual_replay_passed"] is False


def test_failed_ssh_replay_automatically_reenters_diagnosis(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)
    calls = {"code": 0}

    async def fake_code_change(payload):
        calls["code"] += 1
        return {
            "status": "needs_writes",
            "write_generation": {"contexts": [{"relative_path": "custom_sale/models/machine.py"}]},
        }

    async def failed_runtime_replay(session, phase):
        return {
            "phase": phase,
            "status": "failed",
            "action_executed": True,
            "passed": False,
            "fresh_error_count": 1,
            "fresh_errors": ["ValidationError: overlapping attendance"],
        }

    async def failed_assertion(session, assertion, phase):
        return {"phase": phase, "query_status": "ok", "passed": False}

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    monkeypatch.setattr(tools_router, "_debug_run_runtime_replay", failed_runtime_replay)
    monkeypatch.setattr(tools_router, "_debug_run_live_assertion", failed_assertion)
    assertion = OdooDebugRecordAssertion(
        model="hr.attendance",
        fields=["id"],
        expected_count_min=1,
    )
    replay = OdooDebugRuntimeReplay(
        model="zk.machine",
        record_ids=[7],
        method="action_download_attendance",
        confirm_mutation=True,
    )
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises a ValidationError",
            verification_assertion=assertion,
            runtime_replay=replay,
        )
    ))
    session_id = start["debug_session"]["id"]
    session = store.states["Test 18"]["debug_sessions"][session_id]
    session["phase"] = "deploying"
    session["fix_cycles"] = 1
    session["job_id"] = "deploy-replay-failed"
    store.jobs["deploy-replay-failed"] = JobRecord(
        id="deploy-replay-failed",
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"upgrade_module": True},
        result={
            "odoosh_check_commit": {"deployed": True},
            "module_upgrade": {"requested": True, "passed": True},
        },
    )

    result = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload(
            "Download attendance raises a ValidationError",
            action="poll",
            debug_session_id=session_id,
            job_id="deploy-replay-failed",
            verification_assertion=assertion,
            runtime_replay=replay,
        )
    ))

    assert result["status"] == "debug_needs_writes"
    assert result["success_allowed"] is False
    assert result["debug_session"]["fix_cycles"] == 1
    assert calls["code"] == 2


def test_debug_session_rejects_unowned_job_and_out_of_phase_writes(monkeypatch):
    store = _prepare_debug_workflow(monkeypatch)

    async def fake_code_change(payload):
        return {
            "status": "needs_writes",
            "write_generation": {"contexts": [{"relative_path": "custom_sale/models/sync.py"}]},
        }

    monkeypatch.setattr(tools_router, "_workflow_odoo_code_change", fake_code_change)
    start = asyncio.run(tools_router._workflow_odoo_debug_and_fix(
        _debug_payload("Cron fails with a traceback")
    ))
    session_id = start["debug_session"]["id"]
    session = store.states["Test 18"]["debug_sessions"][session_id]
    session["phase"] = "deploying"
    session["job_id"] = "owned-job"

    with pytest.raises(Exception, match="not owned"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload(
                "Cron fails with a traceback",
                action="poll",
                debug_session_id=session_id,
                job_id="foreign-job",
                wait_for_result=False,
            )
        ))
    with pytest.raises(Exception, match="awaiting_writes"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload(
                "Cron fails with a traceback",
                action="continue",
                debug_session_id=session_id,
                writes=[OdooCodeFileChange(
                    relative_path="custom_sale/models/sync.py",
                    content="def run():\n    return True\n",
                )],
            )
        ))


def test_debug_request_rejects_forged_session_identifiers():
    with pytest.raises(ValueError):
        _debug_payload(
            "Controller returns 500",
            debug_session_id="../../another-session",
        )


def test_debug_assertion_requires_explicit_bounded_expectation():
    with pytest.raises(ValueError, match="requires a count or exact-value"):
        OdooDebugRecordAssertion(model="custom.sale", fields=["state"])
    with pytest.raises(ValueError, match="fields must include"):
        OdooDebugRecordAssertion(
            model="custom.sale",
            fields=["id"],
            expected_values={"state": "done"},
        )


def test_debug_state_machine_requires_session_for_continuation(monkeypatch):
    _prepare_debug_workflow(monkeypatch)

    with pytest.raises(Exception, match="requires debug_session_id"):
        asyncio.run(tools_router._workflow_odoo_debug_and_fix(
            _debug_payload("Controller returns 500", action="poll")
        ))


def test_task_description_extracts_concrete_code_clues_only():
    description = (
        "Fix method `_compute_total()` for XML ID sale.report_saleorder "
        "and selector .o_order_total on route /shop/cart."
    )

    clues = _task_description_search_clues(description)
    patterns = _infer_code_change_search_patterns(_payload(description))

    assert "_compute_total()" in clues
    assert "sale.report_saleorder" in clues
    assert ".o_order_total" in clues
    assert any(value in patterns for value in ("_compute_total()", "_compute_total"))
    assert "sale.report_saleorder" in patterns


def test_focused_search_returns_exact_match_lines_for_range_reads():
    class FakeRepo:
        def search(self, project_path, pattern, module_path, max_matches, context_lines, regex):
            return {
                "count": 1,
                "matches": [{
                    "path": "custom_sale/models/sale_order.py",
                    "line": 121,
                    "text": "    def _compute_total(self):",
                    "context": [],
                }],
            }

    results, paths, lines = _focused_search(FakeRepo(), "C:/repo", "custom_sale", ["_compute_total"], 20)

    assert results[0]["focused_count"] == 1
    assert paths == ["custom_sale/models/sale_order.py"]
    assert lines == {"custom_sale/models/sale_order.py": [121]}


def test_exact_write_context_returns_complete_enclosing_python_method(tmp_path):
    target = tmp_path / "custom_sale" / "models" / "sync.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Sync:\n"
        "    def unrelated(self):\n"
        "        return False\n\n"
        "    def run_browser_note_sync(self):\n"
        "        request = {\n"
        "            'title': 'Narrative',\n"
        "            'body': 'full note',\n"
        "        }\n"
        "        return request\n\n"
        "    def after(self):\n"
        "        return True\n",
        encoding="utf-8",
    )

    contexts = _exact_write_context(
        tmp_path,
        [{
            "relative_path": "custom_sale/models/sync.py",
            "status": "read",
            "read_mode": "match_range",
            "match_lines": [5],
        }],
    )

    assert len(contexts) == 1
    assert contexts[0]["context_type"] == "complete_python_function"
    assert contexts[0]["complete_unit"] is True
    assert "def run_browser_note_sync" in contexts[0]["content"]
    assert "'body': 'full note'" in contexts[0]["content"]
    assert "def unrelated" not in contexts[0]["content"]
    assert "def after" not in contexts[0]["content"]


def test_exact_write_context_recovers_method_line_from_task_when_preview_has_no_matches(tmp_path):
    target = tmp_path / "custom_sale" / "models" / "sync.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Sync:\n"
        "    def run_browser_note_sync(self):\n"
        "        return {'body': 'full note'}\n"
        + "\n" * 13_000,
        encoding="utf-8",
    )

    contexts = _exact_write_context(
        tmp_path,
        [{
            "relative_path": "custom_sale/models/sync.py",
            "status": "read",
            "read_mode": "module_analysis_cache",
        }],
        payload=_payload(
            "Fix `run_browser_note_sync` so it sends the complete narrative",
            target_hint="run_browser_note_sync",
        ),
    )

    assert contexts[0]["context_type"] == "complete_python_function"
    assert "return {'body': 'full note'}" in contexts[0]["content"]


def test_contract_candidates_include_implementation_and_existing_tests(tmp_path):
    module = tmp_path / "rethink"
    (module / "models").mkdir(parents=True)
    (module / "tests").mkdir()
    (module / "models" / "session_note.py").write_text(
        "API_METHOD = 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    (module / "tests" / "test_session_note.py").write_text(
        "def test_flow():\n    assert 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="rethink",
        task_description="Implement GetAppointmentsByIdAsync with mocked tests that pass",
        target_hint="GetAppointmentsByIdAsync",
    )
    contract = {
        "required_methods": [],
        "required_symbols": ["GetAppointmentsByIdAsync"],
        "required_layers": ["tests"],
    }

    paths = _contract_candidate_paths(tmp_path, "rethink", payload, contract)

    assert "rethink/models/session_note.py" in paths
    assert "rethink/tests/test_session_note.py" in paths


def test_exact_write_context_returns_relevant_functions_across_multiple_files(tmp_path):
    first = tmp_path / "rethink" / "models" / "session_note.py"
    second = tmp_path / "rethink" / "services" / "browser.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        "def fetch_appointment():\n"
        "    return 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    second.write_text(
        "def fetch_response():\n"
        "    return 'GetFormResponsesAsync'\n",
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="rethink",
        task_description="Implement GetAppointmentsByIdAsync then GetFormResponsesAsync",
    )

    contexts = _exact_write_context(
        tmp_path,
        [
            {"relative_path": "rethink/models/session_note.py", "status": "read"},
            {"relative_path": "rethink/services/browser.py", "status": "read"},
        ],
        payload=payload,
    )

    assert {context["relative_path"] for context in contexts} == {
        "rethink/models/session_note.py",
        "rethink/services/browser.py",
    }
    assert all(context["context_type"] == "complete_python_function" for context in contexts)


def test_core_paths_and_directory_files_are_relevance_ranked(tmp_path):
    sale = tmp_path / "sale" / "models"
    mail = tmp_path / "mail" / "models"
    sale.mkdir(parents=True)
    mail.mkdir(parents=True)
    sale_order = sale / "sale_order.py"
    mail_thread = mail / "mail_thread.py"
    sale_order.write_text("class SaleOrder: pass\n", encoding="utf-8")
    mail_thread.write_text("class MailThread: pass\n", encoding="utf-8")
    payload = _payload("Fix `sale_order` total computation", task_type="python_model")

    ranked_paths = _rank_core_required_paths(
        [str(mail_thread), str(sale_order)],
        payload,
        ["custom_sale/models/sale_order.py"],
        1,
    )
    ranked_files = _rank_core_directory_files(tmp_path, payload, ["custom_sale/models/sale_order.py"])

    assert ranked_paths == [str(sale_order)]
    assert ranked_files[0] == sale_order


def test_unique_quoted_text_replacement_is_single_call_deterministic(tmp_path):
    module = tmp_path / "custom_sale"
    views = module / "views"
    views.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    target = views / "sale.xml"
    target.write_text("<span>Old label</span>\n", encoding="utf-8")
    payload = _payload('Replace "Old label" with "New label"')

    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", payload, [])

    assert strategy == "task_description_text_replace"
    assert reason is None
    assert len(writes) == 1
    assert writes[0].relative_path == "custom_sale/views/sale.xml"
    assert writes[0].search == "Old label"
    assert writes[0].replace == "New label"


def test_cached_analysis_targets_exact_controller_without_graph_scan():
    analysis = {
        "source_evidence": [
            {"path": "models/device.py", "content": "class Device: pass"},
            {"path": "controllers/api.py", "content": "@http.route('/iot/v1/heartbeat')\ndef heartbeat(): return True"},
            {"path": "views/device_views.xml", "content": "<form><field name='name'/></form>"},
        ]
    }
    payload = _payload(
        "Fix route `/iot/v1/heartbeat` in the device API controller",
        task_type="controller",
        target_hint="/iot/v1/heartbeat",
    )

    paths = _analysis_index_target_paths(analysis, payload)

    assert paths[0] == "controllers/api.py"
    assert "models/device.py" not in paths


def test_ambiguous_or_complex_task_keeps_two_pass_flow(tmp_path):
    module = tmp_path / "custom_sale"
    views = module / "views"
    views.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    (views / "one.xml").write_text("<span>Old label</span>\n", encoding="utf-8")
    (views / "two.xml").write_text("<span>Old label</span>\n", encoding="utf-8")

    ambiguous = _payload('Replace "Old label" with "New label"')
    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", ambiguous, [])
    assert writes == []
    assert strategy == "task_description_text_replace"
    assert reason == "replacement_not_unique_or_not_found"

    complex_task = _payload("Add a computed approval workflow with a new button")
    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", complex_task, [])
    assert writes == []
    assert strategy is None
    assert reason == "autopatch_not_requested"
