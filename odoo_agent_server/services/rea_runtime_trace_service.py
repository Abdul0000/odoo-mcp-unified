from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


class ReaRuntimeTraceService:
    """Analyze caller-supplied Odoo trace spans; never enables or captures profiling."""
    MAX_EVENTS = 2000
    MAX_TEXT = 1000

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if number >= 0 else None
        except (TypeError, ValueError):
            return None

    def analyze(self, *, events: list[dict[str, Any]], trace_id: str | None = None,
                slow_threshold_ms: float = 100.0) -> dict[str, Any]:
        if not isinstance(events, list) or not events or len(events) > self.MAX_EVENTS:
            raise ValueError("events must contain between 1 and 2000 trace records")
        methods: dict[str, dict[str, Any]] = {}
        query_counts: Counter[str] = Counter()
        query_samples: dict[str, dict[str, Any]] = {}
        warnings: list[dict[str, Any]] = []
        accepted = 0
        for index, raw in enumerate(events):
            if not isinstance(raw, dict):
                warnings.append({"code": "INVALID_EVENT", "index": index})
                continue
            event_trace = str(raw.get("trace_id") or "")
            if trace_id and event_trace and event_trace != trace_id:
                continue
            kind = str(raw.get("kind") or raw.get("type") or "span").lower()
            name = str(raw.get("name") or raw.get("method") or raw.get("operation") or "unknown")[:self.MAX_TEXT]
            duration = self._number(raw.get("duration_ms", raw.get("duration")))
            accepted += 1
            if kind in {"sql", "query", "db", "database"} or "sql" in name.lower():
                normalized = " ".join(str(raw.get("query") or raw.get("sql") or name).split())[:self.MAX_TEXT]
                # Avoid treating parameter values as stable query identities.
                signature = normalized.split(" VALUES ")[0].split(" WHERE ")[0][:240].lower()
                query_counts[signature] += 1
                query_samples.setdefault(signature, {"sample": normalized[:240], "evidence_index": index})
                continue
            model = str(raw.get("model") or "")[:160]
            method = str(raw.get("method") or name)[:200]
            key = model + "." + method
            item = methods.setdefault(key, {"model": model or None, "method": method,
                "calls": 0, "total_ms": 0.0, "max_ms": 0.0, "slow_calls": 0, "evidence": []})
            item["calls"] += 1
            if duration is not None:
                item["total_ms"] += duration
                item["max_ms"] = max(item["max_ms"], duration)
                if duration >= slow_threshold_ms:
                    item["slow_calls"] += 1
            if len(item["evidence"]) < 5:
                item["evidence"].append({"index": index, "span_id": raw.get("span_id"),
                    "parent_id": raw.get("parent_id"), "duration_ms": duration})
        method_rows = []
        for item in methods.values():
            item["total_ms"] = round(item["total_ms"], 3)
            item["max_ms"] = round(item["max_ms"], 3)
            item["avg_ms"] = round(item["total_ms"] / item["calls"], 3)
            method_rows.append(item)
        method_rows.sort(key=lambda x: (x["total_ms"], x["calls"]), reverse=True)
        repeated = [{**query_samples[key], "count": count} for key, count in query_counts.most_common(100) if count > 1]
        recommendations = []
        for item in method_rows:
            if item["calls"] >= 5:
                recommendations.append({"code": "HIGH_CALL_COUNT", "target": item["model"] + "." + item["method"],
                    "calls": item["calls"], "advice": "Check for record-by-record loops; consider batching or read_group where semantics allow."})
            if item["slow_calls"]:
                recommendations.append({"code": "SLOW_METHOD", "target": item["model"] + "." + item["method"],
                    "slow_calls": item["slow_calls"], "max_ms": item["max_ms"]})
        if repeated:
            recommendations.append({"code": "REPEATED_QUERY_PATTERN", "patterns": len(repeated),
                "advice": "Inspect repeated query signatures for N+1 access; confirm parameters and call context before changing code."})
        return {"status": "analyzed", "trace_id": trace_id, "summary": {
            "events_received": len(events), "events_analyzed": accepted, "methods": len(method_rows),
            "query_events": sum(query_counts.values()), "repeated_query_patterns": len(repeated),
            "slow_threshold_ms": slow_threshold_ms}, "methods": method_rows[:200],
            "repeated_queries": repeated, "recommendations": recommendations[:200],
            "warnings": warnings[:100], "capture": {"performed": False, "source": "caller_supplied_trace"}}
