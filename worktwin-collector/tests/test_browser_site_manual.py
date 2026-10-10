"""Evidence-backed, continuously maintained manuals for explicitly watched sites."""
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from tests.test_browser_capture import sign


def authorized(client):
    html=client.get('/').text
    token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)
    return {'X-Worktwin-Token':token}


def connect(client, dashboard):
    assert client.put('/api/browser-capture',headers=dashboard,
                      json={'enabled':True}).status_code==200
    code=client.post('/api/browser-capture/pairing',headers=dashboard).json()['code']
    token=client.post('/capture/pair',json={'code':code}).json()['token']
    return {'Authorization':'Bearer '+token}


def manually_record(client, dashboard, extension, *, site='https://example.org',
                    label='保存订单', second='修改数量'):
    result=client.put('/api/browser-capture/manual',headers=dashboard,
                      json={'enabled':True,'url':site+'/orders?api_key=private'})
    assert result.status_code==200,result.text
    sid=result.json()['manual_session_id']
    assert client.post('/capture/bind',headers=extension,json={
        'session_id':sid,'tab_id':12,'document_id':'doc-one',
        'current_url':site+'/orders'}).status_code==200
    for seq,kind,name in ((1,'click',label),(2,'change',second),
                          (3,'feedback','feedback_success')):
        answer=client.post('/capture/event',headers=extension,json={
            'session_id':sid,'tab_id':12,'document_id':'doc-one',
            'current_url':site+'/orders','seq':seq,'kind':kind,'label':name})
        assert answer.status_code==200,answer.text
    stop=client.put('/api/browser-capture/manual',headers=dashboard,
                    json={'enabled':False})
    assert stop.status_code==200,stop.text
    return sid


def site_notes(client, dashboard):
    return [k for k in client.get('/api/knowledge',
             headers=dashboard,params={'status':'all','limit':1000}).json()
            if k['kind']=='process' and k['project_key'].startswith('browser-site:')]


def test_two_manual_sessions_update_one_existing_site_note_with_history(tmp_path):
    app=create_app(tmp_path/'site.sqlite',start_worker=False)
    with TestClient(app) as client:
        dashboard=authorized(client)
        extension=connect(client,dashboard)
        sid1=manually_record(client,dashboard,extension)
        notes=site_notes(client,dashboard)
        assert len(notes)==1,notes
        first=notes[0]
        assert first['version']==1 and first['status']=='confirmed'
        assert first['scope']=='project' and first['created_by']=='enterprise_ai'
        assert first['source_bound']==0
        assert '保存订单' in first['body'] and '输入值' in first['body']
        assert sid1 in first['body']
        # New recording of the same steps updates that SAME article. Exact
        # repeated actions are grouped as observations, not copied documents.
        sid2=manually_record(client,dashboard,extension)
        notes=site_notes(client,dashboard)
        assert len(notes)==1 and notes[0]['id']==first['id']
        assert notes[0]['version']==2 and '观察 2 次' in notes[0]['body']
        assert sid2 in notes[0]['body']
        history=client.get(f"/api/knowledge/{first['id']}/history",
                           headers=dashboard).json()
        assert len(history)==1 and history[0]['version']==1
        assert sid1 in history[0]['body']
        assert client.post('/api/browser-capture/sessions/'+sid2+'/summarize',
                           headers=dashboard).status_code==200
        assert site_notes(client,dashboard)[0]['version']==2
        with app.state.db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0]==2
            assert con.execute('SELECT COUNT(*) FROM browser_site_manuals').fetchone()[0]==1


def test_different_sites_get_separate_manuals(tmp_path):
    app=create_app(tmp_path/'site.sqlite',start_worker=False)
    with TestClient(app) as client:
        dashboard=authorized(client)
        extension=connect(client,dashboard)
        manually_record(client,dashboard,extension,site='https://example.org')
        manually_record(client,dashboard,extension,site='https://another.example')
        notes=site_notes(client,dashboard)
        assert len(notes)==2
        assert len({k['project_key'] for k in notes})==2
        assert all(k['scope']=='project' for k in notes)


def test_owner_edit_stays_authoritative_even_after_new_browser_recordings(tmp_path):
    app=create_app(tmp_path/'owner.sqlite',start_worker=False)
    with TestClient(app) as client:
        dashboard=authorized(client)
        extension=connect(client,dashboard)
        manually_record(client,dashboard,extension)
        item=site_notes(client,dashboard)[0]
        manual=client.put(f"/api/knowledge/{item['id']}",headers=dashboard,json={
            'kind':'process','title':'个人修订的网站手册',
            'body':'人工修正后的唯一有效结论','status':'confirmed'})
        assert manual.status_code==200,manual.text
        manually_record(client,dashboard,extension,label='下一步保存')
        latest=site_notes(client,dashboard)[0]
        assert latest['body']=='人工修正后的唯一有效结论'
        assert latest['created_by']=='human'
        with app.state.db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0]==2


def test_workflow_signed_capture_never_enters_manual_site_knowledge(tmp_path,monkeypatch):
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_ORIGIN','https://flow.example.com')
    secret='a'*48
    monkeypatch.setenv('WORKTWIN_CAPTURE_FLOW_SECRET',secret)
    app=create_app(tmp_path/'task.sqlite',start_worker=False)
    with TestClient(app) as client:
        dashboard=authorized(client)
        extension=connect(client,dashboard)
        website='https://example.org/orders'
        sender='https://flow.example.com'
        from time import time
        env=sign(secret,'start','task-only',website,sender,'nonce_task_only_123456',int(time())+90)
        response=client.post('/capture/command',headers=extension,json={
            'sender_origin':sender,'envelope':env})
        assert response.status_code==200,response.text
        sid=response.json()['session_id']
        client.post('/capture/bind',headers=extension,json={
            'session_id':sid,'tab_id':22,'document_id':'taskdoc','current_url':website})
        client.post('/capture/event',headers=extension,json={
            'session_id':sid,'tab_id':22,'document_id':'taskdoc',
            'current_url':website,'kind':'click','seq':1,'label':'保存订单'})
        env2=sign(secret,'complete','task-only',website,sender,'nonce_task_end_12345678',int(time())+90)
        done=client.post('/capture/command',headers=extension,json={
            'sender_origin':sender,'envelope':env2})
        assert done.status_code==200,done.text
        assert client.post(f'/api/browser-capture/sessions/{sid}/summarize',
                           headers=dashboard).status_code==200
        assert site_notes(client,dashboard)==[]
        with app.state.db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM browser_site_observations').fetchone()[0]==0
