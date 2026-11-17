"""Per-request write-ahead intent: serialize duplicates and stop uncertain replay."""

from functools import wraps
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException

from ..config import get_odoo_clients, settings
from .client_settings_store import atomic_json


def identity(client_name):
    client = next((c for c in get_odoo_clients() if str(c.get("name", "")).casefold() == client_name.casefold()), None)
    scope = [client.get(k) for k in ("odoo_url", "odoo_db", "odoo_login")] if client else [settings.odoo_base_url, settings.odoo_db_name, settings.odoo_login]
    return hashlib.sha256(json.dumps(scope).encode()).hexdigest()


def path(client_name, request_id):
    key = hashlib.sha256((client_name.casefold() + "\0" + request_id).encode()).hexdigest()
    root = settings.state_path / "mutation-intents"
    root.mkdir(parents=True, exist_ok=True)
    return root / (key + ".json")


def guarded(method):
    @wraps(method)
    def call(self, request):
        from .server_control_service import assert_no_maintenance

        assert_no_maintenance()
        client, _rpc = self._runtime(request.client_name)
        with self.store._file_lock(path(self._client_key(client), request.request_id)):
            assert_no_maintenance()
            return method(self, request)

    return call


def read(client_name, request_id):
    try:
        return json.loads(path(client_name, request_id).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise HTTPException(503, "Mutation checkpoint is unreadable; do not repeat the write.") from None


def write(client_name, request_id, operation, payload_hash, response=None):
    atomic_json(
        path(client_name, request_id),
        {
            "client_name": client_name,
            "request_id": request_id,
            "operation": operation,
            "payload_hash": payload_hash,
            "runtime_identity": identity(client_name),
            "state": "completed" if response is not None else "dispatched",
            "response": response,
        },
    )
