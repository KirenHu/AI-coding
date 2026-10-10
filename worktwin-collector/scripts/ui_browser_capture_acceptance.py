"""Actual Chromium MV3 extension + HTTPS workflow + WorkTwin loopback acceptance.

No real private customer data or paid model. Creates a temporary signing key,
self-signed TLS fixture and local pages, then exercises an ordinary hyperlink.
Run from worktwin-collector after installing Playwright Chromium.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from worktwin.api import create_app


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def post(path, body, token=None):
    headers={"Content-Type":"application/json"}
    if token:
        headers["X-Worktwin-Token"]=token
    req=urllib.request.Request("http://127.0.0.1:8765"+path,
        data=json.dumps(body).encode(),headers=headers,method="POST" if path.endswith("/pair") else "PUT")
    with urllib.request.urlopen(req,timeout=8) as response:
        return json.load(response)


def get(path, token=None):
    req=urllib.request.Request("http://127.0.0.1:8765"+path,
        headers={"X-Worktwin-Token":token} if token else {})
    with urllib.request.urlopen(req,timeout=8) as response:
        content=response.read().decode()
        return json.loads(content) if path.startswith("/api/") else content


def write_certificate(folder: Path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"WorkTwin local fixture")])
    now=dt.datetime.now(dt.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-dt.timedelta(days=1))
        .not_valid_after(now+dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("flow.example.test"),x509.DNSName("target.example.test")]),False)
        .sign(key,hashes.SHA256()))
    certificate=folder/"server.crt";private=folder/"server.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return certificate,private


def wait(predicate, message, seconds=18):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            result=predicate()
            if result: return result
        except Exception: pass
        time.sleep(.15)
    raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="wt-real-chrome-") as tmp:
        folder=Path(tmp)
        private=ed25519.Ed25519PrivateKey.generate()
        raw_public=private.public_key().public_bytes(
            serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        issuer_ref={"value":""}
        target_ref={"value":""}

        def sign(action, nonce):
            now=int(time.time())
            payload={"iss":issuer_ref["value"],"action":action,"iat":now,
                "exp":now+115,"nonce":nonce,"task_id":"task_real_browser_1",
                "request_id":"user_click_real_1","subject":"employee_1",
                "goal":"点击保存并查看网页反馈","project_id":"sample_workflow",
                "target_url":target_ref["value"]}
            data=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()
            return b64(data)+"."+b64(private.sign(data))

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                host=self.headers.get("Host","")
                if host.startswith("flow."):
                    same_tab=self.path.startswith("/same-tab")
                    start=sign("capture.start","realsignal_start_same123" if same_tab else "realsignal_start_123456")
                    end=sign("capture.complete","realsignal_done_same123" if same_tab else "realsignal_complete_123456")
                    attributes="" if same_tab else 'target="_blank" rel="noopener"'
                    content=("""<!doctype html><html><meta charset="utf-8"><body>
<a id="business-link" """+attributes+""" href='""" + target_ref["value"] + """'>前往第三方系统</a>
<script>
document.getElementById("business-link").addEventListener("click",()=>{
window.postMessage({channel:"worktwin.workflow",type:"capture.start",
ticket:"""+json.dumps(start)+"""},window.location.origin);
});
window.completeWorkTwinTask=()=>window.postMessage({
channel:"worktwin.workflow",type:"capture.complete",
ticket:"""+json.dumps(end)+"""},window.location.origin);
</script></body></html>""")
                elif self.path.startswith("/first"):
                    content="""<!doctype html><html><meta charset="utf-8"><body>
<h1>第三方后台</h1><button id="save">保存设置</button>
<div id="result" role="status"></div><a id="next" href="/second">下一页</a>
<script>document.getElementById("save").onclick=()=>{
document.getElementById("result").textContent="保存成功";
};</script></body></html>"""
                else:
                    content="""<!doctype html><html><meta charset="utf-8"><body>
