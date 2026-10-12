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
from pathlib import Path
from datetime import datetime, timezone


def source_context(con, document, *, authorized_documents=None):
    """Location, chronology and adjacent authorized work are routing inputs."""
    doc = dict(document)
    authorized = set(authorized_documents) if authorized_documents is not None else None
    if authorized is not None and doc['id'] not in authorized:
        raise ValueError('Document is outside the authorized routing scope')
    source = con.execute('SELECT root,kind FROM sources WHERE id=?', (doc['source_id'],)).fetchone()
    original = con.execute('SELECT content FROM documents WHERE id=?', (doc['id'],)).fetchone()['content']
    cwd = re.search(r'^工作目录：([^\n]+)', original, re.M)
    workspace = cwd.group(1).strip() if cwd else str(Path(doc.get('path') or source['root']).parent)
    access = ('d.id IN ('+','.join('?' for _ in authorized)+')'
              if authorized is not None else 's.allow_ai=1')
    neighbors = [dict(row) for row in con.execute('''SELECT d.id,d.title,d.path,
        d.mtime_ns,d.content FROM documents d JOIN sources s ON s.id=d.source_id
        WHERE d.id!=? AND d.deleted=0 AND '''+access+''' AND d.source_id=?
        ORDER BY ABS(d.mtime_ns-?) LIMIT 3''',
        (doc['id'],*(authorized or ()),doc['source_id'],doc.get('mtime_ns', 0)))]
    current_projects=[]
    for article in con.execute('''SELECT DISTINCT k.id,k.project_key,k.project,k.article_summary
            FROM project_documents p JOIN knowledge k ON k.id=p.knowledge_id
            JOIN knowledge_evidence e ON e.knowledge_id=k.id JOIN documents d ON d.id=e.document_id
            WHERE d.source_id=? AND e.is_current=1 AND e.superseded=0 AND k.status!='archived' ''',(doc['source_id'],)):
        evidence=con.execute('''SELECT d.id,d.deleted,s.allow_ai FROM article_current_sources a
            JOIN documents d ON d.id=a.document_id JOIN sources s ON s.id=d.source_id
            WHERE a.knowledge_id=?''',(article['id'],)).fetchall()
        if evidence and all(not r['deleted'] and (r['id'] in authorized if authorized is not None else r['allow_ai']) for r in evidence):
            current_projects.append({'project_key':article['project_key'],'name':article['project'],
                                     'current_summary':article['article_summary'][:500]})
    return {'workspace':workspace, 'folder_name':Path(workspace).name,'source_root':source['root'],
            'file_path':doc.get('path',''), 'title':doc.get('title',''),
            'modified_at':datetime.fromtimestamp(doc.get('mtime_ns', 0)/1e9, timezone.utc).isoformat(),
            'document_opening':original[:1800],
            'project_documents':current_projects,
            'nearby_work':[{'document_id':r['id'], 'title':r['title'], 'path':r['path'],
                            'modified_at':r['mtime_ns'], 'excerpt':r['content'][:700]} for r in neighbors]}


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


def project_brief(project, supports, records=()):
    """A bounded, live project background, distinct from identity witnesses."""
    records = list(records)
    objectives = list(dict.fromkeys(r['objective'] for r in records if r.get('objective')))[:3]
    workspaces = list(dict.fromkeys(r['workspace'] for r in records if r.get('workspace')))[:4]
    topics = list(dict.fromkeys(r['topic'] for r in records if r.get('topic')))[:10]
    # Recent work broadens a project beyond the first extracted feature. Keep
    # the original identity witnesses separately so labels cannot prove themselves.
    excerpts = list(records[:6])
    for support in supports:
        if not any(r['document_id'] == support['document_id'] and r['quote'] == support['quote']
                   for r in excerpts):
            excerpts.append(dict(support))
        if len(excerpts) >= 8:
            break
    background = {'objectives':objectives, 'workspaces':workspaces,
                  'topics':topics, 'records':excerpts}
    parts = ['持续目标：'+'；'.join(objectives)] if objectives else []
    parts.extend(r['quote'][:400] for r in excerpts)
    return dict(project, identity_supports=supports, background=background,
                summary=' / '.join(parts)[:2400])


