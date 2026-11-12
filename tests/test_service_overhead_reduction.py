from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import (
    CommandResult,
    JobRecord,
    JobStatus,
    OdooModuleRequest,
    OdooFailureDiagnosis,
    OdooOperationHandoff,
)
from odoo_agent_server.routers.odoo import _background_job_response
from odoo_agent_server.services import core_sync_service
from odoo_agent_server.services.odoosh_service import OdooShService, _SshMasterPool
from odoo_agent_server.services.store_service import JsonStore


def test_core_sync_negative_cache_avoids_repeating_failed_rsync(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(core_sync_service, "_deployed_sha", lambda client=None: "abc123")
    monkeypatch.setattr(
        core_sync_service,
        "_target_for_sync",
        lambda client: SimpleNamespace(host="example", user="odoo", port=22, key_path=None),
    )
    calls = []

    def failed_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(core_sync_service.subprocess, "run", failed_run)
    core_sync_service._negative_cache.clear()

    assert core_sync_service.ensure_module_synced(str(tmp_path), "missing_module") is False
    assert core_sync_service.ensure_module_synced(str(tmp_path), "missing_module") is False
    assert len(calls) == 1


def test_ssh_pool_identity_includes_private_key(tmp_path):
    pool = _SshMasterPool()
    first = pool._key("host", 22, "odoo", tmp_path / "first")
    second = pool._key("host", 22, "odoo", tmp_path / "second")
    assert first != second


def test_long_deploy_poll_uses_adaptive_interval(monkeypatch):
    service = OdooShService()
    checks = iter([False, True])

    def check(_sha):
        deployed = next(checks)
        return CommandResult(
            command=["check"],
            cwd="odoosh:ssh",
            exit_code=0 if deployed else 1,
            stdout='{"deployed": true}' if deployed else '{"deployed": false}',
        )

    sleeps = []
    monkeypatch.setattr(service, "check_commit_deployed", check)
    monkeypatch.setattr("odoo_agent_server.services.odoosh_service.time.sleep", sleeps.append)
    result = service.poll_deployed_commit("abc123", timeout_seconds=60, interval_seconds=0.25)

    assert result.exit_code == 0
    assert sleeps == [5.0]


def test_running_job_updates_do_not_rewrite_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="deploy", status=JobStatus.running))
    store.update_job(job)
    assert "job.update" not in {event.action for event in store.list_audit()}

    store.finish_job(job, JobStatus.passed, {"ok": True})
    assert "job.update" in {event.action for event in store.list_audit()}


def test_terminal_jobs_and_artifacts_are_retention_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "job_max_count", 20)
    monkeypatch.setattr(settings, "artifact_max_count", 20)
    monkeypatch.setattr(settings, "artifact_max_total_bytes", 1_000_000)
    store = JsonStore()
    running = store.create_job(JobRecord(job_type="active", status=JobStatus.running))
    for index in range(25):
        store.create_job(JobRecord(job_type=f"terminal-{index}", status=JobStatus.passed))
        store.create_artifact("test", "retention", f"artifact-{index}")

    jobs = store.list_jobs(limit=100)
    assert running.id in {job.id for job in jobs}
    assert len(jobs) <= 21  # active jobs are never evicted to satisfy the terminal-history cap
    assert len(list(store.artifacts_dir.glob("art_*.json"))) <= 20


def test_debug_sessions_are_atomically_isolated_under_concurrent_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))

    def save(index):
        JsonStore().save_debug_session(
            {
                "id": f"{index:032x}",
                "operation_id": f"operation-{index}",
                "phase": "diagnosing",
            },
            client_name="Test 18",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(20)))

    store = JsonStore()
    for index in range(20):
        session = store.get_debug_session(f"{index:032x}", client_name="Test 18")
        assert session["operation_id"] == f"operation-{index}"
    assert store.find_debug_session_by_operation("operation-7", client_name="Test 18")["id"] == f"{7:032x}"