<h1>下一页面，禁止继续采集</h1>
<button id="other">此点击不应记录</button></body></html>"""
                data=content.encode()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cert,key=write_certificate(folder)
        https=ThreadingHTTPServer(("127.0.0.1",0),Fixture)
        port=https.server_address[1]
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(str(cert),str(key))
        https.socket=ssl_context.wrap_socket(https.socket,server_side=True)
        issuer_ref["value"]="https://flow.example.test:"+str(port)
        target_ref["value"]="https://target.example.test:"+str(port)+"/first?access_token=do-not-archive"
        os.environ["WORKTWIN_CAPTURE_TRUSTED_PLATFORMS"]=json.dumps({
            issuer_ref["value"]:b64(raw_public)})
        tls_thread=threading.Thread(target=https.serve_forever,daemon=True)
        tls_thread.start()
        app=create_app(folder/"database.sqlite",start_worker=False)
        server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=8765,
            log_level="error",access_log=False))
        thread=threading.Thread(target=server.run,daemon=True)
        thread.start()
        try:
            wait(lambda:server.started,"WorkTwin localhost failed to start")
            dashboard=get("/")
            token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',dashboard).group(1)
            # The enablement and pairing happen explicitly in WorkTwin, not
            # because the extension is installed or a webpage was opened.
            result=post("/api/capture/settings",{"enabled":True,"allow_ai":False},token)
            assert result["enabled"]
            pair=post("/api/capture/pair",{},token)["pairing_token"]
            extension_dir=Path(__file__).resolve().parents[1]/"worktwin"/"static"/"extension"
            assert (extension_dir/"manifest.json").exists()
            with sync_playwright() as p:
                ctx=p.chromium.launch_persistent_context(str(folder/"browser"),
                    channel="chromium",headless=True,
                    ignore_https_errors=True,
                    args=[
                        "--disable-extensions-except="+str(extension_dir),
                        "--load-extension="+str(extension_dir),
                        "--host-resolver-rules=MAP flow.example.test 127.0.0.1,MAP target.example.test 127.0.0.1",
                        "--ignore-certificate-errors","--no-proxy-server",
                    ])
                try:
                    sw=ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event(
                        "serviceworker",timeout=20000)
                    extension_id=sw.url.split("/")[2]
                    popup=ctx.new_page()
                    popup.goto("chrome-extension://"+extension_id+"/popup.html")
                    popup.locator("#pair").fill(pair)
                    popup.locator("#connect").click()
                    wait(lambda:get("/api/capture/status",token)["extension_connected"],
                        "Plugin did not pair/heartbeat")
                    popup.close()
                    flow=ctx.new_page()
                    flow.goto(issuer_ref["value"]+"/task",wait_until="domcontentloaded")
                    with ctx.expect_page(timeout=20000) as opened:
                        flow.locator("#business-link").click()
                    target=opened.value
                    target.wait_for_load_state("domcontentloaded")
                    session=wait(lambda:next((
                        s for s in get("/api/capture/status",token)["sessions"]
                        if s["capture_state"]=="recording"),None),
                        "Original link did not attach its first target document",24)
                    # The extension attaches asynchronously without redirecting
                    # the user's normal native target.
                    time.sleep(.7)
                    target.locator("#save").click()
                    captured=wait(lambda:get(
                        "/api/capture/sessions/"+session["id"],token)["steps"],
                        "DOM click was not captured")
                    assert any("保存" in step["summary"] for step in captured),captured
                    target.locator("#next").click()
                    target.wait_for_url("**/second")
                    after=wait(lambda:get("/api/capture/status",token)["sessions"][0]
                               if get("/api/capture/status",token)["sessions"][0]["capture_state"]=="navigated"
                               else None,"Internal navigation did not stop capture")
                    assert after["task_state"]=="open"
                    count=len(get("/api/capture/sessions/"+session["id"],token)["steps"])
                    target.locator("#other").click()
                    time.sleep(.7)
                    same=get("/api/capture/sessions/"+session["id"],token)
                    assert len(same["steps"])==count
                    assert any("新页面不在本次采集范围" in step["summary"]
                               for step in same["steps"])
                    assert "do-not-archive" not in json.dumps(same,ensure_ascii=False)
                    flow.evaluate("window.completeWorkTwinTask()")
                    wait(lambda:get("/api/capture/status",token)["sessions"][0]["task_state"]=="completed",
                        "Signed workflow completion did not close the task")
                    # Verify the same-tab native href separately. The opener
                    # source page is destroyed, but the local signal must
                    # still match the newly committed document in that tab.
                    before_ids={row["id"] for row in get("/api/capture/status",token)["sessions"]}
                    same=ctx.new_page()
                    same.goto(issuer_ref["value"]+"/same-tab",wait_until="domcontentloaded")
                    same.locator("#business-link").click()
                    same.wait_for_url("**/first?*")
                    second=wait(lambda:next((
                        x for x in get("/api/capture/status",token)["sessions"]
                        if x["id"] not in before_ids and x["capture_state"]=="recording"),None),
                        "Same-tab native navigation did not bind",24)
                    time.sleep(.7)
                    same.locator("#save").click()
                    wait(lambda:len(get("/api/capture/sessions/"+second["id"],token)["steps"])>=1,
                         "Same-tab DOM action was not recorded")
                    same.locator("#next").click()
                    same.wait_for_url("**/second")
                    wait(lambda:next((x for x in get("/api/capture/status",token)["sessions"]
                        if x["id"]==second["id"] and x["capture_state"]=="navigated"),None),
                        "Same-tab internal navigation was not stopped")
                    completion_page=ctx.new_page()
                    completion_page.goto(issuer_ref["value"]+"/same-tab")
                    completion_page.evaluate("window.completeWorkTwinTask()")
                    wait(lambda:next((x for x in get("/api/capture/status",token)["sessions"]
                        if x["id"]==second["id"] and x["task_state"]=="completed"),None),
                        "Same-tab workflow completion signal was lost")
                    print(json.dumps({"result":"PASS","real_extension":True,
                        "native_new_tab":True,"native_same_tab":True,
                        "page_boundary":True,"navigation_stops":True,
                        "workflow_completion":True,"steps":count},ensure_ascii=False))
                finally:
                    ctx.close()
        finally:
            server.should_exit=True
            thread.join(timeout=6)
            https.shutdown()
            https.server_close()
            tls_thread.join(timeout=3)


if __name__=="__main__":
    main()
