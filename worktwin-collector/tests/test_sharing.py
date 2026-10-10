"""Published knowledge survives owner shutdown; permissions never widen."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from fastapi.testclient import TestClient
from tests.support import FakeModel
from worktwin.api import create_app
from worktwin.server import create_server


def auth(c):
    return {'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get('/').text).group(1)}


class InProcessPublisher:
    configured=True
    url='https://enterprise.example'
    token='owner-credential-token'
    def __init__(self,c):self.c=c
    def request(self,method,path,body=None):
        r=self.c.request(method,path,headers={'Authorization':'Bearer '+self.token},json=body)
        r.raise_for_status()
        return r.json()


def test_full_publish_share_update_revoke_and_restart(tmp_path):
    model=FakeModel()
    tokens={'alice':'owner-credential-token','bob':'other-owner-token'}
    server=create_server(tmp_path/'server.sqlite',inference_client=model,publisher_tokens=tokens)
    with TestClient(server) as remote:
        app=create_app(tmp_path/'local.sqlite',start_worker=False,inference_client=model,publishing_client=InProcessPublisher(remote))
        with TestClient(app) as local:
            h=auth(local)
            kid=local.post('/api/knowledge',headers=h,json={'title':'审批节点','body':'审批复用工作流节点与路由引擎','status':'confirmed'}).json()['id']
            secret=local.post('/api/knowledge',headers=h,json={'title':'私有预算','body':'DO-NOT-PUBLISH-998','status':'confirmed'}).json()['id']
            tid=local.post('/api/twins',headers=h,json={'name':'交接分身'}).json()['id']
            local.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[kid]})
            assert local.post(f'/api/twins/{tid}/publish',headers=h).status_code==200
            share=local.post(f'/api/twins/{tid}/sharing',headers=h,json={'recipient':'项目接任者','days':1}).json()
            access={'Authorization':'Bearer '+share['token']}
            assert remote.get('/v1/shared',headers=access).json()['knowledge']==[{'id':kid,'title':'审批节点'}]
            model.requests.clear()
            assert remote.post('/v1/shared/ask',headers=access,json={'question':'审批怎么做？'}).status_code==200
            assert 'DO-NOT-PUBLISH-998' not in json.dumps(model.requests)
            pubid=app.state.publisher.public_id(tid)
            assert remote.get('/v1/twins/'+pubid+'/grants',headers={'Authorization':'Bearer other-owner-token'}).status_code==404
            assert remote.get('/v1/shared').status_code==403
            assert remote.get('/v1/shared',headers={'Authorization':'Bearer owner-credential-token'}).status_code==403
            with server.state.store.connect() as con:
                assert con.execute('SELECT count(*) FROM assets').fetchone()[0]==1
                assert share['token'] not in con.execute('SELECT token_hash FROM grants').fetchone()[0]
            local.put(f'/api/knowledge/{kid}',headers=h,json={'title':'审批新版','body':'审批已经改用新版统一引擎','status':'confirmed'})
            assert remote.get('/v1/shared',headers=access).json()['knowledge'][0]['title']=='审批新版'
        # A separate service instance reads persistent data after local shutdown.
        restarted=create_server(tmp_path/'server.sqlite',inference_client=model,publisher_tokens=tokens)
        with TestClient(restarted) as c:
            assert c.post('/v1/shared/ask',headers=access,json={'question':'现在审批怎么做？'}).status_code==200
            with restarted.state.store.connect() as con:
                con.execute('UPDATE grants SET expires_at=?',(int(time.time())-1,))
            assert c.get('/v1/shared',headers=access).status_code==403
        # Re-enable expiry, then revoke this one invitation without deleting knowledge.
        with server.state.store.connect() as con:con.execute('UPDATE grants SET expires_at=?',(int(time.time())+3600,))
        assert remote.delete(f'/v1/twins/{pubid}/grants/{share["id"]}',headers={'Authorization':'Bearer owner-credential-token'}).status_code==200
        assert remote.get('/v1/shared',headers=access).status_code==403


def test_source_share_consent_and_confirmed_only(tmp_path):
    model=FakeModel()
    server=create_server(tmp_path/'server.sqlite',inference_client=model,publisher_tokens={'alice':'owner-credential-token'})
    with TestClient(server) as remote:
        app=create_app(tmp_path/'local.sqlite',start_worker=False,inference_client=model,publishing_client=InProcessPublisher(remote))
        root=tmp_path/'work';root.mkdir();quote='我们最终决定用一个工作流节点实现审批，避免维护两套系统。'
        (root/'decision.md').write_text(quote)
        with TestClient(app) as c:
            h=auth(c);sid=c.post('/api/sources',headers=h,json={'name':'项目','root':str(root),'allow_ai':True}).json()['id']
            app.state.collector.scan_all();app.state.knowledge_worker.process_next()
            k=c.get('/api/knowledge',headers=h).json()[0]
            tid=c.post('/api/twins',headers=h,json={'name':'助手'}).json()['id']
            assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[k['id']]}).status_code==200  # Auto-active privately; sharing remains separately prohibited
            c.post(f'/api/twins/{tid}/publish',headers=h)
            assert app.state.publisher.snapshot()['assets']==[]
            c.put(f'/api/knowledge/{k["id"]}',headers=h,json={**{key:k[key] for key in ('kind','title','body')},'status':'confirmed'})
            assert app.state.publisher.snapshot()['assets']==[]
            assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[k['id']]}).status_code==200
            c.put(f'/api/sources/{sid}/share',headers=h,json={'allow_share':True})
            assert len(app.state.publisher.snapshot()['assets'])==1
            share=c.post(f'/api/twins/{tid}/sharing',headers=h,json={'recipient':'同事'}).json()
            a={'Authorization':'Bearer '+share['token']}
            assert len(remote.get('/v1/shared',headers=a).json()['knowledge'])==1
            c.put(f'/api/sources/{sid}/ai',headers=h,json={'allow_ai':False})
            assert remote.get('/v1/shared',headers=a).json()['knowledge']==[]
            model.requests.clear()
            assert c.post(f'/api/twins/{tid}/ask',headers=h,json={'question':'怎么审批？'}).json()['context_count']==0
            assert model.requests==[]
            c.delete(f'/api/twins/{tid}/publish',headers=h)
            assert remote.get('/v1/shared',headers=a).status_code==403


def test_revocation_during_model_call_and_stale_publication(tmp_path):
    entered,resume=Event(),Event()
    class Slow(FakeModel):
        def chat(self,messages,max_tokens=1600):
            entered.set();assert resume.wait(10)
            return super().chat(messages,max_tokens)
    app=create_server(tmp_path/'s.sqlite',inference_client=Slow(),publisher_tokens={'alice':'owner-credential-token'})
    with TestClient(app) as c:
        h={'Authorization':'Bearer owner-credential-token'}
        body={'assets':[{'id':1,'title':'知识','body':'审批流程采用统一引擎','version':1,'scope':'global'}], 'twins':[{'id':1,'name':'助手','knowledge_ids':[1]}], 'revision':2}
        pub=c.put('/v1/publications/'+'a'*32,headers=h,json=body)
        assert pub.status_code==200,pub.text
        pid=pub.json()['publications']['1']
        assert c.put('/v1/publications/'+'a'*32,headers=h,json={**body,'revision':1}).status_code==409
        g=c.post('/v1/twins/'+pid+'/grants',headers=h,json={'recipient':'同事'}).json()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(c.post,'/v1/shared/ask',headers={'Authorization':'Bearer '+g['token']},json={'question':'怎么做审批？'})
            assert entered.wait(5)
            c.delete(f'/v1/twins/{pid}/grants/{g["id"]}',headers=h)
            resume.set()
            assert future.result(10).status_code==403


def test_duplicate_twins_reuse_assets_and_pending_sync_is_visible(tmp_path):
    server=create_server(tmp_path/'server.sqlite',inference_client=FakeModel(),publisher_tokens={'alice':'owner-credential-token'})
    with TestClient(server) as remote:
        app=create_app(tmp_path/'local.sqlite',start_worker=False,inference_client=FakeModel(),publishing_client=InProcessPublisher(remote))
        with TestClient(app) as c:
            h=auth(c)
            k=c.post('/api/knowledge',headers=h,json={'title':'共享知识','body':'两个分身使用同一份知识','status':'confirmed'}).json()['id']
            twins=[]
            for name in ('产品','交接'):
                tid=c.post('/api/twins',headers=h,json={'name':name}).json()['id'];twins.append(tid)
                c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[k]})
                c.post(f'/api/twins/{tid}/publish',headers=h)
            with server.state.store.connect() as con:assert con.execute('SELECT count(*) FROM assets').fetchone()[0]==1
            original=app.state.publisher.client.request
            def offline(*args,**kwargs):raise OSError('offline')
            app.state.publisher.client.request=offline
            r=c.delete(f'/api/twins/{twins[0]}/publish',headers=h)
            assert r.json()['state']=='pending'
            assert c.get('/api/settings',headers=h).json()['publication_error']
            app.state.publisher.client.request=original
            assert c.post('/api/sharing/sync',headers=h).json()['state']=='synced'
            with server.state.store.connect() as con:assert con.execute('SELECT count(*) FROM twins').fetchone()[0]==1
