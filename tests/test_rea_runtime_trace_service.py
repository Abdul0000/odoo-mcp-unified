from odoo_agent_server.services.rea_runtime_trace_service import ReaRuntimeTraceService


def test_runtime_trace_aggregates_slow_methods_and_repeated_queries():
    events = [
        {"trace_id": "t1", "kind": "span", "model": "sale.order", "method": "_compute_amount", "duration_ms": 150, "span_id": "1"},
        {"trace_id": "t1", "kind": "span", "model": "sale.order", "method": "_compute_amount", "duration_ms": 20, "span_id": "2"},
        {"trace_id": "t1", "kind": "sql", "query": "SELECT id FROM sale_order WHERE id = 1", "duration_ms": 3},
        {"trace_id": "t1", "kind": "sql", "query": "SELECT id FROM sale_order WHERE id = 2", "duration_ms": 4},
        {"trace_id": "other", "kind": "span", "model": "res.partner", "method": "name_get", "duration_ms": 900},
    ]
    result = ReaRuntimeTraceService().analyze(events=events, trace_id="t1", slow_threshold_ms=100)
    assert result["summary"]["events_analyzed"] == 4
    assert result["methods"][0]["slow_calls"] == 1
    assert result["repeated_queries"][0]["count"] == 2
    assert result["capture"]["performed"] is False


def test_runtime_trace_rejects_unbounded_input():
    import pytest
    with pytest.raises(ValueError):
        ReaRuntimeTraceService().analyze(events=[])
