from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from ..schemas import (
    JobRecord,
    JobStatus,
    JobStep,
    OdooFlowTestRequest,
    OdooModuleTestRequest,
    SelfRunRequest,
)
from .background_executor import submit_background
from .client_resolver_service import ClientResolverService, ResolvedClient
from .functional_operation_service import FunctionalOperationService
from .odoo_endpoint_test_service import OdooEndpointTestService
from .odoosh_service import OdooShService
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .store_service import JsonStore


class ModuleTestService:
    """Coordinate static, native, endpoint and functional module verification."""

    _ODOO_MARKERS = (
        "from odoo",
        "import odoo",
        "odoo.tests",
        "transactioncase",
        "savepointcase",
        "httptestcase",
        "tagged(",
    )
    _SUMMARY_RE = re.compile(
        r"(?P<failed>\d+)\s+failed,\s*(?P<errors>\d+)\s+error(?:\(s\)|s)?\s+of\s+(?P<tests>\d+)\s+tests?",
        re.I,
    )

    def __init__(self) -> None:
        self.store = JsonStore()

    @staticmethod
    def _native_files(module_path: Path) -> list[Path]:
        tests_dir = module_path / "tests"
        if not tests_dir.is_dir():
            return []
        files = sorted(tests_dir.glob("test_*.py"))
        return [
            path for path in files
            if any(
                marker in path.read_text(encoding="utf-8", errors="ignore").lower()
                for marker in ModuleTestService._ODOO_MARKERS
            )
        ]

    @staticmethod
    def _resolve_module_path(project: Path, module_name: str) -> Path:
        direct = project / module_name
        if direct.is_dir() and (direct / "__manifest__.py").is_file():
            return direct
        matches = [
            project / item.path
            for item in RepoService().list_custom_modules(
                str(project),
                max_depth=5,
                include_uninstallable=True,
            )
            if item.module_name == module_name
        ]
        if not matches:
            raise HTTPException(status_code=404, detail=f"Odoo module not found: {module_name}")
        if len(matches) > 1:
            paths = [str(path.relative_to(project)) for path in matches]
            raise HTTPException(
                status_code=409,
                detail=f"Module name {module_name!r} is ambiguous in this checkout: {paths}",
            )
        return matches[0]

    @staticmethod
    def _requested_layers(request: OdooModuleTestRequest, native_files: list[Path]) -> list[str]:
        if request.layers == ["auto"]:
            layers = ["static"]
            if native_files:
                layers.append("native")
            if request.api_cases:
                layers.append("api")
            if request.functional_cases:
                layers.append("functional")
            return layers
        if request.layers == ["all"]:
            return ["static", "native", "api", "functional"]
        return list(request.layers)

    @staticmethod
    def _require_test_client(client: ResolvedClient | None, layers: list[str]) -> None:
        live = sorted(set(layers) & {"native", "api", "functional"})
        if live and (client is None or client.environment != "test"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Live module-test layers {live} require the selected ODOO_CLIENTS "
                    "entry to declare environment=test."
                ),
            )

    @staticmethod
    def _step(name: str, result: dict[str, Any], started: float) -> JobStep:
        return JobStep(
            name=name,
            status=str(result.get("status") or "failed"),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            result=result,
            error=result.get("error"),
        )

    def start(
        self,
        request: OdooModuleTestRequest,
        static_runner: Callable[[SelfRunRequest], Any],
    ) -> dict[str, Any]:
        project = resolve_allowed_path(request.project_path)
        if not project.is_dir():
            raise HTTPException(status_code=404, detail=f"Existing checkout not found: {project}")
        module_path = self._resolve_module_path(project, request.module_name)
        client = ClientResolverService().resolve_client(
            client_name=request.client_name,
            project_path=str(project),
            require_complete=False,
        )
        native_files = self._native_files(module_path)
        layers = self._requested_layers(request, native_files)
        self._require_test_client(client, layers)

        job = JobRecord(
            job_type="workflow.module_test",
            request={
                **request.model_dump(mode="json"),
                "resolved_layers": layers,
                "native_test_files": [path.relative_to(module_path).as_posix() for path in native_files],
            },
        )
        self.store.create_job(job)

        def background() -> None:
            active = self.store.get_job(job.id)
            active.status = JobStatus.running
            self.store.update_job(active)
            results: dict[str, Any] = {}
            try:
                if "static" in layers:
                    started = time.monotonic()
                    report = static_runner(
                        SelfRunRequest(
                            client_name=request.client_name,
                            project_path=str(module_path.parent),
                            module_name=request.module_name,
                            run_pytest=True,
                            test_strategy="auto",
                            include_git_status=False,
                            timeout_seconds=min(request.timeout_seconds, 600),
                        )
                    )
                    data = report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)
                    layer = {
                        "status": data.get("status", "failed"),
                        "passed": data.get("status") in {"passed", "warning"}
                        and not any(
                            item.get("severity") == "error"
                            for item in data.get("findings", [])
                        ),
                        "report": data,
                    }
                    results["static"] = layer
                    self.store.add_step(active, self._step("module_test.static", layer, started))

                if "native" in layers:
                    started = time.monotonic()
                    if not native_files:
                        layer = {
                            "status": "warning",
                            "passed": False,
                            "verified": False,
                            "error": "No Odoo-native tests were discovered in the module tests package.",
                        }
                    else:
                        command = OdooShService(client).run_module_tests(
                            request.module_name,
                            request.native_test_tags,
                            request.timeout_seconds,
                        )
                        text = f"{command.stdout}\n{command.stderr}"
                        matches = list(self._SUMMARY_RE.finditer(text))
                        summary = matches[-1].groupdict() if matches else None
                        proved = (
                            bool(summary)
                            and int(summary["tests"]) > 0
                            and int(summary["failed"]) == 0
                            and int(summary["errors"]) == 0
                        )
                        layer = {
                            "status": "passed" if command.exit_code == 0 and proved else "failed",
                            "passed": command.exit_code == 0 and proved,
                            "verified": proved,
                            "summary": {key: int(value) for key, value in summary.items()} if summary else None,
                            "command": command.model_dump(mode="json"),
                            "error": None if summary else "Odoo test summary was not found; native tests cannot be claimed as passed.",
                        }
                    results["native"] = layer
                    self.store.add_step(active, self._step("module_test.native", layer, started))

                if "api" in layers:
                    started = time.monotonic()
                    cases = (
                        [OdooEndpointTestService(client).run_case(case) for case in request.api_cases]
                        if request.api_cases else []
                    )
                    passed = bool(cases) and all(item.get("passed") is True for item in cases)
                    layer = {
                        "status": "passed" if passed else "warning" if not cases else "failed",
                        "passed": passed,
                        "verified": passed,
                        "cases": cases,
                        "error": None if cases else "No API cases were supplied.",
                    }
                    results["api"] = layer
                    self.store.add_step(active, self._step("module_test.api", layer, started))

                if "functional" in layers:
                    started = time.monotonic()
                    cases: list[dict[str, Any]] = []
                    operation_service = FunctionalOperationService()
                    for index, case in enumerate(request.functional_cases):
                        try:
                            result = operation_service.flow_test(
                                OdooFlowTestRequest(
                                    client_name=request.client_name,
                                    request_id=f"module-test:{job.id}:{index}:{case.name}",
                                    flow=case.flow,
                                    mode="persistent_test",
                                    values=case.values,
                                    stages=case.stages,
                                    verify_fields=case.verify_fields,
                                    steps=case.steps,
                                    approval_id=case.approval_id,
                                )
                            )
                            cases.append({"name": case.name, **result})
                        except Exception as exc:
                            cases.append({
                                "name": case.name,
                                "status": "failed",
                                "verified": False,
                                "success_allowed": False,
                                "error": str(exc),
                            })
                    passed = bool(cases) and all(
                        item.get("verified") is True and item.get("success_allowed") is True
                        for item in cases
                    )
                    layer = {
                        "status": "passed" if passed else "warning" if not cases else "failed",
                        "passed": passed,
                        "verified": passed,
                        "cases": cases,
                        "error": None if cases else "No functional cases were supplied.",
                    }
                    results["functional"] = layer
                    self.store.add_step(active, self._step("module_test.functional", layer, started))

                passed = bool(results) and all(item.get("passed") is True for item in results.values())
                final = {
                    "status": "passed" if passed else "failed",
                    "module_name": request.module_name,
                    "client_name": client.name if client else None,
                    "environment": client.environment if client else None,
                    "layers_requested": layers,
                    "layers": results,
                    "verified": passed,
                    "success_allowed": passed,
                    "writes_deployed": False,
                }
                self.store.finish_job(active, JobStatus.passed if passed else JobStatus.failed, final)
            except Exception as exc:
                self.store.finish_job(
                    active,
                    JobStatus.failed,
                    {
                        "status": "failed",
                        "module_name": request.module_name,
                        "layers_requested": layers,
                        "layers": results,
                        "verified": False,
                        "success_allowed": False,
                        "writes_deployed": False,
                        "error": str(exc),
                    },
                )

        submit_background(background)
        return self.store.get_job(job.id).model_dump(mode="json")
