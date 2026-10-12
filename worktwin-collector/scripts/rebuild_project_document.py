"""Maintain a selected project's current document on a private SQLite snapshot.

The selection JSON names article IDs, original document IDs and project identity.
It is deliberately separate from saved source permissions and bulk extraction.
"""
import argparse
import hashlib
import json
from pathlib import Path

from worktwin.db import Database
from worktwin.credentials import DesktopSecrets
from worktwin.model_settings import ModelRuntime
from worktwin.inference import GatewayClient
from worktwin.article_maintenance import ArticleMaintainer
from worktwin.scope import snapshot_history
import sqlite3


def rebuild(db, selection, model, *, polish=False):
    key, name = selection['project_key'], selection['project_name']
    article_ids=set(selection['article_ids'])
    documents=set(selection['document_ids'])
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        old_keys={r['project_key'] for r in con.execute('SELECT * FROM knowledge') if r['id'] in article_ids}
        con.execute('''INSERT INTO project_entities(project_key,name,origin) VALUES(?,?,'manual')
            ON CONFLICT(project_key) DO UPDATE SET name=excluded.name''',(key,name))
        con.execute('DELETE FROM project_source_priorities WHERE project_key=?',(key,))
        for did in selection.get('current_document_ids',[]):
            if did not in documents:
                raise ValueError('Current baseline must be inside the selected source scope')
            con.execute('INSERT OR IGNORE INTO project_source_priorities VALUES(?,?)',(key,did))
        for did in selection.get('guide_document_ids',[]):
            if did not in documents:
                raise ValueError('Source guide must be inside the selected scope')
            con.execute('INSERT OR IGNORE INTO project_source_guides VALUES(?,?)',(key,did))
        for article_id in article_ids:
            article=con.execute('SELECT * FROM knowledge WHERE id=?',(article_id,)).fetchone()
            refs=con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',(article_id,)).fetchall()
            if not article or any(r[0] not in documents for r in refs):
                raise ValueError('Selected article has sources outside the rebuild scope')
            con.execute("UPDATE knowledge SET scope='project',project_key=?,project=? WHERE id=?",(key,name,article_id))
        # Existing material memberships follow the same explicit project repair.
        for material in con.execute('SELECT * FROM knowledge').fetchall():
            refs=con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',(material['id'],)).fetchall()
            if refs and all(r[0] in documents for r in refs) and material['project_key'] in old_keys:
                con.execute("UPDATE knowledge SET scope='project',project_key=?,project=? WHERE id=?",(key,name,material['id']))
        for work in con.execute('SELECT * FROM work_units').fetchall():
            if work['document_id'] in documents and work['project_key'] in old_keys:
                evidence=json.loads(work['evidence_json'])
                root=con.execute('SELECT s.root FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?',(work['document_id'],)).fetchone()[0]
                evidence.update(identity_kind='inferred',project_context={'workspace':root,'source_root':root},
                                project_objective=selection['project_objective'])
                con.execute("UPDATE work_units SET project_key=?,project_hint=?,status='confirmed',confidence=1,evidence_json=? WHERE id=?",
                    (key,name,json.dumps(evidence,ensure_ascii=False),work['id']))
        for did in documents:
            d=con.execute('SELECT * FROM documents WHERE id=? AND deleted=0',(did,)).fetchone()
            if not d or not d['content'].strip():
                raise ValueError('Selected source document is unavailable')
            fingerprint=hashlib.sha256(f"current-document:{did}:{d['sha256']}".encode()).hexdigest()
            quote=d['content'][:800]
            row=con.execute('''INSERT OR IGNORE INTO knowledge(kind,title,body,status,created_by,
                source_bound,fingerprint,quality,attribution,scope,project_key,project,topic,organization)
                VALUES('fact',?,?,'confirmed','enterprise_ai',1,?,'useful','document','project',?,?,?,'material')''',
                (d['title'],d['title'],fingerprint,key,name,name))
            mid=con.execute('SELECT id FROM knowledge WHERE fingerprint=?',(fingerprint,)).fetchone()[0]
            con.execute('INSERT OR IGNORE INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)',(mid,did,quote))
        con.execute('DELETE FROM article_maintenance_runs WHERE project_key=?',(key,))
    result=ArticleMaintainer(db,model).process_next(authorized_documents=documents,force=True,final_only=polish)
    if result.get('articles')!=1:
        raise RuntimeError('Current project document was not completed: '+json.dumps(result))
    with db.connect() as con:
        current=con.execute('''SELECT k.id,k.title,k.body,k.article_summary,k.status,k.version
            FROM project_documents p JOIN knowledge k ON k.id=p.knowledge_id
            WHERE p.scope='project' AND p.project_key=?''',(key,)).fetchone()
        refs=con.execute('SELECT count(*) FROM article_current_sources WHERE knowledge_id=?',(current['id'],)).fetchone()[0]
    return {'project_document_id':current['id'],'title':current['title'],
            'body_chars':len(current['body']),'overview_chars':len(current['article_summary']),
            'current_source_quotes':refs,'status':current['status'],
            'merged_articles':len(article_ids),'read_documents':len(documents)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot',type=Path)
    parser.add_argument('--selection',required=True,type=Path)
    parser.add_argument('--credentials-from',required=True,type=Path)
    parser.add_argument('--polish',action='store_true',help='Review an existing document against its selected current baselines')
    parser.add_argument('--baseline',type=Path,help='Original backup: collapse private editorial trials into one reviewed revision')
    args=parser.parse_args()
    if args.snapshot.resolve()==args.credentials_from.resolve():
        raise ValueError('Work only on an independent snapshot')
    db=Database(args.snapshot)
    secure=DesktopSecrets(args.credentials_from)
    gateway=GatewayClient(url=db.setting('enterprise_url'),token=secure.get('enterprise_token'))
    model=ModelRuntime(db,secure,enterprise=gateway)
    if not model.configured:
        raise RuntimeError('Saved model is not configured')
    result=rebuild(db,json.loads(args.selection.read_text()),model,polish=args.polish)
    if args.baseline:
        normalize_history(db,args.baseline,json.loads(args.selection.read_text())['article_ids'])
    args.snapshot.with_suffix('.project-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


def normalize_history(db, baseline, article_ids):
    """Private draft iterations are not product changes delivered to the user."""
    with sqlite3.connect('file:'+str(baseline.resolve())+'?mode=ro',uri=True) as original:
        original.row_factory=sqlite3.Row
        max_history=original.execute('SELECT COALESCE(max(id),0) FROM knowledge_history').fetchone()[0]
        with db.connect() as con:
            for kid in article_ids:
                before=original.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
                if before:
                    con.execute('DELETE FROM knowledge_history WHERE knowledge_id=? AND id>?',(kid,max_history))
                    snapshot_history(con,before)
                    con.execute('UPDATE knowledge SET version=? WHERE id=?',(before['version']+1,kid))


if __name__=='__main__':main()
