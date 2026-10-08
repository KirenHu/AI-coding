"""Claude Code local JSONL reader: only visible messages, no tool outputs or thinking.

Session files under ~/.claude/projects are an implementation detail. We parse
conservatively and never try to recover hidden reasoning or raw tool results.
"""
from __future__ import annotations

import json
from pathlib import Path


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
    """Return a readable transcript from user + assistant visible text only.

    Claude can emit multiple JSONL rows with the same message ID while streaming.
    Keep the longest cumulative version rather than showing repeated partial text.
    """
    entries: dict[str, dict] = {}
    order: list[str] = []
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
            text = visible_content(msg.get("content"))
            if not text:
                continue  # excludes tool_result, tool_use and thinking
            if not cwd:
                cwd = str(event.get("cwd") or "")
            if not session:
                session = str(event.get("sessionId") or "")
            # User events have their own uuid; assistant streaming rows share message.id.
            identity = (str(msg.get("id") or event.get("requestId") or event.get("uuid") or len(order))
                        if role == "assistant" else str(event.get("uuid") or len(order)))
            key = role + ":" + identity
            if key not in entries:
                order.append(key)
                entries[key] = {"role": role, "text": text, "ts": str(event.get("timestamp", ""))[:19].replace("T", " ")}
            else:
                old = entries[key]["text"]
                if text in old:
                    continue
                if old in text:
                    entries[key]["text"] = text
                else:
                    # Non-overlapping completed content blocks from the same response.
                    entries[key]["text"] += "\n" + text
    messages = [entries[key] for key in order]
    if not messages:
        return path.stem, ""
    first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
    title = " ".join(first_user.split())[:76] or f"Claude Code 会话 {path.stem}"
    parts = []
    if cwd:
        parts.append("工作目录：" + cwd)
    if session:
        parts.append("会话 ID：" + session)
    for m in messages:
        who = "用户" if m["role"] == "user" else "AI"
        parts.append(f"### {who} · {m['ts']}\n{m['text']}")
    return title, "\n\n".join(parts)
