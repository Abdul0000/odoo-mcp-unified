from __future__ import annotations

from odoo_agent_server.routers.tools import _partition_surgical_qa_errors
from odoo_agent_server.schemas import Finding, Severity


def _finding(code: str, message: str, file: str, line: int | None = None) -> Finding:
    return Finding(severity=Severity.error, code=code, message=message, file=file, line=line)


def test_existing_unrelated_module_debt_is_nonblocking_for_surgical_change(tmp_path):
    old = _finding("IMPLEMENTATION_FILE_EMPTY", "Python implementation file contains no functional code", str(tmp_path / "legacy.py"), 3)
    after = old.model_copy(update={"line": 99})

    blocking, preexisting = _partition_surgical_qa_errors(
        [old.model_dump(mode="json")], [after], strict_module_validation=False
    )

    assert blocking == []
    assert preexisting == [after]


def test_new_qa_error_still_blocks_surgical_change(tmp_path):
    baseline = _finding("IMPLEMENTATION_FILE_EMPTY", "Empty legacy file", str(tmp_path / "legacy.py"))
    introduced = _finding("PYTHON_SYNTAX_ERROR", "invalid syntax", str(tmp_path / "changed.py"), 10)

    blocking, preexisting = _partition_surgical_qa_errors(
        [baseline.model_dump(mode="json")], [baseline, introduced], strict_module_validation=False
    )

    assert blocking == [introduced]
    assert preexisting == [baseline]


def test_requested_contract_and_minimum_manifest_errors_always_block(tmp_path):
    requested = _finding("REQUESTED_METHOD_MISSING", "Requested method is missing: action_run", str(tmp_path / "models"))
    manifest = _finding("MANIFEST_VERSION_CHANGED", "Manifest version changed", str(tmp_path / "__manifest__.py"))
    baseline = [requested.model_dump(mode="json"), manifest.model_dump(mode="json")]

    blocking, preexisting = _partition_surgical_qa_errors(
        baseline, [requested, manifest], strict_module_validation=False
    )

    assert blocking == [requested, manifest]
    assert preexisting == []


def test_new_or_complete_module_keeps_strict_module_wide_gate(tmp_path):
    legacy = _finding("PLACEHOLDER_IMPLEMENTATION", "Placeholder", str(tmp_path / "legacy.py"))

    blocking, preexisting = _partition_surgical_qa_errors(
        [legacy.model_dump(mode="json")], [legacy], strict_module_validation=True
    )

    assert blocking == [legacy]
    assert preexisting == []
