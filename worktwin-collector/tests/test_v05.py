"""Regression of model-assisted knowledge updates, citations and permissions."""
import json
import re

from fastapi.testclient import TestClient

from tests.support import FakeModel
from worktwin.api import create_app
from worktwin.reconcile import existing_for_project


def token(client):
    match = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text)
    return {'X-Worktwin-Token': match.group(1)}


def verify_collected_project(client, headers):
    """New scope contract: owner explicitly links each whole file to a project."""
    projects = client.get('/api/projects/verified', headers=headers).json()
    key = projects[0]['project_key'] if projects else None
    docs = client.get('/api/documents', headers=headers).json()
    for doc in docs:
        if doc['project_verified']:
            continue
        body = {'project': '项目'}
        if key:
            body['existing_project_key'] = key
        response = client.put(f"/api/documents/{doc['id']}/scope",
                              headers=headers, json=body)
        assert response.status_code == 200, response.text
        key = response.json()['project_key']


class RevisingModel(FakeModel):
    def chat(self, messages, max_tokens=2400):
        if messages[0]['role']=='system' and 'new_item' in messages[0]['content']:
            self.requests.append(messages)
            incoming=json.loads(messages[-1]['content'])
            existing=incoming['existing']
            if not existing:
                return '{"items":[]}'
            return json.dumps({'items':[{'index':0,'action':'replace','target_id':existing[0]['id'],
                'title':'最新版：项目路由规则','body':'新的项目路由方案由业务工作台统一执行，旧独立路由已停用。',
                'reason':'后续文档明确决定替换旧路由'}]},ensure_ascii=False)
        return super().chat(messages,max_tokens)


def setup(tmp_path):
    path=tmp_path/'workspace';path.mkdir()
    a=path/'01-old.md'
    a.write_text('最终决定沿用旧版路由，所有审批先经过中间调度服务。',encoding='utf-8')
    model=RevisingModel()
    app=create_app(tmp_path/'memory.sqlite',start_worker=False,inference_client=model)
    return app, path, a, model


def test_revision_requires_review_and_supersedes_old_citations(tmp_path):
    app, folder, original, model=setup(tmp_path)
    with TestClient(app) as client:
        auth=token(client)
        sid=client.post('/api/sources',headers=auth,json={'name':'项目','root':str(folder),'allow_ai':True}).json()['id']
        app.state.collector.scan_all()
        verify_collected_project(client, auth)
        assert app.state.knowledge_worker.process_next()['state']=='done'
        old=client.get('/api/knowledge',headers=auth).json()[0]
        tid=client.post('/api/twins',headers=auth,json={'name':'业务交接'}).json()['id']
        assert client.put(f'/api/knowledge/{old["id"]}',headers=auth,json={**{key:old[key] for key in ('kind','title','body')},'status':'confirmed'}).status_code==200
        assert client.put(f'/api/twins/{tid}/knowledge',headers=auth,json={'knowledge_ids':[old['id']]}).status_code==200
        new=folder/'02-new.md'
        new.write_text('最终决定不再使用旧版路由，而由业务工作台统一执行审批路由。',encoding='utf-8')
        app.state.collector.scan_all()
        verify_collected_project(client, auth)
        out=app.state.knowledge_worker.process_next()
        assert out['state']=='done',out
        assert out['proposals']==1,out
        proposals=client.get('/api/knowledge/proposals',headers=auth).json()
        assert len(proposals)==1 and proposals[0]['target_id']==old['id']
        pid=proposals[0]['id']
        assert client.get('/api/twins',headers=auth).json()[0]['knowledge_count']==0
        assert client.put(f'/api/twins/{tid}/knowledge',headers=auth,json={'knowledge_ids':[old['id']]}).status_code==400
        assert client.post(f'/api/knowledge/proposals/{pid}/accept',headers=auth).status_code==200
        item=next(k for k in client.get('/api/knowledge',headers=auth).json() if k['id']==old['id'])
        assert item['version']==3 and item['status']=='confirmed' and item['needs_review']==0
        assert '新的项目路由' in item['body']
        assert len(client.get(f'/api/knowledge/{old["id"]}/history',headers=auth).json())==2
        assert client.get('/api/twins',headers=auth).json()[0]['knowledge_count']==1
        model.requests.clear()
        answer=client.post(f'/api/twins/{tid}/ask',headers=auth,json={'question':'现在怎么执行路由？'})
        assert answer.status_code==200,answer.text
        sent=json.dumps(model.requests,ensure_ascii=False)
        assert '新的项目路由' in sent and '所有审批先经过中间调度' not in sent
        # Revoking the only source invalidates all derived knowledge, including revisions.
        assert client.delete(f'/api/sources/{sid}',headers=auth).status_code==200
        assert client.get('/api/knowledge',headers=auth).json()==[]
        assert client.get('/api/twins',headers=auth).json()[0]['knowledge_count']==0


