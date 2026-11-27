from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ..schemas import CommandResult


# Collect verdicts from pytest itself. A successful command with zero passing
# test calls (for example, an all-skipped suite) is not runtime evidence.
_PYTEST_CHECK = """
import pytest, sys
class Evidence:
    passed = 0
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.passed and not getattr(report, 'wasxfail', False):
            self.passed += 1
evidence = Evidence()
code = pytest.main(['-q', '-p', 'no:cacheprovider', '--', *sys.argv[1:]], plugins=[evidence])
print(f'ODOO_MCP_PYTEST_PASSED={evidence.passed}', flush=True)
sys.exit(int(code) or (0 if evidence.passed else 5))
"""


class TestRunner:
    """Safe command runner for bounded QA commands."""

    def run(self, command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            with tempfile.TemporaryDirectory(prefix="odoo-mcp-check-") as temporary:
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPYCACHEPREFIX": str(Path(temporary) / "pycache"),
                       "XDG_CACHE_HOME": str(Path(temporary) / "cache"),
                       "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider"}
                completed = subprocess.run(
                    command, cwd=str(cwd), capture_output=True, text=True,
                    timeout=timeout_seconds, check=False, env=env,
                )
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=completed.stdout[-12000:],
                stderr=completed.stderr[-12000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=124,
                stdout=(exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )
        except OSError as exc:
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=127,
                stderr=f"Command could not start in cwd {cwd}: {exc}",
            )

    def git_status(self, cwd: Path, timeout_seconds: int = 20) -> CommandResult:
        return self.run(["git", "status", "--short", "--branch"], cwd, timeout_seconds)

    def pytest(
        self,
        cwd: Path,
        timeout_seconds: int = 90,
        targets: list[str] | None = None,
    ) -> CommandResult:
        from fastapi import HTTPException
        from ..execution_schemas import ProcessStart
        from .execution_service import ExecutionService
        from uuid import uuid4

        display_command = ["python", "-m", "pytest", "-q", "--", *(targets or [])]
        if not targets:
            return CommandResult(command=display_command, cwd=str(cwd), exit_code=5, stderr="No scoped tests selected")
        key = "qa-" + uuid4().hex
        deadline = time.monotonic() + timeout_seconds
        try:
            service = ExecutionService()
            workspace_id = service.open_qa_snapshot(cwd, key, timeout_seconds)
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise TimeoutError
            process = service.start_qa_process(ProcessStart(
                workspace_id=workspace_id, request_id=key,
                argv=["python", "-c", _PYTEST_CHECK, *targets], timeout_seconds=remaining,
            ))
            process_id = process["process_id"]
            while process["status"] in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    service.cancel(process_id, "process")
                    raise TimeoutError
                time.sleep(.25)
                process = service.poll(process_id, max_bytes=1)
            output = service.tail_process_output(process_id)
            match = re.search(r"(?:^|\n)ODOO_MCP_PYTEST_PASSED=(\d+)\s*\Z", output)
            passed = bool(match and int(match.group(1)) > 0 and process["status"] == "succeeded")
            return CommandResult(command=display_command, cwd=str(cwd), exit_code=0 if passed else 1,
                                 stdout=output, stderr="" if passed else "Isolated pytest did not produce passing test evidence")
        except TimeoutError:
            return CommandResult(command=display_command, cwd=str(cwd), exit_code=124, timed_out=True,
                                 stderr="Isolated QA exceeded its time budget")
        except (HTTPException, OSError):
            return CommandResult(command=display_command, cwd=str(cwd), exit_code=125,
                                 stderr="Isolated QA unavailable; check the execution worker and checkout")
