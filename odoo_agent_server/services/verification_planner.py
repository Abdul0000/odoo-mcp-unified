"""Bounded source-derived test proposals; never treat syntax as business proof."""

import json
import hashlib
from pathlib import Path

from fastapi import HTTPException
from ..execution_schemas import TaskPlan
from .execution_service import ExecutionService, git


def suggest_checks(workspace_id, apply=False):
    svc = ExecutionService()
    with svc.store.workspace_lock(workspace_id):
        svc.assert_idle(workspace_id)
        svc.workspace(workspace_id)
        root = svc.root(workspace_id)
        changed = list(dict.fromkeys((git(["diff", "--name-only", "HEAD", "--"], root) + "\n" + git(["ls-files", "--others", "--exclude-standard"], root)).splitlines()))
        if len(changed) > 500:
            raise HTTPException(409, "Change scope exceeds automatic test planning budget")
        paths = []
        for name in changed:
            path = svc.target(workspace_id, name)
            if path.is_file():
                paths.append(name)
        checks = []
        blockers = []

        def add(name, argv, layer):
            name += "-" + hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:8]
            checks.append({"name": name, "argv": argv, "layer": layer, "cwd": ".", "timeout_seconds": 120, "output_dirs": []})

        python = [p for p in paths if p.endswith(".py")]
        xml = [p for p in paths if p.endswith(".xml")]
        if python:
            add("changed-python-syntax", ["python", "-c", "import ast,pathlib,sys; [ast.parse(pathlib.Path(p).read_text(), filename=p) for p in sys.argv[1:]]", *python], "syntax")
        if xml:
            add("changed-xml-syntax", ["python", "-c", "import pathlib,sys,xml.etree.ElementTree as ET; [ET.parse(pathlib.Path(p)) for p in sys.argv[1:]]", *xml], "syntax")
        stems = {Path(p).stem.removeprefix("test_") for p in paths}
        modules = set()
        for name in paths:
            parent = Path(name).parent
            for _ in range(10):
                if (root / parent / "__manifest__.py").is_file():
                    modules.add(parent.as_posix())
                    break
                if parent == Path("."):
                    break
                parent = parent.parent

        def native_path(name):
            return any((module == "." and name.startswith("tests/")) or name.startswith(module + "/tests/") for module in modules)

        candidates = git(["ls-files", "--cached", "--others", "--exclude-standard", "--", "*test*.py"], root).splitlines()
        if len(candidates) > 2000:
            blockers.append("Test inventory exceeds 2,000 files; narrow test scope manually.")
        unit = []
        native = []
        for name in candidates[:2000]:
            if Path(name).stem.removeprefix("test_") not in stems and not native_path(name):
                continue
            svc.target(workspace_id, name)
            if native_path(name):
                native.append(name)
            else:
                unit.append(name)
        if unit:
            add("related-unit-tests", ["python", "-m", "pytest", "-q", *unit[:50]], "unit")
        if native:
            blockers.append("Odoo-native tests require an isolated Odoo test database/runtime and version-correct test tags: " + ", ".join(native[:20]))
        frontend = any(p.endswith((".js", ".ts", ".scss", ".css")) or "/static/" in p for p in paths)
        if frontend and (root / "package.json").is_file():
            svc.target(workspace_id, "package.json")
            try:
                if (root / "package.json").stat().st_size > 100000:
                    raise ValueError
                scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
                if scripts.get("test"):
                    add("frontend-tests", ["npm", "run", "test"], "unit")
                if scripts.get("build"):
                    add("frontend-build", ["npm", "run", "build"], "build")
            except (OSError, ValueError, AttributeError):
                blockers.append("Read package.json manually before selecting frontend commands.")
        if frontend:
            blockers.append("A visible UI change also requires task-specific browser assertions; build success alone is not UI proof.")
        if not unit and not native and not frontend:
            blockers.append("No related regression test was identified. Supply acceptance-specific unit/integration checks.")
        required = sorted({"syntax" if python or xml else "unit", "orm" if native else "unit"} | ({"build"} if frontend else set()))
        available = {c["layer"] for c in checks}
        missing = sorted(set(required) - available)
        result = {
            "workspace_id": workspace_id,
            "tree": svc.tree(workspace_id),
            "changed_paths": changed,
            "suggested_checks": checks,
            "required_layers": required,
            "missing_layers": missing,
            "blockers": blockers,
            "applied": False,
            "success_allowed": False,
            "verification_scope": "source-derived proposals; existing acceptance checks cannot be removed",
        }
    if apply:
        if missing:
            raise HTTPException(409, {"message": "Required test layers need task-specific checks before applying this plan.", "plan": result})
        existing = svc.workspace(workspace_id)
        old = existing.get("checks", [])
        old_names = {c["name"] for c in old}
        combined = old + [c for c in checks if c["name"] not in old_names]
        result["plan"] = svc.plan(
            TaskPlan(
                workspace_id=workspace_id, checks=combined, required_layers=sorted(set(existing.get("required_layers", [])) | set(required)), max_repair_attempts=existing.get("max_repair_attempts", 3)
            )
        )
        result["applied"] = True
    return result
