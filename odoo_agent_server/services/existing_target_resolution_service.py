from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any

from ..schemas import (
    OdooTargetEditPolicy,
    OdooTargetMatch,
    OdooTargetResolutionHint,
    OdooTargetResolutionResult,
)
from .client_resolver_service import ResolvedClient
from .odoo_jsonrpc_service import OdooJsonRpcService


_TEXT_EXTENSIONS = {".py", ".xml", ".js", ".ts", ".tsx", ".scss", ".css", ".csv"}
_EXISTING_WORDS = re.compile(
    r"\b(change|update|modify|rename|fix|adjust|remove|hide|show|replace|inherit|existing)\b",
    re.I,
)
_NEW_WORDS = re.compile(r"\b(add|create|new|introduce|implement)\b", re.I)
_GENERIC_IDENTIFIERS = {
    "field", "fields", "model", "method", "view", "button", "menu", "action",
    "cron", "template", "report", "record", "value", "label", "odoo", "module",
    "add", "create", "new", "introduce", "implement", "change", "update",
    "modify", "rename", "fix", "adjust", "remove", "hide", "show", "replace",
    "inherit", "existing",
}
_EXACT_TECHNICAL_IDENTIFIER = re.compile(
    r"(?:/[A-Za-z0-9_./{}:-]+|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
_EMBEDDED_TECHNICAL_IDENTIFIER = re.compile(
    r"(?<![\w])(?:"
    r"/[A-Za-z0-9_./{}:-]+|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r")(?![\w])"
)
_VCS_TARGET_CONTEXT = re.compile(
    r"\b(?:git\s+)?(?:branch|checkout|target\s+branch)\s*[:=]?\s*[`'\"]?"
    r"[A-Za-z0-9][A-Za-z0-9._/-]*[`'\"]?|"
    r"\b(?:push(?:\s+(?:it|code))?|deploy)\s+(?:to|on)\s+(?:the\s+)?(?:branch\s+)?[`'\"]?"
    r"[A-Za-z0-9][A-Za-z0-9._/-]*[`'\"]?",
    re.I,
)


def strip_vcs_target_context(text: str) -> str:
    """Remove Git routing phrases before extracting Odoo identifiers.

    Branches frequently look like valid Odoo snake-case symbols (for example
    ``test_18``).  They remain available to Git/client resolution through the
    original payload, but must never participate in model/field/view target
    resolution.
    """
    return _VCS_TARGET_CONTEXT.sub(" ", str(text or ""))


