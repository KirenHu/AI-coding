"""Whole-tool transcript selection, independent of model and sharing consent."""
from pathlib import Path

from fastapi import HTTPException

from . import config
from .source_detection import detect_transcripts

TOOLS = {'codex': 'Codex', 'claude': 'Claude Code', 'cursor': 'Cursor CLI'}


def tool_root(tool: str) -> Path:
    return {'codex': config.codex_sessions_path,
            'claude': config.claude_projects_path,
            'cursor': config.cursor_exports_path}[tool]().expanduser().resolve()


def migrate(con):
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
        WHERE s.adapter IN ('codex','claude','cursor') GROUP BY s.id''')]
    result = []
    for tool, name in TOOLS.items():
        source = next((s for s in rows if s['adapter'] == tool and s['collection_scope'] == 'assistant'), None)
        root = Path(source['root']) if source else tool_root(tool)
        detection = detect_transcripts(tool, str(root))
        result.append({'tool': tool, 'name': name, 'selected': bool(source and source['enabled']),
                       'detected': bool(detection['recognized']), 'detection': detection,
                       'source_id': source['id'] if source else None,
                       'document_count': source['document_count'] if source else 0,
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

