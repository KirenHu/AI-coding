"""Real SQLite + HTTP acceptance for opt-in workflow-triggered browser capture."""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from worktwin.api import create_app


ISSUER = "https://flow.example.test"
URL = "https://booking.example.test/orders/approve?temporary_token=never-save"
TARGET = "https://booking.example.test/orders/approve"


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def sign(private, action, *, nonce, task_id="task_123", subject="staff_1",
         request_id="click_1", target_url=URL, **overrides):
    now = int(time.time())
    payload = dict(iss=ISSUER, action=action, nonce=nonce, task_id=task_id,
                   subject=subject, request_id=request_id, iat=now, exp=now+60,
                   target_url=target_url, goal="调整审批人", project_id="project_abc")
    payload.update(overrides)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return b64(raw) + "." + b64(private.sign(raw))


def test_browser_opt_in_signed_trigger_bound_tab_and_navigation(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,format=serialization.PublicFormat.Raw)
    monkeypatch.setenv("WORKTWIN_CAPTURE_TRUSTED_PLATFORMS", json.dumps({ISSUER:b64(public)}))
    app = create_app(tmp_path/"worktwin.sqlite",start_worker=False)
    with TestClient(app) as c:
        h = {"X-Worktwin-Token":re.search(
            r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get("/").text).group(1)}
        status = c.get("/api/capture/status",headers=h).json()
        assert status["enabled"] is False
        assert not status["extension_connected"]
        assert status["trusted_platform_count"] == 1
        assert not status["default_browser_extension_verified"]
        assert c.post("/api/capture/pair",headers=h).status_code == 403
        assert c.post("/api/capture/ext/heartbeat",json={"browser":"Chrome"}).status_code == 403
        assert c.put("/api/capture/settings",headers=h,json={"enabled":True}).status_code == 200
        pair = c.post("/api/capture/pair",headers=h).json()["pairing_token"]
        extension = {"X-Worktwin-Capture-Key":pair}
        assert c.post("/api/capture/ext/heartbeat",headers=extension,
                      json={"browser":"Chrome","version":"0.1.0"}).json()["connected"]
        connected = c.get("/api/capture/status",headers=h).json()
        assert connected["extension_connected"]
        assert connected["extension_browser"] == "Chrome"
        trigger = sign(private,"capture.start",nonce="uniquenonce_0001")
        call = lambda ticket, source_origin=ISSUER: c.post("/api/capture/ext/signal",
            headers=extension,json={"ticket":ticket,"source_origin":source_origin})
        assert call(trigger, "https://evil.example.test").status_code == 403
        assert call("invalid.signature").status_code == 422  # schema rejects too-short tickets
        assert call("a"*40+"."+"b"*70).status_code == 400  # malformed signed JSON
        assert call(trigger).status_code == 200
        start = call(trigger)
        assert start.status_code == 409  # nonce replay
        sessions = connected  # assigned only by a signed workflow ticket
        capture_id = c.get("/api/capture/status",headers=h).json()["sessions"][0]["id"]
        bind = lambda tab,doc,url: c.post("/api/capture/ext/bind",headers=extension,
              json={"session_id":capture_id,"tab_id":tab,"document_id":doc,"page_url":url})
        assert bind(21,"document-a","https://other.example.test").status_code == 403
        assert bind(21,"document-a",URL).json()["recording"]
        assert bind(21,"document-b",URL).status_code == 409
        payload = {
            "session_id":capture_id,"tab_id":21,"document_id":"document-a","page_url":URL,
            "events":[
                {"seq":1,"kind":"click","occurred_at":int(time.time()*1000),
                 "details":{"tag":"button","label":"保存设置","value":"SHOULD_REJECT"}},
            ]}
        assert c.post("/api/capture/ext/events",headers=extension,json=payload).status_code == 400
        payload["events"] = [
            {"seq":1,"kind":"click","occurred_at":int(time.time()*1000),
             "details":{"tag":"button","label":"保存设置"}},
            {"seq":2,"kind":"feedback","occurred_at":int(time.time()*1000),
             "details":{"role":"status","label":"保存成功"}},
            {"seq":4,"kind":"change","occurred_at":int(time.time()*1000),
             "details":{"tag":"select","field_type":"select"}},
        ]
        assert c.post("/api/capture/ext/events",headers=extension,json=payload).json() == {
            "accepted":3,"last_seq":4,"event_gaps":1}
        assert c.post("/api/capture/ext/events",headers=extension,json=payload).json()["accepted"] == 0
        wrong = dict(payload,tab_id=22,events=[{"seq":5,"kind":"click","details":{"label":"偷看"}}])
        assert c.post("/api/capture/ext/events",headers=extension,json=wrong).status_code == 403
        trace = c.get("/api/capture/sessions/"+capture_id,headers=h).json()
        text = json.dumps(trace,ensure_ascii=False)
        assert "保存设置" in text and "页面显示反馈" in text
        assert "never-save" not in text
        assert all(s["evidence_kind"]=="observed" for s in trace["steps"])
        nav = c.post("/api/capture/ext/navigation",headers=extension,json={
            "session_id":capture_id,"tab_id":21,"document_id":"document-a",
            "to_url":"https://booking.example.test/next?access_token=secret",
            "reason":"same_tab_navigation"})
        assert nav.json() == {"capture_state":"navigated","task_state":"open"}
        assert c.post("/api/capture/ext/events",headers=extension,json={
            **payload,"events":[{"seq":5,"kind":"click","details":{"label":"其他页面"}}]}).status_code == 409
        stale = c.get("/api/capture/status",headers=h).json()["sessions"][0]
        assert stale["task_state"]=="open" and stale["capture_state"]=="navigated"
        assert stale["last_seq"]==5
        assert c.get("/api/capture/sessions/"+capture_id,headers=h).json()["steps"][-1]["summary"].endswith(
            "新页面不在本次采集范围内")
        # Even a valid completion ticket for a different employee cannot close the task.
        wrong_completion = sign(private,"capture.complete",nonce="uniquenonce_0002",subject="different")
        assert call(wrong_completion).json()["closed_sessions"] == []
        complete = sign(private,"capture.complete",nonce="uniquenonce_0003")
        assert call(complete).json()["closed_sessions"] == [capture_id]
        assert c.get("/api/capture/status",headers=h).json()["sessions"][0]["task_state"]=="completed"


