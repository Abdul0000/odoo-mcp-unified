from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_execution_worker import execution, worker_for  # noqa: F401
from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.task_schemas import TaskOpen, TaskRun
from odoo_agent_server.execution_schemas import WorkspaceFiles, WorkspacePublish, TaskPlan
from odoo_agent_server.schemas import OdooRecordCreateRequest, OdooRecordWriteRequest, OdooResult
from odoo_agent_server.services.task_service import TaskService
from odoo_agent_server.services.functional_operation_service import FunctionalOperationService
from odoo_agent_server.services.verification_planner import suggest_checks
from odoo_agent_server.services import mutation_journal


@pytest.fixture
def task(execution, monkeypatch):  # noqa: F811
    svc, wid, source, bare = execution
    clients = [dict(name="Alpha", repo=str(bare), path=str(source), branch="staging", odoo_url="https://alpha.example", odoo_db="alpha", odoo_login="admin", odoo_password="never-expose-this")]
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))
    svc.store.update(wid, "workspace", client_name="Alpha")
    ledger = TaskService()
    opened = ledger.open(TaskOpen(client_name="Alpha", request_id="durable-task-001", description="Fix calculation", target="push", module_name="addon"))
    row = ledger.read(opened["task_id"])
    row["refs"]["workspace_id"] = wid
    ledger.save(row)
    return ledger, opened["task_id"], svc, wid, clients


def test_task_contract_idempotency_and_configuration_invalidation(task, monkeypatch):
    ledger, tid, _, _, clients = task
    again = ledger.open(TaskOpen(client_name="Alpha", request_id="durable-task-001", description="Fix calculation", target="push", module_name="addon"))
    assert again["task_id"] == tid
    with pytest.raises(HTTPException):
        ledger.open(TaskOpen(client_name="Alpha", request_id="durable-task-001", description="Other work"))
    row = ledger.read(tid)
    row["claims"]["push_verified"] = True
    ledger.save(row)
    assert ledger.public(row)["success_allowed"]
    clients[0]["branch"] = "new-staging"
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))
    assert ledger.public(row)["status"] == "stale_configuration"
    assert not ledger.public(row)["success_allowed"]


def test_task_run_records_real_existing_tool_and_never_replays_lost_write(task):
    ledger, tid, _, wid, _ = task
    calls = []

    async def interrupted(name, args):
        calls.append((name, args))
        raise TimeoutError("response lost")

    request = TaskRun(task_id=tid, step_id="edit-step-001", tool="execution_files", arguments={"workspace_id": wid, "operation": "write", "path": "new.py", "content": "x=1"})
    with pytest.raises(TimeoutError):
        asyncio.run(ledger.run(request, interrupted))
    result = asyncio.run(TaskService().run(request, interrupted))
    assert result["idempotent_replay"] and len(calls) == 1
    assert result["step"]["status"] == "reconciliation_required"
    assert not result["task"]["success_allowed"]
    with pytest.raises(HTTPException):
        asyncio.run(ledger.run(request.model_copy(update={"arguments": {"workspace_id": wid, "operation": "read", "path": "new.py"}}), interrupted))


@pytest.mark.parametrize(
    "tool,args",
    [
        ("execution_files", {"workspace_id": "a" * 32}),
        ("execution_workspace_open", {"client_name": "Other", "task_description": "x", "request_id": "another-001"}),
        ("execution_workspace_open", {"project_path": "/tmp/other", "task_description": "x", "request_id": "another-001"}),
        ("workflow_resume_job", {"job_id": "unrelated"}),
        ("git_push", {"project_path": "/tmp/other"}),
        ("odoo_mutation_preview", {"operation": "create", "arguments": {"client_name": "Other"}}),
    ],
)
def test_coordinator_cannot_cross_scope_or_expose_hidden_tools(task, tool, args):
    ledger, tid, *_ = task

    async def forbidden(*_):
        raise AssertionError("must not dispatch")

    with pytest.raises(HTTPException):
        asyncio.run(ledger.run(TaskRun(task_id=tid, step_id="bad-step-001", tool=tool, arguments=args), forbidden))


def test_resume_reads_existing_workspace_and_does_not_publish(task):
    from odoo_agent_server.routers.tools import _dispatch

    ledger, tid, _, _, _ = task
    result = asyncio.run(ledger.resume(tid, _dispatch))
    assert result["observations"][0]["tool"] == "execution.workspace_status"
    assert not result["task"]["success_allowed"]
    assert result["task"]["refs"]["workspace_id"]


