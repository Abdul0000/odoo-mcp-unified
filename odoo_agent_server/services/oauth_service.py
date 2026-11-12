from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException

from ..config import settings


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: list[str]
    client_name: str | None = None
    client_secret: str | None = None
    token_endpoint_auth_method: str = "none"




_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _token_secret() -> bytes:
    """Return the stable signing secret for stateless OAuth tokens.

    Koyeb and similar platforms often restart/redeploy containers with an
    empty /tmp. Earlier builds stored access/refresh tokens only in the
    file-backed state directory, so MCP clients could lose OAuth sessions after
    redeploy and tool calls failed with "couldn't connect your account".

    New tokens are signed and self-validating. The file store remains for
    short-lived authorization codes and backwards-compatible legacy tokens.
    """
    if settings.oauth_token_secret:
        return settings.oauth_token_secret.encode("utf-8")
    if settings.api_token and settings.api_token != "change-me":
        # Domain-separated derivation: tokens stay stable across redeploys without using the raw
        # API token as an HMAC key (a leaked access token must not help forge or replay the other).
        return hmac.new(settings.api_token.encode("utf-8"), b"odoo-mcp/oauth-token-signing/v1", hashlib.sha256).digest()
    # No usable secret configured: a random per-process key. Tokens simply stop validating on
    # restart; there is never a publicly known signing key.
    return _EPHEMERAL_SECRET


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _encode_signed_token(prefix: str, payload: dict[str, Any]) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64url_encode(hmac.new(_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{prefix}_{body}.{sig}"


def _decode_signed_token(token: str, prefix: str) -> dict[str, Any] | None:
    expected_prefix = f"{prefix}_"
    if not token.startswith(expected_prefix) or "." not in token:
        return None
    body, sig = token[len(expected_prefix):].split(".", 1)
    expected_sig = _b64url_encode(hmac.new(_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not secrets.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload

class OAuthStore:
    """Dependency-light OAuth 2.1 storage for MCP auth.

    This implementation is intentionally small and file-backed for local/VPS use.
    Replace with PostgreSQL/Redis for multi-worker production deployments.
    """

    _lock = Lock()

    def __init__(self) -> None:
        self.root = settings.state_path
        self.file = self.root / "oauth.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _default_state(self) -> dict[str, Any]:
        return {"clients": {}, "codes": {}, "access_tokens": {}, "refresh_tokens": {}}

    def _read(self) -> dict[str, Any]:
        with self._lock:
            if not self.file.exists():
                return self._default_state()
            try:
                data = json.loads(self.file.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                return self._default_state()
            state = self._default_state()
            if isinstance(data, dict):
                state.update({k: v for k, v in data.items() if k in state and isinstance(v, dict)})
            return state

    def _write(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = metadata.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise HTTPException(status_code=400, detail="redirect_uris is required for dynamic client registration")
        for uri in redirect_uris:
            _validate_redirect_uri(str(uri), allow_chatgpt_defaults=False)

        client_id = "dcr_" + secrets.token_urlsafe(24)
        now = int(time.time())
        record = {
            "client_id": client_id,
            "client_id_issued_at": now,
            "client_name": metadata.get("client_name") or "Odoo MCP Client",
            "redirect_uris": [str(uri) for uri in redirect_uris],
            "grant_types": metadata.get("grant_types") or ["authorization_code", "refresh_token"],
            "response_types": metadata.get("response_types") or ["code"],
            "scope": metadata.get("scope") or settings.oauth_default_scope,
            "token_endpoint_auth_method": metadata.get("token_endpoint_auth_method") or "none",
        }
        state = self._read()
        state["clients"][client_id] = record
        self._write(state)
        return record

    def get_client(self, client_id: str) -> OAuthClient:
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required")

        if client_id == settings.oauth_static_client_id:
            return OAuthClient(
                client_id=client_id,
                redirect_uris=settings.oauth_allowed_redirect_uris,
                client_name="Static Odoo MCP Client",
                client_secret=settings.oauth_static_client_secret,
                token_endpoint_auth_method="client_secret_post" if settings.oauth_static_client_secret else "none",
            )

        state = self._read()
        record = state["clients"].get(client_id)
        if not record:
            # MCP clients can reuse a dynamically registered dcr_* client_id after a
            # Render/Koyeb redeploy. If the OAuth state directory is ephemeral,
            # that client registration is gone even though the connector is the
            # same service. This recovery path keeps clients working while still
            # validating the redirect URI later in _validate_redirect_for_client.
            if settings.oauth_accept_unknown_dcr_clients and client_id.startswith("dcr_"):
                return OAuthClient(
                    client_id=client_id,
                    redirect_uris=[],
                    client_name="Recovered Dynamic MCP Client",
                    client_secret=None,
                    token_endpoint_auth_method="none",
                )
            raise HTTPException(status_code=400, detail="Unknown OAuth client_id")
        return OAuthClient(
            client_id=record["client_id"],
            redirect_uris=list(record.get("redirect_uris") or []),
            client_name=record.get("client_name"),
            client_secret=record.get("client_secret"),
            token_endpoint_auth_method=record.get("token_endpoint_auth_method") or "none",
        )

    def create_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        client = self.get_client(client_id)
        _validate_redirect_for_client(client, redirect_uri)
        if code_challenge_method != "S256" or not code_challenge:
            raise HTTPException(status_code=400, detail="PKCE S256 code_challenge is required")

        code = "code_" + secrets.token_urlsafe(32)
        state = self._read()
        state["codes"][code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or settings.oauth_default_scope,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "expires_at": int(time.time()) + settings.oauth_authorization_code_ttl_seconds,
            "used": False,
        }
        self._write(state)
        return code

    def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        client_secret: str | None = None,
    ) -> dict[str, Any]:
        client = self.get_client(client_id)
        _validate_client_secret(client, client_secret)

        state = self._read()
        record = state["codes"].get(code)
        if not record:
            raise HTTPException(status_code=400, detail="Invalid authorization code")
        if record.get("used"):
            raise HTTPException(status_code=400, detail="Authorization code already used")
        if int(record.get("expires_at", 0)) < int(time.time()):
            raise HTTPException(status_code=400, detail="Authorization code expired")
        if record.get("client_id") != client_id or record.get("redirect_uri") != redirect_uri:
            raise HTTPException(status_code=400, detail="Authorization code client or redirect mismatch")
        if not _verify_pkce_s256(code_verifier, record.get("code_challenge", "")):
            raise HTTPException(status_code=400, detail="Invalid PKCE code_verifier")

        record["used"] = True
        state["codes"][code] = record
        token_payload = self._issue_tokens(state, client_id=client_id, scope=record.get("scope") or settings.oauth_default_scope)
        self._write(state)
        return token_payload

    def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str | None = None,
    ) -> dict[str, Any]:
        # Prefer stateless refresh tokens so MCP clients remain connected across
        # Koyeb/container redeploys even when the file-backed state directory is
        # reset. The client_id check prevents a token issued for one client from
        # being replayed by another.
        stateless = _decode_signed_token(refresh_token, "srt")
        if stateless:
            if stateless.get("client_id") != client_id:
                raise HTTPException(status_code=400, detail="Invalid refresh_token client")
            return self._issue_tokens(self._read(), client_id=client_id, scope=stateless.get("scope") or settings.oauth_default_scope, reuse_refresh_token=refresh_token)

        client = self.get_client(client_id)
        _validate_client_secret(client, client_secret)
        state = self._read()
        record = state["refresh_tokens"].get(refresh_token)
        if not record or record.get("client_id") != client_id:
            raise HTTPException(status_code=400, detail="Invalid refresh_token")
        if int(record.get("expires_at", 0)) < int(time.time()):
            raise HTTPException(status_code=400, detail="Refresh token expired")
        token_payload = self._issue_tokens(state, client_id=client_id, scope=record.get("scope") or settings.oauth_default_scope, reuse_refresh_token=refresh_token)
        self._write(state)
        return token_payload

    def _issue_tokens(
        self,
        state: dict[str, Any],
        *,
        client_id: str,
        scope: str,
        reuse_refresh_token: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        access_payload = {
            "typ": "access",
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + settings.oauth_access_token_ttl_seconds,
            "jti": secrets.token_urlsafe(12),
        }
        refresh_payload = {
            "typ": "refresh",
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + settings.oauth_refresh_token_ttl_seconds,
            "jti": secrets.token_urlsafe(12),
        }
        access_token = _encode_signed_token("sat", access_payload)
        refresh_token = reuse_refresh_token or _encode_signed_token("srt", refresh_payload)

        # Keep a legacy copy in the state store for observability and backwards
        # compatibility with older tests/tools. Validation does not depend on it
        # for sat_/srt_ tokens.
        state.setdefault("access_tokens", {})[access_token] = {
            "client_id": client_id,
            "scope": scope,
            "issued_at": now,
            "expires_at": access_payload["exp"],
        }
        if not reuse_refresh_token:
            state.setdefault("refresh_tokens", {})[refresh_token] = {
                "client_id": client_id,
                "scope": scope,
                "issued_at": now,
                "expires_at": refresh_payload["exp"],
            }
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": settings.oauth_access_token_ttl_seconds,
            "refresh_token": refresh_token,
            "scope": scope,
        }

    def validate_access_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        stateless = _decode_signed_token(token, "sat")
        if stateless and stateless.get("typ") == "access":
            return {
                "client_id": stateless.get("client_id"),
                "scope": stateless.get("scope") or settings.oauth_default_scope,
                "issued_at": stateless.get("iat"),
                "expires_at": stateless.get("exp"),
            }

        state = self._read()
        record = state["access_tokens"].get(token)
        if not record:
            return None
        if int(record.get("expires_at", 0)) < int(time.time()):
            return None
        return record


def _validate_client_secret(client: OAuthClient, client_secret: str | None) -> None:
    if client.client_secret and not secrets.compare_digest(client.client_secret, client_secret or ""):
        raise HTTPException(status_code=401, detail="Invalid OAuth client secret")


def is_loopback_host(host: str) -> bool:
    """True for the loopback hosts allowed by the OAuth 2.1 native-app flow."""
    value = (host or "").strip().lower().rstrip(".")
    return value in {"localhost", "127.0.0.1", "::1"} or value.endswith(".localhost")


def _validate_redirect_uri(uri: str, *, allow_chatgpt_defaults: bool) -> None:
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    if is_loopback_host(host):
        # RFC 8252: native apps may use http on loopback. Never allow loopback
        # with embedded credentials.
        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="loopback redirect_uri must use http or https")
        if parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="redirect_uri must not embed credentials")
        if settings.oauth_allow_loopback_redirects:
            return
        raise HTTPException(status_code=400, detail="loopback redirect_uri is disabled on this deployment")
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="redirect_uri must use https")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="redirect_uri host is required")
    if allow_chatgpt_defaults and _is_chatgpt_redirect_host(host):
        return


