"""Revisit project identities when current source evidence changes.

The worker can both establish and withdraw an automatic relationship. Scope
changes reuse existing history and dynamic twin authorization; no review queue
or new permissions are created.
"""
from __future__ import annotations

import hashlib
import json

from .projects import (catalog, normalized_name, source_anchors, valid_grounding,
                       _identity_kind, continuity_context)
from .scope import snapshot_history


class ProjectRechecker:
    def __init__(self, db, decision_router):
        self.db = db
        self.judge = decision_router

    @staticmethod
    def _signature(projects, sources=()):
        # Model-generated labels/summary timestamps must not trigger themselves.
        stable = [(p['project_key'], p['name'], p.get('anchor',''),
                   p.get('identity_supports', [])) for p in projects]
        return hashlib.sha256(json.dumps([stable, sources], ensure_ascii=False,
            sort_keys=True).encode()).hexdigest()

    def process_next(self, limit=12):
        with self.db.connect() as con:
            projects = catalog(con)
            sources = [
                [tuple(r) for r in con.execute("""SELECT d.id,d.sha256,d.deleted,
                    d.project_verified,s.enabled,s.allow_ai FROM documents d
                    JOIN sources s ON s.id=d.source_id
                    WHERE EXISTS(SELECT 1 FROM work_units w WHERE w.document_id=d.id OR
                        EXISTS(SELECT 1 FROM json_each(w.evidence_json,'$.context_sources') j
                            WHERE json_extract(j.value,'$.document_id')=d.id))
                    ORDER BY d.id""")],
                [tuple(r) for r in con.execute(
                    'SELECT id,document_id,quote_hash,project_hint FROM work_units ORDER BY id')],
                [tuple(r) for r in con.execute("""SELECT e.id,e.is_current,e.superseded
                    FROM knowledge_evidence e
                    WHERE EXISTS(SELECT 1 FROM work_units w WHERE w.document_id=e.document_id)
                    ORDER BY e.id""")],
                [tuple(r) for r in con.execute("""SELECT t.document_id,t.session_id,
                    t.parent_session_id,d.sha256,d.deleted,s.enabled,s.allow_ai
                    FROM transcript_streams t JOIN documents d ON d.id=t.document_id
                    JOIN sources s ON s.id=d.source_id ORDER BY t.document_id""")],
            ]
            signature = self._signature(projects, sources)
            old = con.execute("SELECT value FROM settings WHERE key='project_recheck_signature'").fetchone()
            cursor = con.execute("SELECT value FROM settings WHERE key='project_recheck_cursor'").fetchone()
            position = int(cursor['value']) if old and old['value'] == signature and cursor else 0
            if position < 0:
                return {'state':'idle', 'checked':0, 'updated':0, 'withdrawn':0}
            con.execute("""INSERT INTO settings(key,value) VALUES('project_recheck_signature',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (signature,))
            con.execute("""INSERT INTO settings(key,value) VALUES('project_recheck_cursor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(position),))
            rows = [dict(r) for r in con.execute("""SELECT w.*,d.content,d.sha256,
                d.project_verified,d.deleted,s.enabled source_enabled,s.allow_ai,
                EXISTS(SELECT 1 FROM knowledge_evidence e WHERE e.document_id=w.document_id
                    AND e.quote=json_extract(w.evidence_json,'$.quote')
                    AND e.is_current=1 AND e.superseded=0) current_reference
                FROM work_units w JOIN documents d ON d.id=w.document_id
                JOIN sources s ON s.id=d.source_id
                WHERE w.id>? ORDER BY w.id LIMIT ?""", (position, limit))]
            entities = {p['project_key']:dict(p) for p in con.execute('SELECT * FROM project_entities')}
        updated = withdrawn = 0
        for work in rows:
            if work['project_verified']:
                continue
            evidence = json.loads(work['evidence_json'])
            quote = evidence.get('quote', '')
            local = ' '.join(str(evidence.get(k) or '') for k in
                             ('quote','context_quote','confirmation_quote'))
            authorized = not work['deleted'] and work['source_enabled'] and work['allow_ai']
            grounded = bool(quote and authorized and work['current_reference']
                            and valid_grounding(evidence, work['content']))
            anchors = source_anchors(local)
            hint = str(work['project_hint'] or '').strip()
            explicit = bool(hint and hint.casefold() in local.casefold())
            target = next((p for p in projects if p['project_key'] == work['project_key']), None)
            entity = entities.get(work['project_key'], {})
            repo = entity.get('anchor','') if entity.get('anchor_type') == 'repo' else ''
            contradicted = bool(repo and anchors['repositories'] and repo not in anchors['repositories'])
            independent = bool(grounded and explicit and not contradicted and
                _identity_kind(evidence) == 'explicit')
            # Exclude the assigned work from the candidate's support; matching
            # a previous label against itself would never discover a mistake.
            independent_target = self._without_self(target, work, quote)
            invalid = bool(work['status'] == 'confirmed' and
                (not grounded or contradicted or (not independent and not independent_target)))
            context = ''
            context_keys = set()
            context_sources = []
            if grounded and (not explicit or _identity_kind(evidence) == 'context'):
                from .artifacts import work_context
                with self.db.connect() as con:
                    ctx = work_context(con, work['document_id'], quote=quote)
                context, context_keys, context_sources = continuity_context(
                    {'quote':quote, 'work_context':ctx}, projects, work['document_id'])
                if _identity_kind(evidence) == 'context' and work['project_key'] not in context_keys:
                    invalid = True

            matches = []
            if grounded:
                proposals = [p for p in projects if
                    (explicit and normalized_name(p['name']) == normalized_name(hint)) or
                    (not explicit and p['project_key'] in context_keys)]
                proposals.sort(key=lambda p:p['project_key'] != work['project_key'])
                for project in proposals[:6]:
                    proposal = self._without_self(project, work, quote)
                    restoring_origin = bool(grounded and _identity_kind(evidence) == 'explicit'
                        and evidence.get('original_project_key') == project['project_key']
                        and work['status'] != 'confirmed')
                    if restoring_origin:
                        proposal = project
                    if not proposal:
                        continue
                    if not ((explicit and normalized_name(proposal['name']) == normalized_name(hint))
                            or (not explicit and proposal['project_key'] in context_keys)):
                        continue
                    candidate_repo = proposal.get('anchor','') if proposal.get('anchor_type') == 'repo' else ''
                    observed = anchors['repositories'] or (source_anchors(context)['repositories'] if context else [])
                    if candidate_repo and candidate_repo not in observed and (explicit or observed):
                        continue
                    if proposal['project_key'] == work['project_key'] and independent:
                        continue
                    result = ({'same':1.0, 'conflict':0.0, 'engine':'source-origin'} if restoring_origin else
                        self.judge.compare({'name':hint if explicit else '', 'quote':quote[:700],
                            'topic':work['topic'], 'repositories':anchors['repositories'],
                            'preceding_work':context},
                            {'name':proposal['name'], 'project_key':proposal['project_key'],
                             'anchor':candidate_repo, 'existing_work':proposal.get('summary','')}))
                    if proposal['project_key'] == work['project_key'] and result['conflict'] >= .92:
                        invalid = True
                    if result['same'] >= .92 and result['conflict'] <= .08:
                        matches.append((proposal, result))
            chosen = matches[0] if len(matches) == 1 else None
            if chosen and chosen[0]['project_key'] != work['project_key']:
                # Already grounded identities only move on new exact identity
                # evidence, or after their old basis was withdrawn.
                exact = (chosen[0].get('anchor_type') == 'repo' and
                         chosen[0].get('anchor') in anchors['repositories'])
                if work['status'] == 'confirmed' and not invalid and not exact:
                    chosen = None
            if chosen and (chosen[0]['project_key'] != work['project_key'] or work['status'] != 'confirmed'):
                if self._apply(work, evidence, chosen[0], chosen[1], context, context_sources):
                    updated += 1
            elif invalid:
                if self._apply(work, evidence, None, None, ''):
                    updated += 1
                    withdrawn += 1
        # Advance only after the batch succeeds so interrupted work can retry.
        with self.db.connect() as con:
            con.execute("""INSERT INTO settings(key,value) VALUES('project_recheck_cursor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(rows[-1]['id'] if rows and len(rows) == limit else -1),))
        if updated:
            self.db.event('project_rechecked', f'自动更新 {updated} 个工作事项的项目归属，其中撤销 {withdrawn} 个失效关联')
        return {'state':'processed' if rows else 'idle', 'checked':len(rows),
                'updated':updated, 'withdrawn':withdrawn}

    @staticmethod
    def _without_self(project, work, quote):
        if not project:
            return None
        supports = [s for s in project.get('identity_supports', [])
                    if s.get('verified') or not (s['document_id'] == work['document_id'] and s['quote'] == quote)]
        if not supports:
            return None
        return dict(project, identity_supports=supports,
                    summary=' / '.join(s['quote'][:260] for s in supports[:2])[:550])

    def _apply(self, work, evidence, target, result, context, context_sources=()):
        quote = evidence.get('quote','')
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            current = con.execute("""SELECT w.project_key,w.status,w.evidence_json,d.sha256,
                d.deleted,d.project_verified,s.enabled,s.allow_ai FROM work_units w
                JOIN documents d ON d.id=w.document_id JOIN sources s ON s.id=d.source_id
                WHERE w.id=?""", (work['id'],)).fetchone()
            if (not current or current['project_key'] != work['project_key']
                    or current['status'] != work['status'] or current['sha256'] != work['sha256']
                    or current['evidence_json'] != work['evidence_json'] or current['project_verified']):
                return False
            if target:
                if current['deleted'] or not current['enabled'] or not current['allow_ai']:
                    return False
                for source in context_sources:
                    valid = con.execute("""SELECT 1 FROM documents d JOIN sources s ON s.id=d.source_id
                        WHERE d.id=? AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1
                          AND instr(d.content,?)>0""", (source['document_id'], source['quote'])).fetchone()
                    if not valid:
                        return False
                live = next((p for p in catalog(con) if p['project_key'] == target['project_key']), None)
                if not live or not any(s in live['identity_supports'] for s in target['identity_supports']):
                    return False
            linked = [dict(k) for k in con.execute("""SELECT DISTINCT k.* FROM knowledge k
                JOIN knowledge_evidence e ON e.knowledge_id=k.id
                WHERE e.document_id=? AND e.quote=?""", (work['document_id'], quote))]
            if target and any(k['created_by'] == 'human' or k['scope'] == 'global' for k in linked):
                return False
            key = target['project_key'] if target else 'session:'+str(work['document_id'])
            status = 'confirmed' if target else 'isolated'
            reason = '新项目证据重新确认：'+result['engine'] if target else '项目关联依据已失效，自动恢复会话范围'
            fresh = dict(evidence, reason=reason, identity_kind=_identity_kind(evidence))
            if fresh['identity_kind'] == 'explicit' and work['project_key'].startswith(('auto:', 'manual:')):
                fresh.setdefault('original_project_key', work['project_key'])
            if target:
                fresh.update(identity_kind=('explicit' if result['engine'] == 'source-origin'
                                            else 'context' if context else 'matched'),
                             identity_supports=target['identity_supports'])
                if context:
                    fresh['preceding_work'] = context
                    fresh['context_sources'] = context_sources
            con.execute("""UPDATE work_units SET project_key=?,status=?,confidence=?,
                evidence_json=?,updated_at=datetime('now') WHERE id=?""",
                (key, status, result['same'] if target else 0,
                 json.dumps(fresh, ensure_ascii=False), work['id']))
            for note in linked:
                if note['created_by'] == 'human' or note['scope'] == 'global':
                    continue
                if note['project_key'] not in (work['project_key'], 'session:'+str(work['document_id'])):
                    continue
                # Multi-source content is only promoted when every current
                # contribution independently agrees on the destination.
                other = [dict(r) for r in con.execute("""SELECT e.document_id,e.quote,
                    d.content,d.deleted,s.enabled,s.allow_ai,w.project_key,w.status
                    FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
                    JOIN sources s ON s.id=d.source_id
                    LEFT JOIN work_units w ON w.document_id=e.document_id
                      AND json_extract(w.evidence_json,'$.quote')=e.quote
                    WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0
                      AND NOT(e.document_id=? AND e.quote=?)""",
                    (note['id'], work['document_id'], quote))]
                agrees = lambda r, p: (not r['deleted'] and r['enabled'] and r['allow_ai']
                    and r['quote'] in r['content'] and r['status'] == 'confirmed' and r['project_key'] == p)
                if target and any(not agrees(r, key) for r in other):
                    continue
                if not target and any(agrees(r, note['project_key']) for r in other):
                    continue
                if note['project_key'] == key and note['scope'] == ('project' if target else 'session'):
                    continue
                snapshot_history(con, note)
                con.execute("""UPDATE knowledge SET project_key=?,project=?,scope=?,
                    version=version+1,updated_at=datetime('now') WHERE id=?""",
                    (key, target['name'] if target else note['project'],
                     'project' if target else 'session', note['id']))
                if target:
                    con.execute("""INSERT INTO project_memberships
                        (knowledge_id,project_key,confidence,status,reason) VALUES(?,?,?,'confirmed',?)
                        ON CONFLICT(knowledge_id) DO UPDATE SET project_key=excluded.project_key,
                        confidence=excluded.confidence,status='confirmed',reason=excluded.reason,
                        updated_at=datetime('now')""", (note['id'], key, result['same'], reason))
                else:
                    con.execute('DELETE FROM project_memberships WHERE knowledge_id=?', (note['id'],))
        return True
