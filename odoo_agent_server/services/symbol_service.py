"""Look up Odoo models and fields in the upstream community source for the client's series.

Answers "does ``sale.order`` exist, which module defines it, and what type is ``partner_id``?" from
``odoo/odoo`` at the matching branch, fetched through the GitHub API (no local copy, Render-safe) and
cached in memory. Only modules in the working module's ``depends`` closure are searched, mirroring what
Odoo itself would have loaded. Enterprise modules are private and are NOT covered.
"""
from __future__ import annotations

import ast
import difflib
import threading
from collections import OrderedDict
from typing import Any

from . import outline_service
from .github_commit_service import GitHubRepo

MAX_MODULES = 30
MAX_FILES_PER_MODULE = 80
MAX_DEPTH = 4

import time

_LOCK = threading.Lock()
_HEADS: dict[tuple[str, str, str], tuple[float, dict[str, str]]] = {}
HEAD_TTL = 600.0
_CACHE: "OrderedDict[tuple[str, str, str, str], dict[str, Any]]" = OrderedDict()
_CACHE_MAX = 120


def _module_dir(module: str) -> str:
    return "odoo/addons/base" if module == "base" else f"addons/{module}"


class CoreSource:
    def __init__(self, repo: GitHubRepo, branch: str) -> None:
        self.repo = repo
        self.branch = branch
        self._head: dict[str, str] | None = None

    @property
    def head(self) -> dict[str, str]:
        if self._head is None:
            key = (self.repo.owner, self.repo.repo, self.branch)
            cached = _HEADS.get(key)
            if cached and time.time() - cached[0] < HEAD_TTL:
                self._head = cached[1]
            else:
                self._head = self.repo.head(self.branch)
                _HEADS[key] = (time.time(), self._head)
        return self._head

    def module(self, name: str) -> dict[str, Any]:
        """{depends: [...], classes: [outline classes with _fields]}; empty record when the module is not in core."""
        key = (self.repo.owner, self.repo.repo, self.branch, name)
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None:
                _CACHE.move_to_end(key)
                return hit
        record = self._load(name)
        with _LOCK:
            _CACHE[key] = record
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        return record

    def _load(self, name: str) -> dict[str, Any]:
        base = _module_dir(name)
        top = self.repo.tree_at(self.head["tree"], base)
        if top is None:
            return {"found": False, "depends": [], "classes": []}
        depends: list[str] = []
        manifest = top.get("__manifest__.py")
        if manifest:
            try:
                data = ast.literal_eval(self.repo.blob_text(manifest["sha"]).strip())
                depends = [d for d in data.get("depends", []) if isinstance(d, str)]
            except Exception:  # noqa: BLE001
                depends = []
        models = self.repo.tree_at(self.head["tree"], f"{base}/models") or {}
        py = {n: v for n, v in models.items() if v["type"] == "blob" and n.endswith(".py") and n != "__init__.py"}
        names = sorted(py)[:MAX_FILES_PER_MODULE]
        texts = self.repo.blobs_text({n: py[n]["sha"] for n in names})
        classes: list[dict[str, Any]] = []
        for fname in names:
            out = outline_service.outline_python(f"{base}/models/{fname}", texts[fname])
            for cls in out.get("classes", []):
                cls["module"] = name
                classes.append(cls)
        return {"found": True, "depends": depends, "classes": classes}


def depends_closure(core: CoreSource, roots: list[str]) -> tuple[list[str], list[str]]:
    """Breadth-first closure of module dependencies; returns (modules, skipped_not_in_core)."""
    order: list[str] = []
    skipped: list[str] = []
    queue = [(m, 0) for m in roots]
    seen = set()
    while queue and len(order) < MAX_MODULES:
        name, depth = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        record = core.module(name)
        if not record["found"]:
            skipped.append(name)
            continue
        order.append(name)
        if depth < MAX_DEPTH:
            queue.extend((d, depth + 1) for d in record["depends"] if d not in seen)
    return order, skipped


def lookup(core: CoreSource, local_classes: list[dict[str, Any]], depends: list[str], model: str, field: str | None = None) -> dict[str, Any]:
    modules, skipped = depends_closure(core, list(dict.fromkeys(["base", *depends])))
    defs: list[dict[str, Any]] = []
    all_models: set[str] = set()
    for cls in local_classes:
        cls = {**cls, "module": "(this module)"}
        all_models.add(cls["model"])
        if cls["model"] == model:
            defs.append(cls)
    for name in modules:
        for cls in core.module(name)["classes"]:
            all_models.add(cls["model"])
            if cls["model"] == model:
                defs.append(cls)
    out: dict[str, Any] = {
        "status": "ok",
        "model": model,
        "found": bool(defs),
        "source": f"{core.repo.owner}/{core.repo.repo}@{core.branch}",
        "searched_modules": len(modules),
        "not_in_core": skipped,
        "coverage": "community modules in the depends closure only; enterprise and non-listed modules are not checked",
    }
    if not defs:
        out["did_you_mean"] = difflib.get_close_matches(model, sorted(all_models), n=5, cutoff=0.6)
        out["hint"] = "no loaded module defines this model: check the spelling or add the defining module to 'depends'"
        return out
    out["defined_in"] = [{"module": d["module"], "file": d["file"], "line": d["line"], "kind": d["kind"], "class": d["class"]} for d in defs]
    all_fields: dict[str, list[dict[str, Any]]] = {}
    for d in defs:
        for f in d.get("_fields", []):
            all_fields.setdefault(f["name"], []).append({**f, "module": d["module"], "file": d["file"]})
    if field is None:
        out["field_count"] = len(all_fields)
        out["fields"] = ", ".join(sorted(all_fields)[:200])
        return out
    matches = all_fields.get(field, [])
    out["field"] = field
    out["field_found"] = bool(matches)
    if matches:
        out["field_defs"] = [{k: v for k, v in m.items() if k in {"module", "file", "line", "type", "comodel", "compute", "related", "inverse_name", "options", "required", "store"}} for m in matches]
    else:
        out["did_you_mean"] = difflib.get_close_matches(field, sorted(all_fields), n=6, cutoff=0.5)
        out["hint"] = "field not defined by any searched module (inherited mixins such as mail.thread fields are defined in their own modules)"
    return out
