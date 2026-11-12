from __future__ import annotations

import asyncio

from odoo_agent_server.config import settings
from odoo_agent_server.services.browser_cdp_service import CdpBrowserService, _CdpPage


def test_cdp_browser_missing_config_returns_clear_failure(monkeypatch):
    monkeypatch.delenv("ODOO_AGENT_CDP_URL", raising=False)
    monkeypatch.setattr(settings, "cdp_url", None)

    result = asyncio.run(CdpBrowserService().screenshot_odoo_page(
        base_url="https://example.test",
        login=None,
        password=None,
        start_path="/web",
        screenshot_name="",
    ))

    assert result.status == "failed"
    assert "CDP browser is not configured" in result.error


def test_cdp_event_collection_splits_all_console_errors_and_network():
    page = _CdpPage(ws_url="ws://example.test/devtools/page/1")

    page._handle_event({
        "method": "Runtime.consoleAPICalled",
        "params": {"type": "log", "args": [{"value": "boot ok"}]},
    })
    page._handle_event({
        "method": "Runtime.consoleAPICalled",
        "params": {"type": "error", "args": [{"value": "owl exploded"}]},
    })
    page._handle_event({
        "method": "Runtime.exceptionThrown",
        "params": {"exceptionDetails": {"text": "UncaughtPromiseError"}},
    })
    page._handle_event({
        "method": "Network.responseReceived",
        "params": {"response": {"status": 500, "url": "https://example.test/web/assets"}},
    })

    assert "log: boot ok" in page.console_all
    assert "error: owl exploded" in page.console_all
    assert "error: owl exploded" in page.console_errors
    assert "pageerror: UncaughtPromiseError" in page.console_errors
    assert page.network_errors == ["500 https://example.test/web/assets"]

    console_errors, console_all, network_errors = page.snapshot_and_clear()
    assert console_errors == ["error: owl exploded", "pageerror: UncaughtPromiseError"]
    assert console_all == ["log: boot ok", "error: owl exploded", "pageerror: UncaughtPromiseError"]
    assert network_errors == ["500 https://example.test/web/assets"]
    assert page.snapshot_and_clear() == ([], [], [])
