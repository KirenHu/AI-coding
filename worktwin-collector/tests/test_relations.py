import io
import zipfile
from fastapi.testclient import TestClient
from tests.test_sharing import auth
from worktwin.api import create_app


def test_backlinks_survive_rename_and_disappear_on_archive(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        a=c.post('/api/knowledge',headers=h,json={'title':'审批设计','body':'审批复用工作流节点','status':'confirmed'}).json()['id']
        b=c.post('/api/knowledge',headers=h,json={'title':'项目交接','body':f'参考 [[K{a}|审批设计]]','status':'confirmed'}).json()['id']
        relations=c.get(f'/api/knowledge/{a}/relations',headers=h).json()
        assert relations['backlinks'][0]['id']==b
        c.put(f'/api/knowledge/{a}',headers=h,json={'title':'审批配置新版','body':'审批复用统一引擎','status':'confirmed'})
        assert c.get(f'/api/knowledge/{b}/relations',headers=h).json()['outgoing'][0]['title']=='审批配置新版'
        entries=c.get('/api/knowledge',headers=h).json()
        assert f'href="#knowledge-{a}"' in next(k for k in entries if k['id']==b)['rendered_body']
        assert c.get(f'/api/knowledge/{a}/history',headers=h).json()[0]['title']=='审批设计'
        raw=c.get('/api/export-wiki',headers=h)
        assert raw.status_code==200
        with zipfile.ZipFile(io.BytesIO(raw.content)) as z:
            assert 'INDEX.md' in z.namelist()
            assert f'[[K{a:06d}|审批设计]]' in z.read(f'personal/K{b:06d}.md').decode()
            assert f'personal/K{a:06d}.md' in z.namelist()
        c.put(f'/api/knowledge/{a}',headers=h,json={'title':'审批配置新版','body':'审批复用统一引擎','status':'archived'})
        assert c.get(f'/api/knowledge/{b}/relations',headers=h).json()['outgoing']==[]
        assert c.get(f'/api/knowledge/{b}/relations',headers=h).json()['unresolved_ids']==[a]


def test_markdown_html_and_unsafe_links_are_not_executed(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        c.post('/api/knowledge',headers=h,json={'title':'标记测试','body':'<script>alert(1)</script>\n\n[bad](javascript:alert(1))','status':'confirmed'})
        html=c.get('/api/knowledge',headers=h).json()[0]['rendered_body']
        assert '<script>' not in html and 'href="javascript:' not in html