def catalog(con, *, authorized_documents=None):
    """Project candidates grounded in current source evidence.

    A previous model match is not an independent witness to its own identity.
    Keep source excerpts on candidates so retrospective decisions can exclude
    the work being checked and retain the evidence used for their decision.
    """
    projects = {p['project_key']: dict(p) for p in con.execute(
        'SELECT * FROM project_entities ORDER BY created_at DESC LIMIT 300')}
    supports = {key: [] for key in projects}
    records = {key: [] for key in projects}
    authorized = set(authorized_documents) if authorized_documents is not None else None
    access = ('d.id IN ('+','.join('?' for _ in authorized)+')'
              if authorized is not None else 's.allow_ai=1')
    for row in con.execute("""SELECT d.id,d.project_key,d.project,d.content
        FROM documents d JOIN sources s ON s.id=d.source_id
        WHERE d.project_verified=1 AND d.scope='project' AND d.project_key!=''
          AND d.deleted=0 AND """+access,tuple(authorized or ())):
        key = row['project_key']
        projects.setdefault(key, {'project_key':key, 'name':row['project'],
            'anchor_type':'', 'anchor':'', 'origin':'manual'})
        supports.setdefault(key, []).append({'document_id':row['id'],
            'quote':row['content'][:700], 'verified':True})
        records.setdefault(key, [])
    rows = con.execute("""SELECT DISTINCT k.project_key,k.topic,e.document_id,e.quote,
            d.content,w.id work_id,w.evidence_json,w.status work_status,
            w.project_key work_project_key,w.topic work_topic,w.updated_at,
            s.root,d.mtime_ns
        FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
        JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
        LEFT JOIN work_units w ON w.document_id=e.document_id
          AND json_extract(w.evidence_json,'$.quote')=e.quote
        WHERE k.scope IN ('project','session') AND e.is_current=1 AND e.superseded=0
          AND d.deleted=0 AND """+access+"""
        ORDER BY w.updated_at DESC,d.mtime_ns DESC,e.document_id,e.quote""",tuple(authorized or ()))
    for row in rows:
        evidence = json.loads(row['evidence_json'] or '{}')
        if not row['quote'] or row['quote'] not in row['content'] or not valid_grounding(evidence, row['content']):
            continue
        current_key = row['project_key']
        if current_key in projects and (not row['work_id'] or
                row['work_status'] == 'confirmed' and row['work_project_key'] == current_key):
            cwd = re.search(r'^工作目录：([^\n]+)', row['content'], re.M)
            record = {'document_id':row['document_id'], 'quote':row['quote'],
                      'topic':row['work_topic'] or row['topic'],
                      'objective':str(evidence.get('project_objective') or '')[:550],
                      'workspace':cwd.group(1).strip() if cwd else row['root']}
            if record not in records[current_key] and len(records[current_key]) < 12:
                records[current_key].append(record)
        key = (evidence.get('original_project_key', row['project_key'])
               if _identity_kind(evidence) == 'explicit' else row['project_key'])
        project = projects.get(key)
        if not project:
            continue
        raw = ' '.join(str(evidence.get(key) or '') for key in
                       ('quote', 'context_quote', 'confirmation_quote')).strip() or row['quote']
        named = project['name'].casefold() in raw.casefold()
        inferred = _identity_kind(evidence) == 'inferred'
        if not (named and _identity_kind(evidence) == 'explicit' or inferred):
            continue
        support = {'document_id':row['document_id'], 'quote':row['quote'],
                   'work_id':row['work_id'], 'origin':True,
                   'identity_kind':_identity_kind(evidence)}
        for field in ('context_quote', 'confirmation_quote'):
            if evidence.get(field):
                support[field] = evidence[field]
        if support not in supports[key]:
            supports[key].append(support)
    result = []
    for key, project in projects.items():
        if not supports[key]:
            continue
        result.append(project_brief(project, supports[key], records[key]))
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


