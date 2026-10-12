"""Topic organization keeps evidence, versions and existing access decisions."""
import json

from worktwin.articles import detail_parts, migrate, organize_batch
from worktwin.inference import extract_knowledge
from worktwin.knowledge import store_candidates
from worktwin.knowledge_policy import READY_SQL
from worktwin.reconcile import make_consolidation_plan, resolve_proposal, store_proposals
from tests.test_automation import setup


class Model:
    def __init__(self, items):
        self.items = items
        self.requests = []

    def chat(self, messages, max_tokens):
        self.requests.append((messages, max_tokens))
        return json.dumps({'items': self.items}, ensure_ascii=False)


def parts(item):
    first = item | {'scope':'project','project_key':'project-worktwin','project':'WorkTwin'}
    second = first | {'kind':'process','title':'知识分配步骤',
        'body':'新增说明：当前知识允许分配给数字分身。',
        'quote':'新增说明：当前知识允许分配给数字分身。',
        'scope_detail':'WorkTwin知识库的分身分配步骤'}
    return [first, second]


def group():
    return {'indices':[0,1], 'compatible_conditions':True, 'kind':'process',
        'title':'知识维护与分配指南', 'topic':'知识更新',
        'scope_detail':'WorkTwin知识库；维护结论与分配给分身',
        'body':'## 当前结论\n项目只维护当前结论，旧正文存入版本历史。\n\n## 分配知识\n当前知识允许分配给数字分身。'}


def test_same_batch_combines_types_and_keeps_each_source_part(tmp_path):
    db, did, item = setup(tmp_path)
    model = Model([group()])
    organized = organize_batch(model, parts(item))
    assert len(organized) == 1 and len(organized[0]['parts']) == 2
    with db.connect() as con:
        con.execute('DELETE FROM knowledge')
        assert store_candidates(con,did,[],organized,created_by='enterprise_ai') == 1
        article = con.execute('SELECT * FROM knowledge').fetchone()
        assert article['body'] == group()['body'] and article['organization'] == 'article'
        assert {row['quote'] for row in detail_parts(con,article['id'])} == {p['quote'] for p in parts(item)}
        assert con.execute('SELECT count(*) FROM knowledge_evidence').fetchone()[0] == 2
        assert store_candidates(con,did,[],organized,created_by='enterprise_ai') == 0
        assert con.execute('SELECT count(*) FROM article_parts').fetchone()[0] == 2


def test_grouping_does_not_cross_project_or_attribution_boundaries():
    item = {'scope':'project','project_key':'project-a','attribution':'user','outcome':'none',
        'kind':'decision','topic':'审批','title':'审批规则','body':'审批规则必须满足业务要求。',
        'scope_detail':'采购工作','quote':'审批规则必须满足业务要求。'}
    items = [item, item | {'project_key':'project-b'},
             item | {'attribution':'assistant'}, item | {'outcome':'reported'}]
    model = Model([group()])
    assert organize_batch(model,items) == items
    assert model.requests == []


def test_incompatible_conditions_stay_independent():
    item = {'scope':'project','project_key':'project-a','attribution':'user','outcome':'none',
        'kind':'decision','topic':'审批','title':'审批规则','body':'审批规则必须满足业务要求。',
        'scope_detail':'正式生产','quote':'审批规则必须满足业务要求。'}
    items = [item,item | {'scope_detail':'调试环境','body':'调试时允许跳过审批。'}]
    assert organize_batch(Model([]),items) == items


