"""Source/runtime facts with explicit provenance and configuration/SHA invalidation."""

import ast
import hashlib
import json
from pathlib import Path
import time

from fastapi import HTTPException
from ..config import settings
from .client_resolver_service import ClientResolverService
from .client_settings_store import atomic_json
from .execution_service import git
from .odoo_jsonrpc_service import OdooJsonRpcService
from .path_service import resolve_allowed_path
from .repo_service import RepoService


class ClientKnowledgeService:
    def read(self, client, detail, refresh):
        sha = None
        clean = False
        source_error = None
        project = None
        try:
            project = resolve_allowed_path(client["path"])
            remote = git(["remote", "get-url", "origin"], project, timeout=3)
            if not RepoService()._remote_matches(remote, client["repo"]):
                raise HTTPException(409, "Checkout remote differs from client")
            sha = git(["rev-parse", "HEAD"], project, timeout=3).strip()
            clean = not git(["status", "--porcelain"], project, timeout=3).strip()
        except (HTTPException, OSError, KeyError):
            source_error = "Configured source checkout is unavailable or does not match this client."
        key = hashlib.sha256(client["name"].casefold().encode()).hexdigest()
        path = settings.state_path / "client-knowledge" / (key + ".json")
        cached = None
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        valid = bool(cached and cached.get("configuration_revision") == detail["revision"] and cached.get("source_sha") == sha and clean and time.time() - cached.get("captured_epoch", 0) < 1800)
        if valid and not refresh:
            return {**cached, "cache_hit": True, "fresh": True}
        result = {
            "client_name": client["name"],
            "branch": client.get("branch"),
            "environment": client.get("environment"),
            "configuration_revision": detail["revision"],
            "source_sha": sha,
            "source_clean": clean,
            "validation": detail["validation"],
            "cache_hit": False,
            "fresh": False,
            "runtime_facts": None,
            "source_modules": [],
            "source_error": source_error,
            "next_action": "Call task_knowledge(refresh=true) for bounded source/runtime discovery.",
        }
        if not refresh:
            return result
        if project and sha and clean and not source_error:
            names = git(["ls-files", "--", "*__manifest__.py"], project, timeout=3).splitlines()
            result["source_truncated"] = len(names) > 200
            for name in names[:200]:
                file = project / name
                if not file.resolve().is_relative_to(project.resolve()) or file.stat().st_size > 100000:
                    continue
                try:
                    manifest = ast.literal_eval(file.read_text())
                    if not isinstance(manifest, dict):
                        continue
                    result["source_modules"].append(
                        {
                            "module": file.parent.name,
                            "path": name,
                            "declared_version": str(manifest.get("version", ""))[:100],
                            "depends": [str(d)[:100] for d in manifest.get("depends", [])[:100]],
                            "provenance": "git_manifest_at_source_sha",
                        }
                    )
                except (OSError, ValueError, SyntaxError, TypeError):
                    continue
        resolved = ClientResolverService().resolve_client(client["name"], require_complete=False)
        rpc = OdooJsonRpcService(resolved)
        if rpc.configured():
            response = rpc.search_read("ir.module.module", [["state", "=", "installed"]], ["name", "latest_version"], limit=201, timeout_seconds=10)
            if response.status == "ok" and isinstance(response.result, list):
                rows = response.result
                result["runtime_facts"] = {"installed_modules": rows[:200], "truncated": len(rows) > 200, "provenance": "authenticated_odoo_https"}
                # Query base explicitly: a bounded module page need not include it.
                base = rpc.search_read("ir.module.module", [["name", "=", "base"], ["state", "=", "installed"]], ["name", "latest_version"], limit=1, timeout_seconds=5)
                if base.status == "ok" and isinstance(base.result, list) and base.result:
                    result["runtime_facts"]["base_module_version"] = base.result[0].get("latest_version")
            else:
                result["runtime_error"] = "Authenticated module discovery failed; no runtime version/modules were assumed."
        else:
            result["runtime_error"] = "Odoo HTTPS credentials are not configured."
        result.update(
            fresh=clean, captured_epoch=time.time(), next_action="Facts apply only to this configuration/source SHA. Installed module facts are a bounded runtime snapshot, not execution proof."
        )
        if clean:
            atomic_json(path, result)
        return result
