"""Client-specific local MCP configuration and a bundled stdio bridge."""
import json
import os
import sys
from pathlib import Path


def configurations(twin_id, url, token):
    name=f'worktwin-{twin_id}'
    http={'url':url,'headers':{'Authorization':'Bearer '+token}}
    command=sys.executable
    args=['mcp-bridge'] if getattr(sys,'frozen',False) else ['-m','worktwin','mcp-bridge']
    return {
        'cursor':{'mcpServers':{name:http}},
        'vscode':{'servers':{name:{'type':'http',**http}}},
        'claude':{'mcpServers':{name:{'command':command,'args':args,'env':{'WORKTWIN_MCP_URL':url,'WORKTWIN_MCP_TOKEN':token}}}},
    }


def client_paths():
    home=Path.home()
    support=home/'Library/Application Support' if sys.platform=='darwin' else Path(os.environ.get('APPDATA',home/'.config'))
    return {'cursor':home/'.cursor/mcp.json', 'vscode':support/'Code/User/mcp.json',
            'claude':support/'Claude/claude_desktop_config.json'}


def install(client, config):
    path=client_paths()[client]
    data=json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data,dict):raise ValueError('现有配置不是 JSON 对象')
    for section, servers in config.items():
        if not isinstance(data.get(section,{}),dict):raise ValueError('现有服务器配置格式不兼容')
        data.setdefault(section,{}).update(servers)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        import shutil,time
        shutil.copy2(path,path.with_name(path.name+'.worktwin-'+str(time.time_ns())+'.bak'))
    temp=path.with_name(path.name+'.worktwin.tmp')
    with open(temp,'w',encoding='utf-8',opener=lambda p,f:os.open(p,f,0o600)) as out:
        json.dump(data,out,ensure_ascii=False,indent=2)
    os.replace(temp,path)
    return str(path)


async def bridge():
    import httpx2
    from urllib.parse import urlparse
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    url=os.environ['WORKTWIN_MCP_URL']
    if urlparse(url).hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('WorkTwin bridge requires a local endpoint')
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+os.environ['WORKTWIN_MCP_TOKEN']}) as http:
        async with streamable_http_client(url,http_client=http) as streams:
            async with ClientSession(streams[0],streams[1]) as remote:
                await remote.initialize()
                async def listing(context,params):
                    return await remote.list_tools()
                async def calling(context,params):
                    return await remote.call_tool(params.name,params.arguments or {})
                server=Server('WorkTwin',on_list_tools=listing,on_call_tool=calling)
                async with stdio_server() as (read,write):
                    await server.run(read,write,server.create_initialization_options())
