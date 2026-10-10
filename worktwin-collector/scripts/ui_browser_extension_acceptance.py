"""Real Chromium MV3 extension acceptance on virtual HTTPS workflow sites.

The browser actually loads the unpacked extension, sends a signed external
message, opens the business link normally, and triggers DOM events. Playwright
route.fulfill simulates the unbuilt flow platform and destination without a
public domain or external traffic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
ORIGIN="https://flow.example.com"
TARGET="https://portal.example.com/approval"
SECRET="worktwin-local-browser-acceptance-secret-"+("z"*20)
BASE="http://127.0.0.1:8765"


def signed(task="browser-e2e"):
    nonce="unique_e2e_nonce_for_start_00001"
    expires=str(int(time.time())+90)
    path="/api/worktwin/tasks/"+task+"/status"
    words=["start",task,TARGET,nonce,expires,ORIGIN,path]
    return {"action":"start","task_id":task,"target_url":TARGET,"nonce":nonce,
            "expires_at":int(expires),"status_path":path,
            "signature":hmac.new(SECRET.encode(),"\n".join(words).encode(),
                                 hashlib.sha256).hexdigest()}


def eventually(fn,timeout=12,interval=.2):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            last=fn()
            if last:
                return last
        except Exception as exc:
            last=exc
        time.sleep(interval)
    raise AssertionError("Timeout waiting for browser capture: "+str(last))


def events(path,session):
    with sqlite3.connect(path) as con:
        return con.execute("""SELECT seq,kind,label FROM browser_capture_events
            WHERE session_id=? ORDER BY seq""",(session,)).fetchall()


def main():
    with tempfile.TemporaryDirectory(prefix="worktwin-browser-real-") as folder:
        work=Path(folder)
        app_data=work/"appdata"
        ext=work/"extension"
        shutil.copytree(ROOT/"worktwin"/"browser_extension",ext)
        manifest_path=ext/"manifest.json"
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        # The production extension requests permissions on first use. Only this
        # synthetic test fixture grants the target site in advance, so tests
        # can focus on real signal/nav/DOM behavior without OS permission UI.
        manifest["host_permissions"].append("https://portal.example.com/*")
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        env={**os.environ,
             "WORKTWIN_DATA_DIR":str(app_data),
             "WORKTWIN_CAPTURE_FLOW_ORIGIN":ORIGIN,
             "WORKTWIN_CAPTURE_FLOW_SECRET":SECRET,
             "WORKTWIN_GATEWAY_URL":"","WORKTWIN_GATEWAY_TOKEN":""}
        server=subprocess.Popen([sys.executable,"-m","worktwin","serve","--port","8765"],
                                cwd=ROOT,env=env,stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=BASE,trust_env=False,timeout=8) as client:
                eventually(lambda:client.get("/api/health").status_code==200,timeout=18)
                homepage=client.get("/").text
                match=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',homepage)
                assert match,homepage[:1000]
                dashboard={"X-Worktwin-Token":match.group(1)}
                enabled=client.put("/api/browser-capture",headers=dashboard,json={"enabled":True})
                assert enabled.status_code==200,enabled.text
                pairing=client.post("/api/browser-capture/pairing",headers=dashboard)
                code=pairing.json()["code"]

                with sync_playwright() as pw:
                    browser=pw.chromium.launch_persistent_context(str(work/"profile"),
                        channel="chromium",headless=True,args=[
                            "--no-sandbox",
                            "--disable-extensions-except="+str(ext),
                            "--load-extension="+str(ext)])
                    try:
                        worker=(browser.service_workers[0] if browser.service_workers
                                else browser.wait_for_event("serviceworker",timeout=15000))
                        extension_id=worker.url.split("/")[2]
                        popup=browser.new_page()
                        popup.goto("chrome-extension://"+extension_id+"/popup.html")
                        popup.locator("#pair-code").fill(code)
                        popup.locator("#pair").click()
                        eventually(lambda:"已连接" in popup.locator("#status").inner_text(),timeout=10)

                        browser.route("https://flow.example.com/**",lambda route:
                          route.fulfill(content_type="text/html",body=(
                            '<html><meta charset="utf-8"><a id="business" target="_blank" '
                            'href="'+TARGET+'">前往审批平台</a>'
                            '<script>document.getElementById("business").addEventListener("click",()=>{'
                            'chrome.runtime.sendMessage("'+extension_id+'",'
                            '{"type":"worktwin:task","envelope":'+json.dumps(signed(),ensure_ascii=False)+'},'
                            'r=>{document.body.dataset.trigger=r?.status||r?.error||"no-response"});'
                            '});</script></html>')))
                        browser.route("https://portal.example.com/**",lambda route:
                          route.fulfill(content_type="text/html",body=(
                            '<html><meta charset="utf-8"><title>审批规则</title>'
                            '<button id="add">新增审批规则</button>'
                            '<label for="type">审批方式</label>'
                            '<select id="type"><option>单人审批</option><option>多人审批</option></select>'
                            '<button id="save">保存</button><div id="feedback" role="alert"></div>'
                            '<a id="next" href="https://portal.example.com/next">下一页</a>'
                            '<script>document.getElementById("save").onclick=()=>{'
                            'document.getElementById("feedback").textContent="保存成功";};'
                            '</script></html>')))
                        flow=browser.new_page()
                        flow.goto(ORIGIN+"/tasks",wait_until="domcontentloaded")
                        with browser.expect_page(timeout=12000) as opened:
                            flow.locator("#business").click()
                        target=opened.value
                        target.wait_for_load_state("domcontentloaded")
                        db=app_data/"worktwin.sqlite"
                        def capturing():
                            with sqlite3.connect(db) as con:
                                row=con.execute("""SELECT id FROM browser_capture_sessions
                                    WHERE task_id='browser-e2e' AND status='capturing'
                                    ORDER BY created_at DESC LIMIT 1""").fetchone()
                                return row[0] if row else ""
                        session=eventually(capturing,timeout=14)
                        target.locator("#add").click()
                        target.locator("#type").select_option(label="多人审批")
                        target.locator("#save").click()
                        def enough_events():
                            return len(events(db,session))>=4
                        eventually(enough_events,timeout=12)
                        rows=events(db,session)
                        assert any(kind=="click" and "新增审批规则" in label for _,kind,label in rows),rows
                        assert any(kind=="change" and "审批方式" in label for _,kind,label in rows),rows
                        assert any(kind=="feedback" and label=="feedback_success" for _,kind,label in rows),rows
                        # The same URL opened outside this flow must not be observed.
                        unrelated=browser.new_page()
                        unrelated.goto(TARGET)
                        unrelated.locator("#add").click()
                        time.sleep(.8)
                        assert len(events(db,session))==len(rows),"unrelated tab was captured"
                        # The first actual navigation ends observation, even
                        # when the new page stays on the same HTTPS origin.
                        target.locator("#next").click()
                        def stopped():
                            with sqlite3.connect(db) as con:
                                row=con.execute("""SELECT status FROM browser_capture_sessions
                                    WHERE id=?""",(session,)).fetchone()
                                return row and row[0]=="navigation_stopped"
                        eventually(stopped,timeout=12)
                        count=len(events(db,session))
                        target.locator("#add").click()
                        time.sleep(.5)
                        assert len(events(db,session))==count,"followed navigation was captured"
                        report=client.post("/api/browser-capture/sessions/"+session+"/summarize",
                                           headers=dashboard)
                        assert report.status_code==200,report.text
                        data=report.json()
                        assert "新增审批规则" in data["summary"] and data["summary_status"]=="final",data
                        assert data["summary_source"]=="rule"
                        assert all(isinstance(n,int) for n in data["evidence_seq"]),data
                        print(json.dumps({"result":"PASS","extension":"MV3 Chromium",
                            "events":count,"first_page_only":True,"no_unrelated_tabs":True,
                            "summary":data["summary"]},ensure_ascii=False))
                    finally:
                        browser.close()
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()


if __name__=="__main__":
    main()
