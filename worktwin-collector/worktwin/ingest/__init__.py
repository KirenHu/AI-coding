"""Durable byte-cursor transcript capture, separate from model processing.

Documents/chunks remain the public citation surface. Appends add chunks without
renumbering old ones; normalized events carry the independently acknowledged
model-processing watermark.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..action_trace import render_action
from ..parsers import split_chunks
from .adapters import TRANSCRIPT_ADAPTERS, event_identity, normalize

READ_BUDGET = 8 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024


def ensure_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS transcript_streams (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            adapter TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1,
            byte_offset INTEGER NOT NULL DEFAULT 0,
            captured_seq INTEGER NOT NULL DEFAULT 0,
            processed_seq INTEGER NOT NULL DEFAULT 0,
            device INTEGER NOT NULL DEFAULT 0, inode INTEGER NOT NULL DEFAULT 0,
            prefix_size INTEGER NOT NULL DEFAULT 0, prefix_hash TEXT NOT NULL DEFAULT '',
            tail_hash TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL DEFAULT '{}',
            read_errors INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS transcript_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            event_id TEXT NOT NULL, native_id TEXT NOT NULL DEFAULT '',
            byte_offset INTEGER NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT '', call_id TEXT NOT NULL DEFAULT '',
            parent_event_id TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT '',
            exit_code INTEGER, failed INTEGER NOT NULL DEFAULT 0,
            artifact_paths_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(document_id,generation,event_id)
        );
        CREATE INDEX IF NOT EXISTS ix_transcript_events_document ON transcript_events(document_id,seq);
        CREATE INDEX IF NOT EXISTS ix_transcript_events_call ON transcript_events(document_id,call_id);
        CREATE TABLE IF NOT EXISTS transcript_receipts (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL,
            PRIMARY KEY(document_id,event_id)
        );
        CREATE TABLE IF NOT EXISTS transcript_messages (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            native_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY(document_id,native_id,role)
        );
    """)


def render_event(event: dict) -> str:
    if event["kind"] == "message":
        who = "用户" if event["role"] == "user" else "AI"
        return f"### {who} · {event['timestamp']}\n{event['text']}"
    return render_action(event["timestamp"], event["tool_name"] or "unknown_tool",
                         result=event["kind"] == "tool_result", failed=bool(event["failed"]),
                         exit_code=event["exit_code"])


def _header(stream: dict) -> str:
    lines = []
    if stream["cwd"]:
        lines.append("工作目录：" + stream["cwd"])
    if stream["session_id"]:
        lines.append("会话 ID：" + stream["session_id"])
    return "\n\n".join(lines)


