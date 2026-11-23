"""Concurrent real clone/edit/test/commit workload in disposable repositories.

python -m tests.benchmarks.orchestration --tasks 4 --workers 2
Add --docker on a Docker host to exercise the real isolated command runner.
No production clients, remote pushes, model calls, or Odoo database operations.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import faulthandler
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from odoo_agent_server.config import settings
from odoo_agent_server.execution_worker import ExecutionWorker
from odoo_agent_server.routers.tools import _dispatch
from odoo_agent_server.services.execution_service import ExecutionService
from odoo_agent_server.services.task_service import TaskService
from odoo_agent_server.task_schemas import TaskOpen, TaskRun


def summary(values):
    values = sorted(values)
    return {"p50_ms": round(statistics.median(values), 2), "p95_ms": round(values[max(0, int(len(values)*.95)-1)], 2)}


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()


async def workload(root, tasks, workers, docker, debug=False):
    source = root / "source"
    source.mkdir()
    git(source, "init", "-b", "staging")
    git(source, "config", "user.name", "Orchestration benchmark")
    git(source, "config", "user.email", "benchmark@example.invalid")
    (source / "addon").mkdir()
    (source / "addon/__init__.py").write_text("")
    (source / "addon/__manifest__.py").write_text("{'name':'Benchmark','depends':['base']}\n")
    (source / "addon/calc.py").write_text("def total(n):\n    return 0\n")
    (source / "tests").mkdir()
    (source / "tests/test_calc.py").write_text(
        "import unittest,resource\nfrom addon.calc import total\n"
        "class TestCalculation(unittest.TestCase):\n"
        " def test_values(self):\n"
        "  for n in (0,1,10,10000): self.assertEqual(total(n),n*(n+1)//2)\n"
        " def test_workload(self):\n"
        "  for _ in range(100): self.assertEqual(total(100000),5000050000)\n"
        "  print('BENCHMARK_RSS='+str(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Fixture with failing calculation")
    base = git(source, "rev-parse", "HEAD")
    bare = root / "origin.git"
    git(root, "clone", "--bare", str(source), str(bare))
    git(source, "remote", "add", "origin", str(bare))
    client = {"name":"Benchmark","repo":str(bare),"path":str(source),"branch":"staging"}
    with ExitStack() as patches:
        patches.enter_context(patch.dict(os.environ, {"ODOO_CLIENTS":json.dumps([client])}))
        for key, value in {"execution_enabled":True,"execution_root":str(root / "execution"),
                           "state_dir":str(root / "state"),"allowed_roots_raw":str(root),
                           "target_project_path":None,"execution_max_workers":workers,
                           "execution_memory":"128m","require_approval":False}.items():
            patches.enter_context(patch.object(settings, key, value))
        service = ExecutionService()
        worker = ExecutionWorker(service=service)
        if docker:
            await asyncio.to_thread(worker.refresh_capabilities)
            capabilities = service.store._read(service.store.root / "capabilities.json") or {}
            if not capabilities.get("python") or not capabilities.get("image_id"):
                raise RuntimeError("Docker execution image is unavailable; no workload was started")
        else:
            worker.command = lambda record: record["request"]["argv"]
            worker.remove_container = lambda _: True  # host mode creates no containers
            service.store._write(service.store.root / "capabilities.json", {
                "image":settings.execution_image,"image_id":"host-benchmark","python":True,"odoo":False})
        ledger = TaskService()
        queue_wait, command_times, child_rss = [], [], []
        stop = asyncio.Event()

        async def execute(record):
            queue_wait.append((record["updated_at"]-record["created_at"])*1000)
            started = time.perf_counter()
            await asyncio.to_thread(worker.execute, record)
            command_times.append((time.perf_counter()-started)*1000)
            result = service.poll(record["id"])
            if result["status"] != "succeeded":
                raise RuntimeError("Acceptance command failed: " + str(result))
            for line in result["output"].splitlines():
                if line.startswith("BENCHMARK_RSS="):
                    raw = int(line.split("=",1)[1])
                    child_rss.append(raw/1024 if docker or sys.platform != "darwin" else raw/1024/1024)

        async def commands():
            active = set()
            try:
                while not stop.is_set() or active:
                    for finished in [item for item in active if item.done()]:
                        active.remove(finished)
                        await finished
                    if len(active) < workers:
                        record = await asyncio.to_thread(service.store.claim, "process")
                        if record:
                            active.add(asyncio.create_task(execute(record)))
                            continue
                    await asyncio.sleep(.01)
            finally:
                await asyncio.gather(*active, return_exceptions=True)

        async def coding_task(index):
            start = time.perf_counter()
            opened = await asyncio.to_thread(ledger.open, TaskOpen(client_name="Benchmark", request_id=f"benchmark-task-{index:04}",
                description="Repair calculation, run syntax and regression tests, commit locally", target="coding", module_name="addon"))
            tid = opened["task_id"]
            async def step(name, arguments):
                if debug:
                    print(f"task={index} start={name}", file=sys.stderr, flush=True)
                request = TaskRun(task_id=tid, tool=name, arguments=arguments)
                retry_deadline = time.monotonic()+180
                while True:
                    result = await ledger.run(request, _dispatch)
                    if result.get("status") != "task_busy":
                        break
                    if time.monotonic() > retry_deadline:
                        raise TimeoutError("Coordinator remained busy without dispatching the benchmark step")
                    await asyncio.sleep(result.get("retry_after_ms",200)/1000)
                if result.get("state_class") == "blocked":
                    raise RuntimeError("Workload blocked: " + str(result))
                if debug:
                    print(f"task={index} finished={name} status={result.get('status')}", file=sys.stderr, flush=True)
                return result.get("tool_result", {})
            opened_ws = await step("execution_workspace_open", {"project_path":str(source),"request_id":f"benchmark-workspace-{index:04}"})
            wid = opened_ws["workspace_id"]
            deadline = time.monotonic()+180
            while service.store.get(wid,"workspace")["status"] == "initializing":
                if time.monotonic() > deadline:
                    raise TimeoutError("Workspace initialization timed out")
                await asyncio.sleep(.02)
            current = await step("execution_files", {"operation":"read","path":"addon/calc.py"})
            await step("execution_files", {"operation":"write","path":"addon/calc.py","expected_sha256":current["sha256"],
                                           "content":"def total(n):\n    return sum(range(n+1))\n"})
            python = "python" if docker else sys.executable
            checks = [{"name":"syntax","layer":"syntax","argv":[python,"-B","-c","import ast,pathlib; ast.parse(pathlib.Path('addon/calc.py').read_text())"]},
                      {"name":"regression","layer":"unit","argv":[python,"-B","-m","unittest","discover","-s","tests","-v"]}]
            await step("execution_task_plan", {"checks":checks,"required_layers":["syntax","unit"]})
            control_calls = 0
            while time.monotonic() < deadline:
                state = await ledger.run(TaskRun(task_id=tid), _dispatch)
                control_calls += 1
                if state.get("success_allowed"):
                    workspace = service.workspace(wid)
                    if not workspace.get("published_commit"):
                        await step("execution_publish", {"push":False,"message":"Verified benchmark repair",
                                                         "request_id":f"benchmark-commit-{index:04}"})
                        await asyncio.sleep(.05)
                        continue
                    assert workspace.get("pushed") is False
                    assert git(service.root(wid),"rev-parse","HEAD") != base
                    return {"task":index,"elapsed_ms":round((time.perf_counter()-start)*1000,2),"control_calls":control_calls,
                            "verified":state["task"]["claims"].get("coding_verified") is True}
                if state.get("state_class") == "blocked" or state.get("input_required"):
                    raise RuntimeError("Guided workflow stopped: " + str(state))
                if not state.get("next_call") and state.get("argument_request"):
                    raise RuntimeError("Benchmark plan needs explicit arguments: " + str(state["argument_request"]))
                await asyncio.sleep(.05)
            raise TimeoutError("Coding workflow exceeded 180-second budget: " + str({
                "task":index,"phase":state.get("status"),"next_call":state.get("next_call"),
                "workspace_phase":service.workspace(wid).get("phase")}))

        started = time.perf_counter()
        runner = asyncio.create_task(commands())
        jobs = asyncio.gather(*(coding_task(index) for index in range(tasks)))
        try:
            done, _ = await asyncio.wait({jobs, runner}, return_when=asyncio.FIRST_COMPLETED)
            if runner in done:
                await runner  # propagate a worker error before waiting for task timeouts
                raise RuntimeError("Command runner stopped before the workloads completed")
            results = await jobs
        finally:
            if not jobs.done():
                jobs.cancel()
            await asyncio.gather(jobs, return_exceptions=True)
            stop.set()
            await runner
        assert all(result["verified"] for result in results)
        assert git(source,"rev-parse","HEAD") == base and git(source,"status","--porcelain") == ""
        assert git(root,"--git-dir",str(bare),"rev-parse","staging") == base
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"scope":"disposable real Git clones, edits, acceptance tests and verified local commits; no pushes/deployment/ORM",
                "runner":"docker" if docker else "host subprocess", "tasks":tasks,"workers":workers,
                "elapsed_ms":round((time.perf_counter()-started)*1000,2),"task_latency":summary([r["elapsed_ms"] for r in results]),
                "queue_wait":summary(queue_wait),"command_duration":summary(command_times),"commands":len(command_times),
                "coordinator_peak_rss_mb":round(peak/1024/1024 if sys.platform=="darwin" else peak/1024,2),
                "unit_process_peak_rss_mb":round(max(child_rss,default=0),2),"source_and_remote_unchanged":True,"results":results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Log benchmark stages and dump blocked stacks every 60 seconds")
    args = parser.parse_args()
    if not 1 <= args.tasks <= 8 or not 1 <= args.workers <= 4:
        parser.error("tasks must be 1..8; workers must be 1..4")
    with tempfile.TemporaryDirectory(prefix="odoo-coding-benchmark-") as directory:
        if args.debug:
            faulthandler.dump_traceback_later(60, repeat=True)
        try:
            print(json.dumps(asyncio.run(workload(Path(directory),args.tasks,args.workers,args.docker,args.debug)),indent=2))
        finally:
            if args.debug:
                faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
