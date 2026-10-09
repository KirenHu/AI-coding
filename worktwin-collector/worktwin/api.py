"""Loopback-only API and local knowledge workbench."""

from __future__ import annotations

import io
import hashlib
import os
import json
import re
import zipfile
import secrets
import subprocess
import sys
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .collector import Collector
from .config import database_path, codex_sessions_path, claude_projects_path
from .db import Database
from .knowledge import KIND_LABELS
from .relations import related_knowledge, export_wiki_archive, WIKILINK
from .inference import GatewayClient
from .credentials import DesktopSecrets
from .model_settings import ModelInput, ModelListInput, PersonalModel, ModelRuntime
from .knowledge_policy import READY_SQL, SHARE_SQL, unavailable_reason
from .answers import answer_from_knowledge
from .publishing import Publisher, PublishingClient
from . import __version__
from .jobs import KnowledgeWorker
from .search import search
from .reconcile import review_flags, resolve_proposal
from .answer_policy import check_answer

STATIC = Path(__file__).parent / "static"


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=90)
    kind: Literal["folder", "codex", "claude"] = "folder"
    root: str = Field(min_length=1)
    allow_ai: bool = False
    allow_share: bool = False


class KnowledgeInput(BaseModel):
    title: str = Field(min_length=1, max_length=130)
    body: str = Field(min_length=1, max_length=50000)
    kind: Literal["fact", "decision", "process", "preference"] = "fact"
    status: Literal["draft", "confirmed", "archived"] = "draft"
    document_id: int | None = None
    quote: str | None = None


class SourcePermissionInput(BaseModel):
    allow_ai: bool


class SharePermissionInput(BaseModel):
    allow_share: bool


class ShareInput(BaseModel):
    recipient: str = Field(min_length=1, max_length=90)
    days: int = Field(default=7, ge=1, le=90)


class ConnectionInput(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=16, max_length=500)


class TwinInput(BaseModel):
    name: str = Field(min_length=1, max_length=90)
    description: str = Field(default="", max_length=500)
    knowledge_ids: list[int] | None = Field(default=None,max_length=1500)


class TwinKnowledgeInput(BaseModel):
    knowledge_ids: list[int] = Field(default_factory=list, max_length=1500)


