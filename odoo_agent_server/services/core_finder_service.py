"""V14+ — Self-Learning Agent: Core File Discovery + Adaptive Scope Advisor.

Mandatory dual source read:
  Every coding task MUST read the custom module from the active checkout first,
  then read only the relevant installed Odoo/Odoo.sh source files. The scope
  advisor decides how deeply to read based on what is found (L0 → L4 adaptive
  levels), but it does not duplicate the current custom module from
  /home/odoo/src/user.

Search priority for installed-source reads:
  1. /home/odoo/src/odoo/addons (priority 1 — community/core addons)
  2. /home/odoo/src/enterprise (priority 2 — fallback for enterprise-specific addons)
  3. /home/odoo/src/user       (priority 3 — fallback for other related custom addons)

Adaptive scope levels:
  L0 (base)     — Custom module already read from checkout; no extra source read
  L1 (inherit)  — _inherit chain detected → also read parent model
  L2 (mixin)    — Abstract model / mixin detected → read all mixins (mail.thread etc.)
  L3 (computed) — ir.model / computed fields → full module models/ dir scan
  L4 (cross)    — Cross-module dependency → related module core also read
"""
from __future__ import annotations

import ast
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..schemas import ListModuleFilesResult  # noqa: F401

from . import core_sync_service
from .client_resolver_service import ClientResolverService
from .module_path_service import is_ignored_source_path
from ..schemas import (
    CoreFileMatch,
    FindCoreFileResult,
    FindSimilarViewResult,
    SimilarViewMatch,
    ScopeAdviceResult,
    ScopeLevel,
    ScopeReadInstruction,
)

# ---------------------------------------------------------------------------
# Default search roots ordered by priority
# ---------------------------------------------------------------------------
_DEFAULT_ROOTS: list[tuple[int, str]] = [
    (1, "/home/odoo/src/odoo/addons"),
    (2, "/home/odoo/src/enterprise"),
    (3, "/home/odoo/src/user"),
]

# ---------------------------------------------------------------------------
# file_type -> sub-directory hints (best-effort speedup; fallback to full scan)
# ---------------------------------------------------------------------------
_FILETYPE_SUBDIRS: dict[str, list[str]] = {
    "model":      ["models"],
    "view":       ["views"],
    "wizard":     ["wizard", "wizards"],
    "controller": ["controllers"],
    "report":     ["report", "reports"],
    "data":       ["data", "demo"],
    "security":   ["security"],
    "static":     ["static"],
    "manifest":   ["."],
    "any":        [],
}

# ---------------------------------------------------------------------------
# view_type -> XML attribute / filename patterns
# ---------------------------------------------------------------------------
_VIEWTYPE_PATTERNS: dict[str, list[re.Pattern]] = {
    "form":     [re.compile(r'type=["\']form["\']'), re.compile(r'_form')],
    "tree":     [re.compile(r'type=["\']tree["\']'), re.compile(r'_tree')],
    "list":     [re.compile(r'type=["\']list["\']'), re.compile(r'_list'), re.compile(r'_tree')],
    "kanban":   [re.compile(r'type=["\']kanban["\']'), re.compile(r'_kanban')],
    "search":   [re.compile(r'type=["\']search["\']'), re.compile(r'_search')],
    "pivot":    [re.compile(r'type=["\']pivot["\']'), re.compile(r'_pivot')],
    "graph":    [re.compile(r'type=["\']graph["\']'), re.compile(r'_graph')],
    "calendar": [re.compile(r'type=["\']calendar["\']'), re.compile(r'_calendar')],
    "activity": [re.compile(r'type=["\']activity["\']'), re.compile(r'_activity')],
    "any":      [],
}

# ---------------------------------------------------------------------------
# Known Odoo mixins / abstract models to probe at L2
# ---------------------------------------------------------------------------
_KNOWN_MIXINS: list[tuple[str, str, str]] = [
    ("mail",    "model", "thread"),
    ("mail",    "model", "activity"),
    ("mail",    "model", "alias"),
    ("portal",  "model", "mixin"),
    ("rating",  "model", "mixin"),
    ("website", "model", "published"),
]

