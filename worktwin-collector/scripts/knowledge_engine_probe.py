"""Opt-in Hindsight probe using synthetic data; never reads an installed database.

The default run writes a PENDING report without making network requests. Passing
--endpoint runs against a disposable memory bank. No Hindsight package is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/knowledge_engine_probe.json"


def batch_payload(bank: str, batch: str, item: dict) -> dict:
    """One stable async operation ID per immutable batch, including its content."""
    digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"items": [item], "async": True,
            "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{bank}/{batch}/{digest}"))}


def source_item(source: dict, *, append: bool = False) -> dict:
    return {"document_id": source["id"], "content": source["append" if append else "content"],
            "tags": [source["scope"]],
            "update_mode": "append" if append else "replace",
            "metadata": {"source_id": source["id"], "dataset": "worktwin-synthetic-v1"},
            "observation_scopes": "combined"}


class Hindsight:
    def __init__(self, endpoint: str, bank: str, token: str = "", timeout: float = 300):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint must be an HTTP(S) service URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("put the service token in WORKTWIN_HINDSIGHT_API_KEY, not the URL")
        self.url = endpoint.rstrip("/") + "/v1/default/banks/" + quote(bank, safe="")
        self.bank, self.token, self.timeout = bank, token, timeout
        self.calls: list[dict] = []

    def request(self, method: str, path: str = "", body: dict | None = None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.url + path, method=method, headers=headers,
                          data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        start = time.perf_counter()
        try:
            with urlopen(request, timeout=min(self.timeout, 60)) as response:
                data = response.read()
                return json.loads(data) if data else {}
        finally:
            self.calls.append({"method": method, "path": path,
                               "seconds": round(time.perf_counter() - start, 3)})

    def wait(self, operation: dict) -> dict:
        operation_id = operation.get("operation_id")
        if not operation_id:
            raise RuntimeError("expected an asynchronous operation_id")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = self.request("GET", "/operations/" + quote(operation_id, safe=""))
            if result["status"] == "completed":
                return result
            if result["status"] in {"failed", "cancelled"}:
                raise RuntimeError(f"Hindsight operation {operation_id} {result['status']}")
            time.sleep(0.5)
        raise TimeoutError(f"Hindsight operation {operation_id} did not finish within {self.timeout}s")

    def recall(self, query: str, scope: str):
        return self.request("POST", "/memories/recall", {
            "query": query, "tags": [scope], "tags_match": "all_strict",
            "budget": "low", "max_tokens": 4096,
            "include": {"chunks": {"max_tokens": 8192}, "source_facts": {"max_tokens": 8192}}})

    def settle(self):
        """Wait for automatic consolidation/page work triggered by this tiny corpus."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            operations = self.request("GET", "/operations?limit=100")["operations"]
            if any(op["status"] in {"failed", "cancelled"} for op in operations):
                raise RuntimeError("a background Hindsight operation did not complete")
            if not any(op["status"] in {"pending", "processing"} for op in operations):
                return
            time.sleep(0.5)
        raise TimeoutError("Hindsight background work did not settle")


def recall_facts(result: dict) -> list[dict]:
    return list(result.get("results", [])) + list((result.get("source_facts") or {}).values())


def run_hindsight(client: Hindsight, corpus: dict, report: dict):
    checks = report["checks"]
    sources = corpus["sources"]
    for source in sources:
        item = source_item(source)
        payload = batch_payload(client.bank, source["id"] + "-initial", item)
        client.wait(client.request("POST", "/memories", payload))
    target = sources[0]
    payload = batch_payload(client.bank, target["id"] + "-delta", source_item(target, append=True))
    first = client.request("POST", "/memories", payload)
    client.wait(first)
    # Simulates a lost acknowledgement: same append and operation ID, no new batch.
    retried = client.request("POST", "/memories", payload)
    client.wait(retried)
    checks["retry_reuses_operation"] = first["operation_id"] == retried["operation_id"]
    document_path = "/documents/" + quote(target["id"], safe="")
    document = client.request("GET", document_path)
    checks["append_keeps_history_once"] = (
        target["content"] in document["original_text"]
        and document["original_text"].count(target["append"]) == 1)

    # Explicit consolidation makes this finite probe independent of worker scheduling.
    client.wait(client.request("POST", "/consolidate", {}))
    client.settle()
    recalled = client.recall(corpus["query"], target["scope"])
    report["recall_before_delete"] = recalled
    facts = recall_facts(recalled)
    checks["facts_returned"] = bool(recalled.get("results"))
    checks["project_scope"] = bool(facts) and all(target["scope"] in (f.get("tags") or []) for f in facts)
    foreign = {s["id"] for s in sources if s["scope"] != target["scope"]}
    checks["no_foreign_source"] = all(f.get("document_id") not in foreign for f in facts)
    known = {s["id"] for s in sources}
    raw_facts = [f for f in facts if f.get("type") != "observation"]
    checks["raw_fact_source_traceable"] = bool(raw_facts) and all(
        f.get("document_id") in known for f in raw_facts)
    report["documents_before_delete"] = {s["id"]: client.request(
        "GET", "/documents/" + quote(s["id"], safe="")) for s in sources}

    page = client.request("POST", "/knowledge-base/pages", {
        "name": "合成项目发布说明", "source_query": corpus["query"],
        "tags": [target["scope"]], "max_tokens": 1200})
    client.wait(page)
    page_path = "/knowledge-base/pages/" + quote(page["page_id"], safe="")
    before = client.request("GET", page_path)
    report["page_before_delete"] = before
    checks["page_contains_deletion_canary"] = corpus["delete_canary"] in before["body"]

    client.request("DELETE", document_path)
    try:
        client.request("GET", document_path)
        checks["source_deleted"] = False
    except HTTPError as error:
        if error.code != 404:
            raise
        checks["source_deleted"] = True
    client.wait(client.request("POST", "/consolidate", {}))
    client.settle()
    after_recall = client.recall(corpus["query"], target["scope"])
    report["recall_after_delete"] = after_recall
    checks["deleted_source_not_recalled"] = all(
        f.get("document_id") != target["id"] for f in recall_facts(after_recall))
    checks["deleted_fact_not_recalled"] = corpus["delete_canary"] not in json.dumps(after_recall, ensure_ascii=False)
    report["page_after_automatic_delete"] = client.request("GET", page_path)
    report["tree_after_delete"] = client.request("GET", "/knowledge-base/tree")
    checks["automatic_page_delete_propagation"] = (
        corpus["delete_canary"] not in report["page_after_automatic_delete"]["body"])

    # Measure the documented explicit repair separately from automatic propagation.
    model_id = before.get("mental_model_id") or page.get("mental_model_id")
    if not model_id:
        raise RuntimeError("page response did not expose mental_model_id")
    model_path = "/mental-models/" + quote(model_id, safe="")
    client.request("POST", model_path + "/clear")
    client.wait(client.request("POST", model_path + "/refresh"))
    report["page_after_explicit_rebuild"] = client.request("GET", page_path)
    checks["explicit_page_delete_propagation"] = (
        corpus["delete_canary"] not in report["page_after_explicit_rebuild"]["body"])


