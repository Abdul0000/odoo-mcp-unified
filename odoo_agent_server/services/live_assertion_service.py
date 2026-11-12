from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..schemas import OdooDebugRecordAssertion
from .client_resolver_service import ClientResolverService
from .odoo_service import OdooService


class LiveAssertionService:
    """Evaluate one bounded, read-only Odoo acceptance assertion."""

    def evaluate(
        self,
        assertion_value: OdooDebugRecordAssertion | dict[str, Any],
        *,
        client_name: str | None,
        project_path: str | None,
        phase: str,
    ) -> dict[str, Any]:
        assertion = (
            assertion_value
            if isinstance(assertion_value, OdooDebugRecordAssertion)
            else OdooDebugRecordAssertion.model_validate(assertion_value)
        )
        client = ClientResolverService().resolve_client(
            client_name=client_name,
            project_path=project_path,
            require_complete=False,
        )
        fields = assertion.fields or ["id"]
        result = OdooService(client).search_read(
            assertion.model,
            assertion.domain,
            fields,
            200,
            0,
            "id asc",
            None,
            120,
        )
        records = result.result if result.status == "ok" and isinstance(result.result, list) else []
        count = len(records)
        count_passed = (
            (assertion.expected_count_min is None or count >= assertion.expected_count_min)
            and (assertion.expected_count_max is None or count <= assertion.expected_count_max)
        )

        def values_match(record: Any) -> bool:
            return isinstance(record, dict) and all(
                record.get(key) == expected
                for key, expected in assertion.expected_values.items()
            )

        if not assertion.expected_values:
            values_passed = True
            matched_rows = count
        else:
            matches = [values_match(record) for record in records]
            matched_rows = sum(1 for matched in matches if matched)
            if assertion.match_mode == "first":
                values_passed = bool(matches and matches[0])
            elif assertion.match_mode == "all":
                values_passed = bool(matches and all(matches))
            else:
                values_passed = any(matches)
        payload = assertion.model_dump(mode="json")
        return {
            "phase": phase,
            "query_status": result.status,
            "passed": bool(result.status == "ok" and count_passed and values_passed),
            "model": assertion.model,
            "count": count,
            "matched_rows": matched_rows,
            "expected_count_min": assertion.expected_count_min,
            "expected_count_max": assertion.expected_count_max,
            "checked_fields": sorted(assertion.expected_values),
            "error": self._safe_error(result.error),
            "assertion_fingerprint": hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _safe_error(value: str | None) -> str | None:
        text = str(value or "")[:2000]
        if not text:
            return None
        return re.sub(
            r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            text,
        )
