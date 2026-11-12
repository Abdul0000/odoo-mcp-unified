from pathlib import Path

import pytest

from odoo_agent_server.config import settings
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import time as _real_time


class _ClockProxy:
    """Stands in for a module's ``time`` reference only (never the global ``time`` module).

    Patching ``module.time.monotonic`` mutates the shared stdlib module and freezes asyncio's
    event loop; replacing the module-local name keeps the fake clock scoped to that module.
    """

    def __init__(self, clock):
        self._clock = clock

    def monotonic(self):
        return self._clock.monotonic()

    def sleep(self, seconds):
        return self._clock.sleep(seconds)

    def __getattr__(self, name):
        return getattr(_real_time, name)


def patch_module_clock(monkeypatch, clock, *modules):
    for module in modules:
        monkeypatch.setattr(module, "time", _ClockProxy(clock))


def advance_clock_on_job_wait(monkeypatch, clock):
    """JsonStore.wait_job blocks on a signal, not time.sleep: make that wait advance the fake clock."""
    from odoo_agent_server.services.store_service import JsonStore

    monkeypatch.setattr(JsonStore._job_signal, "wait_change", lambda job_id, version, timeout: clock.sleep(timeout))


WS_TOOLS = frozenset({
    "ws_open", "ws_status", "ws_outline", "ws_scaffold", "ws_extend", "ws_symbol", "ws_read",
    "ws_grep", "ws_edit", "ws_diff", "ws_lint", "ws_commit", "ws_discard",
})


@pytest.fixture(autouse=True)
def _disable_global_approval_by_default(monkeypatch):
    """Keep unrelated tests deterministic; approval-specific tests opt in explicitly."""
    monkeypatch.setattr(settings, "require_approval", False)