def _validate_redirect_for_client(client: OAuthClient, redirect_uri: str) -> None:
    _validate_redirect_uri(redirect_uri, allow_chatgpt_defaults=True)
    if client.redirect_uris:
        if redirect_uri not in client.redirect_uris:
            raise HTTPException(status_code=400, detail="redirect_uri is not registered for this OAuth client")
        return
    # Static client fallback for user-defined OAuth setup. In production,
    # configure ODOO_AGENT_OAUTH_ALLOWED_REDIRECT_URIS for tighter control.
    host = urlparse(redirect_uri).hostname or ""
    if settings.oauth_allow_loopback_redirects and is_loopback_host(host):
        return
    if not _is_chatgpt_redirect_host(host):
        raise HTTPException(status_code=400, detail="static OAuth client only permits built-in compatibility redirect hosts unless an allowlist is configured")


def _is_chatgpt_redirect_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "chatgpt.com" or host.endswith(".chatgpt.com") or host == "openai.com" or host.endswith(".openai.com")


def _verify_pkce_s256(code_verifier: str, expected_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(challenge, expected_challenge or "")


def build_redirect(redirect_uri: str, params: dict[str, Any]) -> str:
    separator = "&" if "?" in redirect_uri else "?"
    return redirect_uri + separator + urlencode({k: v for k, v in params.items() if v is not None})


def parse_basic_auth(header_value: str | None) -> tuple[str | None, str | None]:
    if not header_value or not header_value.lower().startswith("basic "):
        return None, None
    raw = header_value.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None, None
    if ":" not in decoded:
        return decoded, None
    return decoded.split(":", 1)
