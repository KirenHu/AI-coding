"""Capture contracts: new bytes, durable receipts and unchanged public citations."""
import json
from pathlib import Path

from worktwin.collector import Collector
from worktwin.db import Database
from worktwin.ingest import ack_batch, pending_batch


def codex(text, *, role="user", minute=0):
    return dict(type="response_item", timestamp=f"2026-10-10T10:{minute:02d}:00Z",
                payload=dict(type="message", role=role, content=[dict(type="input_text", text=text)]))


def write(path, events, mode="w"):
    with path.open(mode, encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def setup(tmp_path, adapter="codex"):
    root = tmp_path / "sessions"
    root.mkdir()
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,adapter,root,allow_ai) VALUES('会话','folder',?,?,1)", (adapter, str(root)))
    return root / "one.jsonl", db, Collector(db)


def read_state(db):
    with db.connect() as con:
        return dict(con.execute("SELECT * FROM transcript_streams").fetchone())


def acknowledge(db):
    with db.connect() as con:
        stream = con.execute("SELECT * FROM transcript_streams").fetchone()
        ack_batch(con, stream["document_id"], stream["captured_seq"], stream["generation"])
        con.execute("UPDATE ai_jobs SET state='done'")


def test_append_preserves_chunk_and_evidence_identity_and_separate_progress(tmp_path):
    path, db, collector = setup(tmp_path)
    write(path, [dict(type="session_meta", payload=dict(id="session-one", cwd="/work/app")), codex("最初的项目决定")])
    assert collector.scan_all()["new"] == 1
    with db.connect() as con:
        chunk = dict(con.execute("SELECT * FROM chunks").fetchone())
        con.execute("INSERT INTO knowledge(kind,title,body,status) VALUES('decision','决定','最初的项目决定','confirmed')")
        con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,chunk_id,quote) VALUES(1,?,?,?)", (chunk["document_id"], chunk["id"], "最初的项目决定"))
    acknowledge(db)
    before = read_state(db)
    write(path, [codex("新增的执行步骤", minute=1)], "a")
    assert collector.scan_all()["updated"] == 1
    after = read_state(db)
    assert after["processed_seq"] == before["processed_seq"]
    assert after["captured_seq"] > before["captured_seq"]
    with db.connect() as con:
        assert dict(con.execute("SELECT * FROM chunks WHERE id=?", (chunk["id"],)).fetchone()) == chunk
        evidence = con.execute("SELECT * FROM knowledge_evidence").fetchone()
        assert evidence["is_current"] == 1 and evidence["chunk_id"] == chunk["id"]
        batch = pending_batch(con, after["document_id"])
        assert "新增的执行步骤" in batch["new_content"]
        assert "最初的项目决定" not in batch["new_content"]
        assert "最初的项目决定" in batch["content"]
    assert collector.scan_all() == dict(new=0, updated=0, deleted=0, errors=0)


def test_partial_utf8_tail_waits_for_completion_and_survives_reopen(tmp_path):
    path, db, collector = setup(tmp_path)
    write(path, [codex("已完整保存")])
    collector.scan_all()
    before = read_state(db)
    tail = (json.dumps(codex("尾部中文消息", minute=1), ensure_ascii=False) + "\n").encode()
    split = tail.index("中文".encode()) + 1
    with path.open("ab") as handle:
        handle.write(tail[:split])
    collector.scan_all()
    assert read_state(db)["byte_offset"] == before["byte_offset"]
    with path.open("ab") as handle:
        handle.write(tail[split:])
    reopened = Database(db.path)
    assert Collector(reopened).scan_all()["updated"] == 1
    with reopened.connect() as con:
        assert con.execute("SELECT text FROM transcript_events WHERE text='尾部中文消息'").fetchall()
    assert read_state(reopened)["byte_offset"] == path.stat().st_size


def test_rotation_same_prefix_appends_without_reprocessing_and_truncate_replays_receipts(tmp_path):
    path, db, collector = setup(tmp_path)
    initial = [dict(type="session_meta", payload=dict(id="same-session")), codex("第一条"), codex("第二条", minute=1)]
    write(path, initial)
    collector.scan_all()
    acknowledge(db)
    before = read_state(db)
    replacement = path.with_suffix(".replacement")
    write(replacement, initial + [codex("第三条", minute=2)])
    replacement.replace(path)
    collector.scan_all()
    with db.connect() as con:
        batch = pending_batch(con, before["document_id"])
        assert "第三条" in batch["new_content"] and "第一条" not in batch["new_content"]
    assert read_state(db)["generation"] == before["generation"]
    acknowledge(db)
    # A log writer truncates back to its saved prefix. Already handled events
    # remain handled even though the active document must be rebuilt.
    write(path, initial)
    collector.scan_all()
    with db.connect() as con:
        assert pending_batch(con, before["document_id"])["content"] == ""
        assert con.execute("SELECT content FROM documents").fetchone()[0].count("第一条") == 1


