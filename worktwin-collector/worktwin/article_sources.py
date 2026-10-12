"""Source passages inherited from an authorized article's current references."""
import re


def source_passages(con, knowledge_id: int, *, budget: int = 12000) -> list[dict]:
    passages, seen, length = [], set(), 0
    for row in con.execute('''SELECT e.quote,d.id document_id,d.title,d.content
        FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
        JOIN sources s ON s.id=d.source_id
        WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0
          AND d.deleted=0 AND s.allow_ai=1 ORDER BY e.id''', (knowledge_id,)):
        content, quote = row['content'], row['quote']
        start = content.find(quote)
        if start < 0:
            continue
        # Follow the article's own citations, rather than searching unrelated
        # work elsewhere in a mixed conversation or a whole source folder.
        before = content.rfind('\n\n', max(0,start-600), start)
        end = content.find('\n\n', start+len(quote), start+len(quote)+1200)
        if end == start+len(quote):
            following = content.find('\n\n', end+2, start+len(quote)+1200)
            end = following if following >= 0 else min(len(content),end+600)
        text = content[max(0,start-300) if before < 0 else before+2:
                       min(len(content),start+len(quote)+600) if end < 0 else end]
        identity = (row['document_id'], text)
        if identity in seen or length+len(text)>budget:
            continue
        seen.add(identity); length += len(text)
        passages.append({'document_id':row['document_id'], 'title':row['title'], 'text':text})
    return passages


def matching_passages(passages: list[dict], query: str, limit: int = 6) -> list[dict]:
    words = re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]{2,}', query.lower())
    terms = set(words)|{w[i:i+2] for w in words for i in range(len(w)-1)}
    scored = [(sum((p['title']+' '+p['text']).lower().count(t) for t in terms),p) for p in passages]
    return [p for score,p in sorted(scored,key=lambda row:row[0],reverse=True) if score][:limit]
