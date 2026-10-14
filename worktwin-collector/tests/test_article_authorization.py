import re
from fastapi.testclient import TestClient
from worktwin.api import create_app
from worktwin.db import Database
from worktwin.article_sources import source_passages, matching_passages
from worktwin.twin_access import effective_notes
from worktwin.answers import answer_from_knowledge
from tests.test_project_autonomy import create_source


def test_article_grant_carries_current_source_passages_but_material_does_not(tmp_path):
    db=Database(tmp_path/'articles.sqlite')
    quote='审批结果按流程节点传递。'
    with db.connect() as con:
        sid,did=create_source(con,'业务资料','流程.md',quote+'\n\n传递时必须携带活动实例编号，便于幂等处理。\n\n'+'无关内容。'*1000+'\n\n幂等重试的间隔为五秒。')
        article=con.execute('''INSERT INTO knowledge(kind,title,body,status,scope,project_key,project,quality)
            VALUES('process','流程协作说明','业务输出由流程节点传递。','confirmed','project','business','协作产品','useful')''').lastrowid
        material=con.execute('''INSERT INTO knowledge(kind,title,body,status,scope,project_key,quality,organization)
            VALUES('fact','零散观察','这一点还没有整理成文章。','confirmed','project','business','useful','material')''').lastrowid
        con.execute('INSERT INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)',(article,did,quote))
        twin=con.execute("INSERT INTO twins(name,knowledge_mode) VALUES('流程分身','manual')").lastrowid
        con.executemany('INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',[(twin,article),(twin,material)])
        rows=effective_notes(con,twin)
        assert [r['id'] for r in rows]==[article]
        passages=source_passages(con,article)
        assert matching_passages(passages,'活动实例编号')[0]['document_id']==did
        assert '无关内容。'*100 not in passages[0]['text']
        assert '五秒' in source_passages(con,article,query='幂等重试间隔')[0]['text']
        rows[0]['source_passages']=passages
        class Model:
            def chat(self,messages,max_tokens):
                assert '活动实例编号' in messages[-1]['content']
                return f'传递时携带活动实例编号。[K{article}]'
        result=answer_from_knowledge(Model(),'幂等处理使用哪个活动实例编号？',rows)
        assert result['context_count']==1
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(sid,))
        assert effective_notes(con,twin)==[]
        assert source_passages(con,article)==[]


def test_api_rejects_new_material_grants(tmp_path):
    app=create_app(tmp_path/'api.sqlite',start_worker=False)
    with TestClient(app) as client:
        auth={'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)}
        kid=client.post('/api/knowledge',headers=auth,json={'title':'材料','body':'尚未组织成文章的观察。','status':'confirmed'}).json()['id']
        with app.state.db.connect() as con:
            con.execute("UPDATE knowledge SET organization='material' WHERE id=?",(kid,))
        tid=client.post('/api/twins',headers=auth,json={'name':'文章助手'}).json()['id']
        assert client.put(f'/api/twins/{tid}/knowledge',headers=auth,json={'knowledge_ids':[kid]}).status_code==400
        assert client.put(f'/api/twins/{tid}/policy',headers=auth,json={'mode':'dynamic','grants':[
            {'subject_type':'knowledge','subject_key':str(kid),'effect':'allow'}]}).status_code==400
