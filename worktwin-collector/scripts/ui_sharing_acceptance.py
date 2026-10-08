"""Real HTTP + browser sharing acceptance; deterministic provider, no paid key."""
import json
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect
from worktwin.api import create_app
from worktwin.server import create_server
from tests.support import FakeModel

ROOT=Path(__file__).parents[1]


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def start(app,p):
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=p,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started:return server,thread
        time.sleep(.05)
    raise AssertionError('HTTP service did not start')


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-sharing-ui-') as tmp:
        base=Path(tmp);p1,p2=port(),port()
        local_url,remote_url=f'http://127.0.0.1:{p1}',f'http://127.0.0.1:{p2}'
        remote,rt=start(create_server(base/'remote.sqlite',inference_client=FakeModel(),publisher_tokens={'owner':'acceptance-employee-token'}),p2)
        app=create_app(base/'local.sqlite',interval=1)
        local,lt=start(app,p1)
        try:
            with httpx.Client(base_url=local_url,trust_env=False,timeout=30) as c, sync_playwright() as pw:
                html=c.get('/').text.replace('<link rel="stylesheet" href="/assets/styles.css" />','').replace('<script defer src="/assets/app.js"></script>','')
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def bridge(path,options):
                    r=c.request(options.get('method','GET'),path,headers=options.get('headers',{}),content=options.get('body'))
                    return {'status':r.status_code,'body':r.text}
                page.expose_function('__bridge',bridge)
                page.set_content(html)
                page.evaluate("() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})};}")
                page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text())
                page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                expect(page.get_by_text('连接企业知识服务')).to_be_visible()
                page.locator('#model-status').click()
                page.locator('#enterprise-url').fill(remote_url)
                page.locator('#enterprise-token').fill('acceptance-employee-token')
                page.locator('#connect-enterprise').click()
                expect(page.get_by_text('企业知识模型已连接')).to_be_visible()
                # Create a real confirmed article with Markdown formatting.
                page.get_by_role('button',name='新建知识').click()
                page.locator('#edit-k-title').fill('审批节点说明')
                page.locator('#edit-k-body').fill('## 当前方案\n\n审批复用**统一工作流引擎**。')
                page.get_by_role('button',name='保存知识').click()
                expect(page.get_by_text('审批节点说明').first).to_be_visible()
                page.get_by_text('审批节点说明').first.click()
                expect(page.locator('.markdown-body strong')).to_have_text('统一工作流引擎')
                h={'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)}
                kid=c.get('/api/knowledge',headers=h).json()[0]['id']
                linked=c.post('/api/knowledge',headers=h,json={'title':'审批细则','body':'各部门按既有权限发起审批。','status':'confirmed'}).json()['id']
                page.get_by_role('button',name='编辑内容').click()
                page.locator('#edit-k-body').fill(f'## 当前方案\n\n审批复用**统一工作流引擎**。\n\n参见 [[K{linked}|关联审批细则]]。')
                page.get_by_role('button',name='保存知识').click()
                page.get_by_text('审批节点说明').first.click()
                expect(page.locator('.history-version summary')).to_contain_text('v1')
                page.locator(f'.markdown-body a[href="#knowledge-{linked}"]').click()
                expect(page.locator('.drawer-title')).to_have_text('审批细则')
                page.locator(f'[data-open-knowledge="{kid}"]').click()
                expect(page.locator('.drawer-title')).to_have_text('审批节点说明')
                page.get_by_role('button',name='关闭').last.click()
                page.locator('[data-page=twins]').click()
                page.get_by_role('button',name='创建数字分身').click()
                page.locator('#twin-name').fill('项目交接分身')
                page.get_by_role('button',name='创建并选择知识').click()
                page.locator(f'[data-select-entry="{kid}"]').check()
                page.get_by_role('button',name='保存授权').click()
                expect(page.get_by_text('已选择 1 篇')).to_be_visible()
                page.get_by_role('button',name='启用分享').click()
                page.get_by_role('button',name='创建访问链接').click()
                page.locator('#share-recipient').fill('项目接任者')
                page.get_by_role('button',name='创建链接',exact=True).click()
                expect(page.locator('#share-url')).to_be_visible()
                url=page.locator('#share-url').input_value()
                token=url.split('#access=')[1]
                # The recipient executes the shipped page and talks to the real
                # shared HTTP API; no employee process or SQLite access is used.
                with httpx.Client(base_url=remote_url,trust_env=False,timeout=30) as r:
                    receiver=browser.new_page()
                    def shared_bridge(path,opts):
                        rr=r.request(opts.get('method','GET'),path,headers=opts.get('headers',{}),content=opts.get('body'))
                        return {'status':rr.status_code,'body':rr.text}
                    receiver.expose_function('__bridge',shared_bridge)
                    shared_html=r.get('/share').text
                    script=re.search(r'<script>(.*?)</script>',shared_html,re.S).group(1)
                    receiver.set_content(shared_html.replace('<script>'+script+'</script>',''))
                    receiver.evaluate("() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})};}")
                    receiver.add_script_tag(content=script.replace("new URLSearchParams(location.hash.slice(1)).get('access')||''",json.dumps(token)).replace("history.replaceState(null,'',location.pathname);",''))
                    expect(receiver.locator('#name')).to_have_text('项目交接分身')
                    receiver.locator('#question').fill('审批怎么实现？')
                    receiver.locator('#ask').click()
                    expect(receiver.locator('#answer')).to_contain_text('已授权知识生成的回答')
                    # Stop the owner's process entirely; published answers remain.
                    local.should_exit=True;lt.join(10)
                    assert not lt.is_alive()
                    receiver.locator('#ask').click()
                    expect(receiver.locator('#answer')).to_contain_text('已授权知识生成的回答')
                    h={'Authorization':'Bearer acceptance-employee-token'}
                    with app.state.db.connect() as con:pid=con.execute('SELECT remote_id FROM publications').fetchone()[0]
                    grant=r.get('/v1/twins/'+pid+'/grants',headers=h).json()[0]
                    assert r.delete('/v1/twins/'+pid+'/grants/'+grant['id'],headers=h).status_code==200
                    receiver.locator('#ask').click()
                    expect(receiver.locator('#answer')).to_contain_text('已到期或已撤销')
                    assert not errors,errors
                page.locator('#share-url').evaluate("el => el.value='测试链接已隐藏'")
                page.screenshot(path=str(ROOT/'release'/'WorkTwin-1.0-sharing.png'),full_page=True)
                browser.close()
            print(json.dumps({'result':'PASS','connection':'PASS','markdown':'PASS','knowledge_links':'PASS','version_history':'PASS','share_link':'PASS','recipient_ask':'PASS','owner_offline':'PASS','revocation':'PASS','javascript_errors':errors}))
        finally:
            local.should_exit=True;remote.should_exit=True;lt.join(5);rt.join(5)


if __name__=='__main__':
    (ROOT/'release').mkdir(exist_ok=True)
    main()
