"""Low-priority scheduled AI knowledge processing, independent from capture."""

from __future__ import annotations

import threading
import secrets
from datetime import datetime, timedelta, timezone
from .db import Database
from .inference import GatewayClient, extract_knowledge
from .knowledge import store_candidates
from .parsers import split_chunks
from .reconcile import existing_for_project, make_consolidation_plan, store_proposals
from .gardener import KnowledgeGardener
from .scope import document_scope
from .projects import catalog, plan_work_units, apply_assignments
from .project_recheck import ProjectRechecker
from .decision_model import DecisionRouter
from .credentials import DesktopSecrets
from .automation import activate_new, apply_additions, model_signature
from .autonomy import apply_safe_replacements


class KnowledgeWorker:
    def __init__(self, db: Database, *, client: GatewayClient | None = None, interval: int = 30, decision_router=None):
        self.db = db
        self.client = client or GatewayClient()
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.processing = False
        self.gardener = KnowledgeGardener(db, client=self.client)
        self.decision_router=decision_router or DecisionRouter(db,DesktopSecrets(db.path),self.client)
        self.project_rechecker=ProjectRechecker(db,self.decision_router)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="worktwin-knowledge", daemon=True)
        self._thread.start()
        self.schedule()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=4)

    def schedule(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self.client.configured and not self._stop.is_set():
                # A bounded batch avoids a growing backlog while limiting the
                # number of enterprise model calls per scheduling cycle.
                result = "idle"
                for _ in range(3):
                    if self._stop.is_set():
                        break
                    result = self.process_next()["state"]
                    if result in ("idle", "not_configured"):
                        break
                # Maintenance is lower priority than fresh capture: one note
                # per idle cycle and no call if extraction is still backed up.
                if result == "idle" and not self._stop.is_set():
                    # A new project may resolve previously isolated sessions.
                    # Work through a bounded batch before lower priority text
                    # curation; no user confirmation or full-corpus re-extraction.
                    self.project_rechecker.process_next()
                    self.gardener.client = self.client
                    self.gardener.process_next()

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
                    d.id,d.source_id,d.project,d.project_key,d.scope,d.project_verified,d.file_type,COALESCE(s.adapter,s.kind) adapter
                    FROM ai_jobs j JOIN documents d ON d.id=j.document_id
                    JOIN sources s ON s.id=d.source_id
                    WHERE j.state='queued' AND (j.next_run_at IS NULL OR j.next_run_at<=datetime('now'))
                    AND s.enabled=1 AND s.allow_ai=1 AND d.sha256=j.content_sha
                    ORDER BY j.updated_at ASC,j.id ASC LIMIT 1""").fetchone()
            if not row:
                return {"state": "idle"}
            job = dict(row)
            job['claim_token']=secrets.token_hex(16)
            con.execute("UPDATE ai_jobs SET state='running',claim_token=?,attempts=attempts+1,updated_at=datetime('now') WHERE id=?", (job['claim_token'],job["job_id"]))
        self.processing = True
        try:
            outer=self
            class LeasedClient:
                configured=True
                def chat(self,messages,max_tokens=1800):
                    with outer.db.connect() as con:
                        claimed=con.execute("UPDATE ai_jobs SET updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?",(job['job_id'],job['content_sha'],job['claim_token']))
                        if not claimed.rowcount:
                            raise RuntimeError('job superseded')
                    return outer.client.chat(messages,max_tokens=max_tokens)
            leased_client=LeasedClient()
            with self.db.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                metadata=document_scope(job)
                con.execute('UPDATE documents SET project_key=?,scope=? WHERE id=?',(metadata['project_key'],metadata['scope'],job['document_id']))
                known = existing_for_project(con, job["document_id"])
            model_identity=model_signature(self.client)
            items = extract_knowledge(job["content"], transcript=job["adapter"] in ("codex", "claude"),
                                      client=leased_client,scope=metadata,existing=known)
            with self.db.connect() as con:
                document=con.execute('SELECT * FROM documents WHERE id=?',(job['document_id'],)).fetchone()
                known_projects=catalog(con)
            links=plan_work_units(items,document,known_projects,self.decision_router)
            for item,link in zip(items,links):
                item.update({k:link[k] for k in ('scope','project_key','project')})
            # Existing articles are retrieved by work-unit identity, not the
            # containing file's directory. Cross-source AI grants revalidate.
            with self.db.connect() as con:
                known_by_id={k['id']:k for k in known}
                for item in items:
                    for k in existing_for_project(con,job['document_id'],
                            project_key=item['project_key'],scope=item['scope']):
                        known_by_id[k['id']]=k
            known=list(known_by_id.values())[:45]
            plan=make_consolidation_plan(leased_client,items,known)
            plan={i:p for i,p in plan.items() if i<len(items)
                  and p['target_id'] in known_by_id
                  and known_by_id[p['target_id']]['project_key']==items[i]['project_key']
                  and known_by_id[p['target_id']]['scope']==items[i]['scope']}
            with self.db.connect() as con:
                con.execute('BEGIN IMMEDIATE')
                # A revoked source or edited document must never be resurrected by an in-flight response.
                current = con.execute("""SELECT d.sha256,s.enabled,s.allow_ai FROM documents d
                    JOIN sources s ON s.id=d.source_id WHERE d.id=?""", (job["document_id"],)).fetchone()
                claimed=con.execute('SELECT 1 FROM ai_jobs WHERE id=? AND claim_token=? AND content_sha=?',(job['job_id'],job['claim_token'],job['content_sha'])).fetchone()
                if not claimed or not current or current["sha256"] != job["content_sha"] or not current["enabled"] or not current["allow_ai"]:
                    return {"state": "superseded"}
                # Re-validate model references within the DB transaction.
                plan = {i:p for i,p in plan.items() if p['target_id'] in {
                    r['id'] for r in existing_for_project(con,job['document_id'],
                        project_key=items[i]['project_key'],scope=items[i]['scope'])}}
                proposals = store_proposals(con, job['document_id'], job['content_sha'], items, plan)
                fresh = [item for i,item in enumerate(items) if i not in plan]
                n = store_candidates(con, job["document_id"], split_chunks(job["content"]), fresh,
                                     created_by="enterprise_ai")
                apply_assignments(con,job['document_id'],items,links)
                automatic=(apply_additions(con,job['document_id'],self.client)+apply_safe_replacements(con,job['document_id'])+activate_new(con,job['document_id'],fresh,self.client)
                           if model_identity==model_signature(self.client) else 0)
                con.execute("UPDATE ai_jobs SET state='done',error=NULL,next_run_at=NULL,updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?",
                            (job["job_id"], job["content_sha"],job["claim_token"]))
            self.db.event("ai_extracted", f"自动整理 {len(items)} 条知识，新增引用 {n} 条，待复核更新 {proposals} 项")
            return {"state": "done", "candidates": len(items), "added": n, "proposals": proposals,'automatic':automatic}
        except Exception as exc:
            # Do not persist provider error bodies, URLs, or credentials. Retry
            # transient failures a bounded number of times with backoff.
            safe_error = f"模型处理异常：{type(exc).__name__}"
            with self.db.connect() as con:
                attempts = con.execute("SELECT attempts FROM ai_jobs WHERE id=? AND content_sha=? AND claim_token=?",
                                       (job["job_id"],job["content_sha"],job["claim_token"])).fetchone()
                if attempts is None:
                    return {"state": "superseded"}
                if attempts[0] < 4:
                    delay = min(30 * (2 ** (attempts[0] - 1)), 900)
                    when = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
                    con.execute("""UPDATE ai_jobs SET state='queued',next_run_at=?,error=?,
                        updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?""",
                        (when,safe_error,job["job_id"],job["content_sha"],job["claim_token"]))
                    return {"state": "retrying", "after_seconds": delay}
                con.execute("""UPDATE ai_jobs SET state='error',next_run_at=NULL,error=?,
                    updated_at=datetime('now') WHERE id=? AND content_sha=? AND claim_token=?""",
                    (safe_error,job["job_id"],job["content_sha"],job["claim_token"]))
                return {"state": "error", "error": safe_error}
        finally:
            self.processing = False
