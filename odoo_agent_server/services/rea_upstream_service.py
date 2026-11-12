from __future__ import annotations
import ast
import re
from pathlib import Path
from typing import Any
from .core_finder_service import CoreFinderService

class ReaUpstreamService:
    """Compare custom model extensions with installed dependency source."""
    _MAX_FILE_BYTES = 256000

    @staticmethod
    def _strings(node):
        if node is None: return []
        items = node.elts if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else [node]
        return [x.value for x in items if isinstance(x, ast.Constant) and isinstance(x.value, str)]

    @staticmethod
    def _assign(cls, name):
        for item in cls.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets): return item.value
        return None

    @staticmethod
    def _sig(node):
        a=node.args; pos=[x.arg for x in [*a.posonlyargs,*a.args]]
        if pos and pos[0] in {"self","cls"}: pos.pop(0)
        return {"positional":pos,"keyword_only":[x.arg for x in a.kwonlyargs],
                "vararg":a.vararg.arg if a.vararg else None,"kwarg":a.kwarg.arg if a.kwarg else None}

    @staticmethod
    def _field(node):
        if not isinstance(node,ast.Call): return None,None
        fn=node.func; typ=fn.id if isinstance(fn,ast.Name) else fn.attr if isinstance(fn,ast.Attribute) else None
        comodel=node.args[0].value if node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str) else None
        for kw in node.keywords:
            if kw.arg in {"comodel_name","comodel"} and isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str): comodel=kw.value.value
        return typ,comodel

    @classmethod
    def _models(cls, source, path):
        try: tree=ast.parse(source,filename=path)
        except SyntaxError as exc: return [],[{"code":"PYTHON_PARSE_ERROR","path":path,"message":str(exc)[:160]}]
        result=[]
        for node in ast.walk(tree):
            if not isinstance(node,ast.ClassDef): continue
            own=cls._strings(cls._assign(node,"_name")); inh=cls._strings(cls._assign(node,"_inherit"))
            names=list(dict.fromkeys([*own,*inh]))
            if not names: continue
            methods={m.name:cls._sig(m) for m in node.body if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef))}
            fields={}
            for stmt in node.body:
                if isinstance(stmt,(ast.Assign,ast.AnnAssign)):
                    targets=stmt.targets if isinstance(stmt,ast.Assign) else [stmt.target]; spec=cls._field(stmt.value)
                    if spec[0]:
                        for target in targets:
                            if isinstance(target,ast.Name): fields[target.id]=spec
            for name in names: result.append({"model":name,"defined":name in own,"methods":methods,"fields":fields,"path":path,"line":node.lineno})
        return result,[]


    def compare_workspace(
        self, *, files: dict[str, str], module_name: str, workspace: Any,
        max_dependency_files: int = 30, max_total_chars: int = 12000,
    ) -> dict[str, Any]:
        """Compare workspace source with matching Odoo Community source through GitHub.

        The addon is read from the stateless GitHub workspace (including its WIP
        overlay); upstream source is fetched from the matching Odoo series.
        No local Odoo checkout or persistent disk is required.
        """
        manifest_text = files.get("__manifest__.py")
        if not manifest_text:
            raise ValueError("workspace snapshot is missing __manifest__.py")
        try:
            manifest = ast.literal_eval(manifest_text)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"unable to parse addon manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("manifest must contain a dictionary")
        version = str(manifest.get("version") or "")
        match = re.match(r"^(\d{1,2})\.\d+", version)
        if not match:
            return {
                "status": "series_unknown", "module": module_name,
                "message": "Cannot infer Odoo series from the addon manifest; set a valid version such as 18.0.1.0.0.",
                "coverage": {"complete": False, "custom_files_scanned": 0, "dependency_files_scanned": 0},
            }
        series = int(match.group(1))
        all_dependencies = [
            value for value in manifest.get("depends", [])
            if isinstance(value, str) and value.strip()
        ]
        dependencies = all_dependencies[:30]
        dependency_limit_hit = len(all_dependencies) > len(dependencies)
        from .github_commit_service import GitHubRepo

        core_repo = GitHubRepo(
            "odoo", "odoo", workspace.repo.token,
            transport=workspace.repo.transport, sleep=workspace.repo.sleep,
        )
        branch = f"{series}.0"
        head = core_repo.head(branch)
        upstream: dict[str, dict[str, Any]] = {}
        scanned: list[str] = []
        omitted: list[str] = []
        unresolved: list[str] = []
        warnings: list[dict[str, str]] = []
        chars = 0
        budget = max(4000, min(int(max_total_chars), 20000))
        limit = max(5, min(int(max_dependency_files), 60))
        stop = False
        for dependency in dependencies:
            base = "odoo/addons/base" if dependency == "base" else f"addons/{dependency}"
            module_entries = core_repo.tree_at(head["tree"], base)
            module_found = module_entries is not None
            available_folders = {
                name for name, meta in (module_entries or {}).items()
                if meta.get("type") == "tree"
            }
            for folder in ("models", "wizard", "wizards", "controllers"):
                if folder not in available_folders:
                    continue
                entries = core_repo.tree_at(head["tree"], f"{base}/{folder}") or {}
                py_entries = [
                    (name, meta) for name, meta in sorted(entries.items())
                    if meta.get("type") == "blob" and name.endswith(".py") and name != "__init__.py"
                ]
                if py_entries:
                    module_found = True
                for name, meta in py_entries:
                    rel = f"{base}/{folder}/{name}"
                    size = int(meta.get("size") or 0)
                    if size > self._MAX_FILE_BYTES:
                        omitted.append(rel)
                        continue
                    if len(scanned) >= limit or chars >= budget:
                        omitted.append(rel)
                        stop = True
                        break
                    source = core_repo.blobs_text({name: meta["sha"]}).get(name, "")
                    if chars + len(source) > budget:
                        omitted.append(rel)
                        continue
                    chars += len(source)
                    scanned.append(rel)
                    models, errors = self._models(source, rel)
                    warnings.extend(errors)
                    for model in models:
                        upstream.setdefault(model["model"], model)
                if stop:
                    break
            if not module_found:
                unresolved.append(dependency)
            if stop:
                break

        custom: list[dict[str, Any]] = []
        custom_count = 0
        for relative, source in sorted(files.items()):
            if not relative.endswith(".py") or any(part in {"static", "tests", "__pycache__"} for part in Path(relative).parts):
                continue
            custom_count += 1
            models, errors = self._models(source, relative)
            custom.extend(models)
            warnings.extend(errors)

        findings: list[dict[str, Any]] = []
        for item in custom:
            base = upstream.get(item["model"])
            if not base:
                if not item["defined"]:
                    findings.append({
                        "code": "UPSTREAM_MODEL_NOT_FOUND", "severity": "warning",
                        "model": item["model"],
                        "message": "No matching model definition found in the declared dependencies on the selected Community series.",
                        "evidence": {"custom_path": item["path"], "custom_line": item["line"]},
                    })
                continue
            for name, signature in item["methods"].items():
                previous = base["methods"].get(name)
                evidence = {
                    "custom_path": item["path"], "custom_line": item["line"],
                    "upstream_path": base["path"], "upstream_line": base["line"],
                }
                if previous and previous != signature:
                    findings.append({
                        "code": "METHOD_SIGNATURE_DRIFT", "severity": "warning",
                        "model": item["model"], "symbol": name,
                        "message": "Custom override signature differs from the selected Odoo Community definition.",
                        "custom_signature": signature, "upstream_signature": previous, "evidence": evidence,
                    })
                elif previous is None and name not in {"__init__", "__str__", "__repr__"}:
                    findings.append({
                        "code": "CUSTOM_METHOD_NOT_FOUND_UPSTREAM", "severity": "info",
                        "model": item["model"], "symbol": name,
                        "message": "No same-named method exists in the selected Odoo Community definition; confirm this is an intentional extension.",
                        "evidence": evidence,
                    })
            for name, spec in item["fields"].items():
                previous = base["fields"].get(name)
                if previous and previous != spec:
                    findings.append({
                        "code": "FIELD_TYPE_DRIFT", "severity": "warning",
                        "model": item["model"], "symbol": name,
                        "message": "Custom field declaration differs from upstream type or comodel.",
                        "custom_field": {"type": spec[0], "comodel": spec[1]},
                        "upstream_field": {"type": previous[0], "comodel": previous[1]},
                        "evidence": {
                            "custom_path": item["path"], "custom_line": item["line"],
                            "upstream_path": base["path"], "upstream_line": base["line"],
                        },
                    })
        return {
            "status": "compared", "module": module_name, "series": series,
            "upstream_source": f"odoo/odoo@{branch}", "dependencies": dependencies,
            "unresolved_dependencies": unresolved,
            "dependencies_truncated": dependency_limit_hit,
            "coverage": {
                "dependency_files_scanned": len(scanned),
                "dependency_files_omitted": len(omitted),
                "custom_files_scanned": custom_count,
                "source_chars": chars,
                "complete": not omitted and not unresolved and not dependency_limit_hit,
            },
            "findings": findings[:200], "warnings": warnings[:100],
            "omitted_files": omitted[:100],
            "limitations": [
                "Community source only; Enterprise/private addons are not available from odoo/odoo.",
                "Only direct manifest dependencies are searched; transitive dependency closure is a later phase.",
                "This is static evidence and does not prove runtime compatibility.",
            ],
        }

    def compare(self, *, module_path:Path, module_name:str, client_name:str|None=None,
                max_dependency_files:int=30, max_total_chars:int=12000)->dict[str,Any]:
        module_path=module_path.resolve(strict=True); manifest_path=module_path/"__manifest__.py"
        if not module_path.is_dir() or not manifest_path.is_file(): raise ValueError("Valid addon directory and manifest required")
        manifest=ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest,dict): raise ValueError("Manifest must contain a dictionary")
        deps=[x for x in manifest.get("depends",[]) if isinstance(x,str) and x.strip()]
        roots=CoreFinderService()._resolve_roots(None,client_name=client_name)
        root_labels=[{"priority":str(p),"path":str(Path(root))} for p,root in roots]
        upstream={}; scanned=[]; omitted=[]; warnings=[]; chars=0
        budget=max(4000,min(int(max_total_chars),20000)); limit=max(5,min(int(max_dependency_files),60)); stop=False
        for _,root in roots:
            rp=Path(root).resolve()
            for dep_name in deps:
                dep=CoreFinderService._find_module_dir(rp,dep_name)
                if not dep: continue
                for folder in ("models","wizard","wizards","controllers"):
                    folder_path=dep/folder
                    if not folder_path.is_dir(): continue
                    for path in sorted(folder_path.rglob("*.py")):
                        try:
                            resolved=path.resolve(strict=True); resolved.relative_to(dep.resolve()); size=resolved.stat().st_size
                        except (OSError,ValueError):
                            warnings.append({"code":"UNSAFE_OR_UNREADABLE_UPSTREAM_PATH","path":str(path)}); continue
                        rel=resolved.relative_to(rp).as_posix()
                        if size>self._MAX_FILE_BYTES: omitted.append(rel); continue
                        if len(scanned)>=limit or chars>=budget: omitted.append(rel); stop=True; break
                        source=resolved.read_text(encoding="utf-8",errors="replace")
                        if chars+len(source)>budget: omitted.append(rel); continue
                        chars+=len(source); scanned.append(rel); models,errs=self._models(source,rel); warnings.extend(errs)
                        for model in models: upstream.setdefault(model["model"],model)
                    if stop: break
                if stop: break
            if stop: break
        custom=[]; custom_count=0
        for path in sorted(module_path.rglob("*.py")):
            try:
                resolved=path.resolve(strict=True); relpath=resolved.relative_to(module_path)
                if any(x in {".git","__pycache__","static","tests"} for x in relpath.parts) or resolved.stat().st_size>self._MAX_FILE_BYTES: continue
                source=resolved.read_text(encoding="utf-8",errors="replace")
            except (OSError,ValueError): continue
            custom_count+=1; models,errs=self._models(source,relpath.as_posix()); custom.extend(models); warnings.extend(errs)
        findings=[]
        for item in custom:
            base=upstream.get(item["model"])
            if not base:
                if not item["defined"]: findings.append({"code":"UPSTREAM_MODEL_NOT_FOUND","severity":"warning","model":item["model"],"message":"No matching model definition found in declared dependency source roots.","evidence":{"custom_path":item["path"],"custom_line":item["line"]}})
                continue
            for name,sig in item["methods"].items():
                old=base["methods"].get(name)
                if old and old!=sig: findings.append({"code":"METHOD_SIGNATURE_DRIFT","severity":"warning","model":item["model"],"symbol":name,"message":"Custom override signature differs from installed upstream definition.","custom_signature":sig,"upstream_signature":old,"evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
                elif old is None and name not in {"__init__","__str__","__repr__"}: findings.append({"code":"CUSTOM_METHOD_NOT_FOUND_UPSTREAM","severity":"info","model":item["model"],"symbol":name,"message":"No same-named method exists upstream; confirm intentional extension or stale override.","evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
            for name,spec in item["fields"].items():
                old=base["fields"].get(name)
                if old and old!=spec: findings.append({"code":"FIELD_TYPE_DRIFT","severity":"warning","model":item["model"],"symbol":name,"message":"Custom field declaration differs from upstream type or comodel.","custom_field":{"type":spec[0],"comodel":spec[1]},"upstream_field":{"type":old[0],"comodel":old[1]},"evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
        return {"status":"compared","module":module_name,"dependencies":deps,"upstream_roots":root_labels,
            "coverage":{"dependency_files_scanned":len(scanned),"dependency_files_omitted":len(omitted),"custom_files_scanned":custom_count,"source_chars":chars,"complete":not omitted},
            "findings":findings[:200],"warnings":warnings[:100],"omitted_files":omitted[:100],
            "limitations":["Only installed source roots and declared dependencies are compared.","Version/edition is not inferred without source-tree metadata.","Static comparisons do not prove runtime compatibility."]}
