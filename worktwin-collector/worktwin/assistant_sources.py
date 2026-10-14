"""Whole-tool transcript selection, independent of model and sharing consent."""
from pathlib import Path
import json

from fastapi import HTTPException

from . import config
from .source_detection import detect_transcripts

TOOLS = {'codex': 'Codex', 'claude': 'Claude Code', 'cursor': 'Cursor CLI', 'antigravity': 'Google Antigravity'}


def tool_root(tool: str) -> Path:
    return {'codex': config.codex_sessions_path,
            'claude': config.claude_projects_path,
            'cursor': config.cursor_exports_path,
            'antigravity': config.antigravity_path}[tool]().expanduser().resolve()


def migrate(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS assistant_references (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            role TEXT NOT NULL, scope_path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assistant_processing_policy (
            source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
            since TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS assistant_history_requests (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            event_ids TEXT NOT NULL, after_seq INTEGER NOT NULL DEFAULT 0,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL);
    """)

    if not con.execute("SELECT 1 FROM settings WHERE key='assistant_reference_roles_v1'").fetchone():
        from .reference_roles import reference_role, reference_scope
        for row in con.execute('SELECT d.id,d.path,s.root FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.deleted=0').fetchall():
            role=reference_role(row['path'])
            if role:
                con.execute('INSERT OR IGNORE INTO assistant_references VALUES(?,?,?)',(row['id'],role,reference_scope(row['path'],row['root'])))
        con.execute("INSERT INTO settings(key,value) VALUES('assistant_reference_roles_v1','1')")
    if 'collection_scope' not in {r[1] for r in con.execute('PRAGMA table_info(sources)')}:
        con.execute("ALTER TABLE sources ADD COLUMN collection_scope TEXT NOT NULL DEFAULT 'custom'")
    if con.execute("SELECT 1 FROM settings WHERE key='whole_assistant_sources_v1'").fetchone():
        return
    for row in con.execute("SELECT id,adapter,root FROM sources WHERE adapter IN ('codex','claude','cursor')").fetchall():
        whole = Path(row['root']).expanduser().resolve() == tool_root(row['adapter'])
        con.execute('UPDATE sources SET collection_scope=?,enabled=CASE WHEN ? THEN enabled ELSE 0 END WHERE id=?',
                    ('assistant' if whole else 'legacy', whole, row['id']))
    con.execute("INSERT INTO settings(key,value) VALUES('whole_assistant_sources_v1','1')")


def assistant_rows(con) -> list[dict]:
    rows = [dict(r) for r in con.execute('''SELECT s.*,COUNT(d.id) document_count FROM sources s
        LEFT JOIN documents d ON d.source_id=s.id AND d.deleted=0
        WHERE s.adapter IN ('codex','claude','cursor','antigravity') GROUP BY s.id''')]
    result = []
    for tool, name in TOOLS.items():
        source = next((s for s in rows if s['adapter'] == tool and s['collection_scope'] == 'assistant'), None)
        root = Path(source['root']) if source else tool_root(tool)
        if tool == 'antigravity':
            from .antigravity import detect_artifacts
            detection = detect_artifacts(root)
        else:
            detection = detect_transcripts(tool, str(root))
        result.append({'tool': tool, 'name': name, 'selected': bool(source and source['enabled']),
                       'detected': bool(detection['recognized']), 'detection': detection,
                       'source_id': source['id'] if source else None,
                       'document_count': source['document_count'] if source else 0,
                       'waiting_tasks': con.execute('''SELECT count(*) FROM transcript_streams t WHERE t.source_id=?
                           AND EXISTS(SELECT 1 FROM transcript_events e WHERE e.document_id=t.document_id AND e.seq>t.processed_seq AND e.kind='task_started')
                           AND NOT EXISTS(SELECT 1 FROM transcript_events e WHERE e.document_id=t.document_id AND e.seq>t.processed_seq AND e.kind='task_complete')''',
                           (source['id'] if source else None,)).fetchone()[0],
                       'allow_ai': bool(source and source['allow_ai']),
                       'allow_share': bool(source and source['allow_share']),
                       'legacy_sources': [s for s in rows if s['adapter'] == tool and s['collection_scope'] != 'assistant']})
    return result


def select_assistant(con, tool: str, enabled: bool):
    if tool not in TOOLS:
        raise HTTPException(404, '不支持的编程助手')
    source = con.execute("SELECT id FROM sources WHERE adapter=? AND collection_scope='assistant'", (tool,)).fetchone()
    if source:
        con.execute('UPDATE sources SET enabled=? WHERE id=?', (enabled, source['id']))
    elif enabled:
        root = str(tool_root(tool))
        existing = con.execute('SELECT id,adapter FROM sources WHERE root=?', (root,)).fetchone()
        if existing:
            if existing['adapter'] != tool:
                raise HTTPException(409, '该目录已作为其他来源授权，请先在原来源中移除')
            con.execute("UPDATE sources SET collection_scope='assistant',enabled=1 WHERE id=?", (existing['id'],))
        else:
            con.execute('''INSERT INTO sources(name,kind,adapter,root,collection_scope,enabled)
                VALUES(?,?,?,?, 'assistant',1)''', (TOOLS[tool], 'codex' if tool == 'codex' else 'folder', tool, root))



def start_new_processing(con, source_id):
    """Consent applies prospectively; historical extraction requires a separate range."""
    con.execute("INSERT INTO assistant_processing_policy(source_id) VALUES(?) ON CONFLICT(source_id) DO UPDATE SET since=datetime('now')", (source_id,))
    con.execute("UPDATE transcript_streams SET processed_seq=captured_seq WHERE source_id=?", (source_id,))
    con.execute("DELETE FROM ai_jobs WHERE document_id IN (SELECT id FROM documents WHERE source_id=?)", (source_id,))
    con.execute("DELETE FROM assistant_history_requests WHERE document_id IN (SELECT id FROM documents WHERE source_id=?)", (source_id,))


def request_history(con, source_id, start, end):
    # Date inputs use explicit UTC day boundaries, displayed as such in the UI.
    from datetime import date
    try:
        begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, '请选择有效日期')
    if begin > finish:
        raise HTTPException(422, '开始日期不能晚于结束日期')
    source = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not source or source['adapter'] not in ('codex','claude','cursor'):
        raise HTTPException(400, '此来源不支持会话历史整理')
    if not source['enabled'] or not source['allow_ai']:
        raise HTTPException(409, '请先开启采集和允许 AI 使用')
    if con.execute("SELECT 1 FROM ai_jobs j JOIN documents d ON d.id=j.document_id WHERE d.source_id=? AND j.state IN ('queued','running')",(source_id,)).fetchone():
        raise HTTPException(409, '请等待当前整理完成后再选择历史记录')
    rows = con.execute("""SELECT e.document_id,e.seq,d.sha256 FROM transcript_events e
        JOIN documents d ON d.id=e.document_id JOIN transcript_streams t ON t.document_id=d.id
        WHERE d.source_id=? AND d.deleted=0 AND e.generation=t.generation
        AND substr(e.timestamp,1,10)>=? AND substr(e.timestamp,1,10)<=? ORDER BY e.seq""", (source_id,start,end)).fetchall()
    docs = {}
    for row in rows:
        docs.setdefault(row['document_id'], {'ids':[], 'sha':row['sha256']})['ids'].append(row['seq'])
    for document_id, data in docs.items():
        con.execute("INSERT OR REPLACE INTO assistant_history_requests VALUES(?,?,0,?,?)", (document_id,json.dumps(data['ids']),start,end))
        con.execute("""INSERT INTO ai_jobs(document_id,content_sha,state) VALUES(?,?,'queued')
            ON CONFLICT(document_id) DO UPDATE SET state='queued',attempts=0,error=NULL,next_run_at=NULL,
            batch_generation=NULL,batch_from_seq=NULL,batch_through_seq=NULL,content_sha=excluded.content_sha""", (document_id,data['sha']))
    return {'documents':len(docs), 'events':len(rows)}
