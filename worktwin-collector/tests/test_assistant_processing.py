import json
import pytest
from fastapi import HTTPException
from tests.test_ingest import setup, write, codex, read_state
from worktwin.assistant_sources import start_new_processing, request_history
from worktwin.ingest import pending_batch, ack_batch


def test_history_is_explicit_bounded_and_does_not_rewind_live_cursor(tmp_path):
    path,db,collector=setup(tmp_path)
    write(path,[codex('旧的项目决定需要两人审批',minute=1),codex('旧的项目决定已经确认',role='assistant',minute=2)])
    collector.scan_all()
    with db.connect() as con:
        start_new_processing(con,1)
    live=read_state(db)['processed_seq']
    with db.connect() as con:
        assert pending_batch(con,1)['content']==''
        result=request_history(con,1,'2026-10-10','2026-10-10')
        assert result['events']==2
        batch=pending_batch(con,1)
        assert batch['history'] and '两人审批' in batch['content']
        assert ack_batch(con,1,batch['through_seq'],batch['generation'])
        assert con.execute('SELECT COUNT(*) FROM assistant_history_requests').fetchone()[0]==0
        assert con.execute('SELECT processed_seq FROM transcript_streams').fetchone()[0]==live


def test_old_events_discovered_later_are_not_sent_to_model(tmp_path):
    path,db,collector=setup(tmp_path)
    with db.connect() as con:start_new_processing(con,1)
    write(path,[codex('历史中的不应发送内容')])
    collector.scan_all()
    with db.connect() as con:
        assert pending_batch(con,1)['content']==''
        assert con.execute('SELECT processed_seq=captured_seq FROM transcript_streams').fetchone()[0]


def test_native_task_boundary_keeps_confirmation_with_proposal(tmp_path):
    path,db,collector=setup(tmp_path)
    events=[dict(type='event_msg',payload={'type':'task_started','turn_id':'one'})]
    events += [codex('方案：审批需要一名主管',role='assistant',minute=i) for i in range(3)]
    events += [codex('同意这个方案',minute=4),dict(type='event_msg',payload={'type':'task_complete','turn_id':'one'})]
    events += [codex('另一个任务不应混入',minute=5)]
    write(path,events);collector.scan_all()
    with db.connect() as con:
        batch=pending_batch(con,1,max_events=2,max_chars=20)
        assert '一名主管' in batch['content'] and '同意这个方案' in batch['content']
        assert '另一个任务' not in batch['content']


def test_history_requires_permission_and_valid_range(tmp_path):
    _,db,_=setup(tmp_path)
    with db.connect() as con:
        with pytest.raises(HTTPException):request_history(con,1,'2026-10-11','2026-10-10')
        con.execute('UPDATE sources SET allow_ai=0')
        with pytest.raises(HTTPException):request_history(con,1,'2026-10-10','2026-10-11')


def test_rule_files_remain_scoped_references_and_do_not_call_model(tmp_path):
    from worktwin.db import Database
    from worktwin.collector import Collector
    from worktwin.jobs import KnowledgeWorker
    from tests.support import FakeModel
    root=tmp_path/'project';root.mkdir()
    (root/'AGENTS.md').write_text('本项目的回复必须中文。此规则不适用于其他项目。')
    (root/'SKILL.md').write_text('使用这个技能时先阅读接口说明，不代表已经执行。')
    vendor=root/'plugins/cache/example';vendor.mkdir(parents=True)
    (vendor/'SKILL.md').write_text('内置模板不应被采集为个人知识')
    db=Database(tmp_path/'reference.sqlite')
    with db.connect() as con:con.execute("INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES('项目','folder','folder',?,1)",(str(root),))
    Collector(db).scan_all();model=FakeModel();worker=KnowledgeWorker(db,client=model)
    worker.process_next();worker.process_next()
    with db.connect() as con:
        refs=con.execute('SELECT * FROM assistant_references').fetchall()
        assert len(refs)==2 and all(r['scope_path']==str(root) for r in refs)
        assert con.execute('SELECT count(*) FROM knowledge').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM documents').fetchone()[0]==2
    assert not model.requests


def test_native_active_task_waits_until_end_without_losing_cursor(tmp_path):
    path,db,collector=setup(tmp_path)
    write(path,[dict(type='event_msg',payload={'type':'task_started','turn_id':'one'}),codex('请设计审批机制')])
    collector.scan_all()
    with db.connect() as con:
        batch=pending_batch(con,1)
        assert batch['waiting_for_task'] and not batch['content']
        assert con.execute('SELECT processed_seq FROM transcript_streams').fetchone()[0]==0
    write(path,[codex('采用一人审批，最终确认',minute=1),dict(type='event_msg',payload={'type':'task_complete','turn_id':'one'})],'a')
    collector.scan_all()
    with db.connect() as con:
        batch=pending_batch(con,1)
        assert '设计审批机制' in batch['content'] and '最终确认' in batch['content']


def test_typical_long_task_is_read_together_for_confirmation():
    from worktwin.inference import extract_knowledge
    class Reader:
        calls=[]
        def chat(self,messages,max_tokens=1800):
            self.calls.append(messages)
            return '{"items":[]}'
    client=Reader()
    text='### AI · 2026-10-10 10:00:00\n建议采用一名主管审批。\n'+('实施背景与操作过程。'*2000)+'\n### 用户 · 2026-10-10 10:10:00\n同意这个方案。'
    extract_knowledge(text,transcript=True,client=client)
    assert len(client.calls)==1
    assert '一名主管审批' in client.calls[0][-1]['content'] and '同意这个方案' in client.calls[0][-1]['content']