def test_push_and_source_match_never_count_as_deployment(task):
    ledger, tid, *_ = task
    row = ledger.read(tid)
    row["target"] = "deploy"
    ledger.observe(row, "execution.workspace_status", {"status": "ready", "published_commit": "b" * 40, "pushed": True, "success_allowed": True, "verification": {"ready": True}})
    assert row["claims"]["push_verified"] and not ledger.public(row)["success_allowed"]
    ledger.observe(row, "workflow.verify_deployed_module", {"status": "passed", "source_status": "matched"})
    assert not ledger.public(row)["success_allowed"]
    ledger.observe(row, "jobs.get", {"id": "f" * 32, "deploy_confirmed": True, "success_allowed": True, "result": {"sha": "b" * 40, "module_upgraded": True}})
    assert ledger.public(row)["success_allowed"]
    ledger.observe(row, "jobs.get", {"deploy_confirmed": True, "success_allowed": True, "result": {"sha": "c" * 40}})
    assert not ledger.public(row)["success_allowed"]


def test_suggested_tests_are_source_derived_and_dont_fake_missing_layers(task):
    _, _, svc, wid, _ = task
    svc.files(WorkspaceFiles(workspace_id=wid, operation="write", path="model.py", content="x=1\n"))
    result = suggest_checks(wid)
    assert result["suggested_checks"][0]["layer"] == "syntax"
    assert result["missing_layers"] == ["unit"] and not result["applied"]
    with pytest.raises(HTTPException):
        suggest_checks(wid, apply=True)
    assert svc.workspace(wid).get("checks") is None


def test_native_odoo_tests_are_not_sent_to_plain_pytest(task):
    _, _, svc, wid, _ = task
    for path, content in [("addon/__manifest__.py", "{'depends':['base']}"), ("addon/models/order.py", "x=1")]:
        svc.files(WorkspaceFiles(workspace_id=wid, operation="write", path=path, content=content))
    # Simulate an existing native test supplied by the client repository.
    tests = svc.root(wid) / "addon" / "tests"
    tests.mkdir()
    (tests / "test_order.py").write_text("from odoo.tests import TransactionCase")
    from odoo_agent_server.services.execution_service import git

    git(["add", "."], svc.root(wid))
    result = suggest_checks(wid)
    assert "orm" in result["required_layers"]
    assert not any("pytest" in c["argv"] for c in result["suggested_checks"])


