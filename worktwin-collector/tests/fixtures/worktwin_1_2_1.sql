-- Generated from WorkTwin 1.2.1 (9d98e343), synthetic user data only.
-- Preserve this fixture as the pre-upgrade schema; do not regenerate with new code.
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE ai_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  content_sha TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done','error')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  next_run_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
, claim_token TEXT);
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(document_id, ordinal)
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TABLE documents (
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
  deleted INTEGER NOT NULL DEFAULT 0, project_key TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown', project_verified INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_id, path)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  details TEXT NOT NULL,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE knowledge (
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
, scope TEXT NOT NULL DEFAULT 'unknown', project TEXT NOT NULL DEFAULT '', project_key TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '', scope_detail TEXT NOT NULL DEFAULT '', quality TEXT NOT NULL DEFAULT 'useful', quality_reason TEXT NOT NULL DEFAULT '', attribution TEXT NOT NULL DEFAULT 'human', outcome TEXT NOT NULL DEFAULT 'none', extraction_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE knowledge_acceptance (
  model_signature TEXT PRIMARY KEY,
  policy_version TEXT NOT NULL,
  report_json TEXT NOT NULL,
  accepted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE knowledge_curation_runs (
  knowledge_id INTEGER PRIMARY KEY REFERENCES knowledge(id) ON DELETE CASCADE,
  last_version INTEGER NOT NULL,
  attempted_at TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE knowledge_evidence (
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
CREATE TABLE knowledge_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  changed_at TEXT NOT NULL DEFAULT (datetime('now'))
, scope TEXT NOT NULL DEFAULT 'unknown', project TEXT NOT NULL DEFAULT '', project_key TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT '', scope_detail TEXT NOT NULL DEFAULT '', quality TEXT NOT NULL DEFAULT 'uncertain', outcome TEXT NOT NULL DEFAULT 'none');
CREATE TABLE knowledge_proposals (
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
, evidence_json TEXT NOT NULL DEFAULT '{}', occurred_at TEXT);
CREATE TABLE project_entities (
    project_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    anchor_type TEXT NOT NULL DEFAULT '',
    anchor TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE project_memberships (
    knowledge_id INTEGER PRIMARY KEY REFERENCES knowledge(id) ON DELETE CASCADE,
    project_key TEXT NOT NULL REFERENCES project_entities(project_key) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('confirmed','provisional')),
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE TABLE publications (
  twin_id INTEGER PRIMARY KEY REFERENCES twins(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 1,
  remote_id TEXT
);
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('folder','codex')),
  root TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  adapter TEXT,
  last_scanned TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
, allow_ai INTEGER NOT NULL DEFAULT 0, allow_share INTEGER NOT NULL DEFAULT 0);
CREATE TABLE twin_grants (
    twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('project','source','global','knowledge')),
    subject_key TEXT NOT NULL,
    effect TEXT NOT NULL DEFAULT 'allow' CHECK(effect IN ('allow','deny')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(twin_id,subject_type,subject_key,effect)
);
CREATE TABLE twin_knowledge (
  twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE,
  knowledge_id INTEGER NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
  PRIMARY KEY(twin_id,knowledge_id)
);
CREATE TABLE twin_mcp (
  twin_id INTEGER PRIMARY KEY REFERENCES twins(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  allow_logs INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE twins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
, knowledge_mode TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE work_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    quote_hash TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    project_hint TEXT NOT NULL DEFAULT '',
    project_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'isolated'
       CHECK(status IN ('isolated','provisional','confirmed')),
    confidence REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(document_id,quote_hash)
);
CREATE INDEX ix_ai_jobs_state ON ai_jobs(state,updated_at);
CREATE INDEX ix_chunks_document ON chunks(document_id);
CREATE INDEX ix_documents_project ON documents(project);
CREATE INDEX ix_documents_source ON documents(source_id, deleted);
CREATE INDEX ix_knowledge_proposals_status ON knowledge_proposals(status,created_at);
CREATE INDEX ix_knowledge_topic ON knowledge(project_key,topic,scope);
CREATE UNIQUE INDEX ix_project_entities_anchor ON project_entities(anchor_type,anchor)
    WHERE anchor_type!='' AND anchor!='';
CREATE INDEX ix_project_memberships_project ON project_memberships(project_key,status);
CREATE INDEX ix_twin_grants_subject ON twin_grants(subject_type,subject_key);
CREATE INDEX ix_work_units_project ON work_units(project_key,status);
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
  INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
END;
INSERT INTO "ai_jobs" VALUES(1,11,'legacy-digest','done',1,NULL,NULL,'2026-10-10 10:17:54',NULL);
INSERT INTO "documents" VALUES(11,7,'/legacy/authorized/sessions/one.jsonl','one.jsonl','旧对话','.jsonl','WorkTwin','用户确认采用统一工作流。','legacy-digest',72,12345,'2026-10-10 10:17:54',0,'auto:legacy-project','project',0);
INSERT INTO "knowledge" VALUES(41,'decision','工作流决策','采用统一工作流。','confirmed','enterprise_ai',1,NULL,0,0,3,'2026-10-10 10:17:54','2026-10-10 10:17:54','project','WorkTwin','auto:legacy-project','架构','工作流实现','useful','','user','none',0);
INSERT INTO "knowledge_evidence" VALUES(1,41,11,NULL,'用户确认采用统一工作流。',1,NULL,0);
INSERT INTO "knowledge_history" VALUES(1,41,1,'decision','工作流决策','初始方案。','confirmed','2026-10-10 10:17:54','project','WorkTwin','auto:legacy-project','架构','工作流实现','useful','none');
INSERT INTO "knowledge_history" VALUES(2,41,2,'decision','工作流决策','修订方案。','confirmed','2026-10-10 10:17:54','project','WorkTwin','auto:legacy-project','架构','工作流实现','useful','none');
INSERT INTO "project_entities" VALUES('auto:legacy-project','WorkTwin','repo','example/worktwin','auto','2026-10-10 10:17:54');
INSERT INTO "project_memberships" VALUES(41,'auto:legacy-project',1.0,'confirmed','','2026-10-10 10:17:54');
INSERT INTO "settings" VALUES('scoped_knowledge_v1','1');
INSERT INTO "settings" VALUES('project_identity_v2','1');
INSERT INTO "settings" VALUES('autonomous_legacy_scoping_v1','1');
INSERT INTO "sources" VALUES(7,'Legacy work','codex','/legacy/authorized/sessions',1,'codex',NULL,NULL,'2026-10-10 10:17:54',1,1);
INSERT INTO "twin_grants" VALUES(8,'project','auto:legacy-project','allow','2026-10-10 10:17:54');
INSERT INTO "twin_knowledge" VALUES(8,41);
INSERT INTO "twin_mcp" VALUES(8,'e93f0319617fe29b92931e93350f59e83e19507b7b47e995b61c41691aefe0f8',1,1,'2026-10-10 10:17:54');
INSERT INTO "twins" VALUES(8,'项目交接','','2026-10-10 10:17:54','2026-10-10 10:17:54','dynamic');
COMMIT;
PRAGMA foreign_keys=ON;
