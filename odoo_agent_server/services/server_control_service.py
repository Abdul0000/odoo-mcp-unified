"""Submit fixed Oracle operations to a root-owned systemd controller."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import HTTPException

from ..config import settings
from .client_settings_store import atomic_json, settings_lock
from .store_service import JsonStore


def maintenance_pending():
    for name in ('active.json', 'request.json'):
        try:
            data = json.loads((Path(settings.server_control_dir) / name).read_text())
            if time.time() - float(data['created_at']) < 900:
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError):
            return True
    return False


def assert_no_maintenance():
    if maintenance_pending():
        raise HTTPException(503, 'Oracle maintenance is in progress. Retry after services are ready.', headers={'Retry-After': '5'})


class ServerControlService:
    def __init__(self):
        self.root = Path(settings.server_control_dir)

    def status(self):
        active = None
        if maintenance_pending():
            for name in ('request.json', 'active.json'):
                try:
                    active = json.loads((self.root / name).read_text()).get('id')
                    break
                except (OSError, ValueError):
                    pass
        return {'available': (self.root / 'enabled').is_file(), 'active_job_id': active}

    def request(self, action):
        if action not in {'restart', 'update'}:
            raise HTTPException(422, 'Unsupported server action')
        if not self.status()['available']:
            raise HTTPException(503, 'Oracle service controller is not installed')
        from deploy.server_control import active_work
        with settings_lock(settings.state_path):
            if maintenance_pending():
                raise HTTPException(409, 'A server action is already in progress')
            if active_work(settings.state_path, Path(settings.execution_root)):
                raise HTTPException(409, 'Active connector work detected. Wait for coding, validation and deployments to finish.')
            job_id = uuid4().hex
            value = {'id': job_id, 'action': action, 'created_at': time.time()}
            atomic_json(self.root / 'jobs' / f'{job_id}.json', {**value, 'status': 'queued', 'message': 'Oracle action queued.', 'terminal': False})
            atomic_json(self.root / 'request.json', value)
            JsonStore().log_audit('server.' + action, 'queued', 'Oracle server action requested', {'job_id': job_id})
        return {'id': job_id, 'status': 'queued', 'terminal': False}

    def job(self, job_id):
        if not re.fullmatch('[a-f0-9]{32}', job_id):
            raise HTTPException(404, 'Server action not found')
        try:
            return json.loads((self.root / 'jobs' / f'{job_id}.json').read_text())
        except FileNotFoundError:
            raise HTTPException(404, 'Server action not found') from None
