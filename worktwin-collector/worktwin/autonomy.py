"""Automatically accept clearly newer, grounded conclusions in the private library.

This does not assign knowledge to any twin or expand remote sharing permissions.
An AI change cannot silently overwrite a knowledge article edited by its owner.
"""

from __future__ import annotations

import json
from .scope import source_time, topic_key
from .reconcile import existing_for_project, resolve_proposal


def apply_safe_replacements(con, document_id: int) -> int:
    doc = con.execute("""SELECT d.* FROM documents d
        JOIN sources s ON s.id=d.source_id
        WHERE d.id=? AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1""",
        (document_id,)).fetchone()
    if not doc or doc['scope'] not in ('session','project','global'):
        return 0
    permitted = {r['id'] for r in existing_for_project(con, document_id)}
    applied = 0
    pending = con.execute("""SELECT * FROM knowledge_proposals
        WHERE document_id=? AND status='pending' AND origin='consolidation'
          AND action='replace' ORDER BY id""", (document_id,)).fetchall()
    for proposal in pending:
        note = con.execute("SELECT * FROM knowledge WHERE id=?",
                           (proposal['target_id'],)).fetchone()
        if not note or proposal['target_id'] not in permitted:
            continue
        # Manual corrections and ambiguous boundaries remain owner-controlled.
        if (note['status'] != 'confirmed' or note['review_hold']
            or note['created_by'] != 'enterprise_ai' or note['source_bound'] != 1
            or note['quality'] != 'useful' or note['project_key'] != doc['project_key']
            or note['scope'] != doc['scope'] or note['version'] != proposal['target_version']):
            continue
        if con.execute("""SELECT COUNT(*) FROM knowledge_proposals
            WHERE target_id=? AND status='pending'""",(note['id'],)).fetchone()[0] != 1:
            continue
        if (proposal['content_sha'] != doc['sha256']
                or proposal['quote'] not in doc['content']
                or proposal['body'].strip() == note['body'].strip()):
            continue
        metadata = json.loads(proposal['evidence_json'])
        if (metadata.get('attribution') not in ('user','document')
                or metadata.get('outcome') == 'reported'
                or metadata.get('changes_existing_conclusion') is not True
                or topic_key(metadata.get('scope_detail','')) != topic_key(note['scope_detail'])):
            continue
        newer = source_time(proposal['occurred_at'] or '')
        previous = source_time(con.execute("""SELECT MAX(occurred_at) FROM knowledge_evidence
            WHERE knowledge_id=? AND is_current=1 AND superseded=0""",
            (note['id'],)).fetchone()[0] or '')
        # A timestamp-free rewrite is not enough evidence of which conclusion
        # is newer. We can still automatically activate its new independent note.
        if not newer or not previous or newer <= previous:
            continue
        try:
            resolve_proposal(con, proposal['id'], accept=True, actor='enterprise_ai')
        except (ValueError, LookupError):
            continue
        applied += 1
    return applied
