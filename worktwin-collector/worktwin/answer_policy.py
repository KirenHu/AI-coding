"""Structural citation checks for knowledge-twin answers.

A valid [Kx] identifier means the article was authorized and included in
context; it does NOT prove semantic faithfulness or factual correctness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CITATION = re.compile(r"\[K(\d+)\]")
_UNCERTAIN = re.compile(
    r"(不知道|不清楚|无法(确认|判断|确定|回答)|没有(相关|足够|可用)"
    r"|信息不足|不足以(确认|判断|确定|回答)|缺乏(依据|信息)|未找到|无从得知"
    r"|not (enough|sure|found)|don't know)",
    flags=re.IGNORECASE,
)
_FALLBACK = "现有授权知识不足以给出可核实的回答，请向本人确认或补充相关知识。"


@dataclass(frozen=True)
class AnswerCheck:
    answer: str
    cited_ids: frozenset[int]
    state: str  # cited, abstained, blocked
    reason: str  # valid_citations, insufficient, missing_citations, unauthorized_citations


def check_answer(answer: str, authorized_ids: set[int]) -> AnswerCheck:
    """Fail closed on invented citations and assertive uncited answers.

    Callers MUST pass only IDs present in the filtered model context.
    """
    output = str(answer).strip()
    cited = {int(raw) for raw in _CITATION.findall(output)}
    if cited - authorized_ids:
        return AnswerCheck(_FALLBACK, frozenset(), "blocked", "unauthorized_citations")
    if not cited:
        if output and _UNCERTAIN.search(output):
            return AnswerCheck(output, frozenset(), "abstained", "insufficient")
        return AnswerCheck(_FALLBACK, frozenset(), "blocked", "missing_citations")
    return AnswerCheck(output, frozenset(cited), "cited", "valid_citations")
