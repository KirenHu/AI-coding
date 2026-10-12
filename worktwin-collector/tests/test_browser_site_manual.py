"""Browser materials are periodically written into separate, readable task articles."""
import json
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from tests.test_browser_capture import sign


def authorized(client):
    token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
    return {'X-Worktwin-Token': token}


def connect(client, dashboard):
    assert client.put('/api/browser-capture', headers=dashboard,
                      json={'enabled': True}).status_code == 200
    code = client.post('/api/browser-capture/pairing', headers=dashboard).json()['code']
    token = client.post('/capture/pair', json={'code': code}).json()['token']
    return {'Authorization': 'Bearer ' + token}


def manually_record(client, dashboard, extension, *, site='https://example.org',
                    label='保存订单', second='修改数量'):
    result = client.put('/api/browser-capture/manual', headers=dashboard,
                        json={'enabled': True, 'url': site + '/orders?api_key=private'})
    assert result.status_code == 200, result.text
    sid = result.json()['manual_session_id']
    assert client.post('/capture/bind', headers=extension, json={
        'session_id': sid, 'tab_id': 12, 'document_id': 'doc-one',
        'current_url': site + '/orders'}).status_code == 200
    for seq, kind, name in ((1, 'click', label), (2, 'change', second),
                            (3, 'feedback', 'feedback_success')):
        answer = client.post('/capture/event', headers=extension, json={
            'session_id': sid, 'tab_id': 12, 'document_id': 'doc-one',
            'current_url': site + '/orders', 'seq': seq, 'kind': kind, 'label': name})
        assert answer.status_code == 200, answer.text
    stop = client.put('/api/browser-capture/manual', headers=dashboard,
                      json={'enabled': False})
    assert stop.status_code == 200, stop.text
    return sid


def site_notes(client, dashboard):
    return [k for k in client.get('/api/knowledge', headers=dashboard,
             params={'status': 'all', 'limit': 1000}).json()
            if k['kind'] == 'process' and k['project_key'].startswith('browser-site:')]


class ArticleModel:
    configured = True

    def __init__(self):
        self.calls = []

    def chat(self, messages, max_tokens=450):
        self.calls.append(messages)
        request = json.loads(messages[-1]['content'])
        if 'events' in request:
            from worktwin.browser_capture_summary import rule_summary
            summary, evidence = rule_summary(request['events'])
            return json.dumps({'summary': summary, 'event_ids': evidence}, ensure_ascii=False)
        groups = {}
        for observation in request['observations']:
            operations = ' '.join(e['operation'] for e in observation['events'])
            topic = '办理退票' if '退票' in operations else '查询订单' if '查询订单' in operations else '维护订单'
            groups.setdefault(topic, []).append(observation)
        result = []
        claimed_legacy = set()
        for topic, observations in groups.items():
            old = next((a for a in request['articles'] if a['topic'] == topic), None)
            if (old is None and len(request['articles']) == 1 and
                    request['articles'][0]['topic'] == '网站操作手册' and
                    request['articles'][0]['id'] not in claimed_legacy):
                old = request['articles'][0]
                claimed_legacy.add(old['id'])
            if old and old['created_by'] == 'human':
                continue
            # Only changed topics require a fresh writing pass.
            if not any(o['new'] for o in observations):
                continue
            latest = max(observations, key=lambda o: o['recorded_at'])
            steps = '\n'.join(f"{i}. {event['operation']}。" for i, event in enumerate(latest['events'], 1))
            feedback = ('本次页面显示成功反馈，后台业务结果尚未核实。'
                        if any(e['kind'] == 'feedback' for e in latest['events'])
                        else '目前尚未观察到页面结果反馈。')
            coverage = '页面仍在记录，以下只是当前已观察到的部分过程。' if latest['partial'] else ''
            body = ('## 操作对象\n\n本文整理网站中与' + topic + '有关的操作观察。\n\n'
                    '## 已观察到的操作过程\n\n' + coverage + steps + '\n\n'
                    '## 页面反馈与观察范围\n\n' + feedback +
                    f'共保留 {len(observations)} 次操作观察；这些记录不代表经过确认的标准业务流程。')
            result.append({'existing_id': old['id'] if old else None, 'topic': topic,
                           'title': topic + '的操作过程',
                           'summary': '整理网站中' + topic + '的操作步骤及页面反馈，供理解实际操作过程。',
                           'body': body, 'change_summary': '补充本次操作观察并更新操作过程。',
                           'observations': [{'session_id': o['session_id'],
                                             'event_ids': [e['seq'] for e in o['events']]}
                                            for o in observations]})
        return json.dumps({'articles': result}, ensure_ascii=False)


