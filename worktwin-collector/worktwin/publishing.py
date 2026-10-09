"""Opt-in publication of confirmed knowledge, with durable retry state."""
import hashlib
import json
import os
import secrets
import threading
from urllib.request import Request, urlopen
from .inference import GatewayClient
from .knowledge_policy import SHARE_SQL


class PublishingClient(GatewayClient):
    def request(self, method, path, body=None):
        request = Request(self.url+path, method=method,
                          data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
                          headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urlopen(request,timeout=15) as response:
            return json.load(response)


class Publisher:
    def __init__(self, db, client=None):
        self.db = db
        self.client = client or PublishingClient(url=os.getenv('WORKTWIN_SERVER_URL',''), token=os.getenv('WORKTWIN_SERVER_TOKEN',''))
        self.installation = db.setting('installation')
        if not self.installation:
            self.installation = secrets.token_hex(16)
            db.set_setting('installation',self.installation)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        def loop():
            while not self.stop_event.wait(5):
                self.sync()
        self.thread=threading.Thread(target=loop,name='worktwin-publishing',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def snapshot(self):
        with self.db.connect() as con:
            twins = [dict(r) for r in con.execute('SELECT t.id,t.name,t.description FROM twins t JOIN publications p ON p.twin_id=t.id WHERE p.enabled=1')]
            assets = {}
            for t in twins:
                rows = [dict(r) for r in con.execute('''SELECT k.id,k.title,k.body,k.version,k.scope,k.project,k.project_key,k.topic,k.scope_detail FROM twin_knowledge tk
                    JOIN knowledge k ON k.id=tk.knowledge_id WHERE tk.twin_id=?
                    AND ''' + SHARE_SQL,(t['id'],))]
                t['knowledge_ids']=[r['id'] for r in rows]
                assets.update({r['id']:r for r in rows})
            return {'twins':twins,'assets':list(assets.values())}

    def sync(self, force=False):
        if not self.client.configured:
            return {'state':'not_configured'}
        with self.lock:
            body=self.snapshot()
            digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if not force and digest==self.db.setting('publication_digest'):
                return {'state':'synced'}
            revision=int(self.db.setting('publication_revision','0'))+1
            body['revision']=revision
            # Persist the monotonic sequence before dispatch, so a timed-out
            # successful request can be followed by a strictly newer snapshot.
            self.db.set_setting('publication_revision',str(revision))
            try:
                result=self.client.request('PUT','/v1/publications/'+self.installation,body)
                with self.db.connect() as con:
                    for tid,pubid in result['publications'].items():
                        con.execute('UPDATE publications SET remote_id=? WHERE twin_id=?',(pubid,int(tid)))
                    con.execute("INSERT INTO settings(key,value) VALUES('publication_digest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(digest,))
                    con.execute("INSERT INTO settings(key,value) VALUES('publication_error','') ON CONFLICT(key) DO UPDATE SET value='' ")
                return {'state':'synced','revision':revision}
            except Exception:
                self.db.set_setting('publication_error','分享更新尚未同步；服务端仍保留上次发布内容。请恢复连接后重试。')
                return {'state':'pending','detail':self.db.setting('publication_error')}

    def public_id(self, tid):
        with self.db.connect() as con:
            r=con.execute('SELECT remote_id FROM publications WHERE twin_id=? AND enabled=1',(tid,)).fetchone()
            if not r or not r[0]:
                raise ValueError('请先发布分身')
            return r[0]
