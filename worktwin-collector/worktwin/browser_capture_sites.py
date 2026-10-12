"""Persistent site intent and document-sized recording intervals.

Site intent belongs to the user; signed workflow sessions contribute only their
own bound pages. Ending an interval never switches a site off.
"""
from __future__ import annotations

import secrets
import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .browser_capture_manual import manual_page, manual_site


def migrate(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS browser_capture_sites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL UNIQUE,
            manual_enabled INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS browser_capture_event_refs(
            session_id TEXT NOT NULL REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            source_session_id TEXT NOT NULL,
            source_seq INTEGER NOT NULL,
            PRIMARY KEY(session_id,seq),
            FOREIGN KEY(source_session_id,source_seq)
                REFERENCES browser_capture_events(session_id,seq) ON DELETE CASCADE);
        CREATE VIEW IF NOT EXISTS browser_capture_session_events AS
            SELECT session_id,seq,at,kind,label,location FROM browser_capture_events
            UNION ALL
            SELECT r.session_id,r.seq,e.at,e.kind,e.label,e.location
            FROM browser_capture_event_refs r JOIN browser_capture_events e
            ON e.session_id=r.source_session_id AND e.seq=r.source_seq;
    """)
    cols = {row[1] for row in con.execute('PRAGMA table_info(browser_capture_sessions)')}
    if 'site_id' not in cols:
        con.execute('ALTER TABLE browser_capture_sessions ADD COLUMN site_id INTEGER')
        now = int(time.time())
        # Existing sessions are historical. Upgrading does not turn a former
        # one-off authorization into continuous monitoring.
        for row in con.execute('SELECT DISTINCT page_key FROM browser_capture_sessions'):
            site = manual_site(row['page_key'])
            con.execute('''INSERT OR IGNORE INTO browser_capture_sites
                (origin,created_at,updated_at) VALUES(?,?,?)''', (site, now, now))
            con.execute('''UPDATE browser_capture_sessions SET site_id=(
                SELECT id FROM browser_capture_sites WHERE origin=?)
                WHERE page_key=? AND mode='task' ''', (site, row['page_key']))
    con.execute('''CREATE INDEX IF NOT EXISTS ix_capture_site_intervals
        ON browser_capture_sessions(site_id,status,tab_id,document_id)''')


class SiteCapture:
    def _site_rows(self, con):
        now = int(time.time())
        enabled = self.enabled(con)
        client = con.execute('SELECT last_seen FROM browser_extension_clients WHERE id=1').fetchone()
        connected = bool(client and now-client['last_seen'] <= 90)
        rows = []
        for record in con.execute('SELECT * FROM browser_capture_sites ORDER BY id'):
            row = dict(record)
            task_requests = con.execute('''SELECT count(*) FROM browser_capture_sessions s
                JOIN browser_capture_tasks t ON t.task_id=s.task_id AND t.launcher_origin=s.launcher_origin
                WHERE s.site_id=? AND s.mode='task' AND s.status IN ('armed','capturing','paused')
                    AND s.expires_at>? AND t.state='active' ''', (row['id'], now)).fetchone()[0]
            intent = bool(row['manual_enabled'] or task_requests)
            capturing = con.execute('''SELECT 1 FROM browser_capture_sessions
                WHERE site_id=? AND status='capturing' AND expires_at>? LIMIT 1''',
                (row['id'], now)).fetchone()
            row.update(manual_enabled=bool(row['manual_enabled']), enabled=intent,
                       task_requests=task_requests,
                       status=('off' if not intent else 'paused' if not enabled else
                               'waiting_extension' if not connected else
                               'capturing' if capturing else 'waiting_page'))
            rows.append(row)
        return rows

    def sites(self):
        with self.db.connect() as con:
            return self._site_rows(con)

    def add_site(self, url):
        site = manual_site(url)
        now = int(time.time())
        with self.db.connect() as con:
            con.execute('''INSERT OR IGNORE INTO browser_capture_sites
                (origin,created_at,updated_at) VALUES(?,?,?)''', (site, now, now))
            result = next(r for r in self._site_rows(con) if r['origin'] == site)
        return result

    @staticmethod
    def _stop_site(con, site_id, now):
        con.execute('''UPDATE browser_capture_sessions SET status='disabled',ended_at=?
            WHERE site_id=? AND status IN ('armed','capturing','paused','navigation_stopped')''',
            (now, site_id))

    def toggle_site(self, site_id, enabled):
        now = int(time.time())
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            changed = con.execute('''UPDATE browser_capture_sites
                SET manual_enabled=?,updated_at=? WHERE id=?''', (int(enabled), now, site_id))
            if not changed.rowcount:
                raise HTTPException(404, '网站不存在')
            if not enabled:
                self._stop_site(con, site_id, now)
            return next(row for row in self._site_rows(con) if row['id'] == site_id)

    def remove_site(self, site_id):
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._stop_site(con, site_id, int(time.time()))
            con.execute('DELETE FROM browser_capture_sites WHERE id=?', (site_id,))
        return {'ok': True}

    def unpair(self):
        with self.db.connect() as con:
            con.execute('DELETE FROM browser_extension_pairing')
            con.execute('DELETE FROM browser_extension_clients')
            con.execute('''UPDATE browser_capture_sessions SET status='disabled',ended_at=?
                WHERE mode='manual' AND status IN ('armed','capturing')''', (int(time.time()),))
        return self.settings()

    def site_current(self, token):
        with self.db.connect() as con:
            self._extension(con, token, require_enabled=False)
            return {'enabled': self.enabled(con), 'sites': self._site_rows(con)}

    def site_bind(self, token, site_id, tab_id, document_id, current_url):
        site = manual_site(current_url)
        now = int(time.time())
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._extension(con, token)
            row = con.execute('SELECT * FROM browser_capture_sites WHERE id=?', (site_id,)).fetchone()
            if not row or not row['manual_enabled'] or site != row['origin']:
                raise HTTPException(409, '此网站未开启持续采集')
            # A reload or a later visit is a new interval. Repeated discovery
            # of the same document returns its existing interval and sequence.
            con.execute('''UPDATE browser_capture_sessions SET status='navigation_stopped',ended_at=?
                WHERE mode='manual' AND site_id IS NOT NULL AND tab_id=?
                AND status='capturing' AND document_id!=?''', (now, tab_id, document_id))
            existing = con.execute('''SELECT id,last_seq FROM browser_capture_sessions
                WHERE site_id=? AND tab_id=? AND document_id=? AND status='capturing'
                AND mode='manual' ''', (site_id, tab_id, document_id)).fetchone()
            if existing:
                return {'session_id': existing['id'], 'last_seq': existing['last_seq'], 'status': 'capturing'}
            sid = secrets.token_urlsafe(20)
            con.execute('''INSERT INTO browser_capture_sessions
                (id,task_id,page_key,launcher_origin,mode,status,created_at,expires_at,
                 tab_id,document_id,site_id)
                VALUES(?,?,?,'manual','manual','capturing',?,9223372036854,?,?,?)''',
                (sid, 'site-'+str(site_id), site, now, tab_id, document_id, site_id))
            return {'session_id': sid, 'last_seq': 0, 'status': 'capturing'}

    def site_close(self, token, session_id, reason):
        with self.db.connect() as con:
            self._extension(con, token, require_enabled=False)
            con.execute('''UPDATE browser_capture_sessions SET status=?,ended_at=?
                WHERE id=? AND mode='manual' AND site_id IS NOT NULL AND status='capturing' ''',
                (reason, int(time.time()), session_id))
        return {'ok': True}

    def site_event(self, token, session_id, seq, kind, tab_id, document_id,
                   current_url, label='', task_session_ids=None):
        """One physical event, separately authorized references for task views."""
        from .browser_capture import ALLOWED_ACTIONS, valid_label, page_key
        if kind not in ALLOWED_ACTIONS:
            raise HTTPException(400, '事件类型无效')
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._extension(con, token)
            record = con.execute('''SELECT s.* FROM browser_capture_sessions s
                JOIN browser_capture_sites w ON w.id=s.site_id
                WHERE s.id=? AND s.mode='manual' AND w.manual_enabled=1''', (session_id,)).fetchone()
            if not record or record['status'] != 'capturing':
                raise HTTPException(409, '记录区间已结束')
            if record['tab_id'] != tab_id or record['document_id'] != document_id:
                raise HTTPException(403, '文档不属于此记录区间')
            if manual_site(current_url) != record['page_key']:
                raise HTTPException(403, '页面不在开启的网站内')
            if seq == record['last_seq']:
                return {'ack': seq, 'status': 'duplicate', 'task_sequences': {}}
            if seq != record['last_seq']+1:
                raise HTTPException(409, '事件序号不连续')
            now = int(time.time())
            event_label = valid_label(label)
            if kind == 'navigation':
                event_label = '页面导航，继续观察'
            elif kind == 'tab_closed':
                event_label = '标签页关闭，停止观察'
            con.execute('''INSERT INTO browser_capture_events
                (session_id,seq,at,kind,label,location) VALUES(?,?,?,?,?,?)''',
                (session_id, seq, now, kind, event_label, manual_page(current_url)))
            self._update_step(con, session_id, seq, kind, event_label)
            con.execute('''UPDATE browser_capture_sessions SET last_seq=?,event_count=event_count+1
                WHERE id=?''', (seq, session_id))
            task_sequences = {}
            for task_sid in dict.fromkeys(task_session_ids or []):
                task = con.execute('''SELECT s.* FROM browser_capture_sessions s
                    JOIN browser_capture_tasks t ON t.task_id=s.task_id AND t.launcher_origin=s.launcher_origin
                    WHERE s.id=? AND s.mode='task' AND s.status='capturing' AND t.state='active' ''',
                    (task_sid,)).fetchone()
                if (not task or task['expires_at'] <= now or task['tab_id'] != tab_id
                        or task['document_id'] != document_id
                        or task['page_key'] != page_key(current_url)):
                    continue
                task_seq = task['last_seq']+1
                con.execute('''INSERT INTO browser_capture_event_refs
                    (session_id,seq,source_session_id,source_seq) VALUES(?,?,?,?)''',
                    (task_sid, task_seq, session_id, seq))
                con.execute('''UPDATE browser_capture_sessions SET last_seq=?,event_count=event_count+1
                    WHERE id=?''', (task_seq, task_sid))
                self._update_step(con, task_sid, task_seq, kind, event_label)
                task_sequences[task_sid] = task_seq
            return {'ack': seq, 'status': 'capturing', 'task_sequences': task_sequences}


class SiteInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class SiteToggle(BaseModel):
    enabled: bool


class SiteBind(BaseModel):
    site_id: int = Field(gt=0)
    tab_id: int = Field(ge=0)
    document_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    current_url: str = Field(max_length=2048)


class SiteEvent(BaseModel):
    session_id: str
    seq: int = Field(ge=1)
    kind: str
    tab_id: int = Field(ge=0)
    document_id: str
    current_url: str = Field(max_length=2048)
    label: str = Field(default='', max_length=500)
    task_session_ids: list[str] = Field(default_factory=list, max_length=100)


class SiteClose(BaseModel):
    session_id: str
    reason: str = Field(pattern=r'^(navigation_stopped|tab_closed|disabled|connection_lost)$')


def install_routes(app, capture, authorized, capture_credential):
    @app.get('/api/browser-capture/sites', dependencies=[Depends(authorized)])
    def sites():
        return capture.sites()

    @app.post('/api/browser-capture/sites', dependencies=[Depends(authorized)])
    def add(body: SiteInput):
        return capture.add_site(body.url)

    @app.put('/api/browser-capture/sites/{site_id}', dependencies=[Depends(authorized)])
    def toggle(site_id: int, body: SiteToggle):
        return capture.toggle_site(site_id, body.enabled)

    @app.delete('/api/browser-capture/sites/{site_id}', dependencies=[Depends(authorized)])
    def remove(site_id: int):
        return capture.remove_site(site_id)

    @app.post('/api/browser-capture/unpair', dependencies=[Depends(authorized)])
    def unpair():
        return capture.unpair()

    @app.post('/capture/sites/current')
    def current(request: Request):
        return capture.site_current(capture_credential(request))

    @app.post('/capture/sites/bind')
    def bind(request: Request, body: SiteBind):
        return capture.site_bind(capture_credential(request), **body.model_dump())

    @app.post('/capture/sites/event')
    def event(request: Request, body: SiteEvent):
        return capture.site_event(capture_credential(request), **body.model_dump())

    @app.post('/capture/sites/close')
    def close(request: Request, body: SiteClose):
        return capture.site_close(capture_credential(request), **body.model_dump())
