import json
import pytest
from worktwin.db import Database
from worktwin.article_maintenance import ArticleMaintainer
from worktwin.library_organization import LibraryOrganizer, organize, articles_for_organization
from tests.test_article_maintenance import material, article, Writer


class Organizer(Writer):
    def __init__(self, *, merge=False, hook=None):
        super().__init__()
        self.merge,self.organize_hook=merge,hook

    def chat(self,messages,max_tokens):
        payload=json.loads(messages[-1]['content'])
        if payload.get('stage')!='library_organization':
            return super().chat(messages,max_tokens)
        self.requests.append(messages)
        if self.organize_hook:self.organize_hook()
        groups={}
        for a in payload['articles']:
            name='Orion' if 'ORION' in a['body'] else '博洋'
            groups.setdefault(name,[]).append(a['id'])
        return json.dumps({'items':[{'group_name':name,'article_ids':ids,
            'merge_sets':[ids] if self.merge and len(ids)>1 else []} for name,ids in groups.items()]})


def fixture(db):
    with db.connect() as con:
        _,a=material(con,'架构','ORION 提供流程执行引擎。');article(con,a,'架构背景。'*400+'ORION 提供流程执行引擎。')
        _,b=material(con,'集成','ORION 通过 REST 接口供外部系统调用。');article(con,b,'ORION 通过 REST 接口供外部系统调用。')
        _,c=material(con,'另一产品','博洋采用建模平台。');article(con,c,'博洋采用建模平台。')
    return a,b,c


def test_existing_articles_group_from_complete_body_without_new_materials(tmp_path):
    db=Database(tmp_path/'db');a,b,c=fixture(db);model=Organizer()
    worker=LibraryOrganizer(db,model,ArticleMaintainer(db,model))
    assert worker.process_next()['state']=='done'
    assert worker.process_next()['state']=='idle'
    request=json.loads(model.requests[0][-1]['content'])
    assert len(request['articles'][0]['body'])>1200 and 'ORION' in request['articles'][0]['body'][1200:]
    with db.connect() as con:
        rows={r['id']:dict(r) for r in con.execute('SELECT * FROM knowledge')}
        assert rows[a]['group_name']==rows[b]['group_name']=='Orion'
        assert rows[a]['group_key']==rows[b]['group_key']!=rows[c]['group_key']
        assert rows[a]['project_key']=='project:worktwin' and rows[c]['group_name']=='博洋'
        con.execute("UPDATE knowledge SET body='博洋新内容',version=version+1 WHERE id=?",(b,))
    assert worker.process_next()['state']=='done'
    with db.connect() as con:assert con.execute('SELECT group_name FROM knowledge WHERE id=?',(b,)).fetchone()[0]=='博洋'


def test_existing_merge_uses_sources_preserves_history_and_stable_id(tmp_path):
    db=Database(tmp_path/'db');a,b,c=fixture(db);model=Organizer(merge=True)
    worker=LibraryOrganizer(db,model,ArticleMaintainer(db,model))
    assert worker.process_next()['state']=='done'
    result=worker.process_next()
    assert result['state']=='done',result
    with db.connect() as con:
        rows={r['id']:dict(r) for r in con.execute('SELECT * FROM knowledge')}
        assert rows[a]['status']=='confirmed' and rows[a]['version']==2
        assert rows[a]['group_name']=='Orion'
        assert rows[b]['status']=='archived' and rows[c]['status']=='confirmed'
        assert con.execute('SELECT count(*) FROM knowledge_history WHERE knowledge_id IN (?,?)',(a,b)).fetchone()[0]==2
        assert con.execute('SELECT count(DISTINCT document_id) FROM knowledge_evidence WHERE knowledge_id=?',(a,)).fetchone()[0]==2


def test_no_grant_expansion_or_cross_project_merge(tmp_path):
    db=Database(tmp_path/'db');a,b,c=fixture(db);model=Organizer(merge=True)
    with db.connect() as con:
        tid=con.execute("INSERT INTO twins(name) VALUES('读者')").lastrowid
        con.execute("INSERT INTO twin_grants(twin_id,subject_type,subject_key) VALUES(?,'knowledge',?)",(tid,str(a)))
        _,d=material(con,'其他项目','ORION 另一个项目',project='project:other');article(con,d,'ORION 另一个项目')
    worker=LibraryOrganizer(db,model,ArticleMaintainer(db,model))
    assert worker.process_next()['state']=='done'
    assert worker.process_next()['state']=='done'
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM article_merge_queue').fetchone()[0]==0
        assert con.execute('SELECT count(DISTINCT group_key) FROM knowledge WHERE id IN (?,?)',(a,d)).fetchone()[0]==2


def test_revoked_permission_and_concurrent_edits_do_not_publish(tmp_path):
    db=Database(tmp_path/'db');fixture(db)
    def revoke():
        with db.connect() as con:con.execute('UPDATE sources SET allow_ai=0')
    model=Organizer(hook=revoke)
    result=LibraryOrganizer(db,model,ArticleMaintainer(db,model)).process_next()
    assert result['state']=='error'
    with db.connect() as con:
        assert not articles_for_organization(con)
        assert con.execute("SELECT count(*) FROM knowledge WHERE group_name!=''").fetchone()[0]==0


@pytest.mark.parametrize('items',[
    [{'group_name':'a','article_ids':[1],'merge_sets':[]}],
    [{'group_name':'a','article_ids':[1,2],'merge_sets':[[1,3]]}],
    [{'group_name':'a','article_ids':[1,1,2]}],
    [{'group_name':'a','article_ids':[1]},{'group_name':' a ','article_ids':[2]}],
])
def test_invalid_grouping_cannot_drop_duplicate_or_invent_articles(items):
    class Model:
        def chat(self,*args,**kwargs):return json.dumps({'items':items})
    rows=[dict(id=i,title='x',body='x',article_summary='',group_name='',scope_detail='') for i in [1,2]]
    with pytest.raises(ValueError):organize(Model(),'项目',rows)
