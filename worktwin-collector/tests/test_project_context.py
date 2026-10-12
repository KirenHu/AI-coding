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
