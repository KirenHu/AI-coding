"""Apply reviewed article changes without replacing live collection data.

Stop the installed app first. The baseline is its pre-review SQLite backup.
New records are assigned live IDs; original article IDs and source settings stay.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from worktwin.db import Database
from worktwin.browser_capture import BrowserCapture
from worktwin.browser_site_manual import BrowserSiteManuals
from worktwin.article_maintenance import source_materials, _signature


def read(path):
    con=sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    return con


def insert(con, table, row, *, replace=False, drop_id=False):
    values=dict(row)
    if drop_id:values.pop('id',None)
    fields=list(values)
    action='INSERT OR REPLACE' if replace else 'INSERT'
    return con.execute(action+' INTO '+table+'('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',
                       [values[k] for k in fields])


def existing_mapping(stage, baseline, con):
    """Resolve remapped new records by stable fingerprint or exact article."""
    mapping={r[0]:r[0] for r in baseline.execute('SELECT id FROM knowledge')}
    for row in stage.execute('SELECT * FROM knowledge'):
        if row['id'] in mapping:
            continue
        if row['fingerprint']:
            matches=con.execute('SELECT id FROM knowledge WHERE fingerprint=?',(row['fingerprint'],)).fetchall()
        elif row['organization']=='article':
            matches=con.execute('''SELECT id FROM knowledge WHERE organization='article'
                AND title=? AND body=? AND article_summary=? AND version=? AND project_key=? AND scope=?''',
                tuple(row[k] for k in ('title','body','article_summary','version','project_key','scope'))).fetchall()
        else:
            matches=[]
        if len(matches)==1:
            mapping[row['id']]=matches[0][0]
    return mapping


def copy_receipts(stage, con, mapping, *, accepted_articles=None):
    """Acknowledge only unchanged materials whose written article is present."""
    authorized={r[0] for r in stage.execute('''SELECT DISTINCT e.document_id
        FROM knowledge_evidence e JOIN article_material_receipts r ON r.material_id=e.knowledge_id
        WHERE e.is_current=1 AND e.superseded=0''')}
    staged={m['id']:m for m in source_materials(stage,authorized_documents=authorized)}
    current={m['id']:m for m in source_materials(con,authorized_documents=authorized)}
    copied=skipped=0
    for receipt in stage.execute('SELECT * FROM article_material_receipts'):
        material=staged.get(receipt['material_id'])
        live=current.get(mapping.get(receipt['material_id']))
        if not material or not live or receipt['input_signature']!=_signature([material]):
            skipped+=1;continue
        fields=('version','title','body','scope','project_key','quality','status')
        sources=lambda m:sorted((e['document_id'],e['sha256'],e['quote']) for e in m['sources'])
        if any(material[k]!=live[k] for k in fields) or sources(material)!=sources(live):
            skipped+=1;continue
        written=False
        for article in stage.execute("""SELECT k.* FROM knowledge k JOIN article_materials a ON a.article_id=k.id
            WHERE a.material_id=? AND k.organization='article' AND k.status!='archived'""",(material['id'],)):
            if accepted_articles is not None and article['id'] not in accepted_articles:
                continue
            target=con.execute('SELECT * FROM knowledge WHERE id=?',(mapping.get(article['id']),)).fetchone()
            if target and all(article[k]==target[k] for k in
                ('version','title','body','article_summary','scope','project_key','status')) and con.execute(
                    'SELECT 1 FROM article_materials WHERE article_id=? AND material_id=?',(target['id'],live['id'])).fetchone():
                written=True;break
        if not written:
            skipped+=1;continue
        con.execute('''INSERT INTO article_material_receipts(material_id,input_signature) VALUES(?,?)
            ON CONFLICT(material_id) DO UPDATE SET input_signature=excluded.input_signature''',(live['id'],_signature([live])))
        copied+=1
    return {'copied_material_receipts':copied,'skipped_material_receipts':skipped}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot',type=Path)
    parser.add_argument('--baseline',required=True,type=Path)
    parser.add_argument('--live',required=True,type=Path)
    parser.add_argument('--receipts-only',action='store_true',help='Only restore completion receipts after a prior snapshot apply; no migrations or model calls')
    args=parser.parse_args()
    if len({args.snapshot.resolve(),args.baseline.resolve(),args.live.resolve()})!=3:
        raise ValueError('snapshot, baseline and live must be different files')
    stage, baseline=read(args.snapshot),read(args.baseline)
    if args.receipts_only:
        with sqlite3.connect(args.live) as con:
            con.row_factory=sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE')
            result=copy_receipts(stage,con,existing_mapping(stage,baseline,con))
        print(json.dumps({'mode':'receipts_only',**result}))
        return
    original_ids={r[0] for r in baseline.execute("SELECT id FROM knowledge WHERE organization='article'")}
    maximum=baseline.execute('SELECT COALESCE(max(id),0) FROM knowledge').fetchone()[0]
    history_max=baseline.execute('SELECT COALESCE(max(id),0) FROM knowledge_history').fetchone()[0]
    candidates=[dict(r) for r in stage.execute('SELECT * FROM knowledge') if r['id'] in original_ids or r['id']>maximum]
    db=Database(args.live)
    BrowserCapture(db)
    BrowserSiteManuals(db)
    skipped=[]
    skipped_reasons={}
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        permissions=[tuple(r) for r in con.execute('SELECT id,enabled,allow_ai,allow_share FROM sources ORDER BY id')]
        mapping={r[0]:r[0] for r in baseline.execute('SELECT id FROM knowledge')}
        for item in candidates:
            kid=item['id']
            prior=baseline.execute('SELECT version FROM knowledge WHERE id=?',(kid,)).fetchone()
            current=con.execute('SELECT version FROM knowledge WHERE id=?',(kid,)).fetchone()
            if prior and (not current or current['version']!=prior['version']):
                skipped.append(kid);skipped_reasons[kid]='article_version_changed';continue
            references=stage.execute('''SELECT e.*,d.sha256 source_sha FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0''',(kid,)).fetchall()
            reason=None
            for reference in references:
                source=con.execute('SELECT content,deleted,sha256 FROM documents WHERE id=?',(reference['document_id'],)).fetchone()
                if not source or source['deleted'] or reference['quote'] not in source['content']:
                    reason='source_evidence_missing';break
                if source['sha256']!=reference['source_sha']:
                    reason='source_content_changed';break
            if reason:
                skipped.append(kid);skipped_reasons[kid]=reason;continue
            if prior:
                fields=[k for k in item if k!='id']
                con.execute('UPDATE knowledge SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',
                            [item[k] for k in fields]+[kid])
            else:
                mapping[kid]=insert(con,'knowledge',item,drop_id=True).lastrowid
        valid={r['id'] for r in candidates}-set(skipped)
        for old_id in valid:
            con.execute("UPDATE knowledge_proposals SET status='dismissed',resolved_at=datetime('now') WHERE target_id=? AND status='pending' AND (target_version<(SELECT version FROM knowledge WHERE id=?) OR (SELECT status FROM knowledge WHERE id=?)='archived')",(mapping[old_id],mapping[old_id],mapping[old_id]))
        for project in stage.execute('SELECT * FROM project_entities'):
            if not con.execute('SELECT 1 FROM project_entities WHERE project_key=?',(project['project_key'],)).fetchone():
                insert(con,'project_entities',project)
        for table in ('knowledge_evidence','article_parts'):
            for row in stage.execute('SELECT * FROM '+table):
                if row['knowledge_id'] not in valid:continue
                data=dict(row);data['knowledge_id']=mapping[data['knowledge_id']]
                if table=='knowledge_evidence':
                    # Live collection may have rebuilt its search chunks since
                    # the snapshot. The original quote is the stable reference.
                    chunk=con.execute('SELECT id FROM chunks WHERE document_id=? AND instr(text,?)>0 ORDER BY ordinal LIMIT 1',
                                      (data['document_id'],data['quote'][:60])).fetchone()
                    data['chunk_id']=chunk[0] if chunk else None
                    con.execute('DELETE FROM knowledge_evidence WHERE knowledge_id=? AND document_id=? AND quote=?',
                                (data['knowledge_id'],data['document_id'],data['quote']))
                else:
                    con.execute('DELETE FROM article_parts WHERE knowledge_id=? AND document_id=? AND fingerprint=?',
                                (data['knowledge_id'],data['document_id'],data['fingerprint']))
                insert(con,table,data,drop_id=True)
        for row in stage.execute('SELECT * FROM knowledge_history WHERE id>?',(history_max,)):
            if row['knowledge_id'] in valid:
                data=dict(row);data['knowledge_id']=mapping[data['knowledge_id']]
                insert(con,'knowledge_history',data,drop_id=True)
        for row in stage.execute('SELECT * FROM article_materials'):
            if row['article_id'] in valid and row['material_id'] not in skipped:
                con.execute('INSERT OR IGNORE INTO article_materials VALUES(?,?)',
                    (mapping[row['article_id']],mapping.get(row['material_id'],row['material_id'])))
        for row in stage.execute('SELECT * FROM project_memberships'):
            if row['knowledge_id'] in valid:
                data=dict(row);data['knowledge_id']=mapping[data['knowledge_id']]
                insert(con,'project_memberships',data,replace=True)
        documents={r[0] for r in stage.execute('SELECT DISTINCT document_id FROM knowledge_evidence')
                   if stage.execute('SELECT 1 FROM knowledge_evidence WHERE document_id=? AND knowledge_id IN ('+
                      ','.join('?' for _ in valid)+')',(r[0],*valid)).fetchone()}
        for row in stage.execute('SELECT * FROM work_units'):
            if row['document_id'] not in documents:continue
            evidence=json.loads(row['evidence_json']);quote=evidence.get('quote','')
            current=con.execute('SELECT content FROM documents WHERE id=? AND deleted=0',(row['document_id'],)).fetchone()
            if not current or quote not in current['content']:continue
            con.execute('DELETE FROM work_units WHERE document_id=? AND quote_hash=?',(row['document_id'],row['quote_hash']))
            insert(con,'work_units',row,drop_id=True)
        for row in stage.execute('SELECT * FROM twin_knowledge'):
            if row['knowledge_id'] in valid and con.execute('SELECT 1 FROM twins WHERE id=?',(row['twin_id'],)).fetchone():
                con.execute('INSERT OR IGNORE INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',
                            (row['twin_id'],mapping[row['knowledge_id']]))
        for row in stage.execute("SELECT * FROM twin_grants WHERE subject_type='knowledge'"):
            if int(row['subject_key']) in valid and con.execute('SELECT 1 FROM twins WHERE id=?',(row['twin_id'],)).fetchone():
                con.execute('INSERT OR IGNORE INTO twin_grants(twin_id,subject_type,subject_key,effect) VALUES(?,?,?,?)',
                    (row['twin_id'],'knowledge',str(mapping[int(row['subject_key'])]),row['effect']))
        for row in stage.execute('SELECT * FROM browser_site_manuals'):
            if row['knowledge_id'] in valid:
                data=dict(row);data['knowledge_id']=mapping[data['knowledge_id']]
                con.execute('DELETE FROM browser_site_manuals WHERE knowledge_id=?',(data['knowledge_id'],))
                insert(con,'browser_site_manuals',data,replace=True)
        for row in stage.execute('SELECT * FROM browser_article_observations'):
            if row['knowledge_id'] in valid and con.execute('SELECT 1 FROM browser_capture_sessions WHERE id=?',(row['session_id'],)).fetchone():
                data=dict(row);data['knowledge_id']=mapping[data['knowledge_id']]
                insert(con,'browser_article_observations',data,replace=True)
        receipts=copy_receipts(stage,con,mapping,accepted_articles=valid)
        assert permissions==[tuple(r) for r in con.execute('SELECT id,enabled,allow_ai,allow_share FROM sources ORDER BY id')]
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        count=con.execute("SELECT count(*) FROM knowledge WHERE organization='article' AND status!='archived'").fetchone()[0]
    db.event('article_snapshot_applied','已更新现有知识文章，保留原始采集数据及来源开关')
    print(json.dumps({'applied_records':len(valid),'articles':count,'skipped_concurrent_changes':skipped,
                      'skipped_reasons':skipped_reasons,**receipts,
                      'source_permissions_unchanged':True,'integrity':'ok'}))


if __name__=='__main__':main()
