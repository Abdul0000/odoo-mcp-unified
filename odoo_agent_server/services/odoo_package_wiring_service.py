from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


class OdooPackageWiringService:
    """Expand Python writes with the minimal Odoo package ``__init__`` wiring.

    The service is deliberately manifest-driven: ordinary Python projects are
    untouched. Inside an Odoo addon, a new/updated Python implementation file
    receives its direct relative import and every child package is connected up
    to the addon root. Existing init content is preserved byte-for-byte except
    for appended missing import lines.
    """

    _MANIFEST_NAMES = {"__manifest__.py", "__openerp__.py"}
    _PYTHON_METADATA = {"__manifest__.py", "__openerp__.py"}
    _EXCLUDED_PARTS = {"migrations", "static", "__pycache__"}
    _NON_ROOT_PACKAGES = {"test", "tests"}

    def expand(self, project: Path, files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        project = project.resolve()
        expanded = [dict(item) for item in files]
        by_rel = {str(item["relative_path"]): item for item in expanded}
        planned_roots = {
            (project / str(item["relative_path"])).resolve().parent
            for item in expanded
            if Path(str(item["relative_path"])).name in self._MANIFEST_NAMES
        }
        auto_wired: list[str] = []

        python_targets = sorted(
            (project / rel).resolve()
            for rel in by_rel
            if Path(rel).suffix == ".py" and Path(rel).name not in self._PYTHON_METADATA
        )
        for target in python_targets:
            module_root = self._module_root(project, target, planned_roots)
            if module_root is None:
                continue
            relative = target.relative_to(module_root)
            if any(part in self._EXCLUDED_PARTS or part.startswith(".") for part in relative.parts[:-1]):
                continue

            directory = target.parent
            if target.name != "__init__.py":
                self._ensure_import(project, directory / "__init__.py", target.stem, by_rel, expanded, auto_wired)

            while directory != module_root:
                child_name = directory.name
                parent = directory.parent
                if parent == module_root and child_name in self._NON_ROOT_PACKAGES:
                    break
                self._ensure_import(project, parent / "__init__.py", child_name, by_rel, expanded, auto_wired)
                directory = parent

        return expanded, auto_wired

    def _module_root(self, project: Path, target: Path, planned_roots: set[Path]) -> Path | None:
        current = target.parent
        while current == project or project in current.parents:
            if current in planned_roots or any((current / name).is_file() for name in self._MANIFEST_NAMES):
                return current
            if current == project:
                break
            current = current.parent
        return None

    def _ensure_import(
        self,
        project: Path,
        init_path: Path,
        symbol: str,
        by_rel: dict[str, dict[str, Any]],
        expanded: list[dict[str, Any]],
        auto_wired: list[str],
    ) -> None:
        if not symbol.isidentifier():
            raise ValueError(f"Cannot auto-import invalid Python module/package name: {symbol!r}")
        rel = init_path.relative_to(project).as_posix()
        item = by_rel.get(rel)
        if item is not None:
            base = self._effective_item_text(init_path, item)
        else:
            base = init_path.read_text(encoding="utf-8") if init_path.is_file() else ""
        updated = self._append_relative_import(base, symbol, rel)
        if updated == base:
            return
        if item is None:
            item = {
                "relative_path": rel,
                "content": updated,
                "create_dirs": True,
                "mode": "overwrite",
                "force": False,
                "auto_generated": True,
            }
            expanded.append(item)
            by_rel[rel] = item
        else:
            item["content"] = updated
            item["mode"] = "overwrite"
        if rel not in auto_wired:
            auto_wired.append(rel)

    @staticmethod
    def _effective_item_text(path: Path, item: dict[str, Any]) -> str:
        content = str(item.get("content") or "")
        if str(item.get("mode") or "overwrite") != "append":
            return content
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        return existing + content

    @staticmethod
    def _append_relative_import(content: str, symbol: str, relative_path: str) -> str:
        if content.strip():
            try:
                tree = ast.parse(content, filename=relative_path)
            except SyntaxError as exc:
                raise ValueError(f"Cannot auto-wire invalid Python package file {relative_path}: {exc}") from exc
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom) or node.level < 1:
                    continue
                if node.module and node.module.split(".", 1)[0] == symbol:
                    return content
                if node.module is None and any(alias.name == symbol for alias in node.names):
                    return content
        suffix = "" if not content or content.endswith("\n") else "\n"
        return f"{content}{suffix}from . import {symbol}\n"
