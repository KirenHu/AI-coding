"""Configuration, credential storage and the confirmed-knowledge boundary."""
import io
import json
import re
import sqlite3
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.server import create_server
from tests.support import FakeModel


def auth(client):
    return {'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)}


@pytest.fixture
def provider():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            assert self.path=='/v1/models'
            calls.append({'authorization':self.headers['Authorization'],'path':self.path})
            data=json.dumps({'data':[{'id':'personal-model'},{'id':'company-model'},{'id':'personal-model'}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'authorization':self.headers['Authorization'],'body':body,'path':self.path})
            if body['model']=='broken':
                self.send_error(401);return
            data=json.dumps({'choices':[{'message':{'content':'OK'}}],'usage':{'total_tokens':8}}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1',calls
    server.shutdown();thread.join();server.server_close()


def test_personal_configuration_real_call_persistence_and_failure(tmp_path,provider):
    url,calls=provider;path=tmp_path/'worktwin.sqlite'
    app=create_app(path,start_worker=False)
    with TestClient(app) as c:
        h=auth(c)
        assert c.get('/api/settings',headers=h).json()['model_status']=='not_configured'
        models=c.post('/api/model/personal/models',headers=h,json={'base_url':url,'api_key':'private-personal-key'})
        assert models.json()['models']==['company-model','personal-model']
        assert not app.state.model.configured  # Listing is not a successful configuration test.
        cfg={'base_url':url,'model':'personal-model','api_key':'private-personal-key'}
        assert c.put('/api/model/personal',headers=h,json=cfg).status_code==200
        assert calls[-1]['path']=='/v1/chat/completions'
        assert calls[-1]['authorization']=='Bearer private-personal-key'
        assert calls[-1]['body']['model']=='personal-model'
        assert calls[-1]['body']['messages']==[{'role':'user','content':'请只回复 OK。'}]
        assert c.put('/api/model/personal',headers=h,json={**cfg,'model':'broken','api_key':'rejected-key'}).status_code==400
        info=c.get('/api/settings',headers=h).json()
        assert info['personal_model']=='personal-model' and info['model_status']=='connected'
        assert 'private-personal-key' not in json.dumps(info)
        assert c.post('/api/model/test',headers=h).status_code==200
        assert calls[-1]['body']['model']=='personal-model'
        backup=zipfile.ZipFile(io.BytesIO(c.get('/api/backup',headers=h).content))
        saved=tmp_path/'copy.sqlite';saved.write_bytes(backup.read('worktwin.sqlite'))
        con=sqlite3.connect(saved)
        assert not any('private-personal-key' in str(row) for row in con.execute('SELECT * FROM settings'))
        con.close()
    assert 'private-personal-key' not in (tmp_path/'credentials/credentials.json').read_text()
    assert 'private-personal-key' not in path.read_bytes().decode(errors='ignore')
    with TestClient(create_app(path,start_worker=False)) as c:
        h=auth(c);assert c.get('/api/settings',headers=h).json()['model_status']=='configured'
        assert c.post('/api/model/test',headers=h).status_code==200
        assert calls[-1]['authorization']=='Bearer private-personal-key'


def test_personal_unsafe_urls_and_endpoint_key_reuse(tmp_path,provider):
    url,_=provider
    with TestClient(create_app(tmp_path/'local.sqlite',start_worker=False)) as c:
        h=auth(c);cfg={'base_url':url,'model':'personal','api_key':'secret'}
        assert c.put('/api/model/personal',headers=h,json=cfg).status_code==200
        for unsafe in ['http://public.example/v1','https://user:password@host.example/v1','https://host.example/v1?key=secret']:
            assert c.put('/api/model/personal',headers=h,json={**cfg,'base_url':unsafe}).status_code==400
        assert c.put('/api/model/personal',headers=h,json={**cfg,'base_url':url+'/other','api_key':''}).status_code==400
        assert c.put('/api/edition',headers=h,json={'edition':'enterprise'}).status_code==200
        assert c.put('/api/model/personal',headers=h,json=cfg).status_code==403
        with TestClient(create_app(tmp_path/'local.sqlite',start_worker=False)) as restarted:
            assert not restarted.get('/api/settings',headers=auth(restarted)).json()['enterprise_model_ready']


def test_admin_bootstrap_persistent_model_and_revocable_employees(tmp_path,monkeypatch,provider):
    url,calls=provider;monkeypatch.setenv('WORKTWIN_ADMIN_TOKEN','admin-'+'x'*40)
    monkeypatch.delenv('WORKTWIN_BYOK_API_KEY',raising=False);monkeypatch.delenv('WORKTWIN_BYOK_MODEL',raising=False)
    path=tmp_path/'server.sqlite';admin={'Authorization':'Bearer admin-'+'x'*40}
    server=create_server(path,publisher_tokens={})
    with TestClient(server) as c:
        assert c.get('/v1/me',headers=admin).json()['role']=='admin'
        assert not c.get('/v1/admin/settings',headers=admin).json()['model_configured']
        cfg={'base_url':url,'model':'company-model','api_key':'company-private-key','daily_calls':123,'daily_tokens':200000,'minute_calls':10}
        assert c.post('/v1/admin/models',headers=admin,json={'base_url':url,'api_key':'company-private-key'}).json()['models']==['company-model','personal-model']
        assert c.put('/v1/admin/model',headers=admin,json=cfg).status_code==200
        assert c.put('/v1/admin/model',headers=admin,json={**cfg,'model':'broken'}).status_code==400
        info=c.get('/v1/admin/settings',headers=admin).json();assert info['model']=='company-model' and info['daily_calls']==123
        issued=c.post('/v1/admin/employees',headers=admin,json={'identity':'alice@example.com'}).json()
        token=issued['token'];employee={'Authorization':'Bearer '+token}
        assert c.get('/v1/admin/settings',headers=employee).status_code==403
        assert c.post('/v1/admin/models',headers=employee,json={'base_url':url}).status_code==403
        me=c.get('/v1/me',headers=employee).json();assert me['role']=='employee' and 'company-private-key' not in json.dumps(me)
        assert c.post('/v1/chat/completions',headers=employee,json={'messages':[{'role':'user','content':'hello'}]}).status_code==200
        assert calls[-1]['authorization']=='Bearer company-private-key'
        publication={'assets':[{'id':1,'title':'知识','body':'正文','version':1}],'twins':[{'id':1,'name':'助手','knowledge_ids':[1]}],'revision':1}
        published=c.put('/v1/publications/'+'a'*32,headers=employee,json=publication).json()
        twin=published['publications']['1']
        grant=c.post(f'/v1/twins/{twin}/grants',headers=employee,json={'recipient':'receiver'}).json()
        share={'Authorization':'Bearer '+grant['token']}
        assert c.get('/v1/shared/knowledge/1',headers=share).status_code==200
        assert c.get('/v1/shared/knowledge/2',headers=share).status_code==404
        assert c.delete('/v1/admin/employees/alice@example.com',headers=admin).status_code==200
        assert c.get('/v1/me',headers=employee).status_code==401
        assert c.get('/v1/shared',headers=share).status_code==403
    with TestClient(create_server(path,publisher_tokens={'alice@example.com':token})) as c:
        assert c.get('/v1/me',headers=employee).status_code==401
        assert c.get('/v1/admin/settings',headers=admin).json()['model']=='company-model'
    assert 'company-private-key' not in path.read_bytes().decode(errors='ignore')
    assert token not in path.read_bytes().decode(errors='ignore')


def test_draft_archive_restore_and_atomic_twin_save(tmp_path):
    with TestClient(create_app(tmp_path/'local.sqlite',start_worker=False,inference_client=FakeModel())) as c:
        h=auth(c);draft={'title':'流程','body':'审批复用统一工作流引擎','status':'draft'}
        kid=c.post('/api/knowledge',headers=h,json=draft).json()['id']
        tid=c.post('/api/twins',headers=h,json={'name':'original'}).json()['id']
        assert c.put(f'/api/twins/{tid}',headers=h,json={'name':'invalid-change','knowledge_ids':[kid]}).status_code==400
        assert c.get(f'/api/twins/{tid}',headers=h).json()['name']=='original'
        assert c.put(f'/api/knowledge/{kid}',headers=h,json={**draft,'status':'confirmed'}).status_code==200
        assert c.put(f'/api/twins/{tid}',headers=h,json={'name':'saved','knowledge_ids':[kid]}).status_code==200
        assert c.get(f'/api/twins/{tid}/preview',headers=h).json()['usable_count']==1
        assert c.put(f'/api/knowledge/{kid}',headers=h,json={**draft,'status':'archived'}).status_code==200
        assert c.get(f'/api/twins/{tid}/preview',headers=h).json()['usable_count']==0
        assert c.post(f'/api/knowledge/{kid}/restore',headers=h).status_code==200
        assert c.get(f'/api/knowledge-item/{kid}',headers=h).json()['status']=='draft'
        assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[kid]}).status_code==400
        assert c.get('/api/knowledge-counts',headers=h).json()['draft']==1


def test_employee_revoked_while_model_running(tmp_path,monkeypatch):
    monkeypatch.setenv('WORKTWIN_ADMIN_TOKEN','administrator-'+'z'*40)
    class RevokingModel(FakeModel):
        def chat(self,messages,max_tokens=1800):
            server.state.settings.revoke('alice')
            return 'should not be returned'
    server=create_server(tmp_path/'server.sqlite',inference_client=RevokingModel(),publisher_tokens={'alice':'employee-token-value'})
    with TestClient(server) as c:
        r=c.post('/v1/chat/completions',headers={'Authorization':'Bearer employee-token-value'},json={'messages':[{'role':'user','content':'hello'}]})
        assert r.status_code==401 and 'should not be returned' not in r.text
