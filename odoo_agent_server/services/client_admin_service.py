"""Validated, optimistic client editing for the small operator UI."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from ..config import get_odoo_clients, settings
from .client_gate_service import ClientGateService
from .client_settings_store import FILE_NAME, atomic_json, read_overrides, settings_lock
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .store_service import JsonStore

SECRETS = {'token', 'odoo_password', 'odoo_api_key'}
FIELDS = {
    'repo', 'branch', 'path', 'environment', 'odoo_url', 'odoo_db', 'odoo_login',
    'odoo_verify_ssl', 'odoosh_ssh_host', 'odoosh_ssh_user', 'odoosh_ssh_port',
    'odoosh_db_name', 'odoosh_ssh_key_path', 'odoosh_odoo_path', 'odoosh_user_addons_path',
} | SECRETS


def clean_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname:
        # Keep host/port, dropping any embedded credentials and query secrets.
        host = parsed.netloc.rsplit('@', 1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
    return value


class ClientAdminService:
    def _client(self, name):
        for client in get_odoo_clients():
            if str(client.get('name', '')).casefold() == name.casefold():
                return client
        raise HTTPException(404, 'Client not found')

    @staticmethod
    def revision(client):
        return hashlib.sha256(json.dumps(client, sort_keys=True, default=str).encode()).hexdigest()

    def detail(self, name):
        client = self._client(name)
        values = {key: client.get(key) for key in FIELDS - SECRETS}
        values['repo'] = clean_url(str(client.get('repo') or ''))
        validation = ClientGateService().validation_state(name)
        return {'name': client['name'], 'revision': self.revision(client), 'values': values,
                'configured_secrets': {key: bool(client.get(key)) for key in SECRETS},
                'repository_credentials': {
                    'configured': bool(client.get('token')),
                    'behavior': 'Blank keeps this client’s existing token; Save & Validate checks remote repository access.',
                },
                'validation': {key: validation.get(key) for key in (
                    'validated', 'coding_ready', 'deploy_ready', 'data_query_ready',
                    'ssh_ready', 'validated_at', 'stale_reason', 'full_validation_passed', 'summary')},
                'settings_source': 'saved' if name.casefold() in read_overrides(settings.state_path)['clients'] else 'environment'}

    def list(self):
        return {'clients': [{'name': c.get('name'), 'branch': c.get('branch'),
                              'environment': c.get('environment')} for c in get_odoo_clients()], 'defaults': self.defaults()}

    def defaults(self):
        clients = get_odoo_clients()
        roots = settings.allowed_roots
        root = roots[0] if roots else settings.state_path / 'repos'
        for client in clients:
            if client.get('path'):
                candidate = Path(str(client['path'])).parent
                if any(candidate.is_relative_to(allowed) for allowed in roots):
                    root = candidate
                    break
        keys = {str(c['odoosh_ssh_key_path']) for c in clients if c.get('odoosh_ssh_key_path')}
        return {'branch': 'staging', 'environment': 'staging', 'repo': '',
                'odoo_verify_ssl': True, 'odoosh_ssh_port': 22,
                'odoosh_odoo_path': '/home/odoo/src/odoo',
                'odoosh_user_addons_path': '/home/odoo/src/user',
                'odoosh_ssh_key_path': next(iter(keys)) if len(keys) == 1 else '',
                'path_root': str(root)}

    def create(self, name, values):
        from .server_control_service import assert_no_maintenance
        assert_no_maintenance()
        name = name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 or c in '/\\' for c in name):
            raise HTTPException(422, 'Enter a client name without slashes or control characters')
        with settings_lock(settings.state_path):
            from deploy.server_control import active_work
            if active_work(settings.state_path, Path(settings.execution_root)):
                raise HTTPException(409, 'Wait for active connector work to finish before adding a client')
            clients = get_odoo_clients()
            if any(str(c.get('name', '')).casefold() == name.casefold() for c in clients):
                raise HTTPException(409, 'A client with this name already exists')
            defaults = self.defaults()
            root = defaults.pop('path_root')
            slug = re.sub(r'[^a-z0-9_-]+', '-', name.lower()).strip('-') or 'client'
            current = {'name': name, **defaults, 'path': str(Path(root) / slug)}
            changes = self._changes(current, values)
            merged = {**current, **changes}
            if not all(merged.get(key) for key in ('repo', 'branch', 'path')):
                raise HTTPException(422, 'Repository, branch and checkout path are required')
            self._validate(merged, merged.keys() - {'name'})
            target = Path(merged['path']).resolve()
            for client in clients:
                if client.get('path'):
                    existing = Path(str(client['path'])).expanduser().resolve()
                    if target.is_relative_to(existing) or existing.is_relative_to(target):
                        raise HTTPException(409, 'Choose a separate checkout path for the new client')
            data = read_overrides(settings.state_path)
            data['clients'][name.casefold()] = merged
            atomic_json(settings.state_path / FILE_NAME, data)
            JsonStore().log_audit('client.settings.create', 'saved', 'Client added', {'client_name': name})
        return {'saved': True, 'created': True, 'client': self.detail(name)}

    def _ensure_idle(self, client):
        from deploy.server_control import active_locks
        if active_locks(settings.state_path):
            raise HTTPException(409,'An inline task or functional operation is active. Wait for it to finish before changing client settings.')
        name = str(client['name']).casefold()
        for job in JsonStore().list_jobs(limit=1000):
            if job.status.value in {'queued', 'running'} and str(job.request.get('client_name', '')).casefold() == name:
                raise HTTPException(409, 'This client has active work. Wait for it to finish before changing settings.')
        if settings.execution_enabled:
            from .execution_store import ExecutionStore
            store = ExecutionStore()
            for kind in ('process', 'agent', 'operation'):
                for row in store.pending(kind):
                    wid = row.get('request', {}).get('workspace_id')
                    if wid:
                        workspace = store.get(wid, 'workspace')
                        if str(workspace.get('client_name', '')).casefold() == name:
                            raise HTTPException(409, 'This client has active commands or publishing. Wait for completion.')
            for row in store.recent('workspace', limit=64):
                if row.get('status') == 'initializing' and str(row.get('request', {}).get('client_name', '')).casefold() == name:
                    raise HTTPException(409, 'This client is preparing a workspace. Wait for completion.')

    def save(self, name, revision, values):
        from .server_control_service import assert_no_maintenance
        assert_no_maintenance()
        unknown = set(values) - FIELDS
        if unknown:
            raise HTTPException(422, 'Unsupported client settings: ' + ', '.join(sorted(unknown)))
        with settings_lock(settings.state_path):
            current = self._client(name)
            if revision != self.revision(current):
                raise HTTPException(409, 'Settings changed in another session. Reload this client before saving.')
            changes = self._changes(current, values)
            changes.update(self._connection_updates(current, changes, values))
            merged = {**current, **changes}
            self._validate(merged, changes)
            if changes:
                self._ensure_idle(current)
                data = read_overrides(settings.state_path)
                key = str(current['name']).casefold()
                data['clients'][key] = {**data['clients'].get(key, {}), **changes}
                atomic_json(settings.state_path / FILE_NAME, data)
                JsonStore().log_audit('client.settings.update', 'saved', 'Client settings updated',
                                      {'client_name': current['name'], 'fields': sorted(changes)})
        return {'saved': True, 'changed': bool(changes), 'client': self.detail(name)}

    def _connection_updates(self, current, changes, provided):
        """Keep a standard Odoo.sh build profile coherent when its URL/db moves."""
        updates = {}
        merged = {**current, **changes}
        if ('branch' in changes or 'repo' in changes) and 'path' not in provided and current.get('path'):
            # A target change gets its own deterministic cache. Never switch or
            # discard the previous checkout: it may contain uncommitted work.
            identity = json.dumps([merged.get('repo'), merged.get('branch')], separators=(',', ':'))
            suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
            slug = re.sub(r'[^a-z0-9_-]+', '-', str(current['name']).lower()).strip('-') or 'client'
            updates['path'] = str(Path(current['path']).parent / (slug + '--' + suffix))
            target = Path(updates['path']).resolve()
            for other in get_odoo_clients():
                if str(other.get('name', '')).casefold() == str(current['name']).casefold() or not other.get('path'):
                    continue
                other_path = Path(other['path']).expanduser().resolve()
                if target.is_relative_to(other_path) or other_path.is_relative_to(target):
                    raise HTTPException(409, 'Automatic checkout overlaps another client; choose a separate path.')
        try:
            old_host = urlsplit(str(current.get('odoo_url') or '')).hostname
            new_host = urlsplit(str(merged.get('odoo_url') or '')).hostname
        except ValueError:
            return updates  # _validate reports the invalid URL without guessing targets
        if ('odoo_url' in changes and new_host and new_host.endswith('.dev.odoo.com')
                and current.get('odoosh_ssh_host') == old_host and 'odoosh_ssh_host' not in provided):
            updates['odoosh_ssh_host'] = new_host
            old_id = re.search(r'-(\d+)\.dev\.odoo\.com$', old_host or '')
            new_id = re.search(r'-(\d+)\.dev\.odoo\.com$', new_host)
            if old_id and new_id and str(current.get('odoosh_ssh_user')) == old_id.group(1) and 'odoosh_ssh_user' not in provided:
                updates['odoosh_ssh_user'] = new_id.group(1)
        suffix = '.dev.odoo.com'
        if ('odoo_url' in changes and old_host and new_host
                and old_host.endswith(suffix) and new_host.endswith(suffix)
                and current.get('odoo_db') == old_host[:-len(suffix)]
                and 'odoo_db' not in provided):
            updates['odoo_db'] = new_host[:-len(suffix)]
        if ('odoo_db' in changes or 'odoo_db' in updates) and 'odoosh_db_name' not in provided and (not current.get('odoosh_db_name') or current['odoosh_db_name'] == current.get('odoo_db')):
            updates['odoosh_db_name'] = updates.get('odoo_db', merged.get('odoo_db'))
        if (merged.get('odoosh_ssh_host') or updates.get('odoosh_ssh_host')) and (merged.get('odoosh_ssh_user') or updates.get('odoosh_ssh_user')):
            defaults = self.defaults()
            for key in ('odoosh_odoo_path', 'odoosh_user_addons_path', 'odoosh_ssh_key_path'):
                if key not in provided and not merged.get(key) and defaults.get(key):
                    if key != 'odoosh_ssh_key_path' or not merged.get('odoosh_ssh_private_key_b64'):
                        updates[key] = defaults[key]
        return updates

    @staticmethod
    def _changes(current, values):
        unknown = set(values) - FIELDS
        if unknown:
            raise HTTPException(422, 'Unsupported client settings: ' + ', '.join(sorted(unknown)))
        changes = {}
        for key, value in values.items():
            if key in SECRETS:
                if value is None or value == '':
                    continue
                if not isinstance(value, str) or len(value) > 4096:
                    raise HTTPException(422, 'Invalid credential value')
            elif key == 'odoo_verify_ssl':
                if not isinstance(value, bool):
                    raise HTTPException(422, 'TLS verification must be true or false')
            elif key == 'odoosh_ssh_port':
                if type(value) is not int or not 1 <= value <= 65535:
                    raise HTTPException(422, 'SSH port must be between 1 and 65535')
            else:
                if value is None:
                    value = ''
                if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise HTTPException(422, 'Invalid setting value')
                value = value.strip()
            if key == 'repo' and value == clean_url(str(current.get('repo') or '')):
                continue  # Preserve existing embedded credentials when untouched.
            if value != current.get(key):
                changes[key] = value
        return changes

    @staticmethod
    def _validate(client, changes):
        if 'branch' in changes and not RepoService()._safe_branch_name(client.get('branch', '')):
            raise HTTPException(422, 'Enter a valid Git branch name')
        for key in ('repo', 'odoo_url'):
            if key not in changes:
                continue
            if key == 'odoo_url' and not client[key]:
                continue
            try:
                parsed = urlsplit(client[key])
                hostname = parsed.hostname
            except ValueError:
                raise HTTPException(422, 'Enter a valid HTTP(S) URL') from None
            if (parsed.scheme not in {'http', 'https'} or not hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise HTTPException(422, 'Use an HTTP(S) URL without embedded credentials; enter credentials separately.')
        if 'environment' in changes and client['environment'] not in {'test', 'development', 'staging', 'production'}:
            raise HTTPException(422, 'Choose a supported environment')
        for key in ('odoosh_ssh_host', 'odoosh_ssh_user'):
            if key in changes and client[key] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', client[key]):
                raise HTTPException(422, 'Enter a hostname and SSH username separately')
        if 'path' in changes:
            path = Path(client['path']).expanduser()
            if not path.is_absolute():
                raise HTTPException(422, 'Checkout path must be absolute')
            resolve_allowed_path(str(path))
        for key in ('odoosh_odoo_path', 'odoosh_user_addons_path', 'odoosh_ssh_key_path'):
            if key in changes and client[key] and not Path(client[key]).is_absolute():
                raise HTTPException(422, 'Server paths must be absolute')

    def validation_job(self, job_id):
        job = JsonStore().get_job(job_id)
        if job.job_type != 'client_validation':
            raise HTTPException(404, 'Validation job not found')
        result = job.result or {}
        text = str(result.get('summary') or result.get('error') or '')[:2000]
        details = '\n'.join(str(line) for line in (result.get('validation_log') or [])[-20:])[:6000]
        for client in get_odoo_clients():
            for key in SECRETS | {'odoosh_ssh_private_key_b64'}:
                secret = str(client.get(key) or '')
                if secret:
                    text = text.replace(secret, '[hidden]')
                    details = details.replace(secret, '[hidden]')
        return {'job_id': job.id, 'status': job.status.value,
                'terminal': job.status.value not in {'queued', 'running'},
                'validated': bool(result.get('validated')), 'coding_ready': bool(result.get('coding_ready')),
                'deploy_ready': bool(result.get('deploy_ready')), 'full_validation_passed': bool(result.get('full_validation_passed')),
                'data_query_ready': bool(result.get('data_query_ready')), 'summary': text,
                'details': ClientGateService._redact(details),
                'checks': result.get('checks') or {},
                'steps': [{'name': step.name, 'status': step.status} for step in job.steps]}
