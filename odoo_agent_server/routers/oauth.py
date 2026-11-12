from __future__ import annotations

import secrets

from html import escape
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import external_base_url
from ..config import settings
from ..services.oauth_service import OAuthStore, build_redirect, is_loopback_host, parse_basic_auth

router = APIRouter(tags=["oauth"])


def _base(request: Request) -> str:
    return external_base_url(request)


def _oauth_metadata(request: Request) -> dict[str, Any]:
    base = _base(request)
    issuer = (settings.oauth_issuer or base).rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": [settings.oauth_default_scope],
        "service_documentation": f"{base}/docs",
    }


def _protected_resource_metadata(request: Request) -> dict[str, Any]:
    base = _base(request)
    issuer = (settings.oauth_issuer or base).rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/docs",
        "scopes_supported": [settings.oauth_default_scope],
    }


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server_metadata(request: Request) -> dict[str, Any]:
    return _oauth_metadata(request)


@router.get("/.well-known/openid-configuration")
def openid_configuration(request: Request) -> dict[str, Any]:
    # This is provided for clients that look for OIDC-style discovery first.
    return _oauth_metadata(request)


@router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource_metadata(request: Request) -> dict[str, Any]:
    return _protected_resource_metadata(request)


@router.get("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource_metadata_mcp(request: Request) -> dict[str, Any]:
    return _protected_resource_metadata(request)


@router.post("/oauth/register")
async def oauth_register(request: Request) -> JSONResponse:
    metadata = await request.json()
    record = OAuthStore().register_client(metadata)
    return JSONResponse(record, status_code=201)


def _query_params_to_dict(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.query_params.items()}


def _authorization_form(params: dict[str, str], error: str | None = None) -> str:
    hidden = "\n".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}" />'
        for k, v in params.items()
    )
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Authorize Odoo Agent Connector</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:#0f172a; color:#e5e7eb; margin:0; padding:32px; }}
    .card {{ max-width:720px; margin:0 auto; background:#111827; border:1px solid #374151; border-radius:16px; padding:28px; }}
    h1 {{ margin-top:0; font-size:24px; }}
    p {{ color:#cbd5e1; line-height:1.5; }}
    .warning {{ background:#451a03; border:1px solid #f97316; padding:14px; border-radius:10px; color:#fed7aa; }}
    .error {{ background:#7f1d1d; border:1px solid #ef4444; padding:12px; border-radius:10px; margin-bottom:12px; }}
    input {{ width:100%; box-sizing:border-box; padding:12px; border-radius:8px; border:1px solid #475569; background:#020617; color:#e5e7eb; margin:10px 0 16px; }}
    button {{ background:#2563eb; color:white; border:0; border-radius:8px; padding:12px 16px; font-weight:600; cursor:pointer; }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Authorize Odoo Agent Connector</h1>
    <p>An MCP client is requesting access to the MCP tools exposed by your local/VPS Odoo Agent Connector.</p>
    <div class="warning">Only approve this if you started this connector and trust the tunnel/domain. Write tools run directly after connector authentication; keep this service private and token-protected.</div>
    {error_html}
    <form method="post" action="/oauth/authorize">
      {hidden}
      <label>Connector admin token</label>
      <input type="password" name="connector_token" autocomplete="off" placeholder="Paste ODOO_AGENT_API_TOKEN to approve" />
      <button type="submit">Authorize MCP Client</button>
    </form>
  </main>
</body>
</html>
"""


def _authorize_redirect(params: dict[str, str]) -> RedirectResponse:
    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    scope = params.get("scope") or settings.oauth_default_scope
    state = params.get("state")
    if response_type != "code":
        raise HTTPException(status_code=400, detail="Only response_type=code is supported")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail="client_id and redirect_uri are required")
    code = OAuthStore().create_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge or "",
        code_challenge_method=code_challenge_method or "",
    )
    return RedirectResponse(build_redirect(redirect_uri, {"code": code, "state": state}), status_code=302)


def _is_loopback_redirect(redirect_uri: str | None) -> bool:
    if not redirect_uri:
        return False
    from urllib.parse import urlparse

    try:
        host = urlparse(redirect_uri).hostname or ""
    except ValueError:
        return False
    return is_loopback_host(host)


@router.get("/oauth/authorize", response_model=None)
def oauth_authorize_get(request: Request):
    params = _query_params_to_dict(request)
    if settings.oauth_auto_approve:
        return _authorize_redirect(params)
    if settings.oauth_auto_approve_loopback and _is_loopback_redirect(params.get("redirect_uri")):
        # Native clients (Claude Code and other local CLIs) cannot complete an
        # interactive consent form. Loopback redirects are already restricted
        # to localhost/127.0.0.1 and PKCE is still enforced on exchange.
        return _authorize_redirect(params)
    return HTMLResponse(_authorization_form(params))


@router.post("/oauth/authorize", response_model=None)
async def oauth_authorize_post(request: Request):
    raw = (await request.body()).decode("utf-8")
    parsed = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    connector_token = parsed.pop("connector_token", "")
    if not secrets.compare_digest(connector_token.encode("utf-8"), settings.api_token.encode("utf-8")):
        return HTMLResponse(_authorization_form(parsed, error="Invalid connector admin token"), status_code=403)
    return _authorize_redirect(parsed)


async def _parse_token_request(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        return {str(k): str(v) for k, v in data.items() if v is not None}
    raw = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


@router.post("/oauth/token")
async def oauth_token(request: Request) -> dict[str, Any]:
    body = await _parse_token_request(request)
    basic_client_id, basic_client_secret = parse_basic_auth(request.headers.get("authorization"))
    client_id = body.get("client_id") or basic_client_id
    client_secret = body.get("client_secret") or basic_client_secret
    grant_type = body.get("grant_type")
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")
    if grant_type == "authorization_code":
        return OAuthStore().exchange_code(
            code=body.get("code") or "",
            client_id=client_id,
            redirect_uri=body.get("redirect_uri") or "",
            code_verifier=body.get("code_verifier") or "",
            client_secret=client_secret,
        )
    if grant_type == "refresh_token":
        return OAuthStore().refresh_access_token(
            refresh_token=body.get("refresh_token") or "",
            client_id=client_id,
            client_secret=client_secret,
        )
    raise HTTPException(status_code=400, detail="Unsupported grant_type")
