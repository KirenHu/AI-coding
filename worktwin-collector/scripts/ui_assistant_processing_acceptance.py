"""Browser controls for twin MCP, separate log consent and unsaved permissions."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect

sys.path.insert(0,str(Path(__file__).parents[1]))
from worktwin.api import create_app
ROOT=Path(__file__).parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-mcp-ui-') as folder:
        app=create_app(Path(folder)/'db.sqlite',start_worker=False)
        with app.state.db.connect() as con:
            con.execute("INSERT INTO twins(name,description) VALUES('项目知识分身','供其他AI读取授权知识')")
        with TestClient(app) as owner:
            html=owner.get('/').text
            auth={'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)}
            html=re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />|<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>','',html)
            def bridge(path,options):
                r=owner.request(options.get('method','GET'),path,headers=options.get('headers') or {},content=options.get('body'))
                return dict(status=r.status_code,body=r.text)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
                try:
                    page=browser.new_page(viewport={'width':1280,'height':800});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.expose_function('__bridge',bridge);page.set_content(html)
                    page.evaluate('''() => {window.fetch=async(path,opts={})=>{const r=await window.__bridge(path,opts);return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}})}}''')
                    page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text())
                    page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                    page.evaluate("sourceDetail({id:1,name:'Codex',kind:'codex',adapter:'codex',collection_scope:'assistant',document_count:3,allow_ai:false})")
                    page.locator('#source-history').click()
                    expect(page.locator('#history-start')).to_be_visible()
                    page.locator('#history-start').fill('2026-10-01');page.locator('#history-end').fill('2026-10-13')
                    page.locator('#history-submit').click()
                    expect(page.locator('#history-result')).to_contain_text('不支持会话历史整理')
                    out=ROOT/'release/p1-ui';out.mkdir(exist_ok=True)
                    for width in (1176,1366,1920):
                        page.set_viewport_size({'width':width,'height':827})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.screenshot(path=str(out/f'history-{width}.png'))
                    (out/'history-preview.html').write_text(page.content())
                    assert not errors,errors
                    print('UI ASSISTANT PASS: history dates, actionable error, responsive layout')
                finally:browser.close()


if __name__=='__main__':main()
