"""Admin-only read/stage controls for non-secret runtime configuration."""
from pathlib import Path

from fastapi import HTTPException

from ..config import settings
from ..runtime_settings import FIELDS, FILE_NAME, read_values, revision, validate_values
from .client_settings_store import atomic_json, settings_lock
from .server_control_service import assert_no_maintenance
from .store_service import JsonStore


class RuntimeAdminService:
    @staticmethod
    def _revision(saved):
        return revision({'saved': saved, 'effective': {key: getattr(settings, key) for key in FIELDS}})

    def _read(self):
        try:
            return read_values(settings.state_path)
        except (OSError, ValueError):
            raise HTTPException(503, 'Saved server settings are unreadable. Repair runtime_settings.json before continuing.') from None

    def detail(self):
        saved = self._read()
        effective = {key: getattr(settings, key) for key in FIELDS}
        desired = {**effective, **saved}
        return {
            'revision': self._revision(saved), 'values': desired, 'effective': effective,
            'restart_required': desired != effective,
            'fields': [{'name': key, 'group': spec[0], 'label': spec[1], 'help': spec[2],
                        'min': spec[3], 'max': spec[4], 'env': type(settings).model_fields[key].alias,
                        'source': 'saved' if key in saved else 'environment'} for key, spec in FIELDS.items()],
            'system': {'execution_enabled': settings.execution_enabled,
                       'functional_operations_enabled': settings.functional_operations_enabled,
                       'state_dir': str(settings.state_path), 'execution_root': settings.execution_root,
                       'approval_required': settings.require_approval,
                       'github_token_configured': bool(settings.github_token),
                       'browser_configured': bool(settings.cdp_url)},
        }

    def save(self, expected_revision, values):
        try:
            validate_values(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with settings_lock(settings.state_path):
            assert_no_maintenance()
            saved = self._read()
            if expected_revision != self._revision(saved):
                raise HTTPException(409, 'Server settings changed in another session. Reload before saving.')
            changed = {key: value for key, value in values.items() if value != saved.get(key, getattr(settings, key))}
            if changed:
                from deploy.server_control import active_work
                if active_work(settings.state_path, Path(settings.execution_root)):
                    raise HTTPException(409, 'Wait for active connector work to finish before changing server settings.')
                atomic_json(settings.state_path / FILE_NAME, {'version': 1, 'values': {**saved, **changed}})
                JsonStore().log_audit('server.settings.update', 'saved', 'Server settings staged for restart', {'fields': sorted(changed)})
        return {'saved': True, 'changed': bool(changed), **self.detail()}
