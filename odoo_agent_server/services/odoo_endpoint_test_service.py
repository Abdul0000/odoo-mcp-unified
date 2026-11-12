from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from ..schemas import OdooDebugRecordAssertion, OdooEndpointTestCase
from .client_resolver_service import ResolvedClient
from .odoo_jsonrpc_service import OdooJsonRpcService


class OdooEndpointTestService:
    """Execute bounded same-origin endpoint contracts against a test client."""

    def __init__(self, client: ResolvedClient) -> None:
        self.client = client
        self.rpc = OdooJsonRpcService(client)

    @staticmethod
    def _json_subset(expected: Any, actual: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and OdooEndpointTestService._json_subset(value, actual[key])
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return isinstance(actual, list) and all(
                any(OdooEndpointTestService._json_subset(item, candidate) for candidate in actual)
                for item in expected
            )
        return actual == expected

    @staticmethod
    def _redact(value: Any) -> Any:
        sensitive = ("password", "passwd", "token", "secret", "authorization", "cookie")
        if isinstance(value, dict):
            return {
                key: "<redacted>" if any(word in str(key).lower() for word in sensitive)
                else OdooEndpointTestService._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [OdooEndpointTestService._redact(item) for item in value[:100]]
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "...[truncated]"
        return value

    def _redact_text(self, value: str) -> str:
        text = value[:4000] + ("...[truncated]" if len(value) > 4000 else "")
        for secret in (self.client.odoo_password, self.client.odoo_login):
            if secret:
                text = text.replace(secret, "<redacted>")
        text = re.sub(
            r"(?i)(password|passwd|token|secret|authorization|cookie)([\"'\s:=]+)([^,;}\s]+)",
            r"\1\2<redacted>",
            text,
        )
        text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <redacted>", text)
        return text

    def _authenticate(self, session: httpx.Client, timeout: int) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "db": self.client.odoo_db,
                "login": self.client.odoo_login,
                "password": self.client.odoo_password,
            },
            "id": int(time.time() * 1000),
        }
        response = session.post("/web/session/authenticate", json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("error") or not (data.get("result") or {}).get("uid"):
            raise RuntimeError("Odoo session authentication failed for the selected test client.")

    def _assert_record(self, assertion: OdooDebugRecordAssertion) -> dict[str, Any]:
        fields = list(dict.fromkeys(["id", *assertion.fields, *assertion.expected_values]))
        limit = min(200, (assertion.expected_count_max + 1) if assertion.expected_count_max is not None else 200)
        result = self.rpc.search_read(
            assertion.model,
            assertion.domain,
            fields,
            limit=limit,
        )
        if result.status != "ok":
            return {"passed": False, "model": assertion.model, "error": result.error}
        rows = list(result.result or [])
        count_ok = (
            (assertion.expected_count_min is None or len(rows) >= assertion.expected_count_min)
            and (assertion.expected_count_max is None or len(rows) <= assertion.expected_count_max)
        )
        comparisons = [
            all(row.get(key) == expected for key, expected in assertion.expected_values.items())
            for row in rows
        ]
        values_ok = True
        if assertion.expected_values:
            if assertion.match_mode == "first":
                values_ok = bool(comparisons and comparisons[0])
            elif assertion.match_mode == "all":
                values_ok = bool(comparisons) and all(comparisons)
            else:
                values_ok = any(comparisons)
        return {
            "passed": count_ok and values_ok,
            "model": assertion.model,
            "count": len(rows),
            "count_ok": count_ok,
            "values_ok": values_ok,
            "records": self._redact(rows[:20]),
        }

    def run_case(self, case: OdooEndpointTestCase) -> dict[str, Any]:
        if self.client.environment != "test":
            return {
                "name": case.name,
                "status": "blocked",
                "passed": False,
                "verified": False,
                "success_allowed": False,
                "error": "Endpoint tests require an ODOO_CLIENTS entry with environment=test.",
            }
        if not self.client.odoo_url:
            return {
                "name": case.name,
                "status": "failed",
                "passed": False,
                "verified": False,
                "success_allowed": False,
                "error": "odoo_url is not configured for the selected test client.",
            }
        try:
            with httpx.Client(
                base_url=self.client.odoo_url.rstrip("/"),
                verify=True if self.client.odoo_verify_ssl is None else self.client.odoo_verify_ssl,
                follow_redirects=False,
                timeout=httpx.Timeout(case.timeout_seconds, connect=min(10, case.timeout_seconds)),
            ) as session:
                if case.auth == "session":
                    self._authenticate(session, case.timeout_seconds)
                payload = case.body
                if case.protocol == "odoo_jsonrpc":
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "call",
                        "params": case.body,
                        "id": int(time.time() * 1000),
                    }
                response = session.request(
                    case.method,
                    case.path,
                    params=case.query,
                    headers=case.headers,
                    json=None if case.method == "GET" else payload,
                )
            text = response.text
            try:
                response_json: Any = response.json()
            except (json.JSONDecodeError, ValueError):
                response_json = None
            checks = {
                "status": response.status_code == case.expected_status,
                "jsonrpc_error": not (
                    isinstance(response_json, dict) and response_json.get("error")
                ),
                "json_subset": (
                    True if case.expected_json_subset is None
                    else self._json_subset(case.expected_json_subset, response_json)
                ),
                "body_contains": all(fragment in text for fragment in case.expected_body_contains),
            }
            record_checks = [self._assert_record(item) for item in case.record_assertions]
            passed = all(checks.values()) and all(item["passed"] for item in record_checks)
            return {
                "name": case.name,
                "status": "passed" if passed else "failed",
                "passed": passed,
                "verified": passed,
                "success_allowed": passed,
                "request": {"method": case.method, "path": case.path, "protocol": case.protocol, "auth": case.auth},
                "response": {
                    "status_code": response.status_code,
                    "json": self._redact(response_json),
                    "body_length": len(text),
                    "body_preview": None if response_json is not None else self._redact_text(text),
                },
                "checks": checks,
                "record_assertions": record_checks,
            }
        except Exception as exc:
            return {
                "name": case.name,
                "status": "failed",
                "passed": False,
                "verified": False,
                "success_allowed": False,
                "error": str(exc),
            }
