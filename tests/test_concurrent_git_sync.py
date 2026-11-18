"""Concurrent writers must retain both intents and invalidate old verification."""
import subprocess

import pytest
from fastapi import HTTPException

from odoo_agent_server.execution_schemas import WorkspaceFiles, WorkspacePublish, WorkspaceSync
from odoo_agent_server.services.three_way_merge import merge_text
from tests.test_execution_worker import execution, verify  # noqa: F401
from tests.test_workspace_service import make, SALE_PY


def advance(source, bare, content):
    other = source.parent / 'concurrent-writer'
    if not other.exists():
        subprocess.run(['git', 'clone', str(bare), str(other)], check=True, capture_output=True)
    (other / 'hello.py').write_text(content)
    for args in (['add', '.'], ['-c', 'user.name=Other', '-c', 'user.email=other@example.com', 'commit', '-m', 'Manual update'], ['push']):
        subprocess.run(['git', *args], cwd=other, check=True, capture_output=True)


def write(service, wid, content):
    old = service.files(WorkspaceFiles(workspace_id=wid, operation='read', path='hello.py'))
    service.files(WorkspaceFiles(workspace_id=wid, operation='write', path='hello.py', content=content, expected_sha256=old['sha256']))


def test_full_file_write_merges_other_region_without_overwriting():
    gh, _, svc, _ = make()
    wid = svc.open('Acme', 'my_mod')['ws_id']
    svc.edit(wid, [{'path': 'models/sale.py', 'content': SALE_PY.replace('note_x = fields.Char()', 'note_y = fields.Char()')}])
    gh.push_external('staging', {'my_mod/models/sale.py': SALE_PY.replace('from odoo import models, fields', 'from odoo import api, models, fields')})
    assert svc.commit(wid, 'merge both')['status'] == 'pushed'
    final = gh.head_files('staging')['my_mod/models/sale.py']
    assert 'api, models' in final and 'note_y' in final
    cached = svc.read(wid, ['models/sale.py'])['files'][0]['text']
    assert 'api, models' in cached
    svc.edit(wid, [{'path': 'models/sale.py', 'search': 'note_y = fields.Char()', 'replace': 'note_z = fields.Char()'}])
    assert svc.commit(wid, 'follow-up change')['status'] == 'pushed'
    assert 'api, models' in gh.head_files('staging')['my_mod/models/sale.py']


def test_overlapping_full_file_write_and_modify_delete_block_push():
    for delete in (False, True):
        gh, _, svc, _ = make()
        wid = svc.open('Acme', 'my_mod')['ws_id']
        edit = {'path': 'models/sale.py', 'op': 'delete'} if delete else {'path': 'models/sale.py', 'content': SALE_PY.replace('note_x = fields.Char()', 'note_y = fields.Char()')}
        svc.edit(wid, [edit], autofix=False)
        manual = SALE_PY.replace('note_x = fields.Char()', 'note_x = fields.Text()')
        gh.push_external('staging', {'my_mod/models/sale.py': manual})
        result = svc.commit(wid, 'must not overwrite', allow_lint_errors=True)
        assert result['status'] == 'conflict'
        assert gh.head_files('staging')['my_mod/models/sale.py'] == manual


def test_saved_full_file_work_is_merged_on_resume():
    gh, _, svc, registry = make()
    wid = svc.open('Acme', 'my_mod')['ws_id']
    svc.edit(wid, [{'path': 'models/sale.py', 'content': SALE_PY.replace('note_x = fields.Char()', 'note_y = fields.Char()')}])
    registry.clear()
    gh.push_external('staging', {'my_mod/models/sale.py': SALE_PY.replace('from odoo import models, fields', 'from odoo import api, models, fields')})
    resumed = svc.open('Acme', 'my_mod')
    assert not resumed['resumed']['conflicts']
    assert svc.commit(wid, 'restored safe merge')['status'] == 'pushed'
    assert 'api, models' in gh.head_files('staging')['my_mod/models/sale.py']


def test_execution_sync_preserves_untracked_and_requires_fresh_tests(execution, monkeypatch):  # noqa: F811
    svc, wid, source, bare = execution
    svc.files(WorkspaceFiles(workspace_id=wid, operation='write', path='new.py', content='VALUE = 2\n'))
    verify(svc, wid, monkeypatch)
    advance(source, bare, "print('manual')\n")
    merged = svc.sync(WorkspaceSync(workspace_id=wid, request_id='sync-clean-001'))
    assert merged['status'] == 'synced'
    assert (svc.root(wid) / 'hello.py').read_text() == "print('manual')\n"
    assert (svc.root(wid) / 'new.py').read_text() == 'VALUE = 2\n'
    assert (source / 'hello.py').read_text() == "print('hello')\n"
    assert not svc.verification(wid)['ready']
    with pytest.raises(HTTPException, match='409'):
        svc.publish(WorkspacePublish(workspace_id=wid, message='not yet', request_id='sync-before-tests'))
    verify(svc, wid, monkeypatch)
    pushed = svc.publish(WorkspacePublish(workspace_id=wid, message='both changes', request_id='sync-after-tests'))
    assert pushed['pushed']
    assert svc.sync(WorkspaceSync(workspace_id=wid, request_id='sync-noop-001'))['status'] == 'up_to_date'


