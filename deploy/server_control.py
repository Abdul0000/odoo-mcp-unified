#!/usr/bin/env python3
"""Root-owned, fixed-action service controller. No user-provided commands."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request


def active_locks(state: Path) -> bool:
    """Live POSIX locks, not stale status strings, identify inline task/writes."""
    for directory in ('tasks','mutation-intents'):
        for path in (state / directory).glob('*.lock'):
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            finally:
                if fd is not None: os.close(fd)
    return False


def active_work(state: Path, execution: Path) -> bool:
    if active_locks(state): return True
    paths = list((state / 'jobs').glob('*.json'))
    for kind in ('process', 'agent', 'operation', 'workspace'):
        paths.extend((execution / 'records' / kind).glob('*.json'))
    for path in paths:
        try:
            row = json.loads(path.read_text())
            if row.get('status') in {'queued', 'running', 'initializing'}:
                return True
        except (OSError, ValueError):
            return True  # unreadable work state cannot authorize a restart
    return False


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(json.dumps(value).encode()); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3) as response:
            return json.load(response).get('status') == 'ok'
    except (OSError, ValueError):
        return False


def handle(control, state, execution, deploy_root, *, run=subprocess.run, wait_ready=ready, pause=time.sleep):
    request = control / 'request.json'
    try:
        fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        request.unlink(missing_ok=True)
        return
    job_id, action = value.get('id', ''), value.get('action')
    if not re.fullmatch('[a-f0-9]{32}', str(job_id)) or action not in {'restart', 'update'}:
        request.unlink(missing_ok=True)
        return
    job = control / 'jobs' / f'{job_id}.json'
    def progress(status, message):
        write_json(job, {'id': job_id, 'action': action, 'status': status, 'message': message,
                         'terminal': status in {'succeeded', 'failed'}, 'updated_at': time.time()})
    try:
        if time.time() - float(value.get('created_at', 0)) > 900:
            progress('failed', 'This request expired. Submit a new server action.')
            return
        write_json(control / 'active.json', value)
        pause(2)  # allow the HTTP request to return before stopping its API
        if active_work(state, execution):
            progress('failed', 'Active connector work was detected. Nothing was restarted; retry when it finishes.')
            return
        old = (deploy_root / 'current').resolve()
        if action == 'update':
            sha = old.name
            if not re.fullmatch('[a-f0-9]{40}', sha):
                raise RuntimeError('No valid deployed release')
            progress('running', 'Updating MCP from master and running deployment checks…')
            # Existing deploy unit fetches current master, tests and promotes it.
            # The SHA is only the validated systemd instance key, never a command.
            result = run(['systemctl', 'start', f'odoo-mcp-deploy@{sha}.service'], capture_output=True, timeout=720)
            if result.returncode:
                raise RuntimeError('Update did not pass deployment checks')
        if action == 'restart' or (deploy_root / 'current').resolve() == old:
            progress('running', 'Restarting MCP API and execution worker…')
            result = run(['systemctl', 'restart', 'odoo-mcp.service', 'odoo-mcp-execution.service'], capture_output=True, timeout=90)
            if result.returncode:
                raise RuntimeError('Services could not restart')
        progress('running', 'Waiting for API and worker readiness…')
        for _ in range(60):
            if wait_ready():
                progress('succeeded', 'MCP is up to date and services are ready.' if action == 'update' else 'API and worker restarted successfully.')
                return
            pause(1)
        raise RuntimeError('Services did not become ready')
    except Exception:
        progress('failed', 'The server action could not complete. Check the Oracle service/deployment logs before retrying.')
    finally:
        request.unlink(missing_ok=True)
        try:
            active = json.loads((control / 'active.json').read_text())
            if active.get('id') == job_id:
                (control / 'active.json').unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def main():
    if os.geteuid() != 0:
        raise SystemExit('The controller must run through its root-owned systemd unit.')
    handle(Path(os.getenv('ODOO_AGENT_CONTROL_DIR', '/var/lib/odoo-mcp/control')),
           Path(os.getenv('ODOO_AGENT_STATE_DIR', '/var/lib/odoo-mcp/state')),
           Path(os.getenv('ODOO_AGENT_EXECUTION_ROOT', '/var/lib/odoo-mcp/execution')),
           Path('/opt/odoo-mcp'))


if __name__ == '__main__':
    main()
