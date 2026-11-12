"""Phase 9: bounded transitive and reverse addon dependency intelligence.

Analyzes sibling addons in the same GitHub workspace repository. It never
imports addon code, executes it, or contacts the Odoo database.
"""
from __future__ import annotations

import ast
from collections import deque
from pathlib import PurePosixPath
from typing import Any


class ReaDependencyService:
    MAX_MODULES = 100
    MAX_FILES = 240
    MAX_TOTAL_CHARS = 2_000_000
    MAX_FILE_BYTES = 256_000
    SKIP_PARTS = {".git", ".mcp-wip", "__pycache__", "static", "tests", "node_modules", "migrations"}

    @staticmethod
    def _manifest(source: str) -> dict[str, Any] | None:
        try:
            value = ast.literal_eval(source)
        except (SyntaxError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _module_root(path: str) -> str:
        parent = PurePosixPath(path).parent
        return "" if str(parent) == "." else str(parent)

    @staticmethod
    def _parse_models(source: str, path: str, module: str, tree: ast.AST | None = None) -> list[dict[str, Any]]:
        if tree is None:
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError:
                return []
        found: list[dict[str, Any]] = []
        for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
            own: list[str] = []
            inherits: list[str] = []
            delegated: list[str] = []
            for item in cls.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                value = item.value
                if "_name" in names and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    own.append(value.value)
                elif "_inherit" in names:
                    values = [value] if not isinstance(value, (ast.List, ast.Tuple, ast.Set)) else value.elts
                    inherits.extend(v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                elif "_inherits" in names and isinstance(value, ast.Dict):
                    delegated.extend(k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
            models = list(dict.fromkeys(own or inherits))
            if not models and not delegated:
                continue
            fields: list[dict[str, Any]] = []
            for item in cls.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                value = item.value
                if not isinstance(value, ast.Call):
                    continue
                try:
                    field_type = ast.unparse(value.func).split(".")[-1]
                except Exception:  # noqa: BLE001
                    continue
                if field_type not in {"Many2one", "One2many", "Many2many"}:
                    continue
                comodel = None
                if value.args and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
                    comodel = value.args[0].value
                for kw in value.keywords:
                    if kw.arg in {"comodel", "comodel_name"} and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        comodel = kw.value.value
                for target in targets:
                    if isinstance(target, ast.Name):
                        for model in models:
                            fields.append({"model": model, "name": target.id, "type": field_type, "comodel": comodel, "line": item.lineno})
            for model in models:
                found.append({
                    "module": module, "model": model, "role": "defines" if own else "extends",
                    "inherits": inherits if own else [x for x in inherits if x != model],
                    "delegates": delegated, "fields": [f for f in fields if f["model"] == model],
                    "path": path, "line": cls.lineno,
                })
        return found

    def analyze(
        self, *, workspace: Any, files: dict[str, str], module_name: str,
        max_modules: int = 50, max_files: int = 160, max_depth: int = 8,
    ) -> dict[str, Any]:
        max_modules = max(5, min(int(max_modules), self.MAX_MODULES))
        max_files = max(10, min(int(max_files), self.MAX_FILES))
        max_depth = max(1, min(int(max_depth), 12))
        tree = workspace.tree
        manifest_paths = sorted(
            path for path in tree
            if path.endswith("/__manifest__.py") or path == "__manifest__.py"
            if not any(part in self.SKIP_PARTS for part in PurePosixPath(path).parts)
        )
        target_root = workspace.root
        target_manifest_path = f"{target_root}/__manifest__.py" if target_root else "__manifest__.py"
        if target_manifest_path not in manifest_paths:
            raise ValueError("target addon manifest is missing from the GitHub workspace tree")
        candidate_limit = min(len(manifest_paths), max_modules * 2 + 20, 220)
        selected_manifest_paths = manifest_paths[:candidate_limit]
        if target_manifest_path not in selected_manifest_paths:
            selected_manifest_paths = [target_manifest_path] + selected_manifest_paths[:candidate_limit - 1]
        manifest_texts: dict[str, str] = {}
        for path, source in files.items():
            manifest_texts[f"{target_root}/{path}" if target_root else path] = source
        if target_manifest_path in workspace.overlay:
            overlay_manifest = workspace.overlay[target_manifest_path]
            if isinstance(overlay_manifest, str):
                manifest_texts[target_manifest_path] = overlay_manifest
            elif overlay_manifest is None:
                raise ValueError("target addon manifest was deleted in the current WIP overlay")
        manifest_bytes = 0
        fetch: dict[str, str] = {}
        manifest_omitted: list[str] = []
        for path in selected_manifest_paths:
            if path in manifest_texts:
                continue
            size = int(tree[path].get("size", 0))
            if size > 64_000 or manifest_bytes + size > 1_500_000:
                manifest_omitted.append(path)
                continue
            fetch[path] = tree[path]["sha"]
            manifest_bytes += size
        if fetch:
            manifest_texts.update(workspace.repo.blobs_text(fetch))
        manifests: dict[str, dict[str, Any]] = {}
        module_paths: dict[str, list[str]] = {}
        malformed: list[str] = []
        for path in selected_manifest_paths:
            source = manifest_texts.get(path)
            if source is None:
                if path not in manifest_omitted:
                    malformed.append(path)
                continue
            manifest = self._manifest(source)
            if manifest is None:
                malformed.append(path)
                continue
            root = self._module_root(path)
            name = PurePosixPath(root).name if root else workspace.module
            manifests[root] = manifest
            module_paths.setdefault(name, []).append(root)
        if target_root not in manifests:
            raise ValueError("could not parse target addon manifest")
        target_name = workspace.module or module_name
        if target_name not in module_paths or target_root not in module_paths[target_name]:
            target_name = PurePosixPath(target_root).name

        deps_by_root: dict[str, list[str]] = {}
        root_by_name = {name: roots[0] for name, roots in module_paths.items() if len(roots) == 1}
        ambiguous = sorted(name for name, roots in module_paths.items() if len(roots) > 1)
        for root, manifest in manifests.items():
            raw_dependencies = manifest.get("depends", [])
            if not isinstance(raw_dependencies, (list, tuple, set)):
                raw_dependencies = []
            deps_by_root[root] = list(dict.fromkeys(
                dep for dep in raw_dependencies
                if isinstance(dep, str) and dep.strip()
            ))

        unresolved: set[str] = set()
        ambiguous_deps: set[str] = set()
        if target_name in ambiguous:
            ambiguous_deps.add(target_name)
        forward_depth: dict[str, int] = {target_root: 0}
        forward_depth_hit = False
        forward_truncated = False
        queue = deque([target_root])
        while queue and len(forward_depth) < max_modules:
            root = queue.popleft()
            depth = forward_depth[root]
            if depth >= max_depth:
                forward_depth_hit = forward_depth_hit or any(
                    root_by_name.get(dep) not in forward_depth
                    for dep in deps_by_root.get(root, []) if root_by_name.get(dep)
                )
                continue
            for dep in deps_by_root.get(root, []):
                if dep in ambiguous:
                    ambiguous_deps.add(dep)
                    continue
                dep_root = root_by_name.get(dep)
                if dep_root is None:
                    unresolved.add(dep)
                    continue
                if dep_root not in forward_depth:
                    if len(forward_depth) >= max_modules:
                        forward_truncated = True
                        break
                    forward_depth[dep_root] = depth + 1
                    queue.append(dep_root)
        forward_truncated = forward_truncated or bool(queue)

        reverse_by_root: dict[str, list[str]] = {}
        for root, deps in deps_by_root.items():
            for dep in deps:
                dep_root = root_by_name.get(dep)
                if dep_root:
                    reverse_by_root.setdefault(dep_root, []).append(root)
        reverse_depth: dict[str, int] = {target_root: 0}
        reverse_depth_hit = False
        reverse_truncated = False
        queue = deque([target_root])
        while queue and len(reverse_depth) < max_modules:
            root = queue.popleft()
            depth = reverse_depth[root]
            if depth >= max_depth:
                reverse_depth_hit = reverse_depth_hit or any(
                    dependent not in reverse_depth for dependent in reverse_by_root.get(root, [])
                )
                continue
            for dependent in sorted(reverse_by_root.get(root, [])):
                if dependent not in reverse_depth:
                    if len(reverse_depth) >= max_modules:
                        reverse_truncated = True
                        break
                    reverse_depth[dependent] = depth + 1
                    queue.append(dependent)
        reverse_truncated = reverse_truncated or bool(queue)

        included_roots = set(forward_depth) | set(reverse_depth)
        included_roots.add(target_root)
        combined_module_truncated = len(included_roots) > max_modules
        module_limit_hit = (
            forward_truncated or reverse_truncated or forward_depth_hit or reverse_depth_hit
            or combined_module_truncated or len(manifest_paths) > candidate_limit
        )
        included_roots = set(sorted(
            included_roots,
            key=lambda root: (
                root != target_root,
                min(forward_depth.get(root, 999), reverse_depth.get(root, 999)),
                root,
            ),
        )[:max_modules])
        forward_depth = {k: v for k, v in forward_depth.items() if k in included_roots}
        reverse_depth = {k: v for k, v in reverse_depth.items() if k in included_roots}

        dependency_edges = []
        for root in sorted(included_roots):
            source_name = PurePosixPath(root).name if root else workspace.module
            for dep in deps_by_root.get(root, []):
                dep_root = root_by_name.get(dep)
                if dep_root in included_roots:
                    dependency_edges.append({"source": source_name, "target": dep, "kind": "depends_on",
                                             "evidence": {"path": f"{root}/__manifest__.py" if root else "__manifest__.py"}})
        cycle_nodes: set[str] = set()
        visiting: list[str] = []
        visited: set[str] = set()
        adjacency = {
            (PurePosixPath(root).name if root else workspace.module): [
                dep for dep in deps_by_root.get(root, [])
                if dep in root_by_name and root_by_name[dep] in included_roots
            ]
            for root in included_roots
        }
        cycles: list[list[str]] = []
        def visit(name: str) -> None:
            if name in visiting:
                cycle = visiting[visiting.index(name):] + [name]
                if cycle not in cycles:
                    cycles.append(cycle)
                    cycle_nodes.update(cycle)
                return
            if name in visited:
                return
            visiting.append(name)
            for dep in adjacency.get(name, []):
                visit(dep)
            visiting.pop()
            visited.add(name)
        for name in sorted(adjacency):
            visit(name)

        candidates: list[tuple[int, str, str, dict[str, str]]] = []
        target_prefix = f"{target_root}/" if target_root else ""
        for root in included_roots:
            prefix = f"{root}/" if root else ""
            module_files = {
                path: meta for path, meta in tree.items()
                if path.startswith(prefix) and path.endswith(".py")
            }
            if root == target_root:
                for path, source in workspace.overlay.items():
                    if not path.startswith(target_prefix) or not path.endswith(".py"):
                        continue
                    if source is None:
                        module_files.pop(path, None)
                    else:
                        module_files[path] = {
                            "sha": tree.get(path, {}).get("sha", ""),
                            "size": str(len(source.encode("utf-8"))),
                        }
            for path, meta in module_files.items():
                parts = PurePosixPath(path).parts
                if any(part in self.SKIP_PARTS for part in parts):
                    continue
                priority = 0 if "models" in parts else 1 if any(p in {"wizard", "wizards", "controllers"} for p in parts) else 2
                candidates.append((priority, path, PurePosixPath(root).name if root else workspace.module, meta))
        candidates.sort(key=lambda row: (row[0], row[1]))
        selected_files = candidates[:max_files]
        omitted_files = [row[1] for row in candidates[max_files:]]
        texts = {f"{target_root}/{path}" if target_root else path: source for path, source in files.items()}
        for path, source in workspace.overlay.items():
            if not path.startswith(target_prefix):
                continue
            if source is None:
                texts.pop(path, None)
            else:
                texts[path] = source
        fetch_files: dict[str, str] = {}
        fetch_bytes = sum(
            len(text.encode("utf-8")) for _priority, path, _module, _meta in selected_files
            if path in texts
        )
        for _priority, path, _module, meta in selected_files:
            if path in texts:
                continue
            size = int(meta.get("size", 0))
            if size > self.MAX_FILE_BYTES or fetch_bytes + size > self.MAX_TOTAL_CHARS:
                omitted_files.append(path)
                continue
            fetch_files[path] = meta["sha"]
            fetch_bytes += size
        if fetch_files:
            texts.update(workspace.repo.blobs_text(fetch_files))
        source_chars = 0
        files_scanned = 0
        model_rows: list[dict[str, Any]] = []
        unreadable: list[str] = []
        parse_warnings: list[dict[str, str]] = []
        for _priority, path, module, _meta in selected_files:
            source = texts.get(path)
            if source is None:
                unreadable.append(path)
                continue
            if len(source.encode("utf-8")) > self.MAX_FILE_BYTES:
                omitted_files.append(path)
                continue
            if source_chars + len(source) > self.MAX_TOTAL_CHARS:
                omitted_files.append(path)
                continue
            source_chars += len(source)
            files_scanned += 1
            try:
                parsed_tree = ast.parse(source, filename=path)
            except SyntaxError as exc:
                parse_warnings.append({"code": "PYTHON_PARSE_ERROR", "path": path, "message": str(exc)[:200]})
                continue
            model_rows.extend(self._parse_models(source, path, module, tree=parsed_tree))

        model_modules: dict[str, set[str]] = {}
        for row in model_rows:
            model_modules.setdefault(row["model"], set()).add(row["module"])
        cross_links: list[dict[str, Any]] = []
        for row in model_rows:
            for parent in row["inherits"] + row["delegates"]:
                owners = sorted(model_modules.get(parent, set()) - {row["module"]})
                for owner in owners:
                    cross_links.append({"source_module": row["module"], "target_module": owner,
                                        "kind": "extends_model" if parent in row["inherits"] else "delegates_model",
                                        "model": parent, "evidence": {"path": row["path"], "line": row["line"]}})
            for field in row["fields"]:
                if field["comodel"]:
                    owners = sorted(model_modules.get(field["comodel"], set()) - {row["module"]})
                    for owner in owners:
                        cross_links.append({"source_module": row["module"], "target_module": owner,
                                            "kind": "relates_to_model", "model": field["model"],
                                            "field": field["name"], "comodel": field["comodel"],
                                            "evidence": {"path": row["path"], "line": field["line"]}})
        unique_links = {
            (x["source_module"], x["target_module"], x["kind"], x.get("model"), x.get("field")): x
            for x in cross_links
        }
        forward = [
            {"module": PurePosixPath(root).name if root else workspace.module, "depth": depth}
            for root, depth in sorted(forward_depth.items(), key=lambda x: (x[1], x[0])) if root != target_root
        ]
        reverse = [
            {"module": PurePosixPath(root).name if root else workspace.module, "depth": depth}
            for root, depth in sorted(reverse_depth.items(), key=lambda x: (x[1], x[0])) if root != target_root
        ]
        return {
            "status": "analyzed",
            "module_name": target_name,
            "repository": {"owner": workspace.repo.owner, "name": workspace.repo.repo, "branch": workspace.branch,
                           "base_commit": workspace.base_commit},
            "transitive_dependencies": forward,
            "reverse_dependents": reverse,
            "dependency_edges": dependency_edges[:500],
            "dependency_cycles": cycles[:30],
            "cycle_modules": sorted(cycle_nodes),
            "unresolved_dependencies": sorted(unresolved)[:100],
            "ambiguous_dependencies": sorted(ambiguous_deps)[:100],
            "cross_module_relationships": list(unique_links.values())[:500],
            "model_ownership": [
                {"model": row["model"], "module": row["module"], "role": row["role"],
                 "path": row["path"], "line": row["line"]}
                for row in model_rows[:500]
            ],
            "coverage": {
                "repository_addons": len(manifests),
                "manifest_files_omitted": len(manifest_omitted),
                "manifest_omitted_paths": manifest_omitted[:100],
                "manifest_bytes_fetched": manifest_bytes,
                "dependency_depth_truncated": forward_depth_hit or reverse_depth_hit,
                "module_count_truncated": forward_truncated or reverse_truncated or combined_module_truncated,
                "candidate_manifests": len(manifest_paths),
                "manifests_scanned": len(manifests),
                "included_modules": len(included_roots),
                "python_files_scanned": files_scanned,
                "python_files_omitted": len(set(omitted_files)),
                "omitted_file_paths": sorted(set(omitted_files))[:100],
                "unreadable_files": unreadable[:100],
                "source_chars": source_chars,
                "complete": not module_limit_hit and not malformed and not manifest_omitted and not ambiguous_deps and not omitted_files and not unreadable and not parse_warnings,
                "manifest_scan_truncated": len(manifest_paths) > candidate_limit,
            },
            "warnings": (
                [{"code": "MANIFEST_UNREADABLE", "path": p} for p in malformed[:50]]
                + parse_warnings[:50]
            ),
            "limitations": [
                "Dependency resolution covers addons visible in this GitHub repository; Odoo core, Enterprise, and external repositories are reported as unresolved.",
                "Model and field relationships are inferred statically from Python AST; dynamic declarations and runtime registry state are not covered.",
                "Only the target workspace's WIP overlay is included; sibling addons use the selected branch snapshot.",
            ],
        }