def test_new_same_size_rewrite_keeps_document_id_and_invalidates_old_batch(tmp_path):
    path, db, collector = setup(tmp_path)
    write(path, [codex("旧的记录")])
    collector.scan_all()
    before = read_state(db)
    write(path, [codex("新的记录")])
    collector.scan_all()
    after = read_state(db)
    assert after["document_id"] == before["document_id"]
    assert after["generation"] == before["generation"] + 1
    with db.connect() as con:
        assert not ack_batch(con, before["document_id"], before["captured_seq"], before["generation"])
        assert "新的记录" in pending_batch(con, before["document_id"])["new_content"]


def test_claude_cumulative_messages_emit_only_growth_or_actual_revision(tmp_path):
    path, db, collector = setup(tmp_path, "claude")
    def event(text, minute):
        return dict(type="assistant", sessionId="claude-1", parentUuid="user-1", timestamp=f"2026-10-10T10:{minute:02d}:00Z",
                    message=dict(id="m1", role="assistant", content=[dict(type="text", text=text)]))
    write(path, [event("先保留当前方案。", 0)])
    collector.scan_all()
    acknowledge(db)
    write(path, [event("先保留当前方案。", 1), event("先保留当前方案。新增一项灰度发布步骤。", 2)], "a")
    collector.scan_all()
    with db.connect() as con:
        batch = pending_batch(con, read_state(db)["document_id"])
        assert "新增一项灰度发布步骤。" in batch["new_content"]
        assert "先保留当前方案。" not in batch["new_content"]
        assert con.execute("SELECT count(*) FROM transcript_events").fetchone()[0] == 2
        assert con.execute("SELECT parent_event_id FROM transcript_events LIMIT 1").fetchone()[0] == "user-1"
    acknowledge(db)
    write(path, [event("调整为新的发布方案。", 3)], "a")
    collector.scan_all()
    with db.connect() as con:
        assert "调整为新的发布方案。" in pending_batch(con, read_state(db)["document_id"])["new_content"]


def test_cursor_cli_partial_flush_and_complete_messages_do_not_duplicate(tmp_path):
    path, db, collector = setup(tmp_path, "cursor")
    def assistant(text, **extra):
        return dict(type="assistant", session_id="cursor-1", message=dict(role="assistant", content=[dict(type="text", text=text)]), **extra)
    write(path, [dict(type="system", subtype="init", session_id="cursor-1", cwd="/work/project"),
                 assistant("I'll ", timestamp_ms=1791600000000), assistant("check it.", timestamp_ms=1791600000010)])
    collector.scan_all()
    # Pending deltas survive another collector instance before the flush.
    write(path, [assistant("I'll check it.", timestamp_ms=1791600000020, model_call_id="m1"),
                 dict(type="tool_call", subtype="started", call_id="c1", tool_call=dict(readToolCall=dict(args=dict(path="secret.txt")))),
                 assistant("I'll check it."),
                 dict(type="result", result="I'll check it.", session_id="cursor-1")], "a")
    Collector(Database(db.path)).scan_all()
    with db.connect() as con:
        content = con.execute("SELECT content FROM documents").fetchone()[0]
        assert content.count("I'll check it.") == 1
        assert "工具：readToolCall" in content
        assert "secret.txt" not in content


def test_only_authorized_structured_artifact_paths_survive_tool_normalization(tmp_path):
    path, db, collector = setup(tmp_path, "claude")
    allowed = tmp_path / "files"
    allowed.mkdir()
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,root) VALUES('文件','folder',?)", (str(allowed),))
    write(path, [dict(type="assistant", sessionId="claude-1", cwd=str(allowed), message=dict(role="assistant", content=[
        dict(type="thinking", thinking="private reasoning"),
        dict(type="tool_use", id="one", name="Write", input=dict(file_path="result.py", content="secret raw payload")),
        dict(type="tool_use", id="two", name="Write", input=dict(file_path=".env", content="secret raw payload")),
        dict(type="tool_use", id="three", name="Write", input=dict(file_path=str(tmp_path / "outside.py"), content="secret raw payload")),
    ]))])
    collector.scan_all()
    with db.connect() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM transcript_events ORDER BY seq")]
        assert json.loads(rows[0]["artifact_paths_json"]) == [str(allowed / "result.py")]
        assert json.loads(rows[1]["artifact_paths_json"]) == []
        assert json.loads(rows[2]["artifact_paths_json"]) == []
        assert "private reasoning" not in json.dumps(rows)
        assert "secret raw payload" not in json.dumps(rows)


