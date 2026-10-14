import json

from worktwin.article_maintenance import ArticleMaintainer
from worktwin.db import Database
from worktwin.knowledge import store_candidates
from worktwin.scope import snapshot_history
from worktwin.twin_access import effective_notes
from tests.support import current_refs, editor_review


def material(con, name, quote, *, project='project:worktwin', allow_ai=True, context=''):
    sid = con.execute('INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,?,?,?)',
        (name,'folder','/fixtures/'+name,int(allow_ai))).lastrowid
    did = con.execute('''INSERT INTO documents(source_id,path,relative_path,title,file_type,
        content,sha256,size_bytes,mtime_ns,project,project_key,scope,project_verified)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)''',(sid,'/fixtures/'+name+'/a.md','a.md',name,'.md',
        context+quote,name+'-sha',100,1,'WorkTwin',project,'project')).lastrowid
    item = {'kind':'process','title':name,'body':quote,'quote':quote,'topic':'任务办理机制',
        'scope_detail':'WorkTwin中的工作任务','quality':'useful','attribution':'document','outcome':'none',
        'value_reason':'供任务办理时查询','extraction_version':1}
    store_candidates(con,did,[],[item],created_by='enterprise_ai')
    kid = con.execute('SELECT max(id) FROM knowledge').fetchone()[0]
    return did,kid


def article(con, kid, body):
    con.execute("UPDATE knowledge SET organization='article',status='confirmed',body=?,article_summary='任务办理规则概要' WHERE id=?",(body,kid))


class Writer:
    configured=True
    def __init__(self, groups=None, output=None, hook=None):
        self.groups,self.output,self.hook=groups,output,hook
        self.requests=[]

    def chat(self,messages,max_tokens):
        self.requests.append(messages)
        request=json.loads(messages[-1]['content'])
        if request.get('stage')=='article_review': return editor_review(request)
        if 'materials' in request:
            groups = self.groups or [{'topic':'任务办理机制',
                'material_ids':[m['id'] for m in request['materials']],
                'article_ids':[a['id'] for a in request['articles']]}]
            return json.dumps({'items':groups},ensure_ascii=False)
        if self.hook:
            self.hook()
        result=self.output or {'complete':True,'kind':'process',
            'title':'WorkTwin任务办理机制','topic':'任务办理机制','scope_detail':'WorkTwin工作任务',
            'article_summary':'介绍WorkTwin任务从开始处理到完成的状态、结束条件与异常处理。',
            'body':'## 任务是什么\nWorkTwin工作任务是一次需要明确结果的工作事项。\n\n## 办理规则\n开始处理后进入进行中，验收完成后进入已完成。',
            'change_summary':'补充任务状态与完成条件'}
        return json.dumps({'items':[dict(result,source_refs=result.get('source_refs',current_refs(request)))]},ensure_ascii=False)


