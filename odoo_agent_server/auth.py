from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status

from .config import settings
from .services.oauth_service import OAuthStore


def external_base_url(request: Request) -> str:
    configured = settings.public_base_url or settings.oauth_issuer
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def oauth_resource_metadata_url(request: Request) -> str:
    return f"{external_base_url(request)}/.well-known/oauth-protected-resource"


def _request_token(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
) -> str:
    """Extract a connector token from the most common MCP client locations.

    Preferred and standard: Authorization: Bearer <token>.
    Compatibility: some desktop/app MCP UIs expose an API-key field or cannot
    attach headers at all. For those, X-API-Key and an opt-in query token are
    supported without changing the MCP tool protocol.
    """
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            return value.split(" ", 1)[1].strip()
        # Compatibility with clients that put only the token in the
        # Authorization header, or use Token/APIKey-style prefixes.
        lower = value.lower()
        for prefix in ("token ", "apikey ", "api-key "):
            if lower.startswith(prefix):
                return value.split(" ", 1)[1].strip()
        return value

    if x_api_key:
        return x_api_key.strip()

    if settings.query_token_auth_enabled:
        for name in ("access_token", "token", "api_key"):
            value = request.query_params.get(name)
            if value:
                return value.strip()

    return ""


def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Accept the connector bearer token, API-key compatibility, or OAuth access tokens.

    The legacy token keeps REST/OpenAPI Actions and non-OAuth MCP clients such
    as compatibility clients working. OAuth access tokens are required by MCP
    apps/connectors.
    """
    token = _request_token(request, authorization, x_api_key)

    if token and secrets.compare_digest(token, settings.api_token):
        return
    if token and OAuthStore().validate_access_token(token):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid bearer token",
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{oauth_resource_metadata_url(request)}"',
        },
    )


AuthDependency = Depends(require_token)