def project_candidates(item, projects, context_keys=()):
    """Nominate by durable context; a feature name need not equal its project."""
    hint = normalized_name(item.get('project_hint', ''))
    local = ' '.join(str(item.get(k) or '') for k in
                     ('quote', 'context_quote', 'confirmation_quote'))
    anchors = source_anchors(local)
    routing_context = item.get('project_context', {})
    candidates = [p for p in projects if
        hint and normalized_name(p['name']) == hint or
        p['project_key'] in context_keys or
        p.get('anchor_type') == 'repo' and p.get('anchor') in anchors['repositories'] or
        routing_context and p.get('summary')]
    text = (' '.join(str(item.get(k) or '') for k in
                     ('project_hint', 'project_objective', 'topic'))+' '+local).casefold()
    terms = {text[i:i+2] for i in range(len(text)-1) if not text[i:i+2].isspace()}
    workspace = routing_context.get('workspace', '')
    def score(project):
        name = normalized_name(project['name'])
        background = project.get('background', {})
        exact = bool(hint and name == hint)
        named = bool(name and name in hint)
        same_workspace = bool(workspace and workspace in background.get('workspaces', []))
        relevance = sum(t in (project['name']+' '+project.get('summary', '')).casefold() for t in terms)
        return exact, project['project_key'] in context_keys, named, same_workspace, relevance
    candidates.sort(key=score, reverse=True)
    # Exact naming is stronger, but a broader known project is still evaluated
    # when its goal/context agrees: names from extraction are not a partition.
    return candidates[:6]


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
        routing_context = item.get('project_context', {})
        inferred = bool(routing_context)
        context, context_keys, context_sources = continuity_context(item, available, doc['id'])
        if not explicit and not context_keys and not inferred:
            planned.append({'scope':'session', 'project_key':'session:'+str(doc['id']),
                'project':doc.get('project',''), 'status':'isolated', 'confidence':0,
                'identity_kind':'isolated', 'reason':'来源没有可验证的项目名称或连续工作上下文'})
            continue
        key_hint = normalized_name(hint) if explicit or inferred else ''
        # Context differs between units even when their project hints match.
        memo_key = (key_hint, tuple(anchors['repositories']),
                    tuple(anchors['pull_requests']), local, context,
                    str(item.get('project_objective') or ''),
                    json.dumps(routing_context, ensure_ascii=False, sort_keys=True))
        if memo_key in memo:
            planned.append(dict(memo[memo_key]))
            continue
        candidates = project_candidates(item, available, context_keys)
        accepted = []
        for candidate in candidates[:6]:
            repo = candidate.get('anchor','') if candidate.get('anchor_type') == 'repo' else ''
            # Missing identifiers can be supplied by a real neighboring turn,
            # but never by a file title or working directory.
            contextual_anchors = source_anchors(context or routing_context.get('document_opening', ''))
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
                    {'name':hint if explicit or inferred else '', 'topic':item.get('topic',''),
                     'quote':str(item.get('quote',''))[:700],
                     'objective':str(item.get('project_objective') or '')[:550],
                     'repositories':anchors['repositories'],
                     'pull_requests':anchors['pull_requests'],
                     'preceding_work':context if not explicit else '',
                     'source_context':routing_context},
                    {'name':candidate['name'], 'project_key':candidate['project_key'],
                     'anchor_type':candidate.get('anchor_type',''), 'anchor':repo,
                     'project_background':{k:v for k,v in candidate.get('background', {}).items()
                                           if k != 'records'},
                     'existing_work':candidate.get('summary','')})
            if match['same'] >= .92 and match['conflict'] <= .08:
                accepted.append((candidate, match))
        if len(accepted) == 1:
            candidate, match = accepted[0]
            assignment = {'scope':'project', 'project_key':candidate['project_key'],
                'project':candidate['name'], 'status':'confirmed', 'confidence':match['same'],
                'identity_kind':(next(s.get('identity_kind', 'explicit') for s in candidate['identity_supports']
                                     if s['document_id'] == doc['id'] and s['quote'] == item['quote'])
                                 if match['engine'] == 'source-origin'
                                 else 'matched' if explicit or inferred else 'context'),
                'identity_supports':candidate.get('identity_supports', []),
                'reason':'独立项目匹配：'+match['engine']}
            if not explicit:
                assignment['preceding_work'] = context
                assignment['context_sources'] = context_sources
        elif inferred and hint and item.get('project_objective') and not accepted:
            # A new durable objective is decided from content and context;
            # a folder or nearby timestamp alone is insufficient.
            decision = decision_router.compare(
                {'name':hint,'topic':item.get('topic',''),'quote':str(item.get('quote',''))[:1200],
                 'source_context':routing_context},
                {'name':hint,'proposed_new_project':True,
                 'objective':item.get('project_objective','')})
            if decision['same'] >= .92 and decision['conflict'] <= .08:
                assignment = {'scope':'project','project_key':'auto:'+secrets.token_hex(16),
                    'project':hint,'status':'confirmed','confidence':decision['same'],
                    'identity_kind':'explicit' if explicit else 'inferred',
                    'reason':'资料与工作上下文识别新项目：'+decision['engine']}
                available.append({'project_key':assignment['project_key'],'name':hint,
                    'origin':'auto','summary':str(item.get('project_objective') or item['quote'])[:550],
                    'background':{'objectives':[item['project_objective']],
                                  'workspaces':[routing_context.get('workspace','')]},
                    'identity_supports':[{'document_id':doc['id'],'quote':item['quote'],'origin':True,
                                          'identity_kind':assignment['identity_kind']}]})
            else:
                assignment = {'scope':'session','project_key':'session:'+str(doc['id']),
                    'project':doc.get('project',''),'status':'provisional','confidence':0,
                    'identity_kind':'isolated','reason':'项目上下文不足，暂保留本次资料范围'}
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
        if inferred:
            assignment['project_context'] = routing_context
        if item.get('project_objective'):
            assignment['project_objective'] = item['project_objective']
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
                **{k:link[k] for k in ('identity_kind','identity_supports','preceding_work','context_sources','project_context','project_objective')
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
