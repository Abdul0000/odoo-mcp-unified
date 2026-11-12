"""Cached Odoo-aware code search for functional development requests.

The service converts labels and technical clues into ranked Python/XML/QWeb/JS
files plus exact line evidence. It intentionally indexes one selected custom
module at a time; installed-source expansion remains owned by scope_advice.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from .path_service import resolve_allowed_path
from .coding_session_service import CodingSessionService
from .module_path_service import is_ignored_source_path


_CODE_SUFFIXES = {".py", ".xml", ".js", ".scss", ".css", ".csv"}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{1,120}")
_QUOTED_RE = re.compile(r"[`\"']([^`\"'\r\n]{2,180})[`\"']")
_TECHNICAL_RE = re.compile(r"(?<![\w.-])([.#]?[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+|[A-Za-z_][\w]*_[A-Za-z0-9_]+)(?![\w-])")
_CAPITAL_PHRASE_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9/&-]*\s+){1,5}[A-Z][A-Za-z0-9/&-]*\b")
_STOP_WORDS = {
    "add", "after", "alter", "and", "before", "change", "code", "create", "delete", "deploy",
    "development", "do", "field", "file", "for", "form", "from", "in", "list", "manifest", "model",
    "module", "new", "not", "odoo", "of", "on", "or", "preserve", "record", "records", "remove",
    "report", "request", "the", "to", "update", "upgrade", "use", "view", "visible", "with",
}


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _literal(value: ast.AST | None) -> Any:
    if value is None:
        return None
    try:
        return ast.literal_eval(value)
    except Exception:
        return None


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _append_unique(values: list[str], raw: Any, *, limit: int = 300) -> None:
    value = str(raw or "").strip()
    if value and len(value) <= 240 and value not in values and len(values) < limit:
        values.append(value)


class OdooCodeSearchService:
    """Build and query a small relationship index for one custom addon."""

    def search(
        self,
        *,
        project_path: str,
        module_path: str,
        task_description: str,
        target_hint: str | None = None,
        intent: str | None = None,
        search_patterns: list[str] | None = None,
        task_type: str = "auto",
        max_results: int = 10,
        module_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        project = resolve_allowed_path(project_path)
        module = (project / str(module_path or "")).resolve()
        if project != module and project not in module.parents:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Module path escapes project root")
        if not module.is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module path not found: {module}")

        clues = self.expand_clues(task_description, target_hint, intent, search_patterns or [])
        fingerprint = module_fingerprint or self._fingerprint(project, module)
        index, cached = self._load_or_build(project, module, fingerprint)
        ranked = self._rank(index.get("documents") or [], project, clues, task_type, max_results)
        top_score = float(ranked[0]["score"]) if ranked else 0.0
        second_score = float(ranked[1]["score"]) if len(ranked) > 1 else 0.0
        score_margin = max(0.0, top_score - second_score)
        resolution_status = (
            "not_found" if not ranked
            else "ambiguous" if len(ranked) > 1 and score_margin < 10
            else "high_confidence" if top_score >= 80
            else "candidate"
        )
        return {
            "status": "ok",
            "engine": "odoo_code_graph",
            "module_path": module.relative_to(project).as_posix(),
            "cached": cached,
            "fingerprint": fingerprint,
            "clues": clues,
            "matches": ranked,
            "count": len(ranked),
            "confidence": round(min(1.0, top_score / 100.0), 3),
            "score_margin": round(score_margin, 3),
            "resolution_status": resolution_status,
            "indexed_files": len(index.get("documents") or []),
            "scanned_files": int(index.get("scanned_files") or 0),
            "strategy": "hybrid_ranked_module_index",
            "channels": ["symbol_graph", "path_match", "label_value", "bounded_relationship_expansion"],
            "index_truncated": bool(index.get("truncated")),
            "fallback_required": bool(index.get("truncated")) or resolution_status in {"not_found", "ambiguous"},
        }

    @staticmethod
    def expand_clues(description: str, target_hint: str | None, intent: str | None, explicit: list[str]) -> list[str]:
        clues: list[str] = []
        for value in explicit:
            _append_unique(clues, value, limit=25)
        _append_unique(clues, target_hint, limit=25)

        combined = " ".join(filter(None, [description, target_hint or "", intent or ""]))
        for match in _QUOTED_RE.finditer(combined):
            _append_unique(clues, match.group(1), limit=25)
        for match in _TECHNICAL_RE.finditer(combined):
            _append_unique(clues, match.group(1), limit=25)
        for match in _CAPITAL_PHRASE_RE.finditer(combined):
            words = match.group(0).split()
            while words and words[0].lower() in _STOP_WORDS:
                words.pop(0)
            if len(words) >= 2:
                _append_unique(clues, " ".join(words), limit=25)

        # Decompose functional target hints into bounded phrases and technical tokens.
        hint_tokens = [token for token in _WORD_RE.findall(str(target_hint or "")) if token.lower() not in _STOP_WORDS]
        plain_tokens = [token for token in hint_tokens if "_" not in token and "." not in token]
        for size in (4, 3, 2):
            for index in range(0, max(0, len(plain_tokens) - size + 1)):
                _append_unique(clues, " ".join(plain_tokens[index:index + size]), limit=25)
        for token in hint_tokens:
            if "_" in token or "." in token or len(token) >= 4:
                _append_unique(clues, token, limit=25)

        # Callable identifiers may be unquoted, e.g. _compute_total().
        for match in re.finditer(r"\b([A-Za-z_][\w]{2,})\s*\(", combined):
            _append_unique(clues, match.group(1), limit=25)
        return clues[:25]

    def _load_or_build(self, project: Path, module: Path, fingerprint: str) -> tuple[dict[str, Any], bool]:
        cache_file = self._cache_file(project, module)
        if settings.repo_index_enabled and cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8") or "{}")
                age = time.time() - float(payload.get("saved_at") or 0)
                if payload.get("fingerprint") == fingerprint and age <= max(1, settings.repo_index_cache_seconds):
                    return payload.get("index") or {}, True
            except Exception:
                pass
        index = self._build_index(project, module)
        if settings.repo_index_enabled:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"saved_at": time.time(), "fingerprint": fingerprint, "index": index}, ensure_ascii=False), encoding="utf-8")
        return index, False

    def _build_index(self, project: Path, module: Path) -> dict[str, Any]:
        """Build one task-independent symbol graph and reuse it for every query."""
        entries: list[dict[str, Any]] = []
        truncated = False
        for path in sorted(module.rglob("*")):
            if not path.is_file() or is_ignored_source_path(path.relative_to(module).parts):
                continue
            try:
                path.resolve().relative_to(module)
            except (OSError, ValueError):
                continue
            if path.suffix.lower() not in _CODE_SUFFIXES and path.name != "__manifest__.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")[:500_000]
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(project).as_posix()
            entries.append({"path": path, "rel": rel, "text": text})
            if len(entries) >= 2000:
                truncated = True
                break

        documents = [self._parse_document(project, item) for item in entries]

        return {
            "module": module.name,
            "documents": documents,
            "scanned_files": len(entries),
            "truncated": truncated,
        }

    def _parse_document(self, project: Path, item: dict[str, Any]) -> dict[str, Any]:
        path: Path = item["path"]
        text = item["text"]
        doc = {"path": path.relative_to(project).as_posix(), "kind": path.suffix.lower().lstrip(".") or "manifest", "symbols": [], "relations": [], "values": []}
        if path.name == "__manifest__.py":
            self._index_manifest(text, doc)
        elif path.suffix == ".py":
            self._index_python(text, doc)
        elif path.suffix == ".xml":
            self._index_xml(text, doc)
        elif path.suffix == ".js":
            self._index_js(text, doc)
        elif path.suffix in {".scss", ".css"}:
            self._index_styles(text, doc)
        elif path.suffix == ".csv":
            self._index_csv(text, doc)
        return doc

    @staticmethod
    def _symbol(doc: dict[str, Any], name: Any, kind: str, line: int) -> None:
        value = str(name or "").strip()
        if not value or len(value) > 240:
            return
        item = {"name": value, "kind": kind, "line": max(1, int(line or 1))}
        if item not in doc["symbols"]:
            doc["symbols"].append(item)

    def _index_manifest(self, text: str, doc: dict[str, Any]) -> None:
        try:
            data = ast.literal_eval(text)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            return
        for key in ("name", "summary", "category"):
            _append_unique(doc["values"], data.get(key))
        for dependency in data.get("depends") or []:
            _append_unique(doc["relations"], dependency)
            self._symbol(doc, dependency, "manifest_dependency", 1)
        for bundle, items in (data.get("assets") or {}).items():
            self._symbol(doc, bundle, "asset_bundle", 1)
            for item in items if isinstance(items, (list, tuple)) else []:
                _append_unique(doc["relations"], item)

    def _index_python(self, text: str, doc: dict[str, Any]) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._symbol(doc, node.name, "class", node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._symbol(doc, node.name, "method", node.lineno)
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", "") == "route":
                        route = _literal(decorator.args[0]) if decorator.args else None
                        if isinstance(route, str):
                            self._symbol(doc, route, "route", getattr(decorator, "lineno", node.lineno))
                            _append_unique(doc["values"], route)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value_node = node.value
                for target in targets:
                    name = target.id if isinstance(target, ast.Name) else None
                    if not name:
                        continue
                    literal = _literal(value_node)
                    if name in {"_name", "_inherit", "_description"}:
                        values = literal if isinstance(literal, (list, tuple)) else [literal]
                        for value in values:
                            if isinstance(value, str):
                                self._symbol(doc, value, "model" if name != "_description" else "description", getattr(node, "lineno", 1))
                                _append_unique(doc["values"], value)
                                if name == "_inherit":
                                    _append_unique(doc["relations"], value)
                    if isinstance(value_node, ast.Call) and isinstance(value_node.func, ast.Attribute):
                        owner = value_node.func.value
                        if isinstance(owner, ast.Name) and owner.id == "fields":
                            self._symbol(doc, name, "field", getattr(node, "lineno", 1))
                            first_arg = _literal(value_node.args[0]) if value_node.args else None
                            if isinstance(first_arg, str) and "." in first_arg:
                                _append_unique(doc["relations"], first_arg)
                            for keyword in value_node.keywords:
                                value = _literal(keyword.value)
                                if keyword.arg in {"related", "compute", "inverse", "search"} and isinstance(value, str):
                                    self._symbol(doc, value, f"field_{keyword.arg}", getattr(keyword, "lineno", getattr(node, "lineno", 1)))
                                    _append_unique(doc["relations"], value)
                                elif keyword.arg == "string" and isinstance(value, str):
                                    _append_unique(doc["values"], value)

    def _index_xml(self, text: str, doc: dict[str, Any]) -> None:
        for match in re.finditer(r"<(?:record|template)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:record|template)>", text, flags=re.S | re.I):
            attrs, body = match.group("attrs"), match.group("body")
            line = _line_number(text, match.start())
            id_match = re.search(r"\bid=[\"']([^\"']+)[\"']", attrs)
            model_match = re.search(r"\bmodel=[\"']([^\"']+)[\"']", attrs)
            if id_match:
                self._symbol(doc, id_match.group(1), "xml_id", line)
            if model_match:
                self._symbol(doc, model_match.group(1), "xml_record_model", line)
            for field in re.finditer(r"<field\b(?P<attrs>[^>]*)>(?P<value>.*?)</field>|<field\b(?P<selfattrs>[^>]*)/>", body, flags=re.S | re.I):
                field_attrs = field.group("attrs") or field.group("selfattrs") or ""
                name_match = re.search(r"\bname=[\"']([^\"']+)[\"']", field_attrs)
                if not name_match:
                    continue
                field_name = name_match.group(1)
                field_line = _line_number(text, match.start("body") + field.start())
                self._symbol(doc, field_name, "xml_field", field_line)
                ref_match = re.search(r"\bref=[\"']([^\"']+)[\"']", field_attrs)
                raw_value = re.sub(r"<[^>]+>", " ", field.group("value") or "")
                value = re.sub(r"\s+", " ", raw_value).strip()
                relation = ref_match.group(1) if ref_match else value
                if field_name in {"model", "res_model", "inherit_id", "report_name", "binding_model_id", "view_id"} and relation:
                    self._symbol(doc, relation, f"xml_{field_name}", field_line)
                    _append_unique(doc["relations"], relation)
                elif value and len(value) <= 180:
                    _append_unique(doc["values"], value)
        for match in re.finditer(r"\b(?P<attr>t-name|t-call|inherit_id|name|string|id|action|ref|parent)=[\"'](?P<value>[^\"']+)[\"']", text):
            attr = match.group("attr")
            value = match.group("value")
            kind = f"xml_attribute_{attr}"
            self._symbol(doc, value, kind, _line_number(text, match.start()))
            _append_unique(doc["values"], value)
            if attr in {"t-call", "inherit_id", "action", "ref", "parent"} or "." in value:
                _append_unique(doc["relations"], value)
        for match in re.finditer(r">\s*([^<>\n]{2,180}?)\s*<", text):
            _append_unique(doc["values"], match.group(1))

    def _index_js(self, text: str, doc: dict[str, Any]) -> None:
        patterns = (
            (r"\bclass\s+([A-Za-z_$][\w$]*)", "js_class"),
            (r"\b(?:function|async\s+function)\s+([A-Za-z_$][\w$]*)", "js_function"),
            (r"\b(?:static\s+)?template\s*=\s*[\"']([^\"']+)[\"']", "owl_template"),
            (r"registry\.category\([^)]*\)\.add\(\s*[\"']([^\"']+)[\"']", "registry_key"),
            (r"\bpatch\([^,]+,\s*[\"']([^\"']+)[\"']", "patch_key"),
        )
        for pattern, kind in patterns:
            for match in re.finditer(pattern, text):
                self._symbol(doc, match.group(1), kind, _line_number(text, match.start()))
                _append_unique(doc["relations"], match.group(1))
        for match in re.finditer(r"[\"']([^\"'\r\n]{2,160})[\"']", text):
            _append_unique(doc["values"], match.group(1))

    def _index_styles(self, text: str, doc: dict[str, Any]) -> None:
        for match in re.finditer(r"(?m)([.#][A-Za-z_][\w-]*)[^\n{]*\{", text):
            self._symbol(doc, match.group(1), "selector", _line_number(text, match.start()))

    def _index_csv(self, text: str, doc: dict[str, Any]) -> None:
        for line_no, line in enumerate(text.splitlines()[:1000], start=1):
            for value in line.split(",")[:20]:
                value = value.strip().strip('"')
                if value:
                    self._symbol(doc, value, "csv_value", line_no)

    def _rank(self, documents: list[dict[str, Any]], project: Path, clues: list[str], task_type: str, max_results: int) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        clue_norms = [(clue, _normal(clue)) for clue in clues if _normal(clue)]
        for doc in documents:
            score = 0
            reasons: list[str] = []
            lines: set[int] = set()
            path = str(doc.get("path") or "")
            path_norm = _normal(path)
            symbols = doc.get("symbols") or []
            values = [str(value) for value in (doc.get("values") or [])]
            for clue, norm in clue_norms:
                technical = "_" in clue or "." in clue or clue.startswith((".", "#", "/"))
                for symbol in symbols:
                    symbol_norm = _normal(symbol.get("name"))
                    if norm == symbol_norm:
                        score += (75 if technical else 60) + self._symbol_task_bonus(
                            str(symbol.get("kind") or ""),
                            path,
                            task_type,
                        )
                        lines.add(int(symbol.get("line") or 1))
                        _append_unique(reasons, f"exact {symbol.get('kind')}: {symbol.get('name')}", limit=8)
                    elif len(norm) >= 4 and (norm in symbol_norm or symbol_norm in norm):
                        score += 28
                        lines.add(int(symbol.get("line") or 1))
                        _append_unique(reasons, f"related {symbol.get('kind')}: {symbol.get('name')}", limit=8)
                for value in values:
                    value_norm = _normal(value)
                    if norm == value_norm:
                        score += 55
                        _append_unique(reasons, f"exact label/value: {value}", limit=8)
                    elif len(norm) >= 5 and norm in value_norm:
                        score += 25
                        _append_unique(reasons, f"matching label/value: {value}", limit=8)
                if norm in path_norm:
                    score += 18
                    _append_unique(reasons, f"path match: {clue}", limit=8)
            score += self._task_type_bonus(path, task_type)
            if score > 0:
                ranked.append({"path": path, "score": score, "lines": sorted(lines), "reasons": reasons, "relations": doc.get("relations") or [], "symbols": symbols})

        # One bounded graph hop: action/view labels lead to res_model; XML field
        # usages lead to their Python declarations; QWeb names lead to JS/report users.
        existing = {item["path"]: item for item in ranked}
        applied_edges: set[tuple[str, str, str]] = set()
        for _hop in range(2):
            primary = sorted(existing.values(), key=lambda item: (-item["score"], item["path"]))[:8]
            relation_names: set[str] = set()
            shared_symbols: set[str] = set()
            for item in primary:
                for value in item.get("relations") or []:
                    normalized = _normal(value)
                    if normalized:
                        relation_names.add(normalized)
                        relation_names.add(_normal(str(value).split(".")[-1]))
                for symbol in item.get("symbols") or []:
                    normalized = _normal(symbol.get("name"))
                    if normalized:
                        shared_symbols.add(normalized)
            changed = False
            for doc in documents:
                path = str(doc.get("path") or "")
                symbol_map: dict[str, dict[str, Any]] = {}
                for symbol in doc.get("symbols") or []:
                    normalized = _normal(symbol.get("name"))
                    if normalized:
                        symbol_map.setdefault(normalized, symbol)
                        symbol_map.setdefault(_normal(str(symbol.get("name") or "").split(".")[-1]), symbol)
                relation_overlap = relation_names & set(symbol_map)
                field_overlap = {name for name in shared_symbols & set(symbol_map) if "_" in name and len(name) >= 4}
                if not relation_overlap and not field_overlap:
                    continue
                item = existing.setdefault(path, {"path": path, "score": 0, "lines": [], "reasons": [], "relations": doc.get("relations") or [], "symbols": doc.get("symbols") or []})
                for name in sorted(relation_overlap):
                    edge = (path, "relation", name)
                    if edge in applied_edges:
                        continue
                    applied_edges.add(edge); changed = True
                    item["score"] += 45
                    item["lines"].append(int(symbol_map[name].get("line") or 1))
                    _append_unique(item["reasons"], f"relationship target: {symbol_map[name].get('name')}", limit=8)
                for name in sorted(field_overlap):
                    edge = (path, "field", name)
                    if edge in applied_edges:
                        continue
                    applied_edges.add(edge); changed = True
                    item["score"] += 22
                    item["lines"].append(int(symbol_map[name].get("line") or 1))
                    _append_unique(item["reasons"], f"shared field/reference: {symbol_map[name].get('name')}", limit=8)
                item["lines"] = sorted(set(item["lines"]))
            if not changed:
                break
        ranked = list(existing.values())

        ranked = sorted(ranked, key=lambda item: (-item["score"], item["path"]))[: max(1, min(max_results, 10))]
        top_score = float(ranked[0]["score"]) if ranked else 0.0
        for index, item in enumerate(ranked):
            if not item["lines"]:
                item["lines"] = self._find_lines(project / item["path"], clues)
            item["lines"] = item["lines"][:12]
            item.pop("symbols", None)
            item["confidence"] = round(min(1.0, float(item["score"]) / 100.0), 3)
            item["ownership"] = "client_custom"
            item["writable"] = True
            item["role"] = "primary" if index == 0 or float(item["score"]) >= top_score - 10 else "supporting"
        return ranked

    @staticmethod
    def _symbol_task_bonus(kind: str, path: str, task_type: str) -> int:
        """Prefer owning definitions over references for the active task type."""
        task = str(task_type or "auto").lower()
        lowered = path.lower()
        if task == "python_model" and kind in {"model", "method", "field", "class"}:
            return 30
        if task == "controller" and kind in {"route", "method", "class"}:
            return 35 if "/controllers/" in lowered else 15
        if task in {"xml_view", "qweb_template", "report"} and kind.startswith(("xml_", "owl_")):
            return 30
        if task in {"js_owl", "pos_owl", "js_report"} and kind in {
            "js_class", "js_function", "owl_template", "registry_key", "patch_key"
        }:
            return 30
        if task in {"scss_css", "style"} and kind == "selector":
            return 30
        return 0

    @staticmethod
    def _task_type_bonus(path: str, task_type: str) -> int:
        lowered = path.lower()
        task = str(task_type or "auto").lower()
        if task == "python_model" and lowered.endswith(".py"):
            return 15
        if task == "controller" and lowered.endswith(".py"):
            return 25 if "/controllers/" in lowered else 8
        if task in {"xml_view", "qweb_template", "report"} and lowered.endswith(".xml"):
            return 15
        if task in {"js_owl", "pos_owl", "js_report"} and lowered.endswith(".js"):
            return 15
        if task in {"scss_css", "style"} and lowered.endswith((".scss", ".css")):
            return 15
        return 0

    @staticmethod
    def _find_lines(path: Path, clues: list[str]) -> list[int]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        found: list[int] = []
        norms = [str(clue).lower() for clue in clues if str(clue).strip()]
        for line_no, line in enumerate(lines, start=1):
            lowered = line.lower()
            if any(value in lowered for value in norms):
                found.append(line_no)
                if len(found) >= 12:
                    break
        return found

    def _cache_file(self, project: Path, module: Path) -> Path:
        key = hashlib.sha256(f"{project}:{module}".encode("utf-8")).hexdigest()[:24]
        return settings.cache_path / "odoo_code_search" / f"{key}.json"

    @staticmethod
    def _fingerprint(project: Path, module: Path) -> str:
        files = [
            path for path in sorted(module.rglob("*"))
            if path.is_file()
            and not is_ignored_source_path(path.relative_to(module).parts)
            and OdooCodeSearchService._inside_module(path, module)
            and (path.suffix.lower() in _CODE_SUFFIXES or path.name == "__manifest__.py")
        ]
        return CodingSessionService._module_fingerprint_from_files(module, files)

    @staticmethod
    def _inside_module(path: Path, module: Path) -> bool:
        try:
            path.resolve().relative_to(module.resolve())
            return True
        except (OSError, ValueError):
            return False
