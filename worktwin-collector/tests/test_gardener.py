"""Knowledge gardener: persistent curation, owner approval, and AI-consent boundaries."""
from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.gardener import KnowledgeGardener


QUOTE = "项目已经确定将审批编排放在主工作流里，避免出现独立审批系统与业务流程重复维护。"
NEW_BODY = ("## 当前结论\n审批编排继续作为工作流节点实现。"
            "\n\n## 决策依据\n减少跨系统维护、保留审批证据和统一操作入口。"
            "\n\n## 历史方案\n曾讨论独立审批系统，最终没有采用。")


class GardenerModel:
    configured = True

    def __init__(self, hook=None, body=NEW_BODY):
        self.calls = []
        self.hook = hook
        self.body = body

    def chat(self, messages, max_tokens=3600):
        self.calls.append(messages)
        if self.hook:
            self.hook()
        return json.dumps({"body": self.body,
                           "reason": "合并重复信息，形成当前结论与历史决策"}, ensure_ascii=False)


def setup(tmp_path, *, source_ai=True):
    folder = tmp_path / "records"
    folder.mkdir()
    (folder / "one.md").write_text(QUOTE, encoding="utf-8")
    (folder / "two.md").write_text("补充：产品讨论决定保留单节点中的审批历史与流程上下文。", encoding="utf-8")
    (folder / "three.md").write_text("最终决定复用主工作流审计轨迹；独立审批平台仅为废弃备选方案。", encoding="utf-8")
    model = GardenerModel()
    app = create_app(tmp_path / "knowledge.sqlite", start_worker=False, inference_client=model)
    return app, folder, model, source_ai


def prep(client, app, folder, allow_ai):
    match = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get("/").text)
    access = {"X-Worktwin-Token": match.group(1)}
    response = client.post("/api/sources", headers=access, json={
        "name": "项目A", "root": str(folder), "allow_ai": allow_ai})
    assert response.status_code == 200, response.text
    source_id = response.json()["id"]
    app.state.collector.scan_all()
    documents = client.get("/api/documents", headers=access).json()
    assert len(documents) == 3
    primary = next(d for d in documents if d["relative_path"] == "one.md")
    create = client.post("/api/knowledge", headers=access, json={
        "title": "审批节点设计", "body": "审批方案演进：\n" + QUOTE,
        "kind": "decision", "status": "confirmed",
        "document_id": primary["id"], "quote": QUOTE})
    assert create.status_code == 200, create.text
    knowledge_id = create.json()["id"]
    with app.state.db.connect() as con:
        for doc in documents:
            if doc["id"] != primary["id"]:
                text = con.execute("SELECT content FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
                con.execute("""INSERT INTO knowledge_evidence
                  (knowledge_id,document_id,quote) VALUES(?,?,?)""",
                            (knowledge_id, doc["id"], text))
    return access, source_id, knowledge_id


def test_gardener_creates_nonblocking_proposal_and_owner_accepts(tmp_path):
    app, folder, model, allow = setup(tmp_path)
    with TestClient(app) as client:
        access, sid, kid = prep(client, app, folder, allow)
        twin = client.post("/api/twins", headers=access, json={"name": "项目A交接"}).json()["id"]
        assert client.put(f"/api/twins/{twin}/knowledge", headers=access,
                          json={"knowledge_ids": [kid]}).status_code == 200
        gardener = app.state.knowledge_worker.gardener
        result = gardener.process_next()
        assert result["state"] == "proposed", result
        assert gardener.process_next()["state"] == "idle"
        assert len(model.calls) == 1
        proposals = client.get("/api/knowledge/proposals", headers=access).json()
        assert len(proposals) == 1 and proposals[0]["origin"] == "curation"
        assert proposals[0]["target_id"] == kid
        assert "## 当前结论" in proposals[0]["body"]
        assert client.get("/api/twins", headers=access).json()[0]["knowledge_count"] == 1
        assert client.get("/api/knowledge", headers=access).json()[0]["needs_review"] == 0
        pid = proposals[0]["id"]
        accepted = client.post(f"/api/knowledge/proposals/{pid}/accept", headers=access)
        assert accepted.status_code == 200, accepted.text
        current = client.get("/api/knowledge", headers=access).json()[0]
        assert current["version"] == 2 and current["body"] == NEW_BODY
        assert current["needs_review"] == 0
        assert len(client.get(f"/api/knowledge/{kid}/history", headers=access).json()) == 1
        assert client.get("/api/twins", headers=access).json()[0]["knowledge_count"] == 1


def test_gardener_never_sends_unapproved_material(tmp_path):
    app, folder, model, _ = setup(tmp_path, source_ai=False)
    with TestClient(app) as client:
        prep(client, app, folder, False)
        assert app.state.knowledge_worker.gardener.process_next()["state"] == "idle"
        assert model.calls == []


def test_gardener_revocation_during_model_call_cancels_proposal(tmp_path):
    app, folder, model, _ = setup(tmp_path)
    with TestClient(app) as client:
        access, sid, kid = prep(client, app, folder, True)
        def revoke():
            with app.state.db.connect() as con:
                con.execute("UPDATE sources SET allow_ai=0 WHERE id=?", (sid,))
        model.hook = revoke
        assert app.state.knowledge_worker.gardener.process_next()["state"] == "superseded"
        assert client.get("/api/knowledge/proposals", headers=access).json() == []


def test_gardener_protects_wikilinks_and_skips_unchanged(tmp_path):
    app, folder, model, _ = setup(tmp_path)
    with TestClient(app) as client:
        access, _, kid = prep(client, app, folder, True)
        with app.state.db.connect() as con:
            con.execute("UPDATE knowledge SET body=body || ' 详见 [[Projects/审批]]。' WHERE id=?", (kid,))
        assert app.state.knowledge_worker.gardener.process_next()["state"] == "unchanged"
        assert client.get("/api/knowledge/proposals", headers=access).json() == []
        assert len(model.calls) == 1


def test_legacy_sqlite_gets_curation_schema(tmp_path):
    from worktwin.db import Database
    p = tmp_path / "old.sqlite"
    db = Database(p)
    with db.connect() as con:
        con.execute("ALTER TABLE knowledge_proposals DROP COLUMN origin")
        con.execute("DROP TABLE knowledge_curation_runs")
    upgraded = Database(p)
    with upgraded.connect() as con:
        assert "origin" in {r[1] for r in con.execute("PRAGMA table_info(knowledge_proposals)")}
        assert con.execute("SELECT name FROM sqlite_master WHERE name='knowledge_curation_runs'").fetchone()
