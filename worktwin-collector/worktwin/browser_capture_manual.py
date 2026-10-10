"""Explicit local opt-in browser recording, independent of workflow tickets.

One requested site, one bound browser tab and a bounded session. The manually
recorded session uses the same redacted event/summary storage as task capture.
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException


def manual_site(value: str) -> str:
    raw=str(value or '').strip()
    if raw and '://' not in raw:
        raw='https://'+raw
    try:
        url=urlsplit(raw)
        host=(url.hostname or '').lower()
        if not host or url.username or url.password or url.scheme not in ('https','http'):
            raise ValueError()
        if url.scheme=='http' and host not in ('127.0.0.1','localhost'):
            raise ValueError()
        if host in ('::1',) or host.endswith('.localhost'):
            raise ValueError()
        port=url.port
        if port is not None and not 1<=port<=65535:
            raise ValueError()
        return f"{url.scheme}://{host}"+(f":{port}" if port else '')
    except ValueError:
        raise HTTPException(400,'请输入 HTTPS 网站地址；本地测试可使用 http://localhost')


def manual_page(url: str) -> str:
    site=manual_site(url)
    parsed=urlsplit(url)
    return site+(parsed.path or '/')


class ManualCapture:
    def manual_config(self, *, enabled: bool, url: str = '') -> dict:
        target=manual_site(url) if enabled else ''
        now=int(time.time())
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if enabled and not self.enabled(con):
                raise HTTPException(409,'请先启用浏览器行为采集总开关')
            row=con.execute("SELECT manual_enabled,manual_site,manual_session_id FROM browser_capture_settings WHERE id=1").fetchone()
            if enabled and row['manual_enabled'] and row['manual_site']==target:
                session=con.execute("SELECT status,expires_at FROM browser_capture_sessions WHERE id=?",
                                    (row['manual_session_id'],)).fetchone()
                if session and session['status'] in ('armed','capturing') and session['expires_at']>now:
                    return self.settings()
            if row['manual_session_id']:
                con.execute("""UPDATE browser_capture_sessions SET status='completed',ended_at=?
                    WHERE id=? AND mode='manual' AND status IN ('armed','capturing')""",
                    (now,row['manual_session_id']))
            if enabled:
                sid=secrets.token_urlsafe(20)
                con.execute("""INSERT INTO browser_capture_sessions
                    (id,task_id,page_key,launcher_origin,mode,status,created_at,expires_at)
                    VALUES(?,?,?,'manual','manual','armed',?,?)""",
                    (sid,'manual-'+secrets.token_hex(8),target,now,now+8*3600))
                con.execute("""UPDATE browser_capture_settings SET manual_site=?,
                    manual_session_id=?,manual_enabled=1 WHERE id=1""",(target,sid))
            else:
                con.execute("""UPDATE browser_capture_settings SET
                    manual_enabled=0,manual_site='',manual_session_id='' WHERE id=1""")
        return self.settings()

    def manual_current(self, token: str) -> dict:
        with self.db.connect() as con:
            self._extension(con,token)
            row=con.execute("""SELECT b.manual_enabled,b.manual_site,b.manual_session_id,
                s.status,s.expires_at,s.tab_id,s.document_id,s.last_seq
                FROM browser_capture_settings b
                LEFT JOIN browser_capture_sessions s ON s.id=b.manual_session_id
                WHERE b.id=1""").fetchone()
            if not row['manual_enabled'] or row['status'] not in ('armed','capturing') or row['expires_at']<=time.time():
                return {'enabled':False}
            return {'enabled':True,'session_id':row['manual_session_id'],
                    'site':row['manual_site'],'status':row['status'],
                    'tab_id':row['tab_id'],'document_id':row['document_id'],
                    'last_seq':row['last_seq']}

    def manual_rebind(self, token: str, session_id: str, tab_id: int,
                      document_id: str, current_url: str) -> dict:
        target=manual_site(current_url)
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._extension(con,token)
            row=con.execute("""SELECT s.* FROM browser_capture_sessions s
                JOIN browser_capture_settings b ON b.manual_session_id=s.id
                WHERE s.id=? AND b.manual_enabled=1""",(session_id,)).fetchone()
            if (not row or row['mode']!='manual' or row['status']!='capturing'
                or row['expires_at']<=time.time() or row['tab_id']!=tab_id
                or target!=row['page_key'] or not document_id):
                raise HTTPException(409,'手动采集已结束或页面不在指定网站内')
            con.execute("""UPDATE browser_capture_sessions SET document_id=?
                WHERE id=?""",(document_id,session_id))
        return {'status':'capturing'}