class ExistingTargetResolutionService:
    """Resolve an Odoo change target from already-focused source, then optionally live Odoo.

    It deliberately does not scan the repository. The workflow passes only source
    files it already selected, keeping this gate cheap and deterministic.
    """

    _cache: dict[str, tuple[float, OdooTargetResolutionResult]] = {}
    _cache_lock = threading.Lock()
    _cache_ttl_seconds = 300
    _cache_limit = 64

    def resolve(
        self,
        *,
        project: Path,
        module_path: Path,
        module_name: str,
        task_description: str,
        target_hint: str | None,
        intent: str | None,
        task_type: str,
        change_request: dict[str, Any],
        hint: OdooTargetResolutionHint,
        candidate_paths: list[Path],
        planned_paths: list[str],
        client: ResolvedClient | None,
        create_module: bool,
    ) -> OdooTargetResolutionResult:
        started = time.monotonic()
        mode = self._mode(hint, task_description, intent, create_module)
        clues = self._clues(hint, target_hint, task_description, change_request)
        identifier = self._authoritative_identifier(
            hint,
            target_hint,
            task_description,
            change_request,
        )
        kind = self._kind(
            hint,
            identifier,
            task_description,
            task_type,
            change_request,
        )
        model = hint.model or self._model_hint(change_request, task_description, kind, identifier)
        paths = self._safe_paths(project, candidate_paths)
        key = self._cache_key(
            project, module_name, mode, kind, identifier, model, task_description,
            paths, planned_paths, hint.require_live, getattr(client, "name", None),
        )
        cached = self._cache_get(key)
        if cached is not None:
            cached.cache_hit = True
            cached.duration_ms = max(0, int((time.monotonic() - started) * 1000))
            return cached

        if not identifier:
            status = "not_required" if mode == "auto" else "scope_verified"
            result = self._result(
                status=status,
                mode=mode,
                kind=kind,
                identifier=None,
                model=model,
                source_verified=bool(paths),
                edit_policy=self._edit_policy(module_path, [], module_scope=True),
                reason=(
                    "No exact technical identifier was asserted. The focused task/file scope remains authoritative; "
                    "existing source alone is not a new-target collision."
                ),
            )
            return self._finish(key, result, started)

        if self._is_module_scope(
            hint=hint,
            identifier=identifier,
            module_name=module_name,
            task_description=task_description,
            change_request=change_request,
            planned_paths=planned_paths,
        ):
            result = self._result(
                status="scope_verified",
                mode=mode,
                kind="module_scope",
                identifier=module_name,
                model=None,
                source_verified=bool(paths),
                edit_policy=self._edit_policy(module_path, [], module_scope=True),
                reason=(
                    "The request targets the addon as a whole. Multiple definitions and references "
                    "inside that addon are expected and are governed by the module contract, not a single-object ambiguity gate."
                ),
            )
            return self._finish(key, result, started)

        # This gate resolves exactly one target. Remaining task identifiers are
        # inspection context/deliverables, not alternative targets. Treating
        # them as peers lets incidental field names masquerade as method text
        # matches and creates false ambiguity on the write continuation.
        resolution_clues = [identifier] if identifier else clues
        matches = self._source_matches(
            paths=paths,
            module_path=module_path,
            module_name=module_name,
            clues=resolution_clues,
            requested_kind=kind,
            requested_model=model,
        )
        if mode == "existing" and planned_paths:
            normalized_plans = {str(path).replace("\\", "/").lstrip("./") for path in planned_paths}
            planned_matches = [
                match for match in matches
                if any(match.path.replace("\\", "/").endswith(f"/{planned}") for planned in normalized_plans)
            ]
            if planned_matches:
                matches = planned_matches
        definitions = [match for match in matches if match.role == "definition"]
        relevant = definitions or [match for match in matches if match.role == "reference"] or matches
        custom_relevant = [match for match in relevant if match.source == "custom"]
        if custom_relevant:
            # An exact implementation in the selected client addon is the edit
            # target. Its upstream Core/OCA definition is context, not ambiguity.
            relevant = custom_relevant

        if mode == "new" and relevant:
            result = self._result(
                status="collision", mode=mode, kind=kind, identifier=identifier, model=model,
                matches=relevant, source_verified=True, writes_blocked=True,
                edit_policy=self._edit_policy(module_path, relevant),
                question=f"Target {identifier!r} already exists. Confirm whether to modify it or provide a new technical identifier.",
                reason="A new target would collide with focused source.",
            )
            return self._finish(key, result, started)

        if mode == "existing" and identifier and not relevant:
            result = self._result(
                status="not_found", mode=mode, kind=kind, identifier=identifier, model=model,
                writes_blocked=True,
                edit_policy=self._edit_policy(module_path, []),
                question=f"Could not resolve existing target {identifier!r}. Confirm its exact technical identifier, model, XML ID, or file.",
                reason="The asserted existing target was not found in focused custom or installed source.",
            )
            return self._finish(key, result, started)

        distinct = {(match.path, match.identifier, match.model or "") for match in relevant}
        if mode == "existing" and identifier and len(distinct) > 1:
            choices = ", ".join(f"{match.path}:{match.line}" for match in relevant[:4])
            result = self._result(
                status="ambiguous", mode=mode, kind=kind, identifier=identifier, model=model,
                matches=relevant, source_verified=True, writes_blocked=True,
                edit_policy=self._edit_policy(module_path, relevant),
                question=f"Multiple targets match {identifier!r} ({choices}). Confirm the exact model, XML ID, or file.",
                reason="Focused source contains multiple plausible existing targets.",
            )
            return self._finish(key, result, started)

        live_kind = kind in {"model", "field", "view", "xml_id", "cron", "action", "menu", "security", "config", "record"}
        live_required = hint.require_live is True
        live_enabled = live_kind and hint.require_live is not False
        source_verified = bool(relevant) or mode == "new"
        status = "verified"
        live_checked = False
        live_status = "not_applicable"
        writes_blocked = False
        question = None

        if live_enabled:
            rpc = OdooJsonRpcService(client)
            if not rpc.configured():
                live_status = "unavailable"
                status = "source_verified_live_unverified"
                writes_blocked = live_required
            else:
                live_checked = True
                live_status, live_found = self._live_lookup(
                    rpc=rpc, kind=kind, identifier=identifier, model=model, module_name=module_name,
                )
                if live_status == "failed":
                    status = "source_verified_live_unverified"
                    writes_blocked = live_required
                elif mode == "new" and live_found:
                    status = "collision"
                    writes_blocked = True
                    question = f"Target {identifier!r} already exists in live Odoo. Confirm modification or choose a new identifier."
                elif mode == "existing" and not live_found:
                    status = "source_verified_live_missing"
                    writes_blocked = live_required

        if writes_blocked and not question:
            question = f"Live verification is required for {identifier!r} but did not succeed. Confirm connectivity or remove the live requirement."
        result = self._result(
            status=status, mode=mode, kind=kind, identifier=identifier, model=model,
            matches=relevant, source_verified=source_verified, live_required=live_required,
            live_checked=live_checked, live_status=live_status, writes_blocked=writes_blocked,
            edit_policy=self._edit_policy(module_path, relevant, new_target=mode == "new" and not relevant),
            question=question,
            reason=self._reason(status, mode),
        )
        return self._finish(key, result, started)

    @staticmethod
    def _mode(
        hint: OdooTargetResolutionHint,
        task_description: str,
        intent: str | None,
        create_module: bool,
    ) -> str:
        if hint.mode != "auto":
            return hint.mode
        text = f"{intent or ''} {task_description}"
        if create_module:
            return "new"
        if _EXISTING_WORDS.search(text):
            return "existing"
        if _NEW_WORDS.search(text):
            return "new"
        return "existing" if hint.identifier else "auto"

    @staticmethod
    def _authoritative_identifier(
        hint: OdooTargetResolutionHint,
        target_hint: str | None,
        task: str,
        change_request: dict[str, Any],
    ) -> str | None:
        """Return only an explicitly asserted technical target.

        Bare snake_case tokens in evidence are useful search clues, but they may
        be local variables, fixture names, or QA labels.  They must never become
        the object-level write gate's target unless the caller structured,
        labelled, quoted, or explicitly hinted them.
        """
        if hint.identifier:
            return hint.identifier.strip()
        for key in ("target", "identifier", "field", "method", "xml_id", "route", "template", "selector", "model"):
            value = change_request.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        explicit_hint = str(target_hint or "").strip()
        if explicit_hint:
            if _EXACT_TECHNICAL_IDENTIFIER.fullmatch(explicit_hint):
                return explicit_hint
            embedded = list(dict.fromkeys(_EMBEDDED_TECHNICAL_IDENTIFIER.findall(explicit_hint)))
            if len(embedded) == 1:
                return embedded[0]

        task_context = strip_vcs_target_context(task)
        labelled = re.search(
            r"\b(?:field|method|xml[_ -]?id|model|route|template|view|action|button)\b"
            r"\s*(?:=|:|named\s+|called\s+)?\s*[`'\"]?"
            r"(/[A-Za-z0-9_./{}:-]+|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)"
            r"[`'\"]?",
            task_context,
            re.I,
        )
        if labelled:
            return labelled.group(1)
        quoted = [
            value.strip()
            for value in re.findall(r"[`'\"]([^`'\"]{2,120})[`'\"]", task_context)
            if _EXACT_TECHNICAL_IDENTIFIER.fullmatch(value.strip())
        ]
        return quoted[0] if len(set(quoted)) == 1 else None

    @staticmethod
    def _is_module_scope(
        *,
        hint: OdooTargetResolutionHint,
        identifier: str | None,
        module_name: str,
        task_description: str,
        change_request: dict[str, Any],
        planned_paths: list[str],
    ) -> bool:
        if hint.kind not in {"auto", "other"}:
            return False
        identifier_is_module = bool(
            identifier
            and identifier.strip().lower().replace("-", "_")
            == module_name.strip().lower().replace("-", "_")
        )
        broad_contract = bool(re.search(
            r"\b(module[- ]wide|whole module|entire module|full (?:module )?redesign|"
            r"complete (?:module )?(?:implementation|redesign|rewrite|refactor)|"
            r"redesign (?:the )?(?:whole|entire|full) module)\b",
            task_description,
            re.I,
        ))
        structured_scope = str(change_request.get("scope") or "").strip().lower().replace("_", "-") in {
            "module", "module-wide", "whole-module", "entire-module",
        }
        multi_file_module_write = identifier_is_module and len(set(planned_paths)) > 1
        return structured_scope or broad_contract or identifier_is_module or multi_file_module_write

    @staticmethod
    def _clues(
        hint: OdooTargetResolutionHint,
        target_hint: str | None,
        task: str,
        change_request: dict[str, Any],
    ) -> list[str]:
        raw: list[str] = []
        if hint.identifier:
            raw.append(hint.identifier)
        for key in ("target", "identifier", "field", "method", "xml_id", "route", "template", "selector", "model"):
            value = change_request.get(key)
            if isinstance(value, str):
                raw.append(value)
        # A descriptive hint may contain the exact symbol plus prose, for
        # example "aba.recon.match.line invoice_id / Bill linkage". Never use
        # that entire sentence as an Odoo identifier. Prefer embedded snake-
        # case symbols (usually fields/methods), then routes and dotted names.
        target_context = str(target_hint or "")
        task_context = strip_vcs_target_context(task)
        combined = "\n".join(filter(None, (target_context, task_context)))
        embedded = list(dict.fromkeys(_EMBEDDED_TECHNICAL_IDENTIFIER.findall(combined)))
        embedded.sort(key=lambda value: (
            0 if "_" in value and "." not in value and not value.startswith("/") else
            1 if value.startswith("/") else
            2,
            combined.find(value),
        ))
        raw.extend(embedded)
        if target_hint and _EXACT_TECHNICAL_IDENTIFIER.fullmatch(target_hint.strip()):
            raw.append(target_hint)
        raw.extend(re.findall(r"[`'\"]([^`'\"]{2,120})[`'\"]", task_context))
        result: list[str] = []
        for value in raw:
            value = value.strip()
            if value and value.lower() not in _GENERIC_IDENTIFIERS and value not in result:
                result.append(value)
            if len(result) == 10:
                break
        return result

    @staticmethod
    def _kind(
        hint: OdooTargetResolutionHint,
        identifier: str | None,
        task: str,
        task_type: str,
        change_request: dict[str, Any],
    ) -> str:
        if hint.kind != "auto":
            return hint.kind
        for key, structured_kind in (
            ("method", "method"), ("field", "field"), ("xml_id", "xml_id"),
            ("route", "route"), ("template", "template"), ("selector", "component"),
            ("model", "model"),
        ):
            value = change_request.get(key)
            if isinstance(value, str) and (
                not identifier or ExistingTargetResolutionService._matches(value, identifier)
            ):
                return structured_kind
        text = f"{task_type} {task}".lower()
        for token, kind in (
            ("field", "field"), ("method", "method"), ("cron", "cron"), ("menu", "menu"),
            ("action", "action"), ("route", "route"), ("controller", "route"),
            ("qweb", "template"), ("template", "template"), ("owl", "component"),
            ("view", "view"), ("model", "model"),
        ):
            if token in text:
                return kind
        if identifier and identifier.startswith("/"):
            return "route"
        return "xml_id" if identifier and "." in identifier else "other"

    @staticmethod
    def _model_hint(change_request: dict[str, Any], task: str, kind: str, identifier: str | None) -> str | None:
        value = change_request.get("model")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.]+", value):
            return value
        models = re.findall(
            r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b",
            strip_vcs_target_context(task),
        )
        for model in models:
            if model != identifier and not model.startswith(("ir.ui.", "ir.actions.")):
                return model
        return identifier if kind == "model" and identifier and re.fullmatch(r"[A-Za-z0-9_.]+", identifier) else None

    @staticmethod
    def _safe_paths(project: Path, paths: list[Path]) -> list[Path]:
        result: list[Path] = []
        for raw in paths:
            try:
                path = Path(raw).resolve()
            except OSError:
                continue
            if path.is_file() and path.suffix.lower() in _TEXT_EXTENSIONS and path not in result:
                result.append(path)
            if len(result) == 50:
                break
        return result

    def _source_matches(
        self,
        *,
        paths: list[Path],
        module_path: Path,
        module_name: str,
        clues: list[str],
        requested_kind: str,
        requested_model: str | None,
    ) -> list[OdooTargetMatch]:
        found: list[OdooTargetMatch] = []
        lowered = [(clue, clue.lower()) for clue in clues]
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                continue
            symbols = self._symbols(path, text)
            source = "custom" if self._is_relative_to(path, module_path) else "installed"
            owner = module_name if source == "custom" else self._owner(path)
            for symbol in symbols:
                identifier = symbol["identifier"]
                if lowered and not any(self._matches(identifier, clue) for clue, _ in lowered):
                    continue
                compatible = (
                    requested_kind in {"other", symbol["kind"]}
                    or symbol["kind"] == "xml_id"
                    or requested_kind == "xml_id"
                )
                if not compatible:
                    continue
                if requested_model and symbol.get("model") and requested_model != symbol["model"]:
                    continue
                found.append(OdooTargetMatch(
                    path=str(path), line=symbol["line"], kind=symbol["kind"],
                    identifier=identifier, role=symbol["role"], model=symbol.get("model"),
                    owner=owner, source=source,
                ))
            # Plain-text fallback is useful only for unstructured targets that
            # do not have a reliable parser.  Structured Odoo targets must be
            # proved by AST/XML/JS symbols above.  Otherwise a new field such
            # as ``note`` collides with comments, labels, or prose in unrelated
            # installed sources (for example mail.thread documentation).
            if lowered and requested_kind in {"other", "component", "config", "record"}:
                for line_no, line in enumerate(text.splitlines(), 1):
                    for clue, clue_lower in lowered:
                        if re.search(rf"(?<![\w.]){re.escape(clue_lower)}(?![\w.])", line.lower()) and not any(
                            match.path == str(path) and match.line == line_no and self._matches(match.identifier, clue)
                            for match in found
                        ):
                            found.append(OdooTargetMatch(
                                path=str(path), line=line_no, kind=requested_kind,
                                identifier=clue, role="text", model=requested_model,
                                owner=owner, source=source,
                            ))
                            break
                    if len(found) >= 20:
                        break
        unique: dict[tuple[str, int, str, str], OdooTargetMatch] = {}
        for match in found:
            unique[(match.path, match.line, match.identifier, match.role)] = match
        return list(unique.values())[:20]

    def _symbols(self, path: Path, text: str) -> list[dict[str, Any]]:
        if path.suffix.lower() == ".py":
            return self._python_symbols(text)
        if path.suffix.lower() == ".xml":
            return self._xml_symbols(text)
        if path.suffix.lower() in {".js", ".ts", ".tsx"}:
            return self._js_symbols(text)
        return []

    @staticmethod
    def _python_symbols(text: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return symbols
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            model = None
            for item in node.body:
                if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"_name", "_inherit"} for target in item.targets):
                    if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                        model = item.value.value
                        symbols.append({"identifier": model, "kind": "model", "role": "definition", "line": item.lineno, "model": model})
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({"identifier": item.name, "kind": "method", "role": "definition", "line": item.lineno, "model": model})
                    for decorator in item.decorator_list:
                        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "route":
                            for arg in decorator.args:
                                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                    symbols.append({"identifier": arg.value, "kind": "route", "role": "definition", "line": item.lineno, "model": model})
                elif isinstance(item, ast.Assign):
                    is_field = isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Attribute)
                    if is_field and isinstance(item.value.func.value, ast.Name) and item.value.func.value.id == "fields":
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                symbols.append({"identifier": target.id, "kind": "field", "role": "definition", "line": item.lineno, "model": model})
        return symbols

    @staticmethod
    def _xml_symbols(text: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for match in re.finditer(r"<record\b([^>]*)>", text, re.I):
            attrs = match.group(1)
            xml_id = re.search(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", attrs)
            record_model = re.search(r"\bmodel\s*=\s*['\"]([^'\"]+)['\"]", attrs)
            if xml_id:
                model = record_model.group(1) if record_model else None
                kind = {
                    "ir.ui.view": "view", "ir.cron": "cron", "ir.ui.menu": "menu",
                    "res.groups": "security",
                }.get(model or "", "action" if (model or "").startswith("ir.actions.") else "xml_id")
                symbols.append({"identifier": xml_id.group(1), "kind": kind, "role": "definition", "line": text.count("\n", 0, match.start()) + 1, "model": model})
        for match in re.finditer(r"<(?:template|t)\b([^>]*)>", text, re.I):
            attrs = match.group(1)
            identifier = re.search(r"\b(?:id|t-name)\s*=\s*['\"]([^'\"]+)['\"]", attrs)
            if identifier:
                symbols.append({"identifier": identifier.group(1), "kind": "template", "role": "definition", "line": text.count("\n", 0, match.start()) + 1})
        for match in re.finditer(r"\b(?:ref|inherit_id|t-call|t-inherit)\s*=\s*['\"]([^'\"]+)['\"]", text):
            symbols.append({"identifier": match.group(1), "kind": "xml_id", "role": "reference", "line": text.count("\n", 0, match.start()) + 1})
        for match in re.finditer(r"<field\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"]", text, re.I):
            symbols.append({"identifier": match.group(1), "kind": "field", "role": "reference", "line": text.count("\n", 0, match.start()) + 1})
        return symbols

    @staticmethod
    def _js_symbols(text: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for pattern, kind in (
            (r"\bclass\s+([A-Za-z_$][\w$]*)", "component"),
            (r"\b(?:static\s+)?template\s*=\s*['\"]([^'\"]+)['\"]", "template"),
            (r"\bregistry\.category\([^)]*\)\.add\(\s*['\"]([^'\"]+)['\"]", "component"),
        ):
            for match in re.finditer(pattern, text):
                symbols.append({"identifier": match.group(1), "kind": kind, "role": "definition", "line": text.count("\n", 0, match.start()) + 1})
        return symbols

    @staticmethod
    def _matches(identifier: str, clue: str) -> bool:
        left = identifier.strip().lower()
        right = clue.strip().lower()
        return left == right or left.split(".")[-1] == right.split(".")[-1]

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _owner(path: Path) -> str | None:
        parts = path.parts
        for marker in ("addons", "enterprise"):
            if marker in parts:
                index = len(parts) - 1 - list(reversed(parts)).index(marker)
                return parts[index + 1] if index + 1 < len(parts) else None
        return path.parent.name

    @classmethod
    def _edit_policy(
        cls,
        module_path: Path,
        matches: list[OdooTargetMatch],
        *,
        module_scope: bool = False,
        new_target: bool = False,
    ) -> OdooTargetEditPolicy:
        """Choose where a change belongs without changing target-resolution flow."""
        if module_scope or new_target:
            return OdooTargetEditPolicy(
                ownership="client_custom",
                strategy="direct_edit",
                direct_edit_paths=[str(module_path.resolve())],
                reason="The requested work belongs to the selected client-owned custom addon.",
            )
        if not matches:
            return OdooTargetEditPolicy(
                ownership="unknown",
                strategy="clarify",
                reason="No exact source owner was established.",
            )

        ownerships = {cls._match_ownership(match, module_path) for match in matches}
        paths = list(dict.fromkeys(match.path for match in matches))
        if len(ownerships) != 1:
            return OdooTargetEditPolicy(
                ownership="mixed",
                strategy="clarify",
                protected_source_paths=[
                    match.path for match in matches
                    if cls._match_ownership(match, module_path) != "client_custom"
                ],
                reason="Focused matches span client-owned and protected source trees; select the exact owner before writing.",
            )

        ownership = ownerships.pop()
        if ownership == "client_custom":
            return OdooTargetEditPolicy(
                ownership=ownership,
                strategy="direct_edit",
                direct_edit_paths=paths,
                reason="The existing implementation is owned by the selected custom addon; edit that source directly.",
            )
        return OdooTargetEditPolicy(
            ownership=ownership,
            strategy="inherit_extension",
            protected_source_paths=paths,
            reason=(
                "The target is Odoo Core/Enterprise source and must be extended from the selected custom addon."
                if ownership == "odoo_core"
                else "The target is owned by another addon; extend it from the selected custom addon by default."
            ),
        )

    @classmethod
    def _match_ownership(cls, match: OdooTargetMatch, module_path: Path) -> str:
        path = Path(match.path)
        if match.source == "custom" or cls._is_relative_to(path, module_path):
            return "client_custom"
        normalized = path.as_posix().lower()
        if (
            "/home/odoo/src/odoo/" in normalized
            or "/home/odoo/src/enterprise/" in normalized
            or "/odoo/addons/" in normalized
            or "/enterprise/" in normalized
        ):
            return "odoo_core"
        return "third_party"

    @staticmethod
    def _live_lookup(
        *,
        rpc: OdooJsonRpcService,
        kind: str,
        identifier: str,
        model: str | None,
        module_name: str,
    ) -> tuple[str, bool]:
        if kind in {"field", "model"} and model:
            result = rpc.fields_get(model, timeout_seconds=5)
            if result.status != "ok":
                return "failed", False
            fields = result.result if isinstance(result.result, dict) else {}
            return "found" if kind == "model" or identifier in fields else "missing", kind == "model" or identifier in fields
        xml_identifier = identifier if "." in identifier else f"{module_name}.{identifier}"
        xml_module, xml_name = xml_identifier.split(".", 1)
        result = rpc.search_read(
            "ir.model.data",
            [("module", "=", xml_module), ("name", "=", xml_name)],
            ["module", "name", "model", "res_id"],
            limit=2,
            timeout_seconds=5,
        )
        if result.status != "ok":
            return "failed", False
        records = result.result if isinstance(result.result, list) else []
        return ("found", True) if records else ("missing", False)

    @classmethod
    def _cache_key(
        cls,
        project: Path,
        module_name: str,
        mode: str,
        kind: str,
        identifier: str | None,
        model: str | None,
        task: str,
        paths: list[Path],
        planned_paths: list[str],
        require_live: bool | None,
        client_name: str | None,
    ) -> str:
        stamps = []
        for path in paths:
            try:
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        head = cls._git_head(project)
        payload = [
            str(project.resolve()), head, client_name, module_name, mode, kind,
            identifier, model, task, stamps, planned_paths, require_live,
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _git_head(project: Path) -> str | None:
        git_dir = project / ".git"
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                ref = git_dir / head[5:]
                return ref.read_text(encoding="utf-8").strip()
            return head
        except OSError:
            return None

    @classmethod
    def _cache_get(cls, key: str) -> OdooTargetResolutionResult | None:
        now = time.monotonic()
        with cls._cache_lock:
            item = cls._cache.get(key)
            if not item or now - item[0] > cls._cache_ttl_seconds:
                cls._cache.pop(key, None)
                return None
            return deepcopy(item[1])

    @classmethod
    def _finish(
        cls,
        key: str,
        result: OdooTargetResolutionResult,
        started: float,
    ) -> OdooTargetResolutionResult:
        result.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        with cls._cache_lock:
            cls._cache[key] = (time.monotonic(), deepcopy(result))
            while len(cls._cache) > cls._cache_limit:
                cls._cache.pop(next(iter(cls._cache)))
        return result

    @staticmethod
    def _result(**kwargs: Any) -> OdooTargetResolutionResult:
        return OdooTargetResolutionResult(**kwargs)

    @staticmethod
    def _reason(status: str, mode: str) -> str:
        reasons = {
            "verified": f"The {mode} target is unambiguous in focused source and applicable live state.",
            "source_verified_live_unverified": "Focused source verified the target; live Odoo was unavailable and was not retried.",
            "source_verified_live_missing": "Focused source verified the target, but it is absent from the current live registry/database.",
            "collision": "The asserted new target already exists.",
        }
        return reasons.get(status, "The bounded target-resolution gate completed.")
