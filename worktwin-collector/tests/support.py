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
