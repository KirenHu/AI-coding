"""Self-hosted sharing service: durable knowledge, scoped expiring access links.

The client publishes approved knowledge snapshots, never employee raw records.
One asset per installation/knowledge ID is reused by every published twin.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .answers import answer_from_knowledge
from .provider import ChatRequest, ProviderService
from .enterprise_settings import EnterpriseSettings
from .model_settings import ModelInput, ModelListInput, PersonalModel, validate_url


class Asset(BaseModel):
    id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=130)
    body: str = Field(min_length=1, max_length=50000)
    version: int = Field(ge=1)


class PublishedTwin(BaseModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=90)
    description: str = Field(default='', max_length=500)
    knowledge_ids: list[int] = Field(max_length=1500)


class Publication(BaseModel):
    assets: list[Asset] = Field(max_length=1500)
    twins: list[PublishedTwin] = Field(max_length=100)
    revision: int = Field(ge=1)


class GrantInput(BaseModel):
    recipient: str = Field(min_length=1, max_length=90)
    days: int = Field(default=7, ge=1, le=90)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class AdminModelInput(ModelInput):
    daily_calls: int = Field(default=1000, ge=1, le=1000000)
    daily_tokens: int = Field(default=2000000, ge=100, le=1000000000)
    minute_calls: int = Field(default=20, ge=1, le=10000)


class EmployeeInput(BaseModel):
    identity: str = Field(min_length=1, max_length=90, pattern=r'^[\w.@ -]+$')


class SharedStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS installations (
                    owner TEXT NOT NULL, installation TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(owner,installation));
                CREATE TABLE IF NOT EXISTS assets (
                    owner TEXT NOT NULL, installation TEXT NOT NULL, local_id INTEGER NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(owner,installation,local_id));
                CREATE TABLE IF NOT EXISTS twins (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, installation TEXT NOT NULL,
                    local_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(owner,installation,local_id));
                CREATE TABLE IF NOT EXISTS assignments (
                    twin_id TEXT REFERENCES twins(id) ON DELETE CASCADE,
                    asset_id INTEGER NOT NULL, PRIMARY KEY(twin_id,asset_id));
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, twin_id TEXT REFERENCES twins(id) ON DELETE CASCADE,
                    recipient TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, action TEXT NOT NULL, owner TEXT NOT NULL,
                    resource TEXT NOT NULL, at TEXT DEFAULT (datetime('now')));
            ''')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA secure_delete=ON')
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def create_server(path: Path | None = None, *, inference_client=None, publisher_tokens: dict | None = None) -> FastAPI:
    credentials = publisher_tokens if publisher_tokens is not None else json.loads(os.getenv('WORKTWIN_PUBLISHER_TOKENS', '{}'))
    admin_token = os.getenv('WORKTWIN_ADMIN_TOKEN','')
    if (not credentials and len(admin_token)<32) or (admin_token and (len(admin_token)<32 or admin_token in credentials.values())) or len(set(credentials.values())) != len(credentials) or any(not k or not v for k,v in credentials.items()):
        raise RuntimeError('设置 WORKTWIN_PUBLISHER_TOKENS 为员工标识到独立令牌的 JSON 映射')
    store = SharedStore(path or Path(os.getenv('WORKTWIN_SERVER_DATA_DIR', './worktwin-server-data')) / 'sharing.sqlite')
    config = EnterpriseSettings(store, credentials)
    provider_lock = threading.RLock()
    initial_limits={k:config.get(k,os.getenv(env,default)) for k,env,default in (
        ('daily_calls','WORKTWIN_DAILY_CALL_LIMIT','1000'),('daily_tokens','WORKTWIN_DAILY_TOKEN_LIMIT','2000000'),('minute_calls','WORKTWIN_MINUTE_CALL_LIMIT','20'))}
    provider = None if inference_client else ProviderService(store.path.with_name('usage.sqlite'),config=config.model_config(),limits=initial_limits,allow_unconfigured=True)
    app = FastAPI(title='WorkTwin Enterprise', version='1.1.1', docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.provider = provider
    app.state.settings = config

    @app.middleware('http')
    async def headers(request: Request, call_next):
        # Bound request size even when the sender omits Content-Length.
        if request.method in ('PUT','POST'):
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                chunks.append(chunk)
                if size > 4 * 1024 * 1024:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({'detail':'发布内容过大，请减少分身知识数量'}, status_code=413)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def owner(authorization: str = Header(default='')):
        if admin_token and secrets.compare_digest(authorization, 'Bearer '+admin_token):
            return '__administrator__'
        identity = config.identify(authorization.removeprefix('Bearer '))
        if identity:
            return identity
        raise HTTPException(401, '发布凭据无效')

    def administrator(authorization: str = Header(default='')):
        if not admin_token or not secrets.compare_digest(authorization, 'Bearer '+admin_token):
            raise HTTPException(403,'此操作仅限企业管理员')
        return '__administrator__'

    def model_access(authorization):
        if (admin_token and secrets.compare_digest(authorization, 'Bearer '+admin_token)) or config.identify(authorization.removeprefix('Bearer ')):
            return True
        return False

    def public_model():
        if inference_client:
            return {'model':'测试模型','model_configured':True}
        return {'model':provider.model,'model_configured':bool(provider.key and provider.model)}

    def audit(con, action, identity, resource):
        con.execute('INSERT INTO audit(action,owner,resource) VALUES(?,?,?)', (action,identity,resource))

    def owned(con, twin_id, identity):
        row = con.execute('SELECT * FROM twins WHERE id=? AND owner=?', (twin_id,identity)).fetchone()
        if not row:
            raise HTTPException(404, '分身不存在')
        return row

    def view(con, token):
        row = con.execute('''SELECT g.id grant_id,g.expires_at,g.revoked,t.* FROM grants g
            JOIN twins t ON t.id=g.twin_id WHERE g.token_hash=?''', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['revoked'] or row['expires_at'] <= int(time.time()):
            raise HTTPException(403, '分享已到期或已撤销')
        return row

    @app.get('/health')
    def health():
        return {'ok': True, 'version': '1.1.1'}

    @app.get('/v1/me')
    def me(identity=Depends(owner)):
        return {'ok':True,'identity':identity,'role':'admin' if identity=='__administrator__' else 'employee',**public_model()}

    @app.post('/v1/model/test')
    def test_model(identity=Depends(owner)):
        if inference_client:
            inference_client.chat([{'role':'user','content':'请只回复 OK。'}],max_tokens=16)
        else:
            provider.complete(identity,ChatRequest(messages=[{'role':'user','content':'请只回复 OK。'}],max_tokens=16))
        return {'ok':True,**public_model()}

    @app.get('/v1/admin/settings')
    def admin_settings(identity=Depends(administrator)):
        return {**public_model(),'base_url':provider.url if provider else '', 'has_api_key':bool(provider and provider.key),
                'daily_calls':provider.daily_calls if provider else 1000,'daily_tokens':provider.daily_tokens if provider else 2000000,
                'minute_calls':provider.minute_calls if provider else 20,'employees':config.employees()}

    @app.post('/v1/admin/models')
    def admin_models(body: ModelListInput, identity=Depends(administrator)):
        old=config.model_config()
        try:
            url=validate_url(body.base_url)
            if not body.api_key and url!=old['url']:
                raise ValueError()
            return {'models':PersonalModel(url,body.api_key or old['key']).list_models()}
        except Exception as exc:
            raise HTTPException(400,'无法获取模型列表，可直接填写服务商提供的模型名称') from exc

    @app.put('/v1/admin/model')
    def admin_model(body: AdminModelInput, identity=Depends(administrator)):
        nonlocal provider
        with provider_lock:
            old=config.model_config()
            try:
                url=validate_url(body.base_url)
                if not body.api_key and url!=old['url']:
                    raise ValueError('更换模型服务地址时请重新填写 API Key')
                key=body.api_key or old['key']
                candidate=PersonalModel(url,key,body.model)
                if not candidate.configured:
                    raise ValueError('请填写模型 API Key')
                candidate.chat([{'role':'user','content':'请只回复 OK。'}],max_tokens=16)
            except Exception as exc:
                raise HTTPException(400,'测试失败，未更改现有配置；请检查地址、模型、API Key 和服务额度') from exc
            limits={k:getattr(body,k) for k in ('daily_calls','daily_tokens','minute_calls')}
            config.save_model({'url':url,'key':key,'model':body.model},limits)
            provider=ProviderService(store.path.with_name('usage.sqlite'),config={'url':url,'key':key,'model':body.model},limits=limits)
            app.state.provider=provider
        return {'ok':True,**public_model()}

    @app.post('/v1/admin/employees')
    def issue_employee(body: EmployeeInput, identity=Depends(administrator)):
        if body.identity=='__administrator__':
            raise HTTPException(400,'该员工标识不可使用')
        token=config.issue(body.identity)
        return {'identity':body.identity,'token':token}

    @app.delete('/v1/admin/employees/{employee}')
    def revoke_employee(employee: str, identity=Depends(administrator)):
        if not config.revoke(employee):
            raise HTTPException(404,'员工不存在')
        with store.connect() as con:
            con.execute('UPDATE grants SET revoked=1 WHERE twin_id IN (SELECT id FROM twins WHERE owner=?)',(employee,))
            audit(con,'revoke_employee',identity,employee)
        return {'revoked':True}

    @app.post('/v1/chat/completions')
    def completions(body: ChatRequest, authorization: str = Header(default='')):
        if not model_access(authorization):
            raise HTTPException(401, '未获得企业模型访问权限')
        if inference_client:
            content = inference_client.chat([m.model_dump() for m in body.messages], max_tokens=body.max_tokens)
            if not model_access(authorization):
                raise HTTPException(401,'企业 Token 已撤销，请联系管理员')
            return {'choices':[{'message':{'content':content}}]}
        result=provider.complete(authorization, body)
        if not model_access(authorization):
            raise HTTPException(401,'企业 Token 已撤销，请联系管理员')
        return result

    @app.get('/v1/shared/knowledge/{knowledge_id}')
    def shared_knowledge(knowledge_id: int, authorization: str = Header(default='')):
        with store.connect() as con:
            row=view(con,authorization.removeprefix('Bearer '))
            asset=con.execute('''SELECT a.local_id id,a.title,a.body FROM assets a JOIN assignments x ON x.asset_id=a.local_id
                WHERE x.twin_id=? AND a.owner=? AND a.installation=? AND a.local_id=?''',
                (row['id'],row['owner'],row['installation'],knowledge_id)).fetchone()
            if not asset:
                raise HTTPException(404,'这篇知识不在当前分享范围内')
            return dict(asset)

    @app.post('/v1/twins/{twin_id}/preview-ask')
    def preview_ask(twin_id: str, body: Question, authorization: str = Header(default=''), identity=Depends(owner)):
        with store.connect() as con:
            row=owned(con,twin_id,identity)
            revision=row['version']
            rows=[dict(r) for r in con.execute('''SELECT a.local_id id,a.title,a.body FROM assets a JOIN assignments x ON x.asset_id=a.local_id
                WHERE x.twin_id=? AND a.owner=? AND a.installation=?''',(twin_id,identity,row['installation']))]
        class ScopedModel:
            def chat(self,messages,max_tokens=1600):
                if inference_client:
                    return inference_client.chat(messages,max_tokens=max_tokens)
                return provider.complete(identity,ChatRequest(messages=messages,max_tokens=max_tokens))['choices'][0]['message']['content']
        result=answer_from_knowledge(ScopedModel(),body.question,rows)
        owner(authorization)
        with store.connect() as con:
            if owned(con,twin_id,identity)['version']!=revision:
                raise HTTPException(409,'发布版本已更新，请重新提问')
        return {**result,'scope':'published','publication_version':revision}

    @app.put('/v1/publications/{installation}')
    def publish(installation: str, body: Publication, identity=Depends(owner)):
        if not (16 <= len(installation) <= 64 and installation.isalnum()):
            raise HTTPException(400, '安装标识无效')
        asset_ids = {a.id for a in body.assets}
        twin_ids = {t.id for t in body.twins}
        if len(asset_ids)!=len(body.assets) or len(twin_ids)!=len(body.twins) or any(not set(t.knowledge_ids).issubset(asset_ids) for t in body.twins):
            raise HTTPException(400, '知识引用无效或重复')
        publications = {}
        with store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            previous = con.execute('SELECT revision FROM installations WHERE owner=? AND installation=?', (identity,installation)).fetchone()
            if previous and body.revision < previous[0]:
                raise HTTPException(409, '发布版本已过期')
            con.execute('INSERT INTO installations VALUES(?,?,?) ON CONFLICT(owner,installation) DO UPDATE SET revision=excluded.revision', (identity,installation,body.revision))
            con.execute('DELETE FROM assets WHERE owner=? AND installation=?', (identity,installation))
            con.executemany('INSERT INTO assets VALUES(?,?,?,?,?,?)', [(identity,installation,a.id,a.title,a.body,a.version) for a in body.assets])
            old = con.execute('SELECT id,local_id FROM twins WHERE owner=? AND installation=?', (identity,installation)).fetchall()
            for t in old:
                if t['local_id'] not in twin_ids:
                    con.execute('DELETE FROM twins WHERE id=?', (t['id'],))
            for t in body.twins:
                pub_id = hashlib.sha256(f'{identity}\0{installation}\0{t.id}'.encode()).hexdigest()[:32]
                con.execute('''INSERT INTO twins(id,owner,installation,local_id,name,description,version) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,version=excluded.version''',
                    (pub_id,identity,installation,t.id,t.name,t.description,body.revision))
                con.execute('DELETE FROM assignments WHERE twin_id=?', (pub_id,))
                con.executemany('INSERT INTO assignments VALUES(?,?)', [(pub_id,k) for k in set(t.knowledge_ids)])
                publications[str(t.id)] = pub_id
            audit(con,'publish',identity,installation)
        return {'publications':publications, 'revision':body.revision}

    @app.get('/v1/twins/{twin_id}/grants')
    def grants(twin_id: str, identity=Depends(owner)):
        with store.connect() as con:
            owned(con,twin_id,identity)
            return [dict(r) for r in con.execute('SELECT id,recipient,expires_at,revoked FROM grants WHERE twin_id=? ORDER BY rowid DESC', (twin_id,))]

    @app.post('/v1/twins/{twin_id}/grants')
    def grant(twin_id: str, body: GrantInput, identity=Depends(owner)):
        token, gid = secrets.token_urlsafe(32), secrets.token_hex(16)
        expiry = int(time.time()) + body.days * 86400
        with store.connect() as con:
            owned(con,twin_id,identity)
            con.execute('INSERT INTO grants(id,twin_id,recipient,token_hash,expires_at) VALUES(?,?,?,?,?)',
                        (gid,twin_id,body.recipient,hashlib.sha256(token.encode()).hexdigest(),expiry))
            audit(con,'grant',identity,gid)
        return {'id':gid, 'token':token, 'expires_at':expiry}

    @app.delete('/v1/twins/{twin_id}/grants/{grant_id}')
    def revoke(twin_id: str, grant_id: str, identity=Depends(owner)):
        with store.connect() as con:
            owned(con,twin_id,identity)
            row = con.execute('UPDATE grants SET revoked=1 WHERE id=? AND twin_id=?', (grant_id,twin_id))
            if not row.rowcount:
                raise HTTPException(404,'分享不存在')
            audit(con,'revoke',identity,grant_id)
        return {'revoked':True}

    @app.get('/v1/shared')
    def shared_info(authorization: str = Header(default='')):
        with store.connect() as con:
            row = view(con,authorization.removeprefix('Bearer '))
            titles = [dict(r) for r in con.execute('''SELECT a.local_id id,a.title FROM assignments x
                JOIN assets a ON a.local_id=x.asset_id AND a.owner=? AND a.installation=? WHERE x.twin_id=?''',
                (row['owner'],row['installation'],row['id']))]
            return {'name':row['name'], 'description':row['description'], 'knowledge':titles, 'expires_at':row['expires_at']}

    @app.post('/v1/shared/ask')
    def shared_ask(body: Question, authorization: str = Header(default='')):
        token = authorization.removeprefix('Bearer ')
        with store.connect() as con:
            row = view(con,token)
            revision, gid = row['version'], row['grant_id']
            knowledge = [dict(r) for r in con.execute('''SELECT a.local_id id,a.title,a.body FROM assignments x
                JOIN assets a ON a.local_id=x.asset_id AND a.owner=? AND a.installation=? WHERE x.twin_id=?''',
                (row['owner'],row['installation'],row['id']))]
        class ScopedModel:
            def chat(self, messages, max_tokens=1600):
                if inference_client:
                    return inference_client.chat(messages,max_tokens=max_tokens)
                result = provider.complete('grant:'+gid,ChatRequest(messages=messages,max_tokens=max_tokens))
                return result['choices'][0]['message']['content']
        result = answer_from_knowledge(ScopedModel(),body.question,knowledge)
        # Revocation or publication during an in-flight call must not disclose
        # its former snapshot even if the provider already received it.
        with store.connect() as con:
            current = view(con,token)
            if current['version'] != revision:
                raise HTTPException(409,'分身知识已更新，请重新提问')
            audit(con,'ask',row['owner'],gid)
        return result

    @app.get('/share', response_class=HTMLResponse)
    def shared_page():
        return (Path(__file__).parent/'static/shared.html').read_text(encoding='utf-8')

    return app
