"""Behavioral checks for directory routing, staged review and incremental queues.

Model doubles verify orchestration, not the model's semantic accuracy.
"""
import json

from worktwin.article_maintenance import ArticleMaintainer
from worktwin.db import Database
from tests.test_article_maintenance import Writer, material, article
from tests.support import current_refs


def test_distinct_reader_subjects_in_one_project_stay_separate_and_update_in_place(tmp_path):
    db=Database(tmp_path/'subjects.sqlite')
    with db.connect() as con:
        _,a=material(con,'退款流程','申请退款后由客服审核，原路退回。')
        _,b=material(con,'服务部署','服务使用容器部署，先备份再升级。')
    class Editor(Writer):
        def chat(self,messages,max_tokens):
            r=json.loads(messages[-1]['content'])
            if r.get('stage')=='article_routing':
                return json.dumps({'items':[{'topic':m['title'],'material_ids':[m['id']],
                    'article_ids':[p['id'] for p in r['articles'] if p['topic']==m['title']]} for m in r['materials']]})
            if r.get('stage')=='article_review': return super().chat(messages,max_tokens)
            return json.dumps({'items':[{'complete':True,'kind':'process','title':r['topic'],'topic':r['topic'],
                'article_summary':r['topic']+'的办理方法','body':'\n'.join(m['body'] for m in r['source_materials']),
                'source_refs':current_refs(r)}]})
    worker=ArticleMaintainer(db,Editor())
    assert worker.process_next()['articles']==2
    with db.connect() as con:
        rows={r['topic']:dict(r) for r in con.execute("SELECT * FROM knowledge WHERE organization='article'")}
        assert len(list(con.execute('SELECT * FROM project_documents')))==2
        _,new=material(con,'退款补充','已确认：超时退款由客服主管处理。')
        con.execute("UPDATE knowledge SET title='退款流程' WHERE id=?",(new,))
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        after={r['topic']:dict(r) for r in con.execute("SELECT * FROM knowledge WHERE organization='article'")}
    assert after['退款流程']['id']==rows['退款流程']['id']
    assert '主管' in after['退款流程']['body'] and '原路退回' in after['退款流程']['body']
    assert after['服务部署']==rows['服务部署']


def test_rejected_draft_does_not_change_current_article_or_consume_material(tmp_path):
    db=Database(tmp_path/'rejected.sqlite')
    with db.connect() as con:
        _,kid=material(con,'现行方案','现行规则需要两名审批人。');article(con,kid,'需要两名审批人。')
        material(con,'新讨论','有人建议一名审批人，尚未确认。')
    class Reject(Writer):
        def chat(self,messages,max_tokens):
            if json.loads(messages[-1]['content']).get('stage')=='article_review':
                return json.dumps({'items':[{'approved':False,'issues':['把建议写成正式规则'],'resolutions':[]}]})
            return super().chat(messages,max_tokens)
    result=ArticleMaintainer(db,Reject()).process_next()
    assert result['state']=='error' and result['error']=='ValueError: article review rejected'
    with db.connect() as con:
        assert con.execute('SELECT body FROM knowledge WHERE id=?',(kid,)).fetchone()[0]=='需要两名审批人。'
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM article_editor_reviews').fetchone()[0]==2
        assert con.execute('SELECT next_run_at FROM article_maintenance_runs').fetchone()[0]


def test_reviewer_can_request_one_revision_before_publish(tmp_path):
    db=Database(tmp_path/'revise.sqlite')
    with db.connect() as con: material(con,'审批规则','正式规则需要两名审批人。')
    class Revise(Writer):
        reviews=0
        def chat(self,messages,max_tokens):
            r=json.loads(messages[-1]['content'])
            if r.get('stage')=='article_review':
                self.reviews+=1
                return json.dumps({'items':[{'approved':self.reviews==2,
                    'issues':['补充对象定义'] if self.reviews==1 else [],'resolutions':[]}]})
            if r.get('stage')!='article_routing' and self.reviews==1:
                assert '补充对象定义' in messages[0]['content']
            return super().chat(messages,max_tokens)
    writer=Revise()
    assert ArticleMaintainer(db,writer).process_next()['articles']==1 and writer.reviews==2
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge_history').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==1


def test_deferred_batch_does_not_starve_new_context(tmp_path):
    db=Database(tmp_path/'queue.sqlite')
    with db.connect() as con:
        for i in range(24): material(con,f'待补充{i}','按上面的规则处理。')
    class Deferred(Writer):
        seen=[]
        def chat(self,messages,max_tokens):
            r=json.loads(messages[-1]['content'])
            if r.get('stage')=='article_routing':
                self.seen += [m['title'] for m in r['materials']]
                return json.dumps({'items':[{'topic':'待补充','material_ids':[m['id'] for m in r['materials']],
                    'article_ids':[],'defer':True}]})
            raise AssertionError('No writer for deferred context')
    model=Deferred();worker=ArticleMaintainer(db,model)
    assert worker.process_next()['deferred']==24
    assert worker.process_next()['state']=='idle'
    with db.connect() as con: material(con,'新背景','这是给退款业务的操作规则。')
    assert worker.process_next()['deferred']==24
    assert '新背景' in model.seen
    assert worker.process_next()['deferred']==1
    assert worker.process_next()['state']=='idle'


def test_more_than_one_batch_is_eventually_consumed(tmp_path):
    db=Database(tmp_path/'batches.sqlite')
    with db.connect() as con:
        for i in range(26): material(con,f'规则{i}','退款审核应在两个工作日内完成。')
    worker=ArticleMaintainer(db,Writer())
    assert worker.process_next()['articles']==1
    assert worker.process_next()['articles']==1
    assert worker.process_next()['state']=='idle'
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==26
        assert con.execute("SELECT count(*) FROM knowledge WHERE organization='article'").fetchone()[0]==1


def test_pausing_collection_does_not_revoke_existing_ai_permission(tmp_path):
    db=Database(tmp_path/'stopped.sqlite')
    with db.connect() as con:
        did,kid=material(con,'停用来源','STOPPED-SOURCE-MARKER');article(con,kid,'STOPPED-ARTICLE-MARKER')
        con.execute('UPDATE sources SET enabled=0 WHERE id=(SELECT source_id FROM documents WHERE id=?)',(did,))
        material(con,'新来源','退款由客服审核。')
    writer=Writer()
    assert ArticleMaintainer(db,writer).process_next()['articles']==1
    assert 'STOPPED-SOURCE-MARKER' in json.dumps(writer.requests)


def test_invalid_route_never_merges_unlisted_article(tmp_path):
    db=Database(tmp_path/'invalid.sqlite')
    with db.connect() as con: _,mid=material(con,'来源','退款由客服审核。')
    writer=Writer(groups=[{'topic':'退款流程','material_ids':[mid],'article_ids':[9999]}])
    assert ArticleMaintainer(db,writer).process_next()['state']=='error'
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==0