def test_ui_dashboard_auth_scope_and_no_secret_values(task, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "safe-admin-test-token")
    monkeypatch.setattr(settings, "public_base_url", "https://testserver")
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/admin/activity").status_code == 401
    response = client.post("/admin/session", json={"token": settings.api_token})
    csrf = response.json()["csrf_token"]
    result = client.get("/admin/activity").json()
    assert len(result["tasks"]) == 1 and result["workspaces"]
    evidence = client.get("/admin/tasks/" + task[1] + "/evidence")
    assert evidence.status_code == 200 and "checks" in evidence.json()
    assert "never-expose-this" not in evidence.text
    assert "never-expose-this" not in json.dumps(result)
    assert client.post("/admin/tasks/" + task[1] + "/resume", json={}).status_code == 403
    assert client.post("/admin/tasks/" + task[1] + "/resume", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/admin/activity?client_name=Missing").status_code == 404


@pytest.fixture
def functional(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.delenv("ODOO_CLIENTS", raising=False)
    service = FunctionalOperationService()
    rows = []
    calls = []

    class Rpc:
        def create_records(self, model, records, context):
            calls.append(records)
            time.sleep(0.02)
            rows.append({"id": 7, "name": records[0]["name"], "write_date": "first"})
            return OdooResult(status="ok", result=[7])

        def write_records(self, *_):
            calls.append("write")
            return OdooResult(status="ok", result=True)

    rpc = Rpc()
    monkeypatch.setattr(service, "_runtime", lambda _: (SimpleNamespace(name="Alpha"), rpc))
    monkeypatch.setattr(service, "_metadata", lambda *_: {"id": {}, "name": {"type": "char"}, "write_date": {}})
    monkeypatch.setattr(service, "_ensure_access", lambda *_: None)
    monkeypatch.setattr(service, "_read_ok", lambda *_: list(rows))
    return service, rpc, rows, calls


def test_duplicate_functional_calls_execute_once_even_concurrently(functional):
    service, _, _, calls = functional
    request = OdooRecordCreateRequest(client_name="Alpha", request_id="functional-001", model="res.partner", records=[{"name": "Test"}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.create(request), range(2)))
    assert len(calls) == 1 and all(r["success_allowed"] for r in results)
    assert sum(bool(r.get("idempotent_replay")) for r in results) == 1


def test_uncertain_functional_response_never_replays_after_restart(functional, monkeypatch):
    service, rpc, _, calls = functional

    def lost(*args):
        calls.append("committed")
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(rpc, "create_records", lost)
    request = OdooRecordCreateRequest(client_name="Alpha", request_id="uncertain-001", model="res.partner", records=[{"name": "Test"}])
    with pytest.raises(TimeoutError):
        service.create(request)
    result = service.create(request)
    assert result["status"] == "reconciliation_required" and calls == ["committed"]
    assert mutation_journal.read("Alpha", request.request_id)["state"] == "dispatched"


def test_preview_is_readonly_and_stale_record_blocks_write(functional):
    service, _, rows, calls = functional
    rows.append({"id": 7, "name": "Before", "write_date": "first"})
    args = {"client_name": "Alpha", "request_id": "preview-write-001", "model": "res.partner", "ids": [7], "values": {"name": "After"}}
    preview = service.preview("write", args)
    assert preview["write_executed"] is False and not calls
    rows[0]["write_date"] = "later"
    request = OdooRecordWriteRequest(**args, preview_id=preview["preview_id"])
    with pytest.raises(HTTPException, match="409"):
        service.write(request)
    assert not calls and mutation_journal.read("Alpha", request.request_id) is None


@pytest.mark.parametrize("message,category", [("Odoo HTTPS JSON-RPC login failed", "authentication"), ("connection timed out", "ssh_network"), ("unmet dependencies", "missing_dependency")])
def test_debug_diagnosis_uses_trusted_infrastructure_evidence(message, category):
    from odoo_agent_server.routers.tools import _debug_infer_diagnosis

    session = {"id": "a" * 32, "module_name": "addon", "evidence_ledger": [{"id": "evidence-001", "trusted": True, "summary": message}]}
    result = _debug_infer_diagnosis(session)
    assert result.failure_category == category and result.next_strategy != "repair_custom_code"
    session["evidence_ledger"][0]["trusted"] = False
    assert _debug_infer_diagnosis(session).failure_category == "unknown"


def test_real_edit_test_commit_push_and_published_handoff(task, monkeypatch):
    import subprocess
    import sys
    from odoo_agent_server.routers.tools import _dispatch
    from odoo_agent_server.execution_schemas import ProcessStart
    from odoo_agent_server.services.published_workspace_service import validate_published_workspace
    from odoo_agent_server.schemas import SafeDeployModuleRequest

    ledger, tid, svc, wid, clients = task

    async def step(identifier, tool, args):
        return await ledger.run(TaskRun(task_id=tid, step_id=identifier, tool=tool, arguments=args), _dispatch)

    asyncio.run(step("write-code-001", "execution_files", {"workspace_id": wid, "operation": "write", "path": "addon/calc.py", "content": "def total(qty, price):\n    return qty * price\n"}))
    asyncio.run(
        step(
            "write-manifest-001",
            "execution_files",
            {"workspace_id": wid, "operation": "write", "path": "addon/__manifest__.py", "content": "{'name':'Addon','depends':['base'],'version':'18.0.1.0.0'}\n"},
        )
    )
    argv = [sys.executable, "-B", "-c", "from addon.calc import total; assert total(3, 7)==21; assert total(0, 9)==0; assert total(-2, 5)==-10"]
    svc.plan(TaskPlan(workspace_id=wid, checks=[{"name": "calculation-regression", "argv": argv, "layer": "unit"}], required_layers=["unit"]))
    process = svc.start_process(ProcessStart(workspace_id=wid, argv=argv, check_name="calculation-regression", request_id="calculation-check-001"))
    worker_for(svc, monkeypatch).execute(svc.store.claim("process"))
    assert svc.poll(process["process_id"])["status"] == "succeeded"
    published = svc.publish(WorkspacePublish(workspace_id=wid, message="Fix calculation", request_id="task-publish-001"))
    snapshot = asyncio.run(ledger.resume(tid, _dispatch))["task"]
    assert snapshot["success_allowed"] and snapshot["claims"]["push_verified"]
    source = clients[0]["path"]
    subprocess.run(["git", "pull", "--ff-only", "origin", "staging"], cwd=source, capture_output=True, check=True)
    payload = SafeDeployModuleRequest(client_name="Alpha", project_path=source, module_name="addon", execution_workspace_id=wid, commit_message="Deploy exact published commit")
    proof = validate_published_workspace(payload)
    assert proof["sha"] == published["commit"] and "addon/calc.py" in proof["paths"]
    # Exercise the no-local-diff adoption through the real safe-deploy pipeline.
    # Odoo boundaries are simulated; Git and exact-tree checks remain real.
    from odoo_agent_server.routers import workflow
    from odoo_agent_server.schemas import CommandResult, JobRecord, JobStatus, SyntaxVerifyResult
    from odoo_agent_server.services.store_service import JsonStore
    from odoo_agent_server.services.git_service import GitService
    from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService

    upgrades = []

    def command(stdout="ok"):
        return CommandResult(command=["test-boundary"], cwd=source, exit_code=0, stdout=stdout)

    fake = SimpleNamespace(check_commit_deployed=lambda _sha: command('{"deployed":true}'), verify_module_manifest=lambda _module: command(), verify_user_file_hashes=lambda _hashes: command())
    monkeypatch.setattr(workflow, "_odoosh_service_for_client", lambda _client: fake)
    monkeypatch.setattr(SyntaxVerifierService, "verify", lambda *_: SyntaxVerifyResult(status="passed", push_blocked=False))
    monkeypatch.setattr(
        workflow, "_start_and_wait_module_deploy", lambda request: upgrades.append(request.module_name) or OdooResult(status="ok", result={"finished": True, "module_name": request.module_name})
    )

    def duplicate(*_args, **_kwargs):
        raise AssertionError("Already-pushed execution commit must not commit/push again")

    monkeypatch.setattr(GitService, "commit", duplicate)
    monkeypatch.setattr(GitService, "push", duplicate)
    payload.paths = proof["paths"]
    payload.run_qa = False
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running, request=payload.model_dump(mode="json")))
    workflow._run_safe_deploy(payload, job.id)
    finished = store.get_job(job.id)
    assert finished.deploy_confirmed, finished.model_dump()
    assert finished.result["module_upgraded"] is True and upgrades == ["addon"]
    # A new edit invalidates both exact-tree publication and deployment handoff.
    svc.files(WorkspaceFiles(workspace_id=wid, operation="write", path="other.py", content="x=1"))
    with pytest.raises(HTTPException):
        validate_published_workspace(payload)


