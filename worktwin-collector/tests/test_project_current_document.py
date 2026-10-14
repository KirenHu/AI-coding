"""Reader-facing current knowledge excludes superseded discussions and small materials."""
import hashlib
import json

from worktwin.article_maintenance import ArticleMaintainer
from worktwin.article_sources import source_passages
from worktwin.db import Database
from worktwin.search import search
from worktwin.twin_access import effective_notes
from tests.test_article_maintenance import material,article
from tests.support import editor_review


def test_meetings_and_later_rule_become_one_current_document_with_separate_history(tmp_path):
    db=Database(tmp_path/'current.sqlite')
    old='审批必须经过旧网关再交给处理人。'
    current='审批由业务工作台统一执行。'
    with db.connect() as con:
        old_doc,a=material(con,'旧会议',old);article(con,a,old)
        con.execute('INSERT INTO chunks(document_id,ordinal,start_offset,end_offset,text) VALUES(?,0,0,?,?)',(old_doc,len(old),old))
        _,b=material(con,'会议中的另一小点','任务按角色分配。');article(con,b,'任务按角色分配。')
        current_doc,m=material(con,'最新确认',current)
        twin=con.execute("INSERT INTO twins(name) VALUES('工作助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge VALUES(?,?)',(twin,a))
    class Editor:
        configured=True
        def chat(self,messages,max_tokens):
            req=json.loads(messages[-1]['content'])
            if req.get('stage')=='article_review': return editor_review(req)
            if req.get('stage')=='article_routing':
                return json.dumps({'items':[{'topic':'任务机制','material_ids':[m['id'] for m in req['materials']],
                    'article_ids':[a['id'] for a in req['articles']]}]})
            request=json.loads(messages[-1]['content'])
            assert any(d['id']==current_doc for d in request['source_documents'])
            return json.dumps({'items':[{'complete':True,'kind':'process','title':'当前审批与任务机制',
                'article_summary':'业务工作台统一执行审批，并按角色分配任务。',
                'body':'## 审批与任务\n\n'+current+'任务按角色分配。',
                'change_summary':'校正审批执行方式','source_refs':[{'source_id':f'D{current_doc}:0'}]}]},ensure_ascii=False)
    assert ArticleMaintainer(db,Editor()).process_next()['articles']==1
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM knowledge WHERE organization='article' AND status!='archived'").fetchone()[0]==1
        assert con.execute('SELECT status FROM knowledge WHERE id=?',(b,)).fetchone()[0]=='archived'
        assert con.execute('SELECT body FROM knowledge_history WHERE knowledge_id=?',(a,)).fetchone()[0]==old
        assert [r['id'] for r in effective_notes(con,twin)]==[a]
        current_sources=source_passages(con,a,query='审批')
        assert all(r['document_id']==current_doc for r in current_sources)
        assert old not in json.dumps(current_sources,ensure_ascii=False)
        history=source_passages(con,a,query='旧网关',include_history=True)
        assert any(r['document_id']==old_doc and r['usage']=='original_history' for r in history)
        assert search(con,'审批')['documents']==[]
        assert [r['id'] for r in search(con,'审批')['knowledge']]==[a]
        assert search(con,'旧网关')['knowledge']==[]
        assert search(con,'旧网关',include_sources=True)['documents']


def test_changed_source_waits_for_correction_instead_of_answering_old_current_text(tmp_path):
    db=Database(tmp_path/'revision.sqlite')
    text='任务由业务负责人验收。'
    with db.connect() as con: did,m=material(con,'验收规则',text)
    class Editor:
        configured=True
        def chat(self,messages,max_tokens):
            req=json.loads(messages[-1]['content'])
            if req.get('stage')=='article_review': return editor_review(req)
            if req.get('stage')=='article_routing':
                return json.dumps({'items':[{'topic':'任务机制','material_ids':[m['id'] for m in req['materials']],
                    'article_ids':[a['id'] for a in req['articles']]}]})
            return json.dumps({'items':[{'complete':True,'kind':'process','title':'任务验收',
                'article_summary':text,'body':text,'source_refs':[{'source_id':f'D{did}:0'}]}]},ensure_ascii=False)
    ArticleMaintainer(db,Editor()).process_next()
    with db.connect() as con:
        kid=con.execute('SELECT knowledge_id FROM project_documents').fetchone()[0]
        twin=con.execute("INSERT INTO twins(name) VALUES('助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge VALUES(?,?)',(twin,kid))
        revised=text+'\n后续已确认，改由指定审核人验收。'
        con.execute('UPDATE documents SET content=?,sha256=? WHERE id=?',
            (revised,hashlib.sha256(revised.encode()).hexdigest(),did))
        assert effective_notes(con,twin)==[]
        assert source_passages(con,kid)==[]
        assert search(con,'验收')['knowledge']==[]


def test_open_question_does_not_hide_the_rest_of_current_project_knowledge(tmp_path):
    db=Database(tmp_path/'open-question.sqlite')
    known='任务由业务负责人验收，具体负载统计口径尚未确定。'
    with db.connect() as con: did,m=material(con,'验收与分配',known)
    class Editor:
        configured=True
        def chat(self,messages,max_tokens):
            req=json.loads(messages[-1]['content'])
            if req.get('stage')=='article_review': return editor_review(req)
            if req.get('stage')=='article_routing':
                return json.dumps({'items':[{'topic':'任务机制','material_ids':[m['id'] for m in req['materials']],
                    'article_ids':[a['id'] for a in req['articles']]}]})
            return json.dumps({'items':[{'complete':True,'unresolved_conflict':True,
                'kind':'process','title':'当前任务规则','article_summary':'业务负责人验收任务。',
                'body':known,'source_refs':[{'source_id':f'D{did}:0'}]}]},ensure_ascii=False)
    assert ArticleMaintainer(db,Editor()).process_next()['articles']==1
    with db.connect() as con:
        assert search(con,'负责人')['knowledge']
        assert con.execute("SELECT status,needs_review FROM knowledge WHERE organization='article'").fetchone()[0]=='confirmed'
