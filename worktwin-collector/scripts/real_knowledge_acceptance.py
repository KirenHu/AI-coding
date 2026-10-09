"""Paid, opt-in acceptance using actual user statements from this project.

Requires WORKTWIN_ACCEPTANCE_KEY. This creates no receipt in an installed
user database: a small real discussion is not broad-corpus acceptance.
"""
import hashlib
import json
import os
import sys
import re
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]))
from worktwin.inference import extract_knowledge
from worktwin.model_settings import PersonalModel
from worktwin.reconcile import make_consolidation_plan
from worktwin.api import create_app
from worktwin.knowledge import store_candidates

SOURCE='''工作目录：/workspace/AI-coding
### 用户 · 2026-10-09T08:47:00Z
知识整理和呈现的方式可以参考obisidian，我很喜欢他们的思路
### 用户 · 2026-10-09T09:54:40Z
同意，无冲突的新增、补充自动生效；改变已有结论、项目不明或出现矛盾时提醒核对。自动生效前先通过真实资料验收；obsidian只是学习一下人家的部分优秀思路，没让你照着做；“旧知识”的定义似乎并不清晰，什么叫“旧知识”？是已经被更新了的原有知识？那旧知识应该只存在在日志中，知识库中不应该继续存在而应该被覆盖；MCP我觉得可以沿用“数字分身”的权限，所以实际上是支持为数字分身配置MCP，然后能力是只读有效知识和必要引用，完整日志单独授权。
'''


def main():
    key=os.environ.get('WORKTWIN_ACCEPTANCE_KEY','')
    if not key:
        raise SystemExit('需要显式设置 WORKTWIN_ACCEPTANCE_KEY；不读取或输出用户保存的凭据')
    client=PersonalModel(os.environ.get('WORKTWIN_ACCEPTANCE_URL','https://api.deepseek.com/v1'),key,
                         os.environ.get('WORKTWIN_ACCEPTANCE_MODEL','deepseek-chat'))
    client.chat([{'role':'user','content':'请只回复 OK。'}],max_tokens=64)
    items=extract_knowledge(SOURCE,transcript=True,client=client,
        scope={'scope':'project','project':'WorkTwin','project_key':'worktwin-acceptance'})
    corpus='\n'.join(k['body'] for k in items)
    checks={'connection':True,'nonempty':bool(items),
        'grounding':all(k['quote'] in SOURCE for k in items),
        'user_attribution':all(k['attribution']=='user' for k in items),
        'automation_gate':'验收' in corpus and '自动' in corpus,
        'mcp_twin_permission':'MCP' in corpus and '分身' in corpus and '日志' in corpus,
        'current_only':bool(items) and ('覆盖' in corpus or '当前' in corpus or '替代' in corpus or '最新' in corpus),
        'obsidian_reference_only':bool(items) and ('参考' in corpus or '借鉴' in corpus or '学习' in corpus)}
    # This actual policy supersedes the previous product boundary document.
    policy=next((i for i in items if '自动' in i['body'] and '验收' in i['body']),None)
    if policy:
        existing=[{'id':1,'kind':'decision','title':'知识生效机制','topic':policy['topic'],
            'scope_detail':policy['scope_detail'],'body':'一期保留已建立的内容确认机制，更新既有结论通过提案核对后生效。'}]
        plan=make_consolidation_plan(client,[policy],existing)
        checks['policy_change_needs_review']=bool(plan) and plan[0]['action'] in ('replace','conflict')
    else:
        checks['policy_change_needs_review']=False
    if all(checks.values()):
        with tempfile.TemporaryDirectory(prefix='worktwin-real-pipeline-') as folder:
            root=Path(folder);source=root/'records';source.mkdir();(source/'discussion.md').write_text(SOURCE)
            app=create_app(root/'db.sqlite',start_worker=False,inference_client=client)
            with TestClient(app) as owner:
                auth={'X-Worktwin-Token':re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',owner.get('/').text).group(1)}
                assert owner.post('/api/sources',headers=auth,json={'name':'WorkTwin真实讨论','root':str(source),'allow_ai':True}).status_code==200
                app.state.collector.scan_all()
                did=owner.get('/api/documents',headers=auth).json()[0]['id']
                assert owner.put(f'/api/documents/{did}/scope',headers=auth,json={'project':'WorkTwin'}).status_code==200
                with app.state.db.connect() as con:
                    assert store_candidates(con,did,[],items,created_by='enterprise_ai')==len(items)
                notes=owner.get('/api/knowledge',headers=auth).json()
                # Expected current product requirements were confirmed by the
                # real user; approve these checked sample notes as the owner.
                for note in notes:
                    payload={key:note[key] for key in ('kind','title','body','scope','project','project_key','topic','scope_detail','quality')}
                    payload['status']='confirmed'
                    assert owner.put(f"/api/knowledge/{note['id']}",headers=auth,json=payload).status_code==200
                tid=owner.post('/api/twins',headers=auth,json={'name':'WorkTwin产品知识'}).json()['id']
                assert owner.put(f'/api/twins/{tid}/knowledge',headers=auth,json={'knowledge_ids':[note['id'] for note in notes]}).status_code==200
                connection=owner.post(f'/api/twins/{tid}/mcp',headers=auth).json()
                mcp_headers={'Authorization':'Bearer '+connection['token'],'Accept':'application/json, text/event-stream'}
                initialized=owner.post('/mcp/',headers=mcp_headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
                    'params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'real-acceptance','version':'1'}}})
                assert initialized.status_code==200
                mcp_headers['MCP-Protocol-Version']='2025-11-25'
                reply=owner.post('/mcp/',headers=mcp_headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call',
                    'params':{'name':'read_knowledge','arguments':{'knowledge_id':notes[0]['id']}}})
                assert reply.status_code==200 and not reply.json()['result'].get('isError',False)
                result=reply.json()['result']
                returned=[json.loads(block['text']) for block in result['content'] if block.get('type')=='text']
                assert any(data.get('body')==notes[0]['body'] or data.get('result',{}).get('body')==notes[0]['body'] for data in returned)
                assert not owner.get('/api/settings',headers=auth).json()['knowledge_automation']['ready']
                checks['storage_and_mcp']=True
    else:
        checks['storage_and_mcp']=False
    report={'real_data':True,'dataset_sha256':hashlib.sha256(SOURCE.encode()).hexdigest(),
        'model':client.model,'checks':checks,'items':items,'result':'PASS' if all(checks.values()) else 'FAIL',
        'automation_enabled':False,'coverage':'本轮真实产品讨论的有限样本；不代表完整本地资料库验收'}
    path=Path(os.environ.get('WORKTWIN_ACCEPTANCE_REPORT','/tmp/worktwin-real-knowledge-acceptance.json'))
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:report[k] for k in ('result','model','checks','automation_enabled','coverage')},ensure_ascii=False))
    if report['result']!='PASS':
        raise SystemExit(1)


if __name__=='__main__':
    main()
