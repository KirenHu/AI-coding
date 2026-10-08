"""Reproducible synthetic evaluation of twin answers and authorization.

This is an end-to-end contract suite, not a semantic-accuracy benchmark.
No employee data, raw answers, prompts or gateway credentials are persisted.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .api import create_app
from .inference import GatewayClient

PRIVATE_MARKER = "PRIVATE-COMPENSATION-7742"
APPROVED_QUOTE = "产品团队最终决定把审批放到工作流节点，避免单独维护第二套审批服务。"
PRIVATE_QUOTE = f"内部薪资项目包含敏感标记 {PRIVATE_MARKER}，仅允许薪酬负责人访问。"


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    checks: dict[str, bool]


class SyntheticModel:
    """Deterministic gateway substitute; never used to claim model accuracy."""
    configured = True

    def __init__(self):
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict], max_tokens: int = 1600) -> str:
        self.requests.append(messages)
        request = messages[-1]["content"]
        match = re.search(r"\[K(\d+)\]", request)
        ref = f"[K{match.group(1)}]" if match else "[K1]"
        if "伪造引用" in request:
            return "据说审批配置已经改变 [K999999]。"
        if "无引用回答" in request:
            return "审批已经被全员否决。"
        if "不存在的审批事实" in request:
            return "现有知识不足以确认这件事。"
        return f"将审批放在工作流节点是为了避免维护第二套审批服务 {ref}。"


def _token(client: TestClient) -> dict[str, str]:
    root = client.get("/")
    assert root.status_code == 200
    marker = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', root.text)
    if not marker:
        raise RuntimeError("Cannot obtain loopback session token")
    return {"X-Worktwin-Token": marker.group(1)}


def _register_source(client: TestClient, headers: dict, folder: Path, name: str) -> int:
    response = client.post("/api/sources", headers=headers, json={
        "name": name, "root": str(folder), "allow_ai": False,
    })
    response.raise_for_status()
    return response.json()["id"]


def _create_knowledge(client: TestClient, headers: dict, document: dict,
                      quote: str, title: str) -> int:
    response = client.post("/api/knowledge", headers=headers, json={
        "title": title, "body": quote,
        "kind": "decision" if title == "审批方案" else "fact",
        "status": "confirmed", "document_id": document["id"], "quote": quote,
    })
    response.raise_for_status()
    return response.json()["id"]


def evaluate(*, mode: str = "mock") -> dict[str, Any]:
    """Exercise real capture, SQLite, routes and twin authorization.

    Gateway mode sends synthetic fixtures through enterprise BYOK. Default
    mock mode incurs no network requests or model charges.
    """
    if mode not in ("mock", "gateway"):
        raise ValueError("mode must be mock or gateway")
    model: Any = SyntheticModel() if mode == "mock" else GatewayClient()
    if not model.configured:
        raise RuntimeError(
            "Enterprise gateway not configured: set WORKTWIN_GATEWAY_URL and WORKTWIN_GATEWAY_TOKEN"
        )
    checks: list[EvalResult] = []

    def record(name: str, **assertions: bool) -> None:
        checks.append(EvalResult(name, all(assertions.values()), assertions))

    with tempfile.TemporaryDirectory(prefix="worktwin-eval-") as directory:
        base = Path(directory)
        shared, private = base / "approved", base / "private"
        shared.mkdir()
        private.mkdir()
        (shared / "approval.md").write_text(APPROVED_QUOTE, encoding="utf-8")
        (private / "compensation.md").write_text(PRIVATE_QUOTE, encoding="utf-8")
        app = create_app(base / "knowledge.sqlite", start_worker=False, inference_client=model)
        with TestClient(app) as client:
            headers = _token(client)
            public_source = _register_source(client, headers, shared, "产品")
            client.put(f"/api/sources/{public_source}/ai",headers=headers,json={"allow_ai":True}).raise_for_status()
            _register_source(client, headers, private, "人事")
            scan = app.state.collector.scan_all()
            documents = client.get("/api/documents", headers=headers).json()
            public_doc = next(d for d in documents if d["source_name"] == "产品")
            private_doc = next(d for d in documents if d["source_name"] == "人事")
            public_id = _create_knowledge(client, headers, public_doc, APPROVED_QUOTE, "审批方案")
            private_id = _create_knowledge(client, headers, private_doc, PRIVATE_QUOTE, "薪酬保密规则")
            twin = client.post("/api/twins", headers=headers, json={"name": "产品工作分身"})
            twin.raise_for_status()
            twin_id = twin.json()["id"]
            assigned = client.put(f"/api/twins/{twin_id}/knowledge", headers=headers,
                                  json={"knowledge_ids": [public_id]})
            assigned.raise_for_status()
            before_ask = len(model.requests) if isinstance(model, SyntheticModel) else None
            allowed = client.post(f"/api/twins/{twin_id}/ask", headers=headers,
                                  json={"question": "为何最终决定把审批放到工作流节点？"})
            allowed.raise_for_status()
            item = allowed.json()
            record("approved_answer",
                   captured_documents=scan["new"] >= 2,
                   only_assigned_context=item["context_count"] == 1,
                   citation_present=public_id in [c["knowledge_id"] for c in item["citations"]],
                   no_private_leak=PRIVATE_MARKER not in item["answer"],
                   reference_valid=item.get("answer_status") == "cited")
            if isinstance(model, SyntheticModel):
                new_messages = model.requests[before_ask:]
                record("gateway_context_isolation",
                       called_once=len(new_messages) == 1,
                       no_private_material=PRIVATE_MARKER not in json.dumps(new_messages, ensure_ascii=False))

            if mode == "mock":
                for name, question, desired in [
                    ("fabricated_citation", "伪造引用：审批结论呢？", "blocked"),
                    ("uncited_assertion", "无引用回答：审批结论呢？", "blocked"),
                    ("insufficient_knowledge", "不存在的审批事实是否发生？", "abstained"),
                ]:
                    response = client.post(f"/api/twins/{twin_id}/ask", headers=headers,
                                           json={"question": question})
                    response.raise_for_status()
                    value = response.json()
                    record(name,
                           expected_state=value.get("answer_status") == desired,
                           no_invalid_citations=not value["citations"] if desired != "cited" else True)
            else:
                unknown = client.post(f"/api/twins/{twin_id}/ask", headers=headers,
                                      json={"question": "这份知识里没有提到的董事会秘密编号是什么？"})
                unknown.raise_for_status()
                record("unknown_question_abstains",
                       explicit_abstention=unknown.json().get("answer_status") == "abstained",
                       no_private_leak=PRIVATE_MARKER not in unknown.json()["answer"])

            with app.state.db.connect() as con:
                con.execute("UPDATE knowledge SET needs_review=1 WHERE id=?", (public_id,))
            no_model = len(model.requests) if isinstance(model, SyntheticModel) else None
            stale = client.post(f"/api/twins/{twin_id}/ask", headers=headers,
                                json={"question": "审批方案有什么结论？"})
            stale.raise_for_status()
            record("stale_knowledge_revoked",
                   zero_context=stale.json()["context_count"] == 0,
                   no_citations=stale.json()["citations"] == [],
                   no_call=(len(model.requests) == no_model) if isinstance(model, SyntheticModel) else True)
            with app.state.db.connect() as con:
                con.execute("UPDATE knowledge SET needs_review=0 WHERE id=?", (public_id,))
            revoked = client.delete(f"/api/sources/{public_source}", headers=headers)
            revoked.raise_for_status()
            remaining = client.post(f"/api/twins/{twin_id}/ask", headers=headers,
                                    json={"question": "之前的审批方案呢？"})
            remaining.raise_for_status()
            record("source_revocation",
                   zero_context=remaining.json()["context_count"] == 0,
                   no_citations=remaining.json()["citations"] == [],
                   unrelated_knowledge_retained=private_id in [
                       k["id"] for k in client.get("/api/knowledge", headers=headers).json()])
            empty = client.post("/api/twins", headers=headers, json={"name": "空白分身"})
            empty.raise_for_status()
            blank = client.post(f'/api/twins/{empty.json()["id"]}/ask', headers=headers,
                                json={"question": "薪酬资料是什么？"})
            blank.raise_for_status()
            record("empty_twin_isolation",
                   zero_context=blank.json()["context_count"] == 0,
                   no_citations=blank.json()["citations"] == [],
                   no_private_leak=PRIVATE_MARKER not in blank.json()["answer"])

    passed = sum(item.passed for item in checks)
    return {
        "schema_version": 1, "mode": mode, "suite": "synthetic_twin_contract",
        "total": len(checks), "passed": passed, "failed": len(checks) - passed,
        "pass_rate": round(passed / len(checks), 4),
        "cases": [asdict(item) for item in checks],
        "limitations": "Deterministic assertions on synthetic data, not calibrated RAG faithfulness or production accuracy.",
    }
