"""Minimal client manager with an admin-only browser session."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import external_base_url
from ..config import settings
from ..schemas import ClientCheckRequest
from ..services.client_admin_service import ClientAdminService
from ..services.client_settings_store import atomic_json, settings_lock

router = APIRouter(tags=['client-manager'])
ASSETS = Path(__file__).resolve().parent.parent / 'static' / 'clients'
COOKIE = 'odoo_client_admin'
SESSION_SECONDS = 6 * 60 * 60


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(default='', max_length=4096)
    login_code: str = Field(default='', max_length=256)


class Update(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    values: dict[str, str | bool | int | None] = Field(max_length=30)


def _sign(value: str, purpose='session') -> str:
    return hmac.new(settings.api_token.encode(), f'client-admin:{purpose}:{value}'.encode(), hashlib.sha256).hexdigest()


def _encode(value: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')
    return raw + '.' + _sign(raw)


def _session(request: Request) -> str:
    cookie = request.cookies.get(COOKIE, '')
    try:
        if len(cookie) > 2048:
            raise ValueError
        raw, signature = cookie.split('.', 1)
        if not hmac.compare_digest(signature, _sign(raw)):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if data.get('expires', 0) <= time.time() or not data.get('nonce'):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, 'Sign in to manage clients') from None
    if request.method not in {'GET', 'HEAD'}:
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != external_base_url(request):
            raise HTTPException(403, 'Cross-site changes are not allowed')
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), _sign(cookie, 'csrf')):
            raise HTTPException(403, 'Session changed. Refresh this page before saving.')
    return cookie


def issue_login_code() -> str:
    """Operator helper: one-use, ten-minute browser link; never exposes API token."""
    root = settings.state_path / 'client-admin-login'
    root.mkdir(parents=True, exist_ok=True)
    with settings_lock(settings.state_path):
        for path in root.glob('*.json'):
            try:
                if json.loads(path.read_text()).get('expires', 0) < time.time():
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
        if sum(1 for _ in root.glob('*.json')) >= 32:
            raise HTTPException(429, 'Too many pending sign-in links')
        code = secrets.token_urlsafe(32)
        atomic_json(root / (hashlib.sha256(code.encode()).hexdigest() + '.json'),
                    {'expires': time.time() + 600, 'proof': _sign(code, 'login-code')})
    return code


def _consume_login_code(code):
    path = settings.state_path / 'client-admin-login' / (hashlib.sha256(code.encode()).hexdigest() + '.json')
    with settings_lock(settings.state_path):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if value.get('expires', 0) < time.time() or not hmac.compare_digest(value.get('proof', ''), _sign(code, 'login-code')):
            path.unlink(missing_ok=True)
            return False
        path.unlink()
        return True


@router.get('/clients', include_in_schema=False)
def page():
    return FileResponse(ASSETS / 'index.html', media_type='text/html')


@router.get('/clients/assets/{name}', include_in_schema=False)
def asset(name: str):
    if name not in {'app.js', 'style.css'}:
        raise HTTPException(404, 'Asset not found')
    return FileResponse(ASSETS / name, media_type='text/javascript' if name.endswith('.js') else 'text/css')


@router.post('/admin/session')
def login(payload: Login, request: Request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != external_base_url(request):
        raise HTTPException(403, 'Cross-site sign-in is not allowed')
    if settings.api_token == 'change-me':
        raise HTTPException(503, 'Configure a connector admin token before enabling client management')
    accepted = (bool(payload.token) and secrets.compare_digest(payload.token.encode(), settings.api_token.encode()))
    if not accepted and payload.login_code:
        accepted = _consume_login_code(payload.login_code)
    if not accepted:
        raise HTTPException(401, 'Invalid or expired sign-in. Use a new sign-in link or your connector admin token.')
    cookie = _encode({'nonce': secrets.token_urlsafe(24), 'expires': int(time.time()) + SESSION_SECONDS})
    response = JSONResponse({'authenticated': True, 'csrf_token': _sign(cookie, 'csrf')})
    response.set_cookie(COOKIE, cookie, max_age=SESSION_SECONDS, httponly=True,
                        secure=external_base_url(request).startswith('https://'), samesite='strict', path='/admin')
    return response


@router.get('/admin/session')
def session(cookie: str = Depends(_session)):
    return {'authenticated': True, 'csrf_token': _sign(cookie, 'csrf')}


@router.delete('/admin/session')
def logout(cookie: str = Depends(_session)):
    response = JSONResponse({'authenticated': False})
    response.delete_cookie(COOKIE, path='/admin')
    return response


@router.get('/admin/clients', dependencies=[Depends(_session)])
def list_clients():
    return ClientAdminService().list()


@router.get('/admin/clients/{name}', dependencies=[Depends(_session)])
def client(name: str):
    return ClientAdminService().detail(name)


@router.patch('/admin/clients/{name}', dependencies=[Depends(_session)])
def save_client(name: str, payload: Update):
    return ClientAdminService().save(name, payload.revision, payload.values)


@router.post('/admin/clients/{name}/validate', dependencies=[Depends(_session)])
def validate_client(name: str):
    ClientAdminService().detail(name)  # Check the client before queueing work.
    from .tools import _start_client_validation
    return _start_client_validation(ClientCheckRequest(client_name=name, validate=True, force_refresh=True))


@router.get('/admin/validation/{job_id}', dependencies=[Depends(_session)])
def validation_job(job_id: str):
    return ClientAdminService().validation_job(job_id)