def test_disable_revokes_sessions_and_pairing_rotation(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    monkeypatch.setenv("WORKTWIN_CAPTURE_TRUSTED_PLATFORMS",json.dumps({ISSUER:b64(pub)}))
    app = create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as c:
        h = {"X-Worktwin-Token":re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get("/").text).group(1)}
        c.put("/api/capture/settings",headers=h,json={"enabled":True,"allow_ai":True})
        pair1 = c.post("/api/capture/pair",headers=h).json()["pairing_token"]
        pair2 = c.post("/api/capture/pair",headers=h).json()["pairing_token"]
        signal = {"ticket":sign(key,"capture.start",nonce="uniquenonce_0004"),"source_origin":ISSUER}
        assert c.post("/api/capture/ext/signal",
                      headers={"X-Worktwin-Capture-Key":pair1},json=signal).status_code==401
        assert c.post("/api/capture/ext/signal",
                      headers={"X-Worktwin-Capture-Key":pair2},json=signal).status_code==200
        assert c.put("/api/capture/settings",headers=h,
                     json={"enabled":False,"allow_ai":False}).json()["enabled"] is False
        assert c.post("/api/capture/ext/heartbeat",
                      headers={"X-Worktwin-Capture-Key":pair2},json={}).status_code==403
        session = c.get("/api/capture/status",headers=h).json()["sessions"][0]
        assert session["task_state"]=="cancelled"
        assert session["capture_state"]=="disabled"
        assert not c.get("/api/capture/status",headers=h).json()["allow_ai"]
        with app.state.db.connect() as con:
            assert con.execute("SELECT count(*) FROM browser_capture_events").fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 0


def test_expired_tickets_and_untrusted_issuer_rejected(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    monkeypatch.setenv("WORKTWIN_CAPTURE_TRUSTED_PLATFORMS",json.dumps({ISSUER:b64(pub)}))
    app = create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as c:
        h = {"X-Worktwin-Token":re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get("/").text).group(1)}
        c.put("/api/capture/settings",headers=h,json={"enabled":True})
        pair=c.post("/api/capture/pair",headers=h).json()["pairing_token"]
        headers={"X-Worktwin-Capture-Key":pair}
        now=int(time.time())
        late=sign(key,"capture.start",nonce="uniquenonce_0005",iat=now-500,exp=now-200)
        assert c.post("/api/capture/ext/signal",headers=headers,
                      json={"ticket":late,"source_origin":ISSUER}).status_code==403
        forged=sign(key,"capture.start",nonce="uniquenonce_0006",iss="https://other.test")
        assert c.post("/api/capture/ext/signal",headers=headers,
                      json={"ticket":forged,"source_origin":"https://other.test"}).status_code==403
        no_http=sign(key,"capture.start",nonce="uniquenonce_0007",
                     target_url="http://insecure.example.test/unsafe")
        assert c.post("/api/capture/ext/signal",headers=headers,
                      json={"ticket":no_http,"source_origin":ISSUER}).status_code==400
        assert c.get("/api/capture/status",headers=h).json()["sessions"] == []


