"""Evidence-backed project identity for individual knowledge-bearing work units.

A document or Codex session may contain several projects. The project name
comes from grounded extraction, not from a folder. Jev (or the main chat model)
only decides identity among already known candidates; it cannot invent labels.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets


def normalized_name(value):
    return re.sub(r'[\W_]+','',str(value).casefold())[:160]


def source_anchors(content: str, title: str = '') -> dict:
    """Independent repository/PR identifiers are stronger than a directory."""
    text = (title+'\n'+content)[:12000]
    matches=re.findall(r'https?://github\.com/([a-z0-9_.-]+/[a-z0-9_.-]+)(?:/pull/(\d+))?',text,re.I)
    repos={m[0].removesuffix('.git').lower() for m in matches}
    prs={m[0].removesuffix('.git').lower()+'#'+m[1] for m in matches if m[1]}
    return {'repositories':sorted(repos)[:8],'pull_requests':sorted(prs)[:8]}


def catalog(con):
    projects=[dict(p) for p in con.execute('SELECT * FROM project_entities ORDER BY created_at DESC LIMIT 300')]
    known={p['project_key'] for p in projects}
    for p in con.execute("""SELECT project_key,MIN(project) name FROM documents
        WHERE project_verified=1 AND scope='project' AND project_key!=''
        GROUP BY project_key LIMIT 300"""):
        if p['project_key'] not in known:
            projects.append({'project_key':p['project_key'],'name':p['name'],
                             'anchor_type':'','anchor':'','origin':'manual'})
            known.add(p['project_key'])
    # Provide one or two authorized existing conclusions so identity decisions
    # compare actual work rather than bare names. Do not send private sources.
    for project in projects:
        rows=con.execute("""SELECT k.topic,k.body FROM knowledge k
            WHERE k.project_key=? AND k.status='confirmed' AND k.quality='useful'
              AND (k.source_bound=0 OR EXISTS (
                SELECT 1 FROM knowledge_evidence e
                  JOIN documents d ON d.id=e.document_id
                  JOIN sources s ON s.id=d.source_id
                WHERE e.knowledge_id=k.id AND e.is_current=1
                  AND e.superseded=0 AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1))
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id
                JOIN sources s ON s.id=d.source_id
                WHERE e.knowledge_id=k.id AND (s.allow_ai=0 OR s.enabled=0 OR d.deleted=1))
            ORDER BY k.updated_at DESC LIMIT 2""",(project['project_key'],)).fetchall()
        project['summary']=' / '.join((r['topic']+': '+r['body'][:240]) for r in rows)[:550]
    return projects


def plan_work_units(items, document, projects, decision_router):
    """Only explicit project names can create automatic project identities.

    The router is invoked solely on plausible pairs; no user confirmation is
    requested for unmatched or undecidable units.
    """
    doc=dict(document)
    # Anchors must be extracted from each work unit, not the whole file.
    # A weekly report or long coding session may span unrelated projects.
    planned=[]
    available=list(projects)
    memo={}
    for item in items:
        anchors=source_anchors(' '.join(str(item.get(k) or '') for k in
            ('quote','context_quote','confirmation_quote')),doc.get('title',''))
        hint=str(item.get('project_hint') or '').strip()[:120]
        # A name inferred from a directory is not a source-grounded identity.
        # An explicit verified project assigned by the owner always wins.
        if doc.get('project_verified') and doc.get('project_key'):
            planned.append({'scope':'project','project_key':doc['project_key'],
                            'project':doc['project'],'status':'confirmed','confidence':1,
                            'reason':'用户已确认资料的项目归属'})
            continue
        if (not hint or len(normalized_name(hint))<3 or
            not (hint.casefold() in doc.get('content','').casefold())):
            planned.append({'scope':'session','project_key':'session:'+str(doc['id']),
                            'project':doc.get('project',''),'status':'isolated',
                            'confidence':0,'reason':'来源没有可验证的项目名称'})
            continue
        key_hint=normalized_name(hint)
        if key_hint in memo:
            planned.append(dict(memo[key_hint]))
            continue
        candidates=[p for p in available if normalized_name(p['name'])==key_hint or
            bool(anchors['repositories']) and p.get('anchor_type')=='repo' and
            p.get('anchor') in anchors['repositories']]
        accepted=[]
        for candidate in candidates[:6]:
            if candidate.get('anchor_type')=='repo' and candidate.get('anchor') and candidate['anchor'] not in anchors['repositories']:
                # A concrete contradictory repo is an identity conflict.
                continue
            strong=bool(normalized_name(candidate['name'])==key_hint and candidate.get('anchor_type')=='repo' and candidate['anchor'] in anchors['repositories'])
            if strong:
                match={'same':1.0,'conflict':0.0,'engine':'repo-anchor'}
            else:
                summary={'name':hint,'topic':item.get('topic',''),
                    'quote':str(item.get('quote',''))[:700],
                    'repositories':anchors['repositories'],
                    'pull_requests':anchors['pull_requests']}
                target={'name':candidate['name'],'project_key':candidate['project_key'],
                    'anchor_type':candidate.get('anchor_type',''),'anchor':candidate.get('anchor',''),
                    'existing_work':candidate.get('summary','')}
                match=decision_router.compare(summary,target)
            if match['same']>=0.92 and match['conflict']<=0.08:
                accepted.append((candidate,match))
        if len(accepted)==1:
            candidate,match=accepted[0]
            assignment={'scope':'project','project_key':candidate['project_key'],
                'project':candidate['name'],'status':'confirmed','confidence':match['same'],
                'reason':'独立项目匹配：'+match['engine']}
        elif candidates:
            # Two seemingly identical projects may still belong to different
            # customers. Do not guess or add a review obligation.
            assignment={'scope':'session','project_key':'session:'+str(doc['id']),
                'project':doc.get('project',''),'status':'provisional','confidence':0,
                'reason':'现有项目存在同名或冲突候选，保持会话隔离'}
        else:
            repo=anchors['repositories'][0] if len(anchors['repositories'])==1 else ''
            if any(p.get('anchor_type')=='repo' and p.get('anchor')==repo
                    for p in available):
                # A repository can host several business projects.
                repo=''
            assignment={'scope':'project','project_key':'auto:'+secrets.token_hex(16),
                'project':hint,'status':'confirmed','confidence':0.99,
                'anchor_type':'repo' if repo else '',
                'anchor':repo,'reason':'资料明确提及的项目名称'}
            available.append({'project_key':assignment['project_key'],'name':hint,
                'anchor_type':assignment.get('anchor_type',''),
                'anchor':assignment.get('anchor',''),'origin':'auto'})
        memo[key_hint]=assignment
        planned.append(dict(assignment))
    return planned


def apply_assignments(con, document_id, items, plan):
    """Atomic records for provenance and later retrospective relinking."""
    for item,link in zip(items,plan):
        if link['scope']=='project':
            con.execute("""INSERT OR IGNORE INTO project_entities
                (project_key,name,anchor_type,anchor,origin) VALUES(?,?,?,?,?)""",
                (link['project_key'],link['project'],link.get('anchor_type',''),
                 link.get('anchor',''),'auto' if link['project_key'].startswith('auto:') else 'manual'))
        quote=str(item.get('quote') or '')
        if not quote:
            continue
        con.execute("""INSERT INTO work_units
            (document_id,quote_hash,topic,project_hint,project_key,status,confidence,evidence_json)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(document_id,quote_hash) DO UPDATE SET
            topic=excluded.topic,project_hint=excluded.project_hint,
            project_key=excluded.project_key,status=excluded.status,
            confidence=excluded.confidence,evidence_json=excluded.evidence_json,
            updated_at=datetime('now')""",
            (document_id,hashlib.sha256(quote.encode()).hexdigest(),
             item.get('topic',''),item.get('project_hint',''),
             link['project_key'],link['status'],link['confidence'],
             json.dumps({'quote':quote[:1200],'reason':link['reason']},ensure_ascii=False)))
        if link['scope']!='project':
            continue
        # Find the note created from this evidence, regardless of whether it
        # was first seen in the same document or was a fresh addition.
        rows=con.execute("""SELECT k.id FROM knowledge k JOIN knowledge_evidence e
            ON e.knowledge_id=k.id WHERE e.document_id=? AND e.quote=?
            AND k.project_key=?""",(document_id,quote,link['project_key'])).fetchall()
        for row in rows:
            con.execute("""INSERT INTO project_memberships
                (knowledge_id,project_key,confidence,status,reason) VALUES(?,?,?,'confirmed',?)
                ON CONFLICT(knowledge_id) DO UPDATE SET
                project_key=excluded.project_key,confidence=excluded.confidence,
                status='confirmed',reason=excluded.reason,updated_at=datetime('now')""",
                (row['id'],link['project_key'],link['confidence'],link['reason']))
