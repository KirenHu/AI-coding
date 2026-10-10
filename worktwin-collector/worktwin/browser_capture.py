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


class BrowserCapture:
    def __init__(self, db):
        self.db = db
        with db.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS browser_capture_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL DEFAULT 0);
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
                    status TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, ended_at INTEGER, last_seq INTEGER NOT NULL DEFAULT 0,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id, launcher_origin));
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
            """)

    def enabled(self, con) -> bool:
        return bool(con.execute("SELECT enabled FROM browser_capture_settings WHERE id=1").fetchone()[0])

    def settings(self) -> dict:
        now = int(time.time())
        with self.db.connect() as con:
            client = con.execute("SELECT last_seen FROM browser_extension_clients WHERE id=1").fetchone()
            sessions = con.execute("""SELECT count(*) FROM browser_capture_sessions
                WHERE status IN ('armed','capturing','navigation_stopped') AND expires_at>?""", (now,)).fetchone()[0]
            return {"enabled":self.enabled(con),
                    "extension_connected":bool(client and now-int(client["last_seen"])<=90),
                    "active_tasks":sessions,
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

    def _command(self, con, envelope: dict, sender_origin: str) -> tuple[str,str,str]:
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
        if (action not in ("start","complete") or not isinstance(task_id,str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}",task_id)
            or not isinstance(target,str) or len(target)>2048
            or not isinstance(nonce,str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",nonce)
            or not isinstance(expires,int) or not isinstance(signature,str)):
            raise HTTPException(400,"触发信号结构无效")
        now=int(time.time())
        if expires<now or expires>now+120:
            raise HTTPException(403,"触发信号已过期")
        signed="\n".join([action,task_id,target,nonce,str(expires),sender_origin])
        actual=hmac.new(secret.encode(),signed.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual,signature):
            raise HTTPException(403,"流程平台签名无效")
        nonce_hash=hashlib.sha256((sender_origin+":"+nonce).encode()).hexdigest()
        con.execute("DELETE FROM browser_capture_nonces WHERE seen_at<?",(now-600,))
        try:
            con.execute("INSERT INTO browser_capture_nonces(nonce_hash,seen_at) VALUES(?,?)",(nonce_hash,now))
        except Exception as exc:
            raise HTTPException(409,"已处理的触发信号不可重复使用") from exc
        return action,task_id,page_key(target)

    def command(self, token: str, sender_origin: str, envelope: dict):
        now=int(time.time())
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            action,task_id,target=self._command(con,envelope,sender_origin)
            existing=con.execute("""SELECT * FROM browser_capture_sessions
                WHERE task_id=? AND launcher_origin=?""",(task_id,sender_origin)).fetchone()
            if action=="complete":
                if not existing:
                    return {"status":"not_found"}
                if existing["page_key"]!=target:
                    raise HTTPException(409,"任务目标不匹配")
                con.execute("""UPDATE browser_capture_sessions SET status='completed',ended_at=?
                    WHERE id=? AND status NOT IN ('completed','disabled')""",(now,existing["id"]))
                return {"status":"completed","session_id":existing["id"]}
            if existing:
                raise HTTPException(409,"同一任务不得再次扩展监控范围或重新激活")
            session_id=secrets.token_urlsafe(20)
            con.execute("""INSERT INTO browser_capture_sessions
                (id,task_id,page_key,launcher_origin,status,created_at,expires_at)
                VALUES(?,?,?,?,'armed',?,?)""",
                (session_id,task_id,target,sender_origin,now,now+SESSION_TTL_SECONDS))
            return {"status":"armed","session_id":session_id,"target_page":target}

    def bind(self, token: str, session_id: str, tab_id: int, document_id: str, current_url: str):
        if tab_id<0 or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",document_id):
            raise HTTPException(400,"标签页或文档标识无效")
        key=page_key(current_url)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            r=con.execute("SELECT * FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone()
            if not r or r["status"]!="armed" or r["expires_at"]<=time.time() or r["page_key"]!=key:
                raise HTTPException(409,"目标网页与有效待采集任务不匹配")
            con.execute("""UPDATE browser_capture_sessions SET tab_id=?,document_id=?,status='capturing'
                WHERE id=?""",(tab_id,document_id,session_id))
        return {"status":"capturing"}

    def event(self, token: str, session_id: str, seq: int, kind: str,
              tab_id: int, document_id: str, current_url: str, label: str=""):
        if kind not in ALLOWED_ACTIONS or seq<1 or seq>1000000:
            raise HTTPException(400,"事件类型或序号无效")
        now=int(time.time())
        key=(page_key(current_url) if kind not in ("navigation","tab_closed") else
             (page_key(current_url) if origin(current_url) else "[离开 HTTPS 网页]"))
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._extension(con,token)
            r=con.execute("SELECT * FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone()
            if not r or r["status"]!="capturing" or r["expires_at"]<=now:
                raise HTTPException(409,"采集会话已结束或尚未启动")
            if r["tab_id"]!=tab_id or r["document_id"]!=document_id:
                raise HTTPException(403,"非本次任务绑定的浏览器文档")
            if kind!="navigation" and key!=r["page_key"]:
                raise HTTPException(403,"页面已离开授权范围")
            if kind in ("navigation","tab_closed"):
                if seq!=r["last_seq"]+1:
                    raise HTTPException(409,"事件序号不连续")
                con.execute("""INSERT INTO browser_capture_events
                    (session_id,seq,at,kind,label,location) VALUES(?,?,?,?,?,?)""",
                    (session_id,seq,now,kind,"页面导航，停止观察" if kind=="navigation" else "标签页关闭，停止观察",key))
                self._update_step(con,session_id,seq,kind,"")
                con.execute("""UPDATE browser_capture_sessions SET
                    last_seq=?,event_count=event_count+1,status=?,ended_at=?
                    WHERE id=?""",(seq,"navigation_stopped" if kind=="navigation" else "tab_closed",now,session_id))
                return {"ack":seq,"status":"navigation_stopped" if kind=="navigation" else "tab_closed"}
            if seq==r["last_seq"]:
                return {"ack":seq,"status":"duplicate"}
            if seq!=r["last_seq"]+1:
                raise HTTPException(409,"事件序号不连续")
            con.execute("""INSERT INTO browser_capture_events
                (session_id,seq,at,kind,label,location) VALUES(?,?,?,?,?,?)""",
                (session_id,seq,now,kind,valid_label(label),key))
            self._update_step(con,session_id,seq,kind,valid_label(label))
            con.execute("""UPDATE browser_capture_sessions SET
                last_seq=?,event_count=event_count+1 WHERE id=?""",(seq,session_id))
        return {"ack":seq,"status":"capturing"}

    @staticmethod
    def _update_step(con, session_id: str, seq: int, kind: str, label: str):
        """Bounded deterministic action grouping, not speculative AI semantics."""
        row=con.execute("""SELECT * FROM browser_capture_steps WHERE session_id=?
            ORDER BY step DESC LIMIT 1""",(session_id,)).fetchone()
        if kind in ("click","submit") or not row or row["status"] in ("completed","stopped"):
            number=(int(row["step"])+1) if row else 1
            action="点击" if kind=="click" else "提交" if kind=="submit" else "页面事件"
            status="ongoing" if kind not in ("navigation","tab_closed") else "stopped"
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

    def recent(self, limit: int=30):
        with self.db.connect() as con:
            return [dict(x) for x in con.execute("""SELECT id,task_id,page_key,status,
                event_count,created_at,ended_at FROM browser_capture_sessions
                ORDER BY created_at DESC LIMIT ?""",(limit,))]
