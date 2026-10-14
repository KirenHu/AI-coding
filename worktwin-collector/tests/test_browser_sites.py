"""Persistent capture intent and isolated workflow references, using temp DBs."""
import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException

from worktwin.browser_capture import BrowserCapture
from worktwin.browser_capture_manual import manual_site
from worktwin.db import Database


def capture_at(path):
    db = Database(path)
    capture = BrowserCapture(db)
    capture.toggle(True)
    token = capture.pair(capture.pairing_code())
    return capture, token


def task(capture, token, *, action='start', nonce='unique_start_nonce_0001', name='work-1'):
    expires = int(time.time())+60
    target = 'https://portal.example.com/work'
    source = 'https://flow.example.com'
    status = '/api/worktwin/tasks/'+name+'/status'
    signature = hmac.new(b'x'*48, '\n'.join([
        action, name, target, nonce, str(expires), source, status]).encode(), hashlib.sha256).hexdigest()
    return capture.command(token, source, dict(action=action, task_id=name,
        target_url=target, nonce=nonce, expires_at=expires, status_path=status, signature=signature))


def test_exact_origin_and_default_off(tmp_path):
    capture, token = capture_at(tmp_path/'sites.db')
    added = capture.add_site('HTTPS://Example.com:443/path?token=secret')
    assert added['origin'] == 'https://example.com'
    assert added['enabled'] is False
    assert capture.add_site('https://example.com')['id'] == added['id']
    assert capture.add_site('https://example.com:8443')['id'] != added['id']
    assert manual_site('http://localhost:8123/test') == 'http://localhost:8123'
    capture.toggle_site(added['id'], True)
    for url in ('https://sub.example.com/', 'http://example.com/', 'https://example.com:8443/'):
        with pytest.raises(HTTPException):
            capture.site_bind(token, added['id'], 1, 'doc-1', url)


def test_old_connected_extension_is_flagged_until_versioned_heartbeat(tmp_path):
    capture,token=capture_at(tmp_path/'version.sqlite')
    capture.heartbeat(token)
    assert capture.settings()['extension_connected']
    assert capture.settings()['extension_update_required']
    capture.heartbeat(token,'0.4.1')
    assert capture.settings()['extension_version']=='0.4.1'
    assert not capture.settings()['extension_update_required']


def test_site_reports_chrome_permission_separately_from_capture_intent(tmp_path):
    capture,token=capture_at(tmp_path/'permissions.sqlite')
    site=capture.add_site('https://portal.example.com')
    capture.toggle_site(site['id'],True)
    capture.heartbeat(token,'0.4.1',['http://127.0.0.1/*'])
    assert capture.sites()[0]['status']=='needs_permission'
    assert capture.sites()[0]['enabled']
    capture.heartbeat(token,'0.4.1',['https://portal.example.com/*'])
    assert capture.sites()[0]['status']=='waiting_page'
    capture.site_bind(token,site['id'],7,'doc',site['origin']+'/work')
    assert capture.sites()[0]['status']=='capturing'


