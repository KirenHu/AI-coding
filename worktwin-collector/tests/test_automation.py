import hashlib
import pytest

from worktwin.automation import acceptance_status,record_acceptance,activate_new,apply_additions,REQUIRED_CHECKS
from worktwin.db import Database
from worktwin.knowledge import store_candidates
from worktwin.reconcile import store_proposals,resolve_proposal
from worktwin.knowledge_policy import READY_SQL


class Model:
    url='https://test-model.invalid/v1'
    model='offline-test-model'


def receipt():
    # Structural gate test only; never installed into an end user's database.
    return {'real_data':True,'dataset_sha256':hashlib.sha256(b'offline-gate-fixture').hexdigest(),
            'checks':{key:True for key in REQUIRED_CHECKS}}


def setup(tmp_path):
    db=Database(tmp_path/'knowledge.sqlite')
    text='项目只维护当前结论，旧正文存入版本历史。新增说明：当前知识允许分配给数字分身。'
    with db.connect() as con:
        sid=con.execute("INSERT INTO sources(name,kind,root,allow_ai) VALUES('WorkTwin','folder','/authorized',1)").lastrowid
        did=con.execute('''INSERT INTO documents(source_id,path,relative_path,title,file_type,content,sha256,size_bytes,mtime_ns,
            project,project_key,scope,project_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)''',
            (sid,'/authorized/decision.md','decision.md','当前知识','.md',text,'source-sha',100,1,'WorkTwin','project-worktwin','project')).lastrowid
        candidate=dict(kind='decision',title='当前知识更新规则',body='项目只维护当前结论，旧正文存入版本历史。',quote='项目只维护当前结论，旧正文存入版本历史。',
            topic='知识更新',scope_detail='WorkTwin知识库',quality='useful',attribution='user',outcome='none',extraction_version=1,requires_review=False,
            value_reason='用于查阅当前知识与历史的区分')
        store_candidates(con,did,[],[candidate],created_by='enterprise_ai')
    return db,did,candidate


def test_acceptance_gate_requires_all_real_data_checks_and_same_model(tmp_path):
    db,_,_=setup(tmp_path)
    with db.connect() as con:
        assert not acceptance_status(con,Model())['ready']
        report=receipt();report['real_data']=False
        with pytest.raises(ValueError):record_acceptance(con,Model(),report)
        report=receipt();report['checks']['conflict']=False
        with pytest.raises(ValueError):record_acceptance(con,Model(),report)
        record_acceptance(con,Model(),receipt())
        assert acceptance_status(con,Model())['ready']
        changed=Model();changed.model='different-model'
        assert not acceptance_status(con,changed)['ready']


def test_session_scoped_notes_activate_without_each_project_confirmation(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        # The user has authorized the source, but has not named a business
        # project. The resulting note is private to this source/session.
        key = f'session:{did}'
        con.execute('UPDATE documents SET project_verified=0,scope=?,project_key=? WHERE id=?',
                    ('session',key,did))
        con.execute('UPDATE knowledge SET scope=?,project_key=?',('session',key))
        assert not acceptance_status(con,Model())['ready']
        assert activate_new(con,did,[item | {'requires_review':True}],Model()) == 1
        assert len(con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL).fetchall())==1
        # Reprocessing the same source is idempotent.
        assert activate_new(con,did,[item],Model()) == 0

def test_only_exact_additions_activate_and_replacements_stay_pending(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        record_acceptance(con,Model(),receipt())
        activate_new(con,did,[item],Model())
        kid=con.execute('SELECT id FROM knowledge').fetchone()[0]
        addition=item|{'quote':'新增说明：当前知识允许分配给数字分身。','body':'当前知识允许分配给数字分身。'}
        plan={0:{'target_id':kid,'action':'enrich','title':item['title'],'body':item['body']+'\n\n'+addition['body'],
            'reason':'新增说明不改变原结论','changes_existing_conclusion':False}}
        assert store_proposals(con,did,'source-sha',[addition],plan)==1
        assert apply_additions(con,did,Model())==1
        current=con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
        assert current['body']==plan[0]['body'] and current['version']==3
        plan[0].update(action='replace',body='当前知识仅包含最新结论，不保留旧版正文。')
        assert store_proposals(con,did,'source-sha',[item],plan)==1
        assert apply_additions(con,did,Model())==0
        pid=con.execute("SELECT id FROM knowledge_proposals WHERE status='pending'").fetchone()[0]
        resolve_proposal(con,pid,accept=True)
        active=con.execute('SELECT * FROM knowledge WHERE id=?',(kid,)).fetchone()
        assert active['body']==plan[0]['body'] and active['id']==kid
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0]==1
        assert current['body'] in {r[0] for r in con.execute('SELECT body FROM knowledge_history WHERE knowledge_id=?',(kid,))}


def test_model_rewrites_never_count_as_automatic_additions(tmp_path):
    db,did,item=setup(tmp_path)
    with db.connect() as con:
        record_acceptance(con,Model(),receipt());activate_new(con,did,[item],Model())
        kid=con.execute('SELECT id FROM knowledge').fetchone()[0]
        plan={0:{'target_id':kid,'action':'enrich','title':item['title'],'body':'模型重写并删除了原有的当前结论内容。',
            'reason':'模型声称只是整理','changes_existing_conclusion':False}}
        assert store_proposals(con,did,'source-sha',[item],plan)==1
        assert apply_additions(con,did,Model())==0
        assert con.execute('SELECT body FROM knowledge WHERE id=?',(kid,)).fetchone()[0]==item['body']
