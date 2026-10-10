"""Private knowledge evolves automatically; human corrections stay protected."""

from worktwin.automation import activate_new
from worktwin.autonomy import apply_safe_replacements
from worktwin.knowledge_policy import READY_SQL
from worktwin.reconcile import store_proposals
from tests.test_automation import setup, Model


def _create_successor(con, doc_id, old_item):
    con.execute("""UPDATE knowledge_evidence SET occurred_at='2026-10-08 09:00:00'
        WHERE knowledge_id IN (SELECT id FROM knowledge)""")
    successor = dict(old_item,
        quote='新增说明：当前知识允许分配给数字分身。',
        body='当前只保留已生效的结论，旧内容在历史中查阅。',
        occurred_at='2026-10-09T10:00:00Z',
        attribution='user', requires_review=True)
    target = con.execute("SELECT id FROM knowledge").fetchone()[0]
    proposal = {0: {'target_id': target, 'action':'replace',
                    'title':'当前知识的生效规则',
                    'body':successor['body'],
                    'reason':'后续有直接来源的明确新版本',
                    'changes_existing_conclusion':True}}
    assert store_proposals(con,doc_id,'source-sha',[successor],proposal) == 1
    return target


def test_clear_successor_applies_without_acceptance_receipt(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        activate_new(con,did,[item],Model())
        kid=_create_successor(con,did,item)
        assert apply_safe_replacements(con,did)==1
        note=con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
        assert note['body']=='当前只保留已生效的结论，旧内容在历史中查阅。'
        assert note['created_by']=='enterprise_ai'
        assert note['status']=='confirmed' and note['needs_review']==0
        assert con.execute('SELECT count(*) FROM knowledge_history WHERE knowledge_id=?',(kid,)).fetchone()[0] >= 1
        assert len(con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL))==1
        assert apply_safe_replacements(con,did)==0


def test_owner_edited_note_is_never_automatically_replaced(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        activate_new(con,did,[item],Model())
        kid=_create_successor(con,did,item)
        con.execute("UPDATE knowledge SET created_by='human' WHERE id=?",(kid,))
        assert apply_safe_replacements(con,did)==0
        assert con.execute('SELECT count(*) FROM knowledge_proposals WHERE status=?',('pending',)).fetchone()[0]==1


def test_revoked_ai_source_cannot_auto_update(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        activate_new(con,did,[item],Model())
        kid=_create_successor(con,did,item)
        con.execute('UPDATE sources SET allow_ai=0')
        assert apply_safe_replacements(con,did)==0
        assert con.execute('SELECT created_by FROM knowledge WHERE id=?',(kid,)).fetchone()[0]=='enterprise_ai'


def test_updated_file_revises_original_note_without_reconfirmation(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        assert activate_new(con,did,[item],Model())==1
        target=con.execute('SELECT id FROM knowledge').fetchone()[0]
        quote='修改后的资料明确提出知识仅保留最新结论。'
        con.execute("UPDATE documents SET content=?,sha256=? WHERE id=?",(quote,'edited-file-sha',did))
        con.execute("UPDATE knowledge_evidence SET is_current=0 WHERE knowledge_id=?",(target,))
        plan={0:{'target_id':target,'action':'replace','title':item['title'],
            'body':'知识只保留最新结论，旧正文仅出现在版本历史。',
            'reason':'来自原文件修改后的新版本','changes_existing_conclusion':True}}
        item2=dict(item,quote=quote,occurred_at='',body=plan[0]['body'])
        from worktwin.reconcile import existing_for_project
        assert target in {k['id'] for k in existing_for_project(con,did)}
        assert store_proposals(con,did,'edited-file-sha',[item2],plan)==1
        assert con.execute("SELECT action FROM knowledge_proposals ORDER BY id DESC LIMIT 1").fetchone()[0]=='replace'
        assert apply_safe_replacements(con,did)==1
        note=con.execute('SELECT * FROM knowledge WHERE id=?',(target,)).fetchone()
        assert note['body']==plan[0]['body'] and note['needs_review']==0
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0]==1
