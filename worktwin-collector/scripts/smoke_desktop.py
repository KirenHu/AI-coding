"""Black-box smoke test for frozen macOS/Windows WorkTwin applications.

Builds are only considered usable after the *packaged executable*, not the
Python source tree, starts the loopback web service and serves the dashboard.
No employee data or enterprise credentials are involved.
"""

from __future__ import annotations

import json
import io
import zipfile
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def verify_mcp(opener, owner_headers, note_id, base_url='http://127.0.0.1:8765'):
    """Exercise the shipped SDK and its schemas inside the frozen app."""
    def call(path, body, headers=owner_headers, method='POST'):
        request=Request(base_url+path,method=method,headers=headers,
                        data=json.dumps(body).encode() if body is not None else None)
        with opener.open(request,timeout=10) as response:
            return json.load(response)
    twin=call('/api/twins',{'name':'Native MCP smoke'})['id']
    call(f'/api/twins/{twin}/knowledge',{'knowledge_ids':[note_id]},method='PUT')
    connection=call(f'/api/twins/{twin}/mcp',{})
    headers={'Authorization':'Bearer '+connection['token'],
             'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
    initialized=call('/mcp/',{'jsonrpc':'2.0','id':1,'method':'initialize',
        'params':{'protocolVersion':'2025-11-25','capabilities':{},
                  'clientInfo':{'name':'native-package-check','version':'1'}}},headers)
    assert initialized['result']['protocolVersion']=='2025-11-25',initialized
    headers['MCP-Protocol-Version']='2025-11-25'
    listed=call('/mcp/',{'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},headers)
    tools=listed['result']['tools']
    assert {t['name'] for t in tools}=={'list_projects','search_knowledge','read_knowledge','read_source_log'},listed
    assert all(t['annotations']['readOnlyHint'] for t in tools),listed
    result=call('/mcp/',{'jsonrpc':'2.0','id':3,'method':'tools/call',
                        'params':{'name':'read_knowledge','arguments':{'knowledge_id':note_id}}},headers)['result']
    assert not result.get('isError') and 'Packaged Markdown' in json.dumps(result),result
    permissions=call(f'/api/twins/{twin}/mcp',None,method='GET')
    assert permissions['allow_logs'] is False,permissions
    call(f'/api/twins/{twin}/mcp',{},method='DELETE')
    try:
        call('/mcp/',{'jsonrpc':'2.0','id':4,'method':'tools/list','params':{}},headers)
        raise AssertionError('Revoked native MCP credential remained usable')
    except HTTPError as exc:
        assert exc.code==401,exc.code
    print('PASS: packaged MCP initialization, read-only tools, authorized note reading and revocation')


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/smoke_desktop.py <executable>", file=sys.stderr)
        return 2
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        print(f"Executable does not exist: {executable}", file=sys.stderr)
        return 2

    opener = build_opener(ProxyHandler({}))  # Never route localhost via a CI proxy.
    with tempfile.TemporaryDirectory(prefix="worktwin-native-smoke-") as temp:
        env = os.environ.copy()
        env["WORKTWIN_DATA_DIR"] = temp
        # A smoke check must not send local data to any model service.
        env.pop("WORKTWIN_GATEWAY_URL", None)
        env.pop("WORKTWIN_GATEWAY_TOKEN", None)
        env.pop("WORKTWIN_SERVER_URL", None)
        env.pop("WORKTWIN_SERVER_TOKEN", None)
        native_log = Path(temp) / "native-stdout.log"
        with native_log.open("wb") as capture:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                env=env,
                stdout=capture,
                stderr=subprocess.STDOUT,
            )
        last_error: Exception | None = None
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Native executable exited with status {process.returncode}")
                try:
                    with opener.open("http://127.0.0.1:8765/api/health", timeout=2) as response:
                        health = json.load(response)
                    if health.get("ok") is not True or health.get("enterprise_model") is not False:
                        raise AssertionError(f"Unexpected health response: {health}")
                    with opener.open("http://127.0.0.1:8765/", timeout=2) as response:
                        page = response.read().decode("utf-8")
                    if "WorkTwin" not in page:
                        raise AssertionError("Dashboard was not served by packaged app")
                    ui_version = re.search(r'window\.__WORKTWIN_VERSION__="(.*?)";', page).group(1)
                    assert ui_version == health['version'], (ui_version, health)
                    if not (Path(temp) / "worktwin.sqlite").is_file():
                        raise AssertionError("Packaged app did not initialize local database")
                    token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', page).group(1)
                    headers = {'X-Worktwin-Token': token, 'Content-Type': 'application/json'}
                    with opener.open(Request('http://127.0.0.1:8765/api/settings', headers=headers), timeout=5) as response:
                        settings = json.load(response)
                    assert settings['edition'] == 'personal', settings
                    # The extension must be delivered inside the real packaged
                    # application, not only available in the source checkout.
                    with opener.open(Request('http://127.0.0.1:8765/api/capture/status',
                                             headers=headers),timeout=5) as response:
                        capture_settings=json.load(response)
                    assert capture_settings['enabled'] is False, capture_settings
                    with opener.open(Request('http://127.0.0.1:8765/api/capture/extension.zip',
                                             headers=headers),timeout=5) as response:
                        bundled=response.read()
                    with zipfile.ZipFile(io.BytesIO(bundled)) as zipped:
                        assert all(name in zipped.namelist() for name in
                                   ('manifest.json','background.js','content.js','popup.js','popup.html'))
                    print('PASS: native app includes the disabled-by-default browser extension ZIP')
                    assert not settings['storage_error'] and not settings['model_error'], settings
                    if sys.platform in ('darwin', 'win32'):
                        expected_storage = '系统钥匙串' if sys.platform == 'darwin' else 'Windows 凭据管理器'
                        assert settings['secret_storage'] == expected_storage, settings
                    request = Request('http://127.0.0.1:8765/api/edition', method='PUT', headers=headers,
                        data=b'{"edition":"personal"}')
                    with opener.open(request, timeout=5) as response:
                        assert json.load(response)['edition'] == 'personal'
                    # Reach the real provider from the *frozen app*, without a
                    # valid key or a billable generation. This catches missing
                    # CA certificates that localhost startup cannot exercise.
                    request = Request('http://127.0.0.1:8765/api/model/personal/models', method='POST', headers=headers,
                        data=json.dumps({'base_url':'https://api.deepseek.com', 'api_key':'worktwin-native-smoke-invalid-key'}).encode())
                    try:
                        opener.open(request, timeout=40)
                        raise AssertionError('Invalid smoke credential unexpectedly accepted')
                    except HTTPError as exc:
                        detail=json.load(exc)['detail']
                        assert exc.code==400 and ('HTTP 401' in detail or 'HTTP 403' in detail), detail
                    print('PASS: frozen app verified real DeepSeek HTTPS and received authentication rejection; no real key or generation used')
                    request = Request('http://127.0.0.1:8765/api/knowledge', method='POST', headers=headers,
                        data=json.dumps({'title':'Native smoke', 'body':'**Packaged Markdown**', 'status':'confirmed'}).encode())
                    with opener.open(request, timeout=5) as response:
                        note_id=json.load(response)['id']
                        assert note_id > 0
                    with opener.open(Request('http://127.0.0.1:8765/api/knowledge', headers=headers), timeout=5) as response:
                        assert '<strong>Packaged Markdown</strong>' in json.load(response)[0]['rendered_body']
                    verify_mcp(opener,headers,note_id)
                    with opener.open(Request('http://127.0.0.1:8765/api/shutdown', method='POST', headers=headers, data=b'{}'), timeout=5) as response:
                        assert json.load(response)['stopping'] is True
                    process.wait(timeout=15)
                    assert process.returncode == 0, process.returncode
                    print("PASS: packaged startup, settings and credential backend load, provider HTTPS, SQLite write, Markdown rendering, and graceful shutdown")
                    return 0
                except (ConnectionError, HTTPError, URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    time.sleep(1)
            raise RuntimeError(f"Packaged executable did not serve the dashboard: {last_error}")
        except (RuntimeError, AssertionError) as exc:
            for logfile in (Path(temp) / "worktwin-launch.log", native_log):
                if logfile.exists() and logfile.stat().st_size:
                    print(f"Recent {logfile.name} diagnostics:\\n"
                          + logfile.read_text(encoding="utf-8", errors="replace")[-5000:],
                          file=sys.stderr)
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
