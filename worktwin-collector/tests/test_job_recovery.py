from worktwin.db import Database
from worktwin.api import create_app
from worktwin.jobs import KnowledgeWorker
from tests.support import FakeModel


def test_opening_database_does_not_requeue_live_job_and_expired_lease_recovers(tmp_path):
    root=tmp_path/'work';root.mkdir();(root/'note.md').write_text('我们最终决定保留知识原文证据和完整版本记录。')
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with app.state.db.connect() as con:
        con.execute('INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,?,?,1)',('work','folder',str(root)))
    app.state.collector.scan_all()
    with app.state.db.connect() as con:con.execute("UPDATE ai_jobs SET state='running',claim_token='other-live-worker'")
    Database(app.state.db.path)
    worker=KnowledgeWorker(app.state.db,client=FakeModel())
    assert worker.process_next()['state']=='idle'
    with app.state.db.connect() as con:con.execute("UPDATE ai_jobs SET updated_at=datetime('now','-21 minutes')")
    assert worker.process_next()['state']=='done'
    with app.state.db.connect() as con:assert con.execute('SELECT state,attempts FROM ai_jobs').fetchone()[:]==('done',1)
