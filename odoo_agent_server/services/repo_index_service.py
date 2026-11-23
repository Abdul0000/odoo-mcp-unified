"""Fast repository index for targeted Odoo agent decisions.

The index is intentionally lightweight: it records manifests, module names,
asset declarations, frontend/template files, and common source-file categories.
It lets preflight/router/verifier avoid repeatedly walking a large repo.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from itertools import islice

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import RepoIndexModule, RepoIndexResult
from .path_service import resolve_allowed_path
from .source_snapshot_service import content_snapshot, source_files
from .client_settings_store import atomic_json

_TEMPLATE_RE = re.compile(r't-name=["\']([^"\']+)["\']')
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build"}
_INTERESTING_SUFFIXES = {".py", ".xml", ".js", ".scss"}


class RepoIndexService:
    def index(self, project_path: str, force_refresh: bool = False, max_files: int = 5000) -> RepoIndexResult:
        project = resolve_allowed_path(project_path)
        if not project.exists() or not project.is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project path not found: {project}")

        cache_file = self._cache_file(project)
        # One bounded inventory feeds both invalidation and rebuilding. A
        # sentinel entry preserves the explicit partial-coverage indication.
        paths = list(islice(self._walk(project), max_files + 1))
        fingerprint = f"v3:{max_files}:{self._fingerprint(project, paths)}"
        if settings.repo_index_enabled and not force_refresh and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8") or "{}")
                age = time.time() - float(data.get("_saved_at", 0))
                if age <= max(1, settings.repo_index_cache_seconds) and data.get("_fingerprint") == fingerprint:
                    result_data = data.get("result") or {}
                    result_data["cached"] = True
                    return RepoIndexResult.model_validate(result_data)
            except Exception:
                pass

        result = self._build(project, max_files=max_files, paths=paths)
        if settings.repo_index_enabled:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(cache_file, {"_saved_at": time.time(), "_fingerprint": fingerprint, "result": result.model_dump(mode="json")})
        return result

    def related_files(self, written_files: list[str], project_path: str | None = None, max_related: int = 20) -> list[str]:
        """Return written files plus directly related companions only.

        This is not a full validation scan. It adds the local manifest and the
        nearest JS/XML template companions for OWL/QWeb asset consistency.
        """
        seen: set[str] = set()
        ordered: list[str] = []

        def add(path: str | Path) -> None:
            value = str(Path(path))
            if value not in seen and Path(value).exists():
                seen.add(value)
                ordered.append(value)

        for fp in written_files:
            add(fp)

        for fp in list(ordered):
            if len(ordered) >= len(written_files) + max_related:
                break
            path = Path(fp)
            module_root = self._module_root(path)
            if not module_root:
                continue
            manifest = module_root / "__manifest__.py"
            if manifest.exists():
                add(manifest)
            if path.suffix == ".js":
                # Same-module XML templates are the most useful companion for
                # static template / Component.template checks. Cap keeps it fast.
                for candidate in self._iter_interesting(module_root, suffix=".xml"):
                    add(candidate)
                    if len(ordered) >= len(written_files) + max_related:
                        break
            elif path.suffix == ".xml":
                # XML template changes often need related JS component files.
                for candidate in self._iter_interesting(module_root, suffix=".js"):
                    add(candidate)
                    if len(ordered) >= len(written_files) + max_related:
                        break
        return ordered

    def _build(self, project: Path, max_files: int, paths=None) -> RepoIndexResult:
        py: list[str] = []
        xml: list[str] = []
        js: list[str] = []
        scss: list[str] = []
        manifests: list[str] = []
        modules: list[RepoIndexModule] = []
        templates: dict[str, str] = {}
        notes: list[str] = []
        count = 0

        for path in paths if paths is not None else self._walk(project):
            count += 1
            if count > max_files:
                notes.append(f"Index stopped at max_files={max_files}; increase max_files for a complete index.")
                break
            rel = path.relative_to(project).as_posix()
            if path.name == "__manifest__.py":
                manifests.append(rel)
                module = self._parse_manifest(project, path)
                if module:
                    modules.append(module)
            elif path.suffix == ".py":
                py.append(rel)
            elif path.suffix == ".xml":
                xml.append(rel)
                for name in self._extract_templates(path):
                    templates.setdefault(name, rel)
            elif path.suffix == ".js":
                js.append(rel)
            elif path.suffix == ".scss":
                scss.append(rel)

        return RepoIndexResult(
            project_path=str(project),
            cached=False,
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_files=count,
            modules=modules,
            python_files=py[:2000],
            xml_files=xml[:2000],
            js_files=js[:2000],
            scss_files=scss[:2000],
            manifest_files=manifests[:1000],
            template_names=templates,
            notes=notes,
        )

    def _parse_manifest(self, project: Path, manifest: Path) -> RepoIndexModule | None:
        try:
            data = ast.literal_eval(manifest.read_text(encoding="utf-8", errors="ignore"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        module_path = manifest.parent
        assets_raw = data.get("assets") if isinstance(data, dict) else {}
        assets: dict[str, list[str]] = {}
        if isinstance(assets_raw, dict):
            for bundle, items in assets_raw.items():
                if isinstance(items, (list, tuple)):
                    assets[str(bundle)] = [str(item) for item in items]
        depends_raw = data.get("depends") if isinstance(data, dict) else []
        depends = [str(item) for item in depends_raw] if isinstance(depends_raw, (list, tuple)) else []
        return RepoIndexModule(
            name=module_path.name,
            path=module_path.relative_to(project).as_posix(),
            manifest_path=manifest.relative_to(project).as_posix(),
            depends=depends,
            assets=assets,
        )

    def _extract_templates(self, path: Path) -> list[str]:
        try:
            with path.open(encoding="utf-8", errors="ignore") as stream:
                text = stream.read(500_000)
        except Exception:
            return []
        return [m.group(1) for m in _TEMPLATE_RE.finditer(text)]

    def _cache_file(self, project: Path) -> Path:
        key = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:24]
        return settings.cache_path / "repo_index" / f"{key}.json"

    def _fingerprint(self, project: Path, paths=None) -> str:
        digest = hashlib.sha256()
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                                  capture_output=True, timeout=3, check=False)
            digest.update(head.stdout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # Fingerprint indexed content, including dirty and untracked files. Reuse
        # stat-validated per-file digests so cache hits do not reread whole files;
        # content_digest still detects same-size edits with preserved mtime.
        # _walk already sorts each directory and its children. Avoid first
        # materializing and globally sorting the entire repository path set;
        # this fingerprint runs before the index walk on every lookup.
        for path in paths if paths is not None else self._walk(project):
            digest.update(str(path.relative_to(project)).encode())
            try:
                mode, content = content_snapshot(path)
                digest.update(str(mode).encode())
                digest.update(content)
            except OSError:
                digest.update(b"unreadable")
        return digest.hexdigest()

    def _walk(self, project: Path):
        for path in source_files(project, _SKIP_DIRS):
            if path.suffix in _INTERESTING_SUFFIXES or path.name == "__manifest__.py":
                yield path

    def _module_root(self, path: Path) -> Path | None:
        current = path if path.is_dir() else path.parent
        for parent in [current, *current.parents]:
            if (parent / "__manifest__.py").exists():
                return parent
        return None

    def _iter_interesting(self, root: Path, suffix: str):
        for path in root.rglob(f"*{suffix}"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                yield path
