"""Compact structural outlines of Odoo addon files (stdlib + optional lxml).

Reading a 600-line model to learn its fields costs thousands of tokens; the outline gives the same
orientation in a few lines: classes, ``_name``/``_inherit``, fields with types, methods with decorators,
view records with their inheritance, menus and actions.
"""
from __future__ import annotations

import ast
import re
from typing import Any

from . import lint_service as lint

_REL = {"Many2one", "One2many", "Many2many"}
_KEEP = ("comodel_name", "compute", "related", "inverse_name", "required", "store")


def _const(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None


def _field_info(name: str, call: ast.Call, line: int) -> dict[str, Any] | None:
    func = call.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "fields"):
        return None
    kind = func.attr
    info: dict[str, Any] = {"name": name, "type": kind, "line": line}
    if kind in _REL and call.args:
        value = _const(call.args[0])
        if isinstance(value, str):
            info["comodel"] = value
    for kw in call.keywords:
        if kw.arg in _KEEP:
            value = _const(kw.value)
            if kw.arg == "compute" and value is None and isinstance(kw.value, ast.Name):
                value = kw.value.id
            if value is not None:
                info["comodel" if kw.arg == "comodel_name" else kw.arg] = value
    if kind == "Selection" and call.args:
        options = _const(call.args[0])
        if isinstance(options, list):
            info["options"] = [o[0] for o in options if isinstance(o, (tuple, list)) and o]
    return info


def _field_label(info: dict[str, Any]) -> str:
    label = f"{info['name']}:{info['type']}"
    if info.get("comodel"):
        label += f"({info['comodel']})"
    extras = [k for k in ("compute", "related") if info.get(k)]
    if extras:
        label += "[" + ",".join(f"{k}={info[k]}" for k in extras) + "]"
    return label


def outline_python(path: str, text: str) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {"path": path, "error": f"syntax error line {exc.lineno}: {exc.msg}"}
    classes = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        model = inherit = None
        fields: list[dict[str, Any]] = []
        methods: list[str] = []
        for item in node.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                target = item.targets[0].id
                if target == "_name":
                    model = _const(item.value)
                elif target == "_inherit":
                    inherit = _const(item.value)
                elif isinstance(item.value, ast.Call):
                    info = _field_info(target, item.value, item.lineno)
                    if info:
                        fields.append(info)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decos = []
                for d in item.decorator_list:
                    base = d.func if isinstance(d, ast.Call) else d
                    decos.append("@" + (ast.unparse(base).replace("api.", "") if hasattr(ast, "unparse") else "?"))
                methods.append(f"{item.name}{'(' + ' '.join(decos) + ')' if decos else ''}@{item.lineno}")
        if model is None and inherit is None and not fields:
            continue
        inherit_text = ",".join(inherit) if isinstance(inherit, list) else (inherit or "")
        classes.append({
            "file": path,
            "class": node.name,
            "model": model or inherit_text,
            "kind": "define" if model else "extend",
            "inherit": inherit_text if model else "",
            "line": node.lineno,
            "fields": ", ".join(_field_label(f) for f in fields),
            "methods": ", ".join(methods),
            "_fields": fields,
        })
    return {"path": path, "classes": classes}


def outline_xml(path: str, text: str) -> dict[str, Any]:
    try:
        root = lint._xml_parse(text)
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "error": f"xml error: {str(exc)[:160]}"}
    records: list[dict[str, Any]] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        line = getattr(el, "sourceline", None)
        if el.tag == "record":
            model = el.get("model", "")
            entry: dict[str, Any] = {"file": path, "id": el.get("id", ""), "model": model, "line": line}
            fields = {f.get("name"): f for f in el if isinstance(f.tag, str) and f.tag == "field"}
            if model == "ir.ui.view":
                if "model" in fields:
                    entry["of"] = (fields["model"].text or "").strip()
                if "inherit_id" in fields:
                    entry["inherits"] = fields["inherit_id"].get("ref", "")
                arch = fields.get("arch")
                if arch is not None:
                    kids = [c for c in arch if isinstance(c.tag, str)]
                    if kids:
                        entry["root"] = kids[0].tag
                    entry["xpaths"] = "; ".join(f"{x.get('expr', '')}|{x.get('position', '')}" for x in arch.iter("xpath"))[:300]
            elif model == "ir.actions.act_window":
                if "res_model" in fields:
                    entry["of"] = (fields["res_model"].text or "").strip()
            records.append(entry)
        elif el.tag == "menuitem":
            records.append({"file": path, "id": el.get("id", ""), "model": "menuitem", "line": line, "of": el.get("name", ""), "inherits": el.get("parent", ""), "root": el.get("action", "")})
        elif el.tag == "template":
            records.append({"file": path, "id": el.get("id", ""), "model": "qweb", "line": line, "inherits": el.get("inherit_id", "")})
    return {"path": path, "records": records}


def outline_manifest(text: str) -> dict[str, Any]:
    try:
        data = ast.literal_eval(text.strip())
    except Exception:  # noqa: BLE001
        return {"error": "manifest is not a literal dict"}
    if not isinstance(data, dict):
        return {"error": "manifest is not a dict"}
    assets = data.get("assets") or {}
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "depends": data.get("depends", []),
        "data": len(data.get("data", []) or []),
        "demo": len(data.get("demo", []) or []),
        "asset_bundles": sorted(assets) if isinstance(assets, dict) else [],
        "installable": data.get("installable", True),
    }


def series_of(manifest_version: str | None) -> int | None:
    """'18.0.1.0.0' -> 18. Returns None when the version has no Odoo series prefix."""
    m = re.match(r"^(\d{1,2})\.\d+", str(manifest_version or ""))
    return int(m.group(1)) if m else None


def build_outline(files: dict[str, str], root: str) -> dict[str, Any]:
    """Outline a set of module files already loaded into ``files`` (path -> text)."""
    prefix = f"{root}/" if root else ""
    manifest = outline_manifest(files.get(prefix + "__manifest__.py", "")) if (prefix + "__manifest__.py") in files else {}
    models, views, py_errors = [], [], []
    for path in sorted(files):
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if path.endswith(".py") and not path.endswith(("__init__.py", "__manifest__.py")):
            out = outline_python(rel, files[path])
            if out.get("error"):
                py_errors.append({"file": rel, "error": out["error"]})
            for cls in out.get("classes", []):
                cls.pop("_fields", None)
                models.append(cls)
        elif path.endswith(".xml") and not rel.startswith("static/"):
            out = outline_xml(rel, files[path])
            if out.get("error"):
                py_errors.append({"file": rel, "error": out["error"]})
            views.extend(out.get("records", []))
    result: dict[str, Any] = {"status": "ok", "manifest": manifest, "models": models, "records": views}
    if py_errors:
        result["unparseable"] = py_errors
    return result
