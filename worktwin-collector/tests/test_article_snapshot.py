"""Offline snapshot import preserves live data and incremental completion."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from worktwin.article_maintenance import ArticleMaintainer
from worktwin.db import Database
from worktwin.browser_capture import BrowserCapture
from worktwin.browser_site_manual import BrowserSiteManuals
from tests.test_article_maintenance import material, article, Writer


def backup(source,target):
    with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
        old.backup(new)


def snapshot(tmp_path):
    baseline=Database(tmp_path/'baseline.sqlite')
    BrowserCapture(baseline)
    BrowserSiteManuals(baseline)
    with baseline.connect() as con:
        did,kid=material(con,'任务定义','任务是一项需要明确结果的工作事项。')
        article(con,kid,'旧文章只列出任务的一条定义。')
        con.execute('UPDATE sources SET allow_ai=0')
    staged=tmp_path/'snapshot.sqlite';live=tmp_path/'live.sqlite'
    backup(baseline.path,staged);backup(baseline.path,live)
    db=Database(staged)
    with db.connect() as con:
        fields=('kind','title','body','scope','project','project_key','topic','scope_detail','quality','attribution','outcome')
        old=con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
        mid=con.execute('INSERT INTO knowledge('+','.join(fields)+",organization,created_by,source_bound,fingerprint) VALUES("+
            ','.join('?' for _ in fields)+",'material','enterprise_ai',1,'snapshot-material-fixture')",
            tuple(old[f] for f in fields)).lastrowid
        con.execute('''INSERT INTO knowledge_evidence(knowledge_id,document_id,quote,is_current)
            SELECT ?,document_id,quote,is_current FROM knowledge_evidence WHERE knowledge_id=?''',(mid,kid))
    writer=Writer(groups=[{'topic':'任务办理机制','material_ids':[mid],'article_ids':[kid]}])
    assert ArticleMaintainer(db,writer).process_next(authorized_documents={did})['articles']==1
    return baseline.path,staged,live,did,kid,mid


def apply(staged,baseline,live,*options):
    root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/apply_article_snapshot.py'),str(staged),
        '--baseline',str(baseline),'--live',str(live),*options],cwd=root,
        env={**os.environ,'PYTHONPATH':str(root)},text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_snapshot_remaps_receipts_and_does_not_repeat_finished_article_work(tmp_path):
    baseline,staged,live,did,kid,mid=snapshot(tmp_path)
    db=Database(live)
    with db.connect() as con:
        concurrent=con.execute("INSERT INTO knowledge(kind,title,body,status) VALUES('fact','并发保留文章','保留用户在重建期间新增的内容','confirmed')").lastrowid
        assert concurrent==mid
    result=apply(staged,baseline,live)
    assert result['copied_material_receipts']==1 and result['skipped_material_receipts']==0
    with db.connect() as con:
        live_material=con.execute("SELECT id FROM knowledge WHERE fingerprint='snapshot-material-fixture'").fetchone()[0]
        assert live_material!=mid
        assert con.execute('SELECT body FROM knowledge WHERE id=?',(concurrent,)).fetchone()[0]=='保留用户在重建期间新增的内容'
        assert con.execute('SELECT allow_ai FROM sources').fetchone()[0]==0
        assert con.execute('SELECT material_id FROM article_material_receipts').fetchone()[0]==live_material
        con.execute('UPDATE sources SET allow_ai=1')
    writer=Writer()
    assert ArticleMaintainer(db,writer).process_next()['state']=='idle'
    assert writer.requests==[]


def test_receipts_only_does_not_reapply_articles_or_change_source_permissions(tmp_path):
    baseline,staged,live,did,kid,mid=snapshot(tmp_path)
    apply(staged,baseline,live)
    db=Database(live)
    with db.connect() as con:
        con.execute('DELETE FROM article_material_receipts')
        before={table:[tuple(r) for r in con.execute('SELECT * FROM '+table+' ORDER BY id')]
                for table in ('knowledge','documents','sources','knowledge_history')}
    result=apply(staged,baseline,live,'--receipts-only')
    assert result=={'mode':'receipts_only','copied_material_receipts':1,'skipped_material_receipts':0}
    with db.connect() as con:
        after={table:[tuple(r) for r in con.execute('SELECT * FROM '+table+' ORDER BY id')]
               for table in before}
        assert after==before


def test_snapshot_skips_changed_context_even_when_quote_is_still_present(tmp_path):
    baseline,staged,live,did,kid,mid=snapshot(tmp_path)
    db=Database(live)
    with db.connect() as con:
        prior=dict(con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone())
        content=con.execute('SELECT content FROM documents WHERE id=?',(did,)).fetchone()[0]
        current='新背景：由另一部门负责本任务。\n'+content
        con.execute('UPDATE documents SET content=?,sha256=? WHERE id=?',
                    (current,hashlib.sha256(current.encode()).hexdigest(),did))
    result=apply(staged,baseline,live)
    assert result['skipped_reasons']=={str(kid):'source_content_changed',str(mid):'source_content_changed'}
    assert result['copied_material_receipts']==0
    with db.connect() as con:
        assert dict(con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone())==prior
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==0