def test_article_update_keeps_id_history_and_all_new_quotes(tmp_path):
    db, did, item = setup(tmp_path)
    with db.connect() as con:
        old = con.execute('SELECT * FROM knowledge').fetchone()
        con.execute("UPDATE knowledge SET status='confirmed',organization='material'")
        merged = organize_batch(Model([group()]),parts(item))[0]
        plan = {0:{'target_id':old['id'],'action':'enrich','title':group()['title'],
            'body':group()['body'],'reason':'同一工作中的维护规则和分配步骤',
            'compatible_conditions':True,'changes_existing_conclusion':False}}
        assert store_proposals(con,did,'source-sha',[merged],plan) == 1
        proposal = con.execute('SELECT id FROM knowledge_proposals').fetchone()[0]
        resolve_proposal(con,proposal,accept=True)
        current = con.execute('SELECT * FROM knowledge').fetchone()
        assert current['id'] == old['id'] and current['version'] == old['version'] + 1
        assert current['organization'] == 'article'
        assert con.execute('SELECT body FROM knowledge_history').fetchone()[0] == old['body']
        assert len(detail_parts(con,current['id'])) == 2
        assert con.execute('SELECT count(*) FROM knowledge_evidence').fetchone()[0] == 2


def test_legacy_organization_migration_keeps_access_and_content(tmp_path):
    db, _, _ = setup(tmp_path)
    with db.connect() as con:
        con.execute("UPDATE knowledge SET status='confirmed'")
        kid = con.execute('SELECT id FROM knowledge').fetchone()[0]
        twin = con.execute("INSERT INTO twins(name) VALUES('助手')").lastrowid
        con.execute('INSERT INTO twin_knowledge(twin_id,knowledge_id) VALUES(?,?)',(twin,kid))
        con.execute('ALTER TABLE knowledge DROP COLUMN organization')
        before = dict(con.execute('SELECT * FROM knowledge').fetchone())
        usable = [row[0] for row in con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL)]
        migrate(con)
        after = dict(con.execute('SELECT * FROM knowledge').fetchone())
        assert after.pop('organization') == 'material'
        assert after == before
        assert [row[0] for row in con.execute('SELECT k.id FROM knowledge k WHERE '+READY_SQL)] == usable
        assert con.execute('SELECT knowledge_id FROM twin_knowledge').fetchone()[0] == kid
        assert con.execute('SELECT count(*) FROM ai_jobs').fetchone()[0] == 0
        con.execute("UPDATE knowledge SET organization='article'")
        migrate(con)
        assert con.execute('SELECT organization FROM knowledge').fetchone()[0] == 'article'


def test_long_article_is_preserved_and_synonymous_topic_can_update():
    body = ('## 操作说明\n' + ('每次执行前检查当前配置，并在完成后保存结果。\n' * 180)).strip()
    item = {'kind':'process','title':'配置维护指南','body':body,
        'quote':'每次执行前检查当前配置，并在完成后保存结果。',
        'topic':'配置维护','scope_detail':'本项目配置','value_reason':'供后续执行配置维护时查阅',
        'attribution':'document','outcome':'none','requires_review':False}
    model = Model([item])
    result = extract_knowledge(item['quote'],transcript=False,client=model)
    assert result[0]['body'] == body and len(body)>3500
    assert model.requests[0][1] == 12000
    known = [dict(id=42,kind='decision',topic='配置管理',scope_detail='本项目配置',
        title='配置管理指南',body='## 管理要求\n检查当前配置。')]
    combined = known[0]['body']+'\n\n'+body
    response = Model([{'index':0,'target_id':42,'action':'enrich','title':'配置管理指南',
        'body':combined,'reason':'配置管理与配置维护是同一主题','same_topic':True,
        'compatible_conditions':True,'changes_existing_conclusion':False}])
    plan = make_consolidation_plan(response,result,known)
    assert plan[0]['target_id'] == 42 and plan[0]['body'] == combined
    assert json.loads(response.requests[-1][0][-1]['content'])['new_items'][0]['body'] == body


