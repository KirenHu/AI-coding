import json
from pathlib import Path

from worktwin.codex import parse_codex_session


def test_codex_transcript_hides_reasoning_and_deduplicates(tmp_path: Path):
    path = tmp_path / "one.jsonl"
    lines = [
        {"type":"session_meta","payload":{"id":"session_123","cwd":"/work/demo"}},
        {"timestamp":"2026-09-01T08:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"为什么选择审批节点？"}]}},
        {"timestamp":"2026-09-01T08:00:00Z","type":"event_msg","payload":{"type":"user_message","message":"为什么选择审批节点？"}},
        {"timestamp":"2026-09-01T08:01:00Z","type":"response_item","payload":{"type":"reasoning","summary":[{"text":"private thought hidden"}]}},
        {"timestamp":"2026-09-01T08:02:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"最终决定将审批作为流程节点。"}]}},
        {"timestamp":"2026-09-01T08:02:00Z","type":"event_msg","payload":{"type":"agent_message","message":"最终决定将审批作为流程节点。"}},
    ]
    path.write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in lines))
    title,text = parse_codex_session(path)
    assert title.startswith("为什么选择审批节点")
    assert text.count("为什么选择审批节点？") == 1
    assert text.count("最终决定将审批作为流程节点。") == 1
    assert "private thought hidden" not in text
    assert "session_123" in text


def test_codex_event_messages_fallback(tmp_path: Path):
    path = tmp_path / "fallback.jsonl"
    path.write_text(json.dumps({"timestamp":"2026-09-01T09:00:00Z","type":"event_msg","payload":{"type":"user_message","message":"处理文档格式"}},ensure_ascii=False))
    title, text = parse_codex_session(path)
    assert "处理文档格式" in text


def test_codex_directory_is_indexed_end_to_end(tmp_path: Path):
    from worktwin.db import Database
    from worktwin.collector import Collector
    from worktwin.search import search
    root = tmp_path / "sessions"
    day = root / "2026" / "10" / "08"
    day.mkdir(parents=True)
    sample = day / "rollout-001.jsonl"
    events = [
        {"timestamp":"2026-10-08T11:00:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"我希望这个产品的核心是知识来源追溯。"}]}},
        {"timestamp":"2026-10-08T11:00:10Z","type":"response_item","payload":{"type":"reasoning","text":"sensitive internal thoughts"}},
        {"timestamp":"2026-10-08T11:01:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"最终决定保留原始会话引用。"}]}},
    ]
    sample.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        con.execute("INSERT INTO sources(name,kind,root) VALUES(?,?,?)",("Codex","codex",str(root)))
    assert Collector(db).scan_all()["new"] == 1
    with db.connect() as con:
        assert len(search(con, "知识来源")["documents"]) == 1
        text = con.execute("SELECT content FROM documents").fetchone()[0]
        assert "sensitive internal thoughts" not in text
        assert "最终决定" in text


def test_codex_mixed_events_keep_unique_turns(tmp_path: Path):
    path=tmp_path/'mixed.jsonl'
    entries=[
      {'timestamp':'2026-10-08T11:00:00Z','type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'第一条用户要求'}]}},
      {'timestamp':'2026-10-08T11:00:01Z','type':'event_msg','payload':{'type':'user_message','message':'第一条用户要求'}},
      {'timestamp':'2026-10-08T11:04:00Z','type':'event_msg','payload':{'type':'user_message','message':'第二条用户要求：补充一个条件'}},
      {'timestamp':'2026-10-08T11:07:00Z','type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'已经调整第一条条件。'}]}},
    ]
    path.write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in entries),encoding='utf-8')
    _, transcript=parse_codex_session(path)
    assert transcript.count('第一条用户要求')==1
    assert '第二条用户要求：补充一个条件' in transcript
