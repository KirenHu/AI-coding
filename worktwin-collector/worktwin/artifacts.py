"""Small, source-bound observations of work files and their session context.

This module never runs recorded commands or opens paths mentioned in a chat.
It links already collected files to a session by directory and time; the link
is a candidate work relationship, not proof of authorship or project identity.
"""
from __future__ import annotations

import configparser
import difflib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree


def ensure_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS artifact_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            path TEXT NOT NULL,
            previous_path TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL,
            previous_sha TEXT NOT NULL DEFAULT '',
            change_kind TEXT NOT NULL,
            added_lines INTEGER NOT NULL DEFAULT 0,
            removed_lines INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL,
            repository_json TEXT NOT NULL DEFAULT '{}',
            validation_json TEXT NOT NULL DEFAULT '{}',
            observed_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(document_id,mtime_ns,sha256)
        );
        CREATE INDEX IF NOT EXISTS ix_artifact_document ON artifact_observations(document_id,id);
    """)


def _metadata_text(path: Path, root: Path) -> str:
    """Read only small Git metadata within the explicitly authorized folder."""
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        return ''
    try:
        with path.open('r', encoding='utf-8', errors='replace') as stream:
            return stream.read(65536)
    except OSError:
        return ''


def _repository_identity(url: str) -> str:
    # Credentials, query strings and fragments are never retained.
    if '://' not in url:
        match = re.fullmatch(r'(?:[^@\s]+@)?([\w.-]+):([\w./-]+)', url.strip())
        if not match:
            return ''
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ''
        host, path = parsed.hostname or '', parsed.path
    path = path.strip('/').removesuffix('.git')
    if not host or not re.fullmatch(r'[\w.-]+', host) or not re.fullmatch(r'[\w./-]+', path):
        return ''
    return host.lower() + '/' + path


def repository_metadata(path: Path, authorized_root: Path) -> dict:
    """Inspect HEAD and origin without subprocesses, hooks, or config includes."""
    root = authorized_root.resolve()
    current = path.resolve().parent
    while current.is_relative_to(root):
        git = current / '.git'
        if git.is_file() and not git.is_symlink():
            pointer = _metadata_text(git, root).strip()
            if pointer.startswith('gitdir: '):
                target = (current / pointer[8:]).resolve()
                if target.is_relative_to(root):
                    git = target
        if git.is_dir() and not git.is_symlink():
            common = git
            shared = _metadata_text(git / 'commondir', root).strip()
            if shared and (git / shared).resolve().is_relative_to(root):
                common = (git / shared).resolve()
            head = _metadata_text(git / 'HEAD', root).strip()
            branch, commit = '', ''
            if head.startswith('ref: '):
                ref = head[5:]
                if re.fullmatch(r'refs/[\w./-]+', ref) and '..' not in ref.split('/'):
                    branch = ref.removeprefix('refs/heads/')
                    commit = _metadata_text(common / ref, root).strip()
                    if not commit:
                        for line in _metadata_text(common / 'packed-refs', root).splitlines():
                            parts = line.split(' ')
                            if len(parts) == 2 and parts[1] == ref:
                                commit = parts[0]
                                break
            elif re.fullmatch(r'[0-9a-fA-F]{40,64}', head):
                commit = head
            config = configparser.RawConfigParser(strict=False)
            try:
                config.read_string(_metadata_text(common / 'config', root))
                remote = config.get('remote "origin"', 'url', fallback='')
            except configparser.Error:
                remote = ''
            return {'root': str(current), 'identity': _repository_identity(remote),
                    'branch': branch[:200],
                    'commit': commit if re.fullmatch(r'[0-9a-fA-F]{40,64}', commit) else ''}
        if current == root:
            break
        current = current.parent
    return {}


def _test_report(content: str) -> dict:
    """Retain JUnit counts only: a report supports its tests, not user acceptance."""
    if not content.lstrip().startswith('<'):
        return {}
    try:
        root = ElementTree.fromstring(content)
        if root.tag not in ('testsuite', 'testsuites'):
            return {}
        cases = list(root.iter('testcase'))
        if cases:
            tests = len(cases)
            failures = sum(case.find('failure') is not None for case in cases)
            errors = sum(case.find('error') is not None for case in cases)
            skipped = sum(case.find('skipped') is not None for case in cases)
        else:
            suites = [root] if root.tag == 'testsuite' or 'tests' in root.attrib else list(root)
            tests, failures, errors, skipped = (
                sum(int(s.get(key, '0')) for s in suites)
                for key in ('tests', 'failures', 'errors', 'skipped'))
        if min(tests, failures, errors, skipped) < 0 or failures + errors + skipped > tests:
            return {}
        return {'type': 'junit', 'tests': tests, 'failures': failures, 'errors': errors,
                'skipped': skipped, 'status': 'passed' if tests > skipped and not failures and not errors
                else ('failed' if failures or errors else 'no_tests')}
    except (ElementTree.ParseError, ValueError):
        return {}


def record_file_observation(con, document_id: int, *, previous_content=None, previous_sha='') -> int:
    """Call after a successful ordinary-file scan, in the same DB transaction."""
    doc = con.execute("""SELECT d.*,s.root,s.enabled,s.last_scanned,COALESCE(s.adapter,s.kind) adapter
        FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?""", (document_id,)).fetchone()
    if not doc or not doc['enabled'] or doc['deleted'] or doc['adapter'] in ('codex', 'claude'):
        return 0
    added = removed = 0
    if previous_content is not None:
        for tag, start, end, new_start, new_end in difflib.SequenceMatcher(
                None, previous_content.splitlines(), doc['content'].splitlines()).get_opcodes():
            if tag in ('replace', 'delete'):
                removed += end - start
            if tag in ('replace', 'insert'):
                added += new_end - new_start
    repository = repository_metadata(Path(doc['path']), Path(doc['root']))
    validation = _test_report(doc['content']) if doc['file_type'] == '.xml' else {}
    kind = 'file_changed' if previous_content is not None else ('created' if doc['last_scanned'] else 'observed')
    previous_path = ''
    if previous_content is None:
        matches = [row['path'] for row in con.execute("""SELECT path FROM documents
            WHERE source_id=? AND sha256=? AND id!=?""", (doc['source_id'], doc['sha256'], document_id))
            if not Path(row['path']).exists()]
        if len(matches) == 1:
            kind, previous_path, previous_sha = 'renamed', matches[0], doc['sha256']
    con.execute("""INSERT OR IGNORE INTO artifact_observations
        (source_id,document_id,path,previous_path,sha256,previous_sha,change_kind,added_lines,removed_lines,mtime_ns,repository_json,validation_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (doc['source_id'], document_id, doc['path'], previous_path,
        doc['sha256'], previous_sha, kind, added, removed,
        doc['mtime_ns'], json.dumps(repository, ensure_ascii=False), json.dumps(validation)))
    return int(con.execute("""SELECT id FROM artifact_observations
        WHERE document_id=? AND mtime_ns=? AND sha256=?""",
        (document_id, doc['mtime_ns'], doc['sha256'])).fetchone()[0])


