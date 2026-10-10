"""Chromium-driven UI smoke test against actual localhost FastAPI + SQLite.

Requires Playwright Python and local Chromium (`CHROMIUM_PATH` if nonstandard).
Some secured CI sandboxes forbid browser HTTP navigation, so requests are
transported through a minimal Playwright -> localhost httpx bridge. Browser
executes actual UI HTML/CSS/JavaScript and clicks the real controls.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).parents[1]


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-ui-v04-') as tmp:
        state = Path(tmp)
        work = state / 'work' / '审批平台'
        work.mkdir(parents=True)
        (work / '设计决策.md').write_text(
            '# 设计决策\n\n我们最终决定将审批流程做成工作流节点，避免重复维护两套系统。',
            encoding='utf-8')
        port = available_port()
        url = f'http://127.0.0.1:{port}'
        server = subprocess.Popen([sys.executable, '-m', 'worktwin', 'serve', '--port', str(port)],
                                  cwd=ROOT, env={**os.environ, 'WORKTWIN_DATA_DIR': str(state/'db'),
                                                 'WORKTWIN_GATEWAY_URL': '', 'WORKTWIN_GATEWAY_TOKEN': ''},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    if httpx.get(url + '/api/health', timeout=.7, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Local API failed to start')
            client = httpx.Client(base_url=url, timeout=25, trust_env=False)
            try:
                html = client.get('/').text
                html = re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />', '', html)
                html = re.sub(r'<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>', '', html)

                def backend(path, options):
                    if not str(path).startswith('/api/'):
                        raise RuntimeError('Only local WorkTwin API may be bridged')
                    resp = client.request(options.get('method', 'GET'), str(path),
                                          headers=options.get('headers') or {},
                                          content=options.get('body'))
                    return {'status': resp.status_code, 'body': resp.text}

                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True,
                                                executable_path=os.environ.get('CHROMIUM_PATH') or None,
                                                args=['--no-sandbox'])
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 920}, device_scale_factor=1)
                        page.set_default_timeout(12000)
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.expose_function('__worktwinBridge', backend)
                        page.set_content(html, wait_until='domcontentloaded')
                        page.evaluate('''() => { window.fetch = async (path, opts={}) => {
                            const res = await window.__worktwinBridge(path, opts);
                            return new Response(res.body, {status: res.status,
                                headers: {'Content-Type': 'application/json'}});
                        }}''')
                        page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text(encoding='utf-8'))
                        page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text(encoding='utf-8'))
                        expect(page.get_by_role('heading',name='我的知识库')).to_be_visible()
                        page.locator('[data-page=sources]').click()
                        expect(page.locator('[data-source-category]')).to_have_count(4)
                        page.evaluate("window.__navNode=document.querySelector('.main-nav');window.__sourceNode=document.querySelector('#browser-manual-site')")
                        page.locator('#browser-manual-site').fill('https://keep-my-input.example')
                        page.locator('h1').click()
                        page.clock.install()
                        page.clock.fast_forward(16000)
                        assert page.evaluate('window.__sourceNode.isConnected'), 'timer replaced content'
                        expect(page.locator('#browser-manual-site')).to_have_value('https://keep-my-input.example')
                        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)
                        headers={'X-Worktwin-Token':token}
                        added=client.post('/api/sources',headers=headers,json={
                            'kind':'folder','root':str(work),'name':'刷新后出现'})
                        assert added.status_code==200,added.text
                        expect(page.locator('[data-source-category="folder"] .source-item')).to_have_count(0)
                        page.locator('#sources-refresh').click()
                        expect(page.locator('[data-source-category="folder"] .source-item')).to_have_count(1)
                        assert page.evaluate('window.__navNode===document.querySelector(".main-nav")')

                        # Assistant sources detect their transcript location; ordinary
                        # document folders cannot be enabled as a Codex source.
                        page.locator('[data-add-source="codex"]').click()
                        expect(page.locator('#source-detection')).not_to_contain_text('正在检测')
                        page.locator('summary',has_text='高级设置').click()
                        page.locator('#new-source-path').fill(str(work))
                        page.locator('#detect-source').click()
                        expect(page.locator('#source-detection')).to_contain_text('尚未检测到')
                        expect(page.locator('#save-source')).to_be_disabled()
                        transcript=state/'transcripts';transcript.mkdir()
                        (transcript/'sample.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':'sample','cwd':'/work/project'}})+'\n')
                        page.locator('#new-source-path').fill(str(transcript))
                        page.locator('#detect-source').click()
                        expect(page.locator('#source-detection')).to_contain_text('已检测到 Codex')
                        expect(page.locator('#save-source')).to_be_enabled()
                        page.locator('#save-source').click()
                        expect(page.locator('[data-source-category="codex"] .source-item')).to_have_count(1)

                        original={'title':'会变化的知识','body':'最初正文','kind':'process','status':'confirmed'}
                        kid=client.post('/api/knowledge',headers=headers,json=original).json()['id']
                        page.locator('[data-page=knowledge]').click()
                        expect(page.locator(f'[data-entry="{kid}"]')).to_be_visible()
                        client.put(f'/api/knowledge/{kid}',headers=headers,json=dict(original,body='后台新正文'))
                        page.locator(f'[data-entry="{kid}"]').click()
                        expect(page.locator('#refresh-notice')).to_contain_text('已更新')
                        expect(page.locator('#drawer-mask')).to_have_count(0)
                        page.locator('#refresh-content').click()
                        expect(page.locator(f'[data-entry="{kid}"]')).to_contain_text('后台新正文')
                        page.locator(f'[data-entry="{kid}"]').click()
                        expect(page.locator('.drawer-body')).to_contain_text('后台新正文')
                        page.locator('#edit-entry').click()
                        page.locator('#edit-k-body').fill('我还没有保存的修改')
                        client.put(f'/api/knowledge/{kid}',headers=headers,json=dict(original,body='再次更新正文'))
                        page.locator('#save-entry').click()
                        expect(page.locator('#refresh-notice')).to_be_visible()
                        expect(page.locator('#edit-k-body')).to_have_value('我还没有保存的修改')
                        assert client.get(f'/api/knowledge-item/{kid}',headers=headers).json()['body']=='再次更新正文'
                        assert not errors,errors
                        print('PASS: explicit content refresh, stable input/navigation, stale edit preservation, grouped sources and transcript detection')
                    finally:
                        browser.close()
            finally:
                client.close()
        finally:
            server.terminate()
            try: server.wait(timeout=8)
            except subprocess.TimeoutExpired: server.kill()


if __name__=='__main__':
    main()