def test_oversize_unreadable_and_incomplete_walk_are_not_deletions(tmp_path, monkeypatch):
    import worktwin.collector as module
    root = tmp_path / "files"
    root.mkdir()
    path = root / "note.md"
    path.write_text("已经保存的有效资料", encoding="utf-8")
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,root) VALUES('资料','folder',?)", (str(root),))
    collector = Collector(db)
    collector.scan_all()
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 2)
    result = collector.scan_all()
    assert result["deleted"] == 0 and result["errors"] == 1
    monkeypatch.setattr(module, "source_files", lambda *args, **kwargs: iter(()))
    assert collector.scan_all()["deleted"] == 0
    with db.connect() as con:
        assert con.execute("SELECT content FROM documents").fetchone()[0] == "已经保存的有效资料"
    path.unlink()
    assert collector.scan_all()["deleted"] == 1


def test_temporarily_unreadable_transcript_preserves_cursor_and_knowledge_source(tmp_path, monkeypatch):
    path, db, collector = setup(tmp_path)
    write(path, [codex("已保存的会话证据")])
    collector.scan_all()
    before = read_state(db)
    write(path, [codex("还未读取的记录", minute=1)], "a")
    actual_open = Path.open
    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("暂时不可读取")
        return actual_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", unavailable)
    result = collector.scan_all()
    assert result["errors"] == 1 and result["deleted"] == 0
    assert read_state(db)["byte_offset"] == before["byte_offset"]
    with db.connect() as con:
        assert "已保存的会话证据" in con.execute("SELECT content FROM documents").fetchone()[0]
    monkeypatch.setattr(Path, "open", actual_open)
    assert collector.scan_all()["updated"] == 1


def test_large_transcript_append_reads_only_new_bytes_and_fixed_fingerprints(tmp_path, monkeypatch):
    path, db, collector = setup(tmp_path)
    # 65 MiB of ignored reasoning exercises the former 64 MiB file gate without
    # copying this material into either events or the public document.
    ignored = (json.dumps(dict(type="response_item", payload=dict(type="reasoning", text="x" * 1024))) + "\n").encode()
    with path.open("wb") as handle:
        for _ in range((65 * 1024 * 1024) // len(ignored) + 1):
            handle.write(ignored)
    write(path, [codex("首采有效记录")], "a")
    for _ in range(12):
        collector.scan_all()
        if read_state(db)["byte_offset"] == path.stat().st_size:
            break
    assert path.stat().st_size > 64 * 1024 * 1024
    assert read_state(db)["byte_offset"] == path.stat().st_size
    acknowledge(db)
    old_size = path.stat().st_size
    write(path, [codex("追加记录" + "a" * 2000, minute=1)], "a")
    appended = path.stat().st_size - old_size
    actual_open = Path.open
    read_bytes = 0
    class CountingFile:
        def __init__(self, inner):
            self.inner = inner
        def __enter__(self):
            self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def __getattr__(self, key):
            return getattr(self.inner, key)
        def read(self, *args):
            nonlocal read_bytes
            result = self.inner.read(*args)
            read_bytes += len(result)
            return result
        def readline(self, *args):
            nonlocal read_bytes
            result = self.inner.readline(*args)
            read_bytes += len(result)
            return result
    def counted_open(self, *args, **kwargs):
        handle = actual_open(self, *args, **kwargs)
        return CountingFile(handle) if self == path and args and args[0] == "rb" else handle
    monkeypatch.setattr(Path, "open", counted_open)
    assert collector.scan_all()["updated"] == 1
    assert read_bytes <= appended + 2 * (4096 + 256)
    first_read_bytes = read_bytes
    assert collector.scan_all()["updated"] == 0
    assert read_bytes == first_read_bytes
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM transcript_events").fetchone()[0] == 2
    print(f"PERF initial_bytes={old_size} append_bytes={appended} append_read_bytes={first_read_bytes} unchanged_read_bytes=0 events=1+1")
