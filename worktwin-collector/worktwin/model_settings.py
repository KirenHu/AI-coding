"""Personal and enterprise editions share a live, replaceable model connection."""
from __future__ import annotations

import json
import threading
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from .inference import GatewayClient


class ModelInput(BaseModel):
    base_url: str = Field(min_length=8, max_length=300)
    model: str = Field(min_length=1, max_length=150)
    api_key: str = Field(default='', max_length=2000)


class ModelListInput(BaseModel):
    base_url: str = Field(min_length=8,max_length=300)
    api_key: str = Field(default='',max_length=2000)


def validate_url(url):
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('服务地址不能包含密码、查询参数或片段')
    if not parsed.hostname or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1'))):
        raise ValueError('请使用 HTTPS 服务地址，本机测试允许 localhost')
    return url.rstrip('/')


class PersonalModel:
    def __init__(self, url='', key='', model=''):
        self.url = validate_url(url) if url else ''
        self.key = key
        self.model = model.strip()

    @property
    def configured(self):
        return bool(self.url and self.key and self.model)

    def list_models(self):
        if not self.url or not self.key:
            raise ValueError('请先填写模型服务地址和 API Key')
        request=Request(self.url+'/models',headers={'Authorization':'Bearer '+self.key})
        with urlopen(request,timeout=30) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError('模型列表响应过大')
        data=json.loads(raw)
        return sorted({m['id'] for m in data.get('data',[])[:2000] if isinstance(m,dict) and isinstance(m.get('id'),str) and 0<len(m['id'])<=150})

    def chat(self, messages, max_tokens=1800):
        if not self.configured:
            raise RuntimeError('请先配置个人模型')
        request = Request(self.url + '/chat/completions', method='POST',
                          data=json.dumps({'model':self.model,'messages':messages,'max_tokens':max_tokens,'temperature':0.1},ensure_ascii=False).encode(),
                          headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        with urlopen(request, timeout=120) as response:
            data = json.load(response)
        content = data['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('模型没有返回有效文本')
        return content


class ModelRuntime:
    def __init__(self, db, secrets, *, enterprise=None, injected=None):
        self.db, self.secrets = db, secrets
        self.lock = threading.RLock()
        self.error = ''
        self.injected = injected is not None
        default = 'enterprise' if enterprise and enterprise.configured else 'personal'
        self.mode = db.setting('edition', default)
        if injected is not None:
            self.client = injected
        elif self.mode == 'personal':
            try:
                key = secrets.get('personal_model_key')
                self.client = PersonalModel(db.setting('personal_base_url'), key, db.setting('personal_model'))
            except Exception:
                self.client = PersonalModel()
                self.error = '无法读取模型凭据，请检查系统安全存储并重新连接'
        else:
            self.client = enterprise or GatewayClient(url='', token='')
        self.status = 'connected' if self.injected else ('configured' if self.configured else 'not_configured')

    @property
    def configured(self):
        return self.client.configured

    @property
    def url(self):
        return getattr(self.client, 'url', '')

    @property
    def token(self):
        return getattr(self.client, 'token', '')

    def replace(self, client, mode, *, tested=False):
        with self.lock:
            self.client, self.mode = client, mode
            self.status, self.error = ('connected' if tested else 'configured'), ''
            self.db.set_setting('edition', mode)

    def chat(self, messages, max_tokens=1800):
        with self.lock:
            client = self.client
        try:
            result = client.chat(messages, max_tokens=max_tokens)
        except Exception as exc:
            with self.lock:
                if client is self.client:
                    self.status, self.error = 'error', '模型调用失败，请检查连接、模型名称、凭据或服务额度'
            raise RuntimeError('模型调用失败，请检查连接、模型名称、凭据或服务额度') from exc
        with self.lock:
            if client is not self.client:
                raise RuntimeError('模型设置已更改，请重试')
            self.status, self.error = 'connected', ''
        return result

    def test(self):
        return self.chat([{'role':'user','content':'请只回复 OK。'}], max_tokens=16)
