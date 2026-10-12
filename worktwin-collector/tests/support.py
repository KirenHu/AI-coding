"""Deterministic OpenAI-compatible stand-in for offline CI. Never used by the app."""
import json
import re
from worktwin.jobs import KnowledgeWorker
from worktwin.knowledge import visible_turns


class FakeModel:
    configured=True

    def __init__(self):
        self.requests=[]

    def chat(self,messages,max_tokens=2400):
        self.requests.append(messages)
        body=messages[-1]['content']
        if '<source>\n' not in body and body.lstrip().startswith('{'):
            payload=json.loads(body)
            if 'materials' in payload and 'articles' in payload:
                groups={}
                for material in payload['materials']:
                    topic=material['topic']
                    group=groups.setdefault(topic,{'topic':topic,'material_ids':[],
                        'article_ids':[a['id'] for a in payload['articles'] if a['topic']==topic],
                        'defer':False})
                    group['material_ids'].append(material['id'])
                return json.dumps({'items':list(groups.values())},ensure_ascii=False)
            if 'source_materials' in payload:
                materials=payload['source_materials']
                existing=payload['existing_articles']
                paragraphs=list(dict.fromkeys(m['body'] for m in materials))
                title=existing[0]['title'] if existing else materials[0]['title']
                overview=f"本文说明{payload['topic']}，适用于{materials[0]['scope_detail']}。"
                article_body='## 工作事项\n\n'+'\n\n'.join(paragraphs)
                article_body+='\n\n## 适用条件\n\n'+materials[0]['scope_detail']
                return json.dumps({'items':[{'complete':True,'unchanged':False,
                    'unresolved_conflict':False,'kind':'decision','title':title,
                    'topic':payload['topic'],'scope_detail':materials[0]['scope_detail'],
                    'article_summary':overview,'body':article_body,
                    'change_summary':'补充工作要求及其依据' if existing else '形成工作事项说明'}]},ensure_ascii=False)
        if '"batch_items"' in body or '"new_items"' in body:
            return '{"items":[]}'
        if '<source>\n' not in body:
            return '这是根据 '+(re.search(r'\[K\d+\]',body).group(0) if re.search(r'\[K\d+\]',body) else '[K1]')+' 已授权知识生成的回答。'
        source=body.split('<source>\n',1)[1].split('\n</source>',1)[0]
        candidates=[]
        turns=visible_turns(source)
        attributable='\n'.join(t['text'] for t in turns if t['role']=='user') if turns else source
        for text in re.split(r'[。！\n]',attributable):
            text=text.strip()
            if len(text)<8:
                continue
            if re.search(r'决定|最终|采用|选择',text):
                kind='decision'
            elif re.search(r'倾向|习惯|希望|优先考虑',text):
                kind='preference'
            elif re.search(r'流程|步骤|操作',text):
                kind='process'
            else:
                continue
            quote=text + ('。' if text+'。' in source else '')
            candidates.append({'kind':kind,'title':quote[:45],'body':'可复用的工作知识：'+quote,'quote':quote,
                'topic': '工作要求' if kind=='preference' else '工作方案',
                'scope_detail':'仅适用于本资料描述的工作', 'value_reason':'用于查阅本项工作的要求和方案依据',
                'attribution':'user' if turns else 'document','outcome':'none'})
        return json.dumps({'items':candidates[:5]},ensure_ascii=False)


def run_ai(db, count=20):
    fake=FakeModel()
    worker=KnowledgeWorker(db,client=fake)
    for _ in range(count):
        result=worker.process_next()
        if result['state']=='idle':
            break
        assert result['state']=='done',result
    return fake


def run_ai_article(db, count=20, *, model=None):
    """Explicitly run capture extraction and the separate article worker."""
    fake=model or FakeModel()
    worker=KnowledgeWorker(db,client=fake)
    for _ in range(count):
        result=worker.process_next()
        if result['state']=='idle':
            break
        assert result['state']=='done',result
    for _ in range(count):
        result=worker.article_maintainer.process_next()
        if result['state']=='idle':
            break
        assert result['state']=='done',result
    return fake