def test_revisions_expire_if_source_or_target_changed(tmp_path):
    app, folder, oldfile, _=setup(tmp_path)
    with TestClient(app) as client:
        auth=token(client)
        client.post('/api/sources',headers=auth,json={'name':'项目','root':str(folder),'allow_ai':True})
        app.state.collector.scan_all();verify_collected_project(client, auth)
        assert app.state.knowledge_worker.process_next()['state']=='done'
        fresh=folder/'02-new.md';fresh.write_text('最终决定不再使用旧版路由，全部走新的统一引擎。',encoding='utf-8')
        app.state.collector.scan_all();verify_collected_project(client, auth)
        assert app.state.knowledge_worker.process_next()['proposals']==1
        p=client.get('/api/knowledge/proposals',headers=auth).json()[0]
        item=next(k for k in client.get('/api/knowledge',headers=auth).json() if k['id']==p['target_id'])
        modified=client.put(f'/api/knowledge/{item["id"]}',headers=auth,json={
            'title':'人工确认的另一版','body':'人工确认的另外一个版本，不应该被旧提案覆盖。',
            'kind':item['kind'],'status':'confirmed'})
        assert modified.status_code==200
        assert client.post(f'/api/knowledge/proposals/{p["id"]}/accept',headers=auth).status_code==409
        assert '人工确认的另外一个版本' in next(k for k in client.get('/api/knowledge',headers=auth).json() if k['id']==item['id'])['body']
        # A stale proposal cannot overwrite a human edit, but must remain dismissible.
        assert client.post(f'/api/knowledge/proposals/{p["id"]}/dismiss',headers=auth).status_code==200
        assert client.get('/api/knowledge/proposals',headers=auth).json()==[]
        fresh.write_text('最终决定重做新一代统一引擎，不再采用之前的路由模式。',encoding='utf-8')
        app.state.collector.scan_all()
        assert client.get('/api/knowledge/proposals',headers=auth).json()==[]


def test_dismiss_restores_knowledge_and_rejects_unauthorized_reconciliation(tmp_path):
    app, folder, _, model=setup(tmp_path)
    with TestClient(app) as client:
        auth=token(client)
        client.post('/api/sources',headers=auth,json={'name':'项目','root':str(folder),'allow_ai':True})
        app.state.collector.scan_all();verify_collected_project(client, auth)
        app.state.knowledge_worker.process_next()
        second=folder/'02-new.md';second.write_text('最终决定采用新版路由，准备替换旧版调度。',encoding='utf-8')
        app.state.collector.scan_all();verify_collected_project(client, auth)
        app.state.knowledge_worker.process_next()
        proposal=client.get('/api/knowledge/proposals',headers=auth).json()[0]
        assert client.post(f'/api/knowledge/proposals/{proposal["id"]}/dismiss',headers=auth).status_code==200
        assert client.get('/api/knowledge/proposals',headers=auth).json()==[]
        assert client.get('/api/knowledge',headers=auth).json()[0]['needs_review']==0
        # Knowledge from a separate source with AI-processing revoked cannot be sent to the model.
        with app.state.db.connect() as con:
            old_doc_id=con.execute("SELECT id FROM documents WHERE relative_path='01-old.md'").fetchone()[0]
            source_id=con.execute('SELECT source_id FROM documents WHERE id=?',(old_doc_id,)).fetchone()[0]
            con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(source_id,))
            assert existing_for_project(con,old_doc_id)==[]


def test_lost_one_of_multiple_sources_sets_sticky_review_hold(tmp_path):
    app, folder, oldfile, _=setup(tmp_path)
    with TestClient(app) as client:
        auth=token(client)
        sid=client.post('/api/sources',headers=auth,json={'name':'项目','root':str(folder),'allow_ai':False}).json()['id']
        app.state.collector.scan_all()
        docs=client.get('/api/documents',headers=auth).json()
        docid=docs[0]['id']
        quote=oldfile.read_text(encoding='utf-8')
        kid=client.post('/api/knowledge',headers=auth,json={'title':'审批路由归档','body':quote,'kind':'decision','status':'confirmed','document_id':docid,'quote':quote}).json()['id']
        folder2=tmp_path/'another';folder2.mkdir();f2=folder2/'again.md';f2.write_text(quote,encoding='utf-8')
        sid2=client.post('/api/sources',headers=auth,json={'name':'另一个来源','root':str(folder2)}).json()['id']
        app.state.collector.scan_all()
        with app.state.db.connect() as con:
            second_id=con.execute('SELECT id FROM documents WHERE source_id=?',(sid2,)).fetchone()[0]
            con.execute('INSERT INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)',(kid,second_id,quote))
            # A merged article may have a revision containing facts from the
            # source being revoked, even if other live evidence still exists.
            con.execute('INSERT INTO knowledge_history(knowledge_id,version,kind,title,body,status) VALUES(?,?,?,?,?,?)',
                        (kid,0,'decision','旧版路由','仅来自即将撤销来源的私密规则','confirmed'))
        assert client.get(f'/api/knowledge/{kid}/history',headers=auth).json()
        assert client.delete(f'/api/sources/{sid}',headers=auth).status_code==200
        assert client.get(f'/api/knowledge/{kid}/history',headers=auth).json()==[]
        entry=next(k for k in client.get('/api/knowledge',headers=auth).json() if k['id']==kid)
        assert entry['needs_review']==1 and entry['review_hold']==1
        twin=client.post('/api/twins',headers=auth,json={'name':'测试'}).json()['id']
        assert client.put(f'/api/twins/{twin}/knowledge',headers=auth,json={'knowledge_ids':[kid]}).status_code==400
        assert client.put(f'/api/knowledge/{kid}',headers=auth,json={'title':entry['title'],'body':entry['body'],'kind':'decision','status':'confirmed'}).status_code==200
        assert next(k for k in client.get('/api/knowledge',headers=auth).json() if k['id']==kid)['needs_review']==0


