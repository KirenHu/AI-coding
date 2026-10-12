"""Incremental authorship receipts, transient retries and live source revisions."""
import hashlib
import json

from worktwin.article_maintenance import ArticleMaintainer
from worktwin.db import Database
from worktwin.knowledge import store_candidates
from worktwin.reconcile import review_flags
from worktwin.twin_access import effective_notes
from tests.test_article_maintenance import material


class TopicWriter:
    configured=True

    def __init__(self, fail_once=False):
        self.requests=[]
        self.fail_once=fail_once

    def chat(self,messages,max_tokens):
        request=json.loads(messages[-1]['content'])
        self.requests.append(request)
        if 'materials' in request:
            groups={}
            for item in request['materials']:
                group=groups.setdefault(item['topic'],{'topic':item['topic'],
                    'material_ids':[],'article_ids':[a['id'] for a in request['articles']
                                                   if a['topic']==item['topic']]})
                group['material_ids'].append(item['id'])
            return json.dumps({'items':list(groups.values())},ensure_ascii=False)
        if self.fail_once:
            self.fail_once=False
            raise RuntimeError('provider-error-with-private-token-do-not-store')
        body='\n\n'.join(dict.fromkeys(e['context'] for m in request['source_materials']
                                      for e in m['sources']))
        return json.dumps({'items':[{'complete':True,'unchanged':False,'kind':'process',
            'title':request['topic']+'说明','topic':request['topic'],
            'scope_detail':'WorkTwin项目','article_summary':'介绍'+request['topic']+'的对象、规则及适用条件。',
            'body':'## 工作背景与规则\n\n'+body,'change_summary':'更新具体办理规则及其依据'}]},ensure_ascii=False)


def add_material(con,name,quote,topic,context=''):
    did,kid=material(con,name,quote,context=context)
    con.execute('UPDATE knowledge SET topic=? WHERE id=?',(topic,kid))
    return did,kid


def test_one_off_rebuild_reads_only_its_document_set(tmp_path):
    db=Database(tmp_path/'limited-rebuild.sqlite')
    with db.connect() as con:
        selected_doc,selected=add_material(con,'指定资料','任务必须明确完成条件。','任务办理机制')
        _,outside=add_material(con,'范围之外','只有此范围之外资料记载的规则。','任务办理机制')
    writer=TopicWriter()
    assert ArticleMaintainer(db,writer).process_next(authorized_documents={selected_doc})['articles']==1
    assert [m['id'] for m in writer.requests[0]['materials']]==[selected]
    assert all(e['document_id']==selected_doc for m in writer.requests[1]['source_materials'] for e in m['sources'])
    with db.connect() as con:
        assert con.execute('SELECT 1 FROM article_material_receipts WHERE material_id=?',(outside,)).fetchone() is None


def test_new_material_rewrites_only_affected_article_and_reuses_related_context(tmp_path):
    db=Database(tmp_path/'incremental.sqlite')
    with db.connect() as con:
        task_doc,task=add_material(con,'任务定义','任务由负责人开始办理。','任务办理机制')
        model_doc,model=add_material(con,'模型配置','模型地址由管理员配置。','模型配置方法')
    writer=TopicWriter();worker=ArticleMaintainer(db,writer)
    assert worker.process_next()['articles']==2
    with db.connect() as con:
        articles={r['topic']:dict(r) for r in con.execute("SELECT * FROM knowledge WHERE organization='article'")}
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==2
    assert worker.process_next()['state']=='idle'
    writer.requests.clear()
    with db.connect() as con:
        completion_doc,completion=add_material(con,'任务结束','任务完成必须通过验收。','任务办理机制')
    assert worker.process_next()['articles']==1
    assert [m['id'] for m in writer.requests[0]['materials']]==[completion]
    composed=writer.requests[1]
    assert {m['id'] for m in composed['source_materials']}=={task,completion}
    assert {e['document_id'] for m in composed['source_materials'] for e in m['sources']}=={task_doc,completion_doc}
    assert all(e['document_id']!=model_doc for m in composed['source_materials'] for e in m['sources'])
    with db.connect() as con:
        changed=con.execute('SELECT * FROM knowledge WHERE id=?',(articles['任务办理机制']['id'],)).fetchone()
        untouched=con.execute('SELECT * FROM knowledge WHERE id=?',(articles['模型配置方法']['id'],)).fetchone()
        assert changed['version']==2 and '通过验收' in changed['body']
        assert dict(untouched)==articles['模型配置方法']
        assert con.execute('SELECT count(*) FROM knowledge_history WHERE knowledge_id=?',(untouched['id'],)).fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==3
    assert worker.process_next()['state']=='idle'


