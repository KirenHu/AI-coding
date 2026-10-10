"""Autonomous work-unit projects and continuously inherited twin grants."""
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.db import Database
from worktwin.projects import catalog, plan_work_units, apply_assignments
from worktwin.knowledge import store_candidates
from worktwin.reconcile import existing_for_project
from worktwin.twin_access import effective_notes


def session_token(client):
    return {'X-Worktwin-Token':re.search(
        r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)}


def create_source(con, source, name, text):
    sid=con.execute("INSERT INTO sources(name,kind,root,allow_ai,allow_share) VALUES(?,'folder',?,1,1)",
                    (source,'/tmp/auto-project-'+source)).lastrowid
    did=con.execute("""INSERT INTO documents(source_id,path,relative_path,title,file_type,project,
        content,sha256,size_bytes,mtime_ns,project_key,scope)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'session')""",
        (sid,'/tmp/'+source+'/'+name,name,name,'.md',source,text,'sha'+source,
         len(text),1,'session:'+str(sid))).lastrowid
    con.execute('UPDATE documents SET project_key=? WHERE id=?',(f'session:{did}',did))
    con.execute('INSERT INTO chunks(document_id,ordinal,start_offset,end_offset,text) VALUES(?,?,?,?,?)',
                (did,0,0,len(text),text))
    return sid,did


class Judge:
    def __init__(self, response=None):
        self.calls=0
        self.response=response or {'same':.98,'conflict':.01,'engine':'jev'}
    def compare(self,a,b):
        self.calls+=1
        return dict(self.response)


def grounded_item(quote, project='WorkTwin'):
    return dict(kind='decision',title='更新规则',body=quote,quote=quote,topic='更新规则',
        scope_detail='WorkTwin',quality='useful',attribution='user',outcome='none',
        extraction_version=1,project_hint=project)


