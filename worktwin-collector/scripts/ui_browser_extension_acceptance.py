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
import secrets
import shutil
import socket
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


def signed(task="browser-e2e",action="start"):
    nonce="unique_e2e_nonce_"+task+"_"+action
    expires=str(int(time.time())+90)
    path="/api/worktwin/tasks/"+task+"/status"
    words=[action,task,TARGET,nonce,expires,ORIGIN,path]
    return {"action":action,"task_id":task,"target_url":TARGET,"nonce":nonce,
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
        return con.execute("""SELECT seq,kind,label FROM browser_capture_session_events
            WHERE session_id=? ORDER BY seq""",(session,)).fetchall()


def main():
    with tempfile.TemporaryDirectory(prefix="worktwin-browser-real-") as folder:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        work=Path(folder)
        app_data=work/"appdata"
        ext=work/"extension"
        shutil.copytree(ROOT/"worktwin"/"browser_extension",ext)
        worker_file=ext/'service-worker.js'
        worker_file.write_text(worker_file.read_text().replace(
            'http://127.0.0.1:8765',base))
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
        identity=secrets.token_urlsafe(24)
        env['WORKTWIN_ACCEPTANCE_ID']=identity
        env['WORKTWIN_ACCEPTANCE_PORT']=str(port)
        # Only this subprocess exposes the nonce and the isolated DB identity.
        # The installed app has neither this route nor this temporary nonce.
        server_code='''
import os
from pathlib import Path
import uvicorn
from worktwin.api import create_app
from worktwin import __version__
db_path=Path(os.environ['WORKTWIN_DATA_DIR'])/'worktwin.sqlite'
app=create_app(db_path,start_worker=False)
@app.get('/acceptance/identity')
def identity():
    return {'id':os.environ['WORKTWIN_ACCEPTANCE_ID'], 'database':str(db_path), 'version':__version__}
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['WORKTWIN_ACCEPTANCE_PORT']))
'''
        server=subprocess.Popen([sys.executable,'-c',server_code],
                                cwd=ROOT,env=env,stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=base,trust_env=False,timeout=8) as client:
                eventually(lambda:server.poll() is None and client.get("/api/health").status_code==200,timeout=18)
                assert server.poll() is None, 'Isolated acceptance server did not start'
                from worktwin import __version__
                assert client.get('/acceptance/identity').json()=={
                    'id':identity,'database':str(app_data/'worktwin.sqlite'),'version':__version__}
                assert client.get('/api/health').json()['version']==__version__
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
                        state=client.get("/api/browser-capture",headers=dashboard).json()
                        assert state["extension_version"]==manifest["version"] and not state["extension_update_required"]

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
                        # Switching the workflow owner of an existing document
                        # must not let the old worker session consume the click.
                        target.goto(TARGET,wait_until='domcontentloaded')
                        def launch(task_name,action='start'):
                            return flow.evaluate('''args => new Promise(resolve => {
                                chrome.runtime.sendMessage(args.id,
                                  {type:'worktwin:task',envelope:args.envelope},resolve);
                            })''',{'id':extension_id,'envelope':signed(task_name,action)})
                        first=launch('switch-first')
                        assert first['ok'],first
                        first_sid=first['session_id']
                        second=launch('switch-second')
                        assert second['ok'],second
                        second_sid=second['session_id']
                        target.locator('#add').click()
                        eventually(lambda:any('新增审批规则' in label for _,_,label in events(db,second_sid)))
                        assert not events(db,first_sid),'old workflow consumed new workflow click'
                        launch('switch-second','complete')
                        # The whitelist has persistent intent, covering all matching
                        # tabs. Signing a workflow still only owns its bound tab.
                        added=client.post('/api/browser-capture/sites',headers=dashboard,
                            json={'url':'https://portal.example.com'}).json()
                        assert not added['manual_enabled']
                        site_id=added['id']
                        client.put(f'/api/browser-capture/sites/{site_id}',headers=dashboard,
                            json={'enabled':True}).raise_for_status()
                        worker.evaluate('heartbeat()')
                        worker.evaluate("""async url => {
                            const matches=(await chrome.tabs.query({})).filter(t=>t.url===url);
                            const tab=matches.find(t=>t.openerTabId!==undefined);
                            globalThis.acceptanceTabId=tab.id;
                        }""",target.url)
                        def current_manual():
                            return worker.evaluate("""() => [...sessions.values()].find(s=>
                                s.persistent && s.status==='capturing' &&
                                s.tabId===globalThis.acceptanceTabId)?.id""")
                        manual_sid=eventually(current_manual,timeout=18)
                        def manual_bound():
                            return current_manual()==manual_sid
                        target.locator('#add').click()
                        eventually(lambda:len(events(db,manual_sid))>=1,timeout=12)
                        # A normal dialog, dynamic same-site iframe, srcdoc,
                        # and open shadow-root control must all be observable.
                        browser.route('https://portal.example.com/dialog-form',lambda route:
                            route.fulfill(content_type='text/html',body=
                              '<meta charset="utf-8"><label for="field">弹窗字段</label>'
                              '<input id="field"><button id="inner-save">保存弹窗</button>'
                              '<input type="password" id="secret">'))
                        browser.route('https://outside.example.com/widget',lambda route:
                            route.fulfill(content_type='text/html',body=
                              '<button id="outside">FOREIGN_FRAME_ACTION</button>'))
                        target.evaluate("""() => {
                          const dialog=document.createElement('dialog');dialog.id='test-dialog';
                          dialog.innerHTML='<button id="dialog-action">普通弹窗操作</button><div id="shadow-host"></div><iframe id="inner-frame" src="/dialog-form"></iframe><iframe id="inline-frame" srcdoc="<button id=inline-action>内嵌弹窗操作</button>"></iframe><iframe id="foreign-frame" src="https://outside.example.com/widget"></iframe>';
                          document.body.append(dialog);dialog.showModal();
                          dialog.querySelector('#shadow-host').attachShadow({mode:'open'}).innerHTML='<button id="shadow-action">影子弹窗操作</button>';
                        }""")
                        target.locator('#dialog-action').click()
                        target.locator('#shadow-action').click()
                        inner=target.frame_locator('#inner-frame')
                        inner.locator('#inner-save').wait_for()
                        # Wait for actual content script readiness, not merely the frame load.
                        eventually(lambda:worker.evaluate("""async () => {
                            const s=[...sessions.values()].find(s=>s.persistent&&s.status==='capturing'&&s.tabId===globalThis.acceptanceTabId);
                            const frames=await chrome.webNavigation.getAllFrames({tabId:s.tabId});
                            const f=frames.find(f=>f.url.endsWith('/dialog-form'));
                            if(!f)return false;
                            const r=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[f.documentId]},func:()=>globalThis.__worktwinCaptureActive===true});
                            return r[0]?.result;
                        }"""))
                        inner.locator('#field').fill('PRIVATE_INPUT_VALUE')
                        inner.locator('#inner-save').click()
                        inline=target.frame_locator('#inline-frame')
                        eventually(lambda:worker.evaluate("""async () => {
                            const s=[...sessions.values()].find(s=>s.persistent&&s.status==='capturing'&&s.tabId===globalThis.acceptanceTabId);
                            const frames=await chrome.webNavigation.getAllFrames({tabId:s.tabId});
                            const f=frames.find(f=>f.url==='about:srcdoc');
                            if(!f)return false;
                            const r=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[f.documentId]},func:()=>globalThis.__worktwinCaptureActive===true});
                            return r[0]?.result;
                        }"""))
                        inline.locator('#inline-action').click()
                        eventually(lambda:all(any(label in captured for _,_,captured in events(db,manual_sid))
                            for label in ['普通弹窗操作','影子弹窗操作','保存弹窗','弹窗字段','内嵌弹窗操作']))
                        before_foreign=len(events(db,manual_sid))
                        target.frame_locator('#foreign-frame').locator('#outside').click()
                        inner.locator('#secret').fill('PRIVATE_PASSWORD')
                        time.sleep(.5)
                        assert len(events(db,manual_sid))==before_foreign,'foreign/sensitive frame input captured'
                        assert 'PRIVATE_INPUT_VALUE' not in str(events(db,manual_sid))
                        target.evaluate("document.querySelector('#test-dialog').remove()")
                        assert manual_bound(),'embedded frame navigation stopped the parent session'
                        first_interval=manual_sid
                        target.locator('#next').click()
                        manual_sid=eventually(lambda:(sid if (sid:=current_manual())!=first_interval else None),timeout=12)
                        target.locator('#save').click()
                        eventually(lambda:any('保存' in label for _,kind,label in
                                   events(db,manual_sid) if kind=='click'),timeout=12)
                        assert client.get('/api/browser-capture/sites',headers=dashboard).json()[0]['manual_enabled']
                        # Total pause keeps the pairing and site intent. Resume
                        # attaches to the same document without recording its gap.
                        client.put('/api/browser-capture',headers=dashboard,json={'enabled':False})
                        worker.evaluate('heartbeat()')
                        paused_count=len(events(db,manual_sid))
                        target.locator('#add').click()
                        time.sleep(.3)
                        assert len(events(db,manual_sid))==paused_count
                        assert client.get('/api/browser-capture',headers=dashboard).json()['extension_paired']
                        client.put('/api/browser-capture',headers=dashboard,json={'enabled':True})
                        worker.evaluate('heartbeat()')
                        prior=manual_sid
                        manual_sid=eventually(lambda:(sid if (sid:=current_manual())!=prior else None))
                        target.locator('#add').click()
                        eventually(lambda:any('新增审批规则' in label for _,_,label in events(db,manual_sid)))
                        # Removing one site's current requests does not erase
                        # its completed intervals or require re-pairing later.
                        client.put(f'/api/browser-capture/sites/{site_id}',headers=dashboard,json={'enabled':False})
                        worker.evaluate('heartbeat()')
                        count_before=len(events(db,manual_sid))
                        target.locator('#add').click()
                        time.sleep(.3)
                        assert len(events(db,manual_sid))==count_before
                        assert len(events(db,session))==count,'personal events entered the finished workflow'
                        report=client.post('/api/browser-capture/sessions/'+first_interval+'/summarize',headers=dashboard)
                        assert report.status_code==200,report.text
                        def site_guide():
                            notes=client.get('/api/knowledge',headers=dashboard,
                                params={'status':'all','limit':200}).json()
                            return next((n for n in notes if n['kind']=='process' and
                                n['project_key'].startswith('browser-site:')),None)
                        guide=eventually(site_guide,timeout=15)
                        assert '新增审批规则' in guide['body'],guide
                        print(json.dumps({'result':'PASS','extension':'MV3 Chromium',
                            'legacy_task_first_page_only':True,'continuous_site_multi_tab':True,
                            'same_origin_iframe_and_shadow':True,'cross_origin_iframe_excluded':True,
                            'pairing_survives_pause':True,'site_history_preserved':True},ensure_ascii=False))
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
