"""Task-triggered browser monitoring: opt-in, signed, bounded, non-continuous."""
import hashlib
import hmac
import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from worktwin.api import create_app


def dashboard_token(client):
    match = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get("/").text)
    return {"X-Worktwin-Token":match.group(1)}


def sign(secret, action, task_id, target, sender, nonce, expires):
    payload="\n".join([action,task_id,target,nonce,str(expires),sender])
    return dict(action=action,task_id=task_id,target_url=target,
                nonce=nonce,expires_at=expires,
                signature=hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest())


def setup(client):
    auth=dashboard_token(client)
    assert client.get("/api/browser-capture",headers=auth).json()["enabled"] is False
    enabled=client.put("/api/browser-capture",headers=auth,json={"enabled":True})
    assert enabled.status_code==200
    code=client.post("/api/browser-capture/pairing",headers=auth).json()["code"]
    paired=client.post("/capture/pair",json={"code":code})
    assert paired.status_code==200,paired.text
    credential={"Authorization":"Bearer "+paired.json()["token"]}
    assert client.post("/capture/heartbeat",headers=credential).status_code==200
    assert client.get("/api/browser-capture",headers=auth).json()["extension_connected"]
    assert client.post("/capture/pair",json={"code":code}).status_code==403
    return auth,credential


def test_signed_capture_stops_on_first_navigation_and_cannot_restart(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN","https://flow.example.com")
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET","x"*48)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        auth,credential=setup(client)
        sender="https://flow.example.com"
        target="https://portal.example.com/approve?token=never-store#step"
        command=sign("x"*48,"start","task42",target,sender,"random_nonce_value_a",int(time.time())+90)
        request={"sender_origin":sender,"envelope":command}
        assert client.post("/capture/command",headers=credential,json={**request,
            "sender_origin":"https://other.example.com"}).status_code==403
        altered={"sender_origin":sender,"envelope":dict(command,target_url="https://bad.example.com")}
        assert client.post("/capture/command",headers=credential,json=altered).status_code==403
        start=client.post("/capture/command",headers=credential,json=request)
        assert start.status_code==200,start.text
        session=start.json()["session_id"]
        assert start.json()["target_page"]=="https://portal.example.com/approve"
        assert client.post("/capture/command",headers=credential,json=request).status_code==409
        bind=dict(session_id=session,tab_id=18,document_id="doc-1",
                  current_url="https://portal.example.com/approve?token=ignored")
        assert client.post("/capture/bind",headers=credential,json=bind).status_code==200
        assert client.post("/capture/bind",headers=credential,json=bind).status_code==409
        event=dict(session_id=session,seq=1,kind="click",tab_id=18,document_id="doc-1",
                   current_url="https://portal.example.com/approve?secret=not-persisted",
                   label="新增规则")
        assert client.post("/capture/event",headers=credential,json=event).status_code==200
        assert client.post("/capture/event",headers=credential,json=event).json()["status"]=="duplicate"
        assert client.post("/capture/event",headers=credential,
                           json=dict(event,seq=3)).status_code==409
        assert client.post("/capture/event",headers=credential,
                           json=dict(event,seq=2,tab_id=19)).status_code==403
        assert client.post("/capture/event",headers=credential,
                           json=dict(event,seq=2,kind="change",label="password")).status_code==200
        nav=dict(event,seq=3,kind="navigation",current_url="https://other.example.com/new?secret=ab")
        assert client.post("/capture/event",headers=credential,json=nav).json()["status"]=="navigation_stopped"
        assert client.post("/capture/event",headers=credential,json=dict(event,seq=4)).status_code==409
        with app.state.db.connect() as db:
            rows=db.execute("SELECT * FROM browser_capture_events ORDER BY seq").fetchall()
            assert len(rows)==3
            assert rows[0]["label"]=="新增规则"
            assert rows[1]["label"]=="[隐藏]"
            assert rows[2]["location"]=="https://other.example.com/new"
            assert all("secret=" not in row["location"] for row in rows)
        steps=client.get(f"/api/browser-capture/sessions/{session}/steps",headers=auth)
        assert steps.status_code==200
        assert steps.json()[0]["start_seq"]==1
        assert steps.json()[0]["end_seq"]==3
        assert steps.json()[0]["status"]=="stopped"
        complete=sign("x"*48,"complete","task42",target,sender,
                      "random_nonce_value_b",int(time.time())+90)
        done=client.post("/capture/command",headers=credential,
                         json={"sender_origin":sender,"envelope":complete})
        assert done.json()["status"]=="completed"
        another=sign("x"*48,"start","task42",target,sender,
                     "random_nonce_value_c",int(time.time())+90)
        assert client.post("/capture/command",headers=credential,
                           json={"sender_origin":sender,"envelope":another}).status_code==409
        assert client.get("/api/browser-capture/sessions",headers=auth).json()[0]["event_count"]==3


def test_default_disabled_and_local_extension_pairing_boundaries(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN","https://flow.example.com")
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET","y"*48)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        auth=dashboard_token(client)
        assert client.post("/api/browser-capture/pairing",headers=auth).status_code==409
        assert client.post("/capture/pair",json={"code":"random-unused-code"}).status_code==403
        auth,credential=setup(client)
        assert client.post("/capture/heartbeat",headers={"Authorization":"Bearer fake"}).status_code==401
        assert client.post("/capture/heartbeat",headers={**credential,
            "Origin":"https://evil.example"}).status_code==403
        extension=client.get("/api/browser-capture/extension",headers=auth)
        assert extension.status_code==200
        with zipfile.ZipFile(io.BytesIO(extension.content)) as z:
            assert {"manifest.json","content.js","popup.html","popup.js",
                    "service-worker.js"}<=set(z.namelist())
            manifest=json.loads(z.read("manifest.json"))
            assert manifest["manifest_version"]==3
            assert "https://flow.example.com/*" in manifest["externally_connectable"]["matches"]
        assert client.put("/api/browser-capture",headers=auth,
                          json={"enabled":False}).status_code==200
        assert client.post("/capture/heartbeat",headers=credential).status_code==403
        assert client.get("/api/browser-capture",headers=auth).json()["extension_connected"] is False


def test_navigation_to_browser_internal_url_closes_session(monkeypatch,tmp_path):
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_ORIGIN","https://flow.example.com")
    monkeypatch.setenv("WORKTWIN_CAPTURE_FLOW_SECRET","z"*48)
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as client:
        _,credential=setup(client)
        sender="https://flow.example.com"
        target="https://portal.example.com/home"
        command=sign("z"*48,"start","task9",target,sender,"unique_nonce_nine_A",int(time.time())+80)
        s=client.post("/capture/command",headers=credential,
                      json={"sender_origin":sender,"envelope":command}).json()["session_id"]
        client.post("/capture/bind",headers=credential,json={
            "session_id":s,"tab_id":7,"document_id":"doc-seven","current_url":target})
        nav=client.post("/capture/event",headers=credential,json={
            "session_id":s,"seq":1,"kind":"navigation","tab_id":7,
            "document_id":"doc-seven","current_url":"chrome://settings/"})
        assert nav.status_code==200,nav.text
        with app.state.db.connect() as con:
            row=con.execute("SELECT location FROM browser_capture_events").fetchone()
            assert row["location"]=="[离开 HTTPS 网页]"