def pending_batch(con, document_id: int, max_events: int = 80, context_events: int = 8,
                  max_chars: int = 20000, *, after_seq: int | None = None,
                  through_seq: int | None = None) -> dict | None:
    """Snapshot a bounded new-event range with prior dialogue for interpretation.

    ``None`` identifies a legacy non-stream document; an empty content batch is
    a stream with no pending events. Explicit watermarks replay a failed batch.
    """
    row = con.execute("SELECT * FROM transcript_streams WHERE document_id=?", (document_id,)).fetchone()
    if row is None:
        return None
    stream = dict(row)
    start = stream["processed_seq"] if after_seq is None else after_seq
    end = stream["captured_seq"] if through_seq is None else through_seq
    rows = con.execute("SELECT * FROM transcript_events WHERE document_id=? AND generation=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                       (document_id, stream["generation"], start, end, max_events)).fetchall()
    picked, length = [], 0
    for row in rows:
        event = dict(row)
        rendered = render_event(event)
        if picked and length + len(rendered) > max_chars:
            break
        picked.append(event)
        length += len(rendered)
    result = dict(content="", new_content="", generation=stream["generation"],
                  from_seq=start, through_seq=start, new_event_ids=[])
    if not picked:
        return result
    prior = con.execute("SELECT * FROM transcript_events WHERE document_id=? AND generation=? AND seq<=? ORDER BY seq DESC LIMIT ?",
                        (document_id, stream["generation"], start, context_events)).fetchall()
    context = "\n\n".join(render_event(dict(row)) for row in reversed(prior))
    # Keep whole quoted events in the new region; only old context is bounded.
    context = context[-max_chars // 2:]
    new_text = "\n\n".join(render_event(event) for event in picked)
    pieces = [_header(stream)]
    if context:
        pieces.append("前序上下文（仅供理解新增记录，不重复提炼）：\n" + context)
    pieces.append("本次新增记录：\n" + new_text)
    result.update(content="\n\n".join(piece for piece in pieces if piece), new_content=new_text,
                  through_seq=picked[-1]["seq"], new_event_ids=[event["event_id"] for event in picked])
    return result


def ack_batch(con, document_id: int, through_seq: int, generation: int) -> bool:
    return bool(con.execute("UPDATE transcript_streams SET processed_seq=MAX(processed_seq,?),updated_at=datetime('now') WHERE document_id=? AND generation=? AND captured_seq>=?",
                            (through_seq, document_id, generation, through_seq)).rowcount)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprints(handle, offset: int) -> tuple[int, str, str]:
    prefix_size = min(4096, offset)
    handle.seek(0)
    prefix = _digest(handle.read(prefix_size))
    handle.seek(max(0, offset - 256))
    tail = _digest(handle.read(min(offset, 256)))
    return prefix_size, prefix, tail


def _duplicate(con, document_id: int, generation: int, event: dict) -> bool:
    if event["kind"] != "message":
        return False
    # Codex records visible turns both as response_item and event_msg.
    if event["origin"] in ("response_item", "event_msg"):
        rows = con.execute("SELECT timestamp FROM transcript_events WHERE document_id=? AND generation=? AND kind='message' AND role=? AND text=? AND origin!=? ORDER BY seq DESC LIMIT 4",
                           (document_id, generation, event["role"], event["text"], event["origin"]))
        for row in rows:
            if row[0] == event["timestamp"]:
                return True
            try:
                if abs((datetime.fromisoformat(row[0]) - datetime.fromisoformat(event["timestamp"])).total_seconds()) <= 3:
                    return True
            except ValueError:
                pass
    return False


def _message_delta(con, document_id: int, event: dict) -> bool:
    """Claude cumulative snapshots produce a suffix, or a genuine revision."""
    if event["origin"] != "claude" or event["kind"] != "message":
        return True
    row = con.execute("SELECT text FROM transcript_messages WHERE document_id=? AND native_id=? AND role=?",
                      (document_id, event["native_id"], event["role"])).fetchone()
    text = event["text"]
    if row:
        old = row[0]
        if text == old or (text and old.startswith(text)):
            return False
        if text.startswith(old):
            event["text"] = text[len(old):].lstrip()
    con.execute("INSERT INTO transcript_messages(document_id,native_id,role,text) VALUES(?,?,?,?) ON CONFLICT(document_id,native_id,role) DO UPDATE SET text=excluded.text",
                (document_id, event["native_id"], event["role"], text))
    return bool(event["text"])


def _append_chunks(con, document_id: int, text: str, start_offset: int):
    ordinal = con.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM chunks WHERE document_id=?", (document_id,)).fetchone()[0]
    for index, (start, end, chunk) in enumerate(split_chunks(text)):
        con.execute("INSERT INTO chunks(document_id,ordinal,start_offset,end_offset,text) VALUES(?,?,?,?,?)",
                    (document_id, ordinal + index, start_offset + start, start_offset + end, chunk))


def collect_transcript(con, path: Path, src: dict, previous: dict | None, stat) -> dict:
    """Read only new bytes and persist events, cursor and citation updates atomically."""
    adapter = src.get("adapter") or src["kind"]
    document_id = previous["id"] if previous else None
    row = con.execute("SELECT * FROM transcript_streams WHERE document_id=?", (document_id,)).fetchone() if document_id else None
    stream = dict(row) if row else None
    if stream and previous["size_bytes"] == stat.st_size and previous["mtime_ns"] == stat.st_mtime_ns and stream["byte_offset"] == stat.st_size and (stat.st_dev, stat.st_ino) == (stream["device"], stream["inode"]):
        return {"state": None, "errors": 0, "document_id": document_id, "reset": False}
    migrating = previous is not None and stream is None
    state = json.loads(stream["state_json"]) if stream else {}
    if migrating:
        job = con.execute("SELECT state FROM ai_jobs WHERE document_id=?", (document_id,)).fetchone()
        state.update(legacy_size=previous["size_bytes"], legacy_processed=bool(job and job[0] == "done"))
    generation = stream["generation"] if stream else 1
    offset = stream["byte_offset"] if stream else 0
    reset = False
    read_errors = 0
    added = []
    with path.open("rb") as handle:
        if migrating:
            # A one-time 1.2.1 upgrade can encounter a file that grew while the
            # app was closed. Its old raw-file digest distinguishes append from
            # rewrite; only the old prefix is read for this migration check.
            old_digest = hashlib.sha256()
            remaining = previous["size_bytes"]
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                old_digest.update(block)
                remaining -= len(block)
            if remaining or old_digest.hexdigest() != previous["sha256"]:
                reset = True
                state = {}
        if stream:
            reset = stat.st_size < offset
            # Same-length rewrites can change only the middle of the file.
            if stat.st_size == offset and stat.st_mtime_ns != previous["mtime_ns"]:
                reset = True
            if not reset and offset:
                handle.seek(0)
                prefix = _digest(handle.read(stream["prefix_size"]))
                handle.seek(max(0, offset - 256))
                tail = _digest(handle.read(min(offset, 256)))
                reset = prefix != stream["prefix_hash"] or tail != stream["tail_hash"]
            if reset:
                generation += 1
                offset, state = 0, {}
                # Hash-only receipts let a truncated/rotated log replay the
                # already processed prefix without another model call.
                con.execute("INSERT OR IGNORE INTO transcript_receipts(document_id,event_id) SELECT document_id,event_id FROM transcript_events WHERE document_id=? AND seq<=?",
                            (document_id, stream["processed_seq"]))
                con.execute("DELETE FROM transcript_events WHERE document_id=?", (document_id,))
                con.execute("DELETE FROM transcript_messages WHERE document_id=?", (document_id,))
                state["replay_processed_prefix"] = True
        if document_id is None:
            cursor = con.execute("INSERT INTO documents(source_id,path,relative_path,title,file_type,project,content,sha256,size_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,'',?,?)",
                                 (src["id"], str(path), str(path.relative_to(Path(src["root"]))), path.stem, path.suffix.lower(), src["name"], "", stat.st_size, stat.st_mtime_ns))
            document_id = cursor.lastrowid
        state.setdefault("session_id", path.stem)
        state.setdefault("cwd", "")
        state.setdefault("parent_session_id", "")
        captured = 0 if reset else (stream["captured_seq"] if stream else 0)
        processed = 0 if reset else (stream["processed_seq"] if stream else 0)
        handle.seek(offset)
        start_offset = offset
        while handle.tell() - start_offset < READ_BUDGET:
            line_offset = handle.tell()
            line = handle.readline(MAX_RECORD_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_BYTES:
                while line and not line.endswith(b"\n"):
                    line = handle.readline(MAX_RECORD_BYTES + 1)
                offset = handle.tell()
                read_errors += 1
                continue
            if not line.strip():
                offset = handle.tell()
                continue
            try:
                raw = json.loads(line)
            except (ValueError, UnicodeError):
                if not line.endswith(b"\n"):
                    # A writer has not finished the tail; retry these bytes.
                    break
                read_errors += 1
                offset = handle.tell()
                continue
            offset = handle.tell()
            if not isinstance(raw, dict):
                continue
            events = normalize(adapter, raw, state, line_offset)
            for event in events:
                if _duplicate(con, document_id, generation, event):
                    continue
                if not _message_delta(con, document_id, event):
                    continue
                if event["kind"] == "tool_result" and not event["tool_name"]:
                    call = con.execute("SELECT tool_name FROM transcript_events WHERE document_id=? AND generation=? AND call_id=? AND kind='tool_call' ORDER BY seq DESC LIMIT 1",
                                       (document_id, generation, event["call_id"])).fetchone()
                    event["tool_name"] = call[0] if call else "unknown_tool"
                event_id = event_identity(adapter, state["session_id"], event)
                artifact_paths = _authorized_paths(con, event["artifact_paths"], state["cwd"])
                cursor = con.execute("""INSERT OR IGNORE INTO transcript_events(document_id,generation,event_id,byte_offset,kind,role,timestamp,text,native_id,tool_name,call_id,parent_event_id,origin,exit_code,failed,artifact_paths_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (document_id, generation, event_id, line_offset, event["kind"], event["role"], event["timestamp"], event["text"], event["native_id"], event["tool_name"], event["call_id"], event["parent_event_id"], event["origin"], event["exit_code"], event["failed"], json.dumps(artifact_paths)))
                if not cursor.rowcount:
                    continue
                captured = cursor.lastrowid
                if state.get("replay_processed_prefix"):
                    if con.execute("SELECT 1 FROM transcript_receipts WHERE document_id=? AND event_id=?", (document_id, event_id)).fetchone():
                        processed = captured
                    else:
                        state["replay_processed_prefix"] = False
                if line_offset < state.get("legacy_size", 0):
                    if state.get("legacy_processed"):
                        processed = captured
                else:
                    added.append(event)
        prefix_size, prefix, tail = _fingerprints(handle, offset)

    if offset >= state.get("legacy_size", 0):
        state.pop("legacy_size", None)
        state.pop("legacy_processed", None)
    con.execute("""INSERT INTO transcript_streams(document_id,source_id,adapter,session_id,cwd,parent_session_id,generation,byte_offset,captured_seq,processed_seq,device,inode,prefix_size,prefix_hash,tail_hash,state_json,read_errors)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
        session_id=excluded.session_id,cwd=excluded.cwd,parent_session_id=excluded.parent_session_id,
        generation=excluded.generation,byte_offset=excluded.byte_offset,captured_seq=excluded.captured_seq,
        processed_seq=excluded.processed_seq,device=excluded.device,inode=excluded.inode,
        prefix_size=excluded.prefix_size,prefix_hash=excluded.prefix_hash,tail_hash=excluded.tail_hash,
        state_json=excluded.state_json,read_errors=transcript_streams.read_errors+excluded.read_errors,updated_at=datetime('now')""",
        (document_id, src["id"], adapter, state["session_id"], state["cwd"], state["parent_session_id"], generation, offset, captured, processed, stat.st_dev, stat.st_ino, prefix_size, prefix, tail, json.dumps(state), read_errors))
    old_content = previous["content"] if previous else ""
    body = "\n\n".join(render_event(event) for event in added)
    if reset:
        con.execute("UPDATE knowledge_evidence SET is_current=0,chunk_id=NULL WHERE document_id=?", (document_id,))
        con.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        con.execute("DELETE FROM knowledge_proposals WHERE document_id=?", (document_id,))
        old_content = ""
    if body and not old_content:
        body = "\n\n".join(part for part in (_header(state), body) if part)
    addition = ("\n\n" if old_content and body else "") + body
    content = old_content + addition
    digest = _digest(content.encode())
    title = previous["title"] if previous else path.stem
    if not previous or not previous["content"]:
        question = next((event["text"] for event in added if event["role"] == "user"), "")
        title = " ".join(question.split())[:76] or title
    project = Path(state["cwd"]).name if state["cwd"] else src["name"]
    con.execute("UPDATE documents SET title=?,project=CASE WHEN project_verified=1 THEN project ELSE ? END,content=?,sha256=?,size_bytes=?,mtime_ns=?,indexed_at=datetime('now') WHERE id=?",
                (title, project, content, digest, stat.st_size, stat.st_mtime_ns, document_id))
    if addition:
        _append_chunks(con, document_id, addition, len(old_content))
    if reset:
        from ..reconcile import review_flags
        chunks = con.execute("SELECT id,text FROM chunks WHERE document_id=?", (document_id,)).fetchall()
        affected = set()
        for evidence in con.execute("SELECT id,knowledge_id,quote FROM knowledge_evidence WHERE document_id=?", (document_id,)).fetchall():
            affected.add(evidence["knowledge_id"])
            if evidence["quote"] in content:
                chunk = next((chunk for chunk in chunks if evidence["quote"][:60] in chunk["text"]), None)
                con.execute("UPDATE knowledge_evidence SET is_current=1,chunk_id=? WHERE id=?", (chunk["id"] if chunk else None, evidence["id"]))
        review_flags(con, affected)
    if reset:
        con.execute("DELETE FROM ai_jobs WHERE document_id=?", (document_id,))
    if src.get("allow_ai") and captured > processed:
        con.execute("""INSERT INTO ai_jobs(document_id,content_sha,state,attempts,error) VALUES(?,?,'queued',0,NULL)
            ON CONFLICT(document_id) DO UPDATE SET
            content_sha=CASE WHEN ai_jobs.state='running' THEN ai_jobs.content_sha ELSE excluded.content_sha END,
            state=CASE WHEN ai_jobs.state IN ('running','error') THEN ai_jobs.state ELSE 'queued' END,
            updated_at=CASE WHEN ai_jobs.state='running' THEN ai_jobs.updated_at ELSE datetime('now') END""", (document_id, digest))
    return {"state": "new" if previous is None else ("updated" if added or reset else None),
            "errors": read_errors, "document_id": document_id, "reset": reset}


def _authorized_paths(con, candidates: list[str], cwd: str) -> list[str]:
    if not candidates:
        return []
    from ..parsers import EXCLUDED_FILES, EXCLUDED_FOLDERS
    roots = [Path(row[0]).resolve() for row in con.execute("SELECT root FROM sources WHERE enabled=1 AND COALESCE(adapter,kind)='folder'")]
    paths = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            if not cwd:
                continue
            path = Path(cwd) / path
        path = path.resolve()
        name = path.name.lower()
        if name in EXCLUDED_FILES or name.startswith('.env') or name.endswith(('.pem', '.key', '.p12', '.pfx')):
            continue
        if any(part in EXCLUDED_FOLDERS for part in path.parts):
            continue
        if any(path.is_relative_to(root) for root in roots):
            paths.append(str(path))
    return list(dict.fromkeys(paths))