def enable_article_agent(client, dashboard):
    assert client.put('/api/browser-capture/ai', headers=dashboard,
                      json={'allow_ai': True}).status_code == 200


def maintain(app):
    return app.state.browser_summary_service.process_due()


def test_collection_only_saves_materials_until_periodic_authorized_writing(tmp_path):
    model = ArticleModel()
    app = create_app(tmp_path / 'materials.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        sid = manually_record(client, dashboard, extension)
        assert site_notes(client, dashboard) == []
        with app.state.db.connect() as con:
            row = con.execute('SELECT * FROM browser_site_observations').fetchone()
            assert row['session_id'] == sid and row['processed_signature'] == ''
            assert '保存订单' in row['events_json'] and 'api_key' not in row['events_json']
        maintain(app)
        assert model.calls == []
        enable_article_agent(client, dashboard)
        maintain(app)
        notes = site_notes(client, dashboard)
        assert len(notes) == 1
        assert notes[0]['article_summary'] and notes[0]['topic'] == '维护订单'
        assert 'api_key' not in json.dumps(model.calls, ensure_ascii=False)
        count = len(model.calls)
        maintain(app)
        assert len(model.calls) == count


def test_same_task_updates_existing_article_and_preserves_sources_and_history(tmp_path):
    model = ArticleModel()
    app = create_app(tmp_path / 'site.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        sid1 = manually_record(client, dashboard, extension)
        # Stopping records materials; it does not synchronously publish an article.
        assert site_notes(client, dashboard) == []
        maintain(app)
        first = site_notes(client, dashboard)[0]
        assert first['version'] == 1 and first['status'] == 'confirmed'
        assert first['organization'] == 'article' and first['scope'] == 'project'
        assert first['topic'] == '维护订单' and first['title'] == '维护订单的操作过程'
        assert '保存订单' in first['body'] and '输入值' in first['body']
        assert sid1 not in first['body']  # Source IDs belong in linked evidence, not prose.
        mapped = client.get(f'/api/browser-capture/sessions/{sid1}/site-manual', headers=dashboard).json()
        assert mapped['knowledge_ids'] == [first['id']]
        detail = client.get(f"/api/knowledge-item/{first['id']}", headers=dashboard).json()
        assert detail['browser_sources'][0]['session_id'] == sid1
        assert [e['seq'] for e in detail['browser_sources'][0]['events']] == [1, 2, 3]
        assert 'api_key' not in json.dumps(detail['browser_sources'], ensure_ascii=False)
        sid2 = manually_record(client, dashboard, extension)
        maintain(app)
        second = site_notes(client, dashboard)[0]
        assert second['id'] == first['id'] and second['version'] == 2
        assert '共保留 2 次操作观察' in second['body']
        history = client.get(f"/api/knowledge/{first['id']}/history", headers=dashboard).json()
        assert len(history) == 1 and history[0]['body'] == first['body']
        with app.state.db.connect() as con:
            sources = con.execute('SELECT session_id,event_ids_json FROM browser_article_observations WHERE knowledge_id=?',
                                  (first['id'],)).fetchall()
            assert {r['session_id'] for r in sources} == {sid1, sid2}
            assert all(json.loads(r['event_ids_json']) == [1, 2, 3] for r in sources)
        maintain(app)
        assert site_notes(client, dashboard)[0]['version'] == 2


def test_different_tasks_on_same_site_get_separate_articles_and_repeat_updates_only_its_topic(tmp_path):
    model = ArticleModel()
    app = create_app(tmp_path / 'topics.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        query = manually_record(client, dashboard, extension, label='查询订单', second='订单编号')
        refund = manually_record(client, dashboard, extension, label='申请退票', second='票号')
        maintain(app)
        notes = site_notes(client, dashboard)
        assert len(notes) == 2
        assert {a['topic'] for a in notes} == {'查询订单', '办理退票'}
        assert len({a['project_key'] for a in notes}) == 1
        query_note = next(a for a in notes if a['topic'] == '查询订单')
        refund_note = next(a for a in notes if a['topic'] == '办理退票')
        with app.state.db.connect() as con:
            assert con.execute('SELECT session_id FROM browser_article_observations WHERE knowledge_id=?',
                               (query_note['id'],)).fetchone()[0] == query
            assert con.execute('SELECT session_id FROM browser_article_observations WHERE knowledge_id=?',
                               (refund_note['id'],)).fetchone()[0] == refund
        manually_record(client, dashboard, extension, label='查询订单', second='订单编号')
        maintain(app)
        current = site_notes(client, dashboard)
        assert len(current) == 2
        assert next(a for a in current if a['id'] == query_note['id'])['version'] == 2
        assert next(a for a in current if a['id'] == refund_note['id'])['version'] == 1


def test_different_sites_keep_separate_source_boundaries(tmp_path):
    app = create_app(tmp_path / 'sites.sqlite', start_worker=False, inference_client=ArticleModel())
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        manually_record(client, dashboard, extension, site='https://example.org')
        manually_record(client, dashboard, extension, site='https://another.example')
        app.state.browser_summary_service.site_manuals.process_due(limit=2)
        notes = site_notes(client, dashboard)
        assert len(notes) == 2 and len({a['project_key'] for a in notes}) == 2


def test_owner_edit_stays_authoritative_after_new_observations(tmp_path):
    app = create_app(tmp_path / 'owner.sqlite', start_worker=False, inference_client=ArticleModel())
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        manually_record(client, dashboard, extension)
        maintain(app)
        item = site_notes(client, dashboard)[0]
        response = client.put(f"/api/knowledge/{item['id']}", headers=dashboard, json={
            'kind': 'process', 'title': '人工修订的订单操作说明',
            'body': '人工修正后的唯一有效结论', 'status': 'confirmed'})
        assert response.status_code == 200, response.text
        manually_record(client, dashboard, extension)
        maintain(app)
        latest = site_notes(client, dashboard)[0]
        assert latest['body'] == '人工修正后的唯一有效结论'
        assert latest['created_by'] == 'human'
        with app.state.db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0] == 2


def test_legacy_whole_site_article_keeps_id_and_grants_when_rewritten(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    app = create_app(path, start_worker=False, inference_client=ArticleModel())
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        sid = manually_record(client, dashboard, extension)
        with app.state.db.connect() as con:
            kid = con.execute("""INSERT INTO knowledge(kind,title,body,status,created_by,source_bound,organization,
                scope,project,project_key,topic,scope_detail,quality,attribution,outcome)
                VALUES('process','网站操作手册','旧版整站操作日志','confirmed','enterprise_ai',0,'article',
                'project','网站操作 · example.org','browser-site:legacy','网站操作手册','本人操作','useful','user','none')""").lastrowid
            twin = con.execute("INSERT INTO twins(name) VALUES('测试分身')").lastrowid
            con.execute('INSERT INTO twin_knowledge VALUES(?,?)', (twin, kid))
            con.execute('DROP TABLE browser_site_manuals')
            con.execute("""CREATE TABLE browser_site_manuals(site TEXT PRIMARY KEY,knowledge_id INTEGER NOT NULL UNIQUE
                REFERENCES knowledge(id) ON DELETE CASCADE,updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
            con.execute('INSERT INTO browser_site_manuals(site,knowledge_id) VALUES(?,?)', ('https://example.org', kid))
    # Real upgrade path initializes the new topic table without replacing knowledge IDs.
    newer = create_app(path, start_worker=False, inference_client=ArticleModel())
    with TestClient(newer) as client:
        dashboard = authorized(client)
        enable_article_agent(client, dashboard)
        maintain(newer)
        notes = site_notes(client, dashboard)
        assert len(notes) == 1 and notes[0]['id'] == kid and notes[0]['version'] == 2
        assert notes[0]['topic'] == '维护订单'
        with newer.state.db.connect() as con:
            assert con.execute('SELECT knowledge_id FROM twin_knowledge WHERE twin_id=?', (twin,)).fetchone()[0] == kid
            assert con.execute('SELECT session_id FROM browser_article_observations WHERE knowledge_id=?', (kid,)).fetchone()[0] == sid


def test_workflow_signed_capture_stays_separate_from_local_browser_articles(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN', 'https://flow.example.com')
    secret = 'a' * 48
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET', secret)
    app = create_app(tmp_path / 'task.sqlite', start_worker=False, inference_client=ArticleModel())
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        website, sender = 'https://example.org/orders', 'https://flow.example.com'
        from time import time
        envelope = sign(secret, 'start', 'task-only', website, sender, 'nonce_task_only_123456', int(time()) + 90)
        response = client.post('/capture/command', headers=extension,
                              json={'sender_origin': sender, 'envelope': envelope})
        assert response.status_code == 200, response.text
        sid = response.json()['session_id']
        client.post('/capture/bind', headers=extension, json={
            'session_id': sid, 'tab_id': 22, 'document_id': 'taskdoc', 'current_url': website})
        client.post('/capture/event', headers=extension, json={
            'session_id': sid, 'tab_id': 22, 'document_id': 'taskdoc',
            'current_url': website, 'kind': 'click', 'seq': 1, 'label': '查询订单'})
        complete = sign(secret, 'complete', 'task-only', website, sender, 'nonce_task_end_12345678', int(time()) + 90)
        assert client.post('/capture/command', headers=extension,
                           json={'sender_origin': sender, 'envelope': complete}).status_code == 200
        assert client.post(f'/api/browser-capture/sessions/{sid}/summarize', headers=dashboard).status_code == 200
        maintain(app)
        assert site_notes(client, dashboard) == []
        with app.state.db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0] == 0


def test_one_recording_can_supply_two_articles_with_separate_event_references(tmp_path):
    class SplitModel(ArticleModel):
        def chat(self, messages, max_tokens=450):
            request = json.loads(messages[-1]['content'])
            if 'events' in request:
                return super().chat(messages, max_tokens=max_tokens)
            self.calls.append(messages)
            sid = request['observations'][0]['session_id']
            articles = []
            for topic, ids in (('查询订单', [1]), ('申请退票', [2, 3])):
                articles.append({'existing_id': None, 'topic': topic, 'title': topic + '的操作观察',
                    'summary': '记录本次网页中与' + topic + '有关的操作及已观察到的反馈。',
                    'body': '## 操作对象\n\n本次观察涉及' + topic + '。\n\n'
                            '## 操作观察\n\n仅记录页面控件操作，输入值未被采集。'
                            '尚无足够依据认定为标准业务流程。',
                    'change_summary': '记录首次观察到的' + topic + '操作。',
                    'observations': [{'session_id': sid, 'event_ids': ids}]})
            return json.dumps({'articles': articles}, ensure_ascii=False)
    app = create_app(tmp_path / 'multi.sqlite', start_worker=False, inference_client=SplitModel())
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        sid = manually_record(client, dashboard, extension, label='查询订单', second='退票申请')
        maintain(app)
        notes = site_notes(client, dashboard)
        assert len(notes) == 2
        mapped = client.get(f'/api/browser-capture/sessions/{sid}/site-manual', headers=dashboard).json()
        assert set(mapped['knowledge_ids']) == {a['id'] for a in notes}
        for article in notes:
            detail = client.get(f"/api/knowledge-item/{article['id']}", headers=dashboard).json()
            sources = detail['browser_sources']
            assert len(sources) == 1 and sources[0]['session_id'] == sid
            ids = [e['seq'] for e in sources[0]['events']]
            assert ids == ([1] if article['topic'] == '查询订单' else [2, 3])


def test_invalid_article_reply_is_not_billed_again_every_worker_tick(tmp_path):
    class InvalidWriter(ArticleModel):
        def chat(self, messages, max_tokens=450):
            request = json.loads(messages[-1]['content'])
            if 'events' in request or request['site'] == 'https://another.example':
                return super().chat(messages, max_tokens=max_tokens)
            self.calls.append(messages)
            return 'not an article response'
    model = InvalidWriter()
    app = create_app(tmp_path / 'invalid.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        manually_record(client, dashboard, extension)
        maintain(app)
        calls = len(model.calls)
        maintain(app)
        assert len(model.calls) == calls and site_notes(client, dashboard) == []
        with app.state.db.connect() as con:
            run = con.execute('SELECT state,error FROM browser_article_runs').fetchone()
            assert run['state'] == 'error' and run['error'] == 'ValueError'
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0] == 1
        app.state.browser_summary_service.site_manuals.process_due(force=True)
        assert len(model.calls) == calls + 1
        # A failed site's receipt does not block other sites in the worker queue.
        manually_record(client, dashboard, extension, site='https://another.example')
        maintain(app)
        assert len(site_notes(client, dashboard)) == 1


def test_persistent_monitoring_supplies_partial_materials_before_page_closes(tmp_path):
    model = ArticleModel()
    app = create_app(tmp_path / 'live.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        enable_article_agent(client, dashboard)
        result = client.put('/api/browser-capture/manual', headers=dashboard,
                            json={'enabled': True, 'url': 'https://example.org/orders'})
        sid = result.json()['manual_session_id']
        client.post('/capture/bind', headers=extension, json={
            'session_id': sid, 'tab_id': 12, 'document_id': 'open-page',
            'current_url': 'https://example.org/orders'})
        for seq, kind, label in ((1, 'click', '查询订单'), (2, 'change', '订单编号'),
                                 (3, 'click', '查询')):
            assert client.post('/capture/event', headers=extension, json={
                'session_id': sid, 'tab_id': 12, 'document_id': 'open-page',
                'current_url': 'https://example.org/orders', 'seq': seq,
                'kind': kind, 'label': label}).status_code == 200
        maintain(app)
        with app.state.db.connect() as con:
            observation = con.execute('SELECT * FROM browser_site_observations').fetchone()
            assert observation['complete'] == 0
            signature = observation['signature']
            assert con.execute('SELECT status FROM browser_capture_sessions WHERE id=?', (sid,)).fetchone()[0] == 'capturing'
        article_calls = [json.loads(c[-1]['content']) for c in model.calls if 'observations' in json.loads(c[-1]['content'])]
        assert article_calls[-1]['observations'][0]['partial'] is True
        assert len(site_notes(client, dashboard)) == 1
        # Closing finalizes coverage even though no extra event was observed.
        assert client.put('/api/browser-capture/manual', headers=dashboard,
                          json={'enabled': False}).status_code == 200
        maintain(app)
        with app.state.db.connect() as con:
            observation = con.execute('SELECT * FROM browser_site_observations').fetchone()
            assert observation['complete'] == 1 and observation['signature'] != signature
        article_calls = [json.loads(c[-1]['content']) for c in model.calls if 'observations' in json.loads(c[-1]['content'])]
        assert article_calls[-1]['observations'][0]['partial'] is False


def test_once_authorized_rebuild_does_not_change_persistent_browser_ai_permission(tmp_path):
    model = ArticleModel()
    app = create_app(tmp_path / 'once.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        dashboard = authorized(client)
        extension = connect(client, dashboard)
        manually_record(client, dashboard, extension)
        assert model.calls == []
        result = app.state.browser_summary_service.site_manuals.process_due(
            authorized_sites=('https://example.org',))
        assert result['processed'] == 1
        assert client.get('/api/browser-capture', headers=dashboard).json()['allow_ai'] is False
        calls = len(model.calls)
        manually_record(client, dashboard, extension)
        maintain(app)
        assert len(model.calls) == calls
        assert site_notes(client, dashboard)[0]['version'] == 1
