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
            article={'title':'需求评审工作指南','article_summary':'面向项目参与者，说明评审前需要准备的材料，以及评审后如何保留结论和依据。','body':'# 需求评审工作指南\n\n## 准备材料\n\n1. 确认评审范围。\n2. 梳理关键流程。\n\n## 评审后\n\n保留结论及依据。\n\n| 事项 | 说明 |\n| --- | --- |\n| 结论 | 保留依据 |','status':'confirmed'}
            kid=client.post('/api/knowledge',headers=auth,json=article).json()['id']
            client.put(f'/api/knowledge/{kid}',headers=auth,json={**article,'body':article['body']+'\n新增异常事项应记录负责人。'})
            archived=client.post('/api/knowledge',headers=auth,json={**article,'body':'旧版片段，已合并到新的完整文章。','status':'archived'}).json()['id']
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
                expect(page.locator(f'[data-entry="{archived}"]')).to_have_count(0)
                page.locator('[data-lifecycle=archived]').click()
                expect(page.locator(f'[data-entry="{archived}"]')).to_be_visible()
                expect(page.locator(f'[data-entry="{kid}"]')).to_have_count(0)
                page.locator(f'[data-entry="{archived}"]').click()
                expect(page.locator('.drawer-body')).to_contain_text('旧版片段')
                page.locator('#drawer-close').click();page.locator('[data-lifecycle=active]').click()
                expect(page.locator(f'[data-entry="{archived}"]')).to_have_count(0)
                expect(page.locator('#pending-materials')).to_have_count(0)
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-library.png'))
                page.locator(f'[data-entry="{kid}"]').click()
                expect(page.locator('.drawer-top h2')).to_have_text('需求评审工作指南')
                expect(page.locator('.drawer-body h2')).to_have_count(2)
                expect(page.locator('.drawer-body h1')).to_have_count(0)
                expect(page.locator('.article-summary p')).to_have_text(article['article_summary'])
                expect(page.locator('.article-updates tbody tr')).to_have_count(1)
                assert page.locator('.article-summary').evaluate('(el)=>el.offsetTop') < page.locator('.article-updates').evaluate('(el)=>el.offsetTop') < page.locator('.drawer-body').evaluate('(el)=>el.offsetTop')
                page.locator('#drawer-history-jump').click()
                expect(page.locator('.article-updates tbody tr')).to_have_count(2)
                page.locator('[data-view-version="1"]').click()
                expect(page.locator('.version-heading')).to_contain_text('v1 · 历史版本')
                expect(page.locator('#restore-version')).to_be_visible()
                expect(page.locator('.drawer-body')).not_to_contain_text('新增异常事项')
                page.locator('#history-back').click()
                for width,height in [(900,480),(520,700)]:
                    page.set_viewport_size({'width':width,'height':height})
                    box=page.locator('#edit-entry').bounding_box()
                    assert box and box['y']>=0 and box['y']+box['height']<=height,(width,height,box)
                    assert page.evaluate('document.body.scrollWidth<=innerWidth')
                page.set_viewport_size({'width':1280,'height':900})
                expect(page.locator('#knowledge-history')).to_have_count(0)
                expect(page.locator('article[aria-label=文章正文] table')).to_contain_text('保留依据')
                expect(page.locator('.article-updates tbody tr')).to_have_count(1)
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-detail.png'))
                page.locator('#drawer-history-jump').click();expect(page.get_by_role('heading',name='变更日志')).to_be_visible()
                page.locator('#history-back').click()
                page.locator('#edit-entry').click()
                expect(page.locator('#edit-k-summary')).to_have_value(article['article_summary'])
                edited_summary=article['article_summary']+'重点保留待决策问题。'
                # Hold the refreshed list after saving. The editor must remain
                # open so a fast click cannot reopen a stale cached revision.
                page.evaluate('''() => {
                    window.__articleTestFetch=window.fetch;window.__holdArticleReload=true;
                    window.fetch=async(path,opts={})=>{
                        const response=await window.__articleTestFetch(path,opts);
                        if(path.startsWith('/api/knowledge?')&&window.__holdArticleReload){
                            window.__holdArticleReload=false;
                            return new Promise(resolve=>window.__finishArticleReload=()=>resolve(response));
                        }
                        return response;
                    };
                }''')
                page.locator('#edit-k-summary').fill(edited_summary);page.locator('#save-entry').click()
                page.wait_for_function('typeof window.__finishArticleReload === "function"')
                expect(page.locator('#save-entry')).to_be_disabled()
                expect(page.locator('.detail-drawer')).to_be_visible()
                page.evaluate('window.__finishArticleReload();window.fetch=window.__articleTestFetch;delete window.__articleTestFetch;delete window.__finishArticleReload')
                expect(page.locator('.detail-drawer')).to_have_count(0)
                page.locator(f'[data-entry="{kid}"]').click()
                expect(page.locator('.article-summary p')).to_have_text(edited_summary)
                page.locator('#drawer-close').click()
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
                # Article evidence exposes the existing browser-operation viewer.
                client.put(f'/api/knowledge/{kid}',headers=auth,json=article)
                with app.state.db.connect() as con:
                    con.execute("""INSERT INTO browser_capture_sessions
                        (id,task_id,page_key,launcher_origin,mode,status,created_at,expires_at,ended_at,last_seq,event_count)
                        VALUES('ui-browser-evidence','','https://example.org','', 'manual','completed',1,1,1,1,1)""")
                    con.execute("INSERT INTO browser_capture_events(session_id,seq,at,kind,label,location) VALUES('ui-browser-evidence',1,1,'click','查询订单','https://example.org/orders')")
                    con.execute("INSERT INTO browser_capture_summaries(session_id,event_seq,summary,evidence_json,updated_at) VALUES('ui-browser-evidence',1,'用户点击查询订单。','[1]',1)")
                    con.execute("INSERT INTO browser_article_observations VALUES(?,'ui-browser-evidence','[1]')",(kid,))
                    sid=con.execute('SELECT id FROM sources ORDER BY id LIMIT 1').fetchone()[0]
                    con.execute('UPDATE sources SET allow_ai=1 WHERE id=?',(sid,))
                    did=con.execute("""INSERT INTO documents(source_id,path,relative_path,title,file_type,project,content,sha256,size_bytes,mtime_ns)
                        VALUES(?,?,'history.md','历史材料','.md','验收项目','旧版已归档引用','test-history',24,1)""",(sid,str(Path(tmp)/'history.md'))).lastrowid
                    con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,quote,is_current,superseded) VALUES(?,?,'旧版已归档引用',0,1)",(kid,did))

                page.locator('[data-page=knowledge]').click();page.locator(f'[data-entry="{kid}"]').click()
                page.locator('.source-reference > summary',has_text='来源依据').click()
                page.locator('.historical-evidence > summary').click()
                expect(page.locator('.reference-row').filter(has_text='历史材料')).to_contain_text('旧版历史引用')
                expect(page.locator('.reference-row').filter(has_text='历史材料')).not_to_contain_text('需要重新核对')
                expect(page.locator('.browser-evidence-events')).to_contain_text('查询订单')
                page.locator('[data-read-browser]').click()
                expect(page.get_by_role('heading',name='网页操作详情',exact=True)).to_be_visible()
                expect(page.get_by_role('heading',name='完整操作时间线',exact=True)).to_be_visible()
                expect(page.locator('.dialog-body')).to_contain_text('查询订单')
                assert not errors,errors
                print('UI INTERACTION PASS: article overview/update table/body and history restoration, backstage materials, log entry, 12-turn conversation/context reset, website whitelist switches, plugin sections')
            finally:browser.close()


if __name__=='__main__':main()