def test_task_tools_are_available_on_provider_surface():
    from odoo_agent_server.routers.mcp import _PROVIDER_TOOL_NAMES
    from odoo_agent_server.routers.tools import tool_input_schema

    for name in ("task.open", "task.run", "task.resume", "task.deploy", "task.test_plan", "task.knowledge", "odoo.mutation_preview"):
        assert name in _PROVIDER_TOOL_NAMES
        assert tool_input_schema(name)["type"] == "object"


def test_action_postconditions_prove_business_result(functional, monkeypatch):
    from odoo_agent_server.schemas import OdooRecordActionRequest

    service, rpc, rows, calls = functional
    rows.append({"id": 7, "name": "After", "write_date": "first"})
    monkeypatch.setattr(rpc, "run_action", lambda *_: OdooResult(status="ok", result=True), raising=False)
    result = service.action(OdooRecordActionRequest(client_name="Alpha", request_id="assert-action-001", model="res.partner", ids=[7], action="action_custom", expected_values={"name": "After"}))
    assert result["verification_mode"] == "explicit_postconditions" and result["success_allowed"]


def test_live_task_lock_blocks_maintenance_without_stale_status(task):
    from deploy.server_control import active_locks

    ledger, tid, *_ = task
    assert not active_locks(settings.state_path)
    with ledger.lock(tid):
        assert active_locks(settings.state_path)
    assert not active_locks(settings.state_path)