def test_browser_ai_is_separate_permission_and_never_auto_verifies_task(tmp_path, monkeypatch):
    from worktwin.browser_analysis import BrowserCaptureAnalyzer
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    monkeypatch.setenv("WORKTWIN_CAPTURE_TRUSTED_PLATFORMS",json.dumps({ISSUER:b64(public)}))

    class SemanticModel:
        configured = True
        def __init__(self, app=None):
            self.requests = []
            self.app = app
            self.revoke_during_call = False
        def chat(self,messages,max_tokens=650):
            self.requests.append(messages)
            if self.revoke_during_call:
                self.app.state.browser_capture.configure(False,False)
            return json.dumps({"summary":"页面进行了保存操作，并显示成功提示，但未确认服务端持久化。"})

    model=SemanticModel()
    app=create_app(tmp_path/"db.sqlite",start_worker=False,inference_client=model)
    model.app=app
    with TestClient(app) as c:
        h={"X-Worktwin-Token":re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get("/").text).group(1)}
        c.put("/api/capture/settings",headers=h,json={"enabled":True})
        pair=c.post("/api/capture/pair",headers=h).json()["pairing_token"]
        e={"X-Worktwin-Capture-Key":pair}
        start=c.post("/api/capture/ext/signal",headers=e,
            json={"ticket":sign(private,"capture.start",nonce="uniquenonce_ai_1"),
                  "source_origin":ISSUER}).json()
        sid=start["session_id"]
        assert c.post("/api/capture/ext/bind",headers=e,json={
            "session_id":sid,"tab_id":7,"document_id":"doc-ai","page_url":URL}).status_code==200
        events=[{"seq":i,"kind":k,"occurred_at":int(time.time()*1000),"details":d} for i,k,d in [
            (1,"click",{"tag":"button","label":"保存"}),
            (2,"submit",{"tag":"form"}),
            (3,"feedback",{"role":"status","label":"保存成功"})]]
        assert c.post("/api/capture/ext/events",headers=e,json={
            "session_id":sid,"tab_id":7,"document_id":"doc-ai","page_url":URL,
            "events":events}).status_code==200
        analyzer=BrowserCaptureAnalyzer(app.state.db,capture=app.state.browser_capture,client=model)
        assert analyzer.process_next()["state"]=="disabled"
        assert model.requests==[]
        c.put("/api/capture/settings",headers=h,json={"enabled":True,"allow_ai":True})
        analyzed=analyzer.process_next()
        assert analyzed["state"]=="analyzed"
        assert len(model.requests)==1
        assert "保存" in json.dumps(model.requests,ensure_ascii=False)
        report=c.get("/api/capture/sessions/"+sid,headers=h).json()["session"]
        assert report["analysis_status"]=="unverified"
        assert "未确认" in report["analysis_text"]
        assert report["task_state"]=="open"
        # A late external model response may not re-enable AI or update notes.
        c.post("/api/capture/ext/events",headers=e,json={
            "session_id":sid,"tab_id":7,"document_id":"doc-ai","page_url":URL,
            "events":[{"seq":4,"kind":"click","details":{"label":"下一步"}}]}).raise_for_status()
        c.post("/api/capture/ext/navigation",headers=e,json={
            "session_id":sid,"tab_id":7,"document_id":"doc-ai",
            "to_url":"https://booking.example.test/next","reason":"internal_navigation"}).raise_for_status()
        model.revoke_during_call=True
        with app.state.db.connect() as con:
            con.execute("UPDATE browser_capture_sessions SET analysis_attempted_at=0 WHERE id=?",(sid,))
        assert analyzer.process_next()["state"]=="revoked"
        assert c.get("/api/capture/status",headers=h).json()["enabled"] is False


def test_extension_is_bundled_for_onboarding(tmp_path):
    from zipfile import ZipFile
    from io import BytesIO
    app=create_app(tmp_path/"db.sqlite",start_worker=False)
    with TestClient(app) as c:
        headers={"X-Worktwin-Token":re.search(
            r'window\.__WORKTWIN_TOKEN__="(.*?)";',c.get("/").text).group(1)}
        assert c.get("/api/capture/extension.zip").status_code==403
        response=c.get("/api/capture/extension.zip",headers=headers)
        assert response.status_code==200
        with ZipFile(BytesIO(response.content)) as bundle:
            for name in ["manifest.json","background.js","content.js","popup.js","popup.html"]:
                assert name in bundle.namelist()
            manifest=json.loads(bundle.read("manifest.json"))
            assert manifest["manifest_version"]==3
            assert manifest["content_scripts"][0]["run_at"]=="document_start"
            assert "https://*/*" in manifest["content_scripts"][0]["matches"]
            assert "nativeMessaging" not in manifest["permissions"]
