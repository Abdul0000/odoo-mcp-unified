from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_mcp_system_prompt() -> str:
    """Return the original MCP system prompt.

    MCP_SYSTEM_PROMPT.md remains the source of truth for normal Odoo MCP
    behaviour. Client selection uses CLIENT_CHECK_SYSTEM_PROMPT.md before this
    prompt is loaded; do not duplicate or edit MCP_SYSTEM_PROMPT.md for that
    gate.
    """
    prompt_path = _project_root() / "MCP_SYSTEM_PROMPT.md"
    return prompt_path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_client_check_system_prompt() -> str:
    """Return the pre-MCP client selection/validation gate prompt."""
    prompt_path = _project_root() / "CLIENT_CHECK_SYSTEM_PROMPT.md"
    return prompt_path.read_text(encoding="utf-8")


def shared_prompt_payload() -> dict[str, str]:
    prompt = load_mcp_system_prompt()
    return {
        "source": "MCP_SYSTEM_PROMPT.md",
        "name": "mcp_system_prompt",
        "text": prompt,
        "content": prompt,
    }


def client_check_prompt_payload() -> dict[str, str]:
    prompt = load_client_check_system_prompt()
    return {
        "source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
        "name": "client_check_system_prompt",
        "text": prompt,
        "content": prompt,
    }
