from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import get_odoo_clients, settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.client_admin import issue_login_code
from odoo_agent_server.schemas import JobRecord, JobStatus
from odoo_agent_server.services.client_admin_service import ClientAdminService
from odoo_agent_server.services.client_settings_store import FILE_NAME
from odoo_agent_server.services.store_service import JsonStore


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'state_dir', str(tmp_path / 'state'))
    monkeypatch.setattr(settings, 'allowed_roots_raw', str(tmp_path))
    monkeypatch.setattr(settings, 'execution_enabled', False)
    monkeypatch.setattr(settings, 'api_token', 'test-admin-token-not-a-real-secret')
    monkeypatch.setattr(settings, 'public_base_url', 'https://testserver')
    monkeypatch.setattr(settings, 'oauth_issuer', None)
    checkout = tmp_path / 'repo'; checkout.mkdir()
    clients = [dict(name='Alpha', repo='https://gitlab.example/group/project.git', branch='staging',
                    path=str(checkout), environment='staging', token='repository-secret-123',
                    odoo_url='https://alpha.example', odoo_db='alpha', odoo_login='admin',
                    odoo_password='odoo-secret-123', odoo_verify_ssl=True,
                    odoosh_ssh_host='alpha.dev.odoo.com', odoosh_ssh_user='1234', odoosh_ssh_port=22,
                    odoosh_ssh_private_key_b64='private-ssh-key-secret'),
               dict(name='Beta', repo='https://github.com/acme/beta.git', branch='staging',
                    path=str(tmp_path / 'beta'), environment='staging', token='beta-secret')]
    monkeypatch.setenv('ODOO_CLIENTS', json.dumps(clients))
    client = TestClient(app, base_url='https://testserver')
    return client, clients


def login(client):
    result = client.post('/admin/session', json={'token': settings.api_token})
    assert result.status_code == 200
    return {'X-CSRF-Token': result.json()['csrf_token'], 'Origin': 'https://testserver'}


def test_public_page_has_no_client_credentials_and_admin_api_is_private(ui):
    client, clients = ui
    page = client.get('/clients')
    assert page.status_code == 200 and 'Client manager' in page.text
    assert 'no-store' in page.headers['cache-control']
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    for item in clients:
        assert item['token'] not in page.text
    assert client.get('/admin/clients').status_code == 401
    assert client.get('/clients/assets/app.js').status_code == 200
    assert client.get('/clients/assets/unknown.js').status_code == 404
    assert client.post('/admin/session', json={'token': 'wrong'}).status_code == 401


def test_login_cookie_is_secure_http_only_and_csrf_required(ui):
    client, _ = ui
    response = client.post('/admin/session', json={'token': settings.api_token})
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie
    detail = client.get('/admin/clients/Alpha').json()
    payload = {'revision': detail['revision'], 'values': {'branch': 'new-staging'}}
    assert client.patch('/admin/clients/Alpha', json=payload).status_code == 403
    headers = {'X-CSRF-Token': response.json()['csrf_token'], 'Origin': 'https://evil.example'}
    assert client.patch('/admin/clients/Alpha', headers=headers, json=payload).status_code == 403
    assert client.post('/admin/session', headers={'Origin': 'https://evil.example'}, json={'token': settings.api_token}).status_code == 403


def test_client_save_applies_immediately_preserves_secrets_and_survives_new_process(ui):
    client, before = ui
    headers = login(client)
    detail = client.get('/admin/clients/Alpha').json()
    for secret in ('repository-secret-123', 'odoo-secret-123', 'private-ssh-key-secret'):
        assert secret not in json.dumps(detail)
    result = client.patch('/admin/clients/Alpha', headers=headers, json={
        'revision': detail['revision'], 'values': {'branch': 'new-staging', 'odoo_password': '', 'token': ''}})
    assert result.status_code == 200 and result.json()['saved']
    effective = get_odoo_clients()
    assert effective[0]['branch'] == 'new-staging'
    assert effective[0]['token'] == before[0]['token'] and effective[0]['odoo_password'] == before[0]['odoo_password']
    assert effective[0]['odoosh_ssh_private_key_b64'] == before[0]['odoosh_ssh_private_key_b64']
    assert effective[1] == before[1]
    environment = {**os.environ, 'ODOO_AGENT_STATE_DIR': settings.state_dir}
    output = subprocess.check_output([sys.executable, '-c', 'from odoo_agent_server.config import get_odoo_clients; print(get_odoo_clients()[0]["branch"])'], env=environment, text=True)
    assert output.strip() == 'new-staging'
    mode = (settings.state_path / FILE_NAME).stat().st_mode & 0o777
    assert mode == 0o640
    assert 'odoo-secret-123' not in json.dumps([event.model_dump() for event in JsonStore().list_audit()], default=str)


def test_stale_form_cannot_overwrite_newer_saved_settings(ui):
    client, _ = ui
    headers = login(client)
    original = client.get('/admin/clients/Alpha').json()
    first = client.patch('/admin/clients/Alpha', headers=headers, json={'revision': original['revision'], 'values': {'branch': 'first'}})
    assert first.status_code == 200
    second = client.patch('/admin/clients/Alpha', headers=headers, json={'revision': original['revision'], 'values': {'branch': 'second'}})
    assert second.status_code == 409
    assert get_odoo_clients()[0]['branch'] == 'first'