def test_transient_article_error_retries_after_backoff_without_receipting_failure(tmp_path):
    db=Database(tmp_path/'retry.sqlite')
    with db.connect() as con: add_material(con,'验收规则','任务完成必须通过验收。','任务办理机制')
    writer=TopicWriter(fail_once=True);worker=ArticleMaintainer(db,writer)
    result=worker.process_next()
    assert result=={'state':'error','error':'RuntimeError'}
    with db.connect() as con:
        run=con.execute('SELECT * FROM article_maintenance_runs').fetchone()
        assert run['attempts']==1 and run['next_run_at']
        assert 'private-token' not in run['error']
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==0
        assert con.execute("SELECT count(*) FROM knowledge WHERE organization='article'").fetchone()[0]==0
    requests=len(writer.requests)
    assert worker.process_next()['state']=='idle' and len(writer.requests)==requests
    with db.connect() as con:
        con.execute("UPDATE article_maintenance_runs SET next_run_at=datetime('now','-1 second')")
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        assert con.execute('SELECT attempts FROM article_maintenance_runs').fetchone()[0]==2
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==1
    requests=len(writer.requests)
    assert worker.process_next()['state']=='idle' and len(writer.requests)==requests


def test_retained_quote_with_changed_source_context_rewrites_current_article(tmp_path):
    db=Database(tmp_path/'context-revision.sqlite')
    quote='任务完成必须通过验收。'
    with db.connect() as con:
        did,mid=add_material(con,'验收说明',quote,'任务办理机制',context='背景：由业务负责人验收。\n')
    writer=TopicWriter();worker=ArticleMaintainer(db,writer)
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        before=dict(con.execute("SELECT * FROM knowledge WHERE organization='article'").fetchone())
        old_receipt=con.execute('SELECT input_signature FROM article_material_receipts WHERE material_id=?',(mid,)).fetchone()[0]
        text='背景：改为指定审核人验收。\n'+quote
        con.execute('UPDATE documents SET content=?,sha256=?,mtime_ns=2 WHERE id=?',
                    (text,hashlib.sha256(text.encode()).hexdigest(),did))
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        after=con.execute('SELECT * FROM knowledge WHERE id=?',(before['id'],)).fetchone()
        assert after['version']==before['version']+1
        assert '指定审核人' in after['body'] and '业务负责人' not in after['body']
        assert con.execute('SELECT body FROM knowledge_history WHERE knowledge_id=?',(before['id'],)).fetchone()[0]==before['body']
        assert con.execute('SELECT input_signature FROM article_material_receipts WHERE material_id=?',(mid,)).fetchone()[0]!=old_receipt


