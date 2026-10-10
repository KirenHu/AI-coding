"""Upgrade the pinned 1.2.1 database, keeping user knowledge and access intact.

The fixture was dumped from commit 9d98e343. It must not be regenerated with the
current Database constructor: doing so would stop exercising a real upgrade.
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.ingest import pending_batch
from worktwin.twin_access import effective_notes
from worktwin.twin_mcp import grant

FIXTURE = Path(__file__).parent / 'fixtures' / 'worktwin_1_2_1.sql'
TOKEN = 'legacy-upgrade-test-token'
PRESERVED_TABLES = ('sources', 'documents', 'knowledge', 'knowledge_history',
                    'knowledge_evidence', 'twins', 'twin_knowledge', 'twin_grants',
                    'twin_mcp', 'project_entities', 'project_memberships')


def legacy_database(path):
    with sqlite3.connect(path) as con:
        con.executescript(FIXTURE.read_text(encoding='utf-8'))
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    return path


def snapshot(path):
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        return {table: [dict(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in PRESERVED_TABLES}


def test_upgrade_preserves_knowledge_versions_access_and_legacy_backup(tmp_path):
    path = legacy_database(tmp_path / 'worktwin.sqlite')
    before = snapshot(path)
    db = Database(path)
    assert snapshot(path) == before
    with db.connect() as con:
        assert [note['id'] for note in effective_notes(con, 8)] == [41]
        assert grant(con, TOKEN)['twin_id'] == 8
        assert grant(con, TOKEN)['allow_logs'] == 1
        assert con.execute('SELECT version FROM knowledge WHERE id=41').fetchone()[0] == 3
        assert con.execute('SELECT version FROM knowledge_history ORDER BY version').fetchall()[0][0] == 1
        assert con.execute('SELECT COUNT(*) FROM transcript_streams').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM transcript_events').fetchone()[0] == 0
        job = con.execute('SELECT state,batch_generation,batch_from_seq,batch_through_seq FROM ai_jobs').fetchone()
        assert tuple(job) == ('done', None, None, None)
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    backup = path.with_name(path.name + '.pre-1.3.0.sqlite')
    assert backup.is_file()
    assert snapshot(backup) == before
    with sqlite3.connect(backup) as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute("SELECT name FROM sqlite_master WHERE name='transcript_streams'").fetchone() is None
        assert con.execute("SELECT value FROM settings WHERE key='continuous_evidence_v1'").fetchone() is None
        assert 'batch_generation' not in {row[1] for row in con.execute('PRAGMA table_info(ai_jobs)')}
    original_backup = hashlib.sha256(backup.read_bytes()).hexdigest()
    original_mtime = backup.stat().st_mtime_ns
    # Reopening after real user changes must not replace the recovery point.
    db.set_setting('post_upgrade_user_setting', 'saved')
    Database(path)
    assert backup.stat().st_mtime_ns == original_mtime
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original_backup
    assert snapshot(path) == before
    assert db.setting('post_upgrade_user_setting') == 'saved'


def test_fresh_database_does_not_create_an_empty_upgrade_backup(tmp_path):
    path = tmp_path / 'fresh.sqlite'
    Database(path)
    Database(path)
    assert not list(tmp_path.glob('*.pre-1.3.0.sqlite'))


def test_first_upgrade_scan_replaces_a_rewritten_legacy_transcript(tmp_path):
    path = legacy_database(tmp_path / 'legacy.sqlite')
    root = tmp_path / 'sessions'
    root.mkdir()
    transcript = root / 'one.jsonl'
    fresh_quote = '用户明确改为分步发布，旧流程已经取消。'
    transcript.write_text(json.dumps({'timestamp': '2026-10-10T10:00:00Z',
        'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': fresh_quote}]}}, ensure_ascii=False) + '\n', encoding='utf-8')
    with sqlite3.connect(path) as con:
        con.execute('UPDATE sources SET root=? WHERE id=7', (str(root),))
        con.execute('UPDATE documents SET path=?,size_bytes=4000 WHERE id=11', (str(transcript),))
    db = Database(path)
    Collector(db).scan_all()
    with db.connect() as con:
        doc = con.execute('SELECT * FROM documents WHERE id=11').fetchone()
        assert fresh_quote in doc['content']
        assert '用户确认采用统一工作流。' not in doc['content']
        batch = pending_batch(con, 11)
        assert batch and fresh_quote in batch['new_content']
        assert con.execute('SELECT is_current FROM knowledge_evidence WHERE knowledge_id=41').fetchone()[0] == 0
        assert 41 not in {note['id'] for note in effective_notes(con, 8)}
        # The old note remains addressable for history and correction.
        assert con.execute('SELECT id FROM knowledge WHERE id=41').fetchone()[0] == 41


def test_cursor_source_api_uses_export_adapter_without_default_model_consent(tmp_path):
    root = tmp_path / 'cursor-exports'
    root.mkdir()
    app = create_app(tmp_path / 'cursor.sqlite', start_worker=False)
    with TestClient(app) as client:
        token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
        auth = {'X-Worktwin-Token': token}
        result = client.post('/api/sources', headers=auth, json={
            'name': 'Cursor CLI', 'kind': 'cursor', 'root': str(root)})
        assert result.status_code == 200, result.text
        source = next(row for row in client.get('/api/sources', headers=auth).json()
                      if row['id'] == result.json()['id'])
        assert source['source_type'] == source['adapter'] == 'cursor'
        assert source['kind'] == 'folder'  # Existing on-disk kind constraint stays compatible.
        assert source['allow_ai'] == 0 and source['allow_share'] == 0
        assert client.get('/api/ai/jobs', headers=auth).json() == []


def test_completed_legacy_transcript_only_processes_post_upgrade_append(tmp_path):
    from worktwin.codex import parse_codex_session

    path = legacy_database(tmp_path / 'appended.sqlite')
    root = tmp_path / 'logs'
    root.mkdir()
    transcript = root / 'one.jsonl'
    old_quote = '用户确认采用统一工作流。'
    new_quote = '新增决定分两个批次发布。'

    def line(text, timestamp):
        return json.dumps({'timestamp': timestamp, 'type': 'response_item',
            'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': text}]}}, ensure_ascii=False) + '\n'

    legacy_bytes = line(old_quote, '2026-10-09T10:00:00Z').encode()
    transcript.write_bytes(legacy_bytes)
    _, legacy_content = parse_codex_session(transcript)
    with sqlite3.connect(path) as con:
        con.execute('UPDATE sources SET root=? WHERE id=7', (str(root),))
        con.execute('UPDATE documents SET path=?,content=?,size_bytes=?,mtime_ns=?,sha256=? WHERE id=11',
                    (str(transcript), legacy_content, len(legacy_bytes),
                     transcript.stat().st_mtime_ns, hashlib.sha256(legacy_bytes).hexdigest()))
    # The file grows while the previous version is closed; existing knowledge
    # remains valid and must not be reprocessed just because the schema changed.
    with transcript.open('ab') as file:
        file.write(line(new_quote, '2026-10-10T10:00:00Z').encode())
    db = Database(path)
    Collector(db).scan_all()
    with db.connect() as con:
        batch = pending_batch(con, 11)
        assert batch and new_quote in batch['new_content']
        assert old_quote not in batch['new_content']
        assert old_quote in batch['content']  # Retained as interpretation context.
        doc = con.execute('SELECT content FROM documents WHERE id=11').fetchone()[0]
        assert doc.count(old_quote) == 1 and doc.count(new_quote) == 1
        assert con.execute('SELECT is_current FROM knowledge_evidence WHERE knowledge_id=41').fetchone()[0] == 1
        assert con.execute('SELECT version FROM knowledge WHERE id=41').fetchone()[0] == 3
        assert [note['id'] for note in effective_notes(con, 8)] == [41]
