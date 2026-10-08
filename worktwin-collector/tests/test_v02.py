import io
import json
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.claude import parse_claude_session
from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.knowledge import candidates_from_text
from worktwin.search import search
from tests.support import run_ai


def auth(client):
    token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
    return {'X-Worktwin-Token': token}


def test_claude_transcript_streaming_and_private_fields(tmp_path):
    path = tmp_path / 'chat.jsonl'
    rows = [
        {'type':'user','uuid':'u1','sessionId':'c123','cwd':'/company/ProjectOne','message':{'role':'user','content':'我倾向先确认方案再开发。'}},
        {'type':'assistant','uuid':'a1','message':{'role':'assistant','id':'msg1','content':[{'type':'thinking','thinking':'private reasoning'}, {'type':'text','text':'建议采用'}]}},
        {'type':'assistant','uuid':'a2','message':{'role':'assistant','id':'msg1','content':[{'type':'text','text':'建议采用方案 B。'}]}},
        {'type':'assistant','uuid':'a3','message':{'role':'assistant','id':'msg1','content':[{'type':'text','text':'建议采用方案 B。'},{'type':'tool_use','input':{'password':'dont-copy-me'}}]}},
        {'type':'user','uuid':'u2','message':{'role':'user','content':[{'type':'tool_result','content':'secret-tool-response'}]}},
    ]
    path.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in rows))
    title, transcript = parse_claude_session(path)
    assert title.startswith('我倾向')
    assert 'ProjectOne' in transcript
    assert transcript.count('建议采用方案 B。') == 1
    assert 'private reasoning' not in transcript
    assert 'secret-tool-response' not in transcript
    assert 'dont-copy-me' not in transcript


def test_ai_suggestions_do_not_become_user_personal_preferences():
    transcript = '''工作目录：/work/demo

### 用户 · 2026-01-01
请整理一个流程图。

### AI · 2026-01-01
我建议先采用 B 方案，我倾向于扁平化设计。

### 用户 · 2026-01-02
我倾向于先明确业务边界，再决定开发周期。'''
    candidates = candidates_from_text(transcript, transcript=True)
    assert len(candidates) == 1
    assert candidates[0]['kind'] == 'preference'
    assert candidates[0]['body'].startswith('我倾向于先明确')


def test_edit_revalidates_evidence_and_revoke_purges_edited_knowledge(tmp_path):
    root = tmp_path/'work'
    root.mkdir()
    file = root/'decisions.md'
    file.write_text('我们最终决定把审批做成流程节点，避免重复建设独立审批流。',encoding='utf-8')
    app = create_app(tmp_path/'database.sqlite',start_worker=False)
    with TestClient(app) as client:
        h=auth(client)
        sid=client.post('/api/sources',headers=h,json={'name':'Project','root':str(root),'allow_ai':True}).json()['id']
        assert app.state.collector.scan_all()['new']==1
        run_ai(app.state.db)
        first=client.get('/api/knowledge',headers=h).json()[0]
        assert first['evidence'][0]['is_current']==1
        assert client.put(f"/api/knowledge/{first['id']}", headers=h, json={
            'title':'审批设计','body':'审批作为节点','kind':'decision','status':'confirmed'}).status_code==200
        # Content changes, original quote no longer proves this decision.
        file.write_text('我们最终决定将审批切换为独立服务，但需要做好权限隔离。',encoding='utf-8')
        assert app.state.collector.scan_all()['updated']==1
        edited=next(k for k in client.get('/api/knowledge',headers=h).json() if k['id']==first['id'])
        assert edited['needs_review']==1
        assert edited['evidence'][0]['is_current']==0
        # A formerly extracted item never survives source revocation, even if edited.
        assert client.delete(f'/api/sources/{sid}',headers=h).status_code==200
        assert all(k['id']!=first['id'] for k in client.get('/api/knowledge',headers=h).json())
        assert client.get('/api/search',headers=h,params={'q':'独立服务'}).json()['documents']==[]


def test_unchanged_quote_stays_current_on_append(tmp_path):
    root=tmp_path/'notes';root.mkdir()
    path=root/'spec.md'
    quote='我们最终决定采用审批节点作为流程的一个子项，保留原始依据。'
    path.write_text(quote,encoding='utf-8')
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        con.execute('INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,?,?,1)',('notes','folder',str(root)))
    collector=Collector(db)
    collector.scan_all()
    run_ai(db)
    path.write_text(quote+'\n\n新增：审批节点具有操作指引。',encoding='utf-8')
    collector.scan_all()
    with db.connect() as con:
        rows=con.execute('SELECT needs_review,status FROM knowledge').fetchall()
        assert rows and all(row['needs_review']==0 for row in rows)
        assert con.execute('SELECT COUNT(*) FROM knowledge_evidence WHERE is_current=0').fetchone()[0]==0


def test_claude_project_wiki_and_question_retrieval(tmp_path):
    sessions=tmp_path/'claude'/'projects'/'-company-myapp'
    sessions.mkdir(parents=True)
    rows=[
        {'type':'user','uuid':'u1','cwd':'/company/MyApp','message':{'role':'user','content':'我们最终决定将审批能力作为工作流的一个节点，以减少重复配置。'}},
        {'type':'assistant','uuid':'a1','message':{'role':'assistant','id':'a1','content':[{'type':'text','text':'已记录。'}]}},
    ]
    (sessions/'a.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows),encoding='utf-8')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        assert c.get('/api/default-paths',headers=h).status_code==200
        result=c.post('/api/sources',headers=h,json={'name':'Claude','kind':'claude','root':str(sessions.parent),'allow_ai':True})
        assert result.status_code==200
        assert app.state.collector.scan_all()['new']==1
        run_ai(app.state.db)
        projects=c.get('/api/projects',headers=h).json()
        assert projects[0]['name']=='MyApp'
        summary=c.get('/api/projects/summary',headers=h,params={'name':'MyApp'}).json()
        assert len(summary['documents'])==1
        assert len(summary['knowledge'])>=1
        question='为什么审批能力要做成工作流节点？'
        hits=c.get('/api/search',headers=h,params={'q':question}).json()
        assert any('审批' in item['excerpt'] for item in hits['documents'])
        assert not c.get('/api/projects').is_success
        archive=c.get('/api/export-wiki',headers=h)
        assert archive.status_code==200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert any('MyApp' in name for name in z.namelist())
            assert any('作为工作流' in z.read(name).decode() for name in z.namelist() if name.endswith('.md'))


def test_old_database_migration_preserves_content(tmp_path):
    # Existing v0.1 database already has old columns and CHECK(kind IN (...)).
    old = tmp_path/'old.sqlite'
    import sqlite3
    con=sqlite3.connect(old)
    con.executescript("""CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT,kind TEXT CHECK(kind IN ('folder','codex')),root TEXT UNIQUE,enabled INTEGER DEFAULT 1);""")
    con.execute("INSERT INTO sources(name,kind,root) VALUES('old','folder','/tmp/old')")
    con.commit();con.close()
    # Full migration exercised by constructing a database with pre-existing rows.
    # Schema creation handles missing tables and new fields; they are added via ALTER.
    db=Database(old)
    with db.connect() as con:
        assert con.execute('SELECT adapter FROM sources').fetchone()['adapter']=='folder'
