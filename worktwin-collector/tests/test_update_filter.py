import json
from unittest.mock import patch

import pytest

from worktwin.update_filter import canonical, plan, ModelText
from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.jobs import KnowledgeWorker
from worktwin.model_settings import ModelRuntime
from tests.support import FakeModel


@pytest.mark.parametrize('old,new,mode',[
    ('费用100元','费用100元','skip'),
    ('正文\r\n第二行\r\n','正文\n第二行\n\n','skip'),
    ('采用方案A','选择方案A','full'),
    ('费用100元','费用200元','full'),
    ('2026-10-13生效','2026-11-13生效','full'),
    ('允许对外共享','不允许对外共享','full'),
    ('甲段\n\n乙段','乙段\n\n甲段','full'),
    ('甲段\n\n乙段','甲段','full'),
    ('[资料](https://a.test)','[资料](https://b.test)','full'),
    ('    return 1','return 1','full'),
    ('a  \nb','a\nb','full'),
    ('条款一\n\n条款二','条款一\n条款二','full'),
])
def test_meaningful_changes_never_skipped(old,new,mode):
    selection=plan(old,new)
    assert selection['mode']==mode
    if mode=='full':
        assert selection['content']==new and selection['changes']


def long_doc():
    return '# 项目说明\n\n用于维护项目约定。\n\n## 历史背景\n'+('已有背景与规则。\n'*900)+'\n## 当前配置\n\n采用配置A。\n'


def test_new_section_keeps_context_and_falls_back_for_dependencies():
    old=long_doc()
    new=old+'\n## 导出机制\n\n新增观点：最终决定支持导出为 Markdown 文件。\n'
    chosen=plan(old,new)
    assert chosen['mode']=='incremental'
    assert '当前配置' in chosen['content'] and '导出机制' in chosen['content']
    assert chosen['changes'][0]['kind']=='insert'
    assert plan(old,new,enabled=False)['reason']=='incremental_disabled'
    assert plan(old,old+'\n## 更正\n不再采用此前配置。')['mode']=='full'
    assert plan(None,new)['reason']=='first_import'


def setup_file(tmp_path, text):
    root=tmp_path/'source'; root.mkdir()
    path=root/'spec.md'; path.write_text(text)
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,root,allow_ai) VALUES('测试项目','folder',?,1)",(str(root),))
    collector=Collector(db); collector.scan_all()
    model=FakeModel(); worker=KnowledgeWorker(db,client=model)
    return db,collector,path,model,worker


def test_repeat_sync_preserves_sources_and_skips_calls(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,'最终决定采用统一审批机制，供所有业务流程使用。\n')
    assert worker.process_next()['state']=='done'
    before=len(model.requests)
    path.write_text(path.read_text()+'\n\n')
    collector.scan_all()
    assert worker.process_next()['state']=='done'
    assert len(model.requests)==before
    with db.connect() as con:
        run=con.execute('SELECT * FROM knowledge_processing_runs ORDER BY id DESC').fetchone()
        assert run['mode']=='skip' and run['model_calls']==0
        assert con.execute('SELECT COUNT(*) FROM knowledge_evidence WHERE is_current=1').fetchone()[0]>0
        assert con.execute('SELECT allow_ai FROM sources').fetchone()[0]==1
        assert con.execute('SELECT content FROM documents').fetchone()[0]


def test_only_success_advances_baseline_and_filter_failure_falls_back(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,long_doc())
    assert worker.process_next()['state']=='done'
    with db.connect() as con:
        baseline=con.execute('SELECT content FROM document_processing_baselines').fetchone()[0]
    path.write_text(path.read_text()+'\n## 新的模块\n最终决定支持独立模块运行。')
    collector.scan_all()
    with patch('worktwin.jobs.plan',side_effect=ValueError('bad alignment')):
        assert worker.process_next()['state']=='done'
    with db.connect() as con:
        run=con.execute('SELECT * FROM knowledge_processing_runs ORDER BY id DESC').fetchone()
        assert run['reason']=='filter_error' and run['mode']=='full'
        assert con.execute('SELECT content FROM document_processing_baselines').fetchone()[0]!=baseline


def test_changed_during_call_does_not_commit_baseline(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,'最终决定采用统一审批机制，供所有业务流程使用。')
    original=model.chat
    def mutate(messages,max_tokens=1800):
        path.write_text('最终决定取消统一审批机制，每个项目自行配置。')
        collector.scan_all()
        return original(messages,max_tokens)
    model.chat=mutate
    assert worker.process_next()['state']=='superseded'
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM document_processing_baselines').fetchone()[0]==0


