"""Task-triggered, opt-in browser observation for WorkTwin.

The browser extension is a *transport*, not a trusted workflow authority.
Only short-lived Ed25519-signed tickets from provisioned workflow issuers can
open/complete a capture. A ticket scopes exactly one initial page; navigation
terminates observation but does not mark the business task complete.

Browser observations are NOT user statements, verified business outcomes, or
current WorkTwin knowledge. No hidden browser recording or implicit AI consent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from .db import Database

# Fail closed unless enterprise deployment explicitly provisions issuer keys.
ISSUERS_ENV = "WORKTWIN_CAPTURE_TRUSTED_PLATFORMS"
SIGNAL_TTL = 120
MAX_CAPTURE_AGE = 8 * 60 * 60
EXTENSION_ALIVE_SECONDS = 90
EVENT_KINDS = {"click", "change", "submit", "feedback", "navigation_intent"}
SAFE_EVENT_FIELDS = {"tag", "role", "label", "field_type", "status", "target_path"}
SAFE_FIELD = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
SAFE_NONCE = re.compile(r"^[A-Za-z0-9_-]{12,128}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
EMAIL = re.compile(r"(?<![\w])[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}(?![\w])")
PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{9,}\d)(?!\d)")
SECRETS = re.compile(r"(?i)\b(?:bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;]{6,}")
LONG_TOKEN = re.compile(r"(?<![\w])[A-Za-z0-9_-]{40,}(?![\w])")


class CaptureRejected(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _unbase64(encoded: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise CaptureRejected("签名格式无效")
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise CaptureRejected("签名格式无效") from exc


def origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.fragment or parts.query or parts.path not in ("", "/")):
            raise ValueError()
        if parts.hostname.endswith(".") or parts.hostname in ("localhost", "127.0.0.1"):
            raise ValueError()
        return f"https://{parts.netloc.lower()}"
    except (ValueError, TypeError, AttributeError) as exc:
        raise CaptureRejected("流程平台来源必须为可信 HTTPS 域名") from exc


def page_path(value: str) -> str:
    """A single initial document path, never an origin-wide runtime whitelist.

    Queries/fragments are intentionally omitted from stored evidence because
    real business URLs frequently carry tokens or personal information.
    """
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.hostname.endswith(".") or url.hostname in ("localhost", "127.0.0.1")
                or len(value) > 2048):
            raise ValueError()
        path = url.path or "/"
        return urlunsplit((url.scheme, url.netloc.lower(), path, "", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CaptureRejected("仅支持真实 HTTPS 业务页面") from exc


def mask(value: object, limit: int = 140) -> str:
    """Defensive second layer; the extension must not transmit input values."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()[:limit]
    text = SECRETS.sub("[敏感内容]", text)
    text = EMAIL.sub("[邮箱]", text)
    text = PHONE.sub("[数字信息]", text)
    text = LONG_TOKEN.sub("[长标识]", text)
    return text[:limit]


def _sanitize(kind: str, details: object) -> dict[str, str]:
    if kind not in EVENT_KINDS or not isinstance(details, dict):
        raise CaptureRejected("不支持的 DOM 事件")
    if "value" in details or "html" in details or "input" in details or "password" in details:
        raise CaptureRejected("不能上传表单输入值或 DOM 原文")
    result: dict[str, str] = {}
    for field in SAFE_EVENT_FIELDS:
        value = details.get(field)
        if not isinstance(value, str):
            continue
        if field == "target_path":
            result[field] = page_path(value)
        elif field in ("tag", "role", "field_type"):
            safe = value.strip().lower()
            if SAFE_FIELD.fullmatch(safe):
                result[field] = safe
        else:
            result[field] = mask(value, 100 if field == "label" else 60)
    return result


