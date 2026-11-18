"""User-paired Chrome extension endpoints and a single narrow MCP tool."""
import io
from pathlib import Path
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .client_admin import _session
from ..services.browser_extension_service import BrowserExtensionService

router = APIRouter(tags=['browser-extension'])
ASSETS = Path(__file__).resolve().parent.parent / 'static' / 'browser-extension'
EXTENSION = Path(__file__).resolve().parents[2] / 'extensions' / 'chrome'


class PairCode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_name: str | None = None
    permanent: bool = False
    rotate: bool = False


class PairDevice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=16, max_length=128)
    extension_id: str = Field(pattern='^[a-p]{32}$')
    label: str = Field(default='My Chrome', min_length=1, max_length=80)


@router.get('/browser-extension', include_in_schema=False)
def page():
    return FileResponse(ASSETS / 'index.html', headers={'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"})


@router.get('/browser-extension/setup.js', include_in_schema=False)
def script():
    return FileResponse(ASSETS / 'setup.js', media_type='text/javascript', headers={'Cache-Control': 'no-store'})


@router.get('/browser-extension/extension.zip', include_in_schema=False)
def download():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('manifest.json', 'background.js', 'page.js', 'popup.html', 'popup.js', 'popup.css', 'icon128.png'):
            archive.write(EXTENSION / name, name)
    return Response(buffer.getvalue(), media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="odoo-mcp-chrome.zip"', 'Cache-Control': 'no-store'})


@router.post('/admin/browser-extension/pair', dependencies=[Depends(_session)])
def create_pair(payload: PairCode):
    return BrowserExtensionService().pair_code(payload.client_name, permanent=payload.permanent, rotate=payload.rotate)


@router.get('/admin/browser-extension', dependencies=[Depends(_session)])
def devices():
    return BrowserExtensionService().status()


@router.delete('/admin/browser-extension/{device_id}', dependencies=[Depends(_session)])
async def revoke(device_id: str):
    return await BrowserExtensionService().revoke(device_id)


@router.post('/browser-extension/pair')
def exchange(payload: PairDevice, request: Request):
    if request.headers.get('origin') != 'chrome-extension://' + payload.extension_id:
        raise HTTPException(403, 'Pair from the installed Chrome extension')
    return BrowserExtensionService().pair(payload.code, payload.extension_id, payload.label)


@router.websocket('/browser-extension/connect')
async def connect(ws: WebSocket):
    await BrowserExtensionService().connect(ws)
