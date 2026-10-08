"""Grounded, editable knowledge entries and conservative legacy rule helpers."""

from __future__ import annotations

import hashlib
import re
import sqlite3

KIND_LABELS = {"fact": "事实", "decision": "决策", "process": "流程", "preference": "偏好"}
PATTERNS = {
    "decision": re.compile(r"(决定|最终|结论|采用|选择|不采用|不做|舍弃|方案|权衡|取舍|优先选择|因此我们)", re.I),
    "preference": re.compile(r"(我希望|我倾向|我认为|我习惯|我更|我建议|我通常|以后.*?不要|偏好|更倾向|优先考虑)", re.I),
    "process": re.compile(r"(步骤|操作流程|执行流程|第一步|第二步|首先|然后|最后|检查清单|如何操作|处理方法)", re.I),
}


TURN_HEADING = re.compile(r"^### (用户|AI) · ([^\n]*)\n", re.M)


def user_turns(text: str) -> list[tuple[str, str]]:
    """Visible user utterances and their original timestamps from a session.

    Never treat generated assistant messages, tool results or session metadata
    as statements made by the employee.
    """
    headings = list(TURN_HEADING.finditer(text))
    result = []
    for idx, match in enumerate(headings):
        if match.group(1) != "用户":
            continue
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        value = text[match.end():end].strip()
        if value:
            result.append((value, match.group(2).strip()[:32]))
    return result


def candidates_from_text(text: str, max_candidates: int = 8, *, transcript: bool = False) -> list[dict[str, str]]:
    """Extract attributable, verbatim observations, never infer from AI suggestions.

    Session transcripts are structured by their visible speaker headings. Only
    USER statements can become candidates; AI text stays searchable as evidence
    but is not automatically treated as the employee's decision or preference.
    """
    statements = user_turns(text) if transcript else [(text, "")]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for statement, occurred_at in statements:
        blocks = re.split(r"\n\s*\n|(?<=[。！？])(?=\S)", statement)
        for raw in blocks:
            paragraph = raw.strip().replace("\r", "")
            if len(paragraph) < 17 or len(paragraph) > 950:
                continue
            # A question about an earlier decision isn't itself a decision.
            if (paragraph.endswith(("?", "？")) and not
                    re.search(r"(最终决定|决定采用|决定使用|不再采用|我的结论)", paragraph)):
                continue
            kind = next((k for k in ("preference", "decision", "process")
                         if PATTERNS[k].search(paragraph)), None)
            if not kind:
                continue
            fingerprint = hashlib.sha256(paragraph.encode("utf-8")).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            title = re.sub(r"^[\s\-#*\d.、]+", "", paragraph.split("\n")[0]).strip()
            title = title[:48] + ("…" if len(title) > 48 else "")
            out.append({"kind": kind, "title": title, "body": paragraph,
                        "quote": paragraph, "fingerprint": fingerprint,
                        "occurred_at": occurred_at})
            if len(out) >= max_candidates:
                return out
    return out


def store_candidates(con: sqlite3.Connection, doc_id: int, chunks: list[tuple[int, int, str]], candidates: list[dict[str,str]], created_by: str = "rules") -> int:
    added = 0
    # Resolve chunk positions to real database IDs for clickable evidence links.
    rows = con.execute("SELECT id, text FROM chunks WHERE document_id=? ORDER BY ordinal", (doc_id,)).fetchall()
    project = con.execute("SELECT project FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    for item in candidates:
        # Scope de-duplication to project and kind. Two departments repeating
        # the same sentence must not silently share one knowledge entry.
        digest_source = project + "\0" + item["kind"] + "\0" + item["quote"]
        fingerprint = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        cursor = con.execute("INSERT OR IGNORE INTO knowledge(kind,title,body,created_by,source_bound,fingerprint) VALUES(?,?,?,?,1,?)",
                             (item["kind"], item["title"][:130], item["body"], created_by, fingerprint))
        row = con.execute("SELECT id FROM knowledge WHERE fingerprint=?", (fingerprint,)).fetchone()
        if not row:
            continue
        know_id = int(row[0])
        part = next((r for r in rows if item["quote"][:60] in r["text"]), None)
        con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,chunk_id,quote,is_current,occurred_at) VALUES(?,?,?,?,1,?) "
                    "ON CONFLICT(knowledge_id,document_id,quote) DO UPDATE SET is_current=1,chunk_id=excluded.chunk_id,"
                    "occurred_at=COALESCE(excluded.occurred_at,knowledge_evidence.occurred_at)",
                    (know_id, doc_id, part["id"] if part else None, item["quote"][:1200], item.get("occurred_at") or None))
        added += int(cursor.rowcount > 0)
    return added
