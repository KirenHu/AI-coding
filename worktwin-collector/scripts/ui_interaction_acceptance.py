"""Approved article-first, conversational twin and website controls, with real APIs."""
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from worktwin.api import create_app
from tests.support import FakeModel

ROOT=Path(__file__).parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-interaction-') as tmp:
        model=FakeModel();app=create_app(Path(tmp)/'test.sqlite',start_worker=False,inference_client=model)
        with TestClient(app) as client,sync_playwright() as pw:
            html=client.get('/').text
            token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1);auth={'X-Worktwin-Token':token}
            article={'title':'需求评审工作指南','body':'## 准备材料\n\n1. 确认评审范围。\n2. 梳理关键流程。\n\n## 评审后\n\n保留结论及依据。','status':'confirmed'}
            kid=client.post('/api/knowledge',headers=auth,json=article).json()['id']
            material=client.post('/api/knowledge',headers=auth,json={'title':'评审中的单条观察','body':'待归并的资料'}).json()['id']
            with app.state.db.connect() as con:con.execute("UPDATE knowledge SET organization='material' WHERE id=?",(material,))
            tid=client.post('/api/twins',headers=auth,json={'name':'项目协作助手'}).json()['id']
            client.put(f'/api/twins/{tid}',headers=auth,json={'name':'项目协作助手','knowledge_ids':[kid]})
            html=re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />|<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>','',html)
            requests=[]
            def backend(path,options):
                assert path.startswith('/api/')
                if path.endswith('/ask'):requests.append(json.loads(options['body']))
                response=client.request(options.get('method','GET'),path,headers=options.get('headers') or {},content=options.get('body'))
                return {'status':response.status_code,'body':response.text}
            browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_PATH') or None,args=['--no-sandbox'])
            try:
                page=browser.new_page(viewport={'width':1280,'height':900});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.expose_function('__bridge',backend);page.set_content(html)
                page.evaluate("() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})}}")
                page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text());page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                (ROOT/'release').mkdir(exist_ok=True)
                expect(page.locator('[data-entry]')).to_have_count(1)
                expect(page.locator('#pending-materials')).to_have_text('待整理 1')
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-library.png'))
                page.locator('#pending-materials').click();page.locator(f'[data-pending-entry="{material}"]').click()
                expect(page.locator('.drawer-top h2')).to_have_text('评审中的单条观察')
                page.locator('#drawer-close').click();page.locator(f'[data-entry="{kid}"]').click()
                expect(page.locator('.drawer-top h2')).to_have_text('需求评审工作指南')
                expect(page.locator('.drawer-body h2')).to_have_count(2)
                expect(page.locator('#knowledge-history')).to_have_count(0)
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-detail.png'))
                page.locator('#drawer-history-jump').click();expect(page.get_by_role('heading',name='变更日志')).to_be_visible()
                page.locator('#history-back').click();page.locator('#drawer-close').click()
                page.locator('[data-page=twins]').click();page.locator(f'[data-twin="{tid}"]').click()
                expect(page.locator('[data-exclude-note]')).to_have_count(0)
                expect(page.locator(f'[data-select-entry="{material}"]')).to_have_count(0)
                expect(page.locator('.selection-head b')).to_have_text('补充授权文章')
                page.locator('[data-twin-tab=chat]').click()
                expect(page.locator('#twin-question')).to_have_attribute('maxlength','500')
                for i in range(12):
                    page.locator('#twin-question').fill('评审需要什么？' if i==0 else f'第 {i} 次追问，评审再详细说明')
                    page.locator('#twin-ask').click()
                    expect(page.locator('.chat-message.assistant')).to_have_count(i+1)
                    expect(page.locator('#twin-ask')).to_be_enabled()
                    expect(page.locator('.chat-message.assistant').last).to_contain_text('已授权知识生成的回答')
                assert requests[0]['messages']==[]
                assert requests[1]['messages'][0]['content']=='评审需要什么？'
                assert requests[1]['context_revision']
                assert len(requests[-1]['messages'])==20
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-chat.png'))
                second=client.post('/api/twins',headers=auth,json={'name':'另一个助手'}).json()['id']
                page.evaluate("() => {const fetch=window.fetch;window.fetch=async(path,opts)=>{const response=await fetch(path,opts);if(path.endsWith('/ask'))return new Promise(resolve=>window.__finishAsk=()=>resolve(response));return response}}")
                page.locator('#twin-question').fill('评审完成后如何记录');page.locator('#twin-ask').click()
                page.wait_for_function('typeof window.__finishAsk === "function"')
                page.locator('#twins-back').click();page.locator(f'[data-twin="{second}"]').click();page.locator('[data-twin-tab=chat]').click()
                page.evaluate('window.__finishAsk()')
                expect(page.locator('.chat-message')).to_have_count(0)
                page.locator('#twins-back').click();page.locator(f'[data-twin="{tid}"]').click();page.locator('[data-twin-tab=chat]').click()
                expect(page.locator('.chat-message.assistant').last).to_contain_text('已授权知识生成的回答')
                # Finish subsequent responses immediately; the held request tested navigation only.
                page.evaluate("() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})}}")
                page.locator('#new-conversation').click();expect(page.locator('.chat-message')).to_have_count(0)
                page.locator('#twin-question').fill('重新开始评审');page.locator('#twin-ask').click();expect(page.locator('#twin-ask')).to_be_enabled()
                assert requests[-1]['messages']==[] and requests[-1]['context_revision'] is None
                client.put(f'/api/knowledge/{kid}',headers=auth,json={**article,'body':article['body']+'\n新的补充。'})
                page.locator('#twin-question').fill('这条新补充是什么');page.locator('#twin-ask').click()
                expect(page.locator('#toast')).to_contain_text('已开启新会话')
                expect(page.locator('.chat-message')).to_have_count(0)
                expect(page.locator('#twin-question')).to_have_value('这条新补充是什么')
                client.put(f'/api/knowledge/{kid}',headers=auth,json={**article,'status':'archived'})
                page.locator('[data-page=twins]').click();page.locator('[data-twin-tab=grants]').click()
                expect(page.locator(f'[data-select-entry="{kid}"]')).to_be_checked()
                expect(page.locator(f'[data-select-entry="{kid}"]')).to_be_enabled()
                page.locator(f'[data-select-entry="{kid}"]').uncheck();page.locator('#save-selections').click()
                expect(page.locator('#toast')).to_contain_text('配置已保存')
                assert kid not in client.get(f'/api/twins/{tid}',headers=auth).json()['knowledge_ids']
                page.locator('[data-page=sources]').click();page.locator('[data-source-tab=browser]').click()
                page.locator('#add-website').click();page.locator('#website-origin').fill('https://example.com');page.locator('#save-website').click()
                expect(page.locator('[data-website-toggle]')).not_to_be_checked()
                page.locator('[data-website-toggle]').check();expect(page.locator('[data-website-toggle]')).to_be_checked()
                site=client.get('/api/browser-capture/sites',headers=auth).json()[0];assert site['manual_enabled']
                page.locator('#browser-extension-guide').click()
                for heading in ['采集控制','安装与连接','AI 整理']:expect(page.get_by_role('heading',name=heading,exact=True)).to_be_visible()
                page.locator('#browser-capture-enabled').check();expect(page.locator('#browser-capture-enabled')).to_be_checked();expect(page.locator('#browser-capture-enabled')).to_be_enabled()
                page.locator('#browser-capture-enabled').uncheck();expect(page.locator('#browser-capture-enabled')).not_to_be_checked();expect(page.locator('#browser-capture-enabled')).to_be_enabled()
                assert client.get('/api/browser-capture/sites',headers=auth).json()[0]['manual_enabled']
                page.locator('.dialog-footer [data-close]').click();page.locator('[data-website-toggle]').uncheck()
                expect(page.locator('[data-website-toggle]')).not_to_be_checked()
                assert not client.get('/api/browser-capture/sites',headers=auth).json()[0]['enabled']
                expect(page.locator('.website-row').last).to_contain_text('未采集')
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-websites.png'))
                capture=app.state.browser_capture
                capture.toggle(True);extension_token=capture.pair(capture.pairing_code())
                capture.heartbeat(extension_token)
                page.locator('[data-page=sources]').click()
                expect(page.get_by_text('插件需升级，网站持续采集尚不可用',exact=True)).to_be_visible()
                capture.heartbeat(extension_token,'0.4.1')
                page.locator('[data-page=sources]').click()
                expect(page.get_by_text('插件需升级，网站持续采集尚不可用',exact=True)).to_have_count(0)
                for name in ('项目资料','会议记录'):
                    folder=Path(tmp)/name;folder.mkdir()
                    assert client.post('/api/sources',headers=auth,json={'name':'本地文件夹','root':str(folder),'kind':'folder'}).status_code==200
                page.locator('[data-source-tab=folders]').click()
                expect(page.locator('.source-summary b')).to_have_count(2)
                assert set(page.locator('.source-summary b').all_text_contents())=={'项目资料','会议记录'}
                assert not errors,errors
                print('UI INTERACTION PASS: articles/materials, log entry, 12-turn conversation/context reset, website whitelist switches, plugin sections')
            finally:browser.close()


if __name__=='__main__':main()
