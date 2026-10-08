"""Regression tests for verifiable personal knowledge and project decision timelines."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.knowledge import candidates_from_text, user_turns
from worktwin.inference import extract_knowledge
from tests.support import run_ai,FakeModel


def credentials(client):
    token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text).group(1)
    return {"X-Worktwin-Token": token}


def test_user_speech_attribution_preserves_original_decision_time():
    transcript = ("工作目录：/work/demo\n\n"
                  "### 用户 · 2026-03-03 09:03:05\n我们最终决定使用工作流节点承载审批，减少重复配置。\n\n"
                  "### AI · 2026-03-03 09:04:00\n我建议直接删除所有审批设置，以减少工作量。\n\n"
                  "### 用户 · 2026-03-04 10:01:00\n我倾向于先确认清楚项目的业务边界，再安排具体的开发计划。")
    turns = user_turns(transcript)
    assert len(turns) == 2
    extracted = candidates_from_text(transcript, transcript=True)
    assert len(extracted) == 2
    assert extracted[0]["occurred_at"] == "2026-03-03 09:03:05"
    assert extracted[1]["occurred_at"] == "2026-03-04 10:01:00"
    assert all("删除所有审批设置" not in row["body"] for row in extracted)


def test_enterprise_extraction_never_reads_assistant_as_employee():
    transcript=("工作目录：/work/demo\n\n"
         "### 用户 · 2026-03-03 09:03:05\n我倾向于先完成准确的资料采集，再建立复杂的知识组织能力。\n\n"
         "### AI · 2026-03-03 09:03:20\n我建议放弃知识溯源，这是最好的方案。")
    fake=FakeModel()
    result=extract_knowledge(transcript,transcript=True,client=fake)
    assert len(result)==1
    assert result[0]['kind']=='preference'
    assert all('放弃知识溯源' not in str(x) for x in fake.requests)


def test_manual_evidence_requires_verbatim_quote(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    statement = "我们最终决定把审批能力做成独立的流程节点，保留最少配置。"
    (directory / "decisions.md").write_text(statement, encoding="utf-8")
    app = create_app(tmp_path / "worktwin.sqlite", start_worker=False)
    with TestClient(app) as client:
        h = credentials(client)
        assert client.get("/").headers["X-Frame-Options"] == "DENY"
        assert client.get("/").headers["Cache-Control"] == "no-store"
        client.post("/api/sources", headers=h, json={"name": "work", "root": str(directory)})
        assert app.state.collector.scan_all()["new"] == 1
        doc_id = client.get("/api/documents", headers=h).json()[0]["id"]
        manual = {"kind": "decision", "title": "可核查的人工知识", "body": "审批作为流程节点", "status": "confirmed", "document_id": doc_id}
        assert client.post("/api/knowledge", headers=h, json=manual).status_code == 400
        manual["quote"] = "虚构的来源引用内容"
        assert client.post("/api/knowledge", headers=h, json=manual).status_code == 400
        manual["quote"] = statement
        created = client.post("/api/knowledge", headers=h, json=manual)
        assert created.status_code == 200
        item_id = created.json()["id"]
        source = next(k for k in client.get("/api/knowledge", headers=h).json() if k["id"] == item_id)
        assert source["evidence"][0]["chunk_id"] is not None
        assert source["evidence"][0]["is_current"] == 1


def test_project_timeline_uses_original_turn_time_and_json_exports_evidence(tmp_path):
    sessions = tmp_path / "codex" / "sessions" / "2026" / "03"
    sessions.mkdir(parents=True)
    data = [
        {"type": "session_meta", "payload": {"cwd": "/company/Approvals", "id": "sess-1"}},
        {"type": "response_item", "timestamp": "2026-03-01T09:12:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "我们最终决定先把审批做成独立应用，以便快速演示功能。"}]}},
        {"type": "response_item", "timestamp": "2026-03-05T11:30:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "后来我们最终决定让审批成为工作流节点，以复用已有的路由能力。"}]}},
    ]
    (sessions / "a.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in data), encoding="utf-8")
    app = create_app(tmp_path / "db.sqlite", start_worker=False)
    with TestClient(app) as client:
        h = credentials(client)
        client.post("/api/sources", headers=h, json={"name": "Codex", "kind": "codex", "root": str(sessions.parent.parent), "allow_ai": True})
        assert app.state.collector.scan_all()["new"] == 1
        run_ai(app.state.db)
        result = client.get("/api/projects/summary", headers=h, params={"name": "Approvals"})
        assert result.status_code == 200
        timeline = result.json()["decision_timeline"]
        assert len(timeline) == 2
        assert timeline[0]["occurred_at"] == "2026-03-05 11:30:00"
        assert timeline[1]["occurred_at"] == "2026-03-01 09:12:00"
        assert all(record["document_id"] > 0 and record["is_current"] for record in timeline)
        export = client.get("/api/export", headers=h).json()
        assert export["entries"][0]["evidence"][0]["occurred_at"]
        assert export["entries"][0]["evidence"][0]["is_current"] == 1


def test_existing_v02_db_evidence_column_migrates(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    # v0.2 schema populated with a knowledge evidence row.
    from worktwin.db import Database
    db = Database(path)
    with db.connect() as conn:
        conn.execute("ALTER TABLE knowledge_evidence RENAME TO old_evidence")
        conn.execute("CREATE TABLE knowledge_evidence (id INTEGER PRIMARY KEY, knowledge_id INTEGER,document_id INTEGER,chunk_id INTEGER,quote TEXT,is_current INTEGER DEFAULT 1)")
        conn.execute("DROP TABLE old_evidence")
    Database(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_evidence)")}
        assert "occurred_at" in columns
