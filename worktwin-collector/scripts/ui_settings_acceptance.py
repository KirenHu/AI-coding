"""Browser acceptance for model setup, failure handling and editing protection."""
import json
import os
import re
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect
from scripts.ui_sharing_acceptance import start, port
from tests.test_phase1_settings import provider
from worktwin.api import create_app
from worktwin.server import create_server

ROOT=Path(__file__).parents[1]


def main():
    simulated=provider.__wrapped__();model_url,calls=next(simulated)
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp);admin_token='acceptance-admin-'+'x'*40
        os.environ['WORKTWIN_ADMIN_TOKEN']=admin_token
        app=create_app(folder/'local.sqlite',start_worker=False)
        local,lt=start(app,port());remote_port=port()
        remote,rt=start(create_server(folder/'remote.sqlite',publisher_tokens={}),remote_port)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{local.config.port}',trust_env=False,timeout=30) as c,sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_PATH') or None,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1280,'height':900});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                def route_local(route):
                    from urllib.parse import urlsplit
                    req=route.request;parsed=urlsplit(req.url)
                    response=c.request(req.method,parsed.path+('?' + parsed.query if parsed.query else ''),headers=req.headers,content=req.post_data)
                    route.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','application/json')})
                page.route('http://worktwin-settings.test/**',route_local)
                # A replaced app may still be served by an old background
                # process. Give a restart instruction instead of a broken form.
                page.route('**/api/health',lambda route:route.fulfill(json={'ok':True,'version':'1.0.1'}))
                page.goto('http://worktwin-settings.test/')
                expect(page.get_by_role('heading',name='请重新启动 WorkTwin',exact=True)).to_be_visible()
                page.locator('#settings-link').click()
                expect(page.get_by_role('heading',name='请重新启动 WorkTwin',exact=True)).to_be_visible()
                page.unroute('**/api/health')
                page.goto('http://worktwin-settings.test/')
                expect(page.locator('.main-nav .nav-link')).to_have_count(3)
                expect(page.get_by_role('heading',name='我的知识库',exact=True)).to_be_visible()
                page.route('**/api/data-location',lambda route:route.fulfill(status=404,json={'detail':'Not Found'}))
                page.locator('#settings-link').click()
                expect(page.get_by_role('heading',name='模型设置',exact=True)).to_be_visible()
                expect(page.locator('#data-path')).to_contain_text('模型设置仍可使用')
                page.unroute('**/api/data-location')
                page.locator('#personal-url').fill(model_url);page.locator('#personal-model').fill('personal-model');page.locator('#personal-key').fill('ui-private-key')
                # Provider diagnostics must reach the form instead of being
                # replaced by the old generic address/key failure message.
                page.route('**/api/model/personal/models',lambda route:route.fulfill(status=400,json={'detail':'模型账户余额不足（HTTP 402）；也可手动填写模型名称'}))
                page.locator('#personal-list-models').click()
                expect(page.locator('#personal-list-result')).to_contain_text('余额不足（HTTP 402）')
                expect(page.locator('#personal-key')).to_have_value('ui-private-key')
                page.unroute('**/api/model/personal/models')
                page.locator('#personal-list-models').click()
                expect(page.locator('#personal-list-result')).to_contain_text('获取到 2 个模型')
                page.locator('#save-personal-model').click()
                expect(page.locator('#settings-result')).to_contain_text('模型已连接')
                assert page.locator('#personal-key').input_value()==''
                assert calls[-1]['authorization']=='Bearer ui-private-key'
                (ROOT/'release').mkdir(exist_ok=True)
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.1-personal.png'),full_page=True)
                page.locator('#personal-model').fill('broken');page.locator('#save-personal-model').click()
                expect(page.locator('#settings-result')).to_contain_text('未启用新配置')
                expect(page.locator('#settings-result')).to_contain_text('API Key 无效')
                page.once('dialog',lambda d:d.dismiss())
                page.locator('[data-page=knowledge]').click()
                expect(page.locator('#personal-model')).to_have_value('broken')
                page.locator('#personal-model').fill('personal-model');page.locator('#save-personal-model').click()
                expect(page.locator('#settings-result')).to_contain_text('模型已连接')
                page.locator('[data-page=knowledge]').click();page.locator('#create-knowledge').click()
                page.locator('#edit-k-title').fill('未保存的知识');page.locator('#edit-k-body').fill('审批复用统一工作流引擎')
                page.once('dialog',lambda d:d.dismiss());page.keyboard.press('Escape')
                expect(page.locator('#edit-k-title')).to_have_value('未保存的知识')
                page.locator('#save-entry').click();expect(page.locator('[data-entry]').first).to_be_visible()
                page.locator('[data-page=twins]').click();page.get_by_role('button',name='创建数字分身').click()
                page.locator('#twin-name').fill('交接助手');page.get_by_role('button',name='创建并选择知识').click()
                page.locator('[data-select-entry]').first.check();page.locator('#twin-edit-name').fill('更新后的助手');page.locator('#save-selections').click()
                expect(page.locator('#toast')).to_contain_text('分身信息和 1 篇知识授权已保存')
                expect(page.locator('#twin-edit-name')).to_be_enabled()
                expect(page.locator('#twin-edit-name')).to_have_value('更新后的助手')
                expect(page.locator('#selected-count')).to_have_text('已选择 1 篇')
                # A rejected request must preserve input and show no success feedback.
                page.route('**/api/twins/1',lambda route:route.fulfill(status=500,json={'detail':'模拟保存失败'}) if route.request.method=='PUT' else route.fallback())
                page.locator('#twin-edit-name').fill('保存失败时保留这个名称');page.locator('#save-twin-info').click()
                expect(page.locator('#toast')).to_contain_text('操作未完成')
                expect(page.locator('#twin-edit-name')).to_have_value('保存失败时保留这个名称')
                page.unroute('**/api/twins/1');page.locator('#save-twin-info').click()
                expect(page.locator('#toast')).to_contain_text('已保存')
                # Verify settings stays reachable in a narrow window.
                page.set_viewport_size({'width':760,'height':700});expect(page.locator('#settings-link')).to_be_visible()
                page.locator('#settings-link').click();page.set_viewport_size({'width':1280,'height':900})
                page.locator('[data-edition=enterprise]').click();page.locator('#settings-enterprise-connect').click()
                page.locator('#enterprise-url').fill(f'http://127.0.0.1:{remote_port}');page.locator('#enterprise-token').fill(admin_token);page.locator('#connect-enterprise').click()
                expect(page.locator('#admin-section')).to_contain_text('员工 Token')
                page.locator('#admin-url').fill(model_url);page.locator('#admin-model').fill('company-model');page.locator('#admin-key').fill('company-ui-key');page.locator('#save-admin-model').click()
                expect(page.locator('#toast')).to_contain_text('企业模型与限额已保存')
                page.locator('#employee-identity').fill('alice');page.locator('#issue-employee').click()
                expect(page.locator('#employee-token-value')).to_be_visible()
                assert len(page.locator('#employee-token-value').input_value())>=32
                assert not errors,errors
                (ROOT/'release').mkdir(exist_ok=True)
                page.locator('#employee-token-value').evaluate("el=>el.value='演示 Token 已隐藏'")
                page.screenshot(path=str(ROOT/'release/WorkTwin-1.1-settings.png'),full_page=True)
                browser.close()
                print(json.dumps({'result':'PASS','personal_model':'PASS','failed_save':'PASS','unsaved_edits':'PASS','atomic_twin_configuration':'PASS','small_window_settings':'PASS','admin_model':'PASS','employee_token':'PASS','javascript_errors':errors}))
        finally:
            local.should_exit=True;remote.should_exit=True;lt.join(5);rt.join(5)
            next(simulated,None)


if __name__=='__main__':main()