def test_operation_handoff_is_client_scoped_resumable_and_completed_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    handoff = OdooOperationHandoff(
        operation_id="codeop-example-001",
        client_name="Test 18",
        module_name="x_module",
        objective="Apply the requested module change.",
        completed_checkpoints=["inspection"],
        next_action="Generate the exact write set.",
    )

    store.save_operation_handoff(handoff, client_name="Test 18")
    stored = store.get_operation_handoff(handoff.operation_id, client_name="Test 18")
    assert stored["completed_checkpoints"] == ["inspection"]

    completed = store.complete_operation_handoff(handoff.operation_id, client_name="Test 18")
    assert completed["status"] == "completed"
    assert completed["acceptance_evidence_remaining"] == []

    replayed = store.save_operation_handoff(completed, client_name="Test 18")
    assert replayed["status"] == "completed"


def test_terminal_deploy_job_completes_bound_operation_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    handoff = OdooOperationHandoff(
        operation_id="codeop-terminal-001",
        client_name="Test 18",
        module_name="x_module",
        objective="Deploy the verified module change.",
        next_action="Poll the deploy job.",
        acceptance_evidence_remaining=["terminal_deployment_proof"],
    )
    store.save_operation_handoff(handoff, client_name="Test 18")
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"client_name": "Test 18"},
    ))

    finished = store.finish_job(job, JobStatus.passed, {"deploy_confirmed": True})
    store.bind_operation_handoff_to_job(
        finished.id,
        handoff.operation_id,
        client_name="Test 18",
    )
    completed = store.get_operation_handoff(handoff.operation_id, client_name="Test 18")

    assert completed["status"] == "completed"
    assert completed["acceptance_evidence_remaining"] == []
    assert "terminal_deployment_proof" in completed["completed_checkpoints"]


def test_trusted_evidence_lookup_is_bound_to_one_debug_session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    first_id, second_id = "a" * 32, "b" * 32
    first_diagnosis = OdooFailureDiagnosis(
        origin="custom_code",
        crux="First issue maps to custom code.",
        hypothesis="Repair the first issue custom branch.",
        evidence_ids=["evidence.first.001"],
        debug_session_id=first_id,
        next_strategy="repair_custom_code",
    )
    second_diagnosis = first_diagnosis.model_copy(update={
        "evidence_ids": ["evidence.second.001"],
        "debug_session_id": second_id,
    })
    store.save_debug_session(
        {
            "id": first_id,
            "operation_id": "operation-first",
            "phase": "diagnosing",
            "module_name": "x_module",
            "issue_description": "First runtime issue",
            "diagnosis": first_diagnosis.model_dump(mode="json"),
            "evidence_ledger": [{"id": "evidence.first.001", "trusted": True}],
        },
        client_name="Test 18",
    )
    store.save_debug_session(
        {
            "id": second_id,
            "operation_id": "operation-second",
            "phase": "diagnosing",
            "module_name": "x_module",
            "issue_description": "Second runtime issue",
            "diagnosis": second_diagnosis.model_dump(mode="json"),
            "evidence_ledger": [{"id": "evidence.second.001", "trusted": True}],
        },
        client_name="Test 18",
    )

    assert store.trusted_debug_evidence_ids(
        "Test 18",
        debug_session_id=first_id,
        module_name="x_module",
        task_description="Diagnose First runtime issue completely.",
    ) == {"evidence.first.001"}
    assert store.trusted_debug_evidence_ids(
        "Test 18",
        debug_session_id=first_id,
        module_name="x_module",
        task_description="A different runtime issue",
    ) == set()
    assert store.trusted_debug_evidence_ids("Test 18") == set()


def test_odoo_background_wait_honors_request_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running))
    captured = {}

    def wait_job(job_id, timeout_seconds, poll_interval_seconds):
        captured.update(job_id=job_id, timeout=timeout_seconds, interval=poll_interval_seconds)
        return store.get_job(job_id)

    monkeypatch.setattr(store, "wait_job", wait_job)
    result = _background_job_response(
        store,
        job,
        OdooModuleRequest(module_name="x_module", wait_timeout_seconds=7, poll_interval_seconds=0.5),
        "Upgrade started.",
    )

    assert captured == {"job_id": job.id, "timeout": 7, "interval": 0.5}
    assert result.result["status"] == "running"
    assert result.result["next_call"]["tool"] == "jobs.get"
