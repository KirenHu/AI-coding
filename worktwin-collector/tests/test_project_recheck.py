"""Project decisions must follow live source evidence in both directions."""
import hashlib
import json

from worktwin.db import Database
from worktwin.knowledge import store_candidates
from worktwin.projects import apply_assignments, catalog, plan_work_units
from worktwin.project_recheck import ProjectRechecker
from worktwin.twin_access import effective_notes


class Judge:
    def __init__(self, *, conflict=False):
        self.calls = []
        self.conflict = conflict

    def compare(self, work, target):
        self.calls.append((work, target))
        return {'same':0 if self.conflict else .99,
                'conflict':.99 if self.conflict else 0, 'engine':'test-judge'}


def source_document(con, name, text):
    source = con.execute("""INSERT INTO sources(name,kind,root,allow_ai,allow_share)
        VALUES(?,'folder',?,1,1)""", (name, '/tmp/'+name)).lastrowid
    doc = con.execute("""INSERT INTO documents(source_id,path,relative_path,title,
        file_type,project,content,sha256,size_bytes,mtime_ns,scope)
        VALUES(?,?,?,?,'.md',?,?,?,100,1,'session')""",
        (source, '/tmp/'+name+'/work.md','work.md','工作记录',name,text,
         hashlib.sha256(text.encode()).hexdigest())).lastrowid
    con.execute('UPDATE documents SET project_key=? WHERE id=?', ('session:'+str(doc),doc))
    return source, doc


def item_for(quote, hint='WorkTwin', **extra):
    return dict(kind='decision', title='知识维护流程', body=quote, quote=quote,
        project_hint=hint, topic='知识维护流程', scope_detail='工作项目', quality='useful',
        attribution='user', outcome='none', extraction_version=1, **extra)


def add_work(con, doc, item, judge=None):
    document = con.execute('SELECT * FROM documents WHERE id=?',(doc,)).fetchone()
    link = plan_work_units([item],document,catalog(con),judge or Judge())[0]
    item.update({k:link[k] for k in ('scope','project','project_key')})
    store_candidates(con,doc,[],[item],created_by='enterprise_ai')
    apply_assignments(con,doc,[item],[link])
    kid = con.execute('SELECT knowledge_id FROM knowledge_evidence WHERE document_id=? AND quote=?',
                      (doc,item['quote'])).fetchone()[0]
    con.execute("UPDATE knowledge SET status='confirmed',needs_review=0 WHERE id=?",(kid,))
    return kid, link


def replace_content(con, doc, text):
    con.execute('UPDATE documents SET content=?,sha256=? WHERE id=?',
                (text,hashlib.sha256(text.encode()).hexdigest(),doc))


