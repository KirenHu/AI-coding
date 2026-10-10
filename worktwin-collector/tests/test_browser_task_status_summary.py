"""Task-state polling and concise operation summaries, without knowledge writes."""
import hashlib
import hmac
import json
import re
import time

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.browser_capture_status import validate_status_path,fetch_task_state


FLOW="https://flow.example.com"
SECRET="s"*48
TARGET="https://portal.example.com/approval"


def owner(client):
    value=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get("/").text).group(1)
    return {"X-Worktwin-Token":value}


def envelope(task,action="start",target=TARGET,nonce="nonce_for_task_event_0123",
             status_path=None):
    path=status_path or "/api/worktwin/tasks/"+task+"/status"
    expires=int(time.time())+75
    fields=(action,task,target,nonce,str(expires),FLOW,path)
    signature=hmac.new(SECRET.encode(),"\n".join(fields).encode(),hashlib.sha256).hexdigest()
    return dict(action=action,task_id=task,target_url=target,nonce=nonce,
                expires_at=expires,status_path=path,signature=signature)


def connected(client):
    dashboard=owner(client)
    assert client.put("/api/browser-capture",headers=dashboard,json={"enabled":True}).status_code==200
    code=client.post("/api/browser-capture/pairing",headers=dashboard).json()["code"]
    credential=client.post("/capture/pair",json={"code":code}).json()["token"]
    return dashboard,{"Authorization":"Bearer "+credential}


def started(client,credential,task="task-1"):
    body={"sender_origin":FLOW,"envelope":envelope(task)}
    response=client.post("/capture/command",headers=credential,json=body)
    assert response.status_code==200,response.text
    sid=response.json()["session_id"]
    bind=client.post("/capture/bind",headers=credential,json=dict(
        session_id=sid,tab_id=12,document_id="doc_12",current_url=TARGET))
    assert bind.status_code==200,bind.text
    return sid


