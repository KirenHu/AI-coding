from worktwin.db import Database
from worktwin.projects import source_context, plan_work_units, apply_assignments, catalog
from worktwin.knowledge import store_candidates
from worktwin.project_recheck import ProjectRechecker
from tests.test_project_autonomy import create_source, grounded_item, Judge


def add_context_work(con, did, name, objective, judge):
    doc = con.execute('SELECT * FROM documents WHERE id=?',(did,)).fetchone()
    item = grounded_item(doc['content'],name)
    item.update(project_context=source_context(con,doc),project_objective=objective)
    plan = plan_work_units([item],doc,catalog(con),judge)
    item.update({k:plan[0][k] for k in ('scope','project','project_key')})
    store_candidates(con,did,[],[item],created_by='enterprise_ai')
    apply_assignments(con,did,[item],plan)
    return plan[0]


def test_context_identifies_durable_project_without_name_in_each_quote(tmp_path):
    db=Database(tmp_path/'project.sqlite')
    with db.connect() as con:
        _,first=create_source(con,'协作平台','准备.md','需求评审需要整理活动、任务和办理表单的对应关系。')
        seed=add_context_work(con,first,'协作平台','为企业提供流程与任务协作产品',Judge())
        assert seed['scope']=='project' and seed['identity_kind']=='inferred'
        assert catalog(con)[0]['project_key']==seed['project_key']
        _,second=create_source(con,'协作平台补充','交接.md','办理结束后，将任务的输出数据交给后续活动。')
        follow=add_context_work(con,second,'协作平台','为企业提供流程与任务协作产品',Judge())
        assert follow['project_key']==seed['project_key']
        assert len(catalog(con))==1
    result=ProjectRechecker(db,Judge()).process_next()
    assert result['withdrawn']==0
    with db.connect() as con:
        assert {r[0] for r in con.execute('SELECT scope FROM knowledge')}=={'project'}
        con.execute('UPDATE sources SET enabled=0')
    assert ProjectRechecker(db,Judge()).process_next()['withdrawn']==0
    with db.connect() as con:
        assert len(catalog(con))==1


def test_same_directory_can_contain_different_project_objectives(tmp_path):
    class ObjectiveJudge:
        def compare(self,work,candidate):
            same=candidate.get('proposed_new_project') or work['name']==candidate['name']
            return {'same':float(same),'conflict':float(not same),'engine':'test'}
    db=Database(tmp_path/'different.sqlite')
    with db.connect() as con:
        _,first=create_source(con,'共享目录','产品.md','通过流程节点完成业务数据的传递和协作。')
        seed=add_context_work(con,first,'协作平台','建立流程协作产品',ObjectiveJudge())
        _,second=create_source(con,'共享目录2','独立.md','销售团队需要维护客户商机并管理回款。')
        con.execute('UPDATE sources SET root=? WHERE id=(SELECT source_id FROM documents WHERE id=?)',('/tmp/共享目录/独立',second))
        follow=add_context_work(con,second,'销售管理','管理客户商机和回款',ObjectiveJudge())
        assert seed['scope']==follow['scope']=='project'
        assert seed['project_key']!=follow['project_key']


def test_directory_and_time_are_only_clues_not_automatic_membership(tmp_path):
    db=Database(tmp_path/'unclear.sqlite')
    with db.connect() as con:
        _,did=create_source(con,'工作目录','记录.md','今天需要查看这份资料，尚未说明具体业务目标。')
        link=add_context_work(con,did,'暂未命名项目','',Judge({'same':0,'conflict':0,'engine':'test'}))
        assert link['scope']=='session'
        assert not catalog(con)


def test_incremental_context_keeps_session_workspace_and_authorized_neighbors(tmp_path):
    db=Database(tmp_path/'context.sqlite')
    with db.connect() as con:
        _,did=create_source(con,'会话','one.md','工作目录：/work/协作平台\n历史上下文：企业流程协作产品。')
        secret_source,other=create_source(con,'无授权','two.md','此资料不得发送给模型。')
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(secret_source,))
        doc=dict(con.execute('SELECT * FROM documents WHERE id=?',(did,)).fetchone())
        doc['content']='本次新增的办理规则。'
        context=source_context(con,doc)
        assert context['workspace']=='/work/协作平台'
        assert '历史上下文' in context['document_opening']
        assert not context['nearby_work']


class DurableProjectJudge:
    """Business objective agrees; a shared folder is deliberately ignored."""
    def __init__(self):
        self.calls = []

    def compare(self, work, candidate):
        self.calls.append((work, candidate))
        objectives = candidate.get('project_background', {}).get('objectives', [])
        same = bool(candidate.get('proposed_new_project') or
                    work.get('objective') and work['objective'] in objectives)
        return {'same':float(same), 'conflict':float(not same), 'engine':'objective-test'}


