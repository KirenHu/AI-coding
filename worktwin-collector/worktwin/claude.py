"""Claude Code local JSONL adapter.

Visible user/assistant text and tool execution metadata are collected from
authorized sessions. Private thinking, tool arguments, and raw tool results are
not placed into knowledge extraction or the local full-log MCP.
"""
from __future__ import annotations

import json
from pathlib import Path

from .action_trace import render_action


def visible_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    blocks = []
    for value in content:
        if isinstance(value, dict) and value.get("type") == "text":
            txt = value.get("text")
            if isinstance(txt, str) and txt.strip():
                blocks.append(txt.strip())
    return "\n".join(blocks)


def parse_claude_session(path: Path) -> tuple[str, str]:
    """Read visible conversation and action status, never raw execution output.

    Claude streaming may emit multiple rows for one assistant message. Keep
    cumulative text once, keyed by message ID; keep each tool_use ID once.
    """
    entries: dict[str, dict] = {}
    order: list[str] = []
    actions: list[dict] = []
    by_tool_id: dict[str, dict] = {}
    cwd = ""
    session = ""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("type") not in ("user", "assistant"):
                continue
            if event.get("isSidechain") or event.get("isMeta"):
                continue
            msg = event.get("message")
            if not isinstance(msg, dict):
                continue
            role = event["type"]
            if msg.get("role", role) != role:
                continue
            if not cwd:
                cwd = str(event.get("cwd") or "")
            if not session:
                session = str(event.get("sessionId") or "")
            timestamp = str(event.get("timestamp") or "")[:19].replace("T", " ")
            blocks = msg.get("content")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    typ = block.get("type")
                    if role == "assistant" and typ == "tool_use":
                        tool_id = str(block.get("id") or "")
                        if tool_id and tool_id not in by_tool_id:
                            action = {"ts": timestamp, "name": block.get("name") or "unknown_tool",
                                      "done": False, "failed": False}
                            by_tool_id[tool_id] = action
                            actions.append(action)
                    elif role == "user" and typ == "tool_result":
                        tool_id = str(block.get("tool_use_id") or "")
                        action = by_tool_id.get(tool_id)
                        if action is None:
                            # An imported fragment may start in the middle of
                            # a tool exchange; never fabricate a success.
                            action = {"ts": timestamp, "name": "unknown_tool",
                                      "done": False, "failed": False}
                            actions.append(action)
                        action["done"] = True
                        action["failed"] = block.get("is_error") is True
            text = visible_content(blocks)
            if not text:
                continue
            identity = (str(msg.get("id") or event.get("requestId") or event.get("uuid") or len(order))
                        if role == "assistant" else str(event.get("uuid") or len(order)))
            key = role + ":" + identity
            if key not in entries:
                order.append(key)
                entries[key] = {"role": role, "text": text, "ts": timestamp}
            else:
                old = entries[key]["text"]
                if text in old:
                    continue
                if old in text:
                    entries[key]["text"] = text
                else:
                    entries[key]["text"] += "\n" + text
    messages = [entries[key] for key in order]
    if not messages and not actions:
        return path.stem, ""
    first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
    title = " ".join(first_user.split())[:76] or f"Claude Code 会话 {path.stem}"
    lines = []
    if cwd:
        lines.append("工作目录：" + cwd)
    if session:
        lines.append("会话 ID：" + session)
    timeline: list[tuple[str, int, str]] = []
    for index, m in enumerate(messages):
        who = "用户" if m["role"] == "user" else "AI"
        timeline.append((m["ts"], index, f"### {who} · {m['ts']}\n{m['text']}"))
    offset = len(messages)
    for index, action in enumerate(actions):
        timeline.append((action["ts"], offset + index,
                         render_action(action["ts"], action["name"],
                                       result=action["done"], failed=action["failed"])))
    timeline.sort(key=lambda entry: (entry[0], entry[1]))
    lines.extend(entry[2] for entry in timeline)
    return title, "\n\n".join(lines)
