"""Read-only local MCP. Each credential is bound to one live twin grant.

The official SDK implements protocol negotiation and both modern and legacy
Streamable HTTP clients. Knowledge and logs have distinct authorization.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from contextvars import ContextVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from .twin_access import effective_notes

credential = ContextVar('worktwin_mcp_credential',default='')


def grant(con, token):
    return con.execute('''SELECT m.* FROM twin_mcp m JOIN twins t ON t.id=m.twin_id
        WHERE m.token_hash=? AND m.enabled=1''',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone() if token else None


def issue_connection(con,twin_id):
    if not con.execute('SELECT id FROM twins WHERE id=?',(twin_id,)).fetchone():
        raise LookupError('数字分身不存在')
    token=secrets.token_urlsafe(32)
    con.execute('''INSERT INTO twin_mcp(twin_id,token_hash) VALUES(?,?)
        ON CONFLICT(twin_id) DO UPDATE SET token_hash=excluded.token_hash,enabled=1,
        updated_at=datetime('now')''',(twin_id,hashlib.sha256(token.encode()).hexdigest()))
    return token


def authorized_notes(con):
    access=grant(con,credential.get())
    if not access:
        raise ToolError('MCP 连接已关闭或凭据已失效')
    rows=sorted(effective_notes(con,access['twin_id']),key=lambda k:(k['updated_at'],k['id']),reverse=True)
    return access,rows


def create_server(db):
    server=MCPServer('WorkTwin',instructions='按项目检索此数字分身被授权的当前有效知识。引用知识编号与来源。范围不明确时先澄清；完整日志需要独立授权。')
    annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)

    @server.tool(annotations=annotations)
    def list_projects() -> list[dict]:
        """List only projects visible through this twin's currently usable notes."""
        with db.connect() as con:
            _,notes=authorized_notes(con)
            groups={}
            for k in notes:
                key=k['project_key'] if k['scope']!='global' else ''
                item=groups.setdefault(key,dict(project_key=key,name=k['project'] or '通用知识',scope=k['scope'],knowledge_count=0))
                item['knowledge_count']+=1
            return list(groups.values())

    @server.tool(annotations=annotations)
    def search_knowledge(query:str, project_key:str='', limit:int=10) -> list[dict]:
        """Search current notes in a selected project. Empty project selects global notes only."""
        if not query.strip() or len(query)>500 or not 1<=limit<=30:
            raise ToolError('请输入有效查询，返回数量为 1–30')
        with db.connect() as con:
            _,notes=authorized_notes(con)
            notes=[k for k in notes if k['scope']=='global' or (project_key and k['project_key']==project_key)]
            words=re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]{2,}',query.lower())
            terms=set(words)|{word[i:i+2] for word in words for i in range(len(word)-1)}
            for k in notes:
                k['_score']=sum((k['title']+' '+k['topic']+' '+k['body']).lower().count(t) for t in terms)
            notes=sorted((k for k in notes if k['_score']),key=lambda k:k['_score'],reverse=True)[:limit]
            return [{key:k[key] for key in ('id','title','topic','project','project_key','scope','scope_detail','version')}|{'excerpt':k['body'][:600]} for k in notes]

    @server.tool(annotations=annotations)
    def read_knowledge(knowledge_id:int) -> dict:
        """Read one current authorized note and necessary citations. Does not return old versions or complete logs."""
        with db.connect() as con:
            _,notes=authorized_notes(con)
            k=next((k for k in notes if k['id']==knowledge_id),None)
            if k is None:
                raise ToolError('该知识不存在、不可用或未授权给此分身')
            evidence=[dict(e) for e in con.execute('''SELECT e.document_id,d.title, e.quote,e.occurred_at
                FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
                WHERE e.knowledge_id=? AND e.is_current=1 AND e.superseded=0 AND d.deleted=0
                ORDER BY e.id DESC LIMIT 12''',(knowledge_id,))]
            return {key:k[key] for key in ('id','title','body','topic','scope','project','project_key','scope_detail','version','updated_at')}|{'citations':evidence}

    @server.tool(annotations=annotations)
    def read_source_log(document_id:int, offset:int=0, limit:int=12000) -> dict:
        """Read a cited source log only with separate full-log authorization. Pages are bounded to 12,000 characters."""
        if offset<0 or not 1<=limit<=12000:
            raise ToolError('读取范围无效')
        with db.connect() as con:
            access,notes=authorized_notes(con)
            if not access['allow_logs']:
                raise ToolError('此分身尚未获得完整日志授权；必要引用已在知识中提供')
            allowed={k['id'] for k in notes}
            ids={r[0] for r in con.execute('''SELECT e.knowledge_id FROM knowledge_evidence e
                JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
                WHERE e.document_id=? AND e.is_current=1 AND e.superseded=0
                AND d.deleted=0 AND s.allow_ai=1''',(document_id,))}
            if not allowed & ids:
                raise ToolError('此资料未关联到分身当前可用的授权知识')
            d=con.execute('SELECT title,content FROM documents WHERE id=?',(document_id,)).fetchone()
            end=offset+limit
            return dict(document_id=document_id,title=d['title'],content=d['content'][offset:end],offset=offset,
                        next_offset=end if end<len(d['content']) else None,total_chars=len(d['content']))

    mcp_app=server.streamable_http_app(streamable_http_path='/',stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=['127.0.0.1:*','localhost:*','testserver'],allowed_origins=['http://127.0.0.1:*','http://localhost:*']))

    class CredentialGuard:
        async def __call__(self,scope,receive,send):
            if scope['type']!='http':
                return await mcp_app(scope,receive,send)
            headers=dict(scope['headers'])
            auth=headers.get(b'authorization',b'').decode('latin1')
            token=auth[7:] if auth.startswith('Bearer ') else ''
            with db.connect() as con:
                valid=grant(con,token)
            if not valid:
                return await JSONResponse({'detail':'MCP 凭据缺失或已撤销'},status_code=401,headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
            context=credential.set(token)
            try:
                await mcp_app(scope,receive,send)
            finally:
                credential.reset(context)

    return server,CredentialGuard()
