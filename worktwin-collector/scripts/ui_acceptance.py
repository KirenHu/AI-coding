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
                        expect(page.locator('.main-nav .nav-link')).to_have_count(3)
                        expect(page.get_by_role('heading', name='我的知识库')).to_be_visible()
                        page.locator('[data-page=sources]').click()
                        page.get_by_role('button', name='授权采集').first.click()
                        page.locator('#new-source-name').fill('产品文档')
                        page.locator('#new-source-path').fill(str(work.parent))
                        page.get_by_role('button', name='授权并开始采集').click()
                        expect(page.get_by_text('产品文档').first).to_be_visible()
                        # Verify collection actually reached the real SQLite database.
                        headers = {'X-Worktwin-Token': re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', html).group(1)}
                        for _ in range(100):
                            docs = client.get('/api/documents', headers=headers).json()
                            if docs:
                                break
                            time.sleep(.1)
                        assert len(docs) == 1, docs
                        page.locator('[data-page=knowledge]').click()
                        page.get_by_role('button', name='新建知识').click()
                        page.locator('#edit-k-title').fill('为什么审批采用工作流节点')
                        page.locator('#edit-k-kind').select_option('decision')
                        page.locator('#edit-k-body').fill('复用现有工作流路由能力，避免独立系统重复维护。')
                        page.get_by_role('button', name='保存知识').click()
                        expect(page.get_by_text('为什么审批采用工作流节点').first).to_be_visible(timeout=15000)
                        page.get_by_text('为什么审批采用工作流节点').first.click()
                        expect(page.locator('.detail-drawer')).to_be_visible()
                        page.get_by_role('button', name='关闭').last.click()
                        page.locator('[data-page=twins]').click()
                        page.get_by_role('button', name='创建数字分身').first.click()
                        page.locator('#twin-name').fill('项目交接助手')
                        page.locator('#twin-desc').fill('向协作者解释审批节点的设计原因')
                        page.get_by_role('button', name='创建并选择知识').click()
                        expect(page.get_by_text('为什么审批采用工作流节点').first).to_be_visible()
                        page.locator('[data-select-entry]').first.check()
                        page.get_by_role('button', name='保存授权').click()
                        # Wait for the saved editor to reload, rather than accepting
                        # the optimistic selection label while PUT is still pending.
                        expect(page.locator('.status-note').first).to_contain_text('已保存授权 1 篇', timeout=15000)
                        expect(page.locator('#save-selections')).to_be_enabled()
                        expect(page.locator('[data-select-entry]').first).to_be_checked()
                        screenshot = Path(os.environ.get('WORKTWIN_UI_SCREENSHOT', str(state/'preview.png')))
                        screenshot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(screenshot), full_page=True)
                        assigned = client.get('/api/twins', headers=headers).json()
                        assert len(assigned) == 1 and assigned[0]['knowledge_count'] == 1
                        assert not errors, errors
                        print(json.dumps({'ui': 'PASS', 'three_tabs': 'PASS',
                                          'file_authorization': 'PASS', 'knowledge_edit': 'PASS',
                                          'twin_assignment': 'PASS', 'javascript_errors': errors,
                                          'screenshot': str(screenshot)}, ensure_ascii=False))
                    finally:
                        browser.close()
            finally:
                client.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == '__main__':
    main()