def record_file_deletion(con, document_id: int) -> int:
    """Keep a minimal deletion observation; source revocation still purges it."""
    doc = con.execute("""SELECT d.*,COALESCE(s.adapter,s.kind) adapter FROM documents d
        JOIN sources s ON s.id=d.source_id WHERE d.id=?""", (document_id,)).fetchone()
    if not doc or doc['adapter'] in ('codex', 'claude'):
        return 0
    rename = con.execute("""SELECT id FROM artifact_observations
        WHERE source_id=? AND previous_path=? AND sha256=? AND change_kind='renamed'
        ORDER BY id DESC LIMIT 1""", (doc['source_id'], doc['path'], doc['sha256'])).fetchone()
    if rename:
        return int(rename['id'])
    previous = con.execute("""SELECT repository_json FROM artifact_observations
        WHERE document_id=? ORDER BY id DESC LIMIT 1""", (document_id,)).fetchone()
    cursor = con.execute("""INSERT INTO artifact_observations
        (source_id,document_id,path,sha256,previous_sha,change_kind,mtime_ns,repository_json)
        VALUES(?,?,?,'',?,'deleted',?,?)""", (doc['source_id'], document_id,
        doc['path'], doc['sha256'], time.time_ns(), previous['repository_json'] if previous else '{}'))
    return int(cursor.lastrowid)


