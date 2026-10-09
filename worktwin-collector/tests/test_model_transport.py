"""DeepSeek's reasoning default, verified TLS and actionable secret-free errors."""
import io
import json
import ssl
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient

from worktwin import model_transport as transport
from worktwin.api import create_app
from worktwin.credentials import DesktopSecrets
from worktwin.model_settings import PersonalModel
from worktwin.provider import ProviderService, ChatRequest
from tests.test_phase1_settings import auth


def reply(content='OK', finish_reason='stop'):
    return io.BytesIO(json.dumps({'choices':[{'message':{'content':content},'finish_reason':finish_reason}]}).encode())


def test_deepseek_actual_request_shape_and_enterprise_parity(tmp_path,monkeypatch):
    calls=[]
    def open_request(request, **kwargs):
        context=kwargs['context']
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.cert_store_stats()['x509_ca'] > 0
        payload=json.loads(request.data)
        calls.append(payload)
        # A thinking model may consume the old 16-token budget before producing
        # a final answer. Do not mistake a reasoning-only response for success.
        if payload.get('thinking') != {'type':'disabled'}:
            return reply(None,'length')
        return reply()
    monkeypatch.setattr(transport,'urlopen',open_request)
    app=create_app(tmp_path/'local.sqlite',start_worker=False)
    with TestClient(app) as c:
        cfg={'base_url':'https://api.deepseek.com','model':'deepseek-flash','api_key':'example-secret'}
        assert c.put('/api/model/personal',headers=auth(c),json=cfg).status_code==200
        assert c.post('/api/model/test',headers=auth(c)).status_code==200
        assert all(p['max_tokens']==64 and p['stream'] is False for p in calls)
    enterprise=ProviderService(tmp_path/'usage.sqlite',config={'url':cfg['base_url'],'model':cfg['model'],'key':cfg['api_key']})
    assert enterprise.complete('employee',ChatRequest(messages=[{'role':'user','content':'example'}]))['choices']
    assert calls[-1]['thinking']=={'type':'disabled'}
    assert 'thinking' not in transport.chat_payload('https://other.example/v1','deepseek-flash',[],64)


@pytest.mark.parametrize('code,expected',[(401,'API Key 无效'),(402,'余额不足'),(403,'拒绝访问'),(404,'接口不存在'),(429,'额度已满'),(503,'暂时不可用')])
def test_provider_http_errors_reach_both_settings_actions_without_secrets(tmp_path,monkeypatch,code,expected):
    def rejected(request,**kwargs):
        raise HTTPError(request.full_url,code,'private-key',{},io.BytesIO(b'private-key request-private-content'))
    monkeypatch.setattr(transport,'urlopen',rejected)
    app=create_app(tmp_path/'local.sqlite',start_worker=False)
    with TestClient(app) as c:
        headers=auth(c)
        cfg={'base_url':'https://api.deepseek.com','model':'deepseek-flash','api_key':'private-key'}
        for method,path in ((c.post,'/api/model/personal/models'),(c.put,'/api/model/personal')):
            response=method(path,headers=headers,json=cfg)
            assert response.status_code==400
            assert expected in response.json()['detail'] and f'HTTP {code}' in response.json()['detail']
            assert 'private-key' not in response.text and 'request-private-content' not in response.text
        assert not app.state.model.configured and not app.state.model.secrets.get('personal_model_key')


@pytest.mark.parametrize('reason,expected',[(ssl.SSLCertVerificationError('private-proxy'), '证书验证失败'),(TimeoutError('private-proxy'),'超时'),(OSError('private-proxy'),'无法连接')])
def test_network_diagnostics_never_disable_tls_or_echo_exceptions(monkeypatch,reason,expected):
    def failed(*args,**kwargs):
        raise URLError(reason)
    monkeypatch.setattr(transport,'urlopen',failed)
    with pytest.raises(transport.ModelConnectionError) as exc:
        PersonalModel('https://api.deepseek.com','example','deepseek-flash').list_models()
    assert expected in str(exc.value) and 'private-proxy' not in str(exc.value)


def test_connected_provider_but_failed_keychain_has_distinct_message(tmp_path,monkeypatch):
    monkeypatch.setattr(transport,'urlopen',lambda *a,**k:reply())
    app=create_app(tmp_path/'local.sqlite',start_worker=False)
    def fail_save(*args,**kwargs):raise RuntimeError('private-key keychain-details')
    monkeypatch.setattr(DesktopSecrets,'set',fail_save)
    with TestClient(app) as c:
        response=c.put('/api/model/personal',headers=auth(c),json={'base_url':'https://api.deepseek.com','model':'deepseek-flash','api_key':'private-key'})
        assert '模型调用成功，但密钥保存失败' in response.json()['detail']
        assert 'private-key' not in response.text
        assert not app.state.model.configured


@pytest.mark.parametrize('content,finish,expected',[(None,'length','长度上限'),('partial','length','长度上限'),('', 'stop','有效答案')])
def test_empty_reasoning_or_truncated_answers_are_not_success(monkeypatch,content,finish,expected):
    monkeypatch.setattr(transport,'urlopen',lambda *a,**k:reply(content,finish))
    with pytest.raises(transport.ModelConnectionError) as exc:
        PersonalModel('https://other.example/v1','example','reasoning').chat([{'role':'user','content':'example'}])
    assert expected in str(exc.value)
