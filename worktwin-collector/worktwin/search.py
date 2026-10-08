"""Fully offline lexical retrieval for Chinese and English work records.

FTS5 trigram supplies candidates, then evidence gets a deterministic overlap
score. Questions need not be exact substrings of a document. No hallucinated
semantic matches, external API or model download is involved.
"""
from __future__ import annotations

import re
import sqlite3

STOP = ("为什么", "是什么", "哪些", "什么", "如何", "怎么", "请问", "帮我", "是否", "是不是", "当初", "当时",
        "我们", "你们", "他们", "我想", "之前", "以前", "这个", "那个", "这么", "以及", "还有", "能够", "可以", "应该", "需要",
        "想知道", "解释", "一下", "相关", "情况", "背景", "原因", "目前", "最新", "记录", "说过")


def terms_for(query: str) -> list[str]:
    """Overlapping lexical anchors preserve Chinese without installing NLP models."""
    cleaned = query.casefold()
    for word in STOP:
        cleaned = cleaned.replace(word, " ")
    runs = re.findall(r"[\u3400-\u9fff]+|[a-z0-9_+.-]{3,}", cleaned)
    terms = []
    for run in runs:
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            if len(run) <= 4:
                terms.append(run)
            else:
                terms.extend(run[i:i+3] for i in range(len(run)-2))
                terms.extend(run[i:i+2] for i in range(len(run)-1))
        else:
            terms.append(run)
    # Stable dedup; prefer longer anchors in FTS candidate retrieval.
    return list(dict.fromkeys(terms))[:48]


def relevance(text: str, title: str, query: str, terms: list[str]) -> tuple[float, int]:
    src, head, question = text.casefold(), title.casefold(), query.casefold()
    if not src and not head:
        return 0, 0
    direct = bool(question and (question in src or question in head))
    matches = [word for word in terms if word in src or word in head]
    if not matches and not direct:
        return 0, 0
    # Count distinct anchors, not repeated occurrences in verbose transcripts.
    # Longer matches and terms in the title provide stronger evidence.
    score = sum((1.2 if len(w) >= 3 else 0.55) + (1.4 if w in head else 0) for w in matches)
    if direct:
        score += 12
    return round(score, 3), len(matches)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(con: sqlite3.Connection, query: str, limit: int = 30) -> dict:
    query = query.strip()[:160]
    if not query:
        return {"query": "", "documents": [], "knowledge": []}
    limit = max(1, min(limit, 100))
    terms = terms_for(query)
    candidates = []
    long_terms = sorted((t for t in terms if len(t) >= 3), key=lambda t: -len(t))[:24]
    if long_terms:
        match_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
        try:
            candidates = con.execute("""
                SELECT c.id chunk_id,c.text chunk_text,c.start_offset,c.document_id,
                       d.title,d.project,d.path,d.relative_path,d.file_type,s.name source_name
                FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                JOIN documents d ON d.id=c.document_id
                JOIN sources s ON s.id=d.source_id
                WHERE chunks_fts MATCH ? LIMIT 1200""", (match_query,)).fetchall()
        except sqlite3.OperationalError:
            candidates = []
    if not candidates:
        # Two-character Chinese queries can't use trigram FTS. If tokenized
        # natural-language query misses FTS, use title + content LIKE anchors.
        fallback = sorted(terms, key=lambda t: -len(t))[:5] or [query]
        conditions = []
        params = []
        for term in fallback:
            conditions.append("(c.text LIKE ? ESCAPE '\\' OR d.title LIKE ? ESCAPE '\\')")
            pat = '%' + _escape_like(term) + '%'
            params.extend([pat, pat])
        if conditions:
            candidates = con.execute("""
                SELECT c.id chunk_id,c.text chunk_text,c.start_offset,c.document_id,
                       d.title,d.project,d.path,d.relative_path,d.file_type,s.name source_name
                FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN sources s ON s.id=d.source_id
                WHERE """ + " OR ".join(conditions) + " LIMIT 1200",params).fetchall()
    results = []
    for row in candidates:
        value, count = relevance(row["chunk_text"], row["title"], query, terms)
        if not value:
            continue
        chunk = row["chunk_text"]
        anchors = sorted((t for t in terms if t in chunk.casefold()), key=lambda t: -len(t))
        term = query if query.casefold() in chunk.casefold() else (anchors[0] if anchors else "")
        position = chunk.casefold().find(term.casefold()) if term else 0
        position = max(0, position)
        excerpt = chunk[max(0,position-110):min(len(chunk),position+340)].strip()
        results.append({"chunk_id":row["chunk_id"], "document_id":row["document_id"],
                        "title":row["title"], "project":row["project"], "path":row["path"],
                        "relative_path":row["relative_path"], "source_name":row["source_name"],
                        "excerpt":excerpt, "score":value, "matched_terms":count,
                        "offset":row["start_offset"]+position})
    results.sort(key=lambda hit:(-hit["score"],-hit["matched_terms"],hit["document_id"],hit["chunk_id"]))
    # Use one best-matching fragment per document to diversify evidence.
    unique = {}
    for hit in results:
        unique.setdefault(hit["document_id"], hit)
        if len(unique) == limit:
            break

    # Candidate knowledge can be retrieved even when the original file is gone,
    # but source-bound knowledge with stale evidence should be visibly marked.
    krows = con.execute("""SELECT id,kind,title,body,status,needs_review
                           FROM knowledge WHERE status!='archived'
                           ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END,
                           updated_at DESC LIMIT 3000""").fetchall()
    knowledge = []
    for item in krows:
        value, count = relevance(item["body"], item["title"], query, terms)
        if value:
            entry = dict(item)
            entry["score"] = value + (1.5 if item["status"] == "confirmed" else 0) - (10 if item["needs_review"] else 0)
            entry["matched_terms"] = count
            knowledge.append(entry)
    knowledge.sort(key=lambda entry: -entry["score"])
    return {"query":query,"documents":list(unique.values()),"knowledge":knowledge[:limit]}
