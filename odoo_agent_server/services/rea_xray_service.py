"""REA Phase 1: bounded, evidence-backed Odoo module X-Ray graph.

This service is intentionally static and read-only. It inspects a checked-out
custom addon using Python's AST and ElementTree; it does not import addon code,
execute source, contact external hosts, or require persistent storage.
"""
from __future__ import annotations

import ast
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


class ReaXrayService:
    """Build a compact module/model/view dependency graph from addon source."""

    _EXTENSIONS = {".py", ".xml"}
    _SKIP_PARTS = {
        ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules",
        "static", "lib",
    }
    _MAX_FILE_BYTES = 256_000
    _MAX_NODES = 500
    _MAX_EDGES = 1_000

    @staticmethod
    def _literal_strings(node: ast.AST | None) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [
                value.value for value in node.elts
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            ]
        return []

    @staticmethod
    def _target_path(module_path: Path, candidate: Path) -> str | None:
        try:
            return candidate.resolve().relative_to(module_path).as_posix()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _local_tag(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _field_call(value: ast.AST) -> tuple[str | None, str | None]:
        if not isinstance(value, ast.Call):
            return None, None
        try:
            field_type = ast.unparse(value.func).split(".")[-1]
        except Exception:  # noqa: BLE001
            return None, None
        if field_type not in {
            "Many2one", "One2many", "Many2many",
        }:
            return None, None
        comodel = None
        if value.args and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
            comodel = value.args[0].value
        for keyword in value.keywords:
            if keyword.arg in {"comodel_name", "comodel"} and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    comodel = keyword.value.value
        return field_type, comodel

    def build(
        self,
        *,
        module_path: Path,
        module_name: str,
        max_files: int = 50,
    ) -> dict[str, Any]:
        """Return bounded graph nodes/edges with source-path evidence."""
        module_path = module_path.resolve(strict=True)
        if not module_path.is_dir():
            raise ValueError("module_path must be an existing addon directory")
        manifest_path = module_path / "__manifest__.py"
        if not manifest_path.is_file():
            raise ValueError("The selected addon has no __manifest__.py")

        try:
            manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            raise ValueError(f"Unable to parse addon manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("Addon manifest must contain a dictionary")

        candidates: list[tuple[str, Path]] = []
        for path in module_path.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in self._EXTENSIONS:
                continue
            relative = self._target_path(module_path, path)
            if not relative:
                continue
            if any(part in self._SKIP_PARTS for part in Path(relative).parts):
                continue
            candidates.append((relative, path))
        candidates.sort(key=lambda item: (
            0 if item[0] == "__manifest__.py" else
            1 if item[0].startswith("models/") else
            2 if item[0].startswith("views/") else 3,
            item[0],
        ))

        selected = candidates[: max(1, min(int(max_files), 80))]
        omitted = [relative for relative, _ in candidates[len(selected):]]
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        warnings: list[dict[str, str]] = []

        def add_node(node_id: str, kind: str, name: str, **extra: Any) -> None:
            if len(nodes) >= self._MAX_NODES and node_id not in nodes:
                return
            current = nodes.setdefault(node_id, {"id": node_id, "kind": kind, "name": name})
            for key, value in extra.items():
                if value is not None and value != "":
                    current.setdefault(key, value)

        def add_edge(source: str, target: str, kind: str, evidence: dict[str, Any]) -> None:
            key = (source, target, kind)
            if key in edges or len(edges) >= self._MAX_EDGES:
                return
            edges[key] = {
                "source": source,
                "target": target,
                "kind": kind,
                "evidence": evidence,
            }

        module_id = f"module:{module_name}"
        add_node(
            module_id, "module", module_name,
            version=manifest.get("version"),
            summary=manifest.get("summary") or manifest.get("name"),
        )
        dependencies = [
            value for value in manifest.get("depends", [])
            if isinstance(value, str) and value.strip()
        ]
        for dependency in dependencies:
            dependency_id = f"module:{dependency}"
            add_node(dependency_id, "module", dependency, external=True)
            add_edge(
                module_id, dependency_id, "depends_on",
                {"path": "__manifest__.py", "line": None},
            )

        scanned: list[str] = []
        for relative, path in selected:
            try:
                if path.stat().st_size > self._MAX_FILE_BYTES:
                    warnings.append({"code": "FILE_TOO_LARGE", "path": relative})
                    omitted.append(relative)
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                warnings.append({"code": "FILE_UNREADABLE", "path": relative})
                omitted.append(relative)
                continue
            scanned.append(relative)

            if path.suffix.lower() == ".py":
                try:
                    tree = ast.parse(source, filename=relative)
                except SyntaxError as exc:
                    warnings.append({
                        "code": "PYTHON_PARSE_ERROR",
                        "path": relative,
                        "message": str(exc)[:240],
                    })
                    continue

                for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
                    model_names: list[str] = []
                    for item in class_node.body:
                        if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                            continue
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                        names = [target.id for target in targets if isinstance(target, ast.Name)]
                        for target_name in names:
                            if target_name == "_name":
                                model_names.extend(self._literal_strings(item.value))
                            elif target_name == "_inherit":
                                model_names.extend(self._literal_strings(item.value))
                            elif target_name == "_inherits" and isinstance(item.value, ast.Dict):
                                model_names.extend(
                                    key.value for key in item.value.keys
                                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                                )

                    own_model = None
                    for item in class_node.body:
                        if isinstance(item, (ast.Assign, ast.AnnAssign)):
                            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                            if any(isinstance(target, ast.Name) and target.id == "_name" for target in targets):
                                values = self._literal_strings(item.value)
                                own_model = values[0] if values else None
                                break

                    inherited = []
                    for item in class_node.body:
                        if isinstance(item, (ast.Assign, ast.AnnAssign)):
                            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                            if any(isinstance(target, ast.Name) and target.id == "_inherit" for target in targets):
                                inherited = self._literal_strings(item.value)
                                break

                    owner_models = [own_model] if own_model else inherited
                    for model in dict.fromkeys(model_names):
                        model_id = f"model:{model}"
                        add_node(model_id, "model", model)
                        relation = "defines_model" if model == own_model else "extends_model"
                        add_edge(
                            module_id, model_id, relation,
                            {"path": relative, "line": class_node.lineno},
                        )

                    for item in class_node.body:
                        if isinstance(item, (ast.Assign, ast.AnnAssign)):
                            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                            field_type, comodel = self._field_call(item.value)
                            if not field_type:
                                continue
                            for target in targets:
                                if not isinstance(target, ast.Name):
                                    continue
                                for owner in owner_models:
                                    owner_id = f"model:{owner}"
                                    field_id = f"field:{owner}.{target.id}"
                                    add_node(field_id, "field", target.id, model=owner, field_type=field_type, comodel=comodel)
                                    add_edge(owner_id, field_id, "has_field", {"path": relative, "line": item.lineno})
                                    if comodel:
                                        comodel_id = f"model:{comodel}"
                                        add_node(comodel_id, "model", comodel, external=True)
                                        add_edge(field_id, comodel_id, "relates_to", {"path": relative, "line": item.lineno})

                        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for owner in owner_models:
                                method_id = f"method:{owner}.{item.name}"
                                add_node(method_id, "method", item.name, model=owner)
                                add_edge(
                                    f"model:{owner}", method_id, "has_method",
                                    {"path": relative, "line": item.lineno},
                                )

            elif path.suffix.lower() == ".xml":
                try:
                    root = ET.fromstring(source)
                except ET.ParseError as exc:
                    warnings.append({"code": "XML_PARSE_ERROR", "path": relative, "message": str(exc)[:240]})
                    continue

                for record in (node for node in root.iter() if self._local_tag(node.tag) == "record"):
                    xmlid = record.attrib.get("id")
                    record_model = record.attrib.get("model")
                    if not xmlid:
                        continue
                    local_xmlid = f"{module_name}.{xmlid}"
                    fields = {
                        child.attrib.get("name"): (child.text or "").strip()
                        for child in record
                        if self._local_tag(child.tag) == "field" and child.attrib.get("name")
                    }
                    if record_model == "ir.ui.view":
                        view_id = f"view:{local_xmlid}"
                        target_model = fields.get("model")
                        add_node(view_id, "view", xmlid, model=target_model, source=relative)
                        if target_model:
                            model_id = f"model:{target_model}"
                            add_node(model_id, "model", target_model, external=True)
                            add_edge(view_id, model_id, "view_for", {"path": relative, "line": None})
                        inherit_ref = next(
                            (child.attrib.get("ref") for child in record.iter()
                             if self._local_tag(child.tag) == "field"
                             and child.attrib.get("name") == "inherit_id"),
                            None,
                        )
                        if inherit_ref:
                            parent_id = f"view:{inherit_ref}"
                            add_node(parent_id, "view", inherit_ref, external=True)
                            add_edge(view_id, parent_id, "inherits_view", {"path": relative, "line": None})
                        arch = next(
                            (child for child in record.iter()
                             if self._local_tag(child.tag) == "field" and child.attrib.get("name") == "arch"),
                            None,
                        )
                        if arch is not None:
                            for element in arch.iter():
                                tag = self._local_tag(element.tag)
                                if tag == "button" and element.attrib.get("name") and target_model:
                                    method_id = f"method:{target_model}.{element.attrib['name']}"
                                    add_node(method_id, "method", element.attrib["name"], model=target_model)
                                    add_edge(view_id, method_id, "invokes_method", {"path": relative, "line": None})
                                elif tag == "field" and element.attrib.get("name") and target_model:
                                    field_id = f"field:{target_model}.{element.attrib['name']}"
                                    add_node(field_id, "field", element.attrib["name"], model=target_model)
                                    add_edge(view_id, field_id, "displays_field", {"path": relative, "line": None})
                    elif record_model == "ir.actions.act_window":
                        action_id = f"action:{local_xmlid}"
                        add_node(action_id, "action", xmlid, res_model=fields.get("res_model"))
                        if fields.get("res_model"):
                            model_id = f"model:{fields['res_model']}"
                            add_node(model_id, "model", fields["res_model"], external=True)
                            add_edge(action_id, model_id, "opens_model", {"path": relative, "line": None})

                for menu in (node for node in root.iter() if self._local_tag(node.tag) == "menuitem"):
                    menu_xmlid = menu.attrib.get("id")
                    if not menu_xmlid:
                        continue
                    menu_id = f"menu:{module_name}.{menu_xmlid}"
                    add_node(menu_id, "menu", menu.attrib.get("name") or menu_xmlid)
                    action = menu.attrib.get("action")
                    if action:
                        action_id = f"action:{action}"
                        add_node(action_id, "action", action, external="." in action and not action.startswith(module_name + "."))
                        add_edge(menu_id, action_id, "opens_action", {"path": relative, "line": None})
                    parent = menu.attrib.get("parent")
                    if parent:
                        parent_id = f"menu:{parent}"
                        add_node(parent_id, "menu", parent, external=True)
                        add_edge(menu_id, parent_id, "child_of", {"path": relative, "line": None})

                for template in (node for node in root.iter() if self._local_tag(node.tag) in {"template", "t"}):
                    template_name = template.attrib.get("id") or template.attrib.get("t-name")
                    if template_name:
                        template_id = f"template:{module_name}.{template_name}"
                        add_node(template_id, "template", template_name, source=relative)
                        add_edge(module_id, template_id, "defines_template", {"path": relative, "line": None})

        return {
            "status": "mapped",
            "graph_version": "1",
            "module_name": module_name,
            "summary": {
                "dependencies": len(dependencies),
                "models": sum(1 for item in nodes.values() if item["kind"] == "model"),
                "fields": sum(1 for item in nodes.values() if item["kind"] == "field"),
                "methods": sum(1 for item in nodes.values() if item["kind"] == "method"),
                "views": sum(1 for item in nodes.values() if item["kind"] == "view"),
                "actions": sum(1 for item in nodes.values() if item["kind"] == "action"),
                "menus": sum(1 for item in nodes.values() if item["kind"] == "menu"),
                "nodes": len(nodes),
                "edges": len(edges),
            },
            "coverage": {
                "available_files": len(candidates),
                "scanned_files": len(scanned),
                "omitted_files": sorted(set(omitted)),
                "complete": not omitted and len(scanned) == len(candidates),
            },
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "warnings": warnings,
            "evidence_policy": "Every edge includes its source path; relationships are static-source observations, not runtime proof.",
        }
