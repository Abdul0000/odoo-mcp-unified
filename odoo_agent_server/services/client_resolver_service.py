from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status

from ..config import (
    get_odoo_clients,
    make_authenticated_repo_url,
    normalise_client,
    safe_client_public,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedClient:
    name: str
    repo: str
    branch: str
    path: str
    token: str | None = None
    environment: str | None = None
    odoo_url: str | None = None
    odoo_db: str | None = None
    odoo_login: str | None = None
    odoo_password: str | None = None
    odoo_api_key: str | None = None
    odoo_verify_ssl: bool | None = None
    odoosh_db_name: str | None = None
    odoosh_ssh_host: str | None = None
    odoosh_ssh_port: int | None = None
    odoosh_ssh_user: str | None = None
    odoosh_ssh_key_path: str | None = None
    odoosh_ssh_private_key_b64: str | None = None
    odoosh_odoo_path: str | None = None
    odoosh_user_addons_path: str | None = None

    @property
    def authenticated_repo(self) -> str:
        return make_authenticated_repo_url(self.repo, self.token)

    @property
    def public(self) -> dict:
        return safe_client_public(
            {
                "name": self.name,
                "repo": self.repo,
                "branch": self.branch,
                "path": self.path,
                "environment": self.environment,
                "odoo_url": self.odoo_url,
                "odoo_db": self.odoo_db,
                "odoo_verify_ssl": self.odoo_verify_ssl,
                "odoosh_db_name": self.odoosh_db_name,
                "odoosh_ssh_host": self.odoosh_ssh_host,
                "odoosh_ssh_port": self.odoosh_ssh_port,
                "odoosh_ssh_user": self.odoosh_ssh_user,
                "odoosh_odoo_path": self.odoosh_odoo_path,
                "odoosh_user_addons_path": self.odoosh_user_addons_path,
            }
        )


class ClientResolverService:
    """Resolve the active ODOO_CLIENTS entry without exposing secrets."""

    _required = ("repo", "branch", "path")

    def resolve_client(
        self,
        client_name: str | None = None,
        project_path: str | None = None,
        odoo_url: str | None = None,
        odoo_db: str | None = None,
        *,
        require_complete: bool = True,
    ) -> ResolvedClient | None:
        clients = [normalise_client(item) for item in get_odoo_clients()]
        if not clients:
            self._log_resolution(None, "no_clients", client_name, project_path)
            return None

        complete_clients = [item for item in clients if self._is_complete(item)]

        if client_name:
            matches = [item for item in clients if item.get("name", "").lower() == client_name.lower()]
            if not matches:
                self._raise_not_found(client_name, clients)
            resolved = self._to_resolved(matches[0], require_complete=require_complete)
            self._log_resolution(resolved, "client_name", client_name, project_path)
            return resolved

        selected = self._match_path(complete_clients, project_path)
        if selected:
            self._log_resolution(selected, "project_path", client_name, project_path)
            return selected

        # Only use Odoo target matching when the caller explicitly provides
        # odoo_url/odoo_db. Never infer a client from process-wide environment
        # values in multi-client mode; those can be stale from another request.
        selected = self._match_odoo_target(complete_clients, odoo_url, odoo_db)
        if selected:
            self._log_resolution(selected, "odoo_target", client_name, project_path)
            return selected

        if len(complete_clients) == 1:
            resolved = self._to_resolved(complete_clients[0], require_complete=True)
            self._log_resolution(resolved, "single_complete_client", client_name, project_path)
            return resolved

        if len(complete_clients) > 1:
            self._raise_ambiguous(complete_clients)

        if require_complete and clients:
            self._raise_incomplete(clients)
        self._log_resolution(None, "no_complete_match", client_name, project_path)
        return None

    def apply_odoo_env(self, client: ResolvedClient | None) -> None:
        """Compatibility no-op.

        Earlier builds mutated ``os.environ`` and the process-wide ``settings``
        object here. That made concurrent multi-client requests unsafe because
        one client could overwrite another client's active database, SSH host,
        or checkout path. Services must now receive/pass ``ResolvedClient``
        explicitly and fall back to global settings only when no multi-client
        entry is selected.
        """
        if client:
            logger.debug("apply_odoo_env ignored for isolated client %s", client.name)
        return None

    def search_roots(self, client: ResolvedClient | None) -> list[str]:
        """Installed-source roots in the same priority used by Odoo.sh.

        Community addons live under /home/odoo/src/odoo/addons, not directly
        under /home/odoo/src/odoo.  Using the addons path lets local reads and
        lazy core sync resolve native modules such as point_of_sale correctly.
        """
        odoo_root = (client.odoosh_odoo_path if client else None) or "/home/odoo/src/odoo"
        user_root = (client.odoosh_user_addons_path if client else None) or "/home/odoo/src/user"
        roots: list[str] = [
            f"{str(odoo_root).rstrip('/')}/addons",
            "/home/odoo/src/enterprise",
            str(user_root).rstrip("/"),
        ]
        seen: set[str] = set()
        result: list[str] = []
        for root in roots:
            if root and root not in seen:
                seen.add(root)
                result.append(root)
        return result

    def _log_resolution(
        self,
        resolved: ResolvedClient | None,
        reason: str,
        requested_client_name: str | None,
        requested_project_path: str | None,
    ) -> None:
        payload = {
            "reason": reason,
            "requested_client_name": requested_client_name,
            "requested_project_path": requested_project_path,
            "client_name": resolved.name if resolved else None,
            "project_path": resolved.path if resolved else None,
            "branch": resolved.branch if resolved else None,
        }
        logger.info(
            "client_resolver.resolve_client resolved client_name=%s project_path=%s branch=%s reason=%s",
            payload["client_name"],
            payload["project_path"],
            payload["branch"],
            reason,
            extra={"odoo_client_resolution": payload},
        )

    def _match_odoo_target(
        self,
        clients: list[dict],
        odoo_url: str | None,
        odoo_db: str | None,
    ) -> ResolvedClient | None:
        url = (odoo_url or "").rstrip("/")
        db = odoo_db or ""
        if not url or not db:
            return None
        matches = [
            item for item in clients
            if item.get("odoo_url", "").rstrip("/") == url and item.get("odoo_db") == db
        ]
        return self._single_match(matches)

    def _match_path(self, clients: list[dict], project_path: str | None) -> ResolvedClient | None:
        if not project_path:
            return None
        target = self._normal_path(project_path)
        matches = [item for item in clients if self._normal_path(item.get("path")) == target]
        return self._single_match(matches)

    def _single_match(self, matches: list[dict]) -> ResolvedClient | None:
        if not matches:
            return None
        if len(matches) > 1:
            self._raise_ambiguous(matches)
        return self._to_resolved(matches[0], require_complete=True)

    def _to_resolved(self, client: dict, *, require_complete: bool) -> ResolvedClient | None:
        if require_complete and not self._is_complete(client):
            self._raise_incomplete([client])
        if not self._is_complete(client):
            return None
        return ResolvedClient(
            name=client.get("name") or "ODOO_CLIENTS[0]",
            repo=client["repo"],
            branch=client["branch"],
            path=client["path"],
            token=client.get("token") or None,
            environment=self._parse_environment(client.get("environment")),
            odoo_url=client.get("odoo_url") or None,
            odoo_db=client.get("odoo_db") or None,
            odoo_login=client.get("odoo_login") or None,
            odoo_password=client.get("odoo_password") or None,
            odoo_api_key=client.get("odoo_api_key") or None,
            odoo_verify_ssl=self._parse_optional_bool(client.get("odoo_verify_ssl")),
            odoosh_db_name=client.get("odoosh_db_name") or None,
            odoosh_ssh_host=client.get("odoosh_ssh_host") or None,
            odoosh_ssh_port=self._parse_port(client.get("odoosh_ssh_port")),
            odoosh_ssh_user=client.get("odoosh_ssh_user") or None,
            odoosh_ssh_key_path=client.get("odoosh_ssh_key_path") or None,
            odoosh_ssh_private_key_b64=client.get("odoosh_ssh_private_key_b64") or None,
            odoosh_odoo_path=client.get("odoosh_odoo_path") or None,
            odoosh_user_addons_path=client.get("odoosh_user_addons_path") or None,
        )


    @staticmethod
    def _parse_optional_bool(value: object) -> bool | None:
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid boolean value in ODOO_CLIENTS: {value!r}",
        )

    @staticmethod
    def _parse_port(value: str | None) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid odoosh_ssh_port in ODOO_CLIENTS: {value!r}",
            )

    @staticmethod
    def _parse_environment(value: str | None) -> str | None:
        if value in (None, ""):
            return None
        environment = str(value).strip().lower()
        if environment not in {"test", "development", "staging", "production"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid ODOO_CLIENTS environment. Use test, development, "
                    "staging, or production."
                ),
            )
        return environment

    def _is_complete(self, client: dict) -> bool:
        return all(client.get(key) for key in self._required)

    @staticmethod
    def _normal_path(path: str | None) -> str:
        if not path:
            return ""
        return str(Path(path).expanduser().resolve())

    def _raise_not_found(self, name: str, clients: list[dict]) -> None:
        names = [item.get("name") for item in clients if item.get("name")]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Client '{name}' not found in ODOO_CLIENTS. Available clients: {names}",
        )

    def _raise_ambiguous(self, clients: list[dict]) -> None:
        names = [item.get("name") or "(unnamed client)" for item in clients]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Multiple ODOO_CLIENTS entries match. Choose one client_name from: {names}",
        )

    def _raise_incomplete(self, clients: list[dict]) -> None:
        safe = [safe_client_public(item) for item in clients]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "ODOO_CLIENTS is configured, but no selected entry contains all required "
                f"repo/branch/path values. Safe client metadata: {safe}"
            ),
        )