def _epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError):
        return None


def _parent_context(con, stream) -> dict:
    """A native fork relationship supplies candidates, never child citations."""
    if not stream or not stream['parent_session_id']:
        return {}
    parents = [dict(row) for row in con.execute("""SELECT t.* FROM transcript_streams t
        JOIN documents d ON d.id=t.document_id JOIN sources s ON s.id=d.source_id
        WHERE t.session_id=? AND t.adapter=? AND t.document_id!=?
          AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1""",
        (stream['parent_session_id'], stream['adapter'], stream['document_id']))]
    same_source = [parent for parent in parents if parent['source_id'] == stream['source_id']]
    candidates = same_source or parents
    if len(candidates) != 1:
        return {}
    parent = candidates[0]
    # Parent conversation can continue after a fork. Do not let those later
    # decisions rewrite what the child actually inherited when it started.
    started = con.execute("""SELECT MIN(timestamp) FROM transcript_events
        WHERE document_id=? AND generation=? AND timestamp!=''""",
        (stream['document_id'], stream['generation'])).fetchone()[0]
    if not started:
        return {}
    rows = con.execute("""SELECT document_id,seq,event_id,role,timestamp,text
        FROM transcript_events WHERE document_id=? AND generation=? AND kind='message'
          AND timestamp!='' AND timestamp<=? ORDER BY seq DESC LIMIT 4""",
        (parent['document_id'], parent['generation'], started)).fetchall()
    return {'document_id': parent['document_id'], 'session_id': parent['session_id'],
            'relation': 'fork_parent', 'events': [dict(row) for row in reversed(rows)]}


