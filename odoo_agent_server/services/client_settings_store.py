"""Atomic client overrides shared by API and worker; no privileged env edits."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

FILE_NAME = 'client_settings.json'


def read_overrides(root: Path) -> dict:
    try:
        data = json.loads((root / FILE_NAME).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': 1, 'clients': {}}
    except (OSError, ValueError) as exc:
        raise HTTPException(503, 'Saved client settings are unreadable; repair storage before continuing.') from exc
    if (not isinstance(data, dict) or data.get('version') != 1
            or not isinstance(data.get('clients'), dict)
            or any(not isinstance(row, dict) for row in data['clients'].values())):
        raise HTTPException(503, 'Saved client settings have an unsupported format.')
    return data


def apply_client_overrides(clients: list[dict], root: Path) -> list[dict]:
    overrides = read_overrides(root)['clients']
    return [{**client, **overrides.get(str(client.get('name', '')).casefold(), {}),
             'name': client.get('name')} for client in clients]


@contextmanager
def settings_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / 'client_settings.lock', os.O_CREAT | os.O_RDWR, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