def test_source_replacement_supersedes_old_citations_and_keeps_article_usable(tmp_path):
    db=Database(tmp_path/'source-replacement.sqlite')
    old='任务完成由原业务负责人验收。'
    new='任务完成改为指定审核人验收。'
    with db.connect() as con: did,mid=add_material(con,'旧验收',old,'任务办理机制')
    writer=TopicWriter();worker=ArticleMaintainer(db,writer)
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        article=dict(con.execute("SELECT * FROM knowledge WHERE organization='article'").fetchone())
        con.execute('UPDATE documents SET content=?,sha256=? WHERE id=?',
                    (new,hashlib.sha256(new.encode()).hexdigest(),did))
        con.execute('UPDATE knowledge_evidence SET is_current=0 WHERE document_id=?',(did,))
        con.execute("UPDATE knowledge SET status='archived' WHERE id=?",(mid,))
        review_flags(con)
        store_candidates(con,did,[],[{'kind':'process','title':'新验收','body':new,'quote':new,
            'topic':'任务办理机制','scope_detail':'WorkTwin中的工作任务','quality':'useful',
            'attribution':'document','outcome':'none','extraction_version':1}],created_by='enterprise_ai')
        tid=con.execute("INSERT INTO twins(name) VALUES('任务助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',(tid,article['id']))
    assert worker.process_next()['articles']==1
    with db.connect() as con:
        review_flags(con)
        current=con.execute('SELECT * FROM knowledge WHERE id=?',(article['id'],)).fetchone()
        assert current['version']==article['version']+1 and current['needs_review']==0
        assert new in current['body'] and old not in current['body']
        previous=con.execute('SELECT is_current,superseded FROM knowledge_evidence WHERE knowledge_id=? AND quote=?',
                             (article['id'],old)).fetchone()
        assert tuple(previous)==(0,1)
        assert [k['id'] for k in effective_notes(con,tid)]==[article['id']]


def test_unrelated_project_background_is_used_for_routing_not_article_authorship(tmp_path):
    db=Database(tmp_path/'project-privacy.sqlite')
    marker='INTERNAL-PRICING-PRIVATE-7742'
    with db.connect() as con:
        private_doc,private=add_material(con,'内部定价',marker+'只属于内部定价事项。','内部定价机制')
        con.execute("UPDATE knowledge SET organization='article',status='confirmed',article_summary='内部定价机制说明' WHERE id=?",(private,))
        public_doc,public=add_material(con,'公开办理','公开任务由负责人提交验收。','公开办理机制')
        con.execute('UPDATE sources SET allow_share=1 WHERE id=(SELECT source_id FROM documents WHERE id=?)',(public_doc,))
    writer=TopicWriter()
    assert ArticleMaintainer(db,writer).process_next()['articles']==1
    assert marker in json.dumps(writer.requests[0]['project'],ensure_ascii=False)
    composed=writer.requests[1]
    assert marker not in json.dumps(composed,ensure_ascii=False)
    assert {e['document_id'] for m in composed['source_materials'] for e in m['sources']}=={public_doc}
    with db.connect() as con:
        authored=con.execute("SELECT * FROM knowledge WHERE organization='article' AND topic='公开办理机制'").fetchone()
        assert marker not in authored['body']
        tid=con.execute("INSERT INTO twins(name) VALUES('公开事项助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',(tid,authored['id']))
        assert [k['id'] for k in effective_notes(con,tid,sharing=True)]==[authored['id']]


def test_shared_material_is_not_receipted_until_every_selected_topic_is_complete(tmp_path):
    db=Database(tmp_path/'shared-material.sqlite')
    common='任务是一项具有明确结果与验收条件的工作事项。'
    with db.connect() as con: _,shared=add_material(con,'共享定义',common,'任务办理机制')
    class PartialWriter(TopicWriter):
        exception_complete=False

        def chat(self,messages,max_tokens):
            request=json.loads(messages[-1]['content'])
            if 'materials' in request:
                self.requests.append(request)
                ids=[m['id'] for m in request['materials']]
                groups=[{'topic':'异常处理机制','material_ids':ids,
                    'article_ids':[a['id'] for a in request['articles'] if a['topic']=='异常处理机制']}]
                if shared in ids:
                    groups.insert(0,{'topic':'任务办理机制','material_ids':[shared],
                        'article_ids':[a['id'] for a in request['articles'] if a['topic']=='任务办理机制']})
                return json.dumps({'items':groups},ensure_ascii=False)
            if request['topic']=='异常处理机制' and not self.exception_complete:
                self.requests.append(request)
                return '{"items":[{"complete":false}]}'
            return super().chat(messages,max_tokens)
    writer=PartialWriter();worker=ArticleMaintainer(db,writer)
    result=worker.process_next()
    assert result['articles']==1 and result['deferred']==1
    with db.connect() as con:
        assert con.execute('SELECT state FROM article_maintenance_runs').fetchone()[0]=='waiting'
        assert con.execute('SELECT count(*) FROM article_material_receipts WHERE material_id=?',(shared,)).fetchone()[0]==0
        _,background=add_material(con,'异常规则','异常时由审核人退回至办理中状态。','异常处理机制')
    writer.exception_complete=True
    writer.requests.clear()
    assert worker.process_next()['articles']>=1
    assert {m['id'] for m in writer.requests[0]['materials']}=={shared,background}
    with db.connect() as con:
        exception=con.execute("SELECT * FROM knowledge WHERE organization='article' AND topic='异常处理机制'").fetchone()
        assert common in exception['body'] and '退回至办理中' in exception['body']
        assert con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]==2
    assert worker.process_next()['state']=='idle'
