"""Instant, dependency-light lint for Odoo addon files.

Operates on an in-memory ``{relative_path: text}`` mapping so it can run on
every workspace edit without a checkout. Findings are plain dicts:
``{severity, path, line, col, code, msg, fix}``; ``severity`` is ``error``
(will break install/build), ``warn`` or ``info``.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from typing import Any, Iterable

try:  # lxml gives accurate line numbers and XPath compilation
    from lxml import etree as _etree  # type: ignore
except Exception:  # pragma: no cover - lxml is in requirements
    _etree = None

_ACCESS_HEADER = ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"]
_NODE = shutil.which("node")


def finding(severity: str, path: str, code: str, msg: str, line: int | None = None, col: int | None = None, fix: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"severity": severity, "path": path, "code": code, "msg": msg}
    if line is not None:
        out["line"] = line
    if col is not None:
        out["col"] = col
    if fix:
        out["fix"] = fix
    return out


# ------------------------------------------------------------------ per-file lint
def lint_python(path: str, text: str) -> list[dict[str, Any]]:
    try:
        compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return [finding("error", path, "py_syntax", f"{exc.msg}", exc.lineno, exc.offset)]
    out: list[dict[str, Any]] = []
    if "\t" in text and re.search(r"^ +\t|^\t+ +\S", text, re.M):
        out.append(finding("warn", path, "py_mixed_indent", "mixed tabs and spaces in indentation"))
    if path.endswith("__manifest__.py"):
        out.extend(_lint_manifest_shape(path, text))
    return out


def _lint_manifest_shape(path: str, text: str) -> list[dict[str, Any]]:
    try:
        data = ast.literal_eval(text.strip())
    except Exception as exc:  # noqa: BLE001
        return [finding("error", path, "manifest_not_literal", f"manifest must be a single dict literal: {exc}")]
    if not isinstance(data, dict):
        return [finding("error", path, "manifest_not_dict", "manifest must evaluate to a dict")]
    out = []
    if not data.get("name"):
        out.append(finding("error", path, "manifest_name", "manifest needs a 'name'"))
    for key in ("depends", "data", "demo"):
        if key in data and not isinstance(data[key], list):
            out.append(finding("error", path, "manifest_type", f"'{key}' must be a list"))
    if "depends" not in data:
        out.append(finding("warn", path, "manifest_depends", "no 'depends' - module will only load 'base'"))
    return out


def _xml_parse(text: str):
    raw = text.encode("utf-8")
    if _etree is not None:
        parser = _etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
        return _etree.fromstring(raw, parser)
    import xml.etree.ElementTree as ET  # pragma: no cover

    return ET.fromstring(raw)


def lint_xml(path: str, text: str) -> list[dict[str, Any]]:
    try:
        root = _xml_parse(text)
    except Exception as exc:  # noqa: BLE001
        line = getattr(exc, "lineno", None) or getattr(getattr(exc, "position", None), "__getitem__", lambda i: None)(0)
        msg = str(exc)
        m = re.search(r"line (\d+)", msg)
        if m:
            line = int(m.group(1))
        return [finding("error", path, "xml_syntax", msg.split(",")[0][:300], line if isinstance(line, int) else None)]
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        line = getattr(el, "sourceline", None)
        if tag == "record":
            rid = el.get("id")
            if not rid:
                out.append(finding("error", path, "xml_record_id", "<record> without id", line))
            elif rid in seen:
                out.append(finding("error", path, "xml_duplicate_id", f"duplicate record id '{rid}' (first at line {seen[rid]})", line))
            else:
                seen[rid] = line or 0
            if not el.get("model"):
                out.append(finding("error", path, "xml_record_model", "<record> without model", line))
        if tag == "xpath":
            expr = el.get("expr")
            if not expr:
                out.append(finding("error", path, "xpath_missing", "<xpath> without expr", line))
            elif _etree is not None:
                try:
                    _etree.XPath(expr)
                except Exception as exc:  # noqa: BLE001
                    out.append(finding("error", path, "xpath_syntax", f"invalid xpath '{expr}': {exc}", line))
        if tag == "field" and el.get("name") == "inherit_id" and not el.get("ref") and not (el.text or "").strip():
            out.append(finding("error", path, "xml_inherit_ref", "inherit_id field without ref", line))
    return out


_JS_CACHE: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
_JS_CACHE_MAX = 256


def lint_js(path: str, text: str) -> list[dict[str, Any]]:
    """node --check costs a process spawn (~0.25s); identical content is never re-checked."""
    key = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()  # noqa: S324 - cache key only
    cached = _JS_CACHE.get(key)
    if cached is not None:
        _JS_CACHE.move_to_end(key)
        return [{**f, "path": path} for f in cached]
    found = _lint_js_uncached(path, text)
    if not any(f["code"] in {"js_timeout", "js_unchecked"} for f in found):
        _JS_CACHE[key] = found
        while len(_JS_CACHE) > _JS_CACHE_MAX:
            _JS_CACHE.popitem(last=False)
    return found


def _lint_js_uncached(path: str, text: str) -> list[dict[str, Any]]:
    if not _NODE:
        return [finding("info", path, "js_unchecked", "node is not installed - JS syntax not checked")]
    suffix = ".mjs"  # Odoo 17+ addons are ES modules; .mjs makes `import` legal for the parser
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "check" + suffix)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            proc = subprocess.run([_NODE, "--check", target], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return [finding("warn", path, "js_timeout", "node --check timed out")]
    if proc.returncode == 0:
        return []
    err = (proc.stderr or "").strip().splitlines()
    line = None
    for row in err:
        m = re.search(r"check\.mjs:(\d+)", row)
        if m:
            line = int(m.group(1))
            break
    detail = next((row for row in err if "Error" in row), err[0] if err else "syntax error")
    return [finding("error", path, "js_syntax", detail[:300], line)]


def lint_json(path: str, text: str) -> list[dict[str, Any]]:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [finding("error", path, "json_syntax", exc.msg, exc.lineno, exc.colno)]
    return []


def lint_css(path: str, text: str) -> list[dict[str, Any]]:
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    stripped = re.sub(r"//[^\n]*", "", stripped) if path.endswith(".scss") else stripped
    stripped = re.sub(r"(['\"]).*?\1", "", stripped)
    depth = 0
    for lineno, row in enumerate(stripped.splitlines(), 1):
        depth += row.count("{") - row.count("}")
        if depth < 0:
            return [finding("error", path, "css_brace", "unmatched closing brace", lineno)]
    if depth != 0:
        return [finding("error", path, "css_brace", f"{depth} unclosed brace(s)")]
    return []


def lint_access_csv(path: str, text: str) -> list[dict[str, Any]]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [finding("error", path, "csv_empty", "access file is empty")]
    header = [h.strip() for h in rows[0]]
    out = []
    if header != _ACCESS_HEADER:
        out.append(finding("error", path, "csv_header", f"header must be: {','.join(_ACCESS_HEADER)}", 1))
        return out
    for i, row in enumerate(rows[1:], 2):
        if not any(c.strip() for c in row):
            continue
        if len(row) != len(header):
            out.append(finding("error", path, "csv_columns", f"expected {len(header)} columns, got {len(row)}", i))
            continue
        if not row[2].startswith("model_"):
            out.append(finding("error", path, "csv_model_ref", f"model_id:id '{row[2]}' should look like model_<model_name_with_underscores> (or module.model_x)", i))
        for idx in (4, 5, 6, 7):
            if row[idx].strip() not in {"0", "1"}:
                out.append(finding("error", path, "csv_perm", f"{header[idx]} must be 0 or 1", i))
    return out


def lint_file(path: str, text: str) -> list[dict[str, Any]]:
    lower = path.lower()
    if lower.endswith(".py"):
        return lint_python(path, text)
    if lower.endswith(".xml"):
        return lint_xml(path, text)
    if lower.endswith((".js", ".mjs")):
        return lint_js(path, text)
    if lower.endswith(".json"):
        return lint_json(path, text)
    if lower.endswith((".scss", ".css")):
        return lint_css(path, text)
    if lower.endswith("ir.model.access.csv"):
        return lint_access_csv(path, text)
    return []


# ------------------------------------------------------------------ cross-file lint
def module_roots(paths: Iterable[str]) -> list[str]:
    roots = []
    for p in paths:
        if p.endswith("/__manifest__.py") or p == "__manifest__.py":
            roots.append(p.rsplit("/", 1)[0] if "/" in p else "")
    return sorted(set(roots))


def _join(root: str, rel: str) -> str:
    rel = rel.lstrip("./")
    return f"{root}/{rel}" if root else rel


def lint_module_structure(root: str, files: Any) -> list[dict[str, Any]]:
    """Closure checks that make an addon fail to install: dangling references.

    ``files`` is any mapping-like object (dict or lazy view): only ``get``, ``in`` and ``keys``
    are used, and content is read only for the manifest and ``__init__.py`` files.
    """
    out: list[dict[str, Any]] = []
    manifest_path = _join(root, "__manifest__.py")
    manifest_text = files.get(manifest_path)
    manifest = None
    if manifest_text is not None:
        try:
            manifest = ast.literal_eval(manifest_text.strip())
        except Exception:  # noqa: BLE001 - reported by lint_python
            manifest = None
    if isinstance(manifest, dict):
        for key in ("data", "demo"):
            for entry in manifest.get(key, []) or []:
                if isinstance(entry, str) and _join(root, entry) not in files:
                    out.append(finding("error", manifest_path, "manifest_missing_file", f"'{key}' lists '{entry}' but the file does not exist", fix=f"create {_join(root, entry)} or remove it from '{key}'"))
        assets = manifest.get("assets") or {}
        if isinstance(assets, dict):
            for bundle, entries in assets.items():
                for entry in entries or []:
                    target = _asset_path(entry)
                    if target and "*" not in target and not target.startswith(("http", "/")) and "/" in target:
                        parts = target.split("/", 1)
                        if parts[0] == os.path.basename(root) and _join(root, parts[1]) not in files:
                            out.append(finding("error", manifest_path, "asset_missing_file", f"asset '{target}' in bundle '{bundle}' does not exist", fix="create the file or remove it from assets"))
    out.extend(_lint_imports(root, files))
    return out


def _asset_path(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, (tuple, list)) and entry and entry[0] in {"after", "before", "replace", "prepend", "append"} and len(entry) >= 3:
        return entry[2] if isinstance(entry[2], str) else None
    return None


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


_ASSET_EXT = (".js", ".scss", ".css", ".xml", ".sass", ".less")


def lint_assets(root: str, files: Any, changed: Iterable[str]) -> list[dict[str, Any]]:
    """Warn when a static/src file is not covered by any manifest assets entry (it would never load)."""
    prefix = f"{root}/" if root else ""
    candidates = []
    for path in changed:
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        if not rel.startswith("static/src/") or not rel.endswith(_ASSET_EXT) or "/tests/" in rel or ".test." in rel:
            continue
        candidates.append((path, rel))
    if not candidates:
        return []
    manifest_text = files.get(_join(root, "__manifest__.py"))
    try:
        manifest = ast.literal_eval((manifest_text or "").strip())
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(manifest, dict):
        return []
    module = os.path.basename(root) if root else ""
    patterns = []
    assets = manifest.get("assets") or {}
    if isinstance(assets, dict):
        for entries in assets.values():
            for entry in entries or []:
                target = _asset_path(entry)
                if target:
                    patterns.append(_glob_regex(target.lstrip("/")))
    for entry in manifest.get("qweb", []) or []:  # Odoo <= 15 style
        if isinstance(entry, str):
            patterns.append(_glob_regex(f"{module}/{entry}" if not entry.startswith(module + "/") else entry))
    out = []
    for path, rel in candidates:
        logical = f"{module}/{rel}"
        if not any(p.fullmatch(logical) for p in patterns):
            out.append(finding("warn", path, "asset_unregistered", f"'{logical}' is not covered by any manifest 'assets' entry, so the browser will never load it", fix=f"add '{module}/static/src/**/*' (or this exact path) to the right bundle, e.g. 'web.assets_backend'"))
    return out


def _lint_imports(root: str, files: Any) -> list[dict[str, Any]]:
    out = []
    prefix = f"{root}/" if root else ""
    for path in sorted(p for p in files.keys() if p.endswith("__init__.py") and p.startswith(prefix)):
        text = files.get(path)
        if text is None:
            continue
        base = path.rsplit("/", 1)[0] if "/" in path else ""
        for lineno, row in enumerate(text.splitlines(), 1):
            m = re.match(r"\s*from\s+\.\s+import\s+(.+?)\s*(?:#.*)?$", row)
            if not m:
                continue
            for name in [n.strip() for n in m.group(1).split(",") if n.strip()]:
                name = name.split(" as ")[0].strip()
                if name.startswith("("):
                    continue
                candidates = (f"{base}/{name}.py" if base else f"{name}.py", f"{base}/{name}/__init__.py" if base else f"{name}/__init__.py")
                if not any(c in files for c in candidates):
                    out.append(finding("error", path, "import_missing", f"'from . import {name}' but no {candidates[0]} or {candidates[1]}", lineno, fix=f"create {candidates[0]} or remove the import"))
    return out


def lint_xml_references(root: str, files: Any, changed: Iterable[str]) -> list[dict[str, Any]]:
    """Local xmlid closure. Only parses the module's other XML when a changed file has refs to check."""
    prefix = f"{root}/" if root else ""
    module = os.path.basename(root) if root else ""
    todo: list[tuple[str, str, int | None]] = []
    for path in changed:
        if not path.endswith(".xml") or not path.startswith(prefix):
            continue
        text = files.get(path)
        if text is None:
            continue
        try:
            tree = _xml_parse(text)
        except Exception:  # noqa: BLE001 - reported by lint_xml
            continue
        for el in tree.iter():
            if not isinstance(el.tag, str):
                continue
            line = getattr(el, "sourceline", None)
            if el.tag == "field" and el.get("ref") and el.get("name") in {"inherit_id", "view_id", "res_model_id"}:
                todo.append((path, el.get("ref"), line))
            elif el.tag == "menuitem" and el.get("action"):
                todo.append((path, el.get("action"), line))
    if not todo:
        return []
    ids: set[str] = set()
    for path in sorted(p for p in files.keys() if p.endswith(".xml") and p.startswith(prefix)):
        text = files.get(path)
        if text is None:
            continue
        try:
            tree = _xml_parse(text)
        except Exception:  # noqa: BLE001
            continue
        for el in tree.iter():
            if isinstance(el.tag, str) and el.tag in {"record", "template", "menuitem"} and el.get("id"):
                ids.add(el.get("id"))
    out = []
    for path, ref, line in todo:
        if "." in ref:
            head, rest = ref.split(".", 1)
            if head != module:
                continue  # other module: only verifiable against installed source
            ref = rest
        if ref not in ids and not ref.startswith("model_"):
            out.append(finding("error", path, "xmlid_unresolved", f"reference '{ref}' is not defined in this module", line, fix="define it, or use '<module>.<xmlid>' for another module"))
    return out


def lint_workspace(files: Any, changed: Iterable[str], deleted: Iterable[str] = (), roots: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Lint changed files plus module-level closure for every module touched by an edit OR a deletion."""
    changed = [p for p in changed if files.get(p) is not None]
    deleted = list(deleted)
    out: list[dict[str, Any]] = []
    for path in changed:
        out.extend(lint_file(path, files[path]))
    all_roots = list(roots) if roots is not None else module_roots(files.keys())
    touched = {r for r in all_roots if any((p.startswith(r + "/") or p == r) if r else True for p in [*changed, *deleted])}
    for root in sorted(touched):
        out.extend(lint_module_structure(root, files))
        under = [p for p in changed if (not root or p.startswith(root + "/"))]
        out.extend(lint_xml_references(root, files, under))
        out.extend(lint_assets(root, files, under))
    return out


def has_errors(findings: Iterable[dict[str, Any]]) -> bool:
    return any(f.get("severity") == "error" for f in findings)
