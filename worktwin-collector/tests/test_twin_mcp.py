"""Official MCP client over the real ASGI app; authorization changes are live."""
import asyncio
import json
import re

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from worktwin.api import create_app


def test_mcp_is_bound_to_twin_and_only_returns_current_authorized_notes(tmp_path):
    async def scenario():
        app=create_app(tmp_path/'mcp.sqlite',start_worker=False)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='http://testserver') as owner:
                html=(await owner.get('/')).text
                owner.headers['X-Worktwin-Token']=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',html).group(1)
                note=(await owner.post('/api/knowledge',json={'title':'审批规则','body':'审批统一采用工作流节点。','status':'confirmed'})).json()['id']
                hidden=(await owner.post('/api/knowledge',json={'title':'隐藏项目','body':'另一分身的保密项目。','status':'confirmed'})).json()['id']
                twin=(await owner.post('/api/twins',json={'name':'知识读取分身'})).json()['id']
                await owner.put(f'/api/twins/{twin}/knowledge',json={'knowledge_ids':[note]})
                connection=(await owner.post(f'/api/twins/{twin}/mcp')).json()
                token=connection['token']
                with app.state.db.connect() as con:
                    assert token not in con.execute('SELECT token_hash FROM twin_mcp').fetchone()[0]
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),headers={'Authorization':'Bearer '+token}) as http:
                    async with streamable_http_client('http://testserver/mcp/',http_client=http) as streams:
                        async with ClientSession(streams[0],streams[1]) as session:
                            await session.initialize()
                            tools=(await session.list_tools()).tools
                            assert {t.name for t in tools}=={'list_projects','search_knowledge','read_knowledge','read_source_log'}
                            assert all(t.annotations.read_only_hint for t in tools)
                            result=await session.call_tool('read_knowledge',{'knowledge_id':note})
                            assert not result.is_error and '工作流节点' in str(result.content)
                            assert (await session.call_tool('read_knowledge',{'knowledge_id':hidden})).is_error
                            assert (await session.call_tool('read_source_log',{'document_id':1})).is_error
                            assert (await session.call_tool('delete_knowledge',{'knowledge_id':note})).is_error
                            await owner.post('/api/knowledge/disable',json={'knowledge_ids':[note]})
                            assert (await session.call_tool('read_knowledge',{'knowledge_id':note})).is_error
                            await owner.delete(f'/api/twins/{twin}/mcp')
                            assert (await http.post('http://testserver/mcp/',json={})).status_code==401
                assert (await owner.get(f'/api/twins/{twin}/mcp')).json()['enabled'] is False
                assert (await owner.post('/mcp/',json={})).status_code==401
                replacement=(await owner.post(f'/api/twins/{twin}/mcp')).json()
                legacy={'Authorization':'Bearer '+replacement['token'],'Accept':'application/json, text/event-stream'}
                init=await owner.post('/mcp/',headers=legacy,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'legacy-client','version':'1'}}})
                assert init.status_code==200 and init.json()['result']['protocolVersion']=='2025-11-25'
                legacy['MCP-Protocol-Version']='2025-11-25'
                tools=await owner.post('/mcp/',headers=legacy,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
                assert tools.status_code==200 and len(tools.json()['result']['tools'])==4
                rotated=(await owner.post(f'/api/twins/{twin}/mcp')).json()
                assert rotated['token']!=replacement['token']
                assert (await owner.post('/mcp/',headers=legacy,json={})).status_code==401

    asyncio.run(scenario())


def test_full_logs_need_separate_permission_and_selected_current_citation(tmp_path):
    async def scenario():
        folder=tmp_path/'records';folder.mkdir()
        text='审批统一采用工作流节点。\n日志中还有完整讨论，只能在单独授权后提供。'
        (folder/'decision.md').write_text(text)
        app=create_app(tmp_path/'logs.sqlite',start_worker=False)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='http://testserver') as owner:
                owner.headers['X-Worktwin-Token']=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',(await owner.get('/')).text).group(1)
                sid=(await owner.post('/api/sources',json={'name':'审批项目','root':str(folder),'allow_ai':True})).json()['id']
                app.state.collector.scan_all()
                with app.state.db.connect() as con:
                    did=con.execute('SELECT id FROM documents').fetchone()[0]
                note=(await owner.post('/api/knowledge',json={'title':'审批规则','body':'审批统一采用工作流节点。','status':'confirmed','document_id':did,'quote':'审批统一采用工作流节点。'})).json()['id']
                twin=(await owner.post('/api/twins',json={'name':'审批分身'})).json()['id']
                await owner.put(f'/api/twins/{twin}/knowledge',json={'knowledge_ids':[note]})
                token=(await owner.post(f'/api/twins/{twin}/mcp')).json()['token']
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),headers={'Authorization':'Bearer '+token}) as http:
                    async with streamable_http_client('http://testserver/mcp/',http_client=http) as streams:
                        async with ClientSession(*streams[:2]) as session:
                            await session.initialize()
                            before=await session.call_tool('read_knowledge',{'knowledge_id':note})
                            assert '日志中还有完整讨论' not in str(before.content)
                            assert (await session.call_tool('read_source_log',{'document_id':did})).is_error
                            await owner.put(f'/api/twins/{twin}/mcp/logs',json={'allow_logs':True})
                            after=await session.call_tool('read_source_log',{'document_id':did})
                            assert not after.is_error and '日志中还有完整讨论' in str(after.content)
                            await owner.put(f'/api/twins/{twin}/mcp/logs',json={'allow_logs':False})
                            assert (await session.call_tool('read_source_log',{'document_id':did})).is_error
                            await owner.put(f'/api/twins/{twin}/mcp/logs',json={'allow_logs':True})
                            await owner.put(f'/api/twins/{twin}/knowledge',json={'knowledge_ids':[]})
                            assert (await session.call_tool('read_source_log',{'document_id':did})).is_error
                            await owner.put(f'/api/twins/{twin}/knowledge',json={'knowledge_ids':[note]})
                            await owner.put(f'/api/sources/{sid}/ai',json={'allow_ai':False})
                            assert (await session.call_tool('read_knowledge',{'knowledge_id':note})).is_error
                            assert (await session.call_tool('read_source_log',{'document_id':did})).is_error
    asyncio.run(scenario())
