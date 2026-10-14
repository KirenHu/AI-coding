"""A growing coding session advances capture and extraction independently."""

import json
import re

from fastapi.testclient import TestClient

from tests.support import FakeModel
from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.jobs import KnowledgeWorker
from worktwin.api import create_app


FIRST = '最终决定采用统一审批节点，以便多个业务流程共享同一套审批规则。'
SECOND = '最终决定将每周工作报告保留为独立文件，方便查阅工作过程和结论。'


def user_message(text, second=1):
    return {'type': 'event_msg', 'timestamp': f'2026-10-10T10:00:{second:02d}Z',
            'payload': {'type': 'user_message', 'message': text}}


def write_records(path, records, *, append=False):
    with path.open('a' if append else 'w', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def setup_session(tmp_path, messages=None):
    root = tmp_path / 'sessions'
    root.mkdir()
    path = root / 'session.jsonl'
    write_records(path, [
        {'type': 'session_meta', 'payload': {'id': 'session-1', 'cwd': str(tmp_path / 'work')}},
        *(messages if messages is not None else [user_message(FIRST)])])
    db = Database(tmp_path / 'work.db')
    with db.connect() as con:
        con.execute('INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES(?,?,?,?,1)',
                    ('Codex', 'codex', 'codex', str(root)))
    collector = Collector(db)
    assert collector.scan_all()['new'] == 1
    return db, collector, path


def watermarks(db):
    with db.connect() as con:
        return dict(con.execute('SELECT * FROM transcript_streams').fetchone())


def extraction_sources(model):
    return [messages[-1]['content'].split('<source>\n', 1)[1].split('\n</source>', 1)[0]
            for messages in model.requests if '<source>\n' in messages[-1]['content']]


def test_append_during_extraction_finishes_claim_then_processes_only_new_evidence(tmp_path):
    db, collector, path = setup_session(tmp_path)

    class AppendingModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            if not self.requests:
                write_records(path, [user_message(SECOND, 2)], append=True)
                assert collector.scan_all()['updated'] == 1
            return super().chat(messages, max_tokens)

    model = AppendingModel()
    worker = KnowledgeWorker(db, client=model)
    first = worker.process_next()
    assert first['state'] == 'done' and first['candidates'] == 1
    partial = watermarks(db)
    assert partial['processed_seq'] < partial['captured_seq']
    with db.connect() as con:
        assert con.execute('SELECT state FROM ai_jobs').fetchone()[0] == 'queued'
    second = worker.process_next()
    assert second['state'] == 'done' and second['candidates'] == 1
    final = watermarks(db)
    assert final['processed_seq'] == final['captured_seq']
    sources = extraction_sources(model)
    assert len(sources) == 2 and SECOND not in sources[0] and SECOND in sources[1]
    # The previous decision remains useful context but is not extracted again.
    assert FIRST in sources[1]
    collector.scan_all()
    assert worker.process_next()['state'] == 'idle'
    assert len(extraction_sources(model)) == 2
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 2


def test_model_retry_keeps_same_batch_when_more_events_are_captured(tmp_path):
    db, collector, path = setup_session(tmp_path)

    class FailOnceModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            if not self.requests:
                self.requests.append(messages)
                raise TimeoutError('temporary model timeout')
            return super().chat(messages, max_tokens)

    model = FailOnceModel()
    worker = KnowledgeWorker(db, client=model)
    assert worker.process_next()['state'] == 'retrying'
    assert watermarks(db)['processed_seq'] == 0
    with db.connect() as con:
        frozen = dict(con.execute('SELECT * FROM ai_jobs').fetchone())
    write_records(path, [user_message(SECOND, 2)], append=True)
    collector.scan_all()
    with db.connect() as con:
        job = dict(con.execute('SELECT * FROM ai_jobs').fetchone())
        assert job['attempts'] == frozen['attempts'] == 1
        assert job['batch_through_seq'] == frozen['batch_through_seq']
        assert job['next_run_at'] == frozen['next_run_at']
        con.execute("UPDATE ai_jobs SET next_run_at=datetime('now','-1 second')")
    assert worker.process_next()['state'] == 'done'
    sources = extraction_sources(model)
    assert sources[0] == sources[1] and SECOND not in sources[1]
    assert watermarks(db)['processed_seq'] < watermarks(db)['captured_seq']
    assert worker.process_next()['state'] == 'done'
    assert watermarks(db)['processed_seq'] == watermarks(db)['captured_seq']


def test_rewrite_supersedes_inflight_batch_without_acknowledging_new_generation(tmp_path):
    db, collector, path = setup_session(tmp_path)
    initial_generation = watermarks(db)['generation']

    class RewritingModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            if not self.requests:
                write_records(path, [
                    {'type': 'session_meta', 'payload': {'id': 'session-1', 'cwd': '/work/new'}},
                    user_message(SECOND, 2)])
                assert collector.scan_all()['updated'] == 1
            return super().chat(messages, max_tokens)

    worker = KnowledgeWorker(db, client=RewritingModel())
    assert worker.process_next()['state'] == 'superseded'
    assert watermarks(db)['generation'] > initial_generation
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 0
    assert worker.process_next()['state'] == 'done'
    final = watermarks(db)
    assert final['processed_seq'] == final['captured_seq']
    with db.connect() as con:
        quotes = [row[0] for row in con.execute('SELECT quote FROM knowledge_evidence')]
    assert SECOND in quotes and FIRST not in quotes


def test_large_pending_session_advances_in_bounded_batches(tmp_path):
    messages = [user_message(f'这是第{i}次进度同步，仍在进行资料整理。') for i in range(81)]
    db, _, _ = setup_session(tmp_path, messages + [user_message(SECOND, 2)])
    model = FakeModel()
    worker = KnowledgeWorker(db, client=model)
    assert worker.process_next()['state'] == 'done'
    partial = watermarks(db)
    assert partial['processed_seq'] < partial['captured_seq']
    assert worker.process_next()['state'] == 'done'
    final = watermarks(db)
    assert final['processed_seq'] == final['captured_seq']
    assert worker.process_next()['state'] == 'idle'
    with db.connect() as con:
        assert con.execute('SELECT quote FROM knowledge_evidence').fetchone()[0] == SECOND


def test_revoked_source_cannot_commit_inflight_transcript_result(tmp_path):
    db, _, _ = setup_session(tmp_path)

    class RevokingModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            with db.connect() as con:
                con.execute('UPDATE sources SET allow_ai=0')
            return super().chat(messages, max_tokens)

    assert KnowledgeWorker(db, client=RevokingModel()).process_next()['state'] == 'superseded'
    assert watermarks(db)['processed_seq'] == 0
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 0


def test_write_failure_rolls_back_processed_cursor_with_knowledge(tmp_path, monkeypatch):
    import worktwin.jobs as jobs

    db, _, _ = setup_session(tmp_path)
    original = jobs.store_candidates
    calls = 0

    def interrupted_store(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            raise RuntimeError('interrupted before transaction commit')
        return result

    monkeypatch.setattr(jobs, 'store_candidates', interrupted_store)
    worker = KnowledgeWorker(db, client=FakeModel())
    assert worker.process_next()['state'] == 'retrying'
    assert watermarks(db)['processed_seq'] == 0
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 0
        con.execute("UPDATE ai_jobs SET next_run_at=datetime('now','-1 second')")
    assert worker.process_next()['state'] == 'done'
    assert watermarks(db)['processed_seq'] == watermarks(db)['captured_seq']
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 1


def test_cursor_cli_preserves_user_attribution_and_rejects_unconfirmed_ai_decision(tmp_path):
    root = tmp_path / 'cursor-exports'
    root.mkdir()
    suggestion = '建议采用不经过审批直接付款的流程，以减少审核等待时间。'
    write_records(root / 'session.jsonl', [
        {'type': 'system', 'subtype': 'init', 'session_id': 'cursor-1', 'cwd': '/work/payments'},
        {'type': 'user', 'message': {'content': FIRST}},
        {'type': 'assistant', 'message': {'content': suggestion}}])
    db = Database(tmp_path / 'work.db')
    with db.connect() as con:
        con.execute('INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES(?,?,?,?,1)',
                    ('Cursor CLI', 'folder', 'cursor', str(root)))
    assert Collector(db).scan_all()['new'] == 1

    class AttributionModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            if '<source>\n' not in messages[-1]['content']:
                return super().chat(messages, max_tokens)
            self.requests.append(messages)
            return json.dumps({'items': [
                {'kind': 'decision', 'title': quote, 'body': quote, 'quote': quote,
                 'topic': '支付审批方案', 'scope_detail': '本次支付审批项目',
                 'value_reason': '供后续支付流程配置时查阅', 'attribution': role, 'outcome': 'none'}
                for quote, role in [(FIRST, 'user'), (suggestion, 'assistant')]]}, ensure_ascii=False)

    result = KnowledgeWorker(db, client=AttributionModel()).process_next()
    assert result['state'] == 'done' and result['candidates'] == 1
    with db.connect() as con:
        knowledge = con.execute('SELECT attribution,body FROM knowledge').fetchall()
    assert len(knowledge) == 1 and knowledge[0]['attribution'] == 'user'
    assert suggestion not in knowledge[0]['body']


def test_new_confirmation_can_reference_proposal_from_previous_processed_batch(tmp_path):
    proposal = '建议将审批规则统一放在共享节点中，由各个业务流程引用。'
    confirmation = '同意，采用这个方案。'
    db, collector, path = setup_session(tmp_path, [
        {'type': 'event_msg', 'timestamp': '2026-10-10T10:00:00Z',
         'payload': {'type': 'agent_message', 'message': proposal}}])

    class ConfirmationModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            source = messages[-1]['content']
            if '<source>\n' not in source:
                return super().chat(messages, max_tokens)
            self.requests.append(messages)
            if confirmation not in source:
                return '{"items":[]}'
            return json.dumps({'items': [{
                'kind': 'decision', 'title': '复用共享审批规则', 'body': proposal,
                'quote': proposal, 'confirmation_quote': confirmation,
                'topic': '共享审批规则', 'scope_detail': '本次流程配置工作',
                'value_reason': '用于后续配置共享审批规则', 'attribution': 'user', 'outcome': 'none'}]},
                ensure_ascii=False)

    model = ConfirmationModel()
    worker = KnowledgeWorker(db, client=model)
    assert worker.process_next()['candidates'] == 0
    write_records(path, [user_message(confirmation, 2)], append=True)
    collector.scan_all()
    result = worker.process_next()
    assert result['state'] == 'done' and result['candidates'] == 1
    with db.connect() as con:
        quotes = {row[0] for row in con.execute('SELECT quote FROM knowledge_evidence')}
    assert quotes == {proposal, confirmation}


def test_explicit_reprocess_rereads_processed_events_without_recapturing(tmp_path):
    db, _, _ = setup_session(tmp_path)
    model = FakeModel()
    app = create_app(db.path, start_worker=False, inference_client=model)
    with TestClient(app) as client:
        token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
        assert app.state.knowledge_worker.process_next()['candidates'] == 1
        before = watermarks(db)
        response = client.post('/api/knowledge/reprocess', headers={'X-Worktwin-Token': token})
        assert response.status_code == 200 and response.json()['queued'] == 1
        assert watermarks(db)['processed_seq'] == 0
        result = app.state.knowledge_worker.process_next()
        assert result['state'] == 'done' and result['candidates'] == 1
    assert len(extraction_sources(model)) == 2
    after = watermarks(db)
    assert after['processed_seq'] == before['captured_seq'] == after['captured_seq']
    assert after['byte_offset'] == before['byte_offset']
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 1


def test_explicit_document_scope_reprocesses_completed_transcript(tmp_path):
    db, _, _ = setup_session(tmp_path)
    model = FakeModel()
    app = create_app(db.path, start_worker=False, inference_client=model)
    with TestClient(app) as client:
        token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
        assert app.state.knowledge_worker.process_next()['candidates'] == 1
        before = watermarks(db)
        response = client.put(f"/api/documents/{before['document_id']}/scope",
                              headers={'X-Worktwin-Token': token}, json={'project': '共享审批项目'})
        assert response.status_code == 200 and response.json()['queued']
        project_key = response.json()['project_key']
        assert watermarks(db)['processed_seq'] == 0
        result = app.state.knowledge_worker.process_next()
        assert result['state'] == 'done' and result['candidates'] == 1
    with db.connect() as con:
        assigned = con.execute('SELECT project,scope FROM knowledge WHERE project_key=?', (project_key,)).fetchall()
    assert len(assigned) == 1 and assigned[0]['project'] == '共享审批项目' and assigned[0]['scope'] == 'project'
    assert watermarks(db)['processed_seq'] == before['captured_seq']
