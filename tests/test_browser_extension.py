"""Real relay/WebSocket protocol tests; no client databases or browser cookies."""
import base64
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.services.browser_extension_service import BrowserExtensionService, _CONNECTIONS, origin

EXT = 'a' * 32


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'state_dir', str(tmp_path / 'state'))
    monkeypatch.setattr(settings, 'functional_operations_enabled', True)
    monkeypatch.setenv('ODOO_CLIENTS', json.dumps([
        {'name': 'Alpha', 'repo': 'https://example.test/alpha.git', 'path': str(tmp_path), 'branch': 'staging', 'odoo_url': 'https://alpha.example.test', 'environment': 'staging'},
        {'name': 'Beta', 'repo': 'https://example.test/beta.git', 'path': str(tmp_path), 'branch': 'staging', 'odoo_url': 'https://beta.example.test', 'environment': 'staging'},
    ]))
    _CONNECTIONS.clear()
    service = BrowserExtensionService()
    pair = service.pair(service.pair_code('Alpha')['code'], EXT, 'Chrome test')
    with TestClient(app) as client:
        yield service, pair, client
    _CONNECTIONS.clear()


def call(client, action, **kwargs):
    response = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': action, **kwargs}})
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok', body
    return body['result']


def authenticate(ws, pair):
    ws.send_json({'type': 'authenticate', 'device_id': pair['device_id'], 'token': pair['device_token']})
    assert ws.receive_json()['type'] == 'ready'


def finish(ws, command, result):
    ws.send_json({'type': 'result', 'command_id': command['command_id'], 'ok': True, 'result': result})
    ws.send_json({'type': 'ping'})
    assert ws.receive_json()['type'] == 'pong'


def test_pair_is_one_use_and_credentials_are_not_returned_by_status(relay):
    svc, pair, _ = relay
    code = svc.pair_code('Alpha')['code']
    svc.pair(code, EXT, 'Second browser')
    with pytest.raises(HTTPException):
        svc.pair(code, EXT, 'Replay')
    body = json.dumps(svc.status())
    assert pair['device_token'] not in body and 'token_hash' not in body
    assert origin('https://alpha.example.test:443/x') == 'https://alpha.example.test'


def test_admin_pair_requires_session_and_extension_exchange_requires_origin(relay):
    svc, _, client = relay
    assert client.post('/admin/browser-extension/pair', json={'client_name': 'Alpha'}).status_code == 401
    code = svc.pair_code('Alpha')['code']
    assert client.post('/browser-extension/pair', json={'code': code, 'extension_id': EXT}).status_code == 403
    result = client.post('/browser-extension/pair', headers={'Origin': 'chrome-extension://' + EXT}, json={'code': code, 'extension_id': EXT})
    assert result.status_code == 200 and result.json()['origins'] == ['https://alpha.example.test']