_MIXIN_DETECT_RE = re.compile(
    r'_inherit\s*=\s*[\[\(]?["\'](?:mail\.thread|mail\.activity|portal\.mixin|'
    r'rating\.mixin|website\.published|base\.setup|mail\.alias)["\']',
    re.IGNORECASE,
)
_ABSTRACT_DETECT_RE = re.compile(r'_abstract\s*=\s*True|class\s+\w+.*AbstractModel')
_INHERIT_DETECT_RE  = re.compile(r'_inherit\s*=\s*([^\n#]+)')
_MODEL_LITERAL_RE   = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.]+)["\']')
_COMPUTED_DETECT_RE = re.compile(r'fields\.(Many2many|One2many|Many2one)\s*\(|compute\s*=')
_IRMODEL_DETECT_RE  = re.compile(r'ir\.model|env\[.+\]\.fields_get\(')
_CROSSMOD_DETECT_RE = re.compile(
    r'(?:comodel_name\s*=\s*["\']([^"\']+)["\']|fields\.(?:Many2many|One2many|Many2one)\s*\(\s*["\']([^"\']+)["\'])'
)
_XML_REF_RE = re.compile(r'ref=["\']([A-Za-z0-9_]+)\.([A-Za-z0-9_.-]+)["\']')
_XML_INHERIT_FIELD_RE = re.compile(
    r'<field[^>]+name=["\']inherit_id["\'][^>]+ref=["\']([A-Za-z0-9_]+)\.([A-Za-z0-9_.-]+)["\']',
    re.IGNORECASE,
)


def _extract_model_literals(raw: str) -> list[str]:
    """Extract Odoo model strings from scalar or list/tuple assignments."""
    return [m.group(1) for m in _MODEL_LITERAL_RE.finditer(raw)]


def _model_to_filename_hints(model: str) -> list[str]:
    """Convert 'sale.order' to filename hints like 'sale_order', 'order'."""
    parts = model.replace(".", "_")
    last = model.rsplit(".", 1)[-1]
    return [parts, last]


def _focused_python_scope_content(content: str, identifiers: Sequence[str]) -> tuple[str, bool]:
    """Return only exact Python source units relevant to resolved identifiers.

    Large Odoo model files commonly contain several models and dozens of
    unrelated relational fields. Scanning the whole file makes one field edit
    look like a module-wide L3/L4 change. Keep the containing model identity
    (``_name``/``_inherit``/``_inherits``) and the exact matched field/method.
    Fall back to the complete file whenever no exact AST match is available.
    """
    clean = list(dict.fromkeys(
        str(value or "").strip()
        for value in identifiers
        if str(value or "").strip()
    ))[:20]
    if not clean:
        return content, False
    simple = {
        value
        for value in clean
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    }
    dotted = {
        value
        for value in clean
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", value)
    }
    if not simple and not dotted:
        return content, False
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return content, False
    lines = content.splitlines(keepends=True)
    snippets: list[str] = []
    seen: set[tuple[int, int]] = set()

    def source(node: ast.AST) -> str:
        start = max(1, int(getattr(node, "lineno", 1)))
        decorators = getattr(node, "decorator_list", []) or []
        if decorators:
            start = min(start, *(int(getattr(item, "lineno", start)) for item in decorators))
        end = max(start, int(getattr(node, "end_lineno", start)))
        key = (start, end)
        if key in seen:
            return ""
        seen.add(key)
        return "".join(lines[start - 1:end])

    def assigned_names(node: ast.AST) -> set[str]:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        return {target.id for target in targets if isinstance(target, ast.Name)}

    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        matched_units: list[ast.AST] = []
        identity_units: list[ast.AST] = []
        model_identity_match = False
        for node in class_node.body:
            names = assigned_names(node)
            if names & {"_name", "_inherit", "_inherits"}:
                identity_units.append(node)
                for value in ast.walk(node):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        if value.value in dotted:
                            model_identity_match = True
            if names & simple:
                matched_units.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in simple:
                matched_units.append(node)
        if not matched_units and not model_identity_match and class_node.name not in simple:
            continue
        header_end = int(getattr(class_node, "lineno", 1))
        snippets.append("".join(lines[header_end - 1:header_end]))
        for node in identity_units:
            value = source(node)
            if value:
                snippets.append(value)
        if matched_units:
            for node in matched_units:
                value = source(node)
                if value:
                    snippets.append(value)
        else:
            value = source(class_node)
            if value:
                snippets.append(value)

    focused = "\n".join(item.rstrip("\r\n") for item in snippets if item).strip()
    return (focused + "\n", True) if focused else (content, False)


class CoreFinderService:
    """Find Odoo core source files across the /home/odoo/src tree.

    Now includes scope_advice() for adaptive mandatory dual-read planning.
    """

    # ------------------------------------------------------------------
    # Public API -- find_core_file
    # ------------------------------------------------------------------

    def find_core_file(
        self,
        module_name: str,
        file_type: str,
        keyword: str,
        search_roots: Sequence[str] | None = None,
        client_name: str | None = None,
    ) -> FindCoreFileResult:
        """Locate files matching module+type+keyword across priority roots."""
        roots = self._resolve_roots(search_roots, client_name=client_name)
        subdirs = _FILETYPE_SUBDIRS.get(file_type, [])
        kw_lower = keyword.lower()
        all_matches: list[CoreFileMatch] = []
        content_fallbacks: list[tuple[int, str, Path, Path]] = []

        for priority, root in roots:
            root_path = Path(root)
            module_dir = self._module_dir_with_sync(root_path, root, module_name, client_name=client_name)
            if not module_dir:
                continue

            candidates = self._walk_module(module_dir, subdirs)
            for abs_path in candidates:
                name_lower = abs_path.name.lower()
                if file_type == "manifest":
                    if abs_path.name != "__manifest__.py":
                        continue
                elif not self._extension_matches(abs_path, file_type):
                    continue
                elif kw_lower not in name_lower:
                    content_fallbacks.append((priority, root, root_path, abs_path))
                    continue
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    size = 0
                all_matches.append(CoreFileMatch(
                    path=str(abs_path),
                    relative_path=str(abs_path.relative_to(root_path)),
                    root=root,
                    priority=priority,
                    size=size,
                ))

        # Filename lookup is the fast lane. Only when it finds nothing, use a
        # bounded exact-symbol/content fallback across Community + Enterprise.
        if not all_matches and file_type != "manifest" and kw_lower:
            searched_per_root: dict[str, int] = {}
            for priority, root, root_path, abs_path in content_fallbacks:
                if searched_per_root.get(root, 0) >= 200:
                    continue
                searched_per_root[root] = searched_per_root.get(root, 0) + 1
                if not self._content_symbol_match(abs_path, keyword, file_type):
                    continue
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    size = 0
                all_matches.append(CoreFileMatch(
                    path=str(abs_path),
                    relative_path=str(abs_path.relative_to(root_path)),
                    root=root,
                    priority=priority,
                    size=size,
                ))

        all_matches.sort(key=lambda m: (m.priority, len(m.path)))
        best = all_matches[0].path if all_matches else None

        return FindCoreFileResult(
            module_name=module_name,
            file_type=file_type,
            keyword=keyword,
            matches=all_matches,
            best_match=best,
            searched_roots=[r for _, r in roots],
            total_found=len(all_matches),
        )

    # ------------------------------------------------------------------
    # Public API -- find_similar_view
    # ------------------------------------------------------------------

    def find_similar_view(
        self,
        model: str,
        view_type: str,
        search_roots: Sequence[str] | None = None,
        client_name: str | None = None,
    ) -> FindSimilarViewResult:
        """Locate view XML files for a model+view_type across priority roots."""
        roots = self._resolve_roots(search_roots, client_name=client_name)
        model_hints = _model_to_filename_hints(model)
        module_candidate = model.split(".")[0]
        type_patterns = _VIEWTYPE_PATTERNS.get(view_type, [])
        all_matches: list[SimilarViewMatch] = []

        for priority, root in roots:
            root_path = Path(root)
            module_dir = self._module_dir_with_sync(root_path, root, module_candidate, client_name=client_name)
            search_dirs: list[Path] = []
            if module_dir:
                search_dirs.append(module_dir)
            elif not root_path.exists():
                continue
            else:
                search_dirs.append(root_path)

            for search_dir in search_dirs:
                for current, dirs, files in os.walk(search_dir):
                    dirs[:] = [
                        d for d in dirs
                        if not d.startswith(".")
                        and d not in {"__pycache__", ".git", "node_modules", "static", "tests", "test"}
                    ]
                    for fname in files:
                        if not fname.endswith(".xml"):
                            continue
                        fname_lower = fname.lower()
                        if not any(h in fname_lower for h in model_hints):
                            continue
                        abs_path = Path(current) / fname
                        if view_type != "any":
                            matched_pattern = any(
                                p.search(fname_lower) for p in type_patterns
                            )
                            if not matched_pattern:
                                matched_pattern = self._quick_content_check(abs_path, type_patterns)
                            if not matched_pattern:
                                continue
                        try:
                            size = abs_path.stat().st_size
                        except OSError:
                            size = 0
                        all_matches.append(SimilarViewMatch(
                            path=str(abs_path),
                            relative_path=str(abs_path.relative_to(root_path)),
                            root=root,
                            priority=priority,
                            model_hint=model,
                            size=size,
                        ))

        seen: set[str] = set()
        unique: list[SimilarViewMatch] = []
        for m in sorted(all_matches, key=lambda m: (m.priority, len(m.path))):
            if m.path not in seen:
                seen.add(m.path)
                unique.append(m)

        best = unique[0].path if unique else None
        return FindSimilarViewResult(
            model=model,
            view_type=view_type,
            matches=unique,
            best_match=best,
            searched_roots=[r for _, r in roots],
            total_found=len(unique),
        )

    # ------------------------------------------------------------------
    # Public API -- scope_advice  (NEW -- Pillar 2: Adaptive Scope)
    # ------------------------------------------------------------------

    def scope_advice(
        self,
        custom_module_name: str,
        custom_file_paths: list[str],
        task_description: str,
        search_roots: Sequence[str] | None = None,
        client_name: str | None = None,
        focus_identifiers: Sequence[str] | None = None,
    ) -> ScopeAdviceResult:
        """Analyse custom module files and return adaptive mandatory read instructions.

        The agent MUST call this after repo.read on custom files and BEFORE
        writing any code. It returns an ordered list of ScopeReadInstruction
        objects (read_type + path) specifying everything that must be read.

        Levels (adaptive -- each level adds more reads):
          L0 -- always: custom module manifest (baseline)
          L1 -- _inherit detected: read parent core model(s)
          L2 -- mixin/abstract detected: read all related mixins
          L3 -- computed/relational fields or ir.model: scan full models/ dir
          L4 -- cross-module comodel: read that module's manifest + model
        """
        roots = self._resolve_roots(search_roots, client_name=client_name)
        instructions: list[ScopeReadInstruction] = []
        reached_level = ScopeLevel.L0
        cross_modules: set[str] = set()
        related_modules: set[str] = set()
        related_xmlids: set[tuple[str, str]] = set()
        added_instruction_paths: set[str] = set()

        def add_instruction(item: ScopeReadInstruction) -> None:
            key = item.path
            if key in added_instruction_paths:
                return
            added_instruction_paths.add(key)
            instructions.append(item)

        # Scan provided custom files for manifest dependencies, XML inherit refs,
        # and Python model patterns.  Mandatory dual-read applies to XML/CSS/SCSS
        # edits too, so manifest dependencies are used to find the related core
        # installed-source layer even when no Python file changes.
        inherit_models: list[str] = []
        has_mixin = False
        has_computed_or_relational = False
        has_irmodel = False

        focus_used = False
        clean_focus = list(dict.fromkeys(
            str(value or "").strip()
            for value in (focus_identifiers or [])
            if str(value or "").strip()
        ))[:20]
        for fpath in custom_file_paths:
            content = self._safe_read(fpath, max_bytes=300000)
            if not content:
                continue

            if fpath.endswith(".py") and clean_focus:
                content, applied = _focused_python_scope_content(content, clean_focus)
                focus_used = focus_used or applied

            if fpath.endswith("__manifest__.py") or fpath.endswith("__openerp__.py"):
                for dep in self._extract_manifest_dependencies(content):
                    if dep and dep != custom_module_name:
                        related_modules.add(dep)

            if fpath.endswith(".xml"):
                for module, xmlid in _XML_INHERIT_FIELD_RE.findall(content):
                    if module and module != custom_module_name:
                        related_modules.add(module)
                        related_xmlids.add((module, xmlid))
                for module, xmlid in _XML_REF_RE.findall(content):
                    if module and module != custom_module_name:
                        related_modules.add(module)
                        # Only read target XML files for likely inherited templates/views.
                        if "view" in xmlid or "template" in xmlid or "inherit" in xmlid:
                            related_xmlids.add((module, xmlid))

            if not fpath.endswith(".py"):
                continue

            # L1: detect _inherit
            for m in _INHERIT_DETECT_RE.finditer(content):
                for model_name in _extract_model_literals(m.group(1)):
                    inherit_models.append(model_name)

            # L2: detect mixin / abstract
            if _MIXIN_DETECT_RE.search(content) or _ABSTRACT_DETECT_RE.search(content):
                has_mixin = True

            # L3: detect computed / relational fields or ir.model usage
            if _COMPUTED_DETECT_RE.search(content):
                has_computed_or_relational = True
            if _IRMODEL_DETECT_RE.search(content):
                has_irmodel = True

            # L4: detect cross-module comodel references
            for cm in _CROSSMOD_DETECT_RE.finditer(content):
                comodel = cm.group(1) or cm.group(2) or ""
                mod = comodel.split(".", 1)[0] if "." in comodel else ""
                if mod and mod != custom_module_name:
                    cross_modules.add(mod)
                    related_modules.add(mod)

        # L0/L1 baseline: read related module manifests discovered from the
        # current custom module manifest and XML refs.  Search priority is core
        # /home/odoo/src/odoo/addons first, then enterprise fallback, then user.
        for dep_module in sorted(related_modules):
            result = self.find_core_file(dep_module, "manifest", dep_module, search_roots, client_name=client_name)
            if result.best_match:
                add_instruction(ScopeReadInstruction(
                    level=ScopeLevel.L0,
                    reason=f"Related module '{dep_module}' detected from manifest/XML -- read installed-source manifest before writing",
                    read_type="related_module_manifest",
                    path=result.best_match,
                    mandatory=True,
                ))

        for module, xmlid in sorted(related_xmlids):
            match = self._find_xmlid_file(module, xmlid, roots, client_name=client_name)
            if match:
                add_instruction(ScopeReadInstruction(
                    level=ScopeLevel.L1,
                    reason=f"XML inheritance/reference '{module}.{xmlid}' detected -- read the related core XML before writing xpath/template changes",
                    read_type="related_xml_view",
                    path=match,
                    mandatory=True,
                ))
                reached_level = max(reached_level, ScopeLevel.L1, key=lambda value: value.value)

        # L1: add parent model reads
        if inherit_models:
            reached_level = ScopeLevel.L1
            for model in sorted(set(inherit_models)):
                core_module = model.split(".")[0]
                keyword = model.split(".")[-1] if "." in model else model
                result = self.find_core_file(core_module, "model", keyword, search_roots, client_name=client_name)
                if result.best_match:
                    add_instruction(ScopeReadInstruction(
                        level=ScopeLevel.L1,
                        reason=f"_inherit '{model}' detected -- MUST read parent core model before writing fields",
                        read_type="core_model",
                        path=result.best_match,
                        mandatory=True,
                    ))
                else:
                    add_instruction(ScopeReadInstruction(
                        level=ScopeLevel.L1,
                        reason=f"_inherit '{model}' detected but not found in src roots -- check manually",
                        read_type="core_model_missing",
                        path=f"NOT_FOUND: /home/odoo/src/odoo/addons/{core_module}/models/{keyword}.py",
                        mandatory=False,
                    ))

        # L2: add mixin reads
        if has_mixin:
            reached_level = ScopeLevel.L2
            for mixin_module, mixin_type, mixin_kw in _KNOWN_MIXINS:
                result = self.find_core_file(mixin_module, mixin_type, mixin_kw, search_roots, client_name=client_name)
                if result.best_match:
                    add_instruction(ScopeReadInstruction(
                        level=ScopeLevel.L2,
                        reason=f"Mixin/abstract detected -- read {mixin_module}.{mixin_kw} source for correct method signatures",
                        read_type="core_mixin",
                        path=result.best_match,
                        mandatory=True,
                    ))

        # L3: full models/ dir scan hint
        if has_computed_or_relational or has_irmodel:
            reached_level = ScopeLevel.L3
            target_module = inherit_models[0].split(".")[0] if inherit_models else custom_module_name
            for _, root in roots:
                root_path = Path(root)
                module_dir = self._find_module_dir(root_path, target_module)
                if not module_dir:
                    try:
                        core_sync_service.ensure_module_synced(root, target_module, client_name=client_name)
                    except TypeError:
                        core_sync_service.ensure_module_synced(root, target_module)
                    module_dir = self._find_module_dir(root_path, target_module)
                models_dir = module_dir / "models" if module_dir else root_path / target_module / "models"
                if models_dir.exists():
                    add_instruction(ScopeReadInstruction(
                        level=ScopeLevel.L3,
                        reason=(
                            "Computed/relational fields or ir.model detected -- "
                            "scan full models/ dir to find all field definitions and dependencies"
                        ),
                        read_type="core_models_dir_scan",
                        path=str(models_dir),
                        mandatory=True,
                    ))
                    break

        # L4: cross-module dependencies
        if cross_modules:
            reached_level = ScopeLevel.L4
            for dep_module in sorted(cross_modules):
                result = self.find_core_file(dep_module, "manifest", dep_module, search_roots, client_name=client_name)
                if result.best_match:
                    add_instruction(ScopeReadInstruction(
                        level=ScopeLevel.L4,
                        reason=f"Cross-module comodel ref to '{dep_module}' -- verify exact module technical name",
                        read_type="cross_module_manifest",
                        path=result.best_match,
                        mandatory=True,
                    ))

        return ScopeAdviceResult(
            custom_module=custom_module_name,
            reached_level=reached_level,
            instructions=instructions,
            mandatory_count=sum(1 for i in instructions if i.mandatory),
            total_count=len(instructions),
            focus_applied=focus_used,
            focus_identifiers=clean_focus if focus_used else [],
            warning=(
                "MANDATORY DUAL READ: Execute ALL mandatory instructions before writing code. "
                "Push is BLOCKED until workflow.verify_against_core succeeds."
            ),
        )

    # ------------------------------------------------------------------
    # Public API -- list_module_files (gap-fix)
    # ------------------------------------------------------------------

    def list_module_files(
        self,
        module_name: str,
        subdir: str = "",
        search_roots: Sequence[str] | None = None,
        client_name: str | None = None,
    ) -> "ListModuleFilesResult":
        """List all files in a module directory (or subdir) across priority roots.

        Use this when you don't know the exact filename/keyword yet and need
        to browse what exists before calling find_core_file with a keyword.
        """
        from ..schemas import ListModuleFilesResult, ModuleFileEntry

        roots = self._resolve_roots(search_roots, client_name=client_name)
        for priority, root in roots:
            root_path = Path(root)
            module_dir = self._module_dir_with_sync(root_path, root, module_name, client_name=client_name)
            if not module_dir:
                continue

            search_path = module_dir / subdir if subdir else module_dir
            if not search_path.exists():
                continue

            entries: list[ModuleFileEntry] = []
            if search_path.is_file():
                entries.append(ModuleFileEntry(
                    path=str(search_path),
                    relative_path=str(search_path.relative_to(module_dir)),
                    size=search_path.stat().st_size,
                    ext=search_path.suffix,
                ))
            else:
                for current, dirs, files in os.walk(search_path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
                    for fname in sorted(files):
                        fpath = Path(current) / fname
                        try:
                            entries.append(ModuleFileEntry(
                                path=str(fpath),
                                relative_path=str(fpath.relative_to(module_dir)),
                                size=fpath.stat().st_size,
                                ext=fpath.suffix,
                            ))
                        except OSError:
                            continue

            entries.sort(key=lambda e: e.relative_path)
            return ListModuleFilesResult(
                module_name=module_name,
                subdir=subdir or "(root)",
                found=True,
                module_path=str(module_dir),
                root=root,
                priority=priority,
                files=entries[:100],
                total_found=len(entries),
            )

        # Not found in any root
        return ListModuleFilesResult(
            module_name=module_name,
            subdir=subdir or "(root)",
            found=False,
            files=[],
            total_found=0,
            message=(
                f"Module '{module_name}' not found in any search root "
                f"(checked: {', '.join(r for _, r in roots)}). "
                f"Check module name spelling or pass search_roots explicitly."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_roots(search_roots: Sequence[str] | None, client_name: str | None = None) -> list[tuple[int, str]]:
        if search_roots:
            return [(i + 1, r) for i, r in enumerate(search_roots)]
        resolver = ClientResolverService()
        client = resolver.resolve_client(client_name=client_name, require_complete=False) if client_name else resolver.resolve_client(require_complete=False)
        if client:
            return [(i + 1, root) for i, root in enumerate(resolver.search_roots(client))]
        return _DEFAULT_ROOTS

    def _module_dir_with_sync(self, root_path: Path, root: str, module_name: str, client_name: str | None = None) -> Path | None:
        module_dir = self._find_module_dir(root_path, module_name) if root_path.exists() else None
        if module_dir:
            return module_dir
        try:
            core_sync_service.ensure_module_synced(root, module_name, client_name=client_name)
        except TypeError:
            # Backward compatibility for tests/extensions monkeypatching the old
            # two-argument ensure_module_synced(root, module_name) signature.
            core_sync_service.ensure_module_synced(root, module_name)
        return self._find_module_dir(root_path, module_name) if root_path.exists() else None

    @staticmethod
    def _find_module_dir(root_path: Path, module_name: str) -> Path | None:
        for candidate in (
            root_path / module_name,
            root_path / "addons" / module_name,
            root_path / "odoo" / "addons" / module_name,
        ):
            if candidate.is_dir():
                return candidate
        return None

    @staticmethod
    def _walk_module(module_dir: Path, subdirs: list[str]) -> list[Path]:
        results: list[Path] = []
        if subdirs:
            for sub in subdirs:
                sub_path = module_dir / sub if sub != "." else module_dir
                if sub_path.exists():
                    if sub_path.is_file():
                        results.append(sub_path)
                    else:
                        for current, dirs, files in os.walk(sub_path):
                            current_path = Path(current)
                            dirs[:] = sorted(
                                d for d in dirs
                                if not d.startswith(".")
                                and not is_ignored_source_path((current_path / d).relative_to(module_dir).parts)
                            )
                            for fname in sorted(files):
                                results.append(Path(current) / fname)
            if not results:
                subdirs = []
        if not subdirs:
            for current, dirs, files in os.walk(module_dir):
                current_path = Path(current)
                dirs[:] = sorted(
                    d for d in dirs
                    if not d.startswith(".")
                    and not is_ignored_source_path((current_path / d).relative_to(module_dir).parts)
                )
                for fname in sorted(files):
                    results.append(Path(current) / fname)
        return results

    @staticmethod
    def _extension_matches(path: Path, file_type: str) -> bool:
        ext = path.suffix.lower()
        if file_type in {"model", "wizard", "controller"}:
            return ext == ".py"
        if file_type in {"view", "report", "data"}:
            return ext == ".xml"
        if file_type == "security":
            return ext in {".csv", ".xml"}
        if file_type == "static":
            return ext in {".js", ".css", ".scss", ".xml", ".html", ".png", ".svg"}
        return True

    @staticmethod
    def _content_symbol_match(path: Path, keyword: str, file_type: str) -> bool:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")[:500_000]
        except OSError:
            return False
        escaped = re.escape(str(keyword or "").strip())
        if not escaped:
            return False
        if file_type in {"model", "wizard", "controller"}:
            patterns = (
                rf"\b(?:async\s+def|def|class)\s+{escaped}\b",
                rf"(?m)^\s*{escaped}\s*=\s*fields\.",
                rf"[\"']{escaped}[\"']",
            )
        elif file_type in {"view", "report", "data"}:
            patterns = (
                rf"\b(?:id|name|model|ref|t-name|t-call)=[\"'][^\"']*{escaped}[^\"']*[\"']",
                rf"<field\b[^>]*\bname=[\"']{escaped}[\"']",
            )
        else:
            patterns = (escaped,)
        return any(re.search(pattern, content, flags=re.I) for pattern in patterns)

    @staticmethod
    def _quick_content_check(path: Path, patterns: list[re.Pattern], peek_bytes: int = 4096) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                chunk = fh.read(peek_bytes)
            return any(p.search(chunk) for p in patterns)
        except OSError:
            return False

    @staticmethod
    def _extract_manifest_dependencies(content: str) -> list[str]:
        """Best-effort parse of __manifest__.py depends without executing code."""
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "depends":
                            try:
                                deps = ast.literal_eval(value)
                            except Exception:
                                return []
                            if isinstance(deps, (list, tuple)):
                                return [str(dep) for dep in deps if isinstance(dep, str) and dep]
        except Exception:
            pass
        # Fallback for simple textual manifests.
        match = re.search(r'["\']depends["\']\s*:\s*(\[[^\]]*\])', content, re.S)
        if not match:
            return []
        try:
            deps = ast.literal_eval(match.group(1))
        except Exception:
            return []
        return [str(dep) for dep in deps if isinstance(dep, str) and dep]

    def _find_xmlid_file(
        self,
        module_name: str,
        xmlid: str,
        roots: Sequence[tuple[int, str]],
        client_name: str | None = None,
    ) -> str | None:
        """Find the XML file that defines or references an xmlid in a related module."""
        xmlid_patterns = (
            f'id="{xmlid}"',
            f"id='{xmlid}'",
            f'name="{xmlid}"',
            f"name='{xmlid}'",
            f'ref="{module_name}.{xmlid}"',
            f"ref='{module_name}.{xmlid}'",
        )
        for _priority, root in roots:
            root_path = Path(root)
            module_dir = self._module_dir_with_sync(root_path, root, module_name, client_name=client_name)
            if not module_dir:
                continue
            for current, dirs, files in os.walk(module_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git", "node_modules", "static"}]
                for fname in files:
                    if not fname.endswith(".xml"):
                        continue
                    path = Path(current) / fname
                    content = self._safe_read(str(path), max_bytes=200000)
                    if content and any(pattern in content for pattern in xmlid_patterns):
                        return str(path)
        return None

    @staticmethod
    def _safe_read(path: str, max_bytes: int = 65536) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read(max_bytes)
        except OSError:
            return ""
