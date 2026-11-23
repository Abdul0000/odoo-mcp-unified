"""Opaque, credential-bound clarification handles with durable replay receipts.

Only business clarification_answer fields are bridged. Authorization/approval
gates and model-generated technical arguments retain their existing flow.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import time

from mcp.shared.exceptions import MCPError

from ..config import settings
from .client_settings_store import atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class McpClarificationService:
    ttl = 600
    max_pending = 256

    def __init__(self):
        self.root = settings.state_path / "mcp-clarifications"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def continuation(structured):
        question = structured.get("input_request") or {}
        if question.get("kind") != "clarification":
            return None
        call = copy.deepcopy(question.get("resume_call") or structured.get("next_call"))
        if not isinstance(call, dict) or not isinstance(call.get("arguments"), dict):
            return None
        from ..routers.tools import _TOOL_INPUT_MODELS, internal_tool_name
        name = internal_tool_name(str(call.get("tool") or ""))
        if name == "task.run":
            nested = internal_tool_name(str(call["arguments"].get("tool") or ""))
            model = _TOOL_INPUT_MODELS.get(nested)
            field = "arguments.clarification_answer"
        else:
            model = _TOOL_INPUT_MODELS.get(name)
            field = "clarification_answer"
        if model is None or "clarification_answer" not in model.model_fields:
            return None
        return question, call, field

    def issue(self, principal, name, arguments, call, field, fallback):
        # Bound storage without evicting a live dispatch/receipt. Lock files are
        # fixed hash buckets, so clients cannot grow a second unbounded catalog.
        if len(json.dumps({"call": call, "fallback": fallback}, ensure_ascii=False).encode()) > 256_000:
            return None
        with self._lock("catalog"):
            now = time.time()
            paths = list(self.root.glob("mcp-input-*.json"))
            for path in paths:
                if path.stat().st_mtime < now - self.ttl * 2:
                    try:
                        with self._lock(path.stem):
                            path.unlink(missing_ok=True)
                    except MCPError:
                        pass
            if len(list(self.root.glob("mcp-input-*.json"))) >= self.max_pending:
                return None  # Existing custom input_request remains usable.
            state = "mcp-input-" + secrets.token_hex(32)
            atomic_json(self.root / (state + ".json"), {
                "principal": principal, "fingerprint": digest({"name": name, "arguments": arguments}),
                "expires": now + self.ttl, "phase": "waiting", "call": call, "field": field, "fallback": fallback,
            })
            return state

    @contextmanager
    def _lock(self, key):
        bucket = "catalog" if key == "catalog" else hashlib.sha256(key.encode()).hexdigest()[:2]
        fd = os.open(self.root / (bucket + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                raise MCPError(-32602, "Clarification is busy; retry the same request") from None
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def claim(self, state, principal, name, arguments):
        if not re.fullmatch(r"mcp-input-[0-9a-f]{64}", state):
            raise MCPError(-32602, "Invalid clarification state")
        with self._lock(state):
            path = self.root / (state + ".json")
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                raise MCPError(-32602, "Clarification state unavailable") from None
            if (record["expires"] < time.time() or record["principal"] != principal
                    or record["fingerprint"] != digest({"name": name, "arguments": arguments})):
                raise MCPError(-32602, "Clarification state expired or belongs to another request")
            if record["phase"] == "dispatching":
                raise MCPError(-32602, "Prior outcome requires reconciliation; resume the existing task or job")
            yield path, record

    @staticmethod
    def answered_call(record, answer):
        call = copy.deepcopy(record["call"])
        args = call["arguments"]
        if record["field"].startswith("arguments."):
            args = args.setdefault("arguments", {})
        args["clarification_answer"] = answer
        return call
