import pytest
from pydantic import ValidationError
import json
import os
import shlex
import subprocess

from odoo_agent_server.routers.tools import _TOOL_DESCRIPTORS, _TOOL_INPUT_MODELS
from odoo_agent_server.schemas import OdooShLogsReadRequest
from odoo_agent_server.schemas import CommandResult, OdooShShellDiagnosticsRequest
from odoo_agent_server.services.odoosh_service import OdooShService


def test_logs_request_requires_timezone_aware_bounded_range():
    request = OdooShLogsReadRequest(
        since="2026-11-20T10:00:00+00:00", until="2026-11-20T11:00:00+00:00"
    )
    assert request.since.utcoffset().total_seconds() == 0
    with pytest.raises(ValidationError):
        OdooShLogsReadRequest(since="2026-11-20T10:00:00", until="2026-11-20T11:00:00+00:00")
    with pytest.raises(ValidationError):
        OdooShLogsReadRequest(since="2026-11-01T00:00:00+00:00", until="2026-11-10T00:00:00+00:00")


def test_logs_request_bounds_page_and_levels():
    assert OdooShLogsReadRequest(last_hours=2, limit=50).limit == 50
    with pytest.raises(ValidationError):
        OdooShLogsReadRequest(last_hours=2, limit=51)
    with pytest.raises(ValidationError):
        OdooShLogsReadRequest(last_hours=2, levels=[])


def test_new_tools_are_registered_and_read_only():
    for name in ("odoosh.logs_read", "odoosh.shell_diagnostics"):
        descriptor = next(tool for tool in _TOOL_DESCRIPTORS if tool.name == name)
        assert descriptor.read_only is True
        assert _TOOL_INPUT_MODELS[name]


def test_redaction_removes_common_secret_values():
    text = OdooShService._redact_output("password=supersecret postgres://user:pass@db/app")
    assert "supersecret" not in text
    assert "pass@db" not in text
    assert "[REDACTED]" in text


def test_remote_reader_and_diagnostics_commands_are_valid_python(monkeypatch):
    captured = []

    def run_shell(self, script, timeout_seconds=60, output_limit_chars=20000):
        captured.append(script)
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout='{"status":"no_matches","entries":[],"returned_count":0}')

    monkeypatch.setattr(OdooShService, "run_shell", run_shell)
    service = OdooShService()
    service.logs_read(OdooShLogsReadRequest(last_hours=1))
    service.shell_diagnostics(OdooShShellDiagnosticsRequest())
    for command in captured:
        argv = shlex.split(command)
        assert argv[:2] == ["python3", "-c"]
        compile(argv[2], "remote_read_only_check.py", "exec")


def test_generated_log_reader_parses_tracebacks_rotations_and_pages(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "odoo.log").write_text(
        "2026-11-20 10:00:00,000 11 INFO db odoo.http: startup\n"
        "2026-11-20 10:01:00,000 12 ERROR db odoo.service: failed\n"
        "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: secret=abc\n",
        encoding="utf-8",
    )
    (logs / "odoo.log.1.gz").write_bytes(__import__("gzip").compress(
        b"2026-11-20 10:02:00,000 13 WARNING db odoo.addons: rotated warning\n"
    ))
    service = OdooShService()

    def local_shell(self, script, *args):
        argv = shlex.split(script)
        env = dict(os.environ, HOME=str(tmp_path), TZ="UTC")
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        return CommandResult(command=argv, cwd="fixture", exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    monkeypatch.setattr(OdooShService, "run_shell", local_shell)
    result = service.logs_read(OdooShLogsReadRequest(
        since="2026-11-20T10:00:00+00:00", until="2026-11-20T10:03:00+00:00", limit=1,
    ))
    assert result["status"] == "success"
    assert result["returned_count"] == 1
    assert result["entries"][0]["level"] == "INFO"
    assert result["truncated"] is True
    assert result["next_cursor"]
    second = service.logs_read(OdooShLogsReadRequest(last_hours=0.05, limit=1, cursor=result["next_cursor"]))
    assert second["entries"][0]["level"] == "ERROR"
    assert "Traceback" in second["entries"][0]["raw"]
