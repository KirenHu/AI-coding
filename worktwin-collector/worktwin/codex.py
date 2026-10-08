"""Best-effort adapter for locally stored Codex CLI JSONL session files.

Codex JSONL is not a stable public data contract; this parser deliberately
supports several known message envelopes and ignores private reasoning content.
"""

from __future__ import annotations

import json
from pathlib import Path


def _plain_parts(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    texts = []
    for part in value:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text", "text"):
            v = part.get("text")
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())
    return "\n".join(texts)


def parse_codex_session(path: Path) -> tuple[str, str]:
    """Return (human-friendly title, transcript) without reasoning or prompts injected by tools."""
    response_messages: list[tuple[str, str, str]] = []
    event_messages: list[tuple[str, str, str]] = []
    cwd = ""
    session_id = ""
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            timestamp = str(event.get("timestamp", ""))[:19].replace("T", " ")
            if kind == "session_meta":
                cwd = str(payload.get("cwd") or "")
                session_id = str(payload.get("id") or "")
            elif kind == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                msg = _plain_parts(payload.get("content"))
                # System instructions, tool payloads and private chains of thought
                # are deliberately excluded from this knowledge ingestion path.
                if msg and not msg.startswith("<environment_context>"):
                    response_messages.append((role, timestamp, msg))
            elif kind == "event_msg":
                typ = payload.get("type")
                if typ not in ("user_message", "agent_message"):
                    continue
                role = "user" if typ == "user_message" else "assistant"
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    event_messages.append((role, timestamp, msg.strip()))
    # Mixed-format sessions can contain event-only turns even when response_item
    # exists for that role. Deduplicate by text AND close timestamp, never by
    # speaker alone (which silently lost legitimate work records in v0.1).
    from datetime import datetime

    def near(a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) <= 3
        except ValueError:
            return False

    messages = list(response_messages)
    for candidate in event_messages:
        if not any(role == candidate[0] and content == candidate[2] and near(ts, candidate[1])
                   for role, ts, content in response_messages):
            messages.append(candidate)
    messages.sort(key=lambda m: m[1])
    ordered: list[tuple[str, str, str]] = []
    for role, ts, content in messages:
        if ordered and ordered[-1][0] == role and ordered[-1][2] == content:
            continue
        ordered.append((role, ts, content))
    if not ordered:
        return path.stem, ""
    first_question = next((text for role, _, text in ordered if role == "user"), "")
    title = " ".join(first_question.split())[:76] or f"Codex 会话 {path.stem}"
    chunks = []
    if cwd:
        chunks.append(f"工作目录：{cwd}")
    if session_id:
        chunks.append(f"会话 ID：{session_id}")
    for role, ts, content in ordered:
        name = "用户" if role == "user" else "AI"
        chunks.append(f"### {name} · {ts}\n{content}")
    return title, "\n\n".join(chunks)
