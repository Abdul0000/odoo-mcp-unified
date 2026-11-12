from __future__ import annotations

import ast
import re
from collections import deque
from pathlib import Path
from typing import Any


class ReaImpactService:
    """Predict likely addon change impact from source graph and proposed diff evidence."""

    MAX_CHANGED_FILES = 30
    MAX_DIFF_CHARS = 20000
    MAX_RESULTS = 100
    _RISK_PATHS = {
        "models": "high", "security": "high", "data": "high",
        "controllers": "high", "payment": "high", "stock": "high",
        "account": "high", "views": "medium", "report": "medium",
    }

    @staticmethod
    def _safe_relative(value: str) -> str:
        clean = str(value or "").replace("\\", "/").strip()
        path = Path(clean)
        if not clean or path.is_absolute() or any(part in {"", ".", ".."} for part in clean.split("/")):
            raise ValueError("changed file paths must be safe addon-relative paths")
        return path.as_posix()

    @staticmethod
    def _diff_symbols(diff: str) -> set[str]:
        symbols: set[str] = set()
        for line in diff.splitlines()[:1000]:
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            text = line[1:]
            for match in re.finditer(r"\b(?:class|def|async\s+def)\s+([A-Za-z_][A-Za-z0-9_]*)", text):
                symbols.add(match.group(1))
            for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", text):
                symbols.add(match.group(1))
            for match in re.finditer(r"""['"](?:name|model|inherit_id|res_model)['"]\s*[,=:]\s*['"]([A-Za-z_][A-Za-z0-9_.]*)""", text):
                symbols.add(match.group(1))
        return symbols

    @staticmethod
    def _source_symbols(source: str) -> set[str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(node.name)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                found.update(t.id for t in targets if isinstance(t, ast.Name))
        return found

    def predict(
        self, *, module_path: Path, module_name: str,
        changed_files: list[dict[str, Any]], graph: dict[str, Any] | None = None,
        runtime_events: list[dict[str, Any]] | None = None,
        upstream_findings: list[dict[str, Any]] | None = None,
        api_findings: list[dict[str, Any]] | None = None,
        dependency_map: dict[str, Any] | None = None,
        runtime_registry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        root = module_path.resolve(strict=True)
        if not root.is_dir() or not (root / "__manifest__.py").is_file():
            raise ValueError("Valid addon directory and manifest required")
        if not isinstance(changed_files, list) or not changed_files or len(changed_files) > self.MAX_CHANGED_FILES:
            raise ValueError("changed_files must contain between 1 and 30 entries")

        graph = graph or {}
        dependency_map = dependency_map or {}
        runtime_registry = runtime_registry or {"status": "skipped", "evidence_level": "static_only"}
        runtime_summary = runtime_registry.get("summary") or {}
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        node_by_id = {str(n.get("id")): n for n in nodes if n.get("id")}
        adjacency: dict[str, set[str]] = {}
        path_nodes: dict[str, set[str]] = {}
        for edge in edges:
            source, target = str(edge.get("source", "")), str(edge.get("target", ""))
            if source and target:
                adjacency.setdefault(source, set()).add(target)
                adjacency.setdefault(target, set()).add(source)
            evidence = edge.get("evidence") or {}
            evidence_path = evidence.get("path")
            if evidence_path:
                path_nodes.setdefault(str(evidence_path), set()).update(x for x in (source, target) if x)

        normalized = []
        seeds: set[str] = set()
        changed_symbols: set[str] = set()
        warnings = []
        for item in changed_files:
            if not isinstance(item, dict):
                warnings.append({"code": "INVALID_CHANGE_ENTRY"})
                continue
            rel = self._safe_relative(item.get("path", ""))
            diff = str(item.get("diff") or "")
            if len(diff) > self.MAX_DIFF_CHARS:
                raise ValueError(f"diff for {rel} exceeds {self.MAX_DIFF_CHARS} characters")
            file_path = (root / rel).resolve()
            try:
                file_path.relative_to(root)
            except ValueError as exc:
                raise ValueError("changed file path escapes addon root") from exc
            if file_path.exists() and not file_path.is_file():
                raise ValueError(f"changed path is not a file: {rel}")
            source = file_path.read_text(encoding="utf-8", errors="replace")[:256000] if file_path.is_file() else ""
            symbols = self._diff_symbols(diff) | self._source_symbols(source)
            changed_symbols.update(symbols)
            seeds.update(path_nodes.get(rel, set()))
            for node in nodes:
                node_id = str(node.get("id", ""))
                name = str(node.get("name", ""))
                if name in symbols or node_id.rsplit(".", 1)[-1] in symbols:
                    seeds.add(node_id)
                if node.get("source") == rel:
                    seeds.add(node_id)
            normalized.append({
                "path": rel,
                "change_type": str(item.get("change_type") or "modify")[:30],
                "diff_supplied": bool(diff),
                "symbols": sorted(symbols)[:100],
            })

        impacted: dict[str, dict[str, Any]] = {}
        queue = deque((seed, 0) for seed in seeds)
        visited = set(seeds)
        while queue and len(impacted) < self.MAX_RESULTS:
            current, depth = queue.popleft()
            node = node_by_id.get(current)
            if node and current not in impacted:
                impacted[current] = {"id": current, "kind": node.get("kind"), "name": node.get("name"),
                                     "model": node.get("model"), "depth": depth}
            if depth >= 2:
                continue
            for neighbour in adjacency.get(current, set()):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append((neighbour, depth + 1))

        risk_reasons = []
        risk = "low"
        for change in normalized:
            parts = set(Path(change["path"]).parts)
            for part, level in self._RISK_PATHS.items():
                if part in parts:
                    risk_reasons.append(f"{change['path']}: {part} changes can affect business-critical behavior")
                    if level == "high":
                        risk = "high"
                    elif risk == "low":
                        risk = "medium"
            suffix = Path(change["path"]).suffix.lower()
            if suffix in {".py", ".xml"} and not change["diff_supplied"]:
                risk_reasons.append(f"{change['path']}: no diff supplied; impact matching relies on current source and graph")
        if len(normalized) >= 8 and risk != "high":
            risk = "medium"
            risk_reasons.append("Multiple files changed in one addon increase integration risk")
        if not seeds:
            risk_reasons.append("No graph nodes matched the supplied paths or symbols; prediction coverage is limited")
        elif len(impacted) >= 20 and risk == "low":
            risk = "medium"
            risk_reasons.append("The change is connected to a broad set of addon components")

        upstream_findings = upstream_findings or []
        api_findings = api_findings or []
        upstream_matches = [
            item for item in upstream_findings
            if isinstance(item, dict) and (
                item.get("symbol") in changed_symbols or item.get("model") in changed_symbols
            )
        ][:50]
        api_matches = [
            item for item in api_findings
            if isinstance(item, dict) and (item.get("evidence") or {}).get("path") in {x["path"] for x in normalized}
        ][:50]
        if upstream_matches:
            risk_reasons.append("Changed symbols have upstream signature or field compatibility findings")
            if risk == "low":
                risk = "medium"
        if api_matches:
            risk_reasons.append("Changed files contain external API or webhook integration points")
            if risk == "low":
                risk = "medium"

        runtime_matches = []
        for event_index, event in enumerate((runtime_events or [])[:2000]):
            if not isinstance(event, dict):
                continue
            target = ".".join(str(event.get(k) or "") for k in ("model", "method")).strip(".")
            if target and (target in changed_symbols or target.rsplit(".", 1)[-1] in changed_symbols):
                runtime_matches.append({"event_index": event_index, "target": target,
                                        "duration_ms": event.get("duration_ms")})
        if runtime_matches:
            risk_reasons.append("Changed symbols appear in supplied runtime trace evidence")
            if risk == "low":
                risk = "medium"

        reverse_dependents = dependency_map.get("reverse_dependents", [])
        cross_module_relationships = dependency_map.get("cross_module_relationships", [])
        dependency_cycles = dependency_map.get("dependency_cycles", [])
        cycle_modules = set(dependency_map.get("cycle_modules", []))
        affected_modules = [{"module": module_name, "depth": 0, "reason": "changed addon"}]
        affected_modules.extend({
            "module": item.get("module"), "depth": item.get("depth"),
            "reason": "reverse dependency; may consume changed addon behavior",
        } for item in reverse_dependents if isinstance(item, dict) and item.get("module"))
        if reverse_dependents:
            risk_reasons.append(
                f"Changes may affect {len(reverse_dependents)} reverse-dependent addon(s) in this repository"
            )
            if risk == "low":
                risk = "medium"
            if len(reverse_dependents) >= 8:
                risk = "high"
        if module_name in cycle_modules:
            risk_reasons.append("The addon participates in a dependency cycle; change propagation is harder to isolate")
            risk = "high"
        if cross_module_relationships and risk == "low":
            risk = "medium"
            risk_reasons.append("Cross-module model inheritance or relational-field links were found")

        return {
            "status": "predicted",
            "module_name": module_name,
            "risk": risk,
            "summary": {
                "changed_files": len(normalized),
                "changed_symbols": len(changed_symbols),
                "seed_nodes": len(seeds),
                "impacted_components": len(impacted),
                "runtime_trace_matches": len(runtime_matches),
                "upstream_matches": len(upstream_matches),
                "api_integration_matches": len(api_matches),
                "reverse_dependent_addons": len(reverse_dependents),
                "cross_module_relationships": len(cross_module_relationships),
                "dependency_cycles": len(dependency_cycles),
                "graph_complete": bool((graph.get("coverage") or {}).get("complete", False)),
                "dependency_map_complete": bool((dependency_map.get("coverage") or {}).get("complete", False)),
                "runtime_registry_confirmed_models": runtime_summary.get("confirmed_models", 0),
                "runtime_installed_modules": runtime_summary.get("installed_modules", 0),
                "runtime_registry_complete": bool((runtime_registry.get("coverage") or {}).get("complete", False)),
            },
            "changed_files": normalized,
            "impacted_components": sorted(impacted.values(), key=lambda x: (x["depth"], x["kind"] or "", x["name"] or "")),
            "affected_modules": affected_modules[:100],
            "cross_module_relationships": cross_module_relationships[:100],
            "dependency_cycles": dependency_cycles[:30],
            "risk_reasons": list(dict.fromkeys(risk_reasons))[:50],
            "runtime_trace_matches": runtime_matches[:50],
            "runtime_registry": runtime_registry,
            "upstream_matches": upstream_matches,
            "api_integration_matches": api_matches,
            "warnings": warnings,
            "limitations": [
                "Prediction is static and advisory; it does not prove runtime impact.",
                "Only supplied changed-file paths and diff text are treated as the proposed change.",
                "Graph omissions, dynamic dispatch, inherited behavior and third-party addons can hide dependencies.",
                "Validate high-risk predictions with focused tests and an approved staging workflow.",
            ],
        }
