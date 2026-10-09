"""Feature + permission regression tests for the knowledge-first 0.4 client."""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.gateway import create_gateway
from worktwin.inference import GatewayClient, extract_knowledge, text_batches
from worktwin.jobs import KnowledgeWorker
from tests.support import FakeModel, run_ai


def auth(client):
    token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
    return {'X-Worktwin-Token':token}


def test_consent_toggle_enqueues_only_authorized_content(tmp_path):
    root=tmp_path/'work';root.mkdir()
    (root/'decision.md').write_text('我们最终决定保留来源证据而不提供黑盒记忆。',encoding='utf-8')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        source=c.post('/api/sources',headers=h,json={'name':'文件','root':str(root)}).json()['id']
        app.state.collector.scan_all()
        assert c.get('/api/knowledge',headers=h).json()==[]
        assert c.get('/api/ai/jobs',headers=h).json()==[]
        assert app.state.knowledge_worker.process_next()['state']=='not_configured'
        enabled=c.put(f'/api/sources/{source}/ai',headers=h,json={'allow_ai':True})
        assert enabled.status_code==200,enabled.text
        assert c.get('/api/ai/jobs',headers=h).json()[0]['state']=='queued'
        fake=run_ai(app.state.db)
        assert fake.requests
        assert c.get('/api/knowledge',headers=h).json()[0]['evidence'][0]['is_current']==1
        assert c.put(f'/api/sources/{source}/ai',headers=h,json={'allow_ai':False}).status_code==200
        assert not c.get('/api/ai/jobs',headers=h).json()
        (root/'decision.md').write_text('现在我们决定换成另一种实现方式。',encoding='utf-8')
        app.state.collector.scan_all()
        assert not c.get('/api/ai/jobs',headers=h).json()


def test_twin_knowledge_acl_and_revocation(tmp_path):
    root=tmp_path/'work';root.mkdir()
    first='最终决定审批保留在工作流节点中，以便继续复用统一的路由引擎。'
    second='最终决定秘密预算编号为 472995，只有特殊项目同事能够查看。'
    (root/'first.md').write_text(first,encoding='utf-8')
    (root/'second.md').write_text(second,encoding='utf-8')
    live_model=FakeModel()
    app=create_app(tmp_path/'db.sqlite',start_worker=False,inference_client=live_model)
    with TestClient(app) as c:
        h=auth(c)
        sid=c.post('/api/sources',headers=h,json={'name':'work','root':str(root),'allow_ai':True}).json()['id']
        app.state.collector.scan_all()
        assert app.state.knowledge_worker.process_next()['state']=='done'
        assert app.state.knowledge_worker.process_next()['state']=='done'
        entries=c.get('/api/knowledge',headers=h).json()
        assert len(entries)==2
        approved=next(k for k in entries if '工作流节点' in k['body'])
        denied=next(k for k in entries if '472995' in k['body'])
        tid=c.post('/api/twins',headers=h,json={'name':'产品交接','description':'给项目接任者'}).json()['id']
        assert c.put(f'/api/knowledge/{approved["id"]}',headers=h,json={**{key:approved[key] for key in ('kind','title','body')},'status':'confirmed'}).status_code==200
        select=c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[approved['id']]})
        assert select.status_code==200,select.text
        assert c.get(f'/api/twins/{tid}',headers=h).json()['knowledge_ids']==[approved['id']]
        # A real API request to the twin sends only explicitly selected knowledge.
        live_model.requests.clear()
        answer=c.post(f'/api/twins/{tid}/ask',headers=h,json={'question':'为什么审批做在工作流节点？'})
        assert answer.status_code==200,answer.text
        assert answer.json()['citations'][0]['knowledge_id']==approved['id']
        sent=json.dumps(live_model.requests,ensure_ascii=False)
        assert '工作流节点' in sent
        assert '472995' not in sent
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==1
        assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[999999]}).status_code==400
        assert c.get(f'/api/twins/{tid}',headers=h).json()['knowledge_ids']==[approved['id']]
        # A source revocation cascades to all assignments, not just to source docs.
        assert c.delete(f'/api/sources/{sid}',headers=h).status_code==200
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==0
        assert c.get(f'/api/twins/{tid}',headers=h).json()['knowledge_ids']==[]