def test_task_status_polling_ends_collection_without_browser_page(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN",FLOW)
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET",SECRET)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        dashboard,credential=connected(client)
        sid=started(client,credential,"task-finish")
        observed=[]
        def fetcher(origin,task,path,secret):
            observed.append((origin,task,path,secret))
            return "running" if len(observed)==1 else "completed"
        now=int(time.time())
        first=app.state.browser_capture.poll_task_states(fetcher,now=now+10)
        assert first=={"checked":1,"closed":0}
        assert client.post("/capture/sessions/status",headers=credential,
            json={"session_ids":[sid]}).json()["sessions"][sid]=="capturing"
        second=app.state.browser_capture.poll_task_states(fetcher,now=now+22)
        assert second=={"checked":1,"closed":1}
        assert observed==[(FLOW,"task-finish","/api/worktwin/tasks/task-finish/status",SECRET)]*2
        assert client.post("/capture/sessions/status",headers=credential,
            json={"session_ids":[sid]}).json()["sessions"][sid]=="completed"
        assert client.post("/capture/event",headers=credential,json={
            "session_id":sid,"seq":1,"kind":"click","tab_id":12,
            "document_id":"doc_12","current_url":TARGET,"label":"提交"}).status_code==409
        assert client.get("/api/browser-capture",headers=dashboard).json()["active_tasks"]==0


def test_fail_closed_if_completion_cannot_be_checked(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN",FLOW)
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET",SECRET)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        _,credential=connected(client)
        sid=started(client,credential,"task-offline")
        def offline(*_):
            raise OSError("status server unreachable")
        now=int(time.time())
        assert app.state.browser_capture.poll_task_states(offline,now=now+9)["closed"]==0
        assert app.state.browser_capture.poll_task_states(offline,now=now+19)["closed"]==0
        assert app.state.browser_capture.poll_task_states(offline,now=now+29)["closed"]==1
        state=client.post("/capture/sessions/status",headers=credential,
            json={"session_ids":[sid]}).json()["sessions"][sid]
        assert state=="status_unavailable"
        with app.state.db.connect() as con:
            assert con.execute("SELECT state FROM browser_capture_tasks").fetchone()[0]=="status_unavailable"


def test_status_endpoint_only_signed_same_origin_paths(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN",FLOW)
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET",SECRET)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        _,credential=connected(client)
        for path in ("http://127.0.0.1:8765/api/secrets",
                     "//evil.example/api/status","/api/../admin",
                     "/api/%2e%2e/admin","/api/tasks?next=https://evil.example"):
            payload=envelope("task-bad",status_path=path,nonce="unique_nonce_for_bad")
            result=client.post("/capture/command",headers=credential,json={
                "sender_origin":FLOW,"envelope":payload})
            assert result.status_code==400,(path,result.text)
        valid=envelope("task-safe")
        altered=dict(valid,status_path="/api/worktwin/other-task")
        assert client.post("/capture/command",headers=credential,json={
            "sender_origin":FLOW,"envelope":altered}).status_code==403
    assert validate_status_path("/api/worktwin/tasks/abc/status") == "/api/worktwin/tasks/abc/status"


def test_status_transport_pins_origin_and_signs_read_request(monkeypatch):
    from worktwin import browser_capture_status as transport
    requests=[]
    class Response:
        status=200
        def __enter__(self):
            return self
        def __exit__(self,*_):
            return None
        def read(self,_):
            return json.dumps({"task_id":"task-signed","status":"completed"}).encode()
    class Opener:
        def open(self,request,timeout):
            requests.append((request,timeout))
            return Response()
    monkeypatch.setattr(transport,"build_opener",lambda *_:Opener())
    path="/api/worktwin/tasks/task-signed/status"
    state=fetch_task_state(FLOW,"task-signed",path,SECRET,clock=lambda:123456)
    assert state=="completed"
    req,timeout=requests[0]
    assert req.full_url==FLOW+path and timeout==5
    assert req.get_header("X-worktwin-task")=="task-signed"
    signature=hmac.new(SECRET.encode(),
        ("GET\n"+"task-signed\n"+path+"\n123456").encode(),
        hashlib.sha256).hexdigest()
    assert req.get_header("X-worktwin-signature")==signature


def test_model_summary_requires_observed_values_and_outcomes():
    from worktwin.browser_capture_summary import parse_model_summary
    observations=[
        {"seq":1,"kind":"click","label":"新增规则"},
        {"seq":2,"kind":"change","label":"审批方式"},
        {"seq":3,"kind":"click","label":"保存"}
    ]
    def candidate(summary):
        return json.dumps({"summary":summary,"event_ids":[1,2,3]},ensure_ascii=False)
    assert parse_model_summary(candidate("用户点击新增规则，将审批方式设置为多人审批，保存成功。"),observations) is None
    assert parse_model_summary(candidate("用户点击新增规则，修改审批方式并点击保存。"),observations)
    assert parse_model_summary(candidate("用户点击新增规则，页面提示保存成功。"),observations) is None


class Model:
    configured=True
    def __init__(self):
        self.calls=[]
        self.result=json.dumps({"summary":"用户点击新增规则，修改了审批方式，并点击保存。页面提示操作成功。",
                                "event_ids":[1,2,3,4]},ensure_ascii=False)
    def chat(self,messages,max_tokens=450):
        self.calls.append(messages)
        return self.result


def test_live_summary_is_not_a_knowledge_note_and_respects_ai_permission(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN",FLOW)
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET",SECRET)
    model=Model()
    app=create_app(tmp_path/"db.sqlite",start_worker=False,inference_client=model)
    with TestClient(app) as client:
        dashboard,credential=connected(client)
        sid=started(client,credential,"task-summary")
        actions=[("click","按钮：新增规则"),("change","字段：审批方式"),
                 ("click","按钮：保存"),("feedback","feedback_success")]
        for seq,(kind,label) in enumerate(actions,1):
            response=client.post("/capture/event",headers=credential,json={
                "session_id":sid,"seq":seq,"kind":kind,"label":label,
                "tab_id":12,"document_id":"doc_12","current_url":TARGET})
            assert response.status_code==200,response.text
        # Without separate consent, summarization is entirely local and free.
        first=client.post(f"/api/browser-capture/sessions/{sid}/summarize",headers=dashboard)
        assert first.status_code==200,first.text
        assert first.json()["summary_source"]=="rule" and "保存" in first.json()["summary"]
        assert model.calls==[]
        assert client.put("/api/browser-capture/ai",headers=dashboard,
                          json={"allow_ai":True}).status_code==200
        summary=client.post(f"/api/browser-capture/sessions/{sid}/summarize",headers=dashboard)
        assert summary.status_code==200,summary.text
        assert summary.json()["summary_source"]=="model"
        assert summary.json()["evidence_seq"]==[1,2,3,4]
        assert len(model.calls)==1
        assert "task-summary" not in json.dumps(model.calls,ensure_ascii=False)
        with app.state.db.connect() as con:
            assert con.execute("SELECT count(*) FROM knowledge").fetchone()[0]==0
            assert con.execute("SELECT count(*) FROM documents").fetchone()[0]==0
            assert con.execute("SELECT count(*) FROM ai_jobs").fetchone()[0]==0
        model.result=json.dumps({"summary":"用户已经成功完成了所有配置并经过验收。",
                                "event_ids":[1]},ensure_ascii=False)
        # No new page-success evidence would reject an unsupported conclusion.
        client.put("/api/browser-capture",headers=dashboard,json={"enabled":False})
        assert client.get("/api/browser-capture",headers=dashboard).json()["allow_ai"] is False
