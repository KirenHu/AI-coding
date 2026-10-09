"""Real boundaries and chronology: scope mistakes must not enter AI context."""
import json
import re
import io
import zipfile

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.db import Database
from worktwin.inference import extract_knowledge
from worktwin.knowledge_policy import READY_SQL
from worktwin.scope import document_scope, source_time
from worktwin.answers import answer_from_knowledge
from worktwin.reconcile import make_consolidation_plan


class Reply:
    configured=True

    def __init__(self, result):
        self.result=result
        self.requests=[]

    def chat(self, messages, max_tokens=2400):
        self.requests.append(messages)
        return self.result if isinstance(self.result,str) else json.dumps(self.result,ensure_ascii=False)


def candidate(**kwargs):
    return dict(kind='decision',title='提示词交付要求',body='本项目正式交付的提示词采用英文版本，中文版用于维护。',
        topic='提示词交付规范',scope_detail='本项目正式交付的提示词',value_reason='查阅本项目提示词交付使用的语言',
        attribution='user',outcome='none',quote='建议本项目正式交付的提示词采用英文版本。',**kwargs)


def token(client):
    return {'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)}


def test_directory_does_not_establish_same_business_project():
    def doc(i,cwd):
        return dict(id=i,source_id=1,project='demo',file_type='.jsonl',content='工作目录：'+cwd)
    scopes=[document_scope(doc(1,'/work/demo')),document_scope(doc(2,'/work/demo')),
            document_scope(doc(3,'/other/demo')),document_scope(doc(4,'/Users/kirenhu'))]
    assert all(s['scope']=='session' for s in scopes)
    assert len({s['project_key'] for s in scopes})==4


def test_acceptance_needs_the_actual_prior_proposal():
    quote=candidate()['quote']
    text='### AI · 2026-10-09T00:00:00Z\n'+quote+'\n\n### 用户 · 2026-10-09T00:01:00Z\n同意。'
    model=Reply({'items':[candidate(confirmation_quote='同意。')]})
    result=extract_knowledge(text,transcript=True,client=model,scope={'scope':'session'})
    assert len(result)==1 and result[0]['occurred_at']=='2026-10-09 00:01:00'
    assert quote in model.requests[0][-1]['content'] and '同意。' in model.requests[0][-1]['content']
    # The same acknowledgement before the proposal cannot confirm it.
    earlier='### 用户 · 2026-10-09T00:00:00Z\n同意。\n\n### AI · 2026-10-09T00:01:00Z\n'+quote
    assert extract_knowledge(earlier,transcript=True,client=model)==[]
    # An unrelated intervening request breaks a bare acknowledgement's link.
    unrelated=text.replace('### 用户 ·','### 用户 · 2026-10-09T00:00:30Z\n请讨论其他项目。\n\n### 用户 ·',1)
    assert extract_knowledge(unrelated,transcript=True,client=model)==[]


def test_noise_and_unconfirmed_ai_suggestions_are_rejected():
    quote='用户倾向于第二点，没有解释具体理由。'
    item=candidate();item.update(title='用户倾向于第二点',quote=quote,body=quote)
    assert extract_knowledge(quote,transcript=False,client=Reply({'items':[item]}))==[]
    suggestion=candidate();suggestion['attribution']='assistant'
    text='### AI · 2026-10-09\n'+suggestion['quote']
    assert extract_knowledge(text,transcript=True,client=Reply({'items':[suggestion]}))==[]


def test_missing_value_and_invented_evidence_are_rejected():
    item=candidate();item['value_reason']=''
    assert extract_knowledge(item['quote'],transcript=False,client=Reply({'items':[item]}))==[]
    item=candidate()
    assert extract_knowledge('原文没有记载任何提示词要求。',transcript=False,client=Reply({'items':[item]}))==[]


def test_project_requirement_is_not_used_globally_or_in_other_project():
    rows=[dict(id=1,title='英文提示词交付',body='项目甲要求英文提示词。',scope='project',
               project='甲',project_key='A',topic='提示词交付',scope_detail='项目甲提示词'),
          dict(id=2,title='中文提示词交付',body='项目乙要求中文提示词。',scope='project',
               project='乙',project_key='B',topic='提示词交付',scope_detail='项目乙提示词')]
    model=Reply('按当前项目要求使用英文提示词。[K1]')
    result=answer_from_knowledge(model,'提示词应使用什么语言？',rows,project_key='A')
    assert result['citations']==[{'knowledge_id':1,'title':'英文提示词交付'}]
    assert '项目乙' not in model.requests[0][-1]['content']
    model.requests.clear()
    result=answer_from_knowledge(model,'所有提示词都需要英文吗？',rows)
    assert result['answer_status']=='scope_required' and model.requests==[]


def test_an_unrelated_question_does_not_send_arbitrary_notes():
    rows=[dict(id=1,title='审批路由',body='审批采用工作流节点。',scope='global')]
    model=Reply('unsupported')
    assert answer_from_knowledge(model,'酒店早餐供应时间',rows)['answer_status']=='insufficient'
    assert model.requests==[]


def test_move_to_business_project_changes_actual_boundary_and_keeps_history(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as client:
        h=token(client)
        payload=dict(title='提示词交付',body='英文版用于正式交付。',scope='project',project='项目甲',
                     topic='提示词交付',scope_detail='项目甲的交付版本',status='confirmed')
        kid=client.post('/api/knowledge',headers=h,json=payload).json()['id']
        old=client.get('/api/knowledge',headers=h).json()[0]
        payload.update(project='项目乙',scope_detail='项目乙的交付版本')
        assert client.put(f'/api/knowledge/{kid}',headers=h,json=payload).status_code==200
        new=client.get('/api/knowledge',headers=h).json()[0]
        assert new['id']==old['id'] and new['project_key']!=old['project_key'] and new['version']==2
        history=client.get(f'/api/knowledge/{kid}/history',headers=h).json()
        assert history[0]['project']=='项目甲' and history[0]['project_key']==old['project_key']


def test_stop_using_low_value_knowledge_is_immediate_and_reversible(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as client:
        h=token(client)
        payload=dict(title='提示词交付',body='英文版用于正式交付。',status='confirmed')
        kid=client.post('/api/knowledge',headers=h,json=payload).json()['id']
        assert client.post('/api/knowledge/disable',headers=h,json={'knowledge_ids':[kid]}).status_code==200
        with app.state.db.connect() as con:
            assert con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL).fetchall()==[]
            row=con.execute('SELECT body,quality FROM knowledge WHERE id=?',(kid,)).fetchone()
            assert row['body']==payload['body'] and row['quality']=='noise'
        payload.update(quality='useful')
        assert client.put(f'/api/knowledge/{kid}',headers=h,json=payload).status_code==200
        with app.state.db.connect() as con:
            assert con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL).fetchone()[0]==kid


def test_legacy_ai_notes_are_preserved_but_not_ready(tmp_path):
    db=Database(tmp_path/'old.sqlite')
    with db.connect() as con:
        con.execute("DELETE FROM settings WHERE key='scoped_knowledge_v1'")
        con.execute("INSERT INTO knowledge(kind,title,body,status,created_by) VALUES('decision','提示词必须英文','所有提示词必须英文。','confirmed','ai')")
    upgraded=Database(db.path)
    with upgraded.connect() as con:
        row=con.execute('SELECT * FROM knowledge').fetchone()
        assert row['body']=='所有提示词必须英文。' and row['scope']=='unknown' and row['quality']=='uncertain'
        assert con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL).fetchall()==[]
    # An app restart must not reset a subsequently reviewed correction.
    with upgraded.connect() as con:
        con.execute("UPDATE knowledge SET scope='global',quality='useful',needs_review=0,review_hold=0")
    Database(db.path)
    with upgraded.connect() as con:
        assert con.execute('SELECT scope FROM knowledge').fetchone()[0]=='global'


