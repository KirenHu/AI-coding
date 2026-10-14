"""Source passages inherited from an authorized article's current references."""
import re


def source_passages(con, knowledge_id: int, *, query: str = '', budget: int = 12000,
                    include_history: bool = False) -> list[dict]:
    curated=con.execute('SELECT 1 FROM project_documents WHERE knowledge_id=?',(knowledge_id,)).fetchone()
    if curated and not include_history:
        records=[{'document_id':r['document_id'],'title':r['title'],'text':r['quote'],
                  'usage':'current_evidence'} for r in con.execute('''SELECT a.document_id,a.quote,d.title
            FROM article_current_sources a JOIN documents d ON d.id=a.document_id
            JOIN sources s ON s.id=d.source_id WHERE a.knowledge_id=? AND d.deleted=0
              AND s.allow_ai=1 AND a.content_sha=d.sha256 AND instr(d.content,a.quote)>0
              AND EXISTS(SELECT 1 FROM knowledge_evidence e WHERE e.knowledge_id=a.knowledge_id
                AND e.document_id=a.document_id AND e.quote=a.quote AND e.is_current=1 AND e.superseded=0)''',(knowledge_id,))]
        matches=matching_passages(records,query,limit=12) if query else records
        result=[]
        for record in matches:
            if sum(len(p['text']) for p in result)+len(record['text'])<=budget:
                result.append(record)
        return result
    passages, seen, length = [], set(), 0
    records=list(con.execute('''SELECT e.quote,d.id document_id,d.title,d.content
        FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
        JOIN sources s ON s.id=d.source_id
        WHERE e.knowledge_id=? AND (e.superseded=0 OR ?=1) AND e.is_current=1
          AND d.deleted=0 AND s.allow_ai=1 ORDER BY e.id''', (knowledge_id,int(include_history))))
    if query:
        documents={r['document_id']:r for r in records if r['quote'] in r['content']}
        candidates=[]
        for row in documents.values():
            for paragraph in row['content'].split('\n\n'):
                for start in range(0,len(paragraph),1800):
                    candidates.append({'document_id':row['document_id'],'title':row['title'],
                                       'text':paragraph[start:start+1800],
                                       'usage':'original_history' if include_history else 'source_context'})
        for passage in matching_passages(candidates,query,limit=12):
            if length+len(passage['text'])<=budget:
                passages.append(passage);length+=len(passage['text'])
        return passages
    for row in records:
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


def source_versions(con,knowledge_id:int) -> list[tuple]:
    return [tuple(row) for row in con.execute('''SELECT DISTINCT d.id,d.sha256
        FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
        WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0
          AND d.deleted=0 ORDER BY d.id''',(knowledge_id,))]


def matching_passages(passages: list[dict], query: str, limit: int = 6) -> list[dict]:
    words = re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]{2,}', query.lower())
    terms = set(words)|{w[i:i+2] for w in words for i in range(len(w)-1)}
    scored = [(sum((p['title']+' '+p['text']).lower().count(t) for t in terms),p) for p in passages]
    return [p for score,p in sorted(scored,key=lambda row:row[0],reverse=True) if score][:limit]
