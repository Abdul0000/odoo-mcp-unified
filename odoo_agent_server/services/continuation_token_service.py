from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import zlib
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings


_TOKEN_VERSION = "r1"
_TOKEN_KIND = "odoo_code_change"
_TOKEN_MAX_CHARS = 20_000
_SENSITIVE_KEY = re.compile(r"password|passwd|secret|api[_-]?key|authorization|cookie", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key)\b(\s*[:=]\s*)[^\s,;]+"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\bAuthorization\b(\s*[:=]\s*)(?:Bearer\s+)?[^\s,;]+"
)


class ContinuationTokenError(ValueError):
    """A client-carried continuation token is invalid or cannot be issued."""


class _ContinuationClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    kind: str
    issued_at: int
    expires_at: int
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    request: dict[str, Any]


class ContinuationTokenService:
    """Issue compact signed state that an MCP client can carry across restarts.

    Tokens are integrity-protected, not encrypted. The resume contract excludes
    writes, credentials, force authorizations, and server-local session IDs.
    """

    _SAFE_REQUEST_FIELDS = {
        "client_name",
        "project_path",
        "module_name",
        "create_module",
        "task_description",
        "search_patterns",
        "module_hint",
        "task_type",
        "intent",
        "target_hint",
        "change_request",
        "patch_strategy",
        "target_text",
        "style",
        "allow_autopatch",
        "target_files",
        "operation_id",
        "commit_message",
        "auto_deploy",
        "deploy_mode",
        "max_search_matches",
        "clarification_answer",
        "verification_assertion",
    }

    def issue_code_change(self, payload: Any, *, now: int | None = None) -> dict[str, Any]:
        issued_at = int(time.time() if now is None else now)
        request = self._resume_request(payload)
        request_json = self._canonical_json(request)
        claims = _ContinuationClaims(
            kind=_TOKEN_KIND,
            issued_at=issued_at,
            expires_at=issued_at + int(settings.continuation_token_ttl_seconds),
            request_fingerprint=hashlib.sha256(request_json).hexdigest(),
            request=request,
        )
        packed = zlib.compress(self._canonical_json(claims.model_dump(mode="json")), level=9)
        body = self._b64url_encode(packed)
        signed = f"{_TOKEN_VERSION}.{body}"
        signature = self._b64url_encode(
            hmac.new(self._secret(), signed.encode("ascii"), hashlib.sha256).digest()
        )
        token = f"{signed}.{signature}"
        if len(token) > _TOKEN_MAX_CHARS:
            raise ContinuationTokenError(
                "The safe continuation contract is too large for chat transport; narrow the task description."
            )
        return {
            "resume_token": token,
            "resume_token_expires_at": datetime.fromtimestamp(
                claims.expires_at, tz=timezone.utc
            ).isoformat(),
            "resume_request_fingerprint": claims.request_fingerprint,
        }

    def decode_code_change(self, token: str, *, now: int | None = None) -> dict[str, Any]:
        value = str(token or "").strip()
        if not value or len(value) > _TOKEN_MAX_CHARS:
            raise ContinuationTokenError("Continuation token is missing or exceeds its size limit.")
        try:
            version, body, signature = value.split(".", 2)
        except ValueError as exc:
            raise ContinuationTokenError("Continuation token format is invalid.") from exc
        if version != _TOKEN_VERSION:
            raise ContinuationTokenError("Continuation token version is not supported.")
        expected = self._b64url_encode(
            hmac.new(self._secret(), f"{version}.{body}".encode("ascii"), hashlib.sha256).digest()
        )
        if not secrets.compare_digest(signature, expected):
            raise ContinuationTokenError("Continuation token signature is invalid.")
        try:
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(self._b64url_decode(body), 100_001)
            if (
                len(raw) > 100_000
                or not decompressor.eof
                or decompressor.unconsumed_tail
                or decompressor.unused_data
            ):
                raise ContinuationTokenError("Continuation token payload exceeds its safe limit.")
            claims = _ContinuationClaims.model_validate_json(raw)
        except ContinuationTokenError:
            raise
        except (ValueError, zlib.error, ValidationError) as exc:
            raise ContinuationTokenError("Continuation token payload is invalid.") from exc
        current = int(time.time() if now is None else now)
        if claims.version != 1:
            raise ContinuationTokenError("Continuation token claims version is not supported.")
        if claims.kind != _TOKEN_KIND:
            raise ContinuationTokenError("Continuation token belongs to another workflow.")
        if claims.expires_at < current:
            raise ContinuationTokenError("Continuation token expired; resubmit the original task.")
        if claims.issued_at > current + 300:
            raise ContinuationTokenError("Continuation token issue time is invalid.")
        request_json = self._canonical_json(claims.request)
        if not hmac.compare_digest(
            claims.request_fingerprint,
            hashlib.sha256(request_json).hexdigest(),
        ):
            raise ContinuationTokenError("Continuation token request fingerprint is invalid.")
        return dict(claims.request)

    def _resume_request(self, payload: Any) -> dict[str, Any]:
        source = payload.model_dump(mode="json", exclude_none=True)
        request = {
            key: self._scrub(source[key])
            for key in self._SAFE_REQUEST_FIELDS
            if key in source
        }
        request.update({
            "wait_for_result": False,
            "force_new_deploy": False,
            "verification_override": "strict",
            "batch_index": 0,
            "total_batches": 1,
        })
        return request

    def _secret(self) -> bytes:
        for candidate in (
            settings.continuation_token_secret,
            settings.oauth_token_secret,
            settings.api_token,
        ):
            secret = str(candidate or "").strip()
            if secret and secret != "change-me":
                return secret.encode("utf-8")
        raise ContinuationTokenError(
            "Configure a stable ODOO_AGENT_CONTINUATION_TOKEN_SECRET before using client-carried resume tokens."
        )

    @classmethod
    def _scrub(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._scrub(child)
                for key, child in value.items()
                if not _SENSITIVE_KEY.search(str(key))
            }
        if isinstance(value, list):
            return [cls._scrub(child) for child in value]
        if isinstance(value, str):
            redacted = _AUTHORIZATION_VALUE.sub(r"Authorization\1<redacted>", value)
            return _SENSITIVE_VALUE.sub(r"\1\2<redacted>", redacted)
        return value

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _b64url_encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64url_decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
