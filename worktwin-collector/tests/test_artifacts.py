"""Observable file results must stay distinct from reports and acceptance."""
import hashlib
import json
import os
from datetime import datetime, timezone

from worktwin.artifacts import (
    context_for_model, ensure_schema, record_file_observation,
    record_file_deletion, repository_metadata, work_context,
)
from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.ingest import ensure_schema as ensure_ingest_schema


def setup_source(tmp_path, *, allow_ai=True):
    folder = tmp_path / 'project'
    folder.mkdir()
    db = Database(tmp_path / 'state' / 'db.sqlite')
    with db.connect() as con:
        ensure_schema(con)
        source_id = con.execute("""INSERT INTO sources(name,kind,adapter,root,allow_ai)
            VALUES('Project','folder','folder',?,?)""", (str(folder), int(allow_ai))).lastrowid
    return db, folder, source_id


def collect_document(con, source_id, path, text, timestamp, *, previous=None):
    digest = hashlib.sha256(text.encode()).hexdigest()
    found = con.execute('SELECT * FROM documents WHERE source_id=? AND path=?',
                        (source_id, str(path))).fetchone()
    if found:
        con.execute('UPDATE documents SET content=?,sha256=?,mtime_ns=? WHERE id=?',
                    (text, digest, timestamp, found['id']))
        docid = found['id']
    else:
        docid = con.execute("""INSERT INTO documents
            (source_id,path,relative_path,title,file_type,content,sha256,size_bytes,mtime_ns)
            VALUES(?,?,?,?,?,?,?,?,?)""", (source_id, str(path), path.name, path.stem,
            path.suffix, text, digest, len(text), timestamp)).lastrowid
    observation = record_file_observation(con, docid, previous_content=previous,
                                         previous_sha=found['sha256'] if found else '')
    return docid, observation


def add_session(con, folder, content):
    source_id = con.execute("""INSERT INTO sources(name,kind,adapter,root,allow_ai)
        VALUES('Codex','codex','codex',?,1)""", (str(folder.parent / 'sessions'),)).lastrowid
    return con.execute("""INSERT INTO documents
        (source_id,path,relative_path,title,file_type,content,sha256,size_bytes,mtime_ns)
        VALUES(?,?,'session.jsonl','Session','.jsonl',?,'session-sha',1,1)""",
        (source_id, str(folder.parent / 'sessions' / 'session.jsonl'), content)).lastrowid


def test_file_revision_has_diff_counts_and_credential_free_git_identity(tmp_path):
    db, folder, source_id = setup_source(tmp_path)
    git = folder / '.git'
    (git / 'refs' / 'heads').mkdir(parents=True)
    (git / 'HEAD').write_text('ref: refs/heads/main\n')
    (git / 'refs' / 'heads' / 'main').write_text('a' * 40 + '\n')
    (git / 'config').write_text('[remote "origin"]\nurl = https://user:SECRET@github.com/acme/project.git?token=SECRET\n')
    with db.connect() as con:
        docid, initial = collect_document(con, source_id, folder / 'guide.md', 'old\nshared', 1)
        _, changed = collect_document(con, source_id, folder / 'guide.md', 'new\nshared\nSECRET', 2,
                                      previous='old\nshared')
        row = dict(con.execute('SELECT * FROM artifact_observations WHERE id=?', (changed,)).fetchone())
        assert changed != initial
        assert row['change_kind'] == 'file_changed'
        assert (row['added_lines'], row['removed_lines']) == (2, 1)
        assert row['previous_sha'] != row['sha256']
        assert 'SECRET' not in json.dumps(row)
        repo = json.loads(row['repository_json'])
        assert repo == {'root': str(folder), 'identity': 'github.com/acme/project',
                        'branch': 'main', 'commit': 'a' * 40}
        assert record_file_observation(con, docid, previous_content='old\nshared') == changed
        assert con.execute('SELECT COUNT(*) FROM artifact_observations').fetchone()[0] == 2