def test_readonly_step_ids_refresh_without_replaying_mutations(task):
    ledger, tid, _, wid, _ = task
    calls = []

    async def read(name, args):
        calls.append(name)
        return {"status": "ok"}

    req = TaskRun(task_id=tid, step_id="same-read-001", tool="execution_files", arguments={"workspace_id": wid, "operation": "list"})
    asyncio.run(ledger.run(req, read))
    asyncio.run(ledger.run(req, read))
    assert len(calls) == 2 and len(ledger.read(tid)["steps"]) == 1


def test_coding_only_task_cannot_push_or_autodeploy(task):
    ledger, tid, _, wid, _ = task
    row = ledger.read(tid)
    row["target"] = "coding"
    ledger.save(row)

    async def forbidden(*_):
        raise AssertionError("must not dispatch")

    req = TaskRun(task_id=tid, step_id="bad-push-001", tool="execution_publish", arguments={"workspace_id": wid, "message": "Push without request", "request_id": "bad-publish-001", "push": True})
    with pytest.raises(HTTPException):
        asyncio.run(ledger.run(req, forbidden))


def test_task_boundary_never_lifts_publish_success_into_deploy_success(task):
    from odoo_agent_server.routers.mcp import _ensure_provider_control_envelope

    ledger, tid, *_ = task
    row = ledger.read(tid)
    row["target"] = "deploy"
    row["commit_sha"] = "b" * 40
    row["claims"] = {"push_verified": True}
    result = ledger.envelope(row, {"status": "succeeded", "pushed": True, "success_allowed": True})
    assert "result" not in result and result["success_allowed"] is False
    assert result["next_call"]["tool"] == "task_deploy"
    normalized = _ensure_provider_control_envelope(result)
    assert not normalized["success_allowed"] and normalized["completion_required"]


def test_client_knowledge_invalidates_config_and_source_facts(task, monkeypatch):
    from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService

    ledger, _, _, _, clients = task
    monkeypatch.setattr(OdooJsonRpcService, "search_read", lambda *_args, **_kw: OdooResult(status="ok", result=[{"name": "base", "latest_version": "18.0.1.0"}]))
    fresh = ledger.knowledge("Alpha", True)
    assert fresh["runtime_facts"]["base_module_version"] == "18.0.1.0"
    assert fresh["source_sha"] and not fresh["cache_hit"]
    assert ledger.knowledge("Alpha")["cache_hit"]
    clients[0]["odoo_db"] = "different-build"
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))
    stale = ledger.knowledge("Alpha")
    assert not stale["fresh"] and stale["runtime_facts"] is None


def test_debug_task_cannot_finish_from_deployment_without_replay(task):
    ledger, tid, *_ = task
    row = ledger.read(tid)
    row["target"] = "deploy"
    ledger.observe(row, "workflow.odoo_debug_and_fix", {"status": "running", "debug_session": {"id": "a" * 32, "truth_gate": {"verified_fixed": False}}})
    ledger.observe(row, "jobs.get", {"deploy_confirmed": True, "success_allowed": True, "result": {"sha": "b" * 40}})
    assert not ledger.public(row)["success_allowed"]
    ledger.observe(
        row, "workflow.odoo_debug_and_fix", {"status": "passed", "debug_session": {"id": "a" * 32}, "deploy_confirmed": True, "success_allowed": True, "truth_gate": {"verified_fixed": True}}
    )
    assert ledger.public(row)["success_allowed"]


def test_new_creation_recipe_previews_validate_live_fields_without_writes(functional, monkeypatch):
    from odoo_agent_server.schemas import OdooFlowTestRequest

    service, _, _, calls = functional
    monkeypatch.setattr(service, "_metadata", lambda *_: {"name": {"type": "char"}, "type": {"type": "selection"}, "description": {"type": "html"}})
    result = service.flow_test(OdooFlowTestRequest(client_name="Alpha", request_id="crm-preview-001", flow="crm_opportunity", mode="preview", values={"name": "Opportunity"}))
    assert result["write_executed"] is False and not calls
    assert result["plan"]["compatibility_basis"] == "installed_model_fields_and_user_access"


def test_old_publish_receipt_cannot_revalidate_changed_source(task):
    ledger, tid, _, wid, _ = task
    row = ledger.read(tid)
    ledger.observe(row, "execution.workspace_status", {"workspace_id": wid, "verification": {"ready": False}, "pushed": True, "success_allowed": False})
    ledger.observe(row, "execution.status", {"status": "succeeded", "commit": "b" * 40, "pushed": True, "success_allowed": True})
    assert not ledger.public(row)["success_allowed"]


