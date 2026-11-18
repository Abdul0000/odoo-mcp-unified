"""Regression coverage for the seven verification/deployment audit findings."""

import asyncio
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.execution_schemas import TaskPlan, ProcessStart, WorkspacePublish, WorkspaceFiles
from odoo_agent_server.schemas import ApprovalStatus
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.check_scope import test_targets as parse_test_targets
from odoo_agent_server.task_schemas import TaskRun
from tests.test_execution_worker import execution as execution_fixture, worker_for
from tests.test_task_coordinator import task as task_fixture

execution = execution_fixture
task = task_fixture
REPO = Path(__file__).resolve().parents[1]


def run_check(service, wid, monkeypatch, name, argv):
    queued = service.start_process(ProcessStart(workspace_id=wid, argv=argv, check_name=name, request_id="audit-process-" + name))
    record = service.store.claim("process")
    worker_for(service, monkeypatch).execute(record)
    return service.poll(queued["process_id"])


def test_assertion_failure_cannot_be_replaced_by_noop(execution, monkeypatch):
    service, wid, *_ = execution
    argv = [sys.executable, "-c", "assert False, 'No module named appointment'"]
    service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "acceptance", "argv": argv}]))
    result = run_check(service, wid, monkeypatch, "acceptance", argv)
    assert result["exit_code"] == 1
    assert "AssertionError" in result["output"]
    assert result["status"] == "failed"
    noop = [sys.executable, "-c", "pass"]
    with pytest.raises(HTTPException):
        service.plan(
            TaskPlan(
                workspace_id=wid,
                checks=[{"name": "replacement", "argv": noop}],
                replace_unavailable_checks={"acceptance": "replacement"},
                replacement_reason="Attempt to drop the original acceptance assertions.",
            )
        )
    assert service.workspace(wid)["checks"][0]["name"] == "acceptance"
    assert not service.verification(wid)["ready"]


def test_pytest_k_option_preserves_odoo_collection_preflight(execution):
    service, wid, *_ = execution
    root = service.root(wid)
    (root / "tests").mkdir()
    (root / "tests/test_calendar.py").write_text("import odoo\ndef test_calendar(): pass\n")
    plain = [sys.executable, "-m", "pytest"]
    with pytest.raises(HTTPException) as exc:
        service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "all", "argv": plain}]))
    assert exc.value.detail["required_runtime"] == "odoo"
    filtered = plain + ["-k", "calendar"]
    with pytest.raises(HTTPException) as exc:
        service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "filtered", "argv": filtered}]))
    assert exc.value.detail["required_runtime"] == "odoo"
    assert service.workspace(wid).get("checks") is None
    assert parse_test_targets(filtered)[1] == []


def test_protected_push_approval_is_bound_to_payload(execution):
    service = ApprovalService()
    approved = {"workspace_id": "client-A", "branch": "master", "message": "Reviewed A"}
    with pytest.raises(HTTPException) as exc:
        service.ensure_allowed("git.push", None, approved, force=True)
    approval_id = exc.value.detail["approval_id"]
    service.store.set_approval_status(approval_id, ApprovalStatus.approved)
    changed = {"workspace_id": "client-B", "branch": "production", "message": "Different code"}
    with pytest.raises(HTTPException) as exc:
        service.ensure_allowed("git.push", approval_id, changed, force=True)
    assert exc.value.status_code == 403
    assert service.store.get_approval(approval_id).status == ApprovalStatus.approved
    service.ensure_allowed("git.push", approval_id, approved, force=True)
    assert service.store.get_approval(approval_id).status == ApprovalStatus.running


def test_task_correctly_invalidates_claims_after_unverified_source_write(task, monkeypatch):
    ledger, tid, service, wid, *_ = task
    argv = [sys.executable, "-c", "import ast,pathlib; ast.parse(pathlib.Path('hello.py').read_text())"]
    service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "syntax", "argv": argv, "layer": "syntax"}], required_layers=["syntax"]))
    assert run_check(service, wid, monkeypatch, "syntax", argv)["status"] == "succeeded"
    published = service.publish(WorkspacePublish(workspace_id=wid, message="audit base", push=True, request_id="audit-publish-base"))
    assert published["pushed"]
    row = ledger.read(tid)
    ledger.observe(row, "execution.workspace_status", service.status(wid))
    ledger.save(row)
    assert ledger.public(row)["success_allowed"]

    async def dispatch(name, args):
        assert name == "execution.files"
        return service.files(WorkspaceFiles.model_validate(args))

    result = asyncio.run(
        ledger.run(TaskRun(task_id=tid, tool="execution_files", step_id="audit-write-after-push", arguments={"operation": "write", "path": "unverified.py", "content": "invalid Python !!!"}), dispatch)
    )
    assert not service.verification(wid)["ready"]
    assert result["success_allowed"] is False


