from __future__ import annotations

import ast
import csv
from fnmatch import fnmatch
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..schemas import Finding, Severity

COMMON_ODOO_FIELDS = {
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date", "__last_update",
    "message_ids", "message_follower_ids", "message_partner_ids", "activity_ids", "activity_state",
    "activity_type_id", "activity_user_id", "activity_date_deadline", "activity_exception_decoration",
    "active", "sequence", "company_id",
}
FIELD_TYPES_WITH_COMODEL = {"Many2one", "One2many", "Many2many"}
INHERIT_DEPENDS = {
    "account.": "account",
    "sale.": "sale",
    "purchase.": "purchase",
    "stock.": "stock",
    "hr.": "hr",
    "product.": "product",
    "payment.": "payment",
    "website.": "website",
    "calendar.": "calendar",
}

# Lightweight transitive dependency hints used only to reduce noisy static
# warnings.  Odoo validates real dependencies during module install/upgrade;
# these hints stop style-only tasks from being polluted by warnings such as
# ``website_sale`` not directly depending on ``sale``.
TRANSITIVE_DEPENDS_HINTS = {
    "website_sale": {"website", "sale", "product"},
    "sale_management": {"sale", "product"},
    "stock_account": {"stock", "account", "product"},
    "mrp": {"stock", "product"},
    "website_payment": {"website", "payment"},
}


@dataclass
class ParsedModel:
    technical_name: str
    inherited_model: str | None = None
    fields: set[str] = field(default_factory=set)
    relations: dict[str, str] = field(default_factory=dict)
    transient: bool = False
    file: Path | None = None


