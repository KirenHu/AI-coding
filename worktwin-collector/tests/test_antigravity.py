from fastapi.testclient import TestClient
from worktwin.api import create_app
from tests.test_workbench_flows import auth


def test_antigravity_collects_only_markdown_artifacts_without_ai_consent(tmp_path,monkeypatch):
    root=tmp_path/'antigravity'
    monkeypatch.setattr('worktwin.config.antigravity_path',lambda:root)
    for relative,body in {'brain/session/implementation_plan.md':'# 计划\n审批先确认任务范围。',
        'knowledge/customer/content.md':'# 客户流程\n流程按角色办理。',
        'brain/session/log.jsonl':'{"message":"不采集"}',
        'brain/session/command.txt':'不采集', 'cache/plugin/SKILL.md':'不采集',
        'builtin/skills/x/SKILL.md':'不采集','conversations/private.db':'不采集'}.items():
        p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
    external=tmp_path/'external.md';external.write_text('不能越界采集')
    (root/'brain/session/link.md').symlink_to(external)
    app=create_app(tmp_path/'db',start_worker=False)
    with TestClient(app) as client:
        h=auth(client)
        row=client.put('/api/sources/assistants/antigravity',headers=h,json={'enabled':True}).json()
        assert row['selected'] and row['detected'] and not row['allow_ai']
        assert row['detection']['recognized']==2
        assert app.state.collector.scan_all()['new']==2
        assert app.state.collector.scan_all()['new']==0
        with app.state.db.connect() as con:
            assert con.execute('SELECT count(*) FROM ai_jobs').fetchone()[0]==0
            assert {r[0] for r in con.execute('SELECT file_type FROM documents')}=={'.md'}
        p=root/'brain/session/implementation_plan.md';p.write_text('# 更新计划\n确认后再办理。')
        assert app.state.collector.scan_all()['updated']==1
        client.put('/api/sources/assistants/antigravity',headers=h,json={'enabled':False}).raise_for_status()
        p.write_text('# 停止后不读取')
        assert app.state.collector.scan_all()['updated']==0


def test_antigravity_can_wait_for_first_artifact(tmp_path,monkeypatch):
    monkeypatch.setattr('worktwin.config.antigravity_path',lambda:tmp_path/'missing')
    app=create_app(tmp_path/'db',start_worker=False)
    with TestClient(app) as client:
        row=client.put('/api/sources/assistants/antigravity',headers=auth(client),json={'enabled':True}).json()
        assert row['selected'] and not row['detected']
        assert app.state.collector.scan_all()['errors']==0
