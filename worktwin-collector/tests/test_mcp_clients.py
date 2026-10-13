import asyncio
import json
import os
import re
import socket
import sys
import threading
import time

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from worktwin.api import create_app
from worktwin.mcp_clients import configurations, install


def test_client_configs_merge_existing_and_keep_backup(tmp_path,monkeypatch):
    path=tmp_path/'client.json';path.write_text('{"other":true,"mcpServers":{"existing":{"command":"example"}}}')
    monkeypatch.setattr('worktwin.mcp_clients.client_paths',lambda:{'cursor':path})
    config=configurations(3,'http://127.0.0.1:8765/mcp/','test-secret')
    install('cursor',config['cursor'])
    data=json.loads(path.read_text())
    assert data['other'] and 'existing' in data['mcpServers'] and 'worktwin-3' in data['mcpServers']
    assert list(tmp_path.glob('*.bak')) and path.stat().st_mode&0o777==0o600
    assert 'servers' in config['vscode'] and 'env' in config['claude']['mcpServers']['worktwin-3']


def test_bundled_stdio_bridge_initializes_lists_and_reads(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    app=create_app(tmp_path/'mcp.sqlite',start_worker=False)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}') as owner:
            owner.headers['X-Worktwin-Token']=re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";',owner.get('/').text).group(1)
            note=owner.post('/api/knowledge',json={'title':'桥接验收','body':'只返回已授权的当前知识。','status':'confirmed'}).json()['id']
            twin=owner.post('/api/twins',json={'name':'验收分身'}).json()['id']
            owner.put(f'/api/twins/{twin}/knowledge',json={'knowledge_ids':[note]})
            token=owner.post(f'/api/twins/{twin}/mcp').json()['token']
            async def scenario():
                params=StdioServerParameters(command=sys.executable,args=['-m','worktwin','mcp-bridge'],env={**os.environ,'WORKTWIN_MCP_URL':f'http://127.0.0.1:{port}/mcp/','WORKTWIN_MCP_TOKEN':token})
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        assert len((await session.list_tools()).tools)==5
                        result=await session.call_tool('read_knowledge',{'knowledge_id':note})
                        assert not result.is_error and '当前知识' in str(result.content)
            asyncio.run(scenario())
    finally:
        server.should_exit=True;thread.join(timeout=5)