def test_task_continuation_preserves_writes_and_material_questions(task):
    ledger, tid, *_ = task
    row = ledger.read(tid)
    call = {"tool": "workflow_odoo_code_change", "arguments": {"client_name": "Alpha", "module_name": "addon", "task_description": "Fix"}, "required_generated_arguments": ["writes"]}
    out = ledger.envelope(row, {"next_call": call})
    assert "arguments.writes" in out["argument_request"]["required_generated_arguments"]
    assert "step_id" in out["argument_request"]["required_generated_arguments"]
    call["required_generated_arguments"] = ["clarification_answer"]
    out = ledger.envelope(row, {"next_call": call, "clarification_gate": {"question": "Which report?"}})
    assert out["input_required"] and out["input_request"]["prompt"] == "Which report?"


def test_pending_deployment_is_polled_instead_of_starting_another(task):
    ledger, tid, *_ = task
    row = ledger.read(tid)
    row["target"] = "deploy"
    row["claims"]["push_verified"] = True
    row["refs"]["pending_job_ids"] = ["a" * 32]
    assert ledger.envelope(row)["next_call"]["tool"] == "task_resume"


def test_resume_rediscovers_deploy_job_after_lost_queue_response(task):
    from odoo_agent_server.schemas import JobRecord, JobStatus
    from odoo_agent_server.services.store_service import JsonStore
    from odoo_agent_server.routers.tools import _dispatch

    ledger, tid, _, wid, _ = task
    row = ledger.read(tid)
    row["target"] = "deploy"
    row["steps"]["lost-deploy"] = {"tool": "task.deploy", "status": "dispatched", "fingerprint": "lost"}
    ledger.save(row)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.queued, request={"client_name": "Alpha", "module_name": "addon", "execution_workspace_id": wid}))
    result = asyncio.run(ledger.resume(tid, _dispatch))
    assert result["task"]["refs"]["deploy_job_id"] == job.id
    assert ledger.read(tid)["steps"]["lost-deploy"]["status"] == "reconciled"
    assert not result["success_allowed"]
    assert len(store.list_jobs()) == 1


def test_legacy_job_status_does_not_invent_queued_phase_or_upgrade_evidence(task):
    from odoo_agent_server.schemas import JobRecord, JobStatus
    from odoo_agent_server.services.store_service import JsonStore
    from odoo_agent_server.services.task_dashboard_service import TaskDashboardService

    store = JsonStore()
    store.create_job(JobRecord(job_type="module_deploy", status=JobStatus.passed, request={"client_name": "Alpha", "module_name": "addon"}, result={}))
    row = TaskDashboardService().snapshot("Alpha")["deployments"][0]
    assert row["phase"] == "passed"
    assert row["source_deployed"] is None and row["module_upgraded"] is None
    assert row["deploy_confirmed"] is False


def test_server_binds_missing_ids_and_deduplicates_identical_intent(task):
    ledger, tid, _, wid, _ = task
    calls = []
    async def dispatched(name, args):
        calls.append((name, args))
        return {'status': 'written'}
    request = TaskRun(task_id=tid, tool='execution_files', arguments={'operation': 'write', 'path': 'new.py', 'content': 'VALUE = 1\n'})
    asyncio.run(ledger.run(request, dispatched))
    replay = asyncio.run(ledger.run(request, dispatched))
    assert calls[0][1]['workspace_id'] == wid
    assert len(calls) == 1 and replay['idempotent_replay']
    assert len(ledger.read(tid)['steps']) == 1


def test_missing_arguments_are_correctable_without_poisoning_task(task):
    ledger, tid, _, _, _ = task
    async def forbidden(*_):
        pytest.fail('Invalid arguments must never dispatch')
    result = asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_process_start'), forbidden))
    assert result['status'] == 'invalid_arguments' and result['dispatched'] is False
    assert any(error['field'] == 'argv' for error in result['tool_result']['argument_errors'])
    assert not ledger.read(tid)['steps'] and ledger.read(tid)['phase'] == 'inspect'
    completed = ledger.read(tid)
    completed['claims']['push_verified'] = True
    ledger.save(completed)
    invalid = asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_process_start'), forbidden))
    assert not invalid['success_allowed'] and invalid['state_class'] == 'action_required'
    assert invalid['argument_request']['arguments']['tool'] == 'execution_process_start'


