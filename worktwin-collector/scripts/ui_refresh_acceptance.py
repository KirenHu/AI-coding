"""Source selection and stale-edit acceptance against an isolated real API."""
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from worktwin.api import create_app
from worktwin.article_maintenance import source_materials, _signature

ROOT=Path(__file__).parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-refresh-') as tmp:
        state=Path(tmp);work=state/'documents';work.mkdir()
        (work/'note.md').write_text('验收资料',encoding='utf-8')
        transcript=state/'codex'/'sessions';transcript.mkdir(parents=True)
        paths={'codex_sessions_path':transcript,'claude_projects_path':state/'claude','cursor_exports_path':state/'cursor'}
        with patch.multiple('worktwin.config',**{name:(lambda value=value:value) for name,value in paths.items()}):
            app=create_app(state/'test.sqlite',start_worker=False)
            with TestClient(app) as client, sync_playwright() as pw:
                html=client.get('/').text
                token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)
                auth={'X-Worktwin-Token':token}
                html=re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />|<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>','',html)
                def backend(path,options):
                    assert path.startswith('/api/')
                    if path=='/api/pick-folder':return {'status':200,'body':json.dumps({'path':str(work)})}
                    response=client.request(options.get('method','GET'),path,headers=options.get('headers') or {},content=options.get('body'))
                    return {'status':response.status_code,'body':response.text}
                browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_PATH') or None,args=['--no-sandbox'])
                try:
                    page=browser.new_page(viewport={'width':1440,'height':920});errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.expose_function('__bridge',backend);page.set_content(html)
                    page.evaluate("() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})}}")
                    page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text())
                    page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                    expect(page.get_by_role('heading',name='知识库',exact=True)).to_be_visible()
                    page.locator('[data-page=sources]').click()
                    expect(page.locator('[data-source-tab]')).to_have_count(3)
                    page.evaluate("window.__navNode=document.querySelector('.main-nav')")
                    page.locator('#add-folder').click();page.locator('#browse-folder').click()
                    page.clock.install();page.clock.fast_forward(16000)
                    expect(page.locator('#new-source-path')).to_have_text(str(work))
                    page.locator('.dialog-footer [data-close]').click()
                    added=client.post('/api/sources',headers=auth,json={'kind':'folder','root':str(work),'name':'刷新后出现'})
                    assert added.status_code==200,added.text
                    expect(page.locator('.source-item')).to_have_count(0)
                    page.locator('#scan').click();expect(page.locator('.source-item')).to_have_count(1)
                    assert page.evaluate('window.__navNode===document.querySelector(".main-nav")')
                    legacy_path=state/'old-session-scope';legacy_path.mkdir()
                    legacy=client.post('/api/sources',headers=auth,json={'kind':'codex','root':str(legacy_path),'name':'原局部会话'}).json()['id']
                    page.locator('[data-source-tab=assistants]').click()
                    expect(page.locator('[data-assistant]')).to_have_count(4)
                    expect(page.locator('[data-assistant=antigravity]')).to_be_visible()
                    expect(page.locator('#new-source-path')).to_have_count(0)
                    page.locator('[data-assistant=codex]').check()
                    expect(page.locator('[data-assistant=codex]')).to_be_checked()
                    expect(page.locator('.source-item').filter(has=page.locator('[data-assistant=codex]'))).to_contain_text('等待会话记录')
                    rows=client.get('/api/sources/assistants',headers=auth).json();codex=next(row for row in rows if row['tool']=='codex')
                    assert codex['selected'] and not codex['allow_ai'] and not codex['allow_share']
                    page.locator('[data-assistant-detail=codex]').click()
                    expect(page.locator(f'[data-legacy-source="{legacy}"]')).to_be_visible()
                    page.locator(f'[data-legacy-source="{legacy}"]').click()
                    expect(page.locator('.dialog-header h2')).to_have_text('原局部会话')
                    page.locator('.dialog-footer [data-close]').click()
                    (transcript/'sample.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':'sample','cwd':'/work/project'}})+'\n')
                    page.locator('#scan').click()
                    expect(page.locator('.source-item').filter(has=page.locator('[data-assistant=codex]'))).not_to_contain_text('等待会话记录')
                    assert next(row for row in client.get('/api/sources/assistants',headers=auth).json() if row['tool']=='codex')['detected']
                    (ROOT/'release').mkdir(exist_ok=True)
                    page.screenshot(path=str(ROOT/'release/WorkTwin-1.4-assistants.png'))
                    page.locator('[data-assistant=codex]').uncheck()
                    expect(page.locator('[data-assistant=codex]')).not_to_be_checked()
                    assert client.get('/api/sources',headers=auth).json()
                    # New material can wait for the article agent without a run row yet.
                    with app.state.db.connect() as con:
                        source_id=added.json()['id']
                        content='验收资料：订单查询规则。'
                        doc_id=con.execute("""INSERT INTO documents(source_id,path,relative_path,title,file_type,project,content,sha256,size_bytes,mtime_ns)
                            VALUES(?,?,'queued.md','待整理资料','.md','订单项目',?,'ui-queued-doc',40,1)""",(source_id,str(work/'queued.md'),content)).lastrowid
                        con.execute('UPDATE sources SET allow_ai=1 WHERE id=?',(source_id,))
                        mid=con.execute("""INSERT INTO knowledge(kind,title,body,status,created_by,source_bound,organization,scope,project_key,project,topic,scope_detail,quality,attribution)
                            VALUES('process','订单查询材料','订单查询规则及背景。','draft','enterprise_ai',1,'material','project','ui-article-error','订单项目','订单查询','订单查询验收','useful','document')""").lastrowid
                        con.execute('INSERT INTO knowledge_evidence(knowledge_id,document_id,quote,is_current) VALUES(?,?,?,1)',(mid,doc_id,content))
                    page.locator('[data-source-tab=folders]').click()
                    expect(page.locator('#show-processing')).to_have_text('1 项待整理 · 0 项整理中 · 0 项失败')
                    page.locator('#show-processing').click()
                    expect(page.locator('#queued-article-jobs')).to_have_text('1 个项目等待整理。')
                    expect(page.locator('.dialog-body')).not_to_contain_text('没有待处理任务')
                    expect(page.locator('.job-row')).to_have_count(0)
                    page.locator('.dialog-footer [data-close]').click()
                    # Extraction may be finished while article writing is active.
                    with app.state.db.connect() as con:
                        con.execute("INSERT INTO article_maintenance_runs(scope,project_key,input_signature,state) VALUES('project','ui-article-running','test','running')")
                        con.execute("INSERT INTO article_maintenance_runs(scope,project_key,input_signature,state,error) VALUES('project','ui-article-error','test','error','TransientError')")
                    page.locator('[data-source-tab=assistants]').click()
                    expect(page.locator('#show-processing')).to_have_text('1 项整理中 · 1 项失败')
                    page.locator('#show-processing').click()
                    expect(page.locator('.job-row')).to_have_count(2)
                    expect(page.locator('.dialog-body')).to_contain_text('正在维护文章')
                    expect(page.locator('.dialog-body')).to_contain_text('文章整理失败')
                    expect(page.locator('#retry-ai')).to_be_visible()
                    page.locator('#retry-ai').click()
                    expect(page.locator('.dialog')).to_have_count(0)
                    expect(page.locator('#show-processing')).to_have_text('1 项待整理 · 1 项整理中 · 0 项失败')
                    with app.state.db.connect() as con:
                        retried=con.execute("SELECT state,attempts,error FROM article_maintenance_runs WHERE project_key='ui-article-error'").fetchone()
                        assert tuple(retried)==('queued',0,'')
                    page.clock.fast_forward(4000)
                    expect(page.locator('#refresh-notice')).to_have_count(0)
                    with app.state.db.connect() as con:
                        con.execute("UPDATE article_maintenance_runs SET state='done' WHERE project_key LIKE 'ui-article-%'")
                        material=next(m for m in source_materials(con) if m['id']==mid)
                        con.execute('INSERT INTO article_material_receipts(material_id,input_signature) VALUES(?,?)',(mid,_signature([material])))
                    page.clock.fast_forward(4000)
                    expect(page.locator('#refresh-notice')).to_contain_text('文章整理已完成')
                    # The page explains a delayed native credential read without changing the form.
                    page.evaluate("""() => {
                        window.__credentialTestFetch=window.fetch;
                        window.fetch=async(path,opts={})=>{
                            const response=await window.__credentialTestFetch(path,opts);
                            if(path==='/api/settings'){
                                const data=await response.json();data.model_status='reading_credentials';data.model_error='';
                                return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
                            }
                            return response;
                        };
                    }""")
                    page.locator('#settings-link').click()
                    expect(page.locator('#settings-result')).to_have_text('正在读取系统钥匙串，请处理 macOS 的权限提示')
                    page.evaluate('window.fetch=window.__credentialTestFetch;delete window.__credentialTestFetch')
                    original={'title':'会变化的知识','body':'最初正文','kind':'process','status':'confirmed'}
                    kid=client.post('/api/knowledge',headers=auth,json=original).json()['id']
                    page.locator('[data-page=knowledge]').click()
                    expect(page.locator(f'[data-entry="{kid}"]')).to_be_visible()
                    page.wait_for_function('window.__JOB_POLL__ != null')
                    page.evaluate('clearInterval(window.__JOB_POLL__); window.__JOB_POLL__=null')
                    client.put(f'/api/knowledge/{kid}',headers=auth,json=dict(original,body='后台新正文'))
                    page.locator(f'[data-entry="{kid}"]').click()
                    expect(page.locator('#refresh-notice')).to_contain_text('已更新')
                    expect(page.locator('#drawer-mask')).to_have_count(0)
                    page.locator('#refresh-content').click();page.locator(f'[data-entry="{kid}"]').click()
                    expect(page.locator('.drawer-body')).to_contain_text('后台新正文')
                    page.locator('#edit-entry').click();page.locator('#edit-k-body').fill('我还没有保存的修改')
                    client.put(f'/api/knowledge/{kid}',headers=auth,json=dict(original,body='再次更新正文'))
                    page.locator('#save-entry').click()
                    expect(page.locator('#refresh-notice')).to_be_visible()
                    expect(page.locator('#edit-k-body')).to_have_value('我还没有保存的修改')
                    assert client.get(f'/api/knowledge-item/{kid}',headers=auth).json()['body']=='再次更新正文'
                    assert not errors,errors
                    print('PASS: folder and whole-assistant selection, waiting detection, explicit refresh, stable input, stale edit preservation')
                finally:browser.close()


if __name__=='__main__':main()
