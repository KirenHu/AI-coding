"""Normalize public transcript records; never persist reasoning or tool payloads.

Cursor support is for CLI ``--output-format stream-json`` exports, not the IDE
database. Schema reference: https://cursor.com/docs/cli/reference/output-format
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from ..action_trace import exit_code_from_output, safe_tool_name
from ..claude import visible_content
from ..codex import _plain_parts

TRANSCRIPT_ADAPTERS = frozenset(("codex", "claude", "cursor"))
_KEY = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[\"']?[^\s\"',;}]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def safe_text(text: str) -> str:
    return _BEARER.sub("Bearer [redacted]", _KEY.sub(r"\1[redacted]", text)).replace("\x00", "")


def _timestamp(event: dict) -> str:
    if event.get("timestamp"):
        return str(event["timestamp"])[:19].replace("T", " ")
    if isinstance(event.get("timestamp_ms"), (int, float)):
        return datetime.fromtimestamp(event["timestamp_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return ""


def normalize(adapter: str, event: dict, state: dict, offset: int) -> list[dict]:
    """Return small normalized events and update serializable session metadata."""
    ts = _timestamp(event)
    parent = str(event.get("parentUuid") or event.get("parent_id") or "")
    result = []

    def add(kind, *, role="", text="", native_id="", tool_name="", call_id="", failed=False, exit_code=None, origin="", artifact_paths=None):
        identity = native_id or ((ts + ":" + role) if adapter in ("codex", "claude") and ts else str(offset))
        text = safe_text(text)
        result.append(dict(kind=kind, role=role, timestamp=ts, text=text,
                           native_id=identity, tool_name=safe_tool_name(tool_name) if tool_name else "",
                           call_id=call_id, parent_event_id=parent, failed=int(failed),
                           exit_code=exit_code, origin=origin, artifact_paths=artifact_paths or []))

    def paths(value):
        if isinstance(value, str):
            try:
                import json
                value = json.loads(value)
            except (ValueError, TypeError):
                return []
        if not isinstance(value, dict):
            return []
        return [str(value[key]) for key in ("path", "file_path", "target_file")
                if isinstance(value.get(key), str) and value[key]]

    typ = event.get("type")
    if adapter == "codex":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return []
        if typ == "session_meta":
            state.update(session_id=str(payload.get("id") or state.get("session_id", "")),
                         cwd=str(payload.get("cwd") or ""),
                         parent_session_id=str(payload.get("forked_from_id") or payload.get("parent_session_id") or ""))
        elif typ == "turn_context" and payload.get("cwd"):
            state["cwd"] = str(payload["cwd"])
        elif typ == "response_item":
            subtype = payload.get("type")
            if subtype == "message" and payload.get("role") in ("user", "assistant"):
                text = _plain_parts(payload.get("content"))
                if text and not text.startswith("<environment_context>"):
                    add("message", role=payload["role"], text=text,
                        native_id=str(payload.get("id") or ""), origin="response_item")
            elif subtype in ("function_call", "custom_tool_call"):
                call_id = str(payload.get("call_id") or "")
                add("tool_call", tool_name=payload.get("name"), call_id=call_id, native_id=call_id,
                    artifact_paths=paths(payload.get("arguments")))
            elif subtype in ("function_call_output", "custom_tool_call_output"):
                call_id = str(payload.get("call_id") or "")
                add("tool_result", call_id=call_id, native_id=call_id,
                    failed=payload.get("is_error") is True, exit_code=exit_code_from_output(payload.get("output")))
        elif typ == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
            text = payload.get("message")
            if isinstance(text, str) and text.strip() and not text.startswith("<environment_context>"):
                add("message", role="user" if payload["type"] == "user_message" else "assistant",
                    text=text.strip(), origin="event_msg")
    elif adapter == "claude":
        if event.get("sessionId"):
            state["session_id"] = str(event["sessionId"])
        if event.get("cwd"):
            state["cwd"] = str(event["cwd"])
        if event.get("parentSessionId"):
            state["parent_session_id"] = str(event["parentSessionId"])
        if typ not in ("user", "assistant") or event.get("isMeta"):
            return []
        # Sidechain files are their own sessions; an embedded sidechain record
        # must not be attributed to the primary user conversation.
        if event.get("isSidechain"):
            return []
        msg = event.get("message")
        if not isinstance(msg, dict) or msg.get("role", typ) != typ:
            return []
        blocks = msg.get("content")
        has_result = False
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if typ == "assistant" and block.get("type") == "tool_use":
                call_id = str(block.get("id") or "")
                add("tool_call", tool_name=block.get("name"), call_id=call_id, native_id=call_id,
                    artifact_paths=paths(block.get("input")))
            elif typ == "user" and block.get("type") == "tool_result":
                has_result = True
                call_id = str(block.get("tool_use_id") or "")
                add("tool_result", call_id=call_id, native_id=call_id, failed=block.get("is_error") is True)
        text = visible_content(blocks)
        if text and not has_result:
            add("message", role=typ, text=text, native_id=str((msg.get("id") if typ == "assistant" else event.get("uuid")) or ""), origin="claude")
    elif adapter == "cursor":
        def flush_partial():
            text = state.pop("cursor_pending", "")
            if text.strip():
                add("message", role="assistant", text=text.strip(),
                    native_id="partial:" + str(state.pop("cursor_pending_offset", offset)), origin="cursor_cli")
                state["cursor_assistant"] = True

        if event.get("session_id"):
            state["session_id"] = str(event["session_id"])
        if typ == "system" and event.get("subtype") == "init":
            state["cwd"] = str(event.get("cwd") or "")
            state["cursor_partial"] = False
            state["cursor_assistant"] = False
        elif typ in ("user", "assistant"):
            msg = event.get("message")
            if not isinstance(msg, dict):
                return []
            if typ == "assistant":
                if "timestamp_ms" in event:
                    state["cursor_partial"] = True
                    if event.get("model_call_id"):
                        flush_partial()
                        return result
                    # Deltas are held until a message boundary so words split
                    # across JSON records remain a quoteable sentence.
                    state.setdefault("cursor_pending_offset", offset)
                    blocks = msg.get("content")
                    delta = "".join(str(block.get("text", "")) for block in blocks
                                    if isinstance(block, dict) and block.get("type") == "text") if isinstance(blocks, list) else ""
                    state["cursor_pending"] = safe_text(state.get("cursor_pending", "") + delta)
                    return []
                elif state.get("cursor_partial"):
                    flush_partial()
                    return result  # final flush repeats the streamed deltas
            text = visible_content(msg.get("content"))
            if text:
                add("message", role=typ, text=text, origin="cursor_cli")
                if typ == "assistant":
                    state["cursor_assistant"] = True
        elif typ == "tool_call" and event.get("subtype") in ("started", "completed"):
            flush_partial()
            tool = event.get("tool_call")
            tool = tool if isinstance(tool, dict) else {}
            name = next(iter(tool), "unknown_tool")
            detail = tool.get(name) if isinstance(tool.get(name), dict) else {}
            if name == "function":
                name = detail.get("name") or "unknown_tool"
            outcome = detail.get("result")
            failed = isinstance(outcome, dict) and ("error" in outcome or "failure" in outcome)
            call_id = str(event.get("call_id") or "")
            add("tool_result" if event["subtype"] == "completed" else "tool_call",
                native_id=call_id, call_id=call_id, tool_name=name, failed=failed,
                artifact_paths=paths(detail.get("args")))
        elif typ == "result":
            flush_partial()
            if not state.get("cursor_assistant") and isinstance(event.get("result"), str):
                add("message", role="assistant", text=event["result"], native_id=str(event.get("request_id") or ""), origin="cursor_cli")
    return result


def event_identity(adapter: str, session: str, event: dict) -> str:
    material = "\0".join((adapter, session, event["kind"], event["role"], event["native_id"], event["text"],
                          str(event["failed"]), str(event["exit_code"])))
    return hashlib.sha256(material.encode()).hexdigest()
