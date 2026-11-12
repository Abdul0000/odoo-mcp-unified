"""Short-lived URL tokens for Qwen MCP app compatibility.

Qwen app builds may not forward custom Authorization headers to MCP endpoints.
The permanent ODOO_AGENT_API_TOKEN can work in the URL path, but that is not
ideal because URLs may appear in logs. This service mints short-lived opaque
path tokens that map to the real authorization already checked on /qwen/token.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone

from ..config import settings


class QwenTokenService:
    def __init__(self) -> None:
        self.file = settings.state_path / "qwen_short_tokens.json"

    def mint(self) -> dict[str, object]:
        ttl = max(60, int(settings.qwen_short_token_ttl_seconds))
        now = int(time.time())
        token = secrets.token_urlsafe(24)
        rows = self._read()
        self._write({k: v for k, v in rows.items() if int(v.get("expires_at_epoch", 0)) > now} | {
            token: {"created_at_epoch": now, "expires_at_epoch": now + ttl}
        })
        expires_at = datetime.fromtimestamp(now + ttl, timezone.utc).isoformat()
        base = (settings.public_base_url or "").rstrip("/") or "https://YOUR-RENDER-SERVICE.onrender.com"
        return {
            "token": token,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
            "qwen_mcp_url": f"{base}/qwen/mcp/{token}",
            "note": "Use this Qwen-only URL with Auth=None/empty headers. Rotate by minting a new token.",
        }

    def validate(self, token: str | None) -> bool:
        value = (token or "").strip()
        if not value:
            return False
        rows = self._read()
        row = rows.get(value)
        now = int(time.time())
        if not row:
            return False
        if int(row.get("expires_at_epoch", 0)) <= now:
            rows.pop(value, None)
            self._write(rows)
            return False
        return True

    def _read(self) -> dict[str, dict[str, int]]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8") or "{}")
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _write(self, rows: dict[str, dict[str, int]]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.file)
