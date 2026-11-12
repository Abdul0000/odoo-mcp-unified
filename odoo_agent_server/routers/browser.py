from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import BrowserCheckRequest, BrowserCheckResult, PosFlowCheckRequest, PosFlowCheckResult
from ..services.browser_cdp_service import CdpBrowserService

router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[AuthDependency])


@router.post("/screenshot", response_model=BrowserCheckResult)
async def browser_screenshot(payload: BrowserCheckRequest) -> BrowserCheckResult:
    return await CdpBrowserService().screenshot_odoo_page(
        base_url=payload.base_url,
        login=payload.login,
        password=payload.password,
        start_path=payload.start_path,
        screenshot_name=payload.screenshot_name,
        timeout_seconds=payload.timeout_seconds,
        full_page=payload.full_page,
    )


@router.post("/pos-flow", response_model=PosFlowCheckResult)
async def browser_pos_flow(payload: PosFlowCheckRequest) -> PosFlowCheckResult:
    """Run a real end-to-end POS interaction (login -> open POS -> click
    product -> payment -> validate) in one persistent session, with a
    console/network capture at every step. Use this instead of
    /browser/screenshot whenever the change under test touches POS OWL code —
    a single cold-load screenshot never exercises the click/payment surface
    where POS component bugs actually throw.
    """
    return await CdpBrowserService().run_pos_flow(payload)
