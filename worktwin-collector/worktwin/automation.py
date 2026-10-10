"""Automatic activation requires a versioned real-data acceptance receipt.

New material can become current; changes to a conclusion remain proposals.
An accepted receipt is tied to the model and policy, never a UI checkbox.
"""
from __future__ import annotations

import hashlib
import json

from .scope import topic_key

POLICY_VERSION = '2026-10-09-scoped-additions-v1'
REQUIRED_CHECKS = {'new', 'addition', 'replacement', 'conflict', 'unknown_project', 'scope_isolation', 'grounding', 'noise'}


def model_signature(client):
    runtime = client
    client = getattr(client, 'client', client)
    model = getattr(client, 'model', '')
    if not model and hasattr(runtime, 'db'):
        model = runtime.db.setting('enterprise_model_name')
    identity = [getattr(runtime,'mode','personal'),getattr(client,'url',''),model]
    if not all(identity):
        return ''
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def acceptance_status(con, client):
    signature = model_signature(client)
    row = con.execute('''SELECT * FROM knowledge_acceptance WHERE model_signature=?
        AND policy_version=?''',(signature,POLICY_VERSION)).fetchone()
    if not row:
        return {'ready':False,'reason':'本模型与当前整理规则尚未通过真实资料验收'}
    report = json.loads(row['report_json'])
    valid = report.get('real_data') is True and bool(report.get('dataset_sha256')) and REQUIRED_CHECKS <= set(report.get('checks',{}))
    valid = valid and all(report['checks'][key] is True for key in REQUIRED_CHECKS)
    return {'ready':bool(valid),'accepted_at':row['accepted_at'],'reason':'' if valid else '验收记录不完整'}


def record_acceptance(con, client, report):
    signature = model_signature(client)
    if not signature or report.get('real_data') is not True or not report.get('dataset_sha256') or not all(report.get('checks',{}).get(key) is True for key in REQUIRED_CHECKS):
        raise ValueError('实际模型、真实资料及必要验收项均通过后，才能启用自动生效')
    con.execute('''INSERT INTO knowledge_acceptance(model_signature,policy_version,report_json)
        VALUES(?,?,?) ON CONFLICT(model_signature) DO UPDATE SET policy_version=excluded.policy_version,
        report_json=excluded.report_json,accepted_at=datetime('now')''',
        (signature,POLICY_VERSION,json.dumps(report,ensure_ascii=False)))


def activate_new(con, document_id, items, client):
    doc = con.execute("""SELECT d.* FROM documents d JOIN sources s ON s.id=d.source_id
        WHERE d.id=? AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1""",
        (document_id,)).fetchone()
    # Knowledge scoped to this session is useful without a user-created
    # business-project identity. The source still needs explicit AI consent.
    if not doc or doc['scope'] not in ('session','project','global'):
        return 0
    activated = 0
    for item in items:
        if item.get('quality')!='useful' or item.get('attribution') not in ('user','document') or item.get('outcome')=='reported':
            continue
        candidates=con.execute('''SELECT * FROM knowledge WHERE project_key=? AND scope=?
            AND status!='archived' ''',(doc['project_key'],doc['scope'])).fetchall()
        matches=[k for k in candidates if topic_key(k['topic'])==topic_key(item.get('topic',''))]
        # Same-topic duplicates and uncertain relationships need review.
        if len(matches)!=1:
            continue
        k=matches[0]
        if k['status']!='draft' or k['review_hold'] or k['needs_review'] or k['extraction_version']!=1:
            continue
        if con.execute("SELECT 1 FROM knowledge_proposals WHERE target_id=? AND status='pending'",(k['id'],)).fetchone():
            continue
        from .scope import snapshot_history
        snapshot_history(con,k)
        con.execute("UPDATE knowledge SET status='confirmed',version=version+1,updated_at=datetime('now') WHERE id=?",(k['id'],))
        activated+=1
    return activated


def apply_additions(con, document_id, client):
    doc=con.execute("""SELECT d.* FROM documents d JOIN sources s ON s.id=d.source_id
        WHERE d.id=? AND d.deleted=0 AND s.enabled=1 AND s.allow_ai=1""",
        (document_id,)).fetchone()
    if not doc or doc['scope'] not in ('session','project','global'):
        return 0
    from .reconcile import resolve_proposal
    applied=0
    for p in con.execute("SELECT * FROM knowledge_proposals WHERE document_id=? AND status='pending' AND action='enrich' AND origin='consolidation'",(document_id,)).fetchall():
        k=con.execute('SELECT * FROM knowledge WHERE id=?',(p['target_id'],)).fetchone()
        evidence=json.loads(p['evidence_json'])
        pending=con.execute("SELECT count(*) FROM knowledge_proposals WHERE target_id=? AND status='pending'",(k['id'],)).fetchone()[0]
        if k['status']!='confirmed' or pending!=1 or evidence.get('requires_review',True) is not False or evidence.get('changes_existing_conclusion',True) is not False:
            continue
        # Enrichment must preserve the exact current body, not a model rewrite.
        if not p['body'].startswith(k['body'].rstrip()+'\n\n'):
            continue
        resolve_proposal(con,p['id'],accept=True)
        con.execute("UPDATE knowledge SET created_by='enterprise_ai' WHERE id=?",(k['id'],))
        applied+=1
    return applied
