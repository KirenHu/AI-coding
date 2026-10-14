import json
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.source_detection import detect_transcripts


def test_detects_formats_without_indexing_or_calling_model(tmp_path, monkeypatch):
    root=tmp_path/'codex'/'sessions'
    root.mkdir(parents=True)
    monkeypatch.setattr('worktwin.source_detection.codex_sessions_path', lambda: root)
    (root/'log.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':'one'}})+'\n')
    (root/'unrelated.jsonl').write_text('{"hello":"world"}\n')
    assert detect_transcripts('codex')['recognized']==1
    assert detect_transcripts('claude',str(root))['recognized']==0
    app=create_app(tmp_path/'test.sqlite',start_worker=False)
    with TestClient(app) as client:
        headers={'X-Worktwin-Token':re.search(r'window.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)}
        result=client.post('/api/sources/detect',headers=headers,json={'kind':'codex'})
        assert result.status_code==200
        assert result.json()['recognized']==1
        assert client.get('/api/sources',headers=headers).json()==[]
        assert client.get('/api/stats',headers=headers).json()['documents']==0
        rejected=client.post('/api/sources',headers=headers,json={'kind':'claude','name':'Claude',
            'root':str(root),'validate_transcript':True})
        assert rejected.status_code==400
        accepted=client.post('/api/sources',headers=headers,json={'kind':'codex','name':'Codex',
            'root':str(root),'validate_transcript':True})
        assert accepted.status_code==200


def test_missing_empty_and_cursor_export(tmp_path):
    assert not detect_transcripts('codex',str(tmp_path/'missing'))['exists']
    assert detect_transcripts('codex',str(tmp_path))['recognized']==0
    (tmp_path/'cursor.jsonl').write_text(json.dumps({'type':'system','subtype':'init','session_id':'cursor-one'})+'\n')
    assert detect_transcripts('cursor',str(tmp_path))['recognized']==1
    assert detect_transcripts('codex',str(tmp_path))['recognized']==0
    (tmp_path/'claude.jsonl').write_text(json.dumps({'type':'user','sessionId':'claude-one',
        'message':{'role':'user','content':'hello'}})+'\n')
    assert detect_transcripts('claude',str(tmp_path))['recognized']==1
