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
        assert len(client.get(f"/api/knowledge/{item['id']}/history",headers=headers).json()) == 1
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
