from __future__ import annotations

import base64
import json
import shlex
from typing import Any

from ..config import settings
from ..schemas import CommandResult, OdooResult
from .client_resolver_service import ClientResolverService, ResolvedClient
from .odoosh_service import OdooShService
from .odoo_jsonrpc_service import OdooJsonRpcService


class OdooService:
    """Odoo operations over Odoo.sh SSH / ``odoo-bin shell`` only.

    This service intentionally does not use legacy remote transport. All database reads and
    module operations execute inside the selected Odoo.sh build via SSH so
    concurrent multi-client requests stay isolated by ``ResolvedClient``.
    """

    def __init__(self, client: ResolvedClient | None = None) -> None:
        self.client = client

    def _active_client(self) -> ResolvedClient | None:
        if self.client:
            return self.client
        try:
            return ClientResolverService().resolve_client(require_complete=False)
        except Exception:
            return None

    def _ssh(self) -> OdooShService:
        return OdooShService(self._active_client())

    def _database(self) -> str | None:
        client = self._active_client()
        return (client.odoosh_db_name if client else None) or (client.odoo_db if client else None) or settings.odoosh_db_name

    def health(self) -> OdooResult:
        result = self.search_read("ir.module.module", [], ["name", "state"], limit=1)
        if result.status == "ok":
            return OdooResult(status="ok", database=self._database(), result={"message": "Odoo shell health check passed.", "sample": result.result})
        return result

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str | None = None,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 120,
    ) -> OdooResult:
        try:
            ssh_result = self._ssh().search_read(model, domain or [], fields or [], limit, offset, order, context, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            ssh_result = OdooResult(status="failed", database=self._database(), error=f"Odoo SSH query failed before execution: {exc}")
        if ssh_result.status == "ok":
            return ssh_result

        # Quick resilience fix: for read-only data queries, fall back to Odoo's
        # HTTPS JSON-RPC endpoint when direct Odoo.sh SSH is refused/unavailable
        # for a specific build. This does not affect deploy/install/upgrade.
        https_result = OdooJsonRpcService(self._active_client()).search_read(
            model, domain or [], fields or [], limit, offset, order, context, timeout_seconds=min(max(timeout_seconds, 15), 60)
        )
        if https_result.status == "ok":
            if isinstance(https_result.result, list):
                return https_result
            return OdooResult(status="ok", base_url=https_result.base_url, database=https_result.database, result=https_result.result)

        return OdooResult(
            status="failed",
            base_url=https_result.base_url,
            database=self._database(),
            error=(
                "Odoo SSH query failed and HTTPS JSON-RPC fallback also failed. "
                f"SSH error: {ssh_result.error or 'unknown'}; HTTPS error: {https_result.error or 'unknown'}"
            ),
            result={
                "ssh_attempt": ssh_result.model_dump(mode="json"),
                "https_jsonrpc_attempt": https_result.model_dump(mode="json"),
            },
        )

    def read_group(
        self,
        model: str,
        domain: list | None,
        fields: list[str],
        groupby: list[str],
        lazy: bool = False,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 120,
    ) -> OdooResult:
        try:
            ssh_result = self._ssh().read_group(model, domain or [], fields, groupby, lazy, context, timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            ssh_result = OdooResult(status="failed", database=self._database(), error=f"Odoo SSH read_group failed before execution: {exc}")
        if ssh_result.status == "ok":
            return ssh_result
        https_result = OdooJsonRpcService(self._active_client()).read_group(
            model, domain or [], fields, groupby, lazy, context, timeout_seconds=min(max(timeout_seconds, 15), 60)
        )
        if https_result.status == "ok":
            return https_result
        return OdooResult(
            status="failed",
            base_url=https_result.base_url,
            database=self._database(),
            error=(
                "Odoo SSH read_group failed and HTTPS JSON-RPC fallback also failed. "
                f"SSH error: {ssh_result.error or 'unknown'}; HTTPS error: {https_result.error or 'unknown'}"
            ),
            result={"ssh_attempt": ssh_result.model_dump(mode="json"), "https_jsonrpc_attempt": https_result.model_dump(mode="json")},
        )

    def fields_get(self, model: str, limit: int = 500) -> OdooResult:
        """Read-only discovery of one model's fields from the live registry.

        Implemented as a bounded ``ir.model.fields`` search_read so it uses the
        same SSH-first / HTTPS JSON-RPC fallback transport as every other read.
        """
        safe_model = str(model or "").strip()
        if not safe_model or "\x00" in safe_model:
            return OdooResult(status="failed", database=self._database(), error="model is required for odoo.fields_get")
        return self.search_read(
            "ir.model.fields",
            [["model", "=", safe_model]],
            ["name", "ttype", "field_description", "relation", "required", "readonly", "store"],
            limit=max(1, min(int(limit or 500), 2000)),
            offset=0,
            order="name asc",
            context=None,
            timeout_seconds=120,
        )

    def list_models(self, search: str | None = None, include_transient: bool = True, limit: int = 200) -> OdooResult:
        """Read-only discovery of the Odoo data model from ir.model."""
        domain: list[Any] = []
        if search:
            domain.append(["model", "ilike", str(search).strip()])
        if not include_transient:
            domain.append(["transient", "=", False])
        return self.search_read(
            "ir.model",
            domain,
            ["model", "name", "transient", "modules"],
            limit=max(1, min(int(limit or 200), 2000)),
            offset=0,
            order="model asc",
            context=None,
            timeout_seconds=120,
        )

    def feature_inspect(
        self,
        models: list[str],
        sample_fields: dict[str, list[str]] | None = None,
        sample_limit: int = 0,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        """Run a compact multi-model registry/UI/security inspection in one SSH shell."""
        try:
            return self._ssh().feature_inspect(
                models,
                sample_fields=sample_fields,
                sample_limit=sample_limit,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return OdooResult(
                status="failed",
                database=self._database(),
                error=f"Odoo SSH feature inspection failed before execution: {exc}",
            )


    def execute_kw(self, model: str, method: str, args: list | None = None, kwargs: dict | None = None, superuser: bool = False) -> OdooResult:
        """Compatibility wrapper for old callers/tests; executed through Odoo shell only."""
        args = args or []
        kwargs = kwargs or {}
        if method == "search_read":
            domain = args[0] if args else kwargs.get("domain", [])
            fields = kwargs.get("fields", args[1] if len(args) > 1 else [])
            limit = kwargs.get("limit", 20)
            offset = kwargs.get("offset", 0)
            order = kwargs.get("order")
            context = kwargs.get("context")
            return self.search_read(model, domain, fields, limit, offset, order, context)
        return OdooResult(status="failed", database=self._database(), error=f"Unsupported shell execute_kw compatibility call: {model}.{method}")

    def module_status(self, module_name: str) -> OdooResult:
        return self.search_read(
            "ir.module.module",
            [["name", "=", module_name]],
            ["name", "state", "latest_version", "installed_version", "summary"],
            limit=1,
        )

    def module_dependencies_status(self, module_name: str) -> OdooResult:
        payload_b64 = base64.b64encode(json.dumps({"module": module_name}).encode("utf-8")).decode("ascii")
        marker = "MCP_ODOO_DEPS_JSON:"
        ssh = self._ssh()
        safe_db = shlex.quote(ssh._db_name() or "")
        odoo_path = shlex.quote(ssh._odoo_path())
        addons_setup = ssh._addons_path_shell_setup()
        script = (
            "set +e; "
            f"{addons_setup}"
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path=\"$MCP_ADDONS_PATH\" --no-http --log-level=error <<'PY'\n"
            "import base64, json, traceback\n"
            f"payload = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
            f"marker = {marker!r}\n"
            "try:\n"
            "    module_name = payload['module']\n"
            "    mod = env['ir.module.module'].search([('name', '=', module_name)], limit=1)\n"
            "    if not mod:\n"
            "        data = {'status': 'failed', 'error': 'Module not found in apps registry: %s' % module_name}\n"
            "    else:\n"
            "        dep_names = sorted(set(mod.dependencies_id.mapped('name')))\n"
            "        rows = env['ir.module.module'].search_read([('name', 'in', dep_names)], ['name', 'state']) if dep_names else []\n"
            "        by_name = {row['name']: row for row in rows}\n"
            "        missing = []\n"
            "        installed = []\n"
            "        for name in dep_names:\n"
            "            row = by_name.get(name)\n"
            "            if not row or row.get('state') != 'installed':\n"
            "                missing.append({'name': name, 'state': row.get('state') if row else 'not_found'})\n"
            "            else:\n"
            "                installed.append({'name': name, 'state': row.get('state')})\n"
            "        data = {'status': 'ok', 'result': {'module': module_name, 'dependencies': dep_names, 'installed': installed, 'missing': missing}}\n"
            "except Exception as exc:\n"
            "    data = {'status': 'failed', 'error': str(exc), 'traceback': traceback.format_exc()[-4000:]}\n"
            "print(marker + json.dumps(data, ensure_ascii=False))\n"
            "PY\n"
        )
        result = ssh.run_shell(script, timeout_seconds=120)
        parsed = self._parse_marker(result, marker)
        if not parsed:
            return OdooResult(status="failed", database=self._database(), error=(result.stderr or result.stdout or "Dependency probe returned no JSON.")[-4000:], result={"ssh": result.model_dump(mode="json")})
        if parsed.get("status") != "ok" or result.exit_code != 0:
            return OdooResult(status="failed", database=self._database(), error=parsed.get("error") or result.stderr or "Dependency probe failed.", result={"payload": parsed, "ssh": result.model_dump(mode="json")})
        return OdooResult(status="ok", database=self._database(), result=parsed.get("result"))

    def ensure_dependencies(self, module_name: str) -> OdooResult:
        deps = self.module_dependencies_status(module_name)
        if deps.status != "ok" or not isinstance(deps.result, dict):
            return deps
        missing = deps.result.get("missing") or []
        if not missing:
            return OdooResult(status="ok", database=self._database(), result={"module": module_name, "installed": [], "message": "All dependencies are already installed."})
        installed = []
        failures = []
        ssh = self._ssh()
        for row in missing:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            command_result = ssh.module_deploy_ssh(name, timeout_seconds=600)
            item = {"name": name, "result": command_result.model_dump(mode="json")}
            if command_result.exit_code == 0:
                installed.append(item)
            else:
                failures.append(item)
        if failures:
            return OdooResult(status="failed", database=self._database(), error="One or more dependencies failed to install/upgrade through Odoo.sh SSH.", result={"installed": installed, "failures": failures})
        return OdooResult(status="ok", database=self._database(), result={"module": module_name, "installed": installed})

    def module_install(self, module_name: str) -> OdooResult:
        return self._command_result("module_install", self._ssh().module_install_ssh(module_name, timeout_seconds=600))

    def module_update(self, module_name: str) -> OdooResult:
        return self._command_result("module_update", self._ssh().module_update(module_name, timeout_seconds=600))

    def module_deploy(self, module_name: str) -> OdooResult:
        result = self._command_result("module_deploy", self._ssh().module_deploy_ssh(module_name, timeout_seconds=600))
        if result.result is None:
            result.result = {}
        if isinstance(result.result, dict):
            result.result.setdefault("fallback", "odoosh")
            result.result.setdefault("strategy", "hybrid_psql_state_then_single_cli_with_single_shell_fallback")
            if result.result.get("operation") == "module_deploy":
                result.result["operation"] = "deploy"
            result.result.setdefault("cause", "Used the Odoo.sh SSH hybrid deploy engine: lightweight SQL state probe plus one Odoo process, or one-process Odoo shell fallback.")
        return result

    def registry_smoke_test(self, module_name: str, model_names: list[str] | None = None, menu_names: list[str] | None = None, strict: bool = True) -> OdooResult:
        checks: dict[str, Any] = {"module": module_name, "models": [], "menus": [], "module_status": None}
        failures: list[str] = []
        status_result = self.module_status(module_name)
        checks["module_status"] = status_result.model_dump(mode="json")
        if status_result.status != "ok":
            failures.append(status_result.error or "module status failed")

        models = list(model_names or [])
        if not models:
            prefix = module_name.replace("_", ".")
            inferred = self.search_read("ir.model", [["model", "=ilike", prefix + ".%"]], ["model", "name"], 200)
            if inferred.status == "ok" and isinstance(inferred.result, list):
                models = [str(row.get("model")) for row in inferred.result if row.get("model")]
        for model_name in models:
            exists = self.search_read("ir.model", [["model", "=", model_name]], ["model", "name"], 1)
            item: dict[str, Any] = {"model": model_name, "exists": False, "search_read_ok": False}
            if exists.status == "ok" and exists.result:
                item["exists"] = True
                sr = self.search_read(model_name, [], ["id"], 1)
                item["search_read_ok"] = sr.status == "ok"
                if sr.status != "ok":
                    failures.append(f"Model {model_name} exists but search_read failed: {sr.error}")
            elif strict:
                failures.append(f"Model {model_name} not found")
            checks["models"].append(item)

        for menu_name in menu_names or []:
            menu = self.search_read("ir.ui.menu", [["name", "=", menu_name]], ["name", "complete_name", "action", "active"], 20)
            item = {"menu": menu_name, "exists": bool(menu.status == "ok" and menu.result), "result": menu.result if menu.status == "ok" else None, "error": menu.error}
            checks["menus"].append(item)
            if strict and not item["exists"]:
                failures.append(f"Menu {menu_name} not found")

        if failures:
            return OdooResult(status="failed", database=self._database(), result=checks, error="; ".join(failures))
        return OdooResult(status="ok", database=self._database(), result=checks)

    @staticmethod
    def _parse_marker(result: CommandResult, marker: str) -> dict[str, Any] | None:
        for line in reversed((result.stdout or "").splitlines()):
            if line.startswith(marker):
                try:
                    parsed = json.loads(line[len(marker):])
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    return None
        return None

    def _command_result(self, operation: str, result: CommandResult) -> OdooResult:
        payload = {"operation": operation, "ssh": result.model_dump(mode="json")}
        marker = "MCP_MODULE_DEPLOY_DIAGNOSTIC:"
        for line in reversed(((result.stderr or "") + "\n" + (result.stdout or "")).splitlines()):
            if marker not in line:
                continue
            try:
                diagnostic = json.loads(line.split(marker, 1)[1])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(diagnostic, dict):
                payload["deploy_diagnostic"] = diagnostic
                break
        if result.exit_code == 0:
            return OdooResult(status="ok", database=self._database(), result=payload)
        return OdooResult(status="failed", database=self._database(), error=(result.stderr or result.stdout or f"{operation} failed")[-4000:], result=payload)
