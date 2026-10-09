"""Real Chromium interaction for the in-knowledge-library review proposal flow."""
from __future__ import annotations

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
sys.path.insert(0,str(Path(__file__).parents[1]))
from worktwin.db import Database
from worktwin.reconcile import review_flags

ROOT=Path(__file__).parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-ui-v05-') as dirname:
        root=Path(dirname)
        document=root/'work'/'design.md';document.parent.mkdir()
        quote='本季度决定将通知方式改为手工发送，避免影响已有用户。'
        document.write_text(quote,encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'WORKTWIN_DATA_DIR':str(root/'db'),'WORKTWIN_GATEWAY_URL':'','WORKTWIN_GATEWAY_TOKEN':''}
        process=subprocess.Popen([sys.executable,'-m','worktwin','serve','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(80):
                try:
                    if httpx.get(base+'/api/health',timeout=.5,trust_env=False).status_code==200:break
                except httpx.HTTPError:time.sleep(.1)
            else:raise RuntimeError('HTTP server unavailable')
            with httpx.Client(base_url=base,timeout=15,trust_env=False) as client:
                html=client.get('/').text
                token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)
                headers={'X-Worktwin-Token':token}
                assert client.post('/api/sources',headers=headers,json={'name':'工作资料','root':str(document.parent)}).status_code==200
                for _ in range(60):
                    docs=client.get('/api/documents',headers=headers).json()
                    if docs:break
                    time.sleep(.2)
                else:raise RuntimeError('Document was not indexed')
                new=client.post('/api/knowledge',headers=headers,json={'title':'旧通知规则','body':'初版通知要求人工发送，旧规则仍在执行。','kind':'decision','status':'confirmed','document_id':docs[0]['id'],'quote':quote})
                assert new.status_code==200,new.text
                kid=new.json()['id']
                db=Database(root/'db'/'worktwin.sqlite')
                with db.connect() as con:
                    file=con.execute('SELECT sha256 FROM documents WHERE id=?',(docs[0]['id'],)).fetchone()
                    con.execute('''INSERT INTO knowledge_proposals(document_id,content_sha,target_id,target_version,action,kind,title,body,quote,reason,fingerprint)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(docs[0]['id'],file[0],kid,1,'replace','decision','新通知规则','新版通知规则由业务系统自动发送，旧手工流程不再使用。',quote,'新的结论替换旧方式','synthetic-proposal-test'))
                    review_flags(con,[kid])
                html=re.sub(r'<link rel="stylesheet" href="/assets/styles\.css(?:\?[^"]*)?" />|<script defer src="/assets/app\.js(?:\?[^"]*)?"></script>', '', html)
                def backend(path,options):
                    res=client.request(options.get('method','GET'),str(path),headers=options.get('headers') or {},content=options.get('body'))
                    return {'status':res.status_code,'body':res.text}
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_PATH') or None,args=['--no-sandbox'])
                    try:
                        page=browser.new_page(viewport={'width':1380,'height':910})
                        errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.expose_function('__worktwinBridge',backend)
                        page.set_content(html,wait_until='domcontentloaded')
                        page.evaluate('''() => {window.fetch=async(path,opts={}) => {
                          const r=await window.__worktwinBridge(path,opts);
                          return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});
                        }}''')
                        page.add_style_tag(content=(ROOT/'worktwin/static/styles.css').read_text())
                        page.add_script_tag(content=(ROOT/'worktwin/static/app.js').read_text())
                        page.locator('[data-project="reviews"]').click()
                        expect(page.locator('[data-proposal]')).to_have_count(1)
                        page.locator('[data-proposal]').first.click()
                        expect(page.get_by_text('AI 建议的新版本')).to_be_visible()
                        page.locator('#proposal-accept').click()
                        expect(page.locator('[data-project="reviews"] .count')).to_have_text('0')
                        result=client.get('/api/knowledge',headers=headers).json()
                        article=next(k for k in result if k['id']==kid)
                        assert article['version']==2 and article['status']=='confirmed'
                        assert '新版通知规则由业务系统自动发送' in article['body']
                        assert not errors,errors
                        screenshot=os.getenv('WORKTWIN_UI_REVIEW_SCREENSHOT','')
                        if screenshot:
                            page.locator('[data-project="all"]').click()
                            page.screenshot(path=screenshot,full_page=True)
                        print('UI REVIEW PASS: proposal listed, accepted, versioned, no browser errors')
                    finally:browser.close()
        finally:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':main()
