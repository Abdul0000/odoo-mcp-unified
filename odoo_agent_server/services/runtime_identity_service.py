from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


SERVICE_VERSION = "1.8.1"
_STARTED_AT = datetime.now(timezone.utc).isoformat()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMMIT_ENV_KEYS = (
    "ODOO_AGENT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "GIT_COMMIT_SHA",
    "COMMIT_SHA",
)


def _clean_commit_sha(value: str | None) -> str | None:
    candidate = str(value or "").strip()
    return candidate.lower() if re.fullmatch(r"[A-Fa-f0-9]{7,64}", candidate) else None


@lru_cache(maxsize=1)
def _commit_identity() -> tuple[str, str]:
    for key in _COMMIT_ENV_KEYS:
        value = _clean_commit_sha(os.getenv(key))
        if value:
            return value, f"environment:{key}"
    try:
        result = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable", "unavailable"
    value = _clean_commit_sha(result.stdout) if result.returncode == 0 else None
    return (value, "git") if value else ("unavailable", "unavailable")


@lru_cache(maxsize=1)
def _prompt_hashes() -> tuple[str, str]:
    def digest(name: str) -> str:
        try:
            content = (_PROJECT_ROOT / name).read_bytes()
        except OSError:
            return "unavailable"
        return hashlib.sha256(content).hexdigest()

    return digest("MCP_SYSTEM_PROMPT.md"), digest("CLIENT_CHECK_SYSTEM_PROMPT.md")


def runtime_identity() -> dict[str, Any]:
    commit_sha, commit_source = _commit_identity()
    prompt_sha256, client_prompt_sha256 = _prompt_hashes()
    return {
        "service_version": SERVICE_VERSION,
        "commit_sha": commit_sha,
        "commit_source": commit_source,
        "prompt_sha256": prompt_sha256,
        "client_prompt_sha256": client_prompt_sha256,
        "started_at": _STARTED_AT,
    }