def test_project_background_accumulates_features_and_routes_by_sustained_goal(tmp_path):
    db = Database(tmp_path/'background.sqlite')
    goal = '把本地工作资料整理为可追溯的知识并提供个人分身问答'
    judge = DurableProjectJudge()
    with db.connect() as con:
        first_source, first = create_source(con,'最初讨论','方向.md',
            '工作目录：/work/worktwin\nWorkTwin 项目要把本地工作资料整理为可追溯知识。')
        seed = add_context_work(con,first,'WorkTwin',goal,judge)
        second_source, second = create_source(con,'后续讨论','采集.md',
            '工作目录：/work/worktwin\n浏览器采集需要按网站控制是否记录操作。')
        feature = add_context_work(con,second,'浏览器采集',goal,judge)
        assert feature['project_key'] == seed['project_key']
        assert feature['project'] == 'WorkTwin'
        projects = catalog(con)
        assert len(projects) == 1
        assert projects[0]['background']['objectives'] == [goal]
        assert projects[0]['background']['workspaces'] == ['/work/worktwin']
        assert {r['document_id'] for r in projects[0]['background']['records']} == {first,second}
        assert '浏览器采集' in projects[0]['summary']
        evidence = __import__('json').loads(con.execute(
            'SELECT evidence_json FROM work_units WHERE document_id=?',(second,)).fetchone()[0])
        assert evidence['project_objective'] == goal
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(second_source,))
        current = catalog(con)[0]
        assert '浏览器采集' not in current['summary']
        assert {r['document_id'] for r in current['background']['records']} == {first}


def test_same_workspace_routes_unrelated_goal_to_another_project(tmp_path):
    db = Database(tmp_path/'goal.sqlite')
    judge = DurableProjectJudge()
    with db.connect() as con:
        _, first = create_source(con,'共享工作区甲','知识.md',
            '工作目录：/work/shared\nWorkTwin 项目负责本地工作知识整理。')
        seed = add_context_work(con,first,'WorkTwin','维护本地工作知识和个人分身',judge)
        _, second = create_source(con,'共享工作区乙','销售.md',
            '工作目录：/work/shared\n销售管理项目负责客户商机和回款。')
        sales = add_context_work(con,second,'销售管理','管理客户商机和回款',judge)
        assert seed['project_key'] != sales['project_key']
        assert sales['scope'] == 'project'
        assert len(catalog(con)) == 2


def test_nameless_isolated_material_can_rejoin_from_new_project_background(tmp_path):
    import hashlib
    import json

    class BackgroundJudge:
        def compare(self, work, candidate):
            same = bool(candidate.get('proposed_new_project') or
                        '任务' in work['quote'] and '流程协作' in candidate.get('existing_work',''))
            return {'same':float(same), 'conflict':float(not same), 'engine':'background-test'}

    db = Database(tmp_path/'recover.sqlite')
    task = '办理结束后，把任务输出数据交给后续活动。'
    unrelated = '旅行预订完成后，核对酒店入住日期。'
    with db.connect() as con:
        for index, quote in enumerate((task, unrelated)):
            _, did = create_source(con,'旧材料'+str(index),'旧记录.md',quote)
            item = grounded_item(quote,'')
            item.update(scope='session',project_key='session:'+str(did),project='')
            store_candidates(con,did,[],[item],created_by='enterprise_ai')
            con.execute('''INSERT INTO work_units
                (document_id,quote_hash,topic,project_hint,project_key,status,evidence_json)
                VALUES(?,?,?,'',?,'isolated',?)''',
                (did,hashlib.sha256(quote.encode()).hexdigest(),'任务办理',
                 'session:'+str(did),json.dumps({'quote':quote})))
        _, seed_doc = create_source(con,'新项目背景','产品.md',
            '协作平台项目为企业建立流程协作产品，让任务输出在活动之间传递。')
        seed = add_context_work(con,seed_doc,'协作平台','为企业建立流程协作产品',BackgroundJudge())
    result = ProjectRechecker(db,BackgroundJudge()).process_next()
    assert result['updated'] == 1
    with db.connect() as con:
        tasks = {r['quote']:dict(r) for r in con.execute('''SELECT k.*,e.quote
            FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id''')}
        assert tasks[task]['project_key'] == seed['project_key']
        assert tasks[unrelated]['scope'] == 'session'


def test_one_off_routing_does_not_read_other_documents_in_the_same_source(tmp_path):
    db=Database(tmp_path/'one-off-scope.sqlite')
    with db.connect() as con:
        sid,first=create_source(con,'同一来源','开始.md','协作平台处理任务开始与结果交接。')
        _,second=create_source(con,'授权相邻','补充.md','协作平台任务完成必须通过验收。')
        _,private=create_source(con,'未授权相邻','秘密.md','PRIVATE-OUTSIDE-ARTICLE-SCOPE不得参与本次整理。')
        con.execute('UPDATE documents SET source_id=?',(sid,))
        con.execute('UPDATE sources SET allow_ai=0')
        con.execute("UPDATE documents SET scope='project',project_verified=1,project_key='manual:collaboration',project='协作平台'")
        document=con.execute('SELECT * FROM documents WHERE id=?',(first,)).fetchone()
        assert source_context(con,document)['nearby_work']==[]
        assert catalog(con)==[]
        allowed={first,second}
        context=source_context(con,document,authorized_documents=allowed)
        assert [n['document_id'] for n in context['nearby_work']]==[second]
        projects=catalog(con,authorized_documents=allowed)
        assert {s['document_id'] for s in projects[0]['identity_supports']}==allowed
        assert 'PRIVATE-OUTSIDE' not in __import__('json').dumps(projects,ensure_ascii=False)
        assert con.execute('SELECT sum(allow_ai) FROM sources').fetchone()[0]==0
