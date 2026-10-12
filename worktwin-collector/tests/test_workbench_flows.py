"""Product flows introduced by the article-first workbench."""
import json
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.db import Database
from worktwin.twin_access import effective_notes


def auth(client):
    return {'X-Worktwin-Token': re.search(r'window.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)}


def test_whole_tool_can_wait_and_collects_only_its_native_format(tmp_path, monkeypatch):
    root = tmp_path / 'codex' / 'sessions'
    monkeypatch.setattr('worktwin.config.codex_sessions_path', lambda: root)
    monkeypatch.setattr('worktwin.config.claude_projects_path', lambda: tmp_path / 'claude')
    monkeypatch.setattr('worktwin.config.cursor_exports_path', lambda: tmp_path / 'cursor')
    app = create_app(tmp_path / 'state.sqlite', start_worker=False)
    with TestClient(app) as client:
        headers = auth(client)
        row = client.put('/api/sources/assistants/codex', headers=headers, json={'enabled': True}).json()
        assert row['selected'] and not row['detected']
        assert not row['allow_ai'] and not row['allow_share']
        assert app.state.collector.scan_all()['errors'] == 0
        root.mkdir(parents=True)
        event = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': '审批需要先核对任务范围。'}]}}
        (root / 'session.jsonl').write_text(json.dumps(event) + '\n')
        (root / 'unrelated.jsonl').write_text('{"type":"some_log","text":"unrelated"}\n')
        app.state.collector.scan_all()
        tools = client.get('/api/sources/assistants', headers=headers).json()
        assert tools[0]['detected'] and tools[0]['document_count'] == 1
        with app.state.db.connect() as con:
            assert con.execute('SELECT count(*) FROM ai_jobs').fetchone()[0] == 0
        client.put('/api/sources/assistants/codex', headers=headers, json={'enabled': False})
        (root / 'later.jsonl').write_text(json.dumps(event) + '\n')
        app.state.collector.scan_all()
        assert client.get('/api/sources/assistants', headers=headers).json()[0]['document_count'] == 1


def test_assistant_upgrade_does_not_expand_a_narrow_directory(tmp_path, monkeypatch):
    root = tmp_path / 'sessions'
    root.mkdir()
    monkeypatch.setattr('worktwin.config.codex_sessions_path', lambda: root)
    db = Database(tmp_path / 'state.sqlite')
    with db.connect() as con:
        con.execute("DELETE FROM settings WHERE key='whole_assistant_sources_v1'")
        sid = con.execute("INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES('旧会话','codex','codex',?,1)",
                          (str(root / 'one-day'),)).lastrowid
    upgraded = Database(db.path)
    with upgraded.connect() as con:
        source = dict(con.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())
        assert source['collection_scope'] == 'legacy' and source['enabled'] == 0
        assert source['root'] == str(root / 'one-day') and source['allow_ai'] == 1
        assert con.execute('SELECT count(*) FROM sources').fetchone()[0] == 1


def test_exclusion_upgrade_preserves_effective_set_and_stops_dynamic_growth(tmp_path):
    db = Database(tmp_path / 'state.sqlite')
    with db.connect() as con:
        con.execute("INSERT INTO twins(id,name,knowledge_mode) VALUES(1,'原分身','dynamic')")
        for kid in (1, 2):
            con.execute("""INSERT INTO knowledge(id,kind,title,body,status,scope,topic,scope_detail)
                VALUES(?,'fact','审批流程','审批先核对范围','confirmed','global','审批','通用')""", (kid,))
        con.execute("INSERT INTO twin_grants VALUES(1,'global','*','allow',datetime('now'))")
        con.execute("INSERT INTO twin_grants VALUES(1,'knowledge','2','deny',datetime('now'))")
        before = [r['id'] for r in effective_notes(con, 1)]
        shared = [r['id'] for r in effective_notes(con, 1, sharing=True)]
        con.execute("DELETE FROM settings WHERE key='twin_exclusions_removed_v1'")
    upgraded = Database(db.path)
    with upgraded.connect() as con:
        assert [r['id'] for r in effective_notes(con, 1)] == before == [1]
        assert [r['id'] for r in effective_notes(con, 1, sharing=True)] == shared
        assert dict(con.execute('SELECT * FROM twins').fetchone())['grants_migrated'] == 1
        assert con.execute('SELECT count(*) FROM twin_grants').fetchone()[0] == 0
        con.execute("""INSERT INTO knowledge(kind,title,body,status,scope,topic,scope_detail)
            VALUES('fact','新知识','新知识不能自动加入','confirmed','global','新知识','通用')""")
        assert [r['id'] for r in effective_notes(con, 1)] == [1]


class ChatModel:
    configured = True

    def __init__(self):
        self.requests = []

    def chat(self, messages, max_tokens=1600):
        self.requests.append(messages)
        return '先核对范围，再串联评审流程。[K1]'


def test_followup_uses_conversation_and_restarts_after_knowledge_change(tmp_path):
    model = ChatModel()
    app = create_app(tmp_path / 'state.sqlite', start_worker=False, inference_client=model)
    with TestClient(app) as client:
        headers = auth(client)
        client.post('/api/knowledge', headers=headers, json={
            'title': '评审准备', 'body': '评审先核对范围，再串联流程。', 'status': 'confirmed'})
        twin = client.post('/api/twins', headers=headers, json={'name': '项目助手'}).json()['id']
        client.put(f'/api/twins/{twin}/knowledge', headers=headers, json={'knowledge_ids': [1]})
        first = client.post(f'/api/twins/{twin}/ask', headers=headers, json={'question': '评审要准备什么？'}).json()
        history = [{'role': 'user', 'content': '评审要准备什么？'}, {'role': 'assistant', 'content': first['answer']}]
        payload = {'question': '第二项具体怎么做？', 'messages': history, 'context_revision': first['context_revision']}
        second = client.post(f'/api/twins/{twin}/ask', headers=headers, json=payload)
        assert second.status_code == 200 and second.json()['context_count'] == 1
        assert model.requests[-1][1:3] == history
        assert '评审先核对范围' in model.requests[-1][-1]['content']
        client.put('/api/knowledge/1', headers=headers, json={
            'title': '评审准备', 'body': '评审先准备新的审批材料。', 'status': 'confirmed'})
        count = len(model.requests)
        assert client.post(f'/api/twins/{twin}/ask', headers=headers, json=payload).status_code == 409
        assert len(model.requests) == count
        assert client.post(f'/api/twins/{twin}/ask', headers=headers,
                           json={'question': '评审现在如何准备？'}).status_code == 200


def test_material_is_secondary_without_changing_twin_access(tmp_path):
    app = create_app(tmp_path / 'state.sqlite', start_worker=False)
    with TestClient(app) as client:
        headers = auth(client)
        client.post('/api/knowledge', headers=headers, json={
            'title': '原知识点', 'body': '审批之前核对范围', 'status': 'confirmed'})
        with app.state.db.connect() as con:
            con.execute("UPDATE knowledge SET organization='material' WHERE id=1")
        assert client.get('/api/knowledge?organization=article', headers=headers).json() == []
        assert client.get('/api/knowledge?organization=material', headers=headers).json()[0]['can_use']
        assert client.get('/api/knowledge-counts', headers=headers).json()['materials'] == 1
        client.put('/api/knowledge/1', headers=headers, json={
            'title': '审批工作指南', 'body': '## 审批准备\n先核对范围。\n## 审批过程\n记录结论。', 'status': 'confirmed'})
        assert client.get('/api/knowledge?organization=article', headers=headers).json()[0]['id'] == 1


def test_pausing_collection_keeps_existing_knowledge_but_ai_revocation_does_not(tmp_path):
    folder = tmp_path / 'folder'
    folder.mkdir()
    quote = '审批开始前必须核对项目范围。'
    (folder / 'guide.md').write_text(quote)
    app = create_app(tmp_path / 'state.sqlite', start_worker=False)
    with TestClient(app) as client:
        headers = auth(client)
        sid = client.post('/api/sources', headers=headers, json={
            'name': '资料', 'root': str(folder), 'allow_ai': True, 'allow_share': True}).json()['id']
        app.state.collector.scan_all()
        did = client.get('/api/documents', headers=headers).json()[0]['id']
        kid = client.post('/api/knowledge', headers=headers, json={
            'title': '审批流程', 'body': quote, 'status': 'confirmed', 'document_id': did, 'quote': quote}).json()['id']
        tid = client.post('/api/twins', headers=headers, json={'name': '助手'}).json()['id']
        client.put(f'/api/twins/{tid}/knowledge', headers=headers, json={'knowledge_ids': [kid]})
        client.post(f'/api/sources/{sid}/toggle', headers=headers)
        with app.state.db.connect() as con:
            assert [r['id'] for r in effective_notes(con, tid)] == [kid]
            assert [r['id'] for r in effective_notes(con, tid, sharing=True)] == [kid]
        client.put(f'/api/sources/{sid}/ai', headers=headers, json={'allow_ai': False})
        with app.state.db.connect() as con:
            assert effective_notes(con, tid) == []


def test_answer_can_find_a_step_at_the_end_of_a_long_article():
    from worktwin.answers import answer_from_knowledge
    model = ChatModel()
    body = ('## 背景\n这是一份工作说明。\n\n' * 2200) + '## 最后一步\n撤回审批需要先解除关联订单。'
    answer_from_knowledge(model, '如何撤回审批？', [dict(id=1, title='操作手册', body=body, scope='global')])
    context = model.requests[0][-1]['content']
    assert '撤回审批需要先解除关联订单' in context
    assert len(context) < 48000


def test_latest_question_can_select_another_authorized_project():
    from worktwin.answers import answer_from_knowledge
    model = ChatModel()
    rows = [dict(id=1, title='审批', body='项目甲使用旧步骤', scope='project', project='项目甲', project_key='a'),
            dict(id=2, title='审批', body='项目乙使用新步骤', scope='project', project='项目乙', project_key='b')]
    answer_from_knowledge(model, '项目乙的审批呢？', rows,
                          messages=[{'role': 'user', 'content': '项目甲如何审批？'}])
    assert '项目乙使用新步骤' in model.requests[-1][-1]['content']
    assert '项目甲使用旧步骤' not in model.requests[-1][-1]['content']


def test_twin_can_keep_or_remove_unavailable_existing_selection(tmp_path):
    app = create_app(tmp_path / 'state.sqlite', start_worker=False)
    with TestClient(app) as client:
        headers = auth(client)
        client.post('/api/knowledge', headers=headers, json={
            'title': '审批流程', 'body': '审批先检查范围', 'status': 'confirmed'})
        tid = client.post('/api/twins', headers=headers, json={'name': '助手'}).json()['id']
        client.put(f'/api/twins/{tid}/knowledge', headers=headers, json={'knowledge_ids': [1]})
        client.put('/api/knowledge/1', headers=headers, json={
            'title': '审批流程', 'body': '审批先检查范围', 'status': 'draft'})
        assert client.put(f'/api/twins/{tid}', headers=headers,
                          json={'name': '改名助手', 'knowledge_ids': [1]}).status_code == 200
        assert client.get('/api/twins', headers=headers).json()[0]['knowledge_count'] == 0
        assert client.put(f'/api/twins/{tid}', headers=headers,
                          json={'name': '改名助手', 'knowledge_ids': []}).status_code == 200
        assert client.put(f'/api/twins/{tid}', headers=headers,
                          json={'name': '改名助手', 'knowledge_ids': [1]}).status_code == 400


def test_retry_from_source_only_retries_that_sources_jobs(tmp_path):
    app = create_app(tmp_path / 'state.sqlite', start_worker=False)
    with TestClient(app) as client:
        headers = auth(client)
        source_ids = []
        for name in ('a', 'b'):
            folder = tmp_path / name
            folder.mkdir()
            (folder / 'note.md').write_text('需要保留完整的操作步骤和来源依据。')
            source_ids.append(client.post('/api/sources', headers=headers, json={
                'name': name, 'root': str(folder), 'allow_ai': True}).json()['id'])
        app.state.collector.scan_all()
        with app.state.db.connect() as con:
            con.execute("UPDATE ai_jobs SET state='error',error='model unavailable'")
        sid = source_ids[0]
        rows = client.get('/api/ai/jobs', headers=headers, params={'source_id': sid}).json()
        assert len(rows) == 1 and rows[0]['source_id'] == sid
        assert client.post('/api/ai/jobs/retry', headers=headers, params={'source_id': sid}).json()['queued'] == 1
        rows = client.get('/api/ai/jobs', headers=headers).json()
        assert {r['source_id']: r['state'] for r in rows} == {sid: 'queued', source_ids[1]: 'error'}


def test_workbench_upgrade_keeps_a_pre_14_recovery_point(tmp_path):
    import sqlite3
    db = Database(tmp_path / 'state.sqlite')
    with db.connect() as con:
        con.execute("DELETE FROM settings WHERE key='topic_articles_v1'")
        con.execute("INSERT INTO twins(name) VALUES('保留的分身')")
    Database(db.path)
    backup = db.path.with_name(db.path.name + '.pre-1.4.0.sqlite')
    with sqlite3.connect(backup) as con:
        assert con.execute('SELECT name FROM twins').fetchone()[0] == '保留的分身'
        assert con.execute("SELECT 1 FROM settings WHERE key='topic_articles_v1'").fetchone() is None
    before = backup.stat().st_mtime_ns
    Database(db.path)
    assert backup.stat().st_mtime_ns == before
