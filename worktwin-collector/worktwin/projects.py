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


def _identity_kind(evidence):
    """Old rows retain the source/match distinction recorded in their reason."""
    if evidence.get('identity_kind'):
        return evidence['identity_kind']
    reason = evidence.get('reason', '')
    return 'matched' if ('匹配' in reason or '重新确认' in reason) else 'explicit'


def valid_grounding(evidence, content):
    """Identity is grounded in exact source excerpts, never in its assigned label."""
    return all(not evidence.get(field) or evidence[field] in content
               for field in ('quote', 'context_quote', 'confirmation_quote'))


def catalog(con):
    """Project candidates grounded in current source evidence.

    A previous model match is not an independent witness to its own identity.
    Keep source excerpts on candidates so retrospective decisions can exclude
    the work being checked and retain the evidence used for their decision.
    """
    projects = {p['project_key']: dict(p) for p in con.execute(
        'SELECT * FROM project_entities ORDER BY created_at DESC LIMIT 300')}
    supports = {key: [] for key in projects}
    for row in con.execute("""SELECT d.id,d.project_key,d.project,d.content
        FROM documents d JOIN sources s ON s.id=d.source_id
        WHERE d.project_verified=1 AND d.scope='project' AND d.project_key!=''
          AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1"""):
        key = row['project_key']
        projects.setdefault(key, {'project_key':key, 'name':row['project'],
            'anchor_type':'', 'anchor':'', 'origin':'manual'})
        supports.setdefault(key, []).append({'document_id':row['id'],
            'quote':row['content'][:700], 'verified':True})
    rows = con.execute("""SELECT DISTINCT k.project_key,e.document_id,e.quote,
            d.content,w.id work_id,w.evidence_json
        FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
        JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
        LEFT JOIN work_units w ON w.document_id=e.document_id
          AND json_extract(w.evidence_json,'$.quote')=e.quote
        WHERE k.scope IN ('project','session') AND e.is_current=1 AND e.superseded=0
          AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1
        ORDER BY e.document_id,e.quote""")
    for row in rows:
        evidence = json.loads(row['evidence_json'] or '{}')
        key = (evidence.get('original_project_key', row['project_key'])
               if _identity_kind(evidence) == 'explicit' else row['project_key'])
        project = projects.get(key)
        if not project or not row['quote'] or row['quote'] not in row['content']:
            continue
        raw = ' '.join(str(evidence.get(key) or '') for key in
                       ('quote', 'context_quote', 'confirmation_quote')).strip() or row['quote']
        if not valid_grounding(evidence, row['content']):
            continue
        named = project['name'].casefold() in raw.casefold()
        if not named or _identity_kind(evidence) != 'explicit':
            continue
        support = {'document_id':row['document_id'], 'quote':row['quote'],
                   'work_id':row['work_id'], 'origin':_identity_kind(evidence) == 'explicit'}
        for field in ('context_quote', 'confirmation_quote'):
            if evidence.get(field):
                support[field] = evidence[field]
        if support not in supports[key]:
            supports[key].append(support)
    result = []
    for key, project in projects.items():
        if not supports[key]:
            continue
        project['identity_supports'] = supports[key]
        project['summary'] = ' / '.join(s['quote'][:260] for s in supports[key][:2])[:550]
        result.append(project)
    return result


def continuity_context(item, projects, document_id=None):
    """Nearby turns, including an explicit fork parent, nominate candidates."""
    work_context = item.get('work_context', {})
    parent = work_context.get('parent_context', {})
    before = [dict(e, document_id=e.get('document_id', parent.get('document_id')))
              for e in parent.get('events', [])]
    quote = str(item.get('quote') or '')
    for event in work_context.get('events', []):
        before.append(dict(event, document_id=event.get('document_id', document_id)))
        if quote and quote in str(event.get('text') or ''):
            break
    before = [e for e in before if e.get('role') in ('user', 'assistant')][-5:]
    text = '\n'.join(str(e.get('text') or '') for e in before)[-4000:]
    keys = {p['project_key'] for p in projects if p['name'].casefold() in text.casefold()}
    references = [{'document_id':e['document_id'], 'event_id':e.get('event_id', ''),
                   'quote':str(e.get('text') or '')[-4000:]} for e in before
                  if e.get('document_id') is not None]
    return text, keys, references


