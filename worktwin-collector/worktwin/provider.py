"""Enterprise BYOK transport with persistent budgets and content-free audit."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .model_transport import chat_payload, model_urlopen


class Message(BaseModel):
    role: str = Field(pattern='^(system|user|assistant)$')
    content: str = Field(max_length=80000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=100)
    max_tokens: int = Field(default=1800, ge=1, le=5000)
    temperature: float = Field(default=0.1, ge=0, le=1)


class ProviderService:
    def __init__(self, path: Path | None = None, *, config=None, limits=None, allow_unconfigured=False):
        config, limits = config or {}, limits or {}
        self.url = config.get('url',os.getenv('WORKTWIN_BYOK_BASE_URL', 'https://api.openai.com/v1')).rstrip('/')
        self.key = config.get('key',os.getenv('WORKTWIN_BYOK_API_KEY', ''))
        self.model = config.get('model',os.getenv('WORKTWIN_BYOK_MODEL', ''))
        if (not self.key or not self.model) and not allow_unconfigured:
            raise RuntimeError('企业服务需设置 WORKTWIN_BYOK_API_KEY 和 WORKTWIN_BYOK_MODEL')
        parsed = urlparse(self.url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
            raise ValueError('模型提供方必须使用 HTTPS，除非连接本机测试服务')
        self.path = path or Path(os.getenv('WORKTWIN_SERVER_DATA_DIR', './worktwin-server-data')) / 'usage.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_calls = int(limits.get('daily_calls',os.getenv('WORKTWIN_DAILY_CALL_LIMIT', '1000')))
        self.daily_tokens = int(limits.get('daily_tokens',os.getenv('WORKTWIN_DAILY_TOKEN_LIMIT', '2000000')))
        self.minute_calls = int(limits.get('minute_calls',os.getenv('WORKTWIN_MINUTE_CALL_LIMIT', '20')))
        self.semaphore = threading.BoundedSemaphore(int(os.getenv('WORKTWIN_MODEL_CONCURRENCY', '4')))
        with self.connect() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, at TEXT DEFAULT (datetime('now')),
                state TEXT NOT NULL, reserved INTEGER NOT NULL, tokens INTEGER, model TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def complete(self, actor: str, body: ChatRequest, *, transport=None) -> dict:
        if not self.key or not self.model:
            raise HTTPException(503,'企业模型尚未配置，请联系管理员')
        messages = [m.model_dump() for m in body.messages]
        chars = sum(len(m['content']) for m in messages)
        if chars > 80000:
            raise HTTPException(413, '模型请求文本过长')
        reserved = chars * 3 + body.max_tokens
        actor_hash = hashlib.sha256(actor.encode()).hexdigest()
        request_id = secrets.token_hex(16)
        if not self.semaphore.acquire(blocking=False):
            raise HTTPException(429, '企业模型正在处理其他任务，请稍后重试')
        try:
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                calls, tokens = con.execute("SELECT count(*),COALESCE(sum(COALESCE(tokens,reserved)),0) FROM usage WHERE at>=date('now')").fetchone()
                recent = con.execute("SELECT count(*) FROM usage WHERE actor=? AND at>=datetime('now','-1 minute')", (actor_hash,)).fetchone()[0]
                if calls >= self.daily_calls or tokens + reserved > self.daily_tokens or recent >= self.minute_calls:
                    raise HTTPException(429, '企业模型调用额度已达上限，请稍后重试或联系管理员')
                con.execute('INSERT INTO usage(id,actor,state,reserved,model) VALUES(?,?,?,?,?)', (request_id, actor_hash, 'running', reserved, self.model))
            payload = json.dumps(chat_payload(self.url,self.model,messages,body.max_tokens,body.temperature), ensure_ascii=False).encode()
            request = Request(self.url + '/chat/completions', data=payload, method='POST',
                              headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            try:
                with (transport or model_urlopen)(request, timeout=120) as response:
                    data = json.load(response)
                if not isinstance(data['choices'][0]['message']['content'], str):
                    raise ValueError('Invalid response')
                if not data['choices'][0]['message']['content'].strip() or data['choices'][0].get('finish_reason')=='length':
                    raise ValueError('Empty or truncated response')
                total = (data.get('usage') or {}).get('total_tokens')
                total = total if isinstance(total, int) and total >= 0 else None
                with self.connect() as con:
                    con.execute("UPDATE usage SET state='done',tokens=? WHERE id=?", (total, request_id))
                return {'choices': [{'message': {'role': 'assistant', 'content': data['choices'][0]['message']['content']}}],
                        'usage': {'total_tokens': total}, 'model': self.model}
            except Exception as exc:
                with self.connect() as con:
                    con.execute("UPDATE usage SET state='error' WHERE id=?", (request_id,))
                raise HTTPException(502, '企业模型服务暂时不可用') from exc
        finally:
            self.semaphore.release()
