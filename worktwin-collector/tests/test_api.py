import re
from pathlib import Path

from fastapi.testclient import TestClient

from worktwin.api import create_app
from tests.support import run_ai


def test_local_web_workflow(tmp_path: Path):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "方案.md").write_text("# 模块设计\n\n我们最终采用统一的流程节点来管理审批和任务操作，保留人工复核环节。",encoding="utf-8")
    app = create_app(tmp_path/"state.sqlite",start_worker=False)
    with TestClient(app) as client:
        homepage = client.get("/")
        assert homepage.status_code == 200
        token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', homepage.text).group(1)
        assert client.post("/api/sources",json={"name":"Work","kind":"folder","root":str(folder),"allow_ai":True}).status_code == 403
        headers={"X-Worktwin-Token":token}
        response=client.post("/api/sources",json={"name":"Work","kind":"folder","root":str(folder),"allow_ai":True},headers=headers)
        assert response.status_code == 200
        app.state.collector.scan_all()
        run_ai(app.state.db)
        documents = client.get("/api/documents",headers=headers).json()
        assert len(documents) == 1
        hits=client.get("/api/search",params={"q":"审批"},headers=headers).json()
        assert hits["documents"]
        assert client.get("/api/knowledge",headers=headers).json()
        item=client.get("/api/knowledge",headers=headers).json()[0]
        edit=client.put(f"/api/knowledge/{item['id']}",headers=headers,json={"title":"决策记录","body":"保留人工复核","kind":"decision","status":"confirmed"})
        assert edit.status_code == 200
        assert len(client.get(f"/api/knowledge/{item['id']}/history",headers=headers).json()) == 2  # AI activation plus owner edit
        assert client.get("/api/stats",headers=headers).json()["confirmed"] == 1
        assert client.get("/api/export",headers=headers).json()["entries"][0]["title"] == "决策记录"
        assert "决策记录" in client.get("/api/export-markdown",headers=headers).text
        delete=client.delete(f"/api/sources/{response.json()['id']}",headers=headers)
        assert delete.status_code == 200
        assert client.get("/api/documents",headers=headers).json() == []


def test_personal_data_is_not_sent_without_ai_permission(tmp_path):
    folder=tmp_path/'private';folder.mkdir()
    (folder/'note.md').write_text('我们最终决定保留完整的来源审计信息。',encoding='utf-8')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h={'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get('/').text).group(1)}
        c.post('/api/sources',headers=h,json={'name':'私有','root':str(folder),'allow_ai':False})
        app.state.collector.scan_all()
        with app.state.db.connect() as db:
            assert db.execute('SELECT count(*) FROM ai_jobs').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM knowledge').fetchone()[0]==0


def test_history_restore_creates_new_version_and_preserves_scope(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get('/').text).group(1)
        h={'X-Worktwin-Token':token}
        created=c.post('/api/knowledge',headers=h,json={
            'title':'初始结论','body':'第一版结论','kind':'decision','status':'confirmed'
        })
        assert created.status_code==200,created.text
        kid=created.json()['id']
        change=c.put(f'/api/knowledge/{kid}',headers=h,json={
            'title':'当前结论','body':'第二版结论','kind':'fact','status':'confirmed'
        })
        assert change.status_code==200
        before=c.get(f'/api/knowledge-item/{kid}',headers=h).json()
        assert c.post(f'/api/knowledge/{kid}/versions/1/restore').status_code==403
        restored=c.post(f'/api/knowledge/{kid}/versions/1/restore',headers=h)
        assert restored.status_code==200,restored.text
        assert restored.json()=={'restored':True,'from_version':1,'version':3}
        after=c.get(f'/api/knowledge-item/{kid}',headers=h).json()
        assert after['title']=='初始结论' and after['body']=='第一版结论'
        assert after['kind']=='decision' and after['version']==3
        assert after['created_by']=='human'
        assert after['project_key']==before['project_key'] and after['scope']==before['scope']
        history=c.get(f'/api/knowledge/{kid}/history',headers=h).json()
        assert [v['version'] for v in history]==[2,1]
        assert history[0]['body']=='第二版结论'
        assert c.post(f'/api/knowledge/{kid}/versions/999/restore',headers=h).status_code==404
        # Another rollback also creates a version, preserving every predecessor.
        assert c.post(f'/api/knowledge/{kid}/versions/2/restore',headers=h).json()['version']==4
        assert [v['version'] for v in c.get(f'/api/knowledge/{kid}/history',headers=h).json()]==[3,2,1]


def test_restoring_version_clears_stale_ai_proposal_instead_of_requiring_review(tmp_path):
    app=create_app(tmp_path/'rollback-proposal.sqlite',start_worker=False)
    with TestClient(app) as c:
        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get('/').text).group(1)
        h={'X-Worktwin-Token':token}
        kid=c.post('/api/knowledge',headers=h,json={
            'title':'旧结论','body':'旧版本的明确结论','kind':'decision',
            'status':'confirmed'}).json()['id']
        c.put(f'/api/knowledge/{kid}',headers=h,json={
            'title':'新结论','body':'新版本的明确结论','kind':'decision',
            'status':'confirmed'})
        with app.state.db.connect() as con:
            sid=con.execute("""INSERT INTO sources(name,kind,root,allow_ai)
                VALUES('审核来源','folder','/tmp/rollback-proposal',1)""").lastrowid
            doc=con.execute("""INSERT INTO documents(source_id,path,relative_path,title,
                file_type,project,content,sha256,size_bytes,mtime_ns,project_key,scope)
                VALUES(?,'/tmp/rollback-proposal/note.md','note.md','note.md','.md',
                       '审核来源','旧版本的明确结论','source-sha',20,1,'session:review','session')""",
                (sid,)).lastrowid
            con.execute("""INSERT INTO knowledge_proposals
                (document_id,content_sha,target_id,target_version,action,kind,title,
                 body,quote,reason,fingerprint)
                VALUES(?,'source-sha',?,2,'replace','decision','新提案',
                       'AI 提议再次修改','旧版本的明确结论','测试提案','test-history-pending')""",
                (doc,kid))
        assert c.post(f'/api/knowledge/{kid}/versions/1/restore',headers=h).status_code==200
        item=c.get(f'/api/knowledge-item/{kid}',headers=h).json()
        assert item['body']=='旧版本的明确结论'
        assert item['needs_review']==0
        with app.state.db.connect() as con:
            assert con.execute("""SELECT status FROM knowledge_proposals
                WHERE fingerprint='test-history-pending'""").fetchone()[0]=='dismissed'