def test_declared_check_command_and_request_id_are_server_bound(task):
    import sys
    ledger, tid, svc, wid, _ = task
    argv = [sys.executable, '-c', "assert 3 * 7 == 21"]
    svc.plan(TaskPlan(workspace_id=wid, checks=[{'name': 'calculation', 'argv': argv}], required_layers=['unit']))
    captured = []
    async def dispatched(name, args):
        captured.append(args)
        return {'status': 'queued', 'process_id': 'f' * 32}
    asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_process_start', arguments={'check_name': 'calculation'}), dispatched))
    assert captured[0]['argv'] == argv and captured[0]['workspace_id'] == wid
    assert captured[0]['request_id'].startswith('task-')


def test_model_with_only_task_id_can_run_tests_and_publish(task, monkeypatch):
    import sys
    from odoo_agent_server.routers.tools import _dispatch
    ledger, tid, svc, wid, _ = task
    read = svc.files(WorkspaceFiles(workspace_id=wid, operation='read', path='hello.py'))
    svc.files(WorkspaceFiles(workspace_id=wid, operation='write', path='hello.py', content='VALUE = 21\n', expected_sha256=read['sha256']))
    argv = [sys.executable, '-B', '-c', "assert __import__('hello').VALUE == 21"]
    svc.plan(TaskPlan(workspace_id=wid, checks=[{'name': 'actual-result', 'argv': argv}], required_layers=['unit']))
    async def loop():
        for _ in range(100):
            result = await ledger.run(TaskRun(task_id=tid), _dispatch)
            for process in list(svc.store.pending('process')):
                worker_for(svc, monkeypatch).execute(svc.store.claim('process'))
            if result['success_allowed']:
                return result
            await asyncio.sleep(.03)
        pytest.fail('ID-only guided model made no bounded progress: ' + str({key: result.get(key) for key in ('status', 'state_class', 'next_call', 'argument_request', 'uncertain_steps')}))
    result = asyncio.run(loop())
    assert result['task']['claims']['coding_verified'] and result['task']['claims']['push_verified']
    assert not result['deploy_confirmed']
    assert svc.workspace(wid)['pushed']


def test_guided_planner_derives_available_checks_instead_of_guessing(task):
    from odoo_agent_server.routers.tools import _dispatch
    ledger, tid, svc, wid, _ = task
    svc.files(WorkspaceFiles(workspace_id=wid, operation='write', path='tests/test_hello.py', content='def test_result():\n    assert 3 * 7 == 21\n'))
    result = asyncio.run(ledger.run(TaskRun(task_id=tid), _dispatch))
    planned = svc.workspace(wid)
    assert any('pytest' in check['argv'] for check in planned['checks'])
    assert set(planned['required_layers']) == {'syntax', 'unit'}
    assert not result['success_allowed']  # Selecting commands is not passing them.


def test_guided_mode_never_replays_an_uncertain_code_write(task):
    from odoo_agent_server.routers.tools import _dispatch
    ledger, tid, _, _, _ = task
    async def lost(*_):
        raise TimeoutError('lost write response')
    with pytest.raises(TimeoutError):
        asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_files', arguments={'operation': 'write', 'path': 'uncertain.py', 'content': 'VALUE = 1'}), lost))
    calls = []
    async def only_reads(name, args):
        calls.append(name)
        assert name in {'execution.workspace_status', 'execution.status', 'jobs.get'}
        return await _dispatch(name, args)
    result = asyncio.run(ledger.run(TaskRun(task_id=tid), only_reads))
    assert result['uncertain_steps'] and not result['success_allowed']
    assert 'execution.files' not in calls


def test_lost_sync_response_is_reconciled_using_server_record_id(task, monkeypatch):
    from odoo_agent_server.routers.tools import _dispatch
    from odoo_agent_server.services import execution_service
    from tests.test_concurrent_git_sync import advance
    ledger, tid, svc, wid, clients = task
    from pathlib import Path
    source = Path(clients[0]['path'])
    advance(source, Path(clients[0]['repo']), "print('manual')\n")
    monkeypatch.setattr(execution_service, '_submit_io', lambda callback: callback())
    async def lost(name, args):
        await _dispatch(name, args)
        raise TimeoutError('lost sync response')
    with pytest.raises(TimeoutError):
        asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_workspace_sync'), lost))
    saved = ledger.read(tid)
    step = next(iter(saved['steps'].values()))
    assert step['record_id'] in saved['refs']['operation_ids']
    result = asyncio.run(ledger.resume(tid, _dispatch))
    assert not result['uncertain_steps']
    assert next(iter(ledger.read(tid)['steps'].values()))['status'] == 'reconciled'