def test_git_metadata_does_not_follow_worktree_or_parent_outside_authorized_folder(tmp_path):
    parent = tmp_path / 'private'
    (parent / '.git').mkdir(parents=True)
    (parent / '.git' / 'HEAD').write_text('a' * 40)
    allowed = parent / 'allowed'
    allowed.mkdir()
    assert repository_metadata(allowed / 'file.py', allowed) == {}
    (allowed / '.git').write_text('gitdir: ../.git')
    assert repository_metadata(allowed / 'file.py', allowed) == {}


def test_git_worktree_metadata_inside_authorized_root(tmp_path):
    folder = tmp_path / 'authorized'
    worktree = folder / 'worktree'
    metadata = folder / 'main' / '.git'
    state = metadata / 'worktrees' / 'worktree'
    state.mkdir(parents=True)
    worktree.mkdir()
    (worktree / '.git').write_text('gitdir: ../main/.git/worktrees/worktree')
    (state / 'HEAD').write_text('ref: refs/heads/feature')
    (state / 'commondir').write_text('../..')
    (metadata / 'packed-refs').write_text('b' * 40 + ' refs/heads/feature\n')
    (metadata / 'config').write_text('[remote "origin"]\nurl = git@github.com:acme/project.git\n')
    assert repository_metadata(worktree / 'file.py', folder) == {
        'root': str(worktree), 'identity': 'github.com/acme/project',
        'branch': 'feature', 'commit': 'b' * 40}


def test_nearby_file_context_respects_time_directory_and_live_source_grants(tmp_path):
    db, folder, source_id = setup_source(tmp_path)
    moment = int(datetime(2026, 10, 10, 10, tzinfo=timezone.utc).timestamp() * 1e9)
    content = (f'工作目录：{folder}\n\n### 用户 · 2026-10-10 10:00:00\n'
               '为项目添加启动测试。\n\n### AI · 2026-10-10 10:00:10\n已添加测试，尚未执行。')
    with db.connect() as con:
        docid, _ = collect_document(con, source_id, folder / 'test_app.py', 'test()', moment)
        collect_document(con, source_id, folder / 'old.py', 'old()', moment - int(86400e9))
        session = add_session(con, folder, content)
        context = work_context(con, session, '已添加测试，尚未执行。')
        assert [a['document_id'] for a in context['artifacts']] == [docid]
        assert context['artifacts'][0]['change_kind'] == 'observed'
        assert context['artifacts'][0]['relationship'] == 'directory_and_time'
        # Changing the grant takes effect without rebuilding a cached context.
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?', (source_id,))
        assert work_context(con, session)['artifacts'] == []
        con.execute('DELETE FROM sources WHERE id=?', (source_id,))
        assert con.execute('SELECT COUNT(*) FROM artifact_observations').fetchone()[0] == 0


def test_context_only_uses_dialogue_before_quote_and_does_not_open_session_cwd(tmp_path, monkeypatch):
    db, folder, _ = setup_source(tmp_path)
    content = (f'工作目录：{folder}/unauthorized\n\n'
        '### 用户 · 2026-10-10 10:00:00\n讨论甲项目的日志处理。\n\n'
        '### AI · 2026-10-10 10:00:10\n统一日志格式。\n\n'
        '### 用户 · 2026-10-10 10:00:20\n按前面的方案执行。\n\n'
        '### 用户 · 2026-10-10 11:00:00\n现在讨论乙项目。')
    with db.connect() as con:
        session = add_session(con, folder, content)
        def no_file_reads(*args, **kwargs):
            raise AssertionError('session cwd must not authorize a file read')
        monkeypatch.setattr(type(folder), 'open', no_file_reads)
        context = work_context(con, session, '按前面的方案执行。')
        assert len(context['events']) == 3
        assert '乙项目' not in json.dumps(context, ensure_ascii=False)
        assert work_context(con, session, '不存在的引用')['events'] == []