class OdooModuleValidator:
    """Static checks that catch common module errors before Odoo.sh build time."""

    _NON_PACKAGE_WORDS = {
        "root", "extension", "python", "module", "addon", "source", "code",
        "file", "files", "structure", "custom", "base", "odoo",
    }

    def validate(
        self,
        module_path: Path,
        task_description: str = "",
        create_module: bool = False,
        module_contract: dict | None = None,
    ) -> list[Finding]:
        findings = self._validate_basic_structure(module_path)
        if any(finding.code in {"MODULE_NOT_FOUND", "MODULE_NOT_DIRECTORY"} for finding in findings):
            return findings
        manifest, manifest_findings = self._read_manifest(module_path)
        findings.extend(manifest_findings)
        findings.extend(self._validate_manifest_version_unchanged(module_path, manifest))
        findings.extend(self._validate_manifest_files(module_path, manifest))
        findings.extend(self._validate_python_package_inits(module_path))
        findings.extend(self._validate_import_closure(module_path))
        findings.extend(self._validate_manifest_registration(module_path, manifest))
        findings.extend(self._validate_implementation_completeness(module_path))
        findings.extend(self._validate_odoo_controllers(module_path))
        findings.extend(self._validate_python_files(module_path))
        findings.extend(self._validate_xml_files(module_path))
        findings.extend(self._validate_security_csv(module_path))
        findings.extend(self._detect_pyc(module_path))
        parsed_models = self._parse_custom_models(module_path)
        findings.extend(self._validate_missing_access(module_path, parsed_models))
        findings.extend(self._validate_manifest_dependency_hints(module_path, manifest, parsed_models))
        findings.extend(self._validate_xml_references_and_order(module_path, manifest, parsed_models))
        findings.extend(self._validate_custom_view_fields(module_path, parsed_models))
        findings.extend(self._validate_requested_features(module_path, manifest, parsed_models, task_description, create_module, module_contract))
        return findings

    def _validate_basic_structure(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        if not module_path.exists():
            return [Finding(severity=Severity.error, code="MODULE_NOT_FOUND", message=f"Module path does not exist: {module_path}", hint="Check project_path and module_name.")]
        if not module_path.is_dir():
            return [Finding(severity=Severity.error, code="MODULE_NOT_DIRECTORY", message=f"Module path is not a directory: {module_path}")]
        manifest = module_path / "__manifest__.py"
        if not manifest.exists():
            findings.append(Finding(severity=Severity.error, code="MISSING_MANIFEST", message="Missing __manifest__.py", file=str(manifest)))
        init_file = module_path / "__init__.py"
        if not init_file.exists():
            findings.append(Finding(severity=Severity.error, code="MISSING_INIT", message="Missing mandatory __init__.py", file=str(init_file), hint="Create __init__.py before commit/push."))
        return findings

    def _read_manifest(self, module_path: Path) -> tuple[dict, list[Finding]]:
        manifest_path = module_path / "__manifest__.py"
        if not manifest_path.exists():
            return {}, []
        try:
            manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return {}, [Finding(severity=Severity.error, code="MANIFEST_SYNTAX_ERROR", message=str(exc), file=str(manifest_path), line=exc.lineno)]
        except Exception as exc:  # noqa: BLE001
            return {}, [Finding(severity=Severity.error, code="MANIFEST_PARSE_ERROR", message=f"Could not parse manifest as a Python literal dict: {exc}", file=str(manifest_path))]
        if not isinstance(manifest, dict):
            return {}, [Finding(severity=Severity.error, code="MANIFEST_NOT_DICT", message="Manifest must be a Python dictionary.", file=str(manifest_path))]
        findings: list[Finding] = []
        for required_key in ("name", "version", "depends"):
            if required_key not in manifest:
                findings.append(Finding(severity=Severity.error, code="MANIFEST_MISSING_KEY", message=f"Manifest is missing mandatory key: {required_key}", file=str(manifest_path)))
        depends = manifest.get("depends", [])
        if not isinstance(depends, list) or not all(isinstance(dep, str) for dep in depends):
            findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_DEPENDS", message="Manifest 'depends' must be a list of module technical names.", file=str(manifest_path)))
        return manifest, findings

    def _validate_manifest_files(self, module_path: Path, manifest: dict) -> list[Finding]:
        findings: list[Finding] = []
        manifest_path = module_path / "__manifest__.py"
        for key in ("data", "demo", "qweb"):
            entries = manifest.get(key, []) or []
            if not isinstance(entries, list):
                findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_FILE_LIST", message=f"Manifest '{key}' must be a list of file paths.", file=str(manifest_path)))
                continue
            for rel_path in entries:
                if not isinstance(rel_path, str):
                    findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_FILE_ENTRY", message=f"Manifest '{key}' contains a non-string file entry.", file=str(manifest_path)))
                    continue
                target = module_path / rel_path
                if not target.exists():
                    findings.append(Finding(severity=Severity.error, code="MANIFEST_FILE_MISSING", message=f"Manifest references missing file: {rel_path}", file=str(manifest_path), hint="Create the file or remove it from the manifest."))
        return findings

    def _validate_manifest_version_unchanged(self, module_path: Path, manifest: dict) -> list[Finding]:
        """Existing addons may never mutate the manifest version."""
        manifest_path = module_path / "__manifest__.py"
        if not manifest_path.is_file():
            return []
        try:
            root_result = subprocess.run(
                ["git", "-C", str(module_path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if root_result.returncode != 0:
                return []
            repo_root = Path(root_result.stdout.strip()).resolve()
            relative_manifest = manifest_path.resolve().relative_to(repo_root).as_posix()
            baseline_result = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"HEAD:{relative_manifest}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        # An untracked/new addon has no baseline version and may set its initial version.
        if baseline_result.returncode != 0:
            return []
        try:
            baseline = ast.literal_eval(baseline_result.stdout)
        except (SyntaxError, ValueError):
            return []
        if not isinstance(baseline, dict):
            return []
        baseline_version = baseline.get("version")
        current_version = manifest.get("version")
        if baseline_version == current_version:
            return []
        return [Finding(
            severity=Severity.error,
            code="MANIFEST_VERSION_CHANGED",
            message=f"Manifest version must remain unchanged: {baseline_version!r} -> {current_version!r}.",
            file=str(manifest_path),
            hint=f"Restore the existing version exactly to {baseline_version!r}; version changes are forbidden for connector code tasks.",
        )]

    @staticmethod
    def _relative_imports(init_path: Path) -> set[str]:
        try:
            tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
        except Exception:
            return set()
        imported: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level < 1:
                continue
            if node.module:
                imported.add(node.module.split(".", 1)[0])
            else:
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        return imported

    @staticmethod
    def _relative_import_targets(init_path: Path) -> set[str]:
        try:
            tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
        except Exception:
            return set()
        targets: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module:
                targets.add(node.module.split(".", 1)[0])
            else:
                targets.update(alias.name.split(".", 1)[0] for alias in node.names)
        return targets

    def _validate_import_closure(self, module_path: Path) -> list[Finding]:
        """Require every functional Python package/file to be reachable from root."""
        findings: list[Finding] = []
        excluded = {"tests", "test", "migrations", "static", "__pycache__"}
        directories = [module_path]
        directories.extend(
            path for path in module_path.rglob("*")
            if path.is_dir() and not any(part in excluded or part.startswith(".") for part in path.relative_to(module_path).parts)
        )
        for directory in directories:
            py_modules = sorted(path.stem for path in directory.glob("*.py") if path.name not in {"__init__.py", "__manifest__.py", "__openerp__.py"})
            child_packages = sorted(
                child.name for child in directory.iterdir()
                if child.is_dir() and child.name not in excluded and not child.name.startswith(".")
                and any(path.is_file() and path.suffix == ".py" for path in child.rglob("*.py"))
            )
            expected = set(py_modules + child_packages)
            if not expected:
                continue
            init_path = directory / "__init__.py"
            if not init_path.is_file():
                # Reported by the independent file-driven package invariant.
                continue
            for target in sorted(self._relative_import_targets(init_path)):
                if not (directory / f"{target}.py").is_file() and not (directory / target / "__init__.py").is_file():
                    findings.append(Finding(
                        severity=Severity.error,
                        code="PYTHON_IMPORT_TARGET_MISSING",
                        message=f"{init_path.relative_to(module_path)} imports missing module/package: {target}",
                        file=str(init_path),
                        hint=f"Create {target}.py or {target}/__init__.py, or remove the invalid import.",
                    ))
            missing = expected - self._relative_imports(init_path)
            if missing:
                findings.append(Finding(
                    severity=Severity.error,
                    code="PYTHON_IMPORT_CLOSURE_MISSING",
                    message=f"{init_path.relative_to(module_path)} does not import: {', '.join(sorted(missing))}",
                    file=str(init_path),
                    hint="Add relative imports before commit/push.",
                ))
        return findings

    def _validate_python_package_inits(self, module_path: Path) -> list[Finding]:
        """Every addon directory containing Python source must be a package.

        This is deliberately file-driven and independent from import-closure
        inference, so standard folders such as controllers/wizard/tests and
        arbitrary nested packages cannot be skipped accidentally.
        """
        findings: list[Finding] = []
        exempt_parts = {"migrations", "static", "__pycache__"}
        directories: set[Path] = set()
        for py_file in module_path.rglob("*.py"):
            try:
                relative_parts = py_file.relative_to(module_path).parts
            except ValueError:
                continue
            if any(part in exempt_parts or part.startswith(".") for part in relative_parts[:-1]):
                continue
            if py_file.name in {"__init__.py", "__manifest__.py", "__openerp__.py"}:
                continue
            directories.add(py_file.parent)

        for directory in sorted(directories, key=lambda item: item.as_posix()):
            init_path = directory / "__init__.py"
            if init_path.is_file():
                continue
            python_children = sorted(
                path.stem for path in directory.glob("*.py")
                if path.name not in {"__init__.py", "__manifest__.py", "__openerp__.py"}
            )
            rel = directory.relative_to(module_path)
            display = "." if not rel.parts else rel.as_posix()
            findings.append(Finding(
                severity=Severity.error,
                code="PYTHON_PACKAGE_INIT_MISSING",
                message=f"Python source folder is missing mandatory __init__.py: {display}",
                file=str(init_path),
                hint=f"Create {display}/__init__.py and import: {', '.join(python_children)}.",
            ))
        return findings

    @staticmethod
    def tests_explicitly_requested(task_description: str) -> bool:
        """Distinguish requested test files from a test-pass verification gate."""
        lowered = str(task_description or "").lower()
        without_pass_gates = re.sub(
            r"\b(?:existing\s+)?(?:mocked\s+|unit\s+|regression\s+|focused\s+)?"
            r"tests?\s+(?:that\s+)?(?:(?:must|should|need(?:s)?\s+to)\s+)?pass(?:ing|ed)?\b",
            "",
            lowered,
        )
        return bool(
            re.search(
                r"\b(?:add|create|write|implement|include|provide|require)\b"
                r"(?:(?![.!?]).){0,160}\b"
                r"(?:mocked\s+|unit\s+|regression\s+|focused\s+)?tests?\b|"
                r"\btests?\s+(?:covering|to\s+cover|for\s+coverage)\b|"
                r"\b(?:mocked\s+|unit\s+|regression\s+|focused\s+)?tests?\s+"
                r"(?:are\s+)?required\b|"
                r"\btest\s+coverage\b",
                without_pass_gates,
                flags=re.DOTALL,
            )
        )

    @staticmethod
    def _contract_term_is_positive(text: str, term: str) -> bool:
        """Return whether a contract term is requested, not merely preserved.

        Task prompts commonly lock scope with phrases such as ``preserve
        security`` or ``no changes to actions``.  Those references describe a
        non-goal and must not manufacture a required module layer.  The nearest
        directive in the same clause wins, so ``preserve security and add a
        form view`` still requests the view.
        """
        directive = re.compile(
            r"\b(?:"
            r"preserve|keep|retain|leave|avoid|without|"
            r"do\s+not|don't|must\s+not|no|no\s+changes?\s+(?:to|in)|"
            r"add|create|implement|include|require|update|modify|change|build"
            r")\b",
            re.I,
        )
        negative = re.compile(
            r"^(?:preserve|keep|retain|leave|avoid|without|do\s+not|don't|must\s+not|no|no\s+changes?)$",
            re.I,
        )
        for match in re.finditer(re.escape(term), text, flags=re.I):
            clause_start = max(
                text.rfind(separator, 0, match.start())
                for separator in (".", ";", "\n")
            ) + 1
            prefix = text[clause_start:match.start()]
            directives = list(directive.finditer(prefix))
            if directives and negative.match(directives[-1].group(0).strip()):
                continue
            suffix = text[match.end():match.end() + 50]
            if re.match(r"\s+(?:must\s+remain\s+)?(?:unchanged|unmodified|as[- ]is)\b", suffix, re.I):
                continue
            return True
        return False

    @staticmethod
    def requested_contract(task_description: str, create_module: bool = False) -> dict:
        text = str(task_description or "")
        lowered = text.lower()
        tests_explicitly_requested = OdooModuleValidator.tests_explicitly_requested(text)
        full_module = bool(
            create_module and re.search(r"\b(?:complete|full|comprehensive|fully functional|installable)\b|\ball necessary files\b|\bdeliverables\b|\bmvp\b", lowered)
        ) or bool(re.search(r"\b(?:complete|full|comprehensive)\s+(?:odoo\s+)?(?:module|addon)\b|\bmvp\b", lowered))

        packages: list[str] = []
        package_rules = (
            ("controllers", ("controller", "endpoint", "route")),
            ("iot_handlers", ("iot_handlers",)),
            ("wizard", ("wizard",)),
        )
        for package, terms in package_rules:
            if any(OdooModuleValidator._contract_term_is_positive(lowered, term) for term in terms):
                packages.append(package)
        # Arbitrary technical package names requested as folders/directories
        # are part of the contract; this is not limited to known Odoo folders.
        non_python_folders = {"views", "security", "data", "demo", "static", "report", "reports", "i18n"}
        explicit_packages = list(re.finditer(r"`([a-z][a-z0-9_]*)`\s+(?:folder|directory|package)\b", lowered))
        explicit_packages.extend(re.finditer(
            r"\b(?:create|add|include|implement|build)\s+(?:(?:a|an|the)\s+)?(?:new\s+)?"
            r"([a-z][a-z0-9_]*_[a-z0-9_]+)\s+(?:folder|directory|package)\b",
            lowered,
        ))
        for match in explicit_packages:
            package = match.group(1)
            if package not in non_python_folders and package not in OdooModuleValidator._NON_PACKAGE_WORDS and package not in packages:
                packages.append(package)

        view_types = [
            view_type for view_type, terms in (
                ("list", ("list view", "list and form", "list/form", "list,form", "tree view", "tree,", "tree (")),
                ("form", ("form view", "detail view")),
                ("search", ("search view", "advanced filtering")),
                ("kanban", ("kanban",)),
                ("menu", ("menu", "menu item", "menu items", "menus")),
                ("action", ("window action", "ir.actions.act_window", "actions and menus")),
            ) if any(
                OdooModuleValidator._contract_term_is_positive(lowered, term)
                for term in terms
            )
        ]
        if "menu" in view_types and any(view_type in view_types for view_type in ("list", "form", "kanban")):
            view_types.append("action")

        layers = [
            layer for layer, terms in (
                ("models", ("data model", "models", "all python models", "model:", "new model", "model with")),
                ("security", ("security", "access rights", "record rule", "user group", "acl", "ir.model.access")),
                ("views", ("xml view", "views", "all xml views", "list view", "form view", "kanban")),
                ("dashboard", ("dashboard",)),
                ("demo", ("demo data",)),
                ("automation", ("ir.cron", "scheduled action", "automation")),
                ("controllers", ("controller", "endpoint", "route")),
                ("handlers", ("iot_handlers", "interface", "driver")),
                ("wizard", ("wizard",)),
            ) if any(
                OdooModuleValidator._contract_term_is_positive(lowered, term)
                for term in terms
            )
        ]
        if tests_explicitly_requested:
            if "tests" not in packages:
                packages.append("tests")
            if "tests" not in layers:
                layers.append("tests")
        if any(
            OdooModuleValidator._contract_term_is_positive(lowered, term)
            for term in ("report", "reports")
        ):
            layers.append("report")

        models = set(re.findall(r"(?im)^\s*#{2,4}\s*Model:\s*`([^`]+)`", text))
        models.update(re.findall(
            r"\b(?:add|create|implement|introduce)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?"
            r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\s+model\b",
            lowered,
        ))
        models = sorted(models)
        routes: set[str] = set()
        for quoted in re.findall(r"`([^`]+)`", text):
            if quoted.startswith("/"):
                routes.add(quoted.split()[0].rstrip(".,:;"))
            elif quoted.startswith(("http://", "https://")):
                match = re.match(r"https?://[^/]+(/[^\s]+)", quoted)
                if match:
                    routes.add(match.group(1).rstrip(".,:;"))
        for match in re.finditer(r"(?i)\b(?:GET|POST|PUT|PATCH|DELETE)\s+[`*]*(/[^`*\s:,]+)", text):
            routes.add(match.group(1).rstrip(".,:;"))
        methods = sorted(set(
            match for match in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)\s*\([^`]*\)`", text)
            if len(match) > 2
        ))
        # Preserve explicit API/provider identifiers even when they are values
        # used in a request payload rather than Python function definitions.
        # Examples: GetAppointmentsByIdAsync, sessionNoteResponseId, clientId.
        code_symbols = set(re.findall(
            r"\b(?:[A-Z][A-Za-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*|[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)\b",
            text,
        ))
        for quoted in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text):
            if len(quoted) > 2:
                code_symbols.add(quoted)
        required_symbols = sorted(code_symbols - set(methods))
        required_test_symbols = required_symbols if "tests" in layers else []
        return {
            "full_module_requested": full_module,
            "required_packages": packages,
            "required_layers": layers,
            "required_models": models,
            "required_view_types": view_types,
            "required_routes": sorted(routes),
            "required_methods": methods,
            "required_symbols": required_symbols,
            "required_test_symbols": required_test_symbols,
            "tests_explicitly_requested": tests_explicitly_requested,
        }

    @staticmethod
    def merge_contracts(*contracts: dict | None) -> dict:
        """Monotonically merge persisted and current module requirements."""
        merged = {
            "full_module_requested": False,
            "required_packages": [],
            "required_layers": [],
            "required_models": [],
            "required_view_types": [],
            "required_routes": [],
            "required_methods": [],
            "required_symbols": [],
            "required_test_symbols": [],
            "tests_explicitly_requested": False,
        }
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            merged["full_module_requested"] = bool(merged["full_module_requested"] or contract.get("full_module_requested"))
            merged["tests_explicitly_requested"] = bool(
                merged["tests_explicitly_requested"] or contract.get("tests_explicitly_requested")
            )
            for key in merged:
                if key in {"full_module_requested", "tests_explicitly_requested"}:
                    continue
                for value in contract.get(key) or []:
                    item = str(value).strip()
                    if key == "required_packages" and item.lower() in OdooModuleValidator._NON_PACKAGE_WORDS:
                        continue
                    if item and item not in merged[key]:
                        merged[key].append(item)
        return merged

    def _validate_manifest_registration(self, module_path: Path, manifest: dict) -> list[Finding]:
        """Require every deployable data/view/asset file to be in the manifest."""
        findings: list[Finding] = []
        entries: list[str] = []
        for key in ("data", "demo", "qweb"):
            entries.extend(item.replace("\\", "/") for item in (manifest.get(key) or []) if isinstance(item, str))
        entry_set = set(entries)
        asset_patterns: list[str] = []

        def collect_assets(value: object) -> None:
            if isinstance(value, str):
                pattern = value.replace("\\", "/")
                prefix = f"{module_path.name}/"
                asset_patterns.append(pattern[len(prefix):] if pattern.startswith(prefix) else pattern)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect_assets(child)
            elif isinstance(value, dict):
                for child in value.values():
                    collect_assets(child)

        collect_assets(manifest.get("assets") or {})
        excluded_dirs = {"tests", "test", "migrations", "upgrades", "i18n", "__pycache__"}
        for path in module_path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(module_path)
            if any(part in excluded_dirs or part.startswith(".") for part in relative.parts):
                continue
            rel = relative.as_posix()
            suffix = path.suffix.lower()
            is_static_asset = len(relative.parts) >= 2 and relative.parts[0] == "static" and relative.parts[1] == "src" and suffix in {".js", ".xml", ".scss", ".css"}
            if is_static_asset:
                registered = rel in entry_set or any(fnmatch(rel, pattern) for pattern in asset_patterns)
                if not registered:
                    findings.append(Finding(
                        severity=Severity.error,
                        code="MANIFEST_ASSET_UNREGISTERED",
                        message=f"Static source asset is not registered in manifest assets: {rel}",
                        file=str(module_path / "__manifest__.py"),
                        hint=f"Add '{module_path.name}/{rel}' to the correct asset bundle, or use a covering glob.",
                    ))
                continue
            if relative.parts[0] == "static" or suffix not in {".xml", ".csv"}:
                continue
            if rel not in entry_set:
                findings.append(Finding(
                    severity=Severity.error,
                    code="MANIFEST_FILE_UNREGISTERED",
                    message=f"Module file is not registered in manifest data: {rel}",
                    file=str(module_path / "__manifest__.py"),
                    hint=f"Add '{rel}' to manifest data in dependency/load order.",
                ))
        data_entries = [item.replace("\\", "/") for item in (manifest.get("data") or []) if isinstance(item, str)]
        first_non_security = next((index for index, item in enumerate(data_entries) if not item.startswith("security/")), len(data_entries))
        late_security = [item for index, item in enumerate(data_entries) if item.startswith("security/") and index > first_non_security]
        if late_security:
            findings.append(Finding(
                severity=Severity.error,
                code="MANIFEST_SECURITY_ORDER",
                message=f"Security files must load before views/data: {', '.join(late_security)}",
                file=str(module_path / "__manifest__.py"),
            ))
        return findings

    def _validate_requested_features(
        self,
        module_path: Path,
        manifest: dict,
        parsed_models: dict[str, ParsedModel],
        task_description: str,
        create_module: bool,
        module_contract: dict | None,
    ) -> list[Finding]:
        """Prove that generated files satisfy the original request contract."""
        contract = self.merge_contracts(module_contract, self.requested_contract(task_description, create_module))
        if not any(value for value in contract.values()):
            return []
        findings: list[Finding] = []
        entries = [item for item in (manifest.get("data") or []) if isinstance(item, str)]
        layer_checks = {
            "models": (bool(parsed_models), "models with imported Python files"),
            "security": (
                any(item == "security/ir.model.access.csv" for item in entries)
                and any(item.startswith("security/") and item.endswith(".xml") for item in entries),
                "security XML and ACL files registered in manifest",
            ),
            "views": (any(item.startswith(("views/", "wizard/", "wizards/")) for item in entries), "views registered in manifest"),
            "report": (any(item.startswith(("report/", "reports/")) for item in entries), "report files registered in manifest"),
            "dashboard": (any("dashboard" in item.lower() for item in entries) or any("dashboard" in path.name.lower() for path in module_path.rglob("*")), "dashboard implementation"),
            "demo": (bool(manifest.get("demo")), "demo files registered in manifest demo"),
            "automation": (any("ir.cron" in path.read_text(encoding="utf-8", errors="ignore") for path in module_path.rglob("*.xml")), "automation/ir.cron data"),
            "controllers": (
                (module_path / "controllers" / "__init__.py").is_file()
                and any(path.name != "__init__.py" for path in (module_path / "controllers").glob("*.py")),
                "controllers package and endpoint implementation",
            ),
            "handlers": (
                (module_path / "iot_handlers" / "__init__.py").is_file()
                and any(path.name != "__init__.py" for path in (module_path / "iot_handlers").glob("*.py")),
                "iot_handlers package and implementation",
            ),
            "wizard": (
                any((module_path / folder / "__init__.py").is_file() for folder in ("wizard", "wizards"))
                and any(item.startswith(("wizard/", "wizards/")) for item in entries),
                "wizard Python package and registered XML view",
            ),
            "tests": ((module_path / "tests").is_dir() and any((module_path / "tests").glob("test_*.py")), "unit test files"),
        }
        for layer in contract["required_layers"]:
            present, requirement = layer_checks[layer]
            if not present:
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_MODULE_LAYER_MISSING",
                    message=f"Request includes '{layer}' but the module lacks {requirement}.",
                    file=str(module_path),
                    hint="Complete the requested layer before commit/push.",
                ))

        package_aliases = {"wizard": ("wizard", "wizards")}
        for package in contract["required_packages"]:
            choices = package_aliases.get(package, (package,))
            if package == "tests":
                exists = any(
                    (module_path / choice / "__init__.py").is_file()
                    and any((module_path / choice).glob("test_*.py"))
                    for choice in choices
                )
            else:
                exists = any(
                    (module_path / choice / "__init__.py").is_file()
                    and any(path.name != "__init__.py" for path in (module_path / choice).glob("*.py"))
                    for choice in choices
                )
            if not exists:
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_PACKAGE_MISSING",
                    message=f"Requested package/folder is missing: {package}",
                    file=str(module_path / choices[0]),
                ))

        for model_name in contract["required_models"]:
            if model_name not in parsed_models:
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_MODEL_MISSING",
                    message=f"Requested model is not implemented: {model_name}",
                    file=str(module_path / "models"),
                ))

        xml_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in module_path.rglob("*.xml"))
        view_markers = {
            "list": ("<list", "<tree"), "form": ("<form",), "search": ("<search",),
            "kanban": ("<kanban",), "menu": ("<menuitem",), "action": ("ir.actions.act_window",),
        }
        for view_type in contract["required_view_types"]:
            if not any(marker in xml_text for marker in view_markers[view_type]):
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_VIEW_TYPE_MISSING",
                    message=f"Requested Odoo view/UI type is missing: {view_type}",
                    file=str(module_path / "views"),
                ))

        # For explicitly named models, list/form/search coverage is checked per
        # model instead of accepting one token view somewhere in the module.
        per_model_types = {view_type for view_type in contract["required_view_types"] if view_type in {"list", "form", "search"}}
        model_view_types: dict[str, set[str]] = {model: set() for model in contract["required_models"]}
        action_models: set[str] = set()
        for xml_path in module_path.rglob("*.xml"):
            try:
                root = ET.parse(xml_path).getroot()
            except ET.ParseError:
                continue
            for record in root.iter("record"):
                record_model = record.attrib.get("model")
                if record_model == "ir.ui.view":
                    target_model = self._get_record_field_text(record, "model")
                    arch = self._get_record_field_node(record, "arch")
                    if target_model in model_view_types and arch is not None:
                        rendered = ET.tostring(arch, encoding="unicode")
                        for view_type, markers in {"list": ("<list", "<tree"), "form": ("<form",), "search": ("<search",)}.items():
                            if any(marker in rendered for marker in markers):
                                model_view_types[target_model].add(view_type)
                elif record_model == "ir.actions.act_window":
                    target_model = self._get_record_field_text(record, "res_model")
                    if target_model:
                        action_models.add(target_model)
        for model_name, present_types in model_view_types.items():
            for missing_type in sorted(per_model_types - present_types):
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_MODEL_VIEW_MISSING",
                    message=f"Requested model {model_name} lacks a {missing_type} view.",
                    file=str(module_path / "views"),
                ))
            if "action" in contract["required_view_types"] and model_name not in action_models:
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_MODEL_ACTION_MISSING",
                    message=f"Requested model {model_name} lacks an ir.actions.act_window action.",
                    file=str(module_path / "views"),
                ))

        python_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in module_path.rglob("*.py"))
        source_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in module_path.rglob("*")
            if path.is_file() and path.suffix.lower() in {".py", ".js", ".xml", ".csv"}
        )
        for route in contract["required_routes"]:
            if route not in python_text:
                findings.append(Finding(severity=Severity.error, code="REQUESTED_ROUTE_MISSING", message=f"Requested route is missing: {route}", file=str(module_path / "controllers")))
        for method in contract["required_methods"]:
            if not re.search(rf"\bdef\s+{re.escape(method)}\s*\(", python_text):
                findings.append(Finding(severity=Severity.error, code="REQUESTED_METHOD_MISSING", message=f"Requested method is missing: {method}", file=str(module_path)))
        for symbol in contract["required_symbols"]:
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", source_text):
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_SYMBOL_MISSING",
                    message=f"Requested API/field identifier is missing: {symbol}",
                    file=str(module_path),
                    hint="Implement the complete named integration flow before commit/push.",
                ))

        test_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (module_path / "tests").glob("test_*.py")
        ) if (module_path / "tests").is_dir() else ""
        for symbol in contract["required_test_symbols"]:
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", test_text):
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_TEST_COVERAGE_MISSING",
                    message=f"Requested mocked/regression tests do not exercise identifier: {symbol}",
                    file=str(module_path / "tests"),
                    hint="Add a focused test that mocks or asserts this named integration step.",
                ))

        functional_files = [path for path in module_path.rglob("*") if path.is_file() and path.name not in {"__init__.py", "__manifest__.py"}]
        if contract["full_module_requested"] and not functional_files:
            findings.append(Finding(
                severity=Severity.error,
                code="COMPLETE_MODULE_EMPTY",
                message="Complete-module request produced only scaffold files and no functional implementation.",
                file=str(module_path),
            ))
        return findings

    # Fix 8: Odoo-specific wrong-import patterns that pass Python syntax but fail at runtime.
    # Keyed by the bad import string → (correct_alternative, severity).
    _ODOO_WRONG_IMPORTS: dict[str, tuple[str, Severity]] = {
        # Wrong attribute access paths inside odoo package
        "from odoo.models import BaseModel": ("from odoo import models", Severity.error),
        "from odoo.models import Model": ("from odoo import models", Severity.error),
        "from odoo.fields import Field": ("from odoo import fields  # then use fields.Char etc.", Severity.error),
        "from odoo.api import Environment": ("from odoo import api  # env is self.env", Severity.warning),
        "from odoo.exceptions import UserError": (None, Severity.info),  # this one IS correct, skip
        # Common wrong form: importing Command from wrong place
        "from odoo import Command": ("from odoo.fields import Command", Severity.error),
        # Relative import of odoo internals
        "import odoo.models as models": ("from odoo import models", Severity.warning),
        "import odoo.fields as fields": ("from odoo import fields", Severity.warning),
        # Deprecated / removed in 17+
        "from odoo.addons.base.models.ir_ui_view import": (
            "Avoid importing ir.ui.view internals; use self.env.ref() instead.", Severity.warning
        ),
    }

    # Correct patterns — must NOT be flagged
    _ODOO_CORRECT_IMPORTS: set[str] = {
        "from odoo import models, fields, api",
        "from odoo import models",
        "from odoo import fields",
        "from odoo import api",
        "from odoo import _",
        "from odoo import tools",
        "from odoo import SUPERUSER_ID",
        "from odoo.fields import Command",
        "from odoo.exceptions import UserError",
        "from odoo.exceptions import ValidationError",
        "from odoo.exceptions import AccessError",
        "from odoo.tools import",
        "from odoo.tools.translate import",
        "from odoo.http import",
        "from odoo.addons.",
    }

    def _validate_python_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                findings.append(Finding(severity=Severity.error, code="PYTHON_SYNTAX_ERROR", message=str(exc), file=str(py_file), line=exc.lineno))
                continue
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="PYTHON_ENCODING_ERROR", message=str(exc), file=str(py_file)))
                continue
            # Fix 8: validate Odoo-specific import patterns line-by-line
            findings.extend(self._validate_odoo_imports(py_file, source))
        return findings

    @staticmethod
    def _function_is_placeholder(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        body = [item for item in node.body if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )]
        if not body:
            return True
        if len(body) != 1:
            return False
        only = body[0]
        if isinstance(only, ast.Pass):
            decorator_names = {ast.unparse(item) for item in node.decorator_list}
            return not any("abstractmethod" in name for name in decorator_names)
        return isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) and only.value.value is Ellipsis

    def _validate_implementation_completeness(self, module_path: Path) -> list[Finding]:
        """Reject empty shells and placeholder implementations before deploy."""
        findings: list[Finding] = []
        excluded_parts = {"migrations", "static", "__pycache__"}
        for py_file in module_path.rglob("*.py"):
            relative = py_file.relative_to(module_path)
            if any(part in excluded_parts or part.startswith(".") for part in relative.parts[:-1]):
                continue
            if py_file.name in {"__init__.py", "__manifest__.py", "__openerp__.py"}:
                continue
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue  # The syntax validator reports the precise failure.
            substantive = [
                node for node in tree.body
                if not isinstance(node, (ast.Import, ast.ImportFrom))
                and not (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
            ]
            if not substantive:
                findings.append(Finding(
                    severity=Severity.error,
                    code="IMPLEMENTATION_FILE_EMPTY",
                    message=f"Python implementation file contains no functional code: {relative.as_posix()}",
                    file=str(py_file),
                    hint="Implement the requested Odoo behavior; imports or comments alone are not a completed file.",
                ))
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and self._function_is_placeholder(node):
                    findings.append(Finding(
                        severity=Severity.error,
                        code="PLACEHOLDER_IMPLEMENTATION",
                        message=f"Function/method is still an empty placeholder: {node.name}",
                        file=str(py_file),
                        line=node.lineno,
                        hint="Replace pass/ellipsis with the requested functional implementation.",
                    ))

        for xml_file in module_path.rglob("*.xml"):
            try:
                root = ET.parse(xml_file).getroot()
            except ET.ParseError:
                continue
            meaningful = [node for node in root.iter() if node is not root and node.tag not in {"data"}]
            if not meaningful:
                findings.append(Finding(
                    severity=Severity.error,
                    code="IMPLEMENTATION_FILE_EMPTY",
                    message=f"XML implementation file contains no records/templates/views: {xml_file.relative_to(module_path).as_posix()}",
                    file=str(xml_file),
                ))

        for csv_file in module_path.rglob("*.csv"):
            rows = [line for line in csv_file.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
            if len(rows) <= 1:
                findings.append(Finding(
                    severity=Severity.error,
                    code="IMPLEMENTATION_FILE_EMPTY",
                    message=f"CSV implementation file has no data rows: {csv_file.relative_to(module_path).as_posix()}",
                    file=str(csv_file),
                ))

        for pattern in ("*.js", "*.scss", "*.css"):
            for asset_file in module_path.rglob(pattern):
                source = asset_file.read_text(encoding="utf-8", errors="ignore")
                without_comments = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", source, flags=re.DOTALL).strip()
                if without_comments:
                    continue
                findings.append(Finding(
                    severity=Severity.error,
                    code="IMPLEMENTATION_FILE_EMPTY",
                    message=f"Frontend implementation file contains only whitespace/comments: {asset_file.relative_to(module_path).as_posix()}",
                    file=str(asset_file),
                ))
        return findings

    def _validate_odoo_controllers(self, module_path: Path) -> list[Finding]:
        """Controllers must use Odoo's HTTP framework, never requests as a substitute."""
        findings: list[Finding] = []
        controller_files = [
            path for folder in ("controllers", "controller")
            for path in (module_path / folder).rglob("*.py")
            if (module_path / folder).is_dir() and path.name != "__init__.py"
        ]
        if not controller_files:
            return findings

        has_controller_class = False
        has_route = False
        for py_file in controller_files:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imported_module = node.module if isinstance(node, ast.ImportFrom) else ""
                    imported_names = [alias.name for alias in node.names]
                    if (imported_module or "").startswith("requests") or any(name == "requests" or name.startswith("requests.") for name in imported_names):
                        findings.append(Finding(
                            severity=Severity.error,
                            code="CONTROLLER_REQUESTS_FORBIDDEN",
                            message="Odoo controller imports requests instead of using the Odoo HTTP controller framework.",
                            file=str(py_file),
                            line=node.lineno,
                            hint="Use from odoo import http, subclass http.Controller, and expose endpoints with @http.route; keep outbound HTTP clients in a service/handler layer.",
                        ))
                if isinstance(node, ast.ClassDef):
                    bases = {ast.unparse(base) for base in node.bases}
                    has_controller_class = has_controller_class or any(base in {"http.Controller", "Controller"} for base in bases)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = {ast.unparse(item) for item in node.decorator_list}
                    has_route = has_route or any(name.startswith(("http.route", "route")) for name in decorators)

        if not has_controller_class or not has_route:
            findings.append(Finding(
                severity=Severity.error,
                code="ODOO_CONTROLLER_FRAMEWORK_MISSING",
                message="Controller package does not contain a complete Odoo http.Controller with an @http.route endpoint.",
                file=str(module_path / "controllers"),
                hint="Implement the endpoint with Odoo http.Controller/@http.route and use odoo.http.request/request.env for Odoo access.",
            ))
        return findings

    def _validate_odoo_imports(self, py_file: Path, source: str) -> list[Finding]:
        """Check for wrong Odoo import paths that pass Python syntax but blow up at runtime."""
        findings: list[Finding] = []
        for lineno, raw_line in enumerate(source.splitlines(), start=1):
            line = raw_line.strip()
            if not line.startswith(("import ", "from ")):
                continue
            # Skip lines that are known-correct
            if any(line.startswith(pat) for pat in self._ODOO_CORRECT_IMPORTS):
                continue
            # Match against known-wrong patterns
            for wrong_pat, (fix_hint, severity) in self._ODOO_WRONG_IMPORTS.items():
                if wrong_pat == "from odoo.exceptions import UserError":
                    continue  # marked as info/skip above
                if line.startswith(wrong_pat) or wrong_pat in line:
                    hint = f"Use instead: {fix_hint}" if fix_hint else None
                    findings.append(Finding(
                        severity=severity,
                        code="ODOO_WRONG_IMPORT",
                        message=f"Wrong Odoo import pattern: '{line}' — this will fail at module load.",
                        file=str(py_file),
                        line=lineno,
                        hint=hint,
                    ))
        return findings

    def _validate_xml_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for xml_file in self._iter_files(module_path, "*.xml"):
            try:
                ET.parse(xml_file)
            except ET.ParseError as exc:
                line, _col = exc.position
                findings.append(Finding(severity=Severity.error, code="XML_PARSE_ERROR", message=str(exc), file=str(xml_file), line=line))
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="XML_ENCODING_ERROR", message=str(exc), file=str(xml_file)))
        return findings

    def _validate_security_csv(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        expected_header = ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"]
        for csv_file in self._iter_files(module_path, "*.csv"):
            if csv_file.name != "ir.model.access.csv":
                continue
            try:
                with csv_file.open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != expected_header:
                        findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_BAD_HEADER", message="ir.model.access.csv header is not the standard Odoo access header.", file=str(csv_file), hint=",".join(expected_header)))
                    for row_index, row in enumerate(reader, start=2):
                        for perm in ("perm_read", "perm_write", "perm_create", "perm_unlink"):
                            if row.get(perm) not in {"0", "1"}:
                                findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_BAD_PERMISSION", message=f"{perm} must be 0 or 1.", file=str(csv_file), line=row_index))
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_ENCODING_ERROR", message=str(exc), file=str(csv_file)))
        return findings

    def _detect_pyc(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for path in module_path.rglob("*"):
            if path.name == "__pycache__" and path.is_dir():
                findings.append(Finding(severity=Severity.warning, code="PYCACHE_FOUND", message="__pycache__ directory should not be committed.", file=str(path), hint="Delete __pycache__ before commit/push."))
            elif path.suffix == ".pyc":
                findings.append(Finding(severity=Severity.warning, code="PYC_FOUND", message=".pyc file should not be committed.", file=str(path), hint="Delete .pyc files before commit/push."))
        return findings

    def _validate_missing_access(self, module_path: Path, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        access_model_refs: set[str] = set()
        for csv_file in self._iter_files(module_path, "ir.model.access.csv"):
            with csv_file.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    model_ref = row.get("model_id:id") or ""
                    if model_ref.startswith("model_"):
                        access_model_refs.add(model_ref)
        findings: list[Finding] = []
        for model_name, model in sorted(parsed_models.items()):
            if model.inherited_model or model.transient:
                continue
            expected_ref = f"model_{model_name.replace('.', '_')}"
            if expected_ref not in access_model_refs:
                findings.append(Finding(severity=Severity.error, code="ACCESS_ROW_MISSING_STATIC", message=f"No access row found for custom model {model_name}.", file=str(model.file) if model.file else None, hint=f"Add model_{model_name.replace('.', '_')} to security/ir.model.access.csv. New persistent models cannot be pushed without explicit access control."))
        return findings

    def _validate_manifest_dependency_hints(self, module_path: Path, manifest: dict, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        depends = set(manifest.get("depends") or []) if isinstance(manifest.get("depends") or [], list) else set()
        effective_depends = set(depends)
        for dep in list(depends):
            effective_depends.update(TRANSITIVE_DEPENDS_HINTS.get(dep, set()))
        findings: list[Finding] = []
        inherited = {model.inherited_model for model in parsed_models.values() if model.inherited_model}
        for inherited_model in inherited:
            for prefix, dep in INHERIT_DEPENDS.items():
                if inherited_model and inherited_model.startswith(prefix) and dep not in effective_depends:
                    findings.append(Finding(severity=Severity.warning, code="MANIFEST_DEPENDENCY_MISSING_STATIC", message=f"Model inherits {inherited_model} but manifest does not depend on {dep}.", file=str(module_path / "__manifest__.py"), hint=f"Add '{dep}' to manifest depends if this inheritance is active."))
        return findings

    def _validate_xml_references_and_order(self, module_path: Path, manifest: dict, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        findings: list[Finding] = []
        entries = [item for item in (manifest.get("data") or []) if isinstance(item, str)]
        defined: set[str] = set()
        all_ids: set[str] = set()
        parsed_files: list[tuple[str, ET.Element]] = []
        for rel in entries:
            path = module_path / rel
            if path.exists() and path.suffix == ".xml":
                try:
                    root = ET.parse(path).getroot()
                except ET.ParseError:
                    continue
                parsed_files.append((rel, root))
                for node in root.iter():
                    xml_id = node.attrib.get("id")
                    if xml_id:
                        all_ids.add(xml_id)
        for rel, root in parsed_files:
            for node in root:
                xml_id = node.attrib.get("id")
                if node.tag == "menuitem":
                    for attr, code in (("action", "MENU_ACTION_DEFINED_LATE"), ("parent", "MENU_PARENT_DEFINED_LATE")):
                        ref = node.attrib.get(attr)
                        if ref and "." not in ref and ref not in defined:
                            severity = Severity.error if ref not in all_ids else Severity.warning
                            findings.append(Finding(severity=severity, code=code, message=f"Menu {node.attrib.get('id') or node.attrib.get('name')} references {attr}={ref} before it is defined in manifest load order.", file=str(module_path / rel), hint="Define actions before menus, or move the action XML file before menu XML in manifest data."))
                if node.tag == "record" and node.attrib.get("model") == "ir.actions.act_window":
                    action_id = node.attrib.get("id") or "<no id>"
                    res_model = self._get_record_field_text(node, "res_model")
                    if res_model and res_model not in parsed_models and res_model.split(".", 1)[0] == module_path.name.split("_", 1)[0]:
                        findings.append(Finding(severity=Severity.warning, code="ACTION_RESMODEL_NOT_FOUND_STATIC", message=f"Action {action_id} uses model {res_model}, not found in parsed custom models.", file=str(module_path / rel)))
                if xml_id:
                    defined.add(xml_id)
        return findings

    def _validate_custom_view_fields(self, module_path: Path, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        if not parsed_models:
            return []
        findings: list[Finding] = []
        for xml_file in self._iter_files(module_path, "*.xml"):
            try:
                root = ET.parse(xml_file).getroot()
            except ET.ParseError:
                continue
            for record in root.iter("record"):
                if record.attrib.get("model") != "ir.ui.view":
                    continue
                view_model = self._get_record_field_text(record, "model")
                if not view_model or view_model not in parsed_models:
                    continue
                arch_field = self._get_record_field_node(record, "arch")
                if arch_field is None:
                    continue
                findings.extend(self._walk_view_fields(xml_file, arch_field, view_model, parsed_models))
        return findings

    def _walk_view_fields(self, xml_file: Path, node: ET.Element, current_model: str, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        findings: list[Finding] = []
        model = parsed_models.get(current_model)
        model_fields = (model.fields if model else set()) | COMMON_ODOO_FIELDS
        for child in list(node):
            next_model = current_model
            if child.tag == "field":
                field_name = child.attrib.get("name")
                if field_name and field_name not in {"model", "arch", "inherit_id", "name"}:
                    if field_name not in model_fields:
                        findings.append(Finding(severity=Severity.warning, code="VIEW_FIELD_NOT_FOUND_STATIC", message=f"Field '{field_name}' is used in a view for custom model '{current_model}' but was not found in parsed Python fields.", file=str(xml_file), hint="Static check only. Confirm the field exists through inheritance, or add it to the model."))
                    elif model and field_name in model.relations:
                        next_model = model.relations[field_name]
            findings.extend(self._walk_view_fields(xml_file, child, next_model, parsed_models))
        return findings

    def _parse_custom_models(self, module_path: Path) -> dict[str, ParsedModel]:
        models: dict[str, ParsedModel] = {}
        pending_inherits: list[ParsedModel] = []
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except Exception:
                continue
            for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
                base_names = {self._name_from_ast(base) for base in cls.bases}
                transient = any(name.endswith("TransientModel") for name in base_names if name)
                attrs: dict[str, ast.AST] = {}
                for stmt in cls.body:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                attrs[target.id] = stmt.value
                model_name = self._literal(attrs.get("_name"))
                inherited = self._inherit_value(attrs.get("_inherit"))
                if model_name:
                    parsed = ParsedModel(technical_name=model_name, inherited_model=None, transient=transient, file=py_file)
                    self._extract_fields(attrs, parsed)
                    models[model_name] = parsed
                elif inherited:
                    parsed = ParsedModel(technical_name=inherited, inherited_model=inherited, transient=transient, file=py_file)
                    self._extract_fields(attrs, parsed)
                    pending_inherits.append(parsed)
        for inherited in pending_inherits:
            if inherited.technical_name in models:
                models[inherited.technical_name].fields.update(inherited.fields)
                models[inherited.technical_name].relations.update(inherited.relations)
            else:
                models[inherited.technical_name] = inherited
        return models

    def _extract_fields(self, attrs: dict[str, ast.AST], parsed: ParsedModel) -> None:
        for name, value in attrs.items():
            if name.startswith("_"):
                continue
            if isinstance(value, ast.Call):
                func_name = self._name_from_ast(value.func)
                if func_name.startswith("fields."):
                    parsed.fields.add(name)
                    field_type = func_name.split(".")[-1]
                    if field_type in FIELD_TYPES_WITH_COMODEL and value.args:
                        comodel = self._literal(value.args[0])
                        if isinstance(comodel, str):
                            parsed.relations[name] = comodel

    @staticmethod
    def _literal(node: ast.AST | None):
        if node is None:
            return None
        try:
            return ast.literal_eval(node)
        except Exception:
            return None

    @staticmethod
    def _inherit_value(node: ast.AST | None) -> str | None:
        value = OdooModuleValidator._literal(node)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
        return None

    @staticmethod
    def _name_from_ast(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = OdooModuleValidator._name_from_ast(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    @staticmethod
    def _get_record_field_text(record: ET.Element, field_name: str) -> str | None:
        node = OdooModuleValidator._get_record_field_node(record, field_name)
        if node is None or node.text is None:
            return None
        return node.text.strip()

    @staticmethod
    def _get_record_field_node(record: ET.Element, field_name: str) -> ET.Element | None:
        for node in record.findall("field"):
            if node.attrib.get("name") == field_name:
                return node
        return None

    @staticmethod
    def _iter_files(module_path: Path, pattern: str) -> Iterable[Path]:
        return sorted(path for path in module_path.rglob(pattern) if path.is_file() and ".git" not in path.parts)
