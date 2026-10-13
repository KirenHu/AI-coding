"""Low-priority scheduled AI knowledge processing, independent from capture."""

from __future__ import annotations

import threading
import secrets
import time
import json
import os
from .update_filter import plan, start_run, save_baseline, processing_run
from datetime import datetime, timedelta, timezone
from .db import Database
from .inference import GatewayClient, extract_knowledge
from .knowledge import store_candidates
from .reconcile import existing_for_project
from .scope import document_scope
from .projects import catalog, plan_work_units, apply_assignments, source_context
from .project_recheck import ProjectRechecker
from .decision_model import DecisionRouter
from .credentials import DesktopSecrets
from .ingest import pending_batch, ack_batch
from .artifacts import work_context, context_for_model
from .article_maintenance import ArticleMaintainer
from .library_organization import LibraryOrganizer
from .gardener import KnowledgeGardener
from .job_lease import renew_lease


class KnowledgeWorker:
    def __init__(self, db: Database, *, client: GatewayClient | None = None, interval: int = 30, decision_router=None):
        self.db = db
        self.client = client or GatewayClient()
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._editor_wake = threading.Event()
        self._editor_thread = None
        self._thread: threading.Thread | None = None
        self.processing = False
        self.decision_router=decision_router or DecisionRouter(db,DesktopSecrets(db.path),self.client)
        self.project_rechecker=ProjectRechecker(db,self.decision_router)
        self.article_maintainer=ArticleMaintainer(db,self.client)
        self.library_organizer=LibraryOrganizer(db,self.client,self.article_maintainer)
        # Retain explicit owner-reviewed curation and its existing proposal API.
        # Scheduled authorship is handled solely by the article maintainer.
        self.gardener=KnowledgeGardener(db,client=self.client)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._editor_thread = threading.Thread(target=self._editor_loop, name="worktwin-editor", daemon=True)
        self._editor_thread.start()
        self._thread = threading.Thread(target=self._loop, name="worktwin-knowledge", daemon=True)
        self._thread.start()
        self.schedule()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._editor_wake.set()
        if self._editor_thread:
            self._editor_thread.join(timeout=4)
        if self._thread:
            self._thread.join(timeout=4)

    def schedule(self):
        self._wake.set()
        self._editor_wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set() or not self.client.configured:
                continue
            result = self.process_next()["state"]
            if result not in ("idle", "not_configured"):
                self._editor_wake.set()
                self._wake.set()
            elif result == "idle":
                self.project_rechecker.process_next()

    def _editor_loop(self):
        # One editor owns all scheduled article writes. Extraction has its own
        # thread, so even a long document cannot block existing-article grouping.
        while not self._stop.is_set():
            self._editor_wake.wait(self.interval)
            self._editor_wake.clear()
            if self._stop.is_set() or not self.client.configured:
                continue
            self.article_maintainer.client = self.client
            self.library_organizer.client = self.client
            result = self.library_organizer.process_grouping()
            if result['state'] == 'idle':
                result = self.library_organizer.process_merge()
                if result['state'] in ('idle', 'queued'):
                    result = self.article_maintainer.process_next()
            if result['state'] not in ('idle', 'not_configured', 'queued'):
                self._editor_wake.set()

    def process_next(self) -> dict:
        if not self.client.configured:
            return {"state": "not_configured"}
        with self.db.connect() as con:
            # The read and state transition must be one serialized claim.
            # Multiple worker instances must not pay for the same model job.
            con.execute("BEGIN IMMEDIATE")
            # A process crash releases its lease after 20 minutes. Constructing
            # another Database object must never reset a live worker's claim.
            con.execute("UPDATE ai_jobs SET state='queued',claim_token=NULL WHERE state='running' AND updated_at<datetime('now','-20 minutes') AND attempts<4")
            con.execute("UPDATE ai_jobs SET state='error',error='整理任务多次中断，请重试' WHERE state='running' AND updated_at<datetime('now','-20 minutes') AND attempts>=4")
            row = con.execute("""SELECT j.id job_id,j.document_id,j.content_sha,d.content,
                    j.batch_generation,j.batch_from_seq,j.batch_through_seq,t.generation,
                    d.id,d.source_id,d.path,d.title,d.mtime_ns,d.project,d.project_key,d.scope,d.project_verified,d.file_type,COALESCE(s.adapter,s.kind) adapter
                    FROM ai_jobs j JOIN documents d ON d.id=j.document_id
                    JOIN sources s ON s.id=d.source_id
                    LEFT JOIN transcript_streams t ON t.document_id=d.id
                    WHERE j.state='queued' AND (j.next_run_at IS NULL OR j.next_run_at<=datetime('now'))
                    AND s.enabled=1 AND s.allow_ai=1 AND d.deleted=0
                    AND (t.document_id IS NOT NULL OR d.sha256=j.content_sha)
                    ORDER BY j.updated_at ASC,j.id ASC LIMIT 1""").fetchone()
            if not row:
                return {"state": "idle"}
            job = dict(row)
            frozen = (job['generation'] is not None
                      and job['batch_generation'] == job['generation']
                      and job['batch_through_seq'] is not None)
            batch = pending_batch(con, job['document_id'],
                                  after_seq=job['batch_from_seq'] if frozen else None,
                                  through_seq=job['batch_through_seq'] if frozen else None)
            if batch is not None:
                job['content'] = batch['content']
                if not job['content']:
                    con.execute("""UPDATE ai_jobs SET state='done',error=NULL,next_run_at=NULL,
                        claim_token=NULL,batch_generation=NULL,batch_from_seq=NULL,batch_through_seq=NULL,
                        updated_at=datetime('now') WHERE id=?""", (job['job_id'],))
                    return {'state': 'done', 'candidates': 0, 'added': 0, 'proposals': 0, 'automatic': 0}
                con.execute("""UPDATE ai_jobs SET batch_generation=?,batch_from_seq=?,batch_through_seq=?
                    WHERE id=?""", (batch['generation'], batch['from_seq'], batch['through_seq'], job['job_id']))
            job['claim_token']=secrets.token_hex(16)
            con.execute("UPDATE ai_jobs SET state='running',claim_token=?,attempts=attempts+1,updated_at=datetime('now') WHERE id=?", (job['claim_token'],job["job_id"]))
        full_content = job['content']
        selection = {'mode':'incremental','reason':'transcript_watermark','content':full_content,'changes':[]}
        with self.db.connect() as con:
            if batch is None:
                baseline = con.execute('SELECT content,content_sha FROM document_processing_baselines WHERE document_id=?', (job['document_id'],)).fetchone()
                try:
                    selection = plan(baseline[0] if baseline else None, full_content,
                                     enabled=self.db.setting('document_incremental_enabled',os.getenv('WORKTWIN_DOCUMENT_INCREMENTAL','1')) == '1')
                except Exception:
                    selection = {'mode':'full','reason':'filter_error','content':full_content,'changes':[]}
            if batch is None and baseline:
                selection['previous_sha'] = baseline['content_sha']
            if selection['mode']=='skip' and con.execute(
                    'SELECT 1 FROM knowledge_evidence WHERE document_id=? AND is_current=0 AND superseded=0 LIMIT 1',
                    (job['document_id'],)).fetchone():
                selection = dict(selection,mode='full',reason='citation_realignment',content=full_content)
            run_id = start_run(con,job['document_id'],job['content_sha'],selection,len(full_content))
        job['content'] = selection['content']
        metrics_context = processing_run.set(run_id)
        started = time.monotonic()
        model_calls = 0
        outcome = 'error'
        self.processing = True
        try:
            with renew_lease(self.db,"UPDATE ai_jobs SET updated_at=datetime('now') WHERE id=? AND claim_token=?",(job['job_id'],job['claim_token'])):
                outer=self
                class LeasedClient:
                    configured=True
                    def chat(self,messages,max_tokens=1800):
                        nonlocal model_calls
                        with outer.db.connect() as con:
                            claimed=con.execute("UPDATE ai_jobs SET updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?",(job['job_id'],job['content_sha'],job['claim_token']))
                            if not claimed.rowcount:
                                raise RuntimeError('job superseded')
                        model_calls += 1
                        return outer.client.chat(messages,max_tokens=max_tokens)
                leased_client=LeasedClient()
                with self.db.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    metadata=document_scope(job)
                    metadata['project_context'] = source_context(con, job)
                    metadata['candidate_projects'] = [{'project_key':p['project_key'], 'name':p['name'],
                        'summary':p.get('summary','')} for p in catalog(con)][:30]
                    con.execute('UPDATE documents SET project_key=?,scope=? WHERE id=?',(metadata['project_key'],metadata['scope'],job['document_id']))
                    known = existing_for_project(con, job["document_id"])
                    context = work_context(con, job['document_id'],
                                           event_start=batch['from_seq'] + 1 if batch else None,
                                           event_end=batch['through_seq'] if batch else None)
                class ExtractionClient:
                    configured=True
                    def chat(self, messages, max_tokens=1800):
                        messages = [dict(message) for message in messages]
                        if not batch and selection['mode'] == 'incremental':
                            messages[0]['content'] += ('\n本次是文档新增章节及相邻章节上下文。只提取新增章节带来的信息，'
                                '旧章节仅供理解。其他旧章节没有发送，不代表已删除；文章维护阶段仍须核对完整原文。')
                        if batch:
                            messages[0]['content'] += (
                                '\n这是会话的增量批次。前序对话仅供理解指代和确认关系，'
                                '只整理本次新增事件带来的知识或变化，不重复提炼前序知识；'
                                'quote或confirmation_quote必须出自本次新增事件。')
                        if not batch:
                            messages[-1]['content'] += '\n版本变化定位：' + json.dumps({
                                'document_id':job['document_id'],'content_sha':job['content_sha'],
                                'changes':selection['changes']},ensure_ascii=False)
                        messages[-1]['content'] += '\n工作成果上下文（供关联，不替代对话原文）：\n' + context_for_model(context)
                        return leased_client.chat(messages, max_tokens=max_tokens)
                items = extract_knowledge(job["content"], transcript=batch is not None or job["adapter"] in ("codex", "claude", "cursor"),
                                          client=ExtractionClient(),scope=metadata,existing=known)
                if batch is None and selection['mode']=='incremental' and any(
                        item.get(field) and item[field] not in full_content
                        for item in items for field in ('quote','context_quote','confirmation_quote')):
                    selection = dict(selection,mode='full',reason='incremental_evidence_misaligned',content=full_content)
                    with self.db.connect() as con:
                        con.execute('UPDATE knowledge_processing_runs SET mode=?,reason=?,submitted_chars=? WHERE id=?',
                            ('full',selection['reason'],len(full_content),run_id))
                    items = extract_knowledge(full_content,transcript=False,client=ExtractionClient(),scope=metadata,existing=known)
                if batch:
                    items = [item for item in items if any(
                        item.get(field) and item[field] in batch['new_content']
                        for field in ('quote', 'confirmation_quote'))]
                with self.db.connect() as con:
                    document=con.execute('SELECT * FROM documents WHERE id=?',(job['document_id'],)).fetchone()
                    known_projects=catalog(con)
                    for item in items:
                        item['project_context'] = metadata['project_context']
                        item['work_context'] = work_context(con, job['document_id'], quote=item['quote'],
                                                           event_start=batch['from_seq'] + 1 if batch else None,
                                                           event_end=batch['through_seq'] if batch else None)
                links=plan_work_units(items,document,known_projects,self.decision_router)
                for item,link in zip(items,links):
                    item.update({k:link[k] for k in ('scope','project_key','project')})
                with self.db.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    # A revoked source or edited document must never be resurrected by an in-flight response.
                    current = con.execute("""SELECT d.sha256,d.deleted,s.enabled,s.allow_ai,t.generation,t.captured_seq
                        FROM documents d JOIN sources s ON s.id=d.source_id
                        LEFT JOIN transcript_streams t ON t.document_id=d.id WHERE d.id=?""", (job["document_id"],)).fetchone()
                    claimed=con.execute('SELECT 1 FROM ai_jobs WHERE id=? AND claim_token=? AND content_sha=?',(job['job_id'],job['claim_token'],job['content_sha'])).fetchone()
                    unchanged = current and (current['generation'] == batch['generation'] if batch else current['sha256'] == job['content_sha'])
                    if not claimed or not unchanged or current['deleted'] or not current["enabled"] or not current["allow_ai"]:
                        outcome = 'superseded'
                        return {"state": "superseded"}
                    if batch and not ack_batch(con, job['document_id'], batch['through_seq'], batch['generation']):
                        outcome = 'superseded'
                        return {'state': 'superseded'}
                    if batch is None and selection['mode']=='full':
                        # Old extraction summaries are not evidence after a rewrite.
                        # Reconfirmed quotes below become current again; omitted ones stay retired.
                        con.execute("""UPDATE knowledge_evidence SET superseded=1 WHERE document_id=?
                            AND knowledge_id IN (SELECT id FROM knowledge WHERE organization='material')""",(job['document_id'],))
                    n = store_candidates(con, job["document_id"], [], items,
                                         created_by="enterprise_ai")
                    apply_assignments(con,job['document_id'],items,links)
                    if batch is None:
                        save_baseline(con,job['document_id'],job['content_sha'],full_content)
                    has_more = batch and current['captured_seq'] > batch['through_seq']
                    con.execute("""UPDATE ai_jobs SET state=?,content_sha=?,error=NULL,next_run_at=NULL,
                        claim_token=NULL,batch_generation=NULL,batch_from_seq=NULL,batch_through_seq=NULL,
                        attempts=CASE WHEN ? THEN 0 ELSE attempts END,updated_at=datetime('now')
                        WHERE id=? AND content_sha=? AND claim_token=?""",
                        ('queued' if has_more else 'done',current['sha256'],bool(has_more),
                         job["job_id"], job["content_sha"],job["claim_token"]))
                outcome = 'done'
                self.db.event("ai_extracted", f"记录 {len(items)} 条待整理材料，新增 {n} 条")
                return {"state": "done", "candidates": len(items), "added": n, "proposals": 0,'automatic':0}
        except Exception as exc:
            # Do not persist provider error bodies, URLs, or credentials. Retry
            # transient failures a bounded number of times with backoff.
            safe_error = f"模型处理异常：{type(exc).__name__}"
            with self.db.connect() as con:
                attempts = con.execute("SELECT attempts FROM ai_jobs WHERE id=? AND content_sha=? AND claim_token=?",
                                       (job["job_id"],job["content_sha"],job["claim_token"])).fetchone()
                if attempts is None:
                    outcome = 'superseded'
                    return {"state": "superseded"}
                if attempts[0] < 4:
                    delay = min(30 * (2 ** (attempts[0] - 1)), 900)
                    when = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
                    con.execute("""UPDATE ai_jobs SET state='queued',next_run_at=?,error=?,
                        updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?""",
                        (when,safe_error,job["job_id"],job["content_sha"],job["claim_token"]))
                    outcome = 'retrying'
                    return {"state": "retrying", "after_seconds": delay}
                con.execute("""UPDATE ai_jobs SET state='error',next_run_at=NULL,error=?,
                    updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?""",
                    (safe_error,job["job_id"],job["content_sha"],job["claim_token"]))
                return {"state": "error", "error": safe_error}
        finally:
            processing_run.reset(metrics_context)
            with self.db.connect() as con:
                con.execute('UPDATE knowledge_processing_runs SET state=?,model_calls=?,elapsed_ms=? WHERE id=?',
                    (outcome,model_calls,int((time.monotonic()-started)*1000),run_id))
            self.processing = False