def plan_work_units(items, document, projects, decision_router):
    """Explicit names create identities; continuous work can match known ones."""
    doc = dict(document)
    planned = []
    available = list(projects)
    memo = {}
    for item in items:
        local = ' '.join(str(item.get(k) or '') for k in
                         ('quote', 'context_quote', 'confirmation_quote'))
        anchors = source_anchors(local)
        hint = str(item.get('project_hint') or '').strip()[:120]
        if doc.get('project_verified') and doc.get('project_key'):
            planned.append({'scope':'project', 'project_key':doc['project_key'],
                'project':doc['project'], 'status':'confirmed', 'confidence':1,
                'identity_kind':'verified', 'reason':'用户已确认资料的项目归属'})
            continue
        explicit = bool(hint and len(normalized_name(hint)) >= 3 and
                        hint.casefold() in local.casefold())
        context, context_keys, context_sources = continuity_context(item, available, doc['id'])
        if not explicit and not context_keys:
            planned.append({'scope':'session', 'project_key':'session:'+str(doc['id']),
                'project':doc.get('project',''), 'status':'isolated', 'confidence':0,
                'identity_kind':'isolated', 'reason':'来源没有可验证的项目名称或连续工作上下文'})
            continue
        key_hint = normalized_name(hint) if explicit else ''
        # Context differs between units even when their project hints match.
        memo_key = (key_hint, tuple(anchors['repositories']),
                    tuple(anchors['pull_requests']), local, context)
        if memo_key in memo:
            planned.append(dict(memo[memo_key]))
            continue
        candidates = [p for p in available if
            (explicit and normalized_name(p['name']) == key_hint) or
            (explicit and anchors['repositories'] and p.get('anchor_type') == 'repo'
             and p.get('anchor') in anchors['repositories']) or
            (not explicit and p['project_key'] in context_keys)]
        accepted = []
        for candidate in candidates[:6]:
            repo = candidate.get('anchor','') if candidate.get('anchor_type') == 'repo' else ''
            # Missing identifiers can be supplied by a real neighboring turn,
            # but never by a file title or working directory.
            contextual_anchors = source_anchors(context) if not explicit else anchors
            observed = anchors['repositories'] or contextual_anchors['repositories']
            if repo and repo not in observed and (explicit or observed):
                continue
            own_origin = any(s.get('origin') and s['document_id'] == doc['id']
                             and s['quote'] == str(item.get('quote',''))
                             for s in candidate.get('identity_supports', []))
            if own_origin:
                match = {'same':1.0, 'conflict':0.0, 'engine':'source-origin'}
            else:
                match = decision_router.compare(
                    {'name':hint if explicit else '', 'topic':item.get('topic',''),
                     'quote':str(item.get('quote',''))[:700],
                     'repositories':anchors['repositories'],
                     'pull_requests':anchors['pull_requests'],
                     'preceding_work':context if not explicit else ''},
                    {'name':candidate['name'], 'project_key':candidate['project_key'],
                     'anchor_type':candidate.get('anchor_type',''), 'anchor':repo,
                     'existing_work':candidate.get('summary','')})
            if match['same'] >= .92 and match['conflict'] <= .08:
                accepted.append((candidate, match))
        if len(accepted) == 1:
            candidate, match = accepted[0]
            assignment = {'scope':'project', 'project_key':candidate['project_key'],
                'project':candidate['name'], 'status':'confirmed', 'confidence':match['same'],
                'identity_kind':('explicit' if match['engine'] == 'source-origin'
                                 else 'matched' if explicit else 'context'),
                'identity_supports':candidate.get('identity_supports', []),
                'reason':'独立项目匹配：'+match['engine']}
            if not explicit:
                assignment['preceding_work'] = context
                assignment['context_sources'] = context_sources
        elif candidates or not explicit:
            assignment = {'scope':'session', 'project_key':'session:'+str(doc['id']),
                'project':doc.get('project',''), 'status':'provisional', 'confidence':0,
                'identity_kind':'isolated', 'reason':'项目身份尚不明确，保持会话范围'}
        else:
            repo = anchors['repositories'][0] if len(anchors['repositories']) == 1 else ''
            if any(p.get('anchor_type') == 'repo' and p.get('anchor') == repo
                   for p in available):
                repo = ''
            assignment = {'scope':'project', 'project_key':'auto:'+secrets.token_hex(16),
                'project':hint, 'status':'confirmed', 'confidence':.99,
                'anchor_type':'repo' if repo else '', 'anchor':repo,
                'identity_kind':'explicit', 'reason':'资料明确提及的项目名称'}
            available.append({'project_key':assignment['project_key'], 'name':hint,
                'anchor_type':assignment['anchor_type'], 'anchor':repo,
                'origin':'auto', 'summary':str(item.get('quote',''))[:550],
                'identity_supports':[{'document_id':doc['id'], 'quote':str(item.get('quote','')), 'origin':True,
                    **{k:item[k] for k in ('context_quote','confirmation_quote') if item.get(k)}}]})
        memo[memo_key] = assignment
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
             json.dumps({'quote':quote, 'reason':link['reason'],
                **({'original_project_key':link['project_key']}
                   if link.get('identity_kind') == 'explicit' else {}),
                **{k:item[k] for k in ('context_quote','confirmation_quote') if item.get(k)},
                **{k:link[k] for k in ('identity_kind','identity_supports','preceding_work','context_sources')
                   if k in link}}, ensure_ascii=False)))
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