def test_lost_origin_withdraws_matches_without_self_reinforcement_and_can_recover(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    origin = 'WorkTwin 项目明确采用可持续更新的工作知识库。'
    follow = 'WorkTwin 项目补充按来源维护知识的规则。'
    with db.connect() as con:
        source, first = source_document(con,'origin',origin)
        seed, link = add_work(con,first,item_for(origin))
        _, second = source_document(con,'follow',follow)
        note, adopted = add_work(con,second,item_for(follow))
        assert adopted['project_key'] == link['project_key']
        twin = con.execute("INSERT INTO twins(name,knowledge_mode) VALUES('交接','dynamic')").lastrowid
        con.execute("INSERT INTO twin_grants(twin_id,subject_type,subject_key) VALUES(?,'project',?)",
                    (twin,link['project_key']))
        assert {k['id'] for k in effective_notes(con,twin)} == {seed,note}
    recheck = ProjectRechecker(db,Judge())
    recheck.process_next()
    with db.connect() as con:
        con.execute('UPDATE sources SET enabled=0 WHERE id=?',(source,))
    assert recheck.process_next()['withdrawn'] == 2
    for _ in range(3):
        recheck.process_next()
    with db.connect() as con:
        assert not catalog(con)
        assert effective_notes(con,twin) == []
        assert con.execute('SELECT count(*) FROM project_memberships').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM twin_grants').fetchone()[0] == 1
        assert {r[0] for r in con.execute('SELECT scope FROM knowledge')} == {'session'}
        con.execute('UPDATE sources SET enabled=1 WHERE id=?',(source,))
    assert recheck.process_next()['updated'] == 2
    with db.connect() as con:
        assert {k['id'] for k in effective_notes(con,twin)} == {seed,note}
        assert {r[0] for r in con.execute('SELECT project_key FROM knowledge')} == {link['project_key']}
        assert con.execute('SELECT count(*) FROM twin_grants').fetchone()[0] == 1
        assert con.execute('SELECT version FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 3
    assert recheck.process_next()['updated'] == 0
    assert recheck.process_next()['state'] == 'idle'


def test_changed_reference_rechecks_even_while_other_project_support_is_unchanged(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    origin = 'WorkTwin 项目明确选择保留知识历史。'
    follow = 'WorkTwin 项目补充知识回退方式。'
    with db.connect() as con:
        _, first = source_document(con,'stable',origin)
        _, link = add_work(con,first,item_for(origin))
        _, second = source_document(con,'changing',follow)
        note, _ = add_work(con,second,item_for(follow))
    recheck = ProjectRechecker(db,Judge())
    recheck.process_next()
    assert recheck.process_next()['state'] == 'idle'
    with db.connect() as con:
        before = catalog(con)
        replace_content(con,second,'现在记录的是另一项工作，原引用已删除。')
        assert catalog(con) == before  # New trigger is source evidence, not catalog.
    result = recheck.process_next()
    assert result['withdrawn'] == 1
    with db.connect() as con:
        updated = con.execute('SELECT * FROM knowledge WHERE id=?',(note,)).fetchone()
        assert updated['scope'] == 'session'
        assert updated['body'] == follow
        assert not con.execute('SELECT 1 FROM project_memberships WHERE knowledge_id=?',(note,)).fetchone()


def test_explicit_new_contradiction_withdraws_an_old_model_match(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        _, one = source_document(con,'first','WorkTwin 项目为甲公司提供内部知识服务。')
        _, link = add_work(con,one,item_for('WorkTwin 项目为甲公司提供内部知识服务。'))
        _, two = source_document(con,'second','WorkTwin 项目有乙公司的独立知识服务。')
        note, _ = add_work(con,two,item_for('WorkTwin 项目有乙公司的独立知识服务。'))
    result = ProjectRechecker(db,Judge(conflict=True)).process_next()
    assert result['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'
        assert len(catalog(con)) == 1


def test_reextracting_original_source_retains_identity_without_self_match(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    quote = 'WorkTwin 项目确定每条知识保留必要的来源引用。'
    with db.connect() as con:
        _, doc = source_document(con,'reextract',quote)
        _, original = add_work(con,doc,item_for(quote))
        judge = Judge(conflict=True)
        _, repeated = add_work(con,doc,item_for(quote),judge)
        assert repeated['project_key'] == original['project_key']
        assert repeated['identity_kind'] == 'explicit'
        assert judge.calls == []
        assert catalog(con)[0]['project_key'] == original['project_key']
    assert ProjectRechecker(db,Judge(conflict=True)).process_next()['updated'] == 0


def test_previous_explicit_turn_nominates_project_but_shared_cwd_does_not(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    seed = 'WorkTwin 项目需要低成本维护可追溯的知识。'
    quote = '继续这个方案，把自动知识更新后的回退交互补齐。'
    with db.connect() as con:
        _, first = source_document(con,'known',seed)
        _, original = add_work(con,first,item_for(seed))
        _, doc = source_document(con,'continuation',quote)
        document = con.execute('SELECT * FROM documents WHERE id=?',(doc,)).fetchone()
        context = {'cwd':'/repo/shared', 'events':[
            {'role':'user','text':'接下来继续 WorkTwin 项目的知识更新交互。','event_id':'prior'},
            {'role':'user','text':quote,'event_id':'now'}]}
        judge = Judge()
        continued = plan_work_units([item_for(quote,'',work_context=context)],
                                    document,catalog(con),judge)[0]
        assert continued['project_key'] == original['project_key']
        assert continued['identity_kind'] == 'context'
        assert 'WorkTwin' in judge.calls[0][0]['preceding_work']
        unrelated = plan_work_units([item_for(quote,'',work_context={'cwd':'/repo/shared',
            'repositories':['github.com/acme/shared'],'events':[]})],document,catalog(con),Judge())[0]
        assert unrelated['scope'] == 'session'
        rejected = plan_work_units([item_for(quote,'',work_context=context)],
                                  document,catalog(con),Judge(conflict=True))[0]
        assert rejected['scope'] == 'session'


def test_context_grounding_is_retained_and_loss_shrinks_scope(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    context = '这一项属于 WorkTwin 项目。'
    quote = '知识更新后应该可以恢复以前版本，同时保留完整变更记录。'
    with db.connect() as con:
        _, doc = source_document(con,'context',context+'\n'+quote)
        note, _ = add_work(con,doc,item_for(quote,context_quote=context))
        stored = json.loads(con.execute('SELECT evidence_json FROM work_units').fetchone()[0])
        assert stored['context_quote'] == context
        replace_content(con,doc,quote)
    result = ProjectRechecker(db,Judge()).process_next()
    assert result['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'


def test_continuous_turn_can_match_anchored_project_and_is_rechecked_from_live_turns(tmp_path):
    from worktwin.artifacts import work_context
    db = Database(tmp_path/'db.sqlite')
    seed = 'WorkTwin 项目维护 https://github.com/kiren/worktwin 的知识流程。'
    prior = '接下来继续 WorkTwin 项目的知识更新设计。'
    quote = '把刚才的自动更新方案加入回退功能。'
    transcript = '### 用户 · 2026-10-10T08:00:00Z\n'+prior+'\n\n### 用户 · 2026-10-10T08:01:00Z\n'+quote
    with db.connect() as con:
        _, one = source_document(con,'anchor',seed)
        _, target = add_work(con,one,item_for(seed))
        _, two = source_document(con,'turns',transcript)
        context = work_context(con,two,quote)
        note, adopted = add_work(con,two,item_for(quote,'',work_context=context))
        assert adopted['project_key'] == target['project_key']
        assert adopted['identity_kind'] == 'context'
    rechecker = ProjectRechecker(db,Judge())
    assert rechecker.process_next()['updated'] == 0
    with db.connect() as con:
        replace_content(con,two,'### 用户 · 2026-10-10T08:01:00Z\n'+quote)
    assert rechecker.process_next()['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'
        replace_content(con,two,transcript)
    assert rechecker.process_next()['updated'] == 1
    with db.connect() as con:
        assert con.execute('SELECT project_key FROM knowledge WHERE id=?',(note,)).fetchone()[0] == target['project_key']


def test_multi_source_note_keeps_project_when_another_current_work_supports_it(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    first = 'WorkTwin 项目采用持续维护知识的方案。'
    second = 'WorkTwin 项目会保留知识的版本记录。'
    with db.connect() as con:
        _, one = source_document(con,'multi-origin',first)
        note, target = add_work(con,one,item_for(first))
        _, two = source_document(con,'multi-other',second)
        _, _ = add_work(con,two,item_for(second))
        con.execute('INSERT INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)',
                    (note,two,second))
        replace_content(con,two,'当前资料已不再包含第二条旧引用。')
    assert ProjectRechecker(db,Judge()).process_next()['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT project_key FROM knowledge WHERE id=?',(note,)).fetchone()[0] == target['project_key']
        assert con.execute('SELECT version FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 1


def test_stale_reference_flag_withdraws_without_reusing_quote_still_in_source(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    quote = 'WorkTwin 项目确定一个旧的知识更新方案。'
    with db.connect() as con:
        _, doc = source_document(con,'superseded',quote)
        note, _ = add_work(con,doc,item_for(quote))
    recheck = ProjectRechecker(db,Judge())
    recheck.process_next()
    with db.connect() as con:
        con.execute('UPDATE knowledge_evidence SET superseded=1 WHERE knowledge_id=?',(note,))
    assert recheck.process_next()['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'
        assert catalog(con) == []


def test_fork_parent_supplies_live_candidates_without_becoming_child_citation(tmp_path):
    from worktwin.artifacts import work_context
    db = Database(tmp_path/'db.sqlite')
    seed = 'WorkTwin 项目决定持续维护用户知识库。'
    parent_quote = '现在继续 WorkTwin 项目的知识版本功能。'
    child_quote = '沿用刚才的方案，补上回退按钮和操作提示。'
    with db.connect() as con:
        _, origin = source_document(con,'fork-origin',seed)
        _, target = add_work(con,origin,item_for(seed))
        parent_source, parent = source_document(con,'fork-parent',parent_quote)
        child_source, child = source_document(con,'fork-child',child_quote)
        for doc, source, session, parent_id, quote, stamp in (
            (parent,parent_source,'parent-session','',parent_quote,'2026-10-10 09:00:00'),
            (child,child_source,'child-session','parent-session',child_quote,'2026-10-10 10:00:00')):
            con.execute("""INSERT INTO transcript_streams(document_id,source_id,adapter,session_id,parent_session_id)
                VALUES(?,?,'codex',?,?)""",(doc,source,session,parent_id))
            con.execute("""INSERT INTO transcript_events(document_id,generation,event_id,byte_offset,
                kind,role,timestamp,text) VALUES(?,1,?,0,'message','user',?,?)""",
                (doc,session+'-event',stamp,quote))
        note, assigned = add_work(con,child,item_for(child_quote,'',
                                                   work_context=work_context(con,child,child_quote)))
        assert assigned['project_key'] == target['project_key']
        references = json.loads(con.execute('SELECT evidence_json FROM work_units WHERE document_id=?',
                                           (child,)).fetchone()[0])['context_sources']
        assert {ref['document_id'] for ref in references} == {parent,child}
        assert {r[0] for r in con.execute('SELECT document_id FROM knowledge_evidence WHERE knowledge_id=?',
                                         (note,))} == {child}
    recheck = ProjectRechecker(db,Judge())
    assert recheck.process_next()['updated'] == 0
    with db.connect() as con:
        con.execute('UPDATE sources SET allow_ai=0 WHERE id=?',(parent_source,))
    assert recheck.process_next()['withdrawn'] == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'
        con.execute('UPDATE sources SET allow_ai=1 WHERE id=?',(parent_source,))
    assert recheck.process_next()['updated'] == 1
    with db.connect() as con:
        assert con.execute('SELECT project_key FROM knowledge WHERE id=?',(note,)).fetchone()[0] == target['project_key']
        con.execute("UPDATE transcript_streams SET parent_session_id='' WHERE document_id=?",(child,))
    assert recheck.process_next()['withdrawn'] == 1


def test_same_repository_and_display_name_still_requires_business_identity_match(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    a = 'Atlas 项目为甲客户维护 https://github.com/acme/monorepo 内的采购业务。'
    b = 'Atlas 项目为乙客户维护 https://github.com/acme/monorepo 内的薪酬业务。'
    with db.connect() as con:
        _, one = source_document(con,'customer-a',a)
        _, target = add_work(con,one,item_for(a,'Atlas'))
        _, two = source_document(con,'customer-b',b)
        judge = Judge(conflict=True)
        document = con.execute('SELECT * FROM documents WHERE id=?',(two,)).fetchone()
        assignment = plan_work_units([item_for(b,'Atlas')],document,catalog(con),judge)[0]
        assert judge.calls and assignment['scope'] == 'session'
        assert assignment['project_key'] != target['project_key']


def test_old_same_repo_match_can_be_withdrawn_by_business_identity_recheck(tmp_path):
    db = Database(tmp_path/'db.sqlite')
    a = 'Atlas 项目为甲客户维护 https://github.com/acme/monorepo 的采购流程。'
    b = 'Atlas 项目为乙客户维护 https://github.com/acme/monorepo 的薪酬流程。'
    with db.connect() as con:
        _, one = source_document(con,'legacy-customer-a',a)
        _, target = add_work(con,one,item_for(a,'Atlas'))
        _, two = source_document(con,'legacy-customer-b',b)
        note, old_match = add_work(con,two,item_for(b,'Atlas'),Judge())
        assert old_match['project_key'] == target['project_key']
    judge = Judge(conflict=True)
    result = ProjectRechecker(db,judge).process_next()
    assert result['withdrawn'] == 1 and len(judge.calls) == 1
    with db.connect() as con:
        assert con.execute('SELECT scope FROM knowledge WHERE id=?',(note,)).fetchone()[0] == 'session'
        assert catalog(con)[0]['identity_supports'][0]['document_id'] == one
