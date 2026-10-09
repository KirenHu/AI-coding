"""Long-list and short-window acceptance against real HTML/CSS/JS and SQLite."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0,str(Path(__file__).parents[1]))
from worktwin.api import create_app

ROOT=Path(__file__).parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-layout-') as dirname:
        app=create_app(Path(dirname)/'db.sqlite',start_worker=False)
        with app.state.db.connect() as con:
            con.executemany('''INSERT INTO knowledge(kind,title,body,status,created_by,scope,topic,scope_detail,quality)
                VALUES('fact',?,?,'confirmed','human','global','界面验收','合成验收资料','useful')''',
                [(f'验收知识 {i:03}',f'这是第 {i} 篇知识的长正文。'*20) for i in range(260)])
        with TestClient(app) as client:
            html=client.get('/').text
            html=re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />|<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>','',html)
            def backend(path,options):
                assert path.startswith('/api/')
                response=client.request(options.get('method','GET'),path,
                    headers=options.get('headers') or {},content=options.get('body'))
                return dict(status=response.status_code,body=response.text)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
                try:
                    page=browser.new_page(viewport={'width':1280,'height':768})
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.expose_function('__bridge',backend)
                    page.set_content(html)
                    page.evaluate('''() => {window.fetch=async(path,opts={})=>{
                        const r=await window.__bridge(path,opts);
                        return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});
                    }}''')
                    page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text())
                    page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                    expect(page.locator('.knowledge-row')).to_have_count(260)
                    checks=[]
                    for width,height in [(1280,768),(1024,600),(900,480)]:
                        page.set_viewport_size({'width':width,'height':height})
                        settings=page.locator('.settings-link')
                        expect(settings).to_be_visible()
                        box=settings.bounding_box()
                        assert box and box['y']>=0 and box['y']+box['height']<=height,(width,height,box)
                        state=page.evaluate('''() => {
                            const work=document.querySelector('.main-area');
                            return {body:document.body.scrollHeight,view:innerHeight,
                                content:work.scrollHeight,area:work.clientHeight};
                        }''')
                        assert state['body']<=height and state['content']>state['area'],state
                        # No scrolling to reach Settings. The button must really
                        # receive a click and load model settings at every size.
                        settings.click()
                        expect(page.get_by_role('heading',name='模型设置',exact=True)).to_be_visible()
                        page.locator('[data-page=knowledge]').click()
                        expect(page.locator('.knowledge-row')).to_have_count(260)
                        page.evaluate("document.querySelector('.main-area').scrollTop=999999")
                        scrolled=settings.bounding_box()
                        assert scrolled and abs(scrolled['y']-box['y'])<1
                        page.evaluate("document.querySelector('.main-area').scrollTop=0")
                        checks.append(f'{width}x{height}')
                    page.set_viewport_size({'width':1280,'height':768})
                    screenshot=os.getenv('WORKTWIN_UI_LAYOUT_SCREENSHOT','')
                    if screenshot:
                        Path(screenshot).parent.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=screenshot)
                    assert not errors,errors
                    print(json.dumps(dict(result='PASS',knowledge_rows=260,viewports=checks,
                        settings_visible_without_scroll=True,javascript_errors=errors)))
                finally:
                    browser.close()


if __name__=='__main__':
    main()