def test_task_history_filters_before_pagination(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN', 'https://flow.example.com')
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET', 'x'*48)
    capture, token = capture_at(tmp_path/'pages.db')
    task_session = task(capture, token)['session_id']
    site = capture.add_site('https://portal.example.com')
    capture.toggle_site(site['id'], True)
    for index in range(25):
        capture.site_bind(token, site['id'], index + 10, 'doc-' + str(index), site['origin'] + '/work')
    assert all(row['mode'] == 'manual' for row in capture.recent(limit=20))
    assert capture.recent(limit=20, mode='task')[0]['id'] == task_session
    assert len(capture.recent(limit=20, mode='manual', offset=20)) == 5


def test_site_intent_survives_navigation_restart_and_global_pause(tmp_path):
    capture, token = capture_at(tmp_path/'restart.db')
    site = capture.add_site('https://portal.example.com')
    capture.toggle_site(site['id'], True)
    bound = capture.site_bind(token, site['id'], 7, 'doc-a', site['origin']+'/work')
    sid = bound['session_id']
    capture.site_event(token, sid, 1, 'click', 7, 'doc-a', site['origin']+'/work', '开始')
    capture.site_close(token, sid, 'navigation_stopped')
    assert capture.sites()[0]['enabled'] is True
    capture = BrowserCapture(capture.db)
    assert capture.site_current(token)['sites'][0]['manual_enabled'] is True
    next_sid = capture.site_bind(token, site['id'], 7, 'doc-b', site['origin']+'/next')['session_id']
    assert next_sid != sid
    capture.toggle_ai(True)
    capture.toggle(False)
    assert capture.heartbeat(token) == {'ok': True, 'enabled': False}
    assert capture.settings()['allow_ai'] is True
    assert capture.sites()[0]['status'] == 'paused'
    with pytest.raises(HTTPException):
        capture.site_event(token, next_sid, 1, 'click', 7, 'doc-b', site['origin']+'/next', '不记录')
    capture.toggle(True)
    resumed = capture.site_bind(token, site['id'], 7, 'doc-b', site['origin']+'/next')['session_id']
    assert resumed != next_sid
    assert capture.events(sid)[0]['label'] == '开始'
    capture.unpair()
    assert capture.sites()[0]['enabled'] is True
    with pytest.raises(HTTPException):
        capture.heartbeat(token)


def test_human_off_blocks_existing_tasks_but_new_signed_start_can_enable(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN', 'https://flow.example.com')
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET', 'x'*48)
    capture, token = capture_at(tmp_path/'tasks.db')
    old = task(capture, token)['session_id']
    site = capture.sites()[0]
    assert site['enabled'] and site['task_requests'] == 1 and not site['manual_enabled']
    capture.toggle_site(site['id'], False)
    capture.poll_task_states(lambda *_: 'running', now=int(time.time())+10)
    assert capture.sites()[0]['enabled'] is False
    with pytest.raises(HTTPException):
        task(capture, token)
    new = task(capture, token, nonce='unique_explicit_start_0002')['session_id']
    assert old != new
    assert capture.sites()[0]['enabled'] is True
    capture.toggle_site(site['id'], True)
    task(capture, token, action='complete', nonce='unique_complete_nonce_0003')
    assert capture.sites()[0]['enabled'] is True
    assert capture.sites()[0]['task_requests'] == 0


def test_shared_raw_event_keeps_other_tabs_out_of_task_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN', 'https://flow.example.com')
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET', 'x'*48)
    capture, token = capture_at(tmp_path/'evidence.db')
    task_sid = task(capture, token)['session_id']
    capture.bind(token, task_sid, 7, 'doc-work', 'https://portal.example.com/work')
    site = capture.sites()[0]
    capture.toggle_site(site['id'], True)
    manual = capture.site_bind(token, site['id'], 7, 'doc-work', site['origin']+'/work')['session_id']
    duplicate = capture.site_bind(token, site['id'], 7, 'doc-work', site['origin']+'/work')
    assert duplicate['session_id'] == manual
    result = capture.site_event(token, manual, 1, 'click', 7, 'doc-work', site['origin']+'/work',
        '保存', [task_sid])
    assert result['task_sequences'] == {task_sid: 1}
    with capture.db.connect() as con:
        assert con.execute('SELECT count(*) FROM browser_capture_events').fetchone()[0] == 1
    assert capture.events(task_sid)[0]['label'] == '保存'
    other = capture.site_bind(token, site['id'], 9, 'doc-personal', site['origin']+'/work')['session_id']
    capture.site_event(token, other, 1, 'click', 9, 'doc-personal', site['origin']+'/work',
        '个人浏览', [task_sid])
    assert [r['label'] for r in capture.events(task_sid)] == ['保存']
    capture.toggle_site(site['id'], False)
    with pytest.raises(HTTPException):
        capture.site_event(token, manual, 2, 'click', 7, 'doc-work', site['origin']+'/work', '关闭后')
    capture.remove_site(site['id'])
    assert capture.events(task_sid)[0]['label'] == '保存'
    assert capture.add_site(site['origin'])['enabled'] is False


def test_paused_workflow_waits_for_fresh_platform_status(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN', 'https://flow.example.com')
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET', 'x'*48)
    capture, token = capture_at(tmp_path/'paused.db')
    sid = task(capture, token)['session_id']
    capture.bind(token, sid, 7, 'doc-work', 'https://portal.example.com/work')
    capture.toggle(False)
    capture.toggle(True)
    assert capture.extension_sessions(token, [sid])['sessions'][sid] == 'paused'
    capture.poll_task_states(lambda *_: 'running', now=int(time.time())+10)
    assert capture.extension_sessions(token, [sid])['sessions'][sid] == 'capturing'
    capture.toggle(False)
    completed = task(capture, token, action='complete', nonce='unique_complete_when_paused')
    assert completed['status'] == 'completed'
    capture.toggle(True)
    assert capture.extension_sessions(token, [sid])['sessions'][sid] == 'completed'


def test_site_routes_use_local_owner_and_paired_extension_tokens(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from worktwin.api import create_app
    with TestClient(create_app(tmp_path/'api.db', start_worker=False)) as client:
        token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
        auth = {'X-Worktwin-Token': token}
        assert client.post('/api/browser-capture/sites', json={'url': 'example.com'}).status_code == 403
        row = client.post('/api/browser-capture/sites', headers=auth,
                          json={'url': 'example.com'}).json()
        assert row['origin'] == 'https://example.com' and not row['enabled']
        client.put(f"/api/browser-capture/sites/{row['id']}", headers=auth, json={'enabled': True})
        assert client.get('/api/browser-capture/sites', headers=auth).json()[0]['status'] == 'paused'
        assert client.post('/capture/sites/current').status_code == 401
        assert client.post('/api/browser-capture/unpair', headers=auth).status_code == 200