def test_materials_are_not_articles_and_periodic_writer_reads_original_context(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        did,kid=material(con,'状态规则','任务开始办理后进入进行中状态。',context='背景：WorkTwin工作任务代表一次有明确结果的工作事项。\n')
        assert con.execute('SELECT organization FROM knowledge').fetchone()[0]=='material'
    model=Writer();worker=ArticleMaintainer(db,model)
    assert worker.process_next()['articles']==1
    assert worker.process_next()['state']=='idle'
    assert len(model.requests)==3
    authored=json.loads(model.requests[1][-1]['content'])
    assert '工作任务代表' in authored['source_documents'][0]['content']
    with db.connect() as con:
        result=con.execute("SELECT * FROM knowledge WHERE organization='article'").fetchone()
        assert result['id']!=kid and result['status']=='confirmed'
        assert result['article_summary'] and result['change_summary']
        assert con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',(result['id'],)).fetchone()[0]==did


def test_update_rewrites_structure_preserving_id_versions_and_all_sources(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    oldbody='旧章节顺序：先列完成，再解释任务。'
    with db.connect() as con:
        first,kid=material(con,'任务定义','WorkTwin任务是有明确结果的工作事项。');article(con,kid,oldbody)
        second,mid=material(con,'状态规则','任务开始办理后进入进行中状态。')
        tid=con.execute("INSERT INTO twins(name) VALUES('任务助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',(tid,kid))
    writer=Writer(groups=[{'topic':'任务办理机制','material_ids':[mid],'article_ids':[kid]}])
    assert ArticleMaintainer(db,writer).process_next()['articles']==1
    with db.connect() as con:
        current=con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
        assert current['version']==2 and not current['body'].startswith(oldbody)
        history=con.execute('SELECT * FROM knowledge_history WHERE knowledge_id=?',(kid,)).fetchone()
        assert history['body']==oldbody and history['article_summary']=='任务办理规则概要'
        assert {r[0] for r in con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',(kid,))}=={first,second}
        assert [k['id'] for k in effective_notes(con,tid)]==[kid]


def test_insufficient_material_waits_without_repeated_model_cost(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        _,mid=material(con,'孤立指代','第一版需回答这四个问题，具体背景没有记录。')
    writer=Writer(output={'complete':False})
    worker=ArticleMaintainer(db,writer)
    assert worker.process_next()['deferred']==1
    assert worker.process_next()['state']=='idle'
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM knowledge WHERE organization='article'").fetchone()[0]==0
        material(con,'补充背景','WorkTwin任务平台需要明确任务定义、状态、办理步骤与完成条件。')
    writer.output=None
    assert worker.process_next()['articles']==1


def test_other_project_and_unconsented_article_are_not_sent_to_writer(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        _,secret=material(con,'未授权历史','SECRET-SOURCE-PASSAGE不能交给模型。',allow_ai=False);article(con,secret,'SECRET-ARTICLE-BODY')
        material(con,'正常资料','任务开始办理后进入进行中状态。')
        material(con,'另一项目','OTHER-PROJECT独立业务规则。',project='project:other')
    writer=Writer();assert ArticleMaintainer(db,writer).process_next()['articles']==1
    sent=json.dumps(writer.requests,ensure_ascii=False)
    assert 'SECRET' not in sent and 'OTHER-PROJECT' not in sent


def test_revocation_during_model_call_does_not_publish(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con: material(con,'正常资料','任务开始办理后进入进行中状态。')
    def revoke():
        with db.connect() as con: con.execute('UPDATE sources SET allow_ai=0')
    result=ArticleMaintainer(db,Writer(hook=revoke)).process_next()
    assert result['state']=='error'
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM knowledge WHERE organization='article'").fetchone()[0]==0


def test_merge_keeps_duplicate_article_sources_and_article_grants(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        first,a=material(con,'任务定义','WorkTwin任务是有明确结果的工作事项。');article(con,a,'## 定义\n任务是什么。')
        second,b=material(con,'完成条件','任务完成必须满足明确的验收条件。');article(con,b,'## 条件\n完成条件。')
        third,m=material(con,'办理步骤','任务开始办理后进入进行中状态。')
        tid=con.execute("INSERT INTO twins(name) VALUES('助手')").lastrowid
        con.execute("INSERT INTO twin_grants(twin_id,subject_type,subject_key) VALUES(?,'knowledge',?)",(tid,str(b)))
    writer=Writer(groups=[{'topic':'任务办理机制','material_ids':[m],'article_ids':[a,b]}])
    assert ArticleMaintainer(db,writer).process_next()['articles']==1
    with db.connect() as con:
        assert con.execute('SELECT status FROM knowledge WHERE id=?',(b,)).fetchone()[0]=='archived'
        assert {r[0] for r in con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',(a,))}=={first,second,third}
        assert [k['id'] for k in effective_notes(con,tid)]==[a]


def test_large_article_continuation_stays_bounded_and_unchanged_keeps_prior_rewrite(tmp_path,monkeypatch):
    # Exercise continuation with a smaller test envelope; production reads up to
    # 700k characters rather than silently shortening the source or article.
    monkeypatch.setattr('worktwin.article_maintenance.MAX_CONTEXT_CHARS',120000)
    monkeypatch.setattr('worktwin.article_maintenance.PAGE_CONTEXT_CHARS',110000)
    db=Database(tmp_path/'db.sqlite')
    original='旧章节。'*7000
    with db.connect() as con:
        _,kid=material(con,'旧文章','旧资料规定任务按结果完成。');article(con,kid,original)
        ids=[]
        for i in range(20):
            _,mid=material(con,str(i),'任务新增规则'+str(i)+'。',context='有依据的工作背景。'*850)
            ids.append(mid)
    class Continuation(Writer):
        def chat(self,messages,max_tokens):
            req=json.loads(messages[-1]['content'])
            assert sum(len(m['content']) for m in messages)<(128000 if req.get('stage')=='article_writing' else 750000)
            if 'materials' in req or req.get('stage')=='article_review': return super().chat(messages,max_tokens)
            self.requests.append(messages)
            if len([m for m in self.requests if json.loads(m[-1]['content']).get('stage') not in ('article_routing','article_review')])==1:
                return json.dumps({'items':[{'complete':True,'kind':'process','title':'完整任务机制',
                    'article_summary':'任务定义及办理机制','body':'重写后的当前章节。'*2500,'change_summary':'重写章节','source_refs':current_refs(req)}]},ensure_ascii=False)
            assert req['existing_articles'][0]['body'].startswith('重写后的')
            return '{"items":[{"complete":true,"unchanged":true}]}'
    writer=Continuation(groups=[{'topic':'任务机制','material_ids':ids,'article_ids':[kid]}])
    assert ArticleMaintainer(db,writer).process_next()['articles']==1
    assert len(writer.requests)>1
    with db.connect() as con:
        assert con.execute('SELECT body FROM knowledge WHERE id=?',(kid,)).fetchone()[0].startswith('重写后的')
