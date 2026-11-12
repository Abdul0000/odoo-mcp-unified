from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import get_odoo_clients, normalise_client, safe_client_public, settings
from .client_resolver_service import ClientResolverService, ResolvedClient
from .odoo_jsonrpc_service import OdooJsonRpcService
from .odoosh_service import OdooShService
from .git_service import GitService
from .repo_service import RepoService
from .store_service import JsonStore


PLACEHOLDER_RE = re.compile(r"^(CHANGE_ME|YOUR_|CLIENT_2_|ODOO_|ODOOSH_|GITHUB_|PRIVATE_KEY|TOKEN|PASSWORD)|example\.com|client-2", re.I)


class ClientGateService:
    """Client selection and validation gate for multi-client MCP deployments."""

    VALIDATION_TTL_SECONDS = 1800
    SESSION_VALIDATED_KEY = "client_validated"
    SESSION_ACTIVE_KEY = "active_client_name"

    def __init__(self, store: JsonStore | None = None) -> None:
        self.store = store or JsonStore()
        self.resolver = ClientResolverService()

    def check(self, client_name: str | None = None, validate: bool = False, force_refresh: bool = False) -> dict[str, Any]:
        clients = [normalise_client(item) for item in get_odoo_clients()]
        public_clients = [safe_client_public(item) for item in clients]
        names = [item.get("name") or f"ODOO_CLIENTS[{idx}]" for idx, item in enumerate(clients)]

        if not clients:
            return {
                "ok": True,
                "requires_client_selection": False,
                "requires_validation": False,
                "mode": "legacy_env",
                "summary": "No ODOO_CLIENTS entries configured; using legacy single-client environment fallback.",
                "clients": [],
                "next_call": {"tool": "workflow.agent_instructions", "arguments": {}},
            }

        if not client_name:
            if len(clients) == 1:
                selected = clients[0]
                selected_name = selected.get("name") or "ODOO_CLIENTS[0]"
                if validate:
                    return self._validate_and_save(selected_name, force_refresh=force_refresh)
                existing = self.validation_state(selected_name)
                return {
                    "ok": bool(existing.get("validated")),
                    "requires_client_selection": False,
                    "requires_validation": not bool(existing.get("validated")),
                    "client_name": selected_name,
                    "client": safe_client_public(selected),
                    "summary": (
                        "Single client is already validated."
                        if existing.get("validated")
                        else "Single client found. Run client.check with validate=true to validate before coding/deploying."
                    ),
                    "validation": existing,
                    "next_call": {"tool": "workflow.agent_instructions", "arguments": {}} if existing.get("validated") else None,
                }
            return {
                "ok": False,
                "requires_client_selection": True,
                "requires_validation": True,
                "summary": "Multiple clients configured. Ask the user which client_name to use, then run client.check(client_name=..., validate=true).",
                "message": "Multiple clients configured. Please select client_name before continuing.",
                "clients": public_clients,
                "available_client_names": names,
            }

        resolved = self.resolver.resolve_client(client_name=client_name, require_complete=True)
        if not resolved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Client not found or incomplete: {client_name}")

        if validate:
            return self._validate_and_save(resolved.name, force_refresh=force_refresh)

        existing = self.validation_state(resolved.name)
        return {
            "ok": bool(existing.get("validated")),
            "requires_client_selection": False,
            "requires_validation": not bool(existing.get("validated")),
            "client_name": resolved.name,
            "client": resolved.public,
            "summary": "Client is validated." if existing.get("validated") else "Client selected but not validated. Run client.check(validate=true).",
            "validation": existing,
            "next_call": {"tool": "workflow.agent_instructions", "arguments": {}} if existing.get("validated") else None,
        }

    def validation_state(self, client_name: str | None) -> dict[str, Any]:
        if not client_name:
            return {"validated": False, "summary": "No client_name selected."}
        state = self.store.load_session_state(client_name=client_name)
        validation = state.get("client_validation") if isinstance(state, dict) else None
        if not isinstance(validation, dict):
            return {"validated": False, "summary": "Client has not been validated in this MCP session."}
        return validation

    def require_validated_client(self, client_name: str | None, action: str) -> None:
        clients = [normalise_client(item) for item in get_odoo_clients()]
        names = [item.get("name") or f"ODOO_CLIENTS[{idx}]" for idx, item in enumerate(clients)]

        resolved_name = client_name
        if not resolved_name:
            if len(clients) == 1:
                resolved_name = clients[0].get("name") or "ODOO_CLIENTS[0]"
            elif len(clients) == 0:
                self._require_legacy_checkout_exists(action)
                return
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "requires_client_selection": True,
                        "requires_validation": True,
                        "message": f"Multiple ODOO_CLIENTS entries are configured. Before {action}, ask which client/repo to work on, then run client.check(client_name=..., validate=true).",
                        "available_client_names": names,
                        "clients": [safe_client_public(item) for item in clients],
                    },
                )

        validation = self.validation_state(resolved_name)
        requirement = self._requirement_for_action(action)
        if self._validation_satisfies(validation, requirement):
            project_path = self._validated_project_path(resolved_name, validation)
            stale_reason = self._validation_stale_reason(validation, project_path)
            if stale_reason:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "requires_client_selection": False,
                        "requires_validation": True,
                        "client_name": resolved_name,
                        "required_capability": requirement,
                        "project_path": project_path,
                        "message": (
                            f"Client '{resolved_name}' was previously validated, but that validation is stale: "
                            f"{stale_reason}. Run client.check(client_name='{resolved_name}', "
                            f"validate=true, force_refresh=true) before {action}."
                        ),
                        "validation": validation,
                    },
                )
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "requires_client_selection": False,
                "requires_validation": True,
                "client_name": resolved_name,
                "required_capability": requirement,
                "message": (
                    f"Client '{resolved_name}' has not passed {requirement} validation for this session. "
                    f"Run client.check(client_name='{resolved_name}', validate=true) before {action}."
                ),
                "validation": validation,
            },
        )

    def issue_job_validation_lease(
        self,
        client_name: str | None,
        *,
        action: str,
        project_path: str | None,
    ) -> dict[str, Any]:
        """Pin a fresh validated client identity to a server-owned long-running job."""
        self.require_validated_client(client_name, action)
        resolved = self.resolver.resolve_client(
            client_name=client_name,
            project_path=project_path,
            require_complete=False,
        )
        resolved_name = resolved.name if resolved else client_name
        validation = self.validation_state(resolved_name)
        validated_project = self._validated_project_path(resolved_name, validation)
        effective_project = str(Path(project_path or validated_project or "").expanduser().resolve())
        if validated_project and Path(validated_project).expanduser().resolve() != Path(effective_project):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot lease validation for a project_path different from the validated client checkout.",
            )
        capability = self._requirement_for_action(action)
        identity = {
            "version": 1,
            "client_name": resolved_name,
            "project_path": effective_project,
            "repo_fingerprint": hashlib.sha256(
                str(resolved.repo if resolved else validation.get("repo_url") or "").encode("utf-8")
            ).hexdigest(),
            "branch": resolved.branch if resolved else validation.get("branch"),
            "capability": capability,
            "validated_at": validation.get("validated_at"),
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        identity["fingerprint"] = self._job_lease_fingerprint(identity)
        return identity

    def require_validated_client_or_job_lease(
        self,
        client_name: str | None,
        action: str,
        *,
        project_path: str | None,
        lease: dict[str, Any] | None,
    ) -> None:
        """Accept current validation or an immutable lease from the owning stored job."""
        try:
            self.require_validated_client(client_name, action)
            return
        except HTTPException as current_error:
            if not self._job_validation_lease_matches(
                lease,
                client_name=client_name,
                project_path=project_path,
                required_capability=self._requirement_for_action(action),
            ):
                raise current_error

    @staticmethod
    def _job_lease_fingerprint(identity: dict[str, Any]) -> str:
        values = [
            str(identity.get(key) or "")
            for key in ("version", "client_name", "project_path", "repo_fingerprint", "branch", "capability", "validated_at", "issued_at")
        ]
        return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()

    def _job_validation_lease_matches(
        self,
        lease: dict[str, Any] | None,
        *,
        client_name: str | None,
        project_path: str | None,
        required_capability: str,
    ) -> bool:
        if not isinstance(lease, dict) or lease.get("version") != 1:
            return False
        expected_fingerprint = self._job_lease_fingerprint(lease)
        if lease.get("fingerprint") != expected_fingerprint:
            return False
        if client_name and lease.get("client_name") != client_name:
            return False
        leased_project = Path(str(lease.get("project_path") or "")).expanduser()
        if not leased_project.is_dir():
            return False
        if project_path and leased_project.resolve() != Path(project_path).expanduser().resolve():
            return False
        capability = str(lease.get("capability") or "")
        if required_capability == "deploy_ready" and capability != "deploy_ready":
            return False
        resolved = self.resolver.resolve_client(
            client_name=str(lease.get("client_name") or "") or None,
            project_path=str(leased_project),
            require_complete=False,
        )
        if resolved is None:
            return not get_odoo_clients()
        return (
            resolved.path is not None
            and Path(resolved.path).expanduser().resolve() == leased_project.resolve()
            and hashlib.sha256(str(resolved.repo or "").encode("utf-8")).hexdigest()
            == str(lease.get("repo_fingerprint") or "")
            and str(resolved.branch or "") == str(lease.get("branch") or "")
        )

    def _require_legacy_checkout_exists(self, action: str) -> None:
        if action == "repo.prepare":
            return
        project_path = settings.target_project_path
        if project_path and not Path(project_path).expanduser().is_dir():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "requires_client_selection": False,
                    "requires_validation": True,
                    "client_name": None,
                    "project_path": project_path,
                    "message": (
                        f"Legacy single-environment repo checkout no longer exists at {project_path}. "
                        f"Run repo.prepare for the configured TARGET_GIT_REPO/TARGET_GIT_BRANCH before {action}, "
                        "or configure ODOO_CLIENTS and run client.check(validate=true, force_refresh=true)."
                    ),
                },
            )

    def _validated_project_path(self, client_name: str | None, validation: dict[str, Any]) -> str | None:
        project_path = validation.get("project_path")
        if project_path:
            return str(project_path)
        if not client_name:
            return None
        resolved = self.resolver.resolve_client(client_name=client_name, require_complete=False)
        return resolved.path if resolved else None

    def _validation_stale_reason(self, validation: dict[str, Any], project_path: str | None) -> str | None:
        if project_path and not Path(project_path).expanduser().is_dir():
            return f"repo checkout no longer exists at {project_path}"
        age_seconds = self._validation_age_seconds(validation)
        if age_seconds is not None and age_seconds > self.VALIDATION_TTL_SECONDS:
            minutes = self.VALIDATION_TTL_SECONDS // 60
            return f"validation is older than {minutes} minutes"
        return None

    @staticmethod
    def _validation_age_seconds(validation: dict[str, Any]) -> float | None:
        validated_at = validation.get("validated_at")
        if not validated_at:
            return None
        try:
            parsed = datetime.fromisoformat(str(validated_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed).total_seconds()
        except Exception:
            return None

    def _requirement_for_action(self, action: str) -> str:
        if action.startswith("odoosh."):
            return "ssh_ready"
        if action == "workflow.safe_deploy_module.github_fallback":
            return "coding_ready"
        if action in {"workflow.safe_deploy_module", "workflow.resume_job"}:
            return "deploy_ready"
        if action.startswith("odoo.module"):
            return "deploy_ready"
        if action == "git.push" and settings.auto_upgrade_on_push:
            return "deploy_ready"
        if action.startswith("repo.") or action.startswith("git.") or action == "workflow.start_session":
            return "coding_ready"
        return "validated"

    def _validation_satisfies(self, validation: dict[str, Any], requirement: str) -> bool:
        # Backward compatibility with sessions saved by earlier builds.
        if requirement not in validation and validation.get("validated") is True:
            return True
        return bool(validation.get(requirement))

    def any_validated_client(self) -> dict[str, Any] | None:
        for item in get_odoo_clients():
            name = normalise_client(item).get("name")
            if not name:
                continue
            state = self.validation_state(name)
            if state.get("validated") is True:
                return {"client_name": name, "validation": state}
        return None

    def _validate_and_save(self, client_name: str, force_refresh: bool = False) -> dict[str, Any]:
        resolved = self.resolver.resolve_client(client_name=client_name, require_complete=True)
        if not resolved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Client not found or incomplete: {client_name}")

        existing = self.validation_state(resolved.name)
        if existing.get("validated") and not force_refresh:
            cached_path = self._validated_project_path(resolved.name, existing)
            if self._validation_stale_reason(existing, cached_path):
                force_refresh = True
            else:
                return {
                    "ok": True,
                    "validated": True,
                    "requires_client_selection": False,
                    "requires_validation": False,
                    "client_name": resolved.name,
                    "client": resolved.public,
                    "summary": "Client already validated.",
                    "checks": existing.get("checks", {}),
                    "validation_log": existing.get("validation_log", []),
                    "next_call": {"tool": "workflow.agent_instructions", "arguments": {}},
                }

        self.resolver.apply_odoo_env(resolved)
        checks: dict[str, str] = {}
        log: list[str] = []
        base_ok = True
        coding_ready = False
        shell_ok = False
        ssh_ok = False
        error_type: str | None = None

        def fail(check: str, message: str, kind: str) -> None:
            nonlocal base_ok, error_type
            base_ok = False
            error_type = error_type or kind
            checks[check] = "failed"
            log.append(message)

        def pass_check(check: str, message: str) -> None:
            checks[check] = "ok"
            log.append(message)

        config_errors = self._config_errors(resolved)
        if config_errors:
            checks["config"] = "failed"
            base_ok = False
            error_type = "config_error"
            log.extend(config_errors)
        else:
            pass_check("config", "Required client coding settings are present and do not look like placeholders.")

        if base_ok:
            checkout_result = self._check_checkout(resolved)
            checks["checkout"] = checkout_result["status"]
            checks["github"] = checkout_result["status"] if checkout_result.get("network_verified") else "deferred"
            checks["branch"] = checkout_result["status"]
            log.extend(checkout_result["log"])
            if checkout_result["status"] != "ok":
                base_ok = False
                error_type = checkout_result.get("error_type") or "checkout_error"

        coding_ready = bool(base_ok)

        # A checkout is not genuinely coding-ready when the provider-facing
        # composite workflow is missing from the active MCP registry. This
        # prevents the contradictory state where client.check succeeds but the
        # next workflow call returns "Resource not found".
        if coding_ready:
            try:
                from ..main import tool_registry_status

                registry = tool_registry_status()
                if registry.get("coding_workflow_ready"):
                    pass_check("coding_workflows", "Required MCP coding workflows are registered.")
                else:
                    fail(
                        "coding_workflows",
                        "Required MCP coding workflows are missing: "
                        + ", ".join(registry.get("missing_tools", [])),
                        "workflow_registration_error",
                    )
                    coding_ready = False
            except Exception as exc:
                fail(
                    "coding_workflows",
                    f"Could not verify MCP coding workflow registration: {exc}",
                    "workflow_registration_error",
                )
                coding_ready = False

        # Odoo shell is useful for data/model operations, but it is not required
        # for coding work. It runs through the selected client's Odoo.sh SSH
        # settings; no process-wide environment is mutated.
        if coding_ready:
            if self._has_shell_config(resolved):
                odoo_result = self._check_odoo_shell(resolved)
                checks["odoo_shell"] = odoo_result["status"]
                log.extend(odoo_result["log"])
                shell_ok = odoo_result["status"] == "ok"
            else:
                checks["odoo_shell"] = "skipped"
                log.append("Odoo shell skipped: Odoo.sh SSH/db/path settings are missing or placeholders.")

        if coding_ready:
            if self._has_odoosh_config(resolved):
                ssh_result = self._check_odoosh(resolved)
                checks["odoosh_ssh"] = ssh_result["status"]
                checks["addons_path"] = ssh_result.get("addons_path_status", "skipped")
                log.extend(ssh_result["log"])
                ssh_ok = ssh_result["status"] == "ok"
            else:
                checks["odoosh_ssh"] = "skipped"
                checks["addons_path"] = "skipped"
                log.append("Odoo.sh SSH skipped: SSH host/user/key/db/path settings are missing or placeholders.")

        # Safe deploy/module upgrade needs Odoo.sh SSH because the pipeline must
        # verify the pushed SHA, check /home/odoo/src/user file hashes, read
        # Odoo.sh error packs, and run the module upgrade. HTTPS JSON-RPC can
        # make data queries work, but it is not enough to prove source deploy.
        deploy_ready = bool(coding_ready and ssh_ok)
        github_push_ready = bool(coding_ready and resolved.path and Path(resolved.path).expanduser().is_dir())
        ssh_log_text = "\n".join(log).lower()
        fallback_transport_markers = ("permission denied (publickey)", "connection refused", "connection timed out", "no route to host")
        deploy_fallback_eligible = bool(
            github_push_ready
            and not deploy_ready
            and checks.get("odoosh_ssh") in {"failed", "skipped"}
            and (checks.get("odoosh_ssh") == "skipped" or any(marker in ssh_log_text for marker in fallback_transport_markers))
        )
        deploy_fallback_reason = "odoosh_ssh_unavailable" if deploy_fallback_eligible else None
        data_query_ready = bool(shell_ok or ssh_ok)
        full_validation_passed = bool(coding_ready and shell_ok and ssh_ok)
        now = datetime.now(timezone.utc).isoformat()

        if full_validation_passed:
            summary = "Client fully validated successfully."
            final_error_type = None
        elif coding_ready and ssh_ok and not shell_ok:
            summary = "Client validated for coding and deploy/query via Odoo.sh SSH fallback; Odoo shell login failed or is not configured."
            final_error_type = "odoo_login_error" if checks.get("odoo_shell") == "failed" else None
        elif coding_ready and shell_ok and not ssh_ok:
            summary = "Client validated for coding and data queries, but deploy is blocked because Odoo.sh SSH is unavailable."
            final_error_type = "odoosh_ssh_error" if checks.get("odoosh_ssh") == "failed" else None
        elif coding_ready:
            summary = "Client validated for coding only; Odoo shell and Odoo.sh SSH are unavailable."
            final_error_type = "odoo_connection_unavailable"
        else:
            summary = "Client validation failed."
            final_error_type = error_type or "client_validation_error"

        validation = {
            "validated": coding_ready,
            "coding_ready": coding_ready,
            "deploy_ready": deploy_ready,
            "github_push_ready": github_push_ready,
            "deploy_fallback_eligible": deploy_fallback_eligible,
            "deploy_fallback_reason": deploy_fallback_reason,
            "data_query_ready": data_query_ready,
            "shell_ready": shell_ok,
            "ssh_ready": ssh_ok,
            "full_validation_passed": full_validation_passed,
            "read_only_ssh_available": ssh_ok,
            "deploy_blocker": None if deploy_ready else ("Odoo.sh SSH is required for safe deploy/source verification/module upgrade." if coding_ready else None),
            "client_name": resolved.name,
            "project_path": str(resolved.path),
            "validated_at": now,
            "checks": checks,
            "summary": summary,
            "error_type": final_error_type,
            "validation_log": log[-30:],
            "client": resolved.public,
        }
        self.store.save_session_state(
            {
                self.SESSION_ACTIVE_KEY: resolved.name if coding_ready else None,
                self.SESSION_VALIDATED_KEY: coding_ready,
                "client_name": resolved.name,
                "project_path": str(resolved.path),
                "repo_url": resolved.repo,
                "branch": resolved.branch,
                "client_validation": validation,
            },
            client_name=resolved.name,
        )
        return {
            "ok": coding_ready,
            "validated": coding_ready,
            "coding_ready": coding_ready,
            "deploy_ready": deploy_ready,
            "github_push_ready": github_push_ready,
            "deploy_fallback_eligible": deploy_fallback_eligible,
            "deploy_fallback_reason": deploy_fallback_reason,
            "data_query_ready": data_query_ready,
            "shell_ready": shell_ok,
            "ssh_ready": ssh_ok,
            "full_validation_passed": full_validation_passed,
            "requires_client_selection": False,
            "requires_validation": not coding_ready,
            "client_name": resolved.name,
            "client": resolved.public,
            "summary": summary,
            "error_type": final_error_type,
            "read_only_ssh_available": ssh_ok,
            "deploy_blocker": None if deploy_ready else ("Odoo.sh SSH is required for safe deploy/source verification/module upgrade." if coding_ready else None),
            "checks": checks,
            "validation_log": log[-30:],
            "next_call": {"tool": "workflow.agent_instructions", "arguments": {}} if coding_ready else None,
        }

    def _config_errors(self, client: ResolvedClient) -> list[str]:
        """Validate only the minimum settings needed to choose a coding target.

        Odoo shell credentials and Odoo.sh SSH settings are intentionally optional
        here. They are checked separately and surfaced as shell_ready/ssh_ready so
        coding tools are not blocked just because the database login is missing.
        """
        errors: list[str] = []
        required = {
            "repo": client.repo,
            "branch": client.branch,
            "path": client.path,
        }
        for key, value in required.items():
            if value is None or str(value).strip() == "":
                errors.append(f"Missing required client setting: {key}.")
            elif PLACEHOLDER_RE.search(str(value).strip()) and not (
                key == "branch" and str(value).strip().lower().startswith("odoo_")
            ):
                errors.append(f"Placeholder value detected for {key}; replace it in ODOO_CLIENTS.")
        return errors

    def _has_shell_config(self, client: ResolvedClient) -> bool:
        https_values = [
            client.odoo_url,
            client.odoo_db,
            client.odoo_login,
            client.odoo_password,
        ]
        return all(self._usable(value) for value in https_values) or self._has_odoosh_config(client)

    def _has_odoosh_config(self, client: ResolvedClient) -> bool:
        values = [
            client.odoosh_db_name or client.odoo_db,
            client.odoosh_ssh_host,
            client.odoosh_ssh_user,
            client.odoosh_ssh_private_key_b64 or client.odoosh_ssh_key_path,
            client.odoosh_odoo_path,
            client.odoosh_user_addons_path,
        ]
        return all(self._usable(value) for value in values)

    @staticmethod
    def _usable(value: object) -> bool:
        if value is None:
            return False
        text = str(value).strip()
        if not text:
            return False
        return PLACEHOLDER_RE.search(text) is None

    def _check_checkout(self, client: ResolvedClient) -> dict[str, Any]:
        try:
            existing = Path(client.path).expanduser().resolve()
            if existing.is_dir() and (existing / ".git").exists():
                local_check = GitService().validate_target(str(existing), client.branch, "origin")
                if local_check.exit_code == 0:
                    return {"status": "ok", "network_verified": False, "log": [f"Existing checkout validated locally without fetch/pull: {existing} on branch {client.branch}. Remote access will be proven during push."]}
                return {"status": "failed", "error_type": "checkout_target_error", "log": [self._redact(local_check.stderr or local_check.stdout or "Existing checkout target validation failed.")]}

            # Validation itself runs in a connector-owned background job, so it
            # can safely materialize the configured checkout without holding an
            # MCP request open or passing a nonexistent path downstream.
            result = RepoService().prepare_repo(client_name=client.name, timeout_seconds=180)
            return {
                "status": "ok",
                "network_verified": True,
                "log": [f"Local checkout ready: {result.project_path} on branch {result.branch}."],
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error_type": "checkout_error", "log": [f"Repo checkout/prepare failed: {self._redact(str(exc))}"]}

    def _check_odoo_shell(self, client: ResolvedClient) -> dict[str, Any]:
        # Client validation must stay fast. OdooService.health() tries an Odoo.sh
        # shell first (up to 120s) and then HTTPS (up to 60s), duplicating the
        # dedicated SSH check below. Probe HTTPS directly with a short timeout;
        # SSH readiness itself is validated once by _check_odoosh().
        https_values = [client.odoo_url, client.odoo_db, client.odoo_login, client.odoo_password]
        if not all(self._usable(value) for value in https_values):
            return {"status": "skipped", "log": ["Fast Odoo HTTPS health check skipped: HTTPS credentials are not configured; Odoo.sh SSH provides query fallback."]}
        try:
            result = OdooJsonRpcService(client).search_read(
                "ir.module.module", [], ["name", "state"], limit=1, timeout_seconds=15
            )
            if result.status == "ok":
                return {"status": "ok", "log": [f"Odoo HTTPS data-query health check succeeded for database {result.database}."]}
            return {"status": "failed", "error_type": "odoo_login_error", "log": [f"Odoo HTTPS health check failed: {self._redact(result.error or 'unknown error')}"]}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error_type": "odoo_shell_error", "log": [f"Odoo HTTPS validation failed: {self._redact(str(exc))}"]}

    def _check_odoosh(self, client: ResolvedClient) -> dict[str, Any]:
        try:
            service = OdooShService(client)
            user_path = client.odoosh_user_addons_path or "/home/odoo/src/user"
            odoo_path = client.odoosh_odoo_path or "/home/odoo/src/odoo"
            db = client.odoosh_db_name or client.odoo_db or ""
            script = (
                "set -e; echo ODOOSH_SSH_OK; "
                f"test -d {self._sh_quote(user_path)} && echo USER_ADDONS_OK; "
                f"test -d {self._sh_quote(odoo_path)} && echo ODOO_PATH_OK; "
                f"test -n {self._sh_quote(db)} && echo DB_NAME_OK"
            )
            result = service.run_shell(script, timeout_seconds=30)
            text = (result.stdout or "") + "\n" + (result.stderr or "")
            if result.exit_code != 0:
                return {"status": "failed", "addons_path_status": "skipped", "error_type": "odoosh_ssh_error", "log": [f"Odoo.sh SSH failed: {self._redact(text.strip() or 'unknown SSH error')}"]}
            addons_ok = "USER_ADDONS_OK" in result.stdout
            if not addons_ok:
                return {"status": "failed", "addons_path_status": "failed", "error_type": "addons_path_missing", "log": [f"Odoo.sh SSH connected but addons path is missing: {user_path}."]}
            return {"status": "ok", "addons_path_status": "ok", "log": ["Odoo.sh SSH connected and addons/core paths were verified."]}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "addons_path_status": "skipped", "error_type": "odoosh_ssh_error", "log": [f"Odoo.sh validation failed: {self._redact(str(exc))}"]}

    @staticmethod
    def _sh_quote(value: str) -> str:
        import shlex

        return shlex.quote(value)

    @staticmethod
    def _redact(text: str) -> str:
        if not text:
            return text
        text = re.sub(r"oauth2:[^@\s]+@", "oauth2:***@", text)
        text = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "gh***", text)
        text = re.sub(r"(?i)(password|token|private_key)[=:]\s*[^\s,;]+", r"\1=***", text)
        return text[-2000:]