def test_stream_context_uses_frozen_batch_and_links_explicit_tool_path(tmp_path):
    db, folder, source_id = setup_source(tmp_path)
    moment = int(datetime(2026, 10, 10, 10, tzinfo=timezone.utc).timestamp() * 1e9)
    with db.connect() as con:
        ensure_ingest_schema(con)
        collect_document(con, source_id, folder / 'app.py', 'updated', moment)
        session = add_session(con, folder, f'工作目录：{folder}')
        session_source = con.execute('SELECT source_id FROM documents WHERE id=?', (session,)).fetchone()[0]
        con.execute("""INSERT INTO transcript_streams(document_id,source_id,adapter,cwd,generation)
            VALUES(?,?,'codex',?,1)""", (session, session_source, str(folder)))
        def event(name, text, *, generation=1, tool='', paths=()):
            return con.execute("""INSERT INTO transcript_events(document_id,generation,event_id,byte_offset,
                kind,role,timestamp,text,tool_name,artifact_paths_json)
                VALUES(?,?,?,0,?,'assistant','2026-10-10 10:00:00',?,?,?)""",
                (session, generation, name, 'tool_call' if tool else 'message', text,
                 tool, json.dumps(list(paths)))).lastrowid
        event('a', '现在处理甲项目。')
        event('write-a', '', tool='Write', paths=[str(folder / 'app.py')])
        end = event('report-a', '已经完成修改，等待验收。')
        event('b', '现在处理乙项目。')
        event('report-b', '已经完成修改，等待验收。')
        event('old', '已经完成修改，等待验收。', generation=0)
        context = work_context(con, session, '已经完成修改，等待验收。', event_end=end)
        assert [item['event_id'] for item in context['events']] == ['a', 'write-a', 'report-a']
        assert context['artifacts'][0]['relationship'] == 'tool_path_and_observed_file'
        assert context['artifacts'][0]['tool_event_ids'] == ['write-a']
        assert '乙项目' not in json.dumps(context, ensure_ascii=False)
        assert work_context(con, session, '现在处理乙项目。', event_end=end)['events'] == []


def test_fork_parent_context_has_own_citations_and_live_permission(tmp_path):
    db, folder, _ = setup_source(tmp_path)
    with db.connect() as con:
        ensure_ingest_schema(con)
        parent = add_session(con, folder, 'parent')
        child = add_session(con, folder / 'child' / 'project', 'child')
        for docid, native, parent_id in ((parent, 'parent-native', ''), (child, 'child-native', 'parent-native')):
            source = con.execute('SELECT source_id FROM documents WHERE id=?', (docid,)).fetchone()[0]
            con.execute("""INSERT INTO transcript_streams(document_id,source_id,adapter,session_id,parent_session_id)
                VALUES(?,?,'codex',?,?)""", (docid, source, native, parent_id))
        for docid, eventid, stamp, text in (
            (parent, 'before-fork', '2026-10-10 09:00:00', '甲项目使用 GitHub acme/project 仓库。'),
            (child, 'child-task', '2026-10-10 10:00:00', '继续刚才的日志功能。'),
            (parent, 'after-fork', '2026-10-10 11:00:00', '后面改为讨论乙项目。')):
            con.execute("""INSERT INTO transcript_events(document_id,generation,event_id,byte_offset,
                kind,role,timestamp,text) VALUES(?,1,?,0,'message','user',?,?)""",
                (docid, eventid, stamp, text))
        context = work_context(con, child, '继续刚才的日志功能。')
        assert [e['document_id'] for e in context['events']] == [child]
        inherited = context['parent_context']
        assert inherited['document_id'] == parent
        assert inherited['relation'] == 'fork_parent'
        assert [e['event_id'] for e in inherited['events']] == ['before-fork']
        assert '乙项目' not in json.dumps(context, ensure_ascii=False)
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=(SELECT source_id FROM documents WHERE id=?)',
                    (parent,))
        assert work_context(con, child)['parent_context'] == {}


