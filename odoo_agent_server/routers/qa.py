from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import Finding, SelfRunReport, SelfRunRequest, Severity
from ..services.path_service import resolve_allowed_path
from ..services.test_runner import TestRunner
from ..services.validator_service import OdooModuleValidator

router = APIRouter(prefix="/qa", tags=["qa"], dependencies=[AuthDependency])


def _module_test_strategy(module_path, project_path, requested: str) -> tuple[str, list[str]]:
    module_tests = sorted((module_path / "tests").glob("test_*.py")) if (module_path / "tests").is_dir() else []
    project_tests = sorted((project_path / "tests").glob("test_*.py")) if (project_path / "tests").is_dir() else []
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
    if project_path_valid and payload.run_pytest:
        selected_test_strategy, selected_test_targets = _module_test_strategy(
            module_path,
            project_path,
            payload.test_strategy,
        )
        if selected_test_strategy == "pytest":
            pytest_result = runner.pytest(
                project_path,
                timeout_seconds=payload.timeout_seconds,
                targets=selected_test_targets,
            )
            commands.append(pytest_result)
            if pytest_result.exit_code != 0:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="PYTEST_FAILED",
                        message="pytest failed.",
                        hint=(pytest_result.stderr or pytest_result.stdout)[-4000:],
                    )
                )
        elif selected_test_strategy == "odoo_native":
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
            findings.append(
                Finding(
                    severity=Severity.info,
                    code="NO_TESTS_FOUND",
                    message="No tests folder found. Static validation completed only.",
                )
            )

    error_count = sum(1 for item in findings if item.severity == Severity.error)
    warning_count = sum(1 for item in findings if item.severity == Severity.warning)
    status = "failed" if error_count else "warning" if warning_count else "passed"

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
            "module_structure_complete": error_count == 0,
            "test_strategy": selected_test_strategy,
            "test_targets": selected_test_targets[:20],
        },
    )