def test_execution_conflict_preserves_work_and_accepts_explicit_resolution(execution, monkeypatch):  # noqa: F811
    svc, wid, source, bare = execution
    ours = "print('agent')\n"
    write(svc, wid, ours)
    advance(source, bare, "print('manual')\n")
    conflict = svc.sync(WorkspaceSync(workspace_id=wid, request_id='sync-conflict-001'))
    assert conflict['status'] == 'merge_conflict' and conflict['conflicts'][0]['path'] == 'hello.py'
    assert (svc.root(wid) / 'hello.py').read_text() == ours
    with pytest.raises(HTTPException, match='409'):
        svc.sync(WorkspaceSync(workspace_id=wid, request_id='sync-stale-001', expected_remote_sha='0' * 40, resolutions={'hello.py': 'bad'}))
    resolved = "print('agent')\nprint('manual')\n"
    result = svc.sync(WorkspaceSync(workspace_id=wid, request_id='sync-resolved-001', expected_remote_sha=conflict['remote_sha'], resolutions={'hello.py': resolved}))
    assert result['status'] == 'synced'
    assert (svc.root(wid) / 'hello.py').read_text() == resolved
    assert not svc.verification(wid)['ready']
    verify(svc, wid, monkeypatch)
    assert svc.publish(WorkspacePublish(workspace_id=wid, message='resolve both', request_id='sync-resolved-push'))['pushed']


def test_merge_text_binary_and_add_add_are_conservative():
    assert merge_text(None, 'new agent', 'new manual')[1]
    assert merge_text('a\0b', 'c\0b', 'a\0d')[1]
    assert merge_text('same', 'same', 'manual') == ('manual', False)


def test_legacy_push_rejection_does_not_rebase_or_push_untested_code(execution, monkeypatch):  # noqa: F811
    from odoo_agent_server.schemas import CommandResult
    from odoo_agent_server.services.git_service import GitService
    _, _, source, _ = execution
    svc = GitService()
    ok = CommandResult(command=['git'], cwd=str(source), exit_code=0)
    monkeypatch.setattr(svc, 'validate_target', lambda *a: ok)
    calls = []
    def reject(argv, *args):
        calls.append(argv)
        return CommandResult(command=argv, cwd=str(source), exit_code=1, stderr='rejected (fetch first)')
    monkeypatch.setattr(svc, '_run', reject)
    monkeypatch.setattr(svc, 'pull_rebase', lambda *a: pytest.fail('Must not rebase and push without fresh tests'))
    result = svc.push(str(source), branch='staging')
    assert result.exit_code != 0 and 'SYNC_REQUIRED' in result.stderr
    assert len(calls) == 1


def test_legacy_commit_propagates_sync_failure_even_without_default_repo(execution, monkeypatch):  # noqa: F811
    from odoo_agent_server.config import settings
    from odoo_agent_server.schemas import CommandResult
    from odoo_agent_server.services.git_service import GitService
    _, _, source, _ = execution
    monkeypatch.setattr(settings, 'target_git_repo', None)
    svc = GitService()
    failure = CommandResult(command=['git'], cwd=str(source), exit_code=1, stderr='merge conflict')
    monkeypatch.setattr(svc, 'pull_rebase', lambda *a: failure)
    assert svc.commit(str(source), 'must stop', []).exit_code == 1


def test_background_conflict_provides_actionable_resolution_request(execution):  # noqa: F811
    import time
    svc, wid, source, bare = execution
    write(svc, wid, "print('agent')\n")
    advance(source, bare, "print('manual')\n")
    queued = svc.sync_background(WorkspaceSync(workspace_id=wid, request_id='sync-background-001'))
    for _ in range(100):
        result = svc.operation_status(queued['operation_id'])
        if result['status'] not in {'queued', 'running'}:
            break
        time.sleep(.05)
    assert result['status'] == 'merge_conflict'
    assert not result['input_required'] and not result['success_allowed']
    assert result['argument_request']['tool'] == 'execution_workspace_sync'
    assert result['argument_request']['required_generated_arguments'] == ['resolutions']
    assert result['argument_request']['arguments']['expected_remote_sha'] == result['result']['remote_sha']


def test_repeated_remote_changes_are_bounded(execution):  # noqa: F811
    svc, wid, source, bare = execution
    for index in range(3):
        advance(source, bare, f"print('manual {index}')\n")
        request = WorkspaceSync(workspace_id=wid, request_id=f'sync-bounded-{index}')
        if index < 2:
            assert svc.sync(request)['status'] == 'synced'
        else:
            with pytest.raises(HTTPException, match='409'):
                svc.sync(request)
    assert (svc.root(wid) / 'hello.py').read_text() == "print('manual 1')\n"