def test_junit_report_is_specific_support_not_process_exit_or_user_acceptance(tmp_path):
    db, folder, source_id = setup_source(tmp_path)
    with db.connect() as con:
        _, report = collect_document(con, source_id, folder / 'junit.xml',
            '<testsuite><testcase name="ok"/><testcase name="not-run"><skipped/></testcase></testsuite>', 1)
        value = json.loads(con.execute('SELECT validation_json FROM artifact_observations WHERE id=?',
                                       (report,)).fetchone()[0])
        assert value == {'type': 'junit', 'tests': 2, 'failures': 0, 'errors': 0, 'skipped': 1, 'status': 'passed'}
        _, failed = collect_document(con, source_id, folder / 'failed.xml',
            '<testsuite><testcase><failure message="SECRET"/></testcase></testsuite>', 2)
        failed_value = con.execute('SELECT validation_json FROM artifact_observations WHERE id=?',
                                   (failed,)).fetchone()[0]
        assert 'SECRET' not in failed_value
        assert json.loads(failed_value)['status'] == 'failed'
        _, empty = collect_document(con, source_id, folder / 'empty.xml', '<testsuite tests="0"/>', 3)
        assert json.loads(con.execute('SELECT validation_json FROM artifact_observations WHERE id=?',
                                     (empty,)).fetchone()[0])['status'] == 'no_tests'
    model_context = context_for_model({'events': [{'tool_name': 'exec_command', 'exit_code': 0}],
                                      'artifacts': [{'validation': value}]})
    assert '工具退出0不证明成果正确' in model_context
    assert 'accepted必须来自用户实际验收原文' in model_context


def test_rename_and_deletion_keep_metadata_until_source_revocation(tmp_path):
    db, folder, source_id = setup_source(tmp_path)
    original = folder / 'old.md'
    original.write_text('same content')
    with db.connect() as con:
        original_id, _ = collect_document(con, source_id, original, 'same content', 1)
        renamed = folder / 'new.md'
        original.rename(renamed)
        renamed_id, change = collect_document(con, source_id, renamed, 'same content', 2)
        row = con.execute('SELECT * FROM artifact_observations WHERE id=?', (change,)).fetchone()
        assert row['change_kind'] == 'renamed'
        assert row['previous_path'] == str(original)
        assert record_file_deletion(con, original_id) == change
        con.execute('DELETE FROM documents WHERE id=?', (original_id,))
        assert con.execute('SELECT COUNT(*) FROM artifact_observations').fetchone()[0] == 2
        deletion = record_file_deletion(con, renamed_id)
        con.execute('DELETE FROM documents WHERE id=?', (renamed_id,))
        row = con.execute('SELECT * FROM artifact_observations WHERE id=?', (deletion,)).fetchone()
        assert row['change_kind'] == 'deleted' and row['document_id'] is None
        assert row['previous_sha'] == hashlib.sha256(b'same content').hexdigest()
        assert not row['sha256']
        con.execute('DELETE FROM sources WHERE id=?', (source_id,))
        assert con.execute('SELECT COUNT(*) FROM artifact_observations').fetchone()[0] == 0


def test_collector_keeps_revision_observations_in_same_file_record(tmp_path):
    db, folder, _ = setup_source(tmp_path)
    file = folder / 'guide.md'
    file.write_text('first line\n')
    collector = Collector(db)
    assert collector.scan_all()['new'] == 1
    file.write_text('first line\nsecond line\n')
    os.utime(file, ns=(file.stat().st_atime_ns, file.stat().st_mtime_ns + 1_000_000))
    assert collector.scan_all()['updated'] == 1
    with db.connect() as con:
        rows = con.execute('SELECT * FROM artifact_observations ORDER BY id').fetchall()
        assert len(rows) == 2
        assert rows[0]['change_kind'] == 'observed'
        assert rows[1]['change_kind'] == 'file_changed'
        assert rows[1]['added_lines'] == 1
        assert rows[1]['previous_sha'] == rows[0]['sha256']
