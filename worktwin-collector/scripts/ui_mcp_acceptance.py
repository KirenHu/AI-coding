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
                    page.locator('[data-page=twins]').click();page.locator('[data-twin="1"]').click()
                    page.locator('#generate-mcp').click();expect(page.locator('#mcp-config')).to_be_visible()
                    config=json.loads(page.locator('#mcp-config').input_value())
                    assert config['mcpServers']['worktwin-1']['url'].endswith('/mcp/')
                    page.locator('.dialog-footer [data-close]').click()
                    expect(page.locator('#allow-mcp-logs')).not_to_be_checked()
                    page.locator('#allow-mcp-logs').check()
                    expect(page.locator('#toast')).to_contain_text('已单独授权')
                    assert owner.get('/api/twins/1/mcp',headers=auth).json()['allow_logs']
                    page.locator('#allow-mcp-logs').uncheck()
                    expect(page.locator('#toast')).to_contain_text('已停止提供完整日志')
                    page.locator('#disable-mcp').click();expect(page.locator('#generate-mcp')).to_have_text('启用并获取连接配置')
                    assert not owner.get('/api/twins/1/mcp',headers=auth).json()['enabled']
                    page.locator('#twin-edit-name').fill('未保存的更改');page.locator('#generate-mcp').click()
                    expect(page.locator('#toast')).to_contain_text('请先保存分身的知识授权')
                    assert not owner.get('/api/twins/1/mcp',headers=auth).json()['enabled']
                    assert not errors,errors
                    print('UI MCP PASS: enable, connection config, separate logs, revoke, unsaved protection')
                finally:browser.close()


if __name__=='__main__':main()
