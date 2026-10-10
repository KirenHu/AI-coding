"""One dynamic knowledge authorization resolver shared by local QA, MCP and publishing.

Project classification never creates a grant. Explicit knowledge IDs remain a
compatibility allowance. Every source behind an article is checked at read time.
"""
from __future__ import annotations

from .knowledge_policy import READY_SQL, SHARE_SQL


def effective_notes(con, twin_id: int, *, sharing: bool = False) -> list[dict]:
    twin = con.execute("SELECT knowledge_mode FROM twins WHERE id=?", (twin_id,)).fetchone()
    if not twin:
        return []
    ready = SHARE_SQL if sharing else READY_SQL
    candidates = [dict(k) for k in con.execute("SELECT k.* FROM knowledge k WHERE " + ready)]
    pinned = {r[0] for r in con.execute("SELECT knowledge_id FROM twin_knowledge WHERE twin_id=?", (twin_id,))}
    grant_rows = con.execute("""SELECT subject_type,subject_key,effect
        FROM twin_grants WHERE twin_id=?""",(twin_id,)).fetchall()
    grant = {'allow':set(), 'deny':set()}
    for item in grant_rows:
        grant[item['effect']].add((item['subject_type'],item['subject_key']))
    result = []
    for k in candidates:
        kid = str(k['id'])
        records = con.execute("""SELECT d.id,d.deleted,s.id source_id,
                s.enabled,s.allow_ai,s.allow_share,e.is_current,e.superseded
                FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id
                JOIN sources s ON s.id=d.source_id
                WHERE e.knowledge_id=?""",(k['id'],)).fetchall()
        if k['source_bound'] and not records:
            continue
        # Permission revocation or a deleted contributing document invalidates
        # the whole article, including any previously assigned pin.
        if any(not r['enabled'] or r['deleted'] or not r['allow_ai'] or
               (sharing and not r['allow_share']) for r in records):
            continue
        active_sources={str(r['source_id']) for r in records
                        if r['is_current'] and not r['superseded']}
        if k['source_bound'] and not active_sources:
            continue
        project = ('project',k['project_key']) if k['scope']=='project' else None
        # A provisional project identity never expands access.
        if project:
            membership=con.execute("""SELECT status FROM project_memberships
                WHERE knowledge_id=?""",(k['id'],)).fetchone()
            if membership and membership['status']!='confirmed':
                project=None
        neg=grant['deny']
        if (('knowledge',kid) in neg or
            (project is not None and project in neg) or
            (k['scope']=='global' and ('global','*') in neg) or
            any(('source',source) in neg for source in active_sources)):
            continue
        if k['id'] in pinned or ('knowledge',kid) in grant['allow']:
            result.append(k)
            continue
        if twin['knowledge_mode']!='dynamic':
            continue
        pos=grant['allow']
        if (project and project in pos or
            k['scope']=='global' and ('global','*') in pos or
            active_sources and all(('source',source) in pos for source in active_sources)):
            result.append(k)
    return result


def grant_options(con):
    projects = [dict(row) for row in con.execute("""SELECT k.project_key,MIN(k.project) name,
        COUNT(*) knowledge_count FROM knowledge k
        WHERE k.scope='project' AND k.project_key!='' AND k.status!='archived'
        GROUP BY k.project_key ORDER BY name""")]
    sources = [dict(row) for row in con.execute("""SELECT id,name,kind,enabled,allow_ai,allow_share
        FROM sources ORDER BY name,id""")]
    return {'projects':projects,'sources':sources}


def replace_grants(con, twin_id: int, grants: list[dict]) -> None:
    """Called inside the caller's transaction after validation."""
    con.execute("DELETE FROM twin_grants WHERE twin_id=?",(twin_id,))
    con.executemany("""INSERT INTO twin_grants(twin_id,subject_type,subject_key,effect)
        VALUES(?,?,?,?)""",[(twin_id,g['subject_type'],g['subject_key'],g['effect'])
                             for g in grants])