def test_same_project_different_topic_is_not_merged():
    new=candidate()
    existing=[dict(id=7,kind='decision',topic='酒店采购',scope_detail='酒店采购',title='酒店采购标准',body='酒店采购按业务需求处理。')]
    model=Reply({'items':[dict(index=0,action='enrich',target_id=7,title='混合两个主题',
        body='把酒店采购与英文提示词交付要求合并。',reason='模型错误认为都是项目要求')]})
    assert make_consolidation_plan(model,[new],existing)=={}


def test_event_time_is_compared_in_utc():
    assert source_time('2026-10-09T08:30:00+08:00')=='2026-10-09 00:30:00'
    assert source_time('2026-10-09T01:00:00Z')>'2026-10-09 00:30:00'
    assert source_time('unknown')==''


def test_obsidian_export_preserves_scope_and_warns_about_disabled_notes(tmp_path):
    app=create_app(tmp_path/'db.sqlite',start_worker=False)
    with TestClient(app) as client:
        h=token(client)
        payload=dict(title='提示词交付规范',body='正式版本要求英文，维护版本允许中文。',status='confirmed',
            scope='project',project='提示词库',topic='提示词交付',scope_detail='仅适用于提示词库正式版本')
        kid=client.post('/api/knowledge',headers=h,json=payload).json()['id']
        with zipfile.ZipFile(io.BytesIO(client.get('/api/export-wiki',headers=h).content)) as archive:
            text=archive.read(f'projects/提示词库/K{kid:06d}.md').decode()
            assert 'scope: "project"' in text and 'topic: "提示词交付"' in text
            assert 'scope_detail: "仅适用于提示词库正式版本"' in text and 'ai_usable: true' in text
            assert '\n\n# 提示词交付规范\n\n' in text
        client.post('/api/knowledge/disable',headers=h,json={'knowledge_ids':[kid]})
        with zipfile.ZipFile(io.BytesIO(client.get('/api/export-wiki',headers=h).content)) as archive:
            text=archive.read(f'projects/提示词库/K{kid:06d}.md').decode()
            assert 'ai_usable: false' in text and '暂不供 AI 使用' in text and '停用' in text
