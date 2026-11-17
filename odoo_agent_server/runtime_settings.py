"""Allowlisted operator settings shared by API and worker on startup."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FILE_NAME = 'runtime_settings.json'
# Authentication, filesystem roots, executable/image paths and permission flags
# deliberately remain operator-managed. Values here cannot grant new access.
FIELDS = {
    'execution_max_workers': ('runtime', 'Command workers', 'Parallel command workers.', 1, 4),
    'execution_max_pending': ('runtime', 'Command queue limit', 'Maximum pending command jobs.', 1, 100),
    'background_max_workers': ('runtime', 'Background workers', 'Keep at least two for nested deployment jobs.', 2, 4),
    'coding_workflow_max_workers': ('runtime', 'Coding workers', 'Parallel coding workflows.', 1, 8),
    'coding_workflow_max_queue': ('runtime', 'Coding queue limit', 'Workflows waiting for a worker.', 0, 16),
    'workflow_watchdog_observation_budget_seconds': ('runtime', 'Observation budget (seconds)', 'Time before read-only work is reviewed for a stall.', 15, 3600),
    'workflow_watchdog_wait_cap_seconds': ('runtime', 'Wait interval cap (seconds)', 'Maximum suggested polling interval.', 1, 120),
    'pattern_cache_seconds': ('runtime', 'Pattern cache lifetime (seconds)', 'Reuse cached coding patterns.', 60, 86400),
    'repo_index_cache_seconds': ('runtime', 'Repository index lifetime (seconds)', 'Reuse repository search indexes.', 60, 86400),
    'coding_session_ttl_seconds': ('storage', 'Coding session lifetime (seconds)', 'Retention for warm coding sessions.', 300, 86400),
    'coding_session_max_count': ('storage', 'Coding session limit', 'Maximum retained sessions.', 2, 200),
    'coding_workspace_ttl_seconds': ('storage', 'Workspace lifetime (seconds)', 'Retention for coding workspaces.', 300, 86400),
    'coding_workspace_max_count': ('storage', 'Workspace limit', 'Maximum retained coding workspaces.', 2, 200),
    'job_max_count': ('storage', 'Job history limit', 'Maximum retained job records.', 20, 5000),
    'artifact_ttl_seconds': ('storage', 'Artifact lifetime (seconds)', 'Retention for generated artifacts.', 3600, 7776000),
    'artifact_max_count': ('storage', 'Artifact limit', 'Maximum retained artifacts.', 20, 5000),
    'artifact_max_total_bytes': ('storage', 'Artifact budget (bytes)', 'Maximum retained artifact bytes.', 1000000, 5000000000),
    'execution_log_max_bytes': ('storage', 'Command log limit (bytes)', 'Maximum captured log size per command.', 32000, 20000000),
}


def read_values(root: Path) -> dict:
    try:
        data = json.loads((root / FILE_NAME).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('values'), dict):
        raise ValueError('Unsupported runtime settings format')
    return validate_values(data['values'])


def validate_values(values: dict) -> dict:
    if set(values) - FIELDS.keys():
        raise ValueError('Unsupported runtime setting')
    for key, value in values.items():
        low, high = FIELDS[key][3:]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{FIELDS[key][1]} must be between {low} and {high}')
    return values


def revision(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def apply_saved_settings(instance):
    values = read_values(Path(instance.state_dir).expanduser().resolve())
    # Validate the complete resulting Settings object, without rereading .env.
    candidate = instance.model_dump(by_alias=True)
    for key, value in values.items():
        candidate[type(instance).model_fields[key].alias] = value
    return type(instance).model_validate(candidate)