def test_extraction_keeps_materials_for_periodic_article_writer(tmp_path):
    from tests.support import FakeModel
    from tests.test_incremental_jobs import setup_session, user_message
    from worktwin.jobs import KnowledgeWorker

    first = '最终决定采用统一审批节点，以便多个业务流程共享同一套审批规则。'
    second = '审批节点配置完成后先检查执行条件，再测试不同金额对应的审批流程。'
    db, _, _ = setup_session(tmp_path,[user_message(first),user_message(second,2)])

    class ArticleModel(FakeModel):
        def chat(self,messages,max_tokens=2400):
            content = messages[-1]['content']
            if '"batch_items"' in content:
                self.requests.append(messages)
                return json.dumps({'items':[{
                    'indices':[0,1],'compatible_conditions':True,'kind':'decision',
                    'title':'审批规则配置指南','topic':'工作方案',
                    'scope_detail':'仅适用于本资料描述的工作',
                    'body':'## 审批规则\n'+first+'\n\n## 检查步骤\n'+second}]},ensure_ascii=False)
            return super().chat(messages,max_tokens)

    result = KnowledgeWorker(db,client=ArticleModel()).process_next()
    assert result['state']=='done' and result['candidates']==2
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0]==2
        assert con.execute('SELECT count(*) FROM work_units').fetchone()[0]==2
        assert {row[0] for row in con.execute('SELECT organization FROM knowledge')}=={'material'}
        assert {row[0] for row in con.execute('SELECT quote FROM article_parts')}=={first,second}
        assert {row[0] for row in con.execute('SELECT occurred_at FROM knowledge_evidence')} == {
            '2026-10-10 10:00:01','2026-10-10 10:00:02'}


def test_two_full_long_articles_fit_gateway_without_sending_other_bodies():
    from worktwin.provider import ChatRequest

    old_body = '旧结论：审批仍然按业务金额分级。\n' + '已有规则说明。' * 7000
    new_body = '新增说明：执行完成后保存审批记录。\n' + '新的操作说明。' * 7000
    known = [{'id':i,'kind':'process','topic':'审批管理','title':'审批管理指南',
              'scope_detail':'业务审批','body':old_body} for i in range(1,6)]
    item = {'kind':'process','topic':'审批管理','title':'审批管理补充',
            'scope_detail':'业务审批','body':new_body,'attribution':'document'}

    class LocateAndCompose(Model):
        def chat(self,messages,max_tokens):
            self.requests.append((messages,max_tokens))
            request = json.loads(messages[-1]['content'])
            ChatRequest(messages=messages,max_tokens=max_tokens)
            assert sum(len(m['content']) for m in messages) < 128000
            if len(request['existing']) == 5:
                assert all('body' not in article for article in request['existing'])
                return json.dumps({'items':[{'index':0,'target_id':3,'same_topic':True}]})
            assert request['existing'][0]['id']==3
            assert request['existing'][0]['body']==old_body
            assert request['new_items'][0]['body']==new_body
            return json.dumps({'items':[{'index':0,'target_id':3,'action':'replace',
                'title':'审批管理指南','body':'旧结论：审批仍然按业务金额分级。\n\n新增说明：执行完成后保存审批记录。',
                'reason':'保留审批结论并整合执行结果记录','changes_existing_conclusion':True}]})

    model = LocateAndCompose([])
    result = make_consolidation_plan(model,[item],known)
    assert len(model.requests)==2
    assert result[0]['target_id']==3 and '审批仍然按业务金额分级' in result[0]['body']
    assert len(old_body)>48000 and len(new_body)>48000


def test_large_batch_continues_same_topic_without_losing_parts():
    from worktwin.provider import ChatRequest

    body = '审批配置规则。' * 6900
    items = [{'scope':'project','project_key':'workflow','attribution':'document','outcome':'none',
        'kind':'process','topic':'审批管理','title':'审批管理指南','scope_detail':'业务审批',
        'body':body,'quote':f'第{i}份依据说明审批规则。'} for i in range(5)]

    class MergePages(Model):
        def chat(self,messages,max_tokens):
            self.requests.append((messages,max_tokens))
            ChatRequest(messages=messages,max_tokens=max_tokens)
            assert sum(len(m['content']) for m in messages) < 128000
            parts = json.loads(messages[-1]['content'])['batch_items']
            return json.dumps({'items':[{'indices':[p['index'] for p in parts],
                'compatible_conditions':True,'kind':'process','topic':'审批管理',
                'title':'审批管理指南','scope_detail':'业务审批','body':body}]})

    model = MergePages([])
    result = organize_batch(model,items)
    assert len(model.requests)>1 and len(result)==1
    assert {part['quote'] for part in result[0]['parts']} == {item['quote'] for item in items}
