"""SQLite schema. Content and indexes are stored exclusively on this computer."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('folder','codex')),
  root TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  adapter TEXT,
  last_scanned TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  title TEXT NOT NULL,
  file_type TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_id, path)
);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source_id, deleted);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
  INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TABLE IF NOT EXISTS knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('fact','decision','process','preference')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','archived')),
  created_by TEXT NOT NULL DEFAULT 'rules',
  source_bound INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT UNIQUE,
  needs_review INTEGER NOT NULL DEFAULT 0,
  review_hold INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS knowledge_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  quote TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1,
  occurred_at TEXT,
  superseded INTEGER NOT NULL DEFAULT 0,
  UNIQUE(knowledge_id, document_id, quote)
);
CREATE TABLE IF NOT EXISTS knowledge_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  details TEXT NOT NULL,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  content_sha TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done','error')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  next_run_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_ai_jobs_state ON ai_jobs(state,updated_at);
CREATE TABLE IF NOT EXISTS knowledge_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  content_sha TEXT NOT NULL,
  target_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  target_version INTEGER NOT NULL DEFAULT 1,
  action TEXT NOT NULL CHECK(action IN ('enrich','replace','conflict')),
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  quote TEXT NOT NULL,
  reason TEXT NOT NULL,
  fingerprint TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','dismissed')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT,
  origin TEXT NOT NULL DEFAULT 'consolidation'
);
CREATE INDEX IF NOT EXISTS ix_knowledge_proposals_status ON knowledge_proposals(status,created_at);
-- A per-note scheduler receipt: one low-priority curation attempt per version.
CREATE TABLE IF NOT EXISTS knowledge_curation_runs (
  knowledge_id INTEGER PRIMARY KEY REFERENCES knowledge(id) ON DELETE CASCADE,
  last_version INTEGER NOT NULL,
  attempted_at TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS twins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS publications (
  twin_id INTEGER PRIMARY KEY REFERENCES twins(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1,
  remote_id TEXT
);
CREATE TABLE IF NOT EXISTS twin_knowledge (
  twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  PRIMARY KEY(twin_id,knowledge_id)
);
CREATE TABLE IF NOT EXISTS twin_mcp (
  twin_id INTEGER PRIMARY KEY REFERENCES twins(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  allow_logs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS knowledge_acceptance (
  model_signature TEXT PRIMARY KEY,
  policy_version TEXT NOT NULL,
  report_json TEXT NOT NULL,
  accepted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Browser sessions are intentionally separate from file/AI coding documents.
-- No raw DOM, typed values, cookies, URLs with query strings, or screenshots.
CREATE TABLE IF NOT EXISTS browser_capture_signals (
  issuer TEXT NOT NULL,
  nonce TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY(issuer,nonce)
);
CREATE TABLE IF NOT EXISTS browser_capture_sessions (
  id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  request_id TEXT NOT NULL,
  workflow_project_id TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL DEFAULT '',
  target_path TEXT NOT NULL,
  target_url_hash TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  task_state TEXT NOT NULL DEFAULT 'open',
  capture_state TEXT NOT NULL DEFAULT 'pending',
  bound_tab_id INTEGER,
  bound_document_id TEXT,
  started_at INTEGER,
  stopped_at INTEGER,
  navigation_to TEXT,
  last_seq INTEGER NOT NULL DEFAULT 0,
  event_gaps INTEGER NOT NULL DEFAULT 0,
  analysis_seq INTEGER NOT NULL DEFAULT 0,
  analysis_text TEXT NOT NULL DEFAULT '',
  analysis_status TEXT NOT NULL DEFAULT 'none',
  analysis_attempted_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_browser_sessions_task
 ON browser_capture_sessions(issuer,task_id,subject,task_state);
CREATE TABLE IF NOT EXISTS browser_capture_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(session_id,seq)
);
CREATE TABLE IF NOT EXISTS browser_capture_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES browser_capture_events(id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL,
  summary TEXT NOT NULL,
  evidence_kind TEXT NOT NULL DEFAULT 'observed',
  UNIQUE(session_id,step_index)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Restrict app data to the logged-in user when creating it on POSIX.
        import os
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        if os.name == "posix":
            self.path.chmod(0o600)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Idempotent in-place upgrades from the 0.1 SQLite schema."""
        additions = {
            "sources": {"adapter": "TEXT", "allow_ai": "INTEGER NOT NULL DEFAULT 0", "allow_share": "INTEGER NOT NULL DEFAULT 0"},
            "documents": {"project": "TEXT NOT NULL DEFAULT ''", "project_key": "TEXT NOT NULL DEFAULT ''", "scope": "TEXT NOT NULL DEFAULT 'unknown'", "project_verified": "INTEGER NOT NULL DEFAULT 0"},
            "knowledge": {"source_bound": "INTEGER NOT NULL DEFAULT 0", "review_hold": "INTEGER NOT NULL DEFAULT 0",
                "scope": "TEXT NOT NULL DEFAULT 'unknown'", "project": "TEXT NOT NULL DEFAULT ''", "project_key": "TEXT NOT NULL DEFAULT ''",
                "topic": "TEXT NOT NULL DEFAULT ''", "scope_detail": "TEXT NOT NULL DEFAULT ''", "quality": "TEXT NOT NULL DEFAULT 'useful'",
                "quality_reason": "TEXT NOT NULL DEFAULT ''", "attribution": "TEXT NOT NULL DEFAULT 'human'", "outcome": "TEXT NOT NULL DEFAULT 'none'",
                "extraction_version": "INTEGER NOT NULL DEFAULT 0"},
            "knowledge_history": {"scope": "TEXT NOT NULL DEFAULT 'unknown'", "project": "TEXT NOT NULL DEFAULT ''", "project_key": "TEXT NOT NULL DEFAULT ''",
                "topic": "TEXT NOT NULL DEFAULT ''", "scope_detail": "TEXT NOT NULL DEFAULT ''", "quality": "TEXT NOT NULL DEFAULT 'uncertain'", "outcome": "TEXT NOT NULL DEFAULT 'none'"},
            "knowledge_proposals": {"target_version": "INTEGER NOT NULL DEFAULT 1", "origin": "TEXT NOT NULL DEFAULT 'consolidation'",
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'", "occurred_at": "TEXT"},
            "ai_jobs": {"next_run_at": "TEXT", "claim_token": "TEXT"},
            "knowledge_evidence": {"is_current": "INTEGER NOT NULL DEFAULT 1", "occurred_at":"TEXT", "superseded":"INTEGER NOT NULL DEFAULT 0"},
        }
        for table, fields in additions.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for field, definition in fields.items():
                if field not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
        conn.execute("UPDATE sources SET adapter=kind WHERE adapter IS NULL")
        conn.execute("UPDATE documents SET project=(SELECT name FROM sources WHERE sources.id=documents.source_id) WHERE project=''")
        conn.execute("UPDATE knowledge SET source_bound=1 WHERE fingerprint IS NOT NULL OR id IN (SELECT knowledge_id FROM knowledge_evidence)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_documents_project ON documents(project)")
        from .scope import document_scope, low_value
        for row in conn.execute("SELECT * FROM documents WHERE project_key=''").fetchall():
            metadata = document_scope(row)
            conn.execute('UPDATE documents SET project_key=?,scope=? WHERE id=?', (metadata['project_key'],metadata['scope'],row['id']))
        # Run exactly once on upgrade, preserving text, history and assignments.
        # Legacy extraction did not carry enough context to establish scope.
        if not conn.execute("SELECT 1 FROM settings WHERE key='scoped_knowledge_v1'").fetchone():
            for row in conn.execute('SELECT * FROM knowledge').fetchall():
                docs = conn.execute('''SELECT DISTINCT d.project,d.project_key FROM knowledge_evidence e
                    JOIN documents d ON d.id=e.document_id WHERE e.knowledge_id=?''',(row['id'],)).fetchall()
                legacy = row['created_by'] != 'human' or bool(row['source_bound'])
                noise = low_value(row['title'],row['body'])
                project = docs[0]['project'] if len(docs)==1 else ''
                key = docs[0]['project_key'] if len(docs)==1 else ''
                conn.execute('''UPDATE knowledge SET scope=?,project=?,project_key=?,quality=?,quality_reason=?,
                    review_hold=CASE WHEN ? THEN 1 ELSE review_hold END,
                    needs_review=CASE WHEN ? THEN 1 ELSE needs_review END WHERE id=?''',
                    ('unknown' if legacy else 'global',project,key,'noise' if noise else 'uncertain' if legacy else 'useful',
                     '旧知识缺少讨论上下文，请核对适用范围与内容价值' if legacy else '',legacy,legacy,row['id']))
            conn.execute("INSERT INTO settings(key,value) VALUES('scoped_knowledge_v1','1')")
        conn.execute('CREATE INDEX IF NOT EXISTS ix_knowledge_topic ON knowledge(project_key,topic,scope)')
        # 1.1.7: the old collector treated unverified folder labels as
        # project identities. Repair only those automatically inferred keys.
        # Preserve verified assignments and manually corrected scope; freeze
        # any notes that still carry an unverified directory-derived scope
        # allowing old cross-document associations to remain usable.
        if not conn.execute("SELECT 1 FROM settings WHERE key='project_identity_v2'").fetchone():
            unsafe = [r['id'] for r in conn.execute("""
                SELECT id FROM documents WHERE project_verified=0 AND
                  scope='project' AND project_key LIKE 'source:%'""")]
            for doc_id in unsafe:
                conn.execute("UPDATE documents SET project_key=?,scope='session' WHERE id=?",
                             ('session:' + str(doc_id), doc_id))
                conn.execute("""UPDATE knowledge SET review_hold=1,needs_review=1 WHERE
                    source_bound=1 AND project_key LIKE 'source:%' AND id IN
                    (SELECT knowledge_id FROM knowledge_evidence WHERE document_id=?)""", (doc_id,))
            conn.execute("INSERT INTO settings(key,value) VALUES('project_identity_v2','1')")


    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def event(self, action: str, detail: str, source_id: int | None = None) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO events(action,details,source_id) VALUES(?,?,?)", (action,detail,source_id))

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
