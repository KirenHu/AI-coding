"""Low-priority project-identity rechecks as new authorized evidence appears.

Fresh knowledge may make an older session identifiable. This worker revisits
previously extracted work units, not whole folders. It never creates project
permissions; existing twin/MCP permissions are resolved dynamically.
"""
from __future__ import annotations

import hashlib
import json

from .projects import catalog, normalized_name, source_anchors
from .scope import snapshot_history


class ProjectRechecker:
    def __init__(self, db, decision_router):
        self.db = db
        self.judge = decision_router

    @staticmethod
    def _signature(projects):
        # Effective project evidence, not scanning time, triggers rechecking.
        stable = [(p['project_key'],p['name'],p.get('anchor',''),p.get('summary',''))
                  for p in projects]
        return hashlib.sha256(json.dumps(stable,ensure_ascii=False,sort_keys=True)
                              .encode('utf-8')).hexdigest()

    def process_next(self, limit=12):
        """Process one bounded batch, automatically continuing on idle cycles."""
        with self.db.connect() as con:
            projects=catalog(con)
            signature=self._signature(projects)
            old=con.execute("SELECT value FROM settings WHERE key='project_recheck_signature'").fetchone()
            cursor=con.execute("SELECT value FROM settings WHERE key='project_recheck_cursor'").fetchone()
            if not old or old['value']!=signature:
                position=0
                con.execute("""INSERT INTO settings(key,value) VALUES('project_recheck_signature',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(signature,))
            else:
                position=int(cursor['value']) if cursor else 0
            if position<0:
                return {'state':'idle','checked':0,'updated':0}
            rows=[dict(r) for r in con.execute("""
                SELECT w.id,w.document_id,w.quote_hash,w.topic,w.project_hint,
                       w.project_key,w.status,w.evidence_json,
                       d.title,d.content,d.sha256,d.project_verified,d.deleted,
                       s.enabled source_enabled,s.allow_ai
                  FROM work_units w
                  JOIN documents d ON d.id=w.document_id
                  JOIN sources s ON s.id=d.source_id
                 WHERE w.id>? ORDER BY w.id LIMIT ?""",(position,limit))]
            new_position=rows[-1]['id'] if len(rows)==limit else -1
            con.execute("""INSERT INTO settings(key,value) VALUES('project_recheck_cursor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(str(new_position),))
        if not rows:
            return {'state':'idle','checked':0,'updated':0}

        updated=0
        for work in rows:
            if (work['deleted'] or not work['source_enabled'] or not work['allow_ai']
                    or work['project_verified']):
                continue
            evidence=json.loads(work['evidence_json'])
            quote=evidence.get('quote','')
            if not quote or quote not in work['content']:
                continue
            anchors=source_anchors(quote)
            hint=str(work['project_hint'] or '').strip()
            # Only independently grounded source clues or an explicit name
            # may nominate a previously known project.
            proposals=[p for p in projects
                if p['project_key']!=work['project_key'] and
                ((hint and normalized_name(p['name'])==normalized_name(hint)) or
                 (p.get('anchor_type')=='repo' and p.get('anchor') in anchors['repositories']))]
            matches=[]
            for target in proposals[:5]:
                repo=target.get('anchor','') if target.get('anchor_type')=='repo' else ''
                if repo and anchors['repositories'] and repo not in anchors['repositories']:
                    continue
                exact=bool(repo and repo in anchors['repositories'] and hint
                           and normalized_name(target['name'])==normalized_name(hint))
                if work['status']=='confirmed' and not exact:
                    # Reassign a confirmed automatic project only with new
                    # independent identifier evidence, not name similarity.
                    continue
                if exact:
                    result={'same':1.0,'conflict':0.0,'engine':'repo-anchor'}
                else:
                    result=self.judge.compare(
                        {'name':hint,'quote':quote[:700],'topic':work['topic'],
                         'repositories':anchors['repositories']},
                        {'name':target['name'],'project_key':target['project_key'],
                         'anchor':repo,'existing_work':target.get('summary','')})
                if result['same']>=.92 and result['conflict']<=.08:
                    matches.append((target,result))
            if len(matches)!=1:
                continue
            target,result=matches[0]
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                current=con.execute("""SELECT w.project_key,w.status,d.sha256,d.content,
                    d.deleted,d.project_verified,s.enabled,s.allow_ai FROM work_units w
                    JOIN documents d ON d.id=w.document_id
                    JOIN sources s ON s.id=d.source_id WHERE w.id=?""",(work['id'],)).fetchone()
                if (not current or current['project_key']!=work['project_key']
                        or current['status']!=work['status']
                        or current['sha256']!=work['sha256'] or quote not in current['content']
                        or current['deleted'] or current['project_verified']
                        or not current['enabled'] or not current['allow_ai']):
                    continue
                # Do not rewrite owner-edited or multi-source knowledge. An
                # identity change must never silently absorb another source.
                linked=[dict(n) for n in con.execute("""SELECT DISTINCT k.*
                    FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
                    WHERE e.document_id=? AND e.quote=? AND k.project_key=?""",
                    (work['document_id'],quote,work['project_key']))]
                if any(k['created_by']=='human' or k['scope']=='global' or
                    con.execute("""SELECT 1 FROM knowledge_evidence
                        WHERE knowledge_id=? AND document_id!=? LIMIT 1""",
                        (k['id'],work['document_id'])).fetchone() for k in linked):
                    continue
                fresh_evidence=dict(evidence)
                fresh_evidence['reason']='新项目证据重新确认：'+result['engine']
                con.execute("""UPDATE work_units SET project_key=?,status='confirmed',
                    confidence=?,evidence_json=?,updated_at=datetime('now') WHERE id=?""",
                    (target['project_key'],result['same'],
                     json.dumps(fresh_evidence,ensure_ascii=False),work['id']))
                for note in linked:
                    snapshot_history(con,note)
                    con.execute("""UPDATE knowledge SET project_key=?,project=?,
                        scope='project',version=version+1,updated_at=datetime('now')
                        WHERE id=?""",(target['project_key'],target['name'],note['id']))
                    con.execute("""INSERT INTO project_memberships
                        (knowledge_id,project_key,confidence,status,reason)
                        VALUES(?,?,?,'confirmed',?)
                        ON CONFLICT(knowledge_id) DO UPDATE SET
                            project_key=excluded.project_key,
                            confidence=excluded.confidence,status='confirmed',
                            reason=excluded.reason,updated_at=datetime('now')""",
                            (note['id'],target['project_key'],result['same'],
                             fresh_evidence['reason']))
                updated+=1
        if updated:
            self.db.event('project_rechecked',f'依据新增证据自动更新 {updated} 个工作事项的项目归属')
        return {'state':'processed','checked':len(rows),'updated':updated}