@pytest.mark.parametrize('values', [
    {'name': 'renamed'}, {'odoo_password': ['secret']}, {'branch': '-bad'},
    {'repo': 'https://token:secret@example.com/repo.git'}, {'odoo_url': 'file:///etc/passwd'},
    {'path': '/outside/allowed/root'}, {'odoosh_ssh_port': 0},
    {'odoosh_ssh_host': 'host; reboot'}, {'environment': 'anything'},
])
def test_invalid_configuration_does_not_change_client(ui, values):
    client, before = ui
    headers = login(client)
    revision = client.get('/admin/clients/Alpha').json()['revision']
    result = client.patch('/admin/clients/Alpha', headers=headers, json={'revision': revision, 'values': values})
    assert result.status_code in {403, 422}
    assert get_odoo_clients() == before


def test_active_client_job_blocks_config_changes(ui):
    client, _ = ui
    headers = login(client)
    job = JsonStore().create_job(JobRecord(job_type='deployment', status=JobStatus.running, request={'client_name': 'Alpha'}))
    detail = client.get('/admin/clients/Alpha').json()
    response = client.patch('/admin/clients/Alpha', headers=headers, json={'revision': detail['revision'], 'values': {'branch': 'new'}})
    assert response.status_code == 409
    assert get_odoo_clients()[0]['branch'] == 'staging'
    JsonStore().finish_job(job, JobStatus.passed, {})


def test_one_time_link_expires_and_cannot_be_replayed(ui):
    client, _ = ui
    code = issue_login_code()
    assert client.post('/admin/session', json={'login_code': code}).status_code == 200
    assert client.post('/admin/session', json={'login_code': code}).status_code == 401
    code = issue_login_code()
    import hashlib
    path = settings.state_path / 'client-admin-login' / (hashlib.sha256(code.encode()).hexdigest() + '.json')
    data = json.loads(path.read_text()); data['expires'] = time.time() - 1; path.write_text(json.dumps(data))
    assert client.post('/admin/session', json={'login_code': code}).status_code == 401


def test_logout_removes_browser_session(ui):
    client, _ = ui
    headers = login(client)
    assert client.get('/admin/clients').status_code == 200
    assert client.delete('/admin/session', headers=headers).status_code == 200
    assert client.get('/admin/clients').status_code == 401


def test_corrupt_saved_settings_fail_closed(ui):
    settings.state_path.joinpath(FILE_NAME).write_text('corrupt')
    with pytest.raises(HTTPException, match='503'):
        get_odoo_clients()


def test_validation_uses_existing_async_job_and_never_returns_its_secrets(ui, monkeypatch):
    client, _ = ui
    headers = login(client)
    from odoo_agent_server.routers import tools
    called = []
    def start(req):
        called.append(req)
        job = JsonStore().create_job(JobRecord(job_type='client_validation', status=JobStatus.running, request={'client_name': req.client_name}))
        return {'job_id': job.id, 'status': 'validation_started'}
    monkeypatch.setattr(tools, '_start_client_validation', start)
    response = client.post('/admin/clients/Alpha/validate', headers=headers, json={})
    assert response.status_code == 200 and called[0].force_refresh and called[0].run_validation
    job = JsonStore().get_job(response.json()['job_id'])
    JsonStore().finish_job(job, JobStatus.passed, {'validated': True, 'coding_ready': True, 'deploy_ready': True,
                                               'summary': 'odoo-secret-123', 'checks': {'checkout': 'ok'}})
    result = client.get('/admin/validation/' + job.id)
    assert result.json()['terminal'] and result.json()['validated']
    assert 'odoo-secret-123' not in result.text


def test_embedded_repo_credentials_stay_hidden_and_untouched(ui, monkeypatch):
    client, before = ui
    before[0]['repo'] = 'https://oauth2:embedded-secret@gitlab.example/group/project.git'
    monkeypatch.setenv('ODOO_CLIENTS', json.dumps(before))
    headers = login(client)
    detail = client.get('/admin/clients/Alpha').json()
    assert detail['values']['repo'] == 'https://gitlab.example/group/project.git'
    assert 'embedded-secret' not in json.dumps(detail)
    response = client.patch('/admin/clients/Alpha', headers=headers, json={'revision': detail['revision'], 'values': {'repo': detail['values']['repo'], 'branch': 'updated'}})
    assert response.status_code == 200 and get_odoo_clients()[0]['repo'] == before[0]['repo']


def test_invalid_unicode_token_is_rejected_without_server_error(ui):
    client, _ = ui
    assert client.post('/admin/session', json={'token': 'invalid-🔒'}).status_code == 401


def test_editing_https_only_client_does_not_require_ssh_configuration(ui):
    client, _ = ui
    headers = login(client)
    detail = client.get('/admin/clients/Beta').json()
    response = client.patch('/admin/clients/Beta', headers=headers, json={
        'revision': detail['revision'], 'values': {'branch': 'new-staging'}})
    assert response.status_code == 200 and get_odoo_clients()[1]['branch'] == 'new-staging'
