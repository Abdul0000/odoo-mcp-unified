"""Small MCP surface; action-specific fields are validated by the relay."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class BrowserExtensionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_name: str
    action: Literal['status', 'poll', 'tabs', 'open', 'navigate', 'snapshot', 'click', 'fill', 'select', 'press', 'scroll', 'assert', 'screenshot'] = 'status'
    device_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    command_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    request_id: str | None = Field(default=None, min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=12)
    approval_id: str | None = None


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tab_id: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=4000)
    snapshot_id: str | None = Field(default=None, max_length=80)
    ref: str | None = Field(default=None, pattern=r'^e[0-9]{1,4}$')
    text: str | None = Field(default=None, max_length=12000)
    value: str | None = Field(default=None, max_length=4000)
    key: Literal['Enter', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp'] | None = None
    dy: int = Field(default=600, ge=-4000, le=4000)
    expected_text: str | None = Field(default=None, min_length=1, max_length=1000)
    expected_value: str | None = Field(default=None, max_length=1000)
