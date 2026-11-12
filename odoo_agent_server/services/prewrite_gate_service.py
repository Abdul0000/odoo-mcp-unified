from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException, status

from ..schemas import CommandResult, FrontendPatternPreflightResult, ScopeAdviceResult
from .client_resolver_service import ClientResolverService
from .path_service import resolve_allowed_path
from .store_service import JsonStore


_CODE_EXTENSIONS = {".py", ".xml", ".js", ".jsx", ".ts", ".tsx", ".scss", ".css", ".csv"}
_LIGHTWEIGHT_UI_EXTENSIONS = {".xml", ".scss", ".css"}
_LIGHTWEIGHT_UI_DIR_HINTS = {"views", "templates", "static", "static/src", "static/src/scss", "static/src/css", "static/src/xml"}
_STRICT_DIR_HINTS = {"models", "controllers", "security", "wizard", "wizards", "report", "reports", "data", "demo", "migrations", "tests"}
_MANIFEST_NAMES = {"__manifest__.py", "__openerp__.py"}


class PreWriteGateService:
    """Hard server-side gate for the Odoo read -> scope -> write flow.

    The MCP prompt already instructs agents to read the custom module, call
    workflow.scope_advice, read the mandatory installed-source, and only
    then write.  Prompt-only enforcement is not enough: direct repo.write or
    repo.apply_patch_atomic calls must be blocked until the evidence exists in
    persisted session state.

    The gate intentionally applies only to writes inside an existing Odoo addon
    folder (detected by an ancestor __manifest__.py/__openerp__.py). Plain temp
    files, docs outside addons, and first-time new-module scaffolding remain
    allowed so non-Odoo helper flows and tests are not broken.
    """

    def __init__(self, store: JsonStore | None = None) -> None:
        self.store = store or JsonStore()

    # ------------------------------------------------------------------
    # Recording evidence
    # ------------------------------------------------------------------

    def record_repo_inspection(
        self,
        *,
        project_path: str,
        relative_path: str,
        client_name: str | None = None,
        action: str = "repo.read",
        is_content_read: bool = True,
    ) -> None:
        abs_path = self._abs_from_project(project_path, relative_path)
        if not abs_path:
            return
        state = self._load(client_name)
        gate = self._gate(state)
        now = self._now()
        key = str(abs_path)
        gate.setdefault("read_paths", {})[key] = {"at": now, "action": action, "project_path": str(self._safe_project(project_path))}
        if not is_content_read:
            gate.setdefault("inspected_paths", {})[key] = {"at": now, "action": action, "project_path": str(self._safe_project(project_path))}

        module = self._module_for_abs_path(project_path, abs_path)
        if module:
            module_state = self._module_state(gate, str(self._safe_project(project_path)), module)
            if is_content_read:
                module_state.setdefault("custom_reads", {})[key] = {"at": now, "action": action}
            else:
                module_state.setdefault("custom_inspections", {})[key] = {"at": now, "action": action}
        self._save(state, client_name)

    def record_scope_advice(self, result: ScopeAdviceResult, payload: Any) -> None:
        client_name = getattr(payload, "client_name", None) or None
        module_name = result.custom_module
        project_path = self._infer_project_from_custom_paths(module_name, getattr(payload, "custom_file_paths", []) or [])
        if not client_name and project_path:
            try:
                client = ClientResolverService().resolve_client(project_path=project_path, require_complete=False)
                client_name = client.name if client else None
            except Exception:
                client_name = None

        state = self._load(client_name)
        gate = self._gate(state)
        key = self._scope_key(project_path, module_name)
        mandatory = [
            self._normalise_required_path(item.path)
            for item in result.instructions
            if getattr(item, "mandatory", False) and self._normalise_required_path(item.path)
        ]
        gate.setdefault("scope_advice", {})[key] = {
            "at": self._now(),
            "project_path": project_path,
            "module_name": module_name,
            "custom_file_paths": [str(p) for p in (getattr(payload, "custom_file_paths", []) or [])],
            "mandatory_paths": mandatory,
            "mandatory_count": len(mandatory),
            "reached_level": str(result.reached_level.value if hasattr(result.reached_level, "value") else result.reached_level),
        }
        self._save(state, client_name)

    def record_frontend_preflight(self, result: FrontendPatternPreflightResult, payload: Any) -> None:
        client_name = getattr(payload, "client_name", None) or None
        module_name = str(getattr(payload, "target_module_name", None) or "").strip()
        custom_paths = list(getattr(payload, "custom_file_paths", []) or [])
        project_path = self._infer_project_from_custom_paths(module_name, custom_paths) if module_name else None
        if not module_name or not project_path:
            return
        state = self._load(client_name)
        gate = self._gate(state)
        mandatory = [
            self._normalise_required_path(item.path)
            for item in result.instructions
            if getattr(item, "mandatory", False) and self._normalise_required_path(item.path)
        ]
        gate.setdefault("frontend_preflight", {})[self._scope_key(project_path, module_name)] = {
            "at": self._now(),
            "project_path": project_path,
            "module_name": module_name,
            "target_area": result.target_area,
            "mandatory_paths": mandatory,
        }
        self._save(state, client_name)

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def ensure_repo_write_allowed(
        self,
        *,
        project_path: str,
        relative_paths: Iterable[str],
        client_name: str | None = None,
        action: str,
        check_only: bool = False,
    ) -> None:
        if check_only:
            return
        project = self._safe_project(project_path)
        self._reject_nested_addon_manifests(project, relative_paths)
        modules = self._modules_for_relative_paths(project, relative_paths)
        if not modules:
            return

        state = self._load(client_name)
        gate = self._gate(state)
        read_paths = set((gate.get("read_paths") or {}).keys())
        inspected_paths = set((gate.get("inspected_paths") or {}).keys()) | read_paths
        blocked: list[dict[str, Any]] = []
        normalised_targets = list(self._normalised_target_paths(project, relative_paths))
        for module_name, module_dir in sorted(modules.items()):
            module_key = self._module_key(str(project), module_name)
            module_state = (gate.get("modules") or {}).get(module_key) or {}
            custom_reads = set((module_state.get("custom_reads") or {}).keys())
            module_targets = [p for p in normalised_targets if self._path_inside(p, module_dir)]

            # UI/style edits are intentionally small, but they still follow the
            # original mandatory dual-read flow: read the custom target, call
            # workflow.scope_advice, read the mandatory installed-source files
            # it returns, then write.  This keeps the prompt contract intact
            # while the L2 cap and duplicate guidance prevent scope spirals.
            if not custom_reads:
                blocked.append({
                    "module_name": module_name,
                    "reason": "custom_module_not_read",
                    "message": f"Read related custom files in '{module_name}' before writing.",
                    "gate_mode": "mandatory_dual_read",
                    "next_call": {
                        "tool": "repo.read",
                        "arguments": {
                            "project_path": str(project),
                            "relative_path": f"{module_dir.relative_to(project).as_posix()}/__manifest__.py",
                            "client_name": client_name,
                        },
                    },
                })
                continue

            scope_record = self._latest_scope_for(gate, str(project), module_name)
            frontend_record = (gate.get("frontend_preflight") or {}).get(module_key)
            static_frontend_only = bool(module_targets) and all(
                self._is_static_frontend_path(path, module_dir) for path in module_targets
            )
            if static_frontend_only and frontend_record:
                missing_frontend = [
                    path for path in frontend_record.get("mandatory_paths", [])
                    if not self._path_satisfied(path, read_paths, inspected_paths)
                ]
                if missing_frontend:
                    first = missing_frontend[0]
                    blocked.append({
                        "module_name": module_name,
                        "reason": "mandatory_frontend_source_not_read",
                        "message": "Read the mandatory installed frontend patterns returned by workflow.frontend_pattern_preflight.",
                        "gate_mode": "frontend_fast_path",
                        "missing_paths": missing_frontend[:20],
                        "next_call": self._next_call_for_required_path(first, client_name),
                    })
                continue
            if not scope_record:
                blocked.append({
                    "module_name": module_name,
                    "reason": "scope_advice_missing",
                    "message": "Call workflow.scope_advice after reading custom files and before writing any Odoo addon code, including XML/CSS/SCSS UI changes.",
                    "gate_mode": "mandatory_dual_read",
                    "next_call": {
                        "tool": "workflow.scope_advice",
                        "arguments": {
                            "custom_module_name": module_name,
                            "custom_file_paths": sorted(custom_reads),
                            "client_name": client_name,
                        },
                    },
                })
                continue

            missing_custom = [
                path for path in (scope_record.get("custom_file_paths") or [])
                if self._is_project_module_path(path, project, module_name) and str(Path(path).expanduser().resolve()) not in read_paths
            ]
            if missing_custom:
                first_custom = missing_custom[0]
                blocked.append({
                    "module_name": module_name,
                    "reason": "scope_custom_file_not_read",
                    "message": "workflow.scope_advice was called with custom_file_paths that were not read through repo.read/repo.read_range in this session.",
                    "gate_mode": "mandatory_dual_read",
                    "missing_paths": missing_custom[:20],
                    "next_call": self._next_call_for_project_path(first_custom, project, client_name),
                })
                continue

            missing_core = [path for path in scope_record.get("mandatory_paths", []) if not self._path_satisfied(path, read_paths, inspected_paths)]
            if missing_core:
                first = missing_core[0]
                blocked.append({
                    "module_name": module_name,
                    "reason": "mandatory_core_source_not_read",
                    "message": "Read every mandatory installed-source instruction returned by workflow.scope_advice before writing. Priority must be /home/odoo/src/odoo/addons first, then /home/odoo/src/enterprise fallback, then /home/odoo/src/user for other custom dependencies.",
                    "gate_mode": "mandatory_dual_read",
                    "missing_paths": missing_core[:20],
                    "next_call": self._next_call_for_required_path(first, client_name),
                })

        if blocked:
            gate_modes = sorted({str(item.get("gate_mode") or "mandatory_dual_read") for item in blocked})
            message = (
                f"{action} is blocked until the original mandatory dual-read flow is completed: "
                "read target custom module files -> call workflow.scope_advice -> read mandatory source "
                "(/home/odoo/src/odoo/addons first, enterprise fallback) -> then write. "
                "Do not repeat already-satisfied tools; follow the first blocked.next_call."
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "status": "prewrite_gate_blocked",
                    "message": message,
                    "project_path": str(project),
                    "gate_modes": gate_modes,
                    "completion_required": True,
                    "success_allowed": False,
                    "tool_exit_allowed": False,
                    "blocked": blocked,
                },
            )

    @staticmethod
    def _is_static_frontend_path(path: Path, module_dir: Path) -> bool:
        try:
            rel = path.resolve().relative_to(module_dir.resolve())
        except Exception:
            return False
        parts = {part.lower() for part in rel.parts}
        return "static" in parts and path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".xml", ".scss", ".css"}

    @staticmethod
    def _reject_nested_addon_manifests(project: Path, relative_paths: Iterable[str]) -> None:
        """Never allow an addon manifest below an existing addon root."""
        for raw in relative_paths:
            value = str(raw).replace("\\", "/")
            if Path(value).name not in _MANIFEST_NAMES:
                continue
            target = (project / value.lstrip("/")).resolve()
            try:
                target.relative_to(project)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Write target escapes project checkout") from exc
            current = target.parent.parent
            while current == project or project in current.parents:
                if any((current / name).exists() for name in _MANIFEST_NAMES):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Nested Odoo addon rejected: {target.relative_to(project).as_posix()} is inside "
                            f"existing addon {current.relative_to(project).as_posix() or '.'}. "
                            "Use the repository checkout root as project_path."
                        ),
                    )
                if current == project:
                    break
                current = current.parent

    def command_block_if_needed(
        self,
        *,
        project_path: str,
        relative_paths: Iterable[str],
        client_name: str | None = None,
        action: str,
        check_only: bool = False,
    ) -> CommandResult | None:
        try:
            self.ensure_repo_write_allowed(
                project_path=project_path,
                relative_paths=relative_paths,
                client_name=client_name,
                action=action,
                check_only=check_only,
            )
            return None
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return CommandResult(
                command=[action],
                cwd=project_path,
                exit_code=2,
                stdout=self._json_dump(detail),
                stderr=str(detail.get("message") or "Pre-write gate blocked the write."),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load(self, client_name: str | None) -> dict[str, Any]:
        state = self.store.load_session_state(client_name=client_name)
        return state if isinstance(state, dict) and not state.get("requires_client_name") else {}

    def _save(self, state: dict[str, Any], client_name: str | None) -> None:
        self.store.save_session_state({"prewrite_gate": state.get("prewrite_gate", {})}, client_name=client_name)

    def _gate(self, state: dict[str, Any]) -> dict[str, Any]:
        gate = state.setdefault("prewrite_gate", {})
        gate.setdefault("read_paths", {})
        gate.setdefault("inspected_paths", {})
        gate.setdefault("modules", {})
        gate.setdefault("scope_advice", {})
        return gate

    def _module_state(self, gate: dict[str, Any], project_path: str, module_name: str) -> dict[str, Any]:
        return gate.setdefault("modules", {}).setdefault(self._module_key(project_path, module_name), {})

    def _latest_scope_for(self, gate: dict[str, Any], project_path: str, module_name: str) -> dict[str, Any] | None:
        scopes = gate.get("scope_advice") or {}
        exact = scopes.get(self._scope_key(project_path, module_name))
        if exact:
            return exact
        fallback = scopes.get(self._scope_key(None, module_name))
        return fallback if isinstance(fallback, dict) else None

    def _path_satisfied(self, required: str, read_paths: set[str], inspected_paths: set[str]) -> bool:
        if required in read_paths or required in inspected_paths:
            return True
        try:
            req = Path(required).expanduser().resolve()
        except Exception:
            return False
        if req.is_dir():
            req_s = str(req)
            return any(path == req_s or path.startswith(req_s.rstrip("/") + "/") for path in inspected_paths)
        return str(req) in read_paths

    def _is_project_module_path(self, raw_path: str, project: Path, module_name: str) -> bool:
        try:
            path = Path(str(raw_path)).expanduser().resolve()
            rel = path.relative_to(project)
        except Exception:
            return False
        parts = rel.parts
        return bool(parts and module_name in parts)

    def _next_call_for_project_path(self, raw_path: str, project: Path, client_name: str | None) -> dict[str, Any]:
        path = Path(str(raw_path)).expanduser().resolve()
        try:
            rel = path.relative_to(project).as_posix()
            return {"tool": "repo.read", "arguments": {"project_path": str(project), "relative_path": rel, "client_name": client_name}}
        except Exception:
            return {"tool": "repo.read", "arguments": {"project_path": str(path.parent), "relative_path": path.name, "client_name": client_name}}

    def _next_call_for_required_path(self, required: str, client_name: str | None) -> dict[str, Any]:
        path = Path(required)
        if path.is_dir():
            return {"tool": "repo.list", "arguments": {"project_path": str(path), "relative_path": ".", "client_name": client_name}}
        return {"tool": "repo.read", "arguments": {"project_path": str(path.parent), "relative_path": path.name, "client_name": client_name}}


    def _normalised_target_paths(self, project: Path, relative_paths: Iterable[str]) -> Iterable[Path]:
        for raw in relative_paths:
            for value in self._paths_from_maybe_patch_path(raw):
                text = str(value or "").replace("\\", "/").strip()
                if not text or text.startswith(("/", "..")) or "\x00" in text:
                    continue
                try:
                    resolved = (project / text).resolve()
                except Exception:
                    continue
                if resolved == project or project in resolved.parents:
                    yield resolved

    def _path_inside(self, path: Path, directory: Path) -> bool:
        try:
            path.resolve().relative_to(directory.resolve())
            return True
        except Exception:
            return False

    def _is_lightweight_ui_change(self, project: Path, target_paths: Iterable[Path]) -> bool:
        targets = list(target_paths)
        if not targets:
            return False
        for path in targets:
            try:
                rel = path.resolve().relative_to(project.resolve()).as_posix()
            except Exception:
                return False
            parts = set(rel.split("/"))
            suffix = path.suffix.lower()
            if any(part in _STRICT_DIR_HINTS for part in parts):
                return False
            if suffix not in _LIGHTWEIGHT_UI_EXTENSIONS:
                return False
            if suffix in {".css", ".scss"}:
                continue
            # XML is lightweight only for normal view/template/frontend files.
            # Data/security/report/controller/model-related XML still requires
            # the full gate above.
            if not ("views" in parts or "templates" in parts or "static" in parts):
                return False
        return True

    def _modules_for_relative_paths(self, project: Path, relative_paths: Iterable[str]) -> dict[str, Path]:
        modules: dict[str, Path] = {}
        for rel in relative_paths:
            for path in self._paths_from_maybe_patch_path(rel):
                module = self._module_dir_for_relative(project, path)
                if module:
                    modules[module.name] = module
        return modules

    def _module_dir_for_relative(self, project: Path, relative_path: str) -> Path | None:
        value = str(relative_path or "").replace("\\", "/").strip()
        if not value or value.startswith(("/", "..")) or "\x00" in value:
            return None
        parts = [p for p in value.split("/") if p and p not in {".", ".."}]
        if not parts:
            return None
        # For existing files, scan all ancestors. For new files, scan parent dirs.
        candidate = (project / Path(*parts)).resolve()
        ancestors = [candidate] + list(candidate.parents)
        for current in ancestors:
            if current == project or project not in current.parents:
                continue
            if any((current / manifest).exists() for manifest in _MANIFEST_NAMES):
                return current
        return None

    def _module_for_abs_path(self, project_path: str, abs_path: Path) -> str | None:
        project = self._safe_project(project_path)
        try:
            rel = abs_path.resolve().relative_to(project)
        except Exception:
            return None
        module = self._module_dir_for_relative(project, rel.as_posix())
        return module.name if module else None

    def _abs_from_project(self, project_path: str, relative_path: str) -> Path | None:
        try:
            project = self._safe_project(project_path)
        except Exception:
            return None
        rel = Path(str(relative_path or "."))
        if rel.is_absolute() or "\x00" in str(relative_path):
            return None
        try:
            resolved = (project / rel).resolve()
        except Exception:
            return None
        if not (resolved == project or project in resolved.parents):
            return None
        return resolved

    def _safe_project(self, project_path: str) -> Path:
        return resolve_allowed_path(project_path)

    def _infer_project_from_custom_paths(self, module_name: str, custom_file_paths: Iterable[str]) -> str | None:
        for raw in custom_file_paths:
            try:
                path = Path(str(raw)).expanduser().resolve()
            except Exception:
                continue
            parts = path.parts
            for index, part in enumerate(parts):
                if part != module_name or index == 0:
                    continue
                project = Path(*parts[:index]) if parts[0] != "/" else Path("/", *parts[1:index])
                if project.exists() and (project / module_name / "__manifest__.py").exists():
                    return str(project)
                # Still return a reasonable root when the repo has not been
                # materialized in tests; the gate can also use module-only scope.
                return str(project)
        return None

    def _normalise_required_path(self, value: str) -> str | None:
        value = str(value or "").strip()
        if not value or value.startswith("NOT_FOUND:"):
            return None
        try:
            return str(Path(value).expanduser().resolve())
        except Exception:
            return value

    def _paths_from_maybe_patch_path(self, value: str) -> list[str]:
        return [value]

    def paths_from_patch(self, patch: str) -> list[str]:
        paths: list[str] = []
        for line in str(patch or "").splitlines():
            if not line.startswith(("+++ ", "--- ", "diff --git ")):
                continue
            if line.startswith("diff --git "):
                parts = line.split()
                candidates = parts[2:4]
            else:
                candidates = [line[4:].strip()]
            for raw in candidates:
                candidate = raw.split("\t", 1)[0].strip().strip('"')
                if candidate == "/dev/null":
                    continue
                if candidate.startswith(("a/", "b/")):
                    candidate = candidate[2:]
                if candidate and not candidate.startswith(("/", "..")) and candidate not in paths:
                    paths.append(candidate)
        return paths

    def _module_key(self, project_path: str, module_name: str) -> str:
        return f"{project_path}::{module_name}"

    def _scope_key(self, project_path: str | None, module_name: str) -> str:
        return f"{project_path or '*'}::{module_name}"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _json_dump(self, value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