def _description(kind: str, fields: dict) -> str:
    label = fields.get("label") or fields.get("role") or fields.get("tag") or "未命名元素"
    if kind == "click":
        return f"点击「{label}」"
    if kind == "change":
        return f"修改表单控件（{fields.get('field_type', '类型未知')}；不记录输入内容）"
    if kind == "submit":
        return "提交表单（未验证后台结果）"
    if kind == "feedback":
        return f"页面显示反馈：「{label}」（仅为页面提示）"
    return f"点击跳转链接，目标：{fields.get('target_path','未识别')}"


class BrowserCapture:
    def __init__(self, db: Database, *, issuer_keys: dict[str, str] | None = None):
        self.db = db
        configured = issuer_keys
        if configured is None:
            try:
                configured = json.loads(os.getenv(ISSUERS_ENV, "{}"))
            except (ValueError, TypeError) as exc:
                raise ValueError("流程平台公钥配置不是合法 JSON") from exc
        if not isinstance(configured, dict):
            raise ValueError("流程平台公钥配置必须为域名到公钥的映射")
        self.issuers: dict[str, Ed25519PublicKey] = {}
        for name, encoded in configured.items():
            trusted = origin(str(name))
            try:
                pub = Ed25519PublicKey.from_public_bytes(_unbase64(str(encoded)))
            except (ValueError, CaptureRejected) as exc:
                raise ValueError("流程平台 Ed25519 公钥无效") from exc
            self.issuers[trusted] = pub

    @property
    def enabled(self) -> bool:
        return self.db.setting("browser_capture_enabled", "0") == "1"

    @property
    def ai_allowed(self) -> bool:
        return self.db.setting("browser_capture_ai_allowed", "0") == "1"

    def _expire(self, con: sqlite3.Connection) -> None:
        now = int(time.time())
        con.execute("""UPDATE browser_capture_sessions
            SET task_state='expired',capture_state='expired',stopped_at=COALESCE(stopped_at,?)
            WHERE task_state='open' AND expires_at<?""", (now, now))

    def _enabled(self):
        if not self.enabled:
            raise CaptureRejected("任务触发式浏览器采集尚未由用户开启", 403)

    def settings(self) -> dict:
        now = int(time.time())
        last = int(self.db.setting("browser_capture_extension_seen", "0") or "0")
        with self.db.connect() as con:
            self._expire(con)
            sessions = [dict(row) for row in con.execute("""SELECT id,task_id,goal,target_path,
                task_state,capture_state,created_at,started_at,stopped_at,last_seq,
                analysis_seq,analysis_text,analysis_status,event_gaps
                FROM browser_capture_sessions ORDER BY created_at DESC LIMIT 15""")]
        return {
            "enabled": self.enabled,
            "allow_ai": self.ai_allowed,
            "extension_connected": self.enabled and bool(last) and 0 <= now - last < EXTENSION_ALIVE_SECONDS,
            "extension_browser": self.db.setting("browser_capture_extension_browser", ""),
            "extension_version": self.db.setting("browser_capture_extension_version", ""),
            "extension_paired": bool(self.db.setting("browser_capture_extension_hash")),
            "trusted_platform_count": len(self.issuers),
            "sessions": sessions,
            # A handshake proves activity in a compatible browser, not that
            # the browser is the OS default.
            "default_browser_extension_verified": False,
            "default_browser_detection": "需要在实际使用的 Chrome/Edge 中连接插件；操作系统无法通用核对插件安装情况",
        }

    def configure(self, enabled: bool, allow_ai: bool = False) -> dict:
        with self.db.connect() as con:
            con.execute("""INSERT INTO settings(key,value) VALUES('browser_capture_enabled',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if enabled else "0",))
            con.execute("""INSERT INTO settings(key,value) VALUES('browser_capture_ai_allowed',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if enabled and allow_ai else "0",))
            if not enabled:
                now = int(time.time())
                con.execute("""UPDATE browser_capture_sessions SET task_state='cancelled',
                    capture_state='disabled',stopped_at=COALESCE(stopped_at,?)
                    WHERE task_state='open'""", (now,))
        return self.settings()

    def pair(self) -> str:
        self._enabled()
        token = secrets.token_urlsafe(32)
        # Rotating the token invalidates every previously paired extension.
        self.db.set_setting("browser_capture_extension_hash", hashlib.sha256(token.encode()).hexdigest())
        self.db.set_setting("browser_capture_extension_seen", "0")
        return token

    def extension(self, token: str) -> None:
        self._enabled()
        expected = self.db.setting("browser_capture_extension_hash")
        actual = hashlib.sha256((token or "").encode()).hexdigest()
        if not expected or not secrets.compare_digest(actual, expected):
            raise CaptureRejected("浏览器插件未配对或密钥已失效", 401)

    def heartbeat(self, token: str, browser: str, version: str) -> dict:
        self.extension(token)
        name = browser if browser in ("Chrome", "Edge", "Chromium") else "Chromium"
        with self.db.connect() as con:
            for key, value in [
                ("browser_capture_extension_seen", str(int(time.time()))),
                ("browser_capture_extension_browser", name),
                ("browser_capture_extension_version", mask(version, 24)),
            ]:
                con.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
        return {"connected": True, "enabled": True}

    def _ticket(self, ticket: str, source_origin: str) -> dict:
        if not isinstance(ticket, str) or len(ticket) > 4500:
            raise CaptureRejected("流程信号格式无效")
        parts = ticket.split(".")
        if len(parts) != 2:
            raise CaptureRejected("流程信号缺少数字签名")
        try:
            raw = _unbase64(parts[0])
            payload = json.loads(raw)
            signature = _unbase64(parts[1])
        except (ValueError, TypeError, UnicodeError) as exc:
            raise CaptureRejected("无法解析流程信号") from exc
        if not isinstance(payload, dict):
            raise CaptureRejected("流程信号内容无效")
        issuer = origin(str(payload.get("iss") or ""))
        if issuer not in self.issuers or issuer != origin(source_origin):
            raise CaptureRejected("非可信流程平台，拒绝浏览器采集", 403)
        try:
            self.issuers[issuer].verify(signature, raw)
        except InvalidSignature as exc:
            raise CaptureRejected("流程信号签名无效", 403) from exc
        try:
            issued = int(payload["iat"])
            expiry = int(payload["exp"])
        except (KeyError, ValueError, TypeError) as exc:
            raise CaptureRejected("流程信号缺少有效期") from exc
        now = int(time.time())
        if issued > now + 15 or issued < now - SIGNAL_TTL or expiry < now or expiry - issued > SIGNAL_TTL:
            raise CaptureRejected("流程信号已过期或时间无效", 403)
        for field in ("nonce", "task_id", "subject", "request_id"):
            value = payload.get(field)
            if not isinstance(value, str) or not (SAFE_NONCE.fullmatch(value) if field == "nonce" else SAFE_ID.fullmatch(value)):
                raise CaptureRejected("流程信号缺少可靠的任务身份")
        if payload.get("action") not in ("capture.start", "capture.complete"):
            raise CaptureRejected("未知流程信号动作")
        payload["iss"] = issuer
        return payload

    def signal(self, token: str, ticket: str, source_origin: str) -> dict:
        self.extension(token)
        p = self._ticket(ticket, source_origin)
        task = p["task_id"]
        now = int(time.time())
        target = page_path(p.get("target_url", "")) if p["action"] == "capture.start" else None
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire(con)
            try:
                con.execute("""INSERT INTO browser_capture_signals(issuer,nonce,received_at)
                    VALUES(?,?,?)""", (p["iss"], p["nonce"], now))
            except sqlite3.IntegrityError as exc:
                raise CaptureRejected("重复的流程信号已拒绝", 409) from exc
            if p["action"] == "capture.complete":
                matches = con.execute("""SELECT id FROM browser_capture_sessions
                    WHERE issuer=? AND task_id=? AND subject=? AND task_state='open'""",
                    (p["iss"], task, p["subject"])).fetchall()
                con.execute("""UPDATE browser_capture_sessions
                    SET task_state='completed',
                      capture_state=CASE WHEN capture_state IN ('recording','pending') THEN 'completed' ELSE capture_state END,
                      stopped_at=COALESCE(stopped_at,?)
                    WHERE issuer=? AND task_id=? AND subject=? AND task_state='open'""",
                    (now, p["iss"], task, p["subject"]))
                return {"action": "complete", "task_id": task, "closed_sessions": [r["id"] for r in matches]}
            capture_id = secrets.token_hex(16)
            con.execute("""INSERT INTO browser_capture_sessions
                (id,issuer,task_id,subject,request_id,workflow_project_id,goal,
                 target_path,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (capture_id, p["iss"], task, p["subject"], p["request_id"],
                 mask(p.get("project_id", ""), 100), mask(p.get("goal", ""), 240),
                 target, now, now + MAX_CAPTURE_AGE))
            return {"action": "start", "session_id": capture_id,
                    "target_path": target, "task_id": task, "expires_at": now + MAX_CAPTURE_AGE}

    def bind(self, token: str, session_id: str, tab_id: int,
             document_id: str, page_url: str) -> dict:
        self.extension(token)
        canonical = page_path(page_url)
        if not (0 <= tab_id <= 2147483647 and 1 <= len(document_id) <= 128):
            raise CaptureRejected("浏览器标签页或文档标识无效")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire(con)
            s = con.execute("""SELECT * FROM browser_capture_sessions WHERE id=?""",(session_id,)).fetchone()
            if not s or s["task_state"] != "open" or s["capture_state"] != "pending":
                raise CaptureRejected("此采集会话已结束或不可绑定", 409)
            if s["target_path"] != canonical:
                raise CaptureRejected("目标网页不匹配，禁止监控其他页面", 403)
            con.execute("""UPDATE browser_capture_sessions SET capture_state='recording',
                bound_tab_id=?,bound_document_id=?,started_at=? WHERE id=?""",
                (tab_id, document_id, int(time.time()), session_id))
            return {"recording": True, "session_id": session_id, "target_path": canonical}

    def events(self, token: str, session_id: str, tab_id: int, document_id: str,
               page_url: str, events: list[dict]) -> dict:
        self.extension(token)
        if not isinstance(events, list) or not 1 <= len(events) <= 40:
            raise CaptureRejected("每批事件数量应为 1–40")
        canonical = page_path(page_url)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._expire(con)
            s = con.execute("""SELECT * FROM browser_capture_sessions WHERE id=?""",(session_id,)).fetchone()
            if not s or s["task_state"] != "open" or s["capture_state"] != "recording":
                raise CaptureRejected("任务或当前页面未处于采集状态", 409)
            if s["bound_tab_id"] != tab_id or s["bound_document_id"] != document_id or s["target_path"] != canonical:
                raise CaptureRejected("事件来自非授权标签页或页面", 403)
            last = s["last_seq"]
            gap = 0
            accepted = 0
            for event in events:
                if not isinstance(event, dict):
                    raise CaptureRejected("事件格式无效")
                try:
                    seq = int(event["seq"])
                except (ValueError, TypeError, KeyError) as exc:
                    raise CaptureRejected("事件缺少递增序号") from exc
                if seq <= last:
                    continue  # A retransmitted delivery is idempotent.
                if seq > 50000 or seq - last > 1000:
                    raise CaptureRejected("异常事件序号，采集已拒绝")
                kind = event.get("kind")
                fields = _sanitize(kind, event.get("details"))
                occurred_at = event.get("occurred_at")
                now_ms = int(time.time() * 1000)
                if not isinstance(occurred_at, int) or abs(now_ms - occurred_at) > 120000:
                    occurred_at = now_ms
                gap += max(0, seq - last - 1)
                result = con.execute("""INSERT INTO browser_capture_events
                    (session_id,seq,kind,occurred_at,payload_json)
                    VALUES(?,?,?,?,?)""",
                    (session_id, seq, kind, occurred_at, json.dumps(fields, ensure_ascii=False)))
                step_index = con.execute("SELECT count(*) FROM browser_capture_steps WHERE session_id=?",
                                         (session_id,)).fetchone()[0] + 1
                con.execute("""INSERT INTO browser_capture_steps(session_id,event_id,step_index,summary)
                    VALUES(?,?,?,?)""", (session_id, result.lastrowid, step_index, _description(kind, fields)))
                accepted += 1
                last = seq
            if accepted:
                con.execute("""UPDATE browser_capture_sessions SET last_seq=?,event_gaps=event_gaps+?
                    WHERE id=?""", (last, gap, session_id))
            return {"accepted": accepted, "last_seq": last, "event_gaps": gap}

    def navigation(self, token: str, session_id: str, tab_id: int,
                   document_id: str, to_url: str, reason: str) -> dict:
        self.extension(token)
        target = page_path(to_url) if to_url.startswith("https://") else "[不可采集页面]"
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            s = con.execute("SELECT * FROM browser_capture_sessions WHERE id=?", (session_id,)).fetchone()
            if not s or s["task_state"] != "open" or s["capture_state"] != "recording":
                raise CaptureRejected("采集已停止", 409)
            if s["bound_tab_id"] != tab_id or s["bound_document_id"] != document_id:
                raise CaptureRejected("仅已绑定网页可终止其监控", 403)
            seq = s["last_seq"] + 1
            result = con.execute("""INSERT INTO browser_capture_events
                (session_id,seq,kind,occurred_at,payload_json) VALUES(?,?,?,?,?)""",
                (session_id, seq, "navigation", int(time.time()*1000),
                 json.dumps({"target_path":target, "reason":mask(reason,40)},ensure_ascii=False)))
            step_index = con.execute("SELECT count(*) FROM browser_capture_steps WHERE session_id=?",
                                     (session_id,)).fetchone()[0] + 1
            con.execute("""INSERT INTO browser_capture_steps(session_id,event_id,step_index,summary)
                VALUES(?,?,?,?)""", (session_id, result.lastrowid, step_index,
                                      f"页面跳转至 {target}；新页面不在本次采集范围内"))
            con.execute("""UPDATE browser_capture_sessions SET capture_state='navigated',
                navigation_to=?,stopped_at=?,last_seq=? WHERE id=?""",
                (target, int(time.time()), seq, session_id))
            return {"capture_state": "navigated", "task_state": "open"}

    def end_observation(self, token: str, session_id: str, tab_id: int,
                        document_id: str, reason: str) -> dict:
        self.extension(token)
        with self.db.connect() as con:
            row = con.execute("""UPDATE browser_capture_sessions
                SET capture_state=?,stopped_at=? WHERE id=? AND bound_tab_id=?
                    AND bound_document_id=? AND task_state='open' AND capture_state='recording'""",
                ("tab_closed" if reason == "tab_closed" else "stopped",
                 int(time.time()), session_id, tab_id, document_id))
            if not row.rowcount:
                raise CaptureRejected("采集已停止或标签页不匹配", 409)
        return {"capture_state": "tab_closed" if reason == "tab_closed" else "stopped", "task_state": "open"}

    def trace(self, session_id: str) -> dict:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM browser_capture_sessions WHERE id=?",(session_id,)).fetchone()
            if row is None:
                raise CaptureRejected("没有该任务采集记录", 404)
            steps = [dict(s) for s in con.execute("""SELECT step_index,summary,evidence_kind
                FROM browser_capture_steps WHERE session_id=? ORDER BY step_index LIMIT 1500""",
                (session_id,))]
            return {"session": {k:row[k] for k in
                ("id","task_id","goal","target_path","task_state","capture_state",
                 "event_gaps","analysis_text","analysis_status")},
                    "steps": steps}