def work_context(con, document_id: int, quote: str = '', *, event_start=None,
                 event_end=None, event_ids=None) -> dict:
    """Get nearby source events and file observations with live AI grants.

    Directory + timestamp nominate related files; they do not establish which
    tool changed them. The current source text remains the citation authority.
    """
    doc = con.execute("""SELECT d.*,s.enabled,s.allow_ai FROM documents d
        JOIN sources s ON s.id=d.source_id WHERE d.id=?""", (document_id,)).fetchone()
    if not doc or doc['deleted'] or not doc['enabled'] or not doc['allow_ai']:
        return {}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stream = con.execute('SELECT * FROM transcript_streams WHERE document_id=?', (document_id,)).fetchone() \
        if 'transcript_streams' in tables else None
    cwd_match = re.search(r'^工作目录：([^\n]+)', doc['content'])
    cwd = str(stream['cwd']) if stream and stream['cwd'] else (cwd_match.group(1).strip() if cwd_match else '')
    events = []
    if stream and 'transcript_events' in tables:
        event_columns = {row[1] for row in con.execute('PRAGMA table_info(transcript_events)')}
        path_field = 'artifact_paths_json' if 'artifact_paths_json' in event_columns else "'[]' AS artifact_paths_json"
        generation = int(stream['generation']) if 'generation' in stream.keys() else 1
        generation_clause = f' AND generation={generation}' if 'generation' in event_columns else ''
        target = None
        if quote:
            target = con.execute(f"""SELECT seq FROM transcript_events WHERE document_id=?
                AND instr(text,?)>0 AND (? IS NULL OR seq<=?){generation_clause} ORDER BY seq DESC LIMIT 1""",
                (document_id, quote, event_end, event_end)).fetchone()
        if target:
            event_start = event_end = target['seq']
        elif quote:
            event_start = event_end = None
        elif event_ids and not quote:
            marks = ','.join('?' for _ in event_ids)
            bounds = con.execute(f"""SELECT MIN(seq),MAX(seq) FROM transcript_events
                WHERE document_id=? AND event_id IN ({marks}){generation_clause}""", (document_id, *event_ids)).fetchone()
            event_start, event_end = bounds
        if event_end is None and not quote:
            last = con.execute('SELECT MAX(seq) FROM transcript_events WHERE document_id=?' + generation_clause,
                               (document_id,)).fetchone()
            event_start = event_end = last[0]
        if event_end is not None:
            prior = con.execute(f"""SELECT seq FROM transcript_events WHERE document_id=? AND seq<?
                {generation_clause} ORDER BY seq DESC LIMIT 4""", (document_id, event_start or event_end)).fetchall()
            start = min([row[0] for row in prior] + [event_start or event_end])
            events = [dict(row) for row in con.execute(f"""SELECT seq,event_id,kind,role,timestamp,text,
                tool_name,call_id,exit_code,failed,{path_field} FROM transcript_events
                WHERE document_id=? AND seq BETWEEN ? AND ? {generation_clause} ORDER BY seq LIMIT 24""",
                (document_id, start, event_end))]
    if not events and not stream:
        # Existing installations can be used before their next incremental scan.
        from .knowledge import visible_turns
        turns = visible_turns(doc['content'])
        positions = [i for i, turn in enumerate(turns) if quote and quote in turn['text']]
        end = positions[-1] + 1 if positions else (0 if quote else len(turns))
        events = [{'event_id': '', 'role': turn['role'], 'text': turn['text'],
                   'timestamp': turn['occurred_at'], 'kind': 'message'}
                  for turn in turns[max(0, end - 5):end]]
    moments = [moment for event in events if (moment := _epoch(event['timestamp'])) is not None]
    path_events = {}
    for event in events:
        for path in json.loads(event.get('artifact_paths_json', '[]')):
            path_events.setdefault(path, []).append(event['event_id'])
    artifacts = []
    if cwd and 'artifact_observations' in tables:
        directory = Path(cwd)
        rows = con.execute("""SELECT a.* FROM artifact_observations a
            JOIN sources s ON s.id=a.source_id
            WHERE s.enabled=1 AND s.allow_ai=1 ORDER BY a.id DESC LIMIT 500""")
        for row in rows:
            try:
                relative = str(Path(row['path']).relative_to(directory))
            except ValueError:
                continue
            if moments and not min(moments) - 300 <= row['mtime_ns'] / 1e9 <= max(moments) + 600:
                continue
            artifacts.append({'id': row['id'], 'document_id': row['document_id'], 'path': relative,
                'previous_path': str(Path(row['previous_path']).relative_to(directory))
                    if row['previous_path'] and Path(row['previous_path']).is_relative_to(directory) else '',
                'sha256': row['sha256'], 'previous_sha': row['previous_sha'],
                'change_kind': row['change_kind'], 'added_lines': row['added_lines'],
                'removed_lines': row['removed_lines'], 'mtime_ns': row['mtime_ns'],
                'observed_at': row['observed_at'], 'repository': json.loads(row['repository_json']),
                'validation': json.loads(row['validation_json']),
                'tool_event_ids': path_events.get(row['path'], []),
                'relationship': 'tool_path_and_observed_file' if row['path'] in path_events
                    else ('directory_and_time' if moments else 'directory_only')})
            if len(artifacts) >= 12:
                break
    repositories = list({json.dumps(item['repository'], sort_keys=True): item['repository']
                         for item in artifacts if item['repository']}.values())
    for event in events:
        event['document_id'] = document_id
    return {'cwd': cwd, 'session_id': stream['session_id'] if stream else '',
            'events': events, 'artifacts': artifacts, 'repositories': repositories,
            'parent_context': _parent_context(con, stream)}


def context_for_model(context: dict) -> str:
    """Do not duplicate full dialogue; attach compact observable work evidence."""
    if not context:
        return ''
    tools = [{key: event.get(key) for key in ('event_id', 'timestamp', 'tool_name', 'exit_code', 'failed')}
             for event in context.get('events', []) if event.get('tool_name')]
    payload = {'cwd': context.get('cwd', ''), 'session_id': context.get('session_id', ''),
               'tools': tools, 'artifacts': context.get('artifacts', [])}
    if not tools and not payload['artifacts']:
        return ''
    return ('工作证据（上下文，不是用户发言；目录与时间关联不证明操作归属）：\n'
            + json.dumps(payload, ensure_ascii=False)
            + '\nAI声称完成=reported；file_changed仅证明文件改变；工具退出0不证明成果正确；'
            'JUnit passed只支持对应报告列出的测试，不能扩大成整体成果完成；accepted必须来自用户实际验收原文。')