def test_assigning_stale_knowledge_denied(tmp_path):
    folder=tmp_path/'folder';folder.mkdir()
    note=folder/'note.md'
    note.write_text('我们最终决定保留完整的客户审批记录和操作证据。',encoding='utf-8')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        c.post('/api/sources',headers=h,json={'name':'work','root':str(folder),'allow_ai':True})
        app.state.collector.scan_all();run_ai(app.state.db)
        entry=c.get('/api/knowledge',headers=h).json()[0]
        tid=c.post('/api/twins',headers=h,json={'name':'交接'}).json()['id']
        c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[entry['id']]})
        note.write_text('我们最终决定换成完全不同的审批权限设计。',encoding='utf-8')
        app.state.collector.scan_all()
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==0
        assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[entry['id']]}).status_code==400


def test_worker_discards_inflight_response_after_permission_change(tmp_path):
    folder=tmp_path/'folder';folder.mkdir()
    (folder/'a.md').write_text('我们最终决定采用已验证的知识采集技术方案。',encoding='utf-8')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        sid=c.post('/api/sources',headers=h,json={'name':'work','root':str(folder),'allow_ai':True}).json()['id']
        app.state.collector.scan_all()
        class CancellingModel(FakeModel):
            def chat(self,messages,max_tokens=2400):
                c.put(f'/api/sources/{sid}/ai',headers=h,json={'allow_ai':False})
                return super().chat(messages,max_tokens)
        outcome=KnowledgeWorker(app.state.db,client=CancellingModel()).process_next()
        assert outcome['state']=='superseded'
        assert c.get('/api/knowledge',headers=h).json()==[]


def test_enterprise_gateway_forces_corporate_model_and_auth(monkeypatch):
    monkeypatch.setenv('WORKTWIN_BYOK_BASE_URL','https://example-model-provider.invalid/v1')
    monkeypatch.setenv('WORKTWIN_BYOK_API_KEY','enterprise-only-secret')
    monkeypatch.setenv('WORKTWIN_BYOK_MODEL','corporate-model')
    monkeypatch.setenv('WORKTWIN_ENTERPRISE_TOKENS','employee-1,employee-2')
    sent=[]
    class ProviderResponse:
        def __enter__(self):return self
        def __exit__(self,*args):return None
        def read(self):return json.dumps({'choices':[{'message':{'content':'企业回答'}}]}).encode()
    def fake_open(request,timeout=120):
        sent.append((request.full_url,request.headers, json.loads(request.data)))
        return ProviderResponse()
    # gateway calls json.load(response); provide .read() interface
    monkeypatch.setattr('worktwin.gateway.urlopen',fake_open)
    app=create_gateway()
    with TestClient(app) as c:
        body={'messages':[{'role':'user','content':'hello'}],'model':'attacker-model'}
        assert c.post('/v1/chat/completions',json=body).status_code==401
        success=c.post('/v1/chat/completions',json=body,headers={'Authorization':'Bearer employee-1'})
        assert success.status_code==200,success.text
        assert sent[0][2]['model']=='corporate-model'
        assert sent[0][1]['Authorization']=='Bearer enterprise-only-secret'
        assert 'enterprise-only-secret' not in success.text


def test_batching_extraction_is_verbatim_and_no_ollama():
    assert len(text_batches('甲'*14000))==3
    fake=FakeModel()
    result=extract_knowledge('我们最终决定先完善真实数据采集，再进行个人知识的持续治理。',transcript=False,client=fake)
    assert len(result)==1 and result[0]['kind']=='decision'
    assert result[0]['quote'] in '我们最终决定先完善真实数据采集，再进行个人知识的持续治理。'
    assert not GatewayClient(url='').configured
