"""Concurrent AI workers must not claim and bill the same pending document."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient

from tests.support import FakeModel
from worktwin.api import create_app
from worktwin.jobs import KnowledgeWorker


def test_two_workers_claim_only_once(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "decision.md").write_text(
        "最终决定采用统一审批节点，替代独立审批流，以便各业务流程复用。", encoding="utf-8"
    )
    app = create_app(tmp_path / "memory.db", start_worker=False)
    entered, resume = Event(), Event()

    class SlowModel(FakeModel):
        def chat(self, messages, max_tokens=2400):
            entered.set()
            assert resume.wait(timeout=10), "test model gate timed out"
            return super().chat(messages, max_tokens=max_tokens)

    with TestClient(app) as client:
        # The test uses the app's own collector and SQLite database; the
        # production worker protocol is shared across multiple instances.
        with app.state.db.connect() as con:
            con.execute(
                "INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,?,?,1)",
                ("workspace", "folder", str(workspace)),
            )
        assert app.state.collector.scan_all()["new"] == 1
        model = SlowModel()
        first_worker = KnowledgeWorker(app.state.db, client=model)
        second_worker = KnowledgeWorker(app.state.db, client=model)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_worker.process_next)
            assert entered.wait(timeout=5), "first worker never entered model call"
            second = pool.submit(second_worker.process_next)
            assert second.result(timeout=5)["state"] == "idle"
            resume.set()
            assert first.result(timeout=10)["state"] == "done"
        with app.state.db.connect() as con:
            jobs = con.execute("SELECT state,attempts FROM ai_jobs").fetchall()
        assert len(jobs) == 1
        assert jobs[0]["state"] == "done" and jobs[0]["attempts"] == 1
