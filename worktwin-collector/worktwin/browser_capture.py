"""Opt-in, task-triggered browser capture.

Only an authenticated, already-paired extension can transport events. A flow
platform must sign each one-time launch or completion command; it never gets
ambient access to local WorkTwin or permission to collect arbitrary tabs.
Browser actions are stored separately from file/document knowledge ingestion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException

from .browser_capture_status import (
    MAX_STATUS_FAILURES, TASK_STATUS_TTL_SECONDS, validate_status_path,
)
from .browser_capture_manual import ManualCapture, manual_site, manual_page

SESSION_TTL_SECONDS = 8 * 60 * 60
PAIR_TTL_SECONDS = 5 * 60
ALLOWED_ACTIONS = {"click", "change", "submit", "navigation", "feedback", "tab_closed"}
SENSITIVE = re.compile(r"password|passcode|secret|token|api.?key|authorization|credit|card|phone|email|身份证|手机号|银行卡", re.I)


def page_key(url: str) -> str:
    """Discard query/fragment to avoid persisting URL-carried credentials."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("target URL must be public HTTPS")
        if parsed.port not in (None, 443):
            raise ValueError("unsupported HTTPS port")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        if hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost"):
            raise ValueError("loopback target cannot be monitored")
        path = parsed.path or "/"
        return f"https://{hostname}{path}"
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "目标网址必须为正常的 HTTPS 网页") from exc


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return f"https://{parsed.hostname.lower()}" + (f":{parsed.port}" if parsed.port not in (None, 443) else "")


def valid_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f]+", " ", value).strip()[:100]
    if SENSITIVE.search(value):
        return "[隐藏]"
    return value


