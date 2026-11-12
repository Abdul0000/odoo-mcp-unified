from odoo_agent_server.config import settings
from odoo_agent_server.schemas import JobRecord, JobStatus, OdooFailureDiagnosis
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.services.workflow_guard_service import (
    code_change_issue_fingerprint,
    record_code_change_deploy_attempt,
    repeated_failure_guard_status,
)


def _passed_job(store: JsonStore) -> JobRecord:
    job = JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        result={"deploy_confirmed": True},
    )
    return store.create_job(job)


def test_repeated_failure_blocks_third_write_until_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    store = JsonStore()
    fingerprint = code_change_issue_fingerprint(
        project_path="/workspace/repos/example",
        module_name="example_module",
        task_description="Correct the displayed result",
        task_type="controller",
        intent="fix_bug",
        target_hint="final output",
    )
    first = _passed_job(store)
    second = _passed_job(store)
    record_code_change_deploy_attempt(client_name="Example", fingerprint=fingerprint, job_id=first.id)
    record_code_change_deploy_attempt(client_name="Example", fingerprint=fingerprint, job_id=second.id)

    blocked = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
    )
    assert blocked["blocked"] is True
    assert blocked["passed_attempts"] == 2

    narrative_only = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
        investigation_evidence=["Runtime state diverges after the framework compute step."],
    )
    assert narrative_only["blocked"] is True
    assert narrative_only["investigation_evidence_present"] is True

    diagnosis = OdooFailureDiagnosis(
        origin="custom_code",
        crux="Changed custom method returns the wrong runtime value.",
        verified_facts=["Traceback maps to the changed custom source line."],
        hypothesis="The changed branch writes the stale value.",
        evidence_ids=["evidence.custom.001"],
        debug_session_id="a" * 32,
        next_strategy="repair_custom_code",
    )
    allowed = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
        investigation_diagnosis=diagnosis,
        trusted_evidence_ids={"evidence.custom.001"},
    )
    assert allowed["blocked"] is False
    assert allowed["diagnosis_authorizes_repair"] is True

    connector_failure = diagnosis.model_copy(
        update={"origin": "connector_pipeline", "next_strategy": "resume_connector"}
    )
    still_blocked = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
        investigation_diagnosis=connector_failure,
        trusted_evidence_ids={"evidence.custom.001"},
    )
    assert still_blocked["blocked"] is True
    assert still_blocked["diagnosis_authorizes_repair"] is False


def test_guard_does_not_change_normal_workflow_and_can_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    fingerprint = code_change_issue_fingerprint(
        project_path="/workspace/repos/example",
        module_name="example_module",
        task_description="Add a new field",
    )
    normal = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="Add a new field",
    )
    assert normal["blocked"] is False

    reset = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The issue is fixed",
        functional_verification_passed=True,
    )
    assert reset == {"blocked": False, "passed_attempts": 0, "reset": True}
