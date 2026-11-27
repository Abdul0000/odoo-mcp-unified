from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import Finding, SelfRunReport, SelfRunRequest, Severity
from ..services.path_service import resolve_allowed_path
from ..services.test_runner import TestRunner
from ..services.validator_service import OdooModuleValidator

router = APIRouter(prefix="/qa", tags=["qa"], dependencies=[AuthDependency])


def _module_test_strategy(module_path, project_path, requested: str, *, module_only: bool = False) -> tuple[str, list[str]]:
    module_tests = sorted((module_path / "tests").glob("test_*.py")) if (module_path / "tests").is_dir() else []
    project_tests = sorted((project_path / "tests").glob("test_*.py")) if not module_only else []
    if requested == "none":
        return "none", []
    if requested == "odoo_native":
        return "odoo_native", [str(path) for path in module_tests]
    if requested == "pytest":
        targets = module_tests or project_tests
        return "pytest", [path.relative_to(project_path).as_posix() for path in targets]
    if module_tests:
        odoo_markers = (
            "from odoo",
            "import odoo",
            "odoo.tests",
            "transactioncase",
            "savepointcase",
            "httptestcase",
            "tagged(",
        )
        for path in module_tests:
            try:
                source = path.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeDecodeError):
                continue
            if any(marker in source for marker in odoo_markers):
                return "odoo_native", [str(item) for item in module_tests]
        return "pytest", [path.relative_to(project_path).as_posix() for path in module_tests]
    if project_tests:
        return "pytest", [path.relative_to(project_path).as_posix() for path in project_tests]
    return "none", []


@router.post("/self-run", response_model=SelfRunReport)
def self_run(payload: SelfRunRequest) -> SelfRunReport:
    project_path = resolve_allowed_path(payload.project_path)
    module_path = project_path / (payload.module_root_rel or payload.module_name)

    findings: list[Finding] = []
    commands = []

    validator = OdooModuleValidator()
    findings.extend(validator.validate(
        module_path,
        task_description=payload.task_description,
        create_module=payload.create_module,
        module_contract=payload.module_contract,
    ))
    static_status = (
        "failed" if any(item.severity == Severity.error for item in findings)
        else "warning" if any(item.severity == Severity.warning for item in findings)
        else "passed"
    )

    project_path_valid = project_path.exists() and project_path.is_dir()
    if not project_path_valid:
        reason = "Project path is missing." if not project_path.exists() else "Project path is not a directory."
        findings.insert(
            0,
            Finding(
                severity=Severity.error,
                code="PROJECT_PATH_NOT_FOUND",
                message=f"{reason} Cannot run QA commands: {project_path}",
                hint="Verify project_path before running qa.self_run or workflow.safe_deploy_module.",
            ),
        )

    runner = TestRunner()
    if project_path_valid and payload.include_git_status:
        git_result = runner.git_status(project_path)
        commands.append(git_result)
        if git_result.exit_code not in (0, 128):
            findings.append(
                Finding(
                    severity=Severity.warning,
                    code="GIT_STATUS_FAILED",
                    message="Could not read git status.",
                    hint=git_result.stderr or git_result.stdout,
                )
            )

    selected_test_strategy = "none"
    selected_test_targets: list[str] = []
    runtime_status = "not_requested"
    if project_path_valid and payload.run_pytest:
        selected_test_strategy, selected_test_targets = _module_test_strategy(
            module_path,
            project_path,
            payload.test_strategy,
            module_only=payload.require_runtime_tests,
        )
        if selected_test_strategy == "pytest" and selected_test_targets:
            pytest_result = runner.pytest(
                project_path,
                timeout_seconds=payload.timeout_seconds,
                targets=selected_test_targets,
            )
            commands.append(pytest_result)
            runtime_status = {0: "passed", 125: "unavailable"}.get(pytest_result.exit_code, "failed")
            if runtime_status == "failed":
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="PYTEST_FAILED",
                        message="pytest failed.",
                        hint=(pytest_result.stderr or pytest_result.stdout)[-4000:],
                    )
                )
        elif selected_test_strategy == "odoo_native":
            runtime_status = "deferred"
            findings.append(
                Finding(
                    severity=Severity.info,
                    code="ODOO_NATIVE_TESTS_DEFERRED",
                    message="Odoo-native addon tests were not collected with plain pytest.",
                    hint=(
                        "Static validation continues now; execute these tests in an Odoo test-enabled environment. "
                        "A successful module upgrade proves registry/load compatibility but is not a claim that native tests ran."
                    ),
                )
            )
        else:
            runtime_status = "skipped" if payload.test_strategy == "none" else "no_tests_found"
            findings.append(
                Finding(
                    severity=Severity.info,
                    code="NO_TESTS_FOUND",
                    message="Runtime tests were explicitly skipped." if payload.test_strategy == "none" else "No tests found. Static validation completed only.",
                )
            )
    elif payload.run_pytest:
        runtime_status = "unavailable"

    if payload.require_runtime_tests and runtime_status not in {"passed", "failed"}:
        findings.append(Finding(
            severity=Severity.error,
            code="REQUIRED_RUNTIME_TESTS_UNVERIFIED",
            message=f"Required runtime tests did not pass: {runtime_status}.",
            hint="For native Odoo tests, run the required checks in a test-enabled execution workspace.",
        ))

    error_count = sum(1 for item in findings if item.severity == Severity.error)
    warning_count = sum(1 for item in findings if item.severity == Severity.warning)
    incomplete = runtime_status in {"deferred", "no_tests_found", "unavailable"}
    status = "failed" if error_count else "warning" if warning_count or incomplete else "passed"

    return SelfRunReport(
        status=status,
        project_path=str(project_path),
        module_path=str(module_path),
        module_name=payload.module_name,
        findings=findings,
        commands=commands,
        summary={
            "errors": error_count,
            "warnings": warning_count,
            "infos": sum(1 for item in findings if item.severity == Severity.info),
            "commands_run": len(commands),
            "project_path_valid": project_path_valid,
            "module_structure_complete": static_status != "failed",
            "static_status": static_status,
            "runtime_test_status": runtime_status,
            "test_strategy": selected_test_strategy,
            "test_targets": selected_test_targets[:20],
        },
    )