class AskInput(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    scope: Literal['local','published'] = 'local'


class EditionInput(BaseModel):
    edition: Literal['personal','enterprise']


def create_app(path: Path | None = None, *, start_worker: bool = True, interval: int = 20,
               inference_client: GatewayClient | None = None, publishing_client=None, secret_store=None) -> FastAPI:
    db = Database(path or database_path())
    collector = Collector(db, interval=interval)
    saved_url = db.setting("enterprise_url") or os.getenv('WORKTWIN_SERVER_URL', '')
    secure = secret_store or DesktopSecrets(db.path)
    storage_error = ''
    try:
        saved_token = secure.get('enterprise_token') or db.setting('enterprise_token') or os.getenv('WORKTWIN_SERVER_TOKEN','')
        if db.setting('enterprise_token'):
            secure.set('enterprise_token', saved_token)
            db.set_setting('enterprise_token','')
    except Exception:
        saved_token = os.getenv('WORKTWIN_SERVER_TOKEN','')
        storage_error = '系统凭据存储不可用，请解锁后重新连接企业服务'
    share_active=db.setting('share_connection_enabled','1')=='1'
    enterprise = GatewayClient(url=saved_url,token=saved_token) if saved_url and share_active else GatewayClient(url='',token='')
    model_client = ModelRuntime(db,secure,enterprise=enterprise,injected=inference_client)
    publisher = Publisher(db, client=publishing_client or (PublishingClient(url=saved_url,token=saved_token) if saved_url and share_active else PublishingClient(url='',token='')))
    knowledge_worker = KnowledgeWorker(db, client=model_client, interval=max(interval, 3))
    local_token = secrets.token_urlsafe(32)
    # Each build uses a different resource URL, so a browser that has cached
    # the previous app cannot execute its script against the upgraded HTML.
    asset_version = hashlib.sha256(
        (STATIC / 'app.js').read_bytes() + (STATIC / 'styles.css').read_bytes()
    ).hexdigest()[:16]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            collector.start()
            knowledge_worker.start()
            publisher.start()
        yield
        publisher.stop()
        collector.stop()
        knowledge_worker.stop()

    app = FastAPI(title="WorkTwin Collector", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    # A malicious website must not be able to access personal documents via
    # DNS rebinding or unauthenticated cross-origin browser requests.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_api_guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            token = request.headers.get("X-Worktwin-Token", "")
            if not secrets.compare_digest(token, local_token):
                return JSONResponse({"detail": "缺少本地授权令牌"}, status_code=403)
        response = await call_next(request)
        if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/") and response.status_code < 400 and publisher.client.configured:
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(publisher.sync)
        # The dashboard contains a session token. Do not allow it to be framed
        # by another website, cached in browser history, or sent as a referrer.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith(("/api/", "/assets/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.publisher = publisher
    app.state.db = db
    app.state.collector = collector
    app.state.knowledge_worker = knowledge_worker
    app.state.model = model_client
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    def authorized(x_worktwin_token: str | None = Header(default=None)):
        if not x_worktwin_token or not secrets.compare_digest(x_worktwin_token, local_token):
            raise HTTPException(status_code=403, detail="本地授权令牌缺失")

    def launch_scan():
        if start_worker:
            collector.schedule()
            knowledge_worker.schedule()
        # Test mode scans explicitly through collector.scan_all().

    @app.get("/", response_class=HTMLResponse)
    def home():
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace('href="/assets/styles.css"', f'href="/assets/styles.css?v={asset_version}"')
        html = html.replace('src="/assets/app.js"', f'src="/assets/app.js?v={asset_version}"')
        response = HTMLResponse(html.replace("__LOCAL_TOKEN_VALUE__", local_token))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "WorkTwin Collector", "cloud_sync": publisher.client.configured, "version": __version__,
                "enterprise_model": model_client.configured}

    @app.post('/api/shutdown', dependencies=[Depends(authorized)])
    def shutdown():
        callback=getattr(app.state,'shutdown_callback',None)
        if callback:
            import threading
            threading.Timer(.3,callback).start()
        return {'stopping':bool(callback)}

    @app.get("/api/stats")
    def stats():
        with db.connect() as con:
            def count(sql):
                return int(con.execute(sql).fetchone()[0])
            events = [dict(r) for r in con.execute("""SELECT e.action,e.details,e.at,s.name source_name FROM events e LEFT JOIN sources s ON s.id=e.source_id ORDER BY e.id DESC LIMIT 12""")]
            return {"sources":count("SELECT count(*) FROM sources"),
                    "documents":count("SELECT count(*) FROM documents"),
                    "knowledge":count("SELECT count(*) FROM knowledge WHERE status!='archived'"),
                    "confirmed":count("SELECT count(*) FROM knowledge WHERE status='confirmed'"),
                    "needs_review":count("SELECT count(*) FROM knowledge WHERE needs_review=1 AND status!='archived'"),
                    "pending_proposals":count("SELECT count(*) FROM knowledge_proposals WHERE status='pending'"),
                    "chunks":count("SELECT count(*) FROM chunks"),
                    "events":events,"is_scanning":collector.is_scanning,
                    "last_scan":collector.last_scan,"last_result":collector.last_result,
                    "local_only":True,
                    "model_configured":model_client.configured,
                    "ai_jobs":{state:count(f"SELECT count(*) FROM ai_jobs WHERE state='{state}'")
                               for state in ('queued','running','done','error')}}

    @app.get("/api/sources")
    def sources():
        with db.connect() as con:
            return [dict(r) for r in con.execute("""
                SELECT s.*, COALESCE(s.adapter,s.kind) AS source_type, COUNT(d.id) AS document_count FROM sources s
                LEFT JOIN documents d ON d.source_id=s.id
                GROUP BY s.id ORDER BY s.id DESC""")]

    @app.post("/api/sources", dependencies=[Depends(authorized)])
    def create_source(payload: SourceInput):
        root = Path(payload.root).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(400, "所选文件夹不存在或无法访问")
        if payload.kind in ("codex", "claude") and root == Path.home():
            raise HTTPException(400, "请选择 Codex sessions 目录，而不是整个主目录")
        with db.connect() as con:
            if con.execute("SELECT id FROM sources WHERE root=?",(str(root),)).fetchone():
                raise HTTPException(409, "这个目录已添加")
            row = con.execute("INSERT INTO sources(name,kind,adapter,root,allow_ai,allow_share) VALUES(?,?,?,?,?,?)",
                              (payload.name, "folder" if payload.kind == "claude" else payload.kind,
                               payload.kind, str(root), int(payload.allow_ai), int(payload.allow_share)))
            source_id = int(row.lastrowid)
        db.event("source_added", f"已授权：{payload.name}",source_id)
        launch_scan()
        return {"id":source_id,"root":str(root),"message":"数据源已添加，正在建立索引"}

    @app.get("/api/default-paths")
    def default_paths():
        p = codex_sessions_path()
        claude = claude_projects_path()
        return {"codex": str(p), "codex_exists":p.is_dir(),
                "claude":str(claude), "claude_exists":claude.is_dir(), "home":str(Path.home())}

    @app.post("/api/pick-folder", dependencies=[Depends(authorized)])
    def pick_folder():
        """Open a system-owned directory chooser; never silently select a path."""
        try:
            if sys.platform == "darwin":
                out = subprocess.run(["osascript","-e",'POSIX path of (choose folder with prompt "请选择允许 WorkTwin 读取的文件夹")'],
                                     capture_output=True,text=True,timeout=120)
                if out.returncode:
                    raise ValueError("选择已取消")
                selected = out.stdout.strip()
            elif sys.platform == "win32":
                # Frozen sys.executable is the app itself, not a Python interpreter.
                code = ('[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;'
                        'Add-Type -AssemblyName System.Windows.Forms;'
                        '$picker=New-Object System.Windows.Forms.FolderBrowserDialog;'
                        '$picker.Description="选择授权文件夹";'
                        'if($picker.ShowDialog() -eq "OK"){[Console]::WriteLine($picker.SelectedPath)}')
                out = subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',code],
                                     capture_output=True,text=True,encoding='utf-8',timeout=120)
                selected = out.stdout.strip()
            else:
                out = subprocess.run(["zenity","--file-selection","--directory", "--title=选择授权文件夹"],
                                     capture_output=True,text=True,timeout=120)
                selected = out.stdout.strip()
            if not selected or not Path(selected).is_dir():
                raise ValueError("未选择有效目录")
            return {"path":str(Path(selected).resolve())}
        except (ValueError,FileNotFoundError,subprocess.TimeoutExpired) as exc:
            raise HTTPException(400, f"无法选择目录：{exc}") from exc

    @app.post("/api/sources/{source_id}/toggle", dependencies=[Depends(authorized)])
    def toggle_source(source_id: int):
        with db.connect() as con:
            row = con.execute("SELECT enabled FROM sources WHERE id=?",(source_id,)).fetchone()
            if not row:
                raise HTTPException(404,"数据源不存在")
            enabled = 0 if row["enabled"] else 1
            con.execute("UPDATE sources SET enabled=? WHERE id=?",(enabled,source_id))
        if enabled:
            launch_scan()
        db.event("source_toggled", f"数据源{'启用' if enabled else '暂停'}")
        return {"enabled":bool(enabled)}

    @app.put("/api/sources/{source_id}/ai", dependencies=[Depends(authorized)])
    def source_ai_permission(source_id: int, payload: SourcePermissionInput):
        with db.connect() as con:
            row = con.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise HTTPException(404, "数据源不存在")
            con.execute("UPDATE sources SET allow_ai=? WHERE id=?", (int(payload.allow_ai), source_id))
            if payload.allow_ai:
                con.execute("""INSERT INTO ai_jobs(document_id,content_sha,state,attempts,error)
                    SELECT id,sha256,'queued',0,NULL FROM documents WHERE source_id=?
                    ON CONFLICT(document_id) DO UPDATE SET state='queued',content_sha=excluded.content_sha,
                    attempts=0,next_run_at=NULL,error=NULL,updated_at=datetime('now')""", (source_id,))
            else:
                con.execute("""DELETE FROM ai_jobs WHERE document_id IN
                    (SELECT id FROM documents WHERE source_id=?)""", (source_id,))
        db.event("source_ai_permission", f"{'允许' if payload.allow_ai else '停止'}企业模型整理", source_id)
        if payload.allow_ai:
            knowledge_worker.schedule()
        return {"allow_ai":payload.allow_ai}

    @app.put("/api/sources/{source_id}/share", dependencies=[Depends(authorized)])
    def source_share_permission(source_id: int, payload: SharePermissionInput):
        with db.connect() as con:
            row = con.execute("UPDATE sources SET allow_share=? WHERE id=?", (int(payload.allow_share),source_id))
            if not row.rowcount:
                raise HTTPException(404,"数据源不存在")
        return {"allow_share":payload.allow_share}

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(authorized)])
    def delete_source(source_id: int):
        name = collector.forget_source(source_id)
        if name is None:
            raise HTTPException(404, "数据源不存在")
        return {"removed":True, "name":name}

    @app.post("/api/scan", dependencies=[Depends(authorized)])
    def scan_all():
        launch_scan()
        return {"scheduled":True}

    @app.get("/api/documents")
    def documents(source_id: int | None = None, limit: int = Query(default=150, ge=1, le=500)):
        with db.connect() as con:
            if source_id is None:
                query = "SELECT d.id,d.title,d.project,d.relative_path,d.file_type,d.size_bytes,d.indexed_at,s.name source_name FROM documents d JOIN sources s ON s.id=d.source_id ORDER BY d.indexed_at DESC,d.id DESC LIMIT ?"
                return [dict(r) for r in con.execute(query,(limit,))]
            query = "SELECT d.id,d.title,d.relative_path,d.file_type,d.size_bytes,d.indexed_at,s.name source_name FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.source_id=? ORDER BY d.indexed_at DESC,d.id DESC LIMIT ?"
            return [dict(r) for r in con.execute(query,(source_id,limit))]

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: int):
        with db.connect() as con:
            row = con.execute("SELECT d.*,s.name source_name FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?", (doc_id,)).fetchone()
            if not row:
                raise HTTPException(404,"资料不存在")
            result = dict(row)
            result["content"] = result["content"][:80000]
            result["truncated"] = len(row["content"]) > 80000
            return result

    @app.get("/api/knowledge")
    def list_knowledge(status: str = "active", kind: str = "all", limit: int = Query(default=300, ge=1, le=1000), offset: int = Query(default=0,ge=0)):
        conditions, params = [], []
        if status == "active":
            conditions.append("k.status!='archived'")
        elif status in ("draft", "confirmed", "archived"):
            conditions.append("k.status=?")
            params.append(status)
        if kind in KIND_LABELS:
            conditions.append("k.kind=?")
            params.append(kind)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with db.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT k.* FROM knowledge k" + where + " ORDER BY k.needs_review DESC,k.updated_at DESC,k.id DESC LIMIT ? OFFSET ?", [*params,limit,offset])]
            for row in rows:
                from markdown_it import MarkdownIt
                display_body=WIKILINK.sub(lambda m: '['+(m.group(2) or 'K'+m.group(1))+'](#knowledge-'+str(int(m.group(1)))+')',row['body'])
                row["rendered_body"] = MarkdownIt("commonmark", {"html": False}).render(display_body)
                row["evidence"] = [dict(e) for e in con.execute("""
                    SELECT e.document_id,e.chunk_id,e.quote,e.is_current,e.superseded,d.title document_title,d.project,d.relative_path
                    FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
                    WHERE e.knowledge_id=? ORDER BY e.id LIMIT 8""", (row["id"],))]
                row['unavailable_reason']=unavailable_reason(con,row)
                row['share_unavailable_reason']=unavailable_reason(con,row,sharing=True)
                row['can_use']=not row['unavailable_reason']
                row['can_share']=not row['share_unavailable_reason']
            return rows

    @app.get('/api/knowledge-counts')
    def knowledge_counts():
        with db.connect() as con:
            return {'total':con.execute('SELECT count(*) FROM knowledge').fetchone()[0],
                    'draft':con.execute("SELECT count(*) FROM knowledge WHERE status='draft'").fetchone()[0],
                    'archived':con.execute("SELECT count(*) FROM knowledge WHERE status='archived'").fetchone()[0]}

    @app.get('/api/knowledge-item/{knowledge_id}')
    def get_knowledge(knowledge_id: int):
        with db.connect() as con:
            row=con.execute('SELECT * FROM knowledge WHERE id=?',(knowledge_id,)).fetchone()
            if not row:
                raise HTTPException(404,'知识不存在')
            from markdown_it import MarkdownIt
            result=dict(row)
            result['rendered_body']=MarkdownIt('commonmark',{'html':False}).render(WIKILINK.sub(lambda m:'['+(m.group(2) or 'K'+m.group(1))+'](#knowledge-'+m.group(1)+')',result['body']))
            result['evidence']=[dict(e) for e in con.execute('''SELECT e.*,d.title document_title,d.project FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id WHERE e.knowledge_id=?''',(knowledge_id,))]
            return result

    @app.post('/api/knowledge/{knowledge_id}/restore',dependencies=[Depends(authorized)])
    def restore_knowledge(knowledge_id: int):
        with db.connect() as con:
            old=con.execute('SELECT * FROM knowledge WHERE id=?',(knowledge_id,)).fetchone()
            if not old or old['status']!='archived':
                raise HTTPException(400,'只能恢复已归档的知识')
            con.execute('INSERT INTO knowledge_history(knowledge_id,version,kind,title,body,status) VALUES(?,?,?,?,?,?)',
                        (knowledge_id,old['version'],old['kind'],old['title'],old['body'],old['status']))
            con.execute("UPDATE knowledge SET status='draft',version=version+1,updated_at=datetime('now') WHERE id=?",(knowledge_id,))
            review_flags(con,[knowledge_id])
        return {'restored':True,'status':'draft'}

    @app.delete('/api/knowledge/{knowledge_id}',dependencies=[Depends(authorized)])
    def delete_knowledge(knowledge_id: int):
        with db.connect() as con:
            if not con.execute("DELETE FROM knowledge WHERE id=? AND (kind='preference' OR status='archived')",(knowledge_id,)).rowcount:
                raise HTTPException(400,'请先归档知识再删除')
        return {'removed':True}

    @app.post("/api/knowledge", dependencies=[Depends(authorized)])
    def create_knowledge(payload: KnowledgeInput):
        with db.connect() as con:
            linked_chunk = None
            if payload.document_id is not None:
                row = con.execute("SELECT content FROM documents WHERE id=?", (payload.document_id,)).fetchone()
                if not row:
                    raise HTTPException(404, "引用的文档不存在")
                if not payload.quote or len(payload.quote.strip()) < 8 or payload.quote not in row["content"]:
                    raise HTTPException(400, "引用依据必须是原始文档中存在的连续原文，至少 8 字")
                chunk = con.execute("SELECT id FROM chunks WHERE document_id=? AND instr(text,?)>0 ORDER BY ordinal LIMIT 1",
                                    (payload.document_id, payload.quote[:60])).fetchone()
                linked_chunk = chunk["id"] if chunk else None
            res = con.execute("INSERT INTO knowledge(kind,title,body,status,created_by,source_bound) VALUES(?,?,?,?, 'human',?)",
                              (payload.kind,payload.title,payload.body,payload.status, int(payload.document_id is not None)))
            know_id = int(res.lastrowid)
            if payload.document_id is not None:
                con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,chunk_id,quote) VALUES(?,?,?,?)",
                            (know_id,payload.document_id,linked_chunk,payload.quote))
        db.event("knowledge_created",f"人工新增：{payload.title}")
        return {"id":know_id}

    @app.put("/api/knowledge/{knowledge_id}", dependencies=[Depends(authorized)])
    def update_knowledge(knowledge_id: int, payload: KnowledgeInput):
        with db.connect() as con:
            old = con.execute("SELECT * FROM knowledge WHERE id=?",(knowledge_id,)).fetchone()
            if not old:
                raise HTTPException(404,"知识不存在")
            con.execute("INSERT INTO knowledge_history(knowledge_id,version,kind,title,body,status) VALUES(?,?,?,?,?,?)",
                        (knowledge_id,old["version"],old["kind"],old["title"],old["body"],old["status"]))
            con.execute("""UPDATE knowledge SET kind=?,title=?,body=?,status=?,version=version+1,
                created_by='human',review_hold=0,updated_at=datetime('now') WHERE id=?""",
                        (payload.kind,payload.title,payload.body,payload.status,knowledge_id))
            review_flags(con, [knowledge_id])
        db.event("knowledge_updated",f"人工修订：{payload.title}")
        return {"updated":True}

    @app.get("/api/knowledge/proposals")
    def knowledge_proposals():
        with db.connect() as con:
            return [dict(r) for r in con.execute("""SELECT p.id,p.document_id,p.target_id,p.target_version,p.action,
                p.kind,p.title,p.body,p.quote,p.reason,p.origin,p.status,p.created_at,p.content_sha,
                k.title previous_title,k.body previous_body,k.version previous_version,
                d.title source_title,d.project,d.sha256 current_sha
                FROM knowledge_proposals p
                JOIN knowledge k ON k.id=p.target_id JOIN documents d ON d.id=p.document_id
                WHERE p.status='pending' ORDER BY p.id DESC LIMIT 100""")]

    @app.post("/api/knowledge/proposals/{proposal_id}/accept", dependencies=[Depends(authorized)])
    def accept_proposal(proposal_id: int):
        with db.connect() as con:
            try:
                result = resolve_proposal(con, proposal_id, accept=True)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        db.event("knowledge_revision_accepted", f"人工接受知识更新提案 {proposal_id}")
        return {"state": result}

    @app.post("/api/knowledge/proposals/{proposal_id}/dismiss", dependencies=[Depends(authorized)])
    def dismiss_proposal(proposal_id: int):
        with db.connect() as con:
            try:
                result = resolve_proposal(con, proposal_id, accept=False)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        db.event("knowledge_revision_dismissed", f"人工忽略知识更新提案 {proposal_id}")
        return {"state": result}

    @app.get("/api/knowledge/{knowledge_id}/relations")
    def relations(knowledge_id: int):
        with db.connect() as con:
            result=related_knowledge(con,knowledge_id)
            if result is None:
                raise HTTPException(404,'知识不存在')
            return result

    @app.get("/api/knowledge/{knowledge_id}/history")
    def history(knowledge_id: int):
        with db.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM knowledge_history WHERE knowledge_id=? ORDER BY version DESC",(knowledge_id,))]

    @app.get("/api/projects")
    def projects():
        with db.connect() as con:
            return [dict(r) for r in con.execute("""
                SELECT d.project name, COUNT(DISTINCT d.id) documents,
                       COUNT(DISTINCT e.knowledge_id) knowledge,
                       SUM(CASE WHEN e.is_current=0 THEN 1 ELSE 0 END) stale_links,
                       MAX(d.indexed_at) updated_at
                FROM documents d LEFT JOIN knowledge_evidence e ON e.document_id=d.id
                GROUP BY d.project ORDER BY documents DESC, name
            """)]

    @app.get("/api/projects/summary")
    def project_summary(name: str = Query(min_length=1, max_length=200)):
        with db.connect() as con:
            docs = [dict(r) for r in con.execute("""
                SELECT d.id,d.title,d.project,d.relative_path,d.indexed_at,s.name source_name
                FROM documents d JOIN sources s ON s.id=d.source_id
                WHERE d.project=? ORDER BY d.indexed_at DESC LIMIT 150""",(name,))]
            entries = [dict(r) for r in con.execute("""
                SELECT DISTINCT k.id,k.kind,k.title,k.body,k.status,k.needs_review,k.updated_at
                FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
                JOIN documents d ON d.id=e.document_id
                WHERE d.project=? AND k.status!='archived'
                ORDER BY k.kind,k.updated_at DESC LIMIT 200""",(name,))]
            for entry in entries:
                entry["evidence"] = [dict(r) for r in con.execute("""
                    SELECT d.id document_id,d.title,e.quote,e.is_current,e.occurred_at FROM knowledge_evidence e
                    JOIN documents d ON d.id=e.document_id
                    WHERE e.knowledge_id=? AND d.project=? ORDER BY e.is_current DESC LIMIT 12""",
                    (entry["id"],name))]
            timeline = [dict(r) for r in con.execute("""
                SELECT k.id knowledge_id,k.title,k.body,k.status,k.needs_review,
                       e.quote,e.is_current,e.occurred_at,d.id document_id,d.title source_title
                FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
                JOIN documents d ON d.id=e.document_id
                WHERE d.project=? AND k.kind='decision' AND k.status!='archived'
                ORDER BY (e.occurred_at IS NULL),e.occurred_at DESC,d.indexed_at DESC,k.id DESC
                LIMIT 80""", (name,))]
            return {"name":name,"documents":docs,"knowledge":entries,"decision_timeline":timeline}

    @app.get("/api/search")
    def search_endpoint(q: str = Query(default="", max_length=160)):
        with db.connect() as con:
            return search(con,q)

    @app.get("/api/settings")
    def settings():
        # Model provider credentials remain on the enterprise gateway server.
        return {"enterprise_model_ready": model_client.configured,
                "edition":model_client.mode,"edition_selected":bool(db.setting('edition')),
                "model_status":model_client.status,"model_error":model_client.error,
                "model_name":getattr(model_client.client,'model','') or db.setting('enterprise_model_name'),
                "personal_base_url":db.setting('personal_base_url'),"personal_model":db.setting('personal_model'),
                "has_personal_key":bool(getattr(model_client.client,'key','')) if model_client.mode=='personal' else False,
                "enterprise_url":publisher.client.url,"enterprise_role":db.setting('enterprise_role','employee'),
                "secret_storage":secure.label,"storage_error":storage_error,
                "gateway_managed_by":model_client.mode, "cloud_sync": publisher.client.configured,
                "publication_error": db.setting("publication_error"),
                "automatic_ai_processing": True}

    def check_service_switch():
        with db.connect() as con:
            if (con.execute('SELECT count(*) FROM publications WHERE enabled=1').fetchone()[0] or db.setting('publication_error')):
                raise HTTPException(409,'请先停止全部分身分享并确认同步成功，再切换版本或服务')

    @app.put('/api/edition',dependencies=[Depends(authorized)])
    def choose_edition(payload: EditionInput):
        if payload.edition!=model_client.mode:
            check_service_switch()
            # Switching away from the enterprise disconnects its publication
            # transport. This prevents new personal knowledge from syncing there.
            if payload.edition=='personal':
                try:
                    client=PersonalModel(db.setting('personal_base_url'),secure.get('personal_model_key'),db.setting('personal_model'))
                except Exception as exc:
                    raise HTTPException(400,'无法读取个人模型凭据，请解锁安全存储后重试') from exc
            else:
                client=GatewayClient(url='',token='')
            publisher.client=PublishingClient(url='',token='')
            db.set_setting('share_connection_enabled','0')
            model_client.replace(client,payload.edition)
            if not client.configured:
                model_client.status='not_configured'
        db.set_setting('edition',payload.edition)
        return {'edition':payload.edition}

    @app.put('/api/model/personal',dependencies=[Depends(authorized)])
    def save_personal_model(payload: ModelInput):
        if model_client.mode!='personal':
            raise HTTPException(403,'企业版使用企业统一分配的模型，请在管理员设置中操作')
        try:
            if not payload.api_key and payload.base_url.rstrip('/')!=db.setting('personal_base_url'):
                raise ValueError('更换服务地址时请重新填写 API Key')
            key=payload.api_key or secure.get('personal_model_key')
            candidate=PersonalModel(payload.base_url,key,payload.model)
            if not candidate.configured:
                raise ValueError('请填写 API Key')
            candidate.chat([{'role':'user','content':'请只回复 OK。'}],max_tokens=16)
            secure.set('personal_model_key',key)
        except Exception as exc:
            message=str(exc) if isinstance(exc,ValueError) else '连接测试或安全保存失败，未启用新配置；请检查模型信息和系统安全存储'
            raise HTTPException(400,message) from exc
        db.set_setting('personal_base_url',candidate.url)
        db.set_setting('personal_model',candidate.model)
        model_client.replace(candidate,'personal',tested=True)
        knowledge_worker.schedule()
        return {'connected':True,'model':candidate.model}

    @app.post('/api/model/personal/models',dependencies=[Depends(authorized)])
    def personal_models(payload: ModelListInput):
        if model_client.mode!='personal':
            raise HTTPException(403,'企业员工不能配置个人模型')
        try:
            if not payload.api_key and payload.base_url.rstrip('/')!=db.setting('personal_base_url'):
                raise ValueError()
            key=payload.api_key or secure.get('personal_model_key')
            models=PersonalModel(payload.base_url,key).list_models()
            return {'models':models}
        except Exception as exc:
            raise HTTPException(400,'无法获取模型列表，可以填写服务商提供的模型名称；请检查地址和 API Key') from exc

    @app.post('/api/model/test',dependencies=[Depends(authorized)])
    def test_current_model():
        if not model_client.configured:
            raise HTTPException(400,'请先完成模型设置或企业连接')
        try:
            model_client.test()
        except Exception as exc:
            raise HTTPException(502,'模型调用失败，请检查连接、模型名称、凭据或服务额度') from exc
        return {'ok':True}

    @app.put("/api/connection", dependencies=[Depends(authorized)])
    def connection(payload: ConnectionInput):
        try:
            new_client = PublishingClient(url=payload.url,token=payload.token)
            info = new_client.request('GET','/v1/me')
            if not info.get('ok'):
                raise ValueError('服务不可用')
        except Exception as exc:
            raise HTTPException(400,'无法连接企业服务，请检查地址或凭据') from exc
        if new_client.url != publisher.client.url or new_client.token != publisher.client.token or model_client.mode!='enterprise':
            check_service_switch()
        try:
            secure.set('enterprise_token',new_client.token)
        except Exception as exc:
            raise HTTPException(400,'系统安全存储不可用，未保存企业 Token；请解锁后重试') from exc
        db.set_setting('enterprise_url',new_client.url)
        db.set_setting('enterprise_token','')
        db.set_setting('enterprise_role',info.get('role','employee'))
        db.set_setting('enterprise_model_name',info.get('model',''))
        model_client.replace(GatewayClient(url=new_client.url,token=new_client.token),'enterprise')
        publisher.client=new_client
        db.set_setting('share_connection_enabled','1')
        db.set_setting('publication_digest','')
        return {'connected':True,'model_tested':False,'role':info.get('role','employee')}

    @app.put('/api/sharing/connection',dependencies=[Depends(authorized)])
    def share_connection(payload: ConnectionInput):
        if model_client.mode!='personal':
            return connection(payload)
        try:
            client=PublishingClient(url=payload.url,token=payload.token)
            info=client.request('GET','/v1/me')
            if not info.get('ok'):
                raise ValueError()
        except Exception as exc:
            raise HTTPException(400,'分享服务连接失败，请检查地址和 Token') from exc
        if client.url!=publisher.client.url or client.token!=publisher.client.token:
            check_service_switch()
        try:
            secure.set('enterprise_token',client.token)
        except Exception as exc:
            raise HTTPException(400,'无法安全保存分享 Token，请解锁系统安全存储') from exc
        db.set_setting('enterprise_url',client.url)
        db.set_setting('enterprise_token','')
        db.set_setting('enterprise_role',info.get('role','employee'))
        db.set_setting('share_connection_enabled','1')
        db.set_setting('publication_digest','')
        publisher.client=client
        return {'connected':True,'edition':'personal'}

    def admin_request(method,path,body=None):
        if model_client.mode!='enterprise' or not publisher.client.configured:
            raise HTTPException(403,'请先使用企业管理员 Token 连接企业服务')
        try:
            # Role is enforced again by the remote service, never trusted only
            # from a local setting or a hidden menu.
            return publisher.client.request(method,'/v1/admin/'+path,body)
        except Exception as exc:
            from urllib.error import HTTPError
            if isinstance(exc,HTTPError) and exc.code==403:
                raise HTTPException(403,'此操作仅限企业管理员，请使用管理员 Token') from exc
            raise HTTPException(400,'管理操作失败，现有配置未确认更改；请检查权限、连接和填写内容') from exc

    @app.get('/api/admin/settings')
    def admin_settings():
        return admin_request('GET','settings')

    @app.put('/api/admin/model',dependencies=[Depends(authorized)])
    def admin_model(payload: dict):
        result=admin_request('PUT','model',payload)
        db.set_setting('enterprise_model_name',result.get('model',''))
        model_client.status,model_client.error='connected',''
        return result

    @app.post('/api/admin/models',dependencies=[Depends(authorized)])
    def admin_models(payload: ModelListInput):
        return admin_request('POST','models',payload.model_dump())

    @app.post('/api/admin/employees',dependencies=[Depends(authorized)])
    def admin_employee(payload: dict):
        return admin_request('POST','employees',payload)

    @app.delete('/api/admin/employees/{employee}',dependencies=[Depends(authorized)])
    def admin_revoke(employee: str):
        from urllib.parse import quote
        return admin_request('DELETE','employees/'+quote(employee,safe=''))

    @app.post("/api/sharing/sync", dependencies=[Depends(authorized)])
    def sync_sharing():
        return publisher.sync(force=True)

    @app.post("/api/twins/{twin_id}/publish", dependencies=[Depends(authorized)])
    def publish_twin(twin_id: int):
        if not publisher.client.configured:
            raise HTTPException(503,'请先连接企业分享服务')
        with db.connect() as con:
            if not con.execute('SELECT id FROM twins WHERE id=?',(twin_id,)).fetchone():
                raise HTTPException(404,'数字分身不存在')
            con.execute('INSERT INTO publications(twin_id,enabled) VALUES(?,1) ON CONFLICT(twin_id) DO UPDATE SET enabled=1',(twin_id,))
        result=publisher.sync(force=True)
        if result['state']!='synced':
            raise HTTPException(503,result['detail'])
        return {'published':True}

    @app.delete("/api/twins/{twin_id}/publish", dependencies=[Depends(authorized)])
    def unpublish_twin(twin_id: int):
        with db.connect() as con:
            con.execute('UPDATE publications SET enabled=0 WHERE twin_id=?',(twin_id,))
        return publisher.sync(force=True)

    @app.get("/api/twins/{twin_id}/sharing")
    def sharing(twin_id: int):
        with db.connect() as con:
            row=con.execute('SELECT enabled,remote_id FROM publications WHERE twin_id=?',(twin_id,)).fetchone()
        enabled=bool(row and row['enabled'])
        result={'enabled':enabled,'configured':publisher.client.configured,'error':db.setting('publication_error'),'grants':[]}
        if enabled and row['remote_id'] and publisher.client.configured:
            try:
                result['grants']=publisher.client.request('GET','/v1/twins/'+row['remote_id']+'/grants')
            except Exception:
                result['error']='无法连接分享服务；尚不能确认远程分享状态。'
        return result

    @app.get('/api/twins/{twin_id}/preview')
    def twin_preview(twin_id: int):
        with db.connect() as con:
            if not con.execute('SELECT id FROM twins WHERE id=?',(twin_id,)).fetchone():
                raise HTTPException(404,'分身不存在')
            rows=[dict(r) for r in con.execute('SELECT k.* FROM knowledge k JOIN twin_knowledge tk ON tk.knowledge_id=k.id WHERE tk.twin_id=?',(twin_id,))]
            result=[{'id':r['id'],'title':r['title'],'local_reason':unavailable_reason(con,r),'share_reason':unavailable_reason(con,r,sharing=True)} for r in rows]
        return {'selected_count':len(rows),'usable_count':sum(not r['local_reason'] for r in result),
                'publishable_count':sum(not r['share_reason'] for r in result),'knowledge':result}

    @app.post("/api/twins/{twin_id}/sharing", dependencies=[Depends(authorized)])
    def create_share(twin_id: int, payload: ShareInput):
        if publisher.sync(force=True)['state']!='synced':
            raise HTTPException(503,'分享未同步，暂不能创建链接')
        try:
            pubid=publisher.public_id(twin_id)
            result=publisher.client.request('POST','/v1/twins/'+pubid+'/grants',payload.model_dump())
            return {**result,'url':publisher.client.url+'/share#access='+result['token']}
        except Exception as exc:
            raise HTTPException(502,'分享链接创建失败，请检查企业服务') from exc

    @app.delete("/api/twins/{twin_id}/sharing/{grant_id}", dependencies=[Depends(authorized)])
    def revoke_share(twin_id: int, grant_id: str):
        try:
            pubid=publisher.public_id(twin_id)
            return publisher.client.request('DELETE','/v1/twins/'+pubid+'/grants/'+grant_id)
        except Exception as exc:
            raise HTTPException(503,'撤销尚未完成，请恢复连接后重试；原链接目前仍可能可用') from exc

    @app.get("/api/ai/jobs")
    def ai_jobs():
        with db.connect() as con:
            return [dict(r) for r in con.execute("""SELECT j.id,j.document_id,j.state,j.attempts,j.error,
                j.updated_at,j.next_run_at,d.title,d.project,s.name source_name
                FROM ai_jobs j JOIN documents d ON d.id=j.document_id
                JOIN sources s ON s.id=d.source_id
                ORDER BY j.updated_at DESC,j.id DESC LIMIT 100""")]

    @app.post("/api/ai/jobs/retry", dependencies=[Depends(authorized)])
    def retry_failed_jobs():
        with db.connect() as con:
            updated = con.execute("UPDATE ai_jobs SET state='queued',attempts=0,next_run_at=NULL,error=NULL,updated_at=datetime('now') WHERE state='error'")
        knowledge_worker.schedule()
        return {"queued":updated.rowcount}

    @app.get("/api/twins")
    def twins():
        with db.connect() as con:
            rows = [dict(x) for x in con.execute("""SELECT t.*,
                (SELECT count(*) FROM twin_knowledge tk JOIN knowledge k ON k.id=tk.knowledge_id
                  WHERE tk.twin_id=t.id AND """ + READY_SQL + """) knowledge_count
                FROM twins t ORDER BY t.updated_at DESC,t.id DESC""")]
            return rows

    @app.post("/api/twins", dependencies=[Depends(authorized)])
    def create_twin(payload: TwinInput):
        with db.connect() as con:
            res = con.execute("INSERT INTO twins(name,description) VALUES(?,?)",
                              (payload.name.strip(),payload.description.strip()))
            return {"id":int(res.lastrowid)}

    @app.get("/api/twins/{twin_id}")
    def get_twin(twin_id: int):
        with db.connect() as con:
            row = con.execute("SELECT * FROM twins WHERE id=?",(twin_id,)).fetchone()
            if not row:
                raise HTTPException(404,"数字分身不存在")
            selected = [r[0] for r in con.execute("SELECT knowledge_id FROM twin_knowledge WHERE twin_id=?",(twin_id,))]
            return {**dict(row),"knowledge_ids":selected}

    @app.put("/api/twins/{twin_id}", dependencies=[Depends(authorized)])
    def update_twin(twin_id: int,payload: TwinInput):
        with db.connect() as con:
            if payload.knowledge_ids is not None:
                allowed={r[0] for r in con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL)}
                if not set(payload.knowledge_ids).issubset(allowed):
                    raise HTTPException(400,'包含不可用知识，请核对状态后重新保存')
            row = con.execute("UPDATE twins SET name=?,description=?,updated_at=datetime('now') WHERE id=?",
                              (payload.name.strip(),payload.description.strip(),twin_id))
            if not row.rowcount:
                raise HTTPException(404,"数字分身不存在")
            if payload.knowledge_ids is not None:
                con.execute('DELETE FROM twin_knowledge WHERE twin_id=?',(twin_id,))
                con.executemany('INSERT INTO twin_knowledge VALUES(?,?)',[(twin_id,k) for k in sorted(set(payload.knowledge_ids))])
        return {"updated":True}

    @app.delete("/api/twins/{twin_id}", dependencies=[Depends(authorized)])
    def delete_twin(twin_id: int):
        with db.connect() as con:
            row = con.execute("DELETE FROM twins WHERE id=?",(twin_id,))
            if not row.rowcount:
                raise HTTPException(404,"数字分身不存在")
        return {"removed":True}

    @app.put("/api/twins/{twin_id}/knowledge", dependencies=[Depends(authorized)])
    def assign_twin_knowledge(twin_id: int, payload: TwinKnowledgeInput):
        requested = set(payload.knowledge_ids)
        with db.connect() as con:
            if not con.execute("SELECT id FROM twins WHERE id=?",(twin_id,)).fetchone():
                raise HTTPException(404,"数字分身不存在")
            allowed = {r[0] for r in con.execute("""SELECT k.id FROM knowledge k
                WHERE """+READY_SQL)}
            if not requested.issubset(allowed):
                raise HTTPException(400,"包含尚未确认、已归档、需要核对、个人偏好或未允许 AI 使用的知识")
            con.execute("DELETE FROM twin_knowledge WHERE twin_id=?",(twin_id,))
            con.executemany("INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)",
                            [(twin_id,k) for k in sorted(requested)])
            con.execute("UPDATE twins SET updated_at=datetime('now') WHERE id=?",(twin_id,))
        return {"assigned":len(requested)}

    @app.post("/api/twins/{twin_id}/ask", dependencies=[Depends(authorized)])
    def ask_twin(twin_id: int, payload: AskInput):
        if payload.scope=='published':
            try:
                pubid=publisher.public_id(twin_id)
                return publisher.client.request('POST','/v1/twins/'+pubid+'/preview-ask',{'question':payload.question})
            except Exception as exc:
                raise HTTPException(503,'无法读取当前已发布版本，请检查分享服务或先启用分享') from exc
        if not model_client.configured:
            raise HTTPException(503,"请先完成个人模型设置或企业连接")
        with db.connect() as con:
            twin = con.execute("SELECT name,description FROM twins WHERE id=?",(twin_id,)).fetchone()
            if not twin:
                raise HTTPException(404,"数字分身不存在")
            rows = [dict(x) for x in con.execute("""SELECT k.id,k.title,k.body,k.version FROM twin_knowledge tk
                JOIN knowledge k ON k.id=tk.knowledge_id
                WHERE tk.twin_id=? AND """+READY_SQL,(twin_id,))]
            for r in rows:
                r['evidence'] = [dict(e) for e in con.execute("""SELECT e.quote,d.title document_title
                    FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
                    WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0 LIMIT 3""",(r['id'],))]
        if not rows:
            return {"answer":"这个分身还没有被分配可使用的知识。","context_count":0,"citations":[]}
        # Every contributing source must still permit sending derived text to AI.
        with db.connect() as con:
            rows=[r for r in rows if not con.execute("""SELECT 1 FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
                WHERE e.knowledge_id=? AND s.allow_ai=0 LIMIT 1""",(r['id'],)).fetchone()]
        try:
            result=answer_from_knowledge(model_client,payload.question,rows)
        except Exception as exc:
            raise HTTPException(502,"模型请求失败，请检查设置、服务额度或稍后重试") from exc
        # Recheck authorization after a slow model call.
        with db.connect() as con:
            current={(r[0],r[1]) for r in con.execute("""SELECT k.id,k.version FROM twin_knowledge tk JOIN knowledge k ON k.id=tk.knowledge_id
                WHERE tk.twin_id=? AND """+READY_SQL,(twin_id,))}
        if not {(r['id'],r['version']) for r in rows}.issubset(current):
            raise HTTPException(409,'分身知识授权已更新，请重新提问')
        return result

    @app.get('/api/backup')
    def backup():
        # SQLite backup gives a consistent live snapshot, including WAL data.
        # API keys and enterprise tokens live outside this database.
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'worktwin.sqlite'
            with db.connect() as con, sqlite3.connect(target) as copy:
                con.backup(copy)
                copy.execute("DELETE FROM settings WHERE key='enterprise_token'")
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.write(target,'worktwin.sqlite')
                archive.writestr('恢复说明.txt','先退出 WorkTwin，再备份当前数据目录。将压缩包中的 worktwin.sqlite 替换到数据目录，并移走同名 -wal 和 -shm 文件，然后重新启动。模型密钥和企业 Token 不在此备份中；换电脑后需重新配置。恢复旧备份可能包含过期分享状态，恢复后先核对远程分享。')
            return Response(buffer.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="WorkTwin-backup.zip"'})

    @app.get('/api/data-location')
    def data_location():
        return {'path':str(db.path.parent)}

    @app.get("/api/export-markdown", dependencies=[Depends(authorized)])
    def export_markdown():
        """Portable human-readable knowledge wiki; no raw document export."""
        with db.connect() as con:
            items = [dict(x) for x in con.execute("SELECT id,kind,title,body,status,version FROM knowledge WHERE status!='archived' ORDER BY kind,updated_at DESC")]
            for k in items:
                k['evidence'] = [dict(r) for r in con.execute("SELECT d.title,e.quote,e.is_current,e.occurred_at FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id WHERE e.knowledge_id=?",(k['id'],))]
        parts = ["# WorkTwin · 个人工作知识库", "", "> 从本人授权的来源整理；未确认知识仅作为候选，不应直接用于工作决策。", ""]
        for kind,label in KIND_LABELS.items():
            group = [k for k in items if k['kind']==kind]
            if not group:
                continue
            parts.extend([f"## {label}", ""])
            for k in group:
                parts.extend([f"### {k['title']}", "", f"状态：{'已确认' if k['status']=='confirmed' else '待确认'} · 版本：{k['version']}", "", k['body'], ""])
                if k['evidence']:
                    parts.append("**依据**")
                    for e in k['evidence']:
                        marker = "（旧版引用，需复核）" if not e['is_current'] else "（原始会话 " + e['occurred_at'] + "）" if e['occurred_at'] else ""
                        parts.append(f"- {e['title']}{marker}：{e['quote'][:200].replace(chr(10),' ')}")
                    parts.append("")
        response = PlainTextResponse("\n".join(parts),media_type="text/markdown; charset=utf-8")
        response.headers['Content-Disposition']='attachment; filename="worktwin-knowledge.md"'
        return response

    @app.get("/api/export-wiki", dependencies=[Depends(authorized)])
    def export_wiki():
        """Stable-ID Markdown vault with an index and live wiki links."""
        with db.connect() as con:
            raw=export_wiki_archive(con)
        return Response(raw, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="worktwin-wiki.zip"',
            "Cache-Control": "no-store"})

    @app.get("/api/export", dependencies=[Depends(authorized)])
    def export():
        with db.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT id,kind,title,body,status,version,needs_review,updated_at FROM knowledge WHERE status!='archived' ORDER BY id")]
            for row in rows:
                row["evidence"] = [dict(e) for e in con.execute("SELECT d.title AS document_title,d.relative_path,e.quote,e.is_current,e.occurred_at FROM knowledge_evidence e JOIN documents d ON e.document_id=d.id WHERE e.knowledge_id=?",(row["id"],))]
        response = JSONResponse({"schema":"worktwin-knowledge-v1","entries":rows})
        response.headers["Content-Disposition"] = 'attachment; filename="worktwin-knowledge.json"'
        return response

    return app
