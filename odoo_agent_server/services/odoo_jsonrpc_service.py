from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..config import settings
from ..schemas import OdooResult
from .client_resolver_service import ResolvedClient


class OdooJsonRpcService:
    """Authenticated Odoo HTTPS transport.

    Reads and explicitly enabled functional mutations execute as the configured
    Odoo user, so normal ACLs, field groups, company rules, and record rules
    remain authoritative. Deployment operations still use the Odoo.sh pipeline.
    """

    def __init__(self, client: ResolvedClient | None = None) -> None:
        self.client = client

    def _base_url(self) -> str | None:
        return ((self.client.odoo_url if self.client else None) or settings.odoo_base_url or "").rstrip("/") or None

    def _db(self) -> str | None:
        return (self.client.odoo_db if self.client else None) or settings.odoo_db_name

    def _login(self) -> str | None:
        return (self.client.odoo_login if self.client else None) or settings.odoo_login

    def _password(self) -> str | None:
        return (self.client.odoo_password if self.client else None) or settings.odoo_password

    def _api_key(self) -> str | None:
        return (self.client.odoo_api_key if self.client else None) or settings.odoo_api_key

    def configured(self) -> bool:
        credentials_ready = bool(self._api_key()) or bool(self._login() and self._password())
        return bool(self._base_url() and self._db() and credentials_ready)

    def _verify_ssl(self) -> bool:
        if self.client is not None and self.client.odoo_verify_ssl is not None:
            return bool(self.client.odoo_verify_ssl)
        return bool(settings.odoo_https_verify_ssl)

    def _call(self, service: str, method: str, args: list[Any], timeout_seconds: int) -> Any:
        base_url = self._base_url()
        if not base_url:
            raise RuntimeError("ODOO_BASE_URL/odoo_url is not configured for HTTPS JSON-RPC fallback.")
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": int(time.time() * 1000),
        }
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            verify=self._verify_ssl(),
        ) as client:
            response = client.post(f"{base_url}/jsonrpc", json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("error"):
            error = data["error"]
            details = error.get("data") if isinstance(error, dict) else None
            details = details if isinstance(details, dict) else {}
            # Odoo wraps ORM exceptions in a generic top-level "Odoo Server Error".
            # Prefer the nested exception message so callers see the actionable
            # cause (for example, an invalid field or access-rule failure).
            message = (
                details.get("message")
                or error.get("message")
                or str(error)
            )
            exception_name = details.get("name")
            if exception_name and exception_name not in message:
                message = f"{exception_name}: {message}"
            raise RuntimeError(message)
        return data.get("result")

    def _json2_call(self, model: str, method: str, params: dict[str, Any], timeout_seconds: int) -> Any:
        """Call Odoo 19+ External JSON-2 with a dedicated API key."""
        base_url = self._base_url()
        api_key = self._api_key()
        if not base_url or not api_key:
            raise RuntimeError("Odoo JSON-2 requires odoo_api_key and odoo_url.")
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or "") or not re.fullmatch(r"[a-zA-Z0-9_]+", method or ""):
            raise ValueError("Invalid Odoo model or method name for JSON-2.")
        headers = {
            "Authorization": f"bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "odoo-mcp-agent/1.0",
        }
        if self._db():
            headers["X-Odoo-Database"] = str(self._db())
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            verify=self._verify_ssl(),
        ) as client:
            response = client.post(f"{base_url}/json/2/{model}/{method}", headers=headers, json=params)
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()

    def _login_uid(self, timeout_seconds: int) -> int:
        db = self._db()
        login = self._login()
        password = self._password()
        if not (db and login and password):
            raise RuntimeError("Odoo HTTPS credentials are incomplete for JSON-RPC fallback.")
        uid = self._call("common", "login", [db, login, password], timeout_seconds)
        if not uid:
            raise RuntimeError("Odoo HTTPS JSON-RPC login failed.")
        return int(uid)

    def _format_exception(self, exc: Exception) -> str:
        message = f"Odoo HTTPS transport failed: {exc}"
        if isinstance(exc, httpx.ConnectError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
            message += (
                "; TLS certificate verification failed. For Odoo.sh/dev hosts with a known "
                "certificate hostname mismatch, set odoo_verify_ssl=false on that ODOO_CLIENTS "
                "entry or set ODOO_HTTPS_VERIFY_SSL=false for single-client/dev usage. "
                "Do not disable verification for untrusted production hosts."
            )
        return message

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str | None = None,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        if not self.configured():
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Odoo HTTPS transport is not configured for this client.")
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or ""):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid model name for JSON-RPC query.")
        safe_fields: list[str] = []
        for field in fields or []:
            if not isinstance(field, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", field):
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=f"Invalid field name for JSON-RPC query: {field!r}")
            safe_fields.append(field)
        if order and not re.fullmatch(r"[a-zA-Z0-9_,.\s]+(?:\s+(?:asc|desc))?", order, flags=re.I):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid order string for JSON-RPC query.")

        kwargs: dict[str, Any] = {
            "fields": safe_fields,
            "limit": max(1, min(int(limit or 20), 200)),
            "offset": max(0, int(offset or 0)),
        }
        if order:
            kwargs["order"] = order
        if context:
            kwargs["context"] = context

        try:
            if self._api_key():
                params: dict[str, Any] = {
                    "domain": domain or [],
                    "fields": safe_fields,
                    "limit": kwargs["limit"],
                    "offset": kwargs["offset"],
                }
                if order:
                    params["order"] = order
                if context:
                    params["context"] = context
                records = self._json2_call(model, "search_read", params, timeout_seconds)
            else:
                uid = self._login_uid(timeout_seconds)
                db = self._db()
                password = self._password()
                records = self._call(
                    "object",
                    "execute_kw",
                    [db, uid, password, model, "search_read", [domain or []], kwargs],
                    timeout_seconds,
                )
            return OdooResult(
                status="ok",
                base_url=self._base_url(),
                database=self._db(),
                result=records or [],
            )
        except Exception as exc:
            return OdooResult(
                status="failed",
                base_url=self._base_url(),
                database=self._db(),
                error=self._format_exception(exc),
            )

    def read_group(
        self,
        model: str,
        domain: list | None,
        fields: list[str],
        groupby: list[str],
        lazy: bool = False,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        if not self.configured():
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Odoo HTTPS JSON-RPC fallback is not configured for this client.")
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or ""):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid model name for JSON-RPC query.")
        aggregate_re = re.compile(r"^[a-zA-Z0-9_]+(?::(?:sum|avg|min|max|count|count_distinct|bool_and|bool_or|array_agg))?$")
        groupby_re = re.compile(r"^[a-zA-Z0-9_]+(?::(?:day|week|month|quarter|year))?$")
        if not fields or any(not isinstance(field, str) or not aggregate_re.fullmatch(field) for field in fields):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid aggregate field for JSON-RPC read_group.")
        if not groupby or any(not isinstance(field, str) or not groupby_re.fullmatch(field) for field in groupby):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid groupby field for JSON-RPC read_group.")
        kwargs: dict[str, Any] = {"lazy": bool(lazy)}
        if context:
            kwargs["context"] = context
        try:
            if self._api_key():
                params: dict[str, Any] = {
                    "domain": domain or [],
                    "fields": fields,
                    "groupby": groupby,
                    "lazy": bool(lazy),
                }
                if context:
                    params["context"] = context
                records = self._json2_call(model, "read_group", params, timeout_seconds)
            else:
                uid = self._login_uid(timeout_seconds)
                records = self._call(
                    "object",
                    "execute_kw",
                    [self._db(), uid, self._password(), model, "read_group", [domain or [], fields, groupby], kwargs],
                    timeout_seconds,
                )
            return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=records or [])
        except Exception as exc:
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))

    def execute_model_method(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        if not self.configured():
            return OdooResult(
                status="failed",
                base_url=self._base_url(),
                database=self._db(),
                error="Authenticated Odoo HTTPS credentials are not configured for this client.",
            )
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or ""):
            return OdooResult(status="failed", error="Invalid Odoo model name.")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", method or ""):
            return OdooResult(status="failed", error="Invalid Odoo method name.")
        try:
            uid = self._login_uid(timeout_seconds)
            result = self._call(
                "object",
                "execute_kw",
                [self._db(), uid, self._password(), model, method, args or [], kwargs or {}],
                timeout_seconds,
            )
            return OdooResult(
                status="ok",
                base_url=self._base_url(),
                database=self._db(),
                result=result,
            )
        except Exception as exc:
            return OdooResult(
                status="failed",
                base_url=self._base_url(),
                database=self._db(),
                error=self._format_exception(exc),
            )

    def fields_get(self, model: str, timeout_seconds: int = 60) -> OdooResult:
        if self._api_key():
            try:
                result = self._json2_call(
                    model,
                    "fields_get",
                    {"attributes": ["type", "readonly", "required", "string", "relation"]},
                    timeout_seconds,
                )
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result or {})
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        return self.execute_model_method(
            model,
            "fields_get",
            [],
            {"attributes": ["type", "readonly", "required", "string", "relation"]},
            timeout_seconds,
        )

    def check_access_rights(self, model: str, operation: str, timeout_seconds: int = 60) -> OdooResult:
        if self._api_key():
            try:
                result = self._json2_call(
                    model,
                    "check_access_rights",
                    {"operation": operation, "raise_exception": False},
                    timeout_seconds,
                )
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result)
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        return self.execute_model_method(
            model,
            "check_access_rights",
            [operation],
            {"raise_exception": False},
            timeout_seconds,
        )

    def read_records(
        self,
        model: str,
        ids: list[int],
        fields: list[str],
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        kwargs: dict[str, Any] = {"fields": fields}
        if context:
            kwargs["context"] = context
        if self._api_key():
            try:
                result = self._json2_call(
                    model,
                    "read",
                    {"ids": ids, **kwargs},
                    timeout_seconds,
                )
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result or [])
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        return self.execute_model_method(model, "read", [ids], kwargs, timeout_seconds)

    def create_records(
        self,
        model: str,
        records: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        kwargs = {"context": context} if context else {}
        if self._api_key():
            try:
                result = self._json2_call(
                    model,
                    "create",
                    {"vals_list": records, **kwargs},
                    timeout_seconds,
                )
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result)
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        values: dict[str, Any] | list[dict[str, Any]] = records[0] if len(records) == 1 else records
        return self.execute_model_method(model, "create", [values], kwargs, timeout_seconds)

    def write_records(
        self,
        model: str,
        ids: list[int],
        values: dict[str, Any],
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        kwargs = {"context": context} if context else {}
        if self._api_key():
            try:
                result = self._json2_call(
                    model,
                    "write",
                    {"ids": ids, "vals": values, **kwargs},
                    timeout_seconds,
                )
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result)
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        return self.execute_model_method(model, "write", [ids, values], kwargs, timeout_seconds)

    def run_action(
        self,
        model: str,
        ids: list[int],
        action: str,
        arguments: list[Any] | None = None,
        keyword_arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        kwargs = dict(keyword_arguments or {})
        if context:
            kwargs["context"] = context
        if self._api_key():
            if arguments:
                return OdooResult(
                    status="failed",
                    base_url=self._base_url(),
                    database=self._db(),
                    error="Odoo JSON-2 accepts named parameters only; positional action arguments are not supported.",
                )
            try:
                params = dict(kwargs)
                params["ids"] = ids
                result = self._json2_call(model, action, params, timeout_seconds)
                return OdooResult(status="ok", base_url=self._base_url(), database=self._db(), result=result)
            except Exception as exc:
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=self._format_exception(exc))
        return self.execute_model_method(
            model,
            action,
            [ids, *(arguments or [])],
            kwargs,
            timeout_seconds,
        )