def test_two_sources_link_under_one_project_without_document_verification(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    text1='项目 WorkTwin 明确采用自动知识整理，并且保持来源追溯。'
    text2='WorkTwin 项目新增规则：跨会话知识关联不需要逐份确认。'
    with db.connect() as con:
        a,d1=create_source(con,'alpha','one.md',text1)
        b,d2=create_source(con,'beta','two.md',text2)
        first=grounded_item(text1)
        doc=con.execute('SELECT * FROM documents WHERE id=?',(d1,)).fetchone()
        planned=plan_work_units([first],doc,catalog(con),Judge())
        first.update({k:planned[0][k] for k in ('scope','project','project_key')})
        assert first['scope']=='project'
        assert store_candidates(con,d1,[],[first],created_by='enterprise_ai')==1
        apply_assignments(con,d1,[first],planned)
        project=first['project_key']
    with db.connect() as con:
        second=grounded_item(text2)
        doc=con.execute('SELECT * FROM documents WHERE id=?',(d2,)).fetchone()
        judge=Judge()
        plan=plan_work_units([second],doc,catalog(con),judge)
        assert plan[0]['project_key']==project and judge.calls==1
        assert plan[0]['status']=='confirmed'
        assert len(existing_for_project(con,d2,project_key=project,scope='project'))==1
        assert con.execute('SELECT project_verified FROM documents WHERE id=?',(d2,)).fetchone()[0]==0


def test_revoked_project_name_not_sent_to_identity_judge(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    quote='WorkTwin 项目规则：项目资料必须能够撤销模型授权。'
    with db.connect() as con:
        sid,did=create_source(con,'revoked-catalog','entry.md',quote)
        item=grounded_item(quote)
        link=plan_work_units([item],con.execute(
            'SELECT * FROM documents WHERE id=?',(did,)).fetchone(),[],Judge())[0]
        item.update({field:link[field] for field in ('scope','project','project_key')})
        store_candidates(con,did,[],[item],created_by='enterprise_ai')
        apply_assignments(con,did,[item],[link])
        project_key=link['project_key']
        assert project_key in {p['project_key'] for p in catalog(con)}
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(sid,))
        assert project_key not in {p['project_key'] for p in catalog(con)}


def test_mixed_document_assigns_separate_work_units(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    a='WorkTwin 项目需要自动维护知识。'
    b='HAP 项目需要审查审批流的交互配置。'
    with db.connect() as con:
        _,did=create_source(con,'mixed','weekly.md',a+'\n'+b)
        items=[grounded_item(a),grounded_item(b,'HAP')]
        doc=con.execute('SELECT * FROM documents WHERE id=?',(did,)).fetchone()
        plan=plan_work_units(items,doc,catalog(con),Judge())
        assert plan[0]['project_key']!=plan[1]['project_key']
        for item,link in zip(items,plan):
            item.update({k:link[k] for k in ('scope','project','project_key')})
        store_candidates(con,did,[],items,created_by='enterprise_ai')
        apply_assignments(con,did,items,plan)
        assert con.execute('SELECT count(*) FROM work_units').fetchone()[0]==2
        assert con.execute('SELECT count(DISTINCT project_key) FROM project_memberships').fetchone()[0]==2


def test_dynamic_project_grant_inherits_new_articles_and_revokes(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    quote='WorkTwin 项目决定将知识整理和分身问答持续关联。'
    with db.connect() as con:
        sid,did=create_source(con,'dyn','note.md',quote)
        project='auto:unique-project'
        con.execute("INSERT INTO project_entities(project_key,name) VALUES(?,'WorkTwin')",(project,))
        con.execute("INSERT INTO twins(name,knowledge_mode) VALUES('交接分身','dynamic')")
        twin=con.execute("SELECT id FROM twins").fetchone()[0]
        con.execute("""INSERT INTO twin_grants(twin_id,subject_type,subject_key)
            VALUES(?,'project',?)""",(twin,project))
        assert effective_notes(con,twin)==[]
        for idx in range(2):
            body=quote+str(idx)
            note=con.execute("""INSERT INTO knowledge(kind,title,body,status,created_by,
                source_bound,scope,project,project_key,topic,scope_detail,quality,attribution)
                VALUES('decision',? ,?,'confirmed','enterprise_ai',1,'project','WorkTwin',
                       ?,'决策','WorkTwin','useful','user')""",
                ('知识'+str(idx),body,project)).lastrowid
            con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)",
                        (note,did,quote))
            con.execute("""INSERT INTO project_memberships
                (knowledge_id,project_key,status) VALUES(?,?,'confirmed')""",(note,project))
        assert len(effective_notes(con,twin))==2
        con.execute("INSERT INTO twin_grants(twin_id,subject_type,subject_key,effect) VALUES(?,'knowledge',?,'deny')",
                    (twin,str(note)))
        assert len(effective_notes(con,twin))==1
        con.execute('UPDATE sources SET allow_share=0 WHERE id=?',(sid,))
        assert effective_notes(con,twin,sharing=True)==[]
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(sid,))
        assert effective_notes(con,twin)==[]


def test_scoped_grants_api_and_old_manual_pins(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with app.state.db.connect() as con:
        sid,did=create_source(con,'grant','guide.md','本项目决定按项目授权新知识，后续自动继承。')
        note=con.execute("""INSERT INTO knowledge(kind,title,body,status,scope,project,
            project_key,quality) VALUES('decision','授权规则','后续自动继承','confirmed',
            'project','示例','manual:example','useful')""").lastrowid
    with TestClient(app) as c:
        h=session_token(c)
        tid=c.post('/api/twins',headers=h,json={'name':'分身'}).json()['id']
        data=c.get(f'/api/twins/{tid}',headers=h).json()
        assert data['knowledge_mode']=='dynamic'
        options=c.get('/api/twin-grant-options',headers=h).json()
        assert any(p['project_key']=='manual:example' for p in options['projects'])
        update=c.put(f'/api/twins/{tid}',headers=h,json={'name':'分身','knowledge_mode':'dynamic',
            'grants':[{'subject_type':'project','subject_key':'manual:example','effect':'allow'}]})
        assert update.status_code==200,update.text
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==1
        # Legacy IDs work in both modes; switching to manual disables auto grants.
        update=c.put(f'/api/twins/{tid}',headers=h,json={'name':'分身','knowledge_mode':'manual'})
        assert update.status_code==200
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==0
        assert c.put(f'/api/twins/{tid}/knowledge',headers=h,json={'knowledge_ids':[note]}).status_code==200
        assert c.get('/api/twins',headers=h).json()[0]['knowledge_count']==1
