"""Persistent model settings and immediately revocable employee credentials."""
import hashlib
import os
import secrets
from pathlib import Path

from .credentials import EncryptedSecrets
from .model_settings import PersonalModel, validate_url


class EnterpriseSettings:
    def __init__(self, store, initial):
        self.store = store
        self.secrets = EncryptedSecrets(store.path.parent / 'server-credentials')
        with store.connect() as con:
            con.executescript('''CREATE TABLE IF NOT EXISTS service_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS employees(identity TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT(datetime('now')));''')
            for identity, token in initial.items():
                con.execute('INSERT OR IGNORE INTO employees(identity,token_hash) VALUES(?,?)', (identity, self.hash(token)))

    @staticmethod
    def hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, key, default=''):
        with self.store.connect() as con:
            row = con.execute('SELECT value FROM service_settings WHERE key=?',(key,)).fetchone()
            return row[0] if row else default

    def model_config(self):
        return {'url':self.get('base_url',os.getenv('WORKTWIN_BYOK_BASE_URL','https://api.openai.com/v1')),
                'model':self.get('model',os.getenv('WORKTWIN_BYOK_MODEL','')),
                'key':self.secrets.get('model_key') or os.getenv('WORKTWIN_BYOK_API_KEY','')}

    def save_model(self, payload, limits):
        self.secrets.set('model_key', payload['key'])
        with self.store.connect() as con:
            for key,value in {'base_url':payload['url'],'model':payload['model'],**limits}.items():
                con.execute('INSERT INTO service_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def identify(self, token):
        with self.store.connect() as con:
            row=con.execute('SELECT identity FROM employees WHERE token_hash=? AND enabled=1',(self.hash(token),)).fetchone()
            return row[0] if row else None

    def employees(self):
        with self.store.connect() as con:
            return [dict(r) for r in con.execute('SELECT identity,enabled,created_at FROM employees ORDER BY created_at DESC')]

    def issue(self, identity):
        token=secrets.token_urlsafe(32)
        with self.store.connect() as con:
            con.execute('INSERT INTO employees(identity,token_hash) VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET token_hash=excluded.token_hash,enabled=1',(identity,self.hash(token)))
        return token

    def revoke(self, identity):
        with self.store.connect() as con:
            return con.execute('UPDATE employees SET enabled=0 WHERE identity=?',(identity,)).rowcount