def test_interrupted_dependency_install_is_rebuilt_before_cache_reuse(tmp_path):
    script = (REPO / "deploy/deploy.sh").read_text()
    section = script[script.index("REQ_HASH=") : script.index("# Only the new release")]
    release = tmp_path / "release"
    release.mkdir()
    (release / "requirements.txt").write_text("example==1.0")
    (tmp_path / "venvs").mkdir()
    shim = tmp_path / "create-venv"
    shim.write_text("""#!/bin/sh
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'SCRIPT'
#!/bin/sh
if [ "$2" = pip ] && [ "$3" = install ]; then
    echo install-attempt >> "$AUDIT_CALLS"
    [ "$(wc -l < "$AUDIT_CALLS")" -gt 1 ] || exit 1
fi
exit 0
SCRIPT
chmod +x "$3/bin/python"
""")
    shim.chmod(0o755)
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "chown").write_text("#!/bin/sh\nexit 0\n")
    (commands / "chown").chmod(0o755)
    env = {**os.environ, "ROOT": str(tmp_path), "RELEASE": str(release), "PYTHON": str(shim), "AUDIT_CALLS": str(tmp_path / "calls"), "PATH": str(commands) + os.pathsep + os.environ["PATH"]}

    def run():
        return subprocess.run(["bash", "-ec", section], env=env, capture_output=True, text=True)

    assert run().returncode != 0
    assert not list((tmp_path / "venvs").iterdir())
    second = run()
    assert second.returncode == 0, second.stderr
    assert len((tmp_path / "calls").read_text().splitlines()) == 2
    marker = next((tmp_path / "venvs").rglob(".complete"))
    assert len(marker.read_text().strip()) == 64
    assert run().returncode == 0
    assert len((tmp_path / "calls").read_text().splitlines()) == 2
    # A killed build may leave an interpreter but no completion marker.
    marker.unlink()
    assert run().returncode == 0
    assert len((tmp_path / "calls").read_text().splitlines()) == 3


def test_webhook_acknowledges_async_submission_and_deduplicates(tmp_path, monkeypatch):
    from deploy import webhook_receiver as receiver

    secret = b"a" * 32
    secret_path = tmp_path / "secret"
    secret_path.write_bytes(secret)
    monkeypatch.setattr(receiver, "SECRET_FILE", secret_path)
    monkeypatch.setattr(receiver, "_seen", set())
    body = json.dumps({"repository": {"full_name": receiver.REPO}, "ref": "refs/heads/" + receiver.BRANCH, "after": "a" * 40}).encode()
    responses = []
    request = SimpleNamespace(
        path="/github-webhook",
        rfile=io.BytesIO(body),
        respond=lambda status, payload: responses.append((status, payload)),
        headers={
            "Content-Length": str(len(body)),
            "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "audit-timeout-delivery",
        },
    )
    calls = []

    def queued_systemd(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(receiver.subprocess, "run", queued_systemd)
    receiver.Handler.do_POST(request)
    assert responses[0][0] == 202
    assert "audit-timeout-delivery" in receiver._seen
    request.rfile = io.BytesIO(body)
    receiver.Handler.do_POST(request)
    assert responses[1][1]["status"] == "duplicate"
    assert len(calls) == 1
    queue = (REPO / "deploy/queue-deploy.sh").read_text()
    assert "exec /usr/bin/systemctl --no-block start " in queue
    assert "Type=oneshot" in (REPO / "deploy/odoo-mcp-deploy@.service").read_text()


def test_expired_operation_poll_returns_gone(execution):
    service, wid, *_ = execution
    record, _ = service.store.create("operation", "audit-expired-operation", {"workspace_id": wid}, status="succeeded")
    with service.store._lock(service.store.root / "queue.lock"):
        service.store._save_row({"id": record["id"], "kind": "operation", "status": "expired", "created": 0, "updated": 0, "data": {}})
    with pytest.raises(HTTPException) as expired:
        service.operation_status(record["id"])
    assert expired.value.status_code == 410
    assert expired.value.detail["error_code"] == "execution_expired"


def test_published_workspace_rejects_wrong_technical_module_root(task, monkeypatch):
    from odoo_agent_server.schemas import SafeDeployModuleRequest
    from odoo_agent_server.services.published_workspace_service import validate_published_workspace

    ledger, tid, service, wid, clients = task
    service.files(WorkspaceFiles(workspace_id=wid, operation="write", path="addon_b/__manifest__.py", content="{'name':'B'}"))
    argv = [sys.executable, "-c", "import ast,pathlib; ast.parse(pathlib.Path('addon_b/__manifest__.py').read_text())"]
    service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "syntax", "argv": argv, "layer": "syntax"}], required_layers=["syntax"]))
    assert run_check(service, wid, monkeypatch, "syntax", argv)["status"] == "succeeded"
    service.publish(WorkspacePublish(workspace_id=wid, message="audit addon B", push=True, request_id="audit-publish-addon-b"))
    source = Path(clients[0]["path"])
    subprocess.run(["git", "pull", "--ff-only", "origin", "staging"], cwd=source, capture_output=True, check=True)
    payload = SafeDeployModuleRequest(
        client_name="Alpha", project_path=str(source), module_name="addon_a", module_root_rel="addon_a", execution_workspace_id=wid, commit_message="Audit module binding"
    ).model_copy(update={"module_root_rel": "addon_b"})
    with pytest.raises(HTTPException) as mismatch:
        validate_published_workspace(payload)
    assert mismatch.value.status_code == 409
    payload.module_name = "addon_b"
    evidence = validate_published_workspace(payload)
    assert evidence["paths"] == ["addon_b/__manifest__.py"]


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["python", "-u", "-m", "pytest", "-k", "calendar"], []),
        (["pytest", "-m", "not slow", "-r", "a"], []),
        (["pytest", "-o", "cache_dir=/tmp/cache", "tests/unit"], ["tests/unit"]),
        (["python", "-m", "unittest", "discover", "-s", "addon/tests", "-p", "test_dates.py"], ["addon/tests"]),
        (["python", "-m", "unittest", "discover", "tests", "test_dates.py"], ["tests"]),
        (["python", "-m", "unittest", "tests.test_dates.Case.test_date"], ["tests/test_dates/Case/test_date.py"]),
    ],
)
def test_collection_arguments_are_not_confused_with_option_values(argv, expected):
    assert parse_test_targets(argv)[1] == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "unittest", "checks.test_dates.Case.test_date"],
        ["python", "-m", "unittest", "discover", "checks"],
        ["python", "-m", "pytest", "--pyargs", "checks.test_dates"],
    ],
)
def test_selectors_discovery_and_module_collection_detect_odoo(execution, argv):
    service, wid, *_ = execution
    root = service.root(wid)
    (root / "checks").mkdir()
    (root / "checks/test_dates.py").write_text("import odoo\n")
    with pytest.raises(HTTPException) as unavailable:
        service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "dates", "argv": argv}]))
    assert unavailable.value.detail["required_runtime"] == "odoo"