class BrowserCapture(ManualCapture):
    def __init__(self, db):
        self.db = db
        with db.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS browser_capture_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    allow_ai INTEGER NOT NULL DEFAULT 0,
                    manual_enabled INTEGER NOT NULL DEFAULT 0,
                    manual_site TEXT NOT NULL DEFAULT '',
                    manual_session_id TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO browser_capture_settings(id,enabled) VALUES(1,0);
                CREATE TABLE IF NOT EXISTS browser_extension_pairing(
                    id INTEGER PRIMARY KEY CHECK(id=1), code_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_extension_clients(
                    id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL,
                    last_seen INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_capture_sessions(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, page_key TEXT NOT NULL,
                    launcher_origin TEXT NOT NULL, tab_id INTEGER, document_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'task',
                    status TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, ended_at INTEGER, last_seq INTEGER NOT NULL DEFAULT 0,
                    event_count INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS ix_browser_tasks ON browser_capture_sessions(task_id,launcher_origin);
                CREATE TABLE IF NOT EXISTS browser_capture_tasks(
                    task_id TEXT NOT NULL, launcher_origin TEXT NOT NULL,
                    status_path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL, last_success_at INTEGER NOT NULL,
                    last_check_at INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(task_id,launcher_origin));
                CREATE INDEX IF NOT EXISTS ix_capture_task_state ON browser_capture_tasks(state,last_check_at);
                CREATE TABLE IF NOT EXISTS browser_capture_nonces(
                    nonce_hash TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_capture_events(
                    session_id TEXT NOT NULL REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, at INTEGER NOT NULL,
                    kind TEXT NOT NULL, label TEXT NOT NULL, location TEXT NOT NULL,
                    PRIMARY KEY(session_id,seq));
                CREATE INDEX IF NOT EXISTS ix_browser_events_session ON browser_capture_events(session_id,seq);
                CREATE TABLE IF NOT EXISTS browser_capture_steps(
                    session_id TEXT NOT NULL REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
                    step INTEGER NOT NULL, start_seq INTEGER NOT NULL,end_seq INTEGER NOT NULL,
                    action TEXT NOT NULL, status TEXT NOT NULL, label TEXT NOT NULL,
                    PRIMARY KEY(session_id,step));
                CREATE INDEX IF NOT EXISTS ix_browser_steps_session ON browser_capture_steps(session_id,step);
                CREATE TABLE IF NOT EXISTS browser_capture_summaries(
                    session_id TEXT PRIMARY KEY REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
                    event_seq INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'rule',status TEXT NOT NULL DEFAULT 'preview',
                    updated_at INTEGER NOT NULL);
            """)
            # Older preview installations have a settings row without allow_ai.
            cols={r[1] for r in con.execute("PRAGMA table_info(browser_capture_settings)")}
            if "allow_ai" not in cols:
                con.execute("ALTER TABLE browser_capture_settings ADD COLUMN allow_ai INTEGER NOT NULL DEFAULT 0")
            for field,definition in (
                ('manual_enabled',"INTEGER NOT NULL DEFAULT 0"),
                ('manual_site',"TEXT NOT NULL DEFAULT ''"),
                ('manual_session_id',"TEXT NOT NULL DEFAULT ''")):
                if field not in cols:
                    con.execute(f"ALTER TABLE browser_capture_settings ADD COLUMN {field} {definition}")
            session_cols={r[1] for r in con.execute("PRAGMA table_info(browser_capture_sessions)")}
            if 'mode' not in session_cols:
                con.execute("ALTER TABLE browser_capture_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'task'")
            # Manual capture must be deliberately started again after restart.
            con.execute("""UPDATE browser_capture_sessions SET status='disabled',
                ended_at=? WHERE mode='manual' AND status IN ('armed','capturing')""",
                (int(time.time()),))
            con.execute("""UPDATE browser_capture_settings SET manual_enabled=0,
                manual_session_id='',manual_site='' WHERE id=1""")

    def enabled(self, con) -> bool:
        return bool(con.execute("SELECT enabled FROM browser_capture_settings WHERE id=1").fetchone()[0])

    def settings(self) -> dict:
        now = int(time.time())
        with self.db.connect() as con:
            client = con.execute("SELECT last_seen FROM browser_extension_clients WHERE id=1").fetchone()
            sessions = con.execute("""SELECT count(*) FROM
                (SELECT task_id,launcher_origin FROM browser_capture_sessions
                WHERE mode='task' AND status IN ('armed','capturing','navigation_stopped') AND expires_at>?
                GROUP BY task_id,launcher_origin)""", (now,)).fetchone()[0]
            flags=con.execute("""SELECT allow_ai,manual_enabled,manual_site,manual_session_id
                FROM browser_capture_settings WHERE id=1""").fetchone()
            return {"enabled":self.enabled(con),"allow_ai":bool(flags['allow_ai']),
                    "extension_connected":bool(client and now-int(client["last_seen"])<=90),
                    "active_tasks":sessions,
                    "manual_enabled":bool(flags['manual_enabled']),
                    "manual_site":flags['manual_site'],
                    "manual_session_id":flags['manual_session_id'],
                    "flow_configured":bool(os.environ.get("WORKTWIN_CAPTURE_FLOW_ORIGIN") and
                                           os.environ.get("WORKTWIN_CAPTURE_FLOW_SECRET"))}

    def toggle(self, enabled: bool):
        with self.db.connect() as con:
            con.execute("UPDATE browser_capture_settings SET enabled=? WHERE id=1",(int(enabled),))
            if not enabled:
                con.execute("""UPDATE browser_capture_sessions SET status='disabled',ended_at=?
                    WHERE status IN ('armed','capturing','navigation_stopped')""",(int(time.time()),))
                con.execute("DELETE FROM browser_extension_pairing")
                # Existing extension may re-pair when the user next enables capture.
                con.execute("DELETE FROM browser_extension_clients")
                con.execute("""UPDATE browser_capture_settings SET
                    allow_ai=0,manual_enabled=0,manual_site='',manual_session_id='' WHERE id=1""")
                con.execute("UPDATE browser_capture_tasks SET state='disabled' WHERE state='active'")
        return self.settings()

    def toggle_ai(self,allow_ai: bool):
        with self.db.connect() as con:
            if allow_ai and not self.enabled(con):
                raise HTTPException(409,"请先启用浏览器行为采集")
            con.execute("UPDATE browser_capture_settings SET allow_ai=? WHERE id=1",
                        (int(allow_ai),))
        return self.settings()

    def pairing_code(self) -> str:
        now=int(time.time())
        with self.db.connect() as con:
            if not self.enabled(con):
                raise HTTPException(409,"请先启用任务触发式浏览器行为采集")
            code = secrets.token_urlsafe(16)
            con.execute("""INSERT INTO browser_extension_pairing(id,code_hash,expires_at)
                VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET
                code_hash=excluded.code_hash,expires_at=excluded.expires_at""",
                (hashlib.sha256(code.encode()).hexdigest(),now+PAIR_TTL_SECONDS))
        return code

    def pair(self, code: str) -> str:
        now=int(time.time())
        digest=hashlib.sha256(code.encode()).hexdigest()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            entry=con.execute("SELECT * FROM browser_extension_pairing WHERE id=1").fetchone()
            if (not self.enabled(con) or not entry or entry["expires_at"]<now
                or not hmac.compare_digest(entry["code_hash"],digest)):
                raise HTTPException(403,"配对码无效或已过期")
            token=secrets.token_urlsafe(32)
            con.execute("DELETE FROM browser_extension_pairing")
            con.execute("""INSERT INTO browser_extension_clients(id,token_hash,last_seen,created_at)
                VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                token_hash=excluded.token_hash,last_seen=excluded.last_seen,created_at=excluded.created_at""",
                (hashlib.sha256(token.encode()).hexdigest(),now,now))
        return token

    def _extension(self, con, token: str):
        if not self.enabled(con):
            raise HTTPException(403,"浏览器采集已关闭")
        digest=hashlib.sha256(token.encode()).hexdigest()
        client=con.execute("SELECT token_hash FROM browser_extension_clients WHERE id=1").fetchone()
        if not client or not hmac.compare_digest(client["token_hash"],digest):
            raise HTTPException(401,"插件尚未配对或凭据已失效")
        con.execute("UPDATE browser_extension_clients SET last_seen=? WHERE id=1",(int(time.time()),))

    def heartbeat(self, token: str):
        with self.db.connect() as con:
            self._extension(con,token)
        return {"ok":True}

    def _command(self, con, envelope: dict, sender_origin: str) -> tuple[str,str,str,str]:
        secret=os.environ.get("WORKTWIN_CAPTURE_FLOW_SECRET","")
        trusted=os.environ.get("WORKTWIN_CAPTURE_FLOW_ORIGIN","").rstrip("/")
        if len(secret)<32 or not trusted or sender_origin!=trusted or origin(sender_origin)!=trusted:
            raise HTTPException(403,"未配置或不受信任的流程平台")
        action=envelope.get("action")
        task_id=envelope.get("task_id")
        target=envelope.get("target_url")
        nonce=envelope.get("nonce")
        expires=envelope.get("expires_at")
        signature=envelope.get("signature")
        status_path=validate_status_path(envelope.get("status_path"))
        if (action not in ("start","complete") or not isinstance(task_id,str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}",task_id)
            or not isinstance(target,str) or len(target)>2048
            or not isinstance(nonce,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",nonce)
            or not isinstance(expires,int) or not isinstance(signature,str)):
            raise HTTPException(400,"触发信号结构无效")
        now=int(time.time())
        if expires<now or expires>now+120:
            raise HTTPException(403,"触发信号已过期")
        signed="\n".join([action,task_id,target,nonce,str(expires),sender_origin,status_path])
        actual=hmac.new(secret.encode(),signed.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual,signature):
            raise HTTPException(403,"流程平台签名无效")
        nonce_hash=hashlib.sha256((sender_origin+":"+nonce).encode()).hexdigest()
        con.execute("DELETE FROM browser_capture_nonces WHERE seen_at<?",(now-600,))
        try:
            con.execute("INSERT INTO browser_capture_nonces(nonce_hash,seen_at) VALUES(?,?)",(nonce_hash,now))
        except Exception as exc:
            raise HTTPException(409,"已处理的触发信号不可重复使用") from exc
        return action,task_id,page_key(target),status_path

    def command(self, token: str, sender_origin: str, envelope: dict):
        now=int(time.time())
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            action,task_id,target,status_path=self._command(con,envelope,sender_origin)
            task=con.execute("""SELECT * FROM browser_capture_tasks WHERE
                task_id=? AND launcher_origin=?""",(task_id,sender_origin)).fetchone()
            if task and task["status_path"]!=status_path:
                raise HTTPException(409,"同一流程任务的状态接口不能变更")
            existing=con.execute("""SELECT id,status FROM browser_capture_sessions
                WHERE task_id=? AND launcher_origin=?""",(task_id,sender_origin)).fetchall()
            if action=="complete":
                if not existing:
                    return {"status":"not_found","session_ids":[]}
                self._close_task(con,task_id,sender_origin,"completed",now)
                return {"status":"completed","session_ids":[r["id"] for r in existing]}
            if (task and task["state"]!="active") or any(x["status"]=="completed" for x in existing):
                raise HTTPException(409,"流程任务已结束，不能再次启动采集")
            if not task:
                con.execute("""INSERT INTO browser_capture_tasks
                    (task_id,launcher_origin,status_path,created_at,last_success_at)
                    VALUES(?,?,?,?,?)""",(task_id,sender_origin,status_path,now,now))
            # Each *newly signed click* may authorize another first document
            # within the same task. Replaying an old signal remains forbidden.
            session_id=secrets.token_urlsafe(20)
            con.execute("""INSERT INTO browser_capture_sessions
                (id,task_id,page_key,launcher_origin,status,created_at,expires_at)
                VALUES(?,?,?,?,'armed',?,?)""",
                (session_id,task_id,target,sender_origin,now,now+SESSION_TTL_SECONDS))
            return {"status":"armed","session_id":session_id,"target_page":target}

    @staticmethod
    def _close_task(con, task_id: str, launcher_origin: str, status: str, now: int):
        """Single terminal transition for signed complete, polling, or failure."""
        con.execute("""UPDATE browser_capture_tasks SET state=?,last_check_at=?
            WHERE task_id=? AND launcher_origin=? AND state='active'""",
            (status,now,task_id,launcher_origin))
        con.execute("""UPDATE browser_capture_sessions SET status=?,ended_at=?
            WHERE task_id=? AND launcher_origin=? AND
              status IN ('armed','capturing','navigation_stopped')""",
            (status,now,task_id,launcher_origin))

    def poll_task_states(self, fetcher, *, now: int | None = None, max_tasks: int = 25):
        """Check only the signed, same-origin endpoint for each active task.

        Every failure counts toward a bounded fail-closed stop. Do not reset
        failure counts on restart and do not assume silence means 'running'.
        """
        now = int(time.time()) if now is None else now
        trusted=os.environ.get("WORKTWIN_CAPTURE_FLOW_ORIGIN","").rstrip("/")
        secret=os.environ.get("WORKTWIN_CAPTURE_FLOW_SECRET","")
        with self.db.connect() as con:
            if not self.enabled(con):
                return {"checked":0,"closed":0}
            tasks=[dict(x) for x in con.execute("""SELECT * FROM browser_capture_tasks
                WHERE state='active' AND (last_check_at=0 OR last_check_at<=?)
                ORDER BY last_check_at,created_at LIMIT ?""",(now-8,max_tasks))]
        closed=0
        for task in tasks:
            if task["created_at"]+TASK_STATUS_TTL_SECONDS<=now:
                state="expired"
            else:
                try:
                    state=fetcher(trusted,task["task_id"],task["status_path"],secret)
                    if state not in ("running","completed","cancelled"):
                        raise ValueError("invalid remote state")
                except Exception:
                    state="failed"
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                current=con.execute("""SELECT * FROM browser_capture_tasks
                    WHERE task_id=? AND launcher_origin=? AND state='active'""",
                    (task["task_id"],task["launcher_origin"])).fetchone()
                if not current or not self.enabled(con):
                    continue
                if state in ("completed","cancelled","expired"):
                    self._close_task(con,task["task_id"],task["launcher_origin"],state,now)
                    closed+=1
                elif state=="running":
                    con.execute("""UPDATE browser_capture_tasks SET
                        last_success_at=?,last_check_at=?,failures=0
                        WHERE task_id=? AND launcher_origin=?""",
                        (now,now,task["task_id"],task["launcher_origin"]))
                else:
                    failures=current["failures"]+1
                    if failures>=MAX_STATUS_FAILURES:
                        self._close_task(con,task["task_id"],task["launcher_origin"],"status_unavailable",now)
                        closed+=1
                    else:
                        con.execute("""UPDATE browser_capture_tasks SET
                            failures=?,last_check_at=? WHERE task_id=? AND launcher_origin=?""",
                            (failures,now,task["task_id"],task["launcher_origin"]))
        return {"checked":len(tasks),"closed":closed}

    def extension_sessions(self, token: str, session_ids: list[str]):
        """Provide a stop signal even if the flow page is closed or navigated."""
        if len(session_ids)>100:
            raise HTTPException(400,"一次最多查询100个采集会话")
        with self.db.connect() as con:
            self._extension(con,token)
            out={}
            for sid in session_ids:
                if not isinstance(sid,str) or len(sid)>100:
                    continue
                record=con.execute("SELECT status,expires_at FROM browser_capture_sessions WHERE id=?",(sid,)).fetchone()
                if record is None:
                    out[sid]="not_found"
                elif record["status"] in ("armed","capturing") and record["expires_at"]<=time.time():
                    out[sid]="expired"
                else:
                    out[sid]=record["status"]
        return {"sessions":out}

    def bind(self, token: str, session_id: str, tab_id: int, document_id: str, current_url: str):
        if tab_id<0 or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",document_id):
            raise HTTPException(400,"标签页或文档标识无效")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            r=con.execute("SELECT * FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone()
            if not r or r["status"]!="armed" or r["expires_at"]<=time.time():
                raise HTTPException(409,"采集会话已结束或尚未启动")
            key=manual_site(current_url) if r['mode']=='manual' else page_key(current_url)
            if r["page_key"]!=key:
                raise HTTPException(409,"目标网页与有效待采集任务不匹配")
            con.execute("""UPDATE browser_capture_sessions SET tab_id=?,document_id=?,status='capturing'
                WHERE id=?""",(tab_id,document_id,session_id))
        return {"status":"capturing"}

    def event(self, token: str, session_id: str, seq: int, kind: str,
              tab_id: int, document_id: str, current_url: str, label: str=""):
        if kind not in ALLOWED_ACTIONS or seq<1 or seq>1000000:
            raise HTTPException(400,"事件类型或序号无效")
        now=int(time.time())
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            r=con.execute("SELECT * FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone()
            if not r or r["status"]!="capturing" or r["expires_at"]<=now:
                raise HTTPException(409,"采集会话已结束或尚未启动")
            if r["tab_id"]!=tab_id or r["document_id"]!=document_id:
                raise HTTPException(403,"非本次任务绑定的浏览器文档")
            manual=r['mode']=='manual'
            if manual:
                try:
                    allowed=manual_site(current_url)==r['page_key']
                    location=manual_page(current_url) if allowed else '[离开监控网站]'
                except HTTPException:
                    allowed=False
                    location='[离开监控网站]'
                if not allowed and kind not in ('navigation','tab_closed'):
                    raise HTTPException(403,"页面已离开手动授权的网站")
            else:
                location=(page_key(current_url) if kind not in ('navigation','tab_closed')
                          else (page_key(current_url) if origin(current_url) else '[离开 HTTPS 网页]'))
                if kind!='navigation' and location!=r['page_key']:
                    raise HTTPException(403,"页面已离开授权范围")
            if seq==r['last_seq'] and kind not in ('navigation','tab_closed'):
                return {"ack":seq,"status":"duplicate"}
            if seq!=r["last_seq"]+1:
                raise HTTPException(409,"事件序号不连续")
            stopped=kind=='tab_closed' or (kind=='navigation' and
                         (not manual or not allowed))
            status=('tab_closed' if kind=='tab_closed' else 'navigation_stopped') if stopped else 'capturing'
            event_label=('页面导航，继续观察' if manual and kind=='navigation' and allowed
                         else '页面导航，停止观察' if kind=='navigation'
                         else '标签页关闭，停止观察' if kind=='tab_closed'
                         else valid_label(label))
            con.execute("""INSERT INTO browser_capture_events
                (session_id,seq,at,kind,label,location) VALUES(?,?,?,?,?,?)""",
                (session_id,seq,now,kind,event_label,location))
            self._update_step(con,session_id,seq,kind,event_label)
            con.execute("""UPDATE browser_capture_sessions SET
                last_seq=?,event_count=event_count+1,status=?,
                ended_at=CASE WHEN ? THEN ? ELSE ended_at END
                WHERE id=?""",(seq,status,int(stopped),now,session_id))
            if manual and stopped:
                con.execute("""UPDATE browser_capture_settings SET
                    manual_enabled=0,manual_site='',manual_session_id=''
                    WHERE id=1 AND manual_session_id=?""",(session_id,))
        return {"ack":seq,"status":status}

    @staticmethod
    def _update_step(con, session_id: str, seq: int, kind: str, label: str):
        """Bounded deterministic action grouping, not speculative AI semantics."""
        row=con.execute("""SELECT * FROM browser_capture_steps WHERE session_id=?
            ORDER BY step DESC LIMIT 1""",(session_id,)).fetchone()
        if kind in ("click","submit","navigation") or not row or row["status"] in ("completed","stopped"):
            number=(int(row["step"])+1) if row else 1
            action=("点击" if kind=="click" else "提交" if kind=="submit"
                    else "导航" if kind=="navigation" else "页面事件")
            status=("completed" if kind=="navigation" and label=="页面导航，继续观察"
                    else "stopped" if kind in ("navigation","tab_closed")
                    else "ongoing")
            con.execute("""INSERT INTO browser_capture_steps
                (session_id,step,start_seq,end_seq,action,status,label)
                VALUES(?,?,?,?,?,?,?)""",(session_id,number,seq,seq,action,status,label))
        else:
            status=("stopped" if kind in ("navigation","tab_closed") else
                    "completed" if kind=="feedback" else "ongoing")
            con.execute("""UPDATE browser_capture_steps SET end_seq=?,status=?
                WHERE session_id=? AND step=?""",(seq,status,session_id,row["step"]))

    def steps(self, session_id: str, limit: int=100):
        with self.db.connect() as con:
            return [dict(x) for x in con.execute("""SELECT step,start_seq,end_seq,
                action,status,label FROM browser_capture_steps
                WHERE session_id=? ORDER BY step LIMIT ?""",(session_id,limit))]

    def recent(self, limit: int=30, offset: int=0):
        with self.db.connect() as con:
            return [dict(x) for x in con.execute("""SELECT id,task_id,page_key,mode,status,
                event_count,created_at,ended_at FROM browser_capture_sessions
                ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?""",(limit,offset))]

    def events(self, session_id: str, limit: int=100, offset: int=0):
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone():
                raise HTTPException(404,"采集会话不存在")
            return [dict(x) for x in con.execute("""SELECT seq,at,kind,label,location
                FROM browser_capture_events WHERE session_id=?
                ORDER BY seq LIMIT ? OFFSET ?""",(session_id,limit,offset))]