def run_baseline(corpus: dict):
    """Reuse WorkTwin's actual extractor, with explicitly supplied test credentials."""
    names = ("WORKTWIN_PROBE_MODEL_URL", "WORKTWIN_PROBE_MODEL_KEY", "WORKTWIN_PROBE_MODEL")
    if not all(os.environ.get(name) for name in names):
        return {"status": "PENDING", "reason": "Set " + ", ".join(names)}
    sys.path.insert(0, str(ROOT))
    from worktwin.inference import extract_knowledge
    from worktwin.model_settings import PersonalModel
    client = PersonalModel(*(os.environ[name] for name in names))
    result = {"status": "COMPLETED", "model": client.model, "cases": []}
    for source in corpus["sources"]:
        start = time.perf_counter()
        scope = {"scope": "project", "project": source["project"], "project_key": source["scope"]}
        initial = extract_knowledge(source["content"], transcript=True, client=client, scope=scope)
        updated = extract_knowledge(source["content"] + source.get("append", ""),
                                    transcript=True, client=client, scope=scope, existing=initial) if source.get("append") else initial
        result["cases"].append({"source_id": source["id"], "initial": initial, "updated": updated,
                                "seconds": round(time.perf_counter() - start, 3)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="", help="Explicit Hindsight service URL; omitted = no network")
    parser.add_argument("--report", type=Path, default=Path("/tmp/worktwin-hindsight-probe.json"))
    parser.add_argument("--timeout", type=float, default=300, help="Maximum seconds per background operation")
    parser.add_argument("--baseline", action="store_true", help="Run current extractor using explicit test model env vars")
    parser.add_argument("--keep-bank", action="store_true", help="Keep this run's synthetic bank for inspection")
    args = parser.parse_args(argv)
    corpus = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report = {"status": "PENDING", "dataset": corpus["name"], "synthetic_only": True,
              "dataset_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
              "api_reviewed_version": "0.10.3", "checks": {}, "quality_review": "PENDING",
              "model_cost": None, "reason": "No Hindsight endpoint was supplied; no network requests made.",
              "comparison_limit": "Finite synthetic probe; baseline is extraction only, not end-to-end equivalence.",
              "baseline": {"status": "PENDING"}}
    client = None
    exit_code = 2
    try:
        if args.endpoint:
            bank = "worktwin-probe-" + uuid.uuid4().hex
            client = Hindsight(args.endpoint, bank, os.environ.get("WORKTWIN_HINDSIGHT_API_KEY", ""), args.timeout)
            report["bank_id"] = bank
            run_hindsight(client, corpus, report)
            report["status"] = "CHECKS_PASSED" if all(report["checks"].values()) else "CHECKS_FAILED"
            report["reason"] = "Inspect generated content against the fixture's expected outcomes before adopting."
            exit_code = 0 if report["status"] == "CHECKS_PASSED" else 1
        if args.baseline:
            report["baseline"] = run_baseline(corpus)
            if not args.endpoint:
                report["reason"] = "Hindsight endpoint missing; baseline status recorded separately."
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, KeyError) as error:
        # Do not persist remote response bodies or URLs containing credentials.
        report["status"] = "ERROR"
        report["reason"] = f"{type(error).__name__}" + (f" HTTP {error.code}" if isinstance(error, HTTPError) else "")
        exit_code = 1
    finally:
        if client:
            if not args.keep_bank:
                try:
                    client.request("DELETE")
                    report["bank_cleanup"] = "deleted"
                except (HTTPError, URLError, TimeoutError) as error:
                    report["bank_cleanup"] = type(error).__name__
            else:
                report["bank_cleanup"] = "kept_for_inspection"
            report["http_calls"] = client.calls
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(args.report),
                          "checks": report["checks"], "quality_review": report["quality_review"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
