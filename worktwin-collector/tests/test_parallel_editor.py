import threading
from worktwin.db import Database
from worktwin.jobs import KnowledgeWorker
from worktwin.article_maintenance import ArticleMaintainer
from worktwin.library_organization import LibraryOrganizer
from tests.test_library_organization import fixture, Organizer


def test_grouping_completes_while_extraction_is_blocked(tmp_path):
    db=Database(tmp_path/'db');fixture(db)
    worker=KnowledgeWorker(db,client=Organizer(),interval=60)
    extracting=threading.Event();release=threading.Event();grouped=threading.Event()
    def extract():
        extracting.set();release.wait(5)
        return {'state':'idle'}
    original=worker.library_organizer.process_grouping
    def group():
        assert extracting.wait(5)
        result=original()
        if result['state']=='done':grouped.set()
        return result
    worker.process_next=extract
    worker.library_organizer.process_grouping=group
    worker.article_maintainer.process_next=lambda:{'state':'idle'}
    worker.project_rechecker.process_next=lambda:{'state':'idle'}
    try:
        worker.start();thread=worker._editor_thread;worker.start()
        assert worker._editor_thread is thread
        assert grouped.wait(5)
        assert not release.is_set()
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM knowledge WHERE group_key!=''").fetchone()[0]==3
    finally:
        release.set();worker.stop()
    assert not worker._thread.is_alive() and not worker._editor_thread.is_alive()


def test_pending_grouping_precedes_existing_merge(tmp_path):
    db=Database(tmp_path/'db');a,b,c=fixture(db);client=Organizer(merge=True)
    organizer=LibraryOrganizer(db,client,ArticleMaintainer(db,client))
    assert organizer.process_next()['groups']==2
    with db.connect() as con:
        con.execute("UPDATE knowledge SET body=body||'新增内容',version=version+1 WHERE id=?",(c,))
    assert organizer.process_next()['groups']==2
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM article_merge_queue WHERE state='queued'").fetchone()[0]==1
        assert con.execute('SELECT version FROM knowledge WHERE id=?',(a,)).fetchone()[0]==1


def test_concurrent_organizer_cannot_claim_same_project(tmp_path):
    db=Database(tmp_path/'db');fixture(db);results=[]
    second=LibraryOrganizer(db,Organizer(),ArticleMaintainer(db,Organizer()))
    client=Organizer(hook=lambda:results.append(second.process_grouping()))
    first=LibraryOrganizer(db,client,ArticleMaintainer(db,client))
    assert first.process_grouping()['state']=='done'
    assert results==[{'state':'idle'}]


def test_article_edit_requeues_grouping_then_uses_new_content(tmp_path):
    db=Database(tmp_path/'db');a,b,c=fixture(db)
    def edit():
        with db.connect() as con:con.execute("UPDATE knowledge SET body='博洋改动',version=version+1 WHERE id=?",(a,))
    client=Organizer(hook=edit);worker=LibraryOrganizer(db,client,ArticleMaintainer(db,client))
    assert worker.process_grouping()['state']=='superseded'
    client.organize_hook=None
    assert worker.process_grouping()['state']=='done'
    assert worker.process_grouping()['state']=='idle'
    with db.connect() as con:assert con.execute('SELECT group_name FROM knowledge WHERE id=?',(a,)).fetchone()[0]=='博洋'


def test_runtime_caps_concurrent_requests_at_two(tmp_path):
    from worktwin.model_settings import ModelRuntime
    entered=threading.Event();release=threading.Event();lock=threading.Lock()
    class Model:
        configured=True
        active=0
        peak=0
        calls=0
        def chat(self,*args,**kwargs):
            with lock:
                self.active+=1;self.calls+=1;self.peak=max(self.peak,self.active)
                if self.active==2:entered.set()
            release.wait(5)
            with lock:self.active-=1
            return 'OK'
    model=Model();runtime=ModelRuntime(Database(tmp_path/'db'),None,injected=model)
    threads=[threading.Thread(target=lambda:runtime.chat([])) for _ in range(3)]
    for t in threads:t.start()
    try:
        assert entered.wait(5)
        with lock:assert model.calls==2
    finally:
        release.set()
        for t in threads:t.join(5)
    assert model.calls==3 and model.peak==2


def test_long_article_lease_is_renewed_only_by_owner(tmp_path):
    from worktwin.job_lease import renew_lease
    db=Database(tmp_path/'db');observed=threading.Event()
    with db.connect() as con:
        con.execute("INSERT INTO article_maintenance_runs(scope,project_key,input_signature,state,claim_token,updated_at) VALUES('project','p','s','running','owner','2000-01-01')")
    with renew_lease(db,"UPDATE article_maintenance_runs SET updated_at=datetime('now') WHERE claim_token=?",('owner',),interval=.01):
        for _ in range(100):
            with db.connect() as con:
                if con.execute('SELECT updated_at FROM article_maintenance_runs').fetchone()[0]!='2000-01-01':
                    observed.set();break
            observed.wait(.01)
    assert observed.is_set()