def test_real_missing_dependency_cannot_drop_acceptance_assertions(execution, monkeypatch):
    service, wid, *_ = execution
    argv = [sys.executable, "-c", "import missing_audit_runtime; assert missing_audit_runtime.check()"]
    service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "original", "argv": argv}]))
    assert run_check(service, wid, monkeypatch, "original", argv)["status"] == "runtime_unavailable"
    with pytest.raises(HTTPException, match="preserve acceptance"):
        service.plan(
            TaskPlan(
                workspace_id=wid,
                checks=[{"name": "weaker", "argv": [sys.executable, "-c", "assert True"]}],
                replace_unavailable_checks={"original": "weaker"},
                replacement_reason="A reason must not permit dropping the original assertions.",
            )
        )
    assert not service.verification(wid)["ready"]


def test_protected_publish_approval_binds_verified_tree(execution, monkeypatch):
    service, wid, *_ = execution
    argv = [sys.executable, "-c", "import ast,pathlib; ast.parse(pathlib.Path('hello.py').read_text())"]
    service.plan(TaskPlan(workspace_id=wid, checks=[{"name": "parse", "argv": argv, "layer": "syntax"}], required_layers=["syntax"]))
    assert run_check(service, wid, monkeypatch, "parse", argv)["status"] == "succeeded"
    monkeypatch.setattr(settings, "protected_branches_raw", "staging")
    request = WorkspacePublish(workspace_id=wid, message="Reviewed tree", request_id="audit-bound-tree-publish")
    with pytest.raises(HTTPException) as pending:
        service.publish(request)
    approval_id = pending.value.detail["approval_id"]
    approvals = ApprovalService()
    approvals.store.set_approval_status(approval_id, ApprovalStatus.approved)
    read = service.files(WorkspaceFiles(workspace_id=wid, operation="read", path="hello.py"))
    service.files(WorkspaceFiles(workspace_id=wid, operation="write", path="hello.py", content="print('new tree')\n", expected_sha256=read["sha256"]))
    # A new execution request verifies the changed tree, but the old approval stays scoped.
    queued = service.start_process(ProcessStart(workspace_id=wid, argv=argv, check_name="parse", request_id="audit-changed-tree-check"))
    worker_for(service, monkeypatch).execute(service.store.claim("process"))
    assert service.poll(queued["process_id"])["status"] == "succeeded"
    with pytest.raises(HTTPException) as mismatch:
        service.publish(request.model_copy(update={"approval_id": approval_id}))
    assert mismatch.value.status_code == 403
    assert approvals.store.get_approval(approval_id).status == ApprovalStatus.approved


def test_deployment_schema_binds_nested_root_to_module():
    from odoo_agent_server.schemas import SafeDeployModuleRequest

    with pytest.raises(ValueError, match="selected technical module"):
        SafeDeployModuleRequest(project_path="/tmp/repo", module_name="addon_a", module_root_rel="custom/addon_b", commit_message="Deploy A")
    valid = SafeDeployModuleRequest(project_path="/tmp/repo", module_name="addon_a", module_root_rel="custom/addon_a", commit_message="Deploy A")
    assert valid.module_root_rel == "custom/addon_a"
