from odoo_agent_server.routers.workflow import _classify_deployed_verification_failure
from odoo_agent_server.schemas import CommandResult


def result(*, stdout="", stderr="", timed_out=False):
    return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=1, stdout=stdout, stderr=stderr, timed_out=timed_out)


def test_real_hash_mismatch_is_classified_as_mismatch():
    status, _ = _classify_deployed_verification_failure(result(stdout="MISMATCH module/a.py expected=x actual=y"))
    assert status == "deploy_code_mismatch"


def test_shell_quoting_error_is_connector_failure_not_mismatch():
    status, _ = _classify_deployed_verification_failure(result(stderr="sh: 2: Syntax error: Unterminated quoted string"))
    assert status == "deploy_verification_command_failed"


def test_timeout_is_classified_separately():
    status, _ = _classify_deployed_verification_failure(result(timed_out=True))
    assert status == "deploy_verification_timeout"
