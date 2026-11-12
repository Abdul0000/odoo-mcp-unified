"""Phase 10: compare static REA dependency evidence with live Odoo registry metadata.

This service is a pure, bounded normalizer. The caller performs read-only Odoo
queries through the existing OdooService transport; no SQL or writes occur here.
"""
from __future__ import annotations

from typing import Any


class ReaRuntimeRegistryService:
    MAX_MODELS = 250
    MAX_FIELDS = 3000
    MAX_MODULES = 150

    @staticmethod
    def _module_names(value: Any) -> list[str]:
        if isinstance(value, list):
            parts = value
        else:
            parts = str(value or "").replace(";", ",").split(",")
        return sorted({str(item).strip() for item in parts if str(item).strip()})

    def analyze(
        self,
        *,
        dependencies: dict[str, Any],
        model_rows: list[dict[str, Any]],
        field_rows: list[dict[str, Any]],
        module_rows: list[dict[str, Any]],
        query_errors: dict[str, str] | None = None,
        requested_model_count: int = 0,
        requested_module_count: int = 0,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        query_errors = query_errors or {}
        static_owners: dict[str, set[str]] = {}
        for item in dependencies.get("model_ownership", []):
            if isinstance(item, dict) and item.get("model"):
                static_owners.setdefault(str(item["model"]), set()).add(str(item.get("module") or ""))

        runtime_models = {
            str(row.get("model")): row
            for row in model_rows[: self.MAX_MODELS]
            if isinstance(row, dict) and row.get("model")
        }
        runtime_modules = {
            str(row.get("name")): row
            for row in module_rows[: self.MAX_MODULES]
            if isinstance(row, dict) and row.get("name")
        }
        runtime_fields = {
            (str(row.get("model")), str(row.get("name"))): row
            for row in field_rows[: self.MAX_FIELDS]
            if isinstance(row, dict) and row.get("model") and row.get("name")
        }

        model_evidence = []
        for model, owners in sorted(static_owners.items()):
            live = runtime_models.get(model)
            live_owners = self._module_names(live.get("modules")) if live else []
            model_evidence.append({
                "model": model,
                "static_modules": sorted(owner for owner in owners if owner),
                "runtime_present": live is not None,
                "runtime_modules": live_owners,
                "owner_match": bool(owners.intersection(live_owners)),
                "runtime_label": live.get("name") if live else None,
            })

        field_evidence = []
        for relation in dependencies.get("cross_module_relationships", []):
            if not isinstance(relation, dict) or relation.get("kind") != "relates_to_model":
                continue
            model = str(relation.get("model") or relation.get("source_model") or "")
            field_name = str(relation.get("field") or relation.get("field_name") or "")
            static_comodel = relation.get("comodel") or relation.get("target_model")
            if not model or not field_name:
                continue
            live = runtime_fields.get((model, field_name))
            field_evidence.append({
                "model": model,
                "field": field_name,
                "static_comodel": static_comodel,
                "runtime_present": live is not None,
                "runtime_type": live.get("ttype") if live else None,
                "runtime_relation": live.get("relation") if live else None,
                "relation_match": bool(live and live.get("relation") == static_comodel),
            })

        static_modules = {
            str(row.get("module"))
            for key in ("transitive_dependencies", "reverse_dependents")
            for row in dependencies.get(key, [])
            if isinstance(row, dict) and row.get("module")
        }
        static_modules.add(str(dependencies.get("module_name") or ""))
        for relation in dependencies.get("cross_module_relationships", []):
            if isinstance(relation, dict):
                static_modules.update(
                    str(relation[key])
                    for key in ("source_module", "target_module")
                    if relation.get(key)
                )
        module_evidence = []
        for module in sorted(name for name in static_modules if name):
            live = runtime_modules.get(module)
            module_evidence.append({
                "module": module,
                "runtime_found": live is not None,
                "state": live.get("state") if live else None,
                "installed": bool(live and live.get("state") == "installed"),
                "latest_version": live.get("latest_version") if live else None,
            })

        errors = {str(key): str(value)[:400] for key, value in query_errors.items()}
        complete = (
            not errors
            and requested_model_count <= self.MAX_MODELS
            and requested_module_count <= self.MAX_MODULES
            and len(field_rows) < self.MAX_FIELDS
            and len(model_rows) < self.MAX_MODELS
            and len(module_rows) < self.MAX_MODULES
        )
        return {
            "status": "confirmed" if not errors else "partial",
            "evidence_level": "live_registry",
            "client_name": client_name,
            "summary": {
                "static_models_checked": len(static_owners),
                "confirmed_models": sum(1 for row in model_evidence if row["runtime_present"]),
                "owner_matches": sum(1 for row in model_evidence if row["owner_match"]),
                "static_fields_checked": len(field_evidence),
                "confirmed_fields": sum(1 for row in field_evidence if row["runtime_present"]),
                "relation_matches": sum(1 for row in field_evidence if row["relation_match"]),
                "static_modules_checked": len(module_evidence),
                "installed_modules": sum(1 for row in module_evidence if row["installed"]),
                "uninstalled_or_missing_modules": sum(1 for row in module_evidence if not row["installed"]),
            },
            "models": model_evidence[: self.MAX_MODELS],
            "fields": field_evidence[: self.MAX_FIELDS],
            "modules": module_evidence[: self.MAX_MODULES],
            "query_errors": errors,
            "coverage": {
                "complete": complete,
                "models_truncated": requested_model_count > self.MAX_MODELS or len(model_rows) >= self.MAX_MODELS,
                "fields_truncated": len(field_rows) >= self.MAX_FIELDS,
                "modules_truncated": requested_module_count > self.MAX_MODULES or len(module_rows) >= self.MAX_MODULES,
            },
            "limitations": [
                "Registry presence confirms metadata in this database, not that a method or code path executed.",
                "ir.model.modules is Odoo registry ownership metadata and can differ from a repository's static addon inference.",
                "A missing model/module is unresolved when access rights, query failures, or coverage truncation are present.",
            ],
        }