def test_snapshot_action_busy_and_same_id_never_replays(relay):
    svc, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        assert call(client, 'status')['devices'][0]['connected']
        waiting = call(client, 'snapshot', arguments={'tab_id': 12})
        command = ws.receive_json()
        assert command['action'] == 'snapshot'
        assert call(client, 'tabs')['status'] == 'browser_busy'
        finish(ws, command, {'snapshot_id': 'snapshot-1', 'elements': [{'ref': 'e1', 'name': 'Save'}]})
        done = call(client, 'poll', command_id=waiting['command_id'])
        assert done['status'] == 'succeeded' and not done['success_allowed']
        args = {'tab_id': 12, 'snapshot_id': 'snapshot-1', 'ref': 'e1'}
        clicked = call(client, 'click', request_id='click-save-001', arguments=args)
        command = ws.receive_json()
        finish(ws, command, {'performed': True})
        replay = call(client, 'click', request_id='click-save-001', arguments=args)
        assert replay['command_id'] == clicked['command_id'] and replay['status'] == 'succeeded'
        ws.send_json({'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'  # No second command was delivered.
        assert call(client, 'click', request_id='click-save-002', arguments=args)['status'] == 'snapshot_required'
        assert svc.poll('Alpha', clicked['command_id'])['evidence_scope'] == 'browser_ui_only'


def test_cross_client_origin_and_mismatched_request_are_rejected(relay):
    svc, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        response = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Beta', 'device_id': pair['device_id'], 'action': 'tabs'}})
        assert response.json()['status'] == 'failed'
        with pytest.raises(HTTPException):
            svc.poll('Beta', 'a' * 32)
        blocked = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': 'open', 'request_id': 'forbidden-origin', 'arguments': {'url': 'https://elsewhere.test'}}})
        assert blocked.json()['status'] == 'failed'
        first = call(client, 'tabs', request_id='same-read-id-001')
        command = ws.receive_json(); finish(ws, command, {'tabs': []})
        blocked = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': 'snapshot', 'request_id': 'same-read-id-001', 'arguments': {'tab_id': 12}}})
        assert blocked.json()['status'] == 'failed'
        assert first['command_id']


def test_disconnect_keeps_uncertain_mutation_and_expiry_prevents_replay(relay):
    svc, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        result = call(client, 'open', request_id='open-disconnect-001', arguments={'url': 'https://alpha.example.test/web'})
        ws.receive_json()  # Connection lost after delivery, without an acknowledgement.
    row = svc.poll('Alpha', result['command_id'])
    assert row['status'] == 'reconciliation_required'
    path = svc.path('commands', result['command_id']); data = svc.read(path)
    data['created_at'] = time.time() - 172801; data['arguments']['private'] = 'old payload'; path.write_text(json.dumps(data))
    svc.cleanup(time.time() - 172800)
    assert not path.exists()
    assert svc.path('expired', result['command_id']).read_text() == '{"expired": true}'
    with pytest.raises(HTTPException, match='410'):
        svc.poll('Alpha', result['command_id'])


def test_native_mcp_screenshot_image_is_not_dumped_into_text(relay):
    _, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        result = call(client, 'screenshot', arguments={'tab_id': 12})
        command = ws.receive_json()
        data = base64.b64encode(b'\xff\xd8test-jpeg').decode()
        finish(ws, command, {'image': {'mimeType': 'image/jpeg', 'data': data}})
        response = client.post('/mcp', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': 'poll', 'command_id': result['command_id']}}}).json()['result']
        assert any(block['type'] == 'image' and block['data'] == data for block in response['content'])
        assert data not in response['content'][0]['text'] and data not in json.dumps(response['structuredContent'])


def test_extension_download_contains_only_fixed_packaged_assets(relay):
    import io
    import zipfile
    _, _, client = relay
    response = client.get('/browser-extension/extension.zip')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        assert len(package.namelist()) == 6
        manifest = json.loads(package.read('manifest.json'))
        assert manifest['manifest_version'] == 3 and 'cookies' not in manifest['permissions']
        assert not manifest.get('host_permissions')


def test_javascript_driver_dom_guards_and_no_replay():
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('node'):
        pytest.skip('Node.js is not available')
    result = subprocess.run(['node', str(Path(__file__).with_name('browser_extension.test.mjs'))], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_wrong_extension_origin_cannot_authenticate(relay):
    from starlette.websockets import WebSocketDisconnect
    _, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'https://unrelated.test'}) as ws:
        ws.send_json({'type': 'authenticate', 'device_id': pair['device_id'], 'token': pair['device_token']})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_functional_flag_blocks_actions_and_failed_assertion_is_not_success(relay, monkeypatch):
    _, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        result = call(client, 'snapshot', arguments={'tab_id': 12}); command = ws.receive_json()
        finish(ws, command, {'snapshot_id': 'fresh', 'elements': []})
        monkeypatch.setattr(settings, 'functional_operations_enabled', False)
        response = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': 'click', 'request_id': 'disabled-click-001', 'arguments': {'tab_id': 12, 'snapshot_id': 'fresh', 'ref': 'e1'}}})
        assert response.json()['status'] == 'failed'
        assertion = call(client, 'assert', arguments={'tab_id': 12, 'snapshot_id': 'fresh', 'ref': 'e1', 'expected_text': 'Expected'})
        command = ws.receive_json(); finish(ws, command, {'assertion_passed': False, 'observed_text': 'Different'})
        assert call(client, 'poll', command_id=assertion['command_id'])['status'] == 'failed'
        assert call(client, 'poll', command_id=result['command_id'])['status'] == 'succeeded'


def test_page_assertion_supports_visible_confirmation_text(relay):
    _, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        waiting = call(client, 'assert', arguments={'tab_id': 12, 'snapshot_id': 'fresh-page', 'expected_text': 'Order confirmed'})
        command = ws.receive_json()
        finish(ws, command, {'assertion_passed': True, 'observed_text': 'Order confirmed'})
        result = call(client, 'poll', command_id=waiting['command_id'])
        assert result['status'] == 'succeeded' and result['result']['assertion_passed']
        assert result['evidence_scope'] == 'browser_ui_only' and not result['success_allowed']


def test_expiry_marker_race_cannot_recreate_a_mutating_request(relay, monkeypatch):
    import hashlib
    from odoo_agent_server.services.approval_service import ApprovalService
    from odoo_agent_server.services.client_settings_store import atomic_json
    svc, pair, client = relay
    with client.websocket_connect('/browser-extension/connect', headers={'Origin': 'chrome-extension://' + EXT}) as ws:
        authenticate(ws, pair)
        call(client, 'snapshot', arguments={'tab_id': 12})
        command = ws.receive_json(); finish(ws, command, {'snapshot_id': 'fresh', 'elements': []})
        request_id = 'expiry-race-click'
        command_id = hashlib.sha256(f"{pair['device_id']}:{request_id}".encode()).hexdigest()[:32]
        monkeypatch.setattr(ApprovalService, 'ensure_allowed', lambda *a, **kw: atomic_json(svc.path('expired', command_id), {'expired': True}))
        result = client.post('/tools/call', headers={'Authorization': 'Bearer ' + settings.api_token}, json={'name': 'browser_extension', 'arguments': {'client_name': 'Alpha', 'action': 'click', 'request_id': request_id, 'arguments': {'tab_id': 12, 'snapshot_id': 'fresh', 'ref': 'e1'}}}).json()
        assert result['status'] == 'failed' and not svc.path('commands', command_id).exists()
        ws.send_json({'type': 'ping'}); assert ws.receive_json()['type'] == 'pong'
