"""Real (non-mocked) CDP integration test for CdpBrowserService.run_pos_flow.

Most tests stub the browser service, which means the actual CDP browser
automation code — selectors, click sequence, session persistence, screenshot
capture, console/network accumulation — has never been exercised by CI.

This test serves a tiny local fixture site that mimics the DOM shape of a
real POS OWL flow (login -> config select -> new session -> product tile ->
payment button -> payment method -> validate -> receipt) and points
run_pos_flow's base_url at it, so the exact CDP calls in
browser_cdp_service.py run for real.

It is skipped automatically unless ODOO_AGENT_CDP_URL points at an already
running browser that exposes the Chrome DevTools Protocol.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import threading
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fake_pos_site"


def _cdp_available() -> bool:
    return bool(os.environ.get("ODOO_AGENT_CDP_URL"))


@pytest.fixture(scope="module")
def fake_pos_server():
    """Serve tests/fixtures/fake_pos_site on a background thread for the module's lifetime."""
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(FIXTURE_DIR), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.skipif(not _cdp_available(), reason="ODOO_AGENT_CDP_URL is not configured")
def test_run_pos_flow_happy_path_real_browser(fake_pos_server):
    """Drives the real run_pos_flow against a fixture site that mimics an
    Odoo POS OWL session end-to-end. Fails if any selector/click in
    browser_cdp_service.py stops matching the DOM shape it expects.
    """
    from odoo_agent_server.services.browser_cdp_service import CdpBrowserService
    from odoo_agent_server.schemas import PosFlowCheckRequest

    # The fixture's login form redirects straight to pos_backend.html rather
    # than requiring a real Odoo /web/login round trip, so base_url is enough
    # for run_pos_flow's page.goto(base_url + "web/login") call to work.
    request = PosFlowCheckRequest(
        base_url=fake_pos_server,
        login="admin",
        password="admin",
        pos_config_name="Main Register",
        screenshot_prefix="test_pos_flow_happy",
    )

    result = asyncio.run(CdpBrowserService().run_pos_flow(request))

    step_names = [s.step for s in result.steps]
    assert "login" in step_names
    assert "open_pos_backend" in step_names
    assert "open_pos_session" in step_names
    assert "click_product" in step_names
    assert "open_payment_screen" in step_names
    assert "validate_order" in step_names

    click_product_step = next(s for s in result.steps if s.step == "click_product")
    assert click_product_step.status == "ok", f"click_product failed: {click_product_step.error}"

    payment_step = next(s for s in result.steps if s.step == "open_payment_screen")
    assert payment_step.status == "ok", f"open_payment_screen failed: {payment_step.error}"

    validate_step = next(s for s in result.steps if s.step == "validate_order")
    assert validate_step.status == "ok", f"validate_order failed: {validate_step.error}"

    assert result.status == "ok"


@pytest.mark.skipif(not _cdp_available(), reason="ODOO_AGENT_CDP_URL is not configured")
def test_run_pos_flow_reports_failure_when_product_tile_missing(fake_pos_server):
    """When the POS catalog fails to render (simulating a real OWL crash),
    run_pos_flow must report click_product as failed with a clear error —
    not raise, and not silently report 'ok'.
    """
    from odoo_agent_server.services.browser_cdp_service import CdpBrowserService
    from odoo_agent_server.schemas import PosFlowCheckRequest

    # Point pos_config_name so the flow lands on the SAME config click, but we
    # can't redirect open_pos_session to pos_ui_broken.html without changing
    # the fixture's own JS, so instead this test directly exercises the
    # product-not-found branch by asking for a product name that will never
    # exist in the fixture's DOM, on the working pos_ui.html page.
    request = PosFlowCheckRequest(
        base_url=fake_pos_server,
        login="admin",
        password="admin",
        pos_config_name="Main Register",
        product_name="Nonexistent Product XYZ",
        screenshot_prefix="test_pos_flow_broken",
    )

    result = asyncio.run(CdpBrowserService().run_pos_flow(request))

    click_product_step = next(s for s in result.steps if s.step == "click_product")
    assert click_product_step.status == "failed"
    assert click_product_step.error
    assert result.status == "failed"