def test_ambiguous_process_ids_are_not_guessed(task):
    ledger, tid, _, _, _ = task
    saved = ledger.read(tid)
    saved['refs']['process_ids'] = ['a' * 32, 'b' * 32]
    ledger.save(saved)
    async def forbidden(*_):
        pytest.fail('Ambiguous process must not be polled')
    result = asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_process_poll'), forbidden))
    assert result['status'] == 'invalid_arguments' and result['dispatched'] is False


def test_bad_argument_types_return_field_errors_before_scope_handling(task):
    ledger, tid, _, _, _ = task
    async def forbidden(*_):
        pytest.fail('Invalid client type must never dispatch')
    result = asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_workspace_open', arguments={'client_name': ['Alpha']}), forbidden))
    assert result['status'] == 'invalid_arguments' and not result['dispatched']
    assert result['tool_result']['argument_errors'][0]['field'] == 'client_name'
    assert not ledger.read(tid)['steps']


def test_new_request_id_cannot_bypass_uncertain_mutation(task):
    ledger, tid, _, _, _ = task
    calls = []
    async def lost(name, args):
        calls.append(name)
        raise TimeoutError('lost write response')
    with pytest.raises(TimeoutError):
        asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_files', arguments={'operation': 'write', 'path': 'first.py', 'content': 'VALUE = 1'}), lost))
    retry = asyncio.run(ledger.run(TaskRun(task_id=tid, step_id='new-attempt-123', tool='execution_files', arguments={'operation': 'write', 'path': 'second.py', 'content': 'VALUE = 2'}), lost))
    assert len(calls) == 1 and retry['status'] == 'reconciliation_required' and retry['dispatched'] is False
    assert retry['next_call']['tool'] == 'task_resume' and len(ledger.read(tid)['steps']) == 1


def test_interrupted_read_does_not_require_mutation_reconciliation(task):
    from odoo_agent_server.routers.tools import _dispatch
    ledger, tid, _, _, _ = task
    saved = ledger.read(tid)
    saved['steps']['old-read-123'] = {'tool': 'execution.files', 'status': 'dispatched', 'read_only': True}
    ledger.save(saved)
    resumed = asyncio.run(ledger.resume(tid, _dispatch))
    assert not resumed['uncertain_steps']
    async def written(name, args):
        return {'status': 'written'}
    result = asyncio.run(ledger.run(TaskRun(task_id=tid, tool='execution_files', arguments={'operation': 'write', 'path': 'new.py', 'content': 'VALUE = 1'}), written))
    assert result['tool_result']['status'] == 'written'


def test_contended_task_run_preserves_intent_without_dispatch(task):
    ledger, tid, *_ = task
    calls = []
    async def dispatch(*args):
        calls.append(args)
        raise AssertionError('Contended attempt must not dispatch')
    request = TaskRun(task_id=tid, tool='execution_workspace_status', arguments={}, step_id='contended-status')
    before = ledger.read(tid)
    with ledger.lock(tid):
        result = asyncio.run(ledger.run(request, dispatch))
        guided = asyncio.run(ledger.run(TaskRun(task_id=tid), dispatch))
    assert result['status']=='task_busy' and not result['input_required']
    assert result['next_call']['arguments']==request.model_dump(mode='json', exclude_none=True)
    assert not result['dispatched'] and not calls
    assert guided['status']=='task_busy'
    assert ledger.read(tid)==before


def test_contention_after_dispatched_mutation_stays_uncertain(task):
    from odoo_agent_server.services.task_service import TaskBusy
    ledger, tid, *_ = task
    calls = []
    async def dispatch(*args):
        calls.append(args)
        raise TaskBusy(409, 'Another task is busy after this mutation started')
    request = TaskRun(task_id=tid, tool='execution_files', step_id='uncertain-busy-write',
        arguments={'operation':'write', 'path':'new.py', 'content':'VALUE = 1\n'})
    result = asyncio.run(ledger.run(request, dispatch))
    assert result['status']=='reconciliation_required'
    assert result['next_call']['tool']=='task_resume' and result['uncertain_steps']
    asyncio.run(ledger.run(request, dispatch))
    assert len(calls)==1
