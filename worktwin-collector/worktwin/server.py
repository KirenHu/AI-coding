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
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .answers import answer_from_knowledge
from .provider import ChatRequest, ProviderService


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
    if not credentials or len(set(credentials.values())) != len(credentials) or any(not k or not v for k,v in credentials.items()):
        raise RuntimeError('设置 WORKTWIN_PUBLISHER_TOKENS 为员工标识到独立令牌的 JSON 映射')
    store = SharedStore(path or Path(os.getenv('WORKTWIN_SERVER_DATA_DIR', './worktwin-server-data')) / 'sharing.sqlite')
    provider = None if inference_client else ProviderService(store.path.with_name('usage.sqlite'))
    model_tokens = [x.strip() for x in os.getenv('WORKTWIN_ENTERPRISE_TOKENS', '').split(',') if x.strip()]
    app = FastAPI(title='WorkTwin Enterprise', version='1.0.0', docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.provider = provider

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
        for identity, token in credentials.items():
            if secrets.compare_digest(authorization, 'Bearer ' + token):
                return identity
        raise HTTPException(401, '发布凭据无效')

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
        return {'ok': True, 'version': '1.0.0'}

    @app.get('/v1/me')
    def me(identity=Depends(owner)):
        return {'ok':True,'identity':identity}

    @app.post('/v1/chat/completions')
    def completions(body: ChatRequest, authorization: str = Header(default='')):
        if not any(secrets.compare_digest(authorization, 'Bearer ' + t) for t in [*model_tokens, *credentials.values()]):
            raise HTTPException(401, '未获得企业模型访问权限')
        if inference_client:
            content = inference_client.chat([m.model_dump() for m in body.messages], max_tokens=body.max_tokens)
            return {'choices':[{'message':{'content':content}}]}
        return provider.complete(authorization, body)

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