def test_runtime_records_actual_usage_without_estimating_unknown(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    class Client:
        configured=True
        model='test'
        def chat(self,messages,max_tokens=1800):
            return ModelText('OK',{'prompt_tokens':27,'completion_tokens':3})
    runtime=ModelRuntime(db,None,injected=Client())
    assert runtime.chat([{'role':'user','content':'hello'}])=='OK'
    with db.connect() as con:
        row=con.execute('SELECT * FROM model_call_metrics').fetchone()
        assert row['input_tokens']==27 and row['output_tokens']==3
        assert row['elapsed_ms']>=0
    runtime.client.chat=lambda *a,**k:'OK'
    runtime.chat([])
    with db.connect() as con:
        assert con.execute('SELECT input_tokens FROM model_call_metrics ORDER BY id DESC').fetchone()[0] is None


def test_deletion_with_no_new_candidates_refreshes_existing_article(tmp_path):
    from worktwin.article_maintenance import ArticleMaintainer
    text='最终决定采用统一审批机制，供所有业务流程使用。\n\n这是需要长期保留的项目背景。'
    db,collector,path,model,worker=setup_file(tmp_path,text)
    assert worker.process_next()['state']=='done'
    maintainer=ArticleMaintainer(db,model)
    assert maintainer.process_next()['state']=='done'
    with db.connect() as con:
        article=con.execute("SELECT id FROM knowledge WHERE organization='article'").fetchone()[0]
    path.write_text('这是需要长期保留的项目背景。')
    collector.scan_all()
    with db.connect() as con:
        assert con.execute('SELECT review_hold FROM knowledge WHERE id=?',(article,)).fetchone()[0]==1
    assert worker.process_next()['candidates']==0
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM knowledge_evidence e JOIN knowledge k ON k.id=e.knowledge_id WHERE k.organization='material' AND e.superseded=0").fetchone()[0]==0
    # Verify refreshed authorship reads the remaining full source even with no materials.
    calls_before=len(model.requests)
    result=maintainer.process_next()
    assert result.get('source_refresh') and result['state']=='done'
    writes=[json.loads(m[-1]['content']) for m in model.requests[calls_before:]
            if 'source_documents' in m[-1]['content'] and 'source_materials' in m[-1]['content']]
    assert writes and '统一审批机制' not in writes[0]['source_documents'][0]['content']
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM article_source_refresh').fetchone()[0]==0


def test_two_edits_before_worker_compare_last_success_not_last_scan(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,long_doc())
    assert worker.process_next()['state']=='done'
    first='\n## 新模块一\n最终决定采用独立部署机制。'
    second='\n## 新模块二\n最终决定采用独立审批机制。'
    path.write_text(path.read_text()+first); collector.scan_all()
    path.write_text(path.read_text()+second); collector.scan_all()
    before=len(model.requests)
    assert worker.process_next()['state']=='done'
    payloads=[m[-1]['content'] for m in model.requests[before:] if '<source>' in m[-1]['content']]
    assert any(first.strip() in p and second.strip() in p for p in payloads)


def test_revoked_ai_permission_does_not_process_pending_update(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,long_doc())
    assert worker.process_next()['state']=='done'
    with db.connect() as con:
        con.execute('UPDATE sources SET allow_ai=0')
    path.write_text(path.read_text()+'\n## 新模块\n最终决定采用独立部署机制。')
    collector.scan_all()
    before=len(model.requests)
    assert worker.process_next()['state']=='idle'
    assert len(model.requests)==before
    with db.connect() as con:
        assert '新模块' in con.execute('SELECT content FROM documents').fetchone()[0]
        assert '新模块' not in con.execute('SELECT content FROM document_processing_baselines').fetchone()[0]


def test_failed_model_preserves_successful_baseline(tmp_path):
    db,collector,path,model,worker=setup_file(tmp_path,long_doc())
    assert worker.process_next()['state']=='done'
    with db.connect() as con:
        old=con.execute('SELECT content_sha FROM document_processing_baselines').fetchone()[0]
    path.write_text(path.read_text()+'\n## 新模块\n最终决定采用独立部署机制。')
    collector.scan_all()
    def fail(*args,**kwargs):
        raise TimeoutError()
    model.chat=fail
    assert worker.process_next()['state']=='retrying'
    with db.connect() as con:
        assert con.execute('SELECT content_sha FROM document_processing_baselines').fetchone()[0]==old


def test_stats_endpoint_exposes_no_document_text(tmp_path):
    from fastapi.testclient import TestClient
    from worktwin.api import create_app
    db,collector,path,model,worker=setup_file(tmp_path,long_doc())
    worker.process_next()
    app=create_app(db.path,start_worker=False,inference_client=model)
    with TestClient(app) as client:
        import re
        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)
        response=client.get('/api/processing/stats',headers={'X-Worktwin-Token':token})
    assert response.status_code==200
    data=response.json()
    assert data['decisions'] and data['cost'] is None
    assert '已有背景' not in response.text


def test_reconfirmed_material_updates_body_when_quote_survives(tmp_path):
    from worktwin.knowledge import store_candidates
    db,collector,path,model,worker=setup_file(tmp_path,'最终决定采用统一审批机制，供所有业务流程使用。')
    worker.process_next()
    with db.connect() as con:
        material=dict(con.execute("SELECT * FROM knowledge WHERE organization='material'").fetchone())
        evidence=con.execute('SELECT document_id,quote FROM knowledge_evidence WHERE knowledge_id=?',(material['id'],)).fetchone()
        item=dict(material,quote=evidence['quote'],body='结合新的适用条件重新核对后的说明。',value_reason='用于查阅本项工作的要求和方案依据')
        store_candidates(con,evidence['document_id'],[],[item],created_by='enterprise_ai')
        current=con.execute('SELECT body,version FROM knowledge WHERE id=?',(material['id'],)).fetchone()
        assert current['body']==item['body'] and current['version']==material['version']+1
