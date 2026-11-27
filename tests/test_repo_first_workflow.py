from pathlib import Path

from odoo_agent_server.schemas import CodingTaskPlanRequest, RepoPrepareRequest
from odoo_agent_server.routers.tools import _TOOL_DESCRIPTORS, tool_input_schema
from odoo_agent_server.config import settings
from odoo_agent_server.services.repo_service import RepoService


def test_new_repo_first_tools_are_listed():
    names = {tool.name for tool in _TOOL_DESCRIPTORS}
    assert "repo.prepare" in names
    assert "repo.list_custom_modules" in names
    assert "workflow.coding_task_plan" in names


def test_repo_prepare_schema_contains_repo_branch():
    schema = tool_input_schema("repo.prepare")
    props = schema["properties"]
    assert "repo_url" in props
    assert "branch" in props


def test_coding_task_plan_schema_requires_task():
    model = CodingTaskPlanRequest(task="Add field test_mcp")
    assert model.task == "Add field test_mcp"


def test_list_custom_modules_discovers_manifest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    module = tmp_path / "my_custom_module"
    module.mkdir()
    (module / "__manifest__.py").write_text("{'name': 'My Custom Module', 'version': '18.0.1.0.0', 'installable': True, 'author': 'Tester'}", encoding="utf-8")
    modules = RepoService().list_custom_modules(str(tmp_path))
    assert [item.module_name for item in modules] == ["my_custom_module"]
    assert modules[0].name == "My Custom Module"


def test_repo_write_single_pass_threshold_is_1000_lines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    service = RepoService()

    exactly_1000 = service.write_file(str(project), "exactly_1000.txt", "x\n" * 1000)
    over_1000 = service.write_file(str(project), "over_1000.txt", "x\n" * 1001)
    chunk_start = service.write_file(str(project), "chunked.txt", "x\n" * 600, chunk_index=0, total_chunks=2)
    chunk_next = service.write_file(str(project), "chunked.txt", "x\n" * 600, chunk_index=1, total_chunks=2)

    assert "large_file_warning" not in exactly_1000.extra
    assert "large_file_warning" in over_1000.extra
    assert "1000 lines" in over_1000.extra["large_file_warning"]
    assert "large_file_warning" not in chunk_start.extra
    assert "large_file_warning" not in chunk_next.extra


def test_failed_clone_does_not_leave_an_incomplete_target(tmp_path, monkeypatch):
    from odoo_agent_server.schemas import CommandResult
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    target = tmp_path / "client"
    def failed_download(self, command, cwd, timeout_seconds, env=None):
        staging = Path(command[-1])
        (staging / ".git").mkdir()
        (staging / ".git" / "index.lock").write_text("interrupted")
        return CommandResult(command=command, cwd=str(cwd), exit_code=124, timed_out=True, stderr="clone timed out")
    monkeypatch.setattr(RepoService, "_run", failed_download)
    result = RepoService()._clone_checkout(target, "https://gitlab.internal/acme/a.git", "dev", 1, {})
    assert result.timed_out
    assert not target.exists()
    assert not list(tmp_path.glob(".client.clone-*"))


def test_repo_timeout_stops_descendant_processes(tmp_path):
    import os
    import signal
    import sys
    import time
    import pytest
    if os.name == "nt":
        pytest.skip("Oracle process-group cleanup is POSIX-specific")
    child = None
    script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
    try:
        result = RepoService()._run([sys.executable, "-u", "-c", script], tmp_path, 1)
        child = int(result.stdout.strip().splitlines()[0])
        assert result.exit_code == 124 and result.timed_out
        assert "timed out after 1s" in result.stderr
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            try:
                if Path(f"/proc/{child}/stat").read_text().split()[2] == "Z":
                    break  # Dead child awaiting reaping by the container's PID 1.
            except (FileNotFoundError, ProcessLookupError):
                break  # The child was reaped between checks.
            time.sleep(0.02)
        else:
            raise AssertionError("Timed-out command left its child running")
    finally:
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
