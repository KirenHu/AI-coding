import re
from pathlib import Path
from fastapi.testclient import TestClient
from worktwin.api import create_app
from worktwin.collector import Collector
from worktwin.db import Database


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('worktwin.collector.MAX_FILE_BYTES',100)
    root=tmp_path/'files';root.mkdir()
    (root/'large.md').write_text('# 大文件\n'+'正文。'*60)
    (root/'broken.docx').write_bytes(b'not a docx')
    (root/'unsupported.pptx').write_bytes(b'not supported')
    db=Database(tmp_path/'db.sqlite')
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,root) VALUES('资料','folder',?)",(str(root),))
    collector=Collector(db);collector.scan_all()
    return root,db,collector


def test_classification_and_one_time_processing(tmp_path,monkeypatch):
    root,db,collector=fixture(tmp_path,monkeypatch)
    with db.connect() as con:
        issues=[dict(r) for r in con.execute('SELECT * FROM source_file_issues')]
        assert {r['code'] for r in issues}=={'oversized','parse_failed'}
        issue=next(r for r in issues if r['code']=='oversized')
        con.execute('UPDATE sources SET enabled=0')
    collector.request_large_files(1,[issue['id']])
    # Survives a collector restart; a paused source is processed only for this request.
    collector=Collector(db)
    assert collector.scan_all()['new']==1
    with db.connect() as con:
        assert tuple(con.execute('SELECT enabled,allow_ai,allow_share FROM sources').fetchone())==(0,0,0)
        assert con.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]==1
        con.execute('UPDATE sources SET enabled=1')
    collector.scan_all()
    with db.connect() as con:
        assert not con.execute("SELECT 1 FROM source_file_issues WHERE code='oversized'").fetchone()
    (root/'large.md').write_text('# 新版本\n'+'新正文。'*80)
    collector.scan_all()
    with db.connect() as con:
        assert con.execute("SELECT 1 FROM source_file_issues WHERE code='oversized'").fetchone()
        assert '新正文' not in con.execute('SELECT content FROM documents').fetchone()[0]


def test_changed_or_missing_file_after_request(tmp_path,monkeypatch):
    root,db,collector=fixture(tmp_path,monkeypatch)
    with db.connect() as con:
        issue=con.execute("SELECT id FROM source_file_issues WHERE code='oversized'").fetchone()[0]
    collector.request_large_files(1,[issue])
    (root/'large.md').write_text('changed '*60)
    collector.scan_all()
    with db.connect() as con:
        assert con.execute('SELECT state FROM source_file_issues WHERE id=?',(issue,)).fetchone()[0]=='waiting'
        assert con.execute('SELECT count(*) FROM documents').fetchone()[0]==0
    collector.request_large_files(1,[issue])
    (root/'large.md').unlink()
    collector.scan_all()
    with db.connect() as con:
        assert con.execute('SELECT code FROM source_file_issues WHERE id=?',(issue,)).fetchone()[0]=='unreadable'


def test_api_selection_scope_and_auth(tmp_path,monkeypatch):
    root,db,_=fixture(tmp_path,monkeypatch)
    app=create_app(db.path,start_worker=False)
    with TestClient(app) as client:
        token=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)
        headers={'X-Worktwin-Token':token}
        result=client.get('/api/sources/1/file-issues',headers=headers).json()
        issue=next(f for f in result['files'] if f['code']=='oversized')
        assert issue['name']=='large.md' and 'path' not in issue
        url='/api/sources/1/process-large-files';payload={'issue_ids':[issue['id']]}
        assert client.post(url,json=payload).status_code==403
        assert client.post('/api/sources/999/process-large-files',json=payload,headers=headers).status_code==409
        assert client.post(url,json={'issue_ids':[9999]},headers=headers).status_code==409
        assert client.post(url,json=payload,headers=headers).json()=={'queued':1}
        assert client.get('/api/sources',headers=headers).json()[0]['file_issues']
        app.state.collector.scan_all()
        assert len(client.get('/api/documents',headers=headers).json())==1


def test_paused_legacy_status_only_reads_metadata(tmp_path,monkeypatch):
    root,db,_=fixture(tmp_path,monkeypatch)
    with db.connect() as con:
        con.execute('DELETE FROM source_file_issues')
        con.execute("UPDATE sources SET enabled=0,last_error='1 个文件解析失败'")
    Collector(db).scan_all()
    with db.connect() as con:
        assert con.execute('SELECT last_error FROM sources').fetchone()[0]=='1 个文件超过大小限制'
        assert con.execute('SELECT count(*) FROM documents').fetchone()[0]==0
