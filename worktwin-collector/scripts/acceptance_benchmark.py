"""Synthetic 80-file collector benchmark and consent / AI-job sanity test.

Never scans real user folders, and never connects to an outside model.
"""
import json
import tempfile
import time
from pathlib import Path

from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.jobs import KnowledgeWorker
from worktwin.search import search


class DeterministicModel:
    configured = True
    def __init__(self):
        self.requests = 0

    def chat(self, messages, max_tokens=2400):
        self.requests += 1
        raw = messages[-1]['content'].split('<source>\n', 1)[-1].split('\n</source>', 1)[0]
        anchor = '我们最终决定将审批能力封装为一个节点。'
        if anchor not in raw:
            return json.dumps({'items': []}, ensure_ascii=False)
        return json.dumps({'items': [
            {'kind': 'decision', 'title': '审批以工作流节点实现',
             'body': '审批复用工作流上下文和路由机制。', 'quote': anchor,
             'topic':'审批架构','scope_detail':'本项目的审批能力',
             'value_reason':'查阅本项目审批的架构决策','attribution':'document','outcome':'none'}
        ]}, ensure_ascii=False)


def run():
    with tempfile.TemporaryDirectory(prefix='worktwin-v04-benchmark-') as tmp:
        base = Path(tmp)
        sessions = base / 'codex' / 'sessions' / '2026' / '10' / '08'
        docs = base / 'work' / 'approval-node'
        sessions.mkdir(parents=True)
        docs.mkdir(parents=True)
        for i in range(30):
            entries = [
                {'type': 'session_meta', 'payload': {'id': f'rollout-{i}', 'cwd': '/company/approval-node'}},
                {'timestamp': f'2026-10-08T08:{i:02d}:00Z', 'type': 'response_item',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': '我们最终决定将审批能力封装为一个节点。'}]}},
                {'timestamp': f'2026-10-08T08:{i:02d}:10Z', 'type': 'response_item',
                 'payload': {'type': 'message', 'role': 'assistant',
                             'content': [{'type': 'output_text', 'text': '已收到，正在整理方案。'}]}},
            ]
            (sessions / f'rollout-{i}.jsonl').write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in entries), encoding='utf-8')
        for i in range(50):
            (docs / f'spec-{i:03d}.md').write_text(
                f'# 审批方案 {i}\n\n我们最终决定将审批能力封装为一个节点。\n'
                '原因是复用工作流上下文与权限，并避免重复的审批系统维护。', encoding='utf-8')
        db = Database(base / 'app' / 'worktwin.sqlite')
        with db.connect() as con:
            codex_source = con.execute('INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES(?,?,?,?,0)',
                                        ('Codex', 'codex', 'codex', str(sessions.parents[2]))).lastrowid
            docs_source = con.execute('INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES(?,?,?,?,0)',
                                       ('工作方案', 'folder', 'folder', str(docs.parent))).lastrowid
        scanner = Collector(db)
        start = time.perf_counter()
        first = scanner.scan_all()
        initial = time.perf_counter() - start
        start = time.perf_counter()
        second = scanner.scan_all()
        incremental = time.perf_counter() - start
        with db.connect() as con:
            indexed = con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
            knowledge_before = con.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]
            jobs_before = con.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0]
            hits = search(con, '为什么把审批作为工作流节点？')['documents']
            # Artificially authorize just the 50 business documents for AI processing.
            con.execute('UPDATE sources SET allow_ai=1 WHERE id=?', (docs_source,))
            con.execute("""INSERT INTO ai_jobs(document_id,content_sha,state) SELECT id,sha256,'queued'
                           FROM documents WHERE source_id=?""", (docs_source,))
        assert first['new'] == 80, first
        assert second['new'] == 0 and second['updated'] == 0, second
        assert indexed == 80 and knowledge_before == 0 and jobs_before == 0 and hits
        model = DeterministicModel()
        processed = KnowledgeWorker(db, client=model).process_next()
        assert processed['state'] == 'done' and processed['added'] == 1, processed
        with db.connect() as con:
            evidence = con.execute('SELECT COUNT(*) FROM knowledge_evidence WHERE is_current=1').fetchone()[0]
            remaining = con.execute("SELECT COUNT(*) FROM ai_jobs WHERE state='queued'").fetchone()[0]
            restricted = con.execute("SELECT COUNT(*) FROM ai_jobs j JOIN documents d ON d.id=j.document_id WHERE d.source_id=?", (codex_source,)).fetchone()[0]
        assert evidence == 1 and remaining == 49 and restricted == 0
        print(json.dumps({'result': 'PASS', 'documents': indexed, 'initial_scan_seconds': round(initial, 3),
                          'incremental_seconds': round(incremental, 3), 'no_consent_jobs': jobs_before,
                          'model_requests': model.requests, 'knowledge_evidence': evidence,
                          'pending_authorized_jobs': remaining, 'search_hits': len(hits)}, ensure_ascii=False))


if __name__ == '__main__':
    run()