def test_model_errors_backoff_and_do_not_leak_provider_message(tmp_path):
    app, folder, _, _=setup(tmp_path)
    with TestClient(app) as client:
        auth=token(client)
        client.post('/api/sources',headers=auth,json={'name':'项目','root':str(folder),'allow_ai':True})
        app.state.collector.scan_all()
        class FailingModel:
            configured=True
            def chat(self,messages,max_tokens=2400):
                raise RuntimeError('https://secret.example?token=DO_NOT_STORE_SENSITIVE')
        app.state.knowledge_worker.client=FailingModel()
        for attempt in range(1,5):
            r=app.state.knowledge_worker.process_next()
            assert r['state']==('retrying' if attempt<4 else 'error')
            jobs=client.get('/api/ai/jobs',headers=auth).json()
            assert jobs[0]['attempts']==attempt
            assert 'DO_NOT_STORE_SENSITIVE' not in str(jobs)
            if attempt<4:
                assert jobs[0]['next_run_at']
                assert app.state.knowledge_worker.process_next()['state']=='idle'
                with app.state.db.connect() as con:
                    con.execute('UPDATE ai_jobs SET next_run_at=datetime(\'now\',\'-1 second\')')
        assert client.post('/api/ai/jobs/retry',headers=auth).json()['queued']==1
        jobs=client.get('/api/ai/jobs',headers=auth).json()
        assert jobs[0]['attempts']==0 and jobs[0]['next_run_at'] is None


def test_old_schema_migrates_without_losing_knowledge(tmp_path):
    from worktwin.db import Database
    import sqlite3
    dbpath=tmp_path/'old.sqlite'
    db=Database(dbpath)
    with db.connect() as con:
        con.execute('INSERT INTO knowledge(kind,title,body,status,created_by) VALUES(\'fact\',\'历史知识\',\'历史内容\',\'confirmed\',\'human\')')
        # Emulate previous versions where new fields have not been created.
        con.execute('ALTER TABLE knowledge DROP COLUMN review_hold')
        con.execute('ALTER TABLE knowledge_evidence DROP COLUMN superseded')
        con.execute('ALTER TABLE ai_jobs DROP COLUMN next_run_at')
        con.execute('DROP TABLE knowledge_proposals')
    upgraded=Database(dbpath)
    with upgraded.connect() as con:
        assert con.execute('SELECT title FROM knowledge').fetchone()[0]=='历史知识'
        assert con.execute('SELECT review_hold FROM knowledge').fetchone()[0]==0
        assert con.execute("SELECT name FROM sqlite_master WHERE name='knowledge_proposals'").fetchone()


def test_same_quote_across_projects_is_not_silently_shared(tmp_path):
    from worktwin.db import Database
    from worktwin.collector import Collector
    from worktwin.knowledge import store_candidates
    from worktwin.parsers import split_chunks
    db=Database(tmp_path/'db.sqlite')
    roots=[tmp_path/'one',tmp_path/'two']
    quote='已决定本项目采用统一通知服务，统一进行用户消息的记录和重试。'
    for root in roots:
        root.mkdir();(root/'note.md').write_text(quote,encoding='utf-8')
    with db.connect() as con:
        con.executemany('INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,?,?,1)',[(r.name,'folder',str(r)) for r in roots])
    assert Collector(db).scan_all()['new']==2
    with db.connect() as con:
        docs=con.execute('SELECT id,project,content FROM documents ORDER BY id').fetchall()
        for d in docs:
            store_candidates(con,d['id'],split_chunks(d['content']),[{'kind':'decision','title':'通知规则','body':quote,'quote':quote}])
        rows=con.execute('SELECT count(*) FROM knowledge').fetchone()[0]
        assert rows==2
