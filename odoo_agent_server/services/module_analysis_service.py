from __future__ import annotations

import ast
import csv
import io
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..schemas import Severity
from .validator_service import OdooModuleValidator
from .coding_session_service import CodingSessionService


class ModuleAnalysisService:
    """One-pass, local-only Odoo addon evidence collector."""

    _cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
    _lock = threading.Lock()
    _ttl_seconds = 300
    _extensions = {".py", ".xml", ".csv", ".js", ".scss", ".css"}
    _skip_parts = {"__pycache__", ".pytest_cache", "node_modules"}

    @staticmethod
    def _cache_limit() -> int:
        """Keep warm analysis state small on Render's 512 MB free instance."""
        raw = os.getenv("ODOO_AGENT_MODULE_ANALYSIS_CACHE_ENTRIES", "5")
        try:
            return max(1, min(int(raw), 20))
        except ValueError:
            return 5

    @staticmethod
    def _priority(path: Path) -> tuple[int, str]:
        rel = path.as_posix()
        name = path.name
        if name == "__manifest__.py":
            return (0, rel)
        if name == "__init__.py":
            return (1, rel)
        first = path.parts[0] if path.parts else ""
        order = {
            "models": 2, "controllers": 3, "controller": 3, "iot_handlers": 4,
            "handlers": 4, "wizard": 5, "wizards": 5, "security": 6,
            "views": 7, "report": 8, "reports": 8, "data": 9, "demo": 10,
            "tests": 11, "static": 12,
        }
        return (order.get(first, 20), rel)

    @staticmethod
    def _git_sha(project: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else "untracked"
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"

    @staticmethod
    def _preview(content: str, budget: int) -> tuple[str, bool]:
        if len(content) <= budget:
            return content, False
        half = max(1, budget // 2)
        omitted = len(content) - (half * 2)
        return content[:half] + f"\n...[{omitted} chars omitted]...\n" + content[-half:], True

    @staticmethod
    def _focused_preview(content: str, patterns: list[str], budget: int) -> tuple[str, bool]:
        """Return bounded match-centred evidence instead of unrelated file head/tail."""
        if not patterns or len(content) <= budget:
            return ModuleAnalysisService._preview(content, budget)
        lines = content.splitlines()
        hit_lines = [
            index for index, line in enumerate(lines)
            if any(pattern in line.casefold() for pattern in patterns)
        ]
        if not hit_lines:
            return ModuleAnalysisService._preview(content, budget)
        selected: set[int] = set()
        for index in hit_lines[:12]:
            selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
        chunks: list[str] = []
        previous = -2
        for index in sorted(selected):
            if index > previous + 1:
                chunks.append("...")
            chunks.append(f"{index + 1}: {lines[index]}")
            previous = index
        preview = "\n".join(chunks)
        if len(preview) > budget:
            preview = preview[: max(1, budget - 40)] + "\n...[focused evidence truncated]..."
        return preview, len(selected) < len(lines)

    @staticmethod
    def _normalise_target(module_path: Path, raw: str) -> str | None:
        """Return one safe module-relative target path."""
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            return None
        if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
            return None
        while value.startswith("./"):
            value = value[2:]
        if any(part == ".." for part in value.split("/")):
            return None
        prefixes = {module_path.name, module_path.as_posix().rstrip("/").split("/")[-1]}
        first, separator, remainder = value.partition("/")
        if separator and first in prefixes:
            value = remainder
        candidate = (module_path / value).resolve()
        try:
            return candidate.relative_to(module_path.resolve()).as_posix()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _normalise_patterns(patterns: list[str] | None) -> list[str]:
        values: list[str] = []
        for raw in patterns or []:
            value = str(raw or "").strip().strip("`\"'").casefold()
            if len(value) >= 2 and value not in values:
                values.append(value)
        return values[:25]

    def _select_files(
        self,
        *,
        module_path: Path,
        all_files: list[Path],
        max_files: int,
        target_files: list[str] | None,
        search_patterns: list[str] | None,
    ) -> tuple[list[Path], dict[str, dict[str, Any]], dict[str, Any], dict[Path, str]]:
        """Select explicit targets first, then exact search hits, then architecture files."""
        by_relative = {
            path.relative_to(module_path).as_posix(): path
            for path in all_files
        }
        requested: list[str] = []
        invalid: list[str] = []
        for raw in target_files or []:
            normalized = self._normalise_target(module_path, raw)
            if normalized and normalized not in requested:
                requested.append(normalized)
            elif str(raw or "").strip() and str(raw) not in invalid:
                invalid.append(str(raw))

        resolved = [value for value in requested if value in by_relative]
        missing = [value for value in requested if value not in by_relative]
        patterns = self._normalise_patterns(search_patterns)
        metadata: dict[str, dict[str, Any]] = {
            value: {"selected_by": "explicit_target", "matched_patterns": []}
            for value in resolved
        }
        content_cache: dict[Path, str] = {}
        ranked_matches: list[tuple[int, tuple[int, str], Path, list[str]]] = []
        explicit_set = set(resolved)
        if patterns:
            for path in all_files:
                rel = path.relative_to(module_path).as_posix()
                if rel in explicit_set:
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                content_cache[path] = content
                rel_folded = rel.casefold()
                content_folded = content.casefold()
                path_hits = [pattern for pattern in patterns if pattern in rel_folded]
                content_hits = [pattern for pattern in patterns if pattern in content_folded]
                matched = list(dict.fromkeys([*path_hits, *content_hits]))
                if matched:
                    rel_parts = set(path.relative_to(module_path).parts)
                    source_bonus = 40 if rel_parts & {"models", "controllers", "wizard", "views", "security", "tests"} else 0
                    low_signal_penalty = 60 if rel_parts & {"data", "demo"} else 0
                    technical_hits = sum(1 for pattern in matched if "." in pattern or "_" in pattern)
                    score = (len(path_hits) * 100) + (len(content_hits) * 25) + source_bonus + (technical_hits * 35) - low_signal_penalty
                    ranked_matches.append((score, self._priority(path.relative_to(module_path)), path, matched))

        ranked_matches.sort(key=lambda item: (-item[0], item[1]))
        selected: list[Path] = [by_relative[value] for value in resolved]
        reserve = min(4, max(1, max_files // 3))
        match_cap = max(1, min(10, max_files - reserve))
        for _, _, path, matched in ranked_matches[:match_cap]:
            if path not in selected:
                selected.append(path)
                metadata[path.relative_to(module_path).as_posix()] = {
                    "selected_by": "search_match",
                    "matched_patterns": matched,
                }
        for path in all_files:
            if path not in selected:
                selected.append(path)
                metadata[path.relative_to(module_path).as_posix()] = {
                    "selected_by": "architecture_priority",
                    "matched_patterns": [],
                }
        selected = selected[:max_files]
        selected_rel = {path.relative_to(module_path).as_posix() for path in selected}
        selection = {
            "target_files_requested": requested,
            "target_files_resolved": resolved,
            "target_files_missing": missing,
            "target_files_invalid": invalid,
            "search_patterns": patterns,
            "search_matched_files": [
                path.relative_to(module_path).as_posix()
                for _, _, path, _ in ranked_matches
            ],
            "selected_files": [path.relative_to(module_path).as_posix() for path in selected],
            "explicit_targets_omitted_by_limit": [value for value in resolved if value not in selected_rel],
        }
        return selected, metadata, selection, content_cache

    @staticmethod
    def _manifest(module_path: Path) -> dict[str, Any]:
        path = module_path / "__manifest__.py"
        if not path.is_file():
            return {}
        try:
            value = ast.literal_eval(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _cross_module_references(
        self,
        *,
        project: Path,
        module_name: str,
        module_candidates: list[Any] | None,
        identifiers: list[str],
    ) -> list[dict[str, Any]]:
        """Discover direct dependent UI/security owners with a small one-hop scan."""
        if not module_candidates or not identifiers:
            return []
        related: list[dict[str, Any]] = []
        for item in module_candidates:
            candidate_name = str(getattr(item, "module_name", "") or "")
            if not candidate_name or candidate_name == module_name:
                continue
            candidate = (project / str(getattr(item, "path", ""))).resolve()
            try:
                candidate.relative_to(project.resolve())
            except (OSError, ValueError):
                continue
            manifest = self._manifest(candidate)
            depends = [str(value) for value in (manifest.get("depends") or [])]
            if module_name not in depends:
                continue
            refs: list[dict[str, Any]] = []
            scanned = 0
            for path in candidate.rglob("*"):
                rel = path.relative_to(candidate)
                if (
                    not path.is_file()
                    or path.suffix.lower() not in self._extensions
                    or any(part in self._skip_parts for part in rel.parts)
                    or "static/lib" in rel.as_posix()
                ):
                    continue
                scanned += 1
                if scanned > 400:
                    break
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")[:500_000]
                except OSError:
                    continue
                folded = content.casefold()
                matched = [value for value in identifiers if value.casefold() in folded]
                if matched:
                    refs.append({
                        "path": rel.as_posix(),
                        "matched_identifiers": matched[:10],
                        "role": "ui" if path.suffix.lower() == ".xml" else "source",
                    })
                if len(refs) >= 6:
                    break
            if refs:
                related.append({
                    "module_name": candidate_name,
                    "module_path": str(candidate),
                    "depends_on": module_name,
                    "reference_files": refs,
                })
            if len(related) >= 3:
                break
        return related

    @staticmethod
    def _python_facts(path: str, content: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"path": path, "models": [], "classes": [], "methods": [], "routes": []}
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return facts
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(base) for base in node.bases]
                row: dict[str, Any] = {"class": node.name, "bases": bases, "model": None, "inherit": None, "fields": []}
                for item in node.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)):
                        target = item.targets[0] if isinstance(item, ast.Assign) else item.target
                        value = item.value
                        target_name = target.id if isinstance(target, ast.Name) else None
                        if target_name in {"_name", "_inherit"} and isinstance(value, ast.Constant):
                            row["model" if target_name == "_name" else "inherit"] = value.value
                        if target_name and isinstance(value, ast.Call) and "fields." in ast.unparse(value.func):
                            row["fields"].append(target_name)
                facts["classes"].append(row)
                if row["model"] or row["inherit"]:
                    facts["models"].append(row)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                facts["methods"].append(node.name)
                for decorator in node.decorator_list:
                    rendered = ast.unparse(decorator)
                    if rendered.startswith(("http.route", "route")):
                        facts["routes"].append({"method": node.name, "decorator": rendered})
        return facts

    @staticmethod
    def _xml_facts(path: str, content: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"path": path, "records": [], "menus": [], "templates": []}
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return facts
        for node in root.iter():
            if node.tag == "record":
                facts["records"].append({"id": node.attrib.get("id"), "model": node.attrib.get("model")})
            elif node.tag == "menuitem":
                facts["menus"].append({"id": node.attrib.get("id"), "name": node.attrib.get("name"), "action": node.attrib.get("action")})
            elif node.tag in {"template", "t"} and (node.attrib.get("id") or node.attrib.get("t-name")):
                facts["templates"].append(node.attrib.get("id") or node.attrib.get("t-name"))
        return facts

    def analyze(
        self,
        *,
        project: Path,
        module_path: Path,
        module_name: str,
        analysis_goal: str,
        max_files: int,
        max_total_chars: int,
        target_files: list[str] | None = None,
        search_patterns: list[str] | None = None,
        validator_findings: list[dict[str, Any]] | None = None,
        module_candidates: list[Any] | None = None,
    ) -> dict[str, Any]:
        all_files = [
            path for path in module_path.rglob("*")
            if path.is_file()
            and path.suffix.lower() in self._extensions
            and not any(part in self._skip_parts for part in path.relative_to(module_path).parts)
            and "static/lib" not in path.relative_to(module_path).as_posix()
        ]
        all_files.sort(key=lambda item: self._priority(item.relative_to(module_path)))
        content_fingerprint = CodingSessionService._module_fingerprint_from_files(module_path, all_files)
        sha = self._git_sha(project)
        normalized_targets = tuple(
            value
            for value in (self._normalise_target(module_path, raw) for raw in target_files or [])
            if value
        )
        normalized_patterns = tuple(self._normalise_patterns(search_patterns))
        candidate_signature = tuple(
            sorted(
                (str(getattr(item, "module_name", "")), str(getattr(item, "path", "")))
                for item in (module_candidates or [])
            )
        )
        cache_key = (
            str(module_path.resolve()), sha, content_fingerprint, max_files,
            max_total_chars, normalized_targets, normalized_patterns, candidate_signature,
        )
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                result = deepcopy(cached[1])
                self._cache.move_to_end(cache_key)
                result["cache_hit"] = True
                result["analysis_goal"] = analysis_goal
                return result

        selected, selection_metadata, selection, content_cache = self._select_files(
            module_path=module_path,
            all_files=all_files,
            max_files=max_files,
            target_files=target_files,
            search_patterns=search_patterns,
        )
        evidence: list[dict[str, Any]] = []
        python_facts: list[dict[str, Any]] = []
        xml_facts: list[dict[str, Any]] = []
        csv_facts: list[dict[str, Any]] = []
        remaining = max_total_chars
        for index, path in enumerate(selected):
            if remaining <= 0:
                break
            rel = path.relative_to(module_path).as_posix()
            content = content_cache.get(path)
            if content is None:
                content = path.read_text(encoding="utf-8", errors="replace")
            files_left = max(1, len(selected) - index)
            budget = min(4000, max(300, remaining // files_left))
            matched_patterns = selection_metadata.get(rel, {}).get("matched_patterns", [])
            preview, truncated = self._focused_preview(content, matched_patterns, budget)
            evidence.append({
                "path": rel,
                "size": len(content),
                "truncated": truncated,
                "content": preview,
                **selection_metadata.get(rel, {}),
            })
            remaining -= len(preview)
            if path.suffix.lower() == ".py":
                python_facts.append(self._python_facts(rel, content))
            elif path.suffix.lower() == ".xml":
                xml_facts.append(self._xml_facts(rel, content))
            elif path.suffix.lower() == ".csv":
                try:
                    rows = list(csv.DictReader(io.StringIO(content)))
                except csv.Error:
                    rows = []
                csv_facts.append({"path": rel, "row_count": len(rows), "columns": list(rows[0]) if rows else []})

        manifest: dict[str, Any] = {}
        manifest_path = module_path / "__manifest__.py"
        if manifest_path.is_file():
            try:
                parsed = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
                manifest = parsed if isinstance(parsed, dict) else {}
            except (SyntaxError, ValueError):
                manifest = {}
        findings = (
            validator_findings
            if validator_findings is not None
            else [
                item.model_dump(mode="json")
                for item in OdooModuleValidator().validate(module_path)
                if item.severity in {Severity.error, Severity.warning}
            ]
        )
        selected_focus_paths = {
            rel for rel, meta in selection_metadata.items()
            if meta.get("selected_by") in {"explicit_target", "search_match"}
        }
        technical_identifiers: list[str] = []
        for facts in python_facts:
            if selected_focus_paths and facts.get("path") not in selected_focus_paths:
                continue
            for model in facts.get("models") or []:
                for value in (model.get("model"), model.get("inherit")):
                    if isinstance(value, str) and "." in value and value not in technical_identifiers:
                        technical_identifiers.append(value)
        for pattern in self._normalise_patterns(search_patterns):
            if "." in pattern and re.fullmatch(r"[a-z0-9_.]+", pattern) and pattern not in technical_identifiers:
                technical_identifiers.append(pattern)
        technical_identifiers = technical_identifiers[:12]
        cross_module_refs = self._cross_module_references(
            project=project,
            module_name=module_name,
            module_candidates=module_candidates,
            identifiers=technical_identifiers,
        )
        returned_paths = {item["path"] for item in evidence}
        selection["selected_files"] = [item["path"] for item in evidence]
        selection["explicit_targets_omitted_by_budget"] = [
            value
            for value in selection["target_files_resolved"]
            if value not in returned_paths
        ]
        result = {
            "status": "analyzed",
            "module_name": module_name,
            "module_path": str(module_path),
            "git_sha": sha,
            "cache_hit": False,
            "analysis_goal": analysis_goal,
            "manifest": {
                key: manifest.get(key) for key in ("name", "version", "summary", "depends", "data", "demo", "assets", "application")
                if key in manifest
            },
            "inventory": {
                "total_relevant_files": len(all_files),
                "returned_files": len(evidence),
                "omitted_files": [
                    path.relative_to(module_path).as_posix()
                    for path in all_files
                    if path.relative_to(module_path).as_posix() not in returned_paths
                ],
            },
            "selection": selection,
            "architecture": {"python": python_facts, "xml": xml_facts, "csv": csv_facts},
            "feature_contract": {
                "owner_module": module_name,
                "technical_identifiers": technical_identifiers,
                "related_modules": cross_module_refs,
                "live_verification_needed": False,
                "early_stop_reached": bool(technical_identifiers or selected_focus_paths),
            },
            "validator_findings": findings,
            "source_evidence": evidence,
            "complete_for_analysis": len(evidence) == len(all_files),
            "complete_for_feature": bool(technical_identifiers or selected_focus_paths),
            "success_allowed": True,
            "completion_required": False,
            "inspection_instructions": (
                "Answer the user's architecture, use-case, and gap question directly from this evidence. "
                "Stop when feature ownership, logic, security and UI references are resolved. Do not call code-change, "
                "core-source, deployment, QA, history, or another inspection tool. Query live Odoo only when the user "
                "explicitly requests installed/live state or source evidence remains ambiguous."
            ),
            "next_call": None,
        }
        with self._lock:
            while len(self._cache) >= self._cache_limit():
                self._cache.popitem(last=False)
            self._cache[cache_key] = (now + self._ttl_seconds, deepcopy(result))
        return result
