"""Read visible Codex session dialogue and minimal tool-execution evidence.

Codex local JSONL is not a stable API. Unknown events are skipped; neither
private reasoning nor raw tool inputs/outputs are copied into model context.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .action_trace import exit_code_from_output, render_action


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
    """Return a human-readable session, with tool status but no tool payload."""
    response_messages: list[tuple[str, str, str]] = []
    event_messages: list[tuple[str, str, str]] = []
    actions: list[dict] = []
    by_call_id: dict[str, dict] = {}
    cwd = ""
    session_id = ""
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            timestamp = str(event.get("timestamp") or "")[:19].replace("T", " ")
            if kind == "session_meta":
                cwd = str(payload.get("cwd") or "")
                session_id = str(payload.get("id") or "")
            elif kind == "response_item":
                typ = payload.get("type")
                if typ == "message":
                    role = payload.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    msg = _plain_parts(payload.get("content"))
                    if msg and not msg.startswith("<environment_context>"):
                        response_messages.append((role, timestamp, msg))
                elif typ in ("function_call", "custom_tool_call"):
                    # Preserve only tool identity and outcome. Arguments may
                    # embed credentials, source files or injected instructions.
                    call_id = str(payload.get("call_id") or "")
                    action = {"ts": timestamp, "name": payload.get("name") or "unknown_tool",
                              "done": False, "failed": False, "exit_code": None}
                    actions.append(action)
                    if call_id:
                        by_call_id[call_id] = action
                elif typ in ("function_call_output", "custom_tool_call_output"):
                    call_id = str(payload.get("call_id") or "")
                    action = by_call_id.get(call_id)
                    if action is None:
                        # A truncated JSONL can contain only the result half.
                        action = {"ts": timestamp, "name": "unknown_tool", "done": False,
                                  "failed": False, "exit_code": None}
                        actions.append(action)
                    action["done"] = True
                    output = payload.get("output")
                    action["exit_code"] = exit_code_from_output(output)
                    action["failed"] = payload.get("is_error") is True
            elif kind == "event_msg":
                typ = payload.get("type")
                if typ not in ("user_message", "agent_message"):
                    continue
                role = "user" if typ == "user_message" else "assistant"
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    event_messages.append((role, timestamp, msg.strip()))

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
    for role, ts, msg in messages:
        if ordered and ordered[-1][0] == role and ordered[-1][2] == msg:
            continue
        ordered.append((role, ts, msg))
    if not ordered and not actions:
        return path.stem, ""
    first_question = next((msg for role, _, msg in ordered if role == "user"), "")
    title = " ".join(first_question.split())[:76] or f"Codex 会话 {path.stem}"
    lines = []
    if cwd:
        lines.append(f"工作目录：{cwd}")
    if session_id:
        lines.append(f"会话 ID：{session_id}")
    timeline: list[tuple[str, int, str]] = []
    for index, (role, ts, msg) in enumerate(ordered):
        name = "用户" if role == "user" else "AI"
        timeline.append((ts, index, f"### {name} · {ts}\n{msg}"))
    offset = len(ordered)
    for index, action in enumerate(actions):
        timeline.append((action["ts"], offset + index,
                         render_action(action["ts"], action["name"],
                                       result=action["done"], failed=action["failed"],
                                       exit_code=action["exit_code"])))
    timeline.sort(key=lambda record: (record[0], record[1]))
    lines.extend(item[2] for item in timeline)
    return title, "\n\n".join(lines)
