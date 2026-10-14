"""Probe contract tests, not Hindsight quality or service acceptance."""
import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError


SPEC = importlib.util.spec_from_file_location(
    "knowledge_engine_probe", Path(__file__).parents[1] / "scripts/knowledge_engine_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_unconfigured_probe_reports_pending_without_network(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unconfigured probe must not use network")
    monkeypatch.setattr(probe, "urlopen", unexpected)
    report_path = tmp_path / "report.json"
    assert probe.main(["--report", str(report_path)]) == 2
    report = json.loads(report_path.read_text())
    assert report["status"] == "PENDING"
    assert report["quality_review"] == "PENDING"
    assert report["checks"] == {}
    assert report["synthetic_only"] is True


def test_append_retries_use_same_batch_but_changed_content_is_new_operation():
    source = json.loads(probe.FIXTURE.read_text())["sources"][0]
    item = probe.source_item(source, append=True)
    first = probe.batch_payload("bank", "delta-1", item)
    retry = probe.batch_payload("bank", "delta-1", dict(item))
    changed = probe.batch_payload("bank", "delta-1", {**item, "content": item["content"] + "correction"})
    assert first == retry
    assert first["operation_id"] != changed["operation_id"]
    assert first["async"] is True
    assert item["update_mode"] == "append"
    assert item["tags"] == ["project:aurora"]
    assert "document_tags" not in item  # 0.10.3 accepts this deprecated field only at request level.


def test_recall_requests_strict_scope_and_underlying_source_facts():
    client = probe.Hindsight("http://localhost:8888", "test-bank")
    calls = []
    client.request = lambda method, path, body: calls.append((method, path, body)) or {}
    client.recall("通知规则", "project:aurora")
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/memories/recall")
    assert body["tags_match"] == "all_strict"
    assert body["tags"] == ["project:aurora"]
    assert body["include"]["source_facts"]
    assert body["include"]["chunks"]


def test_failed_operation_is_not_treated_as_completed():
    client = probe.Hindsight("http://localhost:8888", "test-bank")
    client.request = lambda *args: {"status": "failed"}
    try:
        client.wait({"operation_id": "123"})
    except RuntimeError as error:
        assert "failed" in str(error)
    else:
        raise AssertionError("failed operation must fail the probe")


def test_http_error_does_not_report_provider_body_or_token(tmp_path, monkeypatch):
    def failure(*args, **kwargs):
        raise HTTPError("https://service.example/secret", 401, "secret-key", {}, None)
    monkeypatch.setattr(probe, "urlopen", failure)
    monkeypatch.setenv("WORKTWIN_HINDSIGHT_API_KEY", "secret-key")
    report_path = tmp_path / "report.json"
    assert probe.main(["--endpoint", "https://service.example", "--report", str(report_path)]) == 1
    data = report_path.read_text()
    assert "secret-key" not in data
    assert "service.example" not in data
    assert json.loads(data)["status"] == "ERROR"


def test_page_rebuild_success_cannot_hide_automatic_delete_failure():
    """Canned 0.10.3 response shapes exercise the probe, not the remote engine."""
    corpus = json.loads(probe.FIXTURE.read_text())
    target = corpus["sources"][0]
    class CannedClient:
        bank = "contract-test"
        deleted = False
        cleared = False

        def wait(self, operation):
            assert operation["operation_id"]

        def settle(self):
            pass

        def recall(self, query, scope):
            return {"results": [] if self.deleted else [{"type": "world", "text": corpus["delete_canary"],
                    "tags": [scope], "document_id": target["id"]}], "source_facts": {}}

        def request(self, method, path, body=None):
            if path == "/memories":
                return {"operation_id": body["operation_id"]}
            if path == "/consolidate" or path.endswith("/refresh"):
                return {"operation_id": "background-op"}
            if path == "/knowledge-base/pages":
                return {"page_id": "page-1", "mental_model_id": "model-1", "operation_id": "page-op"}
            if path == "/knowledge-base/pages/page-1":
                return {"id": "page-1", "body": "保留其他来源的路由需求" if self.cleared else corpus["delete_canary"]}
            if path == "/knowledge-base/tree":
                return {"roots": [{"id": "page-1", "is_stale": False}]}
            if path.endswith("/clear"):
                self.cleared = True
                return {}
            if path.startswith("/documents/"):
                if method == "DELETE":
                    self.deleted = True
                    return {"memory_units_deleted": 1}
                if self.deleted:
                    raise HTTPError("http://localhost/document", 404, "not found", {}, None)
                source = next(s for s in corpus["sources"] if path.endswith(s["id"]))
                return {"original_text": source["content"] + source.get("append", "")}
            raise AssertionError((method, path))

    report = {"checks": {}}
    probe.run_hindsight(CannedClient(), corpus, report)
    assert report["checks"]["automatic_page_delete_propagation"] is False
    assert report["checks"]["explicit_page_delete_propagation"] is True
    assert report["checks"]["page_contains_deletion_canary"] is True
    assert not all(report["checks"].values())
